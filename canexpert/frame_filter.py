"""
Which frames a window shows: identifiers and ranges, message names, and the direction - one syntax and
one bar, shared by the Trace and the CAN monitor.

    7E0, 300-3FF, EngineData      identifiers, hexadecimal ranges, text found in the message name

Pass shows only what matches, Stop hides it, and the direction choice (RX and TX, RX only, TX only)
applies on top of either.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QComboBox, QHBoxLayout, QLineEdit, QWidget

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


class FilterBar(QWidget):
    """Pass/Stop, direction and the filter text; changed() carries the FrameFilter they describe."""
    changed = pyqtSignal(object)

    def __init__(self, placeholder="Filter: 7E0, 300-3FF, EngineData", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(FILTER_MODES)
        self.mode_combo.setToolTip("Pass shows only what matches; Stop hides what matches")
        self.direction_combo = QComboBox()
        self.direction_combo.addItems(DIRECTIONS)
        self.direction_combo.setToolTip("Received frames, frames CAN Expert sent, or both")
        self.text_edit = QLineEdit()
        self.text_edit.setPlaceholderText(placeholder)
        self.text_edit.setClearButtonEnabled(True)
        layout.addWidget(self.mode_combo)
        layout.addWidget(self.direction_combo)
        layout.addWidget(self.text_edit, 1)
        for signal in (self.mode_combo.currentTextChanged, self.direction_combo.currentTextChanged,
                       self.text_edit.textChanged):
            signal.connect(lambda *_: self.changed.emit(self.filter()))

    def filter(self) -> FrameFilter:
        return FrameFilter.from_text(self.text_edit.text(), self.mode_combo.currentText(),
                                     self.direction_combo.currentText())
