"""
Python code editor for panel scripts: syntax highlighting, line numbers, current-line highlight,
auto-indent, and completion of Python keywords, the script API, control names and DBC signals.
"""
import builtins
import keyword
import re

from PyQt5.QtCore import QRect, QRegularExpression, QSize, QStringListModel, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPalette, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextFormat
from PyQt5.QtWidgets import QCompleter, QPlainTextEdit, QTextEdit, QWidget

API_WORDS = [
    "api", "api.on", "api.on_can", "api.every", "api.sleep", "api.running", "api.log", "api.progress",
    "api.flash_cancelled", "api.signal", "api.set_signal", "api.send_message", "api.can.send",
    "api.can.get_latest_messages", "api.ui.get_value", "api.ui.set_value", "api.uds.request",
    "api.uds.tester_present", "api.uds.rdbi", "api.uds.request_download", "api.uds.transfer_data",
    "api.uds.request_transfer_exit", "api.uds.transfer_data_from_file", "api.dll.load", "api.dll.call",
    "on_start", "on_stop", "on_timer", "on_message", "on_signal", "on_control", "DatabaseMainFunction",
    "Flashing", "frame.id", "frame.data", "frame.signals",
]


def _format(colour, bold=False, italic=False):
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(colour))
    if bold:
        fmt.setFontWeight(QFont.Bold)
    fmt.setFontItalic(italic)
    return fmt


class PythonHighlighter(QSyntaxHighlighter):
    """Keywords, builtins, decorators, definitions, numbers, strings (including triple-quoted) and comments."""

    def __init__(self, document, dark=False):
        super().__init__(document)
        colours = ({"keyword": "#569cd6", "builtin": "#4ec9b0", "decorator": "#dcdcaa", "definition": "#dcdcaa",
                    "number": "#b5cea8", "string": "#ce9178", "comment": "#6a9955", "api": "#9cdcfe"} if dark else
                   {"keyword": "#0000ff", "builtin": "#267f99", "decorator": "#795e26", "definition": "#795e26",
                    "number": "#098658", "string": "#a31515", "comment": "#008000", "api": "#001080"})
        self.string_format = _format(colours["string"])
        self.rules = [
            (QRegularExpression(r"\b(" + "|".join(keyword.kwlist) + r")\b"), _format(colours["keyword"], bold=True)),
            (QRegularExpression(r"\b(" + "|".join(n for n in dir(builtins) if not n.startswith("_")) + r")\b"),
             _format(colours["builtin"])),
            (QRegularExpression(r"\bapi\b"), _format(colours["api"], bold=True)),
            (QRegularExpression(r"@[A-Za-z_][\w.]*"), _format(colours["decorator"])),
            (QRegularExpression(r"\b(?:def|class)\s+(\w+)"), _format(colours["definition"], bold=True)),
            (QRegularExpression(r"\b(0[xX][0-9a-fA-F_]+|\d[\d_]*\.?\d*(?:[eE][-+]?\d+)?)\b"), _format(colours["number"])),
            (QRegularExpression(r"""(?<!["'])("[^"\\\n]*(?:\\.[^"\\\n]*)*"|'[^'\\\n]*(?:\\.[^'\\\n]*)*')"""),
             self.string_format),
            (QRegularExpression(r"#[^\n]*"), _format(colours["comment"], italic=True)),
        ]
        self.triple = [(QRegularExpression('"""'), 1), (QRegularExpression("'''"), 2)]

    def highlightBlock(self, text):
        for pattern, fmt in self.rules:
            matches = pattern.globalMatch(text)
            while matches.hasNext():
                match = matches.next()
                group = 1 if pattern.captureCount() >= 1 and "def|class" in pattern.pattern() else 0
                self.setFormat(match.capturedStart(group), match.capturedLength(group), fmt)
        self.setCurrentBlockState(0)
        for delimiter, state in self.triple:
            if self._triple_quoted(text, delimiter, state):
                break

    def _triple_quoted(self, text, delimiter, state):
        """Colour triple-quoted strings, which may span lines (block state = open delimiter)."""
        if self.previousBlockState() == state:
            start, skip = 0, 0
        else:
            match = delimiter.match(text)
            start, skip = (match.capturedStart(), 3) if match.hasMatch() else (-1, 3)
        while start >= 0:
            end = delimiter.match(text, start + skip)
            if end.hasMatch():
                length = end.capturedEnd() - start
                self.setCurrentBlockState(0)
            else:
                length = len(text) - start
                self.setCurrentBlockState(state)
            self.setFormat(start, length, self.string_format)
            match = delimiter.match(text, start + length)
            start, skip = (match.capturedStart(), 3) if match.hasMatch() else (-1, 3)
        return self.currentBlockState() == state


class _LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_line_numbers(event)


class CodeEditor(QPlainTextEdit):
    """QPlainTextEdit with the extras a script editor needs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont("Consolas", 10)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        dark = self.palette().color(QPalette.Base).lightness() < 128
        self.highlighter = PythonHighlighter(self.document(), dark)
        self._line_numbers = _LineNumbers(self)
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._scroll_line_numbers)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_margin()
        self._highlight_current_line()
        self._extra_words = []
        self.completer = QCompleter(self)
        self.completer.setWidget(self)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.activated.connect(self._insert_completion)
        self._refresh_completions()

    # --- line numbers ---------------------------------------------------------------

    def line_number_width(self):
        return 12 + self.fontMetrics().horizontalAdvance("9") * max(3, len(str(self.blockCount())))

    def _update_margin(self, *_):
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _scroll_line_numbers(self, rect, dy):
        if dy:
            self._line_numbers.scroll(0, dy)
        else:
            self._line_numbers.update(0, rect.y(), self._line_numbers.width(), rect.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        contents = self.contentsRect()
        self._line_numbers.setGeometry(QRect(contents.left(), contents.top(), self.line_number_width(), contents.height()))

    def paint_line_numbers(self, event):
        painter = QPainter(self._line_numbers)
        base = self.palette().color(QPalette.Base)
        painter.fillRect(event.rect(), base.darker(106) if base.lightness() > 128 else base.lighter(125))
        text_colour = self.palette().color(QPalette.Text)
        text_colour.setAlpha(120)
        current = self.textCursor().blockNumber()
        block = self.firstVisibleBlock()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        height = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible():
                colour = self.palette().color(QPalette.Text) if block.blockNumber() == current else text_colour
                painter.setPen(colour)
                painter.drawText(0, top, self._line_numbers.width() - 6, height, Qt.AlignRight,
                                 str(block.blockNumber() + 1))
            top += int(self.blockBoundingRect(block).height())
            block = block.next()

    def _highlight_current_line(self):
        selection = QTextEdit.ExtraSelection()
        colour = self.palette().color(QPalette.Highlight)
        colour.setAlpha(28)
        selection.format.setBackground(colour)
        selection.format.setProperty(QTextFormat.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        self.setExtraSelections([selection])
        self._line_numbers.update()

    # --- completion -----------------------------------------------------------------

    def set_completion_words(self, words):
        """Extra words: control names, handler names, DBC signals."""
        self._extra_words = sorted(set(w for w in words if w))
        self._refresh_completions()

    def _refresh_completions(self):
        words = set(keyword.kwlist) | set(API_WORDS) | set(self._extra_words)
        words |= set(re.findall(r"\b[A-Za-z_]\w{2,}\b", self.toPlainText()))
        self.completer.setModel(QStringListModel(sorted(words, key=str.lower), self.completer))

    def _prefix(self):
        cursor = self.textCursor()
        line = cursor.block().text()[:cursor.positionInBlock()]
        match = re.search(r"[A-Za-z_][\w.]*$", line)
        return match.group(0) if match else ""

    def _insert_completion(self, completion):
        cursor = self.textCursor()
        prefix = self._prefix()
        cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, len(prefix))
        cursor.insertText(completion)
        self.setTextCursor(cursor)

    def _show_completions(self, forced=False):
        prefix = self._prefix()
        if not forced and (len(prefix) < 3 and not prefix.endswith(".")):
            self.completer.popup().hide()
            return
        self._refresh_completions()
        self.completer.setCompletionPrefix(prefix)
        if self.completer.completionCount() == 0 or (
                self.completer.completionCount() == 1 and self.completer.currentCompletion() == prefix):
            self.completer.popup().hide()
            return
        self.completer.popup().setCurrentIndex(self.completer.completionModel().index(0, 0))
        rect = self.cursorRect()
        rect.setWidth(self.completer.popup().sizeHintForColumn(0) + 30)
        self.completer.complete(rect)

    # --- editing ----------------------------------------------------------------------

    def keyPressEvent(self, event):
        popup = self.completer.popup()
        if popup.isVisible() and event.key() in (Qt.Key_Enter, Qt.Key_Return, Qt.Key_Tab, Qt.Key_Escape):
            event.ignore()  # the completer handles these
            return
        if event.key() == Qt.Key_Space and event.modifiers() & Qt.ControlModifier:
            self._show_completions(forced=True)
            return
        if event.key() == Qt.Key_Tab and not event.modifiers():
            self.insertPlainText("    ")
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            line = self.textCursor().block().text()[:self.textCursor().positionInBlock()]
            indent = re.match(r"\s*", line).group(0)
            if line.rstrip().endswith(":"):
                indent += "    "
            super().keyPressEvent(event)
            self.insertPlainText(indent)
            return
        super().keyPressEvent(event)
        if event.text() and (event.text().isalnum() or event.text() in "._"):
            self._show_completions()
        elif popup.isVisible():
            popup.hide()

    def check_syntax(self, filename="<script>"):
        """(ok, message, line) for the current code."""
        try:
            compile(self.toPlainText(), filename, "exec")
            return True, "No syntax errors", None
        except SyntaxError as exc:
            return False, f"Line {exc.lineno}: {exc.msg}", exc.lineno

    def go_to_line(self, line, column=0):
        block = self.document().findBlockByNumber(max(0, int(line) - 1))
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, min(column, block.length() - 1))
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()
