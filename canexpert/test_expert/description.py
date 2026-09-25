"""
What an ECU's diagnostics are, as TestExpert tests them: its sessions, security levels, services with their
sub-functions, data identifiers (with the fields of their data: where each is, how it is coded, which values
are valid) and routines, and in which sessions and at which security level each may be used.

The loaders (cdd.py, odx.py, dummy.py) all describe services the same way - the constant bytes a request
starts with, the states it may be executed in, the state transitions it causes - and build_description()
turns that into an EcuDescription. It is saved and loaded as JSON, so a description can be corrected by hand.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from canexpert.uds.observer import SERVICE_NAMES

# ISO 14229-1 services, and those whose second byte is a sub-function.
ISO_SERVICES = (0x10, 0x11, 0x14, 0x19, 0x22, 0x23, 0x24, 0x27, 0x28, 0x29, 0x2A, 0x2C, 0x2E, 0x2F, 0x31, 0x34,
                0x35, 0x36, 0x37, 0x38, 0x3D, 0x3E, 0x83, 0x84, 0x85, 0x86, 0x87)
SUB_FUNCTION_SERVICES = {0x10, 0x11, 0x19, 0x27, 0x28, 0x29, 0x2C, 0x31, 0x3E, 0x83, 0x85, 0x86, 0x87}
DEFAULT_SESSION = 0x01


@dataclass
class Access:
    """Where something may be done: in these sessions (none listed: in every one), with one of these
    security levels unlocked (none listed: locked will do)."""
    sessions: set = field(default_factory=set)
    levels: set = field(default_factory=set)

    def allows(self, session: int) -> bool:
        return not self.sessions or session in self.sessions

    def merged(self, other: "Access") -> "Access":
        """Where either may be done (a service allowed wherever one of its instances is)."""
        sessions = set() if not self.sessions or not other.sessions else self.sessions | other.sessions
        levels = set() if not self.levels or not other.levels else self.levels | other.levels
        return Access(sessions, levels)

    def to_dict(self):
        return {"sessions": sorted(self.sessions), "levels": sorted(self.levels)}

    @classmethod
    def from_dict(cls, values):
        values = values or {}
        return cls(set(values.get("sessions", ())), set(values.get("levels", ())))


@dataclass
class Session:
    id: int                      # its DiagnosticSessionControl sub-function
    name: str
    entered_from: set = field(default_factory=set)   # the sessions it may be entered from (none: any)


@dataclass
class Service:
    sid: int
    name: str
    access: Access = field(default_factory=Access)
    sub_functions: dict = field(default_factory=dict)   # sub-function -> Access, for a service that has them


ENCODINGS = ("unsigned", "signed", "bcd", "ascii", "bytes")


@dataclass
class DataField:
    """A value in a DID's data record: its bits (position of the first, most significant, from the record's
    start; bits 0: text or bytes up to the record's end, of any length), how they are coded, the coded values
    that are valid ([] any; a text table's own values when it has one), and how the report shows it (text, or
    coded * scale + shift and the unit)."""
    name: str
    position: int
    bits: int
    encoding: str = "unsigned"
    valid: list = field(default_factory=list)          # [(low, high)] of coded values
    texts: dict = field(default_factory=dict)          # coded value -> text
    scale: float = 1.0
    shift: float = 0.0
    unit: str = ""

    @property
    def numeric(self) -> bool:
        return self.encoding in ("unsigned", "signed", "bcd") and 0 < self.bits <= 64

    @property
    def variable(self) -> bool:
        """Text or bytes up to the end of the record."""
        return self.bits == 0 and self.encoding in ("ascii", "bytes")

    def limits(self) -> list:
        """The valid coded ranges: its own, else its text table's values."""
        if self.valid:
            return [tuple(pair) for pair in self.valid]
        return [(value, value) for value in sorted(self.texts)]

    def coded(self, record: bytes):
        """Its coded value in record: an int for a number (a BCD's digits as they read), bytes for text and
        bytes; None when the record is too short, or a BCD has a nibble above 9."""
        record = bytes(record)
        if self.variable:
            return record[self.position // 8:] if self.position % 8 == 0 and self.position < len(record) * 8 else None
        end = self.position + self.bits
        if self.bits <= 0 or end > len(record) * 8:
            return None
        if not self.numeric:
            if self.position % 8 or self.bits % 8:
                return None
            return record[self.position // 8:end // 8]
        value = (int.from_bytes(record, "big") >> (len(record) * 8 - end)) & ((1 << self.bits) - 1)
        if self.encoding == "signed" and value >> (self.bits - 1):
            value -= 1 << self.bits
        if self.encoding == "bcd":
            digits = f"{value:0{(self.bits + 3) // 4}X}"
            return int(digits) if digits.isdigit() else None
        return value

    def encode(self, record: bytes, value: int) -> bytes:
        """record with this field set to the coded value."""
        record = bytes(record)
        if self.encoding == "bcd":
            value = int(str(value), 16)
        value &= (1 << self.bits) - 1
        shift = len(record) * 8 - self.position - self.bits
        whole = int.from_bytes(record, "big")
        whole = (whole & ~(((1 << self.bits) - 1) << shift)) | (value << shift)
        return whole.to_bytes(len(record), "big")

    def shown(self, value) -> str:
        """The value as a person reads it: the text table's text, else its physical value and unit."""
        if isinstance(value, bytes):
            trimmed = value.rstrip(b"\x00\xff ")
            if self.encoding == "ascii" and all(0x20 <= byte < 0x7F for byte in trimmed):
                return f'"{trimmed.decode("ascii")}"'
            return value.hex(" ").upper()
        if value in self.texts:
            return f"{self.texts[value]} ({value})"
        physical = value * self.scale + self.shift
        text = f"{physical:g}" if (self.scale, self.shift) != (1.0, 0.0) else str(value)
        return f"{text} {self.unit}".strip()

    def check(self, record: bytes) -> tuple[bool, str]:
        """Whether its value in record is valid, and what the report says of it."""
        value = self.coded(record)
        if value is None:
            return False, "not in the record" if self.position + max(self.bits, 1) > len(record) * 8 else \
                "not a BCD number"
        if isinstance(value, bytes):
            trimmed = value.rstrip(b"\x00\xff ")
            if self.encoding == "ascii" and not all(0x20 <= byte < 0x7F for byte in trimmed):
                return False, f"{value.hex(' ').upper()}: not printable ASCII"
            return True, self.shown(value)
        limits = self.limits()
        ok = not limits or any(low <= value <= high for low, high in limits)
        allowed = ", ".join(self.shown(low) if low == high else f"{self.shown(low)} to {self.shown(high)}"
                            for low, high in limits)
        return ok, self.shown(value) + ("" if ok else f": not a valid value ({allowed})")

    def to_dict(self) -> dict:
        values = {"name": self.name, "position": self.position, "bits": self.bits, "encoding": self.encoding}
        if self.valid:
            values["valid"] = [list(pair) for pair in self.valid]
        if self.texts:
            values["texts"] = {str(key): text for key, text in sorted(self.texts.items())}
        if (self.scale, self.shift) != (1.0, 0.0):
            values.update(scale=self.scale, shift=self.shift)
        if self.unit:
            values["unit"] = self.unit
        return values

    @classmethod
    def from_dict(cls, values: dict) -> "DataField":
        return cls(str(values.get("name", "")), int(values.get("position", 0)), int(values.get("bits", 8)),
                   str(values.get("encoding", "unsigned")),
                   [tuple(int(v) for v in pair) for pair in values.get("valid", ())],
                   {int(key): str(text) for key, text in (values.get("texts") or {}).items()},
                   float(values.get("scale", 1.0)), float(values.get("shift", 0.0)), str(values.get("unit", "")))


@dataclass
class DataIdentifier:
    did: int
    name: str
    length: int | None = None    # bytes of its data record, when the description says
    read: Access | None = None   # None: not readable
    write: Access | None = None  # None: not writable
    fields: list = field(default_factory=list)          # DataField, where the description says


@dataclass
class Routine:
    rid: int
    name: str
    sub_functions: dict = field(default_factory=dict)   # 1 start, 2 stop, 3 results -> Access


@dataclass
class EcuDescription:
    name: str = "ECU"
    source: str = ""
    sessions: dict = field(default_factory=dict)          # id -> Session
    security_levels: dict = field(default_factory=dict)   # requestSeed sub-function -> name
    services: dict = field(default_factory=dict)          # SID -> Service
    dids: dict = field(default_factory=dict)              # identifier -> DataIdentifier
    routines: dict = field(default_factory=dict)          # identifier -> Routine
    warnings: list = field(default_factory=list)          # what a loader could not read
    unknown: set = field(default_factory=set)             # what it does not say: "writing" (which DIDs may be
    # written), "sub-functions", "starting routines" - the tests that would rely on it are left out
    variants: list = field(default_factory=list)          # the variants its file has (CDD VARs, ODX variants)
    variant: str = ""                                     # the one read

    def service(self, sid: int) -> Service | None:
        return self.services.get(sid)

    def session_name(self, session: int) -> str:
        found = self.sessions.get(session)
        return found.name if found else f"session 0x{session:02X}"

    def session_path(self, session: int) -> list[int]:
        """The DiagnosticSessionControl requests that lead from the default session into session: through a
        session it must be entered from, when it cannot be entered from the default one."""
        if session == DEFAULT_SESSION:
            return [DEFAULT_SESSION]
        sources = self.sessions[session].entered_from if session in self.sessions else set()
        if not sources or DEFAULT_SESSION in sources:
            return [DEFAULT_SESSION, session]
        via = next((s for s in sorted(sources) if s != session and s in self.sessions and
                    (not self.sessions[s].entered_from or DEFAULT_SESSION in self.sessions[s].entered_from)), None)
        return [DEFAULT_SESSION, via, session] if via is not None else [DEFAULT_SESSION, session]

    def summary(self) -> str:
        return (f"{len(self.sessions)} sessions, {len(self.security_levels)} security levels, "
                f"{len(self.services)} services, {len(self.dids)} DIDs, {len(self.routines)} routines")

    # --- JSON ------------------------------------------------------------------------------------

    def to_dict(self) -> dict:
        def access(value):
            return value.to_dict() if value is not None else None
        return {
            "name": self.name, "source": self.source,
            "sessions": [{"id": s.id, "name": s.name, "entered_from": sorted(s.entered_from)}
                         for s in sorted(self.sessions.values(), key=lambda s: s.id)],
            "security_levels": [{"level": level, "name": name} for level, name in sorted(self.security_levels.items())],
            "services": [{"sid": s.sid, "name": s.name, "access": s.access.to_dict(),
                          "sub_functions": [{"id": sub, **a.to_dict()} for sub, a in sorted(s.sub_functions.items())]}
                         for s in sorted(self.services.values(), key=lambda s: s.sid)],
            "dids": [{"did": d.did, "name": d.name, "length": d.length, "read": access(d.read), "write": access(d.write),
                      **({"fields": [item.to_dict() for item in d.fields]} if d.fields else {})}
                     for d in sorted(self.dids.values(), key=lambda d: d.did)],
            "routines": [{"rid": r.rid, "name": r.name,
                          "sub_functions": [{"id": sub, **a.to_dict()} for sub, a in sorted(r.sub_functions.items())]}
                         for r in sorted(self.routines.values(), key=lambda r: r.rid)],
            "warnings": list(self.warnings),
            "unknown": sorted(self.unknown),
            "variants": list(self.variants), "variant": self.variant,
        }

    @classmethod
    def from_dict(cls, values: dict) -> "EcuDescription":
        def access(value):
            return Access.from_dict(value) if value is not None else None
        description = cls(values.get("name", "ECU"), values.get("source", ""))
        for item in values.get("sessions", ()):
            description.sessions[int(item["id"])] = Session(int(item["id"]), item.get("name", ""),
                                                            set(item.get("entered_from", ())))
        for item in values.get("security_levels", ()):
            description.security_levels[int(item["level"])] = item.get("name", "")
        for item in values.get("services", ()):
            sid = int(item["sid"])
            description.services[sid] = Service(sid, item.get("name") or SERVICE_NAMES.get(sid, f"0x{sid:02X}"),
                                                Access.from_dict(item.get("access")),
                                                {int(sub["id"]): Access.from_dict(sub) for sub in item.get("sub_functions", ())})
        for item in values.get("dids", ()):
            did = int(item["did"])
            description.dids[did] = DataIdentifier(did, item.get("name", ""), item.get("length"),
                                                   access(item.get("read")), access(item.get("write")),
                                                   [DataField.from_dict(part) for part in item.get("fields", ())])
        for item in values.get("routines", ()):
            rid = int(item["rid"])
            description.routines[rid] = Routine(rid, item.get("name", ""),
                                                {int(sub["id"]): Access.from_dict(sub) for sub in item.get("sub_functions", ())})
        description.warnings = list(values.get("warnings", ()))
        description.unknown = set(values.get("unknown", ()))
        description.variants = [str(name) for name in values.get("variants", ())]
        description.variant = str(values.get("variant", ""))
        return description

    def save(self, path):
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "EcuDescription":
        description = cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        description.source = description.source or str(path)
        return description


# --- from services described as request bytes and states ---------------------------------------------------

@dataclass
class RawService:
    """One service of a description file: the constant bytes its request starts with (SID first), the states
    it may be executed in (keys of RawStates; None: in every state), and the (from, to) transitions it causes."""
    prefix: bytes
    name: str = ""
    allowed: list | None = None
    transitions: list = field(default_factory=list)
    length: int | None = None     # a DID's data length, where the loader knows it
    fields: list = field(default_factory=list)   # a DID's DataFields, where the loader knows them


@dataclass
class RawState:
    group: str                    # "session", "security", or another state group
    name: str


def build_description(raw_services, states: dict, name: str = "ECU", source: str = "") -> EcuDescription:
    """An EcuDescription from services as request prefixes: sessions from DiagnosticSessionControl and the
    session states it leads to, security levels from SecurityAccess, and every other service, DID and routine
    with the sessions and levels its allowed states name."""
    description = EcuDescription(name, source)
    raw_services = [raw for raw in raw_services if raw.prefix]

    # Which session each session state is, which security level unlocks each security state.
    session_of_state, level_of_state = {}, {}
    entered_from = {}
    for raw in raw_services:
        sid, sub = raw.prefix[0], (raw.prefix[1] & 0x7F if len(raw.prefix) > 1 else None)
        if sid == 0x10 and sub is not None:
            targets = {to for _from, to in raw.transitions if states.get(to, RawState("", "")).group == "session"}
            for state in targets:
                session_of_state[state] = sub
            state_name = states[next(iter(targets))].name if targets else ""
            description.sessions[sub] = Session(sub, state_name or raw.name or f"Session 0x{sub:02X}")
            entered_from[sub] = {source for source, to in raw.transitions if to in targets and source != to}
        elif sid == 0x27 and sub is not None:
            level = sub if sub % 2 else sub - 1
            if sub % 2:
                description.security_levels.setdefault(level, raw.name or f"Level 0x{level:02X}")
            for _source, to in raw.transitions:
                if states.get(to, RawState("", "")).group == "security":
                    level_of_state[to] = level
    for sub, sources in entered_from.items():
        # Entered from the sessions whose states lead to it; every one of them: no restriction.
        sessions = {session_of_state[source] for source in sources if source in session_of_state}
        if sessions and set(description.sessions) - {sub} <= sessions:
            sessions = set()
        description.sessions[sub].entered_from = sessions
    if DEFAULT_SESSION not in description.sessions and raw_services:
        description.sessions[DEFAULT_SESSION] = Session(DEFAULT_SESSION, "Default session")
        description.warnings.append("No DiagnosticSessionControl 0x01 described: the default session is assumed")

    def access_of(raw: RawService) -> Access:
        if raw.allowed is None:
            return Access()
        allowed = set(raw.allowed)
        session_states = {key for key, state in states.items() if state.group == "session"}
        security_states = {key for key, state in states.items() if state.group == "security"}
        sessions = {session_of_state[key] for key in allowed & session_states if key in session_of_state}
        if not allowed & session_states or not session_states - allowed:
            sessions = set()                             # every session state allowed, or none named
        levels = set()
        if allowed & security_states and security_states - allowed:
            # Only unlocked states allowed: one of the levels that lead there is needed.
            locked = security_states - set(level_of_state)
            if not allowed & locked:
                levels = {level_of_state[key] for key in allowed & security_states if key in level_of_state}
        return Access(sessions, levels)

    for raw in raw_services:
        sid = raw.prefix[0]
        access = access_of(raw)
        service = description.services.get(sid)
        if service is None:
            service = description.services[sid] = Service(sid, SERVICE_NAMES.get(sid, f"Service 0x{sid:02X}"), access)
        else:
            service.access = service.access.merged(access)
        if sid in SUB_FUNCTION_SERVICES and len(raw.prefix) > 1:
            sub = raw.prefix[1] & 0x7F
            if sid == 0x27:
                sub = sub if sub % 2 else sub - 1                # both steps of a level share its access
            previous = service.sub_functions.get(sub)
            service.sub_functions[sub] = previous.merged(access) if previous else access
        if sid in (0x22, 0x2E) and len(raw.prefix) >= 3:
            did = int.from_bytes(raw.prefix[1:3], "big")
            entry = description.dids.setdefault(did, DataIdentifier(did, raw.name or f"DID 0x{did:04X}"))
            if raw.length is not None and entry.length is None:
                entry.length = raw.length
            if raw.fields and not entry.fields:
                entry.fields = list(raw.fields)
            if sid == 0x22:
                entry.read = entry.read.merged(access) if entry.read else access
            else:
                entry.write = entry.write.merged(access) if entry.write else access
        if sid == 0x31 and len(raw.prefix) >= 4:
            rid = int.from_bytes(raw.prefix[2:4], "big")
            routine = description.routines.setdefault(rid, Routine(rid, raw.name or f"Routine 0x{rid:04X}"))
            sub = raw.prefix[1] & 0x7F
            previous = routine.sub_functions.get(sub)
            routine.sub_functions[sub] = previous.merged(access) if previous else access
    return description
