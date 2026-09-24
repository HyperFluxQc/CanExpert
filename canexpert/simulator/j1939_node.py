"""
The Dummy ECU as a J1939 node (EcuConfig.j1939): it claims its address with its NAME - and gives it up to a
node with a lower NAME - sends DM1 every second with the active faults of its fault memory, puts its address
into the 29-bit application frames of its DBC (DBC/j1939_demo.dbc is one), and answers requests: Address
Claimed, DM1, DM2 (previously active), DM11 and DM3 (clear), SOFT (its software version), VI (its VIN), CI,
and the PGNs it broadcasts. What it does not have gets a NACK when it was asked alone.

A BAM goes out over several ticks, so the diagnostic answers keep their timing; an RTS/CTS session (an answer
to one tester) waits for the tester's CTS.
"""
from __future__ import annotations

import time

import can

from canexpert.j1939.dm import J1939Dtc, build_dm
from canexpert.j1939.name import Name
from canexpert.j1939.pgn import (GLOBAL, NULL_ADDRESS, PGN_ADDRESS_CLAIMED, PGN_CI, PGN_DM1, PGN_DM2, PGN_DM3,
                                 PGN_DM11, PGN_REQUEST, PGN_SOFT, PGN_TP_CM, PGN_TP_DT, PGN_VI, make_id, parse_id)
from canexpert.j1939.transport import ACK, BAM, BAM_INTERVAL, NACK, J1939Error, J1939Link, cm_frame, packets_of
from canexpert.simulator.dtc import CONFIRMED, FAILED_SINCE_CLEAR, TEST_FAILED, WARNING_INDICATOR

DM1_INTERVAL = 1.0
# The default fault memory's DTCs as J1939 reports them: (SPN, FMI). Others: the DTC's upper bytes and low bits.
J1939_DTCS = {0x010100: (132, 2),          # P0101 mass air flow: erratic
              0xC10000: (639, 9)}          # U0100 lost communication: J1939 network #1, abnormal update rate
DEFAULT_NAME = Name(identity=0x0CE01, manufacturer=0x7FF, function=0, industry_group=0,
                    arbitrary_address_capable=1).to_int()


def spn_fmi(dtc: int) -> tuple[int, int]:
    return J1939_DTCS.get(dtc) or ((dtc >> 8) & 0x7FFFF, dtc & 0x1F)


class J1939Node:
    def __init__(self, ecu):
        self.ecu = ecu
        self.address = None               # claimed address; None until claimed, NULL_ADDRESS once lost
        self._claimed = None              # (address, NAME) it claimed with: other settings claim again
        self.last_frames = {}             # PGN -> data of the application frame last sent
        self._next_dm1 = 0.0
        self._outbox = []                 # (when, can.Message-like tuple) of the BAM packets still to go

    @property
    def config(self):
        return self.ecu.config

    def link(self) -> J1939Link:
        address = self.address if self.address is not None else self.config.j1939_address
        return J1939Link(self.ecu._io, address)

    def reset(self):
        """Power-on: the address is claimed again."""
        self.address = self._claimed = None
        self.last_frames.clear()
        self._outbox.clear()

    # --- what it does by itself ----------------------------------------------------------------------

    def tick(self, now: float):
        if not self.config.j1939:
            self.address = None
            return
        if self._claimed != (self.config.j1939_address & 0xFF, self.config.j1939_name):
            self.address = None                             # new settings: a new claim
        if self.address is None:
            self.claim()
        self._send_due(now)
        if self.address != NULL_ADDRESS and now >= self._next_dm1 and self.ecu.application_running(now):
            self._next_dm1 = now + DM1_INTERVAL
            self.send(PGN_DM1, self.dm(active=True), priority=6)

    def claim(self):
        """Address Claimed: this address, with this NAME - or, having lost the address, the NAME from the null
        address (Cannot Claim Address)."""
        if self.address is None:
            self.address = self.config.j1939_address & 0xFF
            self._claimed = (self.address, self.config.j1939_name)
            self.ecu.log(f"J1939: claims address {self.address:02X}")
        self.link().send(PGN_ADDRESS_CLAIMED, self.config.j1939_name.to_bytes(8, "little"), GLOBAL, priority=6)

    def application_frame(self, message):
        """A 29-bit application frame goes out from this node's address, and its data answers a request for
        its PGN; a node that lost its address sends none (None)."""
        if not message.is_extended_id:
            return message
        if self.address in (None, NULL_ADDRESS):
            return None
        message.arbitration_id = (message.arbitration_id & ~0xFF) | self.address
        self.last_frames[parse_id(message.arbitration_id).pgn] = bytes(message.data)
        return message

    # --- sending -----------------------------------------------------------------------------------------

    def send(self, pgn, data, destination=GLOBAL, priority=6):
        """A message from this node: one frame, a BAM spread over the next ticks, or an RTS/CTS session."""
        data = bytes(data)
        link = self.link()
        if len(data) <= 8:
            link.send(pgn, data, destination, priority)
        elif destination == GLOBAL:
            now = time.monotonic()
            packets = packets_of(data)
            self._outbox.append((now, make_id(PGN_TP_CM, link.address, GLOBAL, 7), cm_frame(BAM, pgn, len(data),
                                                                                           len(packets))))
            for number, packet in enumerate(packets, 1):
                self._outbox.append((now + number * BAM_INTERVAL, make_id(PGN_TP_DT, link.address, GLOBAL, 7), packet))
        else:
            try:
                link.send(pgn, data, destination, priority)
            except J1939Error as exc:
                self.ecu.log(f"J1939: {exc}")

    def _send_due(self, now):
        while self._outbox and self._outbox[0][0] <= now:
            _when, can_id, data = self._outbox.pop(0)
            self.ecu._io.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=True))

    # --- what it hears ------------------------------------------------------------------------------------------

    def on_frame(self, message) -> bool:
        """A 29-bit frame for J1939; True when it was one this node acts on."""
        if not self.config.j1939 or self.address is None:
            return False
        j = parse_id(message.arbitration_id)
        data = bytes(message.data)
        if j.pgn == PGN_REQUEST and len(data) >= 3 and j.destination in (GLOBAL, self.address):
            self.answer(int.from_bytes(data[:3], "little"), j.source, j.destination == GLOBAL)
            return True
        if j.pgn == PGN_ADDRESS_CLAIMED and j.source == self.address and len(data) >= 8:
            other = int.from_bytes(data[:8], "little")
            if other == self.config.j1939_name:
                return True
            if other < self.config.j1939_name:            # the lower NAME keeps the address
                self.ecu.log(f"J1939: lost address {self.address:02X} to NAME {other:016X}")
                self.address = NULL_ADDRESS
            self.claim()
            return True
        return False

    def answer(self, pgn, requester, to_everyone):
        if pgn == PGN_ADDRESS_CLAIMED:
            self.claim()
            return
        if self.address == NULL_ADDRESS:
            return
        destination = GLOBAL if to_everyone else requester
        link = self.link()
        if pgn in (PGN_DM11, PGN_DM3):
            self.clear(active=pgn == PGN_DM11)
            if not to_everyone:
                link.acknowledge(pgn, requester, ACK)
            return
        data = self.data_for(pgn)
        if data is None:
            if not to_everyone:                            # a global request is not answered with a NACK
                link.acknowledge(pgn, requester, NACK)
            return
        self.ecu.log(f"J1939: PGN {pgn} to {requester:02X}")
        self.send(pgn, data, destination)

    def data_for(self, pgn) -> bytes | None:
        ecu = self.ecu
        if pgn == PGN_DM1:
            return self.dm(active=True)
        if pgn == PGN_DM2:
            return self.dm(active=False)
        if pgn == PGN_SOFT:                               # number of fields, then each ending with *
            return b"\x01" + bytes(ecu.dids.get(0xF195, b"")).rstrip(b"\x00") + b"*"
        if pgn == PGN_VI:
            return bytes(ecu.dids.get(0xF190, b"")).rstrip(b"\x00") + b"*"
        if pgn == PGN_CI:                                  # make*model*serial number*unit number*
            serial = bytes(ecu.dids.get(0xF18C, b"")).rstrip(b"\x00")
            return b"CANEXPERT*DUMMY ECU*" + serial + b"*1*"
        return self.last_frames.get(pgn)

    # --- the fault memory ----------------------------------------------------------------------------------------

    def dtcs(self, active: bool) -> list[tuple[int, int]]:
        """(DTC, status) of the active faults, or of the previously active ones (confirmed or failed since the
        last clear, not failing now)."""
        memory = self.ecu.dtc_memory
        with memory.lock:
            statuses = dict(memory.statuses)
        if active:
            return [(dtc, status) for dtc, status in statuses.items() if status & TEST_FAILED]
        return [(dtc, status) for dtc, status in statuses.items()
                if not status & TEST_FAILED and status & (CONFIRMED | FAILED_SINCE_CLEAR)]

    def dm(self, active: bool) -> bytes:
        """DM1 or DM2: the amber lamp on while a fault is active, the malfunction indicator while one asks for
        the warning indicator."""
        memory = self.ecu.dtc_memory
        entries = []
        for dtc, _status in self.dtcs(active):
            spn, fmi = spn_fmi(dtc)
            extended = memory.records.get(dtc, (b"", b""))[1]
            entries.append(J1939Dtc(spn, fmi, extended[0] if extended else 1))
        now_active = self.dtcs(True)
        lamps = (int(any(status & WARNING_INDICATOR for _dtc, status in now_active)), 0, int(bool(now_active)), 0)
        return build_dm(entries, lamps)

    def clear(self, active: bool):
        """DM11 clears the active DTCs (and with them every other), DM3 the previously active ones."""
        memory = self.ecu.dtc_memory
        if active:
            memory.clear()
            self.ecu.log("J1939: DM11, the fault memory is cleared")
            return
        for dtc, _status in self.dtcs(False):
            memory.clear(dtc)
        self.ecu.log("J1939: DM3, the previously active DTCs are cleared")
