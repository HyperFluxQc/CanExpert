"""
Reading ISO-TP off the bus: the frames that were seen, grouped into the messages they carried.

The live transport in isotp.py sends and receives; this one only watches, so it has to cope with what a
recording actually holds - a message that never finished, a first frame with no flow control, frames
from before the trace started. It is what the Trace window shows when the transport view is on, and
what a diagnostic exchange looks like in CANoe's trace.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from canexpert.uds.client import FUNCTIONS, NRC_NAMES

# Service names by request identifier, from the functions the panel scripts and the console use.
SERVICE_NAMES = {entry.sid: entry.service for entry in FUNCTIONS if entry.sid}
SINGLE, FIRST, CONSECUTIVE, FLOW_CONTROL = 0x0, 0x1, 0x2, 0x3
FLOW_STATUS = {0x0: "continue", 0x1: "wait", 0x2: "overflow"}


def service_name(payload: bytes) -> str:
    """"ReadDataByIdentifier", "ReadDataByIdentifier response" or "negative response (NRC ...)"."""
    if not payload:
        return ""
    sid = payload[0]
    if sid == 0x7F:
        service = SERVICE_NAMES.get(payload[1], f"service 0x{payload[1]:02X}") if len(payload) > 1 else ""
        nrc = NRC_NAMES.get(payload[2], "unknown") if len(payload) > 2 else ""
        return f"{service} negative response ({nrc})".strip()
    if sid & 0x40 and (sid - 0x40) in SERVICE_NAMES:
        return f"{SERVICE_NAMES[sid - 0x40]} response"
    return SERVICE_NAMES.get(sid, f"service 0x{sid:02X}")


@dataclass
class TransportMessage:
    """One ISO-TP message: the payload, and the frames that carried it."""
    start: float
    direction: str
    can_id: int
    extended: bool = False
    payload: bytes = b""
    announced: int = 0                     # the length the first frame announced, 0 for a single frame
    frames: list = field(default_factory=list)      # (timestamp, label, data)
    flow_control: list = field(default_factory=list)   # (timestamp, status, block size, STmin byte)
    end: float = 0.0

    @property
    def complete(self) -> bool:
        return not self.announced or len(self.payload) >= self.announced

    @property
    def service(self) -> str:
        return service_name(self.payload)

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)

    def summary(self) -> str:
        """What the message is, in words: the service, and what is missing when it did not finish."""
        if not self.complete:
            missing = self.announced - len(self.payload)
            return f"{self.service} (incomplete: {missing} byte(s) missing)".strip()
        return self.service


def _stmin_text(value: int) -> str:
    if value <= 0x7F:
        return f"{value} ms"
    if 0xF1 <= value <= 0xF9:
        return f"{(value - 0xF0) * 100} us"
    return "reserved"


def assemble(frames, address_byte: int | None = None) -> list[TransportMessage]:
    """Group frames - (timestamp, direction, id, data, extended) - into ISO-TP messages.

    Only frames of the identifiers handed in should be given: an application frame whose first byte
    happens to look like a single frame would otherwise be read as a diagnostic message.
    """
    messages, open_messages = [], {}
    for timestamp, direction, can_id, data, extended in frames:
        body = bytes(data)[1:] if address_byte is not None else bytes(data)
        if not body:
            continue
        kind, key = body[0] >> 4, (can_id, direction)
        if kind == SINGLE:
            length = body[0] & 0x0F
            message = TransportMessage(timestamp, direction, can_id, extended, body[1:1 + length],
                                       0, [(timestamp, "single frame", bytes(data))], end=timestamp)
            messages.append(message)
            open_messages.pop(key, None)
        elif kind == FIRST:
            announced = ((body[0] & 0x0F) << 8) | body[1] if len(body) > 1 else 0
            first = body[2:]
            if announced == 0 and len(body) >= 6:          # the escape sequence for long messages
                announced, first = int.from_bytes(body[2:6], "big"), body[6:]
            message = TransportMessage(timestamp, direction, can_id, extended, first, announced,
                                       [(timestamp, "first frame", bytes(data))], end=timestamp)
            messages.append(message)
            open_messages[key] = message
        elif kind == CONSECUTIVE:
            message = open_messages.get(key)
            if message is None:                            # the start was before the trace began
                continue
            message.payload += body[1:]
            message.frames.append((timestamp, f"consecutive frame {body[0] & 0x0F}", bytes(data)))
            message.end = timestamp
            if message.complete:
                message.payload = message.payload[:message.announced]
                open_messages.pop(key, None)
        elif kind == FLOW_CONTROL and len(body) >= 3:
            # Flow control travels the other way; it belongs to whatever is being sent at the time.
            status = FLOW_STATUS.get(body[0] & 0x0F, f"0x{body[0] & 0x0F:X}")
            waiting = [m for m in open_messages.values() if m.can_id != can_id] or list(open_messages.values())
            if waiting:
                target = waiting[-1]
                target.flow_control.append((timestamp, status, body[1], body[2]))
                target.frames.append((timestamp, f"flow control: {status}, block size {body[1]}, "
                                                 f"STmin {_stmin_text(body[2])}", bytes(data)))
                target.end = timestamp
    return messages
