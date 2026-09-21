"""
The built-in flashing sequence, run beside the UI.

canexpert.flash_sequence knows what to send and in which order; this is what keeps it off the Qt thread:
its own mailbox on the running measurement, a thread, and progress, log and finished signals the window
can connect to - the same shape the panel script's Flashing() already reports through.
"""
from __future__ import annotations

import threading

from PyQt5.QtCore import QObject, pyqtSignal

from canexpert.can_bus import ReceiveMailbox
from canexpert.config import uds_transport
from canexpert.flash_sequence import FlashCancelled, FlashError, FlashRun, run_flash
from canexpert.uds.client import UdsFunctions
from canexpert.uds_console import make_request      # the transport the console already binds to a mailbox


class FlashRunner(QObject):
    """Flash a firmware image with the built-in sequence, without blocking the window."""

    progress = pyqtSignal(int, int, str)            # bytes written, bytes in total, what is happening
    logged = pyqtSignal(str)
    finished = pyqtSignal(bool, str)                # succeeded, what to tell the user

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session                      # session() -> (bus, worker, config) while connected
        self.run = None                             # the FlashRun of the last attempt, finished or not
        self._cancel = threading.Event()
        self._thread = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, firmware, profile) -> bool:
        """Begin flashing. False when nothing is connected or a run is already going."""
        session = self.session()
        if session is None or self.busy:
            return False
        bus, worker, config = session
        mailbox = ReceiveMailbox(bus, worker.message_sent.emit)
        worker.add_mailbox(mailbox)
        transport = uds_transport(config)
        uds = UdsFunctions(make_request(mailbox, transport), self.logged.emit, transport["timeout"])
        self.run = FlashRun(profile, firmware, self.logged.emit)
        self._cancel.clear()
        self._thread = threading.Thread(target=self._flash, args=(uds, firmware, profile, worker, mailbox),
                                        daemon=True)
        self._thread.start()
        return True

    def cancel(self):
        """Stop between two steps - a TransferData already sent is still finished first."""
        self._cancel.set()

    def _flash(self, uds, firmware, profile, worker, mailbox):
        run = self.run
        try:
            run_flash(uds, firmware, profile, progress=self.progress.emit, cancelled=self._cancel.is_set,
                      log=self.logged.emit, run=run)
            ok = True
            text = f"{run.written} bytes written in {len(firmware.segments)} segment(s)."
        except FlashCancelled:
            ok, text = False, f"Cancelled after {run.written} of {firmware.size} bytes."
            run.record("Flashing", "cancelled")
        except Exception as exc:                    # a refused service, or the bus going away under it
            ok = False
            text = str(exc) if isinstance(exc, FlashError) else f"{type(exc).__name__}: {exc}"
            run.record("Flashing", text)
        finally:
            worker.remove_mailbox(mailbox)
            mailbox.close()
        try:
            text += f"\n\nReport: {run.write_report()}"
        except OSError as exc:                      # a read-only folder beside the firmware
            text += f"\n\n(the report could not be written: {exc})"
        self.finished.emit(ok, text)
