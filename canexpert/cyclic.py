"""
When each of a set of things is next due to be sent.

Shared by the transmit list and the simulated nodes: both send a number of messages, each at its own
cycle time, from one timer. Keeping the arithmetic here means the drift is handled in one place - a
message due at t is next due at t + cycle, not at "now plus cycle", so a late tick does not make the
whole schedule slide.
"""
from __future__ import annotations

import time

MIN_CYCLE = 0.001      # a cycle of zero would send as fast as the timer runs


class CyclicSchedule:
    """The due times of keys sending at their own cycle. Everything is in seconds."""

    def __init__(self):
        self._due = {}

    def start(self, key, now=None):
        """Make a key due at once (it was just switched on)."""
        self._due[key] = time.monotonic() if now is None else now

    def due(self, key, cycle: float, now=None) -> bool:
        """Whether the key should be sent now; if so, when it is next due is worked out."""
        now = time.monotonic() if now is None else now
        due = self._due.get(key, now)
        if now < due:
            return False
        # Counted from when it was due rather than from now, so a tick arriving late does not make the
        # whole schedule slide; but a long gap - a frozen window - does not queue up missed sends either.
        step = max(float(cycle), MIN_CYCLE)
        self._due[key] = due + step if due + step > now else now + step
        return True

    def drop(self, key):
        self._due.pop(key, None)

    def clear(self):
        self._due.clear()

    def __len__(self):
        return len(self._due)
