"""
The services a description only lets TestExpert see answer, taken further: a download out of order, while
locked or of a format the ECU does not take - and, where the plan names one, a download started and refused
again; memory read, and written back, where the plan names it; periodic data sent and stopped; a
ResponseOnEvent that fires on a DID that changes; CommunicationControl stopping the ECU's own frames; the DIDs
the description gives InputOutputControl, controlled and given back; the DTCs the ECU supports and reports
against the description's.
"""
from __future__ import annotations

import time
from collections import Counter

from canexpert.odx_services import dtc_display
from canexpert.test_expert.transport import Link

TRANSFER, MEMORY, PERIODIC, EVENTS, COMMUNICATION = ("Download and upload", "Memory by address", "Periodic data",
                                                     "ResponseOnEvent", "Communication")
IO_CONTROL, FAULT_MEMORY = "Input/output control", "Fault memory"
IO_DIDS = 4                         # the IO DIDs tested, at most
RETURN_CONTROL, SHORT_TERM = 0x00, 0x03     # inputOutputControlParameters
ADDRESS_FORMAT = 0x44               # addressAndLengthFormatIdentifier: a 4-byte address, a 4-byte size
PROBE_RANGE = (0x00000000, 0x10)    # where a request that must be refused before its address is looked at points
PERIODIC_FAST = 0x03                # transmissionMode sendAtFastRate
PERIODIC_STOP = 0x04                # stopSending
PERIODIC_WATCH = 1.0                # seconds of periodic data watched
PERIODIC_QUIET = 0.5                # after stopSending, no more of it for this long
EVENT_WINDOW = 0x02                 # eventWindowTime: infinite
EVENT_WAIT = 3.0                    # how long an event's response is waited for
CHANGE_WAIT = 1.1                   # between two reads telling whether a DID changes by itself (a seconds count too)
CHANGE_CANDIDATES = 8
LISTEN = 0.6                        # seconds the ECU's own frames are counted
AFTER_CONTROL = 0.15                # a frame already on its way when CommunicationControl was answered


def parse_memory_range(text: str) -> tuple[int, int] | None:
    """"10000:300" -> (0x10000, 0x300): an address and a size in hex; "" -> None. ValueError naming what is
    wrong."""
    text = str(text or "").strip()
    if not text:
        return None
    address, _, size = text.partition(":")
    try:
        values = int(address.strip().lower().removeprefix("0x"), 16), int(size.strip().lower().removeprefix("0x"), 16)
    except ValueError:
        raise ValueError(f"a memory range is an address and a size in hex: 10000:300, not {text!r}") from None
    if not 0 <= values[0] <= 0xFFFFFFFF or not 0 < values[1] <= 0xFFFFFFFF:
        raise ValueError(f"an address and a size of 4 bytes at most, the size above 0: {text!r}")
    return values


def memory_request(sid: int, address: int, size: int, *before) -> bytes:
    """sid, what comes before the format (RequestDownload's dataFormatIdentifier), the format 44, the address
    and the size."""
    return bytes([sid, *before, ADDRESS_FORMAT]) + address.to_bytes(4, "big") + size.to_bytes(4, "big")


def _hex(data) -> str:
    return bytes(data).hex(" ").upper()


class ServiceTests:
    """The groups of a Suite that take services further than their availability."""

    def __init__(self, suite):
        self.s = suite
        self.download = parse_memory_range(suite.o.download)
        self.memory = parse_memory_range(suite.o.memory)

    def _reachable(self, sid) -> bool:
        """Whether the description has the service in one of its sessions."""
        service = self.s.d.service(sid)
        return service is not None and self.s.first_session(service.access) is not None

    def add(self):
        d, add = self.s.d, self.s._add
        if self._reachable(0x34):
            if self._reachable(0x36) or self._reachable(0x37):
                add(TRANSFER, "TransferData and RequestTransferExit before RequestDownload", self.out_of_order,
                    "Without a RequestDownload, TransferData and RequestTransferExit get NRC 0x24.")
            if d.services[0x34].access.levels:
                add(TRANSFER, "RequestDownload while locked", self.download_locked,
                    "RequestDownload needs its security level: NRC 0x33 while locked.")
            add(TRANSFER, "RequestDownload of a format the ECU does not take", self.download_format,
                "A compression method 0xFF, and an address and size of no bytes, get NRC 0x31.")
            if self.download is not None and self.s.o.destructive:
                add(TRANSFER, "A download started, then refused out of order", self.download_started,
                    "The plan's RequestDownload is accepted with a maxNumberOfBlockLength; a second one gets 0x22, "
                    "TransferData with block counter 02 gets 0x73 and RequestTransferExit before the data 0x24. "
                    "Leaving the session ends it.")
        if self._reachable(0x23):
            add(MEMORY, "ReadMemoryByAddress of a format the ECU does not take", self.memory_format,
                "An address and size of no bytes get NRC 0x31.")
            if self.memory is not None:
                add(MEMORY, "ReadMemoryByAddress of the plan's memory", self.memory_read,
                    "The plan's memory is read: as many bytes as asked.")
        if self._reachable(0x3D) and self.memory is not None and self.s.o.destructive:
            if d.services[0x3D].access.levels:
                add(MEMORY, "WriteMemoryByAddress while locked", self.memory_locked,
                    "Writing the plan's memory while locked gets NRC 0x33 (its own bytes, should it be written).")
            if self._reachable(0x23):
                add(MEMORY, "WriteMemoryByAddress of the same bytes", self.memory_written,
                    "The plan's memory, read, written back and read again: the same bytes.")
        periodic = self._periodic_did()
        if self._reachable(0x2A) and periodic is not None:
            add(PERIODIC, f"Periodic data of {periodic:04X}", self.periodic,
                "2A 03 sends the DID's data periodically (6A and its periodic identifier), 2A 04 stops it; a "
                "periodic identifier that does not exist and a transmission mode that does not exist get 0x31.")
        roe = d.services.get(0x86)
        if self._reachable(0x86) and {0x03, 0x05}.issubset(roe.sub_functions or {0x03, 0x05}):
            add(EVENTS, "startResponseOnEvent without an event", self.events_sequence,
                "Starting ResponseOnEvent with no event set up gets NRC 0x24.")
            if 0x22 in d.services:
                add(EVENTS, "An event on a DID that changes", self.events_fire,
                    "onChangeOfDataIdentifier on a DID whose value changes by itself: once started, the ECU sends "
                    "the DID's ReadDataByIdentifier answer unasked; then stopped and cleared.")
        io = [did for did in sorted(d.dids) if d.dids[did].io is not None][:IO_DIDS]
        if self._reachable(0x2F) and io:
            plain = next((did for did in sorted(d.dids) if d.dids[did].io is None and d.dids[did].read is not None
                          and not d.dids[did].read.levels), None)
            if plain is not None:
                add(IO_CONTROL, f"IO control of {plain:04X}, which has none", lambda t, did=plain: self.io_none(t, did),
                    "InputOutputControlByIdentifier of a DID the description gives none gets NRC 0x31.")
            for did in io:
                add(IO_CONTROL, f"IO control of {did:04X} {d.dids[did].name}", lambda t, did=did: self.io(t, did),
                    "returnControlToECU (00) is answered 6F; with destructive tests, shortTermAdjustment (03) to its "
                    "value read first, then control given back.")
        faults = d.services.get(0x19)
        if self._reachable(0x19) and faults is not None and d.dtcs:
            add(FAULT_MEMORY, "The ECU's DTCs are the description's", self.dtcs,
                "The DTCs the ECU supports (19 0A) are the ones the description lists, and every DTC it reports "
                "(19 02 FF) is one of them.")
        control = d.services.get(0x28)
        if self._reachable(0x28) and self._control_off(control) is not None:
            add(COMMUNICATION, "CommunicationControl stops the ECU's own frames", self.communication,
                "The frames the ECU sends besides diagnostics stop while its normal communication's transmission "
                "is disabled, and come back once enabled. Only this ECU should be on the bus.")

    # --- helpers ----------------------------------------------------------------------------------------------

    def _prepare(self, t, sid, unlock=True):
        """Into the first session the service is allowed in, unlocked when it needs a level (skipped without a
        key source)."""
        service = self.s.d.service(sid)
        session = self.s.first_session(service.access)
        if session is None:
            t.skip(f"the description allows {sid:02X} in none of its sessions")
        self.s.enter(t, session)
        if unlock and service.access.levels and not self.s.unlock(t, min(service.access.levels)):
            t.fail(f"not unlocked for {sid:02X}")
        return service

    def _periodic_did(self):
        """A DID of the periodic range (F200-F2FF) readable without security."""
        return next((did for did in sorted(self.s.d.dids) if 0xF200 <= did <= 0xF2FF
                     and self.s.d.dids[did].read is not None and not self.s.d.dids[did].read.levels), None)

    @staticmethod
    def _control_off(service):
        """CommunicationControl's sub-function that disables transmission: 01 enableRxAndDisableTx, else 03."""
        subs = service.sub_functions
        return next((sub for sub in (0x01, 0x03) if not subs or sub in subs), None)

    # --- download and upload -----------------------------------------------------------------------------------

    def out_of_order(self, t):
        self._prepare(t, 0x36 if self._reachable(0x36) else 0x37)
        if self._reachable(0x36):
            self.s.negative(t, b"\x36\x01\x00", "transfer_sequence", "36 01 before RequestDownload: NRC 0x24")
        if self._reachable(0x37):
            self.s.negative(t, b"\x37", "transfer_sequence", "37 before RequestDownload: NRC 0x24")

    def download_locked(self, t):
        self._prepare(t, 0x34, unlock=False)
        address, size = self.download or PROBE_RANGE
        request = memory_request(0x34, address, size, 0x00)
        self.s.negative(t, request, "locked", f"{_hex(request)} while locked: NRC 0x33")

    def download_format(self, t):
        self._prepare(t, 0x34)
        address, size = self.download or PROBE_RANGE
        compressed = memory_request(0x34, address, size, 0xFF)
        self.s.negative(t, compressed, "transfer_format", f"{_hex(compressed)}: compression method 0xFF, NRC 0x31")
        self.s.negative(t, b"\x34\x00\x00", "transfer_format", "34 00 00: an address and size of no bytes, NRC 0x31")

    def download_started(self, t):
        self._prepare(t, 0x34)
        address, size = self.download
        request = memory_request(0x34, address, size, 0x00)
        raw = self.s.positive(t, request, f"{_hex(request)} is accepted")
        if raw is None:
            return
        length = raw[1] >> 4 if len(raw) > 1 else 0
        well_formed = len(raw) == 2 + length and 1 <= length <= 4 and raw[1] & 0x0F == 0
        maximum = int.from_bytes(raw[2:2 + length], "big") if well_formed else 0
        t.check(well_formed and maximum >= 3, "74, the length of maxNumberOfBlockLength and a block length of 3 "
                                              "bytes at least", f"{_hex(raw)}: {maximum} bytes per TransferData")
        self.s.negative(t, request, "transfer_active", "a second RequestDownload while one runs: NRC 0x22")
        if 0x36 in self.s.d.services:
            self.s.negative(t, b"\x36\x02\x00", "transfer_counter", "36 02 as the first TransferData: NRC 0x73")
        if 0x37 in self.s.d.services:
            self.s.negative(t, b"\x37", "transfer_sequence", "37 before the data: NRC 0x24")
        self.s.positive(t, b"\x10\x01", "the default session ends the download (10 01)", echo=[0x01])

    # --- memory ------------------------------------------------------------------------------------------------

    def memory_format(self, t):
        self._prepare(t, 0x23)
        self.s.negative(t, b"\x23\x00", "memory_format", "23 00: an address and size of no bytes, NRC 0x31")

    def _read(self, t, what):
        address, size = self.memory
        request = memory_request(0x23, address, size)
        return self.s.positive(t, request, what, length=1 + size)

    def memory_read(self, t):
        self._prepare(t, 0x23)
        address, size = self.memory
        raw = self._read(t, f"23 44 {address:08X} {size:08X}: 63 and {size} bytes")
        if raw is not None:
            t.log(f"{_hex(raw[1:17])}{' ...' if size > 16 else ''}")

    def memory_locked(self, t):
        d = self.s.d
        address, size = self.memory
        data = bytes(size)
        if 0x23 in d.services:
            self._prepare(t, 0x23)
            raw = self._read(t, "the plan's memory is read first")
            if raw is not None:
                data = bytes(raw[1:])
            self.s._default()
        self._prepare(t, 0x3D, unlock=False)
        request = memory_request(0x3D, address, size) + data
        self.s.negative(t, request, "locked", f"3D 44 {address:08X} {size:08X} while locked: NRC 0x33")

    def memory_written(self, t):
        self._prepare(t, 0x23)
        before = self._read(t, "the plan's memory is read")
        if before is None:
            return
        self.s._default()
        self._prepare(t, 0x3D)
        address, size = self.memory
        request = memory_request(0x3D, address, size) + bytes(before[1:])
        self.s.positive(t, request, "the same bytes are written back: 7D and the address and size",
                        echo=request[1:10])
        self.s._default()
        self._prepare(t, 0x23)
        after = self._read(t, "read again")
        if after is not None:
            t.check(after == before, "the same bytes", f"{_hex(after[1:9])}...")

    # --- periodic data -------------------------------------------------------------------------------------------

    def periodic(self, t):
        did = self._periodic_did()
        identifier = did & 0xFF
        self._prepare(t, 0x2A)
        link = Link(self.s.tester)
        raw = self.s.positive(t, bytes([0x2A, PERIODIC_FAST, identifier]), f"2A 03 {identifier:02X} is answered 6A")
        if raw is None:
            return
        with link.exchange():
            seen = self._watch(link, identifier, PERIODIC_WATCH)
        rate = f", every {PERIODIC_WATCH / len(seen) * 1000:.0f} ms" if seen else ""
        t.check(len(seen) >= 2, f"6A {identifier:02X} and the data come periodically",
                f"{len(seen)} in {PERIODIC_WATCH:g} s{rate}" + (f": {_hex(seen[0])}" if seen else ""))
        self.s.positive(t, bytes([0x2A, PERIODIC_STOP, identifier]), f"2A 04 {identifier:02X} is answered 6A")
        with link.exchange():
            time.sleep(0.05)
            link.drain()
            late = self._watch(link, identifier, PERIODIC_QUIET)
        t.check(not late, f"then no more of it ({PERIODIC_QUIET:g} s)", f"{len(late)} came")
        unused = next(value for value in range(0xFF, 0, -1) if 0xF200 | value not in self.s.d.dids)
        self.s.negative(t, bytes([0x2A, PERIODIC_FAST, unused]), "did_unknown",
                        f"2A 03 {unused:02X} (no DID F2{unused:02X}): NRC 0x31")
        self.s.negative(t, bytes([0x2A, 0x05, identifier]), "periodic_mode",
                        f"2A 05 {identifier:02X} (a transmission mode that does not exist): NRC 0x31")

    @staticmethod
    def _watch(link, identifier, seconds) -> list[bytes]:
        """The periodic messages of the identifier (6A frames on the response ID) within seconds."""
        seen, deadline = [], time.perf_counter() + seconds
        while True:
            frame = link.receive(deadline - time.perf_counter())
            if frame is None:
                return seen
            if frame.kind == 0x0 and frame.data[1:3] == bytes([0x6A, identifier]):
                seen.append(frame.data[1:1 + (frame.data[0] & 0x0F)])

    # --- ResponseOnEvent -----------------------------------------------------------------------------------------

    def events_sequence(self, t):
        self._prepare(t, 0x86)
        self.s.tester.ask(bytes([0x86, 0x06, EVENT_WINDOW]))            # no event left from before
        self.s.negative(t, bytes([0x86, 0x05, EVENT_WINDOW]), "roe_sequence",
                        "86 05 with no event set up: NRC 0x24")

    def _changing_did(self, t):
        """A DID readable here whose value changes by itself, or None."""
        candidates = [did for did in sorted(self.s.d.dids) if self.s.d.dids[did].read is not None
                      and not self.s.d.dids[did].read.levels and did not in (0xF186,)][:CHANGE_CANDIDATES]
        first = {}
        for did in candidates:
            answer = self.s.tester.ask(b"\x22" + did.to_bytes(2, "big"))
            if answer.positive():
                first[did] = answer.raw
        t.wait(CHANGE_WAIT)
        for did, raw in first.items():
            answer = self.s.tester.ask(b"\x22" + did.to_bytes(2, "big"))
            if answer.positive() and answer.raw != raw:
                return did
        return None

    def events_fire(self, t):
        self._prepare(t, 0x86)
        did = self._changing_did(t)
        if did is None:
            t.skip("no DID the description lets be read changes by itself")
        t.log(f"{did:04X} changes by itself")
        self.s.tester.ask(bytes([0x86, 0x06, EVENT_WINDOW]))
        record = did.to_bytes(2, "big")
        setup = bytes([0x86, 0x03, EVENT_WINDOW]) + record + b"\x22" + record
        if self.s.positive(t, setup, f"{_hex(setup)} (onChangeOfDataIdentifier) is accepted", echo=[0x03]) is None:
            return
        link = Link(self.s.tester)
        self.s.positive(t, bytes([0x86, 0x05, EVENT_WINDOW]), "86 05 (startResponseOnEvent) is accepted", echo=[0x05])
        with link.exchange():
            link.drain()
            answer = link.answer(0x22, EVENT_WAIT)
        t.check(answer is not None and answer[:3] == b"\x62" + record,
                f"the ECU sends 62 {did:04X} and its value unasked, within {EVENT_WAIT:g} s",
                _hex(answer[:12]) if answer else "nothing came")
        self.s.positive(t, bytes([0x86, 0x00, EVENT_WINDOW]), "86 00 (stopResponseOnEvent) is accepted", echo=[0x00])
        self.s.positive(t, bytes([0x86, 0x06, EVENT_WINDOW]), "86 06 (clearResponseOnEvent) is accepted", echo=[0x06])

    # --- InputOutputControl -----------------------------------------------------------------------------------------

    def _io_session(self, t, entry):
        """Into the first session the DID's IO control is allowed in, unlocked when it needs a level."""
        session = self.s.first_session(entry.io)
        if session is None:
            t.skip(f"the description allows IO control of {entry.did:04X} in none of its sessions")
        self.s.enter(t, session)
        if entry.io.levels and not self.s.unlock(t, min(entry.io.levels)):
            t.fail("not unlocked for IO control")

    def io_none(self, t, did):
        self._prepare(t, 0x2F)
        request = b"\x2f" + did.to_bytes(2, "big") + bytes([RETURN_CONTROL])
        self.s.negative(t, request, "io_unknown", f"{_hex(request)}: NRC 0x31")

    def io(self, t, did):
        entry = self.s.d.dids[did]
        self._io_session(t, entry)
        parameters = entry.io_parameters
        identifier = did.to_bytes(2, "big")
        if not parameters or RETURN_CONTROL in parameters:
            request = b"\x2f" + identifier + bytes([RETURN_CONTROL])
            self.s.positive(t, request, f"{_hex(request)} (returnControlToECU) is answered 6F",
                            echo=identifier + bytes([RETURN_CONTROL]))
        if not self.s.o.destructive or (parameters and SHORT_TERM not in parameters):
            return
        read = self.s.positive(t, b"\x22" + identifier, f"22 {did:04X}: its value first", echo=identifier) \
            if entry.read is not None and entry.read.allows(self.s.tester.session or 0) else None
        if read is None:
            t.log("its value could not be read: shortTermAdjustment is not tried")
            return
        request = b"\x2f" + identifier + bytes([SHORT_TERM]) + bytes(read[3:])
        self.s.positive(t, request, f"{_hex(request)} (shortTermAdjustment to that value) is answered 6F",
                        echo=identifier + bytes([SHORT_TERM]))
        back = b"\x2f" + identifier + bytes([RETURN_CONTROL])
        self.s.positive(t, back, f"{_hex(back)}: control given back", echo=identifier + bytes([RETURN_CONTROL]))

    # --- the DTCs ---------------------------------------------------------------------------------------------------

    def _described(self, code) -> bool:
        """A DTC the description lists - its three bytes, or its two without the failure type."""
        return code in self.s.d.dtcs or code >> 8 in self.s.d.dtcs

    @staticmethod
    def _codes(raw, start) -> list[int]:
        """The DTCs of a ReadDTCInformation answer: three bytes and a status byte each, from start."""
        body = raw[start:]
        return [int.from_bytes(body[index:index + 3], "big") for index in range(0, len(body) - 3, 4)]

    def dtcs(self, t):
        self._prepare(t, 0x19)
        subs = self.s.d.services[0x19].sub_functions
        described = self.s.d.dtcs
        if not subs or 0x0A in subs:
            raw = self.s.positive(t, b"\x19\x0a", "19 0A (reportSupportedDTC) is answered", echo=[0x0A])
            if raw is not None:
                supported = self._codes(raw, 3)
                t.log(f"{len(supported)} DTCs supported: " + ", ".join(dtc_display(code) for code in supported[:12])
                      + (" ..." if len(supported) > 12 else ""))
                extra = [code for code in supported if not self._described(code)]
                t.check(not extra, "every DTC the ECU supports is in the description",
                        "not described: " + ", ".join(dtc_display(code) for code in extra) if extra
                        else f"{len(supported)} DTCs")
                missing = [code for code in sorted(described) if not any(
                    code in (found, found >> 8) for found in supported)]
                t.check(not missing, "every DTC of the description is supported",
                        "not supported: " + ", ".join(f"{code:06X} {described[code]}" for code in missing) if missing
                        else f"{len(described)} DTCs")
        if not subs or 0x02 in subs:
            raw = self.s.positive(t, b"\x19\x02\xff", "19 02 FF (reportDTCByStatusMask) is answered", echo=[0x02])
            if raw is not None:
                reported = self._codes(raw, 3)
                extra = [code for code in reported if not self._described(code)]
                t.check(not extra, "every DTC it reports is in the description",
                        "not described: " + ", ".join(dtc_display(code) for code in extra) if extra
                        else f"{len(reported)} reported")

    # --- CommunicationControl -------------------------------------------------------------------------------------

    def _own_frames(self, link, seconds) -> Counter:
        """The frames besides diagnostics within seconds, counted by identifier."""
        skip = {link.request_id, link.response_id, link.functional_id}
        counts, deadline = Counter(), time.perf_counter() + seconds
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return counts
            message = link.bus.recv(min(remaining, 0.05))
            if message is not None and not message.is_error_frame and message.arbitration_id not in skip:
                counts[message.arbitration_id] += 1

    def communication(self, t):
        link = Link(self.s.tester)
        with link.exchange():
            link.drain()
            before = self._own_frames(link, LISTEN)
        periodic = sorted(can_id for can_id, count in before.items() if count >= 2)
        if not periodic:
            t.skip(f"the ECU sends no frames of its own ({LISTEN:g} s)")
        t.log(f"its frames: {', '.join(f'{can_id:X}' for can_id in periodic)}")
        service = self._prepare(t, 0x28)
        off = self._control_off(service)
        self.s.positive(t, bytes([0x28, off, 0x01]), f"28 {off:02X} 01 (transmission of normal communication "
                                                     f"disabled) is accepted", echo=[off])
        with link.exchange():
            time.sleep(AFTER_CONTROL)
            link.drain()
            during = self._own_frames(link, LISTEN)
        still = [can_id for can_id in periodic if during[can_id]]
        t.check(not still, "its frames stop", "still: " + ", ".join(f"{can_id:X}" for can_id in still) if still
                else f"none of {len(periodic)} in {LISTEN:g} s")
        self.s.positive(t, b"\x28\x00\x01", "28 00 01 (enableRxAndTx) is accepted", echo=[0x00])
        with link.exchange():
            time.sleep(AFTER_CONTROL)
            link.drain()
            after = self._own_frames(link, LISTEN)
        back = [can_id for can_id in periodic if after[can_id]]
        t.check(len(back) == len(periodic), "they come back",
                f"{len(back)} of {len(periodic)}" + (f"; not {', '.join(f'{c:X}' for c in periodic if c not in back)}"
                                                     if len(back) != len(periodic) else ""))
