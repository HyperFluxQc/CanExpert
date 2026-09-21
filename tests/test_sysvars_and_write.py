"""System variables, the Write window, and the script events for keys, error frames and the bus state."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication, QDialog

from canexpert.can_bus import ReceiveMailbox
from canexpert.can_logger import CANLoggerWindow
from canexpert.config import validate_config
from canexpert.panel.runtime import ScriptRuntime
from canexpert.sysvars import (COL_NAME, COL_VALUE, DefinitionDialog, SystemVariables, SystemVariablesWindow,
                               SysVarDefinition, check_name, convert)
from canexpert.write_window import WriteWindow, watch_values

APP = QApplication.instance() or QApplication([])

SCRIPT = '''
seen = {"keys": []}
counter = 3

@on_start
def begin(api):
    api.sysvar.set("Engine::Target", 10.0)
    api.warn("careful")
    api.write("hello")

@on_sysvar("Engine::Target")
def target(api, value):
    api.ui.set_value("target", value)

@on_sysvar()
def anything(api, value):
    api.ui.set_value("any", value)

@on_key("a", "F5")
def key(api, key):
    seen["keys"].append(key)
    api.ui.set_value("key", key)

@on_error_frame
def errors(api, timestamp):
    api.ui.set_value("error_frame", timestamp)

@on_bus_state
def state(api, state):
    api.ui.set_value("bus_state", state)
'''


def wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def temp_settings(test):
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    return QSettings(str(Path(directory.name) / "settings.ini"), QSettings.IniFormat), Path(directory.name)


class VariablesTest(unittest.TestCase):
    def setUp(self):
        self.settings, self.folder = temp_settings(self)
        self.variables = SystemVariables(self.settings)

    def test_a_variable_is_defined_by_setting_it(self):
        self.assertTrue(self.variables.set("Engine::Target", 1200))
        self.assertEqual(self.variables.get("Engine::Target"), 1200)
        self.assertEqual(self.variables.definition("Engine::Target").kind, "int")
        self.assertFalse(self.variables.set("Engine::Target", 1200), "the same value is not a change")
        self.assertEqual(self.variables.get("Engine::Missing", "none"), "none")

    def test_values_take_the_type_of_the_variable(self):
        self.variables.define(SysVarDefinition("Test::Enabled", "bool", False))
        self.variables.set("Test::Enabled", "on")
        self.assertIs(self.variables.get("Test::Enabled"), True)
        self.variables.set("Engine::Target", 1.5)
        self.variables.set("Engine::Target", "2.25")
        self.assertEqual(self.variables.get("Engine::Target"), 2.25)
        with self.assertRaises(ValueError):
            self.variables.set("Engine::Target", "fast")
        self.assertEqual(convert("int", "7.9"), 7)

    def test_names_are_namespaced(self):
        for bad in ("Target", "Engine::", "::Target", "Engine::Tar get"):
            with self.assertRaises(ValueError):
                check_name(bad)
        check_name("Body::Door_Front::Left")

    def test_a_change_is_announced_once(self):
        seen = []
        self.variables.changed.connect(lambda name, value, when: seen.append((name, value)))
        self.variables.set("Engine::Target", 1)
        self.variables.set("Engine::Target", 1)
        self.variables.set("Engine::Target", 2)
        self.assertEqual(seen, [("Engine::Target", 1), ("Engine::Target", 2)])

    def test_definitions_are_kept_and_values_start_again(self):
        self.variables.define(SysVarDefinition("Engine::Target", "float", 800.0, "rpm", "setpoint"))
        self.variables.set("Engine::Target", 1500.0)
        again = SystemVariables(self.settings)
        self.assertEqual(again.definition("Engine::Target").unit, "rpm")
        self.assertEqual(again.get("Engine::Target"), 800.0, "a new session starts from the initial value")
        self.variables.reset()
        self.assertEqual(self.variables.get("Engine::Target"), 800.0)
        self.variables.remove("Engine::Target")
        self.assertEqual(SystemVariables(self.settings).names(), [])

    def test_definitions_travel_as_a_file(self):
        self.variables.define(SysVarDefinition("Engine::Target", "float", 800.0, "rpm"))
        path = self.folder / "vars.json"
        self.variables.save_file(path)
        other = SystemVariables()
        self.assertEqual(other.load_file(path), 1)
        self.assertEqual(other.get("Engine::Target"), 800.0)
        path.write_text(json.dumps([{"name": "nonsense"}]))
        with self.assertRaises(ValueError):
            other.load_file(path)


class VariablesWindowTest(unittest.TestCase):
    def setUp(self):
        self.settings, _ = temp_settings(self)
        self.variables = SystemVariables(self.settings)
        self.variables.define(SysVarDefinition("Engine::Target", "float", 800.0, "rpm"))
        self.window = SystemVariablesWindow(self.variables)
        self.addCleanup(self.window.close)

    def item(self, name):
        return next(self.window.tree.topLevelItem(row) for row in range(self.window.tree.topLevelItemCount())
                    if self.window.tree.topLevelItem(row).text(COL_NAME) == name)

    def test_typing_a_value_sets_the_variable(self):
        self.item("Engine::Target").setText(COL_VALUE, "1234.5")
        self.assertEqual(self.variables.get("Engine::Target"), 1234.5)
        self.item("Engine::Target").setText(COL_VALUE, "fast")
        self.assertIn("not a number", self.window.status.text())
        self.assertEqual(self.item("Engine::Target").text(COL_VALUE), "1234.5", "the bad value is put back")

    def test_the_window_follows_changes_made_elsewhere(self):
        self.variables.set("Engine::Target", 999.0)
        self.assertEqual(self.item("Engine::Target").text(COL_VALUE), "999")
        self.variables.set("Body::Doors", 4)
        self.assertEqual(self.item("Body::Doors").text(COL_VALUE), "4")

    def test_a_new_variable_from_the_dialog(self):
        def fill(dialog):
            dialog.name_edit.setText("Body::Speed")
            dialog.kind_combo.setCurrentText("int")
            dialog.initial_edit.setText("30")
            dialog._accept()
            return QDialog.Accepted

        with patch.object(DefinitionDialog, "exec_", fill):
            self.window.new_variable()
        self.assertEqual(self.variables.get("Body::Speed"), 30)
        bad = DefinitionDialog()
        self.addCleanup(bad.close)
        bad.name_edit.setText("Speed")
        bad._accept()
        self.assertIn("Namespace::Name", bad.message.text())


class ScriptEventsTest(unittest.TestCase):
    def setUp(self):
        channel = "sysvar-" + str(uuid.uuid4())
        self.bus = can.Bus(interface="virtual", channel=channel)
        self.addCleanup(self.bus.shutdown)
        self.variables = SystemVariables()
        self.values, self.messages = {}, []
        self.runtime = ScriptRuntime(ReceiveMailbox(self.bus), validate_config({"name": "t"}), {}, None,
                                     sysvars=self.variables)
        self.runtime.value_changed.connect(lambda name, value: self.values.__setitem__(name, value))
        self.runtime.message.connect(lambda level, text: self.messages.append((level, text)))
        path = Path(tempfile.mkdtemp()) / "script.py"
        path.write_text(SCRIPT)
        self.runtime.start(path)
        self.addCleanup(self.runtime.stop)
        self.assertTrue(wait(lambda: "target" in self.values), self.messages)

    def test_the_script_and_the_user_share_the_variables(self):
        self.assertEqual(self.variables.get("Engine::Target"), 10.0)          # set by the script
        self.assertEqual(self.values["target"], 10.0)                         # and it heard its own change
        self.variables.set("Engine::Target", 20.0)                            # the user types a value
        self.assertTrue(wait(lambda: self.values.get("target") == 20.0))
        self.variables.set("Body::Doors", 4)                                  # @on_sysvar() hears every one
        self.assertTrue(wait(lambda: self.values.get("any") == 4))

    def test_keys_error_frames_and_the_bus_state(self):
        self.runtime.post("key", "a", None)
        self.runtime.post("key", "b", None)
        self.runtime.post("key", "F5", None)
        self.assertTrue(wait(lambda: self.values.get("key") == "F5"))
        self.assertEqual(self.runtime.namespace["seen"]["keys"], ["a", "F5"], "b has no handler")
        self.runtime.post("error_frame", None, 5.25)
        self.runtime.post("bus_state", None, "bus off")
        self.assertTrue(wait(lambda: self.values.get("bus_state") == "bus off"))
        self.assertEqual(self.values["error_frame"], 5.25)

    def test_output_has_its_level(self):
        self.assertTrue(wait(lambda: ("info", "hello") in self.messages))
        self.assertIn(("warning", "careful"), self.messages)

    def test_the_watch_shows_the_scripts_own_variables(self):
        rows = {name: (kind, value) for name, kind, value in
                watch_values(self.runtime.namespace, self.runtime.hidden_names)}
        self.assertEqual(rows["counter"], ("int", "3"))
        self.assertIn("seen", rows)
        self.assertFalse({"begin", "target", "on_start", "RDBI"} & set(rows), "functions are not variables")


class WriteWindowTest(unittest.TestCase):
    def test_levels_can_be_filtered_and_searched(self):
        window = WriteWindow()
        self.addCleanup(window.close)
        window.add(1_700_000_000.0, "info", "starting")
        window.add(1_700_000_001.0, "warning", "slow answer")
        window.add(1_700_000_002.0, "error", "Script callback failed: boom")
        self.assertEqual(len(window.lines()), 3)
        window.level_combo.setCurrentText("Warnings and errors")
        self.assertEqual(len(window.lines()), 2)
        window.level_combo.setCurrentText("Errors only")
        self.assertEqual([line.split("  ", 1)[1] for line in window.lines()], ["Script callback failed: boom"])
        window.level_combo.setCurrentText("Everything")
        window.filter_edit.setText("slow")
        self.assertEqual(len(window.lines()), 1)

    def test_the_watch_tab_lists_the_variables(self):
        namespace = {"speed": 12.5, "name": "bench", "helper": len, "_private": 1, "api_name": 3}
        window = WriteWindow(watch=lambda: (namespace, {"api_name"}))
        self.addCleanup(window.close)
        rows = window.fill_watch()
        self.assertEqual([row[0] for row in rows], ["name", "speed"])
        namespace["speed"] = 13.0
        window.fill_watch()
        self.assertEqual(window.watch_tree.topLevelItem(1).text(2), "13.0")


class LoggerSysvarTest(unittest.TestCase):
    def test_numeric_variables_can_be_plotted(self):
        logger = CANLoggerWindow()
        self.addCleanup(logger.close)
        logger.on_sysvar("Engine::Target", 800.0, 1_700_000_000.0, "rpm")
        logger.on_sysvar("Engine::Target", 900.0, 1_700_000_001.0, "rpm")
        logger.on_sysvar("Engine::Mode", "eco", 1_700_000_001.0)             # text: nothing to plot
        self.assertIn("Engine::Target", logger._items)
        self.assertNotIn("Engine::Mode", logger._items)
        self.assertEqual(list(logger._series["Engine::Target"].values()), [800.0, 900.0])
        self.assertEqual(logger._units["Engine::Target"], "rpm")
        logger.set_signal_plotted("Engine::Target")
        self.assertIn("Engine::Target", logger._plots)


if __name__ == "__main__":
    unittest.main()
