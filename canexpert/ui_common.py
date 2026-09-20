"""Shared Qt helpers: persistent settings, toolbar icons, Windows 11-style caption buttons, the
collapsible SplitterPanel and the main window's DockTitleBar."""
from PyQt5.QtCore import QByteArray, QEvent, QPointF, QRectF, QSettings, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


# -----------------------------------------------------------------------------
# Persistent settings (theme, last configuration)
# -----------------------------------------------------------------------------

ORGANIZATION = "CanExpert"
APPLICATION = "CanExpert"
# Settings were stored under this name before the project was renamed.
LEGACY_ORGANIZATION, LEGACY_APPLICATION = "EZCan2", "KvaserCAN"
_MIGRATED_KEY = "migrated_legacy_settings"


def app_settings() -> QSettings:
    """CAN Expert settings; values saved under the legacy name are copied over once."""
    settings = QSettings(ORGANIZATION, APPLICATION)
    if not settings.value(_MIGRATED_KEY, False, type=bool):
        legacy = QSettings(LEGACY_ORGANIZATION, LEGACY_APPLICATION)
        for key in legacy.allKeys():
            if not settings.contains(key):
                settings.setValue(key, legacy.value(key))
        settings.setValue(_MIGRATED_KEY, True)
    return settings


def is_dark_theme(widget) -> bool:
    """Dark theme when the window colour is darker than the text on it. Read from the palette in use, so a
    window follows a theme change while it is open."""
    palette = widget.palette()
    return palette.color(QPalette.Window).lightness() < palette.color(QPalette.WindowText).lightness()


def enable_maximize(dialog):
    """Show the title-bar maximize button on a dialog (Windows gives dialogs only close and '?').
    Minimize stays off: an owned dialog has no taskbar entry to restore it from."""
    flags = dialog.windowFlags() & ~Qt.WindowContextHelpButtonHint
    dialog.setWindowFlags(flags | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)


# -----------------------------------------------------------------------------
# Toolbar icons: small scalable symbols with light/dark and disabled colors
# -----------------------------------------------------------------------------

_PATHS = {
    "connect": '<path d="M8 3v5m6-5v5M6 8h10v4a5 5 0 0 1-10 0V8zm5 9v4M18 17h4m-2-2v4"/>',
    "disconnect": '<path d="M8 3v4m6-4v4M6 9v3a5 5 0 0 0 8.5 3.5M16 11V8h-5m0 9v4M3 3l18 18"/>',
    "designer": '<rect x="3" y="4" width="18" height="16" rx="2"/>'
                '<path d="M3 9h18M9 9v11"/>'
                '<path d="m13 17 1-3 5-5 2 2-5 5-3 1z"/>',
    "logger": '<path d="M4 3v17h17M7 14h3l2-7 3 10 2-6h4"/>',
    "diagnostics": '<rect x="6" y="6" width="12" height="12" rx="2"/>'
                   '<path d="M9 3v3m6-3v3M9 18v3m6-3v3M3 9h3m-3 6h3M18 9h3m-3 6h3'
                   'M8 12h2l1-3 2 6 1-3h2"/>',
    "flashing": '<path d="M12 3v8m-3.5-3.5L12 11l3.5-3.5"/>'
                '<rect x="5" y="14" width="14" height="7" rx="1.5"/>'
                '<path d="M8 21v2m4-2v2m4-2v2M9 17.5h6"/>',
    "trace": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M7 13h10M7 16.5h6"/>',
    "transmit": '<path d="M12 3v10"/><path d="M8.5 6.5 12 3l3.5 3.5"/>'
                '<path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6"/>',
    "console": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9.5l3 2.5-3 2.5M13 15h4"/>',
}
_COLORS = {
    "connect": ("#15803d", "#6ee7a0"),
    "disconnect": ("#c43c3c", "#ff9696"),
    "designer": ("#6d4acb", "#bfa7ff"),
    "logger": ("#1566ae", "#7ac4ff"),
    "diagnostics": ("#a6600b", "#f6c16b"),
    "flashing": ("#b42318", "#ff9c8a"),
    "trace": ("#1f6feb", "#8ab4ff"),
    "transmit": ("#b45309", "#fbbf24"),
    "console": ("#7c3aed", "#c4b5fd"),
}


def _svg_icon(body, colours, stroke_width=1.8, sizes=(24, 32, 48, 64, 96)):
    """Icon from an SVG path body drawn in a 24 x 24 box; colours: {QIcon mode: colour or (colour, opacity)}."""
    icon = QIcon()
    for mode, spec in colours.items():
        color, opacity = spec if isinstance(spec, tuple) else (spec, 1.0)
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
               f'<g fill="none" stroke="{color}" stroke-width="{stroke_width}" opacity="{opacity}" '
               f'stroke-linecap="round" stroke-linejoin="round">{body}</g></svg>')
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        for size in sizes:
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            icon.addPixmap(pixmap, mode)
    return icon


def toolbar_icon(name, dark=False):
    return _svg_icon(_PATHS[name], {QIcon.Normal: _COLORS[name][int(dark)],
                                    QIcon.Disabled: "#747b85" if dark else "#a7adb5"})


def line_icon(body, color):
    """Small monochrome line icon (e.g. the Form Designer's layout toolbar) in the given colour."""
    name = QColor(color).name()
    return _svg_icon(body, {QIcon.Normal: name, QIcon.Disabled: (name, 0.35)}, stroke_width=1.6,
                     sizes=(16, 20, 24, 32))


# A button that switches an option on stays pressed in, with an accent line under it, so the state is
# visible at a glance. Qt's own checked look is a faint grey box, and a style sheet that names other
# states (a hover colour, say) suppresses it altogether, leaving a toggle looking the same on and off.
TOGGLE_STYLE = """
    QToolButton { border: 1px solid transparent; border-radius: 4px; }
    QToolButton:hover { background: palette(midlight); }
    QToolButton:checked { background: palette(mid); border: 1px solid palette(dark);
                          border-bottom: 2px solid palette(highlight); }
    QToolButton:checked:hover { background: palette(midlight); }
"""


def style_toggle(button):
    """Give a tool button the pressed-in look. Setting it again re-reads palette(...) after a theme change."""
    button.setStyleSheet(TOGGLE_STYLE)
    return button


# -----------------------------------------------------------------------------
# CaptionButton: Windows 11-style panel buttons (minimize / restore / close)
# -----------------------------------------------------------------------------

class CaptionButton(QToolButton):
    """Flat caption button with thin line glyphs, a soft rounded hover and a red close hover."""
    MINIMIZE, RESTORE, CLOSE = "minimize", "restore", "close"
    CLOSE_HOVER, CLOSE_PRESSED = QColor("#C42B1C"), QColor("#B3261E")

    def __init__(self, kind, tooltip="", parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip or kind)
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.set_compact(False)

    def set_kind(self, kind, tooltip):
        self.kind = kind
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.update()

    def set_compact(self, compact):
        """Compact (square) size fits the thin strip of a minimized panel."""
        self.setFixedSize(22, 22) if compact else self.setFixedSize(30, 22)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        text = self.palette().color(QPalette.ButtonText)
        glyph = QColor(text)
        pressed = self.isDown()
        hovered = self.underMouse() and self.isEnabled()
        background = None
        if self.kind == self.CLOSE and (hovered or pressed):
            background = self.CLOSE_PRESSED if pressed else self.CLOSE_HOVER
            glyph = QColor(Qt.white)
        elif hovered or pressed:
            background = QColor(text)
            background.setAlpha(40 if pressed else 24)
        if background is not None:
            painter.setPen(Qt.NoPen)
            painter.setBrush(background)
            painter.drawRoundedRect(QRectF(self.rect()), 4, 4)
        if not self.isEnabled():
            glyph.setAlpha(110)
        painter.setPen(QPen(glyph, 1.0))
        painter.setBrush(Qt.NoBrush)
        # Glyphs are 10 px, centred on a pixel boundary so 1 px lines stay crisp.
        x, y, half = self.width() // 2 + 0.5, self.height() // 2 + 0.5, 5
        if self.kind == self.MINIMIZE:
            painter.drawLine(QPointF(x - half, y), QPointF(x + half, y))
        elif self.kind == self.RESTORE:
            painter.drawRoundedRect(QRectF(x - half, y - half, 2 * half, 2 * half), 1.5, 1.5)
        else:
            painter.drawLine(QPointF(x - half, y - half), QPointF(x + half, y + half))
            painter.drawLine(QPointF(x - half, y + half), QPointF(x + half, y - half))


# -----------------------------------------------------------------------------
# SplitterPanel: titled panel that minimizes to a thin strip showing only its restore icon
# -----------------------------------------------------------------------------

PANEL_MINIMIZED_SIZE = 28


class SplitterPanel(QWidget):
    """
    A panel with a title bar (title + minimize button) and content.
    Used inside a QSplitter; when minimized, collapses to a thin strip (only icon visible).
    """
    def __init__(self, title: str, content_widget: QWidget, orientation: Qt.Orientation = Qt.Horizontal, parent=None):
        super().__init__(parent)
        self._title = title
        self._content = content_widget
        self._orientation = orientation  # Splitter's orientation: Horizontal = side-by-side panels → minimize width
        self._is_minimized = False
        self._saved_sizes = []  # saved splitter sizes for restore

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._bar = QWidget()
        bar_layout = QHBoxLayout(self._bar)
        bar_layout.setContentsMargins(6, 4, 4, 4)
        bar_layout.setSpacing(4)
        self._title_label = QLabel(title)
        self._title_label.setStyleSheet("font-weight: bold;")
        bar_layout.addWidget(self._title_label)
        bar_layout.addStretch()
        self._min_btn = CaptionButton(CaptionButton.MINIMIZE, "Minimize panel to a thin strip")
        self._min_btn.clicked.connect(self._toggle_minimized)
        bar_layout.addWidget(self._min_btn)
        self._bar_layout = bar_layout
        # Scoped to the bar itself so the title and buttons are not boxed in by the border.
        self._bar.setObjectName("splitterPanelBar")
        self._bar.setAttribute(Qt.WA_StyledBackground, True)
        self._apply_bar_style()
        layout.addWidget(self._bar, 0, Qt.AlignTop)

        layout.addWidget(content_widget, 1)
        self._content.setMinimumWidth(0)
        self._content.setMinimumHeight(0)

    def _apply_bar_style(self):
        self._bar.setStyleSheet("#splitterPanelBar { background: palette(mid); border: 1px solid palette(dark); }")

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange, QEvent.ApplicationPaletteChange):
            self._apply_bar_style()  # palette(...) in a style sheet is resolved when the sheet is set

    def _splitter_and_index(self):
        p = self.parent()
        while p:
            if isinstance(p, QSplitter):
                for i in range(p.count()):
                    if p.widget(i) is self:
                        return p, i
                return None, -1
            p = p.parent()
        return None, -1

    def _toggle_minimized(self):
        if self._is_minimized:
            self._restore()
        else:
            self._minimize()

    def _minimize(self):
        splitter, index = self._splitter_and_index()
        if splitter is not None and index >= 0:
            self._saved_sizes = list(splitter.sizes())
        self._is_minimized = True
        if self._orientation == Qt.Horizontal:
            self.setMinimumWidth(PANEL_MINIMIZED_SIZE)
            self.setMaximumWidth(PANEL_MINIMIZED_SIZE)
        else:
            self.setMinimumHeight(PANEL_MINIMIZED_SIZE)
            self.setMaximumHeight(PANEL_MINIMIZED_SIZE)
        self._content.hide()
        self._bar.setMaximumHeight(PANEL_MINIMIZED_SIZE)  # keep icon bar at top, don't stretch
        self._update_bar_appearance()
        if splitter is not None and index >= 0 and self._saved_sizes:
            new_sizes = self._saved_sizes[:]
            new_sizes[index] = PANEL_MINIMIZED_SIZE
            splitter.setSizes(new_sizes)

    def _restore(self):
        self._is_minimized = False
        if self._orientation == Qt.Horizontal:
            self.setMinimumWidth(80)
            self.setMaximumWidth(16777215)
        else:
            self.setMinimumHeight(80)
            self.setMaximumHeight(16777215)
        self._content.show()
        self._bar.setMaximumHeight(16777215)  # allow bar to size normally when restored
        self._update_bar_appearance()
        splitter, index = self._splitter_and_index()
        if splitter is not None and index >= 0 and self._saved_sizes:
            # Restore our section to saved size (or 200 if not set)
            restored = self._saved_sizes[:]
            restored[index] = max(80, restored[index] if index < len(restored) else 200)
            splitter.setSizes(restored)

    def _update_bar_appearance(self):
        minimized = self._is_minimized
        if minimized:
            self._min_btn.set_kind(CaptionButton.RESTORE, "Restore panel")
        else:
            self._min_btn.set_kind(CaptionButton.MINIMIZE, "Minimize panel to a thin strip")
        self._min_btn.set_compact(minimized)
        self._bar_layout.setContentsMargins(*((3, 3, 3, 3) if minimized else (6, 3, 4, 3)))
        self._title_label.setVisible(not minimized)


DOCK_MINIMIZED_SIZE = 28  # a minimized dock is a thin strip with its restore button


class DockTitleBar(QWidget):
    """Title bar for a dock with title, minimize (collapse to thin strip), and close."""
    def __init__(self, dock, main_window, area, parent=None):
        super().__init__(parent)
        self.dock = dock
        self.main_window = main_window
        self.area = area
        self.is_minimized = False
        self.saved_size = 200  # fallback when restoring

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 2, 2)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignTop)  # when dock is a thin column, keep icon at top
        self.title_label = QLabel(dock.windowTitle())
        self.title_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.title_label)

        self.min_btn = CaptionButton(CaptionButton.MINIMIZE, "Minimize panel to a thin strip")
        self.min_btn.clicked.connect(self._toggle_minimized)
        layout.addWidget(self.min_btn)

        self.close_btn = CaptionButton(CaptionButton.CLOSE, "Close panel")
        self.close_btn.clicked.connect(self.dock.close)
        layout.addWidget(self.close_btn)

        self.setLayout(layout)

    def _toggle_minimized(self):
        if self.is_minimized:
            self.restore()
        else:
            self.minimize()

    def minimize(self):
        self.is_minimized = True
        # Save current size for restore
        if self.area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea):
            self.saved_size = max(80, self.dock.width())
        else:
            self.saved_size = max(80, self.dock.height())
        # Constrain to thin strip
        if self.area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea):
            self.dock.setMinimumWidth(DOCK_MINIMIZED_SIZE)
            self.dock.setMaximumWidth(DOCK_MINIMIZED_SIZE)
        else:
            self.dock.setMinimumHeight(DOCK_MINIMIZED_SIZE)
            self.dock.setMaximumHeight(DOCK_MINIMIZED_SIZE)
        self.dock.widget().hide()
        self._update_title_bar_appearance()

    def restore(self):
        self.is_minimized = False
        if self.area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea):
            self.dock.setMinimumWidth(80)
            self.dock.setMaximumWidth(16777215)
        else:
            self.dock.setMinimumHeight(80)
            self.dock.setMaximumHeight(16777215)
        self.dock.widget().show()
        try:
            orientation = Qt.Horizontal if self.area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea) else Qt.Vertical
            self.main_window.resizeDocks([self.dock], [self.saved_size], orientation)
        except Exception:
            pass
        self._update_title_bar_appearance()

    def _update_title_bar_appearance(self):
        minimized = self.is_minimized
        if minimized:
            self.min_btn.set_kind(CaptionButton.RESTORE, "Restore panel")
        else:
            self.min_btn.set_kind(CaptionButton.MINIMIZE, "Minimize panel to a thin strip")
        self.min_btn.set_compact(minimized)
        self.layout().setContentsMargins(*((3, 3, 3, 3) if minimized else (4, 2, 2, 2)))
        self.title_label.setVisible(not minimized)
        self.close_btn.setVisible(not minimized)
