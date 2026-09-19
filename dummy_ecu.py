#!/usr/bin/env python3
"""
Dummy UDS ECU (ISO 14229-1 over ISO 15765-2) for trying CAN Expert without a vehicle.

Kvaser's Virtual CAN Driver provides two connected channels. Run the ECU on one and
connect CAN Expert to the other:

    python dummy_ecu.py                           (the Dummy ECU window: settings, Connect, log)
    python dummy_ecu.py --console --channel 1     (no window)
    (CAN Expert: receiver "[kvaser] Ch 0", configuration SERVER ID 7E0, ECU ID 7E8)

What it simulates (each value is a setting in the window, or in a profile saved from it):
- Sessions (0x10, announcing P2/P2*), TesterPresent (0x3E), ECUReset (0x11), S3 session timeout,
  a processing delay answered with response pending (NRC 0x78) beyond P2
- ReadDataByIdentifier (0x22) / WriteDataByIdentifier (0x2E), including multi-frame VIN
- SecurityAccess (0x27: level, seed length, key = seed XOR mask, attempt counter and lockout delay)
- ControlDTCSetting (0x85), CommunicationControl (0x28), ReadDTCInformation (0x19), ClearDTC (0x14)
- Flashing: RoutineControl erase / checkProgrammingDependencies (0x31), RequestDownload (0x34),
  RequestUpload (0x35), TransferData (0x36), RequestTransferExit (0x37), with the accepted data and
  address/length formats, maxNumberOfBlockLength, full blocks and memory ranges
- ISO-TP flow control on segmented requests: block size and STmin, optional WAIT frames, overflow for
  requests longer than the receive buffer
- Periodic application frames: 0x300 temperature/pressure, 0x301 status; commands on 0x200/0x201
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import zlib
from dataclasses import asdict, dataclass, field, fields, replace

import can

from uds_services import FC_OVERFLOW, FC_WAIT, N_CR_TIMEOUT, flow_control_frame, isotp_send, parse_first_frame

DEFAULT_SESSION, PROGRAMMING_SESSION, EXTENDED_SESSION = 0x01, 0x02, 0x03
SESSION_NAMES = {DEFAULT_SESSION: "default", PROGRAMMING_SESSION: "programming", EXTENDED_SESSION: "extended"}
# Negative responses an ECU must not send to functionally addressed requests (ISO 14229-1 7.5).
SUPPRESSED_FUNCTIONAL_NRCS = {0x11, 0x12, 0x31, 0x7E, 0x7F}
SERVICE_NAMES = {
    0x10: "DiagnosticSessionControl", 0x11: "ECUReset", 0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation", 0x22: "ReadDataByIdentifier", 0x27: "SecurityAccess",
    0x28: "CommunicationControl", 0x2E: "WriteDataByIdentifier", 0x31: "RoutineControl",
    0x34: "RequestDownload", 0x35: "RequestUpload", 0x36: "TransferData", 0x37: "RequestTransferExit",
    0x3E: "TesterPresent", 0x85: "ControlDTCSetting",
}
DEFAULT_CONNECTION = {"interface": "kvaser", "channel": "1", "bitrate": 500000}


class NegativeResponse(Exception):
    def __init__(self, nrc: int):
        super().__init__(f"NRC 0x{nrc:02X}")
        self.nrc = nrc


@dataclass
class EcuConfig:
    """How the ECU answers. The running ECU reads it for every frame, so changes apply at once."""
    # Addressing
    request_id: int = 0x7E0            # physical requests (tester -> ECU)
    functional_id: int = 0x7DF         # functional requests (tester -> every ECU)
    response_id: int = 0x7E8           # this ECU's responses (ECU -> tester)
    extended_ids: bool = False         # 29-bit identifiers
    address_byte: int | None = None    # ISO-TP extended addressing byte
    padding: int | None = 0xAA         # fills frames up to 8 bytes (None: frames as short as their content)
    # ISO-TP flow control on segmented requests
    block_size: int = 8                # BS: consecutive frames per block (0 = no limit)
    st_min: int = 0x01                 # STmin byte: 0x00-0x7F ms, 0xF1-0xF9 100-900 us
    flow_waits: int = 0                # WAIT frames sent before each ContinueToSend
    wait_interval_ms: int = 100        # time between those frames (the tester waits up to N_Bs, 1 s)
    rx_buffer: int = 0                 # longest request accepted, 0 = maxNumberOfBlockLength; longer: overflow
    # UDS timing and security
    p2_ms: int = 50                    # P2server_max, announced in the DiagnosticSessionControl response
    p2_star_ms: int = 5000             # P2*server_max, likewise
    response_delay_ms: int = 0         # processing time per request; beyond P2 the ECU sends NRC 0x78 first
    pending_interval: float = 2.0      # seconds between NRC 0x78 while busy (within P2*)
    s3_timeout: float = 5.0            # back to the default session after this long without requests
    programming_needs_extended: bool = True
    security_level: int = 0x01         # requestSeed sub-function (odd); sendKey is the next one
    seed_length: int = 4
    key_mask: int = 0xA5               # key = seed XOR key_mask, byte by byte
    max_attempts: int = 3              # wrong keys before the lockout delay
    lockout_seconds: float = 10.0
    # Flashing
    data_formats: tuple = (0x00,)      # dataFormatIdentifier values accepted (0x00: no compression/encryption)
    address_format: int | None = None  # required addressAndLengthFormatIdentifier (None: any)
    max_block_length: int = 0x402      # maxNumberOfBlockLength: TransferData length including SID and counter
    block_length_bytes: int = 2        # bytes used for maxNumberOfBlockLength in the response
    full_blocks: bool = False          # every TransferData but the last must carry max_block_length - 2 bytes
    memory_ranges: tuple = ()          # ((first, last), ...) addresses open to erase/download/upload; () = any
    require_erase: bool = True
    erase_routine: int = 0xFF00
    check_routine: int = 0xFF01
    erase_seconds: float = 1.0
    allow_upload: bool = True          # RequestUpload (0x35) reads the memory back
    # Application traffic and output
    broadcast_interval: float = 0.1    # 0x300/0x301 period in seconds, 0 = off
    dump_path: str | None = None       # the flashed image is written here as S-records

    @property
    def receive_buffer(self) -> int:
        return self.rx_buffer or self.max_block_length


def config_from_dict(values: dict) -> EcuConfig:
    """EcuConfig from saved values; unknown names are ignored, missing ones keep their default."""
    names = {item.name for item in fields(EcuConfig)}
    kwargs = {name: value for name, value in values.items() if name in names}
    if "data_formats" in kwargs:
        kwargs["data_formats"] = tuple(kwargs["data_formats"])
    if "memory_ranges" in kwargs:
        kwargs["memory_ranges"] = tuple(tuple(pair) for pair in kwargs["memory_ranges"])
    return EcuConfig(**kwargs)


def load_profile(path) -> tuple[EcuConfig, dict]:
    """(settings, connection) from a profile saved by the window."""
    with open(path, encoding="utf-8") as handle:
        values = json.load(handle)
    return config_from_dict(values.get("ecu", {})), dict(values.get("connection", {}))


def save_profile(path, config: EcuConfig, connection: dict):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"connection": connection, "ecu": asdict(config)}, handle, indent=2)


def parse_channel(text):
    """Kvaser and most interfaces number their channels; virtual buses use names."""
    text = str(text).strip()
    return int(text) if text.isdigit() else text


@dataclass
class EcuState:
    session: int = DEFAULT_SESSION
    unlocked: bool = False
    seed: bytes | None = None
    failed_attempts: int = 0
    locked_until: float = 0.0
    dtc_setting_on: bool = True
    communication_enabled: bool = True
    erased: list = field(default_factory=list)          # [(start, end)], end exclusive
    transfer: dict | None = None                         # the download or upload in progress
    memory: dict = field(default_factory=dict)            # address -> bytes of each completed download
    last_request: float = field(default_factory=time.monotonic)
    rebooting_until: float = 0.0


class _FrameTap:
    """The ECU's view of its bus: diagnostic frames also go to DummyEcu.trace when it is set."""

    def __init__(self, ecu):
        self.ecu = ecu

    def send(self, message, timeout=None):
        self.ecu.bus.send(message, timeout)
        self.ecu._trace("Tx", message)

    def recv(self, timeout=None):
        message = self.ecu.bus.recv(timeout)
        if message is not None:
            self.ecu._trace("Rx", message)
        return message


class DummyEcu:
    def __init__(self, bus: can.BusABC | None, config: EcuConfig | None = None, log=print):
        self.bus = bus
        self.config = config or EcuConfig()
        self.log = log
        self.trace = None       # trace(direction, message) for every diagnostic frame, when set
        self._io = _FrameTap(self)
        self._started = time.monotonic()
        self.power_on()

    def power_on(self):
        """Factory state: default session, original DIDs and DTCs, erased memory."""
        self.state = EcuState()
        self.dids = {
            0xF187: b"CANEXPERT-DUMMY",           # spare part number
            0xF18C: b"SN000123456",               # ECU serial number
            0xF190: b"WVWZZZ1KZAW000001",         # VIN (writable, 17 bytes)
            0xF195: b"APP-1.0.0",                 # software version (changes after flashing)
        }
        self.dtcs = {0x010100: 0x09, 0xC10000: 0x08}   # DTC -> status (P0101, U0100)
        self.running = False
        self.logging = False
        self.temperature = 21.5
        self.counter = 0
        self._rx = None

    # --- transport ---------------------------------------------------------------

    def _trace(self, direction, message):
        config = self.config
        if self.trace and message.arbitration_id in (config.request_id, config.functional_id, config.response_id):
            self.trace(direction, message)

    def _payload(self, message: can.Message) -> bytes | None:
        data = bytes(message.data)
        if self.config.address_byte is not None:
            if not data or data[0] != self.config.address_byte:
                return None
            data = data[1:]
        return data or None

    def _send_frame(self, body: bytes):
        data = bytes(body)
        if self.config.address_byte is not None:
            data = bytes([self.config.address_byte]) + data
        if self.config.padding is not None:
            data = data.ljust(8, bytes([self.config.padding]))
        self._io.send(can.Message(arbitration_id=self.config.response_id, data=data,
                                  is_extended_id=self.config.extended_ids))

    def respond(self, payload: bytes):
        isotp_send(self._io, self.config.response_id, payload, self.config.request_id,
                   self.config.extended_ids, self.config.address_byte, self.config.padding)

    def on_message(self, message: can.Message):
        if message.is_error_frame or message.is_remote_frame:
            return
        diagnostic = message.arbitration_id in (self.config.request_id, self.config.functional_id)
        if not diagnostic or bool(message.is_extended_id) != self.config.extended_ids:
            self._application_frame(message)
            return
        functional = message.arbitration_id == self.config.functional_id
        data = self._payload(message)
        if data is None:
            return
        kind = data[0] >> 4
        if kind == 0x0:
            length = data[0] & 0x0F
            if 0 < length <= len(data) - 1:
                self._handle(data[1:1 + length], functional)
        elif kind == 0x1 and not functional:  # segmented requests are physical only
            first = parse_first_frame(data, 7 - (self.config.address_byte is not None))
            if first is None:
                return
            self._rx = None
            if first[0] > self.config.receive_buffer:
                self.log(f"ISO-TP: {first[0]}-byte request exceeds the {self.config.receive_buffer}-byte "
                         f"receive buffer; flow control overflow")
                self._send_frame(flow_control_frame(status=FC_OVERFLOW))
                return
            self._rx = {"total": first[0], "data": bytearray(first[1]), "next": 1, "left": self.config.block_size}
            self._continue_to_send()
            self._rx["deadline"] = time.monotonic() + N_CR_TIMEOUT
        elif kind == 0x2 and not functional and self._rx:
            if time.monotonic() > self._rx["deadline"]:
                self.log("ISO-TP: consecutive frame too late (N_Cr); message dropped")
                self._rx = None
                return
            if data[0] & 0x0F != self._rx["next"]:
                self.log("ISO-TP: consecutive frame out of sequence; message dropped")
                self._rx = None
                return
            self._rx["data"] += data[1:]
            if len(self._rx["data"]) >= self._rx["total"]:
                request = bytes(self._rx["data"][:self._rx["total"]])
                self._rx = None
                self._handle(request, False)
                return
            self._rx["next"] = (self._rx["next"] + 1) & 0x0F
            self._rx["left"] -= 1
            if self._rx["left"] == 0:  # block complete: the tester waits for the next flow control
                self._rx["left"] = self.config.block_size
                self._continue_to_send()
            self._rx["deadline"] = time.monotonic() + N_CR_TIMEOUT

    def _continue_to_send(self):
        """Flow control for the next block, after the configured WAIT frames."""
        for _ in range(self.config.flow_waits):
            self._send_frame(flow_control_frame(status=FC_WAIT))
            time.sleep(self.config.wait_interval_ms / 1000)
        self._send_frame(flow_control_frame(self.config.block_size, self.config.st_min))

    # --- UDS ---------------------------------------------------------------------

    def _handle(self, request: bytes, functional: bool):
        now = time.monotonic()
        if now < self.state.rebooting_until:
            return
        self.check_session_timeout(now)
        self.state.last_request = now
        sid = request[0]
        name = SERVICE_NAMES.get(sid, f"service 0x{sid:02X}")
        handler = getattr(self, f"_service_{sid:02x}", None)
        try:
            if handler is None:
                raise NegativeResponse(0x11)
            self._busy(self.config.response_delay_ms / 1000, sid)
            reply = handler(request)
        except NegativeResponse as exc:
            self.log(f"<- {name} {request[:8].hex(' ')}{'...' if len(request) > 8 else ''}  => NRC 0x{exc.nrc:02X}")
            if not (functional and exc.nrc in SUPPRESSED_FUNCTIONAL_NRCS):
                self.respond(bytes([0x7F, sid, exc.nrc]))
            return
        suppressed = reply is None
        if sid != 0x3E:
            self.log(f"<- {name} {request[:8].hex(' ')}{'...' if len(request) > 8 else ''}"
                     f"  => {'(suppressed)' if suppressed else reply[:8].hex(' ')}")
        if not suppressed:
            self.respond(reply)
        if sid == 0x11:
            self._reset()

    def _busy(self, seconds: float, sid: int, pending: bool = False):
        """Spend `seconds` on a request. Beyond P2 (or with pending=True) the tester is kept waiting with
        NRC 0x78 (response pending), repeated every pending_interval."""
        if seconds <= 0:
            return
        if not pending and seconds <= self.config.p2_ms / 1000:
            time.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while True:
            self.respond(bytes([0x7F, sid, 0x78]))
            time.sleep(max(0.0, min(self.config.pending_interval, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                return

    @staticmethod
    def _subfunction(request: bytes, minimum_length: int = 2):
        if len(request) < minimum_length:
            raise NegativeResponse(0x13)
        return request[1] & 0x7F, bool(request[1] & 0x80)

    def _require_session(self, *sessions):
        if self.state.session not in sessions:
            raise NegativeResponse(0x7F)

    def _require_unlocked(self):
        if not self.state.unlocked:
            raise NegativeResponse(0x33)

    def check_session_timeout(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        if self.state.session != DEFAULT_SESSION and now - self.state.last_request > self.config.s3_timeout:
            self.log("S3 timeout: back to default session")
            self._enter_default_session()

    def _enter_default_session(self):
        self.state.session = DEFAULT_SESSION
        self.state.unlocked = False
        self.state.seed = None
        self.state.transfer = None
        self.state.erased.clear()
        self.state.dtc_setting_on = True
        self.state.communication_enabled = True

    def _service_10(self, request):
        session, suppress = self._subfunction(request)
        if session not in SESSION_NAMES:
            raise NegativeResponse(0x12)
        if (session == PROGRAMMING_SESSION and self.state.session == DEFAULT_SESSION
                and self.config.programming_needs_extended):
            raise NegativeResponse(0x22)  # enter the extended session first
        if session == DEFAULT_SESSION:
            self._enter_default_session()
        else:
            if session != self.state.session:
                self.state.unlocked = False
                self.state.seed = None
            self.state.session = session
        # sessionParameterRecord: P2server_max in ms, P2*server_max in units of 10 ms
        timing = self.config.p2_ms.to_bytes(2, "big") + (self.config.p2_star_ms // 10).to_bytes(2, "big")
        return None if suppress else bytes([0x50, session]) + timing

    def _service_11(self, request):
        reset_type, suppress = self._subfunction(request)
        if reset_type not in (0x01, 0x03):
            raise NegativeResponse(0x12)
        return None if suppress else bytes([0x51, reset_type])

    def _reset(self):
        self._enter_default_session()
        self.state.rebooting_until = time.monotonic() + 0.5
        self.log("ECU reset")

    def _service_14(self, request):
        if len(request) != 4:
            raise NegativeResponse(0x13)
        self.dtcs.clear()
        return b"\x54"

    def _service_19(self, request):
        report, suppress = self._subfunction(request, 3)
        mask = request[2]
        matching = {dtc: status for dtc, status in self.dtcs.items() if status & mask}
        if report == 0x01:
            reply = bytes([0x59, 0x01, 0xFF, 0x01]) + len(matching).to_bytes(2, "big")
        elif report == 0x02:
            reply = bytes([0x59, 0x02, 0xFF]) + b"".join(dtc.to_bytes(3, "big") + bytes([status])
                                                          for dtc, status in matching.items())
        else:
            raise NegativeResponse(0x12)
        return None if suppress else reply

    def _did_value(self, did: int) -> bytes:
        if did == 0xF186:
            return bytes([self.state.session])
        if did == 0x0100:  # uptime in seconds
            return int(time.monotonic() - self._started).to_bytes(4, "big")
        if did in self.dids:
            return self.dids[did]
        raise NegativeResponse(0x31)

    def _service_22(self, request):
        if len(request) < 3 or len(request) % 2 == 0:
            raise NegativeResponse(0x13)
        reply = bytearray(b"\x62")
        for index in range(1, len(request), 2):
            did = int.from_bytes(request[index:index + 2], "big")
            reply += request[index:index + 2] + self._did_value(did)
        return bytes(reply)

    def _service_2e(self, request):
        if len(request) < 4:
            raise NegativeResponse(0x13)
        did = int.from_bytes(request[1:3], "big")
        if did != 0xF190:
            raise NegativeResponse(0x31)
        self._require_session(EXTENDED_SESSION, PROGRAMMING_SESSION)
        self._require_unlocked()
        if len(request) - 3 != 17:
            raise NegativeResponse(0x13)
        self.dids[did] = bytes(request[3:])
        return b"\x6E" + request[1:3]

    def _service_27(self, request):
        level, _ = self._subfunction(request)
        request_seed = self.config.security_level
        if level not in (request_seed, request_seed + 1):
            raise NegativeResponse(0x12)
        self._require_session(EXTENDED_SESSION, PROGRAMMING_SESSION)
        if time.monotonic() < self.state.locked_until:
            raise NegativeResponse(0x37)
        if level == request_seed:
            if len(request) != 2:
                raise NegativeResponse(0x13)
            length = self.config.seed_length
            self.state.seed = bytes(length) if self.state.unlocked else os.urandom(length)
            return bytes([0x67, level]) + self.state.seed
        if self.state.seed is None:
            raise NegativeResponse(0x24)
        expected = bytes(b ^ self.config.key_mask for b in self.state.seed)
        self.state.seed = None
        if request[2:] != expected:
            self.state.failed_attempts += 1
            if self.state.failed_attempts >= self.config.max_attempts:
                self.state.failed_attempts = 0
                self.state.locked_until = time.monotonic() + self.config.lockout_seconds
                raise NegativeResponse(0x36)
            raise NegativeResponse(0x35)
        self.state.failed_attempts = 0
        self.state.unlocked = True
        return bytes([0x67, level])

    def _service_28(self, request):
        control, suppress = self._subfunction(request, 3)
        self._require_session(EXTENDED_SESSION, PROGRAMMING_SESSION)
        if control not in (0x00, 0x01, 0x02, 0x03):
            raise NegativeResponse(0x12)
        self.state.communication_enabled = control == 0x00
        return None if suppress else bytes([0x68, control])

    def _service_85(self, request):
        setting, suppress = self._subfunction(request)
        self._require_session(EXTENDED_SESSION, PROGRAMMING_SESSION)
        if setting not in (0x01, 0x02):
            raise NegativeResponse(0x12)
        self.state.dtc_setting_on = setting == 0x01
        return None if suppress else bytes([0xC5, setting])

    def _service_3e(self, request):
        zero, suppress = self._subfunction(request)
        if zero != 0x00:
            raise NegativeResponse(0x12)
        return None if suppress else b"\x7E\x00"

    # --- flashing ----------------------------------------------------------------

    @staticmethod
    def _memory_range(request: bytes, offset: int):
        """(address, size) from the addressAndLengthFormatIdentifier at request[offset] and the fields after it:
        low nibble = memoryAddress bytes, high nibble = memorySize bytes."""
        if len(request) <= offset:
            raise NegativeResponse(0x13)
        fmt = request[offset]
        address_length, size_length = fmt & 0x0F, fmt >> 4
        if not address_length or not size_length:
            raise NegativeResponse(0x31)
        end = offset + 1 + address_length + size_length
        if len(request) != end:
            raise NegativeResponse(0x13)
        address = int.from_bytes(request[offset + 1:offset + 1 + address_length], "big")
        size = int.from_bytes(request[offset + 1 + address_length:end], "big")
        if size == 0:
            raise NegativeResponse(0x31)
        return address, size

    def _check_range(self, address: int, size: int):
        """NRC 0x31 unless the whole range lies inside one of the configured memory ranges."""
        ranges = self.config.memory_ranges
        if ranges and not any(first <= address and address + size - 1 <= last for first, last in ranges):
            raise NegativeResponse(0x31)

    def read_memory(self, address: int, size: int) -> bytearray:
        """size bytes from address: downloaded data where downloads completed, erased flash (0xFF) elsewhere."""
        data = bytearray(b"\xFF" * size)
        for start, segment in sorted(self.state.memory.items()):
            first, last = max(start, address), min(start + len(segment), address + size)
            if first < last:
                data[first - address:last - address] = segment[first - start:last - start]
        return data

    def _service_31(self, request):
        control, suppress = self._subfunction(request, 4)
        routine = int.from_bytes(request[2:4], "big")
        if control != 0x01:
            raise NegativeResponse(0x12)
        if routine == self.config.erase_routine:
            self._require_session(PROGRAMMING_SESSION)
            self._require_unlocked()
            address, size = self._memory_range(request, 4)
            self._check_range(address, size)
            self._busy(self.config.erase_seconds, 0x31, pending=True)  # erasing takes longer than P2
            self.state.erased.append((address, address + size))
            for start in [a for a in self.state.memory if address <= a < address + size]:
                del self.state.memory[start]
            status = 0x00
        elif routine == self.config.check_routine:
            self._require_session(PROGRAMMING_SESSION)
            self._require_unlocked()
            status = 0x00 if self.state.memory else 0x01
            if self.state.memory:
                image = b"".join(bytes(self.state.memory[a]) for a in sorted(self.state.memory))
                self.dids[0xF195] = f"APP-FLASHED-{zlib.crc32(image):08X}".encode()
                if self.config.dump_path:
                    self.write_image(self.config.dump_path)
        else:
            raise NegativeResponse(0x31)
        return None if suppress else bytes([0x71, 0x01, *request[2:4], status])

    def _start_transfer(self, request: bytes, direction: str) -> bytes:
        """Checks shared by RequestDownload and RequestUpload; returns the positive response."""
        self._require_unlocked()
        if self.state.transfer is not None:
            raise NegativeResponse(0x22)  # a transfer is already active
        if len(request) < 3:
            raise NegativeResponse(0x13)
        if request[1] not in self.config.data_formats:
            raise NegativeResponse(0x31)  # compression/encryption method not supported
        if self.config.address_format is not None and request[2] != self.config.address_format:
            raise NegativeResponse(0x31)
        address, size = self._memory_range(request, 2)
        self._check_range(address, size)
        if direction == "download" and self.config.require_erase and not any(
                start <= address and address + size <= end for start, end in self.state.erased):
            raise NegativeResponse(0x70)  # erase before download
        self.state.transfer = {
            "direction": direction, "address": address, "size": size, "done": 0, "next": 1, "last": None,
            "data": bytearray() if direction == "download" else self.read_memory(address, size), "block": b"",
        }
        maximum = self.config.max_block_length
        length = max(self.config.block_length_bytes, (maximum.bit_length() + 7) // 8)
        rsid = 0x74 if direction == "download" else 0x75
        return bytes([rsid, length << 4]) + maximum.to_bytes(length, "big")

    def _service_34(self, request):
        self._require_session(PROGRAMMING_SESSION)
        return self._start_transfer(request, "download")

    def _service_35(self, request):
        if not self.config.allow_upload:
            raise NegativeResponse(0x11)
        self._require_session(PROGRAMMING_SESSION, EXTENDED_SESSION)
        return self._start_transfer(request, "upload")

    def _service_36(self, request):
        transfer = self.state.transfer
        if transfer is None:
            raise NegativeResponse(0x24)
        if len(request) < 2:
            raise NegativeResponse(0x13)
        counter, block = request[1], request[2:]
        if counter == transfer["last"]:  # a repeated request (its response was lost): answer, do not write twice
            return bytes([0x76, counter]) + transfer["block"]
        if counter != transfer["next"]:
            raise NegativeResponse(0x73)
        room, remaining = self.config.max_block_length - 2, transfer["size"] - transfer["done"]
        if transfer["direction"] == "upload":
            if block:
                raise NegativeResponse(0x13)
            if remaining <= 0:
                raise NegativeResponse(0x24)  # everything sent: RequestTransferExit is expected
            reply = bytes(transfer["data"][transfer["done"]:transfer["done"] + room])
            transfer["done"] += len(reply)
        else:
            if not block or len(request) > self.config.max_block_length:
                raise NegativeResponse(0x13)
            if len(block) > remaining:
                raise NegativeResponse(0x71)  # more data than RequestDownload announced
            if self.config.full_blocks and len(block) != min(room, remaining):
                raise NegativeResponse(0x13)
            transfer["data"] += block
            transfer["done"] += len(block)
            reply = b""
        transfer["last"], transfer["next"], transfer["block"] = counter, (counter + 1) & 0xFF, reply
        return bytes([0x76, counter]) + reply

    def _service_37(self, request):
        transfer = self.state.transfer
        if transfer is None or transfer["done"] != transfer["size"]:
            raise NegativeResponse(0x24)
        if transfer["direction"] == "download":
            self.state.memory[transfer["address"]] = bytes(transfer["data"])
        self.state.transfer = None
        return b"\x77"

    def write_image(self, path):
        """Write the downloaded memory as S3 records."""
        lines = []
        for address in sorted(self.state.memory):
            data = self.state.memory[address]
            for offset in range(0, len(data), 32):
                chunk = bytes(data[offset:offset + 32])
                body = bytes([len(chunk) + 5]) + (address + offset).to_bytes(4, "big") + chunk
                lines.append("S3" + (body + bytes([~sum(body) & 0xFF])).hex().upper())
        with open(path, "w", encoding="ascii") as handle:
            handle.write("\n".join(lines) + "\n")
        self.log(f"Flashed image written to {path}")

    # --- application traffic ---------------------------------------------------------

    def _application_frame(self, message: can.Message):
        data = bytes(message.data)
        if message.arbitration_id == 0x200 and data:
            self.running = {0x01: True, 0x02: False}.get(data[0], self.running)
            self.log(f"Command 0x200: {'running' if self.running else 'stopped'}")
        elif message.arbitration_id == 0x201 and data:
            self.logging = bool(data[0] & 0x01)
            self.log(f"Command 0x201: logging {'on' if self.logging else 'off'}")

    def broadcast(self):
        """0x300: temperature (0.1 degC) and pressure (0.01 bar), big-endian; 0x301: status and counter."""
        if not self.state.communication_enabled or time.monotonic() < self.state.rebooting_until:
            return
        target = 85.0 if self.running else 21.5
        self.temperature += (target - self.temperature) * 0.02
        pressure = 1.0 + (0.8 if self.running else 0.0)
        self.counter = (self.counter + 1) & 0xFF
        temperature = int(self.temperature * 10).to_bytes(2, "big")
        self.bus.send(can.Message(arbitration_id=0x300, is_extended_id=False,
                                  data=temperature + int(pressure * 100).to_bytes(2, "big") + bytes(4)))
        self.bus.send(can.Message(arbitration_id=0x301, is_extended_id=False,
                                  data=bytes([int(self.running), int(self.logging), self.state.session,
                                              self.counter, 0, 0, 0, 0])))

    # --- main loop ------------------------------------------------------------------------

    def serve(self, stop: threading.Event):
        next_broadcast = time.monotonic()
        while not stop.is_set():
            now = time.monotonic()
            if self.config.broadcast_interval and now >= next_broadcast:
                self.broadcast()
                next_broadcast = now + self.config.broadcast_interval
            self.check_session_timeout(now)
            message = self._io.recv(timeout=0.01)
            if message is not None:
                try:
                    self.on_message(message)
                except Exception as exc:  # keep the simulator alive on transport errors
                    self.log(f"Error: {exc}")
                    self._rx = None


BROADCAST_IDS = (0x300, 0x301)


def claim_channel(interface, channel):
    """Hold a localhost port as a per-channel lock while this ECU runs, or return None if another dummy ECU
    on this computer already holds it. (On Kvaser, programs sharing a channel do not see each other's
    frames, so only a lock can tell that a second copy was started.)"""
    port = 47000 + zlib.crc32(f"{interface}:{channel}".encode()) % 2000
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no other socket may share the port
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        lock.bind(("127.0.0.1", port))
    except OSError:
        lock.close()
        return None
    return lock


def other_ecu_present(bus, config: EcuConfig, listen: float = 0.4) -> bool:
    """True when another ECU already answers on this bus, e.g. a second dummy ECU on the same channel.
    Two ECUs answering the same requests break security access and flashing."""
    probe = bytes([0x02, 0x3E, 0x00])  # functional TesterPresent
    if config.address_byte is not None:
        probe = bytes([config.address_byte]) + probe
    bus.send(can.Message(arbitration_id=config.functional_id, data=probe, is_extended_id=config.extended_ids))
    deadline = time.monotonic() + listen
    while time.monotonic() < deadline:
        message = bus.recv(0.05)
        if message is None or message.is_error_frame:
            continue
        if message.arbitration_id == config.response_id and bool(message.is_extended_id) == config.extended_ids:
            return True
        if message.arbitration_id in BROADCAST_IDS and not message.is_extended_id:
            return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    hex_int = lambda text: int(text, 16)  # noqa: E731
    parser.add_argument("--console", action="store_true", help="run in this console, without the window")
    parser.add_argument("--config", metavar="FILE", help="settings profile saved from the window (JSON)")
    parser.add_argument("--interface", help="python-can interface (default: kvaser)")
    parser.add_argument("--channel", help="channel (default: 1, the second Kvaser virtual channel)")
    parser.add_argument("--bitrate", type=int, help="bit rate (default 500000)")
    parser.add_argument("--request-id", type=hex_int, help="physical request ID, hex (default 7E0)")
    parser.add_argument("--response-id", type=hex_int, help="response ID, hex (default 7E8)")
    parser.add_argument("--functional-id", type=hex_int, help="functional request ID, hex (default 7DF)")
    parser.add_argument("--extended-ids", action="store_true", default=None, help="use 29-bit identifiers")
    parser.add_argument("--address-byte", type=hex_int, help="extended addressing byte, hex")
    parser.add_argument("--max-block", type=hex_int, help="maxNumberOfBlockLength, hex (default 402)")
    parser.add_argument("--block-size", type=int,
                        help="flow control BS: consecutive frames per block, 0 = no limit (default 8)")
    parser.add_argument("--stmin", type=lambda text: int(text, 0),
                        help="flow control STmin byte: 0-127 ms or 0xF1-0xF9 for 100-900 us (default 1)")
    parser.add_argument("--fc-wait", type=int, metavar="N",
                        help="send N flow control WAIT frames before each ContinueToSend (default 0)")
    parser.add_argument("--erase-seconds", type=float, help="simulated erase time (default 1)")
    parser.add_argument("--no-broadcast", action="store_true", help="do not send 0x300/0x301 frames")
    parser.add_argument("--dump", metavar="FILE", help="write the flashed image as S-records after flashing")
    parser.add_argument("--force", action="store_true", help="start even if another ECU already answers")
    args = parser.parse_args(argv)

    config, connection = load_profile(args.config) if args.config else (None, {})
    options = {"request_id": args.request_id, "response_id": args.response_id, "functional_id": args.functional_id,
               "extended_ids": args.extended_ids, "address_byte": args.address_byte, "max_block_length": args.max_block,
               "block_size": args.block_size, "st_min": args.stmin, "flow_waits": args.fc_wait,
               "erase_seconds": args.erase_seconds, "dump_path": args.dump,
               "broadcast_interval": 0 if args.no_broadcast else None}
    overrides = {name: value for name, value in options.items() if value is not None}
    connection.update({key: value for key, value in (("interface", args.interface), ("channel", args.channel),
                                                     ("bitrate", args.bitrate)) if value is not None})
    if not args.console:
        from dummy_ecu_window import run_window
        return run_window(config, overrides, connection)

    config = replace(config or EcuConfig(), **overrides)
    connection = {**DEFAULT_CONNECTION, **connection}
    interface, channel = connection["interface"], parse_channel(connection["channel"])
    lock = None if args.force else claim_channel(interface, channel)
    bus = can.Bus(interface=interface, channel=channel, bitrate=int(connection["bitrate"]))
    started = time.strftime("%H:%M:%S")

    def log(text):
        print(f"{time.strftime('%H:%M:%S')}  {text}", flush=True)

    if not args.force and (lock is None or other_ecu_present(bus, config)):
        print(f"Another ECU already answers on {interface} channel {channel}"
              f"{' (a dummy ECU is already running there)' if lock is None else ''}. Close the other dummy ECU "
              f"window first: two ECUs answering the same requests break security access and flashing. "
              f"(--force starts anyway.)", flush=True)
        bus.shutdown()
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                input("Press Enter to close...")  # keep a double-clicked window open long enough to read this
            except (EOFError, KeyboardInterrupt):
                pass
        return 1

    ecu = DummyEcu(bus, config, log)
    print(f"{started}  Dummy ECU on {interface} channel {channel}: requests 0x{config.request_id:X} "
          f"(functional 0x{config.functional_id:X}), responses 0x{config.response_id:X}. Ctrl+C to stop.", flush=True)
    stop = threading.Event()
    try:
        ecu.serve(stop)
    except KeyboardInterrupt:
        pass
    finally:
        bus.shutdown()
        if lock is not None:
            lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
