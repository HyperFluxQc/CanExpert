"""
TestExpert's side of the exchange: a request, physically or functionally addressed, and the ECU's answer or
its silence, timed - to its first frame, to each response pending (NRC 0x78) and to the final answer.
"""
from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass, field

import can

from canexpert.uds.client import BUSY_RETRIES, BUSY_RETRY_DELAY, NRC_NAMES
from canexpert.uds.isotp import IsoTpError, drain, isotp_recv, isotp_send

SILENCE = 0.3                 # how long an ECU that must not answer is listened to
PENDING_TIMEOUT = 10.0        # after a response pending, how long the final answer is waited for (at least)
DEFAULT_SESSION = 0x01


@dataclass
class Answer:
    request: bytes
    raw: bytes | None
    elapsed: float            # seconds from the request to the answer (to the wait's end without one)
    functional: bool = False
    first: float | None = None                     # seconds to the first frame of the answer: 0x78 or the answer
    pending: list = field(default_factory=list)    # seconds to each response pending (NRC 0x78)
    retries: int = 0                               # busyRepeatRequest (0x21) answers the request was sent again for
    error: str = ""                                # a transport error: the answer could not be read

    @property
    def nrc(self) -> int | None:
        raw = self.raw
        return raw[2] if raw is not None and len(raw) >= 3 and raw[0] == 0x7F else None

    def positive(self) -> bool:
        return self.raw is not None and bool(self.request) and self.raw[0] == (self.request[0] + 0x40) & 0xFF

    def text(self) -> str:
        """"22 F1 90 -> 62 F1 90 57 ... (4 ms)", with the NRC's name and the responses pending."""
        request = self.request.hex(" ").upper()
        how = " functional" if self.functional else ""
        if self.raw is None:
            why = self.error or "no answer"
            return f"{request}{how} -> {why} ({self.elapsed * 1000:.0f} ms)"
        shown = self.raw[:24].hex(" ").upper() + (" ..." if len(self.raw) > 24 else "")
        name = f" {NRC_NAMES.get(self.nrc, 'unknown')}" if self.nrc is not None else ""
        extra = ""
        if self.pending:
            extra += f", after {len(self.pending)} response pending (first at {self.pending[0] * 1000:.0f} ms)"
        if self.retries:
            extra += f", sent {self.retries + 1} times (busy)"
        return f"{request}{how} -> {shown}{name} ({self.elapsed * 1000:.0f} ms{extra})"


class Tester:
    """Requests on a bus facade (the mailbox of TestExpert's CAN worker, or a bus) with a UDS transport
    (canexpert.config.uds_transport); functional requests go to functional_id. It follows the session the ECU
    is in from its answers to DiagnosticSessionControl and ECUReset (None: not known yet)."""

    def __init__(self, bus, transport: dict, functional_id: int | None = None, silence: float = SILENCE):
        self.bus, self.transport, self.functional_id, self.silence = bus, dict(transport), functional_id, silence
        self.pending_timeout = PENDING_TIMEOUT
        self.session = None
        self.log = []                 # every Answer, for the report's detail
        self.listeners = []           # listener(Answer) after every exchange

    def ask(self, payload, functional: bool = False, timeout: float | None = None) -> Answer:
        options = self.transport
        payload = bytes(payload)
        if functional and self.functional_id is None:
            raise RuntimeError("no functional request ID set")
        request_id = self.functional_id if functional else options["request_id"]
        response_id = options["response_id"]
        timeout = timeout if timeout is not None else options.get("timeout", 2.0)
        link = (options.get("extended", False), options.get("address_byte"), options.get("padding"))
        transaction = getattr(self.bus, "transaction", None)
        raw, error, first, pending, retries = None, "", None, [], 0
        started = time.monotonic()
        with transaction() if callable(transaction) else nullcontext():
            try:
                drain(self.bus)
                isotp_send(self.bus, request_id, payload, response_id, *link)
                started = time.monotonic()
                deadline = started + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    reply = isotp_recv(self.bus, response_id, request_id, remaining, *link,
                                       options.get("block_size", 0), options.get("st_min", 0))
                    if reply is None:
                        break
                    moment = time.monotonic() - started
                    if reply[0] == (payload[0] + 0x40) & 0xFF:
                        if payload[0] == 0x2A and len(reply) > 1:     # periodic data, not the answer
                            continue
                        first = moment if first is None else first
                        raw = reply
                        break
                    if reply[0] == 0x7F and len(reply) >= 3 and reply[1] == payload[0]:
                        first = moment if first is None else first
                        if reply[2] == 0x78:
                            pending.append(moment)
                            deadline = time.monotonic() + self.pending_timeout
                            continue
                        if reply[2] == 0x21 and retries < BUSY_RETRIES:
                            retries += 1
                            time.sleep(BUSY_RETRY_DELAY)
                            isotp_send(self.bus, request_id, payload, response_id, *link)
                            deadline = time.monotonic() + timeout
                            continue
                        raw = reply
                        break
                    # anything else - an event's response, periodic data - is not the answer
            except IsoTpError as exc:
                error = f"ISO-TP error: {exc}"
        answer = Answer(payload, raw, time.monotonic() - started, functional, first, pending, retries, error)
        self._follow(answer)
        self.log.append(answer)
        for listener in self.listeners:
            listener(answer)
        return answer

    def quiet(self, payload, functional: bool = False) -> Answer:
        """A request that must not be answered: listened to for the silence time only."""
        return self.ask(payload, functional, timeout=self.silence)

    def send_frame(self, message: can.Message):
        """A frame of a sequence (ignition on through a gateway, ...) on the tester's bus."""
        self.bus.send(message)

    def _follow(self, answer: Answer):
        request = answer.request
        if len(request) < 2 or request[0] not in (0x10, 0x11):
            return
        suppressed = bool(request[1] & 0x80) and answer.raw is None and not answer.error
        if answer.positive() or suppressed:
            self.session = request[1] & 0x7F if request[0] == 0x10 else DEFAULT_SESSION
