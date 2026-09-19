"""Shared Qt helpers: persistent settings, toolbar icons and the collapsible SplitterPanel."""
from PyQt5.QtCore import QByteArray, QSettings, QSize, Qt
from PyQt5.QtGui import QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QStyle,
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
}
_COLORS = {
    "connect": ("#15803d", "#6ee7a0"),
    "disconnect": ("#c43c3c", "#ff9696"),
    "designer": ("#6d4acb", "#bfa7ff"),
    "logger": ("#1566ae", "#7ac4ff"),
    "diagnostics": ("#a6600b", "#f6c16b"),
    "flashing": ("#b42318", "#ff9c8a"),
}


def toolbar_icon(name, dark=False):
    icon = QIcon()
    for mode in (QIcon.Normal, QIcon.Disabled):
        color = ("#747b85" if dark else "#a7adb5") if mode == QIcon.Disabled else _COLORS[name][int(dark)]
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
               f'<g fill="none" stroke="{color}" stroke-width="1.8" '
               f'stroke-linecap="round" stroke-linejoin="round">{_PATHS[name]}</g></svg>')
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        for size in (24, 32, 48, 64, 96):
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            icon.addPixmap(pixmap, mode)
    return icon


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
        self._min_btn = QToolButton()
        self._min_btn.setToolTip("Minimize panel to a thin strip")
        style = QApplication.style() or self.style()
        self._min_btn.setIcon(style.standardIcon(QStyle.SP_TitleBarMinButton))
        self._min_btn.setIconSize(QSize(16, 16))
        self._min_btn.clicked.connect(self._toggle_minimized)
        bar_layout.addWidget(self._min_btn)
        self._bar.setStyleSheet("background: palette(mid); border: 1px solid palette(dark);")
        layout.addWidget(self._bar, 0, Qt.AlignTop)

        layout.addWidget(content_widget, 1)
        self._content.setMinimumWidth(0)
        self._content.setMinimumHeight(0)

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
        style = QApplication.style() or self.style()
        if self._is_minimized:
            self._min_btn.setIcon(style.standardIcon(QStyle.SP_TitleBarNormalButton))
            self._min_btn.setToolTip("Restore panel")
            self._title_label.hide()
        else:
            self._min_btn.setIcon(style.standardIcon(QStyle.SP_TitleBarMinButton))
            self._min_btn.setToolTip("Minimize panel to a thin strip")
            self._title_label.show()
