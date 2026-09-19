"""Dummy ECU window: connect the simulated ECU to a CAN channel (e.g. a Kvaser virtual channel) and set how it
answers - addressing, ISO-TP flow control, UDS timing and security, flashing. Changes apply at once, even while
connected; the settings are remembered, and can be saved and loaded as JSON profiles."""
from __future__ import annotations

import collections
import json
import sys
import threading
import time
from dataclasses import asdict, fields, replace

import can
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dummy_ecu import (
    DEFAULT_CONNECTION,
    SESSION_NAMES,
    DummyEcu,
    EcuConfig,
    claim_channel,
    config_from_dict,
    load_profile,
    other_ecu_present,
    parse_channel,
    save_profile,
)
from uds_services import flow_control_frame
from ui_common import app_settings, toolbar_icon

INTERFACES = ("kvaser", "virtual", "vector", "ixxat", "pcan", "socketcan")
BITRATES = ("125000", "250000", "500000", "1000000")
ADDRESS_FORMATS = ("Any", "44", "24", "34", "14", "33", "22", "11")
SETTINGS_KEY = "dummy_ecu/profile"
ERROR_STYLE = "background: #fde2e2;"


# --- text fields ------------------------------------------------------------------------

def parse_byte_list(text: str) -> tuple[int, ...]:
    """'00, 11' -> (0x00, 0x11)."""
    values = tuple(int(part, 16) for part in text.replace(",", " ").split())
    if not values or any(not 0 <= value <= 0xFF for value in values):
        raise ValueError("expected hexadecimal bytes, e.g. 00, 11")
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


def stmin_text(value: int) -> str:
    if value <= 0x7F:
        return f"{value} ms"
    if 0xF1 <= value <= 0xF9:
        return f"{(value - 0xF0) * 100} µs"
    return "127 ms (reserved value)"


def hint(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #6b7280;")
    return label


def _stamp() -> str:
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"


def saved_profile() -> tuple[EcuConfig, dict]:
    """The settings the window had when it was last closed."""
    try:
        values = json.loads(app_settings().value(SETTINGS_KEY, "") or "{}")
        return config_from_dict(values.get("ecu", {})), dict(values.get("connection", {}))
    except (ValueError, TypeError):
        return EcuConfig(), {}


class HexSpinBox(QSpinBox):
    """Hexadecimal entry for CAN IDs, bytes and routine identifiers."""

    def __init__(self, maximum: int):
        super().__init__()
        self.setDisplayIntegerBase(16)
        self.setPrefix("0x")
        self.setRange(0, maximum)

    def textFromValue(self, value):
        return f"{value:X}"


class DummyEcuWindow(QMainWindow):
    def __init__(self, config: EcuConfig | None = None, connection: dict | None = None):
        super().__init__()
        self.setWindowTitle("Dummy ECU")
        self.setWindowIcon(toolbar_icon("diagnostics"))
        self.resize(1200, 780)
        self._lines = collections.deque()          # log lines from the ECU thread, shown by a timer
        self._bus = self._lock = self._stop = self._thread = None
        self._loading = True                         # widgets being built or filled: no _apply
        self.ecu = DummyEcu(None, replace(config or EcuConfig()), log=self._log)
        self._build()
        self._fill(self.ecu.config, {**DEFAULT_CONNECTION, **(connection or {})})
        self._apply()                                # the ECU uses exactly what the widgets show
        self._update_labels()
        self._set_connected(False)
        self._flush_timer = QTimer(self)
        self._flush_timer.timeout.connect(self._flush_log)
        self._flush_timer.start(100)
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(250)
        QTimer.singleShot(0, self.detect_channels)

    # --- widgets ------------------------------------------------------------------------

    def _spin(self, low, high, step=1, suffix=""):
        box = QSpinBox()
        box.setRange(low, high)
        box.setSingleStep(step)
        box.setSuffix(suffix)
        box.valueChanged.connect(self._apply)
        return box

    def _double(self, low, high, decimals, suffix=""):
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(decimals)
        box.setSuffix(suffix)
        box.valueChanged.connect(self._apply)
        return box

    def _hex(self, maximum):
        box = HexSpinBox(maximum)
        box.valueChanged.connect(self._apply)
        return box

    def _check(self, text):
        box = QCheckBox(text)
        box.toggled.connect(self._apply)
        return box

    def _line(self, placeholder=""):
        line = QLineEdit()
        line.setPlaceholderText(placeholder)
        line.textChanged.connect(self._apply)
        return line

    @staticmethod
    def _page(*groups):
        page = QWidget()
        layout = QVBoxLayout(page)
        for group in groups:
            layout.addWidget(group)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    @staticmethod
    def _group(title):
        group = QGroupBox(title)
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)  # text fields grow, numbers keep their size
        return group, form

    @staticmethod
    def _row(*widgets):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            layout.addWidget(widget)
        layout.addStretch()
        return row

    def _build(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._connection_bar())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._addressing_page(), "Addressing")
        self.tabs.addTab(self._flow_page(), "Flow control")
        self.tabs.addTab(self._uds_page(), "UDS")
        self.tabs.addTab(self._flashing_page(), "Flashing")
        self.tabs.addTab(self._periodic_page(), "Periodic frames")
        settings = QWidget()
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.addWidget(self.tabs)
        buttons = QHBoxLayout()
        for text, slot in (("Load profile...", self.load_profile), ("Save profile...", self.save_profile),
                           ("Restore defaults", self.restore_defaults)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        settings_layout.addLayout(buttons)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(settings)
        splitter.addWidget(self._monitor())
        splitter.setSizes([600, 600])
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _connection_bar(self):
        box = QGroupBox("Connection")
        row = QHBoxLayout(box)
        self.interface = QComboBox()
        self.interface.setEditable(True)
        self.interface.addItems(INTERFACES)
        self.interface.activated.connect(lambda _: self.detect_channels())
        self.channel = QComboBox()
        self.channel.setEditable(True)
        self.channel.setMinimumContentsLength(26)
        self.bitrate = QComboBox()
        self.bitrate.setEditable(True)
        self.bitrate.addItems(BITRATES)
        self.detect_button = QPushButton("Detect")
        self.detect_button.setToolTip("List the channels of this interface")
        self.detect_button.clicked.connect(self.detect_channels)
        self.connection_state = QLabel()
        self.connect_button = QPushButton("Connect")
        self.connect_button.setMinimumWidth(110)
        self.connect_button.clicked.connect(self.toggle_connection)
        for label, widget in (("Interface", self.interface), ("Channel", self.channel), ("Bit rate", self.bitrate)):
            row.addWidget(QLabel(label))
            row.addWidget(widget)
        row.addWidget(self.detect_button)
        row.addStretch()
        row.addWidget(self.connection_state)
        row.addWidget(self.connect_button)
        return box

    def _addressing_page(self):
        ids, form = self._group("CAN identifiers")
        self.request_id, self.functional_id, self.response_id = (self._hex(0x1FFFFFFF) for _ in range(3))
        self.extended_ids = self._check("29-bit identifiers")
        form.addRow("Physical request ID", self.request_id)
        form.addRow(hint("Requests addressed to this ECU (tester → ECU); segmented requests must use it."))
        form.addRow("Functional request ID", self.functional_id)
        form.addRow(hint("Requests to every ECU, such as the OBD broadcast 7DF (single frames only)."))
        form.addRow("Response ID", self.response_id)
        form.addRow(hint("This ECU's responses and flow control frames (ECU → tester)."))
        form.addRow("", self.extended_ids)
        self.can_expert_hint = hint()
        form.addRow(self.can_expert_hint)
        frames, form = self._group("Frames")
        self.use_address_byte = self._check("Extended addressing byte")
        self.address_byte = self._hex(0xFF)
        self.use_padding = self._check("Pad frames to 8 bytes with")
        self.padding = self._hex(0xFF)
        form.addRow(self.use_address_byte, self.address_byte)
        form.addRow(hint("ISO-TP extended addressing: every frame starts with this byte."))
        form.addRow(self.use_padding, self.padding)
        form.addRow(hint("Without padding, frames are only as long as their content (e.g. 3 bytes for 02 7E 00)."))
        return self._page(ids, frames)

    def _flow_page(self):
        group, form = self._group("Flow control the ECU sends for segmented requests")
        self.block_size = self._spin(0, 255, suffix=" frames")
        self.block_size.setSpecialValueText("0 (no limit)")
        self.st_min = self._spin(0, 127)
        self.st_min_unit = QComboBox()
        self.st_min_unit.addItems(["ms", "× 100 µs"])
        self.st_min_unit.currentIndexChanged.connect(self._stmin_unit_changed)
        self.flow_waits = self._spin(0, 30, suffix=" frames")
        self.wait_interval = self._spin(10, 990, step=10, suffix=" ms")
        self.rx_buffer = self._spin(0, 0xFFFFFF, suffix=" bytes")
        self.rx_buffer.setSpecialValueText("maxNumberOfBlockLength")
        form.addRow("Block size (BS)", self.block_size)
        form.addRow(hint("Consecutive frames the tester sends before it waits for the next flow control frame."))
        form.addRow("STmin", self._row(self.st_min, self.st_min_unit))
        form.addRow(hint("Minimum gap the tester keeps between consecutive frames: 0-127 ms, or 100-900 µs."))
        form.addRow("WAIT frames", self.flow_waits)
        form.addRow("WAIT interval", self.wait_interval)
        form.addRow(hint("Flow control WAIT (31 00 00) sent before each ContinueToSend, like a busy ECU. "
                         "CAN Expert accepts up to 16 in a row, each within 1 s."))
        form.addRow("Receive buffer", self.rx_buffer)
        form.addRow(hint("Longest request the ECU accepts; a longer first frame is refused with flow control "
                         "overflow (32 00 00)."))
        self.flow_preview = hint()
        form.addRow(self.flow_preview)
        return self._page(group)

    def _uds_page(self):
        timing, form = self._group("Timing")
        self.p2 = self._spin(1, 0xFFFF, suffix=" ms")
        self.p2_star = self._spin(10, 655350, step=10, suffix=" ms")
        self.response_delay = self._spin(0, 60000, step=10, suffix=" ms")
        self.pending_interval = self._spin(100, 60000, step=100, suffix=" ms")
        self.s3 = self._double(0.5, 600, 1, " s")
        form.addRow("P2 server", self.p2)
        form.addRow("P2* server", self.p2_star)
        self.timing_hint = hint()
        form.addRow(self.timing_hint)
        form.addRow("Response delay", self.response_delay)
        form.addRow(hint("Time the ECU takes per request. Longer than P2: it first answers NRC 0x78 "
                         "(response pending), repeated every pending interval."))
        form.addRow("Pending interval", self.pending_interval)
        form.addRow("S3 timeout", self.s3)
        form.addRow(hint("Back to the default session when no request arrives for this long."))
        sessions, form = self._group("Sessions")
        self.programming_needs_extended = self._check("Programming session only from the extended session")
        form.addRow(self.programming_needs_extended)
        form.addRow(hint("Otherwise 10 02 from the default session gets NRC 0x22."))
        security, form = self._group("SecurityAccess (0x27)")
        self.security_level = self._hex(0x7F)
        self.security_level.setRange(1, 0x7F)
        self.security_level.setSingleStep(2)
        self.seed_length = self._spin(1, 16, suffix=" bytes")
        self.key_mask = self._hex(0xFF)
        self.max_attempts = self._spin(1, 10)
        self.lockout = self._double(0, 600, 1, " s")
        form.addRow("Level (requestSeed)", self.security_level)
        form.addRow("Seed length", self.seed_length)
        form.addRow("Key XOR mask", self.key_mask)
        self.security_hint = hint()
        form.addRow(self.security_hint)
        form.addRow("Wrong keys allowed", self.max_attempts)
        form.addRow("Lockout delay", self.lockout)
        form.addRow(hint("After that many wrong keys: NRC 0x36, then 0x37 until the delay has passed."))
        return self._page(timing, sessions, security)

    def _flashing_page(self):
        download, form = self._group("RequestDownload (0x34) and TransferData (0x36)")
        self.block_data = self._spin(1, 0xFFFD, suffix=" bytes")
        self.length_bytes = self._spin(1, 4, suffix=" bytes")
        self.full_blocks = self._check("Require full blocks (every TransferData but the last)")
        self.data_formats = self._line("00")
        self.address_format = QComboBox()
        self.address_format.setEditable(True)
        self.address_format.addItems(ADDRESS_FORMATS)
        self.address_format.currentTextChanged.connect(self._apply)
        self.memory_ranges = self._line("any address, e.g. 00010000-0001FFFF, 00020000-0002FFFF")
        form.addRow("Data per TransferData", self.block_data)
        self.block_hint = hint()
        form.addRow(self.block_hint)
        form.addRow("Length field", self.length_bytes)
        form.addRow(hint("Bytes used for maxNumberOfBlockLength in the response (lengthFormatIdentifier)."))
        form.addRow("", self.full_blocks)
        form.addRow(hint("Otherwise any block up to the maximum is accepted, as ISO 14229 allows; a short block "
                         "then gets NRC 0x13."))
        form.addRow("dataFormatIdentifier", self.data_formats)
        form.addRow(hint("Accepted values, e.g. 00, 11. High nibble compression, low nibble encryption, 00 = "
                         "neither. Other accepted values are stored as received (not decoded); the rest get "
                         "NRC 0x31."))
        form.addRow("addressAndLengthFormat", self.address_format)
        form.addRow(hint("Low nibble: memoryAddress bytes, high nibble: memorySize bytes (44 = 4 and 4). "
                         "Any: every format; otherwise other formats get NRC 0x31."))
        form.addRow("Memory ranges", self.memory_ranges)
        form.addRow(hint("Addresses open to erase, download and upload (first-last, hex); outside: NRC 0x31. "
                         "Empty: any address."))
        routines, form = self._group("RoutineControl (0x31)")
        self.require_erase = self._check("Require erase before RequestDownload")
        self.erase_routine = self._hex(0xFFFF)
        self.erase_seconds = self._double(0, 120, 2, " s")
        self.check_routine = self._hex(0xFFFF)
        form.addRow("", self.require_erase)
        form.addRow(hint("RequestDownload of a range not erased gets NRC 0x70."))
        form.addRow("Erase routine", self.erase_routine)
        form.addRow("Erase time", self.erase_seconds)
        form.addRow(hint("Option record: addressAndLengthFormatIdentifier, address, size (31 01 FF 00 44 ...). "
                         "NRC 0x78 is sent while erasing."))
        form.addRow("Check routine", self.check_routine)
        form.addRow(hint("checkProgrammingDependencies: status 00 once a download completed; the software "
                         "version (F195) then changes."))
        other, form = self._group("Upload and image")
        self.allow_upload = self._check("Allow RequestUpload (0x35) to read the memory back")
        self.dump_path = self._line("not saved")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_dump)
        form.addRow("", self.allow_upload)
        form.addRow("Save image to", self._row(self.dump_path, browse))
        form.addRow(hint("Written as S-records after checkProgrammingDependencies."))
        self.dump_path.setMinimumWidth(260)
        return self._page(download, routines, other)

    def _periodic_page(self):
        group, form = self._group("Application frames")
        self.broadcast = self._check("Send 0x300 and 0x301")
        self.broadcast_interval = self._spin(10, 10000, step=10, suffix=" ms")
        form.addRow("", self.broadcast)
        form.addRow("Period", self.broadcast_interval)
        form.addRow(hint("0x300: temperature and pressure, 0x301: status and counter (DBC/dummy_ecu.dbc). They "
                         "stop while CommunicationControl disables normal messages. 0x200 (01 start, 02 stop) and "
                         "0x201 (bit 0 logging) are accepted as commands."))
        return self._page(group)

    def _monitor(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        status = QGroupBox("ECU status")
        form = QFormLayout(status)
        self.status_labels = {}
        for name in ("Session", "Security", "Transfer", "Memory", "Software version"):
            label = QLabel("-")
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(name, label)
            self.status_labels[name] = label
        reset = QPushButton("Reset ECU")
        reset.setToolTip("Back to the factory state: default session, locked, original DIDs and DTCs, erased memory")
        reset.clicked.connect(self.reset_ecu)
        save_image = QPushButton("Save memory as S-record...")
        save_image.clicked.connect(self.save_memory)
        form.addRow(self._row(reset, save_image))
        layout.addWidget(status)
        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        self.show_frames = QCheckBox("Show CAN frames (diagnostic IDs)")
        self.show_frames.toggled.connect(self._show_frames)
        clear = QPushButton("Clear")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_view.setFont(QFont("Consolas", 9))
        clear.clicked.connect(self.log_view.clear)
        log_layout.addWidget(self._row(self.show_frames, clear))
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_box, 1)
        return panel

    # --- settings <-> widgets ---------------------------------------------------------------

    def _stmin_unit_changed(self, index):
        if index == 0:
            self.st_min.setRange(0, 127)
        else:
            self.st_min.setRange(1, 9)
        self._apply()

    def _fill(self, config: EcuConfig, connection: dict | None = None):
        self._loading = True
        try:
            if connection:
                self.interface.setCurrentText(str(connection.get("interface", DEFAULT_CONNECTION["interface"])))
                self.channel.setEditText(str(connection.get("channel", DEFAULT_CONNECTION["channel"])))
                self.bitrate.setCurrentText(str(connection.get("bitrate", DEFAULT_CONNECTION["bitrate"])))
            self.request_id.setValue(config.request_id)
            self.functional_id.setValue(config.functional_id)
            self.response_id.setValue(config.response_id)
            self.extended_ids.setChecked(config.extended_ids)
            self.use_address_byte.setChecked(config.address_byte is not None)
            self.address_byte.setValue(config.address_byte or 0)
            self.use_padding.setChecked(config.padding is not None)
            self.padding.setValue(0xAA if config.padding is None else config.padding)
            self.block_size.setValue(config.block_size)
            microseconds = 0xF1 <= config.st_min <= 0xF9
            self.st_min_unit.setCurrentIndex(1 if microseconds else 0)
            self.st_min.setRange(*((1, 9) if microseconds else (0, 127)))
            self.st_min.setValue(config.st_min - 0xF0 if microseconds else min(config.st_min, 127))
            self.flow_waits.setValue(config.flow_waits)
            self.wait_interval.setValue(config.wait_interval_ms)
            self.rx_buffer.setValue(config.rx_buffer)
            self.p2.setValue(config.p2_ms)
            self.p2_star.setValue(config.p2_star_ms)
            self.response_delay.setValue(config.response_delay_ms)
            self.pending_interval.setValue(round(config.pending_interval * 1000))
            self.s3.setValue(config.s3_timeout)
            self.programming_needs_extended.setChecked(config.programming_needs_extended)
            self.security_level.setValue(config.security_level)
            self.seed_length.setValue(config.seed_length)
            self.key_mask.setValue(config.key_mask)
            self.max_attempts.setValue(config.max_attempts)
            self.lockout.setValue(config.lockout_seconds)
            self.block_data.setValue(max(1, config.max_block_length - 2))
            self.length_bytes.setValue(config.block_length_bytes)
            self.full_blocks.setChecked(config.full_blocks)
            self.data_formats.setText(", ".join(f"{value:02X}" for value in config.data_formats))
            self.address_format.setCurrentText("Any" if config.address_format is None
                                               else f"{config.address_format:02X}")
            self.memory_ranges.setText(format_ranges(config.memory_ranges))
            self.require_erase.setChecked(config.require_erase)
            self.erase_routine.setValue(config.erase_routine)
            self.erase_seconds.setValue(config.erase_seconds)
            self.check_routine.setValue(config.check_routine)
            self.allow_upload.setChecked(config.allow_upload)
            self.dump_path.setText(config.dump_path or "")
            self.broadcast.setChecked(config.broadcast_interval > 0)
            self.broadcast_interval.setValue(round(config.broadcast_interval * 1000) or 100)
        finally:
            self._loading = False

    def _read_config(self) -> EcuConfig:
        """Settings from the widgets; ValueError (and the field marked red) when a text field is invalid."""
        errors = []

        def parsed(widget, parse):
            text = widget.text() if isinstance(widget, QLineEdit) else widget.currentText()
            try:
                value = parse(text)
            except ValueError as exc:
                widget.setStyleSheet(ERROR_STYLE)
                widget.setToolTip(str(exc))
                errors.append(str(exc))
                return None
            widget.setStyleSheet("")
            widget.setToolTip("")
            return value

        data_formats = parsed(self.data_formats, parse_byte_list)
        address_format = parsed(self.address_format, parse_address_format)
        memory_ranges = parsed(self.memory_ranges, parse_ranges)
        if errors:
            raise ValueError("; ".join(errors))
        st_min = self.st_min.value() if self.st_min_unit.currentIndex() == 0 else 0xF0 + self.st_min.value()
        return EcuConfig(
            request_id=self.request_id.value(), functional_id=self.functional_id.value(),
            response_id=self.response_id.value(), extended_ids=self.extended_ids.isChecked(),
            address_byte=self.address_byte.value() if self.use_address_byte.isChecked() else None,
            padding=self.padding.value() if self.use_padding.isChecked() else None,
            block_size=self.block_size.value(), st_min=st_min, flow_waits=self.flow_waits.value(),
            wait_interval_ms=self.wait_interval.value(), rx_buffer=self.rx_buffer.value(),
            p2_ms=self.p2.value(), p2_star_ms=self.p2_star.value(), response_delay_ms=self.response_delay.value(),
            pending_interval=self.pending_interval.value() / 1000, s3_timeout=self.s3.value(),
            programming_needs_extended=self.programming_needs_extended.isChecked(),
            security_level=self.security_level.value() | 1, seed_length=self.seed_length.value(),
            key_mask=self.key_mask.value(), max_attempts=self.max_attempts.value(),
            lockout_seconds=self.lockout.value(),
            data_formats=data_formats, address_format=address_format,
            max_block_length=self.block_data.value() + 2, block_length_bytes=self.length_bytes.value(),
            full_blocks=self.full_blocks.isChecked(), memory_ranges=memory_ranges,
            require_erase=self.require_erase.isChecked(), erase_routine=self.erase_routine.value(),
            check_routine=self.check_routine.value(), erase_seconds=self.erase_seconds.value(),
            allow_upload=self.allow_upload.isChecked(),
            broadcast_interval=self.broadcast_interval.value() / 1000 if self.broadcast.isChecked() else 0,
            dump_path=self.dump_path.text().strip() or None,
        )

    def _apply(self, *_):
        """Copy the widgets into the running ECU's settings (they apply to the next frame)."""
        if self._loading:
            return
        try:
            config = self._read_config()
        except ValueError as exc:
            self.statusBar().showMessage(f"Not applied: {exc}")
            return
        for item in fields(EcuConfig):
            setattr(self.ecu.config, item.name, getattr(config, item.name))
        self.statusBar().clearMessage()
        self._update_labels()

    def _update_labels(self):
        config = self.ecu.config
        self.address_byte.setEnabled(self.use_address_byte.isChecked())
        self.padding.setEnabled(self.use_padding.isChecked())
        self.broadcast_interval.setEnabled(self.broadcast.isChecked())
        self.wait_interval.setEnabled(config.flow_waits > 0)
        obd = (not config.extended_ids and config.functional_id == 0x7DF and 0x7E8 <= config.response_id <= 0x7EF
               and config.request_id == config.response_id - 8)
        self.can_expert_hint.setText(
            f"In CAN Expert's configuration: SERVER ID {config.request_id:X} and ECU ID {config.response_id:X}."
            + (" SERVER ID 7DF works too: CAN Expert then sends UDS requests to ECU ID - 8." if obd else ""))
        waits = "31 00 00 " * config.flow_waits
        frame = flow_control_frame(config.block_size, config.st_min).hex(" ").upper()
        per_block = f"{config.block_size} frames per block" if config.block_size else "the whole message at once"
        self.flow_preview.setText(f"Sent after each first frame{' and each block' if config.block_size else ''}: "
                                  f"{waits}{frame} ({per_block}, at least {stmin_text(config.st_min)} apart). "
                                  f"Receive buffer: {config.receive_buffer} bytes.")
        self.timing_hint.setText(f"Announced in the DiagnosticSessionControl response: 50 xx "
                                 f"{config.p2_ms.to_bytes(2, 'big').hex(' ').upper()} "
                                 f"{(config.p2_star_ms // 10).to_bytes(2, 'big').hex(' ').upper()} "
                                 f"(P2 in ms, P2* in 10 ms units).")
        level = config.security_level
        self.security_hint.setText(f"requestSeed 27 {level:02X}, sendKey 27 {level + 1:02X}; key = each seed byte "
                                   f"XOR {config.key_mask:02X}. The example scripts' compute_key() uses A5.")
        maximum = config.max_block_length
        length = max(config.block_length_bytes, (maximum.bit_length() + 7) // 8)
        response = bytes([0x74, length << 4]) + maximum.to_bytes(length, "big")
        self.block_hint.setText(f"maxNumberOfBlockLength = {maximum} (0x{maximum:X}): the data plus the 0x36 SID and "
                                f"the block counter. Response: {response.hex(' ').upper()}. CAN Expert's Flashing() "
                                f"then sends TransferData blocks of {maximum - 2} bytes.")

    # --- connection ----------------------------------------------------------------------

    def _channel(self):
        text = self.channel.currentText().strip()
        if not text:
            raise ValueError("Choose a channel")
        return parse_channel(text.split()[0])

    def _connection(self) -> dict:
        try:
            bitrate = int(self.bitrate.currentText())
        except ValueError:
            bitrate = DEFAULT_CONNECTION["bitrate"]
        text = self.channel.currentText().strip()
        return {"interface": self.interface.currentText().strip(), "bitrate": bitrate,
                "channel": text.split()[0] if text else DEFAULT_CONNECTION["channel"]}

    def detect_channels(self):
        if self._thread:
            return
        interface = self.interface.currentText().strip()
        current = self.channel.currentText()
        self.channel.clear()
        try:
            configs = can.detect_available_configs(interfaces=[interface], timeout=2.0)
        except Exception as exc:
            configs = []
            self._log(f"{interface} channel detection: {exc}")
        for cfg in configs:
            name = cfg.get("device_name") or cfg.get("description") or ""
            self.channel.addItem(f"{cfg.get('channel', '')}  {name}".strip())
        token = current.split()[0] if current.strip() else ""
        matching = [index for index in range(self.channel.count()) if self.channel.itemText(index).split()[0] == token]
        if matching:
            self.channel.setCurrentIndex(matching[0])
        else:
            self.channel.setEditText(current)

    def toggle_connection(self):
        if self._thread:
            self.disconnect_ecu()
        else:
            self.connect_ecu()

    def connect_ecu(self) -> bool:
        try:
            channel, bitrate = self._channel(), int(self.bitrate.currentText())
        except ValueError as exc:
            QMessageBox.warning(self, "Dummy ECU", str(exc) if "channel" in str(exc) else "Enter a numeric bit rate.")
            return False
        interface = self.interface.currentText().strip()
        lock = claim_channel(interface, channel)
        if lock is None:
            QMessageBox.warning(self, "Dummy ECU", f"Another dummy ECU already runs on {interface} channel {channel}. "
                                "Close it first: two ECUs answering the same requests break security access "
                                "and flashing.")
            return False
        try:
            bus = can.Bus(interface=interface, channel=channel, bitrate=bitrate)
        except Exception as exc:
            lock.close()
            QMessageBox.warning(self, "Dummy ECU", f"Cannot open {interface} channel {channel}:\n{exc}")
            return False
        if other_ecu_present(bus, self.ecu.config) and QMessageBox.question(
                self, "Dummy ECU", f"Another ECU already answers on {interface} channel {channel} "
                f"(0x{self.ecu.config.response_id:X} or 0x300/0x301). Two ECUs answering the same requests break "
                f"security access and flashing.\n\nConnect anyway?") != QMessageBox.Yes:
            bus.shutdown()
            lock.close()
            return False
        self._bus, self._lock, self._stop = bus, lock, threading.Event()
        self.ecu.bus = bus
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._set_connected(True)
        config = self.ecu.config
        self._log(f"Connected to {interface} channel {channel} at {bitrate} bit/s: requests 0x{config.request_id:X} "
                  f"(functional 0x{config.functional_id:X}), responses 0x{config.response_id:X}")
        return True

    def _serve(self):
        try:
            self.ecu.serve(self._stop)
        except Exception as exc:  # e.g. the adapter went away; the status timer then disconnects
            self._log(f"Stopped: {exc}")

    def disconnect_ecu(self):
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(2)
        self._bus.shutdown()
        self._lock.close()
        self.ecu.bus = None
        self._bus = self._lock = self._stop = self._thread = None
        self._set_connected(False)
        self._log("Disconnected")

    def _set_connected(self, connected: bool):
        for widget in (self.interface, self.channel, self.bitrate, self.detect_button):
            widget.setEnabled(not connected)
        self.connect_button.setText("Disconnect" if connected else "Connect")
        if connected:
            connection = self._connection()
            text = f"Connected: {connection['interface']} channel {connection['channel']}"
        else:
            text = "Disconnected"
        colour = "#15803d" if connected else "#9ca3af"
        self.connection_state.setText(f'<span style="color:{colour}">●</span> {text}')

    # --- status and log ------------------------------------------------------------------------

    def _refresh_status(self):
        if self._thread and not self._thread.is_alive():
            self.disconnect_ecu()
        state, now = self.ecu.state, time.monotonic()
        labels = self.status_labels
        labels["Session"].setText(SESSION_NAMES.get(state.session, f"0x{state.session:02X}"))
        if now < state.locked_until:
            labels["Security"].setText(f"locked out for {state.locked_until - now:.0f} s")
        else:
            labels["Security"].setText("unlocked" if state.unlocked else "locked")
        transfer = state.transfer
        if transfer:
            labels["Transfer"].setText(f"{transfer['direction'].capitalize()} at 0x{transfer['address']:08X}: "
                                       f"{transfer['done']} / {transfer['size']} bytes")
        else:
            labels["Transfer"].setText("none")
        memory = state.memory
        size = sum(len(data) for data in memory.values())
        labels["Memory"].setText(f"{size} bytes downloaded in {len(memory)} segment(s)" if memory else "erased")
        labels["Software version"].setText(self.ecu.dids.get(0xF195, b"").decode("latin-1"))

    def _log(self, text):
        """Called from any thread; the text appears with the next timer tick."""
        self._lines.append(f"{_stamp()}  {text}")

    def _trace(self, direction, message):
        can_id = f"{message.arbitration_id:08X}" if message.is_extended_id else f"{message.arbitration_id:03X}"
        self._lines.append(f"{_stamp()}  {direction}  {can_id}  {bytes(message.data).hex(' ').upper()}")

    def _show_frames(self, enabled):
        self.ecu.trace = self._trace if enabled else None

    def _flush_log(self):
        batch = []
        while self._lines and len(batch) < 2000:
            batch.append(self._lines.popleft())
        if batch:
            self.log_view.appendPlainText("\n".join(batch))

    # --- actions ---------------------------------------------------------------------------------

    def reset_ecu(self):
        self.ecu.power_on()
        self._log("ECU reset to its factory state (default session, locked, original DIDs and DTCs, erased memory)")

    def save_memory(self):
        if not self.ecu.state.memory:
            QMessageBox.information(self, "Dummy ECU", "Nothing has been downloaded yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save the ECU memory", "flashed.s19", "S-record (*.s19 *.s37)")
        if path:
            self.ecu.write_image(path)

    def _browse_dump(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save the flashed image to", self.dump_path.text() or "flashed.s19",
                                              "S-record (*.s19 *.s37)")
        if path:
            self.dump_path.setText(path)

    def load_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load a Dummy ECU profile", "", "Dummy ECU profile (*.json)")
        if not path:
            return
        try:
            config, connection = load_profile(path)
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Dummy ECU", f"Cannot load {path}:\n{exc}")
            return
        self._fill(config, None if self._thread else connection)
        self._apply()
        self._log(f"Profile loaded: {path}")

    def save_profile(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save the Dummy ECU profile", "dummy_ecu_profile.json",
                                              "Dummy ECU profile (*.json)")
        if path:
            save_profile(path, self.ecu.config, self._connection())
            self._log(f"Profile saved: {path}")

    def restore_defaults(self):
        self._fill(EcuConfig(), None if self._thread else DEFAULT_CONNECTION)
        self._apply()

    def closeEvent(self, event):
        self.disconnect_ecu()
        app_settings().setValue(SETTINGS_KEY, json.dumps({"connection": self._connection(),
                                                          "ecu": asdict(self.ecu.config)}))
        super().closeEvent(event)


def run_window(config: EcuConfig | None = None, overrides: dict | None = None, connection: dict | None = None) -> int:
    """Open the window with the last settings, a profile (config) and command-line overrides on top."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    saved_config, saved_connection = saved_profile()
    window = DummyEcuWindow(replace(config or saved_config, **(overrides or {})),
                            {**saved_connection, **(connection or {})})
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(run_window())
