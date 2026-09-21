"""
Statistics: what is on the bus, counted rather than listed.

One row per identifier with its rate and cycle time, and the totals underneath: frames, bus load, error
frames and the adapter's error state. The rates are worked out over a moving window, so they follow the
bus rather than averaging since the measurement started.
"""
from __future__ import annotations

import time
from collections import deque

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

from canexpert.ui_common import enable_maximize, write_tree_csv

RATE_WINDOW = 3.0        # seconds of history the rates and the bus load are worked out over
REFRESH_MS = 500
COL_ID, COL_NAME, COL_DIR, COL_COUNT, COL_RATE, COL_CYCLE, COL_MIN, COL_MAX, COL_LOAD, COL_DATA = range(10)
HEADERS = ["ID", "Name", "Dir", "Count", "Frames/s", "Cycle (ms)", "Min", "Max", "Bus load", "Last data"]
# A classic CAN frame carries this many bits besides its data: identifier, control and CRC fields,
# the acknowledge slot and the end of frame, plus the three-bit interframe space.
FRAME_OVERHEAD_BITS = {False: 47, True: 67}


def frame_bits(dlc: int, extended: bool = False) -> int:
    """Bits one frame occupies, including the worst case for bit stuffing.

    Stuffing inserts a bit after five equal ones, so at worst a quarter of the stuffable part is added;
    that is what CANoe's "bus load" counts too, and it is an upper bound, not a measurement.
    """
    bits = FRAME_OVERHEAD_BITS[bool(extended)] + 8 * max(0, dlc)
    return bits + (bits - 13) // 4


class Statistics:
    """The counts behind the window, kept apart from it so they can be tested on their own."""

    def __init__(self, window: float = RATE_WINDOW):
        self.window = window
        self.frames = {}        # (id, extended) -> {"count", "direction", "times" deque, "data", "bits"}
        self.total = 0
        self.error_frames = 0
        self.state = "unknown"
        self.first = None
        self.last = None

    def add(self, timestamp, direction, can_id, data, extended=False):
        entry = self.frames.get((can_id, extended))
        if entry is None:
            entry = self.frames[(can_id, extended)] = {"count": 0, "direction": direction,
                                                       "times": deque(), "data": b"", "bits": 0}
        entry["count"] += 1
        entry["direction"] = direction if entry["direction"] == direction else "RX/TX"
        entry["times"].append(float(timestamp))
        entry["data"] = bytes(data)
        entry["bits"] = frame_bits(len(data), extended)
        self.total += 1
        self.first = self.first if self.first is not None else float(timestamp)
        self.last = float(timestamp)

    def reference_time(self) -> float:
        """The moment the rates are worked out against.

        The wall clock while frames are arriving, so a bus going quiet shows its rate fall to zero; the
        newest frame once nothing has arrived for a window, because a replayed file is timed by the
        clock of the recording, not by today's.
        """
        wall = time.time()
        if self.last is None:
            return wall
        return wall if wall - self.last <= self.window else self.last

    def _recent(self, entry, now):
        times = entry["times"]
        while times and now - times[0] > self.window:
            times.popleft()
        return times

    def rows(self, now=None):
        """One row per identifier: (id, extended, count, frames per second, cycle times, data)."""
        now = self.reference_time() if now is None else now
        rows = []
        for (can_id, extended), entry in sorted(self.frames.items()):
            times = self._recent(entry, now or 0.0)
            gaps = [(second - first) * 1000.0 for first, second in zip(times, list(times)[1:])]
            rate = len(times) / self.window if times else 0.0
            rows.append({"id": can_id, "extended": extended, "count": entry["count"],
                         "direction": entry["direction"], "rate": rate,
                         "cycle": sum(gaps) / len(gaps) if gaps else None,
                         "min": min(gaps) if gaps else None, "max": max(gaps) if gaps else None,
                         "load": rate * entry["bits"], "data": entry["data"]})
        return rows

    def bus_load(self, bitrate, now=None) -> float:
        """Percentage of the bus the frames of the last window took up."""
        if not bitrate:
            return 0.0
        return 100.0 * sum(row["load"] for row in self.rows(now)) / float(bitrate)

    def reset(self):
        self.frames.clear()
        self.total = self.error_frames = 0
        self.first = self.last = None


class StatisticsWindow(QDialog):
    """Frames per identifier with their rate and cycle time, and the totals for the bus."""

    def __init__(self, parent=None, symbols=None, bitrate=None):
        super().__init__(parent)
        self.setWindowTitle("Statistics")
        enable_maximize(self)
        self.setMinimumSize(700, 320)
        self.resize(900, 460)
        self.symbols = symbols
        self.bitrate = bitrate or (lambda: 0)   # the configuration's bit rate, for the bus load
        self.statistics = Statistics()
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        reset = QPushButton("Reset")
        reset.setToolTip("Start counting again")
        reset.clicked.connect(self.reset)
        bar.addWidget(reset)
        self.pause_cb = QCheckBox("Freeze")
        self.pause_cb.setToolTip("Stop refreshing the table; the frames are still counted")
        bar.addWidget(self.pause_cb)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by identifier or name...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self.refresh)
        bar.addWidget(self.filter_edit, 1)
        export = QPushButton("Export...")
        export.clicked.connect(self._export)
        bar.addWidget(export)
        layout.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(COL_ID, Qt.AscendingOrder)
        for column, width in ((COL_ID, 90), (COL_NAME, 170), (COL_DIR, 50), (COL_COUNT, 70),
                              (COL_RATE, 80), (COL_CYCLE, 90), (COL_MIN, 70), (COL_MAX, 70), (COL_LOAD, 80)):
            self.tree.setColumnWidth(column, width)
        self.tree.header().setSectionResizeMode(COL_DATA, QHeaderView.Stretch)
        layout.addWidget(self.tree, 1)

        self.totals = QLabel("No frames")
        layout.addWidget(self.totals)

    # --- the measurement --------------------------------------------------------------------

    def on_frame(self, timestamp, direction, can_id, data, extended=False):
        self.statistics.add(timestamp, direction, can_id, data, extended)

    def on_error_frame(self, _timestamp=None):
        self.statistics.error_frames += 1

    def on_bus_status(self, status):
        """The adapter's error state, as the session worker reports it."""
        self.statistics.state = status.get("state", "unknown")
        self.statistics.error_frames = max(self.statistics.error_frames, status.get("error_frames", 0))

    def reset(self):
        self.statistics.reset()
        self.tree.clear()
        self.refresh()

    # --- the view ---------------------------------------------------------------------------

    def _name(self, can_id):
        return self.symbols.name(can_id) if self.symbols is not None else ""

    def refresh(self):
        if self.pause_cb.isChecked():
            return
        text = self.filter_edit.text().strip().lower()
        rows = self.statistics.rows()
        sorting = self.tree.isSortingEnabled()
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for row in rows:
            name = self._name(row["id"])
            identifier = f"{row['id']:08X}x" if row["extended"] else f"{row['id']:03X}"
            if text and text not in identifier.lower() and text not in name.lower():
                continue
            values = [identifier, name, row["direction"], str(row["count"]), f"{row['rate']:.1f}",
                      "" if row["cycle"] is None else f"{row['cycle']:.1f}",
                      "" if row["min"] is None else f"{row['min']:.1f}",
                      "" if row["max"] is None else f"{row['max']:.1f}",
                      f"{100.0 * row['load'] / self.bitrate():.2f} %" if self.bitrate() else "",
                      row["data"].hex(" ").upper()]
            item = QTreeWidgetItem(values)
            for column in (COL_COUNT, COL_RATE, COL_CYCLE, COL_MIN, COL_MAX, COL_LOAD):
                item.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
            self.tree.addTopLevelItem(item)
        self.tree.setSortingEnabled(sorting)
        self._update_totals()

    def _update_totals(self):
        statistics = self.statistics
        load = statistics.bus_load(self.bitrate())
        seconds = (statistics.last - statistics.first) if statistics.first is not None else 0.0
        parts = [f"{statistics.total} frame(s) from {len(statistics.frames)} identifier(s)",
                 f"over {seconds:.1f} s" if seconds else "",
                 f"bus load {load:.1f} %" if self.bitrate() else "bus load: set a bit rate",
                 f"{statistics.error_frames} error frame(s)",
                 f"bus: {statistics.state}"]
        self.totals.setText("     ".join(part for part in parts if part))
        self.totals.setStyleSheet("color: red;" if statistics.state == "bus off" or statistics.error_frames
                                  else "color: gray;")

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export statistics", "", "CSV files (*.csv);;All files (*.*)")
        if path:
            self.export_csv(path)

    def export_csv(self, path):
        """Write the rows now shown to a CSV file."""
        write_tree_csv(path, self.tree, HEADERS)
