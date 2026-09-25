"""TestExpert: descriptions (JSON, how states become sessions and levels, CDD, ODX), the tests generated from
them against the Dummy ECU - passing when it keeps the rules, failing where it is made not to - the pre-test and
post-test sequences around them, the NRC policy and accepted deviations, what a run covered, discovering what
the ECU has, test plans and their run from the command line, and the window."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import argparse
import json
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
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from canexpert.paths import ODX_DIR
from canexpert.simulator.ecu import DummyEcu, EcuConfig
from canexpert.test_expert import cli
from canexpert.test_expert import odx as odx_loader
from canexpert.test_expert import window as window_module
from canexpert.test_expert.cdd import CddError, load_cdd
from canexpert.test_expert.coverage import Coverage, coverage_html, untested
from canexpert.test_expert.description import (Access, DataField, EcuDescription, RawService, RawState,
                                               build_description)
from canexpert.test_expert.discovery import (Discovery, DiscoveryOptions, DiscoveryResult, compare, discovery_page,
                                             expand, parse_ranges)
from canexpert.test_expert.dummy import dummy_description
from canexpert.test_expert.generator import Options, Suite, parse_routine_starts
from canexpert.test_expert.plan import Connection, KeySource, PlanError, TestPlan, is_plan_file
from canexpert.test_expert.policy import Deviation, NrcPolicy, accept_function, parse_nrcs
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
        self.assertEqual({d: (x.name, x.length, x.read, x.write, x.fields) for d, x in cdd.dids.items()},
                         {d: (x.name, x.length, x.read, x.write, x.fields) for d, x in dummy.dids.items()},
                         "the fields too: text tables, ranges, scales, units, text")
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
        instance = next(i for i in root.iter("DIAGINST") if i.findtext("QUAL") == "VIN")
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


class DataFieldTest(unittest.TestCase):
    def test_values_and_their_limits(self):
        temperature = DataField("Temperature", 0, 16, "signed", [(-400, 1500)], scale=0.1, unit="degC")
        self.assertEqual(temperature.check(bytes.fromhex("00d7")), (True, "21.5 degC"))
        self.assertEqual(temperature.check(bytes.fromhex("07d0")),
                         (False, "200 degC: not a valid value (-40 degC to 150 degC)"))
        self.assertEqual(temperature.coded(bytes.fromhex("ff00")), -256, "two's complement")
        session = DataField("Session", 0, 8, texts={1: "Default", 3: "Extended"})
        self.assertEqual(session.check(b"\x03"), (True, "Extended (3)"))
        self.assertFalse(session.check(b"\x02")[0], "not in the text table")
        self.assertEqual(DataField("VIN", 0, 136, "ascii").check(b"WVWZZZ1KZAW000001"), (True, '"WVWZZZ1KZAW000001"'))
        self.assertFalse(DataField("Name", 0, 32, "ascii").check(b"AB\x01\x02")[0])
        self.assertEqual(DataField("Name", 0, 32, "ascii").check(b"AB\x00\x00"), (True, '"AB"'), "padded")
        date = DataField("Day", 8, 16, "bcd")
        self.assertEqual(date.coded(bytes.fromhex("202409")), 2409)
        self.assertEqual(date.check(bytes.fromhex("20240A")), (False, "not a BCD number"))
        self.assertEqual(date.encode(bytes(3), 1234).hex(), "001234")
        nibble = DataField("Nibble", 4, 8)
        self.assertEqual((nibble.encode(bytes.fromhex("ffff"), 0x12).hex(), nibble.coded(bytes.fromhex("f12f"))),
                         ("f12f", 0x12))
        self.assertEqual(DataField("Short", 0, 32).check(b"\x01"), (False, "not in the record"))
        described = dummy_description()
        again = EcuDescription.from_dict(json.loads(json.dumps(described.to_dict())))
        self.assertEqual(again.dids[0x0110].fields, described.dids[0x0110].fields)
        self.assertEqual(described.dids[0x0110].fields[0].valid, [(600, 1200)])
        self.assertEqual(parse_routine_starts("0201; FF00: 44 00 01"), {0x0201: b"", 0xFF00: b"\x44\x00\x01"})
        with self.assertRaises(ValueError):
            parse_routine_starts("0201: zz")


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

    def test_a_dids_fields(self):
        def limit(value):
            return SimpleNamespace(value=value)

        def dop(bits, base, compu=None, constr=None, unit=None):
            return SimpleNamespace(diag_coded_type=SimpleNamespace(bit_length=bits, base_data_type=base),
                                   compu_method=compu, internal_constr=constr, unit=unit)
        texts = SimpleNamespace(category="CompuCategory.TEXTTABLE", compu_internal_to_phys=SimpleNamespace(
            compu_scales=[SimpleNamespace(lower_limit=limit(1), upper_limit=limit(1), compu_const=SimpleNamespace(vt="Default")),
                          SimpleNamespace(lower_limit=limit(3), upper_limit=limit(3), compu_const=SimpleNamespace(vt="Extended"))]))
        linear = SimpleNamespace(category="LINEAR", compu_internal_to_phys=SimpleNamespace(compu_scales=[
            SimpleNamespace(compu_rational_coeffs=SimpleNamespace(numerators=[-40, 0.5], denominators=[1]))]))
        parameters = [SimpleNamespace(short_name="SID", parameter_type="CODED-CONST", byte_position=0),
                      SimpleNamespace(short_name="DID", parameter_type="CODED-CONST", byte_position=1),
                      SimpleNamespace(short_name="Session", parameter_type="VALUE", byte_position=3, bit_position=0,
                                      dop=dop(8, "DataType.A_UINT32", texts)),
                      SimpleNamespace(short_name="Temperature", parameter_type="VALUE", byte_position=4, bit_position=4,
                                      dop=dop(4, "DataType.A_UINT32", linear, SimpleNamespace(lower_limit=limit(0),
                                                                                              upper_limit=limit(9)),
                                              SimpleNamespace(display_name="degC"))),
                      SimpleNamespace(short_name="Name", parameter_type="VALUE", byte_position=5,
                                      dop=dop(32, "DataType.A_ASCIISTRING"))]
        service = SimpleNamespace(positive_responses=[SimpleNamespace(parameters=parameters)])
        fields = odx_loader.did_fields(service)
        self.assertEqual([(f.name, f.position, f.bits, f.encoding) for f in fields],
                         [("Session", 0, 8, "unsigned"), ("Temperature", 8, 4, "unsigned"), ("Name", 16, 32, "ascii")])
        self.assertEqual(fields[0].texts, {1: "Default", 3: "Extended"})
        self.assertEqual((fields[1].scale, fields[1].shift, fields[1].valid, fields[1].unit), (0.5, -40.0, [(0, 9)], "degC"))
        broken = SimpleNamespace(positive_responses=[SimpleNamespace(parameters=[
            SimpleNamespace(short_name="X", parameter_type="VALUE", byte_position=None, dop=dop(8, "A_UINT32"))])])
        self.assertEqual(odx_loader.did_fields(broken), [], "a field without its place: none at all")

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

    def run(self, description, names=None, sequences=(), base_dir=None, policy=None, deviations=(), **options):
        options.setdefault("key", key)
        options.setdefault("s3_test", False)
        suite = Suite(description, Options(**options), sequences, base_dir, policy)
        suite.tester = Tester(self.tester_bus, TRANSPORT, 0x7DF)
        runner = Runner(suite.module(names), send=suite.tester.send_frame, accept=accept_function(deviations))
        return suite, runner.run(names)


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
        skipped = {case.title: case.error for case in report.cases if case.verdict == "skipped"}
        self.assertEqual(set(skipped.values()), {"no key source set for SecurityAccess"})
        self.assertIn("Data identifiers: Values of CalibrationId (0200)", skipped, "a DID read once unlocked")
        self.assertIn("Routines: EraseMemory (FF00): stop and results before a start", skipped)
        self.assertEqual({title: steps for title, steps in failures(report).items() if title not in skipped}, {})
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
        self.assertIn("Read VIN (F190)", "".join(found))
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


class PolicyTest(unittest.TestCase):
    READ_ONLY = ["data_identifiers.writing_a_read_only_did"]

    def test_the_policy_and_deviations_as_values(self):
        self.assertEqual(parse_nrcs("31, 0x7F 22"), (0x31, 0x7F, 0x22))
        with self.assertRaises(ValueError):
            parse_nrcs("131")
        policy = NrcPolicy()
        self.assertEqual(policy.accepted("did_not_in_session"), (0x31,))
        self.assertEqual(policy.accepted("routine_not_in_session"), (0x31, 0x7F, 0x7E), "what TestExpert tolerated")
        policy.set("did_not_in_session", (0x31, 0x7F))
        policy.set("locked", (0x33,))                                         # the default: nothing kept
        self.assertEqual(policy.to_dict(), {"did_not_in_session": ["31", "7F"]})
        self.assertEqual(NrcPolicy.from_dict(policy.to_dict()), policy)
        deviations = [Deviation("sessions.message_length", "10 alone: incorrect length", "ticket 7"),
                      Deviation("timing.responses_within_p2", "*", "")]
        accept = accept_function(deviations)
        self.assertEqual(accept("sessions.message_length", "10 alone: incorrect length"), "ticket 7")
        self.assertIsNone(accept("sessions.message_length", "10 01 00: one byte too many"))
        self.assertEqual(accept("timing.responses_within_p2", "anything"), "")
        self.assertIsNone(accept_function([]), "no deviations: nothing to ask")

    def test_what_the_ecu_answers_instead(self):
        config = EcuConfig(broadcast_interval=0, forced_nrcs=[{"sid": 0x2E, "nrc": 0x7F}])
        bench = Bench(self, config)
        description = dummy_description(EcuConfig())
        _suite, report = bench.run(description, self.READ_ONLY)
        (case,) = report.cases
        self.assertEqual(case.verdict, "failed", "0x7F where ISO 14229-1 asks for 0x31")
        self.assertIn("expected NRC 0x31 requestOutOfRange", case.steps[0].detail)
        policy = NrcPolicy()
        policy.set("did_read_only", (0x31, 0x7F))
        _suite, report = bench.run(description, self.READ_ONLY, policy=policy)
        (case,) = report.cases
        self.assertEqual(case.verdict, "passed")
        self.assertIn("accepted by the NRC policy; ISO 14229-1 asks for 0x31 requestOutOfRange", case.steps[0].detail)
        policy.set("did_read_only", (0x22,))                  # a specification with its own code
        _suite, report = bench.run(description, self.READ_ONLY, policy=policy)
        self.assertEqual(report.cases[0].verdict, "failed")
        self.assertIn("expected NRC 0x22 conditionsNotCorrect", report.cases[0].steps[0].detail)

    def test_accepted_deviations(self):
        config = EcuConfig(broadcast_interval=0, forced_nrcs=[{"sid": 0x2E, "nrc": 0x7F}])
        bench = Bench(self, config)
        description = dummy_description(EcuConfig())
        _suite, report = bench.run(description, self.READ_ONLY)
        steps = [item.description for item in report.cases[0].steps]
        self.assertGreater(len(steps), 1, "one step for each read-only DID")
        deviation = Deviation(self.READ_ONLY[0], steps[0], "the supplier's 0x7F, agreed in ticket 42")
        _suite, report = bench.run(description, self.READ_ONLY, deviations=[deviation])
        (case,) = report.cases
        self.assertEqual([item.verdict for item in case.steps], ["accepted"] + ["fail"] * (len(steps) - 1))
        self.assertIn("accepted deviation: the supplier's 0x7F", case.steps[0].detail)
        self.assertEqual(case.verdict, "failed", "the other DIDs still fail")
        every = Deviation(self.READ_ONLY[0], "*", "")
        _suite, report = bench.run(description, self.READ_ONLY, deviations=[every])
        self.assertEqual(report.cases[0].verdict, "passed")
        self.assertEqual({item.verdict for item in report.cases[0].steps}, {"accepted"})


class CoverageTest(unittest.TestCase):
    def test_counting(self):
        coverage = Coverage()
        coverage.record(b"\x22\xf1\x90\xf1\x86", 0x03, "pass", "data_identifiers.read")
        coverage.record(b"\x2e\xf1\x90\x00", 0x03, "fail", "data_identifiers.write")
        coverage.record(b"\x31\x01\xff\x00", 0x02, "accepted", "routines.erase")
        coverage.record(b"\x3e\x00", None, "pass", "tester_present", functional=True)
        coverage.record(b"\x10\x01", 0x01, "info")                      # a log line: not a check
        self.assertEqual(coverage.services[(0x22, 0x03)].verdict(), "passed")
        self.assertEqual(coverage.dids[(0xF186, 0x03, "read")].count, 1, "each DID of the request")
        self.assertEqual(coverage.dids[(0xF190, 0x03, "write")].verdict(), "failed")
        self.assertEqual(coverage.routines[(0xFF00, 0x02)].verdict(), "accepted")
        self.assertEqual(coverage.services[(0x3E, -1)].count, 1, "before the session was known")
        self.assertEqual(coverage.functional[0x3E].count, 1)
        self.assertEqual(coverage.services[(0x10, 0x01)].count, 0)
        again = Coverage.from_dict(json.loads(json.dumps(coverage.to_dict())))
        self.assertEqual(again.to_dict(), coverage.to_dict())
        described = dummy_description()
        missing = dict(untested(coverage, described, Options()))
        self.assertEqual(missing["Service 11 ECUReset"], "ECU reset is a destructive test: tick Destructive tests")
        self.assertEqual(missing["Routine FF00 EraseMemory: started"],
                         "routines are not started (only refused where they may not run)")
        self.assertIn("DID F187 SparePartNumber: read", missing)
        page = coverage_html(coverage, described, Options())
        self.assertIn("<h2>Coverage</h2>", page)
        self.assertIn("F190 VIN</td><td>write</td>", page)

    def test_a_run_counts_what_it_checked(self):
        bench = Bench(self)
        names = ["testerpresent.testerpresent_3e", "data_identifiers.read_vin_f190",
                 "service_availability.securityaccess_27_by_session"]
        suite, report = bench.run(dummy_description(bench.ecu.config), names)
        self.assertEqual(report.verdict, "passed", failures(report))
        coverage = suite.coverage
        self.assertEqual(coverage.services[(0x3E, 0x01)].verdict(), "passed")
        self.assertEqual(coverage.services[(0x27, 0x01)].verdict(), "passed", "refused in the default session: 0x7F")
        self.assertIn("service_availability.securityaccess_27_by_session", coverage.services[(0x27, 0x03)].tests)
        self.assertEqual({key[1] for key in coverage.dids if key[0] == 0xF190}, {0x01, 0x02, 0x03})
        self.assertEqual(suite.identification[0xF195], ("systemSupplierECUSoftwareVersionNumber", "APP-1.0.0"),
                         "read at the start, with ISO's name")
        self.assertEqual(suite.identification[0xF190][1], "WVWZZZ1KZAW000001")
        self.assertIn("ECU identification: F195 systemSupplierECUSoftwareVersionNumber = APP-1.0.0",
                      [step.description for step in report.setup.steps])


class DiscoveryTest(unittest.TestCase):
    OPTIONS = DiscoveryOptions([0x01, 0x03], "0100-0102, F180-F19F", "0200-0202, FF00-FF01")

    def test_ranges(self):
        self.assertEqual(parse_ranges("F100-F1FF, 0100"), [(0xF100, 0xF1FF), (0x0100, 0x0100)])
        self.assertEqual(expand(parse_ranges("0100-0102,0101")), [0x100, 0x101, 0x102])
        for text in ("F1FF-F100", "zz", "10000"):
            with self.assertRaises(ValueError):
                parse_ranges(text)

    def discover(self, bench, description, options=None, stop=None):
        tester = Tester(bench.tester_bus, TRANSPORT, 0x7DF)
        return Discovery(tester, description, options or self.OPTIONS, stop=stop).run()

    def test_what_the_ecu_has_and_the_description_does_not_say(self):
        bench = Bench(self)
        description = dummy_description(bench.ecu.config)
        del description.dids[0xF18C]                        # forgotten
        description.dids[0xF190].length = 16                # wrong
        del description.services[0x86]                      # forgotten
        description.dids[0xF1A0] = description.dids[0xF187].__class__(0xF1A0, "Ghost", 4, Access(), None)
        result = self.discover(bench, description)
        self.assertEqual(result.entered(), [0x01, 0x03])
        self.assertTrue(result.service_found(0x86))
        self.assertEqual(result.service_sessions(0x27), {0x03}, "SecurityAccess: extended only")
        self.assertEqual(result.did_length(0xF190), 17)
        self.assertEqual(result.found_levels(), [0x01])
        self.assertTrue(result.routine_found(0x0201), "the self test: its results are asked, it is not started")
        self.assertFalse(bench.ecu.state.routines, "nothing was started")
        findings = {(finding.kind, finding.what) for finding in compare(result, description)}
        self.assertEqual(findings, {("undocumented", "Service 86 ResponseOnEvent"), ("undocumented", "DID F18C"),
                                    ("different", "DID F190 VIN"), ("missing", "DID F1A0 Ghost")})
        again = DiscoveryResult.from_dict(json.loads(json.dumps(result.to_dict())))
        self.assertEqual(again.to_dict(), result.to_dict())
        page = discovery_page(result, description)
        self.assertIn("found, not described", page)
        self.assertIn("the ECU answers 17 bytes, the description says 16", page)

    def test_testing_the_ecu_as_it_was_found(self):
        bench = Bench(self)
        result = self.discover(bench, None, DiscoveryOptions([0x01, 0x03], "0100-0102, F186-F195", "0201"))
        found = result.to_description("Found")
        self.assertEqual(found.unknown, {"writing", "sub-functions", "starting routines"})
        self.assertEqual(found.services[0x34].access, Access({0x02}), "refused everywhere asked: elsewhere")
        self.assertEqual(found.services[0x35].access.levels, {0x01}, "0x33 to the SID alone: behind a level")
        self.assertEqual(found.dids[0xF190].length, 17)
        self.assertEqual(EcuDescription.from_dict(found.to_dict()).unknown, found.unknown)
        _suite, report = bench.run(found)
        self.assertEqual(failures(report), {})
        self.assertNotIn("Data identifiers: Writing a read-only DID", [case.title for case in report.cases],
                         "which DIDs may be written is not known")

    def test_stopping(self):
        bench = Bench(self)
        stop = threading.Event()
        stop.set()
        result = self.discover(bench, dummy_description(bench.ecu.config), stop=stop)
        self.assertEqual(result.probes, 0)
        self.assertIn("Stopped before the end", result.notes[0])


class PlanTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_a_plan_as_json(self):
        (self.folder / "cdd").mkdir()
        description = self.folder / "cdd" / "ecu.cdd"
        description.write_bytes(DUMMY_CDD.read_bytes())
        plan = TestPlan("Nightly", str(description), Connection("vector", "1", 250000, 0x18DA10F1, 0x18DAF110, None,
                                                               True, None),
                        excluded=["timing.responses_within_p2"], key=KeySource("dll", 0x5A, "keys/ecu.dll", "B"),
                        sequences=[Sequence("Hard reset", [SequenceStep("reset", "01")], [Attachment("after", "run")])],
                        nrc_policy=NrcPolicy({"locked": (0x33, 0x22)}),
                        deviations=[Deviation("timing.responses_within_p2", "*", "slow gateway", "2026-09-24")])
        plan.options["destructive"] = True
        plan.discovery = DiscoveryOptions([0x01, 0x02], "F100-F1FF", "0200", False, True)
        path = self.folder / "plans" / "nightly.json"
        path.parent.mkdir()
        plan.path = path
        plan.description = plan.relative(description)
        plan.save(path)
        written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["description"], "../cdd/ecu.cdd", "relative to the plan's folder")
        self.assertEqual(written["connection"]["request_id"], "0x18DA10F1")
        self.assertIsNone(written["connection"]["functional_id"])
        again = TestPlan.load(path)
        self.assertEqual(again.to_dict(), plan.to_dict())
        self.assertEqual(again.resolve(again.description), description.resolve())
        self.assertEqual(again.load_description().name, "DummyECU (CommonDiagnostics)")
        self.assertTrue(again.make_options().destructive)
        self.assertEqual(again.connection.transport()["padding"], None)
        self.assertTrue(is_plan_file(path))
        moved = self.folder / "elsewhere" / "nightly.json"
        moved.parent.mkdir()
        again.save(moved)
        self.assertEqual(json.loads(moved.read_text(encoding="utf-8"))["description"], "../cdd/ecu.cdd")
        self.assertEqual(TestPlan.load(moved).resolve("../cdd/ecu.cdd"), description.resolve())

    def test_what_is_not_a_plan(self):
        described = self.folder / "ecu.json"
        dummy_description().save(described)
        self.assertFalse(is_plan_file(described), "a description saved as JSON")
        with self.assertRaises(PlanError):
            TestPlan.load(described)
        newer = self.folder / "newer.json"
        newer.write_text(json.dumps({"format": "TestExpert plan", "version": 99}), encoding="utf-8")
        with self.assertRaises(PlanError):
            TestPlan.load(newer)
        plan = TestPlan(description="missing.cdd", path=self.folder / "plan.json")
        with self.assertRaises(PlanError):
            plan.load_description()
        self.assertEqual(TestPlan().load_description().name, "Dummy ECU", "no description: the Dummy ECU's")
        from_numbers = TestPlan.from_dict({"format": "TestExpert plan", "connection": {"request_id": 2016, "padding": "AA"}})
        self.assertEqual((from_numbers.connection.request_id, from_numbers.connection.padding), (0x7E0, 0xAA))

    def test_the_command_line(self):
        arguments = argparse.Namespace(file=None, interface="virtual", channel="7", bitrate=None)
        plan = cli.load_plan(arguments)
        self.assertEqual((plan.connection.interface, plan.connection.channel, plan.connection.bitrate),
                         ("virtual", "7", 500000))
        suite = Suite(dummy_description())
        keep = {"sessions.default_session_10_01", "testerpresent.testerpresent_3e"}
        plan = TestPlan("CLI", excluded=[case.name for case in suite.cases if case.name not in keep],
                        sequences=[Sequence("Hard reset", [SequenceStep("reset", "01")], [Attachment("after", "run")])])
        plan.options["reset_time"] = 0.6
        path = self.folder / "cli.json"
        plan.save(path)
        reports, junit = self.folder / "reports", self.folder / "ci" / "junit.xml"
        with patch("sys.stdout") as out:
            code = cli.main([str(path), "--run", "--dummy-ecu", "--report-dir", str(reports), "--junit", str(junit)])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(code, cli.EXIT_PASSED, printed)
        self.assertIn("Running 2 tests against a Dummy ECU", printed)
        self.assertIn("PASSED   Sessions: Default session (10 01)", printed)
        self.assertIn("PASSED: 2 passed, 0 failed", printed)
        self.assertTrue(junit.exists())
        root = ElementTree.parse(junit).getroot()
        self.assertEqual(root.find("testsuite").get("tests"), "2")
        page = next(reports.glob("*.html")).read_text(encoding="utf-8")
        self.assertIn("<th>Test plan</th>", page)
        self.assertIn("<h2>Coverage</h2>", page)
        self.assertIn("<th>F195 systemSupplierECUSoftwareVersionNumber</th><td>APP-1.0.0</td>", page)
        self.assertIn("Post-run &#x27;Hard reset&#x27;", page)

        plan.sequences = [Sequence("Unknown DID", [SequenceStep("request", "22 12 34", "positive")],
                                   [Attachment("before", "each")])]
        plan.record = True
        plan.save(path)
        with patch("sys.stdout"):
            self.assertEqual(cli.main([str(path), "--run", "--dummy-ecu", "--report-dir", str(reports), "--quiet"]),
                             cli.EXIT_FAILED, "blocked tests")
        self.assertTrue(list(reports.glob("*.blf")), "the traffic recorded beside the reports")
        plan.description = "nowhere.cdd"
        plan.save(path)
        described = self.folder / "described.json"
        description = dummy_description()
        description.dids[0xF190].length = 16
        description.save(described)
        with patch("sys.stdout") as out:
            code = cli.main([str(described), "--discover", "--dummy-ecu", "--dids", "F190", "--rids", "0201",
                             "--report-dir", str(reports), "--save-description", str(self.folder / "found.json")])
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertEqual(code, cli.EXIT_FAILED, printed)
        self.assertIn("DIFFERENT    DID F190 VIN: the ECU answers 17 bytes, the description says 16", printed)
        self.assertTrue(list(reports.glob("discovery_*.html")) and list(reports.glob("discovery_*.json")))
        self.assertEqual(EcuDescription.load(self.folder / "found.json").dids[0xF190].length, 17)
        with patch("sys.stdout"):
            self.assertEqual(cli.main([str(DUMMY_CDD), "--discover", "--dummy-ecu", "--dids", "F190", "--rids", "0201",
                                       "--report-dir", str(reports)]), cli.EXIT_PASSED, "no difference")
            self.assertEqual(cli.main([str(DUMMY_CDD), "--discover", "--dummy-ecu", "--dids", "zz"]), cli.EXIT_NOT_RUN)
        with patch("sys.stdout"):
            self.assertEqual(cli.main([str(path), "--run", "--dummy-ecu"]), cli.EXIT_NOT_RUN)
            self.assertEqual(cli.main([str(DUMMY_CDD), "--run", "--interface", "no-such-interface"]), cli.EXIT_NOT_RUN)


class DeeperTest(unittest.TestCase):
    """Values against the description's fields, writes at and beyond their limits, routines started and in the
    wrong order, S3, and responses pending."""

    def test_the_dummy_ecu_keeps_them(self):
        bench = Bench(self, EcuConfig(broadcast_interval=0, s3_timeout=1.0, self_test_seconds=0.5))
        suite, report = bench.run(dummy_description(bench.ecu.config), destructive=True, start_routines="0201",
                                  s3_test=True, s3_seconds=1.0)
        self.assertEqual(failures(report), {})
        steps = {case.title: [step.description for step in case.steps] for case in report.cases}
        self.assertIn("Speed out of range (1201 rpm): refused, NRC 0x31",
                      steps["Data identifiers: Write IdleSpeedTarget (0110): its limits, and out of them"])
        self.assertEqual(steps["Routines: Start SelfTest (0201)"],
                         ["started (31 01)", "its results (31 03)", "stopped (31 02)", "its results after the stop"])
        self.assertIn("Routines: SelfTest (0201): stop and results before a start", steps)
        self.assertIn("after S3 without a request, the default session",
                      steps["Timing: S3: the Extended session ends without requests"])
        self.assertIn("Session: a valid value", steps["Data identifiers: Values of ActiveDiagnosticSession (F186)"])

    def test_what_is_wrong_is_found(self):
        config = EcuConfig(broadcast_interval=0, s3_timeout=3.0, self_test_seconds=0.5)
        for item in config.dids:
            if item["did"] == 0x0110:
                item["data"], item["valid"] = "0100", []           # 256 rpm, and any value taken
        bench = Bench(self, config)
        bench.ecu.state.routines[0x0201] = {"status": 0x00, "until": None}     # results kept from before
        description = dummy_description(EcuConfig())
        names = [case.name for case in Suite(description, Options(destructive=True, s3_test=True)).cases
                 if "0110" in case.title or "S3: the" in case.title]
        _suite, report = bench.run(description, names, destructive=True, s3_test=True, s3_seconds=1.0)
        found = {case.title: [(step.description, step.detail) for step in case.failures()] for case in report.cases}
        self.assertIn(("Speed: a valid value", "256 rpm: not a valid value (600 rpm to 1200 rpm)"),
                      found["Data identifiers: Values of IdleSpeedTarget (0110)"])
        self.assertEqual([step for step, _detail in found["Data identifiers: Write IdleSpeedTarget (0110): its "
                                                          "limits, and out of them"]],
                         ["Speed out of range (1201 rpm): refused, NRC 0x31", "nothing written"])
        self.assertTrue(found["Timing: S3: the Extended session ends without requests"], "S3 3 s, not 1 s")

    def test_responses_pending(self):
        names = ["testerpresent.testerpresent_3e"]
        bench = Bench(self, EcuConfig(broadcast_interval=0, response_delay_ms=120))
        _suite, report = bench.run(dummy_description(bench.ecu.config), names)
        (case,) = report.cases
        self.assertEqual(case.verdict, "passed", failures(report))
        pending = [step for step in case.steps if "response pending within P2" in step.description]
        self.assertTrue(pending and all(step.verdict == "pass" for step in pending))
        suppressed = next(step for step in case.steps if step.description.startswith("3E 80"))
        self.assertIn("after a response pending the answer is sent", suppressed.detail)
        slow = Bench(self, EcuConfig(broadcast_interval=0, response_delay_ms=700, pending_interval=0.5, p2_star_ms=300))
        _suite, report = slow.run(dummy_description(slow.ecu.config), names)
        failed = [step.detail for step in report.cases[0].failures()]
        self.assertTrue(failed and all("(P2* 300 ms)" in detail for detail in failed), failed)


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
        self.window.s3_test.setChecked(False)                                # a few seconds of waiting

    def test_a_description_and_its_tests(self):
        self.assertEqual(self.window.description.name, "Dummy ECU", "the Dummy ECU without a file")
        count = len(self.window.suite.cases)
        self.window.destructive.setChecked(True)
        self.assertGreater(len(self.window.suite.cases), count)
        self.assertIsNotNone(self.window.open_description(DUMMY_CDD))
        kept = json.loads(self.settings.value("test_expert/plan_state"))
        self.assertEqual(kept["description"], str(DUMMY_CDD.resolve()), "kept for the next start")
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
        self.assertIn("Services", self.window.coverage_view.toPlainText())
        self.assertIn("ECU: F195 systemSupplierECUSoftwareVersionNumber = APP-1.0.0", self.window.log.toPlainText())

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

    def test_plans_in_the_window(self):
        window = self.window
        window.open_description(DUMMY_CDD)
        window.interface.setCurrentText("virtual")
        window.channel.setEditText("bench")
        window.destructive.setChecked(True)
        window.plan_name.setText("Bench")
        name = "timing.responses_within_p2"
        window._items[name].setCheckState(0, 0)
        window.sequence_editor.add_preset("Hard reset")
        path = self.folder / "plans" / "bench.json"
        path.parent.mkdir()
        self.assertEqual(window.save_plan_as(path), path)
        self.assertEqual(window.windowTitle(), "TestExpert - Bench")
        saved = TestPlan.load(path)
        self.assertEqual(saved.excluded, [name])
        self.assertFalse(Path(saved.description).is_absolute(), "relative to the plan's folder")
        self.assertEqual(saved.resolve(saved.description), DUMMY_CDD.resolve())
        self.assertTrue(saved.options["destructive"])
        other = window_module.TestExpertWindow(MemorySettings())
        self.addCleanup(other.close)
        self.assertEqual(other.description.name, "Dummy ECU")
        self.assertIsNotNone(other.open_plan(path))
        self.assertEqual(other.description.name, "DummyECU (CommonDiagnostics)")
        self.assertEqual((other.interface.currentText(), other.channel.currentText()), ("virtual", "bench"))
        self.assertTrue(other.destructive.isChecked())
        self.assertEqual(other._items[name].checkState(0), 0, "left out, as the plan says")
        self.assertEqual([sequence.name for sequence in other.sequence_editor.sequences()], ["Hard reset"])
        self.assertEqual(other.plan(path).to_dict(), saved.to_dict())
        other.start_routines.setText("0201: zz")
        self.assertEqual(other.plan().options["start_routines"], "", "not readable: none")
        other.start_routines.setText("0201")
        other.s3_seconds.setValue(2.5)
        self.assertEqual((other.plan().options["start_routines"], other.plan().options["s3_seconds"]), ("0201", 2.5))
        other.rebuild_tests()
        self.assertIn("Routines: Start SelfTest (0201)", [case.title for case in other.suite.cases])
        other.new_plan()
        self.assertEqual((other.description.name, other.sequence_editor.sequences(), other.plan_path),
                         ("Dummy ECU", [], None))
        bad = self.folder / "bad.json"
        bad.write_text("{}", encoding="utf-8")
        self.assertIsNone(other.open_plan(bad))
        self.assertIn("not a TestExpert plan", other.log.toPlainText())
        restarted = window_module.TestExpertWindow(self.settings)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.plan_path, path, "the plan in use when the window was left")
        self.assertEqual(restarted._items[name].checkState(0), 0)

    def test_policy_and_deviations_in_the_window(self):
        window = self.window
        editor = window.policy_editor
        row = next(row for row in range(editor.nrcs.rowCount())
                   if editor.nrcs.item(row, 0).data(Qt.UserRole) == "did_not_in_session")
        editor.nrcs.item(row, 2).setText("31, 7F")
        self.assertEqual(window.plan().nrc_policy.accepted("did_not_in_session"), (0x31, 0x7F))
        editor.nrcs.item(row, 2).setText("zz")
        self.assertEqual(window.plan().nrc_policy.accepted("did_not_in_session"), (0x31, 0x7F), "a typo is not taken")
        bench = Bench(self, EcuConfig(broadcast_interval=0, forced_nrcs=[{"sid": 0x2E, "nrc": 0x7F}]))
        self.assertTrue(window.connect_ecu(can.Bus(interface="virtual", channel=bench.channel)))
        self.addCleanup(window.disconnect_ecu)
        name = PolicyTest.READ_ONLY[0]
        self.assertIsNotNone(window.run([name]))
        self.assertTrue(spin_until(lambda: window.report is not None and window.run_btn.isEnabled()))
        self.assertEqual(window.report.cases[0].verdict, "failed")
        item = window._items[name]
        step = item.child(0)
        self.assertEqual(step.text(1), "fail")
        menu = window.sequence_menu(step)
        self.assertEqual([action.text() for action in menu.actions()], ["Accept this deviation..."])
        test, description = step.data(0, Qt.UserRole)[1:]
        window.accept_deviation(test, description, step, comment="ticket 42")
        self.assertEqual(step.text(1), "accepted")
        self.assertEqual(editor.deviations()[0].comment, "ticket 42")
        self.assertEqual(editor.table.item(0, 0).text(), "Data identifiers: Writing a read-only DID")
        self.assertEqual([action.text() for action in window.sequence_menu(step).actions()],
                         ["No longer accept this deviation"])
        window.report = None
        window.run([name])
        self.assertTrue(spin_until(lambda: window.report is not None and window.run_btn.isEnabled()))
        self.assertEqual(window.report.cases[0].verdict, "failed", "only the first DID's failure is accepted")
        self.assertEqual(window._items[name].child(0).text(1), "accepted")
        self.assertEqual(window._items[name].child(1).text(1), "fail")
        before = window.tree.topLevelItem(0)
        self.assertEqual(before.text(0), "Before the tests", "the run's own steps")
        self.assertIn("P2 50 ms, as the ECU announces", [before.child(i).text(0) for i in range(before.childCount())])
        self.assertEqual([action.text() for action in window.sequence_menu(before).actions()], ["Before the run"])
        item = window._items[name]
        test_menu = [action.text() for action in window.sequence_menu(item).actions()]
        self.assertIn("Accept every failure of this test...", test_menu)
        path = self.folder / "policy.json"
        window.save_plan_as(path)
        saved = TestPlan.load(path)
        self.assertEqual(saved.nrc_policy.accepted("did_not_in_session"), (0x31, 0x7F))
        self.assertEqual([deviation.comment for deviation in saved.deviations], ["ticket 42"])
        editor.remove(test, description)
        self.assertEqual(window.plan().deviations, [])

    def test_discovery_in_the_window(self):
        window = self.window
        bench = Bench(self)
        self.assertIsNone(window.discover(DiscoveryTest.OPTIONS), "not connected")
        self.assertTrue(window.connect_ecu(can.Bus(interface="virtual", channel=bench.channel)))
        self.addCleanup(window.disconnect_ecu)
        window.description.dids[0xF190].length = 16
        self.assertIsNotNone(window.discover(DiscoveryTest.OPTIONS))
        self.assertTrue(spin_until(lambda: window.discovery_result is not None and window.run_btn.isEnabled()))
        self.assertIn("1 difference with the description", window.status.text())
        self.assertIn("the ECU answers 17 bytes", window.discovery_view.browser.toPlainText())
        self.assertEqual(window.plan().discovery, DiscoveryTest.OPTIONS, "kept in the plan")
        page = window.save_discovery(self.folder / "discovery.html")
        self.assertTrue(page.exists() and page.with_suffix(".json").exists())
        found = window.use_discovered(self.folder / "found.json")
        self.assertEqual(found.name, "Dummy ECU (discovered)")
        self.assertEqual(window.description.dids[0xF190].length, 17)
        self.assertEqual(window.plan().description, str((self.folder / "found.json").resolve()))

    def test_main_smoke_test(self):
        self.assertEqual(window_module.main(["--smoke-test"]), 0)
        self.assertEqual(window_module.main([str(DUMMY_CDD), "--smoke-test"]), 0)
        path = self.folder / "plan.json"
        TestPlan(description=str(DUMMY_CDD)).save(path)
        self.assertEqual(window_module.main([str(path), "--smoke-test"]), 0)


if __name__ == "__main__":
    unittest.main()
