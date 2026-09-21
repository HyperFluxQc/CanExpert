"""
The Write window: what the panel script says, apart from what the application says.

CANoe keeps CAPL's write() output in a window of its own; here the script's api.log / api.write /
api.warn and its errors go to the Write window, each line with the measurement's time and a colour
for its level, while the Debug log stays the application's. Script errors are shown in both, since
they are also the application's business.

Its second tab watches the script's global variables - numbers, text, lists - refreshed while it is
open, which is often all the debugging a panel script needs.
"""
from __future__ import annotations

import html
import types
from collections import deque

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canexpert.clock import absolute_text
from canexpert.ui_common import enable_maximize

LEVELS = ("info", "warning", "error")
LEVEL_FILTERS = ("Everything", "Warnings and errors", "Errors only")
COLOURS = {"info": None, "warning": "#b8860b", "error": "#d62728"}
MAX_LINES = 5000
WATCH_MS = 500
VALUE_WIDTH = 200            # characters of a value shown before it is cut


def watch_values(namespace, hidden=()) -> list[tuple[str, str, str]]:
    """(name, type, value) of a script's own global variables: not modules, functions, classes or the
    names CAN Expert puts there, and not _private ones."""
    rows = []
    for name, value in sorted(dict(namespace or {}).items()):
        if name.startswith("_") or name in hidden:
            continue
        if isinstance(value, (types.ModuleType, types.FunctionType, types.BuiltinFunctionType, type,
                              types.MethodType)) or callable(value):
            continue
        try:
            text = repr(value)
        except Exception as exc:                     # a script object whose repr fails
            text = f"<{type(exc).__name__}>"
        rows.append((name, type(value).__name__,
                     text if len(text) <= VALUE_WIDTH else text[:VALUE_WIDTH - 1] + "…"))
    return rows


class WriteWindow(QDialog):
    """Script output with its level and time, and a watch on the script's variables."""

    def __init__(self, parent=None, clock=None, watch=None):
        super().__init__(parent)
        self.setWindowTitle("Write")
        enable_maximize(self)
        self.setMinimumSize(560, 260)
        self.resize(820, 420)
        self.clock = clock
        self.watch = watch or (lambda: ({}, ()))    # watch() -> (script globals, names to leave out)
        self.entries = deque(maxlen=MAX_LINES)      # (timestamp, level, text)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        output = QWidget()
        output_layout = QVBoxLayout(output)
        bar = QHBoxLayout()
        self.level_combo = QComboBox()
        self.level_combo.addItems(LEVEL_FILTERS)
        self.level_combo.currentTextChanged.connect(lambda _: self.rebuild())
        bar.addWidget(self.level_combo)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Find in the output...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(lambda _: self.rebuild())
        bar.addWidget(self.filter_edit, 1)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        bar.addWidget(clear)
        save = QPushButton("Save...")
        save.clicked.connect(self._save)
        bar.addWidget(save)
        output_layout.addLayout(bar)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(MAX_LINES)
        self.text.setPlaceholderText("api.log(...), api.write(...), api.warn(...) and script errors appear here.")
        output_layout.addWidget(self.text, 1)
        self.tabs.addTab(output, "Output")

        variables = QWidget()
        variables_layout = QVBoxLayout(variables)
        note_row = QHBoxLayout()
        self.watch_cb = QCheckBox("Refresh")
        self.watch_cb.setChecked(True)
        self.watch_cb.setToolTip("Read the script's variables twice a second")
        note_row.addWidget(self.watch_cb)
        note = QLabel("The panel script's global variables, as it runs.")
        note.setStyleSheet("color: gray;")
        note_row.addWidget(note, 1)
        variables_layout.addLayout(note_row)
        self.watch_tree = QTreeWidget()
        self.watch_tree.setHeaderLabels(["Variable", "Type", "Value"])
        self.watch_tree.setRootIsDecorated(False)
        self.watch_tree.setColumnWidth(0, 180)
        self.watch_tree.setColumnWidth(1, 80)
        self.watch_tree.header().setSectionResizeMode(2, QHeaderView.Stretch)
        variables_layout.addWidget(self.watch_tree, 1)
        self.tabs.addTab(variables, "Script variables")

        self._timer = QTimer(self)
        self._timer.setInterval(WATCH_MS)
        self._timer.timeout.connect(self.refresh_watch)
        self._timer.start()

    # --- output ---------------------------------------------------------------------------------

    def _shown(self, level, text) -> bool:
        choice = self.level_combo.currentText()
        if choice == LEVEL_FILTERS[1] and level == "info":
            return False
        if choice == LEVEL_FILTERS[2] and level != "error":
            return False
        needle = self.filter_edit.text().strip().lower()
        return not needle or needle in text.lower()

    def _time(self, timestamp) -> str:
        display = getattr(self.parent(), "time_display", "Absolute")
        return self.clock.text(timestamp, display) if self.clock is not None else absolute_text(timestamp)

    def _html(self, timestamp, level, text) -> str:
        # white-space: pre, or HTML would fold the spacing - and whatever a script lines up - into one space.
        line = html.escape(f"{self._time(timestamp)}  {text}")
        colour = COLOURS.get(level)
        style = f"white-space:pre; color:{colour};" if colour else "white-space:pre;"
        return f'<span style="{style}">{line}</span>'

    def add(self, timestamp: float, level: str, text: str):
        self.entries.append((timestamp, level, text))
        if self._shown(level, text):
            self.text.appendHtml(self._html(timestamp, level, text))

    def rebuild(self):
        self.text.clear()
        shown = [self._html(*entry) for entry in self.entries if self._shown(entry[1], entry[2])]
        for line in shown:
            self.text.appendHtml(line)

    def lines(self) -> list[str]:
        return self.text.toPlainText().splitlines()

    def clear(self):
        self.entries.clear()
        self.text.clear()

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save the output", "", "Text files (*.txt);;All files (*.*)")
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                for timestamp, level, text in self.entries:
                    handle.write(f"{self._time(timestamp)}\t{level}\t{text}\n")

    # --- the watch ---------------------------------------------------------------------------------

    def refresh_watch(self):
        if not self.watch_cb.isChecked() or not self.isVisible() or self.tabs.currentIndex() != 1:
            return
        self.fill_watch()

    def fill_watch(self):
        namespace, hidden = self.watch()
        rows = watch_values(namespace, hidden)
        existing = {self.watch_tree.topLevelItem(index).text(0): self.watch_tree.topLevelItem(index)
                    for index in range(self.watch_tree.topLevelItemCount())}
        if list(existing) != [row[0] for row in rows]:
            self.watch_tree.clear()
            existing = {}
            for row in rows:
                existing[row[0]] = QTreeWidgetItem(self.watch_tree, list(row))
        for name, kind, value in rows:
            item = existing[name]
            if item.text(1) != kind:
                item.setText(1, kind)
            if item.text(2) != value:
                item.setText(2, value)
        return rows
