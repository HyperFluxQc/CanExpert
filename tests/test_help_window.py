"""The user manual and the window that shows it."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from pathlib import Path

from PyQt5.QtWidgets import QApplication

from canexpert.help_window import MANUAL, HelpWindow, manual_sections

APP = QApplication.instance() or QApplication([])


class UserManualTest(unittest.TestCase):
    def setUp(self):
        self.text = MANUAL.read_text(encoding="utf-8")

    def test_the_manual_covers_the_windows_it_promises(self):
        sections = manual_sections(self.text)
        for section in ("Starting up", "Configurations", "Connecting", "Form Designer", "CAN Logger",
                        "Diagnostic Window", "Firmware flashing", "Symbol databases", "Trace window",
                        "Transmit window", "UDS Console", "Recording and replaying",
                        "Arranging the windows"):
            self.assertIn(section, sections)

    def test_it_explains_what_the_user_actually_clicks(self):
        for phrase in ("Connect", "Disconnect", "Load DBC", "Graph options", "Send UDS request", "Test panel",
                       "Flashing", "Scan Activity", "Add from database", "Fault memory",
                       "Record to file", "Replay a recorded file", "Save desktop as"):
            self.assertIn(phrase, self.text, f"the manual never mentions {phrase}")

    def test_the_window_lists_the_sections_and_finds_text(self):
        window = HelpWindow()
        self.addCleanup(window.close)
        self.assertEqual([window.contents.item(i).text() for i in range(window.contents.count())],
                         manual_sections(self.text))
        self.assertIn("CAN Expert connects to a CAN bus", window.browser.toPlainText())

        window.contents.setCurrentRow(window.contents.count() - 1)          # jump to the last section
        self.assertEqual(window.browser.textCursor().block().text(), manual_sections(self.text)[-1])
        window.go_to_section("CAN Logger")                                  # the heading, not a mention of it
        self.assertEqual(window.browser.textCursor().block().text(), "CAN Logger")

        window.search.setText("measurement cursors")
        window.find_next()
        self.assertEqual(window.status.text(), "")
        self.assertIn("cursors", window.browser.textCursor().selectedText().lower())
        window.search.setText("something that is not written anywhere")
        window.find_next()
        self.assertEqual(window.status.text(), "not found")

    def test_a_missing_manual_says_so_instead_of_failing(self):
        window = HelpWindow(path=Path("no", "such", "manual.md"))
        self.addCleanup(window.close)
        self.assertIn("could not be read", window.browser.toPlainText())


if __name__ == "__main__":
    unittest.main()
