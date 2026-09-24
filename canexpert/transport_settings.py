"""
How CAN Expert talks ISO-TP to an ECU: frame padding, and the flow control it asks for when it receives.

These are kept in CAN Expert's own settings, one set per configuration name, and never in the
configuration file. At connect the main window folds them into the session's configuration
(apply_transport), and config.uds_transport() hands them on to every request, TesterPresent included.

A configuration dictionary without them - a test, a script's own - behaves as before: unpadded frames,
flow control of BS 0 / STmin 0.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields

from PyQt5.QtWidgets import QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QSpinBox

SETTINGS_GROUP = "transport"           # settings: transport/<configuration name> -> JSON
DEFAULT_PADDING_BYTE = 0xCC            # 11001100 needs no stuff bits, so a padded frame stays short on the wire


@dataclass
class TransportSettings:
    padding: bool = True               # fill every frame CAN Expert sends to 8 bytes
    padding_byte: int = DEFAULT_PADDING_BYTE
    block_size: int = 0                # BS the tester announces when an ECU sends it a long message; 0 = no limit
    st_min: int = 0                    # STmin byte it announces: 0x00-0x7F ms, 0xF1-0xF9 100-900 us

    @classmethod
    def from_dict(cls, values) -> "TransportSettings":
        names = {item.name for item in fields(cls)}
        return cls(**{name: value for name, value in (values or {}).items() if name in names})

    def check(self):
        """ValueError, with what to tell the user, for a value ISO 15765-2 does not allow."""
        if not 0 <= self.padding_byte <= 0xFF:
            raise ValueError("The padding byte must be between 00 and FF")
        if not 0 <= self.block_size <= 0xFF:
            raise ValueError("The block size must be between 0 and 255")
        if not (0 <= self.st_min <= 0x7F or 0xF1 <= self.st_min <= 0xF9):
            raise ValueError("STmin must be 00-7F (milliseconds) or F1-F9 (100-900 microseconds)")
        return self


def load_transport(settings, name: str) -> TransportSettings:
    """The transport settings kept for a configuration name; the defaults when there are none."""
    try:
        values = json.loads(settings.value(f"{SETTINGS_GROUP}/{name}", "", type=str) or "{}")
    except ValueError:
        values = {}
    try:
        return TransportSettings.from_dict(values).check()
    except (TypeError, ValueError):
        return TransportSettings()


def save_transport(settings, name: str, transport: TransportSettings):
    settings.setValue(f"{SETTINGS_GROUP}/{name}", json.dumps(asdict(transport.check())))


def apply_transport(config: dict, transport: TransportSettings) -> dict:
    """The configuration as a session uses it: a copy with the transport settings folded in."""
    session = dict(config)
    session["isotp_padding"] = transport.padding_byte if transport.padding else None
    session["isotp_block_size"] = transport.block_size
    session["isotp_st_min"] = transport.st_min
    return session


def pad(data: bytes, config: dict) -> bytes:
    """A frame as the configuration sends it: filled to 8 bytes when padding is on."""
    byte = config.get("isotp_padding")
    return bytes(data) if byte is None else bytes(data).ljust(8, bytes([byte]))


def st_min_text(value: int) -> str:
    if value <= 0x7F:
        return f"{value} ms"
    return f"{(value - 0xF0) * 100} us"


class TransportGroup(QGroupBox):
    """The transport settings as a form, for the configuration dialog."""

    def __init__(self, transport: TransportSettings, parent=None):
        super().__init__("ISO-TP (kept by CAN Expert, not in the configuration file)", parent)
        form = QFormLayout(self)
        row = QHBoxLayout()
        self.padding_cb = QCheckBox("Fill every frame to 8 bytes with")
        self.padding_cb.setToolTip("Most ECUs ignore diagnostic frames shorter than 8 bytes")
        self.padding_edit = QLineEdit()
        self.padding_edit.setFixedWidth(40)
        self.padding_cb.toggled.connect(self.padding_edit.setEnabled)
        row.addWidget(self.padding_cb)
        row.addWidget(self.padding_edit)
        row.addStretch()
        form.addRow("Padding:", row)
        self.block_size_spin = QSpinBox()
        self.block_size_spin.setRange(0, 255)
        self.block_size_spin.setSpecialValueText("no limit")
        self.block_size_spin.setToolTip("Consecutive frames the ECU may send before waiting for the next "
                                        "flow control frame")
        form.addRow("Block size asked of the ECU:", self.block_size_spin)
        stmin_row = QHBoxLayout()
        self.st_min_edit = QLineEdit()
        self.st_min_edit.setFixedWidth(40)
        self.st_min_edit.setToolTip("00-7F: milliseconds between consecutive frames; F1-F9: 100-900 us")
        self.st_min_label = QLabel("")
        self.st_min_label.setStyleSheet("color: gray;")
        self.st_min_edit.textChanged.connect(self._show_st_min)
        stmin_row.addWidget(self.st_min_edit)
        stmin_row.addWidget(self.st_min_label)
        stmin_row.addStretch()
        form.addRow("STmin asked of the ECU (hex):", stmin_row)
        self.set_transport(transport)

    def _show_st_min(self, text):
        try:
            value = int(text.strip() or "0", 16)
            TransportSettings(st_min=value).check()
            self.st_min_label.setText(st_min_text(value))
        except ValueError:
            self.st_min_label.setText("00-7F or F1-F9")

    def set_transport(self, transport: TransportSettings):
        self.padding_cb.setChecked(transport.padding)
        self.padding_edit.setText(f"{transport.padding_byte:02X}")
        self.padding_edit.setEnabled(transport.padding)
        self.block_size_spin.setValue(transport.block_size)
        self.st_min_edit.setText(f"{transport.st_min:02X}")

    def transport(self) -> TransportSettings:
        """The settings the form holds; ValueError with a message for the user when one is invalid."""
        def number(edit, what):
            try:
                return int(edit.text().strip() or "0", 16)
            except ValueError:
                raise ValueError(f"{what} must be hexadecimal, e.g. CC") from None

        return TransportSettings(padding=self.padding_cb.isChecked(),
                                 padding_byte=number(self.padding_edit, "The padding byte"),
                                 block_size=self.block_size_spin.value(),
                                 st_min=number(self.st_min_edit, "STmin")).check()
