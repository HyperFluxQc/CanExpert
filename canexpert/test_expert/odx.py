"""
ODX and PDX diagnostic descriptions as TestExpert descriptions, through odxtools: each service's request
starts with its coded constants; its pre-condition states say where it may be executed, and its state
transitions which session or security state it leads to. State charts with the semantic SESSION or SECURITY
(or a name saying so) are the session and security states.
"""
from __future__ import annotations

from pathlib import Path

from canexpert.odx_services import first_layer, load_database, name_of
from canexpert.test_expert.description import EcuDescription, RawService, RawState, build_description


def _group(chart) -> str:
    text = f"{getattr(chart, 'semantic', '') or ''} {name_of(chart)}".lower()
    return "session" if "session" in text else "security" if "secur" in text else ""


def load_odx(path) -> EcuDescription:
    database = load_database(path)
    layer = first_layer(database)
    if layer is None:
        raise ValueError("the file describes no ECU")
    states, keys = {}, {}
    for chart in getattr(layer, "state_charts", None) or []:
        group = _group(chart)
        for state in getattr(chart, "states", None) or []:
            key = id(state)
            keys[key] = state
            states[key] = RawState(group, name_of(state))
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
        allowed = [id(state) for state in preconditions] if preconditions else None
        transitions = [(id(transition.source_state), id(transition.target_state))
                       for transition in getattr(service, "state_transitions", None) or []
                       if getattr(transition, "target_state", None) is not None]
        raw.append(RawService(prefix, name_of(service), allowed, transitions))
    description = build_description(raw, states, name_of(layer), str(path))
    description.warnings = warnings + description.warnings
    if not states:
        description.warnings.append("No state charts: every service is taken as allowed in every session")
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
