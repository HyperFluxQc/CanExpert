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
        # The window holds the channel for its own requests; an ECU with other identifiers may join it.
        self.assertIsNone(claim_channel("virtual", self.channel, TESTER))
        other = claim_channel("virtual", self.channel, 0x7E1)
        self.assertIsNotNone(other)
        other.close()
        self.window.disconnect_ecu()
        self.assertEqual(self.window.connect_button.text(), "Connect")
        self.assertIsNone(uds_rdbi(self.tester, 0xF195, TESTER, ECU, timeout=0.3))
        lock = claim_channel("virtual", self.channel, TESTER)
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
        self.assertEqual(self.window.status_labels["Security"].text(), "unlocked (level 01)")
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

    def test_settings_remembered_before_the_signals_get_their_dids(self):
        import json
        old = {"connection": {"interface": "virtual"},
               "ecu": {"key_mask": 0x3C, "dids": [{"did": 0xF190, "data": "4142", "writable": True},
                                                  {"did": 0x0101, "data": "ff", "writable": False}]}}
        dummy_ecu_window.app_settings().setValue(dummy_ecu_window.SETTINGS_KEY, json.dumps(old))
        saved, _ = dummy_ecu_window.saved_profile()
        dids = {item["did"]: item for item in saved.dids}
        self.assertEqual(dids[0xF190]["data"], "4142", "what was there stays")
        self.assertEqual(dids[0x0101]["data"], "ff")
        self.assertNotIn("signal", dids[0x0101], "and is not changed")
        self.assertIn(0xF201, dids)
        self.assertEqual(dids[0x0200].get("level"), 0x01)
        self.assertEqual(saved.key_mask, 0x3C)


class SimulationTabsTest(unittest.TestCase):
    """The tabs of the simulation: Signals, Access, Errors, the fault memory and the bootloader."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat)
        patcher = patch.object(dummy_ecu_window, "app_settings", lambda: settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = DummyEcuWindow(EcuConfig(broadcast_interval=0),
                                     {"interface": "virtual", "channel": "tabs-" + str(uuid.uuid4())})
        self.addCleanup(self.window.close)

    def row_of(self, table, text, column=0):
        return next(row for row in range(table.rowCount()) if table.item(row, column).text() == text)

    def test_a_generator_is_changed_while_the_ecu_runs(self):
        window = self.window
        row = self.row_of(window.signal_table, "EngineData.Temperature")
        self.assertEqual(window.signal_table.cellWidget(row, dummy_ecu_window.SIG_KIND).currentData(), "running")
        window.signal_table.cellWidget(row, dummy_ecu_window.SIG_KIND).setCurrentIndex(
            window.signal_table.cellWidget(row, dummy_ecu_window.SIG_KIND).findData("constant"))
        window.signal_table.item(row, dummy_ecu_window.SIG_LOW).setText("50")
        self.assertEqual(window.ecu.signals.value("EngineData.Temperature"), 50.0)
        self.assertIn("Constant", window.generator_hint.text())
        window.tabs.setCurrentWidget(window.signals_tab)
        window._refresh_status()
        self.assertEqual(window.signal_table.item(row, dummy_ecu_window.SIG_NOW).text(), "50")
        window.signal_table.item(row, dummy_ecu_window.SIG_LOW).setText("warm")
        self.assertIn("Not applied", window.statusBar().currentMessage())

    def test_another_dbc(self):
        window = self.window
        path = Path(self.temp.name) / "body.dbc"
        path.write_text('VERSION ""\n\nNS_ :\n\nBS_:\n\nBU_: Body\n\nBO_ 1024 Doors: 8 Body\n'
                        ' SG_ Open : 0|1@1+ (1,0) [0|1] "" Vector__XXX\n\nBO_ 1025 Seats: 8 Body\n'
                        ' SG_ Heat : 0|8@1+ (1,0) [0|3] "" Vector__XXX\n', encoding="utf-8")
        self.assertTrue(window.choose_dbc(str(path)))
        self.assertEqual(window.ecu.config.dbc_path, str(path))
        self.assertEqual(window.signal_table.rowCount(), 2)
        self.assertEqual(window.ecu.signals.message_ids(), {0x400, 0x401})
        seats = self.row_of(window.message_table, "Seats")
        window.message_table.item(seats, dummy_ecu_window.MSG_SEND).setCheckState(dummy_ecu_window.Qt.Unchecked)
        self.assertEqual(window.ecu.signals.message_ids(), {0x400})
        self.assertIn({"message": "Seats", "on": False, "cycle_ms": 0}, window.ecu.config.messages)
        with patch.object(dummy_ecu_window.QMessageBox, "warning") as warned:
            self.assertFalse(window.choose_dbc(str(Path(self.temp.name) / "missing.dbc")))
        warned.assert_called_once()
        self.assertEqual(window.ecu.config.dbc_path, str(path), "the one in use stays")
        self.assertTrue(window.choose_dbc(""))
        self.assertEqual(window.ecu.signals.message_ids(), {0x300, 0x301})
        row = self.row_of(window.signal_table, "EngineData.Temperature")
        self.assertEqual(window.signal_table.cellWidget(row, dummy_ecu_window.SIG_KIND).currentData(), "running",
                         "the built-in database comes back with the ECU's usual traffic")

    def test_security_levels_service_rules_and_errors(self):
        window = self.window
        window.level_buttons.add_button.click()
        self.assertEqual(window.ecu.config.security_levels,
                         [{"level": 0x03, "seed_length": 4, "key_mask": 0x5A, "dll": "", "variant": ""}])
        window.rule_buttons.add_button.click()
        self.assertEqual(window.ecu.config.service_rules, [{"sid": 0x2F, "sessions": [3], "level": 0x01}])
        window.rule_table.item(0, 1).setText("default, e")
        self.assertEqual(window.ecu.config.service_rules[0]["sessions"], [1, 3])
        window.rule_table.item(0, 0).setText("22")
        self.assertEqual(window.rule_table.item(0, 3).text(), "ReadDataByIdentifier")
        window.rule_table.item(0, 1).setText("warp")
        self.assertIn("no session", window.statusBar().currentMessage())
        window.rule_table.item(0, 1).setText("")
        window.error_spins["error_refuse"].setValue(30)
        window.error_refuse_nrc.setValue(0x22)
        self.assertEqual((window.ecu.config.error_refuse, window.ecu.config.error_refuse_nrc), (30, 0x22))
        self.assertEqual(window.error_nrc_text.text(), "conditionsNotCorrect")

    def test_faults_and_operation_cycles_from_the_data_tab(self):
        window = self.window
        row = self.row_of(window.dtc_table, "C10000")                          # U0100: 08, confirmed
        window.dtc_table.item(row, dummy_ecu_window.DTC_FAULT).setCheckState(dummy_ecu_window.Qt.Checked)
        self.assertEqual(window.ecu.dtcs[0xC10000], 0xAF)
        self.assertEqual(window.dtc_table.item(row, dummy_ecu_window.DTC_NOW).text(), "AF")
        self.assertIn("warningIndicatorRequested", window.dtc_table.item(row, dummy_ecu_window.DTC_NOW).toolTip())
        window.dtc_table.item(row, dummy_ecu_window.DTC_FAULT).setCheckState(dummy_ecu_window.Qt.Unchecked)
        window.new_operation_cycle()
        self.assertEqual(window.ecu.dtc_memory.cycle, 1)
        self.assertEqual(window.dtc_table.item(row, dummy_ecu_window.DTC_NOW).text(), "2C")
        window.confirm_cycles.setValue(4)
        self.assertEqual(window.ecu.dtc_memory.confirm_cycles, 4)
        window.snapshot_dids.setText("0101, F186")
        self.assertEqual(window.ecu.config.snapshot_dids, (0x0101, 0xF186))
        did = self.row_of(window.did_table, "0200")
        self.assertEqual(window.did_table.item(did, dummy_ecu_window.DID_SESSIONS).text(), "extended")
        self.assertEqual(window.did_table.item(did, dummy_ecu_window.DID_LEVEL).text(), "01")
        window.did_table.item(did, dummy_ecu_window.DID_SESSIONS).setText("default, extended")
        entry = next(item for item in window.ecu.config.dids if item["did"] == 0x0200)
        self.assertEqual((entry["sessions"], entry["level"]), ([1, 3], 1))
        self.assertEqual(window.ecu.did_access[0x0200], ("", (1, 3), 1))
        self.assertEqual(window.ecu.dtcs[0xC10000], 0x08, "an edited table starts the fault memory again")
        window.ecu.set_fault(0x010100, True)                                   # not through the box
        window._refresh_dtc_status()
        p0101 = self.row_of(window.dtc_table, "010100")
        self.assertEqual(window.dtc_table.item(p0101, dummy_ecu_window.DTC_FAULT).checkState(),
                         dummy_ecu_window.Qt.Checked, "the box follows the ECU")
        window.reset_ecu()
        self.assertEqual(window.dtc_table.item(p0101, dummy_ecu_window.DTC_FAULT).checkState(),
                         dummy_ecu_window.Qt.Unchecked)

    def test_bootloader_settings_and_a_profile_with_everything(self):
        window = self.window
        window.image_crc.setCurrentIndex(window.image_crc.findData("option"))
        window.version_from_image.setChecked(True)
        window.periodic_own_id.setChecked(True)
        window.periodic_id.setValue(0x5E8)
        window.error_spins["error_stall"].setValue(5)
        window.level_buttons.add_button.click()
        row = self.row_of(window.signal_table, "EcuStatus.Counter")
        window.signal_table.item(row, dummy_ecu_window.SIG_HIGH).setText("15")
        config = window.ecu.config
        self.assertEqual((config.image_crc, config.version_address, config.periodic_id), ("option", 0x20000, 0x5E8))
        path = Path(self.temp.name) / "everything.json"
        with patch.object(dummy_ecu_window.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
            window.save_profile()
        window.restore_defaults()
        self.assertEqual((window.ecu.config.image_crc, window.ecu.config.security_levels), ("off", []))
        with patch.object(dummy_ecu_window.QFileDialog, "getOpenFileName", return_value=(str(path), "")):
            window.load_profile()
        config = window.ecu.config
        self.assertEqual((config.image_crc, config.version_address, config.periodic_id, config.error_stall),
                         ("option", 0x20000, 0x5E8, 5))
        self.assertEqual(len(config.security_levels), 1)
        counter = next(item for item in config.generators if item["signal"] == "EcuStatus.Counter")
        self.assertEqual(counter["high"], 15.0)

    def test_the_status_says_what_the_ecu_is_doing(self):
        window, ecu = self.window, self.window.ecu
        ecu.state.bootloader = True
        ecu.state.periodic = {0x01: {"mode": 3, "next": 0}}
        ecu.state.events = [{"type": 0x03, "window": 2, "record": b"\x01\x01", "service": b"\x22\x01\x01"}]
        window._refresh_status()
        labels = window.status_labels
        self.assertEqual(labels["Application"].text(), "not valid: the bootloader runs")
        self.assertEqual(labels["Software version"].text(), "BOOTLOADER")
        self.assertEqual(labels["Periodic data"].text(), "F201 fast")
        self.assertEqual(labels["Events"].text(), "DID 0101: set up, not started")


if __name__ == "__main__":
    unittest.main()
