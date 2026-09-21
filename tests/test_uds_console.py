"""The UDS console: the service forms, and a real exchange with the simulated ECU."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time
import unittest
import uuid

import can
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from canexpert.can_bus import CanWorker
from canexpert.config import validate_config
from canexpert.simulator.ecu import DummyEcu, EcuConfig
from canexpert.uds.client import FUNCTIONS
from canexpert.uds_console import UdsConsoleWindow, parse_bytes, parse_int, status_text

APP = QApplication.instance() or QApplication([])


def spin_until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


class HelperTest(unittest.TestCase):
    def test_dtc_status_bits(self):
        self.assertEqual(status_text(0x00), "none")
        self.assertEqual(status_text(0x09), "testFailed, confirmedDTC")
        self.assertEqual(status_text(0x80), "warningIndicatorRequested")

    def test_parsing_hexadecimal_input(self):
        self.assertEqual(parse_bytes("22 F1 90"), b"\x22\xf1\x90")
        self.assertEqual(parse_bytes("22F190"), b"\x22\xf1\x90")
        self.assertEqual(parse_bytes(""), b"")
        self.assertEqual(parse_int("F190"), 0xF190)
        self.assertEqual(parse_int("0x22"), 0x22)
        self.assertEqual(parse_int("", 7), 7)


class ServiceFormTest(unittest.TestCase):
    def setUp(self):
        self.console = UdsConsoleWindow()
        self.addCleanup(self.console.close)

    def select(self, name):
        for index in range(self.console.service_tree.topLevelItemCount()):
            group = self.console.service_tree.topLevelItem(index)
            for child in range(group.childCount()):
                if group.child(child).data(0, Qt.UserRole) == name:
                    self.console.service_tree.setCurrentItem(group.child(child))
                    return group.child(child)
        raise AssertionError(f"{name} is not offered")

    def test_every_service_is_offered_under_its_functional_unit(self):
        offered = []
        for index in range(self.console.service_tree.topLevelItemCount()):
            group = self.console.service_tree.topLevelItem(index)
            offered += [group.child(child).data(0, Qt.UserRole) for child in range(group.childCount())]
        self.assertEqual(sorted(offered), sorted(entry.name for entry in FUNCTIONS))

    def test_a_form_is_built_from_the_service_and_its_default_is_filled_in(self):
        self.select("DSC")
        self.assertEqual(set(self.console._widgets), {"session", "suppress"})
        self.assertIn("DiagnosticSessionControl", self.console.doc_label.text())
        self.console._widgets["session"][1].setText("03")
        self.assertEqual(self.console._arguments(), ([0x03], {}))
        self.console._widgets["suppress"][1].setChecked(True)
        self.assertEqual(self.console._arguments(), ([0x03], {"suppress": True}))

    def test_parameters_keep_the_order_the_service_declares(self):
        self.select("RDBI")
        self.console._widgets["did"][1].setText("F190")
        self.console._widgets["more_dids"][1].setText("F195 F187")
        self.assertEqual(self.console._arguments(), ([0xF190, 0xF195, 0xF187], {}))

    def test_a_required_parameter_that_is_empty_is_reported(self):
        self.select("RDBI")
        with self.assertRaises(ValueError) as raised:
            self.console._arguments()
        self.assertIn("did is required", str(raised.exception))
        self.console.send_service()                       # the console says so instead of raising
        self.assertIn("did is required", self.console.log.toPlainText())

    def test_byte_parameters_and_the_security_key(self):
        self.select("WDBI")
        self.console._widgets["did"][1].setText("F190")
        self.console._widgets["data"][1].setText("41 42")
        self.assertEqual(self.console._arguments(), ([0xF190, b"AB"], {}))
        self.select("SecurityUnlock")
        self.console._widgets["level"][1].setText("01")
        self.console._widgets["compute_key"][1].setText("A5")
        args, _ = self.console._arguments()
        self.assertEqual(args[0], 1)
        self.assertEqual(args[1](b"\x01\x02"), bytes([0x01 ^ 0xA5, 0x02 ^ 0xA5]))

    def test_without_a_measurement_nothing_is_sent(self):
        self.select("TP")
        self.assertIsNone(self.console.send_service())
        self.assertIn("No measurement", self.console.log.toPlainText())


class ConsoleAgainstTheEcuTest(unittest.TestCase):
    def setUp(self):
        channel = "console-" + str(uuid.uuid4())
        self.bus = can.Bus(interface="virtual", channel=channel)
        self.ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.ecu = DummyEcu(self.ecu_bus, EcuConfig(broadcast_interval=0), log=lambda text: None)
        self.stop = threading.Event()
        threading.Thread(target=self.ecu.serve, args=(self.stop,), daemon=True).start()
        self.config = validate_config({"name": "Console", "request_id": 0x7E0, "response_id": 0x7E8,
                                       "timeout_ms": 2000})
        self.worker = CanWorker(self.bus, self.config, tester_present=False)
        self.worker.start()
        self.console = UdsConsoleWindow(session=lambda: (self.bus, self.worker, self.config))

    def tearDown(self):
        self.console.close()
        self.worker.stop()
        self.stop.set()
        time.sleep(.05)
        self.bus.shutdown()
        self.ecu_bus.shutdown()

    def run_and_wait(self, call, title):
        thread = self.console.run(call, title)
        self.assertIsNotNone(thread, self.console.log.toPlainText())
        self.assertTrue(spin_until(lambda: not self.console._busy), self.console.log.toPlainText())

    def test_reading_a_data_identifier_shows_the_decoded_answer(self):
        self.run_and_wait(lambda uds: uds.RDBI(0xF190), "RDBI")
        text = self.console.log.toPlainText()
        self.assertIn("WVWZZZ1KZAW000001", text)          # the VIN, over several frames
        self.assertIn("RDBI: 22 F1 90 ->", text)

    def test_a_negative_response_is_named(self):
        self.run_and_wait(lambda uds: uds.RDBI(0x1234), "RDBI")
        self.assertIn("NRC 0x31 requestOutOfRange", self.console.log.toPlainText())

    def test_the_session_and_security_buttons_work_on_the_ecu(self):
        self.console.session_combo.setCurrentIndex(2)     # extended
        self.run_and_wait(lambda uds: uds.DSC(self.console.session_combo.currentData()), "DiagnosticSessionControl")
        self.assertEqual(self.ecu.state.session, 0x03)
        self.console.level_edit.setText("01")
        self.console.mask_edit.setText("A5")
        self.console._unlock()
        self.assertTrue(spin_until(lambda: not self.console._busy))
        self.assertTrue(self.ecu.state.unlocked)

    def test_the_fault_memory_is_read_and_cleared(self):
        self.console.read_dtcs()
        self.assertTrue(spin_until(lambda: self.console.dtc_table.rowCount() == 2))
        rows = {self.console.dtc_table.item(row, 0).text(): self.console.dtc_table.item(row, 2).text()
                for row in range(self.console.dtc_table.rowCount())}
        self.assertEqual(set(rows), {"010100", "C10000"})
        self.assertIn("confirmedDTC", rows["010100"])
        self.console.dtc_table.selectRow(0)
        self.console.clear_dtcs()
        self.assertTrue(spin_until(lambda: not self.console._busy))
        self.console.read_dtcs()
        self.assertTrue(spin_until(lambda: self.console.dtc_table.rowCount() == 0))

    def test_a_raw_request_is_sent_as_typed(self):
        self.console.raw_edit.setText("22 F1 95")
        self.console.send_raw()
        self.assertTrue(spin_until(lambda: not self.console._busy))
        self.assertIn("APP-1.0.0", self.console.log.toPlainText())


if __name__ == "__main__":
    unittest.main()
