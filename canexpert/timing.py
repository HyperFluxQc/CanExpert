"""
Waiting until a moment to a fraction of a millisecond, on Windows too.

Windows wakes a sleeping thread on its timer tick - every 15.6 ms unless a program has asked for finer ones -
and Python 3.10's time.sleep() follows it: a frame due every 10 ms goes out every 15 or 16. A high-resolution
waitable timer (Windows 10 1803 and later, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION; Python 3.11's time.sleep()
uses one too) wakes within about half a millisecond without changing the timer of the whole computer, and
spinning the last half millisecond takes the rest. Measured on a development PC for a 10 ms cycle:
time.sleep() 15.8 ms on average (10.3 to 25.5), the high-resolution timer 10.00 ms (9.3 to 10.6), with the
spin 10.00 ms (9.7 to 10.3).

Python threads also wait for one another: a thread woken on time must wait for the one running Python code
to let go of the interpreter, which it does every 5 ms by default - a busy window thread made a 10 ms cycle
anything from 0 to 36 ms. precise_switching() makes that half a millisecond.
"""
from __future__ import annotations

import sys
import threading
import time

SPIN = 0.0005              # the last part of a wait is spun rather than slept (seconds)
SWITCH_INTERVAL = 0.0005   # how often a busy Python thread hands the interpreter over (seconds)


def precise_switching():
    """Let a thread that woke up run within half a millisecond, however busy the others are."""
    if sys.getswitchinterval() > SWITCH_INTERVAL:
        sys.setswitchinterval(SWITCH_INTERVAL)


def raise_priority():
    """Run the calling thread before the computer's ordinary ones (Windows; elsewhere nothing changes)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 2)          # THREAD_PRIORITY_HIGHEST
    except (OSError, AttributeError):
        pass


class Waiter:
    """Waits until a moment of time.perf_counter(), or until another thread wakes it; for one thread."""

    def __init__(self, spin: float = SPIN):
        self.spin = spin
        self._woken = threading.Event()
        self._timer = _HighResolutionTimer.create() if sys.platform == "win32" else None

    @property
    def high_resolution(self) -> bool:
        """Whether a high-resolution timer is used (else the operating system's ordinary wait)."""
        return self._timer is not None and self._timer.high_resolution or sys.platform != "win32"

    def wait_until(self, deadline: float) -> bool:
        """Return at deadline (time.perf_counter()) - True if woken before it."""
        while True:
            if self._woken.is_set():
                self._woken.clear()
                return True
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return False
            if remaining > self.spin:
                sleep = remaining - self.spin
                if self._timer is not None:
                    self._timer.wait(sleep)                 # also returns when wake() signals it
                else:
                    self._woken.wait(sleep)
            else:                                           # the last moment: spin, with a check to be woken
                while time.perf_counter() < deadline and not self._woken.is_set():
                    pass

    def wake(self):
        """Cut the wait short (from another thread)."""
        self._woken.set()
        if self._timer is not None:
            self._timer.signal()

    def close(self):
        if self._timer is not None:
            self._timer.close()
            self._timer = None


class _HighResolutionTimer:
    """A Windows waitable timer - high-resolution where Windows has them - and an event that ends a wait."""

    @classmethod
    def create(cls):
        try:
            return cls()
        except (OSError, AttributeError):                   # not Windows after all, or no kernel32
            return None

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateWaitableTimerExW.restype = wintypes.HANDLE
        kernel32.CreateWaitableTimerExW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        kernel32.SetWaitableTimer.restype = wintypes.BOOL
        kernel32.SetWaitableTimer.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong), wintypes.LONG,
                                              ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
        kernel32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                                                    wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._ctypes, self._kernel32 = ctypes, kernel32
        high_resolution, timer_all_access = 0x00000002, 0x001F0003
        timer = kernel32.CreateWaitableTimerExW(None, None, high_resolution, timer_all_access)
        self.high_resolution = bool(timer)
        if not timer:                                       # before Windows 10 1803: an ordinary timer
            timer = kernel32.CreateWaitableTimerExW(None, None, 0, timer_all_access)
        event = kernel32.CreateEventW(None, False, False, None)            # auto-reset
        if not timer or not event:
            raise ctypes.WinError(ctypes.get_last_error())
        self._timer, self._event = timer, event
        self._handles = (wintypes.HANDLE * 2)(timer, event)
        self._due = ctypes.c_longlong()

    def wait(self, seconds: float):
        """Sleep seconds, or until signal()."""
        self._due.value = -max(1, int(seconds * 10_000_000))              # relative, in 100 ns
        if self._kernel32.SetWaitableTimer(self._timer, self._ctypes.byref(self._due), 0, None, None, False):
            self._kernel32.WaitForMultipleObjects(2, self._handles, False, 0xFFFFFFFF)
        else:
            time.sleep(seconds)

    def signal(self):
        self._kernel32.SetEvent(self._event)

    def close(self):
        for handle in (self._timer, self._event):
            self._kernel32.CloseHandle(handle)
        self._timer = self._event = None
