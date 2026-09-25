"""
What a TestExpert run covered: for each service, DID and routine, in which sessions it was sent and how the
steps that checked it went - the matrix of the report and of the window's Coverage tab - and what the run did
not test, with the reason.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from canexpert.test_expert.description import ISO_SERVICES, EcuDescription
from canexpert.uds.observer import SERVICE_NAMES

PASSED, FAILED, ACCEPTED = "pass", "fail", "accepted"
COLOURS = {"passed": "#dcfce7", "failed": "#fee2e2", "accepted": "#e0f2fe", "": "#f3f4f6", "refused": "#f9fafb"}
MARKS = {"passed": "&#10003;", "failed": "&#10007;", "accepted": "&#10003;", "": "&ndash;"}
UNKNOWN_SESSION = -1          # a request before the ECU's session was known
SESSION_ORDER = (0x01, 0x03, 0x02)
# Why TestExpert sends a service no further than asking whether it is there.
NOT_EXERCISED = {
    0x11: "ECU reset is a destructive test: tick Destructive tests",
    0x14: "clearing every DTC is a destructive test: tick Destructive tests",
    0x23: "reading memory needs an address range the description does not give",
    0x24: "not tested beyond its availability",
    0x29: "Authentication is not tested beyond its availability",
    0x2A: "periodic data is not tested beyond its availability",
    0x2C: "dynamically defined DIDs are not tested beyond their availability",
    0x2E: "writing a DID is a destructive test: tick Destructive tests",
    0x2F: "input/output control is not tested beyond its availability",
    0x31: "routines are not started",
    0x34: "a download is not tested beyond its availability",
    0x35: "an upload is not tested beyond its availability",
    0x36: "TransferData needs a download or an upload in progress",
    0x37: "RequestTransferExit needs a transfer in progress",
    0x38: "not tested beyond its availability",
    0x3D: "writing memory is not tested",
    0x83: "not tested beyond its availability",
    0x84: "not tested beyond its availability",
    0x86: "ResponseOnEvent is not tested beyond its availability",
    0x87: "not tested beyond its availability",
}


@dataclass
class Cell:
    passed: int = 0
    failed: int = 0
    accepted: int = 0
    tests: set = field(default_factory=set)

    def add(self, verdict: str, test: str):
        if verdict == PASSED:
            self.passed += 1
        elif verdict == FAILED:
            self.failed += 1
        elif verdict == ACCEPTED:
            self.accepted += 1
        else:
            return
        if test:
            self.tests.add(test)

    @property
    def count(self) -> int:
        return self.passed + self.failed + self.accepted

    def verdict(self) -> str:
        """"failed", "accepted", "passed", or "" when nothing was checked there."""
        return "failed" if self.failed else "accepted" if self.accepted else "passed" if self.passed else ""

    def to_list(self) -> list:
        return [self.passed, self.failed, self.accepted, sorted(self.tests)]

    @classmethod
    def from_list(cls, values) -> "Cell":
        return cls(int(values[0]), int(values[1]), int(values[2]), set(values[3]) if len(values) > 3 else set())


class Coverage:
    """Checks counted where they happened: services by (SID, session), DIDs by (DID, session, "read"/"write"),
    routines by (RID, session), functional requests by SID."""

    def __init__(self):
        self.services: dict = {}
        self.dids: dict = {}
        self.routines: dict = {}
        self.functional: dict = {}

    @staticmethod
    def _cell(table, key) -> Cell:
        cell = table.get(key)
        if cell is None:
            cell = table[key] = Cell()
        return cell

    def record(self, request: bytes, session, verdict: str, test: str = "", functional: bool = False):
        """A step's verdict ("pass", "fail", "accepted") on request, sent in session."""
        request = bytes(request)
        if not request:
            return
        session = UNKNOWN_SESSION if session is None else session
        sid = request[0]
        self._cell(self.services, (sid, session)).add(verdict, test)
        if functional:
            self._cell(self.functional, sid).add(verdict, test)
        if sid == 0x22:
            for index in range(1, len(request) - 1, 2):
                did = int.from_bytes(request[index:index + 2], "big")
                self._cell(self.dids, (did, session, "read")).add(verdict, test)
        elif sid == 0x2E and len(request) >= 3:
            self._cell(self.dids, (int.from_bytes(request[1:3], "big"), session, "write")).add(verdict, test)
        elif sid == 0x31 and len(request) >= 4:
            self._cell(self.routines, (int.from_bytes(request[2:4], "big"), session)).add(verdict, test)

    def service(self, sid) -> Cell:
        """Every check of a service, whatever the session."""
        total = Cell()
        for (key, _session), cell in self.services.items():
            if key == sid:
                total.passed, total.failed, total.accepted = (total.passed + cell.passed, total.failed + cell.failed,
                                                              total.accepted + cell.accepted)
                total.tests |= cell.tests
        return total

    # --- JSON (the results of a run) -------------------------------------------------------------------------

    def to_dict(self) -> dict:
        def table(values, key):
            return [[*key(entry), *cell.to_list()] for entry, cell in sorted(values.items(), key=lambda item: str(item[0]))]
        return {"services": table(self.services, lambda entry: entry),
                "dids": table(self.dids, lambda entry: entry),
                "routines": table(self.routines, lambda entry: entry),
                "functional": table(self.functional, lambda entry: (entry,))}

    @classmethod
    def from_dict(cls, values: dict) -> "Coverage":
        coverage = cls()
        for row in values.get("services", ()):
            coverage.services[(row[0], row[1])] = Cell.from_list(row[2:])
        for row in values.get("dids", ()):
            coverage.dids[(row[0], row[1], row[2])] = Cell.from_list(row[3:])
        for row in values.get("routines", ()):
            coverage.routines[(row[0], row[1])] = Cell.from_list(row[2:])
        for row in values.get("functional", ()):
            coverage.functional[row[0]] = Cell.from_list(row[1:])
        return coverage


# --- what was not tested ------------------------------------------------------------------------------------

def untested(coverage: Coverage, description: EcuDescription, options=None) -> list[tuple[str, str]]:
    """(what, why) for the services, DIDs and routines of the description that no step checked."""
    rows = []
    for sid, service in sorted(description.services.items()):
        total = coverage.service(sid)
        if total.count:
            continue
        why = NOT_EXERCISED.get(sid, "no test sends it")
        if sid in (0x11, 0x14, 0x2E) and options is not None and getattr(options, "destructive", False):
            why = "no test sent it"
        rows.append((f"Service {sid:02X} {SERVICE_NAMES.get(sid, service.name)}", why))
    for did, entry in sorted(description.dids.items()):
        read = any(key[0] == did and key[2] == "read" and cell.count for key, cell in coverage.dids.items())
        written = any(key[0] == did and key[2] == "write" and cell.count for key, cell in coverage.dids.items())
        if entry.read is not None and not read:
            rows.append((f"DID {did:04X} {entry.name}: read", "no test read it"))
        if entry.write is not None and not written:
            why = "writing is a destructive test: tick Destructive tests" if not getattr(options, "destructive", False) \
                else "it could not be written (see its test)"
            rows.append((f"DID {did:04X} {entry.name}: write", why))
    for rid, routine in sorted(description.routines.items()):
        if not any(key[0] == rid and cell.count for key, cell in coverage.routines.items()):
            rows.append((f"Routine {rid:04X} {routine.name}", "no test sent it"))
        elif 0x01 in routine.sub_functions:
            rows.append((f"Routine {rid:04X} {routine.name}: started", "routines are not started (only refused where "
                                                                       "they may not run)"))
    return rows


# --- HTML -------------------------------------------------------------------------------------------------

def _sessions(description: EcuDescription, coverage: Coverage) -> list[int]:
    known = list(description.sessions)
    seen = {key[1] for key in coverage.services} | {key[1] for key in coverage.dids}
    ordered = [s for s in SESSION_ORDER if s in known] + sorted(s for s in known if s not in SESSION_ORDER)
    extra = sorted(s for s in seen if s not in ordered and s != UNKNOWN_SESSION)
    return ordered + extra


def _cell_html(cell: Cell | None, allowed: bool | None = None) -> str:
    if cell is None or not cell.count:
        colour = COLOURS["refused"] if allowed is False else COLOURS[""]
        title = "not allowed here by the description; not checked" if allowed is False else "not checked here"
        return f'<td class="cov" style="background:{colour}" title="{title}">&ndash;</td>'
    verdict = cell.verdict()
    counts = f"{cell.passed + cell.accepted}/{cell.count}" if cell.failed else str(cell.count)
    tests = html.escape(", ".join(sorted(cell.tests))[:400])
    title = f"{cell.passed} passed, {cell.failed} failed, {cell.accepted} accepted - {tests}"
    return (f'<td class="cov" style="background:{COLOURS[verdict]}" title="{title}">{MARKS[verdict]} '
            f'{counts}</td>')


def coverage_html(coverage: Coverage, description: EcuDescription, options=None) -> str:
    """The report's Coverage section: services x sessions, DIDs x sessions (read, write), routines x sessions,
    functional requests, and what was not tested."""
    sessions = _sessions(description, coverage)
    head = "".join(f"<th>{html.escape(description.session_name(s))}<br><span class=\"muted\">{s:02X}</span></th>"
                   for s in sessions)
    parts = ['<h2>Coverage</h2><p class="muted">Where each service, DID and routine was checked: the steps that '
             'passed (&#10003;) or failed (&#10007;), per session. A dash: not checked there (paler: not allowed '
             'there by the description). Hover a cell for its tests.</p>']
    rows = []
    described = sorted(description.services)
    others = sorted({key[0] for key in coverage.services} - set(described))
    for sid in described + others:
        service = description.services.get(sid)
        name = SERVICE_NAMES.get(sid, service.name if service else f"0x{sid:02X}")
        label = f"{sid:02X} {html.escape(name)}" + ("" if service else ' <span class="muted">(not described)</span>')
        cells = "".join(_cell_html(coverage.services.get((sid, s)), service.access.allows(s) if service else None)
                        for s in sessions)
        rows.append(f"<tr><td>{label}</td>{cells}</tr>")
    parts.append(f"<h3>Services</h3><table><tr><th>Service</th>{head}</tr>{''.join(rows)}</table>")

    if description.dids:
        rows = []
        for did, entry in sorted(description.dids.items()):
            for kind, access in (("read", entry.read), ("write", entry.write)):
                if access is None and not any(key[0] == did and key[2] == kind for key in coverage.dids):
                    continue
                cells = "".join(_cell_html(coverage.dids.get((did, s, kind)),
                                           access.allows(s) if access is not None else False) for s in sessions)
                label = kind if access is not None else f"{kind} (refused)"
                rows.append(f"<tr><td>{did:04X} {html.escape(entry.name)}</td><td>{label}</td>{cells}</tr>")
        parts.append(f"<h3>Data identifiers</h3><table><tr><th>DID</th><th></th>{head}</tr>{''.join(rows)}</table>")

    if description.routines:
        rows = []
        for rid, routine in sorted(description.routines.items()):
            start = routine.sub_functions.get(0x01)
            cells = "".join(_cell_html(coverage.routines.get((rid, s)), start.allows(s) if start else None)
                            for s in sessions)
            rows.append(f"<tr><td>{rid:04X} {html.escape(routine.name)}</td>{cells}</tr>")
        parts.append(f"<h3>Routines</h3><table><tr><th>Routine</th>{head}</tr>{''.join(rows)}</table>")

    if coverage.functional:
        cells = "".join(f"<tr><td>{sid:02X} {html.escape(SERVICE_NAMES.get(sid, ''))}</td>{_cell_html(cell)}</tr>"
                        for sid, cell in sorted(coverage.functional.items()))
        parts.append(f"<h3>Functional requests</h3><table><tr><th>Service</th><th>Checked</th></tr>{cells}</table>")

    missing = untested(coverage, description, options)
    if missing:
        rows = "".join(f"<tr><td>{html.escape(what)}</td><td>{html.escape(why)}</td></tr>" for what, why in missing)
        parts.append(f"<h3>Not tested</h3><table><tr><th>What</th><th>Why</th></tr>{rows}</table>")
    untested_iso = [sid for sid in ISO_SERVICES if sid not in description.services and not coverage.service(sid).count]
    if untested_iso:
        parts.append('<p class="muted">ISO 14229-1 services neither described nor checked: '
                     + ", ".join(f"{sid:02X}" for sid in untested_iso) + "</p>")
    return "\n".join(parts) + '\n<style>td.cov { text-align: center; min-width: 64px; }</style>'


def summary(coverage: Coverage, description: EcuDescription) -> str:
    """"14 of 16 services, 11 of 11 DIDs checked"."""
    services = sum(1 for sid in description.services if coverage.service(sid).count)
    dids = sum(1 for did in description.dids if any(key[0] == did and cell.count for key, cell in coverage.dids.items()))
    return f"{services} of {len(description.services)} services, {dids} of {len(description.dids)} DIDs checked"
