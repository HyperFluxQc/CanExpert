"""
Form Designer side panels: the control palette, the DBC symbol list (drag a signal onto the form) and the
property editor, with the constants and naming helpers the designer shares.
"""
import re
from pathlib import Path

from PyQt5.QtCore import QMimeData, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QDoubleValidator, QDrag, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from canexpert.panel.controls import APPEARANCE, CATEGORIES, CONTROLS, READ_ONLY
from canexpert.paths import DBC_DIR

try:
    import cantools
    HAS_CANTOOLS = True
except ImportError:
    HAS_CANTOOLS = False

WIDGET_TYPE_MIME = "application/x-ezcan-widget-type"
SIGNAL_MIME = "application/x-canexpert-signal"
BINDING_TYPE_SCRIPT = "script"
BINDING_TYPE_DBC = "dbc"
GRID = 10
UNDO_LIMIT = 100
MIN_SIZE = (10, 10)


def _slug(text):
    return re.sub(r"\W+", "_", str(text)).strip("_").lower() or "control"


def control_name(data):
    """The name a script uses for a control: its script binding, else its label, else its id."""
    if data.get("binding_type", BINDING_TYPE_SCRIPT) == BINDING_TYPE_SCRIPT and data.get("binding_value"):
        return str(data["binding_value"])
    return str(data.get("label") or data.get("text") or data.get("id") or "control")


def default_handler_name(data):
    control = CONTROLS.get(data.get("type"), CONTROLS["button"])
    return f"on_{_slug(control_name(data))}_{control.event}"


# -----------------------------------------------------------------------------
# Palette and symbols
# -----------------------------------------------------------------------------

class DraggablePaletteItem(QLabel):
    """A palette item that can be dragged onto the canvas."""
    def __init__(self, wtype: str, label: str):
        super().__init__(label)
        self.widget_type = wtype
        self.setToolTip(f"Drag onto the form to add a {label}")
        self.setStyleSheet(
            "DraggablePaletteItem { padding: 4px 8px; border: 1px solid palette(mid); border-radius: 4px;"
            " background: palette(button); }"
            "DraggablePaletteItem:hover { border-color: #0f6cbd; }"
        )
        self.setCursor(Qt.OpenHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setCursor(Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            mime = QMimeData()
            mime.setData(WIDGET_TYPE_MIME, self.widget_type.encode("utf-8"))
            mime.setText(self.widget_type)
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec_(Qt.CopyAction)
            self.setCursor(Qt.OpenHandCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.setCursor(Qt.OpenHandCursor)
        super().mouseReleaseEvent(event)


class WidgetPalette(QGroupBox):
    """Palette of control types by category - drag and drop onto the form."""

    def __init__(self):
        super().__init__("Controls")
        self.setToolTip("Drag items onto the form to add them.")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(0, 0, 0, 0)
        for category in CATEGORIES:
            header = QLabel(category)
            header.setStyleSheet("font-weight: bold; margin-top: 4px;")
            layout.addWidget(header)
            grid = QGridLayout()
            grid.setSpacing(4)
            controls = [c for c in CONTROLS.values() if c.category == category and c.in_palette]
            for index, control in enumerate(controls):
                grid.addWidget(DraggablePaletteItem(control.kind, control.label), index // 2, index % 2)
            layout.addLayout(grid)
        layout.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll)


class SymbolTree(QTreeWidget):
    """DBC messages and signals; signals can be dragged onto the form."""

    def mimeData(self, items):
        mime = QMimeData()
        names = [item.data(0, Qt.UserRole) for item in items if item.data(0, Qt.UserRole) and
                 "." in item.data(0, Qt.UserRole)]
        if names:
            mime.setData(SIGNAL_MIME, names[0].encode("utf-8"))
            mime.setText(names[0])
        return mime


class SymbolListPanel(QGroupBox):
    """Left panel: DBC signals and script variables (Vector-style symbol list)."""
    symbol_selected = pyqtSignal(str)  # "Message.Signal" or variable name
    dbc_loaded = pyqtSignal(str)  # path when DBC is loaded

    def __init__(self):
        super().__init__("Symbols")
        self.setToolTip("DBC signals: drag onto the form to add a bound display (Ctrl+drag: an input),\n"
                        "or double-click to bind the selected control.")
        layout = QVBoxLayout()
        self.dbc_path_label = QLabel("No DBC loaded")
        self.dbc_path_label.setStyleSheet("color: gray; font-size: 11px;")
        self.dbc_path_label.setWordWrap(True)
        layout.addWidget(self.dbc_path_label)
        load_dbc_btn = QPushButton("Load DBC...")
        load_dbc_btn.clicked.connect(self._load_dbc)
        layout.addWidget(load_dbc_btn)
        if not HAS_CANTOOLS:
            load_dbc_btn.setEnabled(False)
            layout.addWidget(QLabel("Install cantools for DBC"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter signals...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)
        self.symbol_tree = SymbolTree()
        self.symbol_tree.setHeaderLabels(["Symbol"])
        self.symbol_tree.setColumnWidth(0, 180)
        self.symbol_tree.setDragEnabled(True)
        self.symbol_tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.symbol_tree)
        self.setLayout(layout)
        self._dbc_db = None

    def _load_dbc(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load DBC", str(DBC_DIR),
                                              "DBC (*.dbc);;All (*.*)")
        if path:
            self.load_dbc_path(path)

    def load_dbc_path(self, path: str):
        if not HAS_CANTOOLS:
            return
        try:
            self._dbc_db = cantools.database.load_file(path)
            self.dbc_path_label.setText(Path(path).name)
            self.dbc_path_label.setStyleSheet("font-size: 11px;")
            self._fill_tree()
            self.dbc_loaded.emit(path)
        except Exception as e:
            self.dbc_path_label.setText(f"Error: {e}")
            self.dbc_path_label.setStyleSheet("color: red; font-size: 11px;")
            self._dbc_db = None
            self.symbol_tree.clear()

    def _fill_tree(self):
        self.symbol_tree.clear()
        if not self._dbc_db:
            return
        for msg in sorted(self._dbc_db.messages, key=lambda m: m.name.lower()):
            parent = QTreeWidgetItem(self.symbol_tree, [msg.name])
            parent.setData(0, Qt.UserRole, msg.name)
            parent.setFlags(Qt.ItemIsEnabled)
            for sig in sorted(msg.signals, key=lambda s: s.name.lower()):
                display_name = f"{msg.name}.{sig.name}"
                child = QTreeWidgetItem(parent, [sig.name + (f"  [{sig.unit}]" if sig.unit else "")])
                child.setData(0, Qt.UserRole, display_name)
                child.setToolTip(0, display_name)
                child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
        self._apply_filter()

    def _apply_filter(self, *_):
        text = self.filter_edit.text().strip().lower()
        for index in range(self.symbol_tree.topLevelItemCount()):
            parent = self.symbol_tree.topLevelItem(index)
            visible = 0
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                shown = not text or text in child.data(0, Qt.UserRole).lower()
                child.setHidden(not shown)
                visible += shown
            parent.setHidden(visible == 0)
            parent.setExpanded(bool(text) and visible > 0)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        symbol = item.data(0, Qt.UserRole)
        if symbol and "." in symbol:
            self.symbol_selected.emit(symbol)

    def get_dbc_signals(self) -> list:
        """Return list of 'Message.Signal' strings for property panel combo."""
        if not self._dbc_db:
            return []
        return [f"{msg.name}.{sig.name}" for msg in self._dbc_db.messages for sig in msg.signals]

    def signal_info(self, name):
        """Unit, value table and range of a DBC signal, or None."""
        if not self._dbc_db or "." not in name:
            return None
        message_name, signal_name = name.split(".", 1)
        try:
            signal = self._dbc_db.get_message_by_name(message_name).get_signal_by_name(signal_name)
        except KeyError:
            return None
        return {"name": signal_name, "unit": signal.unit or "",
                "choices": {int(k): str(v) for k, v in (signal.choices or {}).items()},
                "minimum": signal.minimum, "maximum": signal.maximum}


# -----------------------------------------------------------------------------
# Property editor
# -----------------------------------------------------------------------------

class ColorField(QWidget):
    """Colour swatch button plus a clear button; value is '#rrggbb' or ''."""
    changed = pyqtSignal(str)

    def __init__(self, value):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.button = QPushButton()
        self.button.clicked.connect(self._pick)
        clear = QToolButton()
        clear.setText("✕")
        clear.setToolTip("Use the default colour")
        clear.clicked.connect(lambda: self.set_value("", emit=True))
        layout.addWidget(self.button, 1)
        layout.addWidget(clear)
        self.value = ""
        self.set_value(value)

    def set_value(self, value, emit=False):
        self.value = str(value or "")
        colour = QColor(self.value)
        if self.value and colour.isValid():
            pixmap = QPixmap(14, 14)
            pixmap.fill(colour)
            self.button.setIcon(QIcon(pixmap))
            self.button.setText(colour.name())
        else:
            self.button.setIcon(QIcon())
            self.button.setText("Default")
        if emit:
            self.changed.emit(self.value)

    def _pick(self):
        colour = QColorDialog.getColor(QColor(self.value or "#0f6cbd"), self, "Choose colour")
        if colour.isValid():
            self.set_value(colour.name(), emit=True)


class PropertyEditor(QGroupBox):
    """Properties of the selected control: name/binding/handler, geometry, control settings, appearance."""
    properties_changed = pyqtSignal(dict)
    editing = pyqtSignal(str)                 # emitted before a property changes (undo checkpoint)
    handler_requested = pyqtSignal(dict)

    def __init__(self, symbol_panel=None):
        super().__init__("Properties")
        self.symbol_panel = symbol_panel
        self.base_dir = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        self.hint = QLabel("Select a control to edit its properties.")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: gray;")
        outer.addWidget(self.hint)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self.form = QFormLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        self.widget_data = None
        self.controls = {}
        self.last_key = None

    def clear(self):
        while self.form.count():
            item = self.form.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.controls.clear()
        self.widget_data = None
        self.hint.setText("Select a control to edit its properties.")

    def _section(self, title):
        label = QLabel(title)
        label.setStyleSheet("font-weight: bold; margin-top: 6px;")
        self.form.addRow(label)

    def load_widget(self, data: dict, selection_count: int = 1):
        self.clear()
        self.widget_data = data
        if not data:
            return
        wtype = data.get("type", "button")
        control = CONTROLS.get(wtype, CONTROLS["label"])
        self.hint.setText(f"{control.label}" + (f"  —  {selection_count} controls selected; editing the last one"
                                                if selection_count > 1 else ""))

        self._section("Control")
        binding_type = data.get("binding_type", BINDING_TYPE_SCRIPT)
        binding_val = str(data.get("binding_value") if data.get("binding_value") is not None
                          else data.get("variable", ""))
        self._add_combo("binding_type", "Binding type", [BINDING_TYPE_SCRIPT, BINDING_TYPE_DBC], binding_type)
        dbc_signals = self.symbol_panel.get_dbc_signals() if self.symbol_panel else []
        if dbc_signals:
            ctrl = QComboBox()
            ctrl.setEditable(True)
            ctrl.addItems([""] + dbc_signals)
            ctrl.setCurrentText(binding_val)
            ctrl.currentTextChanged.connect(lambda v, k="binding_value": self._on_change(k, v))
            self.controls["binding_value"] = ("str", ctrl)
            self.form.addRow("Name / signal", ctrl)
        else:
            self._add_line("binding_value", "Name (script) or Message.Signal", binding_val)
        if control.interactive:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            handler = QLineEdit(str(data.get("handler", "")))
            handler.setPlaceholderText(default_handler_name(data))
            handler.textChanged.connect(lambda v: self._on_change("handler", v.strip()))
            edit = QPushButton("Edit code")
            edit.setToolTip("Create or open this handler in the Python script (or double-click the control)")
            edit.clicked.connect(lambda: self.handler_requested.emit(self.widget_data))
            row_layout.addWidget(handler, 1)
            row_layout.addWidget(edit)
            self.controls["handler"] = ("str", handler)
            self.form.addRow("Handler function", row)
        self._add_line("id", "ID", str(data.get("id", "1")))
        if control.uses_label:
            self._add_line("label", "Label", str(data.get("label", "")))
        self._add_spin("x", "X", data.get("x", 0), 0, 20000)
        self._add_spin("y", "Y", data.get("y", 0), 0, 20000)
        self._add_spin("width", "Width", data.get("width", 100), MIN_SIZE[0], 4000)
        self._add_spin("height", "Height", data.get("height", 30), MIN_SIZE[1], 4000)

        if control.props:
            self._section(f"{control.label} settings")
            for prop in control.props:
                self._add_prop(prop, data.get(prop.key, prop.default))
        self._section("Appearance")
        for prop in APPEARANCE + ((READ_ONLY,) if control.interactive else ()):
            self._add_prop(prop, data.get(prop.key, prop.default))

    def _add_prop(self, prop, value):
        editor = prop.editor
        if editor == "int":
            self._add_spin(prop.key, prop.label, value, int(prop.minimum), int(prop.maximum))
        elif editor == "float":
            ctrl = QDoubleSpinBox()
            ctrl.setDecimals(3)
            ctrl.setRange(prop.minimum, prop.maximum)
            try:
                ctrl.setValue(float(value))
            except (TypeError, ValueError):
                ctrl.setValue(float(prop.default or 0))
            ctrl.valueChanged.connect(lambda v, k=prop.key: self._on_change(k, int(v) if float(v).is_integer() else v))
            self.controls[prop.key] = ("float", ctrl)
            self.form.addRow(prop.label, ctrl)
        elif editor == "optional_float":
            ctrl = QLineEdit("" if value in (None, "") else str(value))
            validator = QDoubleValidator()
            validator.setNotation(QDoubleValidator.StandardNotation)
            ctrl.setValidator(validator)
            ctrl.setPlaceholderText("blank")
            ctrl.textChanged.connect(lambda v, k=prop.key: self._on_change(k, v.strip()))
            self.controls[prop.key] = ("str", ctrl)
            self.form.addRow(prop.label, ctrl)
        elif editor == "bool":
            ctrl = QCheckBox()
            ctrl.setChecked(str(value).strip().lower() in ("1", "true", "yes", "on") if isinstance(value, str)
                            else bool(value))
            ctrl.toggled.connect(lambda v, k=prop.key: self._on_change(k, v))
            self.controls[prop.key] = ("bool", ctrl)
            self.form.addRow(prop.label, ctrl)
        elif editor == "choice":
            self._add_combo(prop.key, prop.label, list(prop.options), str(value or prop.default))
        elif editor == "color":
            ctrl = ColorField(value)
            ctrl.changed.connect(lambda v, k=prop.key: self._on_change(k, v))
            self.controls[prop.key] = ("str", ctrl)
            self.form.addRow(prop.label, ctrl)
        elif editor == "file":
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            ctrl = QLineEdit(str(value or ""))
            ctrl.textChanged.connect(lambda v, k=prop.key: self._on_change(k, v.strip()))
            browse = QToolButton()
            browse.setText("...")
            browse.clicked.connect(lambda: self._browse_image(ctrl))
            row_layout.addWidget(ctrl, 1)
            row_layout.addWidget(browse)
            self.controls[prop.key] = ("str", ctrl)
            self.form.addRow(prop.label, row)
        else:  # str, states
            self._add_line(prop.key, prop.label, str(value if value is not None else ""))
            if editor == "states":
                self.controls[prop.key][1].setToolTip("value=text:colour; ...  e.g. 0=Off:#5f6368; 1=On:#2e7d32\n"
                                                      "Left at the default, a DBC-bound indicator uses the signal's "
                                                      "value table.")

    def _browse_image(self, line_edit):
        start = str(self.base_dir or Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Choose image", start,
                                              "Images (*.png *.jpg *.jpeg *.bmp *.gif *.svg);;All files (*.*)")
        if not path:
            return
        if self.base_dir:
            try:
                path = str(Path(path).resolve().relative_to(Path(self.base_dir).resolve()))
            except ValueError:
                pass
        line_edit.setText(path)

    def _add_line(self, key: str, label: str, value: str):
        ctrl = QLineEdit(value)
        ctrl.textChanged.connect(lambda v, k=key: self._on_change(k, v))
        self.controls[key] = ("str", ctrl)
        self.form.addRow(label, ctrl)

    def _add_spin(self, key: str, label: str, value, min_val: int, max_val: int):
        ctrl = QSpinBox()
        ctrl.setRange(min_val, max_val)
        try:
            ctrl.setValue(int(float(value)))
        except (TypeError, ValueError):
            ctrl.setValue(min_val)
        ctrl.valueChanged.connect(lambda v, k=key: self._on_change(k, v))
        self.controls[key] = ("int", ctrl)
        self.form.addRow(label, ctrl)

    def _add_combo(self, key: str, label: str, options: list, value: str):
        ctrl = QComboBox()
        ctrl.addItems(options)
        ctrl.setCurrentText(value)
        ctrl.currentTextChanged.connect(lambda v, k=key: self._on_change(k, v))
        self.controls[key] = ("str", ctrl)
        self.form.addRow(label, ctrl)

    def set_geometry_values(self, data):
        """Refresh X/Y/width/height after a move or resize on the canvas without rebuilding the editor."""
        for key in ("x", "y", "width", "height"):
            entry = self.controls.get(key)
            if entry is not None:
                entry[1].blockSignals(True)
                entry[1].setValue(int(data.get(key, 0)))
                entry[1].blockSignals(False)

    def _on_change(self, key: str, value):
        if not self.widget_data:
            return
        self.last_key = key
        self.editing.emit(key)
        if key == "binding_value":
            self.widget_data["binding_value"] = str(value) if value else ""
            if self.widget_data.get("binding_type", BINDING_TYPE_SCRIPT) == BINDING_TYPE_SCRIPT:
                self.widget_data["variable"] = self.widget_data["binding_value"]
        elif key in ("value_type", "text", "binding_type", "handler"):
            self.widget_data[key] = str(value)
        else:
            self.widget_data[key] = value
        self.properties_changed.emit(self.widget_data)

    def get_data(self) -> dict:
        return self.widget_data
