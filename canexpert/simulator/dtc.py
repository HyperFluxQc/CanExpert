"""
The dummy ECU's fault memory: DTCs whose status bits follow their faults, as ISO 14229-1 Annex D has it.

A fault is switched on and off from the window (its "Fault" box). While it is present the DTC's test
fails: the DTC is pending at once, and confirmed once it has failed in confirm_cycles operation cycles.
An operation cycle - an ignition cycle on a vehicle - ends with ECUReset, the window's button or a timer.
A confirmed DTC whose test then passes for aging_cycles cycles ages out. When a fault appears, the
snapshot record is taken from the ECU's data at that moment and the occurrence counter (the first byte
of the extended data record) counts it. While ControlDTCSetting is off the status bits stand still.

Every change of a status byte is queued in changes, where ResponseOnEvent (onDTCStatusChange) finds it.
No Qt here: the window switches faults from its thread, the ECU reads statuses from its own.
"""
from __future__ import annotations

import collections
import threading

TEST_FAILED = 0x01
TEST_FAILED_THIS_CYCLE = 0x02
PENDING = 0x04
CONFIRMED = 0x08
NOT_COMPLETED_SINCE_CLEAR = 0x10
FAILED_SINCE_CLEAR = 0x20
NOT_COMPLETED_THIS_CYCLE = 0x40
WARNING_INDICATOR = 0x80
ALL_GROUPS = 0xFFFFFF


class DtcMemory:
    """Status bytes, records and faults of the DTCs the ECU supports.

    statuses and records are the dictionaries the ReadDTCInformation services read: DTC -> status byte,
    and DTC -> (snapshot record 01, extended data record 01). capture() gives the snapshot record of a
    fault as it appears (None: keep the one from the table)."""

    def __init__(self, statuses: dict, records: dict, confirm_cycles: int = 2, aging_cycles: int = 3, capture=None):
        self.lock = threading.RLock()
        self.statuses = dict(statuses)
        self.records = {dtc: tuple(records.get(dtc, (b"", b""))) for dtc in self.statuses}
        self.confirm_cycles = max(1, int(confirm_cycles))
        self.aging_cycles = max(1, int(aging_cycles))
        self.capture = capture
        self.faults: set[int] = set()
        self.failed_cycles = dict.fromkeys(self.statuses, 0)   # cycles with a failure since it was last not pending
        self.aging = dict.fromkeys(self.statuses, 0)           # passed cycles since it was confirmed
        self.cycle = 0
        self.frozen = False                                    # ControlDTCSetting off
        self.changes = collections.deque(maxlen=1000)          # (dtc, old status, new status)

    def _set(self, dtc: int, status: int):
        old = self.statuses[dtc]
        if status != old:
            self.statuses[dtc] = status
            self.changes.append((dtc, old, status))

    def _tested(self, dtc: int, old: int, appeared: bool = False) -> int:
        """The status after the DTC's test runs on a status of old: it fails while the fault is present and
        passes otherwise. appeared: the fault has just come, a new occurrence even if old says failed."""
        if dtc in self.faults:
            new = (old | TEST_FAILED | TEST_FAILED_THIS_CYCLE | PENDING | FAILED_SINCE_CLEAR) \
                & ~(NOT_COMPLETED_THIS_CYCLE | NOT_COMPLETED_SINCE_CLEAR)
            if not old & TEST_FAILED_THIS_CYCLE:
                self.failed_cycles[dtc] += 1
            if self.failed_cycles[dtc] >= self.confirm_cycles:
                new |= CONFIRMED
            if new & CONFIRMED:
                new |= WARNING_INDICATOR
            self.aging[dtc] = 0
            if appeared or not old & TEST_FAILED:
                self._occurred(dtc)
            return new
        return old & ~(TEST_FAILED | WARNING_INDICATOR | NOT_COMPLETED_THIS_CYCLE | NOT_COMPLETED_SINCE_CLEAR)

    def _test(self, dtc: int, appeared: bool = False):
        if not self.frozen:
            self._set(dtc, self._tested(dtc, self.statuses[dtc], appeared))

    def _occurred(self, dtc: int):
        """A new occurrence: the snapshot of this moment, and one more on the occurrence counter."""
        snapshot, extended = self.records.get(dtc, (b"", b""))
        captured = self.capture() if self.capture is not None else None
        if captured:
            snapshot = captured
        extended = bytes([min(0xFF, extended[0] + 1)]) + extended[1:] if extended else b"\x01"
        self.records[dtc] = (snapshot, extended)

    # --- what the window and the ECU do ----------------------------------------------------------------

    def set_fault(self, dtc: int, present: bool) -> None:
        """The fault behind the DTC appears or goes away; its test runs at once."""
        with self.lock:
            if dtc not in self.statuses:
                raise KeyError(f"DTC {dtc:06X} is not in the ECU's table")
            appeared = present and dtc not in self.faults
            (self.faults.add if present else self.faults.discard)(dtc)
            self._test(dtc, appeared)

    def new_operation_cycle(self) -> None:
        """The operation cycle ends and the next begins: pending DTCs whose test passed are no longer
        pending, confirmed ones age, and every test runs again in the new cycle."""
        with self.lock:
            self.cycle += 1
            if self.frozen:
                return
            for dtc, old in list(self.statuses.items()):
                new = old
                passed = not old & (TEST_FAILED_THIS_CYCLE | NOT_COMPLETED_THIS_CYCLE)
                if passed and new & PENDING:
                    new &= ~PENDING
                    self.failed_cycles[dtc] = 0
                if passed and new & CONFIRMED:
                    self.aging[dtc] += 1
                    if self.aging[dtc] >= self.aging_cycles:
                        new &= ~CONFIRMED
                        self.aging[dtc] = 0
                # The new cycle starts with the test not completed, and it runs at once: one change at most.
                self._set(dtc, self._tested(dtc, (new & ~TEST_FAILED_THIS_CYCLE) | NOT_COMPLETED_THIS_CYCLE))

    def clear(self, group: int = ALL_GROUPS) -> bool:
        """ClearDiagnosticInformation: every DTC (0xFFFFFF) or one of them. False for a DTC not kept.
        Statuses start again from "test not completed", records and counters are forgotten, and the tests
        run at once - a fault still present comes straight back."""
        with self.lock:
            if group == ALL_GROUPS:
                dtcs = list(self.statuses)
            elif group in self.statuses:
                dtcs = [group]
            else:
                return False
            for dtc in dtcs:
                self.records[dtc] = (b"", b"")
                self.failed_cycles[dtc] = self.aging[dtc] = 0
                self._set(dtc, NOT_COMPLETED_SINCE_CLEAR | NOT_COMPLETED_THIS_CYCLE)   # a real change, even if the
                self._test(dtc)                                                        # fault puts it all back
            return True

    def set_frozen(self, frozen: bool) -> None:
        """ControlDTCSetting: off freezes the status bits; back on, the tests run again."""
        with self.lock:
            if self.frozen == frozen:
                return
            self.frozen = frozen
            if not frozen:
                for dtc in self.statuses:
                    self._test(dtc)
