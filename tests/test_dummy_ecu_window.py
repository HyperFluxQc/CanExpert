"""Dummy ECU window: settings applied live, connect/disconnect on a virtual bus, log, profiles."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from canexpert.simulator import window as dummy_ecu_window
from canexpert.simulator.ecu import EcuConfig, claim_channel, load_profile
from canexpert.simulator.window import DummyEcuWindow, parse_address_format, parse_byte_list, parse_ranges
from canexpert.uds.client import uds_rdbi, uds_request

APP = QApplication.instance() or QApplication([])
TESTER, ECU = 0x7E0, 0x7E8


def spin_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class DummyEcuWindowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat)
        patcher = patch.object(dummy_ecu_window, "app_settings", lambda: settings)  # keep the user's settings
        patcher.start()
        self.addCleanup(patcher.stop)
        self.channel = "window-" + str(uuid.uuid4())
        self.window = DummyEcuWindow(EcuConfig(erase_seconds=0.05, broadcast_interval=0),
                                     {"interface": "virtual", "channel": self.channel})
        self.tester = can.Bus(interface="virtual", channel=self.channel)

    def tearDown(self):
        self.window.close()
        self.tester.shutdown()
        self.temp.cleanup()

    def request(self, payload, response_id=ECU):
        return uds_request(self.tester, bytes(payload), TESTER, response_id, 1.0)

    def test_connect_answer_and_disconnect(self):
        self.assertTrue(self.window.connect_ecu())
        self.assertEqual(self.window.connect_button.text(), "Disconnect")
        self.assertFalse(self.window.channel.isEnabled())
        self.assertEqual(uds_rdbi(self.tester, 0xF195, TESTER, ECU), b"APP-1.0.0")
        self.assertIsNone(claim_channel("virtual", self.channel))                 # the window holds the channel
        self.window.disconnect_ecu()
        self.assertEqual(self.window.connect_button.text(), "Connect")
        self.assertIsNone(uds_rdbi(self.tester, 0xF195, TESTER, ECU, timeout=0.3))
        lock = claim_channel("virtual", self.channel)
        self.assertIsNotNone(lock)
        lock.close()

    def test_settings_apply_while_connected(self):
        self.window.connect_ecu()
        self.window.response_id.setValue(0x7E9)
        self.window.block_data.setValue(256)
        self.window.length_bytes.setValue(4)
        self.assertEqual((self.window.ecu.config.response_id, self.window.ecu.config.max_block_length), (0x7E9, 258))
        self.assertIn("Response: 74 40 00 00 01 02", self.window.block_hint.text())
        self.assertEqual(self.request([0x10, 0x03], 0x7E9)[:2], b"\x50\x03")
        self.assertEqual(self.request([0x10, 0x02], 0x7E9)[:2], b"\x50\x02")
        seed = self.request([0x27, 0x01], 0x7E9)[2:]
        self.assertEqual(self.request([0x27, 0x02, *(b ^ 0xA5 for b in seed)], 0x7E9), b"\x67\x02")
        self.assertEqual(self.request([0x31, 0x01, 0xFF, 0x00, 0x44, 0, 1, 0, 0, 0, 0, 1, 0], 0x7E9)[:1], b"\x71")
        self.assertEqual(self.request([0x34, 0x00, 0x44, 0, 1, 0, 0, 0, 0, 1, 0], 0x7E9), b"\x74\x40\x00\x00\x01\x02")
        spin_until(lambda: self.window.status_labels["Transfer"].text().startswith("Download"))
        self.assertEqual(self.window.status_labels["Security"].text(), "unlocked")
        self.assertEqual(self.window.status_labels["Transfer"].text(), "Download at 0x00010000: 0 / 256 bytes")

    def test_text_fields_are_checked(self):
        window, config = self.window, self.window.ecu.config
        window.memory_ranges.setText("10000-1FFFF, 0x20000-2003F")
        self.assertEqual(config.memory_ranges, ((0x10000, 0x1FFFF), (0x20000, 0x2003F)))
        window.memory_ranges.setText("10000-")                                     # incomplete: not applied
        self.assertEqual(config.memory_ranges, ((0x10000, 0x1FFFF), (0x20000, 0x2003F)))
        self.assertIn("background", window.memory_ranges.styleSheet())
        self.assertTrue(window.statusBar().currentMessage().startswith("Not applied"))
        window.memory_ranges.setText("")
        self.assertEqual((config.memory_ranges, window.memory_ranges.styleSheet()), ((), ""))
        window.address_format.setCurrentText("24")
        window.data_formats.setText("00 11")
        window.st_min_unit.setCurrentIndex(1)
        window.st_min.setValue(3)
        self.assertEqual((config.address_format, config.data_formats, config.st_min), (0x24, (0x00, 0x11), 0xF3))
        self.assertIn("30 08 F3", window.flow_preview.text())
        self.assertIn("300 µs", window.flow_preview.text())
        self.assertEqual(parse_ranges(""), ())
        self.assertIsNone(parse_address_format("Any"))
        self.assertEqual(parse_byte_list("00, 11"), (0x00, 0x11))
        for parse, text in ((parse_address_format, "40"), (parse_byte_list, "100"), (parse_ranges, "20-10")):
            with self.assertRaises(ValueError):
                parse(text)

    def test_log_shows_requests_and_frames(self):
        self.window.show_frames.setChecked(True)
        self.window.connect_ecu()
        self.assertEqual(uds_rdbi(self.tester, 0xF195, TESTER, ECU), b"APP-1.0.0")
        self.assertTrue(spin_until(lambda: "Rx  7E0  03 22 F1 95" in self.window.log_view.toPlainText()))
        text = self.window.log_view.toPlainText()
        self.assertIn("Tx  7E8  10 0C 62 F1 95 41 50 50", text)                   # first frame of the reply
        self.assertIn("Rx  7E0  30 00 00", text)                                   # the tester's flow control
        self.assertIn("<- ReadDataByIdentifier 22 f1 95", text)

    def test_profiles_and_remembered_settings(self):
        self.window.key_mask.setValue(0x3C)
        self.window.flow_waits.setValue(2)
        path = Path(self.temp.name) / "profile.json"
        with patch.object(dummy_ecu_window.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
            self.window.save_profile()
        config, connection = load_profile(path)
        self.assertEqual((config.key_mask, config.flow_waits), (0x3C, 2))
        self.assertEqual((connection["interface"], connection["channel"]), ("virtual", self.channel))
        self.window.restore_defaults()
        self.assertEqual((self.window.ecu.config.key_mask, self.window.ecu.config.flow_waits), (0xA5, 0))
        with patch.object(dummy_ecu_window.QFileDialog, "getOpenFileName", return_value=(str(path), "")):
            self.window.load_profile()
        self.assertEqual(self.window.key_mask.value(), 0x3C)
        self.window.close()
        saved, saved_connection = dummy_ecu_window.saved_profile()                # what the next start shows
        self.assertEqual((saved.key_mask, saved.flow_waits, saved_connection["channel"]), (0x3C, 2, self.channel))


if __name__ == "__main__":
    unittest.main()
