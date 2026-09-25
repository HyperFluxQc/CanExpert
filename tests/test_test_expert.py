"""TestExpert: descriptions (JSON, how states become sessions and levels, CDD, ODX), the tests generated from
them against the Dummy ECU - passing when it keeps the rules, failing where it is made not to - the pre-test and
post-test sequences around them, and the window."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import threading
import time
import unittest
import uuid
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import can
from PyQt5.QtWidgets import QApplication

from canexpert.paths import ODX_DIR
from canexpert.simulator.ecu import DummyEcu, EcuConfig
from canexpert.test_expert import odx as odx_loader
from canexpert.test_expert import window as window_module
from canexpert.test_expert.cdd import CddError, load_cdd
from canexpert.test_expert.description import (Access, EcuDescription, RawService, RawState, build_description)
from canexpert.test_expert.dummy import dummy_description
from canexpert.test_expert.generator import Options, Suite
from canexpert.test_expert.sequences import (Attachment, Sequence, SequenceError, SequenceStep, due, parse_expect,
                                             parse_frame, parse_hex, parse_script)
from canexpert.test_expert.tester import Tester
from canexpert.testing.runner import Runner
from canexpert.testing.window import MemorySettings
from canexpert.uds.seed_key import xor_key

APP = QApplication.instance() or QApplication([])
DUMMY_CDD = ODX_DIR / "dummy_ecu.cdd"
TRANSPORT = {"request_id": 0x7E0, "response_id": 0x7E8, "timeout": 1.0, "extended": False, "address_byte": None,
             "padding": 0xCC, "block_size": 0, "st_min": 0}


def key(level, seed):
    return xor_key(0xA5)(seed)


def spin_until(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.01)
    return False


class DescriptionTest(unittest.TestCase):
    STATES = {1: RawState("session", "Default"), 2: RawState("session", "Programming"), 3: RawState("session", "Extended"),
              4: RawState("security", "Locked"), 5: RawState("security", "Unlocked")}

    def test_states_become_sessions_and_levels(self):
        raw = [RawService(b"\x10\x01", "Default", [1, 2, 3, 4, 5], [(1, 1), (2, 1), (3, 1)]),
               RawService(b"\x10\x03", "Extended", [1, 2, 3, 4, 5], [(1, 3), (2, 3), (3, 3)]),
               RawService(b"\x10\x02", "Programming", [1, 2, 3, 4, 5], [(3, 2), (2, 2)]),
               RawService(b"\x27\x01", "Level 1", [3, 4, 5]),
               RawService(b"\x27\x02", "Level 1", [3, 4, 5], [(4, 5)]),
               RawService(b"\x22\xf1\x90", "VIN", None, length=17),
               RawService(b"\x2e\xf1\x90", "VIN", [3, 5]),
               RawService(b"\x31\x01\xff\x00", "Erase", [2, 5])]
        d = build_description(raw, self.STATES, "ECU")
        self.assertEqual({s.id: s.name for s in d.sessions.values()}, {1: "Default", 3: "Extended", 2: "Programming"})
        self.assertEqual(d.sessions[2].entered_from, {3}, "programming only from extended")
        self.assertEqual(d.sessions[3].entered_from, set(), "extended from anywhere")
        self.assertEqual(d.security_levels, {1: "Level 1"})
        self.assertEqual(d.services[0x27].access, Access({3}, set()), "locked or not: no level needed")
        self.assertEqual(d.dids[0xF190].read, Access())
        self.assertEqual(d.dids[0xF190].length, 17)
        self.assertEqual(d.dids[0xF190].write, Access({3}, {1}), "only the unlocked state: level 1")
        self.assertEqual(d.routines[0xFF00].sub_functions[1], Access({2}, {1}))
        self.assertEqual(d.services[0x2E].access, Access({3}, {1}))

    def test_json_round_trip_and_the_default_session(self):
        d = dummy_description()
        again = EcuDescription.from_dict(d.to_dict())
        self.assertEqual(again.to_dict(), d.to_dict())
        path = Path(tempfile.mkdtemp()) / "ecu.json"
        d.save(path)
        self.assertEqual(EcuDescription.load(path).dids.keys(), d.dids.keys())
        only_dids = build_description([RawService(b"\x22\xf1\x90")], {}, "ECU")
        self.assertIn(1, only_dids.sessions)
        self.assertTrue(only_dids.warnings)


class CddTest(unittest.TestCase):
    def test_the_dummy_ecus_cdd_describes_the_dummy_ecu(self):
        cdd, dummy = load_cdd(DUMMY_CDD), dummy_description()
        self.assertEqual(cdd.warnings, [])
        self.assertEqual({s: (x.access, x.sub_functions) for s, x in cdd.services.items()},
                         {s: (x.access, x.sub_functions) for s, x in dummy.services.items()})
        self.assertEqual({d: (x.length, x.read, x.write) for d, x in cdd.dids.items()},
                         {d: (x.length, x.read, x.write) for d, x in dummy.dids.items()})
        self.assertEqual(cdd.routines, dummy.routines)
        self.assertEqual({s: x.entered_from for s, x in cdd.sessions.items()},
                         {s: x.entered_from for s, x in dummy.sessions.items()})
        self.assertEqual(cdd.sessions[3].name, "Extended session", "the displayed name")

    def test_the_file_is_written_by_the_tool(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("make_dummy_cdd", Path(__file__).resolve().parents[1] / "tools" /
                                                      "make_dummy_cdd.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        written = module.Writer(dummy_description(EcuConfig())).write()
        self.assertEqual(written.strip(), DUMMY_CDD.read_text(encoding="utf-8").strip(),
                         "ODX/dummy_ecu.cdd is what tools/make_dummy_cdd.py writes")

    def test_what_a_cdd_can_hold(self):
        text = DUMMY_CDD.read_text(encoding="utf-8")
        root = ElementTree.fromstring(text.split("?>", 1)[1])
        protocol = next(p for p in root.iter("PROTOCOLSERVICE") if p.findtext("QUAL") == "ReadDataByIdentifier")
        protocol.find("REQ").find("CONSTCOMP").set("v", "0x22")                 # a hexadecimal constant
        instance = next(i for i in root.iter("DIAGINST") if i.findtext("QUAL") == "DID_0xF190")
        instance.append(ElementTree.fromstring('<SERVICE tmplref="nowhere" mayBeExec="(1)"/>'))
        path = Path(tempfile.mkdtemp()) / "edited.cdd"
        path.write_text(ElementTree.tostring(root, encoding="unicode"), encoding="utf-8")
        d = load_cdd(path)
        self.assertIn(0xF190, d.dids)
        self.assertTrue(any("without its PROTOCOLSERVICE" in warning for warning in d.warnings))
        broken = Path(tempfile.mkdtemp()) / "broken.cdd"
        broken.write_text("<CANDELA/>", encoding="utf-8")
        with self.assertRaises(CddError):
            load_cdd(broken)


class OdxTest(unittest.TestCase):
    def test_services_states_and_transitions_from_odxtools(self):
        default, extended = SimpleNamespace(short_name="Default"), SimpleNamespace(short_name="Extended")
        locked, unlocked = SimpleNamespace(short_name="Locked"), SimpleNamespace(short_name="Unlocked")

        def service(prefix, name, states=(), transitions=()):
            request = SimpleNamespace(coded_const_prefix=lambda: bytearray(prefix))
            return SimpleNamespace(short_name=name, request=request, pre_condition_states=list(states),
                                   state_transitions=[SimpleNamespace(source_state=a, target_state=b) for a, b in transitions])
        layer = SimpleNamespace(short_name="ECU", state_charts=[
            SimpleNamespace(short_name="Session", semantic="SESSION", states=[default, extended]),
            SimpleNamespace(short_name="Security", semantic="SECURITY", states=[locked, unlocked])], services=[
            service(b"\x10\x01", "Default", (), [(extended, default)]),
            service(b"\x10\x03", "Extended", (), [(default, extended)]),
            service(b"\x27\x01", "Seed", [extended]), service(b"\x27\x02", "Key", [extended], [(locked, unlocked)]),
            service(b"\x2e\xf1\x90", "WriteVIN", [extended, unlocked])])
        with patch.object(odx_loader, "load_database", return_value=None), \
                patch.object(odx_loader, "first_layer", return_value=layer):
            d = odx_loader.load_odx("ecu.odx")
        self.assertEqual(sorted(d.sessions), [1, 3])
        self.assertEqual(d.dids[0xF190].write, Access({3}, {1}))
        self.assertEqual(d.services[0x27].access, Access({3}, set()))

    def test_by_extension(self):
        self.assertEqual(odx_loader.load_description(DUMMY_CDD).name, "DummyECU (CommonDiagnostics)")


class Bench:
    def __init__(self, test, config=None):
        channel = "te-" + str(uuid.uuid4())
        self.tester_bus = can.Bus(interface="virtual", channel=channel)
        ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.ecu = DummyEcu(ecu_bus, config or EcuConfig(broadcast_interval=0, lockout_seconds=1),
                            log=lambda text: None)
        stop = threading.Event()
        threading.Thread(target=self.ecu.serve, args=(stop,), daemon=True).start()
        self.channel = channel

        def close():
            stop.set()
            time.sleep(0.05)
            self.tester_bus.shutdown()
            ecu_bus.shutdown()
        test.addCleanup(close)

    def run(self, description, names=None, sequences=(), base_dir=None, **options):
        options.setdefault("key", key)
        suite = Suite(description, Options(**options), sequences, base_dir)
        suite.tester = Tester(self.tester_bus, TRANSPORT, 0x7DF)
        return suite, Runner(suite.module(names), send=suite.tester.send_frame).run(names)


def failures(report):
    return {case.title: [step.description for step in case.failures()] for case in report.cases
            if case.verdict != "passed"}


class AgainstTheDummyEcuTest(unittest.TestCase):
    def test_the_dummy_ecu_keeps_every_rule(self):
        bench = Bench(self)
        suite, report = bench.run(dummy_description(bench.ecu.config))
        self.assertEqual(failures(report), {})
        groups = set(suite.groups())
        self.assertLessEqual({"Sessions", "TesterPresent", "Services", "Service availability", "NRC order",
                              "Message length", "Sub-functions", "Data identifiers", "Security access", "Routines",
                              "Fault memory", "Communication", "Functional addressing", "Timing"}, groups)

    def test_the_destructive_tests_and_the_lockout(self):
        bench = Bench(self)
        suite, report = bench.run(load_cdd(DUMMY_CDD), destructive=True, lockout=True, lockout_seconds=1)
        self.assertEqual(failures(report), {})
        titles = [case.title for case in report.cases]
        self.assertIn("ECU reset: ECUReset (11 01)", titles)
        self.assertTrue(any(title.startswith("Security access: Level1: lockout") for title in titles))
        self.assertTrue(any(title.startswith("Data identifiers: Write") for title in titles))

    def test_without_a_key_the_unlocking_is_skipped(self):
        bench = Bench(self)
        _suite, report = bench.run(dummy_description(bench.ecu.config), key=None)
        self.assertEqual(failures(report), {})
        security = next(case for case in report.cases if case.title.startswith("Security access"))
        self.assertIn("No key source", [step.description for step in security.steps][-1])

    def test_what_is_wrong_is_found(self):
        config = EcuConfig(broadcast_interval=0, forced_nrcs=[{"sid": 0x85, "nrc": 0x22}], p2_ms=50)
        bench = Bench(self, config)
        description = dummy_description(EcuConfig())
        description.dids[0xF190].length = 16                                  # the description says otherwise
        _suite, report = bench.run(description)
        found = failures(report)
        self.assertIn("Communication: ControlDTCSetting (85)", found)
        self.assertIn("Read DID 0xF190 (F190)", "".join(found))
        self.assertTrue(any("17 bytes" in step.detail or "20 bytes" in step.detail
                            for case in report.cases for step in case.failures()))


class SequenceTest(unittest.TestCase):
    def test_step_values(self):
        self.assertEqual(parse_hex("11 01"), b"\x11\x01")
        self.assertEqual(parse_hex("0x14,0xFF ff FF"), b"\x14\xff\xff\xff")
        self.assertEqual(parse_hex("22F190"), b"\x22\xf1\x90")
        self.assertEqual(parse_expect("NRC 0x22"), ("nrc", 0x22))
        self.assertEqual(parse_expect("31"), ("nrc", 0x31))
        self.assertEqual(parse_expect("no answer"), ("none", None))
        self.assertEqual(parse_frame("12F 01 02"), (0x12F, b"\x01\x02", False))
        self.assertEqual(parse_frame("18FEF100#0102"), (0x18FEF100, b"\x01\x02", True))
        self.assertEqual(parse_frame("012F"), (0x12F, b"", True), "four digits: an extended identifier")
        self.assertEqual(parse_script(r"C:\bench\power.py:cycle"), (r"C:\bench\power.py", "cycle"))
        self.assertEqual(parse_script("power.py"), ("power.py", "run"))
        for text in ("zz", "11 1FF"):
            with self.assertRaises(SequenceError):
                parse_hex(text)
        self.assertEqual(SequenceStep("unlock", "02").problem(), "requestSeed levels are odd")
        self.assertIn("not seconds", SequenceStep("wait", "soon").problem())
        self.assertEqual(SequenceStep("reset", "").problem(), "", "a hard reset by default")
        self.assertEqual(SequenceStep("request", "10 03", "NRC 7F").problem(), "")
        sequence = Sequence("Bench", [SequenceStep("wait", "x")], [Attachment("after", "test", "a.b", "failed")])
        self.assertEqual(Sequence.from_dict(sequence.to_dict()), sequence)
        self.assertEqual(sequence.problems(), ["step 1: not seconds: 'x'"])

    def test_where_sequences_run(self):
        always = Sequence("Always", [], [Attachment("after", "each")])
        failed = Sequence("On failure", [], [Attachment("after", "each", condition="failed")])
        group = Sequence("Group", [], [Attachment("before", "group", "Sessions")])
        off = Sequence("Off", [], [Attachment("after", "each")], enabled=False)
        sequences = [always, failed, group, off]
        self.assertEqual(due(sequences, "after", "each", outcome="passed"), [always])
        self.assertEqual(due(sequences, "after", "each", outcome="failed"), [always, failed])
        self.assertEqual(due(sequences, "after", "each", outcome=None), [always], "a skipped test")
        self.assertEqual(due(sequences, "before", "group", "Sessions"), [group])
        self.assertEqual(due(sequences, "before", "group", "Timing"), [])

    def test_around_tests_against_the_dummy_ecu(self):
        bench = Bench(self)
        description = dummy_description(bench.ecu.config)
        names = ["sessions.enter_the_extended_session_10_03", "sessions.suppress_positive_response",
                 "testerpresent.testerpresent_3e", "timing.responses_within_p2"]
        folder = Path(tempfile.mkdtemp())
        (folder / "bench.py").write_text("def power(t, tester):\n    t.log('power cycled')\n\n"
                                         "def broken(t, tester):\n    return False\n", encoding="utf-8")
        sequences = [
            Sequence("Ignition on", [SequenceStep("frame", "200 01"), SequenceStep("wait", "0.05"),
                                     SequenceStep("script", "bench.py:power")], [Attachment("before", "run")]),
            Sequence("Hard reset", [SequenceStep("reset", "01")],
                     [Attachment("after", "test", "sessions.enter_the_extended_session_10_03")]),
            Sequence("Extended", [SequenceStep("session", "03"), SequenceStep("request", "22 F1 86", "positive")],
                     [Attachment("before", "group", "TesterPresent")]),
            Sequence("Wrong", [SequenceStep("request", "22 12 34", "positive")],
                     [Attachment("before", "test", "sessions.suppress_positive_response")]),
            Sequence("Cleanup", [SequenceStep("request", "22 12 34", "NRC 22"), SequenceStep("wait", "5")],
                     [Attachment("after", "group", "Timing")]),
            Sequence("After a failure", [SequenceStep("script", "bench.py:broken")],
                     [Attachment("after", "each", condition="failed")]),
            Sequence("Done", [SequenceStep("request", "3E 80", "no answer")], [Attachment("after", "run")]),
        ]
        _suite, report = bench.run(description, names, sequences, folder, reset_time=0.6)
        cases = {case.name: case for case in report.cases}
        setup = [step.description for step in report.setup.steps]
        self.assertIn("Pre-run 'Ignition on': frame 200 01 sent", setup)
        self.assertIn("Pre-run 'Ignition on': bench.py:power()", setup)
        self.assertTrue(bench.ecu.running, "the frame reached the ECU: 200 01 starts its application")
        extended = cases["sessions.enter_the_extended_session_10_03"]
        self.assertEqual(extended.verdict, "passed")
        self.assertEqual(extended.steps[-1].description,
                         "Post-test 'Hard reset': ECU reset (11 01) and the ECU back after 0.6 s")
        self.assertEqual(extended.steps[-1].verdict, "pass")
        blocked = cases["sessions.suppress_positive_response"]
        self.assertEqual(blocked.verdict, "blocked")
        self.assertEqual(blocked.error, "the pre-test sequence 'Wrong' failed")
        self.assertFalse(any(step.description.startswith("10 81") for step in blocked.steps), "not run")
        self.assertEqual(blocked.steps[-1].description, "Post-test 'After a failure': bench.py:broken()",
                         "after a test that did not pass")
        self.assertEqual(blocked.steps[-1].verdict, "warn")
        tester_present = cases["testerpresent.testerpresent_3e"]
        self.assertEqual([step.description for step in tester_present.steps[:2]],
                         ["Pre-group 'Extended': Extended session entered (10 03)",
                          "Pre-group 'Extended': 22 F1 86 answered positively"])
        self.assertEqual(tester_present.verdict, "passed")
        timing = cases["timing.responses_within_p2"]
        self.assertEqual(timing.verdict, "passed", "a post-group warning leaves the verdict")
        self.assertEqual(timing.steps[-1].verdict, "warn")
        self.assertIn("22 12 34 answered NRC 0x22", timing.steps[-1].description)
        self.assertFalse(any("wait 5 s" in step.description for step in timing.steps), "stopped at its failure")
        self.assertFalse(any("After a failure" in step.description for step in timing.steps))
        self.assertEqual(report.teardown.steps[-1].description, "Post-run 'Done': 3E 80 is not answered")
        self.assertEqual(report.verdict, "failed", "the blocked test")

    def test_a_failing_pre_group_sequence_blocks_the_group(self):
        bench = Bench(self)
        sequences = [Sequence("Unreachable", [SequenceStep("session", "7E")], [Attachment("before", "group", "Sessions")])]
        names = ["sessions.default_session_10_01", "sessions.message_length", "testerpresent.testerpresent_3e"]
        _suite, report = bench.run(dummy_description(bench.ecu.config), names, sequences)
        verdicts = {case.name: (case.verdict, case.error) for case in report.cases}
        self.assertEqual(verdicts, {
            "sessions.default_session_10_01": ("blocked", "the pre-group sequence 'Unreachable' failed"),
            "sessions.message_length": ("blocked", "the pre-group sequence 'Unreachable' failed"),
            "testerpresent.testerpresent_3e": ("passed", "")})

    def test_a_failing_pre_run_sequence_stops_the_run(self):
        bench = Bench(self)
        sequences = [Sequence("Unlock", [SequenceStep("unlock", "01")], [Attachment("before", "run")])]
        _suite, report = bench.run(dummy_description(bench.ecu.config), ["sessions.message_length"], sequences)
        self.assertEqual(report.setup.verdict, "failed", "SecurityAccess is not allowed in the default session")
        self.assertEqual(report.cases[0].verdict, "skipped")
        self.assertEqual(report.verdict, "failed")


class WindowTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = patch.object(window_module, "TEST_EXPERT_DIR", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = Path(folder.name)
        self.settings = MemorySettings()
        self.window = window_module.TestExpertWindow(self.settings)
        self.addCleanup(self.window.close)

    def test_a_description_and_its_tests(self):
        self.assertEqual(self.window.description.name, "Dummy ECU", "the Dummy ECU without a file")
        count = len(self.window.suite.cases)
        self.window.destructive.setChecked(True)
        self.assertGreater(len(self.window.suite.cases), count)
        self.assertIsNotNone(self.window.open_description(DUMMY_CDD))
        self.assertEqual(self.settings.value("test_expert/description"), str(DUMMY_CDD))
        self.assertIn("DummyECU", self.window.description_label.text())
        bad = Path(tempfile.mkdtemp()) / "bad.cdd"
        bad.write_text("not xml", encoding="utf-8")
        self.assertIsNone(self.window.open_description(bad))
        self.assertIn("could not be read", self.window.log.toPlainText())

    def test_a_run_against_the_dummy_ecu(self):
        bench = Bench(self)
        self.assertIsNone(self.window.run(), "not connected")
        self.assertTrue(self.window.connect_ecu(can.Bus(interface="virtual", channel=bench.channel)))
        self.addCleanup(self.window.disconnect_ecu)
        self.window.request_id.setValue(0x7E0)
        self.window.response_id.setValue(0x7E8)
        self.window.record.setChecked(True)
        first = next(iter(self.window._items))
        self.window._items[first].setCheckState(0, 0)                         # unticked: not run
        self.assertIsNotNone(self.window.run())
        self.assertTrue(spin_until(lambda: self.window.report is not None and self.window.run_btn.isEnabled()),
                        self.window.log.toPlainText())
        report = self.window.report
        self.assertEqual(report.verdict, "passed", failures(report))
        self.assertNotIn(first, [case.name for case in report.cases])
        html, xml = self.window.report_paths
        self.assertEqual(html.parent, self.folder / "reports")
        self.assertTrue(xml.exists())
        self.assertTrue(list((self.folder / "reports").glob("traffic_*.blf")), "the traffic was recorded")
        self.assertIn("PASSED", self.window.status.text())

    def test_sequences_in_the_window(self):
        window = self.window
        name = "sessions.enter_the_extended_session_10_03"
        item = window._items[name]
        self.assertIsNone(window.sequence_menu(None))
        menu = window.sequence_menu(item)
        after = next(action for action in menu.actions() if action.text() == "After this test")
        hard_reset = next(action for action in after.menu().actions() if action.text() == "New: Hard reset")
        hard_reset.trigger()
        self.assertEqual(item.text(window_module.COL_SEQUENCES), "after: Hard reset")
        sequence = window.sequence_editor.sequences()[0]
        self.assertEqual((sequence.name, sequence.attachments), ("Hard reset", [Attachment("after", "test", name)]))
        window.sequence_editor.add_step("wait", "0.5")
        self.assertEqual(window.sequence_editor.sequences()[0].steps[-1], SequenceStep("wait", "0.5"))
        window.rebuild_tests()
        self.assertEqual(window.suite.sequences, window.sequence_editor.sequences())
        again = window_module.TestExpertWindow(self.settings)
        self.addCleanup(again.close)
        self.assertEqual(again.sequence_editor.sequences(), window.sequence_editor.sequences(), "kept in the settings")
        self.assertEqual(window._items[name].text(window_module.COL_SEQUENCES), "after: Hard reset")
        window.sequence_editor.detach("test", name)
        self.assertEqual(window._items[name].text(window_module.COL_SEQUENCES), "")

    def test_main_smoke_test(self):
        self.assertEqual(window_module.main(["--smoke-test"]), 0)
        self.assertEqual(window_module.main([str(DUMMY_CDD), "--smoke-test"]), 0)


if __name__ == "__main__":
    unittest.main()
