"""
One measurement clock for every window.

Every frame carries a timestamp: the adapter's for received frames, the moment it was handed to the
adapter for sent ones, the file's own for a replayed recording. A window shows that timestamp - never
the moment it happened to draw the line - either as the time of day or as seconds since the
measurement started, and "the measurement started" is the same moment for all of them: the connect,
the ECU check or the replay, whichever began it. That is what makes a line in the UDS Console, a row
in the Trace and a point on a Logger graph line up.
"""
from __future__ import annotations

from datetime import datetime

TIME_DISPLAYS = ("Absolute", "Relative")
# Timestamps above this are times of day (it is 2001-09-09); below it they are seconds from a recording's
# own zero, which some formats start at, and are shown as such rather than as a date in 1970.
EPOCH_FLOOR = 1e9


def absolute_text(timestamp: float) -> str:
    """The time of day of a timestamp, to the millisecond, or its seconds when it is not a time of day."""
    if timestamp >= EPOCH_FLOOR:
        return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S.%f")[:-3]
    return f"{timestamp:.3f}"


class MeasurementClock:
    """When the measurement started, and times shown against it."""

    def __init__(self):
        self.start = None

    def begin(self, start: float | None = None):
        """A new measurement: from start, or from the first frame seen when start is None."""
        self.start = start

    def see(self, timestamp: float):
        if self.start is None:
            self.start = float(timestamp)

    def relative(self, timestamp: float) -> float:
        return float(timestamp) - (self.start if self.start is not None else float(timestamp))

    def text(self, timestamp: float, display: str = "Absolute") -> str:
        if display == "Relative":
            return f"{self.relative(timestamp):.3f}"
        return absolute_text(timestamp)
