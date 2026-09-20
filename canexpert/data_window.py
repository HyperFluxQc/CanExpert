"""
Data window: every signal of the symbol databases with the value it holds right now.

The Trace lists frames and the CAN Logger draws signals over time; this is the third view CANoe has -
a flat table answering "what is this signal at the moment", raw and physical, and how long ago it last
changed hands.
"""
from __future__ import annotations

import csv
import time

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from canexpert.ui_common import enable_maximize

REFRESH_MS = 200
COL_SIGNAL, COL_PHYSICAL, COL_UNIT, COL_RAW, COL_AGE, COL_COUNT, COL_ID = range(7)
HEADERS = ["Signal", "Value", "Unit", "Raw", "Age (s)", "Count", "ID"]


def format_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


class SignalValues:
    """The latest value of every signal seen, kept apart from the window so it can be tested alone."""

    def __init__(self, symbols=None):
        self.symbols = symbols
        self.values = {}        # "Message.Signal" -> {"physical", "raw", "unit", "time", "count", "id"}

    def add(self, timestamp, can_id, data):
        """Decode a frame and remember what each of its signals now holds."""
        message = self.symbols.message(can_id) if self.symbols is not None else None
        if message is None:
            return []
        try:
            physical = message.decode(bytes(data), decode_choices=False, allow_truncated=True)
            raw = message.decode(bytes(data), decode_choices=False, scaling=False, allow_truncated=True)
        except Exception:                                  # a frame that does not fit the definition
            return []
        names = []
        for signal in message.signals:
            if signal.name not in physical:
                continue
            name = f"{message.name}.{signal.name}"
            entry = self.values.setdefault(name, {"count": 0, "unit": signal.unit or "", "id": can_id})
            entry.update(physical=physical[signal.name], raw=raw.get(signal.name),
                         time=float(timestamp), count=entry["count"] + 1)
            names.append(name)
        return names

    def rows(self, now=None):
        now = time.time() if now is None else now
        return [{"name": name, **entry, "age": now - entry["time"]}
                for name, entry in sorted(self.values.items())]

    def clear(self):
        self.values.clear()


class DataWindow(QDialog):
    """Every signal with its current value; the ones never received are listed once a database is known."""

    def __init__(self, parent=None, symbols=None):
        super().__init__(parent)
        self.setWindowTitle("Data")
        enable_maximize(self)
        self.setMinimumSize(560, 300)
        self.resize(760, 460)
        self.symbols = symbols
        self.signals = SignalValues(symbols)
        self._items = {}
        self._build_ui()
        if symbols is not None:
            symbols.changed.connect(self.rebuild)
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.rebuild()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter signals...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self.rebuild)
        bar.addWidget(self.filter_edit, 1)
        self.received_only_cb = QCheckBox("Received only")
        self.received_only_cb.setChecked(True)
        self.received_only_cb.setToolTip("Hide the signals of the databases that have not arrived yet")
        self.received_only_cb.toggled.connect(self.rebuild)
        bar.addWidget(self.received_only_cb)
        clear = QPushButton("Clear")
        clear.setToolTip("Forget the values received so far")
        clear.clicked.connect(self.clear)
        bar.addWidget(clear)
        export = QPushButton("Export...")
        export.clicked.connect(self._export)
        bar.addWidget(export)
        layout.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        for column, width in ((COL_SIGNAL, 240), (COL_PHYSICAL, 90), (COL_UNIT, 60), (COL_RAW, 90),
                              (COL_AGE, 70), (COL_COUNT, 70)):
            self.tree.setColumnWidth(column, width)
        self.tree.header().setSectionResizeMode(COL_ID, QHeaderView.Stretch)
        layout.addWidget(self.tree, 1)
        self.status = QLabel("")
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)

    # --- the measurement --------------------------------------------------------------------

    def on_frame(self, timestamp, direction, can_id, data, extended=False):
        """Received and sent frames both carry signal values, so both are decoded."""
        for name in self.signals.add(timestamp, can_id, data):
            if name not in self._items:
                self.rebuild()
                return

    def clear(self):
        self.signals.clear()
        self.rebuild()

    # --- the view ---------------------------------------------------------------------------

    def _known_signals(self):
        return self.symbols.signal_names() if self.symbols is not None else []

    def rebuild(self):
        """Build the rows: every signal of the databases, or only the ones that have arrived."""
        text = self.filter_edit.text().strip().lower()
        received = self.signals.values
        names = sorted(received) if self.received_only_cb.isChecked() else sorted(
            set(self._known_signals()) | set(received))
        self.tree.clear()
        self._items = {}
        for name in names:
            if text and text not in name.lower():
                continue
            item = QTreeWidgetItem([name, "", received.get(name, {}).get("unit", "")
                                    or (self.symbols.unit(name) if self.symbols is not None else ""), "", "", "0", ""])
            for column in (COL_PHYSICAL, COL_RAW, COL_AGE, COL_COUNT):
                item.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
            self.tree.addTopLevelItem(item)
            self._items[name] = item
        self.refresh()

    def refresh(self):
        """Put the current values into the rows; only the cells that change are touched."""
        now = time.time()
        rows = self.signals.rows(now)
        for row in rows:
            item = self._items.get(row["name"])
            if item is None:
                continue
            item.setText(COL_PHYSICAL, format_value(row["physical"]))
            item.setText(COL_RAW, format_value(row["raw"]))
            item.setText(COL_UNIT, row["unit"])
            item.setText(COL_AGE, f"{row['age']:.1f}")
            item.setText(COL_COUNT, str(row["count"]))
            item.setText(COL_ID, f"{row['id']:03X}")
        shown, known = len(self._items), len(self._known_signals())
        self.status.setText(f"{len(rows)} signal(s) received, {shown} shown of {known} in the databases")

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export values", "", "CSV files (*.csv);;All files (*.*)")
        if path:
            self.export_csv(path)

    def export_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(HEADERS)
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                writer.writerow([item.text(column) for column in range(len(HEADERS))])
