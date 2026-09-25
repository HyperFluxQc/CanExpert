"""
Pre-test and post-test sequences: steps TestExpert runs around the generated tests - before or after the whole
run, each group, chosen tests, or every test - such as a hard reset after a test that leaves the ECU changed, an
ignition frame and a wait before the run, or clearing the DTCs before the fault memory is read.

A sequence is a list of steps and where it is attached. A step that fails before a test blocks the test (it
is not run, and counts with the failures); before the run, it stops the run; after a test it is reported as a
warning and the test keeps its verdict. A sequence stops at its first step that fails.

    Request      "11 01"                     the answer expected: positive, NRC 22, any answer, no answer, not checked
    Wait         "2.5"                       seconds
    Keep alive   "10"                        seconds, with TesterPresent (3E 80) every 2 s so the session stays
    Session      "03"                        entered through the sessions it must be entered from
    Unlock       "01"                        SecurityAccess with the key source of the settings
    ECU reset    "01"                        11 01 answered, the reset time waited, the ECU answering 10 01 again
    CAN frame    "12F 01 02" / "18FEF100#01" an identifier above 7FF, or of more than 3 digits, is extended
    Python       "power.py:cycle"            function(t, tester) of that file: False or an exception fails it
"""
from __future__ import annotations

import importlib.util
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from canexpert.uds.client import hex_text

KINDS = {"request": "Request", "wait": "Wait", "keep_alive": "Keep alive", "session": "Session",
         "unlock": "Unlock", "reset": "ECU reset", "frame": "CAN frame", "script": "Python"}
HINTS = {"request": "request bytes in hex: 11 01", "wait": "seconds: 2.5",
         "keep_alive": "seconds, with TesterPresent 3E 80 every 2 s: 10",
         "session": "the DiagnosticSessionControl sub-function: 03", "unlock": "the requestSeed level: 01",
         "reset": "the ECUReset type: 01 hard, 02 key off/on, 03 soft",
         "frame": "identifier and data in hex: 12F 01 02 or 18FEF100#0102",
         "script": "a Python file and its function(t, tester): power.py:cycle"}
EXPECTS = ("positive", "any answer", "no answer", "not checked")      # or "NRC 22"
WHEN = ("before", "after")
SCOPES = {"run": "the run", "group": "the group", "test": "the test", "each": "every test"}
CONDITIONS = {"always": "always", "failed": "if it did not pass", "passed": "if it passed"}
KEEP_ALIVE_PERIOD = 2.0
RESET_RETRIES = 5                  # 10 01 sent this often, 0.5 s apart, for the ECU to answer after a reset


class SequenceError(ValueError):
    """A step whose value cannot be read."""


def parse_hex(text: str) -> bytes:
    """"11 01", "1101", "0x11 0x01", "11,01" -> bytes."""
    cleaned = re.sub(r"0x", "", str(text), flags=re.IGNORECASE)
    tokens = [token for token in re.split(r"[\s,;]+", cleaned.strip()) if token]
    try:
        if len(tokens) == 1 and len(tokens[0]) > 2:
            return bytes.fromhex(tokens[0])
        return bytes(int(token, 16) for token in tokens)
    except ValueError:
        raise SequenceError(f"not hex bytes: {text!r}") from None


def parse_number(text: str, hexadecimal: bool = True) -> int:
    try:
        return int(str(text).strip().lower().removeprefix("0x"), 16 if hexadecimal else 10)
    except ValueError:
        raise SequenceError(f"not a number: {text!r}") from None


def parse_seconds(text: str) -> float:
    try:
        seconds = float(str(text).strip().rstrip("s").strip())
    except ValueError:
        raise SequenceError(f"not seconds: {text!r}") from None
    if seconds < 0:
        raise SequenceError("a time is not negative")
    return seconds


def parse_expect(text: str) -> tuple[str, int | None]:
    """("positive" | "any" | "none" | "ignore" | "nrc", NRC or None)."""
    value = str(text or "positive").strip().lower()
    match = re.fullmatch(r"(?:nrc\s*)?(?:0x)?([0-9a-f]{2})", value)
    if match:
        return "nrc", int(match.group(1), 16)
    for kind, words in (("positive", ("positive", "")), ("any", ("any answer", "any")),
                        ("none", ("no answer", "none")), ("ignore", ("not checked", "ignore"))):
        if value in words:
            return kind, None
    raise SequenceError(f"expected answer {text!r}: positive, NRC xx, any answer, no answer or not checked")


def parse_frame(text: str) -> tuple[int, bytes, bool]:
    """(identifier, data, extended) from "12F 01 02", "12F#0102" or "18FEF100 01"."""
    head, _, rest = str(text).strip().partition("#")
    parts = head.split()
    if not parts:
        raise SequenceError("a CAN frame needs its identifier")
    identifier = parts[0]
    data = parse_hex(" ".join(parts[1:]) + (" " + rest if rest else "")) if (parts[1:] or rest) else b""
    can_id = parse_number(identifier)
    if can_id > 0x1FFFFFFF or len(data) > 8:
        raise SequenceError("a frame has an identifier up to 1FFFFFFF and up to 8 data bytes")
    return can_id, data, can_id > 0x7FF or len(identifier.lower().removeprefix("0x")) > 3


def parse_script(text: str) -> tuple[str, str]:
    """(file, function) from "power.py:cycle" (a drive letter's colon is not the separator); run() by default."""
    value = str(text).strip()
    match = re.fullmatch(r"(.+\.py)(?::([A-Za-z_]\w*))?", value)
    if not match:
        raise SequenceError(f"a Python file, and its function after a colon: {text!r}")
    return match.group(1), match.group(2) or "run"


@dataclass
class SequenceStep:
    kind: str = "request"
    value: str = ""
    expect: str = "positive"          # a request's expected answer
    functional: bool = False          # a request functionally addressed

    def problem(self) -> str:
        """What is wrong with the step, or ""."""
        try:
            if self.kind not in KINDS:
                return f"unknown step {self.kind!r}"
            if self.kind == "request":
                if not parse_hex(self.value):
                    return "a request needs its bytes"
                parse_expect(self.expect)
            elif self.kind in ("wait", "keep_alive"):
                parse_seconds(self.value)
            elif self.kind in ("session", "unlock", "reset"):
                number = parse_number(self.value or ("01" if self.kind == "reset" else ""))
                if not 0 <= number <= 0xFF:
                    return "one byte"
                if self.kind == "unlock" and not number % 2:
                    return "requestSeed levels are odd"
            elif self.kind == "frame":
                parse_frame(self.value)
            elif self.kind == "script":
                parse_script(self.value)
        except SequenceError as exc:
            return str(exc)
        return ""

    def text(self) -> str:
        if self.kind == "request":
            return f"{KINDS['request']} {self.value.upper()}{' (functional)' if self.functional else ''} -> {self.expect}"
        return f"{KINDS.get(self.kind, self.kind)} {self.value}".strip()

    def to_dict(self) -> dict:
        values = {"kind": self.kind, "value": self.value}
        if self.kind == "request":
            values.update(expect=self.expect, functional=self.functional)
        return values

    @classmethod
    def from_dict(cls, values: dict) -> "SequenceStep":
        return cls(str(values.get("kind", "request")), str(values.get("value", "")),
                   str(values.get("expect", "positive")), bool(values.get("functional", False)))


@dataclass
class Attachment:
    """Where a sequence runs: before or after the run, a group (target: its name), a test (target: its name)
    or every test; an after-sequence always, or only when what it follows did (not) pass."""
    when: str = "before"
    scope: str = "each"
    target: str = ""
    condition: str = "always"

    def text(self, titles: dict | None = None) -> str:
        target = self.target
        if self.scope == "test":
            target = (titles or {}).get(self.target, self.target)
        where = {"run": "the run", "each": "every test", "group": f"the group {target}",
                 "test": f"the test {target}"}.get(self.scope, self.scope)
        condition = f", {CONDITIONS.get(self.condition, self.condition)}" if self.when == "after" and \
            self.condition != "always" else ""
        return f"{self.when} {where}{condition}"

    def to_dict(self) -> dict:
        return {"when": self.when, "scope": self.scope, "target": self.target, "condition": self.condition}

    @classmethod
    def from_dict(cls, values: dict) -> "Attachment":
        return cls(str(values.get("when", "before")), str(values.get("scope", "each")),
                   str(values.get("target", "")), str(values.get("condition", "always")))


@dataclass
class Sequence:
    name: str = "Sequence"
    steps: list = field(default_factory=list)          # SequenceStep
    attachments: list = field(default_factory=list)    # Attachment
    enabled: bool = True

    def problems(self) -> list[str]:
        return [f"step {index}: {problem}" for index, step in enumerate(self.steps, 1)
                if (problem := step.problem())]

    def to_dict(self) -> dict:
        return {"name": self.name, "enabled": self.enabled, "steps": [step.to_dict() for step in self.steps],
                "attachments": [attachment.to_dict() for attachment in self.attachments]}

    @classmethod
    def from_dict(cls, values: dict) -> "Sequence":
        return cls(str(values.get("name", "Sequence")),
                   [SequenceStep.from_dict(item) for item in values.get("steps", ())],
                   [Attachment.from_dict(item) for item in values.get("attachments", ())],
                   bool(values.get("enabled", True)))


# Ready-made sequences, for the Sequences tab.
PRESETS = {
    "Hard reset": [SequenceStep("reset", "01")],
    "Key off/on reset": [SequenceStep("reset", "02")],
    "Soft reset": [SequenceStep("reset", "03")],
    "Default session": [SequenceStep("session", "01")],
    "Extended session": [SequenceStep("session", "03")],
    "Clear DTCs": [SequenceStep("request", "14 FF FF FF", "positive")],
    "Keep the session 5 s": [SequenceStep("keep_alive", "5")],
}


def due(sequences, when: str, scope: str, target: str = "", outcome: str | None = None) -> list[Sequence]:
    """The enabled sequences attached there. After something, those whose condition its outcome ("passed",
    "failed", None for neither: skipped) meets; "always" always does."""
    found = []
    for sequence in sequences:
        if not sequence.enabled:
            continue
        for attachment in sequence.attachments:
            if attachment.when != when or attachment.scope != scope:
                continue
            if scope in ("group", "test") and attachment.target != target:
                continue
            if when == "after" and attachment.condition != "always" and attachment.condition != outcome:
                continue
            found.append(sequence)
            break
    return found


LABELS = {("before", "run"): "Pre-run", ("before", "group"): "Pre-group", ("before", "test"): "Pre-test",
          ("before", "each"): "Pre-test", ("after", "run"): "Post-run", ("after", "group"): "Post-group",
          ("after", "test"): "Post-test", ("after", "each"): "Post-test"}


class SequenceRunner:
    """Runs sequences' steps as steps of the test case t, with the suite's tester, key source, reset time and
    session paths. Script paths are taken from base_dir (the plan's folder) when they are relative."""

    def __init__(self, suite, base_dir=None):
        self.suite = suite
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self._modules = {}

    def run(self, t, sequence: Sequence, label: str, mode: str) -> bool:
        """mode "require": a failed step ends the case (the run's setup); "check": a failed step (the caller
        blocks the test); "warn": a warning. Returns whether every step went well - the first that does not
        ends the sequence (a failure the accepted deviations cover does not)."""
        prefix = f"{label} '{sequence.name}'"
        if not sequence.steps:
            t.log(f"{prefix}: no steps")
        for index, step in enumerate(sequence.steps, 1):
            try:
                outcome = self._step(t, step, prefix)
            except SequenceError as exc:
                outcome = (False, f"{prefix}: step {index} ({step.text()})", str(exc))
            if outcome is None:
                continue
            ok, what, detail = outcome
            if mode == "warn":
                if ok:
                    t.check(True, what, detail)
                    continue
                t.warn(what, detail)
                return False
            if mode == "require":
                t.require(ok, what, detail)
                continue
            if not t.check(ok, what, detail):
                return False
        return True

    # --- the steps ---------------------------------------------------------------------------------------

    def _step(self, t, step: SequenceStep, prefix: str):
        """(passed, description, detail), or None for a step that only happened (a wait, a frame)."""
        tester, options = self.suite.tester, self.suite.o
        kind = step.kind
        if kind == "request":
            payload = parse_hex(step.value)
            if not payload:
                raise SequenceError("a request needs its bytes")
            expect, nrc = parse_expect(step.expect)
            if step.functional and tester.functional_id is None:
                return False, f"{prefix}: {hex_text(payload)} functional", "no functional request ID set"
            if expect == "none":
                answer = tester.quiet(payload, step.functional)
                return answer.raw is None, f"{prefix}: {hex_text(payload)} is not answered", answer.text()
            answer = tester.ask(payload, step.functional)
            if expect == "ignore":
                t.log(f"{prefix}: {answer.text()}")
                return None
            if expect == "positive":
                return answer.positive(), f"{prefix}: {hex_text(payload)} answered positively", answer.text()
            if expect == "any":
                return answer.raw is not None, f"{prefix}: {hex_text(payload)} answered", answer.text()
            return (answer.nrc == nrc and answer.raw[1] == payload[0],
                    f"{prefix}: {hex_text(payload)} answered NRC 0x{nrc:02X}", answer.text())
        if kind == "wait":
            seconds = parse_seconds(step.value)
            t.log(f"{prefix}: wait {seconds:g} s")
            t.wait(seconds)
            return None
        if kind == "keep_alive":
            return self._keep_alive(t, parse_seconds(step.value), step.functional, prefix)
        if kind == "session":
            return self._session(parse_number(step.value), prefix)
        if kind == "unlock":
            return self._unlock(parse_number(step.value), prefix)
        if kind == "reset":
            return self._reset(t, parse_number(step.value or "01"), options.reset_time, prefix)
        if kind == "frame":
            can_id, data, extended = parse_frame(step.value)
            t.send(can_id, data, extended)
            t.log(f"{prefix}: frame {can_id:X} {hex_text(data)} sent")
            return None
        if kind == "script":
            return self._script(t, step.value, prefix)
        raise SequenceError(f"unknown step {kind!r}")

    def _keep_alive(self, t, seconds, functional, prefix):
        t.log(f"{prefix}: wait {seconds:g} s with TesterPresent (3E 80)")
        deadline = time.monotonic() + seconds
        while True:
            self.suite.tester.quiet(b"\x3e\x80", functional and self.suite.tester.functional_id is not None)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            t.wait(min(KEEP_ALIVE_PERIOD, remaining))

    def _session(self, session, prefix):
        tester, suite = self.suite.tester, self.suite
        answer = None
        for step in suite.path_to(session):
            answer = tester.ask(bytes([0x10, step]))
            if not answer.positive():
                return False, f"{prefix}: {suite.d.session_name(step)} entered (10 {step:02X})", answer.text()
        return True, f"{prefix}: {suite.d.session_name(session)} entered (10 {session:02X})", answer.text()

    def _unlock(self, level, prefix):
        tester, key = self.suite.tester, self.suite.o.key
        what = f"{prefix}: unlocked level 0x{level:02X}"
        if key is None:
            return False, what, "no key source is set for SecurityAccess"
        seed = tester.ask(bytes([0x27, level]))
        if not seed.positive() or len(seed.raw) < 2:
            return False, what, f"requestSeed: {seed.text()}"
        if not any(seed.raw[2:]):
            return True, what, f"already unlocked: {seed.text()}"
        try:
            computed = bytes(key(level, bytes(seed.raw[2:])))
        except Exception as exc:                    # a seed & key DLL that fails
            return False, what, f"the key could not be computed: {exc}"
        answer = tester.ask(bytes([0x27, level + 1]) + computed)
        return answer.positive(), what, answer.text()

    def _reset(self, t, reset_type, reset_time, prefix):
        tester = self.suite.tester
        request = bytes([0x11, reset_type])
        what = f"{prefix}: ECU reset (11 {reset_type:02X}) and the ECU back after {reset_time:g} s"
        answer = tester.quiet(request) if reset_type & 0x80 else tester.ask(request)
        if not (answer.positive() or (reset_type & 0x80 and answer.raw is None)):
            return False, what, answer.text()
        t.wait(reset_time)
        back = None
        for _ in range(RESET_RETRIES):
            back = tester.ask(b"\x10\x01", timeout=0.5)
            if back.positive():
                return True, what, f"{answer.text()}; {back.text()}"
            t.wait(0.5)
        return False, what, f"{answer.text()}; then {back.text() if back else 'nothing'}"

    def _script(self, t, value, prefix):
        path, name = parse_script(value)
        file = Path(path)
        if not file.is_absolute():
            file = self.base_dir / file
        what = f"{prefix}: {file.name}:{name}()"
        try:
            module = self._load(file)
            function = getattr(module, name)
        except Exception as exc:                    # the user's file: missing, or broken
            return False, what, f"{type(exc).__name__}: {exc}"
        try:
            result = function(t, self.suite.tester)
        except Exception as exc:
            return False, what, f"{type(exc).__name__}: {exc}"
        return result is not False, what, "" if result is not False else "returned False"

    def _load(self, file: Path):
        key = (str(file), file.stat().st_mtime)
        if key not in self._modules:
            spec = importlib.util.spec_from_file_location(f"test_expert_sequence_{len(self._modules)}", file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._modules[key] = module
        return self._modules[key]

