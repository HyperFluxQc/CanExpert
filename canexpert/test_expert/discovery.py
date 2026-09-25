"""
Discovery: what the ECU really answers - the services, DIDs, routines and security levels it has, session by
session - found by asking, and compared with its description: what the description has and the ECU does not,
what the ECU has and the description does not say (an undocumented DID is a question of security too), and
where they differ (sessions, lengths). What was found can also become a description of its own, to test an ECU
that has none.

Every probe is harmless: a service's SID alone (no service is complete, and changes something, in one byte -
the two KWP2000 ones that are, 20 and 82, are not sent), a DID read, a routine's results asked (31 03: never
started), a seed requested.
"""
from __future__ import annotations

import html
import time
from dataclasses import dataclass, field
from datetime import datetime

from canexpert.test_expert.description import (DEFAULT_SESSION, Access, DataIdentifier, EcuDescription, Routine,
                                               Service, Session)
from canexpert.uds.observer import SERVICE_NAMES

SCAN_SERVICES = tuple(sid for sid in list(range(0x10, 0x3F)) + list(range(0x80, 0x90)) if sid not in (0x20, 0x82))
DEFAULT_DIDS = "0100-02FF, F100-F2FF"
DEFAULT_RIDS = "0200-02FF, FF00-FF02"
SECURITY_LEVELS = tuple(range(0x01, 0x43, 2))          # requestSeed 01-41
SILENT_LIMIT = 20                                        # probes without an answer before the ECU is asked if it is there
# A probe's outcome in one session.
ANSWERS, NOT_IN_SESSION, NOT_SUPPORTED, SILENT = "answers", "not in session", "not supported", "silent"
SECURED = "needs security"                               # 0x33 to the SID alone: there, behind SecurityAccess
READ, LOCKED, CONDITIONS, ABSENT = "read", "locked", "conditions not correct", "absent"
FOUND = "found"


class DiscoveryStopped(Exception):
    """Stop was pressed, or the ECU no longer answers: the discovery ends with what it found."""


def parse_ranges(text: str) -> list[tuple[int, int]]:
    """"F100-F1FF, 0100" -> [(0xF100, 0xF1FF), (0x0100, 0x0100)]; ValueError naming what is wrong."""
    ranges = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        first, _, last = part.partition("-")
        try:
            low = int(first.strip().lower().removeprefix("0x"), 16)
            high = int((last or first).strip().lower().removeprefix("0x"), 16)
        except ValueError:
            raise ValueError(f"not a range of hex identifiers: {part!r}") from None
        if not 0 <= low <= high <= 0xFFFF:
            raise ValueError(f"a range of 16-bit identifiers, first to last: {part!r}")
        ranges.append((low, high))
    return ranges


def expand(ranges) -> list[int]:
    values = set()
    for low, high in ranges:
        values.update(range(low, high + 1))
    return sorted(values)


@dataclass
class DiscoveryOptions:
    sessions: list = field(default_factory=lambda: [0x01, 0x03])
    dids: str = DEFAULT_DIDS
    rids: str = DEFAULT_RIDS
    services: bool = True
    security: bool = True
    timeout: float = 0.5               # seconds for each answer


@dataclass
class Finding:
    kind: str                          # "missing" (described, not found), "undocumented", "different"
    what: str
    detail: str


@dataclass
class DiscoveryResult:
    sessions: dict = field(default_factory=dict)       # session -> "" (entered) or why it was not
    services: dict = field(default_factory=dict)       # SID -> {session: state}
    dids: dict = field(default_factory=dict)           # DID -> {session: [state, length or None]}
    routines: dict = field(default_factory=dict)       # RID -> {session: state}
    levels: dict = field(default_factory=dict)         # security level -> {session: state}
    notes: list = field(default_factory=list)
    started: float = 0.0
    duration: float = 0.0
    probes: int = 0

    def entered(self) -> list[int]:
        return [session for session, why in self.sessions.items() if not why]

    # --- what was found --------------------------------------------------------------------------------

    def service_found(self, sid) -> bool:
        return any(state in (ANSWERS, SECURED, NOT_IN_SESSION) for state in self.services.get(sid, {}).values())

    def service_sessions(self, sid) -> set:
        return {session for session, state in self.services.get(sid, {}).items() if state in (ANSWERS, SECURED)}

    def did_found(self, did) -> bool:
        return any(state in (READ, LOCKED, CONDITIONS) for state, _length in self.dids.get(did, {}).values())

    def did_sessions(self, did) -> set:
        return {session for session, (state, _length) in self.dids.get(did, {}).items() if state == READ}

    def did_length(self, did):
        lengths = {length for state, length in self.dids.get(did, {}).values() if state == READ}
        return lengths.pop() if len(lengths) == 1 else None

    def routine_found(self, rid) -> bool:
        return any(state == FOUND for state in self.routines.get(rid, {}).values())

    def found_levels(self) -> list[int]:
        return sorted(level for level, states in self.levels.items() if FOUND in states.values())

    # --- as a description ------------------------------------------------------------------------------------

    def to_description(self, name: str = "Discovered ECU", known: EcuDescription | None = None) -> EcuDescription:
        """What was found, as a description: services and DIDs with the sessions they answered in, the
        routines found and the security levels. known lends its names. Sub-functions, writing and starting
        routines cannot be found by asking harmlessly, so they are not in it."""
        description = EcuDescription(name, f"discovered {datetime.fromtimestamp(self.started or time.time()):%Y-%m-%d %H:%M}")
        entered = self.entered()

        def access(sessions) -> Access:
            return Access(set() if set(entered) <= set(sessions) else set(sessions))
        for session in entered:
            known_session = known.sessions.get(session) if known else None
            description.sessions[session] = Session(session, known_session.name if known_session else
                                                    ("Default session" if session == DEFAULT_SESSION else
                                                     f"Session 0x{session:02X}"),
                                                    set(known_session.entered_from) if known_session else set())
        for sid in sorted(self.services):
            if not self.service_found(sid):
                continue
            sessions = self.service_sessions(sid)
            name = SERVICE_NAMES.get(sid, f"Service 0x{sid:02X}")
            if not sessions:                       # refused (0x7F) in every session asked
                elsewhere = known.services[sid].access.sessions - set(entered) if known and sid in known.services \
                    else ({0x02} - set(entered))
                if not elsewhere:
                    description.warnings.append(f"{sid:02X} {name} answers 0x7F in every session asked: left out")
                    continue
                description.services[sid] = Service(sid, name, Access(set(elsewhere)))
                continue
            levels = set(self.found_levels()) or {0x01} if SECURED in self.services[sid].values() else set()
            description.services[sid] = Service(sid, name, Access(access(sessions).sessions, levels))
        for did in sorted(self.dids):
            if not self.did_found(did):
                continue
            readable = self.did_sessions(did)
            locked = {session for session, (state, _length) in self.dids[did].items() if state == LOCKED}
            known_did = known.dids.get(did) if known else None
            # Only read locked: in those sessions, with one of the levels found (01 when none was).
            read = access(readable) if readable else Access(set(locked), set(self.found_levels()) or {0x01})
            description.dids[did] = DataIdentifier(did, known_did.name if known_did else f"DID 0x{did:04X}",
                                                   self.did_length(did), read, None)
        for rid in sorted(self.routines):
            if self.routine_found(rid):
                known_routine = known.routines.get(rid) if known else None
                sessions = {session for session, state in self.routines[rid].items() if state == FOUND}
                description.routines[rid] = Routine(rid, known_routine.name if known_routine else f"Routine 0x{rid:04X}",
                                                    {0x03: access(sessions)})
        for level in self.found_levels():
            description.security_levels[level] = f"Level 0x{level:02X}"
            if 0x27 in description.services:
                sessions = {session for session, state in self.levels[level].items() if state == FOUND}
                description.services[0x27].sub_functions[level] = access(sessions)
        description.warnings.append("Discovered by asking the ECU: sub-functions, writing DIDs and starting routines "
                                    "cannot be found that way - add them to the JSON where they are known")
        description.unknown = {"writing", "sub-functions", "starting routines"}
        return description

    # --- JSON ---------------------------------------------------------------------------------------------

    def to_dict(self) -> dict:
        def table(values):
            return {f"{key:04X}": {f"{session:02X}": state for session, state in states.items()}
                    for key, states in sorted(values.items())}
        return {"format": "TestExpert discovery", "started": self.started, "duration": self.duration,
                "probes": self.probes, "notes": list(self.notes),
                "sessions": {f"{session:02X}": why for session, why in self.sessions.items()},
                "services": table(self.services), "routines": table(self.routines), "levels": table(self.levels),
                "dids": {f"{did:04X}": {f"{session:02X}": list(found) for session, found in states.items()}
                         for did, states in sorted(self.dids.items())}}

    @classmethod
    def from_dict(cls, values: dict) -> "DiscoveryResult":
        def table(items):
            return {int(key, 16): {int(session, 16): state for session, state in states.items()}
                    for key, states in (items or {}).items()}
        result = cls(started=float(values.get("started", 0)), duration=float(values.get("duration", 0)),
                     probes=int(values.get("probes", 0)), notes=list(values.get("notes", ())))
        result.sessions = {int(session, 16): why for session, why in (values.get("sessions") or {}).items()}
        result.services, result.routines = table(values.get("services")), table(values.get("routines"))
        result.levels = table(values.get("levels"))
        result.dids = {int(did, 16): {int(session, 16): list(found) for session, found in states.items()}
                       for did, states in (values.get("dids") or {}).items()}
        return result


class Discovery:
    """Asks the ECU through tester (a test_expert.tester.Tester). progress(done, total, text) follows it; stop
    (a threading.Event) ends it between two probes."""

    def __init__(self, tester, description: EcuDescription | None = None, options: DiscoveryOptions | None = None,
                 progress=None, stop=None):
        self.tester, self.d = tester, description
        self.o = options or DiscoveryOptions()
        self.progress = progress or (lambda done, total, text: None)
        self.stop = stop
        self.result = DiscoveryResult()
        self._done = self._total = 0
        self._silent = 0

    def plan(self) -> tuple[list[int], list[int], list[int]]:
        """The services, DIDs and routines each session is asked for: the ranges, and all the description has."""
        described_dids = set(self.d.dids) if self.d else set()
        described_rids = set(self.d.routines) if self.d else set()
        services = list(SCAN_SERVICES) if self.o.services else []
        dids = sorted(set(expand(parse_ranges(self.o.dids))) | described_dids)
        rids = sorted(set(expand(parse_ranges(self.o.rids))) | described_rids)
        return services, dids, rids

    def run(self) -> DiscoveryResult:
        result = self.result
        result.started = time.time()
        started = time.monotonic()
        services, dids, rids = self.plan()
        levels = list(SECURITY_LEVELS) if self.o.security else []
        sessions = list(dict.fromkeys(self.o.sessions))
        self._total = len(sessions) * (len(services) + len(dids) + len(rids) + len(levels))
        try:
            for session in sessions:
                why = self._enter(session)
                result.sessions[session] = why
                if why:
                    self._done += len(services) + len(dids) + len(rids) + len(levels)
                    continue
                name = self._session_name(session)
                for sid in services:
                    self._service(sid, session, name)
                if services:
                    self._enter(session)                         # whatever a probe might have changed
                for did in dids:
                    self._did(did, session, name)
                for rid in rids:
                    self._routine(rid, session, name)
                for level in levels:
                    self._level(level, session, name)
        except DiscoveryStopped as stopped:
            result.notes.append(f"{str(stopped) or 'Stopped before the end'}: what was not asked is not in the result")
        finally:
            result.duration = round(time.monotonic() - started, 2)
            self.tester.ask(b"\x10\x01", timeout=self.o.timeout)
        if self.o.security and 0x27 in result.services and not result.found_levels():
            result.notes.append("SecurityAccess is there, but no requestSeed 01-41 answered a seed in the sessions "
                                "asked")
        routine_answers = [state for states in result.routines.values() for state in states.values()]
        if rids and routine_answers and all(state == "sub-function not supported" or state in (NOT_IN_SESSION, SILENT)
                                            for state in routine_answers):
            result.notes.append("The ECU answers 0x12 to requestRoutineResults (31 03): routines cannot be found "
                                "without starting them, which discovery does not do")
        return result

    # --- probes ----------------------------------------------------------------------------------------------

    def _session_name(self, session) -> str:
        return self.d.session_name(session) if self.d else f"session {session:02X}"

    def _path(self, session) -> list[int]:
        if self.d is not None and session in self.d.sessions:
            return self.d.session_path(session)
        if session == DEFAULT_SESSION:
            return [DEFAULT_SESSION]
        return [DEFAULT_SESSION, 0x03, session] if session == 0x02 else [DEFAULT_SESSION, session]

    def _enter(self, session) -> str:
        """"" once the ECU is in the session, else why not."""
        for step in self._path(session):
            answer = self.tester.ask(bytes([0x10, step]), timeout=max(self.o.timeout, 1.0))
            if not answer.positive():
                return f"10 {step:02X}: {answer.text()}"
        return ""

    def _ask(self, payload, text):
        if self.stop is not None and self.stop.is_set():
            raise DiscoveryStopped()
        answer = self.tester.ask(payload, timeout=self.o.timeout)
        self.result.probes += 1
        self._done += 1
        if self._done % 16 == 0 or self._done == self._total:
            self.progress(self._done, self._total, text)
        if answer.raw is None:
            self._silent += 1
            if self._silent >= SILENT_LIMIT:
                self._silent = 0
                if not self.tester.ask(b"\x3e\x00", timeout=1.0).positive():
                    raise DiscoveryStopped(f"The ECU stopped answering (after {text})")
        else:
            self._silent = 0
        return answer

    def _service(self, sid, session, name):
        answer = self._ask(bytes([sid]), f"{name}: service {sid:02X}")
        nrc = answer.nrc
        if answer.raw is None:
            state = SILENT
        elif nrc == 0x11:
            state = NOT_SUPPORTED
        elif nrc == 0x7F:
            state = NOT_IN_SESSION
        elif nrc == 0x33:
            state = SECURED
        else:
            state = ANSWERS
        self.result.services.setdefault(sid, {})[session] = state

    def _did(self, did, session, name):
        answer = self._ask(b"\x22" + did.to_bytes(2, "big"), f"{name}: DID {did:04X}")
        states = {0x31: ABSENT, 0x33: LOCKED, 0x22: CONDITIONS, 0x7F: NOT_IN_SESSION, 0x11: NOT_SUPPORTED}
        if answer.positive() and answer.raw[1:3] == did.to_bytes(2, "big"):
            found = [READ, len(answer.raw) - 3]
        elif answer.raw is None:
            found = [SILENT, None]
        else:
            found = [states.get(answer.nrc, f"NRC 0x{answer.nrc:02X}" if answer.nrc is not None else "answered"), None]
        if found[0] != ABSENT:                     # most of the range: kept short
            self.result.dids.setdefault(did, {})[session] = found
        elif did in (self.d.dids if self.d else {}):
            self.result.dids.setdefault(did, {})[session] = found

    def _routine(self, rid, session, name):
        answer = self._ask(b"\x31\x03" + rid.to_bytes(2, "big"), f"{name}: routine {rid:04X}")
        nrc = answer.nrc
        if answer.raw is None:
            state = SILENT
        elif nrc == 0x31:
            state = ABSENT
        elif nrc == 0x7F:
            state = NOT_IN_SESSION
        elif nrc == 0x11:
            state = NOT_SUPPORTED
        elif nrc == 0x12:
            state = "sub-function not supported"
        else:
            state = FOUND                          # positive, 0x24 (not started), 0x22, 0x33...
        if state != ABSENT or rid in (self.d.routines if self.d else {}):
            self.result.routines.setdefault(rid, {})[session] = state

    def _level(self, level, session, name):
        answer = self._ask(bytes([0x27, level]), f"{name}: security level {level:02X}")
        nrc = answer.nrc
        if answer.positive():
            state = FOUND
        elif nrc in (0x37, 0x36, 0x22, 0x24):          # a delay, conditions: the level is there
            state = FOUND
        elif answer.raw is None:
            state = SILENT
        elif nrc == 0x7F:
            state = NOT_IN_SESSION
        else:
            state = "absent" if nrc in (0x12, 0x31) else f"NRC 0x{nrc:02X}" if nrc is not None else "answered"
        if state != "absent":
            self.result.levels.setdefault(level, {})[session] = state


# --- the comparison ------------------------------------------------------------------------------------------

def compare(result: DiscoveryResult, description: EcuDescription) -> list[Finding]:
    """What the description and the ECU disagree on, in the sessions that were asked."""
    findings = []
    asked = result.entered()

    def sessions_text(sessions) -> str:
        return ", ".join(description.session_name(s) for s in sorted(sessions)) or "no session asked"

    for sid in sorted(set(description.services) | set(result.services)):
        name = f"{sid:02X} {SERVICE_NAMES.get(sid, '')}".strip()
        described = description.services.get(sid)
        if sid not in result.services:
            continue                                      # not asked
        if described is not None and all(state == NOT_SUPPORTED for state in result.services[sid].values()):
            findings.append(Finding("missing", f"Service {name}", "described, but the ECU answers 0x11 "
                                                                  "(serviceNotSupported) in every session asked"))
        elif described is None and result.service_found(sid):
            findings.append(Finding("undocumented", f"Service {name}",
                                    f"the ECU has it (answers in: {sessions_text(result.service_sessions(sid))}); "
                                    f"the description does not"))
        elif described is not None:
            answers = result.service_sessions(sid)
            allowed = {s for s in asked if described.access.allows(s)}
            if answers - allowed:
                findings.append(Finding("different", f"Service {name}", f"answers in {sessions_text(answers - allowed)}, "
                                                                        "where the description does not allow it"))
            refused = {s for s in allowed if result.services[sid].get(s) == NOT_IN_SESSION}
            if refused:
                findings.append(Finding("different", f"Service {name}", f"refused (0x7F) in {sessions_text(refused)}, "
                                                                        "where the description allows it"))
    for did in sorted(set(description.dids) | set(result.dids)):
        described = description.dids.get(did)
        name = f"DID {did:04X}" + (f" {described.name}" if described and not described.name.startswith("DID") else "")
        if described is None:
            if result.did_found(did):
                length = result.did_length(did)
                readable = result.did_sessions(did)
                where = f"read in {sessions_text(readable)}" if readable else "locked"
                findings.append(Finding("undocumented", name, f"the ECU has it ({where}"
                                                              + (f", {length} bytes" if length is not None else "")
                                                              + "); the description does not"))
            continue
        if described.read is None or did not in result.dids:
            continue
        if not result.did_found(did):
            findings.append(Finding("missing", name, "described, but the ECU answers 0x31 (requestOutOfRange) in "
                                                     "every session asked"))
            continue
        length = result.did_length(did)
        if described.length and length is not None and length != described.length:
            findings.append(Finding("different", name, f"the ECU answers {length} bytes, the description says "
                                                       f"{described.length}"))
        readable = result.did_sessions(did)
        allowed = {s for s in asked if described.read.allows(s) and not described.read.levels}
        if allowed - readable:
            findings.append(Finding("different", name, f"not read in {sessions_text(allowed - readable)}, where the "
                                                       "description allows it"))
        if readable - allowed and not described.read.levels:
            findings.append(Finding("different", name, f"read in {sessions_text(readable - allowed)}, where the "
                                                       "description does not allow it"))
    for rid in sorted(set(description.routines) | set(result.routines)):
        described = description.routines.get(rid)
        name = f"Routine {rid:04X}" + (f" {described.name}" if described else "")
        if described is None and result.routine_found(rid):
            findings.append(Finding("undocumented", name, "the ECU has it; the description does not"))
        elif described is not None and rid in result.routines and not result.routine_found(rid) and \
                all(state == ABSENT for state in result.routines[rid].values()):
            findings.append(Finding("missing", name, "described, but the ECU answers 0x31 (requestOutOfRange) in "
                                                     "every session asked"))
    found_levels = set(result.found_levels())
    if result.levels or found_levels:
        for level in sorted(found_levels - set(description.security_levels)):
            findings.append(Finding("undocumented", f"Security level {level:02X}", "the ECU gives a seed; the "
                                                                                   "description has no such level"))
        asked_27 = 0x27 in result.services and any(state == ANSWERS for state in result.services[0x27].values())
        for level in sorted(set(description.security_levels) - found_levels):
            if asked_27:
                findings.append(Finding("missing", f"Security level {level:02X}", "described, but no seed in the "
                                                                                  "sessions asked"))
    return findings


# --- HTML ----------------------------------------------------------------------------------------------------

TABLE = '<table border="1" cellspacing="0" cellpadding="3">'   # Qt's rich text draws no CSS borders
KIND_COLOURS = {"missing": "#fee2e2", "undocumented": "#fef3c7", "different": "#e0f2fe"}
MARKS = {ANSWERS: "&#10003;", SECURED: "33", NOT_IN_SESSION: "7F", NOT_SUPPORTED: "11", SILENT: "&ndash;",
         READ: "&#10003;",
         LOCKED: "33", CONDITIONS: "22", ABSENT: "31", FOUND: "&#10003;", "": ""}
KIND_TEXT = {"missing": "described, not found", "undocumented": "found, not described", "different": "different"}


def discovery_html(result: DiscoveryResult, description: EcuDescription | None = None) -> str:
    """The discovery as an HTML section (the window's Discovery tab, and its report)."""
    parts = ["<h2>Discovery</h2>"]
    entered = result.entered()

    def session_name(session) -> str:
        return description.session_name(session) if description else f"session {session:02X}"

    def matrix(title, rows, legend) -> str:
        """A table of what each identifier got in each session entered."""
        head = "".join(f"<th>{html.escape(session_name(s))}</th>" for s in entered)
        body = "".join(f"<tr><td>{html.escape(label)}</td>" + "".join(f"<td>{MARKS.get(state, html.escape(state))}</td>"
                                                                   for state in states) + "</tr>"
                       for label, states in rows)
        return f'<h3>{title}</h3>{TABLE}<tr><th></th>{head}</tr>{body}</table><p class="muted">{legend}</p>'

    when = datetime.fromtimestamp(result.started).strftime("%Y-%m-%d %H:%M:%S") if result.started else "-"
    asked = ", ".join(session_name(s) + (f" (not entered: {html.escape(why)})" if why else "")
                      for s, why in result.sessions.items())
    parts.append(f"<p>{when}: {result.probes} requests in {result.duration:.1f} s, in {asked}.</p>")
    for note in result.notes:
        parts.append(f"<p><b>Note:</b> {html.escape(note)}</p>")
    services = [sid for sid in sorted(result.services) if result.service_found(sid)]
    dids = [did for did in sorted(result.dids) if result.did_found(did)]
    routines = [rid for rid in sorted(result.routines) if result.routine_found(rid)]
    parts.append(f"<p>Found: <b>{len(services)}</b> services, <b>{len(dids)}</b> DIDs, <b>{len(routines)}</b> "
                 f"routines, <b>{len(result.found_levels())}</b> security levels.</p>")
    if description is not None:
        findings = compare(result, description)
        title = f"<h3>Against {html.escape(description.name)}</h3>"
        if findings:
            rows = "".join(f'<tr style="background:{KIND_COLOURS[f.kind]}"><td>{KIND_TEXT[f.kind]}</td>'
                           f"<td>{html.escape(f.what)}</td><td>{html.escape(f.detail)}</td></tr>" for f in findings)
            parts.append(f"{title}{TABLE}<tr><th>Finding</th><th>What</th><th>Detail</th></tr>{rows}</table>")
        else:
            parts.append(f"{title}<p>The ECU and the description agree on everything asked.</p>")
    if services:
        rows = [(f"{sid:02X} {SERVICE_NAMES.get(sid, '')}", [result.services[sid].get(s, "") for s in entered])
                for sid in services]
        parts.append(matrix("Services", rows, "&#10003; answers in the session; 33: there, behind SecurityAccess; "
                                              "7F: there, but not in this session."))
    if dids:
        rows = []
        for did in dids:
            name = description.dids[did].name if description and did in description.dids else ""
            length = result.did_length(did)
            label = f"{did:04X} {name}".strip() + (f" ({length} bytes)" if length is not None else "")
            rows.append((label, [(result.dids[did].get(s) or ["", None])[0] for s in entered]))
        parts.append(matrix("Data identifiers", rows, "&#10003; read; 33: locked; 22: conditions not correct; 31: "
                                                      "not there in this session."))
    if routines:
        rows = [(f"{rid:04X}", [result.routines[rid].get(s, "") for s in entered]) for rid in routines]
        parts.append(matrix("Routines", rows, "&#10003; there: its results were asked (31 03), it was not started."))
    if result.found_levels():
        rows = [(f"{level:02X}/{level + 1:02X}", [result.levels[level].get(s, "") for s in entered])
                for level in result.found_levels()]
        parts.append(matrix("Security levels", rows, "&#10003; requestSeed answered."))
    return "\n".join(parts)


def discovery_page(result: DiscoveryResult, description: EcuDescription | None = None) -> str:
    """A self-contained HTML page of a discovery."""
    title = f"Discovery{': ' + description.name if description else ''}"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
 body {{ font-family: Segoe UI, Helvetica, Arial, sans-serif; margin: 24px; color: #1f2937; }}
 table {{ border-collapse: collapse; margin: 6px 0 12px; }}
 th, td {{ border: 1px solid #d1d5db; padding: 3px 8px; text-align: left; }}
 th {{ background: #f3f4f6; }}
 .muted {{ color: #6b7280; }}
</style></head><body>
{discovery_html(result, description)}
</body></html>
"""
