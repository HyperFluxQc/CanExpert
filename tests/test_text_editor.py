"""The Form Designer's text editors: line numbers, indenting, comments, Go to line, block (column) editing - and
the find bar: Find, Replace, Replace all, match case, whole words, regular expressions, its keys."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from unittest.mock import patch

from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QMouseEvent, QTextCursor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog, QInputDialog, QVBoxLayout

from canexpert.designer.code_editor import CodeEditor
from canexpert.designer.text_editor import BLOCK_MIME, EditorPane, TextEditor

APP = QApplication.instance() or QApplication([])

SCRIPT = """def speed(api, value):
    api.log(value)
    speed_max = value

def Speed(api):
    api.log("speed")
"""


class FindBarTest(unittest.TestCase):
    def setUp(self):
        # In a dialog, as in the Form Designer, where Esc would close the window.
        self.dialog = QDialog()
        self.pane = EditorPane(CodeEditor())
        QVBoxLayout(self.dialog).addWidget(self.pane)
        self.editor, self.bar = self.pane.editor, self.pane.find_bar
        self.editor.setPlainText(SCRIPT)
        self.dialog.resize(900, 500)
        self.dialog.show()

    def tearDown(self):
        self.dialog.close()

    def selected(self):
        cursor = self.editor.textCursor()
        return cursor.selectedText(), cursor.blockNumber() + 1

    def search(self, text, case=False, words=False, regex=False):
        self.bar.case_box.setChecked(case)
        self.bar.word_box.setChecked(words)
        self.bar.regex_box.setChecked(regex)
        self.bar.find_edit.setText(text)

    def test_find_as_you_type_then_next_and_before_wrapping_around(self):
        self.assertFalse(self.bar.is_open)
        self.editor.moveCursor(QTextCursor.Start)
        self.bar.open()
        self.assertTrue(self.bar.is_open and self.bar.isVisible())
        self.assertIs(self.dialog.focusWidget(), self.bar.find_edit)
        self.assertFalse(self.bar.replacing, "Find alone: no Replace line")
        self.search("speed")
        self.assertEqual(self.selected(), ("speed", 1), "the first one is selected as it is typed")
        self.assertEqual(self.bar.status.text(), "1 of 4")
        self.assertEqual(len(self.editor.extraSelections()), 1 + 4, "the current line, and every match highlighted")
        QTest.keyClick(self.bar.find_edit, Qt.Key_Return)              # Enter: the next one
        self.assertEqual(self.selected(), ("speed", 3))
        self.assertIs(self.dialog.focusWidget(), self.bar.find_edit, "the search goes on from the box")
        self.bar.find()
        self.assertEqual(self.selected(), ("Speed", 5))
        self.bar.find()
        self.assertEqual((self.selected(), self.bar.status.text()), (("speed", 6), "4 of 4"))
        self.assertTrue(self.bar.find())                                # past the last one: the first again
        self.assertEqual((self.selected(), self.bar.status.text()), (("speed", 1), "1 of 4, from the top"))
        QTest.keyClick(self.bar.find_edit, Qt.Key_Return, Qt.ShiftModifier)   # Shift+Enter: the one before
        self.assertEqual((self.selected(), self.bar.status.text()), (("speed", 6), "4 of 4, from the bottom"))

    def test_match_case_whole_words_and_regular_expressions(self):
        self.bar.open()
        self.search("speed", case=True)
        self.assertEqual(self.bar.status.text(), "1 of 3", "not Speed")
        self.search("speed", words=True)
        self.assertEqual(len(self.bar._found), 3, "not speed_max")
        self.search(r"api\.\w+\(", regex=True)
        self.assertEqual(self.selected()[0], "api.log(")
        self.assertEqual(len(self.bar._found), 2)
        self.search("api.", regex=True)
        self.assertEqual(len(self.bar._found), 4, "a regular expression's dot: any character")
        self.search("api.")
        self.assertEqual(len(self.bar._found), 2, "as written, the dot a dot")
        self.search("(unclosed", regex=True)
        self.assertTrue(self.bar.status.text().startswith("Not a regular expression"))
        self.search("nowhere")
        self.assertEqual(self.bar.status.text(), "No match")
        self.assertFalse(self.bar.find())
        self.assertEqual(self.editor.extraSelections()[1:], [], "nothing highlighted")

    def test_the_selected_text_is_what_is_found(self):
        cursor = self.editor.textCursor()
        cursor.setPosition(SCRIPT.index("speed_max"))
        cursor.setPosition(SCRIPT.index("speed_max") + len("speed_max"), QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.bar.open()
        self.assertEqual(self.bar.find_edit.text(), "speed_max")
        self.assertEqual(self.bar.find_edit.selectedText(), "speed_max", "typed over at once")
        self.assertEqual(self.selected(), ("speed_max", 3), "the selection stays the match")

    def test_esc_closes_the_bar_and_not_the_window(self):
        self.bar.open()
        self.search("api")
        QTest.keyClick(self.bar.find_edit, Qt.Key_Escape)
        self.assertFalse(self.bar.is_open)
        self.assertTrue(self.dialog.isVisible(), "the window stays open")
        self.assertEqual(self.editor.extraSelections()[1:], [], "the highlights go with it")
        self.bar.open()
        self.editor.setFocus()
        QTest.keyClick(self.editor, Qt.Key_Escape)                      # from the editor too
        self.assertFalse(self.bar.is_open)
        self.assertTrue(self.dialog.isVisible())

    def test_replace_one_by_one_and_all_at_once(self):
        self.editor.moveCursor(QTextCursor.Start)
        self.bar.open(replace=True)
        self.assertTrue(self.bar.replacing)
        self.search("value", words=True)
        self.bar.replace_edit.setText("level")
        QTest.keyClick(self.bar.replace_edit, Qt.Key_Return)            # Enter in Replace: this one, then the next
        self.assertEqual(self.editor.toPlainText().splitlines()[0], "def speed(api, level):")
        self.assertEqual(self.selected(), ("value", 2), "the next one selected")
        self.assertEqual(self.bar.replace_all(), 2)
        self.assertNotIn("value", self.editor.toPlainText())
        self.assertEqual(self.bar.status.text(), "2 replaced")
        self.editor.undo()                                              # Replace all: one undo step
        self.assertEqual(self.editor.toPlainText().count("value"), 2)
        self.search(r"api\.log\((\w+)\)", regex=True)
        self.bar.replace_edit.setText(r"print(\1)")                     # a regular expression's group
        self.assertEqual(self.bar.replace_all(), 1)
        self.assertIn("    print(value)\n", self.editor.toPlainText())
        self.bar.replace_edit.setText(r"\2")
        self.search(r"api\.(log)", regex=True)
        self.assertEqual(self.bar.replace_all(), 0)
        self.assertTrue(self.bar.status.text().startswith("The replacement:"), "no second group: said, nothing done")

    def test_after_an_emoji_the_matches_are_where_they_are(self):
        self.editor.setPlainText('api.log("\U0001F680 ready")  # \U0001F680 launch ready\nready = True')
        self.editor.moveCursor(QTextCursor.Start)
        self.bar.open()
        self.search("ready")
        for line in (1, 1, 2):
            self.assertEqual(self.selected(), ("ready", line))
            self.bar.find()
        self.bar.replace_edit.setText("set")
        self.bar.replace_all()
        self.assertEqual(self.editor.toPlainText(), 'api.log("\U0001F680 set")  # \U0001F680 launch set\nset = True')


class TextEditorTest(unittest.TestCase):
    def setUp(self):
        self.editor = TextEditor()
        self.editor.resize(500, 300)
        self.editor.show()

    def tearDown(self):
        self.editor.close()

    def select_lines(self, first, last):
        document = self.editor.document()
        cursor = self.editor.textCursor()
        cursor.setPosition(document.findBlockByNumber(first - 1).position())
        cursor.setPosition(document.findBlockByNumber(last - 1).position(), QTextCursor.KeepAnchor)
        cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        self.editor.setTextCursor(cursor)

    def test_line_numbers(self):
        self.editor.setPlainText("\n".join(f"line {n}" for n in range(1, 1201)))
        self.assertGreater(self.editor.viewportMargins().left(), 0, "room for the numbers")
        self.assertEqual(self.editor.viewportMargins().left(), self.editor.line_number_width())
        self.assertFalse(self.editor.grab().isNull())
        width = self.editor.line_number_width()
        self.editor.setPlainText("one line")
        self.assertLess(self.editor.line_number_width(), width, "as wide as the numbers need")

    def test_tab_and_shift_tab_indent_and_unindent_lines(self):
        self.editor.setPlainText("a = 1\n\nb = 2\nc = 3")
        self.select_lines(1, 3)
        QTest.keyClick(self.editor, Qt.Key_Tab)
        self.assertEqual(self.editor.toPlainText(), "    a = 1\n\n    b = 2\nc = 3", "blank lines left as they are")
        self.assertEqual(self.editor.textCursor().selectedText(), "    a = 1      b = 2", "still selected")
        QTest.keyClick(self.editor, Qt.Key_Backtab)
        self.assertEqual(self.editor.toPlainText(), "a = 1\n\nb = 2\nc = 3")
        self.editor.moveCursor(QTextCursor.End)
        QTest.keyClick(self.editor, Qt.Key_Tab)                         # no selection: spaces at the cursor
        self.assertEqual(self.editor.toPlainText().splitlines()[-1], "c = 3    ")

    def test_comments_go_on_and_off(self):
        self.editor.setPlainText("def f():\n    x = 1\n\n    return x")
        self.select_lines(2, 4)
        self.editor.toggle_comment()
        self.assertEqual(self.editor.toPlainText(), "def f():\n    # x = 1\n\n    # return x")
        self.editor.toggle_comment()
        self.assertEqual(self.editor.toPlainText(), "def f():\n    x = 1\n\n    return x")
        self.editor.comment = "//"                                       # the Variables tab's
        self.editor.moveCursor(QTextCursor.Start)
        self.editor.toggle_comment()
        self.assertEqual(self.editor.toPlainText().splitlines()[0], "// def f():")

    def test_go_to_line(self):
        self.editor.setPlainText("\n".join(f"line {n}" for n in range(1, 101)))
        with patch.object(QInputDialog, "getInt", return_value=(42, True)) as ask:
            self.editor.ask_line()
        self.assertEqual(self.editor.textCursor().blockNumber() + 1, 42)
        self.assertEqual(ask.call_args[0][2], "Line (1 to 100):")


ALT_SHIFT = Qt.ShiftModifier | Qt.AltModifier
FIELDS = "uint8 a;\nuint16 bb;\nuint32 ccc;\nx"


class BlockEditingTest(unittest.TestCase):
    """Block (column) editing: a block of columns over several lines, and what is done in it done on each one."""

    def setUp(self):
        self.editor = TextEditor()
        self.editor.resize(600, 300)
        self.editor.show()
        self.editor.setPlainText(FIELDS)

    def tearDown(self):
        self.editor.close()

    def place(self, line, index):
        cursor = self.editor.textCursor()
        cursor.setPosition(self.editor.document().findBlockByNumber(line).position() + index)
        self.editor.setTextCursor(cursor)

    def keys(self, key, count, modifiers=ALT_SHIFT):
        for _ in range(count):
            QTest.keyClick(self.editor, key, modifiers)

    def lines(self):
        return self.editor.toPlainText().split("\n")

    def uint_block(self):
        """The block over "uint" on the first three lines."""
        self.place(0, 0)
        self.keys(Qt.Key_Right, 4)
        self.keys(Qt.Key_Down, 2)
        self.assertEqual(self.editor.block_text(), "uint\nuint\nuint")

    def test_shift_alt_arrows_select_a_block_and_what_is_typed_goes_on_every_line(self):
        self.place(0, 0)
        self.keys(Qt.Key_Down, 2)
        block = self.editor.block
        self.assertEqual((block.top, block.bottom, block.left, block.right), (0, 2, 0, 0), "a caret on three lines")
        QTest.keyClicks(self.editor, "// ")
        self.assertEqual(self.lines(), ["// uint8 a;", "// uint16 bb;", "// uint32 ccc;", "x"])
        self.editor.undo()
        self.assertEqual(self.editor.toPlainText(), FIELDS, "what was typed in a row: one undo step")
        self.assertIsNone(self.editor.block, "Undo ends the block")

    def test_a_block_of_columns_is_replaced_on_every_line(self):
        self.uint_block()
        QTest.keyClicks(self.editor, "s")
        self.assertEqual(self.lines()[:3], ["s8 a;", "s16 bb;", "s32 ccc;"])
        block = self.editor.block
        self.assertEqual((block.left, block.right), (1, 1), "then a caret after it on every line")

    def test_short_lines_backspace_and_delete(self):
        self.editor.setPlainText("ab\nabcdef\nabcd")
        self.place(0, 2)
        self.keys(Qt.Key_Right, 2)                                  # past the end of the first line
        self.keys(Qt.Key_Down, 2)
        self.assertEqual(self.editor.block_text(), "\ncd\ncd", "past the end of a line, nothing of it")
        QTest.keyClick(self.editor, Qt.Key_Delete)                  # the columns go
        self.assertEqual(self.lines(), ["ab", "abef", "ab"])
        QTest.keyClicks(self.editor, "X")
        self.assertEqual(self.lines(), ["abX", "abXef", "abX"])
        QTest.keyClick(self.editor, Qt.Key_Backspace)
        self.assertEqual(self.lines(), ["ab", "abef", "ab"])
        QTest.keyClick(self.editor, Qt.Key_Delete)                  # a caret: the character after it
        self.assertEqual(self.lines(), ["ab", "abf", "ab"])
        self.editor.setPlainText("a\nabcdef")
        self.place(1, 4)
        self.keys(Qt.Key_Up, 1)
        QTest.keyClicks(self.editor, "|")
        self.assertEqual(self.lines(), ["a   |", "abcd|ef"], "a short line filled with spaces up to the block")

    def test_copy_cut_and_paste_a_block(self):
        self.uint_block()
        QTest.keyClick(self.editor, Qt.Key_C, Qt.ControlModifier)
        data = QApplication.clipboard().mimeData()
        self.assertEqual(data.text(), "uint\nuint\nuint")
        self.assertTrue(data.hasFormat(BLOCK_MIME))
        QTest.keyClick(self.editor, Qt.Key_X, Qt.ControlModifier)
        self.assertEqual(self.lines(), ["8 a;", "16 bb;", "32 ccc;", "x"])
        QTest.keyClick(self.editor, Qt.Key_Escape)
        self.assertIsNone(self.editor.block)
        self.place(0, 0)
        QTest.keyClick(self.editor, Qt.Key_V, Qt.ControlModifier)   # a block copied pastes as a block
        self.assertEqual(self.editor.toPlainText(), FIELDS)
        self.editor.undo()
        self.assertEqual(self.lines(), ["8 a;", "16 bb;", "32 ccc;", "x"], "one undo step")
        self.place(3, 1)
        self.editor.paste()                                         # the Edit menu's Paste: the same
        self.assertEqual(self.lines(), ["8 a;", "16 bb;", "32 ccc;", "xuint", " uint", " uint"],
                         "lines added where there are too few")

    def test_text_pasted_in_a_block(self):
        self.place(0, 0)
        self.keys(Qt.Key_Down, 2)
        QApplication.clipboard().setText("const ")
        QTest.keyClick(self.editor, Qt.Key_V, Qt.ControlModifier)   # one line: on every line
        self.assertEqual(self.lines()[:3], ["const uint8 a;", "const uint16 bb;", "const uint32 ccc;"])
        QApplication.clipboard().setText("1\n2\n3")
        QTest.keyClick(self.editor, Qt.Key_V, Qt.ControlModifier)   # as many lines as it has: one on each
        self.assertEqual(self.lines()[:3], ["const 1uint8 a;", "const 2uint16 bb;", "const 3uint32 ccc;"])

    def test_alt_drag_selects_a_block(self):
        editor = self.editor
        viewport = editor.viewport()

        def point(line, column):
            rect = editor.cursorRect(QTextCursor(editor.document().findBlockByNumber(line)))
            return QPoint(int(rect.left() + column * editor._char_width()), rect.center().y())

        def mouse(kind, where, modifiers=Qt.AltModifier, buttons=Qt.LeftButton):
            QApplication.sendEvent(viewport, QMouseEvent(kind, where, Qt.LeftButton, buttons, modifiers))

        mouse(QEvent.MouseButtonPress, point(0, 0))
        mouse(QEvent.MouseMove, point(1, 2), buttons=Qt.LeftButton)
        mouse(QEvent.MouseMove, point(2, 4), buttons=Qt.LeftButton)
        mouse(QEvent.MouseButtonRelease, point(2, 4), buttons=Qt.NoButton)
        self.assertEqual(editor.block_text(), "uint\nuint\nuint")
        self.assertFalse(editor.grab().isNull(), "painted")
        mouse(QEvent.MouseButtonPress, point(1, 1), Qt.NoModifier)  # a click without Alt ends it
        mouse(QEvent.MouseButtonRelease, point(1, 1), Qt.NoModifier, Qt.NoButton)
        self.assertIsNone(editor.block)
        self.assertEqual(editor.textCursor().blockNumber(), 1)
        editor.block_mode = True                                    # block selection mode: a plain drag
        mouse(QEvent.MouseButtonPress, point(0, 0), Qt.NoModifier)
        mouse(QEvent.MouseMove, point(1, 4), Qt.NoModifier, Qt.LeftButton)
        mouse(QEvent.MouseButtonRelease, point(1, 4), Qt.NoModifier, Qt.NoButton)
        self.assertEqual(editor.block_text(), "uint\nuint")
        self.keys(Qt.Key_Down, 1, Qt.ShiftModifier)                 # and Shift + arrows
        self.assertEqual(editor.block_text(), "uint\nuint\nuint")

    def test_what_ends_a_block(self):
        self.uint_block()
        QTest.keyClick(self.editor, Qt.Key_Right)                   # a key that is not block editing's...
        self.assertIsNone(self.editor.block)
        self.assertEqual(self.editor.textCursor().positionInBlock(), 5, "...does what it does")
        self.uint_block()
        QTest.keyClick(self.editor, Qt.Key_A, Qt.ControlModifier)   # Ctrl+A: everything selected
        self.assertIsNone(self.editor.block)
        self.assertEqual(self.editor.textCursor().selectedText().replace("\u2029", "\n"), FIELDS)
        self.uint_block()
        QTest.keyClick(self.editor, Qt.Key_Shift)                   # a modifier alone: it stays
        self.assertIsNotNone(self.editor.block)
        self.editor.go_to_line(4)                                   # anything else moving the cursor
        self.assertIsNone(self.editor.block)

    def test_tabs_and_wide_characters_keep_their_columns(self):
        self.editor.setPlainText("\tab\n    cd\n\U0001F680 ef")
        self.place(1, 4)
        self.keys(Qt.Key_Up, 1)
        QTest.keyClicks(self.editor, "|")
        self.assertEqual(self.lines()[:2], ["\t|ab", "    |cd"], "a tab reaches the next tab stop")
        self.place(2, 3)                                            # after the rocket and the space (Qt: 3)
        self.keys(Qt.Key_Right, 1)
        self.assertEqual(self.editor.block_text(), "e")
        QTest.keyClicks(self.editor, "E")
        self.assertEqual(self.lines()[2], "\U0001F680 Ef")

    def test_the_script_editor_completes_nothing_in_a_block(self):
        editor = CodeEditor()
        self.addCleanup(editor.close)
        editor.show()
        editor.setPlainText("log(1)\nlog(2)")
        cursor = editor.textCursor()
        cursor.setPosition(0)
        editor.setTextCursor(cursor)
        QTest.keyClick(editor, Qt.Key_Down, ALT_SHIFT)
        QTest.keyClicks(editor, "api.")
        self.assertEqual(editor.toPlainText(), "api.log(1)\napi.log(2)")
        self.assertFalse(editor.completer.popup().isVisible())
        QTest.keyClick(editor, Qt.Key_Return)                       # Enter: the block ends, a new line as ever
        self.assertIsNone(editor.block)
        self.assertEqual(editor.blockCount(), 3)


if __name__ == "__main__":
    unittest.main()
