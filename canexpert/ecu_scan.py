"""
Finding the ECUs on a bus: which identifiers answer, which sessions they take, and who they say they are.

The sweep sends TesterPresent to each request identifier of a range - 0x7E0-0x7E7 by default, any
11-bit range, or 29-bit normal fixed addresses 18DA<target><tester> - and whoever answers within a short
window is an ECU, at the identifier it answered on. Each one found is then asked, if wanted, which of
the default and extended sessions it accepts (and, only when asked for, the programming session, which
starts the bootloader of some ECUs) and for a few identification DIDs.

It runs beside a measurement - over a mailbox on the session's bus, with the session's own TesterPresent
paused so it cannot be mistaken for an answer - or on a channel opened for the purpose.
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field

import can
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from canexpert.uds.client import NRC_NAMES, uds_request
from canexpert.uds.isotp import drain
from canexpert.ui_common import enable_maximize

ELEVEN_BIT, NORMAL_FIXED = "11-bit identifiers", "29-bit normal fixed addressing"
IDENTIFICATION_DIDS = {0xF190: "VIN", 0xF187: "Part number", 0xF18C: "Serial number",
                       0xF195: "Software version", 0xF18A: "Supplier", 0xF191: "Hardware number"}
SESSIONS = {0x01: "default", 0x03: "extended", 0x02: "programming"}
COLUMNS = ["Request", "Response", "Sessions", "Identification"]


@dataclass
class ScanPlan:
    addressing: str = ELEVEN_BIT
    first: int = 0x7E0                  # request identifiers, or 29-bit target addresses
    last: int = 0x7E7
    tester_address: int = 0xF1          # the tester's source address with 29-bit normal fixed addressing
    listen: float = 0.06                # seconds to wait for an answer to each TesterPresent
    padding: int | None = 0xCC          # as the session pads its frames
    probe_sessions: bool = True
    programming_session: bool = False   # only when asked: it starts the bootloader of some ECUs
    read_identification: bool = True

    @property
    def extended(self) -> bool:
        return self.addressing == NORMAL_FIXED

    def requests(self) -> list[int]:
        if self.extended:
            return [0x18DA0000 | (target << 8) | self.tester_address for target in range(self.first, self.last + 1)]
        return list(range(self.first, self.last + 1))

    def check(self):
        maximum = 0xFF if self.extended else 0x7FF
        if not 0 <= self.first <= self.last <= maximum:
            raise ValueError(f"The range must run upwards within 0-{maximum:X}")
        return self


@dataclass
class Responder:
    request_id: int
    response_id: int
    extended: bool = False
    sessions: dict = field(default_factory=dict)       # session -> "accepted" or the NRC's name
    identification: dict = field(default_factory=dict)  # DID -> text or hex

    def row(self) -> list[str]:
        width = 8 if self.extended else 3
        sessions = ", ".join(f"{SESSIONS[session]} {outcome}" for session, outcome in self.sessions.items())
        identification = "; ".join(f"{IDENTIFICATION_DIDS[did]}: {value}" for did, value in self.identification.items())
        return [f"{self.request_id:0{width}X}", f"{self.response_id:0{width}X}", sessions, identification]


def _is_tester_present_answer(data: bytes) -> bool:
    """A positive (7E 00) or negative (7F 3E xx) answer to TesterPresent, in a single frame."""
    if not data:
        return False
    length = data[0]
    body = data[1:1 + length] if length <= 7 else b""
    return body[:2] == b"\x7e\x00" or (len(body) >= 3 and body[:2] == b"\x7f\x3e")


def _describe(value: bytes) -> str:
    text = value.decode("latin-1").strip("\x00 ")
    return text if text and all(32 <= ord(char) < 127 for char in text) else value.hex(" ").upper()


def find_responders(bus, plan: ScanPlan, progress=None, cancelled=None) -> list[Responder]:
    """TesterPresent to every request identifier of the plan; who answered, and where."""
    progress = progress or (lambda done, total, text: None)
    cancelled = cancelled or (lambda: False)
    requests = plan.requests()
    found = []
    frame = bytes([0x02, 0x3E, 0x00])
    if plan.padding is not None:
        frame = frame.ljust(8, bytes([plan.padding]))
    for index, request_id in enumerate(requests):
        if cancelled():
            break
        progress(index, len(requests), f"TesterPresent to {request_id:X}")
        drain(bus)
        bus.send(can.Message(arbitration_id=request_id, data=frame, is_extended_id=plan.extended))
        deadline = time.monotonic() + plan.listen
        while time.monotonic() < deadline:
            message = bus.recv(timeout=max(0.0, min(0.02, deadline - time.monotonic())))
            if message is None or message.is_error_frame or bool(message.is_extended_id) != plan.extended:
                continue
            if message.arbitration_id == request_id or not _is_tester_present_answer(bytes(message.data)):
                continue
            if plan.extended:
                target = (request_id >> 8) & 0xFF
                if message.arbitration_id != (0x18DA0000 | (plan.tester_address << 8) | target):
                    continue                         # an answer from someone else, addressed elsewhere
            found.append(Responder(request_id, message.arbitration_id, plan.extended))
            break
    progress(len(requests), len(requests), f"{len(found)} ECU(s) answered")
    return found


def probe(bus, responder: Responder, plan: ScanPlan, timeout=0.5):
    """Sessions and identification of one ECU found by the sweep."""
    def ask(payload):
        return uds_request(bus, payload, responder.request_id, responder.response_id, timeout,
                           extended=responder.extended, padding=plan.padding)

    if plan.probe_sessions:
        sessions = [0x01, 0x03] + ([0x02] if plan.programming_session else [])
        for session in sessions:
            reply = ask(bytes([0x10, session]))
            if reply is None:
                outcome = "no answer"
            elif reply[0] == 0x50:
                outcome = "accepted"
            else:
                outcome = NRC_NAMES.get(reply[2], f"NRC {reply[2]:02X}") if len(reply) > 2 else "refused"
            responder.sessions[session] = outcome
        ask(b"\x10\x01")                              # leave it as it was found
    if plan.read_identification:
        for did in IDENTIFICATION_DIDS:
            reply = ask(bytes([0x22, did >> 8, did & 0xFF]))
            if reply and reply[0] == 0x62 and reply[1:3] == bytes([did >> 8, did & 0xFF]) and len(reply) > 3:
                responder.identification[did] = _describe(reply[3:])
    return responder


class EcuScanner(QThread):
    """The sweep and the probes off the Qt thread."""
    progress = pyqtSignal(int, int, str)
    found = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, bus, plan: ScanPlan, mailbox=None):
        super().__init__()
        self.bus, self.plan, self.mailbox = bus, plan, mailbox    # mailbox: a session's, to pause its TesterPresent

    def run(self):
        try:
            if self.mailbox is not None:
                with self.mailbox.transaction():
                    self._scan()
            else:
                self._scan()
        except Exception as exc:                     # the bus went away, or refused to send
            self.failed.emit(str(exc))

    def _scan(self):
        responders = find_responders(self.bus, self.plan, self.progress.emit, self.isInterruptionRequested)
        for index, responder in enumerate(responders):
            if self.isInterruptionRequested():
                break
            self.progress.emit(index, len(responders), f"Asking {responder.request_id:X} who it is")
            self.found.emit(probe(self.bus, responder, self.plan))


class EcuScanDialog(QDialog):
    """Connection -> Scan for ECUs: the sweep, its results, and a configuration from one of them."""

    def __init__(self, parent=None, open_bus=None, new_configuration=None, detect_bitrate=None):
        """open_bus() -> (bus, mailbox or None, close callable, padding) for the scan, or raises ValueError
        with the reason; new_configuration(responder) offers a configuration for an ECU found."""
        super().__init__(parent)
        self.setWindowTitle("Scan for ECUs")
        enable_maximize(self)
        self.resize(860, 480)
        self.open_bus, self.new_configuration, self.detect_bitrate = open_bus, new_configuration, detect_bitrate
        self.scanner = None
        self._close_bus = None
        self.responders = []
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.addressing_combo = QComboBox()
        self.addressing_combo.addItems([ELEVEN_BIT, NORMAL_FIXED])
        self.addressing_combo.currentTextChanged.connect(self._on_addressing)
        form.addRow("Addressing:", self.addressing_combo)
        range_row = QHBoxLayout()
        self.first_edit, self.last_edit = QLineEdit("7E0"), QLineEdit("7E7")
        for edit in (self.first_edit, self.last_edit):
            edit.setFixedWidth(90)
        self.tester_edit = QLineEdit("F1")
        self.tester_edit.setFixedWidth(50)
        self.tester_label = QLabel("tester address:")
        range_row.addWidget(QLabel("from"))
        range_row.addWidget(self.first_edit)
        range_row.addWidget(QLabel("to"))
        range_row.addWidget(self.last_edit)
        range_row.addWidget(self.tester_label)
        range_row.addWidget(self.tester_edit)
        range_row.addStretch()
        form.addRow("Request identifiers (hex):", range_row)
        self.listen_spin = QSpinBox()
        self.listen_spin.setRange(10, 1000)
        self.listen_spin.setValue(60)
        self.listen_spin.setSuffix(" ms per identifier")
        form.addRow("Wait for an answer:", self.listen_spin)
        self.sessions_cb = QCheckBox("Try the default and extended sessions")
        self.sessions_cb.setChecked(True)
        self.programming_cb = QCheckBox("Also the programming session (it starts the bootloader of some ECUs)")
        self.identification_cb = QCheckBox("Read the identification DIDs (VIN, part and serial numbers, versions)")
        self.identification_cb.setChecked(True)
        for box in (self.sessions_cb, self.programming_cb, self.identification_cb):
            form.addRow("", box)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Scan")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.use_btn = QPushButton("New configuration from this ECU...")
        self.use_btn.setEnabled(False)
        self.use_btn.clicked.connect(self._use_selected)
        export = QPushButton("Export...")
        export.clicked.connect(self._export)
        self.bitrate_btn = QPushButton("Find the bit rate")
        self.bitrate_btn.setVisible(detect_bitrate is not None)
        self.bitrate_btn.clicked.connect(lambda: self.detect_bitrate(self))
        for button in (self.start_btn, self.stop_btn, self.use_btn, export, self.bitrate_btn):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 90)
        self.tree.setColumnWidth(1, 90)
        self.tree.setColumnWidth(2, 230)
        self.tree.header().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tree.itemSelectionChanged.connect(lambda: self.use_btn.setEnabled(
            self.new_configuration is not None and bool(self.tree.selectedItems())))
        layout.addWidget(self.tree, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._on_addressing(self.addressing_combo.currentText())

    def _on_addressing(self, text):
        fixed = text == NORMAL_FIXED
        self.tester_label.setVisible(fixed)
        self.tester_edit.setVisible(fixed)
        self.first_edit.setText("00" if fixed else "7E0")
        self.last_edit.setText("FF" if fixed else "7E7")

    def plan(self, padding=None) -> ScanPlan:
        """The plan the fields describe; ValueError with a message for the user when one is wrong."""
        try:
            first, last = int(self.first_edit.text(), 16), int(self.last_edit.text(), 16)
            tester = int(self.tester_edit.text(), 16)
        except ValueError:
            raise ValueError("The range and the tester address are hexadecimal, e.g. 7E0 to 7E7") from None
        return ScanPlan(self.addressing_combo.currentText(), first, last, tester, self.listen_spin.value() / 1000,
                        padding, self.sessions_cb.isChecked(), self.programming_cb.isChecked(),
                        self.identification_cb.isChecked()).check()

    def start(self):
        try:
            plan = self.plan()
            bus, mailbox, close, padding = self.open_bus()
        except (ValueError, OSError, can.CanError) as exc:
            self.status.setText(str(exc))
            return None
        plan.padding = padding
        self._close_bus = close
        self.tree.clear()
        self.responders = []
        self.scanner = EcuScanner(bus, plan, mailbox)
        self.scanner.progress.connect(self._on_progress)
        self.scanner.found.connect(self._on_found)
        self.scanner.failed.connect(lambda text: self.status.setText(f"The scan stopped: {text}"))
        self.scanner.finished.connect(self._on_finished)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText(f"Sending TesterPresent to {len(plan.requests())} identifier(s)...")
        self.scanner.start()
        return self.scanner

    def stop(self):
        if self.scanner is not None:
            self.scanner.requestInterruption()

    def _on_progress(self, done, total, text):
        self.progress_bar.setMaximum(max(1, total))
        self.progress_bar.setValue(done)
        self.progress_bar.setFormat(text)

    def _on_found(self, responder):
        self.responders.append(responder)
        QTreeWidgetItem(self.tree, responder.row())

    def _on_finished(self):
        if self._close_bus is not None:
            self._close_bus()
            self._close_bus = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if not self.status.text().startswith("The scan stopped"):
            self.status.setText(f"{len(self.responders)} ECU(s) found." if self.responders else
                                "No ECU answered. Check the bit rate, the range and the addressing.")

    def _use_selected(self):
        items = self.tree.selectedItems()
        if items and self.new_configuration is not None:
            self.new_configuration(self.responders[self.tree.indexOfTopLevelItem(items[0])])

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export the ECUs found", "", "CSV files (*.csv)")
        if path:
            self.export_csv(path)

    def export_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(COLUMNS)
            for responder in self.responders:
                writer.writerow(responder.row())

    def done(self, result):
        if self.scanner is not None and self.scanner.isRunning():
            self.scanner.requestInterruption()
            self.scanner.wait()
        super().done(result)
