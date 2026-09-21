"""
Which frames the Trace shows: identifiers and ranges, message names, and the direction.

    7E0, 300-3FF, EngineData      identifiers, hexadecimal ranges, text found in the message name

Pass shows only what matches, Stop hides it, and the direction choice (RX and TX, RX only, TX only)
applies on top of either.
"""
from __future__ import annotations

from dataclasses import dataclass, field

FILTER_MODES = ("Pass", "Stop")
DIRECTIONS = ("RX and TX", "RX only", "TX only")


def parse_filter(text: str) -> tuple[list[tuple[int, int]], list[str]]:
    """'7E0, 300-3FF, Engine' -> ([(0x7E0, 0x7E0), (0x300, 0x3FF)], ['engine']).

    Terms are hexadecimal identifiers, hexadecimal ranges, or text matched against the message name.
    """
    ranges, names = [], []
    for term in (part.strip() for part in str(text).replace(";", ",").split(",")):
        if not term:
            continue
        first, dash, last = term.replace("0x", "").replace("0X", "").partition("-")
        try:
            low = int(first.strip(), 16)
            high = int(last.strip(), 16) if dash else low
        except ValueError:
            names.append(term.lower())
            continue
        ranges.append((min(low, high), max(low, high)))
    return ranges, names


@dataclass
class FrameFilter:
    ranges: list = field(default_factory=list)
    names: list = field(default_factory=list)
    mode: str = "Pass"
    direction: str = "RX and TX"

    @classmethod
    def from_text(cls, text: str, mode: str = "Pass", direction: str = "RX and TX") -> "FrameFilter":
        ranges, names = parse_filter(text)
        return cls(ranges, names, mode, direction)

    @property
    def empty(self) -> bool:
        return not self.ranges and not self.names and self.direction == "RX and TX"

    def passes(self, direction: str, can_id: int, name: str = "") -> bool:
        if self.direction == "RX only" and direction != "RX":
            return False
        if self.direction == "TX only" and direction != "TX":
            return False
        if not self.ranges and not self.names:
            return True
        name = (name or "").lower()
        matched = any(low <= can_id <= high for low, high in self.ranges) or \
            any(text in name for text in self.names if name)
        return matched if self.mode == "Pass" else not matched
