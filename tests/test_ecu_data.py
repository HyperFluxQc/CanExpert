"""The Dummy ECU's data: editable DIDs and DTCs (with snapshot and extended data), forced negative
responses, and several simulated ECUs sharing one channel."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtWidgets import QApplication

from canexpert.simulator import window as dummy_ecu_window
from canexpert.simulator.ecu import (DummyEcu, EcuConfig, config_from_dict, load_profile, other_ecu_present,
                                     save_profile)
from canexpert.simulator.window import DummyEcuWindow
from canexpert.uds.client import UdsFunctions, uds_request

APP = QApplication.instance() or QApplication([])
P0101 = 0x010100


class Bench:
    """One or more dummy ECUs on a virtual channel, and a tester."""

    def __init__(self, test, *configs):
        channel = "ecu-data-" + str(uuid.uuid4())
        self.tester = can.Bus(interface="virtual", channel=channel)
        test.addCleanup(self.tester.shutdown)
        self.stop = threading.Event()
        test.addCleanup(self.stop.set)
        self.ecus = []
        for config in configs:
            bus = can.Bus(interface="virtual", channel=channel)
            test.addCleanup(bus.shutdown)
            ecu = DummyEcu(bus, config, log=lambda text: None)
            threading.Thread(target=ecu.serve, args=(self.stop,), daemon=True).start()
            self.ecus.append(ecu)

    def request(self, payload, request_id=0x7E0, response_id=0x7E8):
        return uds_request(self.tester, bytes(payload), request_id, response_id, 1.0)

    def unlock(self, request_id=0x7E0, response_id=0x7E8):
        uds = UdsFunctions(lambda payload, timeout=None, wait=True: uds_request(
            self.tester, bytes(payload), request_id, response_id, timeout or 1.0, wait=wait))
        assert uds.DSC(0x03)
        assert uds.SecurityUnlock(0x01, lambda seed: bytes(byte ^ 0xA5 for byte in seed))


def quiet(**changes):
    return EcuConfig(broadcast_interval=0, **changes)


class DataTest(unittest.TestCase):
    def test_a_did_of_the_table_is_read_and_written(self):
        config = quiet(dids=[{"did": 0x1234, "data": "0102", "writable": True},
                             {"did": 0x5678, "data": "aa", "writable": False}])
        bench = Bench(self, config)
        self.assertEqual(bench.request(b"\x22\x12\x34"), b"\x62\x12\x34\x01\x02")
        self.assertEqual(bench.request(b"\x22\xF1\x90"), b"\x7f\x22\x31", "the default VIN is not in this table")
        bench.unlock()
        self.assertEqual(bench.request(b"\x2E\x12\x34\x0A\x0B"), b"\x6E\x12\x34")
        self.assertEqual(bench.request(b"\x22\x12\x34"), b"\x62\x12\x34\x0a\x0b")
        self.assertEqual(bench.request(b"\x2E\x12\x34\x0A"), b"\x7f\x2e\x13", "a DID keeps its length")
        self.assertEqual(bench.request(b"\x2E\x56\x78\x00"), b"\x7f\x2e\x31", "not writable")
        bench.ecus[0].power_on()
        self.assertEqual(bench.request(b"\x22\x12\x34"), b"\x62\x12\x34\x01\x02", "power-on restores the table")

    def test_the_dtc_records(self):
        bench = Bench(self, quiet())                        # the default table: P0101 with records, U0100 without
        supported = bench.request(b"\x19\x0A")
        self.assertEqual(supported[:3], b"\x59\x0a\xff")
        self.assertEqual(len(supported[3:]) // 4, 2)
        snapshot = bench.request(b"\x19\x04\x01\x01\x00\xff")
        self.assertEqual(snapshot, b"\x59\x04\x01\x01\x00\x09\x01\x01\xf4\x0d\x32")
        extended = bench.request(b"\x19\x06\x01\x01\x00\x01")
        self.assertEqual(extended, b"\x59\x06\x01\x01\x00\x09\x01\x05")
        self.assertEqual(bench.request(b"\x19\x04\xc1\x00\x00\xff"), b"\x59\x04\xc1\x00\x00\x08", "no record kept")
        self.assertEqual(bench.request(b"\x19\x04\x12\x34\x56\xff"), b"\x7f\x19\x31", "an unknown DTC")
        self.assertEqual(bench.request(b"\x19\x04\x01\x01\x00\x02"), b"\x7f\x19\x31", "an unknown record")
        self.assertEqual(bench.request(b"\x19\x04\x01\x01"), b"\x7f\x19\x13")

    def test_the_consoles_snapshot_and_extended_data_buttons_get_answers(self):
        bench = Bench(self, quiet())
        uds = UdsFunctions(lambda payload, timeout=None, wait=True: uds_request(
            bench.tester, bytes(payload), 0x7E0, 0x7E8, timeout or 1.0, wait=wait))
        self.assertTrue(uds.RDTCI(0x04, P0101, 0xFF))           # what the console's Snapshot button sends
        self.assertTrue(uds.RDTCI(0x06, P0101, 0xFF))

    def test_a_service_can_be_forced_to_refuse(self):
        bench = Bench(self, quiet(forced_nrcs=[{"sid": 0x22, "nrc": 0x22}]))
        self.assertEqual(bench.request(b"\x22\xF1\x90"), b"\x7f\x22\x22")
        self.assertEqual(bench.request(b"\x3E\x00"), b"\x7e\x00", "other services still answer")
        bench.ecus[0].config.forced_nrcs = []                   # the window's change takes effect at once
        self.assertEqual(bench.request(b"\x22\xF1\x90")[:3], b"\x62\xf1\x90")

    def test_a_profile_keeps_the_tables_and_refuses_a_broken_one(self):
        path = Path(tempfile.mkdtemp()) / "ecu.json"
        config = quiet(dids=[{"did": 0x0101, "data": "ff", "writable": False}], forced_nrcs=[{"sid": 0x10, "nrc": 0x22}])
        save_profile(path, config, {"interface": "virtual"})
        loaded, _ = load_profile(path)
        self.assertEqual((loaded.dids, loaded.forced_nrcs), (config.dids, config.forced_nrcs))
        with self.assertRaises(ValueError):
            config_from_dict({"dids": [{"did": 0x0101, "data": "zz"}]})


class SharedChannelTest(unittest.TestCase):
    def test_two_ecus_with_their_own_identifiers_share_a_channel(self):
        engine = quiet(request_id=0x7E0, response_id=0x7E8)
        body = quiet(request_id=0x7E1, response_id=0x7E9, dids=[{"did": 0xF190, "data": b"BODY".hex(), "writable": False}])
        bench = Bench(self, engine, body)
        self.assertEqual(bench.request(b"\x22\xF1\x90")[3:], b"WVWZZZ1KZAW000001")
        self.assertEqual(bench.request(b"\x22\xF1\x90", 0x7E1, 0x7E9)[3:], b"BODY")

    def test_a_second_ecu_is_welcome_with_its_own_identifiers_and_no_periodic_frames(self):
        bench = Bench(self, EcuConfig(broadcast_interval=0.02))   # the first ECU sends 0x300/0x301
        self.assertTrue(other_ecu_present(bench.tester, EcuConfig()), "same identifiers: refused")
        self.assertTrue(other_ecu_present(bench.tester, EcuConfig(request_id=0x7E1, response_id=0x7E9)),
                        "its own identifiers, but it would send 0x300/0x301 too")
        self.assertFalse(other_ecu_present(bench.tester, quiet(request_id=0x7E1, response_id=0x7E9)))


class DataTabTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat)
        patcher = patch.object(dummy_ecu_window, "app_settings", lambda: settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = DummyEcuWindow(quiet(), {"interface": "virtual", "channel": "tab-" + str(uuid.uuid4())})
        self.addCleanup(self.window.close)

    def test_the_tables_show_the_ecus_data(self):
        dids = [self.window.did_table.item(row, 0).text() for row in range(self.window.did_table.rowCount())]
        self.assertEqual(dids, ["F187", "F18C", "F190", "F195", "0101", "0102", "F201", "F202", "0200"])
        self.assertEqual(self.window.did_table.item(2, 2).text(), "WVWZZZ1KZAW000001")
        self.assertEqual(self.window.did_table.item(2, 3).checkState(), Qt.Checked)
        self.assertEqual(self.window.dtc_table.item(0, 0).text(), "010100")

    def test_an_edit_applies_to_the_running_ecu_at_once(self):
        self.window.did_table.item(3, 1).setText("41 50 50 2D 32")               # F195 = "APP-2"
        self.assertEqual(self.window.did_table.item(3, 2).text(), "APP-2")
        self.assertEqual(self.window.ecu.dids[0xF195], b"APP-2")
        self.window.dtc_table.item(0, 1).setText("2F")
        self.assertEqual(self.window.ecu.dtcs[P0101], 0x2F)

    def test_a_bad_cell_is_said_and_not_applied(self):
        self.window.did_table.item(3, 1).setText("nonsense")
        self.assertIn("Not applied", self.window.statusBar().currentMessage())
        self.assertEqual(self.window.ecu.dids[0xF195], b"APP-1.0.0")

    def test_rows_are_added_and_removed(self):
        self.window.nrc_buttons.add_button.click()                               # a forced NRC
        self.assertEqual(self.window.ecu.config.forced_nrcs, [{"sid": 0x22, "nrc": 0x22}])
        self.assertIn("conditionsNotCorrect", self.window.nrc_table.item(0, 2).text())
        self.window.nrc_table.setCurrentCell(0, 0)
        self.window._remove_row(self.window.nrc_table)
        self.assertEqual(self.window.ecu.config.forced_nrcs, [])


if __name__ == "__main__":
    unittest.main()
