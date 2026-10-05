"""The Form Designer's text editors: line numbers, indenting, comments, Go to line - and the find bar: Find,
Replace, Replace all, match case, whole words, regular expressions, its keys."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from unittest.mock import patch

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QTextCursor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog, QInputDialog, QVBoxLayout

from canexpert.designer.code_editor import CodeEditor
from canexpert.designer.text_editor import EditorPane, TextEditor

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


if __name__ == "__main__":
    unittest.main()
