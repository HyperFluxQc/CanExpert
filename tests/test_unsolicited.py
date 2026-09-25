"""Diagnostic responses nobody asked for: periodic data (0x2A) and ResponseOnEvent (0x86) answers, reassembled
by the CAN worker with flow control, listed by the UDS Console and passed to the script's @on_periodic_data and
@on_response_event; and the busyRepeatRequest (NRC 0x21) a request repeats."""
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
from PyQt5.QtWidgets import QApplication

from canexpert import can_bus
from canexpert import main_window as main
from canexpert.can_bus import CanWorker, ReceiveMailbox, UnsolicitedAssembler, is_tester_present_answer
from canexpert.config import uds_transport, validate_config
from canexpert.panel.runtime import ScriptRuntime
from canexpert.simulator.ecu import DummyEcu, EcuConfig
from canexpert.uds import client
from canexpert.uds.client import make_request, uds_request, unsolicited_kind
from canexpert.uds_console import UdsConsoleWindow

APP = QApplication.instance() or QApplication([])
VIN = b"WVWZZZ1KZAW000001"
NEW_VIN = b"WVWZZZ1KZAW000002"


def spin_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


def frame(*data, can_id=0x7E8):
    return can.Message(arbitration_id=can_id, data=bytes(data), is_extended_id=False)


class ScriptedBus:
    """Answers each request with the next scripted replies (single frames), and keeps what uds_request()
    hands over as unsolicited."""

    def __init__(self, *answers):
        self.answers, self.queue, self.sent, self.handed_over = list(answers), [], [], []
        self.unsolicited = self.handed_over.append

    def send(self, message):
        self.sent.append(bytes(message.data))
        for payload in (self.answers.pop(0) if self.answers else []):
            self.queue.append(frame(len(payload), *payload))

    def recv(self, timeout=0):
        if self.queue:
            return self.queue.pop(0)
        time.sleep(min(timeout or 0, 0.01))
        return None


class AssemblerTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.transport = {"request_id": 0x7E0, "response_id": 0x7E8, "extended": False, "address_byte": None,
                          "padding": 0xCC, "block_size": 0, "st_min": 0}

    def assembler(self, **changes):
        return UnsolicitedAssembler(dict(self.transport, **changes), self.sent.append)

    def test_a_single_frame_is_a_message(self):
        assembler = self.assembler()
        self.assertEqual(assembler.push(frame(0x04, 0x6A, 0x01, 0x00, 0xD7, 0xAA, 0xAA, 0xAA)), b"\x6a\x01\x00\xd7")
        self.assertIsNone(assembler.push(frame(0x04, 0x6A, 0x01, 0x00, 0xD7, can_id=0x7E9)), "another ID")
        self.assertIsNone(assembler.push(frame(0x00, 0x6A)), "no length")
        self.assertEqual(self.sent, [])

    def test_a_first_frame_gets_flow_control_and_the_consecutive_frames_finish_it(self):
        assembler = self.assembler()
        payload = b"\x62\xf1\x90" + VIN
        self.assertIsNone(assembler.push(frame(0x10, len(payload), *payload[:6])))
        (flow_control,) = self.sent
        self.assertEqual((flow_control.arbitration_id, bytes(flow_control.data)), (0x7E0, b"\x30\x00\x00" + b"\xcc" * 5))
        self.assertTrue(assembler.busy)
        self.assertIsNone(assembler.push(frame(0x21, *payload[6:13])))
        self.assertEqual(assembler.push(frame(0x22, *payload[13:20])), payload)
        self.assertFalse(assembler.busy)

    def test_block_size_asks_again_after_each_block(self):
        assembler = self.assembler(block_size=1, st_min=5, padding=None, address_byte=0x55)
        payload = bytes(range(20))
        assembler.push(frame(0x55, 0x10, 20, *payload[:5]))
        assembler.push(frame(0x55, 0x21, *payload[5:11]))
        assembler.push(frame(0x55, 0x22, *payload[11:17]))
        self.assertEqual(assembler.push(frame(0x55, 0x23, *payload[17:])), payload)
        self.assertEqual([bytes(m.data) for m in self.sent], [b"\x55\x30\x01\x05"] * 3,
                         "the address byte first, one flow control per block of one frame, none after the last")

    def test_a_frame_out_of_sequence_or_late_drops_the_message(self):
        assembler = self.assembler()
        payload = b"\x62\xf1\x90" + VIN
        assembler.push(frame(0x10, 20, *payload[:6]))
        self.assertIsNone(assembler.push(frame(0x22, *payload[13:20])), "0x21 is missing")
        self.assertIsNone(assembler.push(frame(0x21, *payload[6:13])), "nothing in progress any more")
        assembler.push(frame(0x10, 20, *payload[:6]))
        assembler.message["deadline"] = time.monotonic() - 1                 # N_Cr ran out
        self.assertIsNone(assembler.push(frame(0x21, *payload[6:13])))
        self.assertIsNone(assembler.message)

    def test_what_is_what(self):
        self.assertTrue(is_tester_present_answer(b"\x7e\x00"))
        self.assertTrue(is_tester_present_answer(b"\x7f\x3e\x21"))
        self.assertFalse(is_tester_present_answer(b"\x7f\x22\x31"))
        self.assertEqual(unsolicited_kind(b"\x6a\x01\x00\xd7"), ("periodic", 0xF201, b"\x00\xd7"))
        self.assertEqual(unsolicited_kind(b"\x62\xf1\x90AB"), ("event", 0x22, b"\x62\xf1\x90AB"))
        self.assertEqual(unsolicited_kind(b"\x7f\x19\x31"), ("event", 0x19, b"\x7f\x19\x31"))


class BusyAndUnrelatedRepliesTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(client, "BUSY_RETRY_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_busy_repeat_request_sends_the_request_again(self):
        bus = ScriptedBus([b"\x7f\x22\x21"], [b"\x7f\x22\x21"], [b"\x62\xf1\x90\x01"])
        self.assertEqual(uds_request(bus, b"\x22\xf1\x90", 0x7E0, 0x7E8, 0.5), b"\x62\xf1\x90\x01")
        self.assertEqual(len(bus.sent), 3)

    def test_an_ecu_that_stays_busy_is_believed_in_the_end(self):
        bus = ScriptedBus(*[[b"\x7f\x22\x21"]] * 6)
        self.assertEqual(uds_request(bus, b"\x22\xf1\x90", 0x7E0, 0x7E8, 0.5), b"\x7f\x22\x21")
        self.assertEqual(len(bus.sent), 1 + client.BUSY_RETRIES)

    def test_replies_that_are_not_the_answer_are_handed_over(self):
        bus = ScriptedBus([b"\x6a\x01\x00\xd7", b"\x62\xf1\x90\x01"])
        self.assertEqual(uds_request(bus, b"\x22\xf1\x90", 0x7E0, 0x7E8, 0.5), b"\x62\xf1\x90\x01")
        self.assertEqual(bus.handed_over, [b"\x6a\x01\x00\xd7"])
        bus = ScriptedBus([b"\x6a\x02\x00\x64", b"\x6a"])
        self.assertEqual(uds_request(bus, b"\x2a\x03\x01", 0x7E0, 0x7E8, 0.5), b"\x6a", "the answer to 0x2A")
        self.assertEqual(bus.handed_over, [b"\x6a\x02\x00\x64"])


class EcuBench:
    """The dummy ECU on a virtual channel, a CAN worker taking unsolicited responses, and a mailbox."""

    def __init__(self, test, **ecu_changes):
        channel = "unsolicited-" + str(uuid.uuid4())
        self.bus = can.Bus(interface="virtual", channel=channel)
        ecu_bus = can.Bus(interface="virtual", channel=channel)
        ecu_changes.setdefault("broadcast_interval", 0)
        ecu_changes.setdefault("periodic_rates_ms", (400, 100, 20))
        self.ecu = DummyEcu(ecu_bus, EcuConfig(**ecu_changes), log=lambda text: None)
        stop = threading.Event()
        threading.Thread(target=self.ecu.serve, args=(stop,), daemon=True).start()
        self.config = validate_config({"name": "Unsolicited", "request_id": 0x7E0, "response_id": 0x7E8,
                                       "tester_present_interval_seconds": 0.05, "node_timeout_seconds": 1})
        self.worker = CanWorker(self.bus, self.config, tester_present=True, unsolicited=True)
        self.seen, self.sent = [], []
        self.worker.unsolicited.connect(lambda _time, payload: self.seen.append(bytes(payload)))
        self.worker.message_sent.connect(lambda _stamp, can_id, data, _extended: self.sent.append((can_id, bytes(data))))
        self.worker.start()
        self.mailbox = ReceiveMailbox(self.bus, self.worker.message_sent.emit)
        self.worker.add_mailbox(self.mailbox)
        self.request = make_request(self.mailbox, uds_transport(self.config))

        def close():
            self.worker.stop()
            stop.set()
            time.sleep(.05)
            self.bus.shutdown()
            ecu_bus.shutdown()
        test.addCleanup(close)


class WorkerTest(unittest.TestCase):
    def test_periodic_data_is_reported_and_tester_present_answers_are_not(self):
        bench = EcuBench(self)
        self.assertIs(bench.mailbox.unsolicited.__func__, CanWorker.report)
        self.assertEqual(bench.request(b"\x2a\x03\x01"), b"\x6a")
        self.assertTrue(spin_until(lambda: bench.seen.count(b"\x6a\x01\x00\xd7") >= 3), bench.seen)
        self.assertEqual(bench.request(b"\x2a\x04\x01"), b"\x6a")
        spin_until(lambda: False, 0.1)
        count = len(bench.seen)
        spin_until(lambda: False, 0.2)
        self.assertEqual(len(bench.seen), count, "stopped")
        self.assertTrue(any(data[1:3] == b"\x3e\x00" for _id, data in bench.sent), "TesterPresent was sent")
        self.assertFalse([payload for payload in bench.seen if payload[:1] in (b"\x7e", b"\x7f")])

    def test_a_multi_frame_event_response_gets_flow_control(self):
        bench = EcuBench(self)
        self.assertEqual(bench.request(b"\x86\x03\x02\xf1\x90\x22\xf1\x90"), bytes.fromhex("c6030002 f190 22f190"))
        self.assertEqual(bench.request(b"\x86\x05\x02"), bytes.fromhex("c6050002"))
        bench.sent.clear()
        bench.ecu.dids[0xF190] = NEW_VIN
        self.assertTrue(spin_until(lambda: b"\x62\xf1\x90" + NEW_VIN in bench.seen), bench.seen)
        self.assertIn(0x30, [data[0] for can_id, data in bench.sent if can_id == 0x7E0], "the worker's flow control")

    def test_a_worker_without_it_reports_nothing(self):
        worker = CanWorker(None, validate_config({"name": "t", "request_id": 0x7E0}))
        seen = []
        worker.unsolicited.connect(lambda _time, payload: seen.append(payload))
        worker.report(b"\x6a\x01\x00\xd7")
        APP.processEvents()
        self.assertIsNone(worker.assembler)
        self.assertEqual(seen, [])


class ConsoleTest(unittest.TestCase):
    def setUp(self):
        self.bench = EcuBench(self)
        self.console = UdsConsoleWindow(session=lambda: (self.bench.bus, self.bench.worker, self.bench.config))
        self.addCleanup(self.console.close)
        self.bench.worker.unsolicited.connect(self.console.on_unsolicited)

    def rows(self):
        table = self.console.unsolicited_table
        return {table.item(row, 1).text(): (table.item(row, 0).text(), int(table.item(row, 2).text()),
                                            table.item(row, 3).text())
                for row in range(table.rowCount())}

    def wait_idle(self):
        self.assertTrue(spin_until(lambda: not self.console._busy), self.console.log.toPlainText())

    def test_periodic_data_is_counted_until_stopped(self):
        self.console.periodic_edit.setText("F201")
        self.console.rate_combo.setCurrentIndex(2)                       # fast
        self.console.start_periodic()
        self.assertTrue(spin_until(lambda: self.rows().get("0xF201", ("", 0))[1] >= 3), self.rows())
        kind, _count, data = self.rows()["0xF201"]
        self.assertEqual((kind, data), ("Periodic", "00 d7   (215)"))
        self.console.stop_periodic()
        self.wait_idle()
        self.assertEqual(self.bench.ecu.state.periodic, {})
        self.console.clear_unsolicited()
        self.assertEqual(self.console.unsolicited_table.rowCount(), 0)

    def test_an_event_is_set_up_started_and_shown(self):
        self.console.event_did_edit.setText("F190")
        self.console.set_up_did_event()
        self.wait_idle()
        self.console.response_on_event(0x05)
        self.wait_idle()
        self.assertTrue(self.bench.ecu.state.events_active)
        self.bench.ecu.dids[0xF190] = NEW_VIN
        self.assertTrue(spin_until(lambda: "ReadDataByIdentifier" in self.rows()), self.rows())
        self.assertEqual(self.rows()["ReadDataByIdentifier"][0], "Event")
        self.assertIn("Event response: 62 f1 90", self.console.log.toPlainText())
        self.console.response_on_event(0x04)
        self.wait_idle()
        self.assertRegex(self.console.log.toPlainText(), r"ResponseOnEvent report: 86 04 -> (?i:c6 04 01)")
        self.console.response_on_event(0x06)
        self.wait_idle()
        self.assertEqual(self.bench.ecu.state.events, [])

    def test_an_event_on_a_dtc_status_change(self):
        self.bench.ecu.dtc_memory.confirm_cycles = 1
        self.bench.ecu.set_fault(0xC10000, True)                    # U0100, confirmed before the event is set up
        self.console.event_mask_edit.setText("08")
        self.console.set_up_dtc_event()
        self.wait_idle()
        self.console.response_on_event(0x05)
        self.wait_idle()
        self.console.clear_dtcs()                                   # confirmed again at once: an event
        self.assertTrue(spin_until(lambda: "ReadDTCInformation" in self.rows()), self.rows())
        self.assertEqual(self.rows()["ReadDTCInformation"][2], "59 02 ff c1 00 00 af", "19 02 08: U0100 confirmed")

    def test_identifiers_are_checked(self):
        self.console.periodic_edit.setText("1234")
        self.assertIsNone(self.console.start_periodic())
        self.console.periodic_edit.setText("")
        self.assertIsNone(self.console.start_periodic())
        self.assertIn("Invalid identifier", self.console.log.toPlainText())
        self.assertIn("at least one periodic identifier", self.console.log.toPlainText())


SCRIPT = '''
seen = {"periodic": [], "events": []}

@on_periodic_data(0xF201)
def temperature(api, data, identifier):
    seen["periodic"].append((identifier, data))

@on_periodic_data
def anything(data):
    seen["periodic"].append(("any", data))

@on_response_event(0x22)
def changed(api, response):
    seen["events"].append(response)

@on_response_event(0x19)
def dtcs(response):
    seen["events"].append(("dtc", response))
'''


class ScriptTest(unittest.TestCase):
    def test_the_decorators_get_what_the_ecu_sends(self):
        runtime = ScriptRuntime(ReceiveMailbox(None), validate_config({"name": "t"}), {}, None)
        messages = []
        runtime.message.connect(lambda level, text: messages.append(text))
        path = Path(tempfile.mkdtemp()) / "script.py"
        path.write_text(SCRIPT)
        runtime.start(path)
        self.addCleanup(runtime.stop)
        self.assertTrue(spin_until(lambda: "seen" in runtime.namespace), messages)
        runtime.post("unsolicited", None, b"\x6a\x01\x00\xd7")
        runtime.post("unsolicited", None, b"\x6a\x02\x00\x64")
        runtime.post("unsolicited", None, b"\x62\xf1\x90" + NEW_VIN)
        seen = runtime.namespace["seen"]
        self.assertTrue(spin_until(lambda: len(seen["events"]) == 1 and len(seen["periodic"]) == 3), (seen, messages))
        self.assertEqual(seen["periodic"], [(0xF201, b"\x00\xd7"), ("any", b"\x00\xd7"), ("any", b"\x00\x64")])
        self.assertEqual(seen["events"], [b"\x62\xf1\x90" + NEW_VIN])


PANEL = '''<application_database name="Unsolicited"><pages><page name="Main">
<value id="1" label="Temperature" binding_value="temperature" x="10" y="10"/>
<value id="2" label="VIN" binding_value="vin" x="10" y="50"/>
</page></pages></application_database>'''
PANEL_SCRIPT = '''
@on_periodic_data(0xF201)
def temperature(api, data):
    api.ui.set_value("temperature", int.from_bytes(data, "big") / 10)

@on_response_event(0x22)
def vin(api, response):
    api.ui.set_value("vin", response[3:].decode())
'''


class MainWindowTest(unittest.TestCase):
    """A measurement against the dummy ECU: the console and the panel script both hear the ECU."""

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
        (databases / "panel_2026-09-24_script.py").write_text(PANEL_SCRIPT)
        settings = QSettings(str(root / "settings.ini"), QSettings.IniFormat)
        settings.setValue("last_configuration", "Bench")
        channel = "unsolicited-main-" + str(uuid.uuid4())
        ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.ecu = DummyEcu(ecu_bus, EcuConfig(broadcast_interval=0, periodic_rates_ms=(400, 100, 20)),
                            log=lambda text: None)
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

    def test_the_console_and_the_script_hear_periodic_data_and_events(self):
        self.window.on_connect_clicked()
        self.assertIsNotNone(self.window.worker, self.window.status_label.text())
        console = self.window.open_uds_console()
        console.periodic_edit.setText("F201")
        console.start_periodic()
        widgets = self.window.panel.widgets
        self.assertTrue(spin_until(lambda: widgets["temperature"].text() == "21.5"), widgets["temperature"].text())
        self.assertTrue(spin_until(lambda: console.unsolicited_table.rowCount() == 1))
        console.stop_periodic()
        self.assertTrue(spin_until(lambda: not console._busy))
        console.set_up_did_event()
        self.assertTrue(spin_until(lambda: not console._busy))
        console.response_on_event(0x05)
        self.assertTrue(spin_until(lambda: not console._busy))
        self.ecu.dids[0xF190] = NEW_VIN
        self.assertTrue(spin_until(lambda: widgets["vin"].text() == NEW_VIN.decode()), widgets["vin"].text())
        self.assertIn("Event response: 62 f1 90", console.log.toPlainText())


if __name__ == "__main__":
    unittest.main()
