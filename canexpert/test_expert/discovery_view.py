"""
TestExpert's discovery in the window: the options dialog (sessions, DID and routine ranges) and the Discovery
tab - what was found, compared with the description, kept as a report, or used as the description.
"""
from __future__ import annotations

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from canexpert.test_expert.discovery import DiscoveryOptions, expand, parse_ranges


class DiscoveryDialog(QDialog):
    """What to ask the ECU: the sessions (the programming session unticked: entering it may start the
    bootloader), the DID and routine ranges, the services and the security levels."""

    def __init__(self, description, options: DiscoveryOptions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Discover the ECU")
        layout = QVBoxLayout(self)
        note = QLabel("TestExpert asks the ECU what it has, session by session - each service's SID alone, each DID "
                      "read, each routine's results (31 03: nothing is started), each security level's seed - and "
                      "compares it with the description.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.session_boxes = {}
        row = QHBoxLayout()
        sessions = sorted(description.sessions) if description is not None else [0x01, 0x02, 0x03]
        for session in sessions:
            box = QCheckBox(f"{session:02X} {description.session_name(session) if description else ''}".strip())
            box.setChecked(session in options.sessions)
            if session == 0x02:
                box.setToolTip("Entering the programming session may start the ECU's bootloader")
            self.session_boxes[session] = box
            row.addWidget(box)
        row.addStretch()
        form.addRow("Sessions", row)
        self.dids = QLineEdit(options.dids)
        self.dids.setToolTip("Ranges of DIDs to read, in hex: 0100-02FF, F100-F2FF (and every DID the description "
                             "has)")
        self.rids = QLineEdit(options.rids)
        self.rids.setToolTip("Ranges of routines whose results are asked (31 03), in hex, and every routine the "
                             "description has")
        form.addRow("DIDs", self.dids)
        form.addRow("Routines", self.rids)
        self.services = QCheckBox("Services (10-3E, 80-8F)")
        self.services.setChecked(options.services)
        self.security = QCheckBox("Security levels (requestSeed 01-41)")
        self.security.setChecked(options.security)
        form.addRow("", self.services)
        form.addRow("", self.security)
        layout.addLayout(form)
        self.estimate = QLabel("")
        self.estimate.setStyleSheet("color: gray;")
        layout.addWidget(self.estimate)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Discover")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok = buttons.button(QDialogButtonBox.Ok)
        layout.addWidget(buttons)
        for widget in (self.dids, self.rids):
            widget.textChanged.connect(self._check)
        for box in (*self.session_boxes.values(), self.services, self.security):
            box.toggled.connect(self._check)
        self._check()

    def options(self) -> DiscoveryOptions:
        return DiscoveryOptions([session for session, box in self.session_boxes.items() if box.isChecked()],
                                self.dids.text().strip(), self.rids.text().strip(), self.services.isChecked(),
                                self.security.isChecked())

    def _check(self):
        try:
            dids, rids = len(expand(parse_ranges(self.dids.text()))), len(expand(parse_ranges(self.rids.text())))
        except ValueError as exc:
            self.estimate.setText(str(exc))
            self.ok.setEnabled(False)
            return
        sessions = sum(box.isChecked() for box in self.session_boxes.values())
        per_session = dids + rids + (62 if self.services.isChecked() else 0) + (33 if self.security.isChecked() else 0)
        self.estimate.setText(f"About {sessions * per_session} requests")
        self.ok.setEnabled(sessions > 0)


class DiscoveryView(QWidget):
    """The Discovery tab: the result, and what can be done with it."""
    use_requested = pyqtSignal()
    save_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.use_btn = QPushButton("Use as the description")
        self.use_btn.setToolTip("Test the ECU as it was found (services, DIDs, routines and levels found, in the "
                                "sessions they answered in)")
        self.use_btn.clicked.connect(self.use_requested.emit)
        self.save_btn = QPushButton("Save...")
        self.save_btn.setToolTip("The discovery as an HTML report, and what was found as a JSON description")
        self.save_btn.clicked.connect(self.save_requested.emit)
        for button in (self.use_btn, self.save_btn):
            button.setEnabled(False)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        self.browser = QTextBrowser()
        self.browser.setPlaceholderText("Discover... asks the ECU what it has and compares it with the description")
        layout.addWidget(self.browser, 1)

    def show_html(self, text: str, usable: bool = True):
        self.browser.setHtml(text)
        for button in (self.use_btn, self.save_btn):
            button.setEnabled(usable)
