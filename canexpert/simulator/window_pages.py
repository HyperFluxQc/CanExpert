"""
The Dummy ECU window's tabs of settings - the connection bar, Addressing, Flow control, UDS, Access,
Flashing, Signals, Errors and the monitor - built from small widgets that apply every change at once.
"""
from __future__ import annotations


from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from canexpert.simulator.ecu import IMAGE_CHECKS, PERIODIC_MODES
from canexpert.simulator.widgets import HexSpinBox, hint
from canexpert.simulator.window_tables import MSG_NAME, SIG_NAME



INTERFACES = ("kvaser", "virtual", "vector", "ixxat", "pcan", "socketcan")
BITRATES = ("125000", "250000", "500000", "1000000")
ADDRESS_FORMATS = ("Any", "44", "24", "34", "14", "33", "22", "11")
IMAGE_CHECK_TEXT = {"off": "None: any complete download passes",
                    "option": "CRC-32 given as the check routine's option record",
                    "trailer": "CRC-32 in the image's last four bytes"}

# (setting, label, what happens) of the Errors tab
ERROR_ROWS = (
    ("error_refuse", "Refuse",
     "A negative response instead of the answer: 7F <service> <NRC>. 21 busyRepeatRequest asks the tester to "
     "send the request again, which CAN Expert does three times at most."),
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


class Pages:
    """The settings tabs of DummyEcuWindow (window.py)."""

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

    def _stmin_unit_changed(self, index):
        if index == 0:
            self.st_min.setRange(0, 127)
        else:
            self.st_min.setRange(1, 9)
        self._apply()
