"""The main window's status strip (bus, session, security, last error), its keys, F1 help at the window being
worked in, and the About box with the versions a bug report needs."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtCore import QSettings
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QApplication, QDialog

import canexpert
from canexpert import about, can_bus
from canexpert import main_window as main
from canexpert.simulator.ecu import DummyEcu, EcuConfig
from canexpert.status_strip import DiagnosticState, StatusStrip

APP = QApplication.instance() or QApplication([])
PANEL = '''<application_database name="Status"><pages><page name="Main">
<value id="1" label="Status" binding_value="status" x="10" y="10"/>
</page></pages></application_database>'''


def spin_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


class DiagnosticStateTest(unittest.TestCase):
    def test_session_and_security_follow_the_answers(self):
        state = DiagnosticState()
        self.assertEqual((state.session_text(), state.security_text()), ("unknown", "locked"))
        self.assertTrue(state.on_response(bytes.fromhex("5003 0032 01f4")))
        self.assertFalse(state.on_response(b"\x67\x01\x11\x22"), "requestSeed's answer unlocks nothing")
        self.assertIsNone(state.security)
        state.on_response(b"\x67\x02")
        self.assertEqual((state.session_text(), state.security_text()), ("extended", "unlocked (level 1)"))
        state.on_response(b"\x50\x02")
        self.assertEqual((state.session_text(), state.security_text()), ("programming", "locked"))
        state.on_response(b"\x67\x06")
        self.assertEqual(state.security_text(), "unlocked (level 5)")
        state.on_response(b"\x51\x01")
        self.assertEqual((state.session_text(), state.security_text()), ("default", "locked"), "a reset")

    def test_negative_answers_are_the_last_error_but_pending_is_not(self):
        state = DiagnosticState()
        self.assertFalse(state.on_response(b"\x7f\x22\x78"))
        self.assertEqual(state.error, "")
        self.assertTrue(state.on_response(b"\x7f\x22\x31"))
        self.assertEqual(state.error, "ReadDataByIdentifier: NRC 0x31 requestOutOfRange")
        self.assertFalse(state.on_response(b"\x62\xf1\x90"))

    def test_only_single_frames_carry_what_is_followed(self):
        self.assertEqual(DiagnosticState.payload(bytes.fromhex("025003aaaaaaaaaa")), b"\x50\x03")
        self.assertEqual(DiagnosticState.payload(bytes.fromhex("55026702"), 0x55), b"\x67\x02")
        self.assertIsNone(DiagnosticState.payload(bytes.fromhex("1014 62f190")), "a first frame")
        self.assertIsNone(DiagnosticState.payload(bytes.fromhex("0762")), "longer than the frame")


class StatusStripTest(unittest.TestCase):
    def setUp(self):
        self.strip = StatusStrip()
        self.addCleanup(self.strip.deleteLater)

    def test_what_it_shows(self):
        self.assertEqual(self.strip.bus_label.text(), "Bus: not connected")
        self.assertTrue(self.strip.session_label.isHidden())
        self.strip.connected()
        self.assertEqual(self.strip.bus_label.text(), "Bus: on")
        self.assertIn("does not report", self.strip.bus_label.toolTip())
        self.strip.set_bus("error passive", 3)
        self.assertEqual(self.strip.bus_label.text(), "Bus: error passive, 3 error frames")
        self.assertIn("orange", self.strip.bus_label.styleSheet())
        self.strip.on_response(b"\x50\x03")
        self.strip.on_response(b"\x67\x02")
        self.assertEqual((self.strip.session_label.text(), self.strip.security_label.text()),
                         ("Session: extended", "Security: unlocked (level 1)"))
        self.assertIn("green", self.strip.security_label.styleSheet())
        self.assertTrue(self.strip.error_label.isHidden())
        self.strip.on_response(b"\x7f\x31\x22")
        self.assertIn("RoutineControl: NRC 0x22 conditionsNotCorrect", self.strip.error_label.text())
        self.strip.set_error("Script callback failed: <boom>\n  at line 3")
        self.assertIn("Script callback failed: &lt;boom&gt; at line 3", self.strip.error_label.text())
        clicked = []
        self.strip.error_clicked.connect(lambda: clicked.append(True))
        self.strip.error_label.linkActivated.emit("log")
        self.assertEqual(clicked, [True])
        self.strip.disconnected()
        self.assertEqual(self.strip.bus_label.text(), "Bus: not connected")
        self.assertTrue(self.strip.session_label.isHidden())
        self.assertFalse(self.strip.error_label.isHidden(), "the last error stays until the next one")

    def test_a_long_error_is_shortened_but_kept_whole_in_the_tooltip(self):
        self.strip.set_error("x" * 200)
        self.assertLess(len(self.strip.error_label.text()), 150)
        self.assertIn("x" * 200, self.strip.error_label.toolTip())


class AboutTest(unittest.TestCase):
    def test_the_versions(self):
        rows = dict(about.versions())
        self.assertEqual(rows["CAN Expert"], canexpert.__version__)
        self.assertEqual(rows["python-can"], can.__version__)
        for name in ("Python", "Qt", "PyQt5", "cantools", "odxtools", "pyqtgraph", "PyQtAds", "Kvaser CANlib",
                     "Vector XL Driver Library", "IXXAT VCI", "Operating system"):
            self.assertTrue(rows[name], name)
        self.assertEqual(about.package_version("no-such-package-here"), about.NOT_INSTALLED)
        self.assertEqual(about.package_version("no-such-package-here", "string"), about.NOT_INSTALLED,
                         "a module without __version__")

    def test_the_build_date(self):
        self.assertEqual(about.build_date(), "running from source")
        with patch.object(about.sys, "frozen", True, create=True):
            self.assertRegex(about.build_date(), r"^\d{4}-\d\d-\d\d \d\d:\d\d$")

    def test_a_driver_that_is_missing_and_one_that_is_there(self):
        self.assertEqual(about.driver_version(("no_such_driver_dll",)), about.NOT_INSTALLED)
        with patch.object(about.ctypes.util, "find_library", return_value="C:/drivers/canlib32.dll"), \
                patch.object(about, "file_version", return_value=None):
            self.assertEqual(about.driver_version(("canlib32",)), "installed")

    @unittest.skipUnless(os.name == "nt", "a Windows version resource")
    def test_a_windows_file_version(self):
        version = about.file_version(os.path.join(os.environ["SystemRoot"], "System32", "kernel32.dll"))
        self.assertRegex(version, r"^\d+\.\d+\.\d+\.\d+$")

    def test_the_dialog_copies_its_text(self):
        dialog = about.AboutDialog()
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.table.rowCount(), len(dialog.rows))
        dialog.copy()
        self.assertTrue(APP.clipboard().text().startswith(f"CAN Expert: {canexpert.__version__}\nBuilt: running from source\nPython: "))


class MainWindowTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        configs, databases = root / "Configurations", root / "Databases"
        configs.mkdir()
        databases.mkdir()
        (configs / "config_Bench.json").write_text(json.dumps({
            "name": "Bench", "request_id": 0x7E0, "response_id": 0x7E8, "database_family": "panel",
            "tester_present_interval_seconds": 0.5, "node_timeout_seconds": 2}))
        (databases / "panel_2026-09-24.xml").write_text(PANEL)
        settings = QSettings(str(root / "settings.ini"), QSettings.IniFormat)
        settings.setValue("last_configuration", "Bench")
        channel = "status-" + str(uuid.uuid4())
        ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.ecu = DummyEcu(ecu_bus, EcuConfig(broadcast_interval=0), log=lambda text: None)
        stop = threading.Event()
        threading.Thread(target=self.ecu.serve, args=(stop,), daemon=True).start()
        patches = [patch.object(main, "CONFIG_DIR", configs), patch.object(main, "DATABASES_DIR", databases),
                   patch.object(main, "app_settings", lambda: settings),
                   patch.object(main.can, "detect_available_configs", return_value=[]),
                   patch.object(can_bus, "create_can_bus",
                                lambda *args, **options: can.Bus(interface="virtual", channel=channel))]
        for item in patches:
            item.start()
        self.window = main.MainWindow()
        self.window.selected_channel_config = {"interface": "virtual", "channel": 0}

        def close():
            self.window.close()
            APP.processEvents()
            stop.set()
            time.sleep(.05)
            ecu_bus.shutdown()
            for item in reversed(patches):
                item.stop()
        self.addCleanup(close)

    def run_in_console(self, console, call, title):
        self.assertIsNotNone(console.run(call, title), console.log.toPlainText())
        self.assertTrue(spin_until(lambda: not console._busy), console.log.toPlainText())

    def test_the_strip_follows_the_diagnostics_whoever_sends_them(self):
        strip = self.window.status_strip
        self.window.on_connect_clicked()
        self.assertIsNotNone(self.window.worker, self.window.status_label.text())
        # python-can's virtual bus says it is error active; an adapter that says nothing shows "on".
        self.assertTrue(spin_until(lambda: strip.bus_label.text() in ("Bus: error active", "Bus: on")),
                        strip.bus_label.text())
        self.assertEqual(strip.session_label.text(), "Session: unknown")
        console = self.window.open_uds_console()
        self.run_in_console(console, lambda uds: uds.DSC(0x03), "DiagnosticSessionControl")
        self.assertTrue(spin_until(lambda: strip.session_label.text() == "Session: extended"))
        self.run_in_console(console, lambda uds: uds.SecurityUnlock(0x01, lambda seed: bytes(b ^ 0xA5 for b in seed)),
                            "SecurityAccess")
        self.assertTrue(spin_until(lambda: strip.security_label.text() == "Security: unlocked (level 1)"))
        self.run_in_console(console, lambda uds: uds.RDBI(0x1234), "RDBI")
        self.assertTrue(spin_until(lambda: "requestOutOfRange" in strip.error_label.text()))
        self.window.write_message("error", "Database script failed: boom")
        self.assertIn("Database script failed: boom", strip.error_label.text())
        self.window.on_disconnect_clicked()
        self.assertEqual(strip.bus_label.text(), "Bus: not connected")

    def test_keys(self):
        actions = self.window._toolbar_actions
        self.assertEqual(actions["connect"].shortcut(), QKeySequence("F9"))
        self.assertEqual(actions["disconnect"].shortcut(), QKeySequence("Shift+F9"))
        self.assertEqual(actions["trace"].shortcut(), QKeySequence("Ctrl+1"))
        self.assertIn("Shortcut: Ctrl+6", actions["console"].toolTip())
        self.assertEqual(self.window.context_help_action.shortcut(), QKeySequence.keyBindings(QKeySequence.HelpContents)[0])
        self.assertIn(self.window.context_help_action, self.window.actions(), "F1 works without opening the menu")
        # Every key is used once, and none is a plain letter or F5, which panel scripts' @on_key take.
        keys = [action.shortcut().toString() for action in self.window._shortcut_actions]
        self.assertEqual(len(keys), len(set(keys)), keys)
        self.assertFalse([key for key in keys if len(key) == 1 or key == "F5"])
        # Disconnect and its key are off until there is something to disconnect.
        self.assertFalse(actions["disconnect"].isEnabled())
        self.assertFalse(self.window.disconnect_btn.isEnabled())
        self.window.on_connect_clicked()
        self.assertTrue(actions["disconnect"].isEnabled() and not actions["connect"].isEnabled())
        self.assertFalse(self.window.connect_btn.isEnabled())
        actions["disconnect"].trigger()
        self.assertIsNone(self.window.can_bus)
        self.assertTrue(actions["connect"].isEnabled())

    def test_a_floating_window_gets_the_keys(self):
        self.window.open_trace()
        trace = self.window.tool_panes["trace"]
        trace.setFloating()
        APP.processEvents()
        floating = trace.dockContainer().floatingWidget()
        self.assertIn(self.window.context_help_action, floating.actions())
        self.assertIn(self.window._toolbar_actions["console"], floating.actions())

    def test_f1_opens_the_manual_where_the_window_is_explained(self):
        console = self.window.open_uds_console()
        trace = self.window.open_trace()
        self.assertEqual(self.window.help_section(console.dtc_table), "UDS Console")
        self.assertEqual(self.window.help_section(trace), "Trace window")
        self.assertEqual(self.window.help_section(self.window.config_list), "Configurations")
        self.assertEqual(self.window.help_section(self.window.channel_list), "Connecting")
        self.assertEqual(self.window.help_section(self.window.debug_log), "Starting up")
        self.window.on_connect_clicked()
        self.assertEqual(self.window.help_section(self.window.panel), "Using a panel")
        with patch.object(self.window, "help_section", return_value="UDS Console"):
            manual = self.window.context_help()
        self.addCleanup(manual.close)
        heading = manual.browser.textCursor().block()
        self.assertEqual((heading.text(), bool(heading.blockFormat().headingLevel())), ("UDS Console", True))
        shortcuts = self.window.open_manual("Keyboard shortcuts")
        self.assertEqual(shortcuts.browser.textCursor().block().text(), "Keyboard shortcuts")

    def test_the_about_box(self):
        with patch.object(QDialog, "exec_", return_value=0):
            dialog = self.window.show_about()
        self.assertIn(canexpert.__version__, dialog.text())


if __name__ == "__main__":
    unittest.main()
