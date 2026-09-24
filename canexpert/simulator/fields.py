"""Text in the Dummy ECU window's fields and back: byte and DID lists, address formats, ranges, sessions,
STmin, numbers, and the names of services refused on purpose. No Qt: the window and its tests share them."""
from __future__ import annotations

from canexpert.simulator.ecu import SERVICE_NAMES, SESSION_NAMES
from canexpert.uds.client import NRC_NAMES


def printable(data: bytes) -> str:
    """Bytes as text where they are text, for the DID table's preview."""
    return "".join(chr(byte) if 32 <= byte < 127 else "." for byte in data)


def forced_text(sid: int, nrc: int) -> str:
    """"SecurityAccess: requiredTimeDelayNotExpired" - which service is refused, and how."""
    return f"{SERVICE_NAMES.get(sid, f'service {sid:02X}')}: {NRC_NAMES.get(nrc, 'unknown NRC')}"


def parse_byte_list(text: str) -> tuple[int, ...]:
    """'00, 11' -> (0x00, 0x11)."""
    values = tuple(int(part, 16) for part in text.replace(",", " ").split())
    if not values or any(not 0 <= value <= 0xFF for value in values):
        raise ValueError("expected hexadecimal bytes, e.g. 00, 11")
    return values


def parse_did_list(text: str) -> tuple[int, ...]:
    """'0101, 0102' -> (0x0101, 0x0102); empty -> ()."""
    try:
        values = tuple(int(part, 16) for part in text.replace(",", " ").split())
    except ValueError:
        raise ValueError("expected DIDs in hexadecimal, e.g. 0101, 0102") from None
    if any(not 0 <= value <= 0xFFFF for value in values):
        raise ValueError("a DID is 0000-FFFF")
    return values


def parse_address_format(text: str) -> int | None:
    """'Any' -> None, '44' -> 0x44 (both nibbles must be set)."""
    text = text.strip()
    if not text or text.lower() == "any":
        return None
    value = int(text, 16)
    if not 0 <= value <= 0xFF or not value & 0x0F or not value >> 4:
        raise ValueError("expected Any or a byte like 44")
    return value


def parse_ranges(text: str) -> tuple:
    """'10000-1FFFF, 20000-2FFFF' -> ((0x10000, 0x1FFFF), (0x20000, 0x2FFFF)); empty -> () (any address)."""
    ranges = []
    for part in text.replace(";", ",").split(","):
        if not part.strip():
            continue
        first, separator, last = part.partition("-")
        if not separator:
            raise ValueError(f"expected first-last, got {part.strip()}")
        first, last = int(first, 16), int(last, 16)
        if last < first:
            raise ValueError(f"{part.strip()} ends before it starts")
        ranges.append((first, last))
    return tuple(ranges)


def format_ranges(ranges) -> str:
    return ", ".join(f"{first:08X}-{last:08X}" for first, last in ranges)


SESSION_WORDS = {"default": 0x01, "d": 0x01, "programming": 0x02, "p": 0x02, "extended": 0x03, "e": 0x03}


def parse_sessions(text: str) -> list[int]:
    """'default, extended' (or 'd e', or '01 03') -> [1, 3]; empty -> [] (any session)."""
    sessions = []
    for word in text.replace(",", " ").split():
        word = word.lower()
        try:
            session = SESSION_WORDS[word] if word in SESSION_WORDS else int(word, 16)
        except ValueError:
            raise ValueError(f"{word!r} is no session: default, programming, extended or a number") from None
        if not 0 < session <= 0x7F:
            raise ValueError(f"session {session:02X}: sessions are 01-7F")
        if session not in sessions:
            sessions.append(session)
    return sessions


def format_sessions(sessions) -> str:
    return ", ".join(SESSION_NAMES.get(session, f"{session:02X}") for session in sessions or ())


def stmin_text(value: int) -> str:
    if value <= 0x7F:
        return f"{value} ms"
    if 0xF1 <= value <= 0xF9:
        return f"{(value - 0xF0) * 100} µs"
    return "127 ms (reserved value)"


def number_text(value) -> str:
    return "" if value is None else f"{value:.6g}"
