"""The user manual, its pictures and the window that shows it."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import importlib.util
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtCore import QUrl
from PyQt5.QtGui import QTextDocument
from PyQt5.QtWidgets import QApplication

from canexpert import help_window
from canexpert.help_window import MANUAL, HelpWindow, manual_sections

APP = QApplication.instance() or QApplication([])


class UserManualTest(unittest.TestCase):
    def setUp(self):
        self.text = MANUAL.read_text(encoding="utf-8")

    def test_the_manual_covers_the_windows_it_promises(self):
        sections = manual_sections(self.text)
        for section in ("How to...", "Starting up", "Configurations", "Connecting", "Form Designer", "CAN Logger",
                        "Firmware flashing", "Symbol databases", "Trace window",
                        "Transmit window", "UDS Console", "Recording and replaying",
                        "Arranging the windows"):
            self.assertIn(section, sections)

    def test_it_explains_what_the_user_actually_clicks(self):
        for phrase in ("Connect", "Disconnect", "Load DBC", "Graph options", "Load ODX / CDD", "Test panel",
                       "Flashing", "Scan for ECUs", "Add from database", "Fault memory",
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

    def test_the_manual_shows_its_pictures(self):
        pictures = re.findall(r"!\[[^\]]*\]\((images/[a-z_]+\.png)\)", self.text)
        self.assertGreaterEqual(len(pictures), 10)
        for name in pictures:
            self.assertTrue((MANUAL.parent / name).is_file(), name)
            self.assertIn("[![", self.text.split(f"]({name})")[0][-300:], "a link to itself: full size on GitHub")
        window = HelpWindow()
        self.addCleanup(window.close)
        window.resize(900, 700)
        window.show()
        APP.processEvents()
        window.browser.fit_images()
        images = window.browser.images()
        self.assertEqual(len(images), len(pictures), "every picture, found beside the manual")
        room = window.browser.viewport().width()
        for _position, _length, image in images:
            self.assertLessEqual(image.width(), room, "no wider than the window...")
            self.assertTrue(image.name().endswith("@fitted"), "...smoothly scaled")
            fitted = window.browser.document().resource(QTextDocument.ImageResource, QUrl(image.name()))
            self.assertFalse(fitted.isNull(), image.name())
        window.resize(1600, 900)                                             # wider: made again, never larger
        APP.processEvents()
        window.browser.fit_images()
        self.assertTrue(all(image.width() <= 1280 for _p, _l, image in window.browser.images()))
        opened = []
        with patch.object(help_window.QDesktopServices, "openUrl", opened.append):
            window.browser.picture_clicked.emit("images/trace.png")          # a click on a picture
        self.assertEqual(Path(opened[0].toLocalFile()), MANUAL.parent / "images" / "trace.png", "full size")

    def test_the_pictures_are_the_ones_the_tool_makes(self):
        spec = importlib.util.spec_from_file_location(
            "make_screenshots", Path(__file__).resolve().parent.parent / "tools" / "make_screenshots.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        shown = set(re.findall(r"!\[[^\]]*\]\(images/([a-z_]+)\.png\)", self.text))
        self.assertEqual(shown, set(tool.PICTURES), "python tools/make_screenshots.py makes them all again")

    def test_a_missing_manual_says_so_instead_of_failing(self):
        window = HelpWindow(path=Path("no", "such", "manual.md"))
        self.addCleanup(window.close)
        self.assertIn("could not be read", window.browser.toPlainText())


if __name__ == "__main__":
    unittest.main()
