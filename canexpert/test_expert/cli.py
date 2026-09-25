"""
TestExpert's command line: open the window (with a plan or a description), or run a plan without it -
for a bench script or a CI server, which read the exit code and the JUnit report.

    python test_expert.py                                    the window
    python test_expert.py ODX/ecu.cdd                        the window, with a description
    python test_expert.py nightly.json --run                 run a plan; exit code 0 passed, 1 failed, 2 could not run
    python test_expert.py nightly.json --run --junit results.xml --report-dir reports
    python test_expert.py nightly.json --run --channel 1     the plan, on another channel
    python test_expert.py --run --dummy-ecu                  the built-in description against a Dummy ECU in this process
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
    parser.add_argument("--smoke-test", action="store_true", help="build the window and exit (the Windows build)")
    return parser


def main(argv=None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.run:
        _console()
        return run(arguments)
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
    connection = plan.connection
    if arguments.interface:
        connection.interface = arguments.interface
    if arguments.channel is not None:
        connection.channel = arguments.channel
    if arguments.bitrate:
        connection.bitrate = arguments.bitrate
    return plan


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


def run(arguments) -> int:
    from canexpert.can_bus import create_can_bus
    from canexpert.recording import Recorder
    from canexpert.simulator.ecu import parse_channel
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
        if arguments.dummy_ecu:
            bench = DummyBench(plan.connection)
            bus = bench.tester_bus
        else:
            connection = plan.connection
            bus = create_can_bus(connection.interface, parse_channel(connection.channel), connection.bitrate)
    except Exception as exc:                             # the adapter's own errors, whatever the driver
        _say(f"TestExpert: cannot open {plan.connection.interface} {plan.connection.channel}: {exc}")
        return EXIT_NOT_RUN
    folder = Path(arguments.report_dir) if arguments.report_dir else plan.report_folder(TEST_EXPERT_DIR / "reports")

    def on_event(kind, data):
        if kind == "verdict" and not arguments.quiet:
            _say(f"  {data.verdict.upper():8} {data.title}" + (f"  ({data.error.strip().splitlines()[-1]})"
                                                               if data.error.strip() and data.verdict != "passed" else ""))
    try:
        _say(f"TestExpert: {description.name} - {description.summary()}")
        target = "a Dummy ECU (in this process)" if bench else plan.connection.text()
        tester_bus = bus
        run_ = None
        try:
            if plan.record:
                folder.mkdir(parents=True, exist_ok=True)
                recorder = Recorder(folder / f"traffic_{uuid.uuid4().hex[:8]}.blf")
                tester_bus = RecordingBus(bus, recorder)
            run_ = PlanRun(plan, description, tester_bus, on_event=on_event)
            if not run_.names:
                _say("TestExpert: no test to run")
                return EXIT_NOT_RUN
            _say(f"Running {len(run_.names)} tests against {target}")
            report = run_.run()
        finally:
            if recorder is not None:
                recorder.stop()
        paths = run_.save(folder)
        if recorder is not None:
            recording = folder / f"{report_stem(report)}.blf"
            try:
                Path(recorder.path).replace(recording)
                paths.append(recording)
            except OSError:
                paths.append(Path(recorder.path))
        if arguments.junit:
            Path(arguments.junit).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(paths[1], arguments.junit)
        _say(summary_text(report))
        for path in paths:
            _say(f"  {path}")
        return EXIT_PASSED if report.verdict == "passed" else EXIT_FAILED
    finally:
        if bench is not None:
            bench.close()
        elif bus is not None:
            bus.shutdown()
