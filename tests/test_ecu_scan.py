"""The ECU scan: who answers TesterPresent in a range, which sessions they take, and who they are."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import csv
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

import can
from PyQt5.QtWidgets import QApplication

from canexpert.ecu_scan import NORMAL_FIXED, EcuScanDialog, Responder, ScanPlan, find_responders, probe
from canexpert.simulator.ecu import DummyEcu, EcuConfig

APP = QApplication.instance() or QApplication([])


def spin_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.01)
    return False


class Recorder(can.Listener):
    def __init__(self):
        self.frames = []

    def on_message_received(self, message):
        self.frames.append(message)


class Bench:
    def __init__(self, test, *configs):
        self.channel = "scan-" + str(uuid.uuid4())
        self.tester = can.Bus(interface="virtual", channel=self.channel)
        test.addCleanup(self.tester.shutdown)
        self.stop = threading.Event()
        test.addCleanup(self.stop.set)
        self.ecus = []
        for config in configs:
            bus = can.Bus(interface="virtual", channel=self.channel)
            test.addCleanup(bus.shutdown)
            ecu = DummyEcu(bus, config, log=lambda text: None)
            threading.Thread(target=ecu.serve, args=(self.stop,), daemon=True).start()
            self.ecus.append(ecu)


def ecu(request_id, response_id, **changes):
    return EcuConfig(request_id=request_id, response_id=response_id, broadcast_interval=0, **changes)


class SweepTest(unittest.TestCase):
    def test_every_ecu_in_the_range_is_found_at_its_own_identifier(self):
        bench = Bench(self, ecu(0x7E0, 0x7E8), ecu(0x7E3, 0x7EB))
        found = find_responders(bench.tester, ScanPlan(first=0x7E0, last=0x7E7))
        self.assertEqual([(item.request_id, item.response_id) for item in found], [(0x7E0, 0x7E8), (0x7E3, 0x7EB)])

    def test_the_probe_frames_are_padded_as_the_session_would(self):
        bench = Bench(self)
        spy_bus = can.Bus(interface="virtual", channel=bench.channel)
        self.addCleanup(spy_bus.shutdown)
        spy = Recorder()
        notifier = can.Notifier(spy_bus, [spy])
        self.addCleanup(notifier.stop)
        find_responders(bench.tester, ScanPlan(first=0x7E0, last=0x7E1, listen=0.02, padding=0x55))
        time.sleep(.05)
        self.assertEqual([bytes(frame.data) for frame in spy.frames], [b"\x02\x3e\x00" + b"\x55" * 5] * 2)

    def test_29_bit_normal_fixed_addressing(self):
        bench = Bench(self, ecu(0x18DA10F1, 0x18DAF110, extended_ids=True, functional_id=0x18DB33F1))
        plan = ScanPlan(addressing=NORMAL_FIXED, first=0x0F, last=0x11)
        found = find_responders(bench.tester, plan)
        self.assertEqual([(item.request_id, item.response_id, item.extended) for item in found],
                         [(0x18DA10F1, 0x18DAF110, True)])
        self.assertEqual(found[0].row()[:2], ["18DA10F1", "18DAF110"])

    def test_a_range_that_makes_no_sense_is_refused(self):
        with self.assertRaises(ValueError):
            ScanPlan(first=0x7E7, last=0x7E0).check()
        with self.assertRaises(ValueError):
            ScanPlan(addressing=NORMAL_FIXED, first=0, last=0x100).check()


class ProbeTest(unittest.TestCase):
    def test_sessions_and_identification_and_the_ecu_is_left_as_found(self):
        bench = Bench(self, ecu(0x7E0, 0x7E8))
        responder = probe(bench.tester, Responder(0x7E0, 0x7E8), ScanPlan())
        self.assertEqual(responder.sessions, {0x01: "accepted", 0x03: "accepted"}, "no programming unless asked")
        self.assertEqual(responder.identification[0xF190], "WVWZZZ1KZAW000001")
        self.assertEqual(responder.identification[0xF195], "APP-1.0.0")
        self.assertNotIn(0xF18A, responder.identification, "a DID the ECU does not have is left out")
        self.assertEqual(bench.ecus[0].state.session, 0x01)
        self.assertIn("VIN: WVWZZZ1KZAW000001", responder.row()[3])

    def test_a_refused_session_says_why(self):
        bench = Bench(self, ecu(0x7E0, 0x7E8, forced_nrcs=[{"sid": 0x10, "nrc": 0x22}]))
        responder = probe(bench.tester, Responder(0x7E0, 0x7E8), ScanPlan(programming_session=True,
                                                                          read_identification=False))
        self.assertEqual(set(responder.sessions.values()), {"conditionsNotCorrect"})
        self.assertEqual(len(responder.sessions), 3)


class DialogTest(unittest.TestCase):
    def test_a_scan_from_the_dialog(self):
        bench = Bench(self, ecu(0x7E0, 0x7E8), ecu(0x7E1, 0x7E9))
        chosen = []
        closed = []
        dialog = EcuScanDialog(open_bus=lambda: (bench.tester, None, lambda: closed.append(True), 0xCC),
                               new_configuration=chosen.append)
        self.addCleanup(dialog.close)
        dialog.last_edit.setText("7E2")
        scanner = dialog.start()
        self.assertTrue(spin_until(lambda: scanner.isFinished() and dialog.start_btn.isEnabled()))
        APP.processEvents()
        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        self.assertEqual(closed, [True], "the scan closes what it opened")
        self.assertIn("2 ECU(s) found", dialog.status.text())
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(1))
        dialog._use_selected()
        self.assertEqual((chosen[0].request_id, chosen[0].response_id), (0x7E1, 0x7E9))
        path = Path(tempfile.mkdtemp()) / "ecus.csv"
        dialog.export_csv(path)
        with open(path, newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["Request", "Response", "Sessions", "Identification"])
        self.assertEqual(rows[2][:2], ["7E1", "7E9"])

    def test_a_bad_range_is_said_before_anything_is_sent(self):
        opened = []
        dialog = EcuScanDialog(open_bus=lambda: opened.append(True))
        self.addCleanup(dialog.close)
        dialog.first_edit.setText("zz")
        self.assertIsNone(dialog.start())
        self.assertIn("hexadecimal", dialog.status.text())
        self.assertEqual(opened, [])

    def test_an_unusable_channel_is_said(self):
        def refuse():
            raise ValueError("The channel is set to listen-only")

        dialog = EcuScanDialog(open_bus=refuse)
        self.addCleanup(dialog.close)
        self.assertIsNone(dialog.start())
        self.assertIn("listen-only", dialog.status.text())


if __name__ == "__main__":
    unittest.main()
