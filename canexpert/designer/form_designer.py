"""
Form Designer

Visual editor for panel databases, in the spirit of CANoe's Panel Designer: drag controls from the
palette (or DBC signals from the symbol list) onto pages, arrange them with multi-select, align,
distribute, grid snap, resize handles and undo/redo, set their properties, and write the panel's
Python script (per-control handlers and CAPL-style event decorators). Test mode runs the panel
against the simulated ECU on a virtual CAN bus.
"""
import copy
import re
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from PyQt5.QtCore import QMimeData, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QDoubleValidator, QDrag, QIcon, QPalette, QPen, QPixmap, QPolygonF, QTextCursor
from PyQt5.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGraphicsItem, QGraphicsProxyWidget, QGraphicsRectItem, QGraphicsScene, QGraphicsView, QGridLayout,
    QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QRubberBand, QScrollArea, QSpinBox, QSplitter, QTabWidget, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from canexpert.designer.code_editor import CodeEditor, UdsFunctionPanel
from canexpert.flashing import (choose_firmware, close_progress, confirm_flash, progress_dialog,
                         report_result, update_progress)
from canexpert.panel.database import DATABASES_DIR, parse_widget, parse_application_database
from canexpert.panel.controls import APPEARANCE, CATEGORIES, CONTROLS, READ_ONLY, WIDGET_GROUPS, build
from canexpert.panel.runtime import SCRIPT_TEMPLATE
from canexpert.paths import DBC_DIR, EXAMPLE_FIRMWARE_DIR
from canexpert.ui_common import SplitterPanel, enable_maximize, line_icon

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
    add_clicked = pyqtSignal(str)

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
        self._dbc_path = None

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
            self._dbc_path = path
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
# Canvas items
# -----------------------------------------------------------------------------

class CanvasItem(QGraphicsProxyWidget):
    """A control on the canvas. Mouse handling is delegated to the canvas (selection, dragging)."""

    def __init__(self, canvas, index, kind):
        super().__init__()
        self.canvas, self.index, self.kind = canvas, index, kind
        self.setAcceptHoverEvents(False)
        self.setFlag(QGraphicsItem.ItemIsFocusable, False)
        self.setCursor(Qt.SizeAllCursor)

    def is_container_interior(self, scene_pos):
        """Inside a group box, away from its title and border: a rubber-band start, not a move."""
        if self.kind != "group_box":
            return False
        local = self.mapFromScene(scene_pos)
        rect = self.boundingRect()
        return rect.adjusted(8, 22, -8, -8).contains(local)

    def mousePressEvent(self, event):
        self.canvas.item_pressed(self, event)

    def mouseMoveEvent(self, event):
        self.canvas.item_dragged(event)

    def mouseReleaseEvent(self, event):
        self.canvas.item_released(event)

    def mouseDoubleClickEvent(self, event):
        self.canvas.handler_requested.emit(self.index)

    def wheelEvent(self, event):
        event.ignore()  # scroll the canvas, not the previewed control

    def contextMenuEvent(self, event):
        event.ignore()


class ResizeHandle(QGraphicsRectItem):
    """Bottom-right handle of the primary selection."""
    SIZE = 8

    def __init__(self, canvas):
        super().__init__(0, 0, self.SIZE, self.SIZE)
        self.canvas = canvas
        self.setBrush(QColor("#0f6cbd"))
        self.setPen(QPen(QColor("#ffffff"), 1))
        self.setCursor(Qt.SizeFDiagCursor)
        self.setZValue(20000)
        self._start = None

    def mousePressEvent(self, event):
        self._start = event.scenePos()
        self.canvas.resize_started()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._start is not None:
            delta = event.scenePos() - self._start
            self.canvas.resize_dragged(delta.x(), delta.y())
        event.accept()

    def mouseReleaseEvent(self, event):
        self._start = None
        self.canvas.resize_finished()
        event.accept()


class DroppableGraphicsView(QGraphicsView):
    """Canvas view: accepts palette controls and DBC signals, draws the grid, rubber-band selection, keys."""
    widget_dropped = pyqtSignal(str, int, int)
    signal_dropped = pyqtSignal(str, int, int, bool)   # name, x, y, as input control (Ctrl held)

    def __init__(self, scene, canvas, parent=None):
        super().__init__(scene, parent)
        self.canvas = canvas
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._shown = False
        self._band = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._band_origin = None

    def scroll_to_origin(self):
        """Show the page's top-left corner (Qt otherwise keeps the scene centre in view)."""
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.setValue(bar.minimum())

    def showEvent(self, event):
        super().showEvent(event)
        if not self._shown:
            self._shown = True
            QTimer.singleShot(0, self.scroll_to_origin)  # after the dialog's layout has settled

    def drawBackground(self, painter, rect):
        base = self.palette().color(QPalette.Base)
        painter.fillRect(rect, base)
        if not self.canvas.show_grid:
            return
        dot = self.palette().color(QPalette.Text)
        dot.setAlpha(45)
        painter.setPen(QPen(dot, 1))
        left = int(rect.left()) - int(rect.left()) % GRID
        top = int(rect.top()) - int(rect.top()) % GRID
        points = [QPointF(x, y) for x in range(left, int(rect.right()) + 1, GRID)
                  for y in range(top, int(rect.bottom()) + 1, GRID)]
        if 0 < len(points) < 200000:
            painter.drawPoints(QPolygonF(points))

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(WIDGET_TYPE_MIME) or event.mimeData().hasFormat(SIGNAL_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(WIDGET_TYPE_MIME) or event.mimeData().hasFormat(SIGNAL_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event):
        pos = self.mapToScene(event.pos())
        x, y = max(0, int(pos.x())), max(0, int(pos.y()))
        if event.mimeData().hasFormat(WIDGET_TYPE_MIME):
            self.widget_dropped.emit(event.mimeData().data(WIDGET_TYPE_MIME).data().decode("utf-8"), x, y)
        elif event.mimeData().hasFormat(SIGNAL_MIME):
            as_input = bool(event.keyboardModifiers() & Qt.ControlModifier)
            self.signal_dropped.emit(event.mimeData().data(SIGNAL_MIME).data().decode("utf-8"), x, y, as_input)
        else:
            return
        event.acceptProposedAction()

    def mousePressEvent(self, event):
        self.setFocus()
        scene_pos = self.mapToScene(event.pos())
        top = next((item for item in self.items(event.pos()) if isinstance(item, (CanvasItem, ResizeHandle))), None)
        if event.button() == Qt.LeftButton and (top is None or (isinstance(top, CanvasItem)
                                                                 and top.is_container_interior(scene_pos))):
            if not event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier):
                self.canvas.set_selection([])
            self._band_origin = event.pos()
            self._band.setGeometry(QRect(event.pos(), QSize()))
            self._band.show()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._band_origin is not None:
            self._band.setGeometry(QRect(self._band_origin, event.pos()).normalized())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._band_origin is not None:
            rect = self.mapToScene(QRect(self._band_origin, event.pos()).normalized()).boundingRect()
            self._band.hide()
            self._band_origin = None
            add = bool(event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier))
            self.canvas.select_in_rect(rect, add)
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if not self.canvas.key_pressed(event):
            super().keyPressEvent(event)


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


# -----------------------------------------------------------------------------
# Canvas
# -----------------------------------------------------------------------------

_ICONS = {
    "align_left": '<path d="M4 3v18"/><rect x="7" y="6" width="10" height="4" rx="1"/><rect x="7" y="14" width="14" height="4" rx="1"/>',
    "align_center": '<path d="M12 3v18"/><rect x="6" y="6" width="12" height="4" rx="1"/><rect x="4" y="14" width="16" height="4" rx="1"/>',
    "align_right": '<path d="M20 3v18"/><rect x="7" y="6" width="10" height="4" rx="1"/><rect x="3" y="14" width="14" height="4" rx="1"/>',
    "align_top": '<path d="M3 4h18"/><rect x="6" y="7" width="4" height="10" rx="1"/><rect x="14" y="7" width="4" height="14" rx="1"/>',
    "align_middle": '<path d="M3 12h18"/><rect x="6" y="6" width="4" height="12" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    "align_bottom": '<path d="M3 20h18"/><rect x="6" y="7" width="4" height="10" rx="1"/><rect x="14" y="3" width="4" height="14" rx="1"/>',
    "same_width": '<rect x="4" y="5" width="16" height="5" rx="1"/><rect x="4" y="14" width="16" height="5" rx="1"/><path d="M4 12h16"/>',
    "same_height": '<rect x="5" y="4" width="5" height="16" rx="1"/><rect x="14" y="4" width="5" height="16" rx="1"/><path d="M12 4v16"/>',
    "same_size": '<rect x="3" y="5" width="8" height="8" rx="1"/><rect x="13" y="11" width="8" height="8" rx="1"/>',
    "distribute_h": '<path d="M3 3v18M21 3v18"/><rect x="7" y="8" width="3" height="8" rx="1"/><rect x="14" y="8" width="3" height="8" rx="1"/>',
    "distribute_v": '<path d="M3 3h18M3 21h18"/><rect x="8" y="7" width="8" height="3" rx="1"/><rect x="8" y="14" width="8" height="3" rx="1"/>',
    "front": '<rect x="3" y="3" width="11" height="11" rx="1"/><rect x="10" y="10" width="11" height="11" rx="1" fill="currentColor"/>',
    "back": '<rect x="10" y="10" width="11" height="11" rx="1"/><rect x="3" y="3" width="11" height="11" rx="1" fill="currentColor"/>',
    "undo": '<path d="M9 7L4 12l5 5"/><path d="M4 12h11a5 5 0 0 1 0 10h-2"/>',
    "redo": '<path d="M15 7l5 5-5 5"/><path d="M20 12H9a5 5 0 0 0 0 10h2"/>',
    "grid": '<path d="M4 4h.01M12 4h.01M20 4h.01M4 12h.01M12 12h.01M20 12h.01M4 20h.01M12 20h.01M20 20h.01" stroke-width="3"/>',
}


class FormCanvas(QGroupBox):
    """Pages of controls with selection, dragging, resizing, layout tools, clipboard and undo/redo."""
    widget_selected = pyqtSignal(int, dict)
    selection_cleared = pyqtSignal()
    handler_requested = pyqtSignal(int)
    geometry_changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__("Form Preview")
        self.pages = [{"name": "Main", "widgets": []}]
        self.current_page_index = 0
        self.selection = []                  # indices on the current page; the last one is primary
        self.base_dir = None
        self.snap = True
        self.show_grid = True
        self._widget_clipboard = []
        self._items = []
        self._outlines = []
        self._handle = None
        self._drag = None
        self._resize = None
        self._undo, self._redo = [], []
        self._last_checkpoint = (None, 0.0)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(4, 4, 4, 4)
        self.tool_bar = QHBoxLayout()
        self.tool_bar.setSpacing(2)
        self._tool_buttons = {}
        tools = [("undo", "Undo (Ctrl+Z)", self.undo), ("redo", "Redo (Ctrl+Y)", self.redo), None,
                 ("align_left", "Align left edges", lambda: self.align("left")),
                 ("align_center", "Align centres horizontally", lambda: self.align("center")),
                 ("align_right", "Align right edges", lambda: self.align("right")),
                 ("align_top", "Align top edges", lambda: self.align("top")),
                 ("align_middle", "Align centres vertically", lambda: self.align("middle")),
                 ("align_bottom", "Align bottom edges", lambda: self.align("bottom")), None,
                 ("same_width", "Make same width", lambda: self.align("same_width")),
                 ("same_height", "Make same height", lambda: self.align("same_height")),
                 ("same_size", "Make same size", lambda: self.align("same_size")),
                 ("distribute_h", "Distribute horizontally (3 or more)", lambda: self.align("distribute_h")),
                 ("distribute_v", "Distribute vertically (3 or more)", lambda: self.align("distribute_v")), None,
                 ("front", "Bring to front", self.bring_to_front), ("back", "Send to back", self.send_to_back), None,
                 ("grid", "Show grid and snap to it", self._toggle_grid)]
        for entry in tools:
            if entry is None:
                separator = QFrame()
                separator.setFrameShape(QFrame.VLine)
                separator.setFrameShadow(QFrame.Sunken)
                self.tool_bar.addWidget(separator)
                continue
            name, tip, callback = entry
            button = QToolButton()
            button.setToolTip(tip + ("\nAligned to the last-selected control (outlined in bold)"
                                     if name.startswith(("align", "same")) else ""))
            button.setAutoRaise(True)
            button.setIconSize(QSize(18, 18))
            button.clicked.connect(callback)
            self._tool_buttons[name] = button
            self.tool_bar.addWidget(button)
        self._tool_buttons["grid"].setCheckable(True)
        self._tool_buttons["grid"].setChecked(True)
        self.tool_bar.addStretch()
        main_layout.addLayout(self.tool_bar)

        # Page bar: clickable page names + Add page
        self.page_bar = QWidget()
        page_bar_layout = QHBoxLayout()
        page_bar_layout.setContentsMargins(0, 0, 0, 0)
        self.page_buttons = []
        add_page_btn = QPushButton("+ Add page")
        add_page_btn.clicked.connect(self._add_page)
        page_bar_layout.addWidget(add_page_btn)
        page_bar_layout.addStretch()
        self.page_bar.setLayout(page_bar_layout)
        main_layout.addWidget(self.page_bar)
        self._rebuild_page_bar()

        self.scene = QGraphicsScene(0, 0, 800, 600)
        self.graphics_view = DroppableGraphicsView(self.scene, self)
        # Page origin at the view's top-left, as in the connected panel.
        self.graphics_view.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.graphics_view.setMinimumSize(400, 300)
        self.graphics_view.setToolTip(
            "Drag controls from the palette or signals from the symbol list. Click to select, Ctrl+click or drag a\n"
            "rectangle to select several, drag to move, use the corner handle to resize. Double-click an input to\n"
            "edit its handler. Arrows nudge (Shift = grid), Delete, Ctrl+C/X/V/D, Ctrl+Z/Y, Ctrl+A.")
        self.graphics_view.widget_dropped.connect(self._drop_control)
        self.graphics_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.graphics_view.customContextMenuRequested.connect(self._show_context_menu)
        main_layout.addWidget(self.graphics_view, 1)
        self.setLayout(main_layout)
        self._refresh_icons()
        self._update_tool_states()

    # --- model helpers ----------------------------------------------------------------

    def _current_widgets(self):
        return self.pages[self.current_page_index]["widgets"]

    @property
    def selected_index(self):
        return self.selection[-1] if self.selection else -1

    @selected_index.setter
    def selected_index(self, index):
        self.selection = [index] if index is not None and index >= 0 else []

    def _snap(self, value):
        return int(round(value / GRID) * GRID) if self.snap else int(round(value))

    def _next_id(self):
        return 1 + max((int(w.get("id", 0)) for p in self.pages for w in p["widgets"]
                        if str(w.get("id", "")).isdigit()), default=0)

    def _default_data(self, wtype: str) -> dict:
        control = CONTROLS.get(wtype, CONTROLS["label"])
        n = self._next_id()
        data = {"type": wtype, "id": str(n), "x": 0, "y": 0, "variable": "", "binding_type": BINDING_TYPE_SCRIPT,
                "binding_value": ""}
        data.update(control.defaults())
        if wtype == "label":
            data["text"] = f"Label {n}"
            data["label"] = data["text"]
        else:
            data["label"] = f"{control.label} {n}" if wtype != "group_box" else f"Group {n}"
        return data

    # --- undo / redo --------------------------------------------------------------------

    def _snapshot(self):
        return copy.deepcopy(self.pages), self.current_page_index

    def checkpoint(self, key=None):
        """Remember the current state before a change; repeated edits with the same key within 1.5 s merge."""
        last_key, last_time = self._last_checkpoint
        now = time.monotonic()
        if key is not None and key == last_key and now - last_time < 1.5:
            self._last_checkpoint = (key, now)
            return
        self._undo.append(self._snapshot())
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()
        self._last_checkpoint = (key, now)
        self._update_tool_states()

    def _restore(self, snapshot):
        pages, page_index = snapshot
        self.pages = copy.deepcopy(pages)
        self.current_page_index = min(page_index, len(self.pages) - 1)
        self.selection = []
        self._last_checkpoint = (None, 0.0)
        self._rebuild_page_bar()
        self._rebuild()
        self.selection_cleared.emit()
        self._update_tool_states()

    def undo(self):
        if self._undo:
            self._redo.append(self._snapshot())
            self._restore(self._undo.pop())

    def redo(self):
        if self._redo:
            self._undo.append(self._snapshot())
            self._restore(self._redo.pop())

    # --- pages ----------------------------------------------------------------------------

    def _rebuild_page_bar(self):
        for btn in self.page_buttons:
            btn.deleteLater()
        self.page_buttons.clear()
        bar = self.page_bar.layout()
        for i, page in enumerate(self.pages):
            btn = QPushButton(page.get("name", "Page"))
            btn.setCheckable(True)
            btn.setChecked(i == self.current_page_index)
            btn.setToolTip("Right-click to rename or remove")
            btn.clicked.connect(lambda checked, idx=i: self._switch_page(idx))
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(lambda pos, idx=i, b=btn: self._show_page_context_menu(idx, b, pos))
            self.page_buttons.append(btn)
            bar.insertWidget(i, btn)

    def _add_page(self):
        self.checkpoint()
        self.pages.append({"name": f"Page {len(self.pages) + 1}", "widgets": []})
        self.current_page_index = len(self.pages) - 1
        self.set_selection([])
        self._rebuild_page_bar()
        self._show_page()

    def _show_page_context_menu(self, page_index: int, button: QPushButton, pos):
        menu = QMenu(self)
        rename_act = menu.addAction("Rename page...")
        remove_act = menu.addAction("Remove page")
        remove_act.setEnabled(len(self.pages) > 1)
        action = menu.exec_(button.mapToGlobal(pos))
        if action is rename_act:
            name, ok = QInputDialog.getText(self, "Rename page", "Page name:", QLineEdit.Normal,
                                            self.pages[page_index]["name"])
            if ok and name.strip():
                self.checkpoint()
                self.pages[page_index]["name"] = name.strip()
                self._rebuild_page_bar()
        elif action is remove_act and len(self.pages) > 1:
            self._remove_page(page_index)

    def _remove_page(self, index: int):
        if index < 0 or index >= len(self.pages) or len(self.pages) <= 1:
            return
        self.checkpoint()
        del self.pages[index]
        if self.current_page_index == index:
            self.current_page_index = max(0, index - 1)
        elif self.current_page_index > index:
            self.current_page_index -= 1
        self.set_selection([])
        self._rebuild_page_bar()
        self._show_page()

    def _switch_page(self, index: int):
        if 0 <= index < len(self.pages):
            self.current_page_index = index
            self.set_selection([])
            for i, btn in enumerate(self.page_buttons):
                btn.setChecked(i == index)
            self._show_page()

    def _show_page(self):
        self._rebuild()
        self.graphics_view.scroll_to_origin()

    # --- adding and removing -------------------------------------------------------------

    def add_widget(self, wtype: str) -> dict:
        return self.add_widget_at(wtype, 0, 0)

    def add_widget_at(self, wtype: str, x: int, y: int, **extra) -> dict:
        self.checkpoint()
        data = self._default_data(wtype)
        data.update(extra)
        data["x"], data["y"] = max(0, self._snap(x)), max(0, self._snap(y))
        self._current_widgets().append(data)
        self.selection = [len(self._current_widgets()) - 1]
        self._rebuild()
        self._emit_selection()
        return data

    def _drop_control(self, wtype, x, y):
        self.add_widget_at(wtype, x, y)

    def add_signal_control(self, name, x, y, info, as_input=False):
        """A dropped DBC signal becomes a bound control: display (value table -> indicator) or, with Ctrl, an input."""
        info = info or {"name": name.split(".", 1)[-1], "unit": "", "choices": {}, "minimum": None, "maximum": None}
        choices = info.get("choices") or {}
        if as_input:
            kind = "combo" if choices else "spin"
        else:
            kind = "indicator" if choices else "value"
        extra = {"binding_type": BINDING_TYPE_DBC, "binding_value": name, "variable": name, "label": info["name"]}
        if info.get("unit") and kind in ("value", "spin"):
            extra["unit"] = info["unit"]
        if kind == "combo":
            extra["items"] = ", ".join(choices[k] for k in sorted(choices))
        if kind == "spin":
            if info.get("minimum") is not None:
                extra["min"] = info["minimum"]
            if info.get("maximum") is not None:
                extra["max"] = info["maximum"]
        if kind == "indicator":
            extra["states"] = ""  # empty: the panel uses the DBC value table
        return self.add_widget_at(kind, x, y, **extra)

    def update_widget(self, index: int, data: dict):
        w = self._current_widgets()
        if 0 <= index < len(w):
            w[index] = data
            self._rebuild()

    def delete_selection(self):
        if not self.selection:
            return
        self.checkpoint()
        for index in sorted(set(self.selection), reverse=True):
            del self._current_widgets()[index]
        self.set_selection([])
        self._rebuild()

    # --- clipboard ------------------------------------------------------------------------

    def copy_selection(self):
        widgets = self._current_widgets()
        self._widget_clipboard = [copy.deepcopy(widgets[i]) for i in sorted(self.selection)]

    def cut_selection(self):
        self.copy_selection()
        self.delete_selection()

    def paste_at(self, x=None, y=None):
        """Paste the clipboard; with a position, the pasted group's top-left lands there."""
        if not self._widget_clipboard:
            return
        self.checkpoint()
        left = min(w.get("x", 0) for w in self._widget_clipboard)
        top = min(w.get("y", 0) for w in self._widget_clipboard)
        dx, dy = (x - left, y - top) if x is not None else (GRID * 2, GRID * 2)
        names = {control_name(w) for p in self.pages for w in p["widgets"]}
        new_indices = []
        for source in self._widget_clipboard:
            data = copy.deepcopy(source)
            n = self._next_id()
            data["id"] = str(n)
            data["x"], data["y"] = max(0, self._snap(data.get("x", 0) + dx)), max(0, self._snap(data.get("y", 0) + dy))
            if data.get("binding_type", BINDING_TYPE_SCRIPT) == BINDING_TYPE_SCRIPT and data.get("binding_value"):
                base = data["binding_value"]
                suffix = 2
                while f"{base}_{suffix}" in names:
                    suffix += 1
                data["binding_value"] = data["variable"] = f"{base}_{suffix}"
                names.add(data["binding_value"])
            data.pop("handler", None)
            self._current_widgets().append(data)
            new_indices.append(len(self._current_widgets()) - 1)
        self._widget_clipboard = [copy.deepcopy(self._current_widgets()[i]) for i in new_indices]
        self.selection = new_indices
        self._rebuild()
        self._emit_selection()

    def duplicate_selection(self):
        self.copy_selection()
        self.paste_at()

    # --- selection ------------------------------------------------------------------------

    def set_selection(self, indices):
        count = len(self._current_widgets())
        self.selection = [i for i in dict.fromkeys(indices) if 0 <= i < count]
        self._update_overlay()
        self._emit_selection()

    def _emit_selection(self):
        self._update_tool_states()
        if self.selection:
            self.widget_selected.emit(self.selected_index, self._current_widgets()[self.selected_index])
        else:
            self.selection_cleared.emit()

    def select_in_rect(self, rect, add=False):
        hits = [item.index for item in self._items if rect.intersects(item.sceneBoundingRect())
                and not (item.kind == "group_box" and not rect.contains(item.sceneBoundingRect()))]
        self.set_selection((self.selection if add else []) + hits)

    def select_all(self):
        self.set_selection(list(range(len(self._current_widgets()))))

    def _sync_overlay(self):
        """Move the existing outlines and handle (keeps the resize handle's mouse grab alive)."""
        widgets = self._current_widgets()
        if len(self._outlines) != len(self.selection) or (self.selection and self._handle is None):
            self._update_overlay()
            return
        for outline, index in zip(self._outlines, self.selection):
            data = widgets[index]
            outline.setRect(QRectF(data.get("x", 0) - 2, data.get("y", 0) - 2,
                                   int(data.get("width", 100)) + 4, int(data.get("height", 30)) + 4))
        if self._handle is not None and self.selection:
            data = widgets[self.selected_index]
            self._handle.setPos(data.get("x", 0) + int(data.get("width", 100)) - ResizeHandle.SIZE / 2,
                                data.get("y", 0) + int(data.get("height", 30)) - ResizeHandle.SIZE / 2)

    def _update_overlay(self):
        for outline in self._outlines:
            self.scene.removeItem(outline)
        self._outlines = []
        if self._handle is not None:
            self.scene.removeItem(self._handle)
            self._handle = None
        widgets = self._current_widgets()
        for index in self.selection:
            data = widgets[index]
            primary = index == self.selected_index
            outline = QGraphicsRectItem(QRectF(data.get("x", 0) - 2, data.get("y", 0) - 2,
                                               int(data.get("width", 100)) + 4, int(data.get("height", 30)) + 4))
            pen = QPen(QColor("#0f6cbd"), 2 if primary else 1, Qt.SolidLine if primary else Qt.DashLine)
            outline.setPen(pen)
            outline.setZValue(10000)
            outline.setAcceptedMouseButtons(Qt.NoButton)
            self.scene.addItem(outline)
            self._outlines.append(outline)
        if self.selection:
            data = widgets[self.selected_index]
            self._handle = ResizeHandle(self)
            self._handle.setPos(data.get("x", 0) + int(data.get("width", 100)) - ResizeHandle.SIZE / 2,
                                data.get("y", 0) + int(data.get("height", 30)) - ResizeHandle.SIZE / 2)
            self.scene.addItem(self._handle)

    # --- mouse: select, move, resize ---------------------------------------------------------

    def item_pressed(self, item, event):
        index = item.index
        if event.button() == Qt.RightButton:
            if index not in self.selection:
                self.set_selection([index])
            return
        if event.button() != Qt.LeftButton:
            return
        if event.modifiers() & Qt.ControlModifier:
            if index in self.selection:
                self.set_selection([i for i in self.selection if i != index])
                return
            self.set_selection(self.selection + [index])
        elif index not in self.selection:
            self.set_selection([index])
        else:
            self.set_selection([i for i in self.selection if i != index] + [index])  # make it primary
        widgets = self._current_widgets()
        self._drag = {"start": event.scenePos(), "moved": False, "snapshot": self._snapshot(),
                      "origins": {i: (widgets[i].get("x", 0), widgets[i].get("y", 0)) for i in self.selection}}

    def item_dragged(self, event):
        if self._drag is None or not event.buttons() & Qt.LeftButton:
            return
        delta = event.scenePos() - self._drag["start"]
        if not self._drag["moved"]:
            if abs(delta.x()) <= 3 and abs(delta.y()) <= 3:
                return
            self._drag["moved"] = True
            self._undo.append(self._drag["snapshot"])
            del self._undo[:-UNDO_LIMIT]
            self._redo.clear()
            self._last_checkpoint = (None, 0.0)
        widgets = self._current_widgets()
        # Keep the group's shape: snap the primary control, move the others by the same amount.
        primary = self.selected_index
        px, py = self._drag["origins"][primary]
        dx = max(0, self._snap(px + delta.x())) - px
        dy = max(0, self._snap(py + delta.y())) - py
        dx = max(dx, -min(x for x, _ in self._drag["origins"].values()))
        dy = max(dy, -min(y for _, y in self._drag["origins"].values()))
        for index, (x0, y0) in self._drag["origins"].items():
            widgets[index]["x"], widgets[index]["y"] = int(x0 + dx), int(y0 + dy)
            self._items[index].setPos(widgets[index]["x"], widgets[index]["y"])
        self._sync_overlay()

    def item_released(self, event):
        if self._drag is None:
            return
        moved = self._drag["moved"]
        self._drag = None
        if moved:
            self._fit_scene()
            self._update_tool_states()
            self.geometry_changed.emit(self._current_widgets()[self.selected_index])

    def resize_started(self):
        data = self._current_widgets()[self.selected_index]
        self._resize = {"size": (int(data.get("width", 100)), int(data.get("height", 30))), "snapshot": self._snapshot(),
                        "changed": False}

    def resize_dragged(self, dx, dy):
        if self._resize is None:
            return
        index = self.selected_index
        data = self._current_widgets()[index]
        width0, height0 = self._resize["size"]
        right = self._snap(data.get("x", 0) + width0 + dx)
        bottom = self._snap(data.get("y", 0) + height0 + dy)
        width = max(MIN_SIZE[0], right - data.get("x", 0))
        height = max(MIN_SIZE[1], bottom - data.get("y", 0))
        if (width, height) == (int(data.get("width", 100)), int(data.get("height", 30))):
            return
        if not self._resize["changed"]:
            self._resize["changed"] = True
            self._undo.append(self._resize["snapshot"])
            del self._undo[:-UNDO_LIMIT]
            self._redo.clear()
        data["width"], data["height"] = width, height
        self._items[index].widget().setFixedSize(width, height)
        self._sync_overlay()

    def resize_finished(self):
        if self._resize is None:
            return
        changed = self._resize["changed"]
        self._resize = None
        if changed:
            # Recreate the preview at its new size once the handle's own mouse event has finished.
            QTimer.singleShot(0, self._after_resize)

    def _after_resize(self):
        self._rebuild()
        self._update_tool_states()
        if self.selection:
            self.geometry_changed.emit(self._current_widgets()[self.selected_index])

    # --- keyboard ---------------------------------------------------------------------------

    def key_pressed(self, event):
        key, mods = event.key(), event.modifiers()
        ctrl = bool(mods & Qt.ControlModifier)
        if ctrl and key == Qt.Key_Z:
            self.redo() if mods & Qt.ShiftModifier else self.undo()
        elif ctrl and key == Qt.Key_Y:
            self.redo()
        elif ctrl and key == Qt.Key_A:
            self.select_all()
        elif ctrl and key == Qt.Key_C:
            self.copy_selection()
        elif ctrl and key == Qt.Key_X:
            self.cut_selection()
        elif ctrl and key == Qt.Key_V:
            self.paste_at()
        elif ctrl and key == Qt.Key_D:
            self.duplicate_selection()
        elif key in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selection()
        elif key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down) and self.selection:
            step = GRID if mods & Qt.ShiftModifier else 1
            dx = {Qt.Key_Left: -step, Qt.Key_Right: step}.get(key, 0)
            dy = {Qt.Key_Up: -step, Qt.Key_Down: step}.get(key, 0)
            self.nudge(dx, dy)
        elif key == Qt.Key_Escape:
            self.set_selection([])
        else:
            return False
        return True

    def nudge(self, dx, dy):
        self.checkpoint(key="nudge")
        widgets = self._current_widgets()
        dx = max(dx, -min(widgets[i].get("x", 0) for i in self.selection))
        dy = max(dy, -min(widgets[i].get("y", 0) for i in self.selection))
        for index in self.selection:
            widgets[index]["x"] = int(widgets[index].get("x", 0) + dx)
            widgets[index]["y"] = int(widgets[index].get("y", 0) + dy)
            self._items[index].setPos(widgets[index]["x"], widgets[index]["y"])
        self._sync_overlay()
        self._fit_scene()
        self.geometry_changed.emit(widgets[self.selected_index])

    # --- layout tools -----------------------------------------------------------------------

    def align(self, operation):
        """Align/size/distribute the selection; the primary (last-selected) control is the reference."""
        widgets = self._current_widgets()
        selected = [widgets[i] for i in self.selection]
        needed = 3 if operation.startswith("distribute") else 2
        if len(selected) < needed:
            return
        self.checkpoint()
        ref = widgets[self.selected_index]
        rx, ry = ref.get("x", 0), ref.get("y", 0)
        rw, rh = int(ref.get("width", 100)), int(ref.get("height", 30))
        for data in selected:
            w, h = int(data.get("width", 100)), int(data.get("height", 30))
            if operation == "left":
                data["x"] = rx
            elif operation == "right":
                data["x"] = rx + rw - w
            elif operation == "center":
                data["x"] = rx + (rw - w) // 2
            elif operation == "top":
                data["y"] = ry
            elif operation == "bottom":
                data["y"] = ry + rh - h
            elif operation == "middle":
                data["y"] = ry + (rh - h) // 2
            if operation in ("same_width", "same_size"):
                data["width"] = rw
            if operation in ("same_height", "same_size"):
                data["height"] = rh
            data["x"], data["y"] = max(0, int(data.get("x", 0))), max(0, int(data.get("y", 0)))
        if operation in ("distribute_h", "distribute_v"):
            pos, size = ("x", "width") if operation == "distribute_h" else ("y", "height")
            ordered = sorted(selected, key=lambda d: d.get(pos, 0))
            start = ordered[0].get(pos, 0)
            end = ordered[-1].get(pos, 0) + int(ordered[-1].get(size, 0))
            gap = (end - start - sum(int(d.get(size, 0)) for d in ordered)) / (len(ordered) - 1)
            cursor = float(start)
            for data in ordered:
                data[pos] = int(round(cursor))
                cursor += int(data.get(size, 0)) + gap
        self._rebuild()
        self.geometry_changed.emit(ref)

    def _reorder(self, to_front):
        widgets = self._current_widgets()
        if not self.selection:
            return
        self.checkpoint()
        chosen = [widgets[i] for i in sorted(self.selection)]
        rest = [w for i, w in enumerate(widgets) if i not in self.selection]
        primary = widgets[self.selected_index]
        widgets[:] = rest + chosen if to_front else chosen + rest

        def position(target):  # identity, not equality: two controls may hold identical data
            return next(i for i, w in enumerate(widgets) if w is target)
        self.selection = [position(w) for w in chosen if w is not primary] + [position(primary)]
        self._rebuild()
        self._emit_selection()

    def bring_to_front(self):
        self._reorder(True)

    def send_to_back(self):
        self._reorder(False)

    def _toggle_grid(self):
        self.snap = self.show_grid = self._tool_buttons["grid"].isChecked()
        self.graphics_view.viewport().update()

    def _refresh_icons(self):
        colour = self.palette().color(QPalette.WindowText)
        for name, button in self._tool_buttons.items():
            body = _ICONS[name].replace('fill="currentColor"', f'fill="{colour.name()}"')
            button.setIcon(line_icon(body, colour))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (event.PaletteChange, event.ApplicationPaletteChange) and hasattr(self, "_tool_buttons"):
            self._refresh_icons()

    def _update_tool_states(self):
        count = len(self.selection)
        for name, button in self._tool_buttons.items():
            if name.startswith(("align", "same")):
                button.setEnabled(count >= 2)
            elif name.startswith("distribute"):
                button.setEnabled(count >= 3)
            elif name in ("front", "back"):
                button.setEnabled(count >= 1)
        self._tool_buttons["undo"].setEnabled(bool(self._undo))
        self._tool_buttons["redo"].setEnabled(bool(self._redo))

    # --- rendering --------------------------------------------------------------------------

    def _rebuild(self):
        self.scene.clear()
        self._items, self._outlines, self._handle = [], [], None
        widgets = self._current_widgets()
        self.selection = [i for i in self.selection if 0 <= i < len(widgets)]
        for i, data in enumerate(widgets):
            kind = data.get("type", "button")
            try:
                control, widget = build(kind, data, {"base_dir": self.base_dir})
                control.preview(widget, data)
            except Exception as exc:  # a half-typed property must not break the canvas
                widget = QLabel(f"{kind}: {exc}")
            widget.setFixedSize(max(MIN_SIZE[0], int(data.get("width", 100))), max(MIN_SIZE[1], int(data.get("height", 30))))
            widget.setAttribute(Qt.WA_TransparentForMouseEvents)
            item = CanvasItem(self, i, kind)
            item.setWidget(widget)
            item.setPos(data.get("x", 0), data.get("y", 0))
            item.setZValue(i)
            self.scene.addItem(item)
            self._items.append(item)
        self._update_overlay()
        self._fit_scene()
        self._update_tool_states()

    def _fit_scene(self):
        """Keep the page origin at (0, 0) and grow the page so widgets can be placed beyond 800 x 600."""
        right = max((d.get("x", 0) + int(d.get("width", 100)) for d in self._current_widgets()), default=0)
        bottom = max((d.get("y", 0) + int(d.get("height", 30)) for d in self._current_widgets()), default=0)
        self.scene.setSceneRect(QRectF(0, 0, max(800, right + 100), max(600, bottom + 100)))

    # --- context menu ---------------------------------------------------------------------------

    def _show_context_menu(self, view_pos):
        scene_pos = self.graphics_view.mapToScene(view_pos)
        item = next((i for i in self.graphics_view.items(view_pos) if isinstance(i, CanvasItem)), None)
        menu = QMenu(self)
        if item is not None:
            if item.index not in self.selection:
                self.set_selection([item.index])
            data = self._current_widgets()[item.index]
            control = CONTROLS.get(data.get("type"), CONTROLS["label"])
            if control.interactive:
                menu.addAction("Edit handler code", lambda: self.handler_requested.emit(item.index))
                menu.addSeparator()
            menu.addAction("Cut", self.cut_selection)
            menu.addAction("Copy", self.copy_selection)
        paste = menu.addAction("Paste", lambda: self.paste_at(self._snap(scene_pos.x()), self._snap(scene_pos.y())))
        paste.setEnabled(bool(self._widget_clipboard))
        if item is not None:
            menu.addAction("Duplicate", self.duplicate_selection)
            menu.addAction("Delete", self.delete_selection)
            menu.addSeparator()
            menu.addAction("Bring to front", self.bring_to_front)
            menu.addAction("Send to back", self.send_to_back)
            menu.addSeparator()
            menu.addAction("Change size...", lambda: self._dialog_change_size(item.index))
            menu.addAction("Binding...", lambda: self._dialog_variable(item.index))
        else:
            menu.addAction("Select all", self.select_all)
        menu.exec_(self.graphics_view.mapToGlobal(view_pos))

    def _dialog_change_size(self, index: int):
        data = self._current_widgets()[index]
        width, ok1 = QInputDialog.getInt(self, "Change size", "Width:", int(data.get("width", 100)), MIN_SIZE[0], 4000)
        if not ok1:
            return
        height, ok2 = QInputDialog.getInt(self, "Change size", "Height:", int(data.get("height", 30)), MIN_SIZE[1], 4000)
        if ok2:
            self.checkpoint()
            data["width"], data["height"] = width, height
            self._rebuild()
            self._emit_selection()

    def _dialog_variable(self, index: int):
        data = self._current_widgets()[index]
        name, ok = QInputDialog.getText(self, "Binding", "Script name or DBC signal (e.g. start or Message.Signal):",
                                        QLineEdit.Normal, data.get("binding_value", data.get("variable", "")))
        if ok:
            self.checkpoint()
            val = name.strip()
            data["binding_value"] = data["variable"] = val
            data["binding_type"] = BINDING_TYPE_SCRIPT if "." not in val or " " in val else BINDING_TYPE_DBC
            self._rebuild()
            self._emit_selection()

    # --- load / save ----------------------------------------------------------------------------

    def load_from_data(self, data: dict):
        if data.get("pages"):
            self.pages = [{"name": p.get("name", "Page"), "widgets": [self._normalize_loaded_widget(w)
                                                                       for w in p.get("widgets", [])]}
                          for p in data["pages"]] or [{"name": "Main", "widgets": []}]
        else:
            # Legacy: flat buttons/values/... into one page
            widgets = []
            for group, kind in (("buttons", "button"), ("values", "value"), ("checkboxes", "checkbox"),
                                ("sliders", "slider"), ("labels", "label")):
                for item in data.get(group, []):
                    d = dict(item, type=kind)
                    if kind == "button":
                        d["data_bytes"] = d.get("data_bytes", self._parse_hex(d.get("data", "00 00 00 00 00 00 00 00")))
                    if kind == "label":
                        d["label"] = d.get("text", "")
                    d.setdefault("x", 0)
                    d.setdefault("y", 0)
                    d.setdefault("value_type", "float")
                    widgets.append(d)
            self.pages = [{"name": "Main", "widgets": widgets}]
        self.current_page_index = 0
        self.selection = []
        self._undo, self._redo = [], []
        self._rebuild_page_bar()
        self._show_page()
        self.selection_cleared.emit()

    def _parse_hex(self, s: str) -> list:
        return [int(x, 16) for x in str(s).replace(",", " ").split() if x.strip()] or [0] * 8

    def _normalize_loaded_widget(self, w: dict) -> dict:
        d = w.copy()
        d.setdefault("x", 0)
        d.setdefault("y", 0)
        d.setdefault("width", 100)
        d.setdefault("height", 30)
        d.setdefault("variable", "")
        d.setdefault("binding_type", BINDING_TYPE_SCRIPT)
        d.setdefault("binding_value", d.get("variable", ""))
        if d.get("type") in ("value", "value_display"):
            d.setdefault("value_type", "float")
        if d.get("type") == "label":
            d["label"] = d.get("text", d.get("label", ""))
        return d

    def get_data(self) -> dict:
        pages = []
        for page in self.pages:
            widgets = []
            for item in page["widgets"]:
                data = {k: v for k, v in item.items() if k not in ("data_bytes", "kind") and not k.startswith("_")}
                if "data_bytes" in item:
                    data["data"] = " ".join(f"{v:02X}" for v in item["data_bytes"])
                widgets.append(data)
            pages.append({"name": page["name"], "widgets": widgets})
        return {"pages": pages}


# -----------------------------------------------------------------------------
# Test mode
# -----------------------------------------------------------------------------

class TestPanelDialog(QDialog):
    """Runs the panel and its script against the simulated ECU on a private virtual CAN bus."""
    ecu_log = pyqtSignal(str)

    def __init__(self, database, script_text, simulate_ecu=True, parent=None):
        super().__init__(parent)
        import can
        from canexpert.panel.database import PanelView
        from canexpert.panel.runtime import ReceiveMailbox, ScriptRuntime, validate_config
        self.setWindowTitle(f"Test panel - {database.get('name', '')}")
        enable_maximize(self)
        self.resize(1000, 720)
        layout = QVBoxLayout(self)
        note = QLabel("Test mode: a private virtual CAN bus" + (" with the simulated ECU (dummy_ecu.py)"
                                                                 if simulate_ecu else "") +
                      ". Nothing is sent to hardware.")
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)
        channel = f"designer-test-{uuid.uuid4()}"
        self.bus = can.Bus(interface="virtual", channel=channel)
        self._stop = threading.Event()
        self.ecu_bus = None
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.show_traffic = QCheckBox("Show CAN traffic")
        self.ecu_log.connect(self._log)
        if simulate_ecu:
            from canexpert.simulator.ecu import DummyEcu, EcuConfig
            self.ecu_bus = can.Bus(interface="virtual", channel=channel)
            self.ecu = DummyEcu(self.ecu_bus, EcuConfig(), log=lambda text: self.ecu_log.emit(f"ECU: {text}"))
            threading.Thread(target=self.ecu.serve, args=(self._stop,), daemon=True).start()
        self.panel = PanelView(database, self._send, self._log)
        self.mailbox = ReceiveMailbox(self.bus, lambda can_id, data: self._traffic("TX", can_id, data))
        config = validate_config({"name": "Test", "request_id": 0x7E0, "response_id": 0x7E8})
        self.runtime = ScriptRuntime(self.mailbox, config, self.panel.values(), self)
        self.runtime.value_changed.connect(self.panel.set_value)
        self.runtime.logged.connect(self._log)
        self.panel.control_changed.connect(lambda name, value: self.runtime.post("control", name, value))
        self.runtime.dbc = self.panel.dbc
        self.runtime.handlers = self.panel.handlers()
        self.flash_button = QPushButton("Flashing...")
        self.flash_button.setEnabled(False)
        self.flash_button.setToolTip("Flash a .s19/.hex file into the simulated ECU with the script's Flashing() "
                                     "(enabled when the script defines it)")
        self.flash_button.clicked.connect(self.open_flashing)
        self.flash_dialog = None
        self.runtime.flashing_available.connect(self.flash_button.setEnabled)
        self.runtime.flash_progress.connect(lambda done, total, text: update_progress(self.flash_dialog, done, total, text))
        self.runtime.flash_finished.connect(self._flash_finished)
        bar = QHBoxLayout()
        bar.addWidget(self.flash_button)
        bar.addStretch()
        layout.addLayout(bar)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.panel)
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(self.show_traffic)
        log_layout.addWidget(self.log_view)
        splitter.addWidget(log_box)
        splitter.setSizes([520, 160])
        layout.addWidget(splitter, 1)
        self._script_file = Path(tempfile.mkdtemp()) / "panel_test_script.py"
        self._script_file.write_text(script_text, encoding="utf-8")
        self.runtime.start(self._script_file)
        self._pump = QTimer(self)
        self._pump.setInterval(10)
        self._pump.timeout.connect(self._receive)
        self._pump.start()

    def _log(self, text):
        self.log_view.appendPlainText(str(text))

    def open_flashing(self):
        """Same flow as the main window's Flashing button, against the simulated ECU."""
        start = EXAMPLE_FIRMWARE_DIR if EXAMPLE_FIRMWARE_DIR.exists() else Path.home()
        firmware = choose_firmware(self, start)
        if firmware is not None and confirm_flash(self, firmware, "the simulated ECU"):
            self.start_flashing(firmware)

    def start_flashing(self, firmware):
        self.flash_button.setEnabled(False)
        self.flash_dialog = progress_dialog(self, firmware, self.runtime.cancel_flash)
        self._log(f"Flashing {Path(firmware.path).name}: {firmware.size} bytes in {len(firmware.segments)} segment(s)")
        self.runtime.start_flash(firmware)

    def _flash_finished(self, ok, text):
        dialog, self.flash_dialog = self.flash_dialog, None
        close_progress(dialog)
        self.flash_button.setEnabled(self.runtime.flash_function is not None)
        self._log(f"Flashing {'succeeded' if ok else 'failed'}: {text}")
        report_result(self, ok, text)

    def _traffic(self, direction, can_id, data):
        if self.show_traffic.isChecked():
            self.log_view.appendPlainText(f"{direction} 0x{can_id:03X}  {bytes(data).hex(' ')}")

    def _send(self, can_id, data, extended=None):
        import can
        self.bus.send(can.Message(arbitration_id=can_id, data=bytes(data), is_extended_id=bool(extended)))
        self._traffic("TX", can_id, data)

    def _receive(self):
        for _ in range(500):
            message = self.bus.recv(0)
            if message is None:
                return
            data = bytes(message.data)
            self._traffic("RX", message.arbitration_id, data)
            self.panel.on_message(message.arbitration_id, data)
            self.mailbox.push(message)
            with self.runtime.lock:
                self.runtime.values.update(self.panel.values())
            self.runtime.post("can", message.arbitration_id, data)

    def done(self, result):
        dialog, self.flash_dialog = self.flash_dialog, None
        close_progress(dialog)
        self._pump.stop()
        self.runtime.stop()
        self.mailbox.close()
        self._stop.set()
        time.sleep(0.05)
        for bus in (self.bus, self.ecu_bus):
            if bus is not None:
                bus.shutdown()
        super().done(result)


# -----------------------------------------------------------------------------
# Designer dialog
# -----------------------------------------------------------------------------

class FormDesigner(QDialog):
    """Main form designer dialog."""
    saved = pyqtSignal(str)

    def __init__(self, parent=None, db_id: str = "", db_name: str = "", description: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Form Designer")
        enable_maximize(self)
        self.setMinimumSize(900, 600)
        self.resize(1200, 780)
        self.database_dir = DATABASES_DIR
        self.db_id = db_id or f"new_{date.today().isoformat()}"
        self.db_name = db_name or self.db_id
        self.description = description

        self.symbol_list = SymbolListPanel()
        self.palette = WidgetPalette()
        self.canvas = FormCanvas()
        self.canvas.base_dir = self.database_dir
        self.properties = PropertyEditor(self.symbol_list)
        self.properties.base_dir = self.database_dir
        self.code_editor = CodeEditor()
        self.code_editor.setPlaceholderText("Panel script. Load from file or start from the template.")

        self.canvas.widget_selected.connect(self.on_widget_selected)
        self.canvas.selection_cleared.connect(self.properties.clear)
        self.canvas.geometry_changed.connect(self.properties.set_geometry_values)
        self.canvas.handler_requested.connect(lambda index: self.edit_handler(self.canvas._current_widgets()[index]))
        self.canvas.graphics_view.signal_dropped.connect(self._on_signal_dropped)
        self.properties.properties_changed.connect(self.on_properties_changed)
        self.properties.editing.connect(lambda key: self.canvas.checkpoint(key=("property", id(self.properties.widget_data), key)))
        self.properties.handler_requested.connect(self.edit_handler)
        self.symbol_list.symbol_selected.connect(self._on_symbol_selected)

        left_widget = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_split = QSplitter(Qt.Vertical)
        left_split.addWidget(self.symbol_list)
        left_split.addWidget(self.palette)
        left_split.setSizes([260, 420])
        left_layout.addWidget(left_split)
        left_widget.setLayout(left_layout)

        # Middle: form and Python code editors
        code_page = QWidget()
        code_layout = QVBoxLayout(code_page)
        code_layout.setContentsMargins(0, 0, 0, 0)
        code_bar = QHBoxLayout()
        check_btn = QPushButton("Check syntax")
        check_btn.clicked.connect(self.check_syntax)
        self.syntax_label = QLabel("Ctrl+Space: complete (API, control names, signals). Double-click a control "
                                   "on the Form tab to create its handler.")
        self.syntax_label.setStyleSheet("color: gray;")
        code_bar.addWidget(check_btn)
        code_bar.addWidget(self.syntax_label, 1)
        code_layout.addLayout(code_bar)
        self.uds_panel = UdsFunctionPanel()
        self.uds_panel.insert_requested.connect(self.code_editor.insert_snippet)
        code_split = QSplitter(Qt.Horizontal)
        code_split.addWidget(self.code_editor)
        code_split.addWidget(self.uds_panel)
        code_split.setStretchFactor(0, 1)
        code_split.setSizes([640, 330])
        code_layout.addWidget(code_split, 1)
        self.design_tabs = QTabWidget()
        self.design_tabs.addTab(self.canvas, "Form")
        self.design_tabs.addTab(code_page, "Python script")
        self.design_tabs.currentChanged.connect(self._on_tab_changed)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(True)
        left_widget.setMinimumWidth(0)
        self.design_tabs.setMinimumWidth(0)
        self.properties.setMinimumWidth(0)
        splitter.addWidget(SplitterPanel("Symbols & controls", left_widget, Qt.Horizontal))
        splitter.addWidget(SplitterPanel("Form / Code", self.design_tabs, Qt.Horizontal))
        self.properties_panel = SplitterPanel("Properties", self.properties, Qt.Horizontal)
        splitter.addWidget(self.properties_panel)
        splitter.setSizes([230, 680, 300])

        top_layout = QHBoxLayout()
        self.db_id_edit = QLineEdit(self.db_id)
        self.db_id_edit.setPlaceholderText("Database ID (e.g. engine_2026-09-18)")
        top_layout.addWidget(QLabel("Database ID:"))
        top_layout.addWidget(self.db_id_edit)
        self.db_name_edit = QLineEdit(self.db_name)
        self.db_name_edit.setPlaceholderText("Database name")
        top_layout.addWidget(QLabel("Name:"))
        top_layout.addWidget(self.db_name_edit)
        self.desc_edit = QLineEdit(self.description)
        self.desc_edit.setPlaceholderText("Description")
        top_layout.addWidget(QLabel("Description:"))
        top_layout.addWidget(self.desc_edit)
        top_layout.addWidget(QLabel("DBC path:"))
        self.dbc_path_edit = QLineEdit()
        self.dbc_path_edit.setPlaceholderText("Optional DBC for symbols")
        self.dbc_path_edit.setMinimumWidth(120)
        top_layout.addWidget(self.dbc_path_edit)
        self.symbol_list.dbc_loaded.connect(self.dbc_path_edit.setText)

        btn_layout = QHBoxLayout()
        for text, slot in (("Save", self.save), ("Load", self.load), ("New", self.new_form)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            btn_layout.addWidget(button)
        test_btn = QPushButton("Test panel...")
        test_btn.setToolTip("Run this panel and its script against the simulated ECU on a virtual CAN bus")
        test_btn.clicked.connect(self.test_panel)
        btn_layout.addWidget(test_btn)
        btn_layout.addStretch()

        layout = QVBoxLayout()
        layout.addLayout(top_layout)
        layout.addLayout(btn_layout)
        layout.addWidget(splitter)
        self.setLayout(layout)
        self._load_script()

    # --- script -------------------------------------------------------------------------

    def _script_path(self) -> Path:
        """Path to the database script file for current db_id."""
        db_id = self.db_id_edit.text().strip() or "new"
        return self.database_dir / f"{db_id}_script.py"

    def _load_script(self):
        path = self._script_path()
        if path.exists():
            try:
                self.code_editor.setPlainText(path.read_text(encoding="utf-8"))
            except Exception as e:
                self.code_editor.setPlainText(SCRIPT_TEMPLATE)
                self.code_editor.appendPlainText(f"\n# Error loading script: {e}")
        else:
            self.code_editor.setPlainText(SCRIPT_TEMPLATE)

    def _save_script(self) -> bool:
        path = self._script_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(self.code_editor.toPlainText(), encoding="utf-8")
            return True
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save script: {e}")
            return False

    def check_syntax(self):
        ok, message, line = self.code_editor.check_syntax(str(self._script_path()))
        self.syntax_label.setText(message)
        self.syntax_label.setStyleSheet("color: green;" if ok else "color: red;")
        if line:
            self.code_editor.go_to_line(line)
        return ok

    def _on_tab_changed(self, index):
        # Control properties only make sense next to the form; the script gets the room instead.
        self.properties_panel.setVisible(self.design_tabs.widget(index) is self.canvas)
        if self.design_tabs.widget(index) is not self.canvas:
            words = []
            for page in self.canvas.pages:
                for data in page["widgets"]:
                    words += [control_name(data), str(data.get("handler", ""))]
            words += self.symbol_list.get_dbc_signals()
            self.code_editor.set_completion_words(words)

    def edit_handler(self, data):
        """Open (creating if needed) the handler function of a control in the Python script."""
        control = CONTROLS.get(data.get("type"), CONTROLS["label"])
        if not control.interactive:
            QMessageBox.information(self, "Handler", f"A {control.label} only displays values, so it has no handler.\n"
                                                     "Set it from the script with api.ui.set_value().")
            return
        name = str(data.get("handler") or "").strip() or default_handler_name(data)
        if not name.isidentifier():
            QMessageBox.warning(self, "Handler", f"'{name}' is not a valid Python function name.")
            return
        if data.get("handler") != name:
            self.canvas.checkpoint()
            data["handler"] = name
            if self.properties.widget_data is data:
                self.properties.load_widget(data, len(self.canvas.selection))
        code = self.code_editor.toPlainText()
        match = re.search(rf"^def {re.escape(name)}\s*\(", code, re.M)
        if match is None:
            label = control_name(data)
            stub = (f"\n\ndef {name}(api, value):\n"
                    f'    """{control.label} "{label}" {control.event}."""\n'
                    f'    api.log(f"{label}: {{value}}")\n')
            if not code.endswith("\n"):
                stub = "\n" + stub
            self.code_editor.moveCursor(QTextCursor.End)
            self.code_editor.insertPlainText(stub)
            code = self.code_editor.toPlainText()
            match = re.search(rf"^def {re.escape(name)}\s*\(", code, re.M)
        line = code[:match.start()].count("\n") + 1
        self.design_tabs.setCurrentIndex(1)
        self.code_editor.go_to_line(line + 2, 4)

    # --- canvas events --------------------------------------------------------------------------

    def on_widget_selected(self, index: int, data: dict):
        self.properties.load_widget(data, len(self.canvas.selection))

    def _on_symbol_selected(self, symbol: str):
        """Set current widget binding to the double-clicked DBC symbol."""
        w = self.canvas._current_widgets()
        idx = self.canvas.selected_index
        if 0 <= idx < len(w) and symbol:
            self.canvas.checkpoint()
            w[idx]["binding_type"] = BINDING_TYPE_DBC
            w[idx]["binding_value"] = symbol
            w[idx]["variable"] = symbol
            self.properties.load_widget(w[idx], len(self.canvas.selection))
            self.canvas._rebuild()

    def _on_signal_dropped(self, name, x, y, as_input):
        self.canvas.add_signal_control(name, x, y, self.symbol_list.signal_info(name), as_input)

    def on_properties_changed(self, data: dict):
        for i, w in enumerate(self.canvas._current_widgets()):
            if w is data:
                self.canvas.update_widget(i, data)
                break

    # --- files ----------------------------------------------------------------------------------

    def new_form(self):
        self.canvas.load_from_data({})
        self.properties.clear()
        self.db_id_edit.setText(f"new_{date.today().isoformat()}")
        self.db_name_edit.setText(f"new_{date.today().isoformat()}")
        self.desc_edit.clear()
        self.dbc_path_edit.clear()
        self.symbol_list._dbc_path = None
        self.symbol_list._dbc_db = None
        self.symbol_list.symbol_tree.clear()
        self.code_editor.setPlainText(SCRIPT_TEMPLATE)

    def load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Database", str(self.database_dir), "XML (*.xml)")
        if not path:
            return
        try:
            root = ET.parse(path).getroot()
            self.database_dir = Path(path).resolve().parent
            self.canvas.base_dir = self.properties.base_dir = self.database_dir
            self.db_id = Path(path).stem
            self.db_name = root.get("name", self.db_id)
            self.db_id_edit.setText(self.db_id)
            self.db_name_edit.setText(self.db_name)
            desc = root.find("description")
            self.description = desc.text.strip() if desc is not None and desc.text else ""
            self.desc_edit.setText(self.description)
            dbc_el = root.find("dbc_path")
            dbc_path = root.get("dbc_path", "") or (dbc_el.text.strip() if dbc_el is not None and dbc_el.text else "")
            if dbc_path and not Path(dbc_path).is_absolute():
                dbc_path = str(self.database_dir / dbc_path)
            if dbc_path and Path(dbc_path).exists():
                self.dbc_path_edit.setText(dbc_path)
                self.symbol_list.load_dbc_path(dbc_path)
            else:
                self.dbc_path_edit.clear()

            pages_el = root.find("pages")
            if pages_el is not None:
                data = {"pages": []}
                for page_el in pages_el.findall("page"):
                    # Document order is the z-order (group boxes stay behind their contents).
                    widgets = [parse_widget(elem) for elem in page_el.iter() if elem.tag in WIDGET_GROUPS]
                    data["pages"].append({"name": page_el.get("name", "Page"), "widgets": widgets})
                self.canvas.load_from_data(data)
            else:
                data = {tag: [] for tag in ["buttons", "values", "checkboxes", "sliders", "labels"]}
                for tag in data:
                    for elem in root.findall(".//" + {"checkboxes": "checkbox"}.get(tag, tag[:-1])):
                        data[tag].append(parse_widget(elem))
                self.canvas.load_from_data(data)
            self.properties.clear()
            self._load_script()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load: {e}")

    def _build_root(self, db_name, description):
        data = self.canvas.get_data()
        dbc_path = self.dbc_path_edit.text().strip()
        root = ET.Element("application_database", name=db_name)
        if dbc_path:
            root.set("dbc_path", dbc_path)
        if description:
            ET.SubElement(root, "description").text = description
        pages_el = ET.SubElement(root, "pages")
        for page in data.get("pages", []):
            page_el = ET.SubElement(pages_el, "page", name=page.get("name", "Page"))
            for item in page.get("widgets", []):
                key = item.get("type", "")
                if key not in WIDGET_GROUPS:
                    continue
                attrs = {k: str(v) for k, v in item.items() if k not in ("data_bytes", "kind") and v is not None}
                if "binding_value" in item and item.get("binding_type") == BINDING_TYPE_SCRIPT:
                    attrs["variable"] = str(item.get("binding_value", item.get("variable", "")))
                ET.SubElement(page_el, key, attrs)
        return root

    def save(self):
        db_id = self.db_id_edit.text().strip() or "new"
        db_name = self.db_name_edit.text().strip() or db_id
        description = self.desc_edit.text().strip()

        if Path(db_id).name != db_id or any(c in db_id for c in '/\\:*?"<>|'):
            QMessageBox.warning(self, "Invalid database ID", "Use a filename stem such as engine_2026-09-18.")
            return
        path = self.database_dir
        path.mkdir(parents=True, exist_ok=True)
        filepath = path / f"{db_id}.xml"
        root = self._build_root(db_name, description)
        tree = ET.ElementTree(root)
        ET.indent(tree, space="    ")
        try:
            for elem in root.iter():
                if elem.tag in WIDGET_GROUPS:
                    parse_widget(elem)
            compile(self.code_editor.toPlainText(), str(self._script_path()), "exec")
            tree.write(filepath, encoding="utf-8", xml_declaration=True, default_namespace=None)
            if self._save_script():
                QMessageBox.information(self, "Saved", f"Saved to {filepath}\nScript: {self._script_path()}")
            else:
                QMessageBox.information(self, "Saved", f"Form saved to {filepath}")
            self.saved.emit(str(filepath))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save: {e}")

    def test_panel(self, simulate_ecu=True):
        """Run the form as it is now (no need to save) in a test window."""
        if not self.check_syntax():
            self.design_tabs.setCurrentIndex(1)
            return None
        try:
            root = self._build_root(self.db_name_edit.text().strip() or "Test", self.desc_edit.text().strip())
            temp = Path(tempfile.mkdtemp()) / "test_panel.xml"
            ET.ElementTree(root).write(temp, encoding="utf-8", xml_declaration=True)
            database = parse_application_database(temp)
            database["source_path"] = str(self.database_dir / "test_panel.xml")  # relative DBC/image paths
            dialog = TestPanelDialog(database, self.code_editor.toPlainText(), simulate_ecu, self)
        except Exception as e:
            QMessageBox.critical(self, "Test panel", f"Cannot run the panel: {e}")
            return None
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.show()
        return dialog
