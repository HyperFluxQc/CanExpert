"""
UDS console: every ISO 14229-1 service without an ODX file, and the ECU's fault memory.

The service list, its documentation and its parameters come from canexpert.uds.client, so the console
offers exactly what a panel script can call. Requests run on a background thread over a private
mailbox, the same way the ODX Diagnostic Window does, so the panel script keeps its own replies.
"""
from __future__ import annotations

import inspect
import threading

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canexpert.can_bus import ReceiveMailbox
from canexpert.config import uds_transport
from canexpert.uds.client import FUNCTIONS, GROUPS, UdsFunctions, make_request
from canexpert.uds.seed_key import SeedKeyError, dll_key, xor_key
from canexpert.ui_common import SplitterPanel, enable_maximize

# ISO 14229-1 Annex D: the bits of a DTC status byte, lowest first.
STATUS_BITS = ("testFailed", "testFailedThisOperationCycle", "pendingDTC", "confirmedDTC",
               "testNotCompletedSinceLastClear", "testFailedSinceLastClear",
               "testNotCompletedThisOperationCycle", "warningIndicatorRequested")
SESSIONS = (("Default (0x01)", 0x01), ("Programming (0x02)", 0x02), ("Extended (0x03)", 0x03))
SESSION_NAMES = {0x01: "default", 0x02: "programming", 0x03: "extended", 0x04: "safety system"}
KEY_SOURCES = ("key = seed XOR mask", "seed & key DLL")
# Parameters that carry a byte string rather than a number.
BYTE_PARAMETERS = {"data", "record", "parameter", "state", "mask", "event_record", "service_record", "key"}


def status_text(status: int) -> str:
    """'confirmedDTC, testFailed' for a DTC status byte."""
    return ", ".join(name for index, name in enumerate(STATUS_BITS) if status & (1 << index)) or "none"


def parse_bytes(text: str) -> bytes:
    """'22 F1 90' or '22F190' -> bytes; empty text gives b''."""
    cleaned = str(text).replace(",", " ").replace("0x", " ").strip()
    return bytes.fromhex(cleaned.replace(" ", "")) if cleaned else b""


def parse_int(text: str, default=0) -> int:
    text = str(text).strip()
    if not text:
        return default
    return int(text, 16 if not text.lower().startswith("0x") else 0)


class UdsConsoleWindow(QDialog):
    """Send any UDS service and read the fault memory, with no ODX file."""
    finished_request = pyqtSignal(object)

    def __init__(self, parent=None, session=None):
        super().__init__(parent)
        self.setWindowTitle("UDS Console")
        enable_maximize(self)
        self.setMinimumSize(860, 560)
        self.resize(1080, 700)
        # session() -> (bus, worker, config) while a measurement runs, else None.
        self.session = session or (lambda: None)
        self._widgets = {}
        self._parameters = []
        self._entry = None
        self._busy = False
        self.p2 = self.p2_star = None       # what the ECU said it needs, from its session answer
        self.session_state = "unknown"
        self.security_state = "locked"
        self._library = None                # the seed & key DLL, loaded once
        self._build_ui()
        self.finished_request.connect(self._on_result)

    # --- UI -----------------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(self._session_bar())
        tabs = QTabWidget()
        tabs.addTab(self._services_tab(), "Services")
        tabs.addTab(self._faults_tab(), "Fault memory")
        layout.addWidget(tabs, 1)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setPlaceholderText("Requests and responses appear here.")
        layout.addWidget(self.log, 1)

    def _session_bar(self):
        box = QGroupBox("Session and security")
        rows = QVBoxLayout(box)
        row = QHBoxLayout()
        rows.addLayout(row)
        self.session_combo = QComboBox()
        for label, value in SESSIONS:
            self.session_combo.addItem(label, value)
        self.session_combo.setCurrentIndex(2)
        row.addWidget(QLabel("Session:"))
        row.addWidget(self.session_combo)
        set_session = QPushButton("Set session")
        set_session.setToolTip("DiagnosticSessionControl (0x10)")
        set_session.clicked.connect(lambda: self.run(lambda uds: uds.DSC(self.session_combo.currentData()),
                                                     "DiagnosticSessionControl"))
        row.addWidget(set_session)
        row.addStretch()
        self.state_label = QLabel("")
        row.addWidget(self.state_label)

        security = QHBoxLayout()
        rows.addLayout(security)
        security.addWidget(QLabel("Security level:"))
        self.level_edit = QLineEdit("01")
        self.level_edit.setFixedWidth(46)
        self.level_edit.setToolTip("requestSeed sub-function (odd); sendKey is the next one")
        security.addWidget(self.level_edit)
        self.key_source = QComboBox()
        self.key_source.addItems(KEY_SOURCES)
        self.key_source.currentIndexChanged.connect(self._update_key_source)
        security.addWidget(self.key_source)
        self.mask_edit = QLineEdit("A5")
        self.mask_edit.setFixedWidth(46)
        self.mask_edit.setToolTip("The simple mask the simulated ECU uses; replace it for a real ECU")
        security.addWidget(self.mask_edit)
        self.dll_edit = QLineEdit()
        self.dll_edit.setPlaceholderText("Path to the seed & key DLL (GenerateKeyEx)")
        self.dll_edit.textChanged.connect(lambda _: setattr(self, "_library", None))
        security.addWidget(self.dll_edit, 1)
        self.browse_btn = QPushButton("Browse...")
        self.browse_btn.clicked.connect(self._browse_dll)
        security.addWidget(self.browse_btn)
        security.addWidget(QLabel("Variant:"))
        self.variant_edit = QLineEdit()
        self.variant_edit.setFixedWidth(90)
        self.variant_edit.setToolTip("The variant name the DLL expects, if it asks for one")
        security.addWidget(self.variant_edit)
        unlock = QPushButton("Unlock")
        unlock.setToolTip("SecurityAccess (0x27): requestSeed, then sendKey")
        unlock.clicked.connect(self._unlock)
        security.addWidget(unlock)
        self._update_key_source()
        self._update_state()
        return box

    def _update_key_source(self):
        """Show the mask or the DLL fields, whichever the key comes from."""
        from_dll = self.key_source.currentIndex() == 1
        self.mask_edit.setVisible(not from_dll)
        for widget in (self.dll_edit, self.browse_btn, self.variant_edit):
            widget.setVisible(from_dll)

    def _browse_dll(self):
        path, _ = QFileDialog.getOpenFileName(self, "Seed & key DLL", "", "DLL (*.dll);;All files (*.*)")
        if path:
            self.dll_edit.setText(path)

    def compute_key(self):
        """compute_key(seed) -> key, from the mask or from the DLL; SeedKeyError if it cannot be had."""
        level = parse_int(self.level_edit.text(), 1)
        if self.key_source.currentIndex() == 0:
            return xor_key(parse_int(self.mask_edit.text(), 0xA5))
        if not self.dll_edit.text().strip():
            raise SeedKeyError("No seed & key DLL chosen")
        if self._library is None:
            self._library = dll_key(self.dll_edit.text().strip(), level, self.variant_edit.text().strip())
        return self._library

    def _update_state(self):
        """The strip saying which session is open, what the ECU asked for, and whether it is unlocked."""
        timing = ""
        if self.p2 is not None:
            timing = f"   P2 {self.p2 * 1000:.0f} ms / P2* {self.p2_star * 1000:.0f} ms"
        self.state_label.setText(f"Session: {self.session_state}{timing}   Security: {self.security_state}")
        self.state_label.setStyleSheet("color: green;" if self.security_state.startswith("unlocked")
                                       else "color: gray;")

    def _services_tab(self):
        splitter = QSplitter(Qt.Horizontal)
        self.service_tree = QTreeWidget()
        self.service_tree.setHeaderLabels(["Service"])
        groups = {}
        for group in GROUPS:
            parent = QTreeWidgetItem([group])
            parent.setFlags(Qt.ItemIsEnabled)
            self.service_tree.addTopLevelItem(parent)
            parent.setExpanded(True)
            groups[group] = parent
        for entry in FUNCTIONS:
            label = f"{entry.name} ({entry.sid:#04x}) - {entry.service}" if entry.sid else f"{entry.name} - {entry.service}"
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.UserRole, entry.name)
            groups[entry.group].addChild(item)
        self.service_tree.currentItemChanged.connect(lambda item, _: self._select_service(item))
        splitter.addWidget(SplitterPanel("ISO 14229-1 services", self.service_tree, Qt.Horizontal))

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.doc_label = QLabel("Pick a service.")
        self.doc_label.setWordWrap(True)
        self.doc_label.setStyleSheet("color: gray;")
        right_layout.addWidget(self.doc_label)
        self.form_container = QWidget()
        self.form = QFormLayout(self.form_container)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form_container)
        right_layout.addWidget(scroll, 1)
        send_row = QHBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.setEnabled(False)
        self.send_btn.clicked.connect(self.send_service)
        send_row.addWidget(self.send_btn)
        send_row.addWidget(QLabel("or raw:"))
        self.raw_edit = QLineEdit()
        self.raw_edit.setPlaceholderText("22 F1 90")
        self.raw_edit.returnPressed.connect(self.send_raw)
        send_row.addWidget(self.raw_edit, 1)
        raw_btn = QPushButton("Send raw")
        raw_btn.clicked.connect(self.send_raw)
        send_row.addWidget(raw_btn)
        right_layout.addLayout(send_row)
        splitter.addWidget(SplitterPanel("Request", right, Qt.Horizontal))
        splitter.setSizes([340, 620])
        return splitter

    def _faults_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("Status mask (hex):"))
        self.dtc_mask_edit = QLineEdit("FF")
        self.dtc_mask_edit.setFixedWidth(50)
        row.addWidget(self.dtc_mask_edit)
        for text, slot, tip in (("Read DTCs", self.read_dtcs, "ReadDTCInformation reportDTCByStatusMask (0x19 02)"),
                                ("Count", self.count_dtcs, "reportNumberOfDTCByStatusMask (0x19 01)"),
                                ("Snapshot", self.read_snapshot, "reportDTCSnapshotRecordByDTCNumber (0x19 04)"),
                                ("Extended data", self.read_extended, "reportDTCExtendedDataRecord (0x19 06)"),
                                ("Clear all", self.clear_dtcs, "ClearDiagnosticInformation (0x14 FF FF FF)")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.dtc_table = QTableWidget(0, 3)
        self.dtc_table.setHorizontalHeaderLabels(["DTC", "Status", "Meaning"])
        self.dtc_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.dtc_table.setColumnWidth(0, 110)
        self.dtc_table.setColumnWidth(1, 70)
        self.dtc_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.dtc_table, 1)
        return page

    # --- the request form -------------------------------------------------------------------

    def _select_service(self, item):
        name = item.data(0, Qt.UserRole) if item is not None else None
        self._entry = next((entry for entry in FUNCTIONS if entry.name == name), None)
        while self.form.count():
            child = self.form.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._widgets = {}
        if self._entry is None:
            self.doc_label.setText("Pick a service.")
            self.send_btn.setEnabled(False)
            return
        self.doc_label.setText(f"{self._entry.signature}\n\n{self._entry.doc}\n\nExample: {self._entry.example}")
        self._parameters = [parameter for parameter
                            in inspect.signature(getattr(UdsFunctions, self._entry.name)).parameters.values()
                            if parameter.name not in ("self", "timeout")]
        for parameter in self._parameters:
            self.form.addRow(*self._field(parameter))
        self.send_btn.setEnabled(True)

    def _field(self, parameter):
        """A label and a widget for one function parameter, with its default filled in."""
        name = parameter.name
        default = parameter.default if parameter.default is not inspect.Parameter.empty else None
        if name == "suppress":
            widget = QCheckBox("suppressPosRspMsgIndicationBit (send without waiting)")
            self._widgets[name] = ("flag", widget)
            return ("", widget)
        if name == "compute_key":
            widget = QLineEdit("A5")
            widget.setToolTip("key = seed XOR this mask, byte by byte")
            self._widgets[name] = ("mask", widget)
            return ("key = seed XOR (hex):", widget)
        widget = QLineEdit()
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            widget.setPlaceholderText("further values, hexadecimal, separated by spaces")
            self._widgets[name] = ("numbers", widget)
            return (f"{name} (hex):", widget)
        if name in BYTE_PARAMETERS or name == "request":
            widget.setPlaceholderText("hexadecimal bytes, e.g. 01 02")
            if isinstance(default, (bytes, bytearray)) and default:
                widget.setText(bytes(default).hex(" "))
            self._widgets[name] = ("bytes", widget)
            return (f"{name} (hex):", widget)
        if name in ("path", "sources", "areas"):
            widget.setPlaceholderText("text" if name == "path" else "not editable here; use a panel script")
            widget.setEnabled(name == "path")
            self._widgets[name] = ("text" if name == "path" else "skip", widget)
            return (f"{name}:", widget)
        if isinstance(default, int):
            widget.setText(f"{default:02X}")
        widget.setPlaceholderText("hexadecimal")
        self._widgets[name] = ("int", widget)
        return (f"{name} (hex):", widget)

    def _arguments(self):
        """(positional arguments, keyword arguments) from the form, in the order the function declares.

        A field left empty falls back to the parameter's default; a required one without a value is an
        error the user sees, rather than a TypeError from the call.
        """
        args, kwargs = [], {}
        for parameter in self._parameters:
            kind, widget = self._widgets[parameter.name]
            text = widget.text().strip() if hasattr(widget, "text") else ""
            if kind == "skip":
                raise ValueError(f"{parameter.name} is a list; call this service from a panel script")
            if kind == "flag":
                if widget.isChecked():
                    kwargs[parameter.name] = True
                continue
            if kind == "numbers":
                args.extend(parse_int(part) for part in text.split() if part.strip())
                continue
            if kind == "mask":
                mask = parse_int(text, 0)
                value = lambda seed, mask=mask: bytes(byte ^ mask for byte in seed)  # noqa: E731
            elif not text:
                if parameter.default is inspect.Parameter.empty:
                    raise ValueError(f"{parameter.name} is required")
                value = parameter.default
            elif kind == "bytes":
                value = parse_bytes(text)
            elif kind == "text":
                value = text
            else:
                value = parse_int(text)
            if parameter.kind == inspect.Parameter.KEYWORD_ONLY:
                kwargs[parameter.name] = value
            else:
                args.append(value)
        return args, kwargs

    # --- running requests ---------------------------------------------------------------------

    def run(self, call, title):
        """Run call(UdsFunctions) on a background thread and report the result."""
        if self._busy:
            return None
        session = self.session()
        if not session:
            self._log("No measurement is running: connect or start one first.")
            return None
        bus, worker, config = session
        mailbox = ReceiveMailbox(bus, worker.message_sent.emit)
        worker.add_mailbox(mailbox)
        transport = uds_transport(config)
        functions = UdsFunctions(make_request(mailbox, transport), self._log, transport["timeout"])
        functions.p2, functions.p2_star = self.p2, self.p2_star
        self._busy = True
        thread = threading.Thread(target=self._exchange, args=(call, functions, worker, mailbox, title),
                                  daemon=True)
        thread.start()
        return thread

    def _exchange(self, call, functions, worker, mailbox, title):
        outcome = {"title": title, "text": "", "result": None, "p2": None, "p2_star": None}
        try:
            result = call(functions)
            outcome["result"] = result
            outcome["p2"], outcome["p2_star"] = functions.p2, functions.p2_star
            outcome["text"] = repr(result) if result is not None else "sent"
        except Exception as exc:                          # transport failure, or a bad parameter
            outcome["text"] = f"{type(exc).__name__}: {exc}"
        finally:
            worker.remove_mailbox(mailbox)
            mailbox.close()
        self.finished_request.emit(outcome)

    def _on_result(self, outcome):
        self._busy = False
        result, title = outcome["result"], outcome["title"]
        if outcome.get("p2") is not None:
            self.p2, self.p2_star = outcome["p2"], outcome["p2_star"]
        if title == "DiagnosticSessionControl" and getattr(result, "ok", False):
            session = self.session_combo.currentData()
            self.session_state = SESSION_NAMES.get(session, f"0x{session:02X}")
            if session == 0x01:
                self.security_state = "locked"     # the default session drops security access
        if title == "SecurityAccess":
            self.security_state = (f"unlocked (level {parse_int(self.level_edit.text(), 1)})"
                                   if getattr(result, "ok", False) else "locked")
        self._update_state()
        # The log says how the request went; the strip stays on the session and the security state.
        if isinstance(result, list):                      # ReadDTCs
            self._fill_dtcs(result)
            self._log(f"{title}: {len(result)} DTC(s)")
            return
        self._log(f"{title}: {outcome['text']}")
        if getattr(result, "ok", False) and getattr(result, "data", b""):
            self._log(f"    data: {result.hex()}   int: {result.int}   text: {result.text!r}")

    def _log(self, text):
        self.log.appendPlainText(str(text))

    def send_service(self):
        if self._entry is None:
            return None
        try:
            args, kwargs = self._arguments()
        except ValueError as exc:
            self._log(f"Invalid parameter: {exc}")
            return None
        name = self._entry.name
        return self.run(lambda uds: getattr(uds, name)(*args, **kwargs), name)

    def send_raw(self):
        text = self.raw_edit.text().strip()
        if not text:
            return None
        try:
            payload = parse_bytes(text)
        except ValueError as exc:
            self._log(f"Invalid request: {exc}")
            return None
        return self.run(lambda uds: uds.UDS(payload), "Raw request")

    def _unlock(self):
        level = parse_int(self.level_edit.text(), 1)
        try:
            compute_key = self.compute_key()
        except SeedKeyError as exc:
            self._log(f"SecurityAccess: {exc}")
            return None
        return self.run(lambda uds: uds.SecurityUnlock(level, compute_key), "SecurityAccess")

    # --- fault memory ----------------------------------------------------------------------------

    def read_dtcs(self):
        mask = parse_int(self.dtc_mask_edit.text(), 0xFF)
        return self.run(lambda uds: uds.ReadDTCs(mask), "ReadDTCInformation")

    def count_dtcs(self):
        mask = parse_int(self.dtc_mask_edit.text(), 0xFF)
        return self.run(lambda uds: uds.RDTCI(0x01, mask), "reportNumberOfDTCByStatusMask")

    def _selected_dtc(self):
        row = self.dtc_table.currentRow()
        if row < 0:
            self._log("Select a DTC in the table first.")
            return None
        return int(self.dtc_table.item(row, 0).text(), 16)

    def read_snapshot(self):
        dtc = self._selected_dtc()
        return None if dtc is None else self.run(lambda uds: uds.RDTCI(0x04, dtc, 0xFF), "DTC snapshot")

    def read_extended(self):
        dtc = self._selected_dtc()
        return None if dtc is None else self.run(lambda uds: uds.RDTCI(0x06, dtc, 0xFF), "DTC extended data")

    def clear_dtcs(self):
        return self.run(lambda uds: uds.CDTCI(0xFFFFFF), "ClearDiagnosticInformation")

    def _fill_dtcs(self, dtcs):
        self.dtc_table.setRowCount(len(dtcs))
        for row, (dtc, status) in enumerate(dtcs):
            for column, text in enumerate((f"{dtc:06X}", f"{status:02X}", status_text(status))):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.dtc_table.setItem(row, column, item)
