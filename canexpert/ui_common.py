"""Shared Qt helpers: persistent settings, toolbar icons, the small tool buttons of the analysis windows,
Windows 11-style caption buttons, the collapsible SplitterPanel, the main window's DockTitleBar, and a
tree's rows as CSV."""
import csv

from PyQt5.QtCore import QByteArray, QEvent, QPointF, QRectF, QSettings, QSize, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QFrame,
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


def app_settings() -> QSettings:
    """CAN Expert's settings (the registry on Windows)."""
    return QSettings(ORGANIZATION, APPLICATION)


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
    "data": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M3 14.5h18M11 9v11"/>',
    "statistics": '<path d="M4 20V4"/><path d="M4 20h16"/><rect x="7" y="12" width="3" height="5"/>'
                  '<rect x="12" y="8" width="3" height="9"/><rect x="17" y="5" width="3" height="12"/>',
    "write": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M6.5 9h7M6.5 12.5h5M6.5 16h3"/>'
             '<path d="m14 17 1-3 4.5-4.5 2 2L17 16l-3 1z"/>',
    "tests": '<rect x="4" y="3" width="16" height="18" rx="2"/>'
             '<path d="m7.5 8.5 1.5 1.5 3-3M14 9h3M7.5 15.5 9 17l3-3M14 16h3"/>',
    "j1939": '<rect x="2" y="6" width="12" height="10" rx="1"/><path d="M14 10h4l3 3.5V16h-7z"/>'
             '<circle cx="6.5" cy="18" r="1.8"/><circle cx="17" cy="18" r="1.8"/><path d="M5 11h6"/>',
    "sysvars": '<path d="M7 5c-2 0-2 2-2 3.5S4 11 3 12c1 1 2 1.5 2 3.5S5 19 7 19"/>'
               '<path d="M17 5c2 0 2 2 2 3.5s1 2.5 2 3.5c-1 1-2 1.5-2 3.5S19 19 17 19"/>'
               '<path d="M9 9l6 6M15 9l-6 6"/>',
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
    "data": ("#2563eb", "#93c5fd"),
    "statistics": ("#0e7490", "#67e8f9"),
    "write": ("#4b5563", "#d1d5db"),
    "tests": ("#047857", "#6ee7b7"),
    "j1939": ("#9a3412", "#fdba74"),
    "sysvars": ("#9d174d", "#f9a8d4"),
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


def app_icon(name: str = "canexpert") -> QIcon:
    """The application icon (canexpert, or dummy_ecu), drawn by tools/make_icons.py."""
    from canexpert.paths import RESOURCES_DIR
    icon = QIcon()
    for suffix in (".ico", ".png"):
        path = RESOURCES_DIR / f"{name}{suffix}"
        if path.exists():
            icon.addFile(str(path))
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


# Symbols of the small tool buttons of the Trace and the CAN Logger, drawn in a 24 x 24 box (line_icon).
TOOL_ICONS = {
    "clear": '<path d="M5 7h14M10 4h4"/><path d="M7 7l1 13h8l1-13"/><path d="M10.5 10.5v6M13.5 10.5v6"/>',
    "pause": '<path d="M9.5 5v14M14.5 5v14" stroke-width="2.6"/>',
    "play": '<path d="M8 5l11 7-11 7z"/>',
    "follow": '<path d="M3 12h12"/><path d="M11 7l5 5-5 5"/><path d="M20 4v16"/>',
    "fit": '<path d="M4 10V4h6M14 4h6v6M20 14v6h-6M10 20H4v-6"/>',
    "lock_x": '<path d="M10 7V6a2 2 0 0 1 4 0v1"/><rect x="8.5" y="7" width="7" height="5.5" rx="1.2"/>'
              '<path d="M3 18h18"/><path d="M6.5 15.5 4 18l2.5 2.5"/><path d="M17.5 15.5 20 18l-2.5 2.5"/>',
    "lock_y": '<path d="M13 9V8a2 2 0 0 1 4 0v1"/><rect x="11.5" y="9" width="7" height="5.5" rx="1.2"/>'
              '<path d="M6 3v18"/><path d="M3.5 5.5 6 3l2.5 2.5"/><path d="M3.5 18.5 6 21l2.5-2.5"/>',
    "cursors": '<path d="M8 7v14M16 7v14"/><path d="M5.5 4h5l-2.5 3z" fill="currentColor"/>'
               '<path d="M13.5 4h5l-2.5 3z" fill="currentColor"/>',
    "combine": '<rect x="3" y="4" width="18" height="16" rx="2"/>'
               '<path d="M4.5 16l4-5 3 3 3-6 3 4 2-3"/><path d="M4.5 19h15"/>',
    "colour": '<path d="M12 3a9 9 0 1 0 0 18c1.4 0 2-1 2-1.8 0-1.6-1.6-1.8-1.6-3 0-.9.8-1.6 1.8-1.6H16a5 5 0 0 0 5-5"/>'
              '<circle cx="7.5" cy="12" r="1.2" fill="currentColor"/><circle cx="9.5" cy="8" r="1.2" fill="currentColor"/>'
              '<circle cx="14" cy="7" r="1.2" fill="currentColor"/>',
    "transport": '<path d="M4 7h10a3 3 0 0 1 0 6H8a3 3 0 0 0 0 6h12"/><path d="M17 4l3 3-3 3"/>'
                 '<path d="M7 16l-3 3 3 3"/>',
    "j1939": '<rect x="2" y="6" width="12" height="10" rx="1"/><path d="M14 10h4l3 3.5V16h-7z"/>'
             '<circle cx="6.5" cy="18" r="1.8"/><circle cx="17" cy="18" r="1.8"/>',
}


class ToolButtonsMixin:
    """The small symbol buttons of a window's toolbar (the Trace, the CAN Logger). The window keeps them in
    self._tool_buttons and calls _refresh_tool_icons() after a theme change; Pause shows Play while checked."""

    def _tool_button(self, name, tip, checkable=False, checked=False, clicked=None, toggled=None):
        """A small CANoe-style tool button; its symbol follows the theme (see _refresh_tool_icons)."""
        button = QToolButton()
        button.setAutoRaise(True)
        button.setIconSize(QSize(18, 18))
        button.setToolTip(tip)
        button.setAccessibleName(tip.split(":")[0])
        button.setCheckable(checkable)
        button.setChecked(checked)
        if clicked is not None:
            button.clicked.connect(clicked)
        if toggled is not None:
            button.toggled.connect(toggled)
        self._tool_buttons[name] = button
        return style_toggle(button)

    @staticmethod
    def _separator():
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _refresh_tool_icons(self):
        colour = self.palette().color(QPalette.WindowText)
        for name, button in self._tool_buttons.items():
            symbol = "play" if name == "pause" and button.isChecked() else name
            body = TOOL_ICONS[symbol].replace('fill="currentColor"', f'fill="{colour.name()}"')
            button.setIcon(line_icon(body, colour))
            style_toggle(button)          # the style sheet's palette(...) is resolved when it is set


def write_tree_csv(path, tree, headers):
    """The rows a tree shows, as CSV with the given header row."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for index in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(index)
            writer.writerow([item.text(column) for column in range(len(headers))])


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
