"""Form Designer: palette, signal drops, layout tools, undo/redo, clipboard, keys, resize, handlers, test mode."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtCore import QEvent, QRectF, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QApplication, QMessageBox, QPushButton

from canexpert.designer.form_designer import FormDesigner
from canexpert.designer.side_panels import DraggablePaletteItem, default_handler_name
from canexpert.panel.database import parse_application_database
from canexpert.panel.controls import CONTROLS

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parent.parent / "DBC" / "dummy_ecu.dbc"


def spin_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


class FormDesignerTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.designer = FormDesigner()
        self.designer.database_dir = self.folder
        self.canvas = self.designer.canvas
        self.widgets = self.canvas._current_widgets

    def tearDown(self):
        self.designer.close()

    def key(self, key, modifiers=Qt.NoModifier):
        return self.canvas.key_pressed(QKeyEvent(QEvent.KeyPress, key, modifiers))

    def test_palette_lists_every_control(self):
        kinds = {item.widget_type for item in self.designer.palette.findChildren(DraggablePaletteItem)}
        self.assertEqual(kinds, {k for k, c in CONTROLS.items() if c.in_palette})
        for kind in kinds:
            data = self.canvas.add_widget_at(kind, 20, 20)
            self.assertEqual((data["width"], data["height"]), CONTROLS[kind].size)

    def test_dropped_signals_become_bound_controls(self):
        self.designer.symbol_list.load_dbc_path(str(DBC))
        for name, as_input, kind in (("EngineData.Temperature", False, "value"), ("EcuStatus.Session", False, "indicator"),
                                     ("EngineData.Temperature", True, "spin"), ("EcuStatus.Session", True, "combo")):
            data = self.canvas.add_signal_control(name, 40, 40, self.designer.symbol_list.signal_info(name), as_input)
            self.assertEqual((data["type"], data["binding_type"], data["binding_value"]), (kind, "dbc", name))
        spin = self.widgets()[2]
        self.assertEqual((spin["unit"], spin["min"], spin["max"]), ("degC", 0, 6553.5))
        self.assertEqual(self.widgets()[3]["items"], "default, programming, extended")

    def test_align_size_distribute_and_z_order(self):
        for x, y, w in ((10, 10, 100), (200, 60, 60), (70, 120, 80)):
            data = self.canvas.add_widget_at("button", x, y)
            data["width"] = w
        self.canvas.set_selection([1, 2, 0])                                      # primary: widget 0
        self.canvas.align("left")
        self.assertEqual([w["x"] for w in self.widgets()], [10, 10, 10])
        self.canvas.align("same_width")
        self.assertEqual([w["width"] for w in self.widgets()], [100, 100, 100])
        self.canvas.align("distribute_v")
        self.assertEqual([w["y"] for w in self.widgets()], [10, 65, 120])        # 32 px high: 23 px gaps
        self.canvas.align("right")
        self.assertEqual({w["x"] + w["width"] for w in self.widgets()}, {110})
        first = self.widgets()[0]
        self.canvas.set_selection([0])
        self.canvas.bring_to_front()
        self.assertIs(self.widgets()[-1], first)
        self.assertEqual(self.canvas.selection, [2])
        self.canvas.send_to_back()
        self.assertIs(self.widgets()[0], first)

    def test_undo_redo_and_property_edits_merge(self):
        self.canvas.add_widget_at("button", 10, 10)
        self.canvas.add_widget_at("led", 50, 50)
        self.canvas.set_selection([0, 1])
        self.canvas.delete_selection()
        self.assertEqual(self.widgets(), [])
        self.canvas.undo()
        self.assertEqual([w["type"] for w in self.widgets()], ["button", "led"])
        self.canvas.redo()
        self.assertEqual(self.widgets(), [])
        self.canvas.undo()
        self.canvas.set_selection([0])
        editor = self.designer.properties
        before = len(self.canvas._undo)
        for text in ("S", "St", "Sta", "Start"):
            editor.controls["label"][1].setText(text)
        self.assertEqual(self.widgets()[0]["label"], "Start")
        self.assertEqual(len(self.canvas._undo), before + 1)                     # typing merges into one step
        self.canvas.undo()
        self.assertEqual(self.widgets()[0]["label"], "Button 1")

    def test_clipboard_keys_nudge_and_rubber_band(self):
        self.canvas.add_widget_at("switch", 10, 10, binding_value="run", variable="run", handler="on_run_changed")
        self.assertTrue(self.key(Qt.Key_C, Qt.ControlModifier))
        self.assertTrue(self.key(Qt.Key_V, Qt.ControlModifier))
        pasted = self.widgets()[1]
        self.assertEqual((pasted["x"], pasted["y"], pasted["binding_value"]), (30, 30, "run_2"))
        self.assertNotIn("handler", pasted)
        self.key(Qt.Key_Right)
        self.key(Qt.Key_Down, Qt.ShiftModifier)
        self.assertEqual((pasted["x"], pasted["y"]), (31, 40))
        self.key(Qt.Key_A, Qt.ControlModifier)
        self.assertEqual(self.canvas.selection, [0, 1])
        self.key(Qt.Key_Delete)
        self.assertEqual(self.widgets(), [])
        self.key(Qt.Key_Z, Qt.ControlModifier)
        self.canvas.set_selection([])
        self.canvas.select_in_rect(QRectF(0, 0, 25, 25))
        self.assertEqual(self.canvas.selection, [0])

    def test_resize_handle_snaps_to_grid(self):
        data = self.canvas.add_widget_at("gauge", 20, 20)
        self.canvas.resize_started()
        self.canvas.resize_dragged(33, -7)
        self.canvas.resize_finished()
        self.assertEqual((data["width"], data["height"]), (190, 150))
        spin_until(lambda: False, 0.05)
        self.canvas.undo()
        self.assertEqual((self.widgets()[0]["width"], self.widgets()[0]["height"]), (160, 160))

    def test_handler_stub_is_created_once(self):
        data = self.canvas.add_widget_at("button", 10, 10, binding_value="start", variable="start")
        self.assertEqual(default_handler_name(data), "on_start_clicked")
        self.designer.edit_handler(data)
        self.designer.edit_handler(data)
        code = self.designer.code_editor.toPlainText()
        self.assertEqual(code.count("def on_start_clicked(api, value):"), 1)
        self.assertEqual(data["handler"], "on_start_clicked")
        self.assertEqual(self.designer.design_tabs.currentIndex(), 1)
        self.assertTrue(self.designer.code_editor.check_syntax()[0])
        with patch.object(QMessageBox, "information") as info:
            self.designer.edit_handler(self.canvas.add_widget_at("gauge", 100, 100))
            info.assert_called_once()                                               # displays have no handler

    def test_save_load_keeps_order_and_new_properties(self):
        self.designer.db_id_edit.setText("layout_2026-09-18")
        self.canvas.add_widget_at("group_box", 0, 0, label="Frame")
        self.canvas.add_widget_at("indicator", 20, 30, states="0=Off:#111111; 1=On:#22aa22")
        self.canvas.add_widget_at("led", 20, 80, on_color="#ff0000", blink=True, bold=True)
        with patch.object(QMessageBox, "information"), patch.object(QMessageBox, "critical") as error:
            self.designer.save()
            error.assert_not_called()
        page = parse_application_database(self.folder / "layout_2026-09-18.xml")["pages"][0]
        self.assertEqual([w["kind"] for w in page["widgets"]], ["group_box", "indicator", "led"])
        self.assertEqual(page["widgets"][1]["states"], "0=Off:#111111; 1=On:#22aa22")
        self.assertEqual((page["widgets"][2]["on_color"], page["widgets"][2]["blink"]), ("#ff0000", "True"))
        other = FormDesigner()
        with patch("canexpert.designer.form_designer.QFileDialog.getOpenFileName",
                   return_value=(str(self.folder / "layout_2026-09-18.xml"), "")):
            other.load()
        self.assertEqual([w["type"] for w in other.canvas._current_widgets()], ["group_box", "indicator", "led"])
        other.close()

    def test_disabled_layout_icons_are_visible_and_properties_follow_the_tab(self):
        from PyQt5.QtGui import QIcon
        button = self.canvas._tool_buttons["align_left"]
        self.assertFalse(button.isEnabled())                                   # nothing selected yet
        image = button.icon().pixmap(24, 24, QIcon.Disabled).toImage()
        painted = sum(image.pixelColor(x, y).alpha() > 0 for x in range(24) for y in range(24))
        self.assertGreater(painted, 20)                                          # dimmed, not blank
        self.designer.show()
        form_sizes = self.designer.design_splitter.sizes()
        self.designer.design_tabs.setCurrentIndex(1)                             # the script gets the whole width
        self.assertFalse(self.designer.properties_panel.isVisible())
        self.assertFalse(self.designer.symbols_panel.isVisible())
        self.designer.design_tabs.setCurrentIndex(0)
        self.assertTrue(self.designer.properties_panel.isVisible())
        self.assertTrue(self.designer.symbols_panel.isVisible())
        self.assertEqual(self.designer.design_splitter.sizes(), form_sizes)      # and the form its layout back

    def test_the_test_panel_button_runs_against_the_simulated_ecu(self):
        from canexpert.designer.form_designer import TestPanelDialog
        self.canvas.add_widget_at("label", 10, 10, text="Button test")
        button = next(item for item in self.designer.findChildren(QPushButton) if item.text() == "Test panel...")
        button.click()                                   # clicked's "checked" must not switch the ECU off
        dialogs = self.designer.findChildren(TestPanelDialog)
        self.assertEqual(len(dialogs), 1)
        try:
            self.assertTrue(hasattr(dialogs[0], "ecu"), "the Test panel has its simulated ECU")
        finally:
            dialogs[0].close()

    def test_test_panel_flashes_firmware_into_the_simulated_ecu(self):
        from canexpert.flashing import load_firmware
        example = Path(__file__).resolve().parent.parent / "examples"
        self.canvas.add_widget_at("label", 10, 10, text="Flash test")
        self.designer.code_editor.setPlainText((example / "example_2026-09-18_script.py").read_text(encoding="utf-8"))
        dialog = self.designer.test_panel()
        try:
            self.assertTrue(spin_until(dialog.flash_button.isEnabled))           # the script defines Flashing()
            results = []
            with patch("canexpert.designer.form_designer.report_result", lambda parent, ok, text: results.append((ok, text))):
                dialog.start_flashing(load_firmware(example / "firmware" / "demo_app.s19"))
                self.assertIsNotNone(dialog.flash_dialog)
                self.assertTrue(spin_until(lambda: results, 15))
            self.assertEqual(results, [(True, "Flashing complete")])
            self.assertIn("software version: APP-FLASHED-", dialog.log_view.toPlainText())
            self.assertIsNone(dialog.flash_dialog)
        finally:
            dialog.close()

    def test_test_mode_runs_panel_against_simulated_ecu(self):
        self.designer.symbol_list.load_dbc_path(str(DBC))
        self.designer.dbc_path_edit.setText(str(DBC))
        self.canvas.add_widget_at("switch", 10, 10, binding_value="run", variable="run", handler="on_run_changed")
        self.canvas.add_signal_control("EcuStatus.Running", 10, 60, self.designer.symbol_list.signal_info(
            "EcuStatus.Running"))
        self.designer.code_editor.setPlainText(
            "def on_run_changed(api, value):\n    api.can.send(0x200, [1 if value else 2])\n")
        dialog = self.designer.test_panel()
        self.assertIsNotNone(dialog)
        try:
            dialog.panel.widgets["run"].click()
            running = dialog.panel.widgets["Running"]
            self.assertTrue(spin_until(lambda: running.text() == "1"))
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
