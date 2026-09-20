"""A measurement without a panel database, passive mode, recording, offline replay and the workspace."""
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
from PyQt5.QtWidgets import QApplication

from canexpert import can_bus
from canexpert import main_window as main
from canexpert.recording import Recorder, read_frames

APP = QApplication.instance() or QApplication([])


def spin_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


class MeasurementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.configs = self.root / "Configurations"
        self.databases = self.root / "Databases"
        self.configs.mkdir()
        self.databases.mkdir()
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.IniFormat)
        config = {"name": "Bus", "request_id": 0x7E0, "response_id": 0x7E8,
                  "tester_present_interval_seconds": .05, "node_timeout_seconds": .2,
                  "database_family": "panel"}
        (self.configs / "config_Bus.json").write_text(json.dumps(config))
        self.channel = "measure-" + str(uuid.uuid4())
        self.ecu = can.Bus(interface="virtual", channel=self.channel)
        self.patches = [
            patch.object(main, "CONFIG_DIR", self.configs),
            patch.object(main, "DATABASES_DIR", self.databases),
            patch.object(main, "app_settings", lambda: self.settings),
            patch.object(main.can, "detect_available_configs", return_value=[]),
            patch.object(can_bus, "create_can_bus", self.fake_can_bus),
        ]
        for item in self.patches:
            item.start()
        self.window = main.MainWindow()
        self.window.selected_channel_config = {"interface": "virtual", "channel": 0}

    def fake_can_bus(self, interface, channel, bitrate, **options):
        self.options = options
        return can.Bus(interface="virtual", channel=self.channel)

    def tearDown(self):
        self.window.close()
        APP.processEvents()
        self.ecu.shutdown()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def send_from_ecu(self, can_id=0x300, data=b"\x01\x02"):
        self.ecu.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=False))

    # --- a measurement needs no panel database ------------------------------------------------

    def test_a_measurement_runs_without_a_database_and_stays_quiet(self):
        self.window.active_config["database_family"] = "nothing-here"
        self.window.start_measurement()
        self.assertIsNotNone(self.window.can_bus, self.window.status_label.text())
        self.assertTrue(self.window.measurement_only)
        self.assertIsNone(self.window.panel)
        self.assertIsNone(self.window.script_runtime)
        self.assertFalse(self.window.connect_btn.isEnabled())
        self.assertTrue(self.window.disconnect_btn.isEnabled())
        self.assertIn("Measurement running", self.window.status_label.text())
        # No TesterPresent: a measurement only watches (that is what Check ECUs is for).
        self.assertFalse(spin_until(lambda: self.ecu.recv(0) is not None, timeout=.4))
        # Received frames still reach the application, with the adapter's timestamp.
        self.send_from_ecu(0x123, b"\xaa\xbb")
        self.assertTrue(spin_until(lambda: any(frame[2] == 0x123 for frame in self.window.frame_history)))
        frame = next(f for f in self.window.frame_history if f[2] == 0x123)
        self.assertEqual((frame[1], frame[3]), ("RX", b"\xaa\xbb"))
        self.assertAlmostEqual(frame[0], time.time(), delta=30)
        self.assertIn("ID: 0x123", self.window.can_log.toPlainText())

    def test_connect_still_requires_a_database(self):
        # Requirement 2.4 is unchanged: Connect loads a panel, and says so when there is none.
        self.window.active_config["database_family"] = "nothing-here"
        self.window.on_connect_clicked()
        self.assertIsNone(self.window.can_bus)
        self.assertIn("No matching database", self.window.status_label.text())
        self.assertTrue(self.window.connect_btn.isEnabled())

    def test_passive_mode_never_transmits(self):
        self.window.passive_action.setChecked(True)
        self.window.start_measurement()
        self.assertTrue(self.window.passive_measurement)
        with self.assertRaises(RuntimeError) as raised:
            self.window.send_can_message(0x200, b"\x01")
        self.assertIn("passive", str(raised.exception).lower())
        self.assertFalse(spin_until(lambda: self.ecu.recv(0) is not None, timeout=.3))
        self.assertTrue(self.settings.value(main.PASSIVE, False, type=bool))

    def test_passive_asks_a_kvaser_adapter_for_silent_mode(self):
        calls = []

        def fake(interface, channel, bitrate, **options):
            calls.append((interface, options))
            return can.Bus(interface="virtual", channel=self.channel + "-silent")

        with patch.object(can_bus, "create_can_bus", fake):
            bus = can_bus.open_channel({"interface": "kvaser", "channel": 1}, 500000, passive=True)
            bus.shutdown()
            bus = can_bus.open_channel({"interface": "kvaser", "channel": 1}, 500000)
            bus.shutdown()
            bus = can_bus.open_channel({"interface": "virtual", "channel": 0}, 500000, passive=True)
            bus.shutdown()
        self.assertEqual(calls[0], ("kvaser", {"driver_mode": False}))
        self.assertEqual(calls[1], ("kvaser", {}))
        self.assertEqual(calls[2], ("virtual", {}))    # only Kvaser has a silent mode in python-can

    def test_a_measurement_stops_without_starting_the_ecu_check(self):
        self.window.start_measurement()
        self.window.disconnect_database()
        self.assertIsNone(self.window.can_bus)
        self.assertIsNone(self.window.ecu_monitor)
        self.assertTrue(self.window.connect_btn.isEnabled())

    # --- every window sees the measurement ------------------------------------------------------

    def test_a_window_opened_later_still_shows_what_was_received(self):
        self.window.start_measurement()
        self.send_from_ecu(0x321, b"\x05")
        self.assertTrue(spin_until(lambda: any(frame[2] == 0x321 for frame in self.window.frame_history)))
        trace = self.window.open_trace()
        self.assertEqual(trace.tree.topLevelItemCount(), len(self.window.frame_history))
        self.assertTrue(any(trace.tree.topLevelItem(row).text(2) == "321"
                            for row in range(trace.tree.topLevelItemCount())))

    def test_the_ecu_check_also_feeds_the_trace(self):
        # Frames seen while only the ECUs are checked used to reach the CAN monitor alone.
        trace = self.window.open_trace()
        self.window.check_ecus({"interface": "virtual", "channel": 0})
        self.assertIsNotNone(self.window.ecu_monitor)
        self.send_from_ecu(0x456, b"\x07")
        self.assertTrue(spin_until(lambda: (trace.flush(), any(frame[2] == 0x456 for frame in trace.frames))[1]))

    # --- recording and offline replay -------------------------------------------------------------

    def test_recording_writes_a_file_that_can_be_read_back(self):
        path = self.root / "session.asc"
        recorder = Recorder(path)
        recorder.write(1000.5, "RX", 0x300, b"\x01\x02\x03", False)
        recorder.write(1000.75, "TX", 0x7E0, b"\x02\x3e\x00", False)
        recorder.stop()
        self.assertEqual(recorder.count, 2)
        frames = read_frames(path)
        self.assertEqual([(frame[1], frame[2], frame[3]) for frame in frames],
                         [("RX", 0x300, b"\x01\x02\x03"), ("TX", 0x7E0, b"\x02\x3e\x00")])
        self.assertAlmostEqual(frames[1][0] - frames[0][0], 0.25, places=3)

    def test_the_measurement_is_recorded_and_replayed_into_the_windows(self):
        path = self.root / "measurement.asc"
        with patch.object(main.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
            self.assertIsNotNone(self.window.start_recording())
        self.window.start_measurement()
        self.send_from_ecu(0x300, b"\x11\x22")
        self.send_from_ecu(0x301, b"\x33")
        self.assertTrue(spin_until(lambda: self.window.recorder.count >= 2))
        self.window.on_disconnect_clicked()               # stops the recording as well
        self.assertIsNone(self.window.recorder)
        self.assertTrue(path.exists())

        # Offline: the file plays back into the trace, without any bus.
        with patch.object(main.QFileDialog, "getOpenFileName", return_value=(str(path), "")):
            dialog = self.window.replay_log()
        self.addCleanup(dialog.close)
        trace = self.window.tool_widget("trace")
        trace.clear()
        self.assertIn("Offline", trace.offline_label.text())
        dialog.speed_combo.setCurrentIndex(dialog.speed_combo.count() - 1)   # as fast as possible
        dialog.start_replay()
        self.assertTrue(spin_until(lambda: (trace.flush(), len(trace.frames) >= 2)[1]))
        self.assertEqual(sorted(frame[2] for frame in trace.frames)[:2], [0x300, 0x301])
        self.assertIsNone(self.window.can_bus, "replaying must not open a bus")

    # --- the workspace ----------------------------------------------------------------------------

    def test_tool_windows_are_panes_of_the_main_window(self):
        for name, opener in (("trace", self.window.open_trace), ("logger", self.window.open_can_logger),
                             ("transmit", self.window.open_transmit), ("console", self.window.open_uds_console),
                             ("diagnostics", self.window.open_diagnostic_window)):
            widget = opener()
            dock = self.window.tool_docks[name]
            self.assertIs(dock.widget(), widget, name)
            self.assertIs(dock.parent(), self.window, name)
            self.assertFalse(dock.isHidden(), name)   # the test window itself is never shown
            self.assertIs(opener(), widget, f"{name} must be reused, not rebuilt")

    def test_the_layout_is_remembered_and_desktops_can_be_saved(self):
        self.window.open_trace()
        self.window.save_desktop("Analysis")
        self.assertIn("Analysis", self.window.desktops())
        self.window.tool_docks["trace"].hide()
        self.window.apply_desktop("Analysis")
        self.assertFalse(self.window.tool_docks["trace"].isHidden())
        self.window.reset_layout()
        self.assertTrue(self.window.tool_docks["trace"].isHidden())
        # Closing writes the arrangement, and a new window restores it.
        self.window.open_trace()
        self.window.close()
        self.assertTrue(self.settings.value(main.LAYOUT_STATE))
        restored = main.MainWindow()
        self.assertIsNotNone(restored.open_trace())
        restored.close()          # closed here, not in a cleanup: the settings patch is still in place


if __name__ == "__main__":
    unittest.main()
