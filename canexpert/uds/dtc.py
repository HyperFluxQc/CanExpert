"""
The DTC status byte of ISO 14229-1 (annex D): its eight bits by name, for whoever shows one - the UDS Console
reading an ECU's fault memory, the Dummy ECU keeping its own.
"""
from __future__ import annotations

STATUS_BITS = ("testFailed", "testFailedThisOperationCycle", "pendingDTC", "confirmedDTC",
               "testNotCompletedSinceLastClear", "testFailedSinceLastClear",
               "testNotCompletedThisOperationCycle", "warningIndicatorRequested")


def status_text(status: int) -> str:
    """"testFailed, confirmedDTC" - the bits of a status byte by name; "none" when no bit is set."""
    return ", ".join(name for bit, name in enumerate(STATUS_BITS) if status & (1 << bit)) or "none"
