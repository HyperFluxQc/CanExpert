"""
Which variant of its description an ECU is. An ODX file says it itself: each ECU variant's ECU-VARIANT-PATTERN
names services and the values their answers must give (read through odxtools). For a CDD or a JSON
description, the test plan says it: a DID to read and the value each variant answers - its text, or its bytes
in hex.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from canexpert.test_expert.generator import value_text


@dataclass
class Identification:
    """The DID that tells a description's variants apart, and the value each variant answers."""
    did: int | None = None
    values: dict = field(default_factory=dict)        # variant -> expected value (text, or hex bytes)

    def to_dict(self) -> dict:
        return {"did": f"{self.did:04X}" if self.did is not None else "", "values": dict(self.values)}

    @classmethod
    def from_dict(cls, values) -> "Identification":
        values = values or {}
        did = str(values.get("did", "") or "").strip()
        return cls(int(did, 16) if did else None, {str(key): str(value) for key, value in
                                                   (values.get("values") or {}).items()})

    def matches(self, expected: str, data: bytes) -> bool:
        """Whether data is the expected value: the same text, or the same bytes written in hex."""
        expected = expected.strip()
        if expected == value_text(data):
            return True
        try:
            return bytes.fromhex(expected.replace(" ", "")) == bytes(data)
        except ValueError:
            return False


def is_odx(path) -> bool:
    return bool(path) and Path(path).suffix.lower() not in (".cdd", ".json")


def identify(path, tester, identification: Identification | None = None) -> tuple[str | None, str]:
    """(the variant the ECU is, what told it) - or (None, why it could not be told). path: the description's
    file; tester: a test_expert.tester.Tester on the ECU."""
    reasons = []
    if is_odx(path):
        from canexpert.test_expert.odx import identify_odx

        def ask(request, physical):
            if not physical and tester.functional_id is None:
                return b""
            return tester.ask(request, functional=not physical, timeout=1.0).raw or b""
        variant, detail = identify_odx(path, ask)
        if variant is not None:
            return variant, f"its ECU-VARIANT-PATTERN: {detail}"
        reasons.append(detail)
    identification = identification or Identification()
    if identification.did is not None and identification.values:
        did = identification.did
        answer = tester.ask(b"\x22" + did.to_bytes(2, "big"), timeout=1.0)
        if not answer.positive() or answer.raw[1:3] != did.to_bytes(2, "big"):
            return None, f"22 {did:04X}: {answer.text()}"
        data = bytes(answer.raw[3:])
        for variant, expected in identification.values.items():
            if identification.matches(expected, data):
                return variant, f"{did:04X} answers {value_text(data)}"
        return None, f"{did:04X} answers {value_text(data)}: no variant expects it"
    reasons.append("the plan names no DID telling the variants apart")
    return None, "; ".join(reasons)
