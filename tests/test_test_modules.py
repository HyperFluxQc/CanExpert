"""Test modules: test cases in Python run against the bus - verdicts per step, setup and teardown, stopping -
their HTML and JUnit reports, the Test window running the example module against the Dummy ECU, and its place
in the main window."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import queue
import shutil
import tempfile
import threading
import time
import unittest
import uuid
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from unittest.mock import patch

import can
import cantools
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from canexpert import can_bus
from canexpert import main_window as main
from canexpert.can_bus import CanWorker
from canexpert.config import validate_config
from canexpert.paths import DBC_DIR, TEST_MODULES_DIR
from canexpert.simulator.ecu import DummyEcu, EcuConfig
from canexpert.testing.report import html_report, junit_report, save_reports
from canexpert.testing.runner import CaseResult, Runner, TestContext, call_hook, load_module, run_module, uds_names
from canexpert.testing.window import MemorySettings, TestWindow

APP = QApplication.instance() or QApplication([])

MODULE = '''"""Bench checks

What the runner does with each outcome.
"""
events = []

def setup(t):
    events.append("setup")
    t.log("setting up")

def before_each(t):
    events.append("before")

def after_each(t):
    events.append("after")

def teardown(t):
    events.append("teardown")

@testcase("Everything passes")
def passes(t):
    """Two checks that hold."""
    t.check(True, "first")
    t.check_equal(2 + 2, 4, "arithmetic")

@testcase("A check fails and the case goes on")
def check_fails(t):
    t.check(False, "this fails", "why it failed")
    t.check(True, "this still runs")

@testcase("A requirement ends the case")
def require_stops(t):
    t.require(0, "needed <first>")
    t.check(True, "never reached")

@testcase("An exception is an error")
def raises(t):
    {}["missing"]

@testcase("Skipped on purpose")
def skipped(t):
    t.skip("no hardware for this")

@testcase
def uds_answers(t):
    t.check(RDBI(0xF190), "VIN read")
    t.expect_nrc(RDBI(0x1234), 0x31)
    t.check_range(5, 1, 3, "out of range")
'''


def fake_ecu(payload, timeout=None, wait=True, pending=None):
    """Answers 22 F1 90 with a VIN and everything else with requestOutOfRange."""
    payload = bytes(payload)
    if payload == b"\x22\xf1\x90":
        return b"\x62\xf1\x90" + b"WVWZZZ1KZAW000001"
    return bytes([0x7F, payload[0], 0x31])


def spin_until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


def write_module(test, text=MODULE, name="bench_checks.py"):
    folder = Path(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, folder, True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.path = write_module(self)
        self.module = load_module(self.path, uds_names())

    def test_a_module_is_read_as_written(self):
        self.assertEqual(self.module.title, "Bench checks")
        self.assertEqual([case.name for case in self.module.cases],
                         ["passes", "check_fails", "require_stops", "raises", "skipped", "uds_answers"])
        self.assertEqual(self.module.cases[-1].title, "uds answers", "a bare @testcase takes the function's name")
        self.assertEqual(self.module.cases[0].doc, "Two checks that hold.")
        self.assertEqual(set(self.module.hooks), {"setup", "teardown", "before_each", "after_each"})

    def test_each_outcome_has_its_verdict(self):
        events = []
        report = Runner(self.module, fake_ecu, on_event=lambda kind, data: events.append(kind)).run()
        verdicts = {case.name: case.verdict for case in report.cases}
        self.assertEqual(verdicts, {"passes": "passed", "check_fails": "failed", "require_stops": "failed",
                                    "raises": "error", "skipped": "skipped", "uds_answers": "failed"})
        cases = {case.name: case for case in report.cases}
        self.assertEqual([step.description for step in cases["check_fails"].steps], ["this fails", "this still runs"])
        self.assertEqual(cases["check_fails"].steps[0].detail, "why it failed")
        self.assertEqual([step.description for step in cases["require_stops"].steps], ["needed <first>"])
        self.assertIn("KeyError: 'missing'", cases["raises"].error)
        self.assertEqual(cases["skipped"].error, "no hardware for this")
        uds = cases["uds_answers"].steps
        self.assertEqual([step.verdict for step in uds], ["pass", "pass", "fail"])
        self.assertIn("62 F1 90", uds[0].detail.upper(), "a UdsResult shows what was sent and answered")
        self.assertIn("expected 1 to 3, got 5", uds[2].detail)
        self.assertEqual(report.setup.verdict, "passed")
        self.assertEqual(report.setup.steps[0].verdict, "info")
        self.assertEqual(report.verdict, "failed")
        self.assertEqual(report.counts(), {"passed": 1, "failed": 3, "error": 1, "skipped": 1, "blocked": 0})
        hooks = self.module.namespace["events"]
        self.assertEqual(hooks[0], "setup")
        self.assertEqual(hooks[-1], "teardown")
        self.assertEqual(hooks.count("before"), 6)
        self.assertEqual(hooks.count("after"), 6, "after_each also after a failure, an error or a skip")
        self.assertEqual(events.count("case"), 6)
        self.assertEqual(events.count("verdict"), 6)

    def test_only_the_chosen_cases_run(self):
        report = Runner(self.module, fake_ecu).run(["passes", "uds_answers"])
        self.assertEqual([case.name for case in report.cases], ["passes", "uds_answers"])

    def test_a_failing_setup_skips_every_case_but_teardown_runs(self):
        path = write_module(self, MODULE.replace('t.log("setting up")', 't.require(False, "the ECU is there")'))
        report = run_module(path, fake_ecu)
        self.assertEqual(report.setup.verdict, "failed")
        self.assertEqual({case.verdict for case in report.cases}, {"skipped"})
        self.assertEqual({case.error for case in report.cases}, {"setup failed"})
        self.assertEqual(report.teardown.verdict, "passed")
        self.assertEqual(report.verdict, "failed")

    def test_stopping_skips_the_rest_and_still_tears_down(self):
        text = MODULE.replace('    t.check(True, "first")', '    t.wait(5)\n    t.check(True, "first")')
        module = load_module(write_module(self, text), uds_names())
        runner = Runner(module, fake_ecu)
        threading.Timer(0.2, runner.stop).start()
        started = time.monotonic()
        report = runner.run()
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(report.stopped)
        self.assertEqual({case.verdict for case in report.cases}, {"skipped"})
        self.assertEqual(report.teardown.verdict, "passed")
        self.assertEqual(module.namespace["events"][-2:], ["after", "teardown"])

    def test_frames_and_signals_are_waited_for(self):
        frames = queue.Queue()
        path = write_module(self, '''
@testcase
def frames(t):
    t.send(0x123, [1, 2])
    frame = t.wait_for_frame(0x300, timeout=1)
    t.check_equal(frame.data, bytes([0x10, 0x20]), "the answer, though it came before the wait")
    t.send(0x124, [3])
    t.check_equal(t.wait_for_signal("EngineData.Temperature", lambda value: value > 50, timeout=1), 64,
                  "the first temperature above 50")
    t.check(t.wait_for_frame(0x999, timeout=0.1) is None, "nothing on 0x999")
''')
        sent = []
        answers = [(0x10,), (0x20, 0x40)]

        def send(message):
            sent.append(message)
            frames.put((time.monotonic() - 1, can.Message(arbitration_id=0x300, data=[0x77, 0x20])))  # too early
            for value in answers.pop(0):                          # what the bus answers, at once
                frames.put((time.monotonic(), can.Message(arbitration_id=0x300, data=[value, 0x20])))

        def decode(can_id, data):
            return ("EngineData", {"Temperature": data[0]}) if can_id == 0x300 else ("", {})
        report = run_module(path, fake_ecu, frames, send, decode)
        (case,) = report.cases
        self.assertEqual(case.verdict, "passed", case.steps)
        self.assertEqual((sent[0].arbitration_id, bytes(sent[0].data), sent[0].is_extended_id), (0x123, b"\x01\x02", False))

    def test_blocked_warned_and_accepted(self):
        path = write_module(self, '''
def before_each(t):
    if t.result.name == "needs_power":
        t.check(False, "power supply on")
        t.block("the power supply did not answer")

def after_each(t):
    if t.result.name == "cleans_up":
        t.warn("the ECU reset was not answered", "11 01 -> no answer")

@testcase
def needs_power(t):
    t.check(True, "never reached")

@testcase
def cleans_up(t):
    t.check(True, "done")

@testcase
def known_deviation(t):
    t.check(False, "NRC 0x31 expected", "7F 22 7F")
    t.check(False, "something else")
''')
        module = load_module(path, uds_names())
        accepted = {("known_deviation", "NRC 0x31 expected"): "the supplier answers 0x7F here (ticket 42)"}
        report = Runner(module, fake_ecu, accept=lambda name, step: accepted.get((name, step))).run()
        cases = {case.name: case for case in report.cases}
        self.assertEqual(cases["needs_power"].verdict, "blocked")
        self.assertEqual(cases["needs_power"].error, "the power supply did not answer")
        self.assertEqual([step.description for step in cases["needs_power"].steps], ["power supply on"])
        self.assertEqual(cases["cleans_up"].verdict, "passed", "a warning does not fail the case")
        self.assertEqual([step.verdict for step in cases["cleans_up"].steps], ["pass", "warn"])
        deviation = cases["known_deviation"]
        self.assertEqual([step.verdict for step in deviation.steps], ["accepted", "fail"])
        self.assertIn("accepted deviation: the supplier answers 0x7F here", deviation.steps[0].detail)
        self.assertEqual(deviation.verdict, "failed", "the other failure still counts")
        self.assertEqual(report.counts()["blocked"], 1)
        self.assertEqual(report.verdict, "failed", "a blocked case fails the run")
        page = html_report(report)
        self.assertIn("1 blocked", page)
        self.assertIn("1 with a warning", page)
        self.assertIn("1 accepted", page)
        root = ElementTree.fromstring(junit_report(report))
        blocked = next(case for case in root.iter("testcase") if case.get("name") == "needs power")
        self.assertEqual(blocked.find("error").get("type"), "Blocked")

    def test_a_hook_run_with_another_cases_context(self):
        result = CaseResult("case", "A case")
        t = TestContext(result, None, None, None, threading.Event())
        self.assertEqual(call_hook(lambda t: t.check(True, "fine"), t), ("passed", ""))
        self.assertEqual(call_hook(lambda t: t.check(False, "not fine"), t), ("failed", "a step failed"))
        self.assertEqual(call_hook(lambda t: t.require(False, "needed"), t), ("failed", "needed"))
        self.assertEqual(call_hook(lambda t: t.skip("not here"), t), ("skipped", "not here"))
        self.assertEqual(call_hook(lambda t: t.block("no power"), t), ("blocked", "no power"))
        verdict, why = call_hook(lambda t: {}["missing"], t)
        self.assertEqual(verdict, "error")
        self.assertIn("KeyError", why)
        with t.lenient():
            self.assertTrue(t.check(False, "a clean-up that did not work") is False, "the check still says so")
            verdict, _ = call_hook(lambda t: t.require(False, "required in a clean-up"), t)
        self.assertEqual(verdict, "failed", "require() still ends it")
        self.assertEqual([step.verdict for step in result.steps[-2:]], ["warn", "warn"], "recorded as warnings")
        self.assertFalse(t.check(False, "after it"))
        self.assertEqual(result.steps[-1].verdict, "fail")

    def test_without_a_bus_uds_calls_fail_as_errors(self):
        report = run_module(self.path, names=["uds_answers"])
        self.assertEqual(report.cases[0].verdict, "error")
        self.assertIn("connect first", report.cases[0].error)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.path = write_module(self)
        self.report = run_module(self.path, fake_ecu, configuration="Bench")

    def test_html(self):
        page = html_report(self.report)
        self.assertIn("<h1>Bench checks</h1>", page)
        self.assertIn("1 passed, 3 failed, 1 with\nan error, 1 skipped", page)
        self.assertIn("needed &lt;first&gt;", page, "the steps' text is escaped")
        self.assertIn("KeyError", page)
        self.assertIn("<td>Bench</td>", page)

    def test_junit(self):
        root = ElementTree.fromstring(junit_report(self.report))
        suite = root.find("testsuite")
        self.assertEqual({key: suite.get(key) for key in ("name", "tests", "failures", "errors", "skipped")},
                         {"name": "Bench checks", "tests": "6", "failures": "3", "errors": "1", "skipped": "1"})
        cases = {case.get("name"): case for case in suite.findall("testcase")}
        self.assertEqual(cases["A check fails and the case goes on"].find("failure").get("message"),
                         "this fails: why it failed")
        self.assertIn("KeyError", cases["An exception is an error"].find("error").get("message"))
        self.assertEqual(cases["Skipped on purpose"].find("skipped").get("message"), "no hardware for this")
        self.assertIsNone(cases["Everything passes"].find("failure"))
        self.assertIn("PASS first", cases["Everything passes"].find("system-out").text)
        properties = {item.get("name"): item.get("value") for item in suite.find("properties")}
        self.assertEqual(properties["configuration"], "Bench")

    def test_both_are_saved_beside_each_other(self):
        folder = self.path.parent / "reports"
        html_path, xml_path = save_reports(self.report, folder)
        self.assertEqual(html_path.parent, folder)
        self.assertTrue(html_path.name.startswith("bench_checks_") and html_path.suffix == ".html")
        self.assertEqual(xml_path.with_suffix(".html"), html_path)
        ElementTree.parse(xml_path)


def symbol_decoder():
    database = cantools.database.load_file(str(DBC_DIR / "dummy_ecu.dbc"))

    def decode(can_id, data):
        try:
            message = database.get_message_by_frame_id(can_id)
        except KeyError:
            return "", {}
        return message.name, message.decode(bytes(data), decode_choices=False, allow_truncated=True)
    return decode


class ExampleAgainstTheEcuTest(unittest.TestCase):
    """The example module in TestModules/, run by the Test window against the Dummy ECU."""

    def setUp(self):
        channel = "tests-" + str(uuid.uuid4())
        self.bus = can.Bus(interface="virtual", channel=channel)
        ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.ecu = DummyEcu(ecu_bus, EcuConfig(), log=lambda text: None)
        stop = threading.Event()
        threading.Thread(target=self.ecu.serve, args=(stop,), daemon=True).start()
        self.config = validate_config({"name": "Dummy", "request_id": 0x7E0, "response_id": 0x7E8})
        self.worker = CanWorker(self.bus, self.config, tester_present=False)
        self.worker.start()
        self.module = write_module(self, (TEST_MODULES_DIR / "dummy_ecu_checks.py").read_text(encoding="utf-8"),
                                   "dummy_ecu_checks.py")        # a copy: the reports go beside it
        self.settings = MemorySettings({"tests/module": str(self.module)})
        self.window = TestWindow(session=lambda: (self.bus, self.worker, self.config), decode=symbol_decoder(),
                                 settings=self.settings)

        def close():
            self.window.stop()
            if self.window.thread is not None:
                self.window.thread.join(5)
            self.window.deleteLater()
            self.worker.stop()
            stop.set()
            time.sleep(.05)
            self.bus.shutdown()
            ecu_bus.shutdown()
        self.addCleanup(close)

    def run_and_wait(self):
        self.assertIsNotNone(self.window.run(), self.window.log.toPlainText())
        self.assertTrue(spin_until(lambda: self.window.report is not None and self.window.run_btn.isEnabled(), 20),
                        self.window.log.toPlainText())
        return self.window.report

    def test_every_example_case_passes_and_leaves_its_reports(self):
        self.assertEqual(self.window.module.path, self.module)
        self.assertEqual(len(self.window.ticked()), 5)
        report = self.run_and_wait()
        self.assertEqual(report.verdict, "passed", self.window.log.toPlainText())
        self.assertEqual(report.counts()["passed"], 5)
        engine = next(case for case in report.cases if case.name == "engine_data")
        self.assertIn("the temperature is between -40 and 150 degC", [step.description for step in engine.steps])
        html_path, xml_path = self.window.report_paths
        self.assertEqual(html_path.parent, self.module.parent / "reports")
        self.assertTrue(xml_path.exists())
        self.assertIn("PASSED: 5 passed", self.window.status.text())
        item = self.window._items["identification"]
        self.assertEqual(item.text(1), "passed")
        self.assertEqual(item.childCount(), 4, "one line per step")

    def test_only_the_ticked_cases_run_and_a_failure_is_shown(self):
        for name, item in self.window._items.items():
            item.setCheckState(0, 2 if name == "identification" else 0)
        self.ecu.dids[0xF190] = b"SHORT-VIN"
        report = self.run_and_wait()
        self.assertEqual([case.name for case in report.cases], ["identification"])
        self.assertEqual(report.cases[0].verdict, "failed")
        self.assertIn("expected 17, got 9", self.window.log.toPlainText())
        self.assertTrue(self.window._items["identification"].isExpanded())

    def test_without_a_measurement_nothing_runs(self):
        window = TestWindow(settings=MemorySettings({"tests/module": str(self.module)}))
        self.addCleanup(window.deleteLater)
        self.assertIsNone(window.run())
        self.assertIn("connect first", window.log.toPlainText())

    def test_a_module_that_does_not_load_is_reported(self):
        broken = write_module(self, "def oops(:\n", "broken.py")
        self.assertIsNone(self.window.open_module(broken))
        self.assertIn("could not be read: SyntaxError", self.window.log.toPlainText())
        self.assertEqual(self.window.module.path, self.module, "the module before stays")


class MainWindowTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "Configurations").mkdir()
        (root / "Databases").mkdir()
        (root / "Configurations" / "config_Bench.json").write_text(json.dumps(
            {"name": "Bench", "request_id": 0x7E0, "response_id": 0x7E8}))
        settings = QSettings(str(root / "settings.ini"), QSettings.IniFormat)
        patches = [patch.object(main, "CONFIG_DIR", root / "Configurations"),
                   patch.object(main, "DATABASES_DIR", root / "Databases"),
                   patch.object(main, "app_settings", lambda: settings),
                   patch.object(main.can, "detect_available_configs", return_value=[]),
                   patch.object(can_bus, "create_can_bus", lambda *a, **k: can.Bus(interface="virtual",
                                                                                  channel="tests-main"))]
        for item in patches:
            item.start()
        self.window = main.MainWindow()

        def close():
            self.window.close()
            APP.processEvents()
            for item in reversed(patches):
                item.stop()
        self.addCleanup(close)

    def test_the_test_window_is_a_tool_window(self):
        action = self.window._toolbar_actions["tests"]
        self.assertEqual(action.shortcut().toString(), "Ctrl+8")
        action.trigger()
        tests = self.window.tool_widget("tests")
        self.assertIsInstance(tests, TestWindow)
        self.assertTrue(action.isChecked())
        self.assertEqual(self.window.help_section(tests.tree), "Test modules")
        self.assertIsNone(tests.run())
        self.assertIn("connect first", tests.log.toPlainText())
        name, signals = tests.decode(0x999, b"\x00")
        self.assertEqual((name, signals), ("", {}))


if __name__ == "__main__":
    unittest.main()
