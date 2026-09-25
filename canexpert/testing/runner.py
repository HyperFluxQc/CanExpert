"""
Test modules, as CANoe has them, in Python: a file of test cases run against the live bus.

    \"\"\"Checks of the Dummy ECU.\"\"\"                # the module's title in the Test window and the report

    def setup(t):                                     # before the test cases; a failure blocks them
        t.require(DSC(0x03), "extended session")

    @testcase("The VIN has 17 characters")
    def vin(t):
        vin = RDBI(0xF190)
        t.require(vin, "RDBI F190 is answered")       # a failed require ends the test case
        t.check_equal(len(vin.data), 17, "length")    # a failed check fails it and goes on

    def teardown(t):                                  # after them, also when one failed
        DSC(0x01)

before_each(t) and after_each(t) run around every test case. The UDS service functions (RDBI, DSC, ...) are
there as in panel scripts. t is a TestContext: check, check_equal, expect_nrc, require, fail, skip, block, warn,
log, wait, send, wait_for_frame, wait_for_signal.

A test case whose preconditions could not be set up - t.block() in before_each - is blocked: not run, and
counted with the failures. t.warn() notes what went wrong without failing the case (a clean-up that did not
work). A Runner given accept(case name, step description) turns a failed step it knows into an accepted one.

run_module() runs the chosen test cases on the calling thread and returns a TestReport; report.py writes it
as HTML and JUnit XML. call_hook() runs a module's hook with another test case's t - TestExpert runs modules
among its own tests that way.
"""
from __future__ import annotations

import inspect
import queue
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import can

from canexpert.can_bus import ReceiveMailbox
from canexpert.j1939.transport import J1939Link
from canexpert.uds.client import UdsFunctions, UdsResult

PASS, FAIL, INFO, WARN, ACCEPTED = "pass", "fail", "info", "warn", "accepted"      # a step's verdict
PASSED, FAILED, ERROR, SKIPPED, BLOCKED = "passed", "failed", "error", "skipped", "blocked"   # a test case's
HOOKS = ("setup", "teardown", "before_each", "after_each")


class TestStopped(BaseException):
    """The run was stopped: no more steps, no more test cases (teardown still runs)."""


class _Abort(Exception):
    """A require() failed, or fail() was called: the test case ends here."""


class _Skip(Exception):
    """skip() was called."""


class _Block(Exception):
    """block() was called: what the test case needs could not be set up."""


@dataclass
class Step:
    time: float                 # seconds since the test case started
    description: str
    verdict: str                # pass, fail or info
    detail: str = ""


@dataclass
class CaseResult:
    name: str
    title: str
    verdict: str = PASSED
    steps: list = field(default_factory=list)
    duration: float = 0.0
    error: str = ""             # the traceback of an exception, or why the case was skipped

    def failures(self) -> list[Step]:
        return [step for step in self.steps if step.verdict == FAIL]

    def accepted(self) -> list[Step]:
        """The failed steps an accepted deviation covers."""
        return [step for step in self.steps if step.verdict == ACCEPTED]

    def warnings(self) -> list[Step]:
        return [step for step in self.steps if step.verdict == WARN]


@dataclass
class TestReport:
    title: str
    path: str
    started: float              # time.time()
    configuration: str = ""
    duration: float = 0.0
    setup: CaseResult | None = None
    teardown: CaseResult | None = None
    cases: list = field(default_factory=list)
    stopped: bool = False

    def counts(self) -> dict:
        counts = {PASSED: 0, FAILED: 0, ERROR: 0, SKIPPED: 0, BLOCKED: 0}
        for case in self.cases:
            counts[case.verdict] += 1
        return counts

    @property
    def verdict(self) -> str:
        """failed if anything failed, broke or was blocked (setup and teardown included), else passed."""
        results = self.cases + [hook for hook in (self.setup, self.teardown) if hook is not None]
        if any(result.verdict in (FAILED, ERROR, BLOCKED) for result in results):
            return FAILED
        return PASSED if any(case.verdict == PASSED for case in self.cases) else SKIPPED


@dataclass
class TestCase:
    name: str
    title: str
    function: object
    doc: str = ""


@dataclass
class TestModule:
    path: Path
    title: str
    cases: list
    hooks: dict
    namespace: dict


def testcase(title=None):
    """@testcase("What it checks") or @testcase: the function is a test case of the module."""
    def mark(function, title=title):
        function.__testcase__ = title if isinstance(title, str) and title else function.__name__.replace("_", " ")
        return function
    if callable(title):
        return mark(title, None)
    return mark


def load_module(path, extra_names=None) -> TestModule:
    """Read a test module: its test cases in the order they are written, its hooks, and its title (the
    docstring's first line, else the file name). extra_names go into its globals (the UDS functions)."""
    path = Path(path)
    source = path.read_text(encoding="utf-8-sig")
    code = compile(source, str(path), "exec")
    namespace = {"__file__": str(path), "__name__": f"canexpert_test_{path.stem}", "testcase": testcase,
                 **(extra_names or {})}
    exec(code, namespace)
    cases = [TestCase(name, value.__testcase__, value, inspect.getdoc(value) or "")
             for name, value in namespace.items() if callable(value) and hasattr(value, "__testcase__")]
    hooks = {name: namespace[name] for name in HOOKS if callable(namespace.get(name))}
    doc = (namespace.get("__doc__") or "").strip()
    title = doc.splitlines()[0].strip() if doc else path.stem.replace("_", " ")
    return TestModule(path, title, cases, hooks, namespace)


def uds_names() -> dict:
    """The UDS function names - and j1939 - a module may use, with placeholders, so it can be read without a
    bus."""
    def offline(*_args, **_kwargs):
        raise RuntimeError("No measurement is running: connect first")
    return {**UdsFunctions(offline).namespace(), "j1939": J1939Link(_Offline())}


class _Offline:
    """The bus of a module read without a measurement."""

    def send(self, _message):
        raise RuntimeError("No measurement is running: connect first")

    def recv(self, timeout=None):
        raise RuntimeError("No measurement is running: connect first")


class FrameMailbox(ReceiveMailbox):
    """The run's view of the bus: every frame with the moment it arrived (time.perf_counter()), for
    wait_for_frame(), whatever clock the adapter stamps its frames with."""

    def push(self, message):
        return super().push((time.perf_counter(), message))


class TestContext:
    """What a test case gets as t: steps with verdicts, waits, frames and signals."""

    def __init__(self, result: CaseResult, frames: queue.Queue | None, send, decode, stop: threading.Event,
                 report_step=None, marker=None, accept=None):
        self.result = result
        self._accept = accept           # accept(case name, step description) -> comment, or None: still a failure
        self._frames = frames           # (arrival time, frame) for every received frame: FrameMailbox.messages
        self._send = send               # send(can.Message)
        self._decode = decode           # decode(can_id, data) -> (message name, {signal name: value})
        self._stop = stop
        self._report_step = report_step or (lambda step: None)
        self._marker = marker or (lambda when, text: None)   # marker(when, comment): into the measurement
        self._start = time.perf_counter()
        self._since = None              # when the last send() went out: a wait after it takes the answer too
        self._lenient = 0               # inside lenient(): failed steps are recorded as warnings

    # --- steps --------------------------------------------------------------------------------

    def _step(self, description, verdict, detail=""):
        self._check_stop()
        description, detail = str(description), str(detail)
        if verdict == FAIL and self._accept is not None:
            comment = self._accept(self.result.name, description)
            if comment is not None:
                verdict = ACCEPTED
                detail = f"{detail} - accepted deviation" + (f": {comment}" if comment else "")
        passed = verdict != FAIL
        if not passed and self._lenient:
            verdict = WARN
        step = Step(round(time.perf_counter() - self._start, 4), description, verdict, detail)
        self.result.steps.append(step)
        self._report_step(step)
        return passed

    @contextmanager
    def lenient(self):
        """Meanwhile, failed steps are warnings: a clean-up that did not work does not fail the test case.
        require() still ends what it is in."""
        self._lenient += 1
        try:
            yield self
        finally:
            self._lenient -= 1

    def check(self, condition, description="check", detail=None) -> bool:
        """A step that passes when condition is true - a positive UdsResult is. Returns the verdict."""
        if detail is None and isinstance(condition, UdsResult):
            detail = repr(condition)
        return self._step(description, PASS if condition else FAIL, detail or "")

    def check_equal(self, actual, expected, description="values are equal") -> bool:
        return self._step(description, PASS if actual == expected else FAIL, f"expected {expected!r}, got {actual!r}")

    def check_range(self, value, low, high, description="value in range") -> bool:
        ok = value is not None and low <= value <= high
        return self._step(description, PASS if ok else FAIL, f"expected {low!r} to {high!r}, got {value!r}")

    def expect_nrc(self, result, nrc, description=None) -> bool:
        """A step that passes when the ECU answered with this negative response code."""
        description = description or f"NRC 0x{nrc:02X} expected"
        return self._step(description, PASS if getattr(result, "nrc", None) == nrc else FAIL, repr(result))

    def require(self, condition, description="requirement", detail=None):
        """check(), and a failure ends the test case (the rest would only fail because of it)."""
        if not self.check(condition, description, detail):
            raise _Abort(description)
        return condition

    def fail(self, description="failed", detail=""):
        self._step(description, FAIL, detail)
        raise _Abort(description)

    def skip(self, reason="skipped"):
        raise _Skip(reason)

    def block(self, reason="blocked"):
        """End the test case as blocked: what it needs could not be set up, so it is not run (before_each)."""
        raise _Block(reason)

    def warn(self, description, detail=""):
        """A step that did not go as it should, without failing the test case."""
        self._step(description, WARN, detail)

    def log(self, text):
        """A line in the report, without a verdict."""
        self._step(text, INFO)

    def marker(self, comment):
        """A marker in the measurement - the Trace, the Logger's graphs, the recording - and a line in the
        report, so the two can be read side by side."""
        self._marker(time.time(), str(comment))
        self._step(f"Marker: {comment}", INFO)

    # --- time and the bus -----------------------------------------------------------------------

    def _check_stop(self):
        if self._stop.is_set():
            raise TestStopped()

    def wait(self, seconds):
        deadline = time.perf_counter() + seconds
        while True:
            self._check_stop()
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.05))

    def send(self, can_id, data, extended=None):
        """Send one frame; an identifier above 0x7FF is extended unless said otherwise. The next wait takes
        the frames from this moment on, so an answer that comes before it starts is not missed."""
        extended = can_id > 0x7FF if extended is None else extended
        self._since = time.perf_counter()
        self._send(can.Message(arbitration_id=can_id, data=bytes(data), is_extended_id=extended))

    def wait_for_frame(self, can_id=None, timeout=1.0, condition=None):
        """The next frame (of can_id, and meeting condition(frame) if given) within timeout, as a Frame with
        id, data, message and signals; None if none came. "Next": received after this call, or after the
        test's last send() if that came just before."""
        if self._frames is None:
            raise RuntimeError("No measurement is running: connect first")
        since = self._since if self._since is not None else time.perf_counter()
        self._since = None
        deadline = time.perf_counter() + timeout
        while True:
            self._check_stop()
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            try:
                arrived, message = self._frames.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            if arrived < since or (can_id is not None and message.arbitration_id != can_id):
                continue
            data = bytes(message.data)
            frame = Frame(message.arbitration_id, data, *self._decode(message.arbitration_id, data))
            if condition is None or condition(frame):
                return frame

    def wait_for_signal(self, name, condition=None, timeout=1.0):
        """The value of the signal ("Message.Signal") in the next frame that carries it (and meets
        condition(value)) within timeout; None if none came. Decoded with the symbol databases."""
        message_name, _, signal_name = str(name).partition(".")
        found = {}

        def carries(frame):
            if signal_name not in frame.signals or (message_name and frame.message != message_name):
                return False
            value = frame.signals[signal_name]
            if condition is None or condition(value):
                found["value"] = value
                return True
            return False
        return found.get("value") if self.wait_for_frame(None, timeout, carries) is not None else None


class Frame:
    """A received frame in a test: id, data, and its message and signals as the symbol databases decode it."""
    __slots__ = ("id", "data", "message", "signals")

    def __init__(self, can_id, data, message="", signals=None):
        self.id, self.data, self.message, self.signals = can_id, data, message, dict(signals or {})

    def __repr__(self):
        return f"Frame(0x{self.id:X}, {self.data.hex(' ')})"


class Runner:
    """Runs a module's test cases, in order, on the calling thread.

    request: the UDS request function (payload, timeout, wait, pending) bound to the session's mailbox;
    frames: the queue of a FrameMailbox, (arrival time, frame); send(can.Message); decode(can_id, data) gives
    (message name, {signal: value}). on_event(kind, data) follows the run: ("case", CaseResult) when one
    starts, ("step", Step), ("verdict", CaseResult) when one ends.
    """

    def __init__(self, module: TestModule, request=None, frames=None, send=None, decode=None, timeout=None,
                 on_event=None, configuration="", marker=None, j1939=None, accept=None):
        self.module = module
        self.accept = accept                     # accept(case name, step description) -> comment or None
        self.frames, self.configuration = frames, configuration
        self.send = send or _not_connected
        self.decode = decode or (lambda can_id, data: ("", {}))
        self.on_event = on_event or (lambda kind, data: None)
        self.marker = marker                     # marker(when, comment), for t.marker()
        self.stop_event = threading.Event()
        if request is not None:
            functions = UdsFunctions(request, None, timeout)
            module.namespace.update(functions.namespace())      # the module's calls go to the bus now
        if j1939 is not None:
            module.namespace["j1939"] = j1939                   # a J1939Link on the run's mailbox

    def stop(self):
        self.stop_event.set()

    def _context(self, result):
        return TestContext(result, self.frames, self.send, self.decode, self.stop_event,
                           lambda step: self.on_event("step", step), self.marker, self.accept)

    def _call(self, function, result) -> None:
        """Run one function as (part of) result's body and set its verdict."""
        try:
            function(self._context(result))
            if result.failures():
                result.verdict = FAILED
        except _Abort:
            result.verdict = FAILED
        except _Skip as reason:
            result.verdict, result.error = SKIPPED, str(reason)
        except _Block as reason:
            result.verdict, result.error = BLOCKED, str(reason)
        except TestStopped:
            result.verdict, result.error = SKIPPED, "stopped"
            raise
        except Exception:
            result.verdict, result.error = ERROR, traceback.format_exc(limit=8)

    def _clean_up(self, function, result):
        """after_each and teardown run also when the run is being stopped: they put the ECU back."""
        stopping = self.stop_event.is_set()
        self.stop_event.clear()
        start = time.perf_counter()
        try:
            self._call(function, result)
        except TestStopped:
            pass
        finally:
            result.duration = round(result.duration + time.perf_counter() - start, 4)
            if stopping:
                self.stop_event.set()

    def run(self, names=None) -> TestReport:
        """Run the test cases named (all when None) with the hooks around them."""
        module = self.module
        chosen = [case for case in module.cases if names is None or case.name in names]
        report = TestReport(module.title, str(module.path), time.time(), self.configuration)
        started = time.perf_counter()
        blocked = ""
        try:
            if "setup" in module.hooks:
                report.setup = CaseResult("setup", "Setup")
                start = time.perf_counter()
                try:
                    self._call(module.hooks["setup"], report.setup)
                finally:
                    report.setup.duration = round(time.perf_counter() - start, 4)
                if report.setup.verdict != PASSED:
                    blocked = f"setup {report.setup.verdict}"
            for case in chosen:
                result = CaseResult(case.name, case.title)
                report.cases.append(result)
                self.on_event("case", result)
                if blocked:
                    result.verdict, result.error = SKIPPED, blocked
                else:
                    self._run_case(case, result)
                self.on_event("verdict", result)
        except TestStopped:
            report.stopped = True
            for case in chosen[len(report.cases):]:
                report.cases.append(CaseResult(case.name, case.title, SKIPPED, error="stopped"))
        finally:
            if "teardown" in module.hooks:
                report.teardown = CaseResult("teardown", "Teardown")
                self._clean_up(module.hooks["teardown"], report.teardown)
            report.duration = round(time.perf_counter() - started, 3)
        return report

    def _run_case(self, case, result):
        """before_each, the case, after_each; a failing before_each fails the case without running it (one
        that blocks it: blocked). The case keeps the worst verdict of the three."""
        hooks = self.module.hooks
        start = time.perf_counter()
        try:
            if "before_each" in hooks:
                self._call(hooks["before_each"], result)
            if result.verdict == PASSED:
                self._call(case.function, result)
        finally:
            result.duration = round(time.perf_counter() - start, 4)
            if "after_each" in hooks:
                verdict, error = result.verdict, result.error
                self._clean_up(hooks["after_each"], result)
                if verdict != PASSED:              # after_each passing does not undo the case's verdict
                    result.verdict, result.error = verdict, error or result.error


def _not_connected(_message):
    raise RuntimeError("No measurement is running: connect first")


def call_hook(function, t) -> tuple[str, str]:
    """Run function(t) - a module's setup, before_each... - with another test case's context, as a Runner runs
    its own: (verdict, why) - passed; failed (a failed step, require() or fail()), skipped, blocked, or error
    (the traceback). Stopping the run goes through."""
    failures = len(t.result.failures())
    try:
        function(t)
    except _Abort as reason:
        return FAILED, str(reason)
    except _Skip as reason:
        return SKIPPED, str(reason)
    except _Block as reason:
        return BLOCKED, str(reason)
    except TestStopped:
        raise
    except Exception:
        return ERROR, traceback.format_exc(limit=8)
    return (FAILED, "a step failed") if len(t.result.failures()) > failures else (PASSED, "")


def run_module(path, request=None, frames=None, send=None, decode=None, names=None, **options) -> TestReport:
    """Load and run a test module in one go (on the calling thread)."""
    module = load_module(path, uds_names())
    return Runner(module, request, frames, send, decode, **options).run(names)
