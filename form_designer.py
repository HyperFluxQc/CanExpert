"""
Form Designer - Visual editor for application database UIs.
Supports multiple named pages and precise X,Y placement of widgets.
"""
import xml.etree.ElementTree as ET
from pathlib import Path
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton,
    QGroupBox, QFormLayout, QLineEdit, QSpinBox, QScrollArea, QFrame,
    QSplitter, QMessageBox, QFileDialog, QCheckBox, QSlider, QComboBox,
    QDoubleSpinBox, QGraphicsScene, QGraphicsView, QGraphicsProxyWidget,
    QPlainTextEdit, QTabWidget, QMenu, QAction, QInputDialog, QApplication,
    QGraphicsItem,
)
from PyQt5.QtCore import Qt, pyqtSignal, QPointF, QMimeData, QTimer
from PyQt5.QtGui import QFont, QColor, QDrag, QCursor

try:
    from database_api import SCRIPT_TEMPLATE
except ImportError:
    SCRIPT_TEMPLATE = '"""Database script - define DatabaseMainFunction(api)."""\n\ndef DatabaseMainFunction(api):\n    pass\n'

# MIME type for drag-and-drop widget type
WIDGET_TYPE_MIME = "application/x-ezcan-widget-type"


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
        ]:
            item = DraggablePaletteItem(wtype, label)
            layout.addWidget(item)
        layout.addStretch()
        self.setLayout(layout)


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
    """Editor for selected widget properties."""
    properties_changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__("Properties")
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

    def load_widget(self, data: dict):
        self.clear()
        self.widget_data = data
        if not data:
            return

        wtype = data.get("type", "button")

        # Common: id, label, position, size, variable (only variable links widget to database code)
        self._add_line("id", "ID", str(data.get("id", "1")))
        if wtype != "label":
            self._add_line("label", "Label", str(data.get("label", "New Widget")))
        self._add_spin("x", "X", data.get("x", 0), 0, 10000)
        self._add_spin("y", "Y", data.get("y", 0), 0, 10000)
        self._add_spin("width", "Width", data.get("width", 100), 20, 2000)
        self._add_spin("height", "Height", data.get("height", 30), 10, 500)
        self._add_line("variable", "Variable", str(data.get("variable", "")))

        if wtype == "value":
            self._add_line("unit", "Unit", str(data.get("unit", "")))
            self._add_combo("value_type", "Display type", ["float", "integer"], data.get("type", data.get("value_type", "float")))

        elif wtype == "slider":
            self._add_spin("min", "Min", data.get("min", 0), -32768, 32767)
            self._add_spin("max", "Max", data.get("max", 100), -32768, 32767)

        elif wtype == "label":
            self._add_line("text", "Text", str(data.get("text", "Label")))

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
        elif key == "text":
            self.widget_data["text"] = str(value)
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
            self.page_buttons.append(btn)
            bar.insertWidget(i, btn)

    def _add_page(self):
        n = len(self.pages) + 1
        self.pages.append({"name": f"Page {n}", "widgets": []})
        self.current_page_index = len(self.pages) - 1
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
        base = {"x": 0, "y": 0, "width": 100, "height": 30, "variable": ""}
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
            placeholder = QFrame()
            placeholder.setFrameStyle(QFrame.Box | QFrame.Raised)
            if i == self.selected_index:
                placeholder.setStyleSheet("QFrame { background-color: #e0e8ff; border: 2px solid #0066cc; color: #222; }")
            else:
                placeholder.setStyleSheet("QFrame { background-color: #fff; border: 1px solid #ccc; color: #222; }")
            plbl = QLabel(self._preview_label(data))
            plbl.setStyleSheet("color: #222;")
            pl_layout = QVBoxLayout()
            pl_layout.addWidget(plbl)
            placeholder.setLayout(pl_layout)
            placeholder.setMinimumSize(data.get("width", 100), data.get("height", 30))
            # Let the proxy receive mouse events so ItemIsMovable works (drag to move)
            placeholder.setAttribute(Qt.WA_TransparentForMouseEvents)
            proxy = MovableProxyWidget(i)
            proxy.setWidget(placeholder)
            proxy.setCursor(Qt.SizeAllCursor)  # show move cursor over widget
            proxy.clicked.connect(self._on_select)
            proxy.right_clicked.connect(lambda idx: self._show_widget_context_menu(idx, QCursor.pos()))
            proxy.position_changed.connect(self._on_widget_moved)
            proxy.setPos(px, py)
            proxy.setZValue(i)
            self.scene.addItem(proxy)

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
        var_act = menu.addAction("Variable...")
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
            self, "Variable",
            "Variable name (linked in script; e.g. Start for a button):",
            QLineEdit.Normal,
            data.get("variable", "")
        )
        if ok:
            w[index]["variable"] = name.strip()
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
        if d.get("type") in ("value", "value_display"):
            d["value_type"] = d.get("type", d.get("value_type", "float"))
        if d.get("type") == "label":
            d["label"] = d.get("text", d.get("label", ""))
        return d

    def get_data(self) -> dict:
        # Only variable links widget to code; no CAN message data
        out_pages = []
        for page in self.pages:
            widgets = []
            for w in page["widgets"]:
                t = w.get("type", "")
                d = {k: v for k, v in w.items() if k not in ("data_bytes", "can_id", "data", "byte_start", "byte_length", "byte", "bit", "scale", "offset")}
                if "value_type" in d:
                    d["type"] = d["value_type"]
                    del d["value_type"]
                if t == "label":
                    d["text"] = d.get("text", d.get("label", ""))
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
        self.db_name = db_name or db_id or "New Database"
        self.description = description

        self.palette = WidgetPalette()
        self.canvas = FormCanvas()
        self.properties = PropertyEditor()
        self.code_editor = QPlainTextEdit()
        self.code_editor.setPlaceholderText("Database script (DatabaseMainFunction(api)). Load from file or start from template.")
        self.code_editor.setFont(QFont("Consolas", 10))

        self.canvas.widget_selected.connect(self.on_widget_selected)
        self.properties.properties_changed.connect(self.on_properties_changed)

        # Middle: tabs "Form" and "Code"
        self.design_tabs = QTabWidget()
        self.design_tabs.addTab(self.canvas, "Form")
        self.design_tabs.addTab(self.code_editor, "Database code")
        self.design_tabs.currentChanged.connect(self._on_tab_changed)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.palette)
        splitter.addWidget(self.design_tabs)
        splitter.addWidget(self.properties)
        splitter.setSizes([150, 500, 250])

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

    def on_properties_changed(self, data: dict):
        for i, w in enumerate(self.canvas._current_widgets()):
            if w is data:
                self.canvas.update_widget(i, data)
                break

    def new_form(self):
        self.canvas.load_from_data({})
        self.properties.clear()
        self.db_id_edit.clear()
        self.db_name_edit.clear()
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

            pages_el = root.find("pages")
            if pages_el is not None:
                data = {"pages": []}
                for page_el in pages_el.findall("page"):
                    name = page_el.get("name", "Page")
                    widgets = []
                    for tag, elem_tag in [("button", "button"), ("value", "value"), ("checkbox", "checkbox"), ("slider", "slider"), ("label", "label")]:
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
        root = ET.Element("application_database", name=db_name)
        if description:
            ET.SubElement(root, "description").text = description

        pages_el = ET.SubElement(root, "pages")
        for page in data.get("pages", []):
            page_el = ET.SubElement(pages_el, "page", name=page.get("name", "Page"))
            for item in page.get("widgets", []):
                t = item.get("type", "")
                key = {"button": "button", "value": "value", "checkbox": "checkbox", "slider": "slider", "label": "label"}.get(t)
                if not key:
                    continue
                exclude = {"data_bytes", "value_type", "can_id", "data", "byte_start", "byte_length", "byte", "bit", "scale", "offset"}
                attrs = {k: str(v) for k, v in item.items() if k not in exclude}
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
