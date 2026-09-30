"""User manual window: docs/USER_MANUAL.md rendered with a list of its sections beside it, and its pictures
(docs/images) no wider than the window - a click on one opens it full size."""
import re
from pathlib import Path

from PyQt5 import sip
from PyQt5.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QImage, QTextCursor, QTextDocument, QTextFormat
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
)

from canexpert.paths import DOCS_DIR
from canexpert.ui_common import enable_maximize, fit_new_window

MANUAL = DOCS_DIR / "USER_MANUAL.md"


def manual_sections(text):
    """The "## " headings of the manual, in order."""
    return re.findall(r"^## (.+)$", text, re.M)


class ManualBrowser(QTextBrowser):
    """The manual's text. Its pictures - found beside the manual (search paths) - are shown no wider than the
    window and never wider than they are, smoothly scaled: one fitted copy of each, made again when the window's
    width changes. A click on a picture asks for it full size (picture_clicked)."""
    SOURCE = QTextFormat.UserProperty + 1       # a picture's own file, kept on its fitted copy's format
    picture_clicked = pyqtSignal(str)           # the picture's file, as the manual names it (images/...)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)     # the manual is long: the width stays put
        # The text wraps a little inside the view: Qt lays tables out a pixel or two wider than the width they
        # are given, which would show a horizontal scroll bar for nothing.
        self.setLineWrapMode(QTextEdit.FixedPixelWidth)
        self.viewport().setMouseTracking(True)
        self._pressed = None
        self._cursor_before = None              # the view's cursor before one over a picture
        self._originals = {}                    # picture file -> its QImage (None: not found)
        self._refit = QTimer(self)              # once the window has stopped changing size
        self._refit.setSingleShot(True)
        self._refit.setInterval(80)
        self._refit.timeout.connect(self.fit_images)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setLineWrapColumnOrWidth(max(100, self.viewport().width() - 4))
        self._refit.start()

    def picture_at(self, point):
        """The file of the picture at a point of the view, else None."""
        position = self.cursorForPosition(point).position()
        cursor = QTextCursor(self.document())
        for after in (position + 1, position):           # the character after the point, then the one before
            if 0 < after <= self.document().characterCount():
                cursor.setPosition(after)
                image = cursor.charFormat()
                if image.isImageFormat():
                    rect = self.cursorRect(cursor)
                    width = image.toImageFormat().width()
                    if rect.left() - width - 2 <= point.x() <= rect.left() + 2:   # on it, not beside it
                        return image.stringProperty(self.SOURCE) or image.toImageFormat().name()
        return None

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)             # (a link's own hand cursor is Qt's)
        on_picture = not event.buttons() and self.picture_at(event.pos()) is not None
        if on_picture and self._cursor_before is None:
            self._cursor_before = self.viewport().cursor()
            self.viewport().setCursor(Qt.PointingHandCursor)
        elif not on_picture and self._cursor_before is not None:
            self.viewport().setCursor(self._cursor_before)
            self._cursor_before = None

    def mousePressEvent(self, event):
        self._pressed = event.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        clicked = event.button() == Qt.LeftButton and self._pressed is not None and \
            (event.pos() - self._pressed).manhattanLength() < 4
        if clicked and not self.textCursor().hasSelection():
            picture = self.picture_at(event.pos())
            if picture:
                self.picture_clicked.emit(picture)
        self._pressed = None

    def original(self, name):
        if name not in self._originals:
            image = None
            for folder in self.searchPaths():
                candidate = QImage(str(Path(folder) / name))
                if not candidate.isNull():
                    image = candidate
                    break
            self._originals[name] = image
        return self._originals[name]

    def images(self):
        """(position, length, format) of every picture in the text."""
        found = []
        block = self.document().begin()
        while block.isValid():
            fragments = block.begin()
            while not fragments.atEnd():
                fragment = fragments.fragment()
                if fragment.charFormat().isImageFormat():
                    found.append((fragment.position(), fragment.length(), fragment.charFormat().toImageFormat()))
                fragments += 1
            block = block.next()
        return found

    def fit_images(self):
        room = max(120, self.lineWrapColumnOrWidth() - 2 * int(self.document().documentMargin()) - 12)
        ratio = self.devicePixelRatioF()
        cursor = QTextCursor(self.document())
        for position, length, image in self.images():
            source = image.stringProperty(self.SOURCE) or image.name()
            original = self.original(source)
            if original is None:
                continue
            width = min(original.width(), room)
            name = f"{source}@fitted"
            if image.name() == name and round(image.width()) == width:
                continue
            pixels = min(original.width(), round(width * ratio))       # sharp on a high-DPI screen too
            fitted = original if pixels == original.width() else original.scaledToWidth(pixels,
                                                                                       Qt.SmoothTransformation)
            self.document().addResource(QTextDocument.ImageResource, QUrl(name), fitted)
            image.setName(name)
            image.setProperty(self.SOURCE, source)
            image.setWidth(width)
            image.setHeight(round(original.height() * width / original.width()))
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.KeepAnchor)
            cursor.setCharFormat(image)


class HelpWindow(QDialog):
    """The user manual: sections on the left, the text on the right, with a find box."""

    def __init__(self, parent=None, path=MANUAL):
        super().__init__(parent)
        self.setWindowTitle("CAN Expert user manual")
        enable_maximize(self)
        self.resize(1100, 800)                  # the pictures a readable size
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
        self.browser = ManualBrowser()
        self.browser.setSearchPaths([str(Path(path).parent)])    # images/... beside the manual
        self.browser.setOpenExternalLinks(True)
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self.open_link)
        self.browser.picture_clicked.connect(lambda name: self.open_link(QUrl(name)))
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
        self.browser.fit_images()
        self.contents.addItems(manual_sections(text))

    def open_link(self, url):
        """A web link in the browser; a picture's link (images/...) in the viewer of pictures, full size; a place
        in the manual (#...) scrolled to."""
        if url.isRelative() and not url.path():
            self.browser.scrollToAnchor(url.fragment())
            return
        if url.isRelative():
            url = QUrl.fromLocalFile(str(Path(self.path).parent / url.path()))
        QDesktopServices.openUrl(url)

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
    # The window it was opened from may have been deleted, and the manual with it.
    if window[0] is None or sip.isdeleted(window[0]) or window[0].parent() is not parent:
        window[0] = HelpWindow(parent)
        fit_new_window(window[0], parent)
    window[0].show()
    window[0].raise_()
    window[0].activateWindow()
    return window[0]
