"""Small, scalable toolbar symbols with explicit light/dark and disabled colors."""
from PyQt5.QtCore import Qt, QByteArray
from PyQt5.QtGui import QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer


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
}
_COLORS = {
    "connect": ("#15803d", "#6ee7a0"),
    "disconnect": ("#c43c3c", "#ff9696"),
    "designer": ("#6d4acb", "#bfa7ff"),
    "logger": ("#1566ae", "#7ac4ff"),
    "diagnostics": ("#a6600b", "#f6c16b"),
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
