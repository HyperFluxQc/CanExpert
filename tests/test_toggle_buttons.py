"""A button that switches an option on must look switched on, in both themes."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication, QToolButton

from canexpert import main_window as main
from canexpert.designer.canvas import FormCanvas

APP = QApplication.instance() or QApplication([])
# The checked look fills the button, so nearly every pixel changes; well under that is still a clear
# difference, while the faint outline Qt draws by itself (or nothing at all) is not.
VISIBLE = 20.0


def difference(button) -> float:
    """Percentage of pixels that change between the button switched off and switched on."""
    was = button.isChecked()
    try:
        button.setChecked(False)
        off = button.grab().toImage()
        button.setChecked(True)
        on = button.grab().toImage()
    finally:
        button.setChecked(was)
    changed = sum(1 for y in range(off.height()) for x in range(off.width())
                  if off.pixel(x, y) != on.pixel(x, y))
    return 100.0 * changed / max(1, off.width() * off.height())


class ToggleButtonTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "Configurations").mkdir()
        (root / "Configurations" / "config_Bus.json").write_text(json.dumps({"name": "Bus"}))
        self.settings = QSettings(str(root / "settings.ini"), QSettings.IniFormat)
        self.patches = [
            patch.object(main, "CONFIG_DIR", root / "Configurations"),
            patch.object(main, "DATABASES_DIR", root),
            patch.object(main, "app_settings", lambda: self.settings),
            patch.object(main.can, "detect_available_configs", return_value=[]),
        ]
        for item in self.patches:
            item.start()
        self.window = main.MainWindow()
        self.window.resize(1250, 700)
        self.window.show()
        APP.processEvents()

    def tearDown(self):
        self.window.close()
        APP.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def toggles(self):
        """Every button in the application that switches an option on: (where, name, button)."""
        passive = next(button for button in self.window.findChildren(QToolButton)
                       if button.accessibleName() == "Passive")
        found = [("toolbar", "passive", passive)]
        for where, window in (("logger", self.window.open_can_logger()), ("trace", self.window.open_trace())):
            found += [(where, name, button) for name, button in window._tool_buttons.items()
                      if button.isCheckable()]
        canvas = FormCanvas()
        self.addCleanup(canvas.deleteLater)
        canvas.show()
        APP.processEvents()
        found += [("designer", name, button) for name, button in canvas._tool_buttons.items()
                  if button.isCheckable()]
        return found

    def test_a_switched_on_button_stays_pressed_in_light_and_dark(self):
        for theme in ("light", "dark"):
            self.window.apply_theme(theme)
            APP.processEvents()
            checked = self.toggles()
            self.assertGreaterEqual(len(checked), 10, "the toggles were not found")
            for where, name, button in checked:
                self.assertGreater(difference(button), VISIBLE,
                                   f"{where} '{name}' looks the same on and off in the {theme} theme")

    def test_the_toolbar_says_what_a_checked_button_looks_like(self):
        # The Passive button used to show nothing at all: a style sheet that names :hover and :pressed
        # replaces Qt's own checked drawing, so the checked state has to be named too.
        sheet = self.window.findChild(main.QToolBar).styleSheet()
        self.assertIn("QToolButton:checked", sheet)
        self.assertIn("QToolButton:checked:hover", sheet)

    def test_a_button_that_is_not_a_toggle_is_left_alone(self):
        logger = self.window.open_can_logger()
        self.assertFalse(logger.clear_btn.isCheckable())
        self.assertFalse(self.window._toolbar_actions["connect"].isCheckable())


if __name__ == "__main__":
    unittest.main()
