"""
Simulated nodes: the messages an ECU would send, sent by CAN Expert instead.

This is CANoe's rest-bus simulation in small: a symbol database says which node sends which message and
how often, so ticking a node puts its messages on the bus at their cycle times. It is what makes an
ECU on the bench believe the rest of the car is there.
"""
from __future__ import annotations

import json

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from canexpert.cyclic import CyclicSchedule
from canexpert.transmit_window import SignalEditor
from canexpert.ui_common import app_settings, enable_maximize, style_toggle

SETTING = "simulated_messages"     # settings: the messages that were being sent, as JSON
TICK_MS = 5
DEFAULT_CYCLE_MS = 100             # for a message whose database gives no cycle time
NO_SENDER = "(no sender named)"
COL_NAME, COL_ID, COL_CYCLE, COL_DLC, COL_DATA, COL_COUNT = range(6)
HEADERS = ["Node / message", "ID", "Cycle (ms)", "DLC", "Data (hex)", "Sent"]


class SimulationWindow(QDialog):
    """Tick the nodes of a symbol database to send their messages, as the real ECUs would."""

    def __init__(self, parent=None, symbols=None, send=None, settings=None):
        super().__init__(parent)
        self.setWindowTitle("Simulated nodes")
        enable_maximize(self)
        self.setMinimumSize(640, 340)
        self.resize(860, 520)
        self.symbols = symbols
        self.send = send                 # send(can_id, data, extended); raises when nothing is connected
        self.settings = settings or app_settings()
        self.messages = {}               # message name -> {"message", "data", "cycle_ms", "sent", "on"}
        self._items = {}
        self._schedule = CyclicSchedule()
        self._updating = False
        self._build_ui()
        if symbols is not None:
            symbols.changed.connect(self.rebuild)
        self.rebuild()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.tick)
        self._timer.start()

    # --- UI -----------------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.start_btn = style_toggle(QPushButton("Start sending"))
        self.start_btn.setCheckable(True)
        self.start_btn.setToolTip("Send the ticked messages at their cycle times")
        self.start_btn.toggled.connect(self._on_start_toggled)
        bar.addWidget(self.start_btn)
        for text, slot, tip in (("Edit signals...", self.edit_signals,
                                 "Set what the selected message carries, signal by signal"),
                                ("Send once", self.send_selected, "Send the selected message once"),
                                ("Tick node", lambda: self._set_node(True), "Send every message of the node"),
                                ("Untick node", lambda: self._set_node(False), "Stop the node's messages")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            bar.addWidget(button)
        bar.addStretch()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter nodes and messages...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        bar.addWidget(self.filter_edit, 1)
        layout.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setColumnWidth(COL_NAME, 260)
        self.tree.setColumnWidth(COL_ID, 80)
        self.tree.setColumnWidth(COL_CYCLE, 90)
        self.tree.setColumnWidth(COL_DLC, 50)
        self.tree.header().setSectionResizeMode(COL_DATA, QHeaderView.Stretch)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemDoubleClicked.connect(lambda item, _column: self.edit_signals())
        layout.addWidget(self.tree, 1)
        self.status = QLabel("")
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)

    def rebuild(self):
        """A branch per node of the databases, with the messages it sends."""
        remembered = self._remembered()
        kept = {name: entry for name, entry in self.messages.items()}
        self.messages, self._items = {}, {}
        self._updating = True
        try:
            self.tree.clear()
            for node, messages in sorted(self._nodes().items()):
                parent = QTreeWidgetItem([node, "", "", "", "", ""])
                parent.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.tree.addTopLevelItem(parent)
                parent.setExpanded(True)
                for message in messages:
                    previous = kept.get(message.name, {})
                    entry = {"message": message, "sent": previous.get("sent", 0),
                             "cycle_ms": previous.get("cycle_ms") or int(message.cycle_time or DEFAULT_CYCLE_MS),
                             "data": previous.get("data") or self._initial_data(message),
                             "on": previous.get("on", message.name in remembered)}
                    self.messages[message.name] = entry
                    item = QTreeWidgetItem(parent, [message.name, f"{message.frame_id:03X}",
                                                    str(entry["cycle_ms"]), str(message.length),
                                                    entry["data"].hex(" ").upper(), str(entry["sent"])])
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
                    item.setCheckState(COL_NAME, Qt.Checked if entry["on"] else Qt.Unchecked)
                    self._items[message.name] = item
        finally:
            self._updating = False
        self._apply_filter()
        self._update_status()

    def _nodes(self):
        """node name -> the messages it sends, from the symbol databases."""
        nodes = {}
        for message in (self.symbols.messages() if self.symbols is not None else []):
            for sender in (message.senders or [NO_SENDER]):
                nodes.setdefault(sender, []).append(message)
        return nodes

    @staticmethod
    def _initial_data(message):
        """The message as its database describes it at rest."""
        try:
            return bytes(message.encode({signal.name: signal.initial if signal.initial is not None else 0
                                         for signal in message.signals}, strict=False))
        except Exception:                      # a database that cannot be encoded from its initial values
            return bytes(message.length)

    def _apply_filter(self, *_):
        text = self.filter_edit.text().strip().lower()
        for index in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(index)
            visible = 0
            for child in range(node.childCount()):
                item = node.child(child)
                shown = not text or text in item.text(COL_NAME).lower() or text in node.text(COL_NAME).lower()
                item.setHidden(not shown)
                visible += shown
            node.setHidden(visible == 0)

    # --- editing ---------------------------------------------------------------------------

    def _on_item_changed(self, item, column):
        if self._updating or item.parent() is None:
            return
        entry = self.messages.get(item.text(COL_NAME))
        if entry is None:
            return
        problem = ""
        if column == COL_NAME:
            entry["on"] = item.checkState(COL_NAME) == Qt.Checked
            self._schedule.start(item.text(COL_NAME)) if entry["on"] else self._schedule.drop(item.text(COL_NAME))
        elif column == COL_CYCLE:
            try:
                entry["cycle_ms"] = max(1, int(item.text(COL_CYCLE)))
            except ValueError:
                problem = f"{entry['message'].name}: the cycle time must be a number of milliseconds"
        self._refresh_row(entry)
        self._remember()
        self._update_status()
        if problem:
            self.status.setText(problem)

    def _refresh_row(self, entry):
        item = self._items.get(entry["message"].name)
        if item is None:
            return
        self._updating = True
        try:
            item.setCheckState(COL_NAME, Qt.Checked if entry["on"] else Qt.Unchecked)
            item.setText(COL_CYCLE, str(entry["cycle_ms"]))
            item.setText(COL_DATA, entry["data"].hex(" ").upper())
            item.setText(COL_COUNT, str(entry["sent"]))
        finally:
            self._updating = False

    def _selected(self):
        item = self.tree.currentItem()
        if item is None:
            return None
        return self.messages.get(item.text(COL_NAME))

    def _set_node(self, on: bool):
        """Tick or untick every message of the selected node."""
        item = self.tree.currentItem()
        node = item if item is not None and item.parent() is None else (item.parent() if item else None)
        if node is None:
            return
        for child in range(node.childCount()):
            entry = self.messages.get(node.child(child).text(COL_NAME))
            if entry is not None:
                entry["on"] = on
                self._schedule.start(entry["message"].name) if on else self._schedule.drop(entry["message"].name)
                self._refresh_row(entry)
        self._remember()
        self._update_status()

    def edit_signals(self):
        """Set what a message carries, signal by signal."""
        entry = self._selected()
        if entry is None:
            return
        editor = SignalEditor(entry["message"], entry["data"], self)
        if editor.exec_() == QDialog.Accepted:
            entry["data"] = editor.data
            self._refresh_row(entry)

    # --- sending ---------------------------------------------------------------------------

    def _on_start_toggled(self, running):
        self.start_btn.setText("Stop sending" if running else "Start sending")
        for name in list(self.messages):
            self._schedule.start(name)
        self._update_status()

    def send_message(self, name) -> bool:
        """Send one message once. False (with the reason in the status line) when it could not go out."""
        entry = self.messages.get(name)
        if entry is None or self.send is None:
            self.status.setText("No measurement is running.")
            return False
        try:
            self.send(entry["message"].frame_id, entry["data"], bool(entry["message"].is_extended_frame))
        except Exception as exc:                          # not connected, or the adapter refused
            self.start_btn.setChecked(False)              # stop the whole simulation rather than spin
            self.status.setText(f"{name}: {exc}")
            return False
        entry["sent"] += 1
        self._refresh_row(entry)
        return True

    def send_selected(self):
        entry = self._selected()
        if entry is not None:
            self.send_message(entry["message"].name)

    def tick(self):
        """Send every ticked message whose cycle time has come."""
        if not self.start_btn.isChecked():
            return
        for name, entry in list(self.messages.items()):
            if entry["on"] and self._schedule.due(name, entry["cycle_ms"] / 1000.0):
                if not self.send_message(name):
                    return

    def _update_status(self):
        ticked = [entry for entry in self.messages.values() if entry["on"]]
        nodes = {sender for entry in ticked for sender in (entry["message"].senders or [NO_SENDER])}
        state = "sending" if self.start_btn.isChecked() else "stopped"
        self.status.setText(f"{len(ticked)} message(s) of {len(nodes)} node(s) ticked - {state}"
                            if ticked else "Tick the nodes or messages to simulate, then press Start sending")

    # --- what is remembered ------------------------------------------------------------------

    def _remembered(self):
        try:
            return set(json.loads(self.settings.value(SETTING, "[]", type=str) or "[]"))
        except ValueError:
            return set()

    def _remember(self):
        self.settings.setValue(SETTING, json.dumps(sorted(name for name, entry in self.messages.items()
                                                          if entry["on"])))

    def hideEvent(self, event):
        """Closing the window stops the simulation: nothing keeps sending out of sight."""
        self.start_btn.setChecked(False)
        super().hideEvent(event)
