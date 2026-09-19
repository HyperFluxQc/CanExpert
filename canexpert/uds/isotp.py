"""
ISO-TP (ISO 15765-2) on classic CAN: single, first and consecutive frames, flow control (block size, STmin,
WAIT, overflow) in both directions, and the escape sequence for messages longer than 4095 bytes.
"""
import time

import can

ISOTP_MAX_LENGTH = 0xFFF                 # longest message a first frame's 12-bit length can announce
ISOTP_MAX_ESCAPED_LENGTH = 0xFFFFFFFF    # with the escape sequence: FF_DL = 0, then a 32-bit length
N_BS_TIMEOUT = 1.0   # wait for a flow control frame
N_CR_TIMEOUT = 1.0   # wait between consecutive frames
MAX_FC_WAITS = 16    # flow-control WAIT frames accepted before giving up (N_WFTmax)
FC_CONTINUE, FC_WAIT, FC_OVERFLOW = 0x0, 0x1, 0x2   # FlowStatus of a flow control frame


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
    """STmin byte to seconds: 0x00-0x7F ms, 0xF1-0xF9 100-900 us; reserved values mean the maximum, 127 ms."""
    if value <= 0x7F:
        return value / 1000.0
    if 0xF1 <= value <= 0xF9:
        return (value - 0xF0) / 10000.0
    return 0x7F / 1000.0


def _wait_until(moment: float) -> None:
    """Wait until time.perf_counter() reaches moment. time.sleep() can overshoot by up to ~15 ms on Windows,
    far more than a 1 ms STmin, so the last stretch is spent yielding in a loop."""
    while True:
        remaining = moment - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(remaining - 0.016 if remaining > 0.02 else 0)


def flow_control_frame(block_size: int = 0, st_min: int = 0, status: int = FC_CONTINUE) -> bytes:
    """FlowControl N_PCI: FlowStatus, BlockSize (0 = no limit) and the STmin byte."""
    return bytes([0x30 | status, block_size, st_min])


def parse_first_frame(data: bytes, room: int):
    """(message length, first data bytes) of a first frame, or None for an invalid one, which is ignored:
    a first frame fills the CAN frame and announces more than a single frame can carry."""
    if len(data) < room + 1:
        return None
    total = ((data[0] & 0x0F) << 8) | data[1]
    if total:
        return (total, data[2:]) if total > room else None
    total = int.from_bytes(data[2:6], "big")                   # escape sequence
    return (total, data[6:]) if total > ISOTP_MAX_LENGTH else None


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
    """(block size, STmin in seconds) of the receiver's next ContinueToSend flow control frame."""
    deadline = time.monotonic() + N_BS_TIMEOUT
    waits = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IsoTpError("No flow control frame from ECU")
        data = _recv_payload(bus, response_id, extended, address_byte, min(0.1, remaining))
        if not data or data[0] >> 4 != 0x3 or len(data) < 3:
            continue
        status = data[0] & 0x0F
        if status == FC_CONTINUE:
            return data[1], _stmin_seconds(data[2])
        if status == FC_WAIT:  # the receiver is not ready yet: N_Bs starts again
            waits += 1
            if waits > MAX_FC_WAITS:
                raise IsoTpError("ECU kept sending flow control WAIT")
            deadline = time.monotonic() + N_BS_TIMEOUT
            continue
        if status == FC_OVERFLOW:
            raise IsoTpError("ECU reported buffer overflow: the message is longer than it accepts")
        raise IsoTpError(f"Invalid flow status 0x{status:X} from ECU")


def isotp_send(bus, request_id: int, payload: bytes, response_id: int, extended: bool = False,
               address_byte: int | None = None, padding: int | None = None) -> None:
    """Send payload as one single frame, or as a first frame and consecutive frames paced by the
    receiver's flow control: a new flow control frame after every BS frames, at least STmin between frames."""
    payload = bytes(payload)
    room = 7 - (address_byte is not None)
    if not payload:
        raise IsoTpError("Empty ISO-TP payload")
    if len(payload) <= room:
        _send_frame(bus, request_id, bytes([len(payload)]) + payload, extended, address_byte, padding)
        return
    if len(payload) > ISOTP_MAX_ESCAPED_LENGTH:
        raise IsoTpError(f"ISO-TP payload exceeds {ISOTP_MAX_ESCAPED_LENGTH} bytes")
    if len(payload) <= ISOTP_MAX_LENGTH:
        header = bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF])
    else:
        header = b"\x10\x00" + len(payload).to_bytes(4, "big")  # escape sequence (ISO 15765-2:2016)
    first = room + 1 - len(header)
    _send_frame(bus, request_id, header + payload[:first], extended, address_byte, padding)
    offset, sequence = first, 1
    while offset < len(payload):
        block_size, st_min = _wait_flow_control(bus, response_id, extended, address_byte)
        sent, next_frame = 0, 0.0
        while offset < len(payload) and (block_size == 0 or sent < block_size):
            _wait_until(next_frame)
            chunk = payload[offset:offset + room]
            _send_frame(bus, request_id, bytes([0x20 | sequence]) + chunk, extended, address_byte, padding)
            next_frame = time.perf_counter() + st_min
            offset += len(chunk)
            sequence = (sequence + 1) & 0x0F
            sent += 1


def isotp_recv(bus, response_id: int, request_id: int, timeout: float, extended: bool = False,
               address_byte: int | None = None, padding: int | None = None, block_size: int = 0,
               st_min: int = 0) -> bytes | None:
    """
    Receive one ISO-TP message, or None if none starts within timeout. A first frame is answered with
    flow control (block_size frames per block, 0 = no limit; st_min as the STmin byte), repeated after
    every block. A new single or first frame replaces a message in progress.
    """
    room = 7 - (address_byte is not None)
    deadline = time.monotonic() + timeout
    message = None  # the multi-frame message in progress: total, data, next sequence number, frames left
    while True:
        limit = message["deadline"] if message else deadline
        remaining = limit - time.monotonic()
        if remaining <= 0:
            if message:
                raise IsoTpError("Timed out waiting for consecutive frame")
            return None
        data = _recv_payload(bus, response_id, extended, address_byte, min(0.1, remaining))
        if not data:
            continue
        kind = data[0] >> 4
        if kind == 0x0:
            length = data[0] & 0x0F
            if 0 < length <= min(room, len(data) - 1):
                return data[1:1 + length]
        elif kind == 0x1:
            first = parse_first_frame(data, room)
            if first is None:
                continue
            message = {"total": first[0], "data": bytearray(first[1]), "next": 1, "left": block_size}
            _send_frame(bus, request_id, flow_control_frame(block_size, st_min), extended, address_byte, padding)
            message["deadline"] = time.monotonic() + N_CR_TIMEOUT
        elif kind == 0x2 and message:
            if data[0] & 0x0F != message["next"]:
                raise IsoTpError("Consecutive frame out of sequence")
            message["data"] += data[1:]
            if len(message["data"]) >= message["total"]:
                return bytes(message["data"][:message["total"]])
            message["next"] = (message["next"] + 1) & 0x0F
            message["left"] -= 1
            if message["left"] == 0:  # end of the block: the sender waits for the next flow control
                message["left"] = block_size
                _send_frame(bus, request_id, flow_control_frame(block_size, st_min), extended, address_byte, padding)
            message["deadline"] = time.monotonic() + N_CR_TIMEOUT


