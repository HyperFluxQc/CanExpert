"""
The J1939 transport protocol (J1939-21) for messages of 9 to 1785 bytes, and requests.

A multi-packet message starts with a connection management frame (TP.CM, PGN 0xEC00) naming its size, its
number of packets and its PGN, and travels in data transfer frames (TP.DT, PGN 0xEB00) of seven bytes after
a sequence number. To everyone it is a Broadcast Announce Message (BAM): the packets follow 50 to 200 ms
apart. To one node it is a session: Request To Send, the receiver's Clear To Send for some packets, those
packets, and the receiver's End of Message Acknowledgment.

J1939Assembler watches frames and puts the messages back together, the transport ones as one message (the
Trace, the J1939 window). J1939Link takes part: it sends a message the way its size and destination call for,
and requests a PGN, answering an RTS with CTS as a receiver must.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import can

from canexpert.j1939.pgn import (GLOBAL, PGN_ACKNOWLEDGMENT, PGN_REQUEST, PGN_TP_CM, PGN_TP_DT, TOOL_ADDRESS,
                                 make_id, parse_id, pgn_name)

RTS, CTS, END_OF_MESSAGE_ACK, BAM, ABORT = 16, 17, 19, 32, 255
MAX_LENGTH = 1785                # 255 packets of 7 bytes
BAM_INTERVAL = 0.05              # between the packets of a BAM (50-200 ms)
T1, T2, T3 = 0.75, 1.25, 1.25    # the receiver's wait for a packet, for packets after a CTS; the sender's for a CTS
REQUEST_TIMEOUT = 1.25           # the wait for the answer to a request (J1939-21: 1.25 s)
ACK, NACK, ACCESS_DENIED, BUSY = 0, 1, 2, 3


class J1939Error(Exception):
    """A transport session that failed: no CTS, an abort, packets missing."""


@dataclass
class J1939Message:
    timestamp: float
    pgn: int
    source: int
    destination: int
    data: bytes
    priority: int = 6
    transport: str = ""          # "", "BAM" or "RTS/CTS"
    complete: bool = True
    frames: list = field(default_factory=list)   # (timestamp, what, data) of a transport message
    acknowledgment: int | None = None            # the control byte of an Acknowledgment answering a request
    direction: str = ""                          # RX or TX, where the frames came from the measurement

    @property
    def name(self) -> str:
        return pgn_name(self.pgn)

    def summary(self) -> str:
        what = f"{self.name} (PGN {self.pgn})" if self.name else f"PGN {self.pgn}"
        destination = "Global" if self.destination == GLOBAL else f"{self.destination:02X}"
        how = f", {self.transport}" if self.transport else ""
        unfinished = ", unfinished" if not self.complete else ""
        return f"{what} {self.source:02X} → {destination}{how}, {len(self.data)} bytes{unfinished}"


def cm_frame(control: int, pgn: int, size: int = 0, packets: int = 0, extra: int = 0xFF) -> bytes:
    """A TP.CM frame: control byte, then what that control carries, then the PGN (little-endian)."""
    if control in (RTS, BAM):
        head = bytes([control, size & 0xFF, size >> 8, packets, extra])
    elif control == CTS:
        head = bytes([CTS, size, packets, 0xFF, 0xFF])          # size: packets to send; packets: next one
    elif control == END_OF_MESSAGE_ACK:
        head = bytes([END_OF_MESSAGE_ACK, size & 0xFF, size >> 8, packets, 0xFF])
    else:
        head = bytes([ABORT, extra, 0xFF, 0xFF, 0xFF])          # extra: the reason
    return head + pgn.to_bytes(3, "little")


def packets_of(data: bytes) -> list[bytes]:
    """The TP.DT frames of a message: sequence number and seven bytes, the last filled with 0xFF."""
    return [bytes([number + 1]) + data[number * 7:number * 7 + 7].ljust(7, b"\xff")
            for number in range((len(data) + 6) // 7)]


class J1939Assembler:
    """Frames in, J1939 messages out: a single frame is a message, a transport session becomes one message
    when its last packet arrives (or an unfinished one, when it is aborted or its packets stop coming)."""

    def __init__(self):
        self.sessions = {}          # (source, destination) -> the session being received

    def push(self, timestamp: float, can_id: int, data: bytes, extended: bool = True) -> list[J1939Message]:
        if not extended:
            return []
        data = bytes(data)
        j = parse_id(can_id)
        finished = self._expire(timestamp)
        if j.pgn == PGN_TP_CM and len(data) >= 8:
            finished += self._connection(timestamp, j, data)
        elif j.pgn == PGN_TP_DT and data:
            finished += self._packet(timestamp, j, data)
        else:
            finished.append(J1939Message(timestamp, j.pgn, j.source, j.destination, data, j.priority))
        return finished

    def _connection(self, timestamp, j, data):
        control, pgn = data[0], int.from_bytes(data[5:8], "little")
        finished = []
        if control in (RTS, BAM):
            key = (j.source, j.destination)
            if key in self.sessions:                            # a new session replaces an unfinished one
                finished.append(self._unfinished(self.sessions.pop(key)))
            self.sessions[key] = {
                "message": J1939Message(timestamp, pgn, j.source, j.destination, b"", j.priority,
                                        "BAM" if control == BAM else "RTS/CTS", False,
                                        [(timestamp, "TP.CM BAM" if control == BAM else "TP.CM RTS", data)]),
                "size": data[1] | (data[2] << 8), "packets": data[3], "next": 1, "buffer": bytearray(),
                "last": timestamp}
        elif control in (CTS, END_OF_MESSAGE_ACK, ABORT):
            # From the receiver of a session: its key is the other way round. An abort may come from either.
            session = self.sessions.get((j.destination, j.source)) or \
                (self.sessions.get((j.source, j.destination)) if control == ABORT else None)
            if session is not None:
                label = {CTS: "TP.CM CTS", END_OF_MESSAGE_ACK: "TP.CM EndOfMsgAck", ABORT: "TP.CM Abort"}[control]
                session["message"].frames.append((timestamp, label, data))
                session["last"] = timestamp
                if control == ABORT:
                    key = next(key for key, value in self.sessions.items() if value is session)
                    finished.append(self._unfinished(self.sessions.pop(key)))
        return finished

    def _packet(self, timestamp, j, data):
        key = (j.source, j.destination)
        session = self.sessions.get(key)
        if session is None:
            return []
        message = session["message"]
        message.frames.append((timestamp, f"TP.DT {data[0]}", data))
        session["last"] = timestamp
        if data[0] != session["next"]:                          # a packet lost: the message is too
            return [self._unfinished(self.sessions.pop(key))]
        session["buffer"] += data[1:8]
        session["next"] += 1
        if len(session["buffer"]) >= session["size"] or session["next"] > session["packets"]:
            self.sessions.pop(key)
            message.data = bytes(session["buffer"][:session["size"]])
            message.complete = len(session["buffer"]) >= session["size"]
            return [message]
        return []

    def _expire(self, now):
        """Sessions whose packets stopped coming (T1/T2 passed) end unfinished."""
        stale = [key for key, session in self.sessions.items() if now - session["last"] > T2]
        return [self._unfinished(self.sessions.pop(key)) for key in stale]

    def finish(self) -> list[J1939Message]:
        """The sessions still open, as unfinished messages (the end of a recording)."""
        open_sessions, self.sessions = list(self.sessions.values()), {}
        return [self._unfinished(session) for session in open_sessions]

    @staticmethod
    def _unfinished(session):
        message = session["message"]
        message.data, message.complete = bytes(session["buffer"]), False
        return message


class J1939Link:
    """A J1939 node on a bus facade with send(can.Message) and recv(timeout) - the session's mailbox, or the
    Dummy ECU's bus: sends messages of any size and requests PGNs, from address."""

    def __init__(self, bus, address: int = TOOL_ADDRESS, priority: int = 6):
        self.bus, self.address, self.priority = bus, address, priority

    def _send_frame(self, pgn, data, destination=GLOBAL, priority=None):
        can_id = make_id(pgn, self.address, destination, self.priority if priority is None else priority)
        self.bus.send(can.Message(arbitration_id=can_id, data=bytes(data), is_extended_id=True))

    def send(self, pgn: int, data, destination: int = GLOBAL, priority: int | None = None):
        """Send a message: up to eight bytes in one frame, more in a BAM to everyone or an RTS/CTS session
        with the destination."""
        data = bytes(data)
        if len(data) > MAX_LENGTH:
            raise J1939Error(f"{len(data)} bytes: J1939 carries {MAX_LENGTH} at most")
        if len(data) <= 8:
            self._send_frame(pgn, data, destination, priority)
        elif destination == GLOBAL:
            self._send_bam(pgn, data)
        else:
            self._send_session(pgn, data, destination)

    def _send_bam(self, pgn, data):
        packets = packets_of(data)
        self._send_frame(PGN_TP_CM, cm_frame(BAM, pgn, len(data), len(packets)), GLOBAL, 7)
        for packet in packets:
            time.sleep(BAM_INTERVAL)
            self._send_frame(PGN_TP_DT, packet, GLOBAL, 7)

    def _send_session(self, pgn, data, destination):
        packets = packets_of(data)
        self._send_frame(PGN_TP_CM, cm_frame(RTS, pgn, len(data), len(packets)), destination, 7)
        while True:
            answer = self._wait_cm(destination, pgn, T3)
            if answer is None:
                raise J1939Error(f"no CTS from {destination:02X} for PGN {pgn}")
            control = answer[0]
            if control == END_OF_MESSAGE_ACK:
                return
            if control == ABORT:
                raise J1939Error(f"{destination:02X} aborted the session (reason {answer[1]})")
            count, first = answer[1], answer[2]
            if count == 0:                                      # hold on: wait for the next CTS
                continue
            for packet in packets[first - 1:first - 1 + count]:
                self._send_frame(PGN_TP_DT, packet, destination, 7)
            if first - 1 + count >= len(packets):
                answer = self._wait_cm(destination, pgn, T3)
                if answer is not None and answer[0] == ABORT:
                    raise J1939Error(f"{destination:02X} aborted the session (reason {answer[1]})")
                return                                          # EndOfMsgAck, or none: the packets are out

    def _wait_cm(self, source, pgn, timeout):
        """The next TP.CM frame of source to this node about pgn, or None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self.bus.recv(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            if message is None or not message.is_extended_id:
                continue
            j = parse_id(message.arbitration_id)
            data = bytes(message.data)
            if j.pgn == PGN_TP_CM and j.source == source and j.destination == self.address and len(data) >= 8 \
                    and int.from_bytes(data[5:8], "little") == pgn:
                return data
        return None

    def send_request(self, pgn: int, destination: int = GLOBAL):
        """A Request for pgn, without waiting: the answers come as frames (e.g. every node's Address Claimed)."""
        self._send_frame(PGN_REQUEST, pgn.to_bytes(3, "little"), destination)

    def request(self, pgn: int, destination: int = GLOBAL, timeout: float = REQUEST_TIMEOUT) -> J1939Message | None:
        """Request a PGN (PGN 0xEA00) and return the answer: the message itself - in one frame, a BAM or an
        RTS/CTS session this node takes part in - or, when the node refuses, a message whose acknowledgment
        says how (NACK, access denied, busy). None when nothing answers in time. To everyone (GLOBAL), the
        first answer is returned."""
        clear = getattr(self.bus, "clear", None)
        if callable(clear):
            clear()                                             # only what answers this request
        self.send_request(pgn, destination)
        deadline = time.monotonic() + timeout
        session = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            message = self.bus.recv(timeout=min(0.05, remaining))
            if message is None or not message.is_extended_id:
                continue
            j = parse_id(message.arbitration_id)
            data = bytes(message.data)
            if destination != GLOBAL and j.source != destination:
                continue
            if j.destination not in (GLOBAL, self.address):
                continue
            now = message.timestamp or time.time()
            if j.pgn == pgn:
                return J1939Message(now, pgn, j.source, j.destination, data, j.priority)
            if j.pgn == PGN_ACKNOWLEDGMENT and len(data) >= 8 and int.from_bytes(data[5:8], "little") == pgn \
                    and data[4] in (self.address, GLOBAL):
                return J1939Message(now, pgn, j.source, j.destination, b"", j.priority, acknowledgment=data[0])
            if j.pgn == PGN_TP_CM and len(data) >= 8 and int.from_bytes(data[5:8], "little") == pgn:
                if data[0] in (BAM, RTS):
                    session = {"source": j.source, "size": data[1] | (data[2] << 8), "packets": data[3],
                               "buffer": bytearray(), "next": 1, "how": "BAM" if data[0] == BAM else "RTS/CTS",
                               "priority": j.priority, "destination": j.destination}
                    if data[0] == RTS:
                        self._send_frame(PGN_TP_CM, cm_frame(CTS, pgn, data[3], 1), j.source, 7)
                    deadline = time.monotonic() + T2
                elif data[0] == ABORT and session is not None and session["source"] == j.source:
                    return None
            elif j.pgn == PGN_TP_DT and session is not None and j.source == session["source"] and data:
                if data[0] != session["next"]:
                    if session["how"] == "RTS/CTS":
                        self._send_frame(PGN_TP_CM, cm_frame(ABORT, pgn, extra=3), j.source, 7)
                    return None
                session["buffer"] += data[1:8]
                session["next"] += 1
                deadline = time.monotonic() + T1
                if len(session["buffer"]) >= session["size"]:
                    if session["how"] == "RTS/CTS":
                        self._send_frame(PGN_TP_CM, cm_frame(END_OF_MESSAGE_ACK, pgn, session["size"],
                                                             session["packets"]), j.source, 7)
                    return J1939Message(now, pgn, session["source"], session["destination"],
                                        bytes(session["buffer"][:session["size"]]), session["priority"],
                                        session["how"])

    def acknowledge(self, pgn: int, requester: int, control: int = NACK):
        """Answer a request with an Acknowledgment (J1939-21): ACK, NACK, access denied or busy, to everyone
        with the requester's address in it."""
        self._send_frame(PGN_ACKNOWLEDGMENT, bytes([control, 0xFF, 0xFF, 0xFF, requester])
                         + pgn.to_bytes(3, "little"), GLOBAL)
