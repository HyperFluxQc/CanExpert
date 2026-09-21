"""Dummy ECU window: connect the simulated ECU to a CAN channel (e.g. a Kvaser virtual channel) and set how it
answers - addressing, ISO-TP flow control, UDS timing, periodic data, security levels and access rules,
flashing and its bootloader, the application frames with a generator per signal, its data (the DIDs, the
DTCs with their faults and life cycle, services forced to answer with a negative response) and transport
errors on purpose. Changes apply at once, even while connected; the settings are remembered, and can be
saved and loaded as JSON profiles. Several dummy ECUs can share a channel when each has its own identifiers."""
from __future__ import annotations

import collections
import json
import sys
import threading
import time
from dataclasses import asdict, fields, replace

import can
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from canexpert.paths import DBC_DIR
from canexpert.simulator.dtc import status_text
from canexpert.simulator.ecu import (
    DEFAULT_CONNECTION,
    DEFAULT_DIDS,
    IMAGE_CHECKS,
    PERIODIC_MODES,
    SERVICE_NAMES,
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
from canexpert.simulator.signals import DEFAULT_GENERATORS, GENERATORS, SignalSimulation
from canexpert.uds.client import NRC_NAMES
from canexpert.uds.isotp import flow_control_frame
from canexpert.ui_common import app_settings, toolbar_icon

INTERFACES = ("kvaser", "virtual", "vector", "ixxat", "pcan", "socketcan")
BITRATES = ("125000", "250000", "500000", "1000000")
ADDRESS_FORMATS = ("Any", "44", "24", "34", "14", "33", "22", "11")
SETTINGS_KEY = "dummy_ecu/profile"
ERROR_STYLE = "background: #fde2e2;"
BUILTIN_DBC_TEXT = "Built-in: DBC/dummy_ecu.dbc (0x300 EngineData, 0x301 EcuStatus)"
IMAGE_CHECK_TEXT = {"off": "None: any complete download passes",
                    "option": "CRC-32 given as the check routine's option record",
                    "trailer": "CRC-32 in the image's last four bytes"}
GENERATOR_TEXT = {"constant": "Constant", "ramp": "Ramp", "sine": "Sine", "square": "Square", "random": "Random",
                  "counter": "Counter", "running": "Engine running", "logging": "Logging", "session": "Session"}
# (setting, label, what happens) of the Errors tab
ERROR_ROWS = (
    ("error_refuse", "Refuse",
     "A negative response instead of the answer: 7F <service> <NRC>. 21 busyRepeatRequest asks the tester to "
     "send the request again."),
    ("error_no_answer", "No answer",
     "The request is carried out but not answered: the tester runs into its P2 timeout."),
    ("error_wrong_id", "Answer on another ID",
     "The response goes to the response ID + 1: the tester never sees it."),
    ("error_drop_frame", "Drop a consecutive frame",
     "One consecutive frame of a long response is not sent: the tester sees the sequence jump and drops the "
     "message."),
    ("error_wrong_sequence", "Wrong sequence number",
     "One consecutive frame of a long response carries the wrong sequence number."),
    ("error_stall", "Consecutive frame late",
     "One consecutive frame of a long response waits 1.2 s, past the tester's N_Cr of 1 s."),
)
# Columns of the tables
DID_DID, DID_DATA, DID_TEXT, DID_WRITABLE, DID_SIGNAL, DID_SESSIONS, DID_LEVEL = range(7)
DTC_DTC, DTC_STATUS, DTC_NOW, DTC_FAULT, DTC_SNAPSHOT, DTC_EXTENDED = range(6)
SIG_NAME, SIG_UNIT, SIG_KIND, SIG_LOW, SIG_HIGH, SIG_PERIOD, SIG_NOW = range(7)
MSG_NAME, MSG_ID, MSG_PERIOD, MSG_SEND = range(4)


# --- text fields ------------------------------------------------------------------------

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


def hint(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #6b7280;")
    return label


def _stamp() -> str:
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"


def saved_profile() -> tuple[EcuConfig, dict]:
    """The settings the window had when it was last closed. Settings remembered before the ECU had signals
    (no generators in them) also get the DIDs that came with them - the live, periodic and protected
    ones - beside the DIDs they had."""
    try:
        values = json.loads(app_settings().value(SETTINGS_KEY, "") or "{}")
        ecu = dict(values.get("ecu", {}))
        if ecu.get("dids") and "generators" not in ecu:
            known = {int(item["did"]) for item in ecu["dids"]}
            ecu["dids"] = list(ecu["dids"]) + [dict(item) for item in DEFAULT_DIDS if item["did"] not in known]
        return config_from_dict(ecu), dict(values.get("connection", {}))
    except (ValueError, TypeError, KeyError):
        return EcuConfig(), {}


def signal_setup(config: EcuConfig) -> SignalSimulation:
    """The messages and signals of a configuration's DBC with its generators (the built-in DBC when the
    configured one cannot be read), for the Signals tab."""
    engine = SignalSimulation()
    try:
        engine.load(config.dbc_path)
    except ValueError:
        engine.load("")
    try:
        engine.configure(config.generators, config.messages)
    except ValueError:
        pass
    return engine


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
        self.resize(1280, 820)
        self._lines = collections.deque()          # log lines from the ECU thread, shown by a timer
        self._bus = self._lock = self._stop = self._thread = None
        self._loading = True                         # widgets being built or filled: no _apply
        self._dbc_path = ""
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
    def _row(*widgets, stretch=True):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            layout.addWidget(widget)
        if stretch:
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
        self.tabs.addTab(self._access_page(), "Access")
        self.tabs.addTab(self._flashing_page(), "Flashing")
        self.signals_tab = self._signals_page()
        self.tabs.addTab(self.signals_tab, "Signals")
        self.data_tab = self._data_page()
        self.tabs.addTab(self.data_tab, "Data")
        self.tabs.addTab(self._errors_page(), "Errors")
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
        splitter.setSizes([700, 580])
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
        form.addRow(hint("Otherwise 10 02 from the default session gets NRC 0x22. The bootloader takes it from "
                         "any session."))
        periodic, form = self._group("Periodic data (0x2A) and events (0x86)")
        self.periodic_rates = [self._spin(10, 60000, step=10, suffix=" ms") for _ in PERIODIC_MODES]
        form.addRow("Slow, medium, fast", self._row(*self.periodic_rates))
        self.periodic_own_id = self._check("Send periodic data on an ID of its own")
        self.periodic_id = self._hex(0x1FFFFFFF)
        form.addRow(self.periodic_own_id, self.periodic_id)
        self.periodic_hint = hint()
        form.addRow(self.periodic_hint)
        form.addRow(hint("ReadDataByPeriodicIdentifier sends the F2xx DIDs (2A 03 01 sends F201 fast; 2A 04 "
                         "stops). ResponseOnEvent answers unasked when a DID changes (86 03 02 <DID> 22 <DID>) or "
                         "a DTC's status gets a bit of a mask (86 01 02 <mask> 19 02 <mask>), from startResponse"
                         "OnEvent (86 05 02) until stopResponseOnEvent (86 00 02). Both end when the session "
                         "changes."))
        return self._page(timing, sessions, periodic)

    def _access_page(self):
        security, form = self._group("SecurityAccess (0x27)")
        self.security_level = self._hex(0x7F)
        self.security_level.setRange(1, 0x7F)
        self.security_level.setSingleStep(2)
        self.seed_length = self._spin(1, 16, suffix=" bytes")
        self.key_mask = self._hex(0xFF)
        self.key_dll = self._line("the mask above is used while this is empty")
        self.key_dll.setMinimumWidth(260)
        browse = QPushButton("Browse...")
        browse.clicked.connect(lambda: self._browse_dll(self.key_dll))
        self.key_variant = self._line()
        self.max_attempts = self._spin(1, 10)
        self.lockout = self._double(0, 600, 1, " s")
        form.addRow("Level (requestSeed)", self.security_level)
        form.addRow("Seed length", self.seed_length)
        form.addRow("Key XOR mask", self.key_mask)
        self.security_hint = hint()
        form.addRow(self.security_hint)
        form.addRow("Seed && key DLL", self._row(self.key_dll, browse, stretch=False))   # && : not a shortcut
        form.addRow("DLL variant", self.key_variant)
        form.addRow(hint("With a DLL the ECU expects the key its GenerateKeyEx computes, which is what the UDS "
                         "Console and the flashing sequence send when they are given the same DLL."))
        form.addRow("Wrong keys allowed", self.max_attempts)
        form.addRow("Lockout delay", self.lockout)
        form.addRow(hint("After that many wrong keys: NRC 0x36, then 0x37 until the delay has passed."))
        levels, form = self._group("More security levels")
        self.level_table = self._table(["Level", "Seed length", "Key mask", "Seed & key DLL", "Variant"],
                                       stretch_column=3)
        self.level_table.setMinimumHeight(110)
        form.addRow(self.level_table)
        self.level_buttons = self._table_buttons(self.level_table, self._add_free_level)
        form.addRow(self.level_buttons)
        form.addRow(hint("Each level is unlocked on its own - requestSeed with the level, sendKey with the level "
                         "+ 1 - and stays unlocked until the session changes. A DID or a service can require "
                         "one."))
        rules, form = self._group("Service rules")
        self.rule_table = self._table(["Service", "Sessions", "Level", "Meaning"], stretch_column=3)
        self.rule_table.setMinimumHeight(110)
        form.addRow(self.rule_table)
        self.rule_buttons = self._table_buttons(self.rule_table, lambda: self._add_rule(0x2F, [0x03], 0x01))
        form.addRow(self.rule_buttons)
        form.addRow(hint("A service used outside its sessions gets NRC 0x7F (serviceNotSupportedInActive"
                         "Session), without its level unlocked NRC 0x33 (securityAccessDenied). Sessions: "
                         "default, programming, extended (or 01, 02, 03), empty for any; Level: empty for none. "
                         "The ECU's own rules still apply: 2F wants the extended session, flashing the "
                         "programming session and security."))
        return self._page(security, levels, rules)

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
        form.addRow(hint("Addresses open to erase, download, upload and the memory services 23 and 3D "
                         "(first-last, hex); outside: NRC 0x31. Empty: any address."))
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
        form.addRow(hint("checkProgrammingDependencies: status 00 once a download completed and the image "
                         "passes the check below; the software version (F195) then changes."))
        boot, form = self._group("Bootloader")
        self.image_crc = QComboBox()
        for name in IMAGE_CHECKS:
            self.image_crc.addItem(IMAGE_CHECK_TEXT[name], name)
        self.image_crc.currentIndexChanged.connect(self._apply)
        form.addRow("Image check", self.image_crc)
        form.addRow(hint("Option record: 31 01 FF 01 and the CRC-32 of the image (its segments' data in address "
                         "order, one after the other) - the built-in flashing sequence sends it when asked to. "
                         "A wrong CRC gets status 01."))
        self.version_from_image = self._check("Read the software version from the image at")
        self.version_address = self._hex(0x7FFFFFFF)
        self.version_length = self._spin(1, 64, suffix=" bytes")
        form.addRow(self.version_from_image, self._row(self.version_address, self.version_length))
        form.addRow(hint("Otherwise F195 becomes APP-FLASHED-<CRC-32>. The demo image "
                         "(examples/firmware/demo_app.s19) has its name at 00020000."))
        form.addRow(hint("An erase or a download makes the application invalid until the check passes. An "
                         "ECUReset with an invalid application - a flash that failed or was abandoned - starts "
                         "the bootloader: no application frames, F195 answers BOOTLOADER, and only the "
                         "services needed to flash again are answered. A reset after a good check starts the "
                         "application."))
        other, form = self._group("Upload and image")
        self.allow_upload = self._check("Allow RequestUpload (0x35) to read the memory back")
        self.dump_path = self._line("not saved")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_dump)
        form.addRow("", self.allow_upload)
        form.addRow("Save image to", self._row(self.dump_path, browse))
        form.addRow(hint("Written as S-records after checkProgrammingDependencies."))
        self.dump_path.setMinimumWidth(260)
        return self._page(download, routines, boot, other)

    def _signals_page(self):
        source, form = self._group("Database")
        self.dbc_label = QLineEdit()
        self.dbc_label.setReadOnly(True)
        self.dbc_label.setMinimumWidth(300)
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_dbc)
        builtin = QPushButton("Built-in")
        builtin.setToolTip("DBC/dummy_ecu.dbc, which the example panels use")
        builtin.clicked.connect(lambda: self.choose_dbc(""))
        form.addRow("DBC", self._row(self.dbc_label, browse, builtin, stretch=False))
        form.addRow(hint("The messages the ECU sends and the signals in them. Choosing another DBC starts every "
                         "signal at its initial value; the built-in one comes back with the ECU's usual "
                         "traffic."))
        frames, form = self._group("Application frames")
        self.broadcast = self._check("Send the application frames")
        self.broadcast_interval = self._spin(10, 10000, step=10, suffix=" ms")
        form.addRow("", self.broadcast)
        form.addRow("Default period", self.broadcast_interval)
        form.addRow(hint("For the messages without a period of their own (below, or GenMsgCycleTime in the "
                         "DBC). They stop while CommunicationControl disables normal messages and while the "
                         "bootloader runs. 0x200 (01 start, 02 stop) and 0x201 (bit 0 logging) are accepted as "
                         "commands."))
        self.message_table = self._table(["Message", "ID", "Period (ms)", "Send"], stretch_column=MSG_NAME)
        self.message_table.setMinimumHeight(110)
        form.addRow(self.message_table)
        form.addRow(hint("Period 0: the DBC's, or else the default period. Multiplexed messages and CAN FD "
                         "lengths are not sent."))
        signals, form = self._group("Signals")
        self.signal_table = self._table(["Signal", "Unit", "Generator", "Low", "High", "Period (s)", "Now"],
                                        stretch_column=SIG_NAME)
        self.signal_table.setMinimumHeight(240)
        self.signal_table.currentCellChanged.connect(lambda row, *_: self._show_generator(row))
        form.addRow(self.signal_table)
        self.generator_hint = hint("Pick a signal to see what its generator does.")
        form.addRow(self.generator_hint)
        form.addRow(hint("Values are physical, in the signal's unit. InputOutputControlByIdentifier (0x2F) on "
                         "a DID that follows a signal takes the signal over (Now says so) until control "
                         "returns to the ECU."))
        return self._page(source, frames, signals)

    def _errors_page(self):
        group, form = self._group("Transport errors on purpose")
        form.addRow(hint("The chance that a response gets each error. Use them to see how a tester copes; "
                         "0 % everywhere is a well-behaved ECU."))
        self.error_spins = {}
        for name, label, text in ERROR_ROWS:
            spin = self._spin(0, 100, suffix=" %")
            self.error_spins[name] = spin
            if name == "error_refuse":
                self.error_refuse_nrc = self._hex(0xFF)
                self.error_nrc_text = QLabel()
                form.addRow(label, self._row(spin, QLabel("with NRC"), self.error_refuse_nrc, self.error_nrc_text))
            else:
                form.addRow(label, spin)
            form.addRow(hint(text))
        self.errors_on_tester_present = self._check("Also on TesterPresent")
        form.addRow("", self.errors_on_tester_present)
        form.addRow(hint("Off, the TesterPresent of CAN Expert's ECU check always gets its answer, so the ECU "
                         "stays in the node list while the other requests go wrong."))
        return self._page(group)

    def _monitor(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        status = QGroupBox("ECU status")
        form = QFormLayout(status)
        self.status_labels = {}
        for name in ("Session", "Security", "Application", "DTC setting", "Normal messages", "Transfer", "Memory",
                     "Software version", "Periodic data", "Events", "I/O control", "Operation cycle"):
            label = QLabel("-")
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setWordWrap(True)
            form.addRow(name, label)
            self.status_labels[name] = label
        reset = QPushButton("Reset ECU")
        reset.setToolTip("Back to the factory state: default session, locked, original DIDs and DTCs, erased memory, "
                         "a valid application")
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

    # --- tables ---------------------------------------------------------------------------------

    def _table(self, headers, stretch_column):
        """A table that fits the settings pane: stretch_column takes the room, the others their content."""
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        for column in range(len(headers)):
            header.setSectionResizeMode(column, QHeaderView.Stretch if column == stretch_column
                                        else QHeaderView.ResizeToContents)
        table.setMinimumHeight(150)
        table.itemChanged.connect(self._on_data_edited)
        return table

    def _table_buttons(self, table, add):
        add_button, remove_button = QPushButton("Add"), QPushButton("Remove")
        add_button.clicked.connect(lambda: (add(), self._apply()))
        remove_button.clicked.connect(lambda: self._remove_row(table))
        row = self._row(add_button, remove_button)
        row.add_button = add_button
        return row

    def _data_page(self):
        dids, form = self._group("DIDs: ReadDataByIdentifier (0x22) and WriteDataByIdentifier (0x2E)")
        self.did_table = self._table(["DID", "Data (hex)", "As text", "Writable", "Signal", "Sessions", "Level"],
                                     stretch_column=DID_DATA)
        form.addRow(self.did_table)
        self.did_buttons = self._table_buttons(self.did_table, lambda: self._add_did({"did": 0x0000, "data": "00"}))
        form.addRow(self.did_buttons)
        form.addRow(hint("A writable DID takes a new value of the same length, in the extended or programming "
                         "session once security access is unlocked. Signal (Message.Signal): the DID answers "
                         "that signal's raw value in as many bytes as its data has, and 2F controls it. "
                         "Sessions: where it can be read (and written), empty for any - elsewhere NRC 0x31; "
                         "Level: the security level it needs - otherwise NRC 0x33. F186 (session) and 0100 "
                         "(uptime) are always there; F2xx DIDs are the periodic ones."))
        dtcs, form = self._group("DTCs: ReadDTCInformation (0x19) and ClearDiagnosticInformation (0x14)")
        self.dtc_table = self._table(["DTC", "Status", "Now", "Fault", "Snapshot record 01 (hex)",
                                      "Extended data 01 (hex)"], stretch_column=DTC_SNAPSHOT)
        form.addRow(self.dtc_table)
        self.dtc_buttons = self._table_buttons(self.dtc_table, lambda: self._add_dtc(0x000000, 0x00, b"", b""))
        form.addRow(self.dtc_buttons)
        form.addRow(hint("Status: at power-on; Now: as it is. Tick Fault and the DTC's test fails: pending at "
                         "once, confirmed after the operation cycles below, with the snapshot of that moment "
                         "and one more occurrence (the extended data's first byte). Untick it and the DTC "
                         "heals: no longer pending after a cycle, aged out after more. Snapshot: the number "
                         "of identifiers, then each DID and its data (19 04). 19 01, 19 02 and 19 0A report "
                         "the DTCs and their status."))
        cycle, form = self._group("Fault memory")
        self.confirm_cycles = self._spin(1, 255, suffix=" cycles")
        self.aging_cycles = self._spin(1, 255, suffix=" cycles")
        self.operation_cycle = self._double(0, 3600, 1, " s")
        self.operation_cycle.setSpecialValueText("on ECUReset and the button")
        self.snapshot_dids = self._line("none: the table's snapshot is kept")
        new_cycle = QPushButton("New operation cycle")
        new_cycle.setToolTip("End this operation cycle (ignition off) and start the next (ignition on)")
        new_cycle.clicked.connect(self.new_operation_cycle)
        form.addRow("Confirmed after", self.confirm_cycles)
        form.addRow("Aged out after", self.aging_cycles)
        form.addRow("Operation cycle every", self._row(self.operation_cycle, new_cycle))
        form.addRow("Snapshot DIDs", self.snapshot_dids)
        form.addRow(hint("When a fault appears, the snapshot record takes these DIDs with their values at that "
                         "moment, e.g. 0101, 0102 (temperature and pressure). ControlDTCSetting off (85 02) "
                         "freezes every status."))
        forced, form = self._group("Forced negative responses")
        self.nrc_table = self._table(["Service", "NRC", "Meaning"], stretch_column=2)
        self.nrc_table.setMinimumHeight(110)
        form.addRow(self.nrc_table)
        self.nrc_buttons = self._table_buttons(self.nrc_table, lambda: self._add_forced(0x22, 0x22))
        form.addRow(self.nrc_buttons)
        form.addRow(hint("Every request of that service is answered 7F <service> <NRC> - to see how a tester "
                         "copes with a refusal."))
        return self._page(dids, dtcs, cycle, forced)

    @staticmethod
    def _cell(text, editable=True):
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        return item

    def _check_cell(self, checked):
        item = self._cell("")
        item.setFlags((item.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        return item

    def _append(self, table, cells):
        loading, self._loading = self._loading, True
        try:
            row = table.rowCount()
            table.insertRow(row)
            for column, cell in enumerate(cells):
                if isinstance(cell, QWidget):
                    table.setCellWidget(row, column, cell)
                else:
                    table.setItem(row, column, cell)
        finally:
            self._loading = loading

    def _add_did(self, item):
        data = bytes.fromhex(str(item.get("data", "")))
        level = int(item.get("level", 0) or 0)
        self._append(self.did_table, [self._cell(f"{int(item['did']):04X}"), self._cell(data.hex(" ").upper()),
                                      self._cell(printable(data), editable=False),
                                      self._check_cell(bool(item.get("writable"))),
                                      self._cell(str(item.get("signal", "") or "")),
                                      self._cell(format_sessions(item.get("sessions"))),
                                      self._cell(f"{level:02X}" if level else "")])

    def _add_dtc(self, dtc, status, snapshot, extended):
        now = self._cell(f"{status:02X}", editable=False)
        now.setToolTip(status_text(status))
        self._append(self.dtc_table, [self._cell(f"{dtc:06X}"), self._cell(f"{status:02X}"), now,
                                      self._check_cell(False), self._cell(snapshot.hex(" ").upper()),
                                      self._cell(extended.hex(" ").upper())])

    def _add_forced(self, sid, nrc):
        meaning = self._cell(forced_text(sid, nrc), editable=False)
        meaning.setToolTip(f"Every request of this service is answered 7F {sid:02X} {nrc:02X}")
        self._append(self.nrc_table, [self._cell(f"{sid:02X}"), self._cell(f"{nrc:02X}"), meaning])

    def _add_level(self, level, seed_length, key_mask, dll, variant):
        self._append(self.level_table, [self._cell(f"{level:02X}"), self._cell(str(seed_length)),
                                        self._cell(f"{key_mask:02X}"), self._cell(dll), self._cell(variant)])

    def _add_free_level(self):
        taken = {self.security_level.value()}
        for row in range(self.level_table.rowCount()):
            try:
                taken.add(int(self.level_table.item(row, 0).text(), 16))
            except ValueError:
                pass
        level = next(level for level in range(0x03, 0x80, 2) if level not in taken)
        self._add_level(level, 4, 0x5A, "", "")

    def _add_rule(self, sid, sessions, level):
        self._append(self.rule_table, [self._cell(f"{sid:02X}"), self._cell(format_sessions(sessions)),
                                       self._cell(f"{level:02X}" if level else ""),
                                       self._cell(SERVICE_NAMES.get(sid, f"service {sid:02X}"), editable=False)])

    def _add_message(self, entry):
        message = entry.message
        can_id = f"{message.frame_id:08X}x" if message.is_extended_frame else f"{message.frame_id:03X}"
        send = self._check_cell(entry.on)
        if not entry.sendable:
            send.setFlags(send.flags() & ~Qt.ItemIsEnabled)
            send.setToolTip("Multiplexed or longer than 8 bytes: not sent")
        period = round(entry.cycle * 1000) if entry.cycle and entry.cycle != (message.cycle_time or 0) / 1000 else 0
        self._append(self.message_table, [self._cell(message.name, editable=False), self._cell(can_id, editable=False),
                                          self._cell(str(period)), send])

    def _add_signal(self, engine, item):
        signal = engine.signal(item["signal"])
        kind = QComboBox()
        for name in GENERATORS:
            kind.addItem(GENERATOR_TEXT[name], name)
        kind.setCurrentIndex(max(0, kind.findData(item["kind"])))
        kind.currentIndexChanged.connect(lambda _index, combo=kind: self._generator_changed(combo))
        self._append(self.signal_table, [self._cell(item["signal"], editable=False),
                                         self._cell(signal.unit or "" if signal is not None else "", editable=False),
                                         kind, self._cell(number_text(item["low"])),
                                         self._cell(number_text(item["high"])),
                                         self._cell(number_text(item["period"])), self._cell("", editable=False)])

    def _fill_signal_tables(self, engine: SignalSimulation):
        loading, self._loading = self._loading, True
        try:
            for table in (self.message_table, self.signal_table):
                table.setRowCount(0)
            for entry in engine.messages:
                self._add_message(entry)
            for item in engine.generators():
                self._add_signal(engine, item)
        finally:
            self._loading = loading

    def _generator_changed(self, combo):
        for row in range(self.signal_table.rowCount()):
            if self.signal_table.cellWidget(row, SIG_KIND) is combo:
                self._show_generator(row)
        self._apply()

    def _show_generator(self, row):
        combo = self.signal_table.cellWidget(row, SIG_KIND) if row >= 0 else None
        if combo is not None:
            name = self.signal_table.item(row, SIG_NAME).text()
            self.generator_hint.setText(f"{name}: {GENERATORS[combo.currentData()]}.")

    def _remove_row(self, table):
        row = table.currentRow()
        if row >= 0:
            table.removeRow(row)
            self._apply()

    def _on_data_edited(self, item):
        if self._loading:
            return
        table = item.tableWidget()
        if table is self.dtc_table and item.column() == DTC_FAULT:
            self._fault_toggled(item)
            return
        loading, self._loading = self._loading, True      # the previews are the window's own writing
        try:
            if table is self.did_table and item.column() == DID_DATA:
                try:
                    preview = printable(bytes.fromhex(item.text()))
                except ValueError:
                    preview = ""
                self.did_table.item(item.row(), DID_TEXT).setText(preview)
            if table is self.nrc_table and item.column() in (0, 1):
                try:
                    preview = forced_text(int(self.nrc_table.item(item.row(), 0).text(), 16),
                                          int(self.nrc_table.item(item.row(), 1).text(), 16))
                except ValueError:
                    preview = ""
                self.nrc_table.item(item.row(), 2).setText(preview)
            if table is self.rule_table and item.column() == 0:
                try:
                    sid = int(item.text(), 16)
                    preview = SERVICE_NAMES.get(sid, f"service {sid:02X}")
                except ValueError:
                    preview = ""
                self.rule_table.item(item.row(), 3).setText(preview)
        finally:
            self._loading = loading
        self._apply()

    def _fault_toggled(self, item):
        """The Fault box: the fault behind the DTC appears or goes away in the running ECU."""
        try:
            dtc = int(self.dtc_table.item(item.row(), DTC_DTC).text(), 16)
            self.ecu.set_fault(dtc, item.checkState() == Qt.Checked)
        except (ValueError, KeyError):
            loading, self._loading = self._loading, True
            item.setCheckState(Qt.Unchecked)
            self._loading = loading
            self.statusBar().showMessage("Not applied: that DTC is not in the ECU's table yet (check the row)")
            return
        self._refresh_dtc_status()

    # --- reading the widgets -----------------------------------------------------------------------

    @staticmethod
    def _mark(item, error: str | None):
        if error:
            item.setBackground(QColor("#fde2e2"))
            raise ValueError(error)
        item.setData(Qt.BackgroundRole, None)

    def _number(self, table, row, column, what, maximum, optional=False):
        item = table.item(row, column)
        text = item.text().strip()
        if optional and not text:
            self._mark(item, None)
            return 0
        try:
            value = int(text, 16)
            valid = 0 <= value <= maximum
        except ValueError:
            valid = False
        self._mark(item, None if valid else f"{what} in row {row + 1} must be hexadecimal, at most {maximum:X}")
        return value

    def _decimal(self, table, row, column, what, low=None, high=None):
        item = table.item(row, column)
        try:
            value = float(item.text().strip().replace(",", "."))
            valid = (low is None or value >= low) and (high is None or value <= high)
        except ValueError:
            valid = False
        self._mark(item, None if valid else f"{what} in row {row + 1} must be a number"
                   + (f", {low} or more" if low is not None else ""))
        return value

    def _hex_data(self, table, row, column, what, required):
        item = table.item(row, column)
        try:
            raw = bytes.fromhex(item.text())
            valid = bool(raw) or not required
        except ValueError:
            valid = False
        self._mark(item, None if valid else f"{what} in row {row + 1} must be hexadecimal bytes, e.g. 57 56 57")
        return raw.hex()

    def _session_list(self, table, row, column):
        item = table.item(row, column)
        try:
            sessions = parse_sessions(item.text())
        except ValueError as exc:
            self._mark(item, f"Sessions in row {row + 1}: {exc}")
        self._mark(item, None)
        return sessions

    def _read_tables(self):
        """The data tables as configuration lists; ValueError naming the bad cell."""
        dids = []
        for row in range(self.did_table.rowCount()):
            entry = {"did": self._number(self.did_table, row, DID_DID, "The DID", 0xFFFF),
                     "data": self._hex_data(self.did_table, row, DID_DATA, "The DID's data", True),
                     "writable": self.did_table.item(row, DID_WRITABLE).checkState() == Qt.Checked}
            signal = self.did_table.item(row, DID_SIGNAL).text().strip()
            sessions = self._session_list(self.did_table, row, DID_SESSIONS)
            level = self._number(self.did_table, row, DID_LEVEL, "The level", 0x7F, optional=True)
            entry.update({key: value for key, value in (("signal", signal), ("sessions", sessions),
                                                        ("level", level)) if value})
            dids.append(entry)
        dtcs = [{"dtc": self._number(self.dtc_table, row, DTC_DTC, "The DTC", 0xFFFFFF),
                 "status": self._number(self.dtc_table, row, DTC_STATUS, "The status", 0xFF),
                 "snapshot": self._hex_data(self.dtc_table, row, DTC_SNAPSHOT, "The snapshot", False),
                 "extended": self._hex_data(self.dtc_table, row, DTC_EXTENDED, "The extended data", False)}
                for row in range(self.dtc_table.rowCount())]
        forced = [{"sid": self._number(self.nrc_table, row, 0, "The service", 0xFF),
                   "nrc": self._number(self.nrc_table, row, 1, "The NRC", 0xFF)}
                  for row in range(self.nrc_table.rowCount())]
        levels = []
        for row in range(self.level_table.rowCount()):
            level = self._number(self.level_table, row, 0, "The level", 0x7F)
            if not level % 2:
                self._mark(self.level_table.item(row, 0), f"The level in row {row + 1} must be odd (requestSeed)")
            length = int(self._decimal(self.level_table, row, 1, "The seed length", 1, 64))
            levels.append({"level": level, "seed_length": length,
                           "key_mask": self._number(self.level_table, row, 2, "The key mask", 0xFF),
                           "dll": self.level_table.item(row, 3).text().strip(),
                           "variant": self.level_table.item(row, 4).text().strip()})
        rules = [{"sid": self._number(self.rule_table, row, 0, "The service", 0xFF),
                  "sessions": self._session_list(self.rule_table, row, 1),
                  "level": self._number(self.rule_table, row, 2, "The level", 0x7F, optional=True)}
                 for row in range(self.rule_table.rowCount())]
        messages = [{"message": self.message_table.item(row, MSG_NAME).text(),
                     "on": self.message_table.item(row, MSG_SEND).checkState() == Qt.Checked,
                     "cycle_ms": int(self._decimal(self.message_table, row, MSG_PERIOD, "The period", 0, 3600000))}
                    for row in range(self.message_table.rowCount())]
        generators = [{"signal": self.signal_table.item(row, SIG_NAME).text(),
                       "kind": self.signal_table.cellWidget(row, SIG_KIND).currentData(),
                       "low": self._decimal(self.signal_table, row, SIG_LOW, "Low"),
                       "high": self._decimal(self.signal_table, row, SIG_HIGH, "High"),
                       "period": self._decimal(self.signal_table, row, SIG_PERIOD, "The period", 0)}
                      for row in range(self.signal_table.rowCount())]
        return dids, dtcs, forced, levels, rules, messages, generators

    def _fill_tables(self, config: EcuConfig):
        for table in (self.did_table, self.dtc_table, self.nrc_table, self.level_table, self.rule_table):
            table.setRowCount(0)
        for item in config.dids:
            self._add_did(item)
        for item in config.dtcs:
            self._add_dtc(int(item["dtc"]), int(item.get("status", 0)), bytes.fromhex(item.get("snapshot", "")),
                          bytes.fromhex(item.get("extended", "")))
        for item in config.forced_nrcs:
            self._add_forced(int(item["sid"]), int(item["nrc"]))
        for item in config.security_levels:
            self._add_level(int(item["level"]), int(item.get("seed_length", 4)), int(item.get("key_mask", 0)),
                            str(item.get("dll", "") or ""), str(item.get("variant", "") or ""))
        for item in config.service_rules:
            self._add_rule(int(item["sid"]), item.get("sessions") or [], int(item.get("level", 0) or 0))
        engine = signal_setup(config)
        if engine.source != config.dbc_path:
            self._log(f"Cannot read {config.dbc_path}: the built-in database is used")
        self._dbc_path = engine.source
        self.dbc_label.setText(engine.source or BUILTIN_DBC_TEXT)
        self._fill_signal_tables(engine)

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
            for spin, rate in zip(self.periodic_rates, config.periodic_rates_ms):
                spin.setValue(int(rate))
            self.periodic_own_id.setChecked(config.periodic_id is not None)
            self.periodic_id.setValue(0x6FF if config.periodic_id is None else config.periodic_id)
            self.security_level.setValue(config.security_level)
            self.seed_length.setValue(config.seed_length)
            self.key_mask.setValue(config.key_mask)
            self.key_dll.setText(config.key_dll)
            self.key_variant.setText(config.key_variant)
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
            self.image_crc.setCurrentIndex(max(0, self.image_crc.findData(config.image_crc)))
            self.version_from_image.setChecked(config.version_address is not None)
            self.version_address.setValue(0x20000 if config.version_address is None else config.version_address)
            self.version_length.setValue(config.version_length)
            self.allow_upload.setChecked(config.allow_upload)
            self.dump_path.setText(config.dump_path or "")
            self.broadcast.setChecked(config.broadcast_interval > 0)
            self.broadcast_interval.setValue(round(config.broadcast_interval * 1000) or 100)
            self.confirm_cycles.setValue(config.confirm_cycles)
            self.aging_cycles.setValue(config.aging_cycles)
            self.operation_cycle.setValue(config.operation_cycle_seconds)
            self.snapshot_dids.setText(", ".join(f"{did:04X}" for did in config.snapshot_dids))
            for name, spin in self.error_spins.items():
                spin.setValue(int(getattr(config, name)))
            self.error_refuse_nrc.setValue(config.error_refuse_nrc)
            self.errors_on_tester_present.setChecked(config.errors_on_tester_present)
            self._fill_tables(config)
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
        snapshot_dids = parsed(self.snapshot_dids, parse_did_list)
        try:
            dids, dtcs, forced_nrcs, levels, rules, messages, generators = self._read_tables()
        except ValueError as exc:
            errors.append(str(exc))
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
            key_mask=self.key_mask.value(), key_dll=self.key_dll.text().strip(),
            key_variant=self.key_variant.text().strip(), security_levels=levels,
            max_attempts=self.max_attempts.value(), lockout_seconds=self.lockout.value(), service_rules=rules,
            periodic_rates_ms=tuple(spin.value() for spin in self.periodic_rates),
            periodic_id=self.periodic_id.value() if self.periodic_own_id.isChecked() else None,
            data_formats=data_formats, address_format=address_format,
            max_block_length=self.block_data.value() + 2, block_length_bytes=self.length_bytes.value(),
            full_blocks=self.full_blocks.isChecked(), memory_ranges=memory_ranges,
            require_erase=self.require_erase.isChecked(), erase_routine=self.erase_routine.value(),
            check_routine=self.check_routine.value(), erase_seconds=self.erase_seconds.value(),
            allow_upload=self.allow_upload.isChecked(), image_crc=self.image_crc.currentData(),
            version_address=self.version_address.value() if self.version_from_image.isChecked() else None,
            version_length=self.version_length.value(),
            broadcast_interval=self.broadcast_interval.value() / 1000 if self.broadcast.isChecked() else 0,
            dbc_path=self._dbc_path, messages=messages, generators=generators,
            dump_path=self.dump_path.text().strip() or None,
            dids=dids, dtcs=dtcs, forced_nrcs=forced_nrcs,
            confirm_cycles=self.confirm_cycles.value(), aging_cycles=self.aging_cycles.value(),
            operation_cycle_seconds=self.operation_cycle.value(), snapshot_dids=snapshot_dids,
            error_refuse_nrc=self.error_refuse_nrc.value(),
            errors_on_tester_present=self.errors_on_tester_present.isChecked(),
            **{name: spin.value() for name, spin in self.error_spins.items()},
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
        data_changed = any(getattr(config, name) != getattr(self.ecu.config, name) for name in ("dids", "dtcs"))
        for item in fields(EcuConfig):
            setattr(self.ecu.config, item.name, getattr(config, item.name))
        problem = self.ecu.refresh(data=data_changed)
        if data_changed:                              # the fault memory starts again: no fault present
            self._refresh_dtc_status()
        if problem:
            self.statusBar().showMessage(f"Not applied: {problem}")
        else:
            self.statusBar().clearMessage()
        self._update_labels()

    def _update_labels(self):
        config = self.ecu.config
        self.address_byte.setEnabled(self.use_address_byte.isChecked())
        self.padding.setEnabled(self.use_padding.isChecked())
        self.broadcast_interval.setEnabled(self.broadcast.isChecked())
        self.wait_interval.setEnabled(config.flow_waits > 0)
        self.periodic_id.setEnabled(self.periodic_own_id.isChecked())
        self.version_address.setEnabled(self.version_from_image.isChecked())
        self.version_length.setEnabled(self.version_from_image.isChecked())
        self.error_refuse_nrc.setEnabled(config.error_refuse > 0)
        self.error_nrc_text.setText(NRC_NAMES.get(config.error_refuse_nrc, "unknown NRC"))
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
        room = self.ecu._periodic_room()
        self.periodic_hint.setText(
            (f"Frames of ID {config.periodic_id:X}: the periodic identifier, then up to {room} data bytes (ISO "
             f"15765-3 type 2)." if config.periodic_id is not None else
             f"6A frames on the response ID: 04 6A 01 00 D7 for F201, up to {room} data bytes (ISO 15765-3 type 1)."))
        level = config.security_level
        key = "the key its DLL computes" if config.key_dll else f"key = each seed byte XOR {config.key_mask:02X}"
        self.security_hint.setText(f"requestSeed 27 {level:02X}, sendKey 27 {level + 1:02X}; {key}. The example "
                                   f"scripts' compute_key() uses A5.")
        maximum = config.max_block_length
        length = max(config.block_length_bytes, (maximum.bit_length() + 7) // 8)
        response = bytes([0x74, length << 4]) + maximum.to_bytes(length, "big")
        self.block_hint.setText(f"maxNumberOfBlockLength = {maximum} (0x{maximum:X}): the data plus the 0x36 SID and "
                                f"the block counter. Response: {response.hex(' ').upper()}. CAN Expert's Flashing() "
                                f"then sends TransferData blocks of {maximum - 2} bytes.")

    # --- the database of the application frames -----------------------------------------------------

    def choose_dbc(self, path: str) -> bool:
        """Send the messages of another DBC ("" = the built-in one): the signal tables start again."""
        engine = SignalSimulation()
        try:
            engine.load(path)
            engine.configure(DEFAULT_GENERATORS if not path else ())
        except ValueError as exc:
            QMessageBox.warning(self, "Dummy ECU", str(exc))
            return False
        self._dbc_path = path
        self.dbc_label.setText(path or BUILTIN_DBC_TEXT)
        self._fill_signal_tables(engine)
        self._apply()
        return True

    def _browse_dbc(self):
        path, _ = QFileDialog.getOpenFileName(self, "Messages the ECU sends", str(DBC_DIR),
                                              "CAN database (*.dbc *.arxml *.kcd *.sym);;All files (*.*)")
        if path:
            self.choose_dbc(path)

    def _browse_dll(self, line):
        path, _ = QFileDialog.getOpenFileName(self, "Seed & key DLL", "", "DLL (*.dll);;All files (*.*)")
        if path:
            line.setText(path)

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
        lock = claim_channel(interface, channel, self.ecu.config.request_id)
        if lock is None:
            QMessageBox.warning(self, "Dummy ECU", f"Another dummy ECU already answers requests to "
                                f"0x{self.ecu.config.request_id:X} on {interface} channel {channel}. Close it, or "
                                "give this one other identifiers (Addressing): two ECUs answering the same "
                                "requests break security access and flashing.")
            return False
        try:
            bus = can.Bus(interface=interface, channel=channel, bitrate=bitrate)
        except Exception as exc:
            lock.close()
            QMessageBox.warning(self, "Dummy ECU", f"Cannot open {interface} channel {channel}:\n{exc}")
            return False
        if other_ecu_present(bus, self.ecu.config) and QMessageBox.question(
                self, "Dummy ECU", f"Another ECU already answers on {interface} channel {channel} with "
                f"0x{self.ecu.config.response_id:X}, or sends the application frames this one would. Two ECUs "
                f"answering the same requests break security access and flashing; a second ECU on the channel "
                f"needs its own identifiers and its application frames off.\n\nConnect anyway?") != QMessageBox.Yes:
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
        ecu = self.ecu
        state, now = ecu.state, time.monotonic()
        labels = self.status_labels
        labels["Session"].setText(SESSION_NAMES.get(state.session, f"0x{state.session:02X}"))
        if now < state.locked_until:
            labels["Security"].setText(f"locked out for {state.locked_until - now:.0f} s")
        elif state.unlocked_levels:
            levels = ", ".join(f"{level:02X}" for level in sorted(state.unlocked_levels))
            labels["Security"].setText(f"unlocked (level {levels})")
        else:
            labels["Security"].setText("locked")
        if state.bootloader:
            labels["Application"].setText("not valid: the bootloader runs")
        else:
            labels["Application"].setText("valid" if state.application_valid else
                                          "not valid until checkProgrammingDependencies passes")
        labels["DTC setting"].setText("on" if state.dtc_setting_on else "off (ControlDTCSetting)")
        labels["Normal messages"].setText("on" if state.communication_enabled else "off (CommunicationControl)")
        transfer = state.transfer
        if transfer:
            labels["Transfer"].setText(f"{transfer['direction'].capitalize()} at 0x{transfer['address']:08X}: "
                                       f"{transfer['done']} / {transfer['size']} bytes")
        else:
            labels["Transfer"].setText("none")
        memory = state.memory
        size = sum(len(data) for data in memory.values())
        labels["Memory"].setText(f"{size} bytes downloaded in {len(memory)} segment(s)" if memory else "erased")
        version = ecu.dids.get(0xF195, b"") if not state.bootloader else b"BOOTLOADER"
        labels["Software version"].setText(version.decode("latin-1"))
        periodic = sorted(state.periodic.items())
        labels["Periodic data"].setText(", ".join(f"F2{identifier:02X} {PERIODIC_MODES[entry['mode']]}"
                                                  for identifier, entry in periodic) or "none")
        if state.events:
            kinds = ", ".join("DTC status" if event["type"] == 0x01 else f"DID {event['record'].hex().upper()}"
                              for event in state.events)
            labels["Events"].setText(f"{kinds}: {'active' if state.events_active else 'set up, not started'}")
        else:
            labels["Events"].setText("none")
        controls = []
        for did in sorted(state.io_controls):
            try:
                controls.append(f"{did:04X} = {ecu._did_value(did).hex(' ').upper()}")
            except Exception:
                controls.append(f"{did:04X}")
        labels["I/O control"].setText(", ".join(controls) or "none")
        labels["Operation cycle"].setText(str(ecu.dtc_memory.cycle))
        if self.tabs.currentWidget() is self.data_tab:
            self._refresh_dtc_status()
        if self.tabs.currentWidget() is self.signals_tab:
            self._refresh_signal_values()

    def _refresh_dtc_status(self):
        """The Now and Fault columns: the running ECU's statuses and faults, however they changed."""
        statuses, faults = self.ecu.dtcs, self.ecu.dtc_memory.faults
        loading, self._loading = self._loading, True
        try:
            for row in range(self.dtc_table.rowCount()):
                try:
                    dtc = int(self.dtc_table.item(row, DTC_DTC).text(), 16)
                except ValueError:
                    continue
                status = statuses.get(dtc)
                item = self.dtc_table.item(row, DTC_NOW)
                text = "" if status is None else f"{status:02X}"
                if item.text() != text:
                    item.setText(text)
                    item.setToolTip("" if status is None else status_text(status))
                fault = Qt.Checked if dtc in faults else Qt.Unchecked
                if self.dtc_table.item(row, DTC_FAULT).checkState() != fault:
                    self.dtc_table.item(row, DTC_FAULT).setCheckState(fault)
        finally:
            self._loading = loading

    def _refresh_signal_values(self):
        signals = self.ecu.signals
        overridden = signals.overridden()
        loading, self._loading = self._loading, True
        try:
            for row in range(self.signal_table.rowCount()):
                key = self.signal_table.item(row, SIG_NAME).text()
                value = signals.value(key)
                text = number_text(value) + (" (I/O control)" if key in overridden else "")
                item = self.signal_table.item(row, SIG_NOW)
                if item.text() != text:
                    item.setText(text)
        finally:
            self._loading = loading

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
        self._refresh_dtc_status()
        self._log("ECU reset to its factory state (default session, locked, original DIDs and DTCs, erased memory, "
                  "a valid application)")

    def new_operation_cycle(self):
        self.ecu.new_operation_cycle()
        self._refresh_dtc_status()

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
        except (OSError, ValueError, TypeError, KeyError) as exc:
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
