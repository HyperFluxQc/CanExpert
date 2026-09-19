#!/usr/bin/env python3
"""
Dummy UDS ECU (ISO 14229-1 over ISO 15765-2) for trying CAN Expert without a vehicle.

Kvaser's Virtual CAN Driver provides two connected channels. Run the ECU on one and
connect CAN Expert to the other:

    python dummy_ecu.py --interface kvaser --channel 1
    (CAN Expert: receiver "[kvaser] Ch 0", configuration SERVER ID 7E0, ECU ID 7E8)

What it simulates:
- Sessions (0x10), TesterPresent (0x3E), ECUReset (0x11), S3 session timeout
- ReadDataByIdentifier (0x22) / WriteDataByIdentifier (0x2E), including multi-frame VIN
- SecurityAccess (0x27, key = seed XOR 0xA5, attempt counter and lockout delay)
- ControlDTCSetting (0x85), CommunicationControl (0x28), ReadDTCInformation (0x19), ClearDTC (0x14)
- Flashing: RoutineControl erase / checkProgrammingDependencies (0x31), RequestDownload (0x34),
  TransferData (0x36), RequestTransferExit (0x37), response pending (NRC 0x78) during erase
- ISO-TP flow control on segmented requests: block size and STmin (--block-size, --stmin), optional
  WAIT frames (--fc-wait), overflow for requests longer than maxNumberOfBlockLength
- Periodic application frames: 0x300 temperature/pressure, 0x301 status; commands on 0x200/0x201
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field

import can

from uds_services import FC_OVERFLOW, FC_WAIT, N_CR_TIMEOUT, flow_control_frame, isotp_send, parse_first_frame

DEFAULT_SESSION, PROGRAMMING_SESSION, EXTENDED_SESSION = 0x01, 0x02, 0x03
SESSION_NAMES = {DEFAULT_SESSION: "default", PROGRAMMING_SESSION: "programming", EXTENDED_SESSION: "extended"}
ERASE_ROUTINE, CHECK_DEPENDENCIES_ROUTINE = 0xFF00, 0xFF01
# Negative responses an ECU must not send to functionally addressed requests (ISO 14229-1 7.5).
SUPPRESSED_FUNCTIONAL_NRCS = {0x11, 0x12, 0x31, 0x7E, 0x7F}
SERVICE_NAMES = {
    0x10: "DiagnosticSessionControl", 0x11: "ECUReset", 0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation", 0x22: "ReadDataByIdentifier", 0x27: "SecurityAccess",
    0x28: "CommunicationControl", 0x2E: "WriteDataByIdentifier", 0x31: "RoutineControl",
    0x34: "RequestDownload", 0x36: "TransferData", 0x37: "RequestTransferExit",
    0x3E: "TesterPresent", 0x85: "ControlDTCSetting",
}


class NegativeResponse(Exception):
    def __init__(self, nrc: int):
        super().__init__(f"NRC 0x{nrc:02X}")
        self.nrc = nrc


@dataclass
class EcuConfig:
    request_id: int = 0x7E0
    response_id: int = 0x7E8
    functional_id: int = 0x7DF
    extended_ids: bool = False
    address_byte: int | None = None
    padding: int | None = 0xAA
    max_block_length: int = 0x402      # also the receive buffer: longer requests get flow control overflow
    block_size: int = 8                # flow control BS: consecutive frames per block (0 = no limit)
    st_min: int = 0x01                 # flow control STmin byte: 0x00-0x7F ms, 0xF1-0xF9 100-900 us
    flow_waits: int = 0                # flow control WAIT frames sent before each ContinueToSend
    erase_seconds: float = 1.0
    s3_timeout: float = 5.0
    broadcast_interval: float = 0.1
    dump_path: str | None = None


@dataclass
class EcuState:
    session: int = DEFAULT_SESSION
    unlocked: bool = False
    seed: bytes | None = None
    failed_attempts: int = 0
    locked_until: float = 0.0
    dtc_setting_on: bool = True
    communication_enabled: bool = True
    erased: list = field(default_factory=list)          # [(start, end)]
    download: dict | None = None                         # address, size, received, next_sequence
    memory: dict = field(default_factory=dict)            # address -> bytearray of committed data
    last_request: float = field(default_factory=time.monotonic)
    rebooting_until: float = 0.0


class DummyEcu:
    def __init__(self, bus: can.BusABC, config: EcuConfig | None = None, log=print):
        self.bus = bus
        self.config = config or EcuConfig()
        self.log = log
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
        self._started = time.monotonic()
        self._lock = threading.Lock()

    # --- transport ---------------------------------------------------------------

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
        self.bus.send(can.Message(arbitration_id=self.config.response_id, data=data,
                                  is_extended_id=self.config.extended_ids))

    def respond(self, payload: bytes):
        isotp_send(self.bus, self.config.response_id, payload, self.config.request_id,
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
            if first[0] > self.config.max_block_length:
                self.log(f"ISO-TP: {first[0]}-byte request exceeds the receive buffer; flow control overflow")
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
        """Flow control for the next block, after the configured WAIT frames (each well within N_Bs)."""
        for _ in range(self.config.flow_waits):
            self._send_frame(flow_control_frame(status=FC_WAIT))
            time.sleep(0.1)
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
        self.state.download = None
        self.state.erased.clear()
        self.state.dtc_setting_on = True
        self.state.communication_enabled = True

    def _service_10(self, request):
        session, suppress = self._subfunction(request)
        if session not in SESSION_NAMES:
            raise NegativeResponse(0x12)
        if session == PROGRAMMING_SESSION and self.state.session == DEFAULT_SESSION:
            raise NegativeResponse(0x22)  # enter the extended session first
        if session == DEFAULT_SESSION:
            self._enter_default_session()
        else:
            if session != self.state.session:
                self.state.unlocked = False
                self.state.seed = None
            self.state.session = session
        # P2 = 50 ms, P2* = 5000 ms (10 ms resolution)
        return None if suppress else bytes([0x50, session, 0x00, 0x32, 0x01, 0xF4])

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
        if level not in (0x01, 0x02):
            raise NegativeResponse(0x12)
        self._require_session(EXTENDED_SESSION, PROGRAMMING_SESSION)
        if time.monotonic() < self.state.locked_until:
            raise NegativeResponse(0x37)
        if level == 0x01:
            if len(request) != 2:
                raise NegativeResponse(0x13)
            self.state.seed = bytes(4) if self.state.unlocked else os.urandom(4)
            return b"\x67\x01" + self.state.seed
        if self.state.seed is None:
            raise NegativeResponse(0x24)
        expected = bytes(b ^ 0xA5 for b in self.state.seed)
        self.state.seed = None
        if request[2:] != expected:
            self.state.failed_attempts += 1
            if self.state.failed_attempts >= 3:
                self.state.failed_attempts = 0
                self.state.locked_until = time.monotonic() + 10.0
                raise NegativeResponse(0x36)
            raise NegativeResponse(0x35)
        self.state.failed_attempts = 0
        self.state.unlocked = True
        return b"\x67\x02"

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

    @staticmethod
    def _memory_range(request: bytes, offset: int):
        if len(request) <= offset:
            raise NegativeResponse(0x13)
        fmt = request[offset]
        address_length, size_length = fmt & 0x0F, fmt >> 4
        if not 1 <= address_length <= 4 or not 1 <= size_length <= 4:
            raise NegativeResponse(0x31)
        end = offset + 1 + address_length + size_length
        if len(request) != end:
            raise NegativeResponse(0x13)
        address = int.from_bytes(request[offset + 1:offset + 1 + address_length], "big")
        size = int.from_bytes(request[offset + 1 + address_length:end], "big")
        if size == 0:
            raise NegativeResponse(0x31)
        return address, size

    def _service_31(self, request):
        control, suppress = self._subfunction(request, 4)
        routine = int.from_bytes(request[2:4], "big")
        if control != 0x01:
            raise NegativeResponse(0x12)
        if routine == ERASE_ROUTINE:
            self._require_session(PROGRAMMING_SESSION)
            self._require_unlocked()
            address, size = self._memory_range(request, 4)
            deadline = time.monotonic() + self.config.erase_seconds
            while True:  # erasing takes longer than P2: keep the tester waiting with NRC 0x78
                self.respond(b"\x7F\x31\x78")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(2.0, remaining))
            self.state.erased.append((address, address + size))
            for start in [a for a in self.state.memory if address <= a < address + size]:
                del self.state.memory[start]
            status = 0x00
        elif routine == CHECK_DEPENDENCIES_ROUTINE:
            self._require_session(PROGRAMMING_SESSION)
            self._require_unlocked()
            status = 0x00 if self.state.memory else 0x01
            if self.state.memory:
                image = b"".join(bytes(self.state.memory[a]) for a in sorted(self.state.memory))
                self.dids[0xF195] = f"APP-FLASHED-{zlib.crc32(image):08X}".encode()
                self._dump()
        else:
            raise NegativeResponse(0x31)
        return None if suppress else bytes([0x71, 0x01, *request[2:4], status])

    def _service_34(self, request):
        self._require_session(PROGRAMMING_SESSION)
        self._require_unlocked()
        if self.state.download is not None:
            raise NegativeResponse(0x22)
        if len(request) < 3 or request[1] != 0x00:
            raise NegativeResponse(0x31)  # no compression/encryption support
        address, size = self._memory_range(request, 2)
        if not any(start <= address and address + size <= end for start, end in self.state.erased):
            raise NegativeResponse(0x70)  # erase before download
        self.state.download = {"address": address, "size": size, "data": bytearray(), "next": 1, "last": None}
        return bytes([0x74, 0x20]) + self.config.max_block_length.to_bytes(2, "big")

    def _service_36(self, request):
        download = self.state.download
        if download is None:
            raise NegativeResponse(0x24)
        if len(request) < 2:
            raise NegativeResponse(0x13)
        sequence = request[1]
        if sequence == download["last"]:
            return bytes([0x76, sequence])  # repeated block: acknowledge without writing twice
        if sequence != download["next"]:
            raise NegativeResponse(0x73)
        block = request[2:]
        if len(request) > self.config.max_block_length:
            raise NegativeResponse(0x13)
        if len(download["data"]) + len(block) > download["size"]:
            raise NegativeResponse(0x71)
        download["data"] += block
        download["last"], download["next"] = sequence, (sequence + 1) & 0xFF
        return bytes([0x76, sequence])

    def _service_37(self, request):
        download = self.state.download
        if download is None or len(download["data"]) != download["size"]:
            raise NegativeResponse(0x24)
        self.state.memory[download["address"]] = download["data"]
        self.state.download = None
        return b"\x77"

    def _dump(self):
        if not self.config.dump_path:
            return
        lines = []
        for address in sorted(self.state.memory):
            data = self.state.memory[address]
            for offset in range(0, len(data), 32):
                chunk = bytes(data[offset:offset + 32])
                body = bytes([len(chunk) + 5]) + (address + offset).to_bytes(4, "big") + chunk
                lines.append("S3" + (body + bytes([~sum(body) & 0xFF])).hex().upper())
        with open(self.config.dump_path, "w", encoding="ascii") as handle:
            handle.write("\n".join(lines) + "\n")
        self.log(f"Flashed image written to {self.config.dump_path}")

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
            message = self.bus.recv(timeout=0.01)
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
    parser.add_argument("--interface", default="kvaser", help="python-can interface (default: kvaser)")
    parser.add_argument("--channel", default="1", help="channel (default: 1, the second Kvaser virtual channel)")
    parser.add_argument("--bitrate", type=int, default=500000)
    parser.add_argument("--request-id", type=hex_int, default=0x7E0, help="physical request ID, hex (default 7E0)")
    parser.add_argument("--response-id", type=hex_int, default=0x7E8, help="response ID, hex (default 7E8)")
    parser.add_argument("--functional-id", type=hex_int, default=0x7DF, help="functional request ID, hex (default 7DF)")
    parser.add_argument("--extended-ids", action="store_true", help="use 29-bit identifiers")
    parser.add_argument("--address-byte", type=hex_int, help="extended addressing byte, hex")
    parser.add_argument("--max-block", type=hex_int, default=0x402, help="maxNumberOfBlockLength, hex (default 402)")
    parser.add_argument("--block-size", type=int, default=8,
                        help="flow control BS: consecutive frames per block, 0 = no limit (default 8)")
    parser.add_argument("--stmin", type=lambda text: int(text, 0), default=0x01,
                        help="flow control STmin byte: 0-127 ms or 0xF1-0xF9 for 100-900 us (default 1)")
    parser.add_argument("--fc-wait", type=int, default=0, metavar="N",
                        help="send N flow control WAIT frames before each ContinueToSend (default 0)")
    parser.add_argument("--erase-seconds", type=float, default=1.0, help="simulated erase time")
    parser.add_argument("--no-broadcast", action="store_true", help="do not send 0x300/0x301 frames")
    parser.add_argument("--dump", metavar="FILE", help="write the flashed image as S-records after flashing")
    parser.add_argument("--force", action="store_true", help="start even if another ECU already answers")
    args = parser.parse_args(argv)

    channel = int(args.channel) if args.channel.isdigit() else args.channel
    config = EcuConfig(request_id=args.request_id, response_id=args.response_id, functional_id=args.functional_id,
                       extended_ids=args.extended_ids, address_byte=args.address_byte,
                       max_block_length=args.max_block, block_size=args.block_size, st_min=args.stmin,
                       flow_waits=args.fc_wait, erase_seconds=args.erase_seconds,
                       broadcast_interval=0 if args.no_broadcast else 0.1, dump_path=args.dump)
    lock = None if args.force else claim_channel(args.interface, channel)
    bus = can.Bus(interface=args.interface, channel=channel, bitrate=args.bitrate)
    started = time.strftime("%H:%M:%S")

    def log(text):
        print(f"{time.strftime('%H:%M:%S')}  {text}", flush=True)

    if not args.force and (lock is None or other_ecu_present(bus, config)):
        print(f"Another ECU already answers on {args.interface} channel {channel}"
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
    print(f"{started}  Dummy ECU on {args.interface} channel {channel}: requests 0x{config.request_id:X} "
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
