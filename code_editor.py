"""
Python code editor for panel scripts: syntax highlighting, line numbers, current-line highlight,
auto-indent, and completion of Python keywords, the script API, control names and DBC signals;
plus the side panel listing the ISO 14229 UDS functions, which inserts calls into the script.
"""
import builtins
import html
import keyword
import re

from PyQt5.QtCore import QRect, QRegularExpression, QSize, QStringListModel, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPalette, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextFormat
from PyQt5.QtWidgets import (QCompleter, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSplitter,
                             QTextBrowser, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from uds_library import EXCLUDED_SERVICES, FUNCTIONS, GROUPS

API_WORDS = [
    "api", "api.on", "api.on_can", "api.every", "api.sleep", "api.running", "api.log", "api.progress",
    "api.flash_cancelled", "api.signal", "api.set_signal", "api.send_message", "api.can.send",
    "api.can.get_latest_messages", "api.ui.get_value", "api.ui.set_value", "api.uds.request",
    "api.uds.tester_present", "api.uds.rdbi", "api.uds.request_download", "api.uds.transfer_data",
    "api.uds.request_transfer_exit", "api.uds.transfer_data_from_file", "api.dll.load", "api.dll.call",
    "on_start", "on_stop", "on_timer", "on_message", "on_signal", "on_control", "DatabaseMainFunction",
    "Flashing", "frame.id", "frame.data", "frame.signals",
    ".ok", ".data", ".text", ".int", ".nrc", ".nrc_name", ".error", ".raw", ".max_block_length",
] + [entry.name for entry in FUNCTIONS]


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

    def insert_snippet(self, text):
        """Insert a call: at the cursor on an empty line, else on a new line below (indented one level
        deeper after a line ending in ':')."""
        cursor = self.textCursor()
        line = cursor.block().text()
        if cursor.hasSelection() or not line.strip():
            cursor.insertText(text)
        else:
            indent = re.match(r"\s*", line).group(0) + ("    " if line.rstrip().endswith(":") else "")
            cursor.movePosition(QTextCursor.EndOfBlock)
            cursor.insertText("\n" + indent + text)
        self.setTextCursor(cursor)
        self.setFocus()

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


class UdsFunctionPanel(QWidget):
    """ISO 14229 service functions grouped by functional unit; double-click (or Insert) adds a call."""
    insert_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("UDS functions (ISO 14229-1)")
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter: name, service or SID (e.g. 22)...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)
        splitter = QSplitter(Qt.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Function", "SID", "Service"])
        self.tree.setColumnWidth(0, 150)
        self.tree.setColumnWidth(1, 44)
        self.items = {}
        for group in GROUPS:
            parent = QTreeWidgetItem(self.tree, [group])
            parent.setFirstColumnSpanned(True)
            font = parent.font(0)
            font.setBold(True)
            parent.setFont(0, font)
            parent.setFlags(Qt.ItemIsEnabled)
            for entry in (e for e in FUNCTIONS if e.group == group):
                item = QTreeWidgetItem(parent, [entry.name, f"{entry.sid:02X}" if entry.sid is not None else "",
                                                entry.service])
                item.setData(0, Qt.UserRole, entry.name)
                item.setToolTip(0, entry.signature)
                self.items[entry.name] = item
            parent.setExpanded(True)
        self.tree.currentItemChanged.connect(self._show_details)
        self.tree.itemDoubleClicked.connect(lambda item, _column: self._insert(item))
        splitter.addWidget(self.tree)
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        splitter.addWidget(self.details)
        splitter.setSizes([420, 220])
        layout.addWidget(splitter, 1)
        row = QHBoxLayout()
        self.insert_button = QPushButton("Insert")
        self.insert_button.setToolTip("Insert the example call into the script (or double-click a function)")
        self.insert_button.clicked.connect(lambda: self._insert(self.tree.currentItem()))
        self.insert_button.setEnabled(False)
        row.addWidget(self.insert_button)
        excluded = ", ".join(f"0x{sid:02X} {name}" for sid, name in EXCLUDED_SERVICES.items())
        note = QLabel(f"Not included: {excluded}")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: 11px;")
        row.addWidget(note, 1)
        layout.addLayout(row)
        self.details.setHtml("<p>Select a function to see its parameters.</p><p>Every function returns a result: "
                             "<code>if result:</code> tests for a positive response; <code>result.data</code>, "
                             "<code>.text</code>, <code>.int</code>, <code>.error</code>, <code>.nrc</code>.</p>")

    def _entry(self, item):
        name = item.data(0, Qt.UserRole) if item is not None else None
        return next((e for e in FUNCTIONS if e.name == name), None)

    def _show_details(self, item, _previous=None):
        entry = self._entry(item)
        self.insert_button.setEnabled(entry is not None)
        if entry is None:
            return
        service = f"0x{entry.sid:02X} {entry.service}" if entry.sid is not None else entry.service
        doc = html.escape(entry.doc).replace("\n", "<br>")
        self.details.setHtml(f"<p><b>{html.escape(entry.signature)}</b><br><i>{html.escape(service)}</i></p>"
                             f"<p>{doc}</p><p>Example:<br><code>{html.escape(entry.example)}</code></p>")

    def _insert(self, item):
        entry = self._entry(item)
        if entry is not None:
            self.insert_requested.emit(entry.example)

    def _apply_filter(self, text):
        text = text.strip().lower()
        text = text[2:] if text.startswith("0x") else text
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            visible = 0
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                haystack = " ".join(child.text(column) for column in range(3)).lower()
                shown = not text or text in haystack
                child.setHidden(not shown)
                visible += shown
            parent.setHidden(visible == 0)
