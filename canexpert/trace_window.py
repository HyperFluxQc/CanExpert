"""
Trace window: every frame of the measurement as a row, decoded with the symbol databases.

Columns are time, direction, identifier, symbolic message name, length and data; a row that a DBC
decodes can be expanded to its signals. The view is fed from a buffer on a timer, because a busy bus
delivers far more frames than a widget can repaint, and keeps at most MAX_ROWS frames.
"""
from __future__ import annotations

import csv
from collections import deque

from PyQt5.QtCore import QEvent, QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from canexpert.clock import absolute_text
from canexpert.frame_filter import DIRECTIONS, FILTER_MODES, FrameFilter, parse_filter
from canexpert.uds.observer import assemble
from canexpert.ui_common import enable_maximize, is_dark_theme, line_icon, style_toggle

MAX_ROWS = 20000            # frames kept; the oldest are dropped
FLUSH_INTERVAL_MS = 80      # how often buffered frames reach the view
TIME_MODES = ("Absolute", "Relative", "Delta")
COL_TIME, COL_DIR, COL_ID, COL_NAME, COL_DLC, COL_DATA = range(6)

# Symbols of the small tool buttons, drawn in a 24 x 24 box (see ui_common.line_icon).
TOOL_ICONS = {
    "clear": '<path d="M5 7h14M10 4h4"/><path d="M7 7l1 13h8l1-13"/><path d="M10.5 10.5v6M13.5 10.5v6"/>',
    "pause": '<path d="M9.5 5v14M14.5 5v14" stroke-width="2.6"/>',
    "play": '<path d="M8 5l11 7-11 7z"/>',
    "follow": '<path d="M3 12h12"/><path d="M11 7l5 5-5 5"/><path d="M20 4v16"/>',
    "colour": '<path d="M12 3a9 9 0 1 0 0 18c1.4 0 2-1 2-1.8 0-1.6-1.6-1.8-1.6-3 0-.9.8-1.6 1.8-1.6H16a5 5 0 0 0 5-5"/>'
              '<circle cx="7.5" cy="12" r="1.2" fill="currentColor"/><circle cx="9.5" cy="8" r="1.2" fill="currentColor"/>'
              '<circle cx="14" cy="7" r="1.2" fill="currentColor"/>',
    "transport": '<path d="M4 7h10a3 3 0 0 1 0 6H8a3 3 0 0 0 0 6h12"/><path d="M17 4l3 3-3 3"/>'
                 '<path d="M7 16l-3 3 3 3"/>',
}
# One colour per identifier, picked by the identifier itself so a message keeps its colour.
_ID_COLORS_LIGHT = ["#1f77b4", "#b8410e", "#2e7d32", "#8a6d00", "#6a1b9a", "#00707f", "#a3145c", "#3f51b5"]
_ID_COLORS_DARK = ["#5eb3f6", "#ff9d6b", "#7fd18a", "#ffd43b", "#cc92e2", "#4fd2e0", "#ff8ab5", "#9fa8ff"]


class TraceWindow(QDialog):
    """CANoe-style trace: frames as they arrive, symbolic where a database describes them."""

    def __init__(self, parent=None, symbols=None, clock=None):
        super().__init__(parent)
        self.setWindowTitle("Trace")
        self.clock = clock                     # the measurement's (clock.py); Relative counts from its start
        enable_maximize(self)
        self.setMinimumSize(760, 380)
        self.resize(1100, 620)
        self.symbols = symbols
        self.frames = deque(maxlen=MAX_ROWS)   # (timestamp, direction, can_id, data, extended)
        self.diagnostic_ids = set()            # the identifiers the transport view assembles
        self.address_byte = None               # ISO-TP extended addressing, when the configuration uses it
        self._pending = []
        self._filter = ([], [])
        self._tool_buttons = {}
        self._build_ui()
        if symbols is not None:
            symbols.changed.connect(self.rebuild)
        self._timer = QTimer(self)
        self._timer.setInterval(FLUSH_INTERVAL_MS)
        self._timer.timeout.connect(self.flush)
        self._timer.start()

    # --- UI -----------------------------------------------------------------------------

    def _tool_button(self, name, tip, checkable=False, checked=False, clicked=None, toggled=None):
        button = QToolButton()
        button.setAutoRaise(True)
        button.setIconSize(QSize(18, 18))
        button.setToolTip(tip)
        button.setAccessibleName(tip.split(":")[0])
        button.setCheckable(checkable)
        button.setChecked(checked)
        if clicked is not None:
            button.clicked.connect(clicked)
        if toggled is not None:
            button.toggled.connect(toggled)
        self._tool_buttons[name] = button
        return style_toggle(button)

    @staticmethod
    def _separator():
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _refresh_tool_icons(self):
        colour = self.palette().color(QPalette.WindowText)
        for name, button in self._tool_buttons.items():
            symbol = "play" if name == "pause" and button.isChecked() else name
            body = TOOL_ICONS[symbol].replace('fill="currentColor"', f'fill="{colour.name()}"')
            button.setIcon(line_icon(body, colour))
            style_toggle(button)          # the style sheet's palette(...) is resolved when it is set

    def _build_ui(self):
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        bar.addWidget(self._tool_button("clear", "Clear: discard the recorded frames", clicked=self.clear))
        bar.addWidget(self._separator())
        self.pause_btn = self._tool_button("pause", "Pause: freeze the view; frames keep being recorded",
                                           checkable=True, toggled=self._on_pause_toggled)
        bar.addWidget(self.pause_btn)
        self.follow_btn = self._tool_button("follow", "Follow: keep the newest frame in view",
                                            checkable=True, checked=True)
        bar.addWidget(self.follow_btn)
        self.colour_btn = self._tool_button("colour", "Colour: give each identifier its own colour",
                                            checkable=True, checked=True, toggled=lambda _: self.rebuild())
        bar.addWidget(self.colour_btn)
        self.transport_btn = self._tool_button("transport", "Transport: show the diagnostic messages the "
                                               "frames carry, not the frames themselves", checkable=True,
                                               toggled=lambda _: self.rebuild())
        bar.addWidget(self.transport_btn)
        bar.addWidget(self._separator())
        bar.addWidget(QLabel("Time:"))
        self.time_combo = QComboBox()
        self.time_combo.addItems(TIME_MODES)
        self.time_combo.setToolTip("Absolute clock time, seconds since the first frame, or since the frame above")
        self.time_combo.currentTextChanged.connect(lambda _: self.rebuild())
        bar.addWidget(self.time_combo)
        bar.addWidget(self._separator())
        self.filter_mode = QComboBox()
        self.filter_mode.addItems(FILTER_MODES)
        self.filter_mode.setToolTip("Pass shows only what matches; Stop hides what matches")
        self.filter_mode.currentTextChanged.connect(lambda _: self.rebuild())
        bar.addWidget(self.filter_mode)
        self.direction_combo = QComboBox()
        self.direction_combo.addItems(DIRECTIONS)
        self.direction_combo.setToolTip("Received frames, frames CAN Expert sent, or both")
        self.direction_combo.currentTextChanged.connect(lambda _: self.rebuild())
        bar.addWidget(self.direction_combo)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter: 7E0, 300-3FF, EngineData")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_filter_changed)
        bar.addWidget(self.filter_edit, 1)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Find...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.returnPressed.connect(self.find_next)
        bar.addWidget(self.search_edit, 1)
        find_btn = QPushButton("Find next")
        find_btn.clicked.connect(self.find_next)
        bar.addWidget(find_btn)
        export_btn = QPushButton("Export...")
        export_btn.setToolTip("Write the rows now shown to a CSV file")
        export_btn.clicked.connect(self._export)
        bar.addWidget(export_btn)
        layout.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Time", "Dir", "ID", "Name", "DLC", "Data"])
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setColumnWidth(COL_TIME, 120)
        self.tree.setColumnWidth(COL_DIR, 40)
        self.tree.setColumnWidth(COL_ID, 90)
        self.tree.setColumnWidth(COL_NAME, 200)
        self.tree.setColumnWidth(COL_DLC, 44)
        self.tree.itemExpanded.connect(self._fill_signals)
        layout.addWidget(self.tree, 1)

        status = QHBoxLayout()
        self.status = QLabel("No frames")
        self.status.setStyleSheet("color: gray;")
        status.addWidget(self.status)
        status.addStretch()
        self.offline_label = QLabel("")
        self.offline_label.setStyleSheet("color: gray;")
        status.addWidget(self.offline_label)
        layout.addLayout(status)
        self._refresh_tool_icons()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange, QEvent.ApplicationPaletteChange) and self._tool_buttons:
            self._refresh_tool_icons()
            QTimer.singleShot(0, self.rebuild)   # the row colours follow the theme

    # --- data ---------------------------------------------------------------------------

    def add_frame(self, timestamp, direction, can_id, data, extended=False):
        """Record one frame; it reaches the view at the next flush."""
        self._pending.append((float(timestamp), str(direction), int(can_id), bytes(data), bool(extended)))

    on_frame = add_frame        # what the main window hands every window of the measurement

    def set_source(self, text: str):
        """Name what is being traced, e.g. a replayed file."""
        self.offline_label.setText(text)

    def clear(self):
        self.frames.clear()
        self._pending.clear()
        self.tree.clear()
        self._update_status()

    def flush(self):
        """Move buffered frames into the view (called by the timer, and directly by the tests)."""
        if not self._pending:
            return
        pending, self._pending = self._pending, []
        dropped = max(0, len(self.frames) + len(pending) - MAX_ROWS)
        self.frames.extend(pending)
        if self.pause_btn.isChecked():
            return
        if dropped or self.transport_btn.isChecked():   # the view is not a row per frame any more
            self.rebuild()
            return
        previous = self.frames[len(self.frames) - len(pending) - 1] if len(self.frames) > len(pending) else None
        for frame in pending:
            if self._passes(frame):
                self._append_row(frame, previous)
            previous = frame
        if self.follow_btn.isChecked():
            self.tree.scrollToBottom()
        self._update_status()

    def set_diagnostic_ids(self, identifiers, address_byte=None):
        """Which identifiers the transport view assembles, and the extended addressing byte if any."""
        self.diagnostic_ids = {int(identifier) for identifier in identifiers}
        self.address_byte = address_byte
        if self.transport_btn.isChecked():
            self.rebuild()

    def rebuild(self):
        """Rebuild every row: the filter, the time mode, the view or the databases changed."""
        self.tree.clear()
        if self.transport_btn.isChecked():
            self._rebuild_transport()
        else:
            previous = None
            for frame in self.frames:
                if self._passes(frame):
                    self._append_row(frame, previous)
                previous = frame
        if self.follow_btn.isChecked():
            self.tree.scrollToBottom()
        self._update_status()

    def transport_messages(self):
        """The diagnostic messages the recorded frames carry."""
        diagnostic = [frame for frame in self.frames if frame[2] in self.diagnostic_ids]
        return assemble(diagnostic, self.address_byte)

    def _rebuild_transport(self):
        """A row per diagnostic message, opening into the frames that carried it."""
        if not self.diagnostic_ids:
            item = QTreeWidgetItem(["", "", "", "No diagnostic identifiers yet - connect, or set the "
                                    "request and response IDs in the configuration", "", ""])
            self.tree.addTopLevelItem(item)
            return
        previous = None
        for message in self.transport_messages():
            if not self._passes((message.start, message.direction, message.can_id, message.payload,
                                 message.extended)):
                continue
            frame = (message.start, message.direction, message.can_id, message.payload, message.extended)
            item = QTreeWidgetItem([self._time_text(frame, previous), message.direction,
                                    f"{message.can_id:08X}x" if message.extended else f"{message.can_id:03X}",
                                    message.summary(), str(len(message.payload)),
                                    message.payload.hex(" ").upper()])
            if self.colour_btn.isChecked():
                item.setForeground(COL_ID, self._colour(message.can_id))
            if not message.complete:
                item.setForeground(COL_NAME, QColor("#c0392b"))
            for timestamp, label, data in message.frames:
                item.addChild(QTreeWidgetItem([f"{timestamp - message.start:+.6f}", "", "", label,
                                               str(len(data)), bytes(data).hex(" ").upper()]))
            self.tree.addTopLevelItem(item)
            previous = frame

    def _on_filter_changed(self, text):
        self._filter = parse_filter(text)
        self.rebuild()

    def _on_pause_toggled(self, paused):
        self.pause_btn.setToolTip("Resume: show the frames recorded meanwhile" if paused
                                  else "Pause: freeze the view; frames keep being recorded")
        self._refresh_tool_icons()
        if not paused:
            self.rebuild()

    def _passes(self, frame) -> bool:
        ranges, names = self._filter
        rule = FrameFilter(ranges, names, self.filter_mode.currentText(), self.direction_combo.currentText())
        return rule.empty or rule.passes(frame[1], frame[2], self._name(frame[2]))

    # --- rows ---------------------------------------------------------------------------

    def _name(self, can_id: int) -> str:
        return self.symbols.name(can_id) if self.symbols is not None else ""

    def _time_text(self, frame, previous) -> str:
        mode = self.time_combo.currentText()
        if mode == "Absolute":
            return absolute_text(frame[0])
        if mode == "Delta":
            return f"{frame[0] - previous[0]:.6f}" if previous is not None else "0.000000"
        if self.clock is not None and self.clock.start is not None and self.clock.start <= frame[0]:
            return f"{frame[0] - self.clock.start:.6f}"
        first = self.frames[0][0] if self.frames else frame[0]
        return f"{frame[0] - first:.6f}"

    def _append_row(self, frame, previous):
        timestamp, direction, can_id, data, extended = frame
        name = self._name(can_id)
        item = QTreeWidgetItem([self._time_text(frame, previous), direction,
                                f"{can_id:08X}x" if extended else f"{can_id:03X}",
                                name, str(len(data)), data.hex(" ").upper()])
        item.setData(COL_TIME, Qt.UserRole, frame)
        for column in (COL_ID, COL_DATA):
            item.setTextAlignment(column, Qt.AlignLeft | Qt.AlignVCenter)
        if self.colour_btn.isChecked():
            item.setForeground(COL_ID, self._colour(can_id))
        if name:
            # The signals are built only when the row is opened: decoding every frame would cost far
            # more than the rows a user ever expands.
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        self.tree.addTopLevelItem(item)

    def _colour(self, can_id: int) -> QColor:
        colours = _ID_COLORS_DARK if is_dark_theme(self) else _ID_COLORS_LIGHT
        return QColor(colours[can_id % len(colours)])

    def _fill_signals(self, item):
        if item.childCount() or item.parent() is not None or self.symbols is None:
            return
        frame = item.data(COL_TIME, Qt.UserRole)
        if frame is None:
            return
        for signal, value in self.symbols.decode(frame[2], frame[3]).items():
            unit = self.symbols.unit(f"{self._name(frame[2])}.{signal}")
            text = f"{value:.6g}" if isinstance(value, float) else str(value)
            child = QTreeWidgetItem(["", "", "", signal, "", f"{text} {unit}".strip()])
            item.addChild(child)
        if not item.childCount():
            item.setChildIndicatorPolicy(QTreeWidgetItem.DontShowIndicator)

    def _update_status(self):
        shown, total = self.tree.topLevelItemCount(), len(self.frames)
        if self.transport_btn.isChecked():
            messages = self.transport_messages()
            unfinished = sum(1 for message in messages if not message.complete)
            self.status.setText(f"{shown} of {len(messages)} diagnostic message(s) from {total} frame(s)"
                                + (f", {unfinished} unfinished" if unfinished else ""))
            return
        self.status.setText(f"{shown} of {total} frame(s)" if shown != total else f"{total} frame(s)")

    # --- find and export ------------------------------------------------------------------

    def find_next(self):
        """Select the next row containing the search text, wrapping at the end."""
        text = self.search_edit.text().strip().lower()
        if not text:
            return None
        count = self.tree.topLevelItemCount()
        current = self.tree.indexOfTopLevelItem(self.tree.currentItem()) if self.tree.currentItem() else -1
        for offset in range(1, count + 1):
            item = self.tree.topLevelItem((current + offset) % count) if count else None
            if item is None:
                break
            row = " ".join(item.text(column) for column in range(self.tree.columnCount())).lower()
            if text in row:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                self.follow_btn.setChecked(False)   # a found row should stay in view
                self.status.setText(f"Found '{text}'")
                return item
        self.status.setText(f"'{text}' not found")
        return None

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export trace", "", "CSV files (*.csv);;All files (*.*)")
        if path:
            self.export_csv(path)

    def export_csv(self, path):
        """Write the rows now shown (the filter applies) to a CSV file."""
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Time", "Direction", "ID", "Name", "DLC", "Data"])
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                writer.writerow([item.text(column) for column in range(self.tree.columnCount())])
