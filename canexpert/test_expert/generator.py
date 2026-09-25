"""
The tests TestExpert generates from a description, as DiVa does: for every session, service, sub-function,
DID, routine and security level of the description, what ISO 14229-1 says the ECU must answer - positive
where it is allowed, and the right negative response code where it is not.

Each test case is a function run by canexpert.testing.runner (verdicts per step, Stop, HTML and JUnit reports);
Suite.module() gives them as a test module, with the pre-test and post-test sequences (sequences.py) around
the run, the groups and the tests, and CAN Expert's test modules after them (modules.py). What changes the ECU for good - ECU reset, clearing the fault memory,
writing a DID (its own value back), the security lockout - runs only when Options ask for it.

A test case's name - "data_identifiers.read_vin_f190" - comes from its group and title, so a test plan can
name it whatever else the description or the options add.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from canexpert.test_expert.coverage import Coverage
from canexpert.test_expert.description import DEFAULT_SESSION, ISO_SERVICES, SUB_FUNCTION_SERVICES, EcuDescription
from canexpert.test_expert.modules import GROUP_PREFIX
from canexpert.test_expert.policy import NrcPolicy, nrc_text
from canexpert.test_expert.sequences import LABELS, SequenceRunner, due
from canexpert.test_expert.services import ServiceTests
from canexpert.test_expert.transport import TransportTests
from canexpert.testing.runner import BLOCKED, ERROR, FAILED, PASSED, SKIPPED, TestCase, TestModule, call_hook
from canexpert.uds.client import UdsFunctions
from canexpert.uds.observer import SERVICE_NAMES

# Identifiers that are hardly ever used: the ones the tests send expecting "not supported", when free.
UNUSED_DIDS = (0xFDFF, 0xFD00, 0xBEEF, 0x1234, 0x0001)
UNUSED_RIDS = (0xFDFF, 0xBEEF, 0x1234)
UNUSED_SUB_FUNCTIONS = (0x7E, 0x7D, 0x5F, 0x06)
UNUSED_LEVELS = (0x7D, 0x61, 0x41, 0x09, 0x07, 0x05, 0x03)
UNUSED_DTC_GROUP = 0xFFFFFE
# Services whose minimal request only reads or asks: sent where the service is allowed, to see it answer.
HARMLESS = {0x19, 0x22, 0x23, 0x27, 0x28, 0x2A, 0x3E, 0x85, 0x86}
SESSION_ORDER = (0x01, 0x03, 0x02)        # default, extended, programming: the order sessions are tried in
SEED_ATTEMPTS = 4                         # seeds asked for, each in a new session, that must all differ
# ISO 14229-1 annex C: the identification DIDs read at the start of a run, for the report (and comparing runs).
IDENTIFICATION = {
    0xF180: "bootSoftwareIdentification", 0xF181: "applicationSoftwareIdentification",
    0xF182: "applicationDataIdentification", 0xF187: "vehicleManufacturerSparePartNumber",
    0xF188: "vehicleManufacturerECUSoftwareNumber", 0xF189: "vehicleManufacturerECUSoftwareVersionNumber",
    0xF18A: "systemSupplierIdentifier", 0xF18B: "ECUManufacturingDate", 0xF18C: "ECUSerialNumber",
    0xF190: "VIN", 0xF191: "vehicleManufacturerECUHardwareNumber", 0xF192: "systemSupplierECUHardwareNumber",
    0xF193: "systemSupplierECUHardwareVersionNumber", 0xF194: "systemSupplierECUSoftwareNumber",
    0xF195: "systemSupplierECUSoftwareVersionNumber", 0xF197: "systemNameOrEngineType", 0xF199: "programmingDate",
    0xF19E: "ODXFile",
}


@dataclass
class Options:
    destructive: bool = False     # ECU reset, clearing DTCs, writing DIDs (their value back, their limits)
    lockout: bool = False         # wrong keys until the lockout, then its delay (slow)
    functional: bool = True       # functionally addressed requests (needs the functional ID)
    key: object = None            # key(level, seed) -> bytes; None: what needs an unlocked ECU is skipped
    timing_margin_ms: int = 50    # tolerance over P2 (and P2*) for the response time
    reset_time: float = 1.0       # seconds an ECU reset takes
    attempts: int = 3             # wrong keys before the lockout
    lockout_seconds: float = 10.0
    s3_test: bool = True          # the session ends after S3 without requests, and TesterPresent keeps it
    s3_seconds: float = 5.0       # S3server (ISO 14229-2: 5 s)
    start_routines: str = ""      # routines that may be started: "0201; FF00: 44 00 01 00 00 00 00 00 04"
    transport: bool = True        # the transport layer's tests (ISO 15765-2): a few seconds of waiting
    download: str = ""            # a RequestDownload the ECU accepts, "address:size" in hex: "10000:300"
    memory: str = ""              # memory ReadMemoryByAddress may read (and, destructive, write back): "F000:10"


def parse_routine_starts(text: str) -> dict:
    """"0201; FF00: 44 00 01" -> {0x0201: b"", 0xFF00: b"\x44\x00\x01"}: the routines a run may start, with
    the option record each is started with. ValueError naming what is wrong."""
    starts = {}
    for part in str(text or "").replace(",", ";").split(";"):
        if not part.strip():
            continue
        rid, _, record = part.partition(":")
        try:
            number = int(rid.strip().lower().removeprefix("0x"), 16)
            data = bytes.fromhex(record.replace(" ", "")) if record.strip() else b""
        except ValueError:
            raise ValueError(f"a routine to start is its identifier and, after a colon, its option record in "
                             f"hex: {part.strip()!r}") from None
        if not 0 <= number <= 0xFFFF:
            raise ValueError(f"a routine identifier is 16 bits: {rid.strip()}")
        starts[number] = data
    return starts


def _hex(data) -> str:
    return bytes(data).hex(" ").upper()


def _hook_failure(hook, verdict, why) -> str:
    last = why.strip().splitlines()[-1] if why.strip() else ""
    return f"the module's {hook} {verdict}" + (f": {last}" if last else "")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def value_text(data: bytes) -> str:
    """An identification value as text when it is printable (trailing padding dropped), else its hex."""
    trimmed = bytes(data).rstrip(b"\x00\xff ")
    if trimmed and all(0x20 <= byte < 0x7F for byte in trimmed):
        return trimmed.decode("ascii")
    return _hex(data) if data else "(empty)"


class Suite:
    """The generated test cases of a description; tester is set before a run (a test_expert.tester.Tester).
    sequences: the pre-test and post-test sequences; base_dir: where their Python files are looked for;
    policy: the NRCs that pass in each situation (NrcPolicy; ISO 14229-1's by default); modules: CAN Expert's
    test modules to run after the generated tests (modules.LoadedModule), each as a group."""

    def __init__(self, description: EcuDescription, options: Options | None = None, sequences=(), base_dir=None,
                 policy: NrcPolicy | None = None, modules=()):
        self.d = description
        self.modules = list(modules)
        self.module_groups = {}                 # group -> its modules.LoadedModule
        self.o = options or Options()
        self.policy = policy or NrcPolicy()
        self.sequences = list(sequences)
        self.sequence_runner = SequenceRunner(self, base_dir)
        self.tester = None
        self.p2 = 0.05                     # the P2 the ECU announces in its default session answer
        self.p2_star = 5.0                 # and its P2*
        self.cases: list[TestCase] = []
        self.group_of: dict[str, str] = {}      # test case name -> its group
        self._build()
        self._start_run(None)

    # --- the module ----------------------------------------------------------------------------------

    def module(self, names=None) -> TestModule:
        """The test module to run; names: the test cases the run takes (None: all), for the sequences that
        follow a group."""
        self._start_run(names)
        path = Path(self.d.source) if self.d.source and Path(self.d.source).suffix else Path(_slug(self.d.name))
        hooks = {"setup": self._setup, "teardown": self._teardown, "before_each": self._before_each,
                 "after_each": self._after_each}
        return TestModule(path, f"TestExpert: {self.d.name}", list(self.cases), hooks, {})

    def groups(self) -> dict[str, list[TestCase]]:
        grouped = {}
        for case in self.cases:
            grouped.setdefault(self.group_of[case.name], []).append(case)
        return grouped

    def titles(self) -> dict[str, str]:
        """test case name -> its title."""
        return {case.name: case.title for case in self.cases}

    def _add(self, group, title, function, doc="", name=None):
        base = name or f"{_slug(group)}.{_slug(title)}"
        name, number = base, 1
        while name in self.group_of:
            number += 1
            name = f"{base}_{number}"
        self.group_of[name] = group
        self.cases.append(TestCase(name, f"{group}: {title}", function, doc))

    # --- the run and its sequences ------------------------------------------------------------------------

    def _start_run(self, names):
        self.coverage = Coverage()
        self.identification = {}           # DID -> (name, value as text): read at the start of the run
        chosen = [case.name for case in self.cases if names is None or case.name in names]
        self._last_in_group = {self.group_of[name]: name for name in chosen}
        self._group = None
        self._group_failed = {}
        self._blocked_groups = {}
        self._skipped_groups = {}
        self._run_failed = False
        self._modules_started = set()      # the module groups whose setup ran...
        self._modules_ended = set()        # ...and whose teardown did
        self._module_answers = []          # the answers to the module test case running

    def _sequences(self, t, when, scope, target="", outcome=None, mode="check") -> str:
        """Run the sequences attached there; returns the name of the one that failed, or ""."""
        for sequence in due(self.sequences, when, scope, target, outcome):
            if not self.sequence_runner.run(t, sequence, LABELS[when, scope], mode):
                return sequence.name
        return ""

    def _before_each(self, t):
        name = t.result.name
        group = self.group_of.get(name, "")
        loaded = self._module(group)
        if loaded is None or group != self._group:
            self._default()                     # a module's test cases go on from where its setup left the ECU
        if group != self._group:
            self._group = group
            failed = self._sequences(t, "before", "group", group)
            if failed:
                self._blocked_groups[group] = f"the pre-group sequence '{failed}' failed"
            elif loaded is not None:
                self._modules_started.add(group)
                verdict, why = self._module_hook(t, loaded, "setup")
                if verdict == SKIPPED:
                    self._skipped_groups[group] = why
                elif verdict != PASSED:
                    self._blocked_groups[group] = _hook_failure("setup", verdict, why)
        if group in self._blocked_groups:
            t.block(self._blocked_groups[group])
        if group in self._skipped_groups:
            t.skip(self._skipped_groups[group])
        failed = self._sequences(t, "before", "each") or self._sequences(t, "before", "test", name)
        if failed:
            t.block(f"the pre-test sequence '{failed}' failed")
        if loaded is not None:
            self._module_answers = []
            verdict, why = self._module_hook(t, loaded, "before_each")
            if verdict == SKIPPED:
                t.skip(why)
            elif verdict == BLOCKED:
                t.block(why)
            elif verdict != PASSED:
                t.fail(_hook_failure("before_each", verdict, why), why)     # the test case is not run

    def _after_each(self, t):
        name, verdict = t.result.name, t.result.verdict
        group = self.group_of.get(name, "")
        loaded = self._module(group)
        outcome = "passed" if verdict == PASSED else "failed" if verdict in (FAILED, ERROR, BLOCKED) else None
        if outcome == "failed":
            self._run_failed = self._group_failed[group] = True
        if loaded is not None:
            if group in self._modules_started and group not in self._blocked_groups and \
                    group not in self._skipped_groups:
                self._module_hook(t, loaded, "after_each")      # its failures fail the test case, as in CAN Expert
            if outcome:
                for answer in self._module_answers:             # what the module's test case asked, covered
                    self.coverage.record(answer.request, answer.session, "pass" if outcome == "passed" else "fail",
                                         name, answer.functional)
            self._module_answers = []
        self._sequences(t, "after", "test", name, outcome, "warn")
        self._sequences(t, "after", "each", "", outcome, "warn")
        last = self._last_in_group.get(group) == name
        if last and loaded is not None:
            self._end_module(t, group)
        if last:
            self._sequences(t, "after", "group", group, "failed" if self._group_failed.get(group) else "passed",
                            "warn")
        if loaded is None or last:
            self._default()

    # --- CAN Expert's test modules -------------------------------------------------------------------------

    def _module(self, group):
        """The readable module whose group this is, or None."""
        loaded = self.module_groups.get(group)
        return loaded if loaded is not None and loaded.module is not None else None

    def _module_hook(self, t, loaded, hook) -> tuple[str, str]:
        function = loaded.module.hooks.get(hook)
        if function is None:
            return PASSED, ""
        if hook in ("setup", "teardown"):
            t.log(f"{loaded.title}: {hook}")
        return call_hook(function, t)

    def _end_module(self, t, group):
        """The module's teardown, once, after its setup; what fails there is a warning."""
        if group not in self._modules_started or group in self._modules_ended:
            return
        self._modules_ended.add(group)
        with t.lenient():
            verdict, why = self._module_hook(t, self.module_groups[group], "teardown")
        if verdict not in (PASSED, FAILED):                    # a failed step is a warning already
            t.warn(_hook_failure("teardown", verdict, why), why)

    def _bind_modules(self):
        """The modules' UDS functions (RDBI, DSC...) and j1939 on the run's tester."""
        modules = [loaded.module for loaded in self.modules if loaded.module is not None]
        if not modules:
            return
        from canexpert.j1939.transport import J1939Link
        functions = UdsFunctions(self._module_request, None, self.tester.transport.get("timeout", 2.0)).namespace()
        for module in modules:
            module.namespace.update(functions)
            module.namespace["j1939"] = J1939Link(self.tester.bus)

    def _module_request(self, payload, timeout=None, wait=True):
        """A module's UDS request, through the tester: the answer's bytes (None without one)."""
        answer = self.tester.ask(payload, timeout=timeout if wait else 0.0)
        self._module_answers.append(answer)
        return answer.raw if wait else None

    # --- steps ------------------------------------------------------------------------------------------

    def positive(self, t, payload, what, echo=None, length=None, functional=False):
        """A step: a positive response (echoing echo, of length bytes). Returns it, or None."""
        answer = self.tester.ask(payload, functional)
        raw = answer.raw
        ok = answer.positive() and (echo is None or raw[1:1 + len(echo)] == bytes(echo))
        detail = answer.text()
        if ok and length is not None and len(raw) != length:
            ok = False
            detail += f" - {len(raw)} bytes, {length} expected"
        t.check(ok, what, detail)
        self._covered(t, answer)
        self._pending(t, answer)
        return raw if ok else None

    def negative(self, t, payload, situation, what, functional=False):
        """A step: the negative response the situation calls for (policy.SITUATIONS): one of the NRCs the NRC
        policy accepts there. One that ISO 14229-1 does not name passes with a note."""
        iso, accepted = self.policy.iso(situation), self.policy.accepted(situation)
        answer = self.tester.ask(payload, functional)
        got = answer.nrc
        good = answer.raw is not None and answer.raw[:2] == bytes([0x7F, payload[0]]) and got in accepted
        if good and got not in iso:
            verdict = t.check(True, what, f"{answer.text()} - accepted by the NRC policy; ISO 14229-1 asks for "
                                          f"{' or '.join(nrc_text(code) for code in iso)}")
        else:
            wanted = " or ".join(nrc_text(code) for code in accepted)
            verdict = t.check(good, what, answer.text() if good else f"{answer.text()} - expected NRC {wanted}")
        self._covered(t, answer)
        self._pending(t, answer)
        return verdict

    def silent(self, t, payload, what, functional=False):
        """A step: no answer at all - but an ECU that needed more time and sent a response pending must then
        send its positive answer (ISO 14229-1: 0x78 lifts suppressPosRspMsgIndicationBit)."""
        answer = self.tester.quiet(payload, functional)
        if answer.raw is not None and answer.pending and answer.positive():
            verdict = t.check(True, what, f"{answer.text()} - after a response pending the answer is sent")
        else:
            verdict = t.check(answer.raw is None, what, answer.text())
        self._covered(t, answer)
        return verdict

    def available(self, t, payload, what):
        """A step: the service answers - positive, or refused for another reason than not being there."""
        answer = self.tester.ask(payload)
        ok = answer.raw is not None and answer.nrc not in (0x11, 0x7F)
        verdict = t.check(ok, what, answer.text())
        self._covered(t, answer)
        self._pending(t, answer)
        return verdict

    def _pending(self, t, answer):
        """ISO 14229-2 when the ECU needs time: the first response pending (0x78) within P2, the next ones
        within P2* of each other, and the answer within P2* of the last."""
        if not answer.pending:
            return
        margin = self.o.timing_margin_ms / 1000
        gaps = [later - earlier for earlier, later in zip(answer.pending, answer.pending[1:])]
        last = answer.elapsed - answer.pending[-1]
        ok = answer.pending[0] <= self.p2 + margin and all(gap <= self.p2_star + margin for gap in gaps) and \
            answer.raw is not None and last <= self.p2_star + margin
        detail = (f"{len(answer.pending)} response pending: the first after {answer.pending[0] * 1000:.0f} ms "
                  f"(P2 {self.p2 * 1000:.0f} ms)" + (f", the longest gap {max(gaps) * 1000:.0f} ms" if gaps else "")
                  + f", the answer {last * 1000:.0f} ms after the last (P2* {self.p2_star * 1000:.0f} ms)")
        t.check(ok, f"{_hex(answer.request[:4])}: response pending within P2, then P2*", detail)

    def _covered(self, t, answer, verdict=None):
        """The step just checked (the last of t) counts for what answer asked, in the session it was sent in."""
        if verdict is None:
            verdict = t.result.steps[-1].verdict if t.result.steps else ""
        self.coverage.record(answer.request, answer.session, verdict, t.result.name, answer.functional)

    def path_to(self, session) -> list[int]:
        return self.d.session_path(session)

    def enter(self, t, session):
        """Into a session, through the sessions it must be entered from; the case ends if one refuses."""
        for step in self.path_to(session):
            answer = self.tester.ask(bytes([0x10, step]))
            if not answer.positive():
                self._covered(t, answer, "fail")
                t.require(False, f"enter {self.d.session_name(step)} (10 {step:02X})", answer.text())
            else:
                self._covered(t, answer, "pass")

    def unlock(self, t, level):
        """SecurityAccess for level with the key source; the case is skipped without one."""
        if self.o.key is None:
            t.skip("no key source set for SecurityAccess")
        seed = self.positive(t, bytes([0x27, level]), f"requestSeed level 0x{level:02X}", echo=[level])
        if seed is None:
            t.fail("no seed")
        if not any(seed[2:]):
            return True                                         # already unlocked
        key = self.o.key(level, bytes(seed[2:]))
        return self.positive(t, bytes([0x27, level + 1]) + bytes(key), f"sendKey level 0x{level + 1:02X}",
                             echo=[level + 1]) is not None

    def _default(self, _t=None):
        self.tester.ask(b"\x10\x01")
        time.sleep(0.02)

    def _setup(self, t):
        self._bind_modules()
        self._sequences(t, "before", "run", mode="require")
        answer = self.tester.ask(b"\x10\x01")
        t.require(answer.positive(), "the ECU answers DiagnosticSessionControl default (10 01)", answer.text())
        if answer.raw is not None and len(answer.raw) >= 4:
            p2 = int.from_bytes(answer.raw[2:4], "big") / 1000
            if 0 < p2 < 5:
                self.p2 = p2
                t.log(f"P2 {p2 * 1000:.0f} ms, as the ECU announces")
        if answer.raw is not None and len(answer.raw) >= 6:
            p2_star = int.from_bytes(answer.raw[4:6], "big") / 100
            if 0 < p2_star <= 600:
                self.p2_star = p2_star
                self.tester.pending_timeout = max(10.0, 2 * p2_star)
                t.log(f"P2* {p2_star * 1000:.0f} ms, as the ECU announces")
        self._identify(t)

    def _identify(self, t):
        """Read the identification DIDs the description has (readable in the default session, locked): the
        report shows them, and runs are compared with them."""
        for did, iso_name in sorted(IDENTIFICATION.items()):
            entry = self.d.dids.get(did)
            if entry is None or entry.read is None or entry.read.levels or not entry.read.allows(DEFAULT_SESSION):
                continue
            answer = self.tester.ask(b"\x22" + did.to_bytes(2, "big"))
            if not answer.positive() or answer.raw[1:3] != did.to_bytes(2, "big"):
                continue
            self.identification[did] = (iso_name, value_text(answer.raw[3:]))       # ISO's name: runs compare
            t.log(f"ECU identification: {did:04X} {iso_name} = {self.identification[did][1]}")

    def _teardown(self, t):
        for group in sorted(self._modules_started - self._modules_ended):     # a run stopped inside a module
            self._end_module(t, group)
        self._default()
        self._sequences(t, "after", "run", "", "failed" if self._run_failed else "passed", "warn")

    # --- what the description has ---------------------------------------------------------------------

    def sessions(self) -> list[int]:
        known = list(self.d.sessions)
        return [s for s in SESSION_ORDER if s in known] + sorted(s for s in known if s not in SESSION_ORDER)

    def allowed(self, sid, session) -> bool:
        service = self.d.service(sid)
        return service is not None and service.access.allows(session)

    def first_session(self, access) -> int | None:
        return next((s for s in self.sessions() if access.allows(s)), None)

    def unused(self, candidates, used) -> int:
        return next((value for value in candidates if value not in used), candidates[-1])

    def readable_did(self):
        return next((did for did in sorted(self.d.dids) if self.d.dids[did].read is not None
                     and not self.d.dids[did].read.levels), None)

    def minimal(self, sid) -> bytes | None:
        """A well-formed request of the service, for seeing where it is allowed."""
        service = self.d.service(sid)
        subs = sorted(service.sub_functions) if service else []
        did = self.readable_did() or next(iter(sorted(self.d.dids)), None)
        writable = next((d for d in sorted(self.d.dids) if self.d.dids[d].write is not None), did)
        rid = next(iter(sorted(self.d.routines)), 0xFF00)
        level = next(iter(sorted(self.d.security_levels)), 0x01)
        memory = b"\x44\x00\x00\x00\x00\x00\x00\x00\x10"
        requests = {
            0x11: bytes([0x11, subs[0] if subs else 0x01]), 0x14: b"\x14\xff\xff\xff",
            0x19: bytes([0x19, 0x01, 0xFF]) if not subs or 0x01 in subs else bytes([0x19, subs[0], 0xFF]),
            0x22: b"\x22" + did.to_bytes(2, "big") if did is not None else None,
            0x23: b"\x23\x14\x00\x00\x00\x00\x01", 0x27: bytes([0x27, level]), 0x28: b"\x28\x00\x01",
            0x2A: b"\x2a\x04",
            0x2E: (b"\x2e" + writable.to_bytes(2, "big") + bytes(self.d.dids[writable].length or 1))
            if writable is not None else None,
            0x2F: b"\x2f" + did.to_bytes(2, "big") + b"\x00" if did is not None else None,
            0x31: b"\x31\x01" + rid.to_bytes(2, "big"), 0x34: b"\x34\x00" + memory, 0x35: b"\x35\x00" + memory,
            0x36: b"\x36\x01", 0x37: b"\x37", 0x3D: b"\x3d\x14\x00\x00\x00\x00\x01\x00", 0x3E: b"\x3e\x00",
            0x85: b"\x85\x01", 0x86: b"\x86\x04",
        }
        if sid in requests:
            return requests[sid]
        if sid in SUB_FUNCTION_SERVICES:
            return bytes([sid, subs[0] if subs else 0x01])
        return bytes([sid])

    # --- the cases ----------------------------------------------------------------------------------------

    def _build(self):
        self._sessions()
        self._tester_present()
        self._unsupported_services()
        self._availability()
        self._nrc_order()
        self._message_length()
        self._sub_functions()
        self._data_identifiers()
        self._did_values()
        self._security()
        self._routines()
        self._routine_control()
        self._fault_memory()
        self._communication()
        ServiceTests(self).add()
        self._reset()
        self._functional()
        self._timing()
        if self.o.transport:
            TransportTests(self).add()
        self._s3()
        self._modules()

    def _modules(self):
        """CAN Expert's test modules, each a group after the generated tests; its test cases are named after its
        file and their function. A module that cannot be read is a test case that fails."""
        for loaded in self.modules:
            group = f"{GROUP_PREFIX}{loaded.title}"
            if group in self.module_groups:
                group = f"{group} ({loaded.path.name})"
            self.module_groups[group] = loaded
            if loaded.module is None:
                def unreadable(t, loaded=loaded):
                    t.fail(f"the test module {loaded.path.name} is read", loaded.error)
                self._add(group, "Read the module", unreadable, str(loaded.path), f"{loaded.key}.read")
                continue
            for case in loaded.module.cases:
                self._add(group, case.title, case.function, case.doc, f"{loaded.key}.{case.name}")

    def _sessions(self):
        group = "Sessions"

        def default(t):
            raw = self.positive(t, b"\x10\x01", "10 01 is answered 50 01 with P2 and P2* (6 bytes)", echo=[0x01],
                                length=6)
            if raw is not None:
                t.log(f"P2 {int.from_bytes(raw[2:4], 'big')} ms, P2* {int.from_bytes(raw[4:6], 'big') * 10} ms")
        self._add(group, "Default session (10 01)", default)

        for session in self.sessions():
            if session == DEFAULT_SESSION:
                continue

            def enter(t, session=session):
                path = self.path_to(session)
                for step in path[:-1]:
                    self.positive(t, bytes([0x10, step]), f"on the way: 10 {step:02X}", echo=[step])
                self.positive(t, bytes([0x10, session]), f"10 {session:02X} is answered 50 {session:02X} with its "
                              f"timing", echo=[session], length=6)
                self.positive(t, b"\x10\x01", "back to the default session (10 01)", echo=[0x01])
            self._add(group, f"Enter the {self.d.session_name(session)} (10 {session:02X})", enter)

            sources = self.d.sessions[session].entered_from
            if sources and DEFAULT_SESSION not in sources:
                def refused(t, session=session):
                    self.negative(t, bytes([0x10, session]), "session_not_from_here",
                                  f"from the default session, 10 {session:02X} is refused")
                self._add(group, f"The {self.d.session_name(session)} is not entered from the default session",
                          refused)

        unused = self.unused(UNUSED_SUB_FUNCTIONS, self.d.sessions)

        def unsupported(t):
            self.negative(t, bytes([0x10, unused]), "sub_function_not_supported", f"10 {unused:02X}: no such session")
            self.negative(t, bytes([0x10, unused | 0x80]), "sub_function_not_supported",
                          f"10 {unused | 0x80:02X}: the suppress bit does not hide a negative response")
        self._add(group, "A session that does not exist", unsupported)

        def length(t):
            self.negative(t, b"\x10", "incorrect_length", "10 alone: incorrect length")
            self.negative(t, b"\x10\x01\x00", "incorrect_length", "10 01 00: one byte too many")
        self._add(group, "Message length", length)

        def suppress(t):
            self.silent(t, b"\x10\x81", "10 81: the suppress bit set, no answer")
            self.positive(t, b"\x10\x01", "and the ECU answers the next request", echo=[0x01])
        self._add(group, "Suppress positive response", suppress)

    def _tester_present(self):
        if 0x3E not in self.d.services:
            return

        def case(t):
            self.positive(t, b"\x3e\x00", "3E 00 is answered 7E 00", echo=[0x00], length=2)
            self.silent(t, b"\x3e\x80", "3E 80: the suppress bit set, no answer")
            self.negative(t, b"\x3e\x01", "sub_function_not_supported", "3E 01: zeroSubFunction only")
            self.negative(t, b"\x3e\x81", "sub_function_not_supported",
                          "3E 81: the suppress bit does not hide a negative response")
            self.negative(t, b"\x3e", "incorrect_length", "3E alone: incorrect length")
            self.negative(t, b"\x3e\x00\x00", "incorrect_length", "3E 00 00: one byte too many")
        self._add("TesterPresent", "TesterPresent (3E)", case)

    def _unsupported_services(self):
        missing = [sid for sid in ISO_SERVICES if sid not in self.d.services]
        if not missing:
            return

        def case(t):
            for sid in missing:
                self.negative(t, bytes([sid]), "service_not_supported",
                              f"{sid:02X} {SERVICE_NAMES.get(sid, '')}: not supported")
        self._add("Services", f"Services the ECU does not have ({len(missing)})", case,
                  "Each ISO 14229-1 service the description does not list must get NRC 0x11 serviceNotSupported.")

    def _availability(self):
        group = "Service availability"
        for sid in sorted(self.d.services):
            if sid in (0x10,):
                continue
            service = self.d.services[sid]
            request = self.minimal(sid)
            if request is None:
                continue
            refused = [s for s in self.sessions() if not service.access.allows(s)]
            harmless = sid in HARMLESS
            if not refused and not harmless:
                continue

            def case(t, sid=sid, service=service, request=request, harmless=harmless):
                for session in self.sessions():
                    self.enter(t, session)
                    name = self.d.session_name(session)
                    if not service.access.allows(session):
                        self.negative(t, request, "service_not_in_session",
                                      f"{name}: {_hex(request)} is refused, NRC 0x7F")
                    elif harmless:
                        self.available(t, request, f"{name}: {_hex(request)} is answered")
            self._add(group, f"{service.name} ({sid:02X}) by session", case)

    def _nrc_order(self):
        """ISO 14229-1's order: a service not allowed in the session gets 0x7F before its request's length or
        sub-function is looked at."""
        cases = []
        for sid, service in sorted(self.d.services.items()):
            session = next((s for s in self.sessions() if not service.access.allows(s)), None)
            if session is None or sid in (0x36, 0x37):
                continue
            cases.append((sid, service, session))
        if not cases:
            return

        def case(t):
            for sid, service, session in cases:
                self.enter(t, session)
                name = self.d.session_name(session)
                self.negative(t, bytes([sid]), "service_not_in_session",
                              f"{name}: {sid:02X} alone gets 0x7F before 0x13")
                if sid in SUB_FUNCTION_SERVICES:
                    unused = self.unused(UNUSED_SUB_FUNCTIONS, service.sub_functions)
                    self.negative(t, bytes([sid, unused, 0x00, 0x00]), "service_not_in_session",
                                  f"{name}: {sid:02X} {unused:02X} gets 0x7F before 0x12")
        self._add("NRC order", "Session before length and sub-function", case,
                  "ISO 14229-1 figure 5: SID supported, then supported in the active session, then the rest.")

    def _message_length(self):
        checks = []
        for sid, service in sorted(self.d.services.items()):
            session = self.first_session(service.access)
            if session is None or sid in (0x10, 0x3E, 0x36, 0x37):
                continue
            wrong = [bytes([sid])]
            if sid == 0x22 and self.readable_did() is not None:
                wrong.append(b"\x22" + self.readable_did().to_bytes(2, "big")[:1])
                wrong.append(b"\x22" + self.readable_did().to_bytes(2, "big") + b"\x00")
            if sid == 0x19 and 0x02 in service.sub_functions:
                wrong.append(b"\x19\x02")
            if sid == 0x28:
                wrong.append(b"\x28\x00\x01\x00")
            if sid == 0x31:
                wrong.append(b"\x31\x01")
            if sid == 0x14:
                wrong.append(b"\x14\xff\xff")
            if sid == 0x2E:
                did = next((d for d in sorted(self.d.dids) if self.d.dids[d].write is not None), None)
                if did is not None:
                    wrong.append(b"\x2e" + did.to_bytes(2, "big"))
            checks.append((sid, service, session, wrong))

        def case(t):
            for sid, service, session, wrong in checks:
                self.enter(t, session)
                if service.access.levels:
                    if self.o.key is None:
                        t.log(f"{sid:02X} needs an unlocked ECU and no key source is set: not tested")
                        continue
                    self.unlock(t, min(service.access.levels))
                for request in wrong:
                    self.negative(t, request, "incorrect_length",
                                  f"{self.d.session_name(session)}: {_hex(request)} has an incorrect length")
        if checks:
            self._add("Message length", "Requests too short or too long", case)

    def _sub_functions(self):
        checks = []
        for sid, service in sorted(self.d.services.items()):
            if sid not in SUB_FUNCTION_SERVICES or sid in (0x10, 0x3E) or not service.sub_functions:
                continue
            session = self.first_session(service.access)
            if session is None:
                continue
            used = set(service.sub_functions) | ({level + 1 for level in self.d.security_levels} if sid == 0x27 else set())
            unused = self.unused(UNUSED_LEVELS if sid == 0x27 else UNUSED_SUB_FUNCTIONS, used)
            rid = next(iter(sorted(self.d.routines)), 0xFF00)
            tail = {0x19: b"\xff", 0x28: b"\x01", 0x31: rid.to_bytes(2, "big"), 0x86: b"\x02"}.get(sid, b"")
            checks.append((sid, session, bytes([sid, unused]) + tail))

        def case(t):
            for sid, session, request in checks:
                self.enter(t, session)
                self.negative(t, request, "sub_function_not_supported",
                              f"{self.d.session_name(session)}: {_hex(request)}: no such sub-function")
        if checks:
            self._add("Sub-functions", "Sub-functions the ECU does not have", case)

    def _data_identifiers(self):
        group = "Data identifiers"
        for did, entry in sorted(self.d.dids.items()):
            if entry.read is None:
                continue

            def read(t, did=did, entry=entry):
                request = b"\x22" + did.to_bytes(2, "big")
                length = 3 + entry.length if entry.length else None
                for session in self.sessions():
                    self.enter(t, session)
                    name = self.d.session_name(session)
                    if not self.allowed(0x22, session):
                        self.negative(t, request, "service_not_in_session",
                                      f"{name}: ReadDataByIdentifier is not allowed")
                    elif not entry.read.allows(session):
                        self.negative(t, request, "did_not_in_session",
                                      f"{name}: not readable in this session, NRC 0x31")
                    elif entry.read.levels:
                        self.negative(t, request, "locked", f"{name}: locked, NRC 0x33")
                        if self.o.key is not None and self.unlock(t, min(entry.read.levels)):
                            self.positive(t, request, f"{name}: unlocked, read", echo=did.to_bytes(2, "big"),
                                          length=length)
                    else:
                        self.positive(t, request, f"{name}: read ({entry.length or '?'} bytes)",
                                      echo=did.to_bytes(2, "big"), length=length)
            self._add(group, f"Read {entry.name} ({did:04X})", read)

        unknown = self.unused(UNUSED_DIDS, self.d.dids)
        if 0x22 in self.d.services:
            def unknown_did(t):
                for session in self.sessions():
                    if self.allowed(0x22, session):
                        self.enter(t, session)
                        self.negative(t, b"\x22" + unknown.to_bytes(2, "big"), "did_unknown",
                                      f"{self.d.session_name(session)}: DID {unknown:04X} does not exist")
            self._add(group, f"A DID that does not exist ({unknown:04X})", unknown_did)

        if 0x2E not in self.d.services:
            return
        read_only = [did for did, entry in sorted(self.d.dids.items()) if entry.write is None]
        session = self.first_session(self.d.services[0x2E].access)
        if read_only and session is not None and "writing" not in self.d.unknown:
            def not_writable(t):
                self.enter(t, session)
                for did in read_only:
                    data = bytes(self.d.dids[did].length or 1)
                    self.negative(t, b"\x2e" + did.to_bytes(2, "big") + data, "did_read_only",
                                  f"{did:04X} ({self.d.dids[did].name}) is not writable, NRC 0x31")
            self._add(group, "Writing a read-only DID", not_writable)
        for did, entry in sorted(self.d.dids.items()):
            if entry.write is None or not self.o.destructive:
                continue
            write_session = self.first_session(entry.write)
            if write_session is None or entry.read is None:
                continue

            def write(t, did=did, entry=entry, session=write_session):
                self.enter(t, session)
                identifier = did.to_bytes(2, "big")
                if entry.write.levels:
                    probe = b"\x2e" + identifier + bytes(entry.length or 1)
                    self.negative(t, probe, "locked", "locked: NRC 0x33, nothing written")
                    if self.o.key is None:
                        t.skip("no key source set for SecurityAccess")
                    self.unlock(t, min(entry.write.levels))
                current = self.positive(t, b"\x22" + identifier, "its value, read first", echo=identifier)
                if current is None:
                    t.fail("the DID could not be read")
                value = current[3:]
                self.positive(t, b"\x2e" + identifier + value, "its own value written back", echo=identifier, length=3)
                self.negative(t, b"\x2e" + identifier + value + b"\x00", "incorrect_length",
                              "one byte too many: NRC 0x13")
            self._add(group, f"Write {entry.name} ({did:04X}) back", write)

    def _did_values(self):
        """Each DID the description gives fields for: its values valid (their limits, their text table, text
        that is text); with destructive tests, a writable one written at its limits and out of them (0x31)."""
        group = "Data identifiers"
        for did, entry in sorted(self.d.dids.items()):
            checked = [item for item in entry.fields if item.limits() or item.encoding == "ascii"]
            session = self.first_session(entry.read) if entry.read is not None else None
            if not checked or session is None:
                continue

            def values(t, did=did, entry=entry, checked=checked, session=session):
                self.enter(t, session)
                if entry.read.levels:
                    self.unlock(t, min(entry.read.levels))
                identifier = did.to_bytes(2, "big")
                raw = self.positive(t, b"\x22" + identifier, f"{self.d.session_name(session)}: read",
                                    echo=identifier)
                if raw is None:
                    return
                for item in checked:
                    ok, shown = item.check(raw[3:])
                    t.check(ok, f"{item.name}: a valid value", shown)
            self._add(group, f"Values of {entry.name} ({did:04X})", values,
                      "Each field of the DID's data record within its limits, in its text table, or text.")

        if not self.o.destructive or 0x2E not in self.d.services:
            return
        for did, entry in sorted(self.d.dids.items()):
            limited = [item for item in entry.fields if item.numeric and item.limits()]
            if entry.write is None or entry.read is None or not limited:
                continue
            session = self.first_session(entry.write)
            if session is None or not entry.read.allows(session):
                continue

            def limits(t, did=did, entry=entry, item=limited[0], session=session):
                self.enter(t, session)
                levels = entry.write.levels | self.d.services[0x2E].access.levels
                if levels:
                    self.unlock(t, min(levels))
                identifier = did.to_bytes(2, "big")
                raw = self.positive(t, b"\x22" + identifier, "its value, read first", echo=identifier)
                if raw is None:
                    t.fail("the DID could not be read")
                original = raw[3:]
                ranges = item.limits()
                low, high = min(pair[0] for pair in ranges), max(pair[1] for pair in ranges)
                try:
                    for value in dict.fromkeys((low, high)):
                        record = item.encode(original, value)
                        self.positive(t, b"\x2e" + identifier + record, f"{item.name} = {item.shown(value)}: written",
                                      echo=identifier)
                        self.positive(t, b"\x22" + identifier, "and read back", echo=identifier + record)
                    top = (1 << (item.bits - 1)) - 1 if item.encoding == "signed" else (1 << item.bits) - 1
                    bottom = -(1 << (item.bits - 1)) if item.encoding == "signed" else 0
                    wrong = next((value for value in (high + 1, low - 1) if bottom <= value <= top and
                                  not any(a <= value <= b for a, b in ranges)), None)
                    if item.encoding == "bcd" and wrong is not None and wrong > int("9" * ((item.bits + 3) // 4)):
                        wrong = None
                    if wrong is None:
                        t.log(f"{item.name}: every coded value is valid, none out of range to write")
                    else:
                        self.negative(t, b"\x2e" + identifier + item.encode(original, wrong), "did_out_of_range",
                                      f"{item.name} out of range ({item.shown(wrong)}): refused, NRC 0x31")
                        self.positive(t, b"\x22" + identifier, "nothing written",
                                      echo=identifier + item.encode(original, high))
                finally:
                    self.positive(t, b"\x2e" + identifier + original, "its own value written back", echo=identifier)
            self._add(group, f"Write {entry.name} ({did:04X}): its limits, and out of them", limits,
                      "The lowest and highest valid values written and read back, a value out of range refused "
                      "with 0x31, and the DID's value put back.")

    def _security(self):
        if 0x27 not in self.d.services:
            return
        service = self.d.services[0x27]
        for level, name in sorted(self.d.security_levels.items()):
            access = service.sub_functions.get(level, service.access)
            session = self.first_session(access)
            if session is None:
                continue

            def case(t, level=level, session=session):
                self.enter(t, session)
                self.negative(t, bytes([0x27, level + 1, 0x00, 0x00, 0x00, 0x00]), "key_before_seed",
                              "sendKey before requestSeed: NRC 0x24 requestSequenceError")
                seed = self.positive(t, bytes([0x27, level]), "requestSeed answers a seed", echo=[level])
                if seed is None:
                    return
                t.check(len(seed) > 2 and any(seed[2:]), "the seed is not empty and not zero while locked", _hex(seed))
                wrong = bytes(byte ^ 0xFF for byte in (self.o.key(level, seed[2:]) if self.o.key else seed[2:]))
                self.negative(t, bytes([0x27, level + 1]) + wrong, "invalid_key", "a wrong key: NRC 0x35 invalidKey")
                if self.o.key is None:
                    t.log("No key source: unlocking is not tested")
                    return
                self.unlock(t, level)
                raw = self.positive(t, bytes([0x27, level]), "unlocked, requestSeed answers a zero seed", echo=[level])
                if raw is not None:
                    t.check(not any(raw[2:]), "the seed is zero", _hex(raw))
                self.enter(t, DEFAULT_SESSION)
                self.enter(t, session)
                raw = self.positive(t, bytes([0x27, level]), "a new session locks again: a seed", echo=[level])
                if raw is not None:
                    t.check(any(raw[2:]), "the seed is not zero", _hex(raw))
            self._add("Security access", f"{name} (27 {level:02X} / {level + 1:02X})", case)

            def seeds(t, level=level, session=session):
                found = []
                for attempt in range(SEED_ATTEMPTS):
                    self.enter(t, DEFAULT_SESSION)
                    self.enter(t, session)
                    raw = self.positive(t, bytes([0x27, level]), f"attempt {attempt + 1}: requestSeed", echo=[level])
                    if raw is None:
                        return
                    found.append(bytes(raw[2:]))
                if len(found[0]) < 2:
                    t.log(f"seeds of {len(found[0])} byte: they may repeat by chance")
                    return
                t.check(len(set(found)) == len(found), f"{len(found)} seeds, each for a new attempt, all different",
                        ", ".join(_hex(seed) for seed in found))
            self._add("Security access", f"{name}: seeds do not repeat", seeds,
                      "requestSeed in a new session each time: every seed is new.")

            def key_length(t, level=level, session=session):
                self.enter(t, session)
                for change, what in ((1, "a byte too many"), (-1, "a byte too few")):
                    seed = self.positive(t, bytes([0x27, level]), "requestSeed", echo=[level])
                    if seed is None:
                        return
                    key = bytes(self.o.key(level, bytes(seed[2:])))
                    if change < 0 and len(key) < 2:
                        break
                    wrong = key + b"\x00" if change > 0 else key[:-1]
                    self.negative(t, bytes([0x27, level + 1]) + wrong, "key_wrong_length",
                                  f"the key with {what} ({len(wrong)} bytes): NRC 0x13")
                self.unlock(t, level)                       # a good key: what the attempts counted is cleared
            if self.o.key is not None:
                self._add("Security access", f"{name}: a key of the wrong length", key_length,
                          "sendKey with a byte too many and a byte too few gets NRC 0x13; then the right key unlocks.")

            if self.o.lockout:
                def lockout(t, level=level, session=session):
                    self.enter(t, session)
                    for attempt in range(1, self.o.attempts + 1):
                        seed = self.positive(t, bytes([0x27, level]), f"attempt {attempt}: requestSeed", echo=[level])
                        if seed is None:
                            return
                        wrong = bytes(byte ^ 0xFF for byte in seed[2:])
                        expected = "attempts_exceeded" if attempt == self.o.attempts else "invalid_key"
                        self.negative(t, bytes([0x27, level + 1]) + wrong, expected, f"attempt {attempt}: a wrong key")
                    self.negative(t, bytes([0x27, level]), "delay_not_expired",
                                  "locked out: NRC 0x37 requiredTimeDelayNotExpired")
                    deadline = time.monotonic() + self.o.lockout_seconds + 0.5
                    while time.monotonic() < deadline:
                        t.wait(min(2.0, max(0.0, deadline - time.monotonic())))
                        self.tester.quiet(b"\x3e\x80")               # the session stays
                    self.positive(t, bytes([0x27, level]), "after the delay: a seed again", echo=[level])
                self._add("Security access", f"{name}: lockout after {self.o.attempts} wrong keys", lockout)

            reset = self.d.services.get(0x11)
            if self.o.lockout and self.o.destructive and reset is not None and \
                    (not reset.sub_functions or 0x01 in reset.sub_functions) and \
                    self.o.lockout_seconds > self.o.reset_time:          # else the delay is over before it is back
                def lockout_reset(t, level=level, session=session):
                    self.enter(t, session)
                    for attempt in range(1, self.o.attempts + 1):
                        seed = self.positive(t, bytes([0x27, level]), f"attempt {attempt}: requestSeed", echo=[level])
                        if seed is None:
                            return
                        self.tester.ask(bytes([0x27, level + 1]) + bytes(byte ^ 0xFF for byte in seed[2:]))
                    locked = time.monotonic()
                    self.positive(t, b"\x11\x01", "locked out, then an ECU reset (11 01)", echo=[0x01])
                    t.wait(self.o.reset_time)
                    self.enter(t, session)
                    if time.monotonic() - locked >= self.o.lockout_seconds:
                        t.skip(f"the delay ({self.o.lockout_seconds:g} s) was over before the ECU was back")
                    self.negative(t, bytes([0x27, level]), "delay_not_expired",
                                  "after the reset, still locked out: NRC 0x37")
                    deadline = time.monotonic() + self.o.lockout_seconds + 0.5
                    while time.monotonic() < deadline:
                        t.wait(min(2.0, max(0.0, deadline - time.monotonic())))
                        self.tester.quiet(b"\x3e\x80")
                    self.positive(t, bytes([0x27, level]), "after the delay: a seed again", echo=[level])
                self._add("Security access", f"{name}: the lockout outlasts an ECU reset", lockout_reset,
                          "Locked out by wrong keys, the ECU keeps its delay through an ECU reset (ISO 14229-1: "
                          "the delay timer restarts after a power-on).")
        self._levels_apart(service)

    def _levels_apart(self, service):
        """With two levels or more: unlocking one does not unlock the other."""
        levels = sorted(self.d.security_levels)
        if len(levels) < 2 or self.o.key is None:
            return
        first, other = levels[0], levels[1]
        session = next((s for s in self.sessions() if service.sub_functions.get(first, service.access).allows(s)
                        and service.sub_functions.get(other, service.access).allows(s)), None)
        if session is None:
            return
        # Something the other level alone opens: a DID read, else a DID write, needing it and not the first.
        needs_other = next((("22", did) for did, entry in sorted(self.d.dids.items()) if entry.read is not None
                            and entry.read.levels == {other} and entry.read.allows(session)), None)

        def case(t):
            self.enter(t, session)
            self.unlock(t, first)
            raw = self.positive(t, bytes([0x27, other]), f"level 0x{first:02X} unlocked: requestSeed 0x{other:02X}",
                                echo=[other])
            if raw is not None:
                t.check(any(raw[2:]), f"level 0x{other:02X} is still locked: its seed is not zero", _hex(raw))
            if needs_other is not None:
                _service, did = needs_other
                self.negative(t, b"\x22" + did.to_bytes(2, "big"), "locked",
                              f"22 {did:04X} (level 0x{other:02X} only) is still refused: NRC 0x33")
            self.unlock(t, other)
            if needs_other is not None:
                _service, did = needs_other
                self.positive(t, b"\x22" + did.to_bytes(2, "big"), f"level 0x{other:02X} unlocked: 22 {did:04X} "
                              f"is read", echo=did.to_bytes(2, "big"))
        self._add("Security access", f"Levels 0x{first:02X} and 0x{other:02X} are apart", case,
                  "Unlocking one security level does not unlock another.")

    def _routines(self):
        if 0x31 not in self.d.services:
            return
        service = self.d.services[0x31]
        for rid, routine in sorted(self.d.routines.items()):
            start = routine.sub_functions.get(0x01)
            if start is None:
                continue

            def case(t, rid=rid, start=start, routine=routine):
                request = b"\x31\x01" + rid.to_bytes(2, "big")
                for session in self.sessions():
                    if not service.access.allows(session):
                        continue
                    self.enter(t, session)
                    name = self.d.session_name(session)
                    if not start.allows(session):
                        self.negative(t, request, "routine_not_in_session", f"{name}: not in this session")
                    elif start.levels:
                        self.negative(t, request, "locked", f"{name}: locked, NRC 0x33")
                    else:
                        t.log(f"{name}: allowed; not started (routines are not run)")
            self._add("Routines", f"{routine.name} ({rid:04X})", case)
        unknown = self.unused(UNUSED_RIDS, self.d.routines)
        session = self.first_session(service.access)
        if session is not None:
            def unknown_routine(t):
                self.enter(t, session)
                self.negative(t, b"\x31\x01" + unknown.to_bytes(2, "big"), "routine_unknown",
                              f"routine {unknown:04X} does not exist")
            self._add("Routines", f"A routine that does not exist ({unknown:04X})", unknown_routine)

    def _routine_control(self):
        """Routines with a stop or results: asked before a start (0x24); the ones a run may start, started,
        asked for their results and stopped."""
        if 0x31 not in self.d.services:
            return
        starts = parse_routine_starts(self.o.start_routines)
        for rid, routine in sorted(self.d.routines.items()):
            controls = [sub for sub in (0x03, 0x02) if sub in routine.sub_functions]
            access = routine.sub_functions.get(controls[0]) if controls else None
            session = self.first_session(access) if access is not None else None
            if session is None:
                continue
            identifier = rid.to_bytes(2, "big")

            def sequence(t, rid=rid, routine=routine, controls=controls, access=access, session=session,
                         identifier=identifier):
                self.enter(t, session)
                if access.levels:
                    self.unlock(t, min(access.levels))
                for sub in controls:
                    what = "its results" if sub == 0x03 else "a stop"
                    self.negative(t, b"\x31" + bytes([sub]) + identifier, "routine_sequence",
                                  f"{what} before a start: NRC 0x24 requestSequenceError")
            self._add("Routines", f"{routine.name} ({rid:04X}): stop and results before a start", sequence)

        for rid, record in starts.items():
            routine = self.d.routines.get(rid)
            start = routine.sub_functions.get(0x01) if routine else None
            session = self.first_session(start) if start is not None else None
            if session is None:
                continue
            identifier = rid.to_bytes(2, "big")

            def run(t, rid=rid, routine=routine, start=start, session=session, record=record, identifier=identifier):
                self.enter(t, session)
                if start.levels:
                    self.unlock(t, min(start.levels))
                raw = self.positive(t, b"\x31\x01" + identifier + record, "started (31 01)",
                                    echo=b"\x01" + identifier)
                if raw is None:
                    return
                if 0x03 in routine.sub_functions:
                    self.positive(t, b"\x31\x03" + identifier, "its results (31 03)", echo=b"\x03" + identifier)
                if 0x02 in routine.sub_functions:
                    self.positive(t, b"\x31\x02" + identifier, "stopped (31 02)", echo=b"\x02" + identifier)
                    if 0x03 in routine.sub_functions:
                        self.positive(t, b"\x31\x03" + identifier, "its results after the stop",
                                      echo=b"\x03" + identifier)
            self._add("Routines", f"Start {routine.name} ({rid:04X})", run,
                      "The routine started with its option record, its results asked for, and stopped.")

    def _fault_memory(self):
        service = self.d.service(0x19)
        if service is not None:
            session = self.first_session(service.access)
            subs = service.sub_functions

            def read(t):
                self.enter(t, session)
                if not subs or 0x01 in subs:
                    self.positive(t, b"\x19\x01\xff", "19 01 FF: the number of DTCs (6 bytes)", echo=[0x01], length=6)
                if not subs or 0x02 in subs:
                    raw = self.positive(t, b"\x19\x02\xff", "19 02 FF: the DTCs by status mask", echo=[0x02])
                    if raw is not None:
                        t.check(len(raw) >= 3 and (len(raw) - 3) % 4 == 0,
                                "availability mask, then four bytes a DTC", _hex(raw))
                if 0x0A in subs:
                    raw = self.positive(t, b"\x19\x0a", "19 0A: every supported DTC", echo=[0x0A])
                    if raw is not None:
                        t.check((len(raw) - 3) % 4 == 0, "availability mask, then four bytes a DTC", _hex(raw))
            if session is not None:
                self._add("Fault memory", "ReadDTCInformation (19)", read)
        clear = self.d.service(0x14)
        if clear is not None and self.first_session(clear.access) is not None:
            session = self.first_session(clear.access)

            def clear_case(t):
                self.enter(t, session)
                self.negative(t, b"\x14" + UNUSED_DTC_GROUP.to_bytes(3, "big"), "dtc_group_unknown",
                              f"14 {UNUSED_DTC_GROUP:06X}: no such group of DTCs")
                if self.o.destructive:
                    self.positive(t, b"\x14\xff\xff\xff", "14 FF FF FF: every DTC cleared", length=1)
                else:
                    t.log("Clearing every DTC is a destructive test: not run")
            self._add("Fault memory", "ClearDiagnosticInformation (14)", clear_case)

    def _communication(self):
        for sid, enable, other, response in ((0x28, b"\x00\x01", None, 0x68), (0x85, b"\x01", b"\x02", 0xC5)):
            service = self.d.service(sid)
            if service is None:
                continue
            session = self.first_session(service.access)
            if session is None:
                continue

            def case(t, sid=sid, enable=enable, other=other, session=session):
                self.enter(t, session)
                self.positive(t, bytes([sid]) + enable, f"{sid:02X} {_hex(enable)} is answered", echo=enable[:1])
                if other is not None:
                    self.positive(t, bytes([sid]) + other, f"{sid:02X} {_hex(other)} is answered", echo=other[:1])
                self.silent(t, bytes([sid, enable[0] | 0x80]) + enable[1:], "the suppress bit set: no answer")
                self.positive(t, bytes([sid]) + enable, "back as it was", echo=enable[:1])
            self._add("Communication", f"{SERVICE_NAMES.get(sid, '')} ({sid:02X})", case)

    def _reset(self):
        service = self.d.service(0x11)
        if service is None or not self.o.destructive:
            return
        subs = sorted(service.sub_functions) or [0x01]
        session = self.first_session(service.access)
        if session is None:
            return
        others = [s for s in self.sessions() if s != DEFAULT_SESSION and service.access.allows(s)]

        def case(t):
            before = others[0] if others else session
            self.enter(t, before)
            self.positive(t, bytes([0x11, subs[0]]), f"11 {subs[0]:02X} is answered", echo=[subs[0]])
            t.wait(self.o.reset_time)
            active = self.d.dids.get(0xF186)
            if active is not None and active.read is not None:
                raw = self.positive(t, b"\x22\xf1\x86", "after the reset: the active session (F186)", echo=[0xF1, 0x86])
                if raw is not None:
                    t.check(raw[3:4] == bytes([DEFAULT_SESSION]), "the default session", _hex(raw))
            else:
                self.positive(t, b"\x10\x01", "after the reset: the ECU answers", echo=[0x01])
            self.enter(t, session)
            self.negative(t, bytes([0x11, self.unused(UNUSED_SUB_FUNCTIONS, service.sub_functions)]),
                          "sub_function_not_supported", "a reset type that does not exist")
            self.negative(t, bytes([0x11, subs[0], 0x00]), "incorrect_length", "one byte too many")
        self._add("ECU reset", f"ECUReset (11 {subs[0]:02X})", case)

    def _functional(self):
        if not self.o.functional:
            return
        missing = next((sid for sid in ISO_SERVICES if sid not in self.d.services), None)
        unknown = self.unused(UNUSED_DIDS, self.d.dids)

        def case(t):
            if self.tester.functional_id is None:
                t.skip("no functional request ID set")
            self.positive(t, b"\x10\x01", "10 01 functional is answered", echo=[0x01], functional=True)
            if 0x3E in self.d.services:
                self.positive(t, b"\x3e\x00", "3E 00 functional is answered", echo=[0x00], functional=True)
                self.silent(t, b"\x3e\x80", "3E 80 functional: no answer", functional=True)
            if missing is not None:
                self.silent(t, bytes([missing]), f"{missing:02X} (not supported) functional: NRC 0x11 is not sent",
                            functional=True)
            self.silent(t, bytes([0x10, self.unused(UNUSED_SUB_FUNCTIONS, self.d.sessions)]),
                        "a session that does not exist, functional: NRC 0x12 is not sent", functional=True)
            if 0x22 in self.d.services:
                self.silent(t, b"\x22" + unknown.to_bytes(2, "big"),
                            f"DID {unknown:04X} functional: NRC 0x31 is not sent", functional=True)
        self._add("Functional addressing", "Functional requests (ISO 14229-1 7.5)", case,
                  "A functional request answers positively; NRC 0x11, 0x12, 0x31, 0x7E and 0x7F are not sent.")

    def _timing(self):
        short = next((did for did in sorted(self.d.dids) if self.d.dids[did].read is not None
                      and not self.d.dids[did].read.levels and self.d.dids[did].read.allows(DEFAULT_SESSION)
                      and (self.d.dids[did].length or 99) <= 4), None)

        def case(t):
            limit = self.p2 + self.o.timing_margin_ms / 1000
            requests = [b"\x10\x01"] + ([b"\x3e\x00"] if 0x3E in self.d.services else []) + \
                ([b"\x22" + short.to_bytes(2, "big")] if short is not None else [])
            for request in requests:
                answer = self.tester.ask(request)
                first = answer.first if answer.first is not None else answer.elapsed     # 0x78 counts
                t.check(answer.positive() and first <= limit,
                        f"{_hex(request)} answered within P2 ({self.p2 * 1000:.0f} ms + {self.o.timing_margin_ms} ms)",
                        answer.text())
                self._covered(t, answer)
        self._add("Timing", "Responses within P2", case)

    def _session_probe(self, session):
        """How to tell whether the ECU is in session rather than the default one: reading F186, else a
        harmless request of a service allowed there and not in the default session. None: no way."""
        active = self.d.dids.get(0xF186)
        if active is not None and active.read is not None and not active.read.levels and \
                active.read.allows(DEFAULT_SESSION) and active.read.allows(session):
            return "did"
        sid = next((sid for sid in sorted(HARMLESS) if sid in self.d.services and self.allowed(sid, session)
                    and not self.allowed(sid, DEFAULT_SESSION) and not self.d.services[sid].access.levels), None)
        return sid

    def _in_session(self, t, probe, session, what):
        """A step: the ECU is in session (by the probe of _session_probe)."""
        if probe == "did":
            raw = self.positive(t, b"\x22\xf1\x86", what, echo=b"\xf1\x86")
            if raw is not None:
                t.check(raw[3:4] == bytes([session]), f"the {self.d.session_name(session)} (F186 = {session:02X})",
                        _hex(raw))
        elif session == DEFAULT_SESSION:
            self.negative(t, self.minimal(probe), "service_not_in_session", f"{what}: {probe:02X} is refused again")
        else:
            self.available(t, self.minimal(probe), f"{what}: {probe:02X} is still answered")

    def _s3(self):
        """S3 (ISO 14229-2): a session other than the default one ends after S3 without a request, and
        TesterPresent keeps it."""
        if not self.o.s3_test:
            return
        session = next((s for s in self.sessions() if s != DEFAULT_SESSION and self._session_probe(s) is not None),
                       None)
        if session is None:
            return
        probe = self._session_probe(session)
        margin = self.o.timing_margin_ms / 1000
        name = self.d.session_name(session)

        def timeout(t):
            self.enter(t, session)
            t.log(f"No request for {self.o.s3_seconds + 0.5 + margin:.1f} s (S3 {self.o.s3_seconds:g} s)")
            t.wait(self.o.s3_seconds + 0.5 + margin)
            self._in_session(t, probe, DEFAULT_SESSION, "after S3 without a request, the default session")
        self._add("Timing", f"S3: the {name} ends without requests", timeout)
        if 0x3E not in self.d.services:
            return

        def kept(t):
            self.enter(t, session)
            period = max(0.5, self.o.s3_seconds / 2.5)
            deadline = time.monotonic() + self.o.s3_seconds + 1.0
            t.log(f"TesterPresent (3E 80) every {period:.1f} s for {self.o.s3_seconds + 1:.1f} s")
            while time.monotonic() < deadline:
                self.tester.quiet(b"\x3e\x80")
                t.wait(min(period, max(0.0, deadline - time.monotonic())))
            self._in_session(t, probe, session, f"with TesterPresent, still the {name}")
        self._add("Timing", f"S3: TesterPresent keeps the {name}", kept)
