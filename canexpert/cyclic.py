"""
Cyclic frames: when each is next due, and the thread that sends them on time.

Shared by the transmit list and the simulated nodes. A frame due at t is next due at t + cycle, not at "now
plus cycle", so a late send does not make the whole schedule slide; one more than a cycle late is not caught
up with in a burst either.

CyclicSender sends from a thread of its own, at a raised priority, waiting with timing.Waiter: on the moment
to about half a millisecond, where a window's timer, woken on Windows' 15.6 ms tick and behind whatever the
window is drawing, sent a 10 ms frame every 15 or 16 ms.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from canexpert.timing import Waiter, precise_switching, raise_priority

MIN_CYCLE = 0.001      # a cycle of zero would send as fast as the thread runs
MEASURED = 100         # the intervals a key's measured cycle is taken over


class CyclicSchedule:
    """The due times of keys sending at their own cycle. Everything is in seconds, on one clock: now is
    time.monotonic() unless given."""

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
        # Counted from when it was due rather than from now, so a send made late does not make the whole
        # schedule slide; but a long gap - a frozen window - does not queue up missed sends either.
        step = max(float(cycle), MIN_CYCLE)
        self._due[key] = due + step if due + step > now else now + step
        return True

    def next_due(self):
        """The earliest due time, or None when nothing is scheduled."""
        return min(self._due.values(), default=None)

    def drop(self, key):
        self._due.pop(key, None)

    def clear(self):
        self._due.clear()

    def __len__(self):
        return len(self._due)


class CycleStats:
    """How often a key really went: its sends, and the intervals between its last ones."""

    def __init__(self):
        self.sent = 0
        self._new = 0                       # sends since take_new()
        self._last = None
        self.intervals = deque(maxlen=MEASURED)

    def add(self, moment: float):
        if self._last is not None:
            self.intervals.append(moment - self._last)
        self._last = moment
        self.sent += 1
        self._new += 1

    def take_new(self) -> int:
        """The sends since the last call (for a counter shown elsewhere)."""
        new, self._new = self._new, 0
        return new

    def text(self) -> str:
        """"10.0 ms (9.7-10.3)": the mean interval and its range, over the last sends."""
        intervals = list(self.intervals)
        if not intervals:
            return ""
        mean = sum(intervals) / len(intervals) * 1000
        return f"{mean:.1f} ({min(intervals) * 1000:.1f}-{max(intervals) * 1000:.1f})"


class CyclicSender:
    """Sends cyclic frames from a thread of its own, each on time.

    set(key, cycle, frame): frame() is asked at every send - so a frame edited meanwhile goes out as it is then
    - and gives (can_id, data, extended), or None to skip this time. send(can_id, data, extended) puts it on
    the bus, from this thread. A key is first sent at once, then every cycle. A send that raises stops the key,
    and failed(key, error) is called (from this thread).
    """

    def __init__(self, send, failed=None, spin=None):
        self._send = send
        self._failed = failed or (lambda key, error: None)
        self._lock = threading.Lock()
        self._entries = {}                  # key -> (cycle, frame)
        self._stats = {}                    # key -> CycleStats
        self._schedule = CyclicSchedule()   # on time.perf_counter()
        self._waiter = Waiter() if spin is None else Waiter(spin)
        self._thread = None
        self._closed = False

    def set(self, key, cycle: float, frame):
        """Send frame() every cycle seconds - at once for a new key; a key already sent keeps its rhythm."""
        with self._lock:
            if self._closed:
                return
            if key not in self._entries:
                self._schedule.start(key, time.perf_counter())
                self._stats[key] = CycleStats()
            self._entries[key] = (max(float(cycle), MIN_CYCLE), frame)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="Cyclic frames", daemon=True)
                self._thread.start()
        self._waiter.wake()

    def remove(self, key):
        with self._lock:
            self._entries.pop(key, None)
            self._stats.pop(key, None)
            self._schedule.drop(key)

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._stats.clear()
            self._schedule.clear()

    def keys(self) -> list:
        with self._lock:
            return list(self._entries)

    def take(self, key) -> tuple[int, str]:
        """(sends since the last call, the measured cycle as CycleStats.text()) of a key; (0, "") for one not
        sent. For a window's counters: the sending thread adds to them meanwhile."""
        with self._lock:
            stats = self._stats.get(key)
            return (stats.take_new(), stats.text()) if stats is not None else (0, "")

    def close(self):
        """Stop sending, for good."""
        with self._lock:
            self._closed = True
            self._entries.clear()
            thread = self._thread
        self._waiter.wake()
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)
        self._waiter.close()

    def _run(self):
        precise_switching()
        raise_priority()
        while True:
            with self._lock:
                if self._closed or not self._entries:
                    self._thread = None
                    return
                deadline = self._schedule.next_due()
            if self._waiter.wait_until(deadline):
                continue                                    # woken: something changed
            now = time.perf_counter()
            with self._lock:
                due = [(key, frame) for key, (cycle, frame) in self._entries.items()
                       if self._schedule.due(key, cycle, now)]
            for key, frame in due:
                try:
                    message = frame()
                    if message is None:
                        continue
                    self._send(*message)
                except Exception as exc:                    # not connected, adapter error, a frame that is wrong
                    self.remove(key)
                    self._failed(key, exc)
                    continue
                with self._lock:
                    stats = self._stats.get(key)
                    if stats is not None:
                        stats.add(time.perf_counter())
