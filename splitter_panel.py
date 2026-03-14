"""
Reusable splitter panel with a title bar and minimize button.
When minimized, the panel collapses to a thin strip showing only the restore icon.
"""
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QSplitter,
    QStyle,
    QApplication,
)

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
