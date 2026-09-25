"""
TestExpert's command line: open the window (with a plan or a description), or run a plan without it -
for a bench script or a CI server, which read the exit code and the JUnit report.

    python test_expert.py                                    the window
    python test_expert.py ODX/ecu.cdd                        the window, with a description
    python test_expert.py nightly.json --run                 run a plan; exit code 0 passed, 1 failed, 2 could not run
    python test_expert.py nightly.json --run --junit results.xml --report-dir reports
    python test_expert.py nightly.json --run --channel 1     the plan, on another channel
    python test_expert.py ecu.pdx --run --identify           ask the ECU which of the file's variants it is, test that one
    python test_expert.py ecu.cdd --run --variant BOOT       the file's variant BOOT
    python test_expert.py ecu.cdd --run --module checks.py   a CAN Expert test module too, after the generated tests
    python test_expert.py nightly.json --run --test sessions --repeat 20 --until-failure
                                                             one group, twenty times or until a run fails
    python test_expert.py --run --dummy-ecu                  the built-in description against a Dummy ECU in this process
    python test_expert.py nightly.json --discover            ask the ECU what it has; exit code 0 when it matches
                                                             the description, 1 when it does not
    python test_expert.py ecu.cdd --discover --save-description found.json --dids F100-F1FF
    python test_expert.py --compare before.json after.json  two runs' results; exit code 1 when a test regressed
"""
from __future__ import annotations

import argparse
import shutil
import sys
import threading
import uuid
from pathlib import Path

EXIT_PASSED, EXIT_FAILED, EXIT_NOT_RUN = 0, 1, 2


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="test_expert", description="TestExpert: UDS conformance tests generated from a CDD, ODX or PDX file")
    parser.add_argument("file", nargs="?", help="a test plan (.json), or a description (.cdd, .odx, .pdx, .json)")
    parser.add_argument("--run", action="store_true",
                        help="run without the window; exit code 0 when every test passed, 1 when one did not, "
                             "2 when the run could not start")
    parser.add_argument("--dummy-ecu", action="store_true",
                        help="with --run: test a Dummy ECU started in this process on a virtual bus")
    parser.add_argument("--report-dir", help="with --run: the folder for the reports (default: the plan's)")
    parser.add_argument("--junit", help="with --run: also write the JUnit XML report to this file")
    parser.add_argument("--interface", help="with --run: the interface, instead of the plan's")
    parser.add_argument("--channel", help="with --run: the channel, instead of the plan's")
    parser.add_argument("--bitrate", type=int, help="with --run: the bit rate, instead of the plan's")
    parser.add_argument("--quiet", action="store_true", help="with --run: print the summary only")
    parser.add_argument("--module", action="append", default=[], metavar="FILE",
                        help="with --run: a CAN Expert test module to run too, after the generated tests (repeatable)")
    parser.add_argument("--symbols", action="append", default=[], metavar="FILE",
                        help="with --run: a symbol database (DBC...) the modules' frames are decoded with (repeatable)")
    parser.add_argument("--test", action="append", default=[], metavar="NAME",
                        help="with --run: only this test (sessions.default_session_10_01) or group (sessions), "
                             "left out by the plan or not; repeatable")
    parser.add_argument("--repeat", type=int, metavar="N", help="with --run: run the tests N times, one run after "
                                                                  "the other (each with its reports)")
    parser.add_argument("--until-failure", action="store_true",
                        help="with --repeat: stop after the first run that fails")
    parser.add_argument("--variant", help="the variant of the description to test (a CDD's VAR, an ODX variant)")
    parser.add_argument("--identify", action="store_true",
                        help="with --run or --discover: ask the ECU which of the description's variants it is, and "
                             "test that one (an ODX file's ECU-VARIANT-PATTERNs, else the plan's DID); exit code 2 "
                             "when it cannot be told")
    parser.add_argument("--discover", action="store_true",
                        help="ask the ECU what it has and compare it with the description, without the window; exit "
                             "code 0 when they agree, 1 when they differ, 2 when it could not run")
    parser.add_argument("--dids", help="with --discover: the DID ranges to read, e.g. 0100-02FF,F100-F2FF")
    parser.add_argument("--rids", help="with --discover: the routine ranges whose results are asked")
    parser.add_argument("--save-description", help="with --discover: what was found, as a JSON description")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                        help="compare two runs' results (the .json beside their reports); exit code 1 when a test "
                             "that passed before does not now")
    parser.add_argument("--output", help="with --compare: the comparison as an HTML page")
    parser.add_argument("--smoke-test", action="store_true", help="build the window and exit (the Windows build)")
    return parser


def main(argv=None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.compare:
        _console()
        return compare(arguments)
    if arguments.run or arguments.discover:
        _console()
        return discover(arguments) if arguments.discover else run(arguments)
    from canexpert.test_expert.window import gui
    return gui(arguments)


def _console():
    """TestExpert.exe is a windowed program: with --run, print into the console it was started from."""
    if sys.stdout is not None or sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):          # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
    except (OSError, AttributeError):
        pass


def _say(text=""):
    if sys.stdout is not None:
        print(text, flush=True)


def load_plan(arguments):
    """The plan to run: the file given (a plan, or a description in a plan of the defaults), with the command
    line's connection."""
    from canexpert.test_expert.plan import TestPlan, is_plan_file
    if arguments.file and is_plan_file(arguments.file):
        plan = TestPlan.load(arguments.file)
    else:
        plan = TestPlan()
        if arguments.file:
            plan.description = str(Path(arguments.file).resolve())
    plan.modules += [str(Path(module).resolve()) for module in getattr(arguments, "module", None) or ()]
    plan.symbols += [str(Path(database).resolve()) for database in getattr(arguments, "symbols", None) or ()]
    if getattr(arguments, "variant", None):
        plan.variant = arguments.variant
    if getattr(arguments, "identify", False):
        plan.identify = True
    connection = plan.connection
    if arguments.interface:
        connection.interface = arguments.interface
    if arguments.channel is not None:
        connection.channel = arguments.channel
    if arguments.bitrate:
        connection.bitrate = arguments.bitrate
    return plan.check()


class DummyBench:
    """A Dummy ECU in this process, answering on a virtual bus with the plan's identifiers."""

    def __init__(self, connection):
        import can

        from canexpert.simulator.ecu import DummyEcu, EcuConfig
        channel = f"test_expert-{uuid.uuid4()}"
        self.ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.tester_bus = can.Bus(interface="virtual", channel=channel)
        config = EcuConfig(request_id=connection.request_id, response_id=connection.response_id,
                           functional_id=connection.functional_id if connection.functional_id is not None else 0x7DF,
                           extended_ids=connection.extended)
        self.ecu = DummyEcu(self.ecu_bus, config, log=lambda text: None)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self.ecu.serve, args=(self._stop,), daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(2)
        for bus in (self.tester_bus, self.ecu_bus):
            bus.shutdown()


def _open_bus(arguments, plan):
    """(bus, Dummy ECU bench or None) for the plan's connection; OSError-like errors propagate."""
    from canexpert.can_bus import create_can_bus
    from canexpert.simulator.ecu import parse_channel
    if arguments.dummy_ecu:
        bench = DummyBench(plan.connection)
        return bench.tester_bus, bench
    connection = plan.connection
    return create_can_bus(connection.interface, parse_channel(connection.channel), connection.bitrate), None


def selected(tests, cases) -> list[str]:
    """The tests named on the command line: a test's name, or a group's - the first part of its tests' names.
    PlanError for a name no test has."""
    from canexpert.test_expert.plan import PlanError
    chosen = []
    for wanted in tests:
        wanted = str(wanted).strip().rstrip(".")
        found = [case.name for case in cases if case.name == wanted or case.name.startswith(wanted + ".")]
        if not found:
            raise PlanError(f"no test or group is named {wanted!r}")
        chosen += [name for name in found if name not in chosen]
    return chosen


def _identified(plan, bus, description):
    """The description of the variant the ECU says it is, when the plan says to ask it (else the one read);
    PlanError when it cannot be told."""
    if not plan.identify or len(description.variants) < 2:
        return description
    from canexpert.test_expert.plan import PlanError
    from canexpert.test_expert.tester import Tester
    from canexpert.test_expert.variants import identify
    tester = Tester(bus, plan.connection.transport(), plan.connection.functional_id)
    variant, detail = identify(plan.resolve(plan.description), tester, plan.identification)
    if variant is None:
        raise PlanError(f"the ECU's variant could not be told: {detail}")
    _say(f"TestExpert: the ECU is the variant {variant} ({detail})")
    plan.variant = variant
    return plan.load_description()


def compare(arguments) -> int:
    from canexpert.test_expert.compare import ResultsError, compare_runs, comparison_page, load_results
    try:
        before, after = (load_results(path) for path in arguments.compare)
    except ResultsError as exc:
        _say(f"TestExpert: {exc}")
        return EXIT_NOT_RUN
    comparison = compare_runs(before, after)
    for did, name, old, new in comparison.identification:
        _say(f"  {did} {name}: {old or '-'} -> {new or '-'}")
    for change in comparison.changes:
        _say(f"  {change.kind.upper():10} {change.title}: {change.before or '-'} -> {change.after or '-'}")
    _say(comparison.summary())
    if arguments.output:
        Path(arguments.output).write_text(comparison_page(comparison), encoding="utf-8")
        _say(f"  {arguments.output}")
    return EXIT_FAILED if comparison.regressions else EXIT_PASSED


def discover(arguments) -> int:
    from datetime import datetime

    from canexpert.test_expert.discovery import Discovery, compare, discovery_page, parse_ranges
    from canexpert.test_expert.plan import PlanError
    from canexpert.test_expert.tester import Tester
    from canexpert.test_expert.window import TEST_EXPERT_DIR
    try:
        plan = load_plan(arguments)
        description = plan.load_description()
        options = plan.discovery
        if arguments.dids:
            parse_ranges(arguments.dids)
            options.dids = arguments.dids
        if arguments.rids:
            parse_ranges(arguments.rids)
            options.rids = arguments.rids
    except (PlanError, OSError, ValueError) as exc:
        _say(f"TestExpert: {exc}")
        return EXIT_NOT_RUN
    try:
        bus, bench = _open_bus(arguments, plan)
    except Exception as exc:                             # the adapter's own errors, whatever the driver
        _say(f"TestExpert: cannot open {plan.connection.interface} {plan.connection.channel}: {exc}")
        return EXIT_NOT_RUN
    try:
        try:
            description = _identified(plan, bus, description)
        except (PlanError, OSError, ValueError) as exc:
            _say(f"TestExpert: {exc}")
            return EXIT_NOT_RUN
        _say(f"TestExpert: discovering, against {description.name} - sessions "
             f"{', '.join(f'{s:02X}' for s in options.sessions)}, DIDs {options.dids}, routines {options.rids}")
        tester = Tester(bus, plan.connection.transport(), plan.connection.functional_id)
        result = Discovery(tester, description, options).run()
    finally:
        if bench is not None:
            bench.close()
        else:
            bus.shutdown()
    findings = compare(result, description)
    for note in result.notes:
        _say(f"  Note: {note}")
    for finding in findings:
        _say(f"  {finding.kind.upper():12} {finding.what}: {finding.detail}")
    folder = Path(arguments.report_dir) if arguments.report_dir else plan.report_folder(TEST_EXPERT_DIR / "reports")
    folder.mkdir(parents=True, exist_ok=True)
    page = folder / f"discovery_{datetime.fromtimestamp(result.started):%Y%m%d-%H%M%S}.html"
    page.write_text(discovery_page(result, description), encoding="utf-8")
    import json
    page.with_suffix(".json").write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    _say(f"{result.probes} requests in {result.duration:.1f} s: {len(findings)} difference"
         f"{'s' if len(findings) != 1 else ''} with the description")
    _say(f"  {page}")
    if arguments.save_description:
        found = result.to_description(f"{description.name} (discovered)", description)
        found.save(arguments.save_description)
        _say(f"  {arguments.save_description}")
    return EXIT_PASSED if not findings else EXIT_FAILED


def run(arguments) -> int:
    from canexpert.recording import Recorder
    from canexpert.test_expert.engine import PlanRun, RecordingBus
    from canexpert.test_expert.plan import PlanError
    from canexpert.test_expert.window import TEST_EXPERT_DIR
    from canexpert.testing.report import report_stem, summary_text
    try:
        plan = load_plan(arguments)
        description = plan.load_description()
    except (PlanError, OSError, ValueError) as exc:
        _say(f"TestExpert: {exc}")
        return EXIT_NOT_RUN
    bench = bus = recorder = None
    try:
        bus, bench = _open_bus(arguments, plan)
    except Exception as exc:                             # the adapter's own errors, whatever the driver
        _say(f"TestExpert: cannot open {plan.connection.interface} {plan.connection.channel}: {exc}")
        return EXIT_NOT_RUN
    folder = Path(arguments.report_dir) if arguments.report_dir else plan.report_folder(TEST_EXPERT_DIR / "reports")

    def on_event(kind, data):
        if kind == "verdict" and not arguments.quiet:
            _say(f"  {data.verdict.upper():8} {data.title}" + (f"  ({data.error.strip().splitlines()[-1]})"
                                                               if data.error.strip() and data.verdict != "passed" else ""))
    try:
        try:
            description = _identified(plan, bus, description)
        except (PlanError, OSError, ValueError) as exc:
            _say(f"TestExpert: {exc}")
            return EXIT_NOT_RUN
        _say(f"TestExpert: {description.name} - {description.summary()}")
        target = "a Dummy ECU (in this process)" if bench else plan.connection.text()
        tester_bus = bus
        runs = max(1, getattr(arguments, "repeat", None) or plan.repeat)
        until_failure = getattr(arguments, "until_failure", False) or plan.until_failure
        verdicts, junit = [], None
        try:
            if plan.record:
                folder.mkdir(parents=True, exist_ok=True)
                recorder = Recorder(folder / f"traffic_{uuid.uuid4().hex[:8]}.blf")
                tester_bus = RecordingBus(bus, recorder)
            names = None
            if getattr(arguments, "test", None):
                try:
                    names = selected(arguments.test, PlanRun(plan, description, tester_bus).suite.cases)
                except PlanError as exc:
                    _say(f"TestExpert: {exc}")
                    return EXIT_NOT_RUN
            for index in range(1, runs + 1):
                run_ = PlanRun(plan, description, tester_bus, names, on_event=on_event)
                if not run_.names:
                    _say("TestExpert: no test to run")
                    return EXIT_NOT_RUN
                prefix = f"Run {index} of {runs}: " if runs > 1 else ""
                _say(f"{prefix}Running {len(run_.names)} tests against {target}")
                report = run_.run()
                paths = run_.save(folder, f"_run{index}" if runs > 1 else "")
                _say(prefix + summary_text(report))
                for path in paths:
                    _say(f"  {path}")
                verdicts.append(report.verdict)
                if report.verdict != "passed" and junit is None:
                    junit = paths[1]                         # the first run that did not pass, for the CI server
                if report.stopped or (until_failure and report.verdict == "failed"):
                    break
        finally:
            if recorder is not None:
                recorder.stop()
        if recorder is not None:
            recording = folder / f"{report_stem(report)}.blf"
            try:
                Path(recorder.path).replace(recording)
            except OSError:
                recording = Path(recorder.path)
            _say(f"  {recording}")
        if arguments.junit:
            Path(arguments.junit).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(junit or paths[1], arguments.junit)
        if runs > 1:
            passed = verdicts.count("passed")
            _say(f"{len(verdicts)} run{'s' if len(verdicts) != 1 else ''} of {runs}: {passed} passed, "
                 f"{len(verdicts) - passed} did not")
        return EXIT_PASSED if all(verdict == "passed" for verdict in verdicts) else EXIT_FAILED
    finally:
        if bench is not None:
            bench.close()
        elif bus is not None:
            bus.shutdown()
