"""
UDS (Unified Diagnostic Services) over ISO-TP - TesterPresent, RDBI, RequestDownload,
TransferData, RequestTransferExit. S-record / Intel HEX firmware loading for flashing.
"""
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import can

# --- Firmware images (Motorola S-record, Intel HEX) ---

FIRMWARE_FILE_FILTER = ("Firmware (*.s19 *.s28 *.s37 *.srec *.mot *.hex *.ihex);;"
                        "Motorola S-record (*.s19 *.s28 *.s37 *.srec *.mot);;Intel HEX (*.hex *.ihex);;All files (*.*)")


@dataclass
class Firmware:
    """A firmware image: contiguous (address, data) segments in ascending address order."""
    path: str
    segments: list[tuple[int, bytes]]

    @property
    def size(self) -> int:
        return sum(len(data) for _, data in self.segments)


def _srecord_data(lines: list[str]) -> list[tuple[int, bytes]]:
    records = []
    for number, line in enumerate(lines, 1):
        if not line.startswith("S") or len(line) < 4:
            raise ValueError(f"Line {number}: not an S-record")
        kind = line[1]
        try:
            raw = bytes.fromhex(line[2:])
        except ValueError:
            raise ValueError(f"Line {number}: invalid hexadecimal") from None
        if raw[0] != len(raw) - 1:
            raise ValueError(f"Line {number}: byte count does not match record length")
        if (sum(raw[:-1]) + raw[-1]) & 0xFF != 0xFF:
            raise ValueError(f"Line {number}: checksum error")
        address_length = {"1": 2, "2": 3, "3": 4}.get(kind)
        if address_length is None:
            continue  # S0 header, S5/S6 count, S7-S9 start address
        address = int.from_bytes(raw[1:1 + address_length], "big")
        data = raw[1 + address_length:-1]
        if data:
            records.append((address, data))
    return records


def _intel_hex_data(lines: list[str]) -> list[tuple[int, bytes]]:
    records, base = [], 0
    for number, line in enumerate(lines, 1):
        if not line.startswith(":"):
            raise ValueError(f"Line {number}: not an Intel HEX record")
        try:
            raw = bytes.fromhex(line[1:])
        except ValueError:
            raise ValueError(f"Line {number}: invalid hexadecimal") from None
        if len(raw) < 5 or len(raw) != raw[0] + 5:
            raise ValueError(f"Line {number}: byte count does not match record length")
        if sum(raw) & 0xFF:
            raise ValueError(f"Line {number}: checksum error")
        kind, data = raw[3], raw[4:-1]
        if kind == 0x00:
            if data:
                records.append((base + int.from_bytes(raw[1:3], "big"), data))
        elif kind == 0x01:
            break
        elif kind == 0x02:
            base = int.from_bytes(data, "big") << 4
        elif kind == 0x04:
            base = int.from_bytes(data, "big") << 16
    return records


def _read_lines(path: str | Path) -> list[str]:
    return [line.strip() for line in Path(path).read_text(encoding="ascii", errors="replace").splitlines()
            if line.strip()]


def parse_s19_s28_file(path: str | Path) -> list[tuple[int, bytes]]:
    """(address, data) of every S1/S2/S3 record, unmerged; [] if the file is missing. Bad records raise ValueError."""
    return _srecord_data(_read_lines(path)) if Path(path).exists() else []


def load_firmware(path: str | Path) -> Firmware:
    """Read an S-record (S19/S28/S37) or Intel HEX file, verifying checksums and merging contiguous records."""
    lines = _read_lines(path)
    if not lines:
        raise ValueError("Firmware file is empty")
    if lines[0].startswith(":"):
        records = _intel_hex_data(lines)
    elif lines[0].startswith("S"):
        records = _srecord_data(lines)
    else:
        raise ValueError("Unrecognised format; expected Motorola S-record or Intel HEX")
    if not records:
        raise ValueError("Firmware file contains no data records")
    segments: list[tuple[int, bytearray]] = []
    for address, data in sorted(records, key=lambda record: record[0]):
        if segments and address < segments[-1][0] + len(segments[-1][1]):
            raise ValueError(f"Overlapping data at 0x{address:X}")
        if segments and address == segments[-1][0] + len(segments[-1][1]):
            segments[-1][1].extend(data)
        else:
            segments.append((address, bytearray(data)))
    return Firmware(str(path), [(address, bytes(data)) for address, data in segments])


# --- ISO-TP transport (ISO 15765-2, classic CAN) ---

ISOTP_MAX_LENGTH = 0xFFF
N_BS_TIMEOUT = 1.0   # wait for a flow control frame
N_CR_TIMEOUT = 1.0   # wait between consecutive frames
MAX_FC_WAITS = 16    # flow-control WAIT frames accepted before giving up


class IsoTpError(Exception):
    """Transport-level failure (no flow control, overflow, sequence error, ...)."""


def _frame(body: bytes, address_byte: int | None, padding: int | None) -> bytes:
    data = bytes(body)
    if address_byte is not None:
        data = bytes([address_byte]) + data
    if padding is not None:
        data = data.ljust(8, bytes([padding]))
    return data


def _send_frame(bus, can_id, body, extended, address_byte, padding):
    bus.send(can.Message(arbitration_id=can_id, data=_frame(body, address_byte, padding),
                         is_extended_id=extended))


def _recv_payload(bus, response_id, extended, address_byte, timeout):
    """Next frame from response_id with the address byte stripped, or None."""
    msg = bus.recv(timeout=max(0.0, timeout))
    if msg is None or msg.arbitration_id != response_id or bool(msg.is_extended_id) != extended:
        return None
    data = bytes(msg.data)
    if address_byte is not None:
        data = data[1:]
    return data or None


def _stmin_seconds(value: int) -> float:
    if value <= 0x7F:
        return value / 1000.0
    if 0xF1 <= value <= 0xF9:
        return (value - 0xF0) / 10000.0
    return 0x7F / 1000.0


def drain(bus) -> None:
    """Discard frames already queued, so an earlier reply cannot answer a new request."""
    clear = getattr(bus, "clear", None)
    if callable(clear):
        clear()
        return
    for _ in range(4096):
        if bus.recv(timeout=0) is None:
            break


def _wait_flow_control(bus, response_id, extended, address_byte):
    deadline = time.monotonic() + N_BS_TIMEOUT
    waits = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IsoTpError("No flow control frame from ECU")
        data = _recv_payload(bus, response_id, extended, address_byte, min(0.1, remaining))
        if not data or data[0] >> 4 != 0x3:
            continue
        status = data[0] & 0x0F
        if status == 0x0:
            block_size = data[1] if len(data) > 1 else 0
            st_min = _stmin_seconds(data[2]) if len(data) > 2 else 0.0
            return block_size, st_min
        if status == 0x1:
            waits += 1
            if waits > MAX_FC_WAITS:
                raise IsoTpError("ECU kept sending flow control WAIT")
            deadline = time.monotonic() + N_BS_TIMEOUT
            continue
        raise IsoTpError("ECU reported buffer overflow")


def isotp_send(bus, request_id: int, payload: bytes, response_id: int, extended: bool = False,
               address_byte: int | None = None, padding: int | None = None) -> None:
    """Send payload as one single frame, or as first + consecutive frames with flow control."""
    payload = bytes(payload)
    room = 7 - (address_byte is not None)
    if not payload:
        raise IsoTpError("Empty ISO-TP payload")
    if len(payload) <= room:
        _send_frame(bus, request_id, bytes([len(payload)]) + payload, extended, address_byte, padding)
        return
    if len(payload) > ISOTP_MAX_LENGTH:
        raise IsoTpError(f"ISO-TP payload exceeds {ISOTP_MAX_LENGTH} bytes")
    first = room - 1
    header = bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF])
    _send_frame(bus, request_id, header + payload[:first], extended, address_byte, padding)
    offset, sequence = first, 1
    while offset < len(payload):
        block_size, st_min = _wait_flow_control(bus, response_id, extended, address_byte)
        sent = 0
        while offset < len(payload) and (block_size == 0 or sent < block_size):
            if sent and st_min:
                time.sleep(st_min)
            chunk = payload[offset:offset + room]
            _send_frame(bus, request_id, bytes([0x20 | sequence]) + chunk, extended, address_byte, padding)
            offset += len(chunk)
            sequence = (sequence + 1) & 0x0F
            sent += 1


def isotp_recv(bus, response_id: int, request_id: int, timeout: float, extended: bool = False,
               address_byte: int | None = None, padding: int | None = None) -> bytes | None:
    """Receive one ISO-TP message; sends flow control for multi-frame replies. None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = _recv_payload(bus, response_id, extended, address_byte,
                             min(0.1, deadline - time.monotonic()))
        if not data:
            continue
        kind = data[0] >> 4
        if kind == 0x0:
            length = data[0] & 0x0F
            if 0 < length <= len(data) - 1:
                return data[1:1 + length]
        elif kind == 0x1 and len(data) >= 2:
            total = ((data[0] & 0x0F) << 8) | data[1]
            buffer = bytearray(data[2:])
            _send_frame(bus, request_id, bytes([0x30, 0x00, 0x00]), extended, address_byte, padding)
            expected = 1
            frame_deadline = time.monotonic() + N_CR_TIMEOUT
            while len(buffer) < total:
                remaining = frame_deadline - time.monotonic()
                if remaining <= 0:
                    raise IsoTpError("Timed out waiting for consecutive frame")
                part = _recv_payload(bus, response_id, extended, address_byte, min(0.1, remaining))
                if not part or part[0] >> 4 != 0x2:
                    continue
                if part[0] & 0x0F != expected:
                    raise IsoTpError("Consecutive frame out of sequence")
                buffer += part[1:]
                expected = (expected + 1) & 0x0F
                frame_deadline = time.monotonic() + N_CR_TIMEOUT
            return bytes(buffer[:total])
    return None


# --- UDS services ---

def uds_request(bus, request: bytes, request_id: int = 0x7DF, response_id: int = 0x7E8,
                timeout: float = 2.0, extended: bool = False, address_byte: int | None = None,
                padding: int | None = None, pending_timeout: float = 5.0) -> bytes | None:
    """
    Send one UDS request and return the ECU's reply (positive or 0x7F negative), or None on timeout.
    Frames queued before the request are discarded, unrelated replies are skipped and
    NRC 0x78 (response pending) extends the wait. A bus exposing transaction() (the
    session mailbox) pauses the periodic TesterPresent while the exchange is in progress.
    """
    request = bytes(request)
    sid = request[0]
    transaction = getattr(bus, "transaction", None)
    with transaction() if callable(transaction) else nullcontext():
        drain(bus)
        isotp_send(bus, request_id, request, response_id, extended, address_byte, padding)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            reply = isotp_recv(bus, response_id, request_id, remaining, extended, address_byte, padding)
            if reply is None:
                return None
            if reply[0] == sid + 0x40:
                return reply
            if reply[0] == 0x7F and len(reply) >= 3 and reply[1] == sid:
                if reply[2] == 0x78:
                    deadline = time.monotonic() + pending_timeout
                    continue
                return reply


def _positive(reply: bytes | None, sid: int) -> bool:
    return bool(reply) and reply[0] == sid + 0x40


def uds_tester_present(bus, request_id: int = 0x7DF, response_id: int = 0x7E8, timeout: float = 0.5,
                       **transport) -> bool:
    """Send TesterPresent (0x3E 0x00). Returns True if a positive response is received."""
    return _positive(uds_request(bus, b"\x3E\x00", request_id, response_id, timeout, **transport), 0x3E)


def uds_rdbi(bus, did: int, request_id: int = 0x7DF, response_id: int = 0x7E8, timeout: float = 2.0,
             **transport) -> bytes | None:
    """ReadDataByIdentifier (0x22). Returns the data record (after the echoed DID) or None."""
    did_bytes = bytes([(did >> 8) & 0xFF, did & 0xFF])
    reply = uds_request(bus, b"\x22" + did_bytes, request_id, response_id, timeout, **transport)
    if not _positive(reply, 0x22) or reply[1:3] != did_bytes:
        return None
    return reply[3:]


def _request_download(bus, format: int, address: int, size: int, request_id: int, response_id: int,
                      timeout: float, **transport) -> int | None:
    """RequestDownload (0x34). Returns maxNumberOfBlockLength (0 if not reported) or None on failure."""
    address_length, size_length = format & 0x0F, format >> 4
    if not address_length or not size_length:
        raise ValueError("Format must give address and size lengths, e.g. 0x44 or 0x22")
    request = (bytes([0x34, 0x00, format]) + address.to_bytes(address_length, "big")
               + size.to_bytes(size_length, "big"))
    reply = uds_request(bus, request, request_id, response_id, timeout, **transport)
    if not _positive(reply, 0x34):
        return None
    field_length = reply[1] >> 4 if len(reply) > 1 else 0
    return int.from_bytes(reply[2:2 + field_length], "big") if field_length else 0


def uds_request_download(
    bus,
    format: int,
    address: int,
    size: int,
    request_id: int = 0x7DF,
    response_id: int = 0x7E8,
    timeout: float = 2.0,
    **transport,
) -> bool:
    """RequestDownload (0x34). format is the addressAndLengthFormatIdentifier, e.g. 0x44 (4-byte address and size)."""
    return _request_download(bus, format, address, size, request_id, response_id, timeout, **transport) is not None


def uds_transfer_data(
    bus,
    sequence: int,
    data: bytes,
    request_id: int = 0x7DF,
    response_id: int = 0x7E8,
    timeout: float = 2.0,
    **transport,
) -> bool:
    """TransferData (0x36). sequence is the block sequence counter (0-255). Returns True if acknowledged."""
    if not 0 <= sequence <= 0xFF:
        return False
    reply = uds_request(bus, bytes([0x36, sequence]) + bytes(data), request_id, response_id, timeout, **transport)
    return _positive(reply, 0x36) and len(reply) > 1 and reply[1] == sequence


def uds_request_transfer_exit(bus, request_id: int = 0x7DF, response_id: int = 0x7E8, timeout: float = 2.0,
                              **transport) -> bool:
    """RequestTransferExit (0x37)."""
    return _positive(uds_request(bus, b"\x37", request_id, response_id, timeout, **transport), 0x37)


def uds_flash_from_file(
    bus,
    s19_path: str | Path,
    packet_size: int,
    request_id: int = 0x7DF,
    response_id: int = 0x7E8,
    progress_cb: Callable[[int, int], None] | None = None,
    timeout: float = 2.0,
    **transport,
) -> tuple[bool, str]:
    """
    Flash an S-record or Intel HEX file: RequestDownload, TransferData blocks and RequestTransferExit per segment.
    packet_size = data bytes per TransferData, capped by the ECU's maxNumberOfBlockLength.
    Returns (success, error_message).
    """
    try:
        firmware = load_firmware(s19_path)
    except (OSError, ValueError) as exc:
        return False, str(exc)
    total_sent = 0
    total_size = firmware.size
    for addr, data in firmware.segments:
        size = len(data)
        max_block = _request_download(bus, 0x44, addr, size, request_id, response_id, timeout, **transport)
        if max_block is None:
            return False, f"RequestDownload failed at 0x{addr:X}"
        chunk_size = max(1, min(packet_size, max_block - 2) if max_block > 2 else packet_size)
        offset = 0
        seq = 1
        while offset < size:
            chunk = data[offset : offset + chunk_size]
            if not uds_transfer_data(bus, seq, chunk, request_id, response_id, timeout, **transport):
                return False, f"TransferData failed at 0x{addr:X} seq {seq}"
            offset += len(chunk)
            seq = (seq + 1) & 0xFF
            total_sent += len(chunk)
            if progress_cb:
                progress_cb(total_sent, total_size)
        if not uds_request_transfer_exit(bus, request_id, response_id, timeout, **transport):
            return False, f"RequestTransferExit failed at 0x{addr:X}"
    return True, ""
