"""
Form Designer canvas: the widgets on the page (move, resize, select, z-order), the drop target for the
palette and DBC signals, and FormCanvas with its pages, layout tools, clipboard and undo/redo.
"""
import copy
import time

from PyQt5.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPalette, QPen, QPolygonF
from PyQt5.QtWidgets import (
    QFrame, QGraphicsItem, QGraphicsProxyWidget, QGraphicsRectItem, QGraphicsScene, QGraphicsView, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QPushButton, QRubberBand, QToolButton, QVBoxLayout,
    QWidget,
)

from canexpert.designer.side_panels import (BINDING_TYPE_DBC, BINDING_TYPE_SCRIPT, GRID, MIN_SIZE, SIGNAL_MIME,
                                            UNDO_LIMIT, WIDGET_TYPE_MIME, control_name)
from canexpert.panel.controls import CONTROLS, build
from canexpert.panel.database import parse_hex_bytes
from canexpert.ui_common import line_icon, style_toggle

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
        style_toggle(self._tool_buttons["grid"])
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
            if button.isCheckable():
                style_toggle(button)      # re-reads the style sheet's palette(...) for the new theme

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
                        d["data_bytes"] = d.get("data_bytes") or parse_hex_bytes(d.get("data", "")) or [0] * 8
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
