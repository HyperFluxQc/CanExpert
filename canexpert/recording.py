"""
Recording a measurement to a file, and replaying a recorded file back into the application.

python-can writes and reads BLF, ASC, CSV, LOG and TRC files, so the format follows the file name.
Replay is offline mode: the frames reach the Trace window, the CAN Logger and the panels exactly as
live ones do, but nothing is transmitted to a bus.
"""
from __future__ import annotations

import time
from pathlib import Path

import can
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

LOG_FILE_FILTER = ("CAN logs (*.blf *.asc *.csv *.log *.trc);;Vector binary (*.blf);;Vector ASCII (*.asc);;"
                   "CSV (*.csv);;All files (*.*)")
# Replay speeds offered in the dialog; 0 means "as fast as the file can be read".
SPEEDS = (("Real time", 1.0), ("2x", 2.0), ("5x", 5.0), ("10x", 10.0), ("As fast as possible", 0.0))
BATCH = 500           # frames emitted in one go when replaying without pacing
BATCH_SECONDS = 0.005  # frames closer together than this are emitted together


def make_message(timestamp, direction, can_id, data, extended=False) -> can.Message:
    """A python-can message for the writers; is_rx is only set where python-can supports it."""
    try:
        return can.Message(timestamp=float(timestamp), arbitration_id=int(can_id), data=bytes(data),
                           is_extended_id=bool(extended), is_rx=direction != "TX", check=False)
    except TypeError:                                    # python-can before 4.0 has no is_rx
        return can.Message(timestamp=float(timestamp), arbitration_id=int(can_id), data=bytes(data),
                           is_extended_id=bool(extended), check=False)


def read_frames(path) -> list[tuple]:
    """(timestamp, direction, id, data, extended) of every frame in a recorded file."""
    frames = []
    reader = can.LogReader(str(path))
    try:
        for message in reader:
            if message.is_error_frame or message.is_remote_frame:
                continue
            frames.append((float(message.timestamp), "TX" if getattr(message, "is_rx", True) is False else "RX",
                           int(message.arbitration_id), bytes(message.data), bool(message.is_extended_id)))
    finally:
        reader.stop()
    return frames


class Recorder:
    """Writes every frame of the measurement to a file until stop()."""

    def __init__(self, path):
        self.path = str(path)
        self.count = 0
        self.writer = can.Logger(self.path)

    def write(self, timestamp, direction, can_id, data, extended=False):
        self.writer.on_message_received(make_message(timestamp, direction, can_id, data, extended))
        self.count += 1

    def write_marker(self, timestamp, text) -> bool:
        """A marker and its comment, where the format holds one: BLF as a global marker (CANoe shows it on
        its time axis), ASC as a comment line with its time, TRC as a comment. CSV and LOG have no place
        for it: False."""
        log_event = getattr(self.writer, "log_event", None)
        if log_event is None:
            return False
        if isinstance(self.writer, can.TRCWriter):
            log_event(f";   Marker: {text}", timestamp)        # a TRC line starting with ; is a comment
        else:
            log_event(text if isinstance(self.writer, can.BLFWriter) else f"Marker: {text}", timestamp)
        return True

    def stop(self):
        self.writer.stop()

    @property
    def name(self) -> str:
        return Path(self.path).name


class ReplayWorker(QThread):
    """Reads a recorded file and hands its frames back at their recorded spacing (speed 0: at once)."""
    frames_ready = pyqtSignal(list)
    progress = pyqtSignal(int, int)
    replay_finished = pyqtSignal(int, str)

    def __init__(self, path, speed: float = 1.0, parent=None):
        super().__init__(parent)
        self.path, self.speed = str(path), float(speed)
        self._frames = []

    def run(self):
        error = ""
        try:
            self._frames = read_frames(self.path)
        except Exception as exc:                          # a file python-can cannot read at all
            self.replay_finished.emit(0, str(exc))
            return
        total, sent, batch = len(self._frames), 0, []
        previous = None
        start = time.perf_counter()
        first = self._frames[0][0] if self._frames else 0.0
        for frame in self._frames:
            if self.isInterruptionRequested():
                break
            gap = 0.0 if previous is None else frame[0] - previous
            previous = frame[0]
            if self.speed > 0 and gap > BATCH_SECONDS:
                if batch:
                    self.frames_ready.emit(batch)
                    batch = []
                # Pace against the wall clock, so a slow consumer cannot make the replay drift.
                due = start + (frame[0] - first) / self.speed
                while True:
                    remaining = due - time.perf_counter()
                    if remaining <= 0 or self.isInterruptionRequested():
                        break
                    self.msleep(int(min(remaining, 0.05) * 1000) or 1)
            batch.append(frame)
            sent += 1
            if len(batch) >= BATCH or (self.speed > 0 and len(batch) >= 50):
                self.frames_ready.emit(batch)
                batch = []
                self.progress.emit(sent, total)
        if batch:
            self.frames_ready.emit(batch)
        self.progress.emit(sent, total)
        self.replay_finished.emit(sent, error)

    def stop(self):
        self.requestInterruption()
        self.wait(2000)


class ReplayDialog(QDialog):
    """Plays a recorded file back into the application; deliver(frames) is called on the GUI thread."""

    def __init__(self, path, deliver, parent=None, speed_index=0):
        super().__init__(parent)
        self.setWindowTitle(f"Replay - {Path(path).name}")
        self.resize(460, 150)
        self.deliver = deliver
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Replaying {Path(path).name}. Nothing is transmitted to a bus."))
        row = QHBoxLayout()
        row.addWidget(QLabel("Speed:"))
        self.speed_combo = QComboBox()
        for label, _ in SPEEDS:
            self.speed_combo.addItem(label)
        self.speed_combo.setCurrentIndex(speed_index)
        self.speed_combo.setToolTip("The speed is taken when the replay starts")
        row.addWidget(self.speed_combo)
        row.addStretch()
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_replay)
        row.addWidget(self.start_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_replay)
        row.addWidget(self.stop_btn)
        layout.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        layout.addWidget(self.bar)
        self.status = QLabel("Ready")
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)
        self.worker = None
        self.path = str(path)

    def start_replay(self):
        if self.worker is not None:
            return
        speed = SPEEDS[self.speed_combo.currentIndex()][1]
        self.worker = ReplayWorker(self.path, speed, self)
        self.worker.frames_ready.connect(self.deliver)
        self.worker.progress.connect(self._on_progress)
        self.worker.replay_finished.connect(self._on_finished)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Replaying...")
        self.worker.start()

    def stop_replay(self):
        if self.worker is not None:
            self.worker.stop()

    def _on_progress(self, done, total):
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(done)

    def _on_finished(self, count, error):
        self.worker = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText(f"Replay failed: {error}" if error else f"Replayed {count} frame(s)")
        self.status.setStyleSheet("color: red;" if error else "color: gray;")

    def done(self, result):
        self.stop_replay()
        super().done(result)
