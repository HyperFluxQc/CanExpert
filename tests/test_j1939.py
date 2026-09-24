"""J1939: identifiers, NAME, DM1/DM2, the transport protocol (BAM, RTS/CTS) watched and taken part in, the
Dummy ECU as a J1939 node, symbol databases matching by PGN, the Trace's J1939 view, the J1939 window, and
j1939 / @on_pgn in scripts and test modules."""
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
from canexpert.j1939 import transport
from canexpert.j1939.dm import J1939Dtc, build_dm, parse_dm
from canexpert.j1939.name import Name
from canexpert.j1939.pgn import (GLOBAL, NULL_ADDRESS, PGN_ADDRESS_CLAIMED, PGN_CI, PGN_DM1, PGN_DM2, PGN_DM11,
                                 PGN_SOFT, PGN_TP_CM, PGN_TP_DT, PGN_VI, describe, make_id, parse_id, pgn_name)
from canexpert.j1939.transport import (ACK, BAM, CTS, END_OF_MESSAGE_ACK, NACK, RTS, J1939Assembler, J1939Error,
                                       J1939Link, cm_frame, packets_of)
from canexpert.paths import DBC_DIR
from canexpert.simulator.ecu import DummyEcu, EcuConfig
from canexpert.symbols import SymbolDatabases
from canexpert.testing.runner import Runner, load_module, uds_names
from canexpert.trace_window import COL_NAME, TraceWindow

APP = QApplication.instance() or QApplication([])
DEMO_DBC = str(DBC_DIR / "j1939_demo.dbc")


def spin_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


def frames_of(pgn, data, source=0x00, destination=GLOBAL, start=10.0, bam=True):
    """(timestamp, can_id, data) of a message sent in a BAM (or the frames of one RTS/CTS session)."""
    packets = packets_of(bytes(data))
    control = BAM if bam else RTS
    frames = [(start, make_id(PGN_TP_CM, source, destination, 7), cm_frame(control, pgn, len(data), len(packets)))]
    if not bam:
        frames.append((start + 0.01, make_id(PGN_TP_CM, destination, source, 7), cm_frame(CTS, pgn, len(packets), 1)))
    for number, packet in enumerate(packets, 1):
        frames.append((start + 0.05 * number, make_id(PGN_TP_DT, source, destination, 7), packet))
    return frames


class IdentifierTest(unittest.TestCase):
    def test_pdu1_and_pdu2(self):
        eec1 = parse_id(0x0CF00400)
        self.assertEqual((eec1.priority, eec1.pgn, eec1.source, eec1.destination), (3, 61444, 0x00, GLOBAL))
        request = parse_id(0x18EA00F9)
        self.assertEqual((request.pgn, request.source, request.destination), (0xEA00, 0xF9, 0x00))
        self.assertEqual(make_id(0xEA00, 0xF9, 0x00, 6), 0x18EA00F9)
        self.assertEqual(make_id(0xFEF1, 0x00), 0x18FEF100)
        self.assertEqual(parse_id(make_id(0x1EF00, 0x21, 0x42, 7)).pgn, 0x1EF00, "the data page is kept")

    def test_names(self):
        self.assertEqual(describe(0x0CF00400), "EEC1 (PGN 61444) 00 → Global")
        self.assertEqual(describe(0x18EA00F9), "Request (PGN 59904) F9 → 00")
        self.assertEqual(pgn_name(0xFF42), "PropB")
        self.assertEqual(pgn_name(0x1234), "")


class NameTest(unittest.TestCase):
    def test_fields_round_trip_and_order(self):
        name = Name(identity=0x1234, manufacturer=0x7FF, function=0, industry_group=1, arbitrary_address_capable=1)
        self.assertEqual(name.to_int(), 0x90000000FFE01234)
        self.assertEqual(Name.from_bytes(name.to_bytes()), name)
        self.assertEqual(name.to_bytes()[0], 0x34, "little-endian on the bus")
        self.assertIn("Engine #0", name.describe())
        with self.assertRaises(ValueError):
            Name(function=256).to_int()


class DiagnosticMessageTest(unittest.TestCase):
    def test_dm1_round_trip(self):
        data = build_dm([J1939Dtc(132, 2, 3), J1939Dtc(0x7F001, 31, 1)], lamps=(1, 0, 1, 0))
        self.assertEqual(data[:2], bytes([0x44, 0xFF]))
        message = parse_dm(data)
        self.assertEqual(message.dtcs, [J1939Dtc(132, 2, 3), J1939Dtc(0x7F001, 31, 1)], "19-bit SPN")
        self.assertEqual(message.lamps_text(), "Malfunction indicator, Amber warning")
        self.assertIn("Mass air flow: Erratic", message.dtcs[0].text())

    def test_no_fault_and_padding(self):
        self.assertEqual(build_dm([]), bytes.fromhex("00ff00000000ffff"))
        self.assertEqual(parse_dm(build_dm([])).dtcs, [])
        self.assertEqual(parse_dm(build_dm([J1939Dtc(100, 1)]) + b"\xff\xff\xff\xff").dtcs, [J1939Dtc(100, 1)])
        with self.assertRaises(ValueError):
            build_dm([J1939Dtc(1 << 19, 0)])


class AssemblerTest(unittest.TestCase):
    def push_all(self, frames, assembler=None):
        assembler = assembler or J1939Assembler()
        found = []
        for timestamp, can_id, data in frames:
            found += assembler.push(timestamp, can_id, data)
        return found, assembler

    def test_a_bam_becomes_one_message(self):
        payload = bytes(range(20))
        (message,), _ = self.push_all(frames_of(PGN_DM1, payload))
        self.assertEqual((message.pgn, message.source, message.destination, message.data, message.transport),
                         (PGN_DM1, 0x00, GLOBAL, payload, "BAM"))
        self.assertEqual([what for _t, what, _d in message.frames], ["TP.CM BAM", "TP.DT 1", "TP.DT 2", "TP.DT 3"])
        self.assertIn("DM1 (PGN 65226) 00 → Global, BAM, 20 bytes", message.summary())

    def test_an_rts_cts_session(self):
        payload = b"WVWZZZ1KZAW000001*"
        frames = frames_of(PGN_VI, payload, 0x00, 0xF9, bam=False)
        frames.append((11.0, make_id(PGN_TP_CM, 0xF9, 0x00, 7), cm_frame(END_OF_MESSAGE_ACK, PGN_VI, 18, 3)))
        found, assembler = self.push_all(frames)
        (message,) = found
        self.assertEqual((message.data, message.transport, message.destination), (payload, "RTS/CTS", 0xF9))
        self.assertIn("TP.CM CTS", [what for _t, what, _d in message.frames])
        self.assertEqual(assembler.sessions, {})

    def test_what_does_not_finish(self):
        frames = frames_of(PGN_DM1, bytes(20))
        del frames[2]                                           # packet 2 lost
        (message,), _ = self.push_all(frames)
        self.assertFalse(message.complete)
        found, assembler = self.push_all(frames_of(PGN_DM1, bytes(20))[:2])
        self.assertEqual(found, [])
        (late,) = assembler.push(20.0, 0x18FEF100, bytes(8))[:1]     # T2 passed: the session ends unfinished
        self.assertFalse(late.complete)
        found, assembler = self.push_all(frames_of(PGN_DM1, bytes(20))[:2])
        self.assertEqual([message.complete for message in assembler.finish()], [False])

    def test_single_frames_and_standard_ones(self):
        assembler = J1939Assembler()
        (message,) = assembler.push(1.0, 0x0CF00400, bytes(8))
        self.assertEqual((message.name, message.transport), ("EEC1", ""))
        self.assertEqual(assembler.push(1.0, 0x300, bytes(8), extended=False), [])


class Bench:
    """The Dummy ECU as a J1939 node on a virtual channel, and a tester's bus."""

    def __init__(self, test, **changes):
        channel = "j1939-" + str(uuid.uuid4())
        self.tester = can.Bus(interface="virtual", channel=channel)
        ecu_bus = can.Bus(interface="virtual", channel=channel)
        changes.setdefault("j1939", True)
        changes.setdefault("dbc_path", DEMO_DBC)
        self.logs = []
        self.ecu = DummyEcu(ecu_bus, EcuConfig(**changes), log=self.logs.append)
        stop = threading.Event()
        threading.Thread(target=self.ecu.serve, args=(stop,), daemon=True).start()
        self.link = J1939Link(self.tester, 0xF9)

        def close():
            stop.set()
            time.sleep(0.05)
            self.tester.shutdown()
            ecu_bus.shutdown()
        test.addCleanup(close)

    def messages(self, seconds):
        assembler, found = J1939Assembler(), []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            message = self.tester.recv(0.02)
            if message is not None and message.is_extended_id:
                found += assembler.push(message.timestamp, message.arbitration_id, bytes(message.data))
        return found


class EcuNodeTest(unittest.TestCase):
    def test_it_claims_broadcasts_from_its_address_and_reports_its_faults(self):
        bench = Bench(self)
        messages = bench.messages(1.3)
        claims = [message for message in messages if message.pgn == PGN_ADDRESS_CLAIMED]
        self.assertEqual(claims[0].source, 0x00)
        self.assertEqual(Name.from_bytes(claims[0].data).to_int(), bench.ecu.config.j1939_name)
        self.assertEqual({message.name for message in messages if message.name in ("EEC1", "CCVS")}, {"EEC1", "CCVS"})
        self.assertEqual({message.source for message in messages}, {0x00}, "never the DBC's placeholder FE")
        dm1 = next(message for message in messages if message.pgn == PGN_DM1)
        faults = parse_dm(dm1.data)
        self.assertEqual([(dtc.spn, dtc.fmi) for dtc in faults.dtcs], [(132, 2)], "P0101 is active by default")
        self.assertEqual(faults.lamps[2], 1, "the amber lamp")

    def test_several_faults_go_in_a_bam(self):
        bench = Bench(self, confirm_cycles=1)
        bench.ecu.set_fault(0xC10000, True)
        dm1 = next(message for message in bench.messages(2.2) if message.pgn == PGN_DM1 and len(message.data) > 8)
        self.assertEqual(dm1.transport, "BAM")
        self.assertEqual({(dtc.spn, dtc.fmi) for dtc in parse_dm(dm1.data).dtcs}, {(132, 2), (639, 9)})

    def test_requests(self):
        bench = Bench(self)
        soft = bench.link.request(PGN_SOFT, 0x00)
        self.assertEqual((soft.data, soft.transport), (b"\x01APP-1.0.0*", "RTS/CTS"))
        vin = bench.link.request(PGN_VI, GLOBAL)
        self.assertEqual((vin.data, vin.transport), (b"WVWZZZ1KZAW000001*", "BAM"))
        self.assertEqual(bench.link.request(PGN_CI, 0x00).data, b"CANEXPERT*DUMMY ECU*SN000123456*1*")
        dm2 = parse_dm(bench.link.request(PGN_DM2, 0x00).data)
        self.assertEqual([(dtc.spn, dtc.fmi) for dtc in dm2.dtcs], [(639, 9)], "U0100 was confirmed, not active")
        eec1 = bench.link.request(0xF004, 0x00)
        self.assertEqual(len(eec1.data), 8, "a PGN it broadcasts: its latest data")
        self.assertEqual(bench.link.request(0x1234, 0x00).acknowledgment, NACK)
        self.assertIsNone(bench.link.request(0x1234, GLOBAL, timeout=0.3), "no NACK to a global request")
        self.assertEqual(bench.link.request(PGN_DM11, 0x00).acknowledgment, ACK)
        self.assertTrue(any("DM11" in line for line in bench.logs))

    def test_a_lower_name_takes_the_address(self):
        bench = Bench(self)
        bench.messages(0.2)
        bench.link.address = 0x00
        bench.link.send(PGN_ADDRESS_CLAIMED, (1).to_bytes(8, "little"))       # NAME 1: lower than any
        claims = [message for message in bench.messages(0.5) if message.pgn == PGN_ADDRESS_CLAIMED]
        self.assertEqual(claims[0].source, NULL_ADDRESS, "Cannot Claim Address")
        self.assertEqual(bench.ecu.j1939.address, NULL_ADDRESS)
        self.assertFalse([message for message in bench.messages(0.4) if message.name == "EEC1"], "then it is quiet")

    def test_a_new_address_is_claimed_while_running(self):
        bench = Bench(self)
        bench.messages(0.2)
        bench.ecu.config.j1939_address = 0x21
        self.assertIn(0x21, {message.source for message in bench.messages(0.4) if message.pgn == PGN_ADDRESS_CLAIMED})
        self.assertEqual(bench.link.request(PGN_SOFT, 0x21).data, b"\x01APP-1.0.0*")

    def test_off_it_says_nothing(self):
        bench = Bench(self, j1939=False, dbc_path="")
        self.assertFalse([message for message in bench.messages(0.4)])
        self.assertIsNone(bench.link.request(PGN_SOFT, 0x00, timeout=0.3))


class LinkTest(unittest.TestCase):
    """The tester's side of an RTS/CTS session and a BAM, with a receiver on the other end."""

    def setUp(self):
        channel = "link-" + str(uuid.uuid4())
        self.sender, self.receiver = (can.Bus(interface="virtual", channel=channel) for _ in range(2))
        self.addCleanup(self.sender.shutdown)
        self.addCleanup(self.receiver.shutdown)

    def test_a_session_in_blocks_of_two(self):
        payload = bytes(range(30))                               # five packets
        got = []

        def receive():
            def cm(control, count=0, first=0):
                data = cm_frame(control, 0xEF00, count, first) if control == CTS else \
                    cm_frame(END_OF_MESSAGE_ACK, 0xEF00, 30, 5)
                self.receiver.send(can.Message(arbitration_id=make_id(PGN_TP_CM, 0x00, 0xF9, 7), data=data,
                                               is_extended_id=True))
            while True:
                message = self.receiver.recv(1)
                if message is None:
                    return
                j, data = parse_id(message.arbitration_id), bytes(message.data)
                if j.pgn == PGN_TP_CM and data[0] == RTS:
                    cm(CTS, 2, 1)
                elif j.pgn == PGN_TP_DT:
                    got.append(data)
                    if len(got) in (2, 4):
                        cm(CTS, 2, len(got) + 1)
                    elif len(got) == 5:
                        cm(END_OF_MESSAGE_ACK)
                        return
        thread = threading.Thread(target=receive)
        thread.start()
        J1939Link(self.sender, 0xF9).send(0xEF00, payload, 0x00)
        thread.join(3)
        self.assertEqual(b"".join(packet[1:] for packet in got)[:30], payload)

    def test_no_cts_is_an_error_and_a_bam_needs_none(self):
        with patch.object(transport, "T3", 0.1):
            with self.assertRaises(J1939Error):
                J1939Link(self.sender, 0xF9).send(0xEF00, bytes(12), 0x00)
        with patch.object(transport, "BAM_INTERVAL", 0):
            J1939Link(self.sender, 0xF9).send(PGN_DM1, bytes(20))
        frames = [self.receiver.recv(0.2) for _ in range(6)]
        assembler = J1939Assembler()
        found = [m for frame in frames if frame is not None and frame.arbitration_id >> 8 & 0xFFFF in (0xECFF, 0xEBFF)
                 for m in assembler.push(frame.timestamp, frame.arbitration_id, bytes(frame.data))]
        self.assertEqual([(m.pgn, m.transport, len(m.data)) for m in found], [(PGN_DM1, "BAM", 20)])


class SymbolsAndTraceTest(unittest.TestCase):
    def test_a_j1939_dbc_matches_any_source_address(self):
        settings = QSettings(str(Path(tempfile.mkdtemp()) / "s.ini"), QSettings.IniFormat)
        symbols = SymbolDatabases([DEMO_DBC], settings=settings)
        for can_id in (0x0CF00400, 0x0CF00421, 0x18F00400):
            self.assertEqual(symbols.name(can_id), "EEC1", hex(can_id))
        self.assertAlmostEqual(symbols.decode(0x0CF00421, bytes([0, 0, 0, 0x80, 0x3E, 0, 0, 0]))["EngineSpeed"], 2000)
        self.assertEqual(symbols.name(0x0CF10400), "", "another PGN")
        dummy = SymbolDatabases([str(DBC_DIR / "dummy_ecu.dbc")], settings=settings)
        self.assertEqual(dummy.name(0x300), "EngineData")

    def test_the_trace_names_and_joins(self):
        trace = TraceWindow()
        self.addCleanup(trace.deleteLater)
        trace.add_frame(1.0, "RX", 0x0CF00400, bytes(8), True)
        for timestamp, can_id, data in frames_of(PGN_DM1, bytes(12), start=2.0):
            trace.add_frame(timestamp, "RX", can_id, data, True)
        trace.flush()
        self.assertEqual(trace.tree.topLevelItem(0).text(COL_NAME), "")
        trace.j1939_btn.setChecked(True)
        self.assertEqual(trace.tree.topLevelItem(0).text(COL_NAME), "EEC1 (PGN 61444) 00 → Global")
        trace.filter_edit.setText("TP.CM")                     # ("EEC1" would be read as the identifier EEC1)
        self.assertEqual(trace.tree.topLevelItemCount(), 1, "filtered by parameter group name")
        trace.filter_edit.setText("")
        trace.transport_btn.setChecked(True)
        self.assertEqual(trace.tree.topLevelItemCount(), 1)
        row = trace.tree.topLevelItem(0)
        self.assertIn("DM1 (PGN 65226) 00 → Global, BAM, 12 bytes", row.text(COL_NAME))
        self.assertEqual(row.childCount(), 3, "TP.CM and two TP.DT")


PANEL = '''<application_database name="J1939"><pages><page name="Main">
<value id="1" label="Faults" binding_value="faults" x="10" y="10"/>
<value id="2" label="Software" binding_value="software" x="10" y="50"/>
</page></pages></application_database>'''
PANEL_SCRIPT = '''
@on_start
def ask(api):
    software = j1939.request(0xFEDA, 0x00)
    api.ui.set_value("software", software.data.decode("latin-1") if software else "no answer")

@on_pgn(0xFECA)
def faults(api, message):
    api.ui.set_value("faults", f"{message.source:02X}: {len(message.data)} bytes")
'''


class MainWindowTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        configs, databases = root / "Configurations", root / "Databases"
        configs.mkdir()
        databases.mkdir()
        (configs / "config_Truck.json").write_text(json.dumps({
            "name": "Truck", "request_id": 0x7E0, "response_id": 0x7E8, "database_family": "panel",
            "tester_present_interval_seconds": 0.5, "node_timeout_seconds": 2}))
        (databases / "panel_2026-09-24.xml").write_text(PANEL)
        (databases / "panel_2026-09-24_script.py").write_text(PANEL_SCRIPT)
        self.settings = QSettings(str(root / "settings.ini"), QSettings.IniFormat)
        self.settings.setValue("last_configuration", "Truck")
        channel = "j1939-main-" + str(uuid.uuid4())
        ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.ecu = DummyEcu(ecu_bus, EcuConfig(j1939=True, dbc_path=DEMO_DBC), log=lambda text: None)
        stop = threading.Event()
        threading.Thread(target=self.ecu.serve, args=(stop,), daemon=True).start()
        patches = [patch.object(main, "CONFIG_DIR", configs), patch.object(main, "DATABASES_DIR", databases),
                   patch.object(main, "app_settings", lambda: self.settings),
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

    def wait_idle(self, window):
        self.assertTrue(spin_until(lambda: not window._busy), window.log.toPlainText())

    def test_the_j1939_window_against_the_dummy_ecu(self):
        self.window.on_connect_clicked()
        self.assertIsNotNone(self.window.worker, self.window.status_label.text())
        window = self.window.open_j1939()
        self.assertEqual(self.window._toolbar_actions["j1939"].shortcut().toString(), "Ctrl+9")
        self.assertEqual(self.window.help_section(window.node_table), "J1939")
        window.request_claims()
        self.assertTrue(spin_until(lambda: 0x00 in window.nodes), window.log.toPlainText())
        self.assertEqual(window.node_table.item(0, 1).text(), "Engine")
        self.assertTrue(spin_until(lambda: window.fault_table.rowCount() >= 1, 3))
        self.assertEqual((window.fault_table.item(0, 2).text(), window.fault_table.item(0, 3).text()), ("132", "2"))
        window.request(PGN_SOFT, 0x00)
        self.wait_idle(window)
        self.assertIn("APP-1.0.0", window.log.toPlainText())
        window.request(0x1234, 0x00)
        self.wait_idle(window)
        self.assertIn("NACK", window.log.toPlainText())
        window.fault_node.setEditText("00")
        window.read_dm2()
        self.wait_idle(window)
        self.assertIn("J1939 network #1", window.log.toPlainText())
        window.clear_active()
        self.wait_idle(window)
        self.assertIn("DM11", window.log.toPlainText())
        window.address_spin.setValue(0x80)
        self.assertEqual(int(self.settings.value("j1939/address")), 0x80)

    def test_a_panel_script_requests_and_hears_pgns(self):
        self.window.on_connect_clicked()
        widgets = self.window.panel.widgets
        self.assertTrue(spin_until(lambda: "APP-1.0.0" in widgets["software"].text()), widgets["software"].text())
        self.assertTrue(spin_until(lambda: widgets["faults"].text().startswith("00: "), 3), widgets["faults"].text())


class TestModuleTest(unittest.TestCase):
    def test_j1939_in_a_test_module(self):
        bench = Bench(self)
        path = Path(tempfile.mkdtemp()) / "truck.py"
        path.write_text("@testcase\ndef vin(t):\n    t.check_equal(j1939.request(0xFEEC, 0x00).data[:17], "
                        "b'WVWZZZ1KZAW000001', 'VIN over J1939')\n")
        module = load_module(path, uds_names())
        self.assertIn("j1939", module.namespace)
        report = Runner(module, j1939=bench.link).run()
        self.assertEqual(report.cases[0].verdict, "passed", report.cases[0].steps)
        offline = Runner(load_module(path, uds_names())).run()
        self.assertIn("connect first", offline.cases[0].error)


if __name__ == "__main__":
    unittest.main()
