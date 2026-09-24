"""Panel pages as windows of their own: designed geometry kept, drawn at any zoom, fitted to the window."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path

from PyQt5.QtCore import QPoint, QPointF, Qt
from PyQt5.QtGui import QFont, QWheelEvent
from PyQt5.QtWidgets import QApplication, QLabel, QPushButton, QTabWidget

from canexpert.panel.database import parse_application_database
from canexpert.panel.page_window import PanelPage, PanelWindow, zoom_factor
from canexpert.panel.view import PanelView

APP = QApplication.instance() or QApplication([])

TWO_PAGES = '''<application_database name="Bench"><description>Two pages</description><pages>
<page name="Engine">
<button label="Start" binding_value="start" x="10" y="10" width="100" height="30"/>
<value label="Status" binding_value="status" x="10" y="60" width="200" height="30"/>
</page>
<page name="Body">
<checkbox label="Door" binding_value="door" x="20" y="20" width="120" height="30"/>
</page>
</pages></application_database>'''


def page_with_button():
    page = PanelPage()
    button = QPushButton("Start")
    font = QFont(button.font())
    font.setPointSizeF(10)
    button.setFont(font)
    page.place(button, 100, 50, 200, 40)
    return page, button


class PanelPageTest(unittest.TestCase):
    def test_controls_keep_their_designed_place_and_size(self):
        page, button = page_with_button()
        self.assertEqual(button.geometry().getRect(), (100, 50, 200, 40))
        self.assertEqual((page.designed_size.width(), page.designed_size.height()), (600, 400))
        page.place(QLabel("far"), 700, 500, 100, 20)
        self.assertEqual((page.designed_size.width(), page.designed_size.height()), (820, 540))

    def test_a_zoom_scales_positions_sizes_and_fonts_together(self):
        page, button = page_with_button()
        page.set_zoom(1.5)
        self.assertEqual(button.geometry().getRect(), (150, 75, 300, 60))
        self.assertAlmostEqual(button.font().pointSizeF(), 15.0)
        self.assertEqual((page.width(), page.height()), (900, 600))
        page.set_zoom(1.0)                                  # back exactly, from the designed values
        self.assertEqual(button.geometry().getRect(), (100, 50, 200, 40))
        self.assertAlmostEqual(button.font().pointSizeF(), 10.0)
        page.set_zoom(100)
        self.assertEqual(page.zoom, 4.0, "zoom is kept within reason")


class PanelWindowTest(unittest.TestCase):
    def setUp(self):
        self.page, self.button = page_with_button()
        self.window = PanelWindow(self.page, "Engine")
        self.addCleanup(self.window.close)
        self.window.resize(400, 300)
        self.window.show()
        APP.processEvents()

    def test_a_fixed_zoom(self):
        self.window.zoom_combo.setCurrentText("150 %")
        self.assertEqual(self.page.zoom, 1.5)
        self.assertEqual(zoom_factor("75 %"), 0.75)
        self.assertIsNone(zoom_factor("Fit"))

    def test_fit_follows_the_window(self):
        seen = []
        self.window.zoom_changed.connect(seen.append)
        self.window.zoom_combo.setCurrentText("Fit")
        self.assertEqual(seen, ["Fit"])
        viewport = self.window.scroll.viewport().size()
        self.assertLessEqual(self.page.width(), viewport.width())
        self.assertLessEqual(self.page.height(), viewport.height())
        small = self.page.zoom
        self.window.resize(1200, 900)
        APP.processEvents()
        self.assertGreater(self.page.zoom, small, "a bigger window, a bigger page")
        self.assertEqual(self.window.scroll.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)

    def test_ctrl_and_the_wheel_step_the_zoom(self):
        self.assertEqual(self.page.zoom, 1.0)
        self.window.step_zoom(1)
        self.assertEqual(self.window.zoom_combo.currentText(), "125 %")
        self.window.step_zoom(-1)
        self.window.step_zoom(-1)
        self.assertEqual(self.window.zoom_combo.currentText(), "75 %")
        viewport = self.window.scroll.viewport()
        event = QWheelEvent(QPointF(10, 10), QPointF(viewport.mapToGlobal(QPoint(10, 10))), QPoint(0, 0),
                            QPoint(0, 120), Qt.NoButton, Qt.ControlModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(viewport, event)
        self.assertEqual(self.window.zoom_combo.currentText(), "100 %")


class PanelViewTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "bench_2026-09-20.xml"
        path.write_text(TWO_PAGES)
        self.database = parse_application_database(path)

    def test_pages_in_tabs_for_the_designer_and_its_test_panel(self):
        view = PanelView(self.database, lambda *args: None, self.fail)
        self.addCleanup(view.close)
        tabs = view.findChild(QTabWidget)
        self.assertEqual([tabs.tabText(index) for index in range(tabs.count())], ["Engine", "Body"])
        self.assertIsInstance(tabs.widget(0), PanelWindow)

    def test_pages_handed_out_as_windows_keep_their_zoom(self):
        view = PanelView(self.database, lambda *args: None, self.fail, tabs=False, zooms={"Body": "150 %"})
        self.addCleanup(view.close)
        self.assertIsNone(view.findChild(QTabWidget))
        self.assertEqual([name for name, _ in view.page_windows], ["Engine", "Body"])
        self.assertEqual(view.page_windows[1][1].page.zoom, 1.5)
        view.set_value("door", True)                          # every page's controls, one panel
        self.assertTrue(view.widgets["door"].isChecked())


if __name__ == "__main__":
    unittest.main()
