"""The Channel setup dialog: sample point and SJW, listen-only, receive filter, and finding the bit rate."""
from __future__ import annotations

from dataclasses import replace

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from canexpert.can_bus import LISTEN_ONLY_OPTIONS
from canexpert.channel_setup import (DETECT_BITRATES, ChannelSetup, SetupError, bit_timing, detect_bitrate,
                                     parse_filters, timing_text)


def bitrate_text(bitrate: int) -> str:
    return f"{bitrate / 1000:g} kbit/s"


class BitrateDetector(QThread):
    """detect_bitrate() off the Qt thread."""
    detected = pyqtSignal(object, list)      # the bit rate or None, and what was heard at each one
    failed = pyqtSignal(str)

    def __init__(self, channel_config, candidates=DETECT_BITRATES, listen_time=0.4):
        super().__init__()
        self.channel_config, self.candidates, self.listen_time = channel_config, candidates, listen_time

    def run(self):
        try:
            bitrate, report = detect_bitrate(self.channel_config, self.candidates, self.listen_time,
                                             cancelled=self.isInterruptionRequested)
        except SetupError as exc:
            self.failed.emit(str(exc))
            return
        self.detected.emit(bitrate, report)


class ChannelSetupDialog(QDialog):
    """How one adapter channel is opened. Kept under the channel, used from the next connection on."""

    def __init__(self, channel_config: dict, setup: ChannelSetup, bitrate: int, parent=None, in_use=False):
        super().__init__(parent)
        self.channel_config, self.bitrate, self.setup = channel_config, int(bitrate), setup
        self.interface = channel_config.get("interface", "")
        self.detector = None
        self.setWindowTitle(f"Channel setup - [{self.interface}] Ch {channel_config.get('channel', 0)}")
        self.resize(560, 360)
        layout = QVBoxLayout(self)
        note = QLabel("Kept for this adapter channel and used from the next connection on. The bit rate "
                      "itself comes from the configuration.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)

        form = QFormLayout()
        self.sample_point_spin = QDoubleSpinBox()
        self.sample_point_spin.setRange(0, 95)
        self.sample_point_spin.setSingleStep(0.5)
        self.sample_point_spin.setDecimals(1)
        self.sample_point_spin.setSuffix(" %")
        self.sample_point_spin.setSpecialValueText("the adapter's default")
        form.addRow("Sample point:", self.sample_point_spin)
        self.sjw_spin = QSpinBox()
        self.sjw_spin.setRange(0, 16)
        self.sjw_spin.setSpecialValueText("automatic")
        self.sjw_spin.setSuffix(" tq")
        form.addRow("SJW:", self.sjw_spin)
        self.timing_label = QLabel("")
        self.timing_label.setWordWrap(True)
        form.addRow("", self.timing_label)
        self.listen_only_cb = QCheckBox("Listen-only: no acknowledge, no TesterPresent, nothing sent")
        if self.interface not in LISTEN_ONLY_OPTIONS:
            self.listen_only_cb.setEnabled(False)
            self.listen_only_cb.setToolTip(f"python-can cannot open {self.interface} adapters listen-only")
        form.addRow("Mode:", self.listen_only_cb)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("everything; e.g. 7E8, 300-3FF, 18DAF100x")
        self.filter_edit.setToolTip("Identifiers and ranges the channel receives. The adapter filters where it "
                                    "can, python-can otherwise. The configuration's response identifiers are "
                                    "always let through.")
        form.addRow("Receive only:", self.filter_edit)
        detect_row = QHBoxLayout()
        self.detect_btn = QPushButton("Find the bit rate")
        self.detect_btn.setToolTip("Listen at each common bit rate, without acknowledging anything, until one "
                                   "carries frames and no error frames")
        self.detect_btn.clicked.connect(lambda: self.find_bitrate())     # not clicked's checked as candidates
        self.detect_label = QLabel("")
        self.detect_label.setWordWrap(True)
        detect_row.addWidget(self.detect_btn)
        detect_row.addWidget(self.detect_label, 1)
        form.addRow("Bit rate:", detect_row)
        if in_use:
            self.detect_btn.setEnabled(False)
            self.detect_label.setText("Disconnect first: the channel is in use.")
        elif self.interface not in LISTEN_ONLY_OPTIONS:
            self.detect_btn.setEnabled(False)
            self.detect_label.setText("Needs listen-only, which python-can does not offer for this adapter.")
        layout.addLayout(form)
        layout.addStretch()
        self.message = QLabel("")
        self.message.setStyleSheet("color: red;")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self.sample_point_spin.setValue(setup.sample_point)
        self.sjw_spin.setValue(setup.sjw)
        self.listen_only_cb.setChecked(setup.listen_only and self.listen_only_cb.isEnabled())
        self.filter_edit.setText(setup.filters)
        self.sample_point_spin.valueChanged.connect(self._show_timing)
        self.sjw_spin.valueChanged.connect(self._show_timing)
        self._show_timing()

    def values(self) -> ChannelSetup:
        return replace(self.setup, sample_point=self.sample_point_spin.value(), sjw=self.sjw_spin.value(),
                       listen_only=self.listen_only_cb.isChecked(), filters=self.filter_edit.text().strip())

    def _show_timing(self, *_):
        try:
            timing = bit_timing(self.interface, self.bitrate, self.values())
        except SetupError as exc:
            self.timing_label.setStyleSheet("color: red;")
            self.timing_label.setText(str(exc))
            return
        self.timing_label.setStyleSheet("color: gray;")
        self.timing_label.setText(f"At {bitrate_text(self.bitrate)}: {timing_text(timing)}" if timing else
                                  f"At {bitrate_text(self.bitrate)}: the adapter chooses the bit timing")

    def _accept(self):
        setup = self.values()
        try:
            bit_timing(self.interface, self.bitrate, setup)
            parse_filters(setup.filters)
        except SetupError as exc:
            self.message.setText(str(exc))
            return
        self.setup = setup
        self.accept()

    # --- finding the bit rate -------------------------------------------------------------------

    def find_bitrate(self, candidates=DETECT_BITRATES, listen_time=0.4):
        self.detect_btn.setEnabled(False)
        self.detect_label.setStyleSheet("")
        self.detect_label.setText("Listening...")
        self.detector = BitrateDetector(self.channel_config, candidates, listen_time)
        self.detector.detected.connect(self._on_detected)
        self.detector.failed.connect(self._on_detect_failed)
        self.detector.finished.connect(lambda: self.detect_btn.setEnabled(True))
        self.detector.start()
        return self.detector

    def _on_detected(self, bitrate, report):
        if bitrate is None:
            heard = [f"{bitrate_text(rate)}: {errors} error frame(s)" for rate, frames, errors, _ in report if errors]
            self.detect_label.setStyleSheet("color: red;")
            self.detect_label.setText("No bit rate carried clean traffic" +
                                      (f" ({'; '.join(heard)})" if heard else " - is the bus quiet?"))
            return
        self.detect_label.setStyleSheet("color: green;")
        note = "" if bitrate == self.bitrate else f" - the configuration uses {bitrate_text(self.bitrate)}"
        self.detect_label.setText(f"Traffic at {bitrate_text(bitrate)}{note}")

    def _on_detect_failed(self, text):
        self.detect_label.setStyleSheet("color: red;")
        self.detect_label.setText(text)

    def done(self, result):
        if self.detector is not None and self.detector.isRunning():
            self.detector.requestInterruption()
            self.detector.wait()
        super().done(result)
