"""
Transmit window (CANoe's Interactive Generator): a list of messages to send once or cyclically.

Rows are raw frames or messages of a symbol database; a database row can be edited signal by signal.
The list is kept in the settings between sessions and can be saved to a JSON file of its own, so no
configuration or panel file is involved.
"""
from __future__ import annotations

import json
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canexpert.cyclic import CyclicSchedule
from canexpert.ui_common import app_settings, enable_maximize

SETTING = "transmit_list"       # settings: the rows as JSON
TICK_MS = 5                     # how often due rows are looked for
MAX_CYCLE_MS = 3600000
COL_ON, COL_NAME, COL_ID, COL_EXT, COL_DLC, COL_DATA, COL_CYCLE, COL_COUNT = range(8)
HEADERS = ["On", "Name", "ID (hex)", "Ext", "DLC", "Data (hex)", "Cycle (ms)", "Sent"]


def default_row(name="Message", can_id=0x100, data=b"\x00", cycle_ms=100, extended=False, message=""):
    return {"enabled": False, "name": name, "id": int(can_id), "extended": bool(extended),
            "data": bytes(data), "cycle_ms": int(cycle_ms), "message": message, "sent": 0}


def rows_to_json(rows) -> str:
    return json.dumps([{"enabled": row["enabled"], "name": row["name"], "id": row["id"],
                        "extended": row["extended"], "data": bytes(row["data"]).hex(),
                        "cycle_ms": row["cycle_ms"], "message": row["message"]} for row in rows], indent=2)


def rows_from_json(text) -> list[dict]:
    rows = []
    for item in json.loads(text or "[]"):
        try:
            rows.append(default_row(str(item.get("name", "Message")), int(item.get("id", 0)),
                                    bytes.fromhex(str(item.get("data", ""))), int(item.get("cycle_ms", 100)),
                                    bool(item.get("extended", False)), str(item.get("message", ""))))
            rows[-1]["enabled"] = bool(item.get("enabled", False))
        except (TypeError, ValueError):
            continue                                     # a row that cannot be read is skipped, not fatal
    return rows


class MessagePicker(QDialog):
    """Pick a message of the symbol databases to add to the list."""

    def __init__(self, symbols, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add message from database")
        self.resize(420, 420)
        self.messages = symbols.messages() if symbols is not None else []
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        for message in self.messages:
            item = QListWidgetItem(f"{message.name}  (0x{message.frame_id:X}, {message.length} bytes)")
            item.setData(Qt.UserRole, message.name)
            self.list.addItem(item)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        layout.addWidget(self.list, 1)
        if not self.messages:
            layout.addWidget(QLabel("No symbol database loaded (Tools ▸ Symbol databases...)."))
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self):
        item = self.list.currentItem()
        if item is None:
            return None
        name = item.data(Qt.UserRole)
        return next((message for message in self.messages if message.name == name), None)


class SignalEditor(QDialog):
    """Edit one database message signal by signal; the encoded bytes are the result."""

    def __init__(self, message, data=b"", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Signals - {message.name}")
        self.resize(420, 460)
        self.message = message
        self.widgets = {}
        try:
            values = message.decode(bytes(data).ljust(message.length, b"\x00"), decode_choices=False,
                                    allow_truncated=True)
        except Exception:                                 # data that does not fit: start from the defaults
            values = {}
        layout = QVBoxLayout(self)
        inner = QWidget()
        form = QFormLayout(inner)
        for signal in message.signals:
            value = values.get(signal.name, signal.initial if signal.initial is not None else 0)
            if signal.choices:
                widget = QComboBox()
                for raw, label in sorted(signal.choices.items()):
                    widget.addItem(f"{label} ({raw})", int(raw))
                index = widget.findData(int(value) if isinstance(value, (int, float)) else 0)
                widget.setCurrentIndex(max(0, index))
            else:
                widget = QDoubleSpinBox()
                widget.setDecimals(0 if float(signal.scale).is_integer() and signal.scale >= 1 else 4)
                widget.setRange(float(signal.minimum) if signal.minimum is not None else -1e12,
                                float(signal.maximum) if signal.maximum is not None else 1e12)
                widget.setSingleStep(abs(float(signal.scale)) or 1.0)
                widget.setSuffix(f" {signal.unit}" if signal.unit else "")
                try:
                    widget.setValue(float(value))
                except (TypeError, ValueError):
                    widget.setValue(0.0)
            self.widgets[signal.name] = widget
            form.addRow(f"{signal.name}:", widget)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)
        self.error = QLabel("")
        self.error.setStyleSheet("color: red;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.data = bytes(data)

    def values(self):
        return {name: (widget.currentData() if isinstance(widget, QComboBox) else widget.value())
                for name, widget in self.widgets.items()}

    def _accept(self):
        try:
            self.data = bytes(self.message.encode(self.values(), strict=False))
        except Exception as exc:                          # out-of-range or missing signal
            self.error.setText(str(exc))
            return
        self.accept()


class TransmitWindow(QDialog):
    """Send messages once or cyclically, raw or from a database."""

    def __init__(self, parent=None, symbols=None, send=None, settings=None, stop_when_hidden=True):
        super().__init__(parent)
        self.setWindowTitle("Transmit")
        self.stop_when_hidden = stop_when_hidden     # off in the Transmit window's tab, which stops on close
        enable_maximize(self)
        self.setMinimumSize(720, 320)
        self.resize(900, 420)
        self.symbols = symbols
        self.send = send                                  # send(can_id, data, extended); raises when not connected
        self.settings = settings or app_settings()
        self.rows: list[dict] = rows_from_json(self.settings.value(SETTING, "", type=str))
        self._schedule = CyclicSchedule()
        self._updating = False
        self._build_ui()
        self._fill_table()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.tick)
        self._timer.start()

    # --- UI -----------------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        for text, slot, tip in (("Add", self.add_raw, "Add a raw frame"),
                                ("Add from database...", self.add_from_database,
                                 "Add a message of a symbol database"),
                                ("Edit signals...", self.edit_signals,
                                 "Edit the selected database message signal by signal"),
                                ("Duplicate", self.duplicate_selected, "Copy the selected row"),
                                ("Remove", self.remove_selected, "Delete the selected row")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            bar.addWidget(button)
        bar.addStretch()
        self.send_btn = QPushButton("Send now")
        self.send_btn.setToolTip("Send the selected row once")
        self.send_btn.clicked.connect(self.send_selected)
        bar.addWidget(self.send_btn)
        self.all_off_btn = QPushButton("All off")
        self.all_off_btn.setToolTip("Stop every cyclic row")
        self.all_off_btn.clicked.connect(self.stop_all)
        bar.addWidget(self.all_off_btn)
        for text, slot in (("Save list...", self.save_list), ("Load list...", self.load_list)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            bar.addWidget(button)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(COL_DATA, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemDoubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self.table, 1)
        self.status = QLabel("Connect a measurement to send. Tick a row's On box to send it cyclically.")
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)

    def _fill_table(self):
        self._updating = True
        try:
            self.table.setRowCount(len(self.rows))
            for index, row in enumerate(self.rows):
                self._fill_row(index, row)
        finally:
            self._updating = False

    def _fill_row(self, index, row):
        def cell(text, editable=True, checked=None):
            item = QTableWidgetItem(text)
            flags = item.flags() | Qt.ItemIsEnabled | Qt.ItemIsSelectable
            if checked is None:
                flags = flags | Qt.ItemIsEditable if editable else flags & ~Qt.ItemIsEditable
            else:
                flags = (flags | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            item.setFlags(flags)
            return item

        self.table.setItem(index, COL_ON, cell("", checked=row["enabled"]))
        self.table.setItem(index, COL_NAME, cell(row["name"]))
        self.table.setItem(index, COL_ID, cell(f"{row['id']:X}"))
        self.table.setItem(index, COL_EXT, cell("", checked=row["extended"]))
        self.table.setItem(index, COL_DLC, cell(str(len(row["data"])), editable=False))
        self.table.setItem(index, COL_DATA, cell(bytes(row["data"]).hex(" ").upper()))
        self.table.setItem(index, COL_CYCLE, cell(str(row["cycle_ms"])))
        self.table.setItem(index, COL_COUNT, cell(str(row["sent"]), editable=False))

    def _refresh_row(self, index):
        self._updating = True
        try:
            self._fill_row(index, self.rows[index])
        finally:
            self._updating = False

    # --- editing ---------------------------------------------------------------------------

    def _on_item_changed(self, item):
        if self._updating:
            return
        index, column, row = item.row(), item.column(), None
        if 0 <= item.row() < len(self.rows):
            row = self.rows[item.row()]
        if row is None:
            return
        text = item.text().strip()
        try:
            if column == COL_ON:
                row["enabled"] = item.checkState() == Qt.Checked
                self._schedule.start(index)                # a row just switched on sends at once
            elif column == COL_EXT:
                row["extended"] = item.checkState() == Qt.Checked
            elif column == COL_NAME:
                row["name"] = text or "Message"
            elif column == COL_ID:
                value = int(text, 16)
                if not 0 <= value <= (0x1FFFFFFF if row["extended"] else 0x7FF):
                    raise ValueError("identifier out of range")
                row["id"] = value
            elif column == COL_DATA:
                data = bytes.fromhex(text.replace(",", " "))
                if len(data) > 8:
                    raise ValueError("a classic CAN frame carries at most eight bytes")
                row["data"] = data
            elif column == COL_CYCLE:
                row["cycle_ms"] = max(1, min(MAX_CYCLE_MS, int(text)))
            self.status.setText("")
        except ValueError as exc:
            self.status.setText(f"{HEADERS[column]}: {exc}")
        self._refresh_row(index)
        self.save_rows()

    def _on_double_clicked(self, item):
        if item.column() == COL_DATA and self.rows[item.row()]["message"]:
            self.edit_signals()

    def add_raw(self):
        self.rows.append(default_row())
        self._fill_table()
        self.table.selectRow(len(self.rows) - 1)
        self.save_rows()

    def add_from_database(self):
        dialog = MessagePicker(self.symbols, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        message = dialog.selected()
        if message is None:
            return
        try:
            data = bytes(message.encode({signal.name: signal.initial if signal.initial is not None else 0
                                         for signal in message.signals}, strict=False))
        except Exception:
            data = bytes(message.length)
        cycle = int(message.cycle_time) if getattr(message, "cycle_time", None) else 100
        self.rows.append(default_row(message.name, message.frame_id, data, cycle,
                                     bool(message.is_extended_frame), message.name))
        self._fill_table()
        self.table.selectRow(len(self.rows) - 1)
        self.save_rows()

    def _selected(self):
        index = self.table.currentRow()
        return (index, self.rows[index]) if 0 <= index < len(self.rows) else (-1, None)

    def edit_signals(self):
        index, row = self._selected()
        if row is None:
            return
        message = None
        if row["message"] and self.symbols is not None:
            message = next((m for m in self.symbols.messages() if m.name == row["message"]), None)
        if message is None:
            self.status.setText("This row is a raw frame: edit its data bytes directly.")
            return
        editor = SignalEditor(message, row["data"], self)
        if editor.exec_() == QDialog.Accepted:
            row["data"] = editor.data
            self._refresh_row(index)
            self.save_rows()

    def duplicate_selected(self):
        index, row = self._selected()
        if row is not None:
            self.rows.insert(index + 1, dict(row, sent=0))
            self._fill_table()
            self.table.selectRow(index + 1)
            self.save_rows()

    def remove_selected(self):
        index, row = self._selected()
        if row is not None:
            self.rows.pop(index)
            self._schedule.clear()
            self._fill_table()
            self.save_rows()

    def stop_all(self):
        for row in self.rows:
            row["enabled"] = False
        self._fill_table()
        self.save_rows()

    # --- sending ---------------------------------------------------------------------------

    def send_row(self, index) -> bool:
        """Send one row once. False (with the reason in the status line) when it could not be sent."""
        row = self.rows[index]
        if self.send is None:
            self.status.setText("No measurement is running.")
            return False
        try:
            self.send(row["id"], row["data"], row["extended"])
        except Exception as exc:                          # not connected, listen-only, adapter error
            row["enabled"] = False                        # a failing row would otherwise repeat the error
            self._refresh_row(index)
            self.status.setText(f"{row['name']}: {exc}")
            return False
        row["sent"] += 1
        self._updating = True
        try:
            self.table.item(index, COL_COUNT).setText(str(row["sent"]))
        finally:
            self._updating = False
        return True

    def send_selected(self):
        index, row = self._selected()
        if row is not None:
            self.send_row(index)

    def tick(self):
        """Send every enabled row whose cycle time has come."""
        for index, row in enumerate(self.rows):
            if not row["enabled"]:
                self._schedule.drop(index)
            elif self._schedule.due(index, row["cycle_ms"] / 1000.0):
                self.send_row(index)

    # --- the list --------------------------------------------------------------------------

    def save_rows(self):
        self.settings.setValue(SETTING, rows_to_json(self.rows))

    def save_list(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save transmit list", "", "JSON files (*.json)")
        if path:
            Path(path).write_text(rows_to_json(self.rows), encoding="utf-8")

    def load_list(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load transmit list", "", "JSON files (*.json)")
        if not path:
            return
        try:
            self.rows = rows_from_json(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Transmit list", f"Cannot read {Path(path).name}: {exc}")
            return
        self._schedule.clear()
        self._fill_table()
        self.save_rows()

    def hideEvent(self, event):
        """Closing the window stops every cyclic row: nothing keeps sending out of sight."""
        # getattr: Qt also hides a window it is destroying, after Python has already cleared its attributes.
        if getattr(self, "stop_when_hidden", False):
            self.stop_all()
        super().hideEvent(event)
