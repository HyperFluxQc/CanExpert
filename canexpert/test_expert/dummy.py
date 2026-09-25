"""
The Dummy ECU's description, from its settings: what TestExpert expects of it - its sessions, security
levels, services with their sessions and sub-functions, DIDs with their sessions, levels, lengths and fields
(what each holds, the values it may take), and
the routines: erasing and checking (start and results, in the programming session, unlocked) and the self
test (start, stop and results, in the extended session). Tested against it, a Dummy ECU that keeps the rules passes; its errors on purpose,
forced NRCs and changed settings show up as failures.
"""
from __future__ import annotations

from canexpert.simulator.ecu import (DEFAULT_SESSION, EXTENDED_SESSION, PROGRAMMING_SESSION, SERVICE_NAMES,
                                     SERVICE_SESSIONS, SUB_FUNCTIONS, EcuConfig, data_tables, security_levels,
                                     service_rules)
from canexpert.test_expert.description import (Access, DataField, DataIdentifier, EcuDescription, Routine,
                                               Service, Session)

ALL_SESSIONS = {DEFAULT_SESSION, PROGRAMMING_SESSION, EXTENDED_SESSION}
# DIDs every Dummy ECU answers besides its table: the active session, and the seconds since power-on.
BUILT_IN_DIDS = {0xF186: ("ActiveDiagnosticSession", 1), 0x0100: ("Uptime", 4)}
# The names of the Dummy ECU's own DIDs (others: "DID 0x...").
DID_NAMES = {0xF187: "SparePartNumber", 0xF18C: "ECUSerialNumber", 0xF190: "VIN", 0xF195: "SoftwareVersion",
             0x0101: "Temperature", 0x0102: "Pressure", 0xF201: "PeriodicTemperature", 0xF202: "PeriodicPressure",
             0x0200: "CalibrationId", 0x0110: "IdleSpeedTarget"}
# The default DBC's signals a DID can follow: (scale, unit) of their raw value.
SIGNAL_SCALES = {"EngineData.Temperature": (0.1, "degC"), "EngineData.Pressure": (0.01, "bar")}
SESSION_TEXTS = {DEFAULT_SESSION: "Default", PROGRAMMING_SESSION: "Programming", EXTENDED_SESSION: "Extended"}
# Texts of a length of their own: the software version, which the bootloader and a flashed image change.
VARIABLE_TEXT = {0xF195}


def did_fields(did: int, name: str, data: bytes, signal: str = "", valid=()) -> list[DataField]:
    """What a DID of the Dummy ECU holds: its valid values, its signal's scale, text, or nothing known."""
    bits = len(data) * 8
    if did == 0xF186:
        return [DataField("Session", 0, 8, texts=dict(SESSION_TEXTS))]
    if did == 0x0100:
        return [DataField("Seconds", 0, 32, unit="s")]
    if did == 0x0110:
        return [DataField("Speed", 0, bits, valid=[tuple(pair) for pair in valid], unit="rpm")]
    if did in VARIABLE_TEXT:
        return [DataField("Text", 0, 0, "ascii")]
    if valid:
        return [DataField(name, 0, bits, valid=[tuple(pair) for pair in valid])]
    if signal:
        scale, unit = SIGNAL_SCALES.get(signal, (1.0, ""))
        return [DataField(signal.rpartition(".")[2], 0, bits, scale=scale, unit=unit)]
    if data and all(0x20 <= byte < 0x7F for byte in data):
        return [DataField("Text", 0, bits, "ascii")]
    return []


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
        signal, did_sessions, level = tables.access.get(did, ("", (), 0))
        read = Access(set(did_sessions or ()), {level} if level else set())
        write = None
        if did in tables.writable and not signal:
            write_sessions = set(did_sessions or ALL_SESSIONS) & set(SERVICE_SESSIONS[0x2E])
            write = Access(write_sessions, {level} if level else set(levels))     # WDBI needs some unlocked level
        name = DID_NAMES.get(did, f"DID 0x{did:04X}")
        description.dids[did] = DataIdentifier(did, name, None if did in VARIABLE_TEXT else len(data), read, write,
                                               did_fields(did, name, data, signal, tables.valid.get(did, ())))
    for did, (name, length) in BUILT_IN_DIDS.items():
        description.dids.setdefault(did, DataIdentifier(did, name, length, Access(), None,
                                                        did_fields(did, name, bytes(length))))

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
