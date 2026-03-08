"""
UDS (Unified Diagnostic Services) - TesterPresent, RDBI, RequestDownload, TransferData.
S19/S28 file parsing for flashing.
"""
import time
import struct
from pathlib import Path
from typing import Callable

# Optional: can is passed in by caller
try:
    import can
except ImportError:
    can = None


def _bytes_to_hex(data: list | bytes) -> str:
    data = list(data) if not isinstance(data, (list, bytearray)) else list(data)
    return " ".join(f"{b:02X}" for b in data[:8])


# --- S19 / S28 / S37 parser (Motorola S-record) ---

def _parse_s_record_line(line: str) -> tuple[str, int, int, bytes] | None:
    """Parse one S-record. Returns (type, address, length, data) or None."""
    line = line.strip()
    if not line or line[0] != "S":
        return None
    try:
        typ = line[1]
        byte_count = int(line[2:4], 16)
        if typ == "0":
            # S0: header, address is 0
            return ("S0", 0, byte_count - 3, bytes.fromhex(line[8 : 8 + (byte_count - 3) * 2]))
        if typ == "1":
            # S1: 16-bit address, 2 addr bytes
            addr = int(line[4:8], 16)
            data_len = byte_count - 3
            data = bytes.fromhex(line[8 : 8 + data_len * 2])
            return ("S1", addr, data_len, data)
        if typ == "2":
            # S2: 24-bit address
            addr = int(line[4:10], 16)
            data_len = byte_count - 4
            data = bytes.fromhex(line[10 : 10 + data_len * 2])
            return ("S2", addr, data_len, data)
        if typ == "3":
            # S3: 32-bit address
            addr = int(line[4:12], 16)
            data_len = byte_count - 5
            data = bytes.fromhex(line[12 : 12 + data_len * 2])
            return ("S3", addr, data_len, data)
        if typ in "789":
            # S7/S8/S9: termination, no data
            return (f"S{typ}", 0, 0, b"")
    except (ValueError, IndexError):
        pass
    return None


def parse_s19_s28_file(path: str | Path) -> list[tuple[int, bytes]]:
    """
    Parse S19 or S28 (or S37) file. Returns list of (address, data) chunks.
    """
    path = Path(path)
    if not path.exists():
        return []
    blocks = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            r = _parse_s_record_line(line)
            if r is None:
                continue
            typ, addr, _len, data = r
            if typ in ("S1", "S2", "S3") and data:
                blocks.append((addr, data))
    return blocks


# --- UDS over CAN (ISO-TP style single frame assumed for simplicity) ---

def _make_single_frame(payload: bytes) -> bytes:
    """First byte = length (0-7 for single frame), then payload."""
    if len(payload) > 7:
        payload = payload[:7]
    return bytes([0x0 | len(payload)]) + payload


def uds_tester_present(bus, request_id: int = 0x7DF, response_id: int = 0x7E8, timeout: float = 0.5) -> bool:
    """Send TesterPresent (0x3E 0x00). Returns True if response received."""
    payload = _make_single_frame(bytes([0x3E, 0x00]))
    msg = can.Message(arbitration_id=request_id, data=payload, is_extended_id=False)
    bus.send(msg)
    deadline = time.time() + timeout
    while time.time() < deadline:
        recv = bus.recv(timeout=0.05)
        if recv and recv.arbitration_id == response_id and recv.dlc >= 2 and recv.data[1] == 0x7E:
            return True
    return False


def uds_rdbi(bus, did: int, request_id: int = 0x7DF, response_id: int = 0x7E8, timeout: float = 2.0) -> bytes | None:
    """ReadDataByIdentifier (0x22). Returns response data or None."""
    payload = _make_single_frame(bytes([0x22, (did >> 8) & 0xFF, did & 0xFF]))
    msg = can.Message(arbitration_id=request_id, data=payload, is_extended_id=False)
    bus.send(msg)
    deadline = time.time() + timeout
    while time.time() < deadline:
        recv = bus.recv(timeout=0.1)
        if recv and recv.arbitration_id == response_id and recv.dlc >= 3 and recv.data[1] == 0x62:
            return bytes(recv.data[3:])
    return None


def uds_request_download(
    bus,
    format: int,
    address: int,
    size: int,
    request_id: int = 0x7DF,
    response_id: int = 0x7E8,
    timeout: float = 2.0,
) -> bool:
    """RequestDownload (0x34). format e.g. 0x22 (2-byte addr/size) or 0x44 (4-byte). Single-frame: max 7 payload bytes so 0x22 + 2 addr + 2 size."""
    if format == 0x44:
        addr_b = address.to_bytes(4, "big")
        size_b = size.to_bytes(4, "big")
        payload = bytes([0x34, format]) + addr_b + size_b
        # 10 bytes - need multi-frame; send as single 8-byte CAN frame without ISO-TP length byte
        msg = can.Message(arbitration_id=request_id, data=payload[:8], is_extended_id=False)
    else:
        addr_b = (address & 0xFFFF).to_bytes(2, "big")
        size_b = (size & 0xFFFF).to_bytes(2, "big")
        payload = _make_single_frame(bytes([0x34, format]) + addr_b + size_b)
        msg = can.Message(arbitration_id=request_id, data=payload[:8], is_extended_id=False)
    bus.send(msg)
    if format == 0x44 and len(payload) > 8:
        msg2 = can.Message(arbitration_id=request_id, data=payload[8:], is_extended_id=False)
        bus.send(msg2)
    deadline = time.time() + timeout
    while time.time() < deadline:
        recv = bus.recv(timeout=0.1)
        if recv and recv.arbitration_id == response_id and recv.dlc >= 2 and recv.data[1] == 0x74:
            return True
    return False


def uds_transfer_data(
    bus,
    sequence: int,
    data: bytes,
    request_id: int = 0x7DF,
    response_id: int = 0x7E8,
    timeout: float = 0.5,
) -> bool:
    """TransferData (0x36). sequence 1-based, data up to 6 bytes in single frame. Returns True if ACK."""
    if sequence < 1 or sequence > 0xFF or len(data) > 6:
        return False
    payload = _make_single_frame(bytes([0x36, sequence & 0xFF]) + data[:6])
    msg = can.Message(arbitration_id=request_id, data=payload, is_extended_id=False)
    bus.send(msg)
    deadline = time.time() + timeout
    while time.time() < deadline:
        recv = bus.recv(timeout=0.05)
        if recv and recv.arbitration_id == response_id and recv.dlc >= 2 and recv.data[1] == 0x76:
            return True
    return False


def uds_flash_from_file(
    bus,
    s19_path: str | Path,
    packet_size: int,
    request_id: int = 0x7DF,
    response_id: int = 0x7E8,
    progress_cb: Callable[[int, int], None] | None = None,
) -> tuple[bool, str]:
    """
    Perform UDS flashing: RequestDownload then TransferData for each chunk from S19/S28 file.
    packet_size = bytes per TransferData (e.g. 4 or 6 for single-frame).
    Returns (success, error_message).
    """
    blocks = parse_s19_s28_file(s19_path)
    if not blocks:
        return False, "No data records in file or file not found"
    total_sent = 0
    total_size = sum(len(d) for _, d in blocks)
    for addr, data in blocks:
        size = len(data)
        if not uds_request_download(bus, 0x44, addr, size, request_id, response_id):
            return False, f"RequestDownload failed at 0x{addr:X}"
        offset = 0
        seq = 1
        while offset < size:
            chunk = data[offset : offset + min(packet_size, 6)]
            if not uds_transfer_data(bus, seq, chunk, request_id, response_id):
                return False, f"TransferData failed at 0x{addr:X} seq {seq}"
            offset += len(chunk)
            seq += 1
            total_sent += len(chunk)
            if progress_cb:
                progress_cb(total_sent, total_size)
    return True, ""
