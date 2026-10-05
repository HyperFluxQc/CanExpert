"""
Text editing for the Form Designer's Python script and Variables tabs: line numbers, the current line, Tab and
Shift+Tab to indent and unindent lines, a comment switched on and off, Go to line - and the find bar: Find
(Ctrl+F) and Replace (Ctrl+H), every match highlighted, F3 and Shift+F3 to the next and the one before.
"""
import re
from bisect import bisect_left, bisect_right

from PyQt5.QtCore import QEvent, QRect, QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter, QPalette, QTextCursor, QTextFormat
from PyQt5.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QPlainTextEdit, QPushButton, QStyle, QTextEdit, QToolButton, QVBoxLayout, QWidget)

INDENT = "    "
WIDE = re.compile("[\U00010000-\U0010FFFF]")


class _Positions:
    """Python string indexes and the document positions they stand for: past a character beyond the Basic
    Multilingual Plane (an emoji), Qt counts one more - it takes two UTF-16 units."""

    def __init__(self, text):
        self._wide = [match.start() for match in WIDE.finditer(text)]
        self._ends = [index + number + 2 for number, index in enumerate(self._wide)]   # where each one ends, in Qt

    def to_qt(self, index):
        return index + bisect_left(self._wide, index)

    def to_python(self, position):
        return position - bisect_right(self._ends, position)


class _LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_line_numbers(event)


class TextEditor(QPlainTextEdit):
    """A plain text editor with line numbers, the current line and a search's matches highlighted (FindBar), Tab
    and Shift+Tab indenting and unindenting the selected lines, toggle_comment() and Go to line."""
    comment = "#"            # what starts a comment line (toggle_comment)

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont("Consolas", 10)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.find_bar = None            # the FindBar under it, if any (Esc closes it)
        self._found = []                # the highlighted matches (ExtraSelections)
        self._line_numbers = _LineNumbers(self)
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._scroll_line_numbers)
        self.cursorPositionChanged.connect(self._refresh_selections)
        self._update_margin()
        self._refresh_selections()

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

    # --- highlights: the current line, a search's matches -------------------------------

    def show_found(self, matches, current=None):
        """Highlight matches - (start, end) document positions - and, stronger, the current one."""
        dark = self.palette().color(QPalette.Base).lightness() < 128
        self._found = []
        for start, end in matches:
            selection = QTextEdit.ExtraSelection()
            strong = (start, end) == current
            selection.format.setBackground(QColor(("#8a5a14" if strong else "#5c5320") if dark else
                                                  ("#ffc35c" if strong else "#fff0a0")))
            selection.cursor = QTextCursor(self.document())
            selection.cursor.setPosition(start)
            selection.cursor.setPosition(end, QTextCursor.KeepAnchor)
            self._found.append(selection)
        self._refresh_selections()

    def _refresh_selections(self):
        line = QTextEdit.ExtraSelection()
        colour = self.palette().color(QPalette.Highlight)
        colour.setAlpha(28)
        line.format.setBackground(colour)
        line.format.setProperty(QTextFormat.FullWidthSelection, True)
        line.cursor = self.textCursor()
        line.cursor.clearSelection()
        self.setExtraSelections([line] + self._found)
        self._line_numbers.update()

    # --- editing ----------------------------------------------------------------------

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.find_bar is not None and self.find_bar.is_open:
            self.find_bar.close_bar()
            return
        if event.key() == Qt.Key_Tab and not event.modifiers():
            if self._several_lines():
                self.indent_lines()
            else:
                self.insertPlainText(INDENT)
            return
        if event.key() == Qt.Key_Backtab:
            self.indent_lines(unindent=True)
            return
        super().keyPressEvent(event)

    def _several_lines(self):
        cursor = self.textCursor()
        document = self.document()
        return document.findBlock(cursor.selectionStart()) != document.findBlock(cursor.selectionEnd())

    def _selected_blocks(self):
        """The lines the selection - or the cursor - is on; a selection ending where a line starts leaves it out."""
        cursor = self.textCursor()
        first = self.document().findBlock(cursor.selectionStart())
        last = self.document().findBlock(cursor.selectionEnd())
        if cursor.hasSelection() and last != first and last.position() == cursor.selectionEnd():
            last = last.previous()
        blocks = [first]
        while blocks[-1] != last:
            blocks.append(blocks[-1].next())
        return blocks

    def _select_lines(self, blocks):
        cursor = self.textCursor()
        cursor.setPosition(blocks[0].position())
        cursor.setPosition(blocks[-1].position() + blocks[-1].length() - 1, QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)

    def indent_lines(self, unindent=False):
        """The selected lines one level in (blank ones left as they are) - or, with unindent, one level out."""
        blocks = self._selected_blocks()
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for block in blocks:
            edit = QTextCursor(block)
            text = block.text()
            if unindent:
                spaces = len(text) - len(text.lstrip(" "))
                edit.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor,
                                  min(spaces, len(INDENT)) or int(text.startswith("\t")))
                edit.removeSelectedText()
            elif text.strip() or len(blocks) == 1:
                edit.insertText(INDENT)
        cursor.endEditBlock()
        if len(blocks) > 1:
            self._select_lines(blocks)

    def toggle_comment(self):
        """The selected lines (or the cursor's) made comments - or, when they all are, made code again. Blank
        lines are left as they are."""
        blocks = [block for block in self._selected_blocks() if block.text().strip()]
        if not blocks:
            return
        prefix = self.comment
        indents = [len(block.text()) - len(block.text().lstrip()) for block in blocks]
        uncomment = all(block.text().lstrip().startswith(prefix) for block in blocks)
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for block, indent in zip(blocks, indents):
            edit = QTextCursor(block)
            if uncomment:
                rest = block.text()[indent + len(prefix):]
                edit.setPosition(block.position() + indent)
                edit.setPosition(block.position() + indent + len(prefix) + rest.startswith(" "), QTextCursor.KeepAnchor)
                edit.removeSelectedText()
            else:
                edit.setPosition(block.position() + min(indents))
                edit.insertText(prefix + " ")
        cursor.endEditBlock()

    def go_to_line(self, line, column=0):
        block = self.document().findBlockByNumber(max(0, int(line) - 1))
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.Right, QTextCursor.MoveAnchor, min(column, block.length() - 1))
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()

    def ask_line(self):
        """Go to line (Ctrl+G): asks which."""
        line, ok = QInputDialog.getInt(self, "Go to line", f"Line (1 to {self.blockCount()}):",
                                       self.textCursor().blockNumber() + 1, 1, self.blockCount())
        if ok:
            self.go_to_line(line)


class FindBar(QFrame):
    """Find - and Replace - under an editor. Every match is highlighted as you type and the next one from the
    cursor selected, wrapping around at the end; Enter (F3) goes to the next one, Shift+Enter (Shift+F3) to the
    one before. Match case, whole words, and regular expressions (Python's: \\1 in the replacement is the first
    group). Replace replaces the selected match and goes to the next; Replace all, one undo step. Esc closes it."""
    MOST_SHOWN = 5000                   # highlighted matches, at most

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        editor.find_bar = self
        self.setFrameShape(QFrame.StyledPanel)
        self._found = []
        self._origin = 0                # where a search being typed starts from
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find")
        self.find_edit.setClearButtonEnabled(True)
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("Replace with")
        self.case_box = QCheckBox("Match case")
        self.word_box = QCheckBox("Whole words")
        self.word_box.setToolTip("Only where it is a word of its own: speed, not speed_max")
        self.regex_box = QCheckBox("Regex")
        self.regex_box.setToolTip("A regular expression (Python's): .* any text, \\d a digit, ^ a line's start... "
                                  "In the replacement, \\1 is what its first group found")
        self.previous_button = self._tool_button(Qt.UpArrow, "The one before (Shift+Enter, Shift+F3)",
                                                 lambda: self.find(backwards=True))
        self.next_button = self._tool_button(Qt.DownArrow, "The next one (Enter, F3)", self.find)
        self.status = QLabel()
        self.status.setMinimumWidth(self.status.fontMetrics().horizontalAdvance("9999 of 9999, from the top") + 4)
        close = QToolButton()
        close.setIcon(self.style().standardIcon(QStyle.SP_TitleBarCloseButton))
        close.setAutoRaise(True)
        close.setToolTip("Close (Esc)")
        close.clicked.connect(self.close_bar)
        self.replace_button = QPushButton("Replace")
        self.replace_button.setToolTip("Replace the selected match and go to the next one (Enter in the box)")
        self.replace_all_button = QPushButton("Replace all")
        for button in (self.replace_button, self.replace_all_button):
            button.setAutoDefault(False)            # Enter in a box of the window does not press them
        self.replace_button.clicked.connect(self.replace)
        self.replace_all_button.clicked.connect(self.replace_all)

        layout = QGridLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setVerticalSpacing(3)
        layout.addWidget(QLabel("Find"), 0, 0)
        layout.addWidget(self.find_edit, 0, 1)
        layout.addWidget(self.previous_button, 0, 2)
        layout.addWidget(self.next_button, 0, 3)
        layout.addWidget(self.status, 0, 4)
        layout.addWidget(self.case_box, 0, 5)
        layout.addWidget(self.word_box, 0, 6)
        layout.addWidget(self.regex_box, 0, 7)
        layout.addWidget(close, 0, 8)
        replace_label = QLabel("Replace")
        buttons = QWidget()
        button_row = QHBoxLayout(buttons)
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.addWidget(self.replace_button)
        button_row.addWidget(self.replace_all_button)
        button_row.addStretch(1)
        layout.addWidget(replace_label, 1, 0)
        layout.addWidget(self.replace_edit, 1, 1)
        layout.addWidget(buttons, 1, 2, 1, 7)
        layout.setColumnStretch(1, 1)
        self.replace_widgets = (replace_label, self.replace_edit, buttons)

        self.find_edit.textChanged.connect(self._search_typed)
        for box in (self.case_box, self.word_box, self.regex_box):
            box.toggled.connect(self._search_typed)
        for edit in (self.find_edit, self.replace_edit):
            edit.installEventFilter(self)
        self._timer = QTimer(self)                  # the text edited: its matches found again
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.refresh)
        editor.textChanged.connect(lambda: self.is_open and self._timer.start())
        self.hide()

    def _tool_button(self, arrow, tip, slot):
        button = QToolButton()
        button.setArrowType(arrow)
        button.setAutoRaise(True)
        button.setToolTip(tip)
        button.clicked.connect(lambda _checked=False: slot())
        return button

    @property
    def is_open(self) -> bool:
        """Open under its editor - even while its tab is not the one in front."""
        return not self.isHidden()

    @property
    def replacing(self) -> bool:
        return not self.replace_edit.isHidden()

    def open(self, replace=False):
        """Show the bar - with the Replace line too, with replace - the selected text to find, if it is on one line."""
        selected = self.editor.textCursor().selectedText()
        if selected and " " not in selected:          # Qt's paragraph separator: not on one line
            self.find_edit.blockSignals(True)
            self.find_edit.setText(re.escape(selected) if self.regex_box.isChecked() else selected)
            self.find_edit.blockSignals(False)
        for widget in self.replace_widgets:
            widget.setVisible(replace)
        self.show()
        self.find_edit.setFocus()
        self.find_edit.selectAll()
        self._origin = self.editor.textCursor().selectionStart()
        self.refresh(select=True)

    def close_bar(self):
        self.hide()
        self._timer.stop()
        self.editor.show_found([])
        self.editor.setFocus()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.close_bar()
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if watched is self.replace_edit:
                    self.replace()
                else:
                    self.find(backwards=bool(event.modifiers() & Qt.ShiftModifier))
                return True
        return super().eventFilter(watched, event)

    # --- searching ------------------------------------------------------------------------

    def pattern(self):
        """What is searched, as a compiled regular expression - None when there is nothing to find. re.error for a
        regular expression that is not one."""
        text = self.find_edit.text()
        if not text:
            return None
        source = text if self.regex_box.isChecked() else re.escape(text)
        if self.word_box.isChecked():
            source = rf"(?<!\w)(?:{source})(?!\w)"
        return re.compile(source, re.MULTILINE | (0 if self.case_box.isChecked() else re.IGNORECASE))

    def _search(self):
        """The matches, as (start, end) document positions, and what is wrong with the search ("" when nothing)."""
        try:
            pattern = self.pattern()
        except re.error as exc:
            return [], f"Not a regular expression: {exc}"
        if pattern is None:
            return [], ""
        text = self.editor.toPlainText()
        positions = _Positions(text)
        return [(positions.to_qt(match.start()), positions.to_qt(match.end()))
                for match in pattern.finditer(text) if match.end() > match.start()], ""

    def _current(self):
        """The match that is selected, or None."""
        cursor = self.editor.textCursor()
        selected = (cursor.selectionStart(), cursor.selectionEnd())
        index = bisect_left(self._found, selected)
        return selected if index < len(self._found) and self._found[index] == selected else None

    def refresh(self, select=False, wrapped=""):
        """Find the matches again and highlight them - with select, selecting the first one from where the search
        started."""
        self._found, error = self._search()
        if select and self._found:
            starts = [start for start, _end in self._found]
            index = bisect_left(starts, self._origin)
            self._select(self._found[index] if index < len(self._found) else self._found[0])
        self.editor.show_found(self._found[:self.MOST_SHOWN], self._current())
        self._show_status(error, wrapped)

    def _search_typed(self, *_):
        self._origin = self.editor.textCursor().selectionStart()
        self.refresh(select=True)

    def find(self, backwards=False) -> bool:
        """Select the next match after the selection - or, backwards, the one before it - wrapping around."""
        self._found, error = self._search()
        if not self._found:
            self.editor.show_found([])
            self._show_status(error)
            return False
        cursor = self.editor.textCursor()
        starts = [start for start, _end in self._found]
        if backwards:
            index = bisect_left(starts, cursor.selectionStart()) - 1
            wrapped = "from the bottom" if index < 0 else ""
        else:
            index = bisect_left(starts, cursor.selectionEnd())
            wrapped = "from the top" if index >= len(self._found) else ""
            index = index % len(self._found)
        match = self._found[index]
        self._select(match)
        self._origin = match[0]
        self.editor.show_found(self._found[:self.MOST_SHOWN], match)
        self._show_status(error, wrapped)
        return True

    def _select(self, match):
        cursor = self.editor.textCursor()
        cursor.setPosition(match[0])
        cursor.setPosition(match[1], QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cursor)

    def _show_status(self, error="", wrapped=""):
        current = self._current()
        if error:
            text = error
        elif not self.find_edit.text():
            text = ""
        elif not self._found:
            text = "No match"
        elif current is not None:
            text = f"{self._found.index(current) + 1} of {len(self._found)}" + (f", {wrapped}" if wrapped else "")
        else:
            text = f"{len(self._found)} match{'es' if len(self._found) != 1 else ''}"
        self.status.setText(text)
        self.status.setToolTip(text if error else "")
        self.status.setStyleSheet("color: #c42b1c;" if error or text == "No match" else "")

    # --- replacing ------------------------------------------------------------------------

    def _replacement(self, match):
        """What a match is replaced with: the Replace box - a regular expression's groups put in. re.error / IndexError
        for a group that is not there."""
        text = self.replace_edit.text()
        return match.expand(text) if self.regex_box.isChecked() else text

    def replace(self):
        """Replace the selected match and select the next one - or, when no match is selected, select the next."""
        self._found, error = self._search()
        current = self._current()
        if current is not None:
            text = self.editor.toPlainText()
            positions = _Positions(text)
            match = self.pattern().match(text, positions.to_python(current[0]))
            if match is not None and positions.to_qt(match.end()) == current[1]:
                try:
                    replacement = self._replacement(match)
                except (re.error, IndexError) as exc:
                    self._show_status(f"The replacement: {exc}")
                    return
                cursor = self.editor.textCursor()
                cursor.insertText(replacement)
                self.editor.setTextCursor(cursor)
        self.find()

    def replace_all(self) -> int:
        """Replace every match, in one undo step; says how many."""
        try:
            pattern = self.pattern()
        except re.error as exc:
            self._show_status(f"Not a regular expression: {exc}")
            return 0
        if pattern is None:
            return 0
        text = self.editor.toPlainText()
        positions = _Positions(text)
        try:
            changes = [(positions.to_qt(match.start()), positions.to_qt(match.end()), self._replacement(match))
                       for match in pattern.finditer(text) if match.end() > match.start()]
        except (re.error, IndexError) as exc:
            self._show_status(f"The replacement: {exc}")
            return 0
        cursor = QTextCursor(self.editor.document())
        cursor.beginEditBlock()
        for start, end, replacement in reversed(changes):
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            cursor.insertText(replacement)
        cursor.endEditBlock()
        self.refresh()
        self.status.setText(f"{len(changes)} replaced")
        self.status.setStyleSheet("")
        return len(changes)


class EditorPane(QWidget):
    """An editor with its find bar under it."""

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.find_bar = FindBar(editor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(editor, 1)
        layout.addWidget(self.find_bar)
