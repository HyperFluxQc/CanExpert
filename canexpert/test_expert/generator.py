"""
The tests TestExpert generates from a description, as DiVa does: for every session, service, sub-function,
DID, routine and security level of the description, what ISO 14229-1 says the ECU must answer - positive
where it is allowed, and the right negative response code where it is not.

Each test case is a function run by canexpert.testing.runner (verdicts per step, Stop, HTML and JUnit reports);
Suite.module() gives them as a test module. What changes the ECU for good - ECU reset, clearing the fault
memory, writing a DID (its own value back), the security lockout - runs only when Options ask for it.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from canexpert.test_expert.description import DEFAULT_SESSION, ISO_SERVICES, SUB_FUNCTION_SERVICES, EcuDescription
from canexpert.testing.runner import TestCase, TestModule
from canexpert.uds.client import NRC_NAMES
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


@dataclass
class Options:
    destructive: bool = False     # ECU reset, clearing DTCs, writing DIDs (their own value back)
    lockout: bool = False         # wrong keys until the lockout, then its delay (slow)
    functional: bool = True       # functionally addressed requests (needs the functional ID)
    key: object = None            # key(level, seed) -> bytes; None: what needs an unlocked ECU is skipped
    timing_margin_ms: int = 50    # tolerance over P2 for the response time
    reset_time: float = 1.0       # seconds an ECU reset takes
    attempts: int = 3             # wrong keys before the lockout
    lockout_seconds: float = 10.0


def _hex(data) -> str:
    return bytes(data).hex(" ").upper()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class Suite:
    """The generated test cases of a description; tester is set before a run (a test_expert.tester.Tester)."""

    def __init__(self, description: EcuDescription, options: Options | None = None):
        self.d = description
        self.o = options or Options()
        self.tester = None
        self.p2 = 0.05                     # the P2 the ECU announces in its default session answer
        self.cases: list[TestCase] = []
        self._build()

    # --- the module ----------------------------------------------------------------------------------

    def module(self) -> TestModule:
        path = Path(self.d.source) if self.d.source and Path(self.d.source).suffix else Path(_slug(self.d.name))
        hooks = {"setup": self._setup, "teardown": self._default, "before_each": self._default,
                 "after_each": self._default}
        return TestModule(path, f"TestExpert: {self.d.name}", list(self.cases), hooks, {})

    def groups(self) -> dict[str, list[TestCase]]:
        grouped = {}
        for case in self.cases:
            grouped.setdefault(case.title.split(":")[0], []).append(case)
        return grouped

    def _add(self, group, title, function, doc=""):
        name = f"{_slug(group)}_{len(self.cases) + 1:03d}"
        self.cases.append(TestCase(name, f"{group}: {title}", function, doc))

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
        return raw if ok else None

    def negative(self, t, payload, nrc, what, functional=False, tolerated=()):
        """A step: NRC nrc (or one of them: the first is ISO's). An NRC in tolerated passes with a note."""
        expected = (nrc,) if isinstance(nrc, int) else tuple(nrc)
        answer = self.tester.ask(payload, functional)
        got = answer.nrc
        good = answer.raw is not None and answer.raw[:2] == bytes([0x7F, payload[0]]) and got in expected
        if not good and got in tolerated and answer.raw[1] == payload[0]:
            t.check(True, what, f"{answer.text()} - ISO 14229-1 asks for NRC 0x{expected[0]:02X} "
                               f"{NRC_NAMES.get(expected[0], '')}; tolerated")
            return True
        wanted = " or ".join(f"0x{code:02X} {NRC_NAMES.get(code, '')}" for code in expected)
        return t.check(good, what, answer.text() if good else f"{answer.text()} - expected NRC {wanted}")

    def silent(self, t, payload, what, functional=False):
        """A step: no answer at all."""
        answer = self.tester.quiet(payload, functional)
        return t.check(answer.raw is None, what, answer.text())

    def available(self, t, payload, what):
        """A step: the service answers - positive, or refused for another reason than not being there."""
        answer = self.tester.ask(payload)
        ok = answer.raw is not None and answer.nrc not in (0x11, 0x7F)
        return t.check(ok, what, answer.text())

    def path_to(self, session) -> list[int]:
        if session == DEFAULT_SESSION:
            return [DEFAULT_SESSION]
        sources = self.d.sessions[session].entered_from if session in self.d.sessions else set()
        if not sources or DEFAULT_SESSION in sources:
            return [DEFAULT_SESSION, session]
        via = next((s for s in sorted(sources) if s != session and s in self.d.sessions and
                    (not self.d.sessions[s].entered_from or DEFAULT_SESSION in self.d.sessions[s].entered_from)), None)
        return [DEFAULT_SESSION, via, session] if via is not None else [DEFAULT_SESSION, session]

    def enter(self, t, session):
        """Into a session, through the sessions it must be entered from; the case ends if one refuses."""
        for step in self.path_to(session):
            answer = self.tester.ask(bytes([0x10, step]))
            if not answer.positive():
                t.require(False, f"enter {self.d.session_name(step)} (10 {step:02X})", answer.text())

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
        answer = self.tester.ask(b"\x10\x01")
        t.require(answer.positive(), "the ECU answers DiagnosticSessionControl default (10 01)", answer.text())
        if answer.raw is not None and len(answer.raw) >= 4:
            p2 = int.from_bytes(answer.raw[2:4], "big") / 1000
            if 0 < p2 < 5:
                self.p2 = p2
                t.log(f"P2 {p2 * 1000:.0f} ms, as the ECU announces")

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
        self._security()
        self._routines()
        self._fault_memory()
        self._communication()
        self._reset()
        self._functional()
        self._timing()

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
                    self.negative(t, bytes([0x10, session]), (0x22, 0x7E),
                                  f"from the default session, 10 {session:02X} is refused")
                self._add(group, f"The {self.d.session_name(session)} is not entered from the default session",
                          refused)

        unused = self.unused(UNUSED_SUB_FUNCTIONS, self.d.sessions)

        def unsupported(t):
            self.negative(t, bytes([0x10, unused]), 0x12, f"10 {unused:02X}: no such session")
            self.negative(t, bytes([0x10, unused | 0x80]), 0x12,
                          f"10 {unused | 0x80:02X}: the suppress bit does not hide a negative response")
        self._add(group, "A session that does not exist", unsupported)

        def length(t):
            self.negative(t, b"\x10", 0x13, "10 alone: incorrect length")
            self.negative(t, b"\x10\x01\x00", 0x13, "10 01 00: one byte too many")
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
            self.negative(t, b"\x3e\x01", 0x12, "3E 01: zeroSubFunction only")
            self.negative(t, b"\x3e\x81", 0x12, "3E 81: the suppress bit does not hide a negative response")
            self.negative(t, b"\x3e", 0x13, "3E alone: incorrect length")
            self.negative(t, b"\x3e\x00\x00", 0x13, "3E 00 00: one byte too many")
        self._add("TesterPresent", "TesterPresent (3E)", case)

    def _unsupported_services(self):
        missing = [sid for sid in ISO_SERVICES if sid not in self.d.services]
        if not missing:
            return

        def case(t):
            for sid in missing:
                self.negative(t, bytes([sid]), 0x11, f"{sid:02X} {SERVICE_NAMES.get(sid, '')}: not supported")
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
                        self.negative(t, request, 0x7F, f"{name}: {_hex(request)} is refused, NRC 0x7F")
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
                self.negative(t, bytes([sid]), 0x7F, f"{name}: {sid:02X} alone gets 0x7F before 0x13")
                if sid in SUB_FUNCTION_SERVICES:
                    unused = self.unused(UNUSED_SUB_FUNCTIONS, service.sub_functions)
                    self.negative(t, bytes([sid, unused, 0x00, 0x00]), 0x7F,
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
                    self.negative(t, request, 0x13, f"{self.d.session_name(session)}: {_hex(request)} has an "
                                                    f"incorrect length")
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
                self.negative(t, request, 0x12, f"{self.d.session_name(session)}: {_hex(request)}: no such "
                                                f"sub-function")
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
                        self.negative(t, request, 0x7F, f"{name}: ReadDataByIdentifier is not allowed")
                    elif not entry.read.allows(session):
                        self.negative(t, request, 0x31, f"{name}: not readable in this session, NRC 0x31")
                    elif entry.read.levels:
                        self.negative(t, request, 0x33, f"{name}: locked, NRC 0x33")
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
                        self.negative(t, b"\x22" + unknown.to_bytes(2, "big"), 0x31,
                                      f"{self.d.session_name(session)}: DID {unknown:04X} does not exist")
            self._add(group, f"A DID that does not exist ({unknown:04X})", unknown_did)

        if 0x2E not in self.d.services:
            return
        read_only = [did for did, entry in sorted(self.d.dids.items()) if entry.write is None]
        session = self.first_session(self.d.services[0x2E].access)
        if read_only and session is not None:
            def not_writable(t):
                self.enter(t, session)
                for did in read_only:
                    data = bytes(self.d.dids[did].length or 1)
                    self.negative(t, b"\x2e" + did.to_bytes(2, "big") + data, 0x31,
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
                    self.negative(t, probe, 0x33, "locked: NRC 0x33, nothing written")
                    if self.o.key is None:
                        t.skip("no key source set for SecurityAccess")
                    self.unlock(t, min(entry.write.levels))
                current = self.positive(t, b"\x22" + identifier, "its value, read first", echo=identifier)
                if current is None:
                    t.fail("the DID could not be read")
                value = current[3:]
                self.positive(t, b"\x2e" + identifier + value, "its own value written back", echo=identifier, length=3)
                self.negative(t, b"\x2e" + identifier + value + b"\x00", 0x13, "one byte too many: NRC 0x13")
            self._add(group, f"Write {entry.name} ({did:04X}) back", write)

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
                self.negative(t, bytes([0x27, level + 1, 0x00, 0x00, 0x00, 0x00]), 0x24,
                              "sendKey before requestSeed: NRC 0x24 requestSequenceError")
                seed = self.positive(t, bytes([0x27, level]), "requestSeed answers a seed", echo=[level])
                if seed is None:
                    return
                t.check(len(seed) > 2 and any(seed[2:]), "the seed is not empty and not zero while locked", _hex(seed))
                wrong = bytes(byte ^ 0xFF for byte in (self.o.key(level, seed[2:]) if self.o.key else seed[2:]))
                self.negative(t, bytes([0x27, level + 1]) + wrong, 0x35, "a wrong key: NRC 0x35 invalidKey")
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

            if self.o.lockout:
                def lockout(t, level=level, session=session):
                    self.enter(t, session)
                    for attempt in range(1, self.o.attempts + 1):
                        seed = self.positive(t, bytes([0x27, level]), f"attempt {attempt}: requestSeed", echo=[level])
                        if seed is None:
                            return
                        wrong = bytes(byte ^ 0xFF for byte in seed[2:])
                        expected = 0x36 if attempt == self.o.attempts else 0x35
                        self.negative(t, bytes([0x27, level + 1]) + wrong, expected, f"attempt {attempt}: a wrong key")
                    self.negative(t, bytes([0x27, level]), 0x37, "locked out: NRC 0x37 requiredTimeDelayNotExpired")
                    deadline = time.monotonic() + self.o.lockout_seconds + 0.5
                    while time.monotonic() < deadline:
                        t.wait(min(2.0, max(0.0, deadline - time.monotonic())))
                        self.tester.quiet(b"\x3e\x80")               # the session stays
                    self.positive(t, bytes([0x27, level]), "after the delay: a seed again", echo=[level])
                self._add("Security access", f"{name}: lockout after {self.o.attempts} wrong keys", lockout)

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
                        self.negative(t, request, 0x31, f"{name}: not in this session", tolerated=(0x7F, 0x7E))
                    elif start.levels:
                        self.negative(t, request, 0x33, f"{name}: locked, NRC 0x33")
                    else:
                        t.log(f"{name}: allowed; not started (routines are not run)")
            self._add("Routines", f"{routine.name} ({rid:04X})", case)
        unknown = self.unused(UNUSED_RIDS, self.d.routines)
        session = self.first_session(service.access)
        if session is not None:
            def unknown_routine(t):
                self.enter(t, session)
                self.negative(t, b"\x31\x01" + unknown.to_bytes(2, "big"), 0x31, f"routine {unknown:04X} does not exist")
            self._add("Routines", f"A routine that does not exist ({unknown:04X})", unknown_routine)

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
                self.negative(t, b"\x14" + UNUSED_DTC_GROUP.to_bytes(3, "big"), 0x31,
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
            self.negative(t, bytes([0x11, self.unused(UNUSED_SUB_FUNCTIONS, service.sub_functions)]), 0x12,
                          "a reset type that does not exist")
            self.negative(t, bytes([0x11, subs[0], 0x00]), 0x13, "one byte too many")
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
                t.check(answer.positive() and answer.elapsed <= limit,
                        f"{_hex(request)} answered within P2 ({self.p2 * 1000:.0f} ms + {self.o.timing_margin_ms} ms)",
                        answer.text())
        self._add("Timing", "Responses within P2", case)
