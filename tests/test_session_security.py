"""The session and security state: P2 timing learnt from the ECU, and seed & key."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time
import unittest
import uuid

import can
from PyQt5.QtWidgets import QApplication

from canexpert.can_bus import CanWorker
from canexpert.config import validate_config
from canexpert.simulator.ecu import DummyEcu, EcuConfig
from canexpert.uds.client import P2_MARGIN, UdsFunctions
from canexpert.uds.seed_key import KEY_BUFFER, SeedKeyError, dll_key, generate_key, xor_key
from canexpert.uds_console import UdsConsoleWindow

APP = QApplication.instance() or QApplication([])


def spin_until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


class Transport:
    """Stands in for the bus: records what it was asked and answers from a script."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, payload, timeout=None, wait=True, pending=None):
        self.calls.append({"payload": bytes(payload), "timeout": timeout, "pending": pending})
        return self.replies.pop(0) if self.replies else None


class P2Test(unittest.TestCase):
    """An ECU says how long it may take; CAN Expert honours it without ever becoming less patient."""

    def session_answer(self, p2_ms, p2_star_ms):
        return bytes([0x50, 0x03]) + p2_ms.to_bytes(2, "big") + (p2_star_ms // 10).to_bytes(2, "big")

    def test_the_announced_timing_is_picked_up_and_used(self):
        transport = Transport([self.session_answer(120, 4000), b"\x62\xf1\x90"])
        uds = UdsFunctions(transport, timeout=2.0)
        self.assertIsNone(uds.p2)
        uds.DSC(0x03)
        self.assertAlmostEqual(uds.p2, 0.120, places=6)
        self.assertAlmostEqual(uds.p2_star, 4.0, places=6)
        uds.RDBI(0xF190)
        # 2 s is what the configuration allows, and the ECU asked for less: the longer one wins.
        self.assertAlmostEqual(transport.calls[1]["timeout"], 2.0, places=6)
        self.assertAlmostEqual(transport.calls[1]["pending"], 4.0, places=6)

    def test_an_ecu_that_needs_longer_than_the_configuration_gets_it(self):
        transport = Transport([self.session_answer(3000, 10000), b"\x62\xf1\x90"])
        uds = UdsFunctions(transport, timeout=2.0)
        uds.DSC(0x03)
        uds.RDBI(0xF190)
        self.assertAlmostEqual(transport.calls[1]["timeout"], 3.0 + P2_MARGIN, places=6)

    def test_a_timeout_the_caller_gives_is_left_alone(self):
        transport = Transport([self.session_answer(120, 4000), b"\x62\xf1\x90"])
        uds = UdsFunctions(transport, timeout=2.0)
        uds.DSC(0x03)
        uds.RDBI(0xF190, timeout=0.5)
        self.assertAlmostEqual(transport.calls[1]["timeout"], 0.5, places=6)

    def test_an_answer_that_makes_no_sense_is_not_believed(self):
        for answer in (bytes([0x50, 0x03]),                        # no timing at all
                       self.session_answer(0, 5000),               # P2 of nothing
                       self.session_answer(1000, 10),              # P2* shorter than P2
                       bytes([0x50, 0x03, 0xFF, 0xFF, 0xFF, 0xFF])):   # P2 of 65 s
            uds = UdsFunctions(Transport([answer]), timeout=2.0)
            uds.DSC(0x03)
            self.assertIsNone(uds.p2, answer.hex(" "))

    def test_a_transport_that_takes_three_arguments_still_works(self):
        # Panel scripts and older callers pass (payload, timeout, wait) and nothing else.
        calls = []

        def three(payload, timeout=None, wait=True):
            calls.append((bytes(payload), timeout))
            return bytes([0x50, 0x03, 0x00, 0x32, 0x01, 0xF4])

        uds = UdsFunctions(three, timeout=1.0)
        uds.DSC(0x03)
        uds.TP()
        self.assertEqual(len(calls), 2)
        self.assertAlmostEqual(uds.p2_star, 5.0, places=6)


class SeedKeyTest(unittest.TestCase):
    def test_the_mask(self):
        self.assertEqual(xor_key(0xA5)(b"\x01\x02"), bytes([0x01 ^ 0xA5, 0x02 ^ 0xA5]))

    def test_a_dll_is_called_the_way_those_dlls_are_written(self):
        """GenerateKeyEx(seed, size, level, variant, key, key size, out size) -> 0 on success."""
        seen = {}

        class Stub:
            def GenerateKeyEx(self, seed, seed_size, level, variant, key, key_size, out_size):
                seen.update(seed=bytes(seed[:seed_size]), level=level, variant=variant, room=key_size)
                answer = bytes(byte + 1 for byte in bytes(seed[:seed_size]))
                for index, byte in enumerate(answer):
                    key[index] = byte
                out_size._obj.value = len(answer)
                return 0

        stub = Stub()
        key = dll_key("ignored.dll", level=3, variant="Body", library=stub)(b"\x10\x20")
        self.assertEqual(key, b"\x11\x21")
        self.assertEqual((seen["seed"], seen["level"], seen["variant"]), (b"\x10\x20", 3, b"Body"))
        self.assertEqual(seen["room"], KEY_BUFFER)

    def test_a_dll_that_refuses_says_why(self):
        class Refusing:
            def GenerateKeyEx(self, *args):
                return 2                                   # security level not supported

        with self.assertRaises(SeedKeyError) as raised:
            generate_key(Refusing(), b"\x01", level=9)
        self.assertIn("security level not supported", str(raised.exception))

    def test_a_dll_returning_nothing_is_an_error_not_an_empty_key(self):
        class Empty:
            def GenerateKeyEx(self, seed, seed_size, level, variant, key, key_size, out_size):
                out_size._obj.value = 0
                return 0

        with self.assertRaises(SeedKeyError):
            generate_key(Empty(), b"\x01", level=1)

    def test_a_missing_file_is_reported_before_anything_is_sent(self):
        with self.assertRaises(SeedKeyError) as raised:
            dll_key("no-such-file.dll", level=1)
        self.assertIn("does not exist", str(raised.exception))


class ConsoleStateTest(unittest.TestCase):
    """The console follows the session and the security state against the simulated ECU."""

    def setUp(self):
        channel = "session-" + str(uuid.uuid4())
        self.bus = can.Bus(interface="virtual", channel=channel)
        self.ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.ecu = DummyEcu(self.ecu_bus, EcuConfig(broadcast_interval=0, p2_ms=75, p2_star_ms=4000),
                            log=lambda text: None)
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

    def wait(self):
        self.assertTrue(spin_until(lambda: not self.console._busy), self.console.log.toPlainText())

    def test_the_strip_follows_the_session_the_timing_and_the_lock(self):
        self.assertIn("Session: unknown", self.console.state_label.text())
        self.assertIn("Security: locked", self.console.state_label.text())

        self.console.session_combo.setCurrentIndex(2)              # extended
        self.console.run(lambda uds: uds.DSC(0x03), "DiagnosticSessionControl")
        self.wait()
        self.assertIn("Session: extended", self.console.state_label.text())
        self.assertIn("P2 75 ms", self.console.state_label.text())  # what the ECU announced
        self.assertIn("P2* 4000 ms", self.console.state_label.text())
        self.assertAlmostEqual(self.console.p2, 0.075, places=6)

        self.console.level_edit.setText("01")
        self.console.mask_edit.setText("A5")
        self.console._unlock()
        self.wait()
        self.assertTrue(self.ecu.state.unlocked)
        self.assertIn("Security: unlocked (level 1)", self.console.state_label.text())

        self.console.session_combo.setCurrentIndex(0)              # back to the default session
        self.console.run(lambda uds: uds.DSC(0x01), "DiagnosticSessionControl")
        self.wait()
        self.assertIn("Session: default", self.console.state_label.text())
        self.assertIn("Security: locked", self.console.state_label.text())

    def test_the_timing_the_ecu_asked_for_is_used_by_the_next_request(self):
        self.console.run(lambda uds: uds.DSC(0x03), "DiagnosticSessionControl")
        self.wait()
        self.assertIsNotNone(self.console.p2)
        # A request after that still succeeds, with the learnt timing carried into the next exchange.
        self.console.run(lambda uds: (self.assertAlmostEqual(uds.p2, 0.075, places=6), uds.RDBI(0xF190))[1],
                         "RDBI")
        self.wait()
        self.assertIn("WVWZZZ1KZAW000001", self.console.log.toPlainText())

    def test_the_key_source_switches_between_the_mask_and_a_dll(self):
        self.console.key_source.setCurrentIndex(0)
        self.assertTrue(self.console.mask_edit.isVisibleTo(self.console))
        self.assertFalse(self.console.dll_edit.isVisibleTo(self.console))
        self.console.mask_edit.setText("A5")
        self.assertEqual(self.console.compute_key()(b"\x01"), bytes([0x01 ^ 0xA5]))

        self.console.key_source.setCurrentIndex(1)
        self.assertTrue(self.console.dll_edit.isVisibleTo(self.console))
        self.assertFalse(self.console.mask_edit.isVisibleTo(self.console))
        with self.assertRaises(SeedKeyError):
            self.console.compute_key()                              # no DLL chosen yet
        self.console._unlock()                                      # says so instead of sending anything
        self.assertIn("No seed & key DLL chosen", self.console.log.toPlainText())
        self.assertFalse(self.ecu.state.unlocked)


if __name__ == "__main__":
    unittest.main()
