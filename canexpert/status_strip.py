"""
The main window's status strip: the bus state, the diagnostic session and security state, and the last error.

The session and the security state are read off the ECU's answers as they pass on the bus (DiagnosticState),
so they are right whoever sent the request - the UDS Console, the panel script, the flash sequence.
"""
from __future__ import annotations

import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLabel, QWidget

from canexpert.clock import absolute_text
from canexpert.uds.client import NRC_NAMES
from canexpert.uds.observer import SERVICE_NAMES

SESSION_NAMES = {0x01: "default", 0x02: "programming", 0x03: "extended", 0x04: "safety system"}
IGNORED_NRCS = {0x78}          # responsePending: the answer is on its way, nothing went wrong


class DiagnosticState:
    """Session and security, from the positive answers to DiagnosticSessionControl (50), ECUReset (51) and
    SecurityAccess sendKey (67 <even>); a negative answer is kept as the last error."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.session = None            # the session number, None until an answer says
        self.security = None           # the unlocked level (requestSeed sub-function), None while locked
        self.error = ""

    @staticmethod
    def payload(data: bytes, address_byte: int | None = None) -> bytes | None:
        """The UDS payload of a single frame (the answers followed here all fit one), else None."""
        data = bytes(data)
        if address_byte is not None:
            data = data[1:]
        if not data or data[0] >> 4 != 0:
            return None
        length = data[0] & 0x0F
        return data[1:1 + length] if 0 < length <= len(data) - 1 else None

    def on_response(self, payload: bytes) -> bool:
        """Take one answer of the ECU; returns whether the strip has something new to show."""
        payload = bytes(payload or b"")
        if len(payload) >= 2 and payload[0] == 0x50:
            self.session, self.security = payload[1] & 0x7F, None      # a new session locks the ECU again
            return True
        if payload[:1] == b"\x51":
            self.session, self.security = 0x01, None                     # a reset starts in the default session
            return True
        if len(payload) >= 2 and payload[0] == 0x67 and not payload[1] & 1:
            self.security = (payload[1] & 0x7F) - 1                      # sendKey accepted: that level's seed
            return True
        if len(payload) >= 3 and payload[0] == 0x7F and payload[2] not in IGNORED_NRCS:
            service = SERVICE_NAMES.get(payload[1], f"service 0x{payload[1]:02X}")
            self.error = f"{service}: NRC 0x{payload[2]:02X} {NRC_NAMES.get(payload[2], 'unknown')}"
            return True
        return False

    def session_text(self) -> str:
        if self.session is None:
            return "unknown"
        return SESSION_NAMES.get(self.session, f"0x{self.session:02X}")

    def security_text(self) -> str:
        return "locked" if self.security is None else f"unlocked (level {self.security})"


class StatusStrip(QWidget):
    """Bus | Session | Security | Last error, for the main window's status bar. Clicking the last error
    asks for the log (error_clicked)."""
    error_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = DiagnosticState()
        self.bus_text = ""
        self._error_time = ""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(14)
        self.bus_label, self.session_label, self.security_label = QLabel(), QLabel(), QLabel()
        self.error_label = QLabel()
        self.error_label.setTextFormat(Qt.RichText)               # a link to the log
        self.error_label.linkActivated.connect(lambda _link: self.error_clicked.emit())
        for label in (self.bus_label, self.session_label, self.security_label, self.error_label):
            layout.addWidget(label)
        self.session_label.setToolTip("The diagnostic session, from the ECU's last answer to "
                                      "DiagnosticSessionControl (0x10) or ECUReset (0x11)")
        self.security_label.setToolTip("SecurityAccess (0x27): unlocked once the ECU accepts a key; a new "
                                       "session or a reset locks it again")
        self.disconnected()

    # --- what the main window tells it -------------------------------------------------------

    def connected(self):
        self.state.reset()
        self.set_bus("unknown", 0)
        self._show()

    def disconnected(self):
        self.state.session = self.state.security = None
        self.bus_text = ""
        self._show()

    def set_bus(self, state: str, error_frames: int = 0):
        """The adapter's error state (error active, error passive, bus off, unknown) and its error frames."""
        text = "on" if state == "unknown" else state
        if error_frames:
            text += f", {error_frames} error frame{'s' if error_frames != 1 else ''}"
        self.bus_text = text
        self.bus_label.setToolTip("" if state != "unknown" else "This adapter does not report its error state")
        self.bus_label.setStyleSheet(
            {"bus off": "color: red;", "error passive": "color: orange;"}.get(state, ""))
        self._show()

    def on_response(self, payload: bytes):
        """An answer of the ECU, as a UDS payload."""
        before = self.state.error
        if self.state.on_response(payload):
            if self.state.error != before:
                self._stamp_error()
            self._show()

    def set_error(self, text: str):
        """The last thing that went wrong: an NRC, a script error, the session failing, bus off."""
        self.state.error = " ".join(str(text).split())
        self._stamp_error()
        self._show()

    # --- drawing -----------------------------------------------------------------------------

    def _stamp_error(self):
        self._error_time = absolute_text(time.time())

    def _show(self):
        connected = bool(self.bus_text)
        self.bus_label.setText(f"Bus: {self.bus_text}" if connected else "Bus: not connected")
        self.session_label.setText(f"Session: {self.state.session_text()}" if connected else "")
        self.security_label.setText(f"Security: {self.state.security_text()}" if connected else "")
        self.session_label.setVisible(connected)
        self.security_label.setVisible(connected)
        self.security_label.setStyleSheet("color: green;" if self.state.security is not None else "")
        error = self.state.error
        self.error_label.setVisible(bool(error))
        if error:
            shown = error if len(error) <= 70 else error[:67] + "..."
            self.error_label.setText(f'Last error: <a href="log" style="color: red;">{_escape(shown)}</a>')
            self.error_label.setToolTip(f"{self._error_time}  {error}\nClick to show the Log")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
