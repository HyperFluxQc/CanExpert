"""
Form Designer

Visual editor for application database UIs: drag widgets onto pages,
set properties, and save to XML (+ optional script). Used to create
the databases loaded by the main application.
"""
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton,
    QGroupBox, QFormLayout, QLineEdit, QSpinBox, QScrollArea, QFrame,
    QSplitter, QMessageBox, QFileDialog, QCheckBox, QSlider, QComboBox,
    QDoubleSpinBox, QGraphicsScene, QGraphicsView, QGraphicsProxyWidget,
    QPlainTextEdit, QTabWidget, QMenu, QAction, QInputDialog, QApplication,
    QGraphicsItem, QTreeWidget, QTreeWidgetItem, QProgressBar,
)
from PyQt5.QtCore import Qt, pyqtSignal, QPointF, QMimeData, QTimer
from PyQt5.QtGui import QFont, QColor, QDrag, QCursor

try:
    from database_api import SCRIPT_TEMPLATE
except ImportError:
    SCRIPT_TEMPLATE = '"""Database script - define DatabaseMainFunction(api)."""\n\ndef DatabaseMainFunction(api):\n    pass\n'

from splitter_panel import SplitterPanel

try:
    import cantools
    HAS_CANTOOLS = True
except ImportError:
    HAS_CANTOOLS = False

# MIME type for drag-and-drop widget type
WIDGET_TYPE_MIME = "application/x-ezcan-widget-type"
BINDING_TYPE_SCRIPT = "script"
BINDING_TYPE_DBC = "dbc"


class DraggablePaletteItem(QLabel):
    """A palette item that can be dragged onto the canvas."""
    def __init__(self, wtype: str, label: str):
        super().__init__(f"▸ {label}")
        self.widget_type = wtype
        self.setStyleSheet(
            "DraggablePaletteItem { padding: 6px 10px; border: 1px solid #888; "
            "border-radius: 4px; background: #e0e0e0; color: #222; }"
            "DraggablePaletteItem:hover { background: #c8d4e8; border-color: #0066cc; color: #111; }"
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
    """Palette of widget types - drag and drop onto the form."""
    add_clicked = pyqtSignal(str)

    def __init__(self):
        super().__init__("Widget Palette")
        self.setToolTip("Drag items onto the form to add them.")
        layout = QVBoxLayout()
        for wtype, label in [
            ("button", "Button"),
            ("value", "Value Display"),
            ("checkbox", "Checkbox"),
            ("slider", "Slider"),
            ("label", "Label"),
            ("gauge", "Gauge"),
            ("progress_bar", "Progress Bar"),
            ("led", "LED"),
            ("combo", "Combo Box"),
            ("io_box", "I/O Box"),
        ]:
            item = DraggablePaletteItem(wtype, label)
            layout.addWidget(item)
        layout.addStretch()
        self.setLayout(layout)


class SymbolListPanel(QGroupBox):
    """Left panel: DBC signals and script variables (Vector-style symbol list)."""
    symbol_selected = pyqtSignal(str)  # "Message.Signal" or variable name
    dbc_loaded = pyqtSignal(str)  # path when DBC is loaded

    def __init__(self):
        super().__init__("Symbols")
        self.setToolTip("DBC signals and script variables. Select and bind in Properties.")
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
        self.symbol_tree = QTreeWidget()
        self.symbol_tree.setHeaderLabels(["Symbol"])
        self.symbol_tree.setColumnWidth(0, 180)
        self.symbol_tree.header().setContextMenuPolicy(Qt.CustomContextMenu)
        self.symbol_tree.header().customContextMenuRequested.connect(self._show_symbol_tree_column_menu)
        self.symbol_tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.symbol_tree)
        layout.addWidget(QLabel("Script variables: set in Properties\n(Binding type = Script, value = name)"))
        self.setLayout(layout)
        self._dbc_db = None
        self._dbc_path = None

    def _load_dbc(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load DBC", "", "DBC (*.dbc);;All (*.*)"
        )
        if path:
            self.load_dbc_path(path)

    def load_dbc_path(self, path: str):
        if not HAS_CANTOOLS:
            return
        from pathlib import Path
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
        for msg in self._dbc_db.messages:
            parent = QTreeWidgetItem(self.symbol_tree, [msg.name])
            for sig in msg.signals:
                display_name = f"{msg.name}.{sig.name}"
                child = QTreeWidgetItem(parent, [display_name])
                child.setData(0, Qt.UserRole, display_name)
            parent.setData(0, Qt.UserRole, msg.name)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        symbol = item.data(0, Qt.UserRole)
        if symbol and "." in symbol:
            self.symbol_selected.emit(symbol)

    def _show_symbol_tree_column_menu(self, pos):
        """Context menu on Symbol tree header: toggle column visibility."""
        menu = QMenu(self)
        for col in range(self.symbol_tree.columnCount()):
            act = menu.addAction("Show 'Symbol'")
            act.setCheckable(True)
            act.setChecked(not self.symbol_tree.isColumnHidden(col))
            act.triggered.connect(lambda checked, c=col: self.symbol_tree.setColumnHidden(c, not checked))
        menu.exec_(self.symbol_tree.header().mapToGlobal(pos))

    def get_dbc_path(self) -> str:
        return self._dbc_path or ""

    def get_dbc_signals(self) -> list:
        """Return list of 'Message.Signal' strings for property panel combo."""
        if not self._dbc_db:
            return []
        out = []
        for msg in self._dbc_db.messages:
            for sig in msg.signals:
                out.append(f"{msg.name}.{sig.name}")
        return out


class MovableProxyWidget(QGraphicsProxyWidget):
    """Proxy that allows moving the widget on the scene; manual drag to move; click to select."""
    position_changed = pyqtSignal(int, float, float)
    clicked = pyqtSignal(int)
    right_clicked = pyqtSignal(int)

    def __init__(self, widget_index: int, parent=None):
        super().__init__(parent)
        self._widget_index = widget_index
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges)
        self._drag_start_scene = None
        self._drag_start_pos = None
        self._did_drag = False

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.right_clicked.emit(self._widget_index)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self._drag_start_scene = event.scenePos()
            self._drag_start_pos = self.pos()
            self._did_drag = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self._drag_start_scene is not None:
            delta = event.scenePos() - self._drag_start_scene
            if not self._did_drag:
                if abs(delta.x()) > 3 or abs(delta.y()) > 3:
                    self._did_drag = True
            if self._did_drag:
                new_pos = self._drag_start_pos + QPointF(delta.x(), delta.y())
                new_pos.setX(max(0, new_pos.x()))
                new_pos.setY(max(0, new_pos.y()))
                self.setPos(new_pos)
                # position_changed is emitted by itemChange(ItemPositionHasChanged)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self._did_drag:
                self.clicked.emit(self._widget_index)
            self._drag_start_scene = None
            self._drag_start_pos = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            p = self.pos()
            self.position_changed.emit(self._widget_index, p.x(), p.y())
        return super().itemChange(change, value)


class DroppableGraphicsView(QGraphicsView):
    """Graphics view that accepts widget-type drops and emits drop position in scene coords."""
    widget_dropped = pyqtSignal(str, int, int)

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(WIDGET_TYPE_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(WIDGET_TYPE_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event):
        if not event.mimeData().hasFormat(WIDGET_TYPE_MIME):
            return
        wtype = event.mimeData().data(WIDGET_TYPE_MIME).data().decode("utf-8")
        pos = self.mapToScene(event.pos())
        x = max(0, int(pos.x()))
        y = max(0, int(pos.y()))
        self.widget_dropped.emit(wtype, x, y)
        event.acceptProposedAction()


class PropertyEditor(QGroupBox):
    """Editor for selected widget properties (Vector-style: binding + common + type-specific)."""
    properties_changed = pyqtSignal(dict)

    def __init__(self, symbol_panel=None):
        super().__init__("Properties")
        self.symbol_panel = symbol_panel
        self.layout = QFormLayout()
        self.widget_data = None
        self.controls = {}
        self.setLayout(self.layout)

    def clear(self):
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.controls.clear()
        self.widget_data = None

    def set_symbol_panel(self, panel):
        self.symbol_panel = panel

    def load_widget(self, data: dict):
        self.clear()
        self.widget_data = data
        if not data:
            return

        wtype = data.get("type", "button")

        # Binding (Vector-style: type + value)
        binding_type = data.get("binding_type", BINDING_TYPE_SCRIPT)
        if data.get("binding_value") is not None:
            binding_val = str(data.get("binding_value", ""))
        else:
            binding_val = str(data.get("variable", ""))
        self._add_combo("binding_type", "Binding type", [BINDING_TYPE_SCRIPT, BINDING_TYPE_DBC], binding_type)
        dbc_signals = []
        if self.symbol_panel:
            dbc_signals = self.symbol_panel.get_dbc_signals()
        if dbc_signals:
            ctrl = QComboBox()
            ctrl.setEditable(True)
            ctrl.addItems([""] + dbc_signals)
            ctrl.setCurrentText(binding_val)
            ctrl.currentTextChanged.connect(lambda v, k="binding_value": self._on_change(k, v))
            self.controls["binding_value"] = ("str", ctrl)
            self.layout.addRow("Binding (DBC or variable)", ctrl)
        else:
            self._add_line("binding_value", "Binding (variable or Message.Signal)", binding_val)

        # Common: id, label, position, size
        self._add_line("id", "ID", str(data.get("id", "1")))
        if wtype != "label":
            self._add_line("label", "Label", str(data.get("label", "New Widget")))
        self._add_spin("x", "X", data.get("x", 0), 0, 10000)
        self._add_spin("y", "Y", data.get("y", 0), 0, 10000)
        self._add_spin("width", "Width", data.get("width", 100), 20, 2000)
        self._add_spin("height", "Height", data.get("height", 30), 10, 500)

        if wtype == "value":
            self._add_line("unit", "Unit", str(data.get("unit", "")))
            self._add_combo("value_type", "Display type", ["float", "integer"], data.get("type", data.get("value_type", "float")))

        elif wtype == "slider":
            self._add_spin("min", "Min", data.get("min", 0), -32768, 32767)
            self._add_spin("max", "Max", data.get("max", 100), -32768, 32767)

        elif wtype == "label":
            self._add_line("text", "Text", str(data.get("text", "Label")))

        elif wtype == "gauge":
            self._add_spin("min", "Min", data.get("min", 0), -10000, 10000)
            self._add_spin("max", "Max", data.get("max", 100), -10000, 10000)
            self._add_line("unit", "Unit", str(data.get("unit", "")))

        elif wtype == "progress_bar":
            self._add_spin("min", "Min", data.get("min", 0), -32768, 32767)
            self._add_spin("max", "Max", data.get("max", 100), -32768, 32767)

        elif wtype == "led":
            self._add_line("on_text", "On text", str(data.get("on_text", "ON")))
            self._add_line("off_text", "Off text", str(data.get("off_text", "OFF")))

        elif wtype == "combo":
            self._add_line("items", "Items (comma-separated)", str(data.get("items", "")))

        elif wtype == "io_box":
            self._add_line("unit", "Unit", str(data.get("unit", "")))
            self._add_combo("value_type", "Value type", ["float", "integer", "string"], data.get("value_type", "float"))

    def _add_line(self, key: str, label: str, value: str):
        ctrl = QLineEdit(value)
        ctrl.textChanged.connect(lambda v, k=key: self._on_change(k, v))
        self.controls[key] = ("str", ctrl)
        self.layout.addRow(label, ctrl)

    def _add_spin(self, key: str, label: str, value: int, min_val: int, max_val: int):
        ctrl = QSpinBox()
        ctrl.setRange(min_val, max_val)
        ctrl.setValue(value)
        ctrl.valueChanged.connect(lambda v, k=key: self._on_change(k, v))
        self.controls[key] = ("int", ctrl)
        self.layout.addRow(label, ctrl)

    def _add_double(self, key: str, label: str, value: float):
        ctrl = QDoubleSpinBox()
        ctrl.setRange(-1e9, 1e9)
        ctrl.setValue(value)
        ctrl.valueChanged.connect(lambda v, k=key: self._on_change(k, v))
        self.controls[key] = ("float", ctrl)
        self.layout.addRow(label, ctrl)

    def _add_combo(self, key: str, label: str, options: list, value: str):
        ctrl = QComboBox()
        ctrl.addItems(options)
        ctrl.setCurrentText(value)
        ctrl.currentTextChanged.connect(lambda v, k=key: self._on_change(k, v))
        self.controls[key] = ("str", ctrl)
        self.layout.addRow(label, ctrl)

    def _on_change(self, key: str, value):
        if not self.widget_data:
            return
        if key == "value_type":
            self.widget_data["type"] = str(value)
            if self.widget_data.get("type") == "value":
                self.widget_data["value_type"] = str(value)
        elif key == "text":
            self.widget_data["text"] = str(value)
        elif key == "binding_type":
            self.widget_data["binding_type"] = str(value)
        elif key == "binding_value":
            self.widget_data["binding_value"] = str(value) if value else ""
            if self.widget_data.get("binding_type", BINDING_TYPE_SCRIPT) == BINDING_TYPE_SCRIPT:
                self.widget_data["variable"] = self.widget_data["binding_value"]
        else:
            self.widget_data[key] = value
        self.properties_changed.emit(self.widget_data)

    def _bytes_to_hex(self, data: list) -> str:
        return " ".join(f"{b:02X}" for b in (data or [0] * 8)[:8])

    def get_data(self) -> dict:
        return self.widget_data


class FormCanvas(QGroupBox):
    """Canvas showing the form with widgets. Click to select."""
    widget_selected = pyqtSignal(int, dict)

    def __init__(self):
        super().__init__("Form Preview")
        self.pages = [{"name": "Main", "widgets": []}]
        self.current_page_index = 0
        self.selected_index = -1
        self._widget_clipboard = None  # for Copy/Cut/Paste

        def _current_widgets():
            return self.pages[self.current_page_index]["widgets"]
        self._current_widgets = lambda: self.pages[self.current_page_index]["widgets"]

        main_layout = QVBoxLayout()

        # Page bar: clickable page names + Add page
        self.page_bar = QWidget()
        page_bar_layout = QHBoxLayout()
        self.page_buttons = []
        add_page_btn = QPushButton("+ Add page")
        add_page_btn.clicked.connect(self._add_page)
        page_bar_layout.addWidget(add_page_btn)
        page_bar_layout.addStretch()
        self.page_bar.setLayout(page_bar_layout)
        main_layout.addWidget(self.page_bar)
        self._rebuild_page_bar()

        # Content: database page only (drag from palette, left-click to move, right-click for menu)
        content = QWidget()
        content_layout = QHBoxLayout()
        self.scene = QGraphicsScene(0, 0, 800, 600)
        self.scene.setBackgroundBrush(QColor(245, 245, 245))
        self.graphics_view = DroppableGraphicsView(self.scene)
        self.graphics_view.setMinimumSize(400, 300)
        self.graphics_view.setToolTip("Drag widgets from the palette. Left-click and drag to move. Right-click for Copy/Cut/Paste/Delete/Size/Variable.")
        self.graphics_view.widget_dropped.connect(self.add_widget_at)
        self.graphics_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.graphics_view.customContextMenuRequested.connect(self._show_canvas_context_menu)
        content_layout.addWidget(self.graphics_view, 1)
        content.setLayout(content_layout)
        main_layout.addWidget(content)
        self.setLayout(main_layout)

    def _rebuild_page_bar(self):
        for btn in self.page_buttons:
            btn.deleteLater()
        self.page_buttons.clear()
        bar = self.page_bar.layout()
        for i, page in enumerate(self.pages):
            name = page.get("name", "Page")
            btn = QPushButton(name)
            btn.setProperty("page_index", i)
            btn.setCheckable(True)
            btn.setChecked(i == self.current_page_index)
            btn.clicked.connect(lambda checked, idx=i: self._switch_page(idx))
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(lambda pos, idx=i, b=btn: self._show_page_context_menu(idx, b, pos))
            self.page_buttons.append(btn)
            bar.insertWidget(i, btn)

    def _add_page(self):
        n = len(self.pages) + 1
        self.pages.append({"name": f"Page {n}", "widgets": []})
        self.current_page_index = len(self.pages) - 1
        self.selected_index = -1
        self._rebuild_page_bar()
        self._rebuild()

    def _show_page_context_menu(self, page_index: int, button: QPushButton, pos):
        menu = QMenu(self)
        remove_act = menu.addAction("Remove page")
        remove_act.setEnabled(len(self.pages) > 1)
        action = menu.exec_(button.mapToGlobal(pos))
        if action is remove_act and len(self.pages) > 1:
            self._remove_page(page_index)

    def _remove_page(self, index: int):
        if index < 0 or index >= len(self.pages) or len(self.pages) <= 1:
            return
        del self.pages[index]
        if self.current_page_index == index:
            self.current_page_index = max(0, index - 1)
        elif self.current_page_index > index:
            self.current_page_index -= 1
        self.selected_index = -1
        self._rebuild_page_bar()
        self._rebuild()

    def _switch_page(self, index: int):
        if 0 <= index < len(self.pages):
            self.current_page_index = index
            self.selected_index = -1
            for i, btn in enumerate(self.page_buttons):
                btn.setChecked(i == index)
            self._rebuild()

    def add_widget(self, wtype: str) -> dict:
        return self.add_widget_at(wtype, 0, 0)

    def add_widget_at(self, wtype: str, x: int, y: int) -> dict:
        data = self._default_data(wtype)
        data["x"] = x
        data["y"] = y
        self._current_widgets().append(data)
        self._rebuild()
        return data

    def _default_data(self, wtype: str) -> dict:
        n = len(self._current_widgets()) + 1
        base = {"x": 0, "y": 0, "width": 100, "height": 30, "variable": "", "binding_type": BINDING_TYPE_SCRIPT, "binding_value": ""}
        if wtype == "button":
            return {**base, "type": "button", "id": str(n), "label": f"Button {n}"}
        elif wtype == "value":
            return {**base, "type": "value", "id": str(n), "label": f"Value {n}", "unit": "", "value_type": "float"}
        elif wtype == "checkbox":
            return {**base, "type": "checkbox", "id": str(n), "label": f"Checkbox {n}"}
        elif wtype == "slider":
            return {**base, "type": "slider", "id": str(n), "label": f"Slider {n}", "min": 0, "max": 100}
        elif wtype == "label":
            return {**base, "type": "label", "id": str(n), "text": f"Label {n}"}
        elif wtype == "gauge":
            return {**base, "type": "gauge", "id": str(n), "label": f"Gauge {n}", "min": 0, "max": 100, "unit": ""}
        elif wtype == "progress_bar":
            return {**base, "type": "progress_bar", "id": str(n), "label": f"Progress {n}", "min": 0, "max": 100}
        elif wtype == "led":
            return {**base, "type": "led", "id": str(n), "label": f"LED {n}", "on_text": "ON", "off_text": "OFF"}
        elif wtype == "combo":
            return {**base, "type": "combo", "id": str(n), "label": f"Combo {n}", "items": ""}
        elif wtype == "io_box":
            return {**base, "type": "io_box", "id": str(n), "label": f"I/O {n}", "unit": "", "value_type": "float"}
        return {**base, "type": wtype, "id": str(n), "label": f"Widget {n}"}

    def update_widget(self, index: int, data: dict):
        w = self._current_widgets()
        if 0 <= index < len(w):
            w[index] = data
            self._rebuild()

    def remove_widget(self, index: int):
        w = self._current_widgets()
        if 0 <= index < len(w):
            del w[index]
            self.selected_index = -1
            self._rebuild()

    def move_up(self, index: int):
        w = self._current_widgets()
        if index > 0:
            w[index], w[index - 1] = w[index - 1], w[index]
            self.selected_index = index - 1
            self._rebuild()

    def move_down(self, index: int):
        w = self._current_widgets()
        if 0 <= index < len(w) - 1:
            w[index], w[index + 1] = w[index + 1], w[index]
            self.selected_index = index + 1
            self._rebuild()

    def _rebuild(self):
        self.scene.clear()
        widgets = self._current_widgets()
        for i, data in enumerate(widgets):
            px, py = data.get("x", 0), data.get("y", 0)
            w = self._make_preview_widget(data, selected=(i == self.selected_index))
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
            proxy = MovableProxyWidget(i)
            proxy.setWidget(w)
            proxy.setCursor(Qt.SizeAllCursor)
            proxy.clicked.connect(self._on_select)
            proxy.right_clicked.connect(lambda idx: self._show_widget_context_menu(idx, QCursor.pos()))
            proxy.position_changed.connect(self._on_widget_moved)
            proxy.setPos(px, py)
            proxy.setZValue(i)
            self.scene.addItem(proxy)

    def _make_preview_widget(self, data: dict, selected: bool = False):
        """Build the actual widget type for the canvas preview (button looks like button, etc.)."""
        t = data.get("type", "button")
        w_ = data.get("width", 100)
        h_ = data.get("height", 30)
        sel_style = "border: 2px solid #0066cc;"
        norm_style = "border: 1px solid #ccc;"
        base_style = (sel_style if selected else norm_style) + " color: #222; min-width: 0;"

        if t == "button":
            btn = QPushButton(data.get("label", "Button"))
            btn.setFixedSize(w_, h_)
            btn.setStyleSheet(base_style)
            return btn
        if t == "value":
            lbl = QLabel("--")
            lbl.setFixedSize(w_, h_)
            lbl.setStyleSheet(base_style + " background: #f8f8f8;")
            lbl.setAlignment(Qt.AlignCenter)
            return lbl
        if t == "checkbox":
            cb = QCheckBox(data.get("label", "Checkbox"))
            cb.setFixedSize(w_, h_)
            cb.setStyleSheet(base_style)
            return cb
        if t == "slider":
            sl = QSlider(Qt.Horizontal)
            sl.setRange(data.get("min", 0), data.get("max", 100))
            sl.setValue(0)
            sl.setFixedSize(w_, h_)
            sl.setStyleSheet(base_style)
            return sl
        if t == "label":
            lbl = QLabel(data.get("text", "Label"))
            lbl.setFixedSize(w_, h_)
            lbl.setStyleSheet(base_style)
            return lbl
        if t == "gauge":
            lbl = QLabel("--")
            lbl.setFixedSize(w_, h_)
            lbl.setStyleSheet(base_style + " background: #f0f0f0; font-weight: bold;")
            lbl.setAlignment(Qt.AlignCenter)
            return lbl
        if t == "progress_bar":
            prog = QProgressBar()
            prog.setRange(data.get("min", 0), data.get("max", 100))
            prog.setValue(0)
            prog.setFixedSize(w_, h_)
            prog.setStyleSheet(base_style)
            return prog
        if t == "led":
            lbl = QLabel(data.get("off_text", "OFF"))
            lbl.setFixedSize(w_, h_)
            lbl.setStyleSheet(base_style + " background: #444; color: #fff;")
            lbl.setAlignment(Qt.AlignCenter)
            return lbl
        if t == "combo":
            combo = QComboBox()
            items = [s.strip() for s in (data.get("items") or "").split(",") if s.strip()]
            if items:
                combo.addItems(items)
            combo.setFixedSize(w_, h_)
            combo.setStyleSheet(base_style)
            return combo
        if t == "io_box":
            le = QLineEdit()
            le.setPlaceholderText(data.get("label", ""))
            le.setFixedSize(w_, h_)
            le.setStyleSheet(base_style)
            return le
        lbl = QLabel(data.get("label", "?"))
        lbl.setFixedSize(w_, h_)
        lbl.setStyleSheet(base_style)
        return lbl

    def _preview_label(self, data: dict) -> str:
        t = data.get("type", "")
        if t == "button":
            return f"[Button] {data.get('label', '')}"
        if t == "value":
            return f"[Value] {data.get('label', '')} ({data.get('unit', '')})"
        if t == "checkbox":
            return f"[Checkbox] {data.get('label', '')}"
        if t == "slider":
            return f"[Slider] {data.get('label', '')}"
        if t == "label":
            return f"[Label] {data.get('text', '')}"
        if t == "gauge":
            return f"[Gauge] {data.get('label', '')}"
        if t == "progress_bar":
            return f"[Progress] {data.get('label', '')}"
        if t == "led":
            return f"[LED] {data.get('label', '')}"
        if t == "combo":
            return f"[Combo] {data.get('label', '')}"
        if t == "io_box":
            return f"[I/O] {data.get('label', '')}"
        return str(data.get("label", ""))

    def _on_select(self, index: int):
        self.selected_index = index
        # Defer rebuild so we don't delete the proxy while it's still in mousePressEvent
        QTimer.singleShot(0, self._rebuild)
        w = self._current_widgets()
        if 0 <= index < len(w):
            self.widget_selected.emit(index, w[index])

    def _on_move_up(self, index: int):
        self.move_up(index)
        w = self._current_widgets()
        if 0 <= self.selected_index < len(w):
            self.widget_selected.emit(self.selected_index, w[self.selected_index])

    def _on_move_down(self, index: int):
        self.move_down(index)
        w = self._current_widgets()
        if 0 <= self.selected_index < len(w):
            self.widget_selected.emit(self.selected_index, w[self.selected_index])

    def _on_delete(self, index: int):
        self.remove_widget(index)

    def _on_widget_moved(self, index: int, x: float, y: float):
        """Update stored x,y when user drags a widget on the scene (no rebuild to avoid interrupting drag)."""
        w = self._current_widgets()
        if 0 <= index < len(w):
            w[index]["x"] = max(0, int(x))
            w[index]["y"] = max(0, int(y))

    def _show_widget_context_menu(self, index: int, global_pos):
        """Show right-click menu for a widget: Copy, Cut, Paste, Delete, Change size, Variable."""
        w = self._current_widgets()
        if index < 0 or index >= len(w):
            return
        data = w[index]
        menu = QMenu(self)
        copy_act = menu.addAction("Copy")
        cut_act = menu.addAction("Cut")
        paste_act = menu.addAction("Paste")
        paste_act.setEnabled(bool(self._widget_clipboard))
        menu.addSeparator()
        del_act = menu.addAction("Delete")
        menu.addSeparator()
        size_act = menu.addAction("Change size...")
        var_act = menu.addAction("Binding...")
        choice = menu.exec_(global_pos)
        if choice == copy_act:
            self._widget_clipboard = {k: v for k, v in data.items() if k not in ("data_bytes", "can_id", "data")}
            if isinstance(self._widget_clipboard.get("variable"), str):
                pass
        elif choice == cut_act:
            self._widget_clipboard = {k: v for k, v in data.items() if k not in ("data_bytes", "can_id", "data")}
            self.remove_widget(index)
        elif choice == paste_act and self._widget_clipboard:
            # Paste near this widget
            x = data.get("x", 0) + 15
            y = data.get("y", 0) + 15
            self.paste_at(x, y)
        elif choice == del_act:
            self.remove_widget(index)
        elif choice == size_act:
            self._dialog_change_size(index, data)
        elif choice == var_act:
            self._dialog_variable(index, data)

    def _dialog_change_size(self, index: int, data: dict):
        w = self._current_widgets()
        if index < 0 or index >= len(w):
            return
        width, ok1 = QInputDialog.getInt(self, "Change size", "Width:", data.get("width", 100), 20, 2000)
        if not ok1:
            return
        height, ok2 = QInputDialog.getInt(self, "Change size", "Height:", data.get("height", 30), 10, 500)
        if ok2:
            w[index]["width"] = width
            w[index]["height"] = height
            self._rebuild()
            if self.selected_index == index:
                self.widget_selected.emit(index, w[index])

    def _dialog_variable(self, index: int, data: dict):
        w = self._current_widgets()
        if index < 0 or index >= len(w):
            return
        name, ok = QInputDialog.getText(
            self, "Binding",
            "Variable or DBC signal (e.g. Start or Message.Signal):",
            QLineEdit.Normal,
            data.get("binding_value", data.get("variable", ""))
        )
        if ok:
            val = name.strip()
            w[index]["binding_value"] = val
            w[index]["variable"] = val
            w[index]["binding_type"] = BINDING_TYPE_SCRIPT if "." not in val or " " in val else BINDING_TYPE_DBC
            self._rebuild()
            if self.selected_index == index:
                self.widget_selected.emit(index, w[index])

    def _show_canvas_context_menu(self, view_pos):
        """Right-click on empty canvas: Paste if clipboard has a widget."""
        menu = QMenu(self)
        if self._widget_clipboard:
            paste_act = menu.addAction("Paste")
            choice = menu.exec_(self.graphics_view.mapToGlobal(view_pos))
            if choice == paste_act:
                pos = self.graphics_view.mapToScene(view_pos)
                self.paste_at(int(pos.x()), int(pos.y()))
        # else no menu or just "Paste" disabled

    def paste_at(self, x: int, y: int):
        """Paste clipboard widget at (x, y)."""
        if not self._widget_clipboard:
            return
        data = dict(self._widget_clipboard)
        data["x"] = max(0, x)
        data["y"] = max(0, y)
        n = len(self._current_widgets()) + 1
        data["id"] = str(n)
        if data.get("type") != "label":
            data["label"] = data.get("label", "Widget") + " (copy)"
        else:
            data["text"] = data.get("text", "Label") + " (copy)"
        self._current_widgets().append(data)
        self._rebuild()

    def load_from_data(self, data: dict):
        if data.get("pages"):
            self.pages = []
            for p in data["pages"]:
                widgets = []
                for w in p.get("widgets", []):
                    d = self._normalize_loaded_widget(w)
                    widgets.append(d)
                self.pages.append({"name": p.get("name", "Page"), "widgets": widgets})
            if not self.pages:
                self.pages = [{"name": "Main", "widgets": []}]
        else:
            # Legacy: flat buttons/values/... into one page
            widgets = []
            for b in data.get("buttons", []):
                d = b.copy()
                d["type"] = "button"
                d["data_bytes"] = d.get("data_bytes", self._parse_hex(d.get("data", "00 00 00 00 00 00 00 00")))
                d["value_type"] = d.get("type", "float")
                d.setdefault("x", 0)
                d.setdefault("y", 0)
                widgets.append(d)
            for v in data.get("values", []):
                d = v.copy()
                d["type"] = "value"
                d["value_type"] = d.get("type", "float")
                d.setdefault("x", 0)
                d.setdefault("y", 0)
                widgets.append(d)
            for c in data.get("checkboxes", []):
                d = c.copy()
                d["type"] = "checkbox"
                d.setdefault("x", 0)
                d.setdefault("y", 0)
                widgets.append(d)
            for s in data.get("sliders", []):
                d = s.copy()
                d["type"] = "slider"
                d.setdefault("x", 0)
                d.setdefault("y", 0)
                widgets.append(d)
            for l in data.get("labels", []):
                d = l.copy()
                d["type"] = "label"
                d["label"] = d.get("text", "")
                d.setdefault("x", 0)
                d.setdefault("y", 0)
                widgets.append(d)
            self.pages = [{"name": "Main", "widgets": widgets}]
        self.current_page_index = 0
        self.selected_index = -1
        self._rebuild_page_bar()
        self._rebuild()

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
            d["value_type"] = d.get("type", d.get("value_type", "float"))
        if d.get("type") == "label":
            d["label"] = d.get("text", d.get("label", ""))
        return d

    def get_data(self) -> dict:
        out_pages = []
        for page in self.pages:
            widgets = []
            for w in page["widgets"]:
                t = w.get("type", "")
                d = {k: v for k, v in w.items() if k not in ("data_bytes", "can_id", "data", "byte_start", "byte_length", "byte", "bit", "scale", "offset")}
                if "value_type" in d and d.get("type") == "value":
                    d["type"] = d["value_type"]
                    del d["value_type"]
                if t == "label":
                    d["text"] = d.get("text", d.get("label", ""))
                if d.get("binding_type") == BINDING_TYPE_SCRIPT:
                    d["variable"] = d.get("binding_value", d.get("variable", ""))
                widgets.append(d)
            out_pages.append({"name": page["name"], "widgets": widgets})
        return {"pages": out_pages}


class FormDesigner(QDialog):
    """Main form designer dialog."""
    saved = pyqtSignal(str)

    def __init__(self, parent=None, db_id: str = "", db_name: str = "", description: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Form Designer")
        self.setMinimumSize(900, 600)
        self.resize(1000, 700)
        self.db_id = db_id or "new"
        _default_name = f"{self.db_id}_{date.today().isoformat()}"
        self.db_name = db_name or (db_id and f"{db_id}_{date.today().isoformat()}") or _default_name
        self.description = description

        self.symbol_list = SymbolListPanel()
        self.palette = WidgetPalette()
        self.canvas = FormCanvas()
        self.properties = PropertyEditor(self.symbol_list)
        self.code_editor = QPlainTextEdit()
        self.code_editor.setPlaceholderText("Database script (DatabaseMainFunction(api)). Load from file or start from template.")
        self.code_editor.setFont(QFont("Consolas", 10))

        self.canvas.widget_selected.connect(self.on_widget_selected)
        self.properties.properties_changed.connect(self.on_properties_changed)
        self.symbol_list.symbol_selected.connect(self._on_symbol_selected)

        # Left: Symbols (Vector-style) + Widget palette
        left_widget = QWidget()
        left_layout = QVBoxLayout()
        left_layout.addWidget(self.symbol_list)
        left_layout.addWidget(self.palette)
        left_widget.setLayout(left_layout)

        # Middle: tabs "Form", "Database code", "Outline"
        self.outline_tree = QTreeWidget()
        self.outline_tree.setHeaderLabels(["Control", "Binding"])
        self.outline_tree.setColumnWidth(0, 180)
        self.outline_tree.header().setContextMenuPolicy(Qt.CustomContextMenu)
        self.outline_tree.header().customContextMenuRequested.connect(self._show_outline_column_menu)
        self.design_tabs = QTabWidget()
        self.design_tabs.addTab(self.canvas, "Form")
        self.design_tabs.addTab(self.code_editor, "Database code")
        self.design_tabs.addTab(self.outline_tree, "Outline")
        self.design_tabs.currentChanged.connect(self._on_tab_changed)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(True)
        left_widget.setMinimumWidth(0)
        self.design_tabs.setMinimumWidth(0)
        self.properties.setMinimumWidth(0)
        left_panel = SplitterPanel("Symbols & palette", left_widget, Qt.Horizontal)
        form_panel = SplitterPanel("Form / Code / Outline", self.design_tabs, Qt.Horizontal)
        props_panel = SplitterPanel("Properties", self.properties, Qt.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(form_panel)
        splitter.addWidget(props_panel)
        splitter.setSizes([200, 480, 260])

        top_layout = QHBoxLayout()
        self.db_id_edit = QLineEdit(self.db_id)
        self.db_id_edit.setPlaceholderText("Database ID (e.g. 1001)")
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
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save)
        load_btn = QPushButton("Load")
        load_btn.clicked.connect(self.load)
        new_btn = QPushButton("New")
        new_btn.clicked.connect(self.new_form)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(load_btn)
        btn_layout.addWidget(new_btn)
        btn_layout.addStretch()

        layout = QVBoxLayout()
        layout.addLayout(top_layout)
        layout.addLayout(btn_layout)
        layout.addWidget(splitter)
        self.setLayout(layout)

    def _on_tab_changed(self, index: int):
        if index == 1:
            self._load_script()
        elif index == 2:
            self._refresh_outline()

    def _script_path(self) -> Path:
        """Path to the database script file for current db_id."""
        db_id = self.db_id_edit.text().strip() or "new"
        return Path("Databases") / f"{db_id}_script.py"

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

    def on_widget_selected(self, index: int, data: dict):
        self.properties.load_widget(data)

    def _on_symbol_selected(self, symbol: str):
        """Set current widget binding to the double-clicked DBC symbol."""
        w = self.canvas._current_widgets()
        idx = self.canvas.selected_index
        if 0 <= idx < len(w) and symbol:
            w[idx]["binding_type"] = BINDING_TYPE_DBC
            w[idx]["binding_value"] = symbol
            w[idx]["variable"] = symbol
            self.properties.load_widget(w[idx])
            self.canvas._rebuild()

    def _show_outline_column_menu(self, pos):
        """Context menu on Outline header: toggle column visibility."""
        menu = QMenu(self)
        labels = ["Control", "Binding"]
        for col in range(min(self.outline_tree.columnCount(), len(labels))):
            name = labels[col]
            act = menu.addAction(f"Show '{name}'")
            act.setCheckable(True)
            act.setChecked(not self.outline_tree.isColumnHidden(col))
            act.triggered.connect(lambda checked, c=col: self.outline_tree.setColumnHidden(c, not checked))
        menu.exec_(self.outline_tree.header().mapToGlobal(pos))

    def _refresh_outline(self):
        """Fill Outline tab with pages and widgets and their bindings."""
        self.outline_tree.clear()
        for page in self.canvas.pages:
            page_name = page.get("name", "Page")
            page_item = QTreeWidgetItem(self.outline_tree, [page_name, ""])
            for w in page.get("widgets", []):
                label = w.get("label", w.get("text", w.get("type", "?")))
                binding = w.get("binding_value", w.get("variable", "")) or ""
                btype = w.get("binding_type", BINDING_TYPE_SCRIPT)
                if binding:
                    binding = f"[{btype}] {binding}"
                QTreeWidgetItem(page_item, [f"{w.get('type', '?')}: {label}", binding])
        self.outline_tree.expandAll()

    def on_properties_changed(self, data: dict):
        for i, w in enumerate(self.canvas._current_widgets()):
            if w is data:
                self.canvas.update_widget(i, data)
                break
        if self.design_tabs.currentIndex() == 2:
            self._refresh_outline()

    def new_form(self):
        self.canvas.load_from_data({})
        self.properties.clear()
        self.db_id_edit.clear()
        self.db_name_edit.setText(f"new_{date.today().isoformat()}")
        self.desc_edit.clear()
        self.code_editor.setPlainText(SCRIPT_TEMPLATE)

    def load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Database", "Databases", "XML (*.xml)")
        if not path:
            return
        try:
            tree = ET.parse(path)
            root = tree.getroot()
            self.db_id = Path(path).stem
            self.db_name = root.get("name", self.db_id)
            self.db_id_edit.setText(self.db_id)
            self.db_name_edit.setText(self.db_name)
            desc = root.find("description")
            self.description = desc.text.strip() if desc is not None and desc.text else ""
            self.desc_edit.setText(self.description)
            dbc_el = root.find("dbc_path")
            dbc_path = root.get("dbc_path", "") or (dbc_el.text.strip() if dbc_el is not None and dbc_el.text else "")
            if dbc_path and Path(dbc_path).exists():
                self.dbc_path_edit.setText(dbc_path)
                self.symbol_list.load_dbc_path(dbc_path)
            else:
                self.dbc_path_edit.clear()

            pages_el = root.find("pages")
            if pages_el is not None:
                data = {"pages": []}
                for page_el in pages_el.findall("page"):
                    name = page_el.get("name", "Page")
                    widgets = []
                    for tag, elem_tag in [("button", "button"), ("value", "value"), ("checkbox", "checkbox"), ("slider", "slider"), ("label", "label"), ("gauge", "gauge"), ("progress_bar", "progress_bar"), ("led", "led"), ("combo", "combo"), ("io_box", "io_box")]:
                        for elem in page_el.findall(elem_tag):
                            d = self._elem_to_widget_dict(elem, tag)
                            widgets.append(d)
                    data["pages"].append({"name": name, "widgets": widgets})
                self.canvas.load_from_data(data)
            else:
                data = {tag: [] for tag in ["buttons", "values", "checkboxes", "sliders", "labels"]}
                for tag in data:
                    for elem in root.findall(f".//{tag[:-1]}"):
                        d = self._elem_to_widget_dict(elem, tag[:-1])
                        data[tag].append(d)
                self.canvas.load_from_data(data)
            self.properties.clear()
            self._load_script()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load: {e}")

    def _elem_to_widget_dict(self, elem, tag: str) -> dict:
        d = dict(elem.attrib)
        for k in ["x", "y", "width", "height", "byte_start", "byte_length", "byte", "bit", "min", "max"]:
            if k in d:
                try:
                    d[k] = int(d[k])
                except ValueError:
                    pass
        for k in ["scale", "offset"]:
            if k in d:
                try:
                    d[k] = float(d[k])
                except ValueError:
                    pass
        if "can_id" in d:
            try:
                d["can_id"] = int(d["can_id"], 16) if str(d["can_id"]).startswith("0x") else int(d["can_id"])
            except ValueError:
                d["can_id"] = 0
        if "data" in d:
            d["data_bytes"] = [int(x, 16) for x in d["data"].replace(",", " ").split() if x.strip()]
        if tag == "value":
            d["value_type"] = d.get("type", "float")
        if tag == "label":
            d["text"] = d.get("text", d.get("label", ""))
        d["type"] = tag
        return d

    def save(self):
        db_id = self.db_id_edit.text().strip() or "new"
        db_name = self.db_name_edit.text().strip() or db_id
        description = self.desc_edit.text().strip()

        path = Path("Databases")
        path.mkdir(exist_ok=True)
        filepath = path / f"{db_id}.xml"

        data = self.canvas.get_data()
        dbc_path = self.symbol_list.get_dbc_path() or self.dbc_path_edit.text().strip()
        root = ET.Element("application_database", name=db_name)
        if dbc_path:
            root.set("dbc_path", dbc_path)
        if description:
            ET.SubElement(root, "description").text = description

        pages_el = ET.SubElement(root, "pages")
        for page in data.get("pages", []):
            page_el = ET.SubElement(pages_el, "page", name=page.get("name", "Page"))
            for item in page.get("widgets", []):
                t = item.get("type", "")
                key = {"button": "button", "value": "value", "checkbox": "checkbox", "slider": "slider", "label": "label", "gauge": "gauge", "progress_bar": "progress_bar", "led": "led", "combo": "combo", "io_box": "io_box"}.get(t)
                if not key:
                    continue
                exclude = {"data_bytes", "value_type", "can_id", "data", "byte_start", "byte_length", "byte", "bit", "scale", "offset"}
                attrs = {k: str(v) for k, v in item.items() if k not in exclude}
                if "binding_value" in item and item.get("binding_type") == BINDING_TYPE_SCRIPT:
                    attrs["variable"] = str(item.get("binding_value", item.get("variable", "")))
                if "value_type" in item:
                    attrs["type"] = item["value_type"]
                if key == "label" and "text" in item:
                    attrs["text"] = item["text"]
                ET.SubElement(page_el, key, attrs)

        tree = ET.ElementTree(root)
        ET.indent(tree, space="    ")
        try:
            tree.write(filepath, encoding="utf-8", xml_declaration=True, default_namespace=None)
            if self._save_script():
                QMessageBox.information(self, "Saved", f"Saved to {filepath}\nScript: {self._script_path()}")
            else:
                QMessageBox.information(self, "Saved", f"Form saved to {filepath}")
            self.saved.emit(str(filepath))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save: {e}")
