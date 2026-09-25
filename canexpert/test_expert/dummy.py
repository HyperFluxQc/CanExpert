"""
The Dummy ECU's description, from its settings: what TestExpert expects of it - its sessions, security
levels, services with their sessions and sub-functions, DIDs with their sessions, levels and lengths, and
the routines: erasing and checking (start and results, in the programming session, unlocked) and the self
test (start, stop and results, in the extended session). Tested against it, a Dummy ECU that keeps the rules passes; its errors on purpose,
forced NRCs and changed settings show up as failures.
"""
from __future__ import annotations

from canexpert.simulator.ecu import (DEFAULT_SESSION, EXTENDED_SESSION, PROGRAMMING_SESSION, SERVICE_NAMES,
                                     SERVICE_SESSIONS, SUB_FUNCTIONS, EcuConfig, data_tables, security_levels,
                                     service_rules)
from canexpert.test_expert.description import (Access, DataIdentifier, EcuDescription, Routine, Service,
                                               Session)

ALL_SESSIONS = {DEFAULT_SESSION, PROGRAMMING_SESSION, EXTENDED_SESSION}
# DIDs every Dummy ECU answers besides its table: the active session, and the seconds since power-on.
BUILT_IN_DIDS = {0xF186: ("ActiveDiagnosticSession", 1), 0x0100: ("Uptime", 4)}


def dummy_description(config: EcuConfig | None = None) -> EcuDescription:
    config = config or EcuConfig()
    description = EcuDescription("Dummy ECU", "the Dummy ECU's settings")
    extended_first = {EXTENDED_SESSION} if config.programming_needs_extended else set()
    description.sessions = {
        DEFAULT_SESSION: Session(DEFAULT_SESSION, "Default session"),
        PROGRAMMING_SESSION: Session(PROGRAMMING_SESSION, "Programming session", extended_first),
        EXTENDED_SESSION: Session(EXTENDED_SESSION, "Extended session"),
    }
    levels = security_levels(config)
    description.security_levels = {level: f"Level 0x{level:02X}" for level in sorted(levels)}
    rules = service_rules(config)

    for sid, name in SERVICE_NAMES.items():
        if sid == 0x35 and not config.allow_upload:
            continue
        sessions = set(SERVICE_SESSIONS.get(sid, ())) if set(SERVICE_SESSIONS.get(sid, ())) != ALL_SESSIONS else set()
        rule = rules.get(sid)
        level_needed = set()
        if rule:
            rule_sessions, level = rule
            if rule_sessions:
                sessions = (sessions or set(ALL_SESSIONS)) & set(rule_sessions)
            if level:
                level_needed = {level}
        if sid in (0x34, 0x35):
            level_needed = level_needed or set(levels)        # a transfer needs some level unlocked
        access = Access(sessions, level_needed)
        service = Service(sid, name, access)
        for sub in SUB_FUNCTIONS.get(sid, ()):
            service.sub_functions[sub] = Access(set(sessions), set(level_needed))
        if sid == 0x27:
            for level in levels:
                service.sub_functions[level] = Access(set(sessions), set())
        description.services[sid] = service

    tables = data_tables(config)
    for did, data in tables.dids.items():
        _signal, did_sessions, level = tables.access.get(did, ("", (), 0))
        read = Access(set(did_sessions or ()), {level} if level else set())
        write = None
        if did in tables.writable and not _signal:
            write_sessions = set(did_sessions or ALL_SESSIONS) & set(SERVICE_SESSIONS[0x2E])
            write = Access(write_sessions, {level} if level else set(levels))     # WDBI needs some unlocked level
        description.dids[did] = DataIdentifier(did, f"DID 0x{did:04X}", len(data), read, write)
    for did, (name, length) in BUILT_IN_DIDS.items():
        description.dids.setdefault(did, DataIdentifier(did, name, length, Access(), None))

    for rid, name in ((config.erase_routine, "EraseMemory"), (config.check_routine, "CheckProgrammingDependencies")):
        description.routines[rid] = Routine(rid, name, {sub: Access({PROGRAMMING_SESSION}, set(levels))
                                                        for sub in (0x01, 0x03)})
    description.routines[config.self_test_routine] = Routine(
        config.self_test_routine, "SelfTest", {sub: Access({EXTENDED_SESSION}, set()) for sub in (0x01, 0x02, 0x03)})

    # As a CDD describes them: WriteDataByIdentifier where a DID may be written, RoutineControl where a routine
    # may run.
    def merged(accesses):
        accesses = list(accesses)
        result = accesses[0] if accesses else Access()
        for access in accesses[1:]:
            result = result.merged(access)
        return result
    writes = [entry.write for entry in description.dids.values() if entry.write is not None]
    if writes and 0x2E in description.services:
        description.services[0x2E].access = merged(writes)
    controls = {}
    for routine in description.routines.values():
        for sub, access in routine.sub_functions.items():
            controls.setdefault(sub, []).append(access)
    if controls and 0x31 in description.services:
        description.services[0x31].access = merged(access for accesses in controls.values() for access in accesses)
        description.services[0x31].sub_functions = {sub: merged(accesses) for sub, accesses in sorted(controls.items())}
    return description
