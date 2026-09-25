"""
The transport layer's tests (ISO 15765-2, classic CAN), with raw frames on the plan's identifiers, addressing
and padding (Link). How the ECU takes a segmented request: its flow control, a consecutive frame out of
sequence or too late, a new request in the middle of one, frames it must ignore, a request longer than it takes.
How it sends a segmented answer: keeping to the tester's block size and STmin, waiting on a flow control WAIT,
stopping on an overflow, a reserved flow status or none at all.
"""
from __future__ import annotations

import time
from contextlib import nullcontext
from dataclasses import dataclass

import can

from canexpert.test_expert.description import DEFAULT_SESSION
from canexpert.uds.client import hex_text
from canexpert.uds.isotp import FC_CONTINUE, FC_OVERFLOW, FC_WAIT, IsoTpError, drain, flow_control_frame, isotp_recv

GROUP = "Transport layer (ISO 15765-2)"
N_BS = 1.0                  # how long a sender waits for a flow control (N_Bs)
N_CR = 1.0                  # how long a receiver waits for a consecutive frame (N_Cr)
LATE = N_CR + 0.25          # later than a receiver waits
QUIET = 0.3                 # how long what must not come is listened for
BLOCK_PAUSE = 0.15          # after a block, no consecutive frame may come before the next flow control
ST_MIN = 0x32               # 50 ms: the STmin the ECU is asked to keep
WAIT_HOLD = 0.2             # a flow control WAIT held this long before ContinueToSend
MAX_WAITS = 16              # flow control WAITs taken before giving up (N_WFTmax)
LONGEST = 0xFFF             # the longest request a first frame announces without the escape sequence
FLOW_STATUS = {FC_CONTINUE: "ContinueToSend", FC_WAIT: "Wait", FC_OVERFLOW: "Overflow"}


def valid_st_min(value: int) -> bool:
    """ISO 15765-2: 00-7F ms, F1-F9 100-900 us; the rest is reserved."""
    return value <= 0x7F or 0xF1 <= value <= 0xF9


def st_min_seconds(value: int) -> float:
    if value <= 0x7F:
        return value / 1000
    if 0xF1 <= value <= 0xF9:
        return (value - 0xF0) / 10000
    return 0x7F / 1000                  # a reserved value: the longest



@dataclass
class Frame:
    """A frame of the ECU: its data from the N_PCI on (extended addressing's address byte taken off), when it
    was read (time.perf_counter()), its own timestamp and its CAN data length."""
    data: bytes
    read: float
    stamp: float
    dlc: int

    @property
    def kind(self) -> int:
        return self.data[0] >> 4

    @property
    def status(self) -> int:
        return self.data[0] & 0x0F

    def text(self) -> str:
        return hex_text(self.data)


def gap(earlier: Frame, later: Frame) -> float:
    """The time between two frames: the longer of what the reader saw and what their timestamps say - one
    clock may be coarse (15.6 ms on Windows), the other late when the reader was busy."""
    seen = later.read - earlier.read
    if earlier.stamp and later.stamp:
        return max(seen, later.stamp - earlier.stamp)
    return seen


class Link:
    """Raw frames between TestExpert and the ECU: sent on the request (or functional) identifier with the
    plan's addressing and padding, read from the response identifier."""

    def __init__(self, tester):
        options = tester.transport
        self.tester, self.bus = tester, tester.bus
        self.request_id, self.response_id = options["request_id"], options["response_id"]
        self.functional_id = tester.functional_id
        self.extended = bool(options.get("extended", False))
        self.address_byte = options.get("address_byte")
        self.padding = options.get("padding")
        self.room = 7 - (self.address_byte is not None)            # the data bytes of a single frame
        self.timeout = options.get("timeout", 2.0)

    def exchange(self):
        """The frames are this link's meanwhile (a CAN worker's mailbox hands them over)."""
        transaction = getattr(self.bus, "transaction", None)
        return transaction() if callable(transaction) else nullcontext()

    def drain(self):
        drain(self.bus)

    def send(self, body, functional=False, pad=True) -> float:
        """One frame; pad=False sends it as short as it is, whatever the plan's padding. Returns when it went
        (time.perf_counter())."""
        data = bytes(body)
        if self.address_byte is not None:
            data = bytes([self.address_byte]) + data
        if pad and self.padding is not None:
            data = data.ljust(8, bytes([self.padding]))
        self.bus.send(can.Message(arbitration_id=self.functional_id if functional else self.request_id,
                                  data=data[:8], is_extended_id=self.extended))
        return time.perf_counter()

    def receive(self, timeout) -> Frame | None:
        """The ECU's next frame within timeout, or None."""
        deadline = time.perf_counter() + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            message = self.bus.recv(min(remaining, 0.05))
            if message is None or message.is_error_frame or message.arbitration_id != self.response_id or \
                    bool(message.is_extended_id) != self.extended:
                continue
            data = bytes(message.data)[1 if self.address_byte is not None else 0:]
            if data:
                return Frame(data, time.perf_counter(), message.timestamp or 0.0, len(message.data))

    def flow_control(self, timeout=N_BS) -> Frame | None:
        """The ECU's next flow control frame; what else it sends meanwhile is passed over."""
        deadline = time.perf_counter() + timeout
        while True:
            frame = self.receive(deadline - time.perf_counter())
            if frame is None or (frame.kind == 0x3 and len(frame.data) >= 3):
                return frame

    def continue_to_send(self, timeout=N_BS) -> Frame | None:
        """The ECU's next ContinueToSend, through its WAITs; None when it sends none (or an overflow)."""
        for _ in range(MAX_WAITS + 1):
            flow = self.flow_control(timeout)
            if flow is None or flow.status != FC_WAIT:
                return flow if flow is not None and flow.status == FC_CONTINUE else None
        return None

    def segments(self, payload) -> tuple[bytes, list[bytes]]:
        """A request's first frame and its consecutive frames (sequence numbers from 1)."""
        payload = bytes(payload)
        first = bytes([0x10 | len(payload) >> 8, len(payload) & 0xFF]) + payload[:self.room - 1]
        rest = payload[self.room - 1:]
        return first, [bytes([0x20 | (number + 1) & 0x0F]) + rest[offset:offset + self.room]
                       for number, offset in enumerate(range(0, len(rest), self.room))]

    def send_consecutive(self, frames, flow: Frame) -> str:
        """Consecutive frames as the ECU's flow control asks: its block size and STmin, a new flow control
        after each block. "" when all went, else why not."""
        block, pause, sent = flow.data[1], st_min_seconds(flow.data[2]), 0
        for body in frames:
            if block and sent == block:
                flow = self.continue_to_send()
                if flow is None:
                    return f"no ContinueToSend after a block of {block}"
                block, pause, sent = flow.data[1], st_min_seconds(flow.data[2]), 0
            self.send(body)
            sent += 1
            if pause:
                time.sleep(pause)
        return ""

    def answer(self, sid, timeout=None) -> bytes | None:
        """The ECU's answer to a request of the service, through its responses pending: a single frame, or a
        segmented one taken with a flow control of no limits; None without one."""
        deadline = time.perf_counter() + (self.timeout if timeout is None else timeout)
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            try:
                reply = isotp_recv(self.bus, self.response_id, self.request_id, remaining, self.extended,
                                   self.address_byte, self.padding)
            except IsoTpError:
                return None
            if reply is None:
                return None
            if reply[:3] == bytes([0x7F, sid, 0x78]):
                deadline = time.perf_counter() + self.tester.pending_timeout
                continue
            if reply[0] == (sid + 0x40) & 0xFF or reply[:2] == bytes([0x7F, sid]):
                return reply

    def first_frame(self, sid) -> Frame | None:
        """The first frame of the ECU's answer to a request of the service (its responses pending passed over);
        a single frame when the answer is not segmented; None without one."""
        deadline = time.perf_counter() + self.timeout
        while True:
            frame = self.receive(deadline - time.perf_counter())
            if frame is None:
                return None
            if frame.kind == 0x0 and frame.data[1:4] == bytes([0x7F, sid, 0x78]):
                deadline = time.perf_counter() + self.tester.pending_timeout
                continue
            if frame.kind in (0x0, 0x1):
                return frame


class TransportTests:
    """The group of transport layer tests of a Suite: the ECU as the receiver of a segmented request, then -
    when a DID with a segmented answer is known - as its sender."""

    def __init__(self, suite):
        self.s = suite
        d = suite.d
        readable = [did for did in sorted(d.dids) if d.dids[did].read is not None and not d.dids[did].read.levels
                    and d.dids[did].read.allows(DEFAULT_SESSION)]
        # A request of several frames: a read of four DIDs (one again and again if need be), else a
        # TesterPresent too long, answered with NRC 0x13 - either way an answer.
        if readable and 0x22 in d.services:
            dids = (readable * 4)[:4]
            self.request = b"\x22" + b"".join(did.to_bytes(2, "big") for did in dids)
        else:
            self.request = b"\x3e\x00" + bytes(7)
        self.short = b"\x3e\x00" if 0x3E in d.services else b"\x10\x01"
        # An answer of several frames - two consecutive frames at least: a DID of 11 bytes or more.
        long = [did for did in readable if (d.dids[did].length or 0) >= 11]
        self.long_did = max(long, key=lambda did: (d.dids[did].length, -did)) if long and 0x22 in d.services \
            else None

    def add(self):
        add = self.s._add
        add(GROUP, "A segmented request: flow control and answer", self.segmented_request,
            "The first frame is answered by a flow control within N_Bs - ContinueToSend, a valid STmin - and the "
            "whole request, sent as it asks, is answered.")
        add(GROUP, "A consecutive frame out of sequence", self.wrong_sequence,
            "A consecutive frame with the wrong sequence number ends the request (N_WRONG_SN): it is not answered.")
        add(GROUP, "A consecutive frame after N_Cr", self.late,
            "Consecutive frames later than N_Cr (1000 ms) are not taken (N_TIMEOUT_Cr): the request is not "
            "answered.")
        add(GROUP, "A new request during a segmented one", self.interrupted,
            "A single frame during a segmented request is taken and the segmented one dropped (N_UNEXP_PDU).")
        add(GROUP, "Frames that are ignored", self.ignored,
            "Consecutive and flow control frames out of the blue, single frames of length 0 or longer than their "
            "CAN frame, a first frame of a length a single frame carries, a first frame functionally addressed.")
        add(GROUP, "A request longer than the ECU takes", self.overflow,
            "A first frame of 4095 bytes is answered by a flow control: ContinueToSend when the ECU takes that "
            "much, else Overflow.")
        if self.long_did is None:
            return
        add(GROUP, "The tester's block size", self.block_size,
            "With a block size of 1, the ECU sends one consecutive frame, then waits for the next flow control.")
        add(GROUP, "The tester's STmin", self.st_min,
            f"With an STmin of {ST_MIN} ms, the ECU leaves that much between its consecutive frames.")
        add(GROUP, "A flow control WAIT", self.wait,
            "After a flow control WAIT, the ECU sends nothing until ContinueToSend, then the rest of the answer.")
        add(GROUP, "A flow control overflow", self.overflow_answer,
            "After a flow control Overflow, the ECU stops the answer.")
        add(GROUP, "A reserved flow status", self.reserved_status,
            "After a flow control with a reserved flow status, the ECU stops the answer (N_INVALID_FS).")
        add(GROUP, "No flow control", self.no_flow_control,
            "Without a flow control, the ECU sends no consecutive frame and gives up after N_Bs (1000 ms).")

    # --- helpers ----------------------------------------------------------------------------------------------

    def _link(self) -> Link:
        return Link(self.s.tester)

    def _quiet(self) -> float:
        return max(QUIET, self.s.p2 + self.s.o.timing_margin_ms / 1000)

    def _silent(self, t, link, what, seconds=None):
        """A step: the ECU sends nothing for a while."""
        frame = link.receive(self._quiet() if seconds is None else seconds)
        return t.check(frame is None, what, f"it sent {frame.text()}" if frame else "nothing came")

    def _answers_again(self, t):
        """A step: the ECU answers a request again."""
        request = self.short
        self.s.positive(t, request, f"the ECU answers again ({hex_text(request)})", echo=request[1:2])

    def _start(self, t, link, payload):
        """Send a request's first frame and take the ECU's ContinueToSend; the test ends without it."""
        first, consecutive = link.segments(payload)
        link.drain()
        link.send(first)
        flow = link.continue_to_send()
        t.require(flow is not None, f"the first frame of {hex_text(payload[:3])}... ({len(payload)} bytes) is "
                                    f"answered by ContinueToSend", flow.text() if flow else "none came")
        return flow, consecutive

    def _ask_long(self, t, link):
        """Ask for the long DID; its answer's first frame, or the test ends."""
        request = b"\x22" + self.long_did.to_bytes(2, "big")
        link.drain()
        link.send(bytes([len(request)]) + request)
        first = link.first_frame(0x22)
        t.require(first is not None and first.kind == 0x1,
                  f"22 {self.long_did:04X} is answered in several frames: a first frame",
                  first.text() if first else "no answer")
        total = ((first.data[0] & 0x0F) << 8) | first.data[1]
        return first, total, bytearray(first.data[2:])

    def _consecutive(self, t, link, data, total, number, timeout=N_CR):
        """The ECU's next consecutive frame, checked for its sequence number; None when none came."""
        frame = link.receive(timeout)
        if frame is None or frame.kind != 0x2:
            return None
        if frame.status != number & 0x0F:
            t.check(False, "the consecutive frames' sequence numbers follow each other",
                    f"{frame.status:X} after {(number - 1) & 0x0F:X}")
        data += frame.data[1:]
        return frame

    def _whole(self, t, data, total):
        what = f"the answer is whole: 62 {self.long_did:04X} and {total - 3} bytes"
        t.check(len(data) >= total and bytes(data[:3]) == b"\x62" + self.long_did.to_bytes(2, "big"), what,
                f"{len(data)} of {total} bytes: {hex_text(data[:12])}{' ...' if len(data) > 12 else ''}")

    # --- the ECU receiving -------------------------------------------------------------------------------------

    def segmented_request(self, t):
        link = self._link()
        payload = self.request
        first, consecutive = link.segments(payload)
        with link.exchange():
            link.drain()
            sent = link.send(first)
            flow = link.flow_control()
            t.require(flow is not None, f"the first frame of {hex_text(payload[:3])}... ({len(payload)} bytes) is "
                                        f"answered by a flow control within N_Bs ({N_BS * 1000:.0f} ms)",
                      f"{flow.text()} after {(flow.read - sent) * 1000:.0f} ms" if flow else "none came")
            waits = 0
            while flow is not None and flow.status == FC_WAIT and waits < MAX_WAITS:
                waits += 1
                flow = link.flow_control()
            t.require(flow is not None and flow.status == FC_CONTINUE,
                      "its flow status is ContinueToSend" + (f" (after {waits} WAIT)" if waits else ""),
                      flow.text() if flow else "no ContinueToSend after the WAITs")
            t.check(valid_st_min(flow.data[2]), f"its STmin ({flow.data[2]:02X}) is not a reserved value",
                    flow.text())
            t.log(f"block size {flow.data[1]} ({'no limit' if not flow.data[1] else 'frames per block'}), "
                  f"STmin {flow.data[2]:02X}; frames of {flow.dlc} bytes")
            why = link.send_consecutive(consecutive, flow)
            t.require(not why, "the consecutive frames are taken, block by block", why)
            answer = link.answer(payload[0])
            t.check(answer is not None, "the whole request is answered",
                    hex_text(answer[:16]) + (" ..." if answer and len(answer) > 16 else "") if answer else "no answer")

    def wrong_sequence(self, t):
        link = self._link()
        with link.exchange():
            _flow, consecutive = self._start(t, link, self.request)
            wrong = bytes([0x20 | ((consecutive[0][0] + 1) & 0x0F)]) + consecutive[0][1:]
            link.send(wrong)
            self._silent(t, link, f"a consecutive frame numbered {wrong[0] & 0x0F} instead of "
                                  f"{consecutive[0][0] & 0x0F} ends the request: it is not answered")
        self._answers_again(t)

    def late(self, t):
        link = self._link()
        with link.exchange():
            _flow, consecutive = self._start(t, link, self.request)
            t.log(f"the consecutive frames held back {LATE:.2f} s (N_Cr {N_CR * 1000:.0f} ms)")
            t.wait(LATE)
            for body in consecutive:
                link.send(body)
            self._silent(t, link, "consecutive frames after N_Cr are not taken: the request is not answered")
        self._answers_again(t)

    def interrupted(self, t):
        link = self._link()
        short = self.short
        with link.exchange():
            _flow, consecutive = self._start(t, link, self.request)
            link.send(bytes([len(short)]) + short)
            answer = link.answer(short[0])
            t.check(answer is not None and answer[0] == short[0] + 0x40,
                    f"a single frame ({hex_text(short)}) in the middle of a segmented request is taken",
                    hex_text(answer) if answer else "no answer")
            for body in consecutive:
                link.send(body)
            self._silent(t, link, "the rest of the dropped request is ignored: it is not answered")

    def ignored(self, t):
        link = self._link()
        room = link.room
        cases = [(bytes([0x21]) + bytes(room), True, "a consecutive frame out of the blue"),
                 (flow_control_frame(0, 0), True, "a flow control out of the blue"),
                 (bytes([0x00]) + self.short, True, "a single frame of length 0"),
                 (bytes([room]) + self.short, False, f"a single frame of length {room} in a frame of "
                                                     f"{len(self.short) + 1 + (link.address_byte is not None)} bytes"),
                 (bytes([0x10, room]) + self.short.ljust(room - 1, b"\x00"), True,
                  f"a first frame of {room} bytes (a single frame carries that)")]
        with link.exchange():
            for body, pad, what in cases:
                link.drain()
                link.send(body, pad=pad)
                self._silent(t, link, f"{what} ({hex_text(body)}) is ignored")
            if self.s.o.functional and link.functional_id is not None:
                link.drain()
                first, _ = link.segments(self.request)
                link.send(first, functional=True)
                self._silent(t, link, "a first frame functionally addressed is ignored: no flow control")
        self._answers_again(t)

    def overflow(self, t):
        link = self._link()
        body = bytes([0x10 | LONGEST >> 8, LONGEST & 0xFF]) + self.short.ljust(link.room - 1, b"\x00")
        with link.exchange():
            link.drain()
            link.send(body)
            flow = link.flow_control()
            t.require(flow is not None, f"a first frame of {LONGEST} bytes is answered by a flow control",
                      flow.text() if flow else "none came")
            t.check(flow.status in FLOW_STATUS, "ContinueToSend when the ECU takes that much, else Overflow",
                    f"{flow.text()}: {FLOW_STATUS.get(flow.status, 'a reserved flow status')}")
            if flow.status != FC_OVERFLOW:
                t.log(f"the ECU takes {LONGEST} bytes; the request is left unfinished (N_Cr)")
                t.wait(LATE)
        self._answers_again(t)

    # --- the ECU sending -----------------------------------------------------------------------------------

    def block_size(self, t):
        link = self._link()
        with link.exchange():
            first, total, data = self._ask_long(t, link)
            t.log(f"its frames: {first.dlc} bytes")
            number, kept = 1, True
            link.send(flow_control_frame(1, 0))
            while len(data) < total:
                frame = self._consecutive(t, link, data, total, number)
                t.require(frame is not None, f"consecutive frame {number} comes", frame.text() if frame else "none came")
                number += 1
                if len(data) >= total:
                    break
                extra = link.receive(BLOCK_PAUSE)
                if extra is not None:
                    kept = False
                    t.check(False, "one consecutive frame per block of 1: then it waits for the next flow control",
                            f"{extra.text()} came without one")
                    break
                link.send(flow_control_frame(1, 0))
            if kept:
                t.check(True, "one consecutive frame per block of 1: then it waits for the next flow control",
                        f"{number - 1} blocks")
                self._whole(t, data, total)

    def st_min(self, t):
        link = self._link()
        with link.exchange():
            _first, total, data = self._ask_long(t, link)
            link.send(flow_control_frame(0, ST_MIN))
            frames = []
            while len(data) < total:
                frame = self._consecutive(t, link, data, total, len(frames) + 1)
                t.require(frame is not None, f"consecutive frame {len(frames) + 1} comes",
                          frame.text() if frame else "none came")
                frames.append(frame)
            self._whole(t, data, total)
            if len(frames) < 2:
                t.skip("one consecutive frame: no time between them to measure")
            gaps = [gap(earlier, later) for earlier, later in zip(frames, frames[1:])]
            least = st_min_seconds(ST_MIN)
            t.check(min(gaps) >= least * 0.9 - 0.001, f"at least STmin ({least * 1000:.0f} ms) between them",
                    f"{len(gaps)} gaps, the shortest {min(gaps) * 1000:.1f} ms")

    def wait(self, t):
        link = self._link()
        with link.exchange():
            _first, total, data = self._ask_long(t, link)
            link.send(flow_control_frame(0, 0, FC_WAIT))
            early = link.receive(WAIT_HOLD)
            t.check(early is None, f"after a WAIT the ECU sends nothing ({WAIT_HOLD * 1000:.0f} ms)",
                    early.text() if early else "nothing came")
            link.send(flow_control_frame(0, 0))
            number = 1
            while len(data) < total:
                frame = self._consecutive(t, link, data, total, number)
                if frame is None:
                    break
                number += 1
            self._whole(t, data, total)

    def _stopped(self, t, flow, what):
        link = self._link()
        with link.exchange():
            self._ask_long(t, link)
            link.send(flow)
            frame = link.receive(self._quiet())
            t.check(frame is None, what, f"it sent {frame.text()}" if frame else "nothing came")
            t.wait(0.05)
        self._answers_again(t)

    def overflow_answer(self, t):
        self._stopped(t, flow_control_frame(0, 0, FC_OVERFLOW), "after an Overflow the ECU sends no consecutive "
                                                                "frame")

    def reserved_status(self, t):
        self._stopped(t, flow_control_frame(0, 0, 0x3), "after the reserved flow status 3 the ECU sends no "
                                                        "consecutive frame")

    def no_flow_control(self, t):
        link = self._link()
        with link.exchange():
            self._ask_long(t, link)
            frame = link.receive(LATE)
            t.check(frame is None or frame.kind != 0x2,
                    f"without a flow control the ECU sends no consecutive frame ({LATE:.2f} s)",
                    f"it sent {frame.text()}" if frame else "nothing came")
        self._answers_again(t)
