"""User manual window: docs/USER_MANUAL.md rendered with a list of its sections beside it."""
import re

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QDesktopServices, QTextCursor
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
)

from canexpert.paths import DOCS_DIR
from canexpert.ui_common import enable_maximize

MANUAL = DOCS_DIR / "USER_MANUAL.md"


def manual_sections(text):
    """The "## " headings of the manual, in order."""
    return re.findall(r"^## (.+)$", text, re.M)


class HelpWindow(QDialog):
    """The user manual: sections on the left, the text on the right, with a find box."""

    def __init__(self, parent=None, path=MANUAL):
        super().__init__(parent)
        self.setWindowTitle("CAN Expert user manual")
        enable_maximize(self)
        self.resize(900, 700)
        self.path = path
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find in the manual...")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.find_next)
        find_button = QPushButton("Find next")
        find_button.clicked.connect(self.find_next)
        self.status = QLabel("")
        self.status.setStyleSheet("color: gray;")
        bar.addWidget(self.search, 1)
        bar.addWidget(find_button)
        bar.addWidget(self.status)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        self.contents = QListWidget()
        self.contents.currentTextChanged.connect(self.go_to_section)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(QDesktopServices.openUrl)
        splitter.addWidget(self.contents)
        splitter.addWidget(self.browser)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([230, 670])
        layout.addWidget(splitter, 1)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.load()

    def load(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            self.browser.setPlainText(f"The manual could not be read:\n{exc}\n\nIt belongs at {self.path}.")
            return
        self.browser.setMarkdown(text)
        self.contents.addItems(manual_sections(text))

    def go_to_section(self, title):
        """Scroll to a heading from the contents list (the heading itself, not a mention of it in the text)."""
        if not title:
            return
        block = self.browser.document().begin()
        while block.isValid():
            if block.blockFormat().headingLevel() and block.text().strip() == title:
                self.browser.setTextCursor(QTextCursor(block))
                bar = self.browser.verticalScrollBar()
                bar.setValue(bar.value() + self.browser.cursorRect().top())  # put the heading at the top
                return
            block = block.next()

    def find_next(self):
        text = self.search.text().strip()
        if not text:
            return
        if not self.browser.find(text):                       # wrap around
            self.browser.moveCursor(self.browser.textCursor().Start)
            found = self.browser.find(text)
            self.status.setText("" if found else "not found")
            return
        self.status.setText("")


def show_manual(parent=None, window=[None]):  # noqa: B006 (one window, reused)
    """Open the manual window, raising the existing one."""
    if window[0] is None or window[0].parent() is not parent:
        window[0] = HelpWindow(parent)
    window[0].show()
    window[0].raise_()
    window[0].activateWindow()
    return window[0]
