"""
System variables: named values shared by the panel script, the windows and the user - CANoe's glue.

A system variable is called Namespace::Name ("Engine::TargetSpeed"). A script sets and reads them
(api.sysvar) and reacts when one changes (@on_sysvar); the System Variables window lists them with
their values, where the user can type a new one; the CAN Logger can plot them next to bus signals.
So a value the script works out, or a setpoint the user types, is visible and plottable without a
panel control or a DBC signal for it - and without touching the panel database.

Definitions (name, type, initial value, unit, comment) are kept in the settings and can be saved to
and read from a JSON file; values start again from their initial ones with every measurement.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
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

SETTINGS_KEY = "system_variables"      # settings: the definitions, as JSON
KINDS = ("float", "int", "bool", "text")
COL_NAME, COL_VALUE, COL_KIND, COL_UNIT, COL_COMMENT = range(5)
HEADERS = ["Name", "Value", "Type", "Unit", "Comment"]


@dataclass
class SysVarDefinition:
    name: str
    kind: str = "float"
    initial: object = 0.0
    unit: str = ""
    comment: str = ""

    @classmethod
    def from_dict(cls, values) -> "SysVarDefinition":
        names = {item.name for item in fields(cls)}
        definition = cls(**{name: value for name, value in dict(values).items() if name in names})
        check_name(definition.name)
        if definition.kind not in KINDS:
            raise ValueError(f"{definition.name}: the type must be one of {', '.join(KINDS)}")
        definition.initial = convert(definition.kind, definition.initial)
        return definition


def check_name(name: str) -> str:
    """Namespace::Name, each part letters, digits and underscores. ValueError otherwise."""
    parts = str(name).split("::")
    if len(parts) < 2 or not all(part and part.replace("_", "a").isalnum() for part in parts):
        raise ValueError(f"{name!r}: a system variable is named Namespace::Name, e.g. Engine::TargetSpeed")
    return name


def convert(kind: str, value):
    """value as the type says. ValueError when it cannot be."""
    if kind == "text":
        return "" if value is None else str(value)
    if kind == "bool":
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("1", "true", "on", "yes"):
                return True
            if text in ("0", "false", "off", "no", ""):
                return False
            raise ValueError(f"{value!r} is not true or false")
        return bool(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a number") from None
    return int(number) if kind == "int" else number


def kind_of(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "text"


class SystemVariables(QObject):
    """The variables and their current values. Safe to use from the script thread; changed() is emitted
    in the thread that made the change, so GUI receivers get it queued."""
    changed = pyqtSignal(str, object, float)       # name, value, when (Unix time)
    definitions_changed = pyqtSignal()

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._lock = threading.Lock()
        self._definitions = {}                     # name -> SysVarDefinition
        self._values = {}                          # name -> (value, time)
        self._load()

    # --- definitions ---------------------------------------------------------------------------

    def _load(self):
        if self._settings is None:
            return
        try:
            stored = json.loads(self._settings.value(SETTINGS_KEY, "[]", type=str) or "[]")
            for values in stored:
                definition = SysVarDefinition.from_dict(values)
                self._definitions[definition.name] = definition
        except (TypeError, ValueError):
            pass                                   # a damaged entry leaves no variables, not a crash

    def _save(self):
        if self._settings is not None:
            self._settings.setValue(SETTINGS_KEY, json.dumps([asdict(item) for item in self.definitions()]))

    def definitions(self) -> list[SysVarDefinition]:
        with self._lock:
            return sorted(self._definitions.values(), key=lambda item: item.name.lower())

    def definition(self, name: str) -> SysVarDefinition | None:
        with self._lock:
            return self._definitions.get(name)

    def define(self, definition: SysVarDefinition):
        definition = SysVarDefinition.from_dict(asdict(definition))
        with self._lock:
            self._definitions[definition.name] = definition
            self._values.setdefault(definition.name, (definition.initial, time.time()))
        self._save()
        self.definitions_changed.emit()

    def remove(self, name: str):
        with self._lock:
            self._definitions.pop(name, None)
            self._values.pop(name, None)
        self._save()
        self.definitions_changed.emit()

    def save_file(self, path):
        Path(path).write_text(json.dumps([asdict(item) for item in self.definitions()], indent=2), encoding="utf-8")

    def load_file(self, path) -> int:
        """Add the definitions of a file (replacing ones of the same name). Returns how many."""
        loaded = [SysVarDefinition.from_dict(values) for values in json.loads(Path(path).read_text(encoding="utf-8"))]
        with self._lock:
            for definition in loaded:
                self._definitions[definition.name] = definition
                self._values[definition.name] = (definition.initial, time.time())
        self._save()
        self.definitions_changed.emit()
        return len(loaded)

    # --- values ------------------------------------------------------------------------------------

    def names(self) -> list[str]:
        with self._lock:
            return sorted(set(self._definitions) | set(self._values), key=str.lower)

    def get(self, name: str, default=None):
        with self._lock:
            if name in self._values:
                return self._values[name][0]
            definition = self._definitions.get(name)
            return definition.initial if definition is not None else default

    def when(self, name: str) -> float | None:
        with self._lock:
            entry = self._values.get(name)
            return entry[1] if entry else None

    def set(self, name: str, value, timestamp: float | None = None) -> bool:
        """Give a variable a value; one that was never defined is defined by it. Returns whether it changed.
        ValueError when the value does not fit the variable's type."""
        check_name(name)
        timestamp = time.time() if timestamp is None else float(timestamp)
        created = False
        with self._lock:
            definition = self._definitions.get(name)
            if definition is None:
                definition = SysVarDefinition(name, kind_of(value), convert(kind_of(value), value))
                self._definitions[name] = definition
                created = True
            value = convert(definition.kind, value)
            previous = self._values.get(name, (object(), 0.0))[0]
            self._values[name] = (value, timestamp)
        if created:
            self._save()
            self.definitions_changed.emit()
        if previous != value:
            self.changed.emit(name, value, timestamp)
            return True
        return False

    def reset(self):
        """A new measurement: every variable back to its initial value."""
        now = time.time()
        with self._lock:
            self._values = {name: (definition.initial, now) for name, definition in self._definitions.items()}
            changed = list(self._values.items())
        for name, (value, when) in changed:
            self.changed.emit(name, value, when)


# --- the window ------------------------------------------------------------------------------------

class DefinitionDialog(QDialog):
    """A new system variable: name, type, initial value, unit and comment."""

    def __init__(self, parent=None, definition: SysVarDefinition | None = None):
        super().__init__(parent)
        self.setWindowTitle("System variable")
        self.definition = definition
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(definition.name if definition else "")
        self.name_edit.setPlaceholderText("Namespace::Name, e.g. Engine::TargetSpeed")
        self.kind_combo = QComboBox()
        self.kind_combo.addItems(KINDS)
        self.initial_edit = QLineEdit(str(definition.initial) if definition else "0")
        self.unit_edit = QLineEdit(definition.unit if definition else "")
        self.comment_edit = QLineEdit(definition.comment if definition else "")
        if definition:
            self.kind_combo.setCurrentText(definition.kind)
            self.name_edit.setReadOnly(True)
        for label, widget in (("Name:", self.name_edit), ("Type:", self.kind_combo),
                              ("Initial value:", self.initial_edit), ("Unit:", self.unit_edit),
                              ("Comment:", self.comment_edit)):
            form.addRow(label, widget)
        layout.addLayout(form)
        self.message = QLabel("")
        self.message.setStyleSheet("color: red;")
        layout.addWidget(self.message)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _accept(self):
        try:
            self.definition = SysVarDefinition.from_dict({
                "name": self.name_edit.text().strip(), "kind": self.kind_combo.currentText(),
                "initial": self.initial_edit.text(), "unit": self.unit_edit.text().strip(),
                "comment": self.comment_edit.text().strip()})
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        self.accept()


def value_text(value) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


class SystemVariablesWindow(QDialog):
    """Every system variable with its value; type a value to set it."""

    def __init__(self, variables: SystemVariables, parent=None):
        super().__init__(parent)
        self.setWindowTitle("System variables")
        enable_maximize(self)
        self.setMinimumSize(560, 280)
        self.resize(760, 420)
        self.variables = variables
        self._items = {}
        self._updating = False
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        for text, slot, tip in (("New...", self.new_variable, "Define a system variable"),
                                ("Edit...", self.edit_variable, "Change the type, initial value, unit or comment"),
                                ("Remove", self.remove_variable, "Forget the selected variable"),
                                ("Load...", self.load_file, "Add the variables of a JSON file"),
                                ("Save...", self.save_file, "Keep the definitions in a JSON file")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            bar.addWidget(button)
        bar.addStretch()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        bar.addWidget(self.filter_edit, 1)
        layout.addLayout(bar)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(COL_NAME, 240)
        self.tree.setColumnWidth(COL_VALUE, 110)
        self.tree.header().setSectionResizeMode(COL_COMMENT, QHeaderView.Stretch)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.tree, 1)
        self.status = QLabel("")
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)
        variables.changed.connect(self._on_changed)
        variables.definitions_changed.connect(self.rebuild)
        self.rebuild()

    def rebuild(self):
        self._updating = True
        try:
            self.tree.clear()
            self._items = {}
            for definition in self.variables.definitions():
                item = QTreeWidgetItem([definition.name, value_text(self.variables.get(definition.name)),
                                        definition.kind, definition.unit, definition.comment])
                item.setFlags(item.flags() | Qt.ItemIsEditable)
                self.tree.addTopLevelItem(item)
                self._items[definition.name] = item
        finally:
            self._updating = False
        self._apply_filter()
        self.status.setText(f"{len(self._items)} variable(s). Double-click a value to change it.")

    def _apply_filter(self, *_):
        text = self.filter_edit.text().strip().lower()
        for name, item in self._items.items():
            item.setHidden(bool(text) and text not in name.lower())

    def _on_changed(self, name, value, _when):
        item = self._items.get(name)
        if item is None:
            return
        self._updating = True
        try:
            item.setText(COL_VALUE, value_text(value))
        finally:
            self._updating = False

    def _on_double_click(self, item, column):
        if column == COL_VALUE:
            self.tree.editItem(item, COL_VALUE)

    def _on_item_changed(self, item, column):
        if self._updating or column != COL_VALUE:
            return
        name = item.text(COL_NAME)
        try:
            self.variables.set(name, item.text(COL_VALUE))
            self.status.setText(f"{name} = {value_text(self.variables.get(name))}")
        except ValueError as exc:
            self.status.setText(f"{name}: {exc}")
            self._on_changed(name, self.variables.get(name), 0.0)

    def _selected(self):
        item = self.tree.currentItem()
        return item.text(COL_NAME) if item is not None else None

    def new_variable(self):
        dialog = DefinitionDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            self.variables.define(dialog.definition)
        return dialog

    def edit_variable(self):
        name = self._selected()
        if name is None:
            return None
        dialog = DefinitionDialog(self, self.variables.definition(name))
        if dialog.exec_() == QDialog.Accepted:
            self.variables.define(dialog.definition)
        return dialog

    def remove_variable(self):
        name = self._selected()
        if name is not None:
            self.variables.remove(name)

    def save_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save system variables", "", "JSON files (*.json)")
        if path:
            self.variables.save_file(path)
            self.status.setText(f"Saved to {Path(path).name}")

    def load_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load system variables", "", "JSON files (*.json)")
        if not path:
            return
        try:
            count = self.variables.load_file(path)
        except (OSError, ValueError, TypeError) as exc:
            self.status.setText(f"Cannot read {Path(path).name}: {exc}")
            return
        self.status.setText(f"Loaded {count} variable(s) from {Path(path).name}")
