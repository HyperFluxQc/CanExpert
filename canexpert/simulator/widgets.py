"""Small widgets of the Dummy ECU window: a grey explanation under a setting, and hexadecimal entry."""
from PyQt5.QtWidgets import QLabel, QSpinBox


def hint(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #6b7280;")
    return label


class HexSpinBox(QSpinBox):
    """Hexadecimal entry for CAN IDs, bytes and routine identifiers."""

    def __init__(self, maximum: int):
        super().__init__()
        self.setDisplayIntegerBase(16)
        self.setPrefix("0x")
        self.setRange(0, maximum)

    def textFromValue(self, value):
        return f"{value:X}"
