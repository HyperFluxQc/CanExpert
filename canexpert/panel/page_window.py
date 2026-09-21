"""
A panel page as a window of its own: its controls where the Form Designer put them, at any zoom.

CANoe's panels are windows in the desktop that can be docked, tabbed, floated and resized; here each
page of the loaded database is one. PanelPage keeps every control's designed position, size and font,
and draws them all at a zoom factor - which is how a page follows its window: Fit scales it to the
window, or a fixed 50 % ... 200 % keeps it at that size and scrolls. Ctrl + mouse wheel steps the zoom.
"""
from __future__ import annotations

from PyQt5.QtCore import QEvent, QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QComboBox, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

ZOOM_CHOICES = ("Fit", "50 %", "75 %", "100 %", "125 %", "150 %", "200 %")
DEFAULT_ZOOM = "100 %"
MIN_ZOOM, MAX_ZOOM = 0.25, 4.0
MARGIN = 20                 # room kept right of and below the last control, as the designer leaves it


class PanelPage(QWidget):
    """A page's controls at their designed geometry, scaled together by set_zoom()."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._designed = []                     # (widget, designed rect, designed font)
        self.designed_size = QSize(600, 400)
        self.zoom = 1.0

    def place(self, widget, x: int, y: int, width: int, height: int):
        """Put a control on the page where the designer put it."""
        widget.setParent(self)
        rect = QRect(int(x), int(y), int(width), int(height))
        widget.setGeometry(rect)
        self._designed.append((widget, rect, QFont(widget.font())))
        self.designed_size = self.designed_size.expandedTo(QSize(rect.right() + 1 + MARGIN,
                                                                 rect.bottom() + 1 + MARGIN))
        self.setFixedSize(self.designed_size)

    def set_zoom(self, zoom: float):
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, float(zoom)))
        if abs(zoom - self.zoom) < 1e-6:
            return
        self.zoom = zoom
        for widget, rect, font in self._designed:
            widget.setGeometry(round(rect.x() * zoom), round(rect.y() * zoom),
                               max(1, round(rect.width() * zoom)), max(1, round(rect.height() * zoom)))
            scaled = QFont(font)
            if font.pointSizeF() > 0:
                scaled.setPointSizeF(font.pointSizeF() * zoom)
            elif font.pixelSize() > 0:
                scaled.setPixelSize(max(1, round(font.pixelSize() * zoom)))
            widget.setFont(scaled)
        self.setFixedSize(round(self.designed_size.width() * zoom), round(self.designed_size.height() * zoom))


def zoom_factor(text: str) -> float | None:
    """"125 %" -> 1.25; None for Fit."""
    if text == "Fit":
        return None
    return float(text.rstrip(" %")) / 100.0


class PanelWindow(QWidget):
    """A page with its zoom: the content of one panel window in the workspace."""
    zoom_changed = pyqtSignal(str)

    def __init__(self, page: PanelPage, name: str, zoom: str = DEFAULT_ZOOM, parent=None):
        super().__init__(parent)
        self.page, self.name = page, name
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 2, 6, 0)
        bar.addStretch()
        bar.addWidget(QLabel("Zoom:"))
        self.zoom_combo = QComboBox()
        self.zoom_combo.addItems(ZOOM_CHOICES)
        self.zoom_combo.setToolTip("Fit scales the page to the window; Ctrl + mouse wheel steps the zoom")
        bar.addWidget(self.zoom_combo)
        layout.addLayout(bar)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.scroll.setWidget(page)
        self.scroll.viewport().installEventFilter(self)
        layout.addWidget(self.scroll, 1)
        self.zoom_combo.setCurrentText(zoom if zoom in ZOOM_CHOICES else DEFAULT_ZOOM)
        self.zoom_combo.currentTextChanged.connect(self._on_zoom_chosen)
        self.apply_zoom()

    def fit_factor(self) -> float:
        viewport, designed = self.scroll.viewport().size(), self.page.designed_size
        if designed.width() <= 0 or designed.height() <= 0 or viewport.width() <= 1:
            return 1.0
        return max(MIN_ZOOM, min(MAX_ZOOM, min(viewport.width() / designed.width(),
                                               viewport.height() / designed.height())))

    def apply_zoom(self):
        factor = zoom_factor(self.zoom_combo.currentText())
        fit = factor is None
        policy = Qt.ScrollBarAlwaysOff if fit else Qt.ScrollBarAsNeeded   # a fitted page never needs them
        self.scroll.setHorizontalScrollBarPolicy(policy)
        self.scroll.setVerticalScrollBarPolicy(policy)
        self.page.set_zoom(self.fit_factor() if fit else factor)

    def _on_zoom_chosen(self, text):
        self.apply_zoom()
        self.zoom_changed.emit(text)

    def step_zoom(self, steps: int):
        """One notch of Ctrl + wheel: the next fixed zoom up or down from where the page is now."""
        fixed = [zoom_factor(text) for text in ZOOM_CHOICES[1:]]
        current = self.page.zoom
        if steps > 0:
            target = next((value for value in fixed if value > current + 1e-6), fixed[-1])
        else:
            target = next((value for value in reversed(fixed) if value < current - 1e-6), fixed[0])
        self.zoom_combo.setCurrentText(f"{target * 100:g} %")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if zoom_factor(self.zoom_combo.currentText()) is None:
            self.apply_zoom()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel and event.modifiers() & Qt.ControlModifier:
            self.step_zoom(1 if event.angleDelta().y() > 0 else -1)
            return True
        return super().eventFilter(watched, event)
