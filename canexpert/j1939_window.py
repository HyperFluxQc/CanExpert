"""
The J1939 window: the nodes of the network with the NAME each claimed its address with, the faults each
reports in DM1 (and DM2 on request) with its lamps, and any PGN requested from a node or sent to it.

What the window sees comes from the measurement's frames (on_frame), put together by the transport protocol
when a message spans several; what it asks and sends goes through its own mailbox on the session's bus, from
CAN Expert's J1939 address (F9, the off-board diagnostic tool, by default).
"""
from __future__ import annotations

import threading
import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from canexpert.can_bus import ReceiveMailbox
from canexpert.clock import absolute_text
from canexpert.j1939.dm import LAMPS, parse_dm
from canexpert.j1939.name import Name
from canexpert.j1939.pgn import (GLOBAL, PGN_ADDRESS_CLAIMED, PGN_CI, PGN_DM1, PGN_DM2, PGN_DM3, PGN_DM11, PGN_SOFT,
                                 PGN_VI, TOOL_ADDRESS, address_text, make_id, pgn_name)
from canexpert.j1939.transport import ACK, J1939Assembler, J1939Link
from canexpert.simulator.widgets import HexSpinBox

ADDRESS_SETTING = "j1939/address"            # CAN Expert's source address on a J1939 network
ACK_TEXT = {0: "ACK", 1: "NACK: the node does not have it", 2: "access denied", 3: "busy, ask again"}
# The PGNs offered in the Request field (it takes any other as well).
COMMON_REQUESTS = (PGN_SOFT, PGN_VI, PGN_CI, PGN_DM1, PGN_DM2, PGN_ADDRESS_CLAIMED, 0xFEE5, 0xFEE0, 0xFEF1)


def text_fields(data: bytes) -> str:
    """SOFT, VI and CI carry text fields, each ending with *; SOFT starts with how many there are."""
    body = bytes(data)
    if body[:1] and body[0] < 0x20:
        body = body[1:]
    fields = [field.decode("latin-1") for field in body.split(b"*")]
    return " | ".join(field for field in fields if field)


def address_setting(settings) -> int:
    try:
        value = int(settings.value(ADDRESS_SETTING, TOOL_ADDRESS))
    except (TypeError, ValueError):
        return TOOL_ADDRESS
    return value if 0 <= value <= 0xFD else TOOL_ADDRESS


class J1939Window(QWidget):
    """session() -> (bus, worker, configuration) while a measurement runs, else None."""
    finished = pyqtSignal(object)               # (title, J1939Message or None, error text), from the thread

    def __init__(self, parent=None, session=None, symbols=None, settings=None, time_text=None):
        super().__init__(parent)
        self.session = session or (lambda: None)
        self.symbols = symbols
        self.settings = settings
        self.time_text = time_text or absolute_text
        self.assembler = J1939Assembler()
        self.nodes = {}                          # address -> (Name, time of its last claim)
        self.faults = {}                         # (source, "active" / "previously active") -> DiagnosticMessage
        self._busy = False
        self._build_ui()
        self.finished.connect(self._on_finished)

    # --- UI -----------------------------------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("CAN Expert's address:"))
        self.address_spin = HexSpinBox(0xFD)
        self.address_spin.setValue(address_setting(self.settings) if self.settings is not None else TOOL_ADDRESS)
        self.address_spin.setToolTip("The source address of what the window requests and sends (F9: off-board "
                                     "diagnostic-service tool #1)")
        self.address_spin.valueChanged.connect(self._remember_address)
        bar.addWidget(self.address_spin)
        bar.addStretch()
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Vertical)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._network_tab(), "Network")
        self.tabs.addTab(self._faults_tab(), "Faults (DM1)")
        self.tabs.addTab(self._request_tab(), "Request and send")
        splitter.addWidget(self.tabs)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setPlaceholderText("Requests, answers and what was sent appear here.")
        splitter.addWidget(self.log)
        splitter.setSizes([420, 160])
        layout.addWidget(splitter, 1)

    @staticmethod
    def _table(headers, widths):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.verticalHeader().setVisible(False)
        for column, width in enumerate(widths):
            table.setColumnWidth(column, width)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _network_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        claim = QPushButton("Request address claims")
        claim.setToolTip("Request Address Claimed (PGN 60928) from every node: each answers with its NAME")
        claim.clicked.connect(self.request_claims)
        row.addWidget(claim)
        row.addStretch()
        layout.addLayout(row)
        self.node_table = self._table(["Address", "Function", "NAME", "Manufacturer", "Identity", "Industry group",
                                       "Last claim"], (70, 190, 150, 95, 80, 150))
        layout.addWidget(self.node_table, 1)
        return page

    def _faults_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("Node:"))
        self.fault_node = QComboBox()
        self.fault_node.setEditable(True)
        self.fault_node.setToolTip("The node's address (hex), or pick one the window has seen")
        self.fault_node.setMinimumWidth(80)
        row.addWidget(self.fault_node)
        for text, slot, tip in (("Read previously active (DM2)", self.read_dm2, "Request DM2 (PGN 65227)"),
                                ("Clear active (DM11)", self.clear_active, "Request DM11 (PGN 65235): the node "
                                 "clears its active faults and answers with an acknowledgment"),
                                ("Clear previously active (DM3)", self.clear_previous, "Request DM3 (PGN 65228)")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.fault_table = self._table(["Node", "Kind", "SPN", "FMI", "Description", "Occurrences", "Lamps"],
                                       (60, 120, 70, 45, 360, 85))
        layout.addWidget(self.fault_table, 1)
        self.fault_label = QLabel("Active faults arrive in DM1, which nodes send every second while one is active.")
        self.fault_label.setStyleSheet("color: gray;")
        layout.addWidget(self.fault_label)
        return page

    def _request_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("PGN (hex):"))
        self.pgn_combo = QComboBox()
        self.pgn_combo.setEditable(True)
        for pgn in COMMON_REQUESTS:
            self.pgn_combo.addItem(f"{pgn:04X} {pgn_name(pgn)}", pgn)
        self.pgn_combo.setMinimumWidth(170)
        row.addWidget(self.pgn_combo)
        row.addWidget(QLabel("to (hex, FF: everyone):"))
        self.destination_edit = QLineEdit("00")
        self.destination_edit.setFixedWidth(50)
        row.addWidget(self.destination_edit)
        request = QPushButton("Request")
        request.setToolTip("Request (PGN 59904) this PGN and wait for the answer - one frame, a BAM, or an "
                           "RTS/CTS session the window takes part in")
        request.clicked.connect(lambda: self.request())
        row.addWidget(request)
        row.addStretch()
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Data (hex):"))
        self.data_edit = QLineEdit()
        self.data_edit.setPlaceholderText("up to 1785 bytes; more than eight go as a BAM or an RTS/CTS session")
        row.addWidget(self.data_edit, 1)
        row.addWidget(QLabel("Priority:"))
        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 7)
        self.priority_spin.setValue(6)
        row.addWidget(self.priority_spin)
        send = QPushButton("Send")
        send.setToolTip("Send this PGN with the data to the node (or to everyone)")
        send.clicked.connect(lambda: self.send())
        row.addWidget(send)
        layout.addLayout(row)
        layout.addStretch()
        return page

    def _remember_address(self, value):
        if self.settings is not None:
            self.settings.setValue(ADDRESS_SETTING, int(value))

    def _write(self, text):
        self.log.appendPlainText(f"{self.time_text(time.time())}  {text}")

    # --- what the bus shows -------------------------------------------------------------------------------------

    def on_frame(self, timestamp, direction, can_id, data, extended=False):
        """Every frame of the measurement: address claims and DM1 are kept, whole when they span several."""
        if not extended:
            return
        for message in self.assembler.push(timestamp, can_id, data):
            if message.pgn == PGN_ADDRESS_CLAIMED and len(message.data) >= 8:
                self.nodes[message.source] = (Name.from_bytes(message.data), timestamp)
                self._fill_nodes()
            elif message.pgn == PGN_DM1 and message.complete and len(message.data) >= 2:
                self._set_faults(message.source, "active", message.data)

    def _fill_nodes(self):
        self.node_table.setRowCount(len(self.nodes))
        for row, address in enumerate(sorted(self.nodes)):
            name, when = self.nodes[address]
            values = (address_text(address), name.function_text(), f"{name.to_int():016X}", str(name.manufacturer),
                      f"0x{name.identity:05X}", f"{name.industry_group}", self.time_text(when))
            for column, value in enumerate(values):
                self.node_table.setItem(row, column, QTableWidgetItem(value))
        known = {self.fault_node.itemData(index) for index in range(self.fault_node.count())}
        for address in sorted(set(self.nodes) - known):
            self.fault_node.addItem(f"{address:02X}", address)

    def _set_faults(self, source, kind, data):
        try:
            message = parse_dm(data)
        except ValueError:
            return
        if self.faults.get((source, kind)) == message:
            return
        self.faults[(source, kind)] = message
        if self.fault_node.findData(source) < 0:
            self.fault_node.addItem(f"{source:02X}", source)
        self._fill_faults()

    def _fill_faults(self):
        rows = [(node, what, dtc, dm) for (node, what), dm in sorted(self.faults.items()) for dtc in (dm.dtcs or [None])]
        self.fault_table.setRowCount(len(rows))
        for row, (node, what, dtc, dm) in enumerate(rows):
            values = (f"{node:02X}", what) + ((str(dtc.spn), str(dtc.fmi), dtc.text(), str(dtc.occurrences))
                                              if dtc is not None else ("", "", "no fault", ""))
            for column, value in enumerate(values + (dm.lamps_text(),)):
                self.fault_table.setItem(row, column, QTableWidgetItem(value))
        active = sum(len(dm.dtcs) for (_node, what), dm in self.faults.items() if what == "active")
        self.fault_label.setText(f"{active} active fault{'s' if active != 1 else ''} on "
                                 f"{sum(1 for _node, what in self.faults if what == 'active')} node(s). "
                                 f"Lamps: {', '.join(LAMPS)}.")

    # --- asking and sending ---------------------------------------------------------------------------------------

    def _link(self):
        """A J1939 link over a mailbox of the session's worker, and a way to put it away; None: not connected."""
        session = self.session()
        if not session:
            self._write("No measurement is running: connect first.")
            return None, None
        bus, worker, _config = session
        mailbox = ReceiveMailbox(bus, worker.message_sent.emit)
        worker.add_mailbox(mailbox)

        def close():
            worker.remove_mailbox(mailbox)
            mailbox.close()
        return J1939Link(mailbox, self.address_spin.value()), close

    def _run(self, title, call):
        """call(link) on a background thread; its J1939Message (or None) reaches _on_finished."""
        if self._busy:
            return None
        link, close = self._link()
        if link is None:
            return None
        self._busy = True

        def work():
            result, error = None, ""
            try:
                result = call(link)
            except Exception as exc:                  # a session that failed, a bus that went away
                error = f"{type(exc).__name__}: {exc}"
            finally:
                close()
            self.finished.emit((title, result, error))
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread

    def _node(self) -> int | None:
        text = self.fault_node.currentText().strip()
        try:
            return int(text, 16) & 0xFF
        except ValueError:
            self._write("Pick the node (its address in hex) first.")
            return None

    def request_claims(self):
        return self._run("Address claims requested", lambda link: link.send_request(PGN_ADDRESS_CLAIMED, GLOBAL))

    def read_dm2(self):
        node = self._node()
        return None if node is None else self._run("DM2", lambda link: link.request(PGN_DM2, node))

    def clear_active(self):
        node = self._node()
        return None if node is None else self._run("DM11", lambda link: link.request(PGN_DM11, node))

    def clear_previous(self):
        node = self._node()
        return None if node is None else self._run("DM3", lambda link: link.request(PGN_DM3, node))

    def _pgn(self) -> int | None:
        index = self.pgn_combo.currentIndex()
        text = self.pgn_combo.currentText().strip()
        if index >= 0 and text == self.pgn_combo.itemText(index):
            return self.pgn_combo.itemData(index)
        try:
            return int(text.split()[0], 16) & 0x3FFFF
        except (ValueError, IndexError):
            self._write(f"Invalid PGN: {text!r} (hexadecimal, e.g. FEDA)")
            return None

    def _destination(self) -> int | None:
        try:
            return int(self.destination_edit.text().strip() or "FF", 16) & 0xFF
        except ValueError:
            self._write("Invalid destination: an address in hex, FF for everyone")
            return None

    def request(self, pgn=None, destination=None):
        pgn = self._pgn() if pgn is None else pgn
        destination = self._destination() if destination is None else destination
        if pgn is None or destination is None:
            return None
        return self._run(f"Request {pgn_name(pgn) or 'PGN'} {pgn} from {address_text(destination)}",
                         lambda link: link.request(pgn, destination))

    def send(self):
        pgn, destination = self._pgn(), self._destination()
        try:
            data = bytes.fromhex(self.data_edit.text().replace(",", " ").replace("0x", " "))
        except ValueError:
            self._write("Invalid data: hexadecimal bytes, e.g. 01 02 03")
            return None
        if pgn is None or destination is None:
            return None
        priority = self.priority_spin.value()
        return self._run(f"Sent {pgn_name(pgn) or 'PGN'} {pgn} to {address_text(destination)}, {len(data)} bytes",
                         lambda link: link.send(pgn, data, destination, priority))

    def _on_finished(self, outcome):
        self._busy = False
        title, result, error = outcome
        if error:
            self._write(f"{title}: {error}")
            return
        if result is None:
            self._write(title if title.startswith(("Sent", "Address claims")) else f"{title}: no answer")
            return
        if result.acknowledgment is not None:
            self._write(f"{title}: {ACK_TEXT.get(result.acknowledgment, f'control {result.acknowledgment}')} "
                        f"from {result.source:02X}")
            if result.acknowledgment == ACK and result.pgn in (PGN_DM11, PGN_DM3):
                self.faults.pop((result.source, "active" if result.pgn == PGN_DM11 else "previously active"), None)
                self._fill_faults()                  # a fault still present comes back with the next DM1
            return
        self._write(f"{title}: {result.summary()}")
        self._write(f"    {result.data.hex(' ').upper()}")
        explained = self.explain(result)
        if explained:
            self._write(f"    {explained}")

    def explain(self, message) -> str:
        """What an answer says, where the window knows its PGN or a symbol database does."""
        if message.pgn in (PGN_SOFT, PGN_VI, PGN_CI):
            return text_fields(message.data)
        if message.pgn in (PGN_DM1, PGN_DM2):
            kind = "active" if message.pgn == PGN_DM1 else "previously active"
            self._set_faults(message.source, kind, message.data)
            dm = parse_dm(message.data)
            return "; ".join(dtc.text() for dtc in dm.dtcs) or f"no {kind} fault"
        if message.pgn == PGN_ADDRESS_CLAIMED and len(message.data) >= 8:
            return Name.from_bytes(message.data).describe()
        if self.symbols is not None:
            can_id = make_id(message.pgn, message.source, message.destination, message.priority)
            signals = self.symbols.decode(can_id, message.data)
            if signals:
                return ", ".join(f"{name} = {value:.6g}" if isinstance(value, float) else f"{name} = {value}"
                                 for name, value in signals.items())
        return ""
