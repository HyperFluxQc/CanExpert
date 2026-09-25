"""
ODX and PDX diagnostic descriptions as TestExpert descriptions, through odxtools: each service's request
starts with its coded constants; its pre-condition states say where it may be executed, and its state
transitions which session or security state it leads to. State charts with the semantic SESSION or SECURITY
(or a name saying so) are the session and security states. A ReadDataByIdentifier's positive response gives
the DID's fields: each value parameter's place (BYTE-POSITION, BIT-POSITION), its coded type (bit length, base
data type), its text table or linear scale (COMPU-METHOD), its limits (INTERNAL-CONSTR) and its unit.

States are matched by their ODX IDs: a variant inheriting its state charts gets copies of the base variant's
objects. A DID or a routine is named after its services, without the _Read, _Write, _Start... CANdelaStudio's
ODX export puts after them.
"""
from __future__ import annotations

from pathlib import Path

from canexpert.odx_services import first_layer, load_database, name_of
from canexpert.test_expert.description import DataField, EcuDescription, RawService, RawState, build_description

RECORD_START = 3                     # 62, then the DID: the data record's first byte in the response
NAME_ENDINGS = ("_Read", "_Write", "_Start", "_Stop", "_RequestResults", "_Results", "_RequestSeed", "_SendKey")


def _key(state):
    """What identifies a state in every copy of it: its ODX ID, else its name."""
    link = getattr(state, "odx_id", None)
    return getattr(link, "local_id", None) or name_of(state)


def plain_name(name: str) -> str:
    """"VIN_Read" -> "VIN": a DID or routine named after its services."""
    for ending in NAME_ENDINGS:
        if name.endswith(ending) and len(name) > len(ending):
            return name[:-len(ending)]
    return name


def _group(chart) -> str:
    text = f"{getattr(chart, 'semantic', '') or ''} {name_of(chart)}".lower()
    return "session" if "session" in text else "security" if "secur" in text else ""


def _value(limit):
    """A limit's value as a number, or None."""
    value = getattr(limit, "value", limit)
    try:
        return int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _field(param) -> DataField | None:
    """A DataField of a response's value parameter, or None where it cannot be told where it is."""
    dop = getattr(param, "dop", None)
    coded = getattr(dop, "diag_coded_type", None)
    byte = getattr(param, "byte_position", None)
    bits = getattr(coded, "bit_length", None)
    if dop is None or byte is None or not bits or byte < RECORD_START:
        return None
    base = getattr(coded, "base_data_type", "")
    base = str(getattr(base, "value", base)).upper()          # odxtools' DataType: its value is A_UINT32...
    encoding = "ascii" if "ASCII" in base else "bytes" if "BYTE" in base or "UNICODE" in base else \
        "signed" if "_INT" in base and "UINT" not in base else "unsigned"
    shift_bits = getattr(param, "bit_position", None) or 0
    span = (bits + shift_bits + 7) // 8
    position = (byte - RECORD_START) * 8 + span * 8 - shift_bits - bits
    field = DataField(str(getattr(param, "short_name", "") or f"Byte{byte}"), position, int(bits), encoding)
    compu = getattr(dop, "compu_method", None)
    category = str(getattr(compu, "category", "")).upper()
    scales = getattr(getattr(compu, "compu_internal_to_phys", None), "compu_scales", None) or []
    if "TEXTTABLE" in category:
        for scale in scales:
            low, high = _value(getattr(scale, "lower_limit", None)), _value(getattr(scale, "upper_limit", None))
            text = getattr(getattr(scale, "compu_const", None), "vt", None)
            if low is None:
                continue
            high = low if high is None else high
            if low == high and text is not None:
                field.texts[low] = str(text)
            else:
                field.valid.append((low, high))
        if field.valid and field.texts:
            field.valid += [(value, value) for value in field.texts]
    elif "LINEAR" in category and scales:
        coefficients = getattr(scales[0], "compu_rational_coeffs", None)
        numerators = list(getattr(coefficients, "numerators", None) or [])
        denominators = list(getattr(coefficients, "denominators", None) or [1])
        if len(numerators) >= 2 and denominators[0]:
            field.shift, field.scale = float(numerators[0]) / denominators[0], float(numerators[1]) / denominators[0]
    constraint = getattr(dop, "internal_constr", None)
    low, high = _value(getattr(constraint, "lower_limit", None)), _value(getattr(constraint, "upper_limit", None))
    if low is not None and high is not None and not field.texts:
        field.valid = [(low, high)]
    unit = getattr(dop, "unit", None)
    field.unit = str(getattr(unit, "display_name", None) or getattr(unit, "short_name", "") or "") if unit else ""
    return field


def did_fields(service) -> list[DataField]:
    """The fields of a ReadDataByIdentifier service's record, from its first positive response; [] when one
    of its value parameters cannot be placed."""
    responses = list(getattr(service, "positive_responses", None) or [])
    if not responses:
        return []
    fields = []
    for param in getattr(responses[0], "parameters", None) or []:
        kind = str(getattr(param, "parameter_type", "") or type(param).__name__).upper()
        if "CONST" in kind or getattr(param, "dop", None) is None:
            continue                                # the SID, the DID
        try:
            field = _field(param)
        except (AttributeError, TypeError, ValueError):
            field = None
        if field is None:
            return []
        fields.append(field)
    return fields


def load_odx(path) -> EcuDescription:
    database = load_database(path)
    layer = first_layer(database)
    if layer is None:
        raise ValueError("the file describes no ECU")
    states = {}
    for chart in getattr(layer, "state_charts", None) or []:
        group = _group(chart)
        for state in getattr(chart, "states", None) or []:
            states[_key(state)] = RawState(group, getattr(state, "long_name", None) or name_of(state))
    warnings, raw = [], []
    for service in getattr(layer, "services", None) or []:
        request = getattr(service, "request", None)
        if request is None:
            continue
        try:
            prefix = bytes(request.coded_const_prefix())
        except Exception as exc:                     # a request odxtools cannot encode the start of
            warnings.append(f"{name_of(service)}: {exc}")
            continue
        preconditions = list(getattr(service, "pre_condition_states", None) or [])
        allowed = [_key(state) for state in preconditions] if preconditions else None
        transitions = [(_key(transition.source_state), _key(transition.target_state))
                       for transition in getattr(service, "state_transitions", None) or []
                       if getattr(transition, "target_state", None) is not None]
        fields = did_fields(service) if prefix[:1] == b"\x22" and len(prefix) >= 3 else []
        length = max((item.position + item.bits + 7) // 8 for item in fields) if fields else None
        raw.append(RawService(prefix, plain_name(name_of(service)), allowed, transitions, length, fields))
    description = build_description(raw, states, name_of(layer), str(path))
    description.warnings = warnings + description.warnings
    if not states:
        description.warnings.append("No state charts: every service is taken as allowed in every session")
    elif not any(state.group == "session" for state in states.values()):
        description.warnings.append("No state chart of sessions (semantic SESSION): the sessions are taken from "
                                    "DiagnosticSessionControl's constants")
    if 0 in description.sessions:
        description.warnings.append("10 00 is no UDS session (ISO 14229-1 reserves 00): this is not a UDS ECU's "
                                    "description, or not all of it")
    return description


def load_description(path) -> EcuDescription:
    """Any description file: .cdd, .json (TestExpert's own), else ODX/PDX."""
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        return EcuDescription.load(path)
    if suffix == ".cdd":
        from canexpert.test_expert.cdd import load_cdd
        return load_cdd(path)
    return load_odx(path)
