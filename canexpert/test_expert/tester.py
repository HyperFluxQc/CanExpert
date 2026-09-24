"""
TestExpert's side of the exchange: a request, physically or functionally addressed, and the ECU's answer or
its silence, timed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from canexpert.uds.client import NRC_NAMES, uds_request

SILENCE = 0.3                 # how long an ECU that must not answer is listened to


@dataclass
class Answer:
    request: bytes
    raw: bytes | None
    elapsed: float            # seconds from the request to the answer (to the wait's end without one)
    functional: bool = False

    @property
    def nrc(self) -> int | None:
        raw = self.raw
        return raw[2] if raw is not None and len(raw) >= 3 and raw[0] == 0x7F else None

    def positive(self) -> bool:
        return self.raw is not None and bool(self.request) and self.raw[0] == (self.request[0] + 0x40) & 0xFF

    def text(self) -> str:
        """"22 F1 90 -> 62 F1 90 57 ... (4 ms)", with the NRC's name."""
        request = self.request.hex(" ").upper()
        if self.raw is None:
            return f"{request} -> no answer ({self.elapsed * 1000:.0f} ms)"
        shown = self.raw[:24].hex(" ").upper() + (" ..." if len(self.raw) > 24 else "")
        name = f" {NRC_NAMES.get(self.nrc, 'unknown')}" if self.nrc is not None else ""
        how = " functional" if self.functional else ""
        return f"{request}{how} -> {shown}{name} ({self.elapsed * 1000:.0f} ms)"


class Tester:
    """Requests on a bus facade (the mailbox of TestExpert's CAN worker) with a UDS transport
    (canexpert.config.uds_transport); functional requests go to functional_id."""

    def __init__(self, bus, transport: dict, functional_id: int | None = None, silence: float = SILENCE):
        self.bus, self.transport, self.functional_id, self.silence = bus, dict(transport), functional_id, silence
        self.log = []                 # every Answer, for the report's detail

    def ask(self, payload, functional: bool = False, timeout: float | None = None) -> Answer:
        options = dict(self.transport)
        if functional:
            if self.functional_id is None:
                raise RuntimeError("no functional request ID set")
            options["request_id"] = self.functional_id
        options["timeout"] = timeout if timeout is not None else options.get("timeout", 2.0)
        payload = bytes(payload)
        started = time.monotonic()
        raw = uds_request(self.bus, payload, **options)
        answer = Answer(payload, raw, time.monotonic() - started, functional)
        self.log.append(answer)
        return answer

    def quiet(self, payload, functional: bool = False) -> Answer:
        """A request that must not be answered: listened to for the silence time only."""
        return self.ask(payload, functional, timeout=self.silence)
