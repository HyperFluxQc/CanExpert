"""
TestExpert's Modules tab: the CAN Expert test modules a run takes after its generated tests, and the symbol
databases their frames are decoded with.
"""
from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from canexpert.paths import DBC_DIR, TEST_MODULES_DIR

MODULE_FILTER = "Python test modules (*.py)"
SYMBOL_FILTER = "Symbol databases (*.dbc *.arxml *.kcd *.sym);;All files (*)"


class ModulesTab(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        note = QLabel("CAN Expert's test modules run after the generated tests, each as a group: its setup "
                      "before its first test case, its teardown after its last. Their UDS functions (RDBI, "
                      "DSC...) go through TestExpert's connection. CAN Expert's Tools → Test writes and tries "
                      "them.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addWidget(QLabel("<b>Test modules</b>"))
        self.module_list = self._list()
        layout.addWidget(self.module_list, 2)
        row = QHBoxLayout()
        for text, tip, slot in (("Add...", "Add test module files", lambda: self.add_modules()),
                                ("Remove", "Remove the selected modules", lambda: self._remove(self.module_list)),
                                ("Read again", "Read the files again, after editing them", self.changed.emit)):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        layout.addWidget(QLabel("<b>Symbol databases</b> — the frames' signals, for wait_for_signal()"))
        self.symbol_list = self._list()
        layout.addWidget(self.symbol_list, 1)
        row = QHBoxLayout()
        for text, tip, slot in (("Add...", "Add DBC or other symbol database files", lambda: self.add_symbols()),
                                ("Remove", "Remove the selected databases", lambda: self._remove(self.symbol_list))):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)

    @staticmethod
    def _list():
        widget = QListWidget()
        widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        return widget

    # --- the paths ---------------------------------------------------------------------------------------

    def set_paths(self, modules, symbols):
        """Show these files, without a change."""
        for widget, paths in ((self.module_list, modules), (self.symbol_list, symbols)):
            widget.clear()
            for path in paths:
                self._item(widget, path)

    def modules(self) -> list[str]:
        return self._paths(self.module_list)

    def symbols(self) -> list[str]:
        return self._paths(self.symbol_list)

    @staticmethod
    def _paths(widget) -> list[str]:
        return [widget.item(row).data(Qt.UserRole) for row in range(widget.count())]

    @staticmethod
    def _item(widget, path):
        item = QListWidgetItem(Path(path).name)
        item.setData(Qt.UserRole, str(path))
        item.setToolTip(str(path))
        widget.addItem(item)
        return item

    def add_modules(self, paths=None):
        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(self, "Test modules", str(TEST_MODULES_DIR), MODULE_FILTER)
        self._add(self.module_list, paths)

    def add_symbols(self, paths=None):
        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(self, "Symbol databases", str(DBC_DIR), SYMBOL_FILTER)
        self._add(self.symbol_list, paths)

    def _add(self, widget, paths):
        known = set(self._paths(widget))
        added = [str(Path(path).resolve()) for path in paths or () if str(Path(path).resolve()) not in known]
        for path in dict.fromkeys(added):
            self._item(widget, path)
        if added:
            self.changed.emit()

    def _remove(self, widget):
        rows = sorted((widget.row(item) for item in widget.selectedItems()), reverse=True)
        for row in rows:
            widget.takeItem(row)
        if rows:
            self.changed.emit()

    def show_loaded(self, loaded):
        """What each module file holds (modules.LoadedModule), or why it could not be read."""
        by_path = {str(entry.path): entry for entry in loaded}
        for row in range(self.module_list.count()):
            item = self.module_list.item(row)
            entry = by_path.get(item.data(Qt.UserRole))
            name = Path(item.data(Qt.UserRole)).name
            if entry is None:
                item.setText(name)
            elif entry.module is None:
                item.setText(f"{name} — not read: {entry.error}")
            else:
                count = len(entry.module.cases)
                item.setText(f"{name} — {entry.title} ({count} test case{'s' if count != 1 else ''})")
