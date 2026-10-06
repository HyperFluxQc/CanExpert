"""
Text editing for the Form Designer's Python script and Variables tabs: line numbers, the current line, Tab and
Shift+Tab to indent and unindent lines, a comment switched on and off, Go to line, block (column) editing - and
the find bar: Find (Ctrl+F) and Replace (Ctrl+H), every match highlighted, F3 and Shift+F3 to the next and the one
before.
"""
import re
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from dataclasses import dataclass

from PyQt5.QtCore import QEvent, QMimeData, QRect, QRectF, QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QFontMetricsF, QKeySequence, QPainter, QPalette, QTextCursor, QTextFormat
from PyQt5.QtWidgets import (QApplication, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                             QLineEdit, QMenu, QPlainTextEdit, QPushButton, QStyle, QTextEdit, QToolButton,
                             QVBoxLayout, QWidget)

INDENT = "    "
WIDE = re.compile("[\U00010000-\U0010FFFF]")
TAB_WIDTH = 4                                   # columns to a tab stop: setTabStopDistance is four spaces
BLOCK_MIME = "application/x-canexpert-block"    # with a block's text on the clipboard: it pastes as a block


def visual_column(text, index):
    """The column text[:index] ends at, as the line shows it: a tab reaches the next tab stop."""
    column = 0
    for character in text[:index]:
        column = (column // TAB_WIDTH + 1) * TAB_WIDTH if character == "\t" else column + 1
    return column


def index_at(text, column):
    """The index of the character at a column of a line, as it shows; len(text) past its end."""
    current = 0
    for index, character in enumerate(text):
        if current >= column:
            return index
        current = (current // TAB_WIDTH + 1) * TAB_WIDTH if character == "\t" else current + 1
    return len(text)


def qt_offset(text, index):
    """Where text[:index] ends, counted as Qt counts in a line: a character beyond the BMP takes two."""
    return index + len(WIDE.findall(text[:index]))


def python_index(text, offset):
    """The index into a line of a position Qt gives in it (QTextCursor.positionInBlock)."""
    count = 0
    for index, character in enumerate(text):
        if count >= offset:
            return index
        count += 2 if ord(character) > 0xFFFF else 1
    return len(text)


@dataclass
class Block:
    """A block of columns over whole lines: where it was begun (the anchor) and where its caret is - lines by
    number, columns as the lines show them (visual_column, index_at)."""
    anchor_line: int
    anchor_column: int
    line: int
    column: int

    @property
    def top(self) -> int:
        return min(self.anchor_line, self.line)

    @property
    def bottom(self) -> int:
        return max(self.anchor_line, self.line)

    @property
    def left(self) -> int:
        return min(self.anchor_column, self.column)

    @property
    def right(self) -> int:
        return max(self.anchor_column, self.column)


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
    and Shift+Tab indenting and unindenting the selected lines, toggle_comment(), Go to line, and block editing:
    Alt + drag or Shift + Alt + arrows select a block of columns over several lines (a drag and Shift + arrows too,
    in block_mode), and what is typed, Backspace, Delete, Tab, Copy, Cut and Paste act on each of its lines."""
    comment = "#"            # what starts a comment line (toggle_comment)
    BLOCK_ARROWS = {Qt.Key_Up: (-1, 0), Qt.Key_Down: (1, 0), Qt.Key_Left: (0, -1), Qt.Key_Right: (0, 1)}

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont("Consolas", 10)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.find_bar = None            # the FindBar under it, if any (Esc closes it)
        self._found = []                # the highlighted matches (ExtraSelections)
        self.block = None               # the block of columns selected, or None
        self.block_mode = False         # Edit > Block selection mode: a drag and Shift + arrows select blocks
        self._block_busy = 0            # the block's own edits and moves going on: they do not end it
        self._block_typing = False      # what is typed in a row is one undo step
        self._block_drag = False
        self._line_numbers = _LineNumbers(self)
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._scroll_line_numbers)
        self.cursorPositionChanged.connect(self._refresh_selections)
        self.cursorPositionChanged.connect(self._end_block_if_moved)
        self.textChanged.connect(self._end_block_if_moved)
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
        if self.block_key(event):
            return
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

    def selectAll(self):
        self.end_block()
        super().selectAll()

    def remove_selected(self):
        """Edit > Delete: the block's columns, or the text selected."""
        if self.block is not None:
            self._type_in_block("")
        else:
            self.textCursor().removeSelectedText()

    # --- block (column) editing ---------------------------------------------------------------

    @contextmanager
    def _block_edit(self):
        """The block's own edits and cursor moves: they do not end it."""
        self._block_busy += 1
        try:
            yield
        finally:
            self._block_busy -= 1

    def _end_block_if_moved(self, *_):
        """Anything else moving the cursor or changing the text - a click, a key, Find, Undo - ends the block."""
        if self.block is not None and not self._block_busy:
            self.end_block()

    def end_block(self):
        if self.block is not None:
            self.block = None
            self._block_typing = False
            self.viewport().update()

    def set_block(self, block):
        """Select a block of columns. The text cursor goes to its caret, so the view follows it."""
        self.block = block
        line = self.document().findBlockByNumber(block.line)
        text = line.text()
        cursor = QTextCursor(line)
        cursor.setPosition(line.position() + qt_offset(text, index_at(text, block.column)))
        with self._block_edit():
            self.setTextCursor(cursor)
        self.viewport().update()

    def _char_width(self):
        return QFontMetricsF(self.font()).horizontalAdvance(" ")

    def _x_of(self, line, column):
        """Where a column of a line is across the viewport: as the text is laid out, within it - so it holds
        whatever the display's scaling - and a space's width a column past its end."""
        text = line.text()
        end = visual_column(text, len(text))
        cursor = QTextCursor(line)
        cursor.setPosition(line.position() + qt_offset(text, index_at(text, min(column, end))))
        return self.cursorRect(cursor).left() + max(0, column - end) * self._char_width()

    def _cursor_place(self):
        """(line, column) of the text cursor."""
        cursor = self.textCursor()
        text = cursor.block().text()
        return cursor.blockNumber(), visual_column(text, python_index(text, cursor.positionInBlock()))

    def _place_at(self, point):
        """(line, column) at a point of the viewport: within the text, where Qt would put the cursor; past the end
        of the line, a column a space's width."""
        cursor = self.cursorForPosition(point)
        line = cursor.block()
        text = line.text()
        end = visual_column(text, len(text))
        past = (point.x() - self._x_of(line, end)) / self._char_width()
        if past < 0.5:
            return line.blockNumber(), visual_column(text, python_index(text, cursor.positionInBlock()))
        return line.blockNumber(), end + round(past)

    def _block_lines(self):
        line = self.document().findBlockByNumber(self.block.top)
        for _number in range(self.block.top, self.block.bottom + 1):
            if not line.isValid():
                return
            yield line
            line = line.next()

    def _change_lines(self, change, join=False):
        """change(line's text) gives (start, end, text) - its characters start to end replaced with text - or
        None, for each line of the block: all of them one undo step, joined to the one before while typing."""
        cursor = QTextCursor(self.document())
        with self._block_edit():
            if join:
                cursor.joinPreviousEditBlock()
            else:
                cursor.beginEditBlock()
            try:
                for line in list(self._block_lines()):
                    text = line.text()
                    edit = change(text)
                    if edit is None:
                        continue
                    start, end, new = edit
                    cursor.setPosition(line.position() + qt_offset(text, start))
                    cursor.setPosition(line.position() + qt_offset(text, end), QTextCursor.KeepAnchor)
                    cursor.insertText(new)
            finally:
                cursor.endEditBlock()

    def _type_in_block(self, text, join=False):
        """text in place of the block's columns on each of its lines - a line too short is filled with spaces up
        to them first - then a caret after it on every line."""
        block = self.block
        left, right = block.left, block.right

        def change(line):
            end = visual_column(line, len(line))
            if end < left:
                return (len(line), len(line), " " * (left - end) + text) if text else None
            return index_at(line, left), index_at(line, right), text
        self._change_lines(change, join)
        column = left + len(text)
        self.set_block(Block(block.anchor_line, column, block.line, column))

    def _block_backspace(self):
        block = self.block
        if block.left != block.right or block.column == 0:
            if block.left != block.right:
                self._type_in_block("")
            return
        column = block.column

        def change(line):
            if visual_column(line, len(line)) < column:
                return None                             # nothing there: only the caret moves
            end = index_at(line, column)
            return (end - 1, end, "") if end > 0 else None
        self._change_lines(change)
        self.set_block(Block(block.anchor_line, column - 1, block.line, column - 1))

    def _block_delete(self):
        block = self.block
        if block.left != block.right:
            self._type_in_block("")
            return

        def change(line):
            start = index_at(line, block.column)
            return (start, start + 1, "") if start < len(line) else None
        self._change_lines(change)
        self.set_block(block)

    def block_text(self):
        """The block's columns: a line of text for each of its lines."""
        block = self.block
        return "\n".join(text[index_at(text, block.left):index_at(text, block.right)]
                         for text in (line.text() for line in self._block_lines()))

    def copy(self):
        if self.block is None:
            super().copy()
            return
        data = QMimeData()
        data.setText(self.block_text())
        data.setData(BLOCK_MIME, b"block")
        QApplication.clipboard().setMimeData(data)

    def cut(self):
        if self.block is None:
            super().cut()
            return
        self.copy()
        self._type_in_block("")

    def insertFromMimeData(self, source):
        """What every paste comes through - the keys, the Edit menu, the context menu, a drop. A block copied
        pastes as a block, here or anywhere. In a block, a line of text goes on each of its lines, and as many
        lines as it has, one on each."""
        text = source.text().replace("\r\n", "\n")
        rows = text.split("\n")
        if source.hasFormat(BLOCK_MIME):
            self._paste_block(rows)
        elif self.block is None:
            super().insertFromMimeData(source)
        elif len(rows) == 1:
            self._type_in_block(text)
        elif len(rows) == self.block.bottom - self.block.top + 1:
            self._paste_block(rows)
        else:
            self.end_block()
            super().insertFromMimeData(source)

    def contextMenuEvent(self, event):
        """In a block, a menu that acts on it: the standard one sees nothing selected."""
        if self.block is None:
            super().contextMenuEvent(event)
            return
        menu = QMenu(self)
        for text, slot in (("Cu&t", self.cut), ("&Copy", self.copy), ("&Paste", self.paste),
                           ("&Delete", self.remove_selected)):
            menu.addAction(text, slot)
        menu.exec_(event.globalPos())

    def _paste_block(self, rows):
        """rows one under the other, from the caret's column - from the block's left edge, in place of its columns,
        in a block - on lines filled with spaces up to it when short, and added at the end when there are too few;
        one undo step. The cursor ends after the last row."""
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        try:
            if self.block is not None:
                top, column = self.block.top, self.block.left
                if self.block.left != self.block.right:
                    self._type_in_block("")             # its columns go first, in the same undo step
            else:
                top, column = self._cursor_place()
            with self._block_edit():
                missing = top + len(rows) - self.blockCount()
                if missing > 0:
                    cursor.movePosition(QTextCursor.End)
                    cursor.insertText("\n" * missing)
                line = self.document().findBlockByNumber(top)
                for row in rows:
                    text = line.text()
                    end = visual_column(text, len(text))
                    cursor.setPosition(line.position() + qt_offset(text, len(text) if end < column
                                                                   else index_at(text, column)))
                    cursor.insertText(" " * max(0, column - end) + row)
                    after = cursor.position()
                    line = line.next()
        finally:
            cursor.endEditBlock()
        self.end_block()
        caret = self.textCursor()
        caret.setPosition(after)
        self.setTextCursor(caret)

    def block_key(self, event) -> bool:
        """The keys of block editing - True when the key was one: Shift + Alt + arrows (Shift + arrows in block
        mode) select a block, or make it larger or smaller; in one, what is typed (AltGr's characters too),
        Backspace, Delete, Tab, Copy, Cut and Paste act on each of its lines, and Esc ends it. Any other key ends
        it and does what it does."""
        key, modifiers = event.key(), event.modifiers()
        shift, alt, ctrl = (bool(modifiers & flag) for flag in (Qt.ShiftModifier, Qt.AltModifier, Qt.ControlModifier))
        if key in self.BLOCK_ARROWS and shift and not ctrl and (alt or self.block_mode):
            block = self.block
            if block is None:
                line, column = self._cursor_place()
                block = Block(line, column, line, column)
            lines, columns = self.BLOCK_ARROWS[key]
            line = min(max(0, block.line + lines), self.blockCount() - 1)
            self._block_typing = False
            self.set_block(Block(block.anchor_line, block.anchor_column, line, max(0, block.column + columns)))
            return True
        if self.block is None:
            return False
        if key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr):
            return True                                 # a modifier on its own: the block stays
        typed = False
        if key == Qt.Key_Escape:
            self.end_block()
        elif event.matches(QKeySequence.Copy):
            self.copy()
        elif event.matches(QKeySequence.Cut):
            self.cut()
        elif event.matches(QKeySequence.Paste):
            self.paste()
        elif key == Qt.Key_Backspace and not (ctrl or alt):
            self._block_backspace()
        elif key == Qt.Key_Delete and not (ctrl or alt):
            self._block_delete()
        elif key == Qt.Key_Tab and not modifiers:
            self._type_in_block(INDENT, join=self._block_typing)
            typed = True
        elif event.text() and event.text().isprintable() and ctrl == alt:      # AltGr is Ctrl + Alt
            self._type_in_block(event.text(), join=self._block_typing)
            typed = True
        else:
            self.end_block()
            return False
        self._block_typing = typed
        return True

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and (event.modifiers() & Qt.AltModifier or self.block_mode):
            line, column = self._place_at(event.pos())
            block = self.block
            self._block_typing = False
            if event.modifiers() & Qt.ShiftModifier and block is not None:
                self.set_block(Block(block.anchor_line, block.anchor_column, line, column))
            else:
                self.set_block(Block(line, column, line, column))
            self._block_drag = True
            self.setFocus()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._block_drag and event.buttons() & Qt.LeftButton and self.block is not None:
            line, column = self._place_at(event.pos())
            block = self.block
            if (line, column) != (block.line, block.column):
                self.set_block(Block(block.anchor_line, block.anchor_column, line, column))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._block_drag:
            self._block_drag = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.block is not None:
            self._paint_block()

    def _paint_block(self):
        """The block's columns over its lines - past their ends too - and its caret on each of them."""
        painter = QPainter(self.viewport())
        block, bottom = self.block, self.viewport().height()
        fill = QColor(self.palette().color(QPalette.Highlight))
        fill.setAlpha(90)
        caret = self.palette().color(QPalette.Text)
        for line in self._block_lines():
            box = self.blockBoundingGeometry(line).translated(self.contentOffset())
            if box.bottom() < 0 or box.top() > bottom:
                continue
            if block.right > block.left:
                left = self._x_of(line, block.left)
                painter.fillRect(QRectF(left, box.top(), self._x_of(line, block.right) - left, box.height()), fill)
            painter.fillRect(QRectF(self._x_of(line, block.column), box.top(), 1.5, box.height()), caret)
        painter.end()

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
