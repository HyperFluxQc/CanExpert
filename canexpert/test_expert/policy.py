"""
What TestExpert accepts besides ISO 14229-1's letter: the NRC policy - for each situation a test meets, the
negative response codes that pass (vehicle manufacturers' specifications choose differently where ISO leaves
room, or ask for another code) - and accepted deviations: failures known and agreed on, with a comment, which
are shown as accepted instead of failed.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from canexpert.uds.client import NRC_NAMES

# situation -> (what it is, the NRCs ISO 14229-1 allows there: the first is the one it asks for)
SITUATIONS = {
    "service_not_supported": ("A service the ECU does not have", (0x11,)),
    "service_not_in_session": ("A service not allowed in the active session", (0x7F,)),
    "sub_function_not_supported": ("A sub-function the service does not have", (0x12,)),
    "incorrect_length": ("A request too short or too long", (0x13,)),
    "session_not_from_here": ("A session that is not entered from the default session", (0x22, 0x7E)),
    "did_not_in_session": ("A DID read where it may not be", (0x31,)),
    "did_unknown": ("A DID that does not exist", (0x31,)),
    "did_read_only": ("Writing a DID that is read-only", (0x31,)),
    "did_out_of_range": ("Writing a value out of the DID's range", (0x31,)),
    "locked": ("What needs a security level, while locked", (0x33,)),
    "routine_not_in_session": ("A routine where it may not run", (0x31,)),
    "routine_unknown": ("A routine that does not exist", (0x31,)),
    "routine_sequence": ("Stopping a routine, or asking its results, before it was started", (0x24,)),
    "dtc_group_unknown": ("A group of DTCs that does not exist", (0x31,)),
    "key_before_seed": ("sendKey before requestSeed", (0x24,)),
    "invalid_key": ("A wrong key", (0x35,)),
    "attempts_exceeded": ("The wrong key that starts the lockout", (0x36,)),
    "delay_not_expired": ("requestSeed during the lockout delay", (0x37,)),
}
# What TestExpert accepted before it had a policy: a routine refused in the session with 0x7F or 0x7E.
DEFAULT_EXTRA = {"routine_not_in_session": (0x7F, 0x7E)}


def nrc_text(nrc: int) -> str:
    return f"0x{nrc:02X} {NRC_NAMES.get(nrc, '')}".strip()


def parse_nrcs(text: str) -> tuple[int, ...]:
    """"31, 7F" or "0x31 0x7F" -> (0x31, 0x7F)."""
    values = []
    for token in str(text).replace(",", " ").replace(";", " ").split():
        value = int(token.lower().removeprefix("0x"), 16)
        if not 0 <= value <= 0xFF:
            raise ValueError(f"an NRC is one byte: {token}")
        if value not in values:
            values.append(value)
    return tuple(values)


@dataclass
class NrcPolicy:
    """situation -> the NRCs that pass there, where they differ from the defaults (ISO's, and DEFAULT_EXTRA)."""
    nrcs: dict = field(default_factory=dict)

    @staticmethod
    def default(situation: str) -> tuple[int, ...]:
        return SITUATIONS[situation][1] + DEFAULT_EXTRA.get(situation, ())

    def accepted(self, situation: str) -> tuple[int, ...]:
        return tuple(self.nrcs.get(situation) or self.default(situation))

    @staticmethod
    def iso(situation: str) -> tuple[int, ...]:
        return SITUATIONS[situation][1]

    def set(self, situation: str, nrcs):
        nrcs = tuple(nrcs)
        if not nrcs or nrcs == self.default(situation):
            self.nrcs.pop(situation, None)
        else:
            self.nrcs[situation] = nrcs

    def to_dict(self) -> dict:
        return {situation: [f"{nrc:02X}" for nrc in nrcs] for situation, nrcs in sorted(self.nrcs.items())}

    @classmethod
    def from_dict(cls, values: dict) -> "NrcPolicy":
        policy = cls()
        for situation, nrcs in (values or {}).items():
            if situation in SITUATIONS:
                policy.set(situation, tuple(int(str(nrc), 16) if isinstance(nrc, str) else int(nrc) for nrc in nrcs))
        return policy


@dataclass
class Deviation:
    """A failure accepted: of a test (its name), at a step (its description; "*": any failed step of it)."""
    test: str
    step: str = "*"
    comment: str = ""
    added: str = ""                       # when it was accepted (YYYY-MM-DD)

    def covers(self, test: str, step: str) -> bool:
        return self.test in (test, "*") and self.step in (step, "*")

    def to_dict(self) -> dict:
        return {"test": self.test, "step": self.step, "comment": self.comment, "added": self.added}

    @classmethod
    def from_dict(cls, values: dict) -> "Deviation":
        return cls(str(values.get("test", "")), str(values.get("step", "*")), str(values.get("comment", "")),
                   str(values.get("added", "")))


def accept_function(deviations):
    """accept(test name, step description) for canexpert.testing.runner: the comment of the deviation that
    covers a failed step, else None."""
    deviations = list(deviations)

    def accept(test, step):
        for deviation in deviations:
            if deviation.covers(test, step):
                return deviation.comment
        return None
    return accept if deviations else None


def today() -> str:
    return datetime.date.today().isoformat()
