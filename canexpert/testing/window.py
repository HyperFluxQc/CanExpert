"""
The Test window: open a test module, tick its test cases, run them against the measurement's bus and see
each step's verdict as it comes; every run leaves an HTML and a JUnit XML report.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices
from PyQt5.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canexpert.can_bus import ReceiveMailbox
from canexpert.clock import absolute_text
from canexpert.config import uds_transport
from canexpert.paths import TEST_MODULES_DIR
from canexpert.testing.report import COLOURS, save_reports
from canexpert.testing.runner import PASSED, FrameMailbox, Runner, load_module, uds_names
from canexpert.uds.client import make_request

MODULE_SETTING = "tests/module"
EXAMPLE_MODULE = TEST_MODULES_DIR / "dummy_ecu_checks.py"
COL_NAME, COL_VERDICT, COL_STEPS, COL_TIME = range(4)


class TestWindow(QWidget):
    """session() -> (bus, worker, configuration) while a measurement runs, else None; decode(can_id, data) ->
    (message name, {signal: value}) with the symbol databases."""
    run_event = pyqtSignal(str, object)       # from the run's thread: case, step, verdict
    run_finished = pyqtSignal(object)         # the TestReport
    marker_requested = pyqtSignal(float, str)  # t.marker() in the running module: (when, comment)

    def __init__(self, parent=None, session=None, decode=None, settings=None, time_text=None):
        super().__init__(parent)
        self.session = session or (lambda: None)
        self.decode = decode or (lambda can_id, data: ("", {}))
        self.settings = settings if settings is not None else MemorySettings()   # never the user's own
        self.time_text = time_text or absolute_text
        self.module = None
        self.runner = None
        self.thread = None
        self.report = None
        self.report_paths = None
        self._items = {}                      # test case name -> its tree item
        self._running_item = None
        self._build_ui()
        self.run_event.connect(self._on_event)
        self.run_finished.connect(self._on_finished)
        remembered = Path(self.settings.value(MODULE_SETTING, "", type=str) or "")
        start = remembered if remembered.is_file() else EXAMPLE_MODULE
        if start.is_file():
            self.open_module(start)

    # --- UI -------------------------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        for text, slot, tip in (("Open...", lambda: self.open_module(), "Open a test module (.py)"),
                                ("Reload", self.reload, "Read the test module again, after editing it")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            bar.addWidget(button)
        self.path_label = QLabel("No test module")
        self.path_label.setStyleSheet("color: gray;")
        bar.addWidget(self.path_label, 1)
        self.run_btn = QPushButton("Run")
        self.run_btn.setToolTip("Run the ticked test cases against the measurement's bus")
        self.run_btn.clicked.connect(lambda: self.run())
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setToolTip("Stop after the current step; teardown still runs")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        self.report_btn = QPushButton("Open report")
        self.report_btn.setToolTip("The HTML report of the last run")
        self.report_btn.clicked.connect(self.open_report)
        self.report_btn.setEnabled(False)
        for button in (self.run_btn, self.stop_btn, self.report_btn):
            bar.addWidget(button)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Test case / step", "Verdict", "Steps", "Time (s)"])
        self.tree.setColumnWidth(COL_NAME, 460)
        self.tree.setColumnWidth(COL_VERDICT, 80)
        self.tree.setColumnWidth(COL_STEPS, 60)
        splitter.addWidget(self.tree)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setPlaceholderText("The steps of the run appear here.")
        splitter.addWidget(self.log)
        splitter.setSizes([380, 160])
        layout.addWidget(splitter, 1)
        self.status = QLabel("")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)

    def _write(self, text):
        self.log.appendPlainText(text)

    # --- the module -------------------------------------------------------------------------------

    def open_module(self, path=None):
        if path is None:
            start = str(self.module.path.parent if self.module else TEST_MODULES_DIR)
            path, _ = QFileDialog.getOpenFileName(self, "Test module", start, "Python test module (*.py)")
            if not path:
                return None
        try:
            module = load_module(path, uds_names())
        except Exception as exc:                      # a syntax error, or code failing at import
            self._write(f"The test module could not be read: {type(exc).__name__}: {exc}")
            return None
        self.module = module
        self.settings.setValue(MODULE_SETTING, str(module.path))
        self.path_label.setText(f"{module.title}  ({module.path.name}, {len(module.cases)} test cases)")
        self.path_label.setToolTip(str(module.path))
        self._fill_tree()
        return module

    def reload(self):
        return self.open_module(self.module.path) if self.module else self.open_module()

    def _fill_tree(self):
        ticked = {name for name, item in self._items.items() if item.checkState(COL_NAME) == Qt.Checked}
        known = set(self._items)
        self.tree.clear()
        self._items = {}
        for case in self.module.cases:
            item = QTreeWidgetItem([case.title, "", "", ""])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(COL_NAME, Qt.Checked if case.name in ticked or case.name not in known else Qt.Unchecked)
            item.setToolTip(COL_NAME, case.doc or case.name)
            self.tree.addTopLevelItem(item)
            self._items[case.name] = item

    def ticked(self) -> list[str]:
        return [name for name, item in self._items.items() if item.checkState(COL_NAME) == Qt.Checked]

    # --- running ------------------------------------------------------------------------------------

    def run(self, names=None):
        """Run the ticked test cases (or those named) on a background thread; returns the thread."""
        if self.thread is not None and self.thread.is_alive():
            return None
        if self.module is None:
            self._write("Open a test module first.")
            return None
        session = self.session()
        if not session:
            self._write("No measurement is running: connect first.")
            return None
        module = self.reload()                          # the file as it is now
        if module is None:
            return None
        names = self.ticked() if names is None else list(names)
        if not names:
            self._write("Tick at least one test case.")
            return None
        bus, worker, config = session
        requests = ReceiveMailbox(bus, worker.message_sent.emit)
        frames = FrameMailbox(bus)                       # every frame of the run, for wait_for_frame
        worker.add_mailbox(requests)
        worker.add_mailbox(frames)
        transport = uds_transport(config)
        self.runner = Runner(module, make_request(requests, transport), frames.messages, requests.send, self.decode,
                             transport["timeout"], lambda kind, data: self.run_event.emit(kind, data),
                             config.get("name", ""), self.marker_requested.emit)
        for name in names:
            item = self._items[name]
            item.takeChildren()
            for column in (COL_VERDICT, COL_STEPS, COL_TIME):
                item.setText(column, "")
        self._write(f"{self.time_text(time.time())}  Running {len(names)} test case(s) of {module.title}")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.thread = threading.Thread(target=self._run, args=(self.runner, names, worker, (requests, frames)),
                                       daemon=True)
        self.thread.start()
        return self.thread

    def _run(self, runner, names, worker, mailboxes):
        report = None
        try:
            report = runner.run(names)
        finally:
            for mailbox in mailboxes:
                worker.remove_mailbox(mailbox)
                mailbox.close()
            self.run_finished.emit(report)

    def stop(self):
        if self.runner is not None:
            self.runner.stop()

    def _on_event(self, kind, data):
        if kind == "case":
            self._running_item = self._items.get(data.name)
            if self._running_item is not None:
                self._running_item.setText(COL_VERDICT, "running")
            self._write(f"  {data.title}")
        elif kind == "step" and self._running_item is not None:
            step = QTreeWidgetItem([step_text(data), data.verdict, "", f"{data.time:.3f}"])
            step.setForeground(COL_VERDICT, QColor(COLOURS.get(data.verdict, "#6b7280")))
            self._running_item.addChild(step)
            self._write(f"    {data.verdict.upper():4}  {step_text(data)}")
        elif kind == "verdict":
            item = self._items.get(data.name)
            if item is not None:
                self._show_verdict(item, data)
            self._write(f"  => {data.verdict}" + (f": {data.error.strip().splitlines()[-1]}" if data.error.strip() else ""))

    def _show_verdict(self, item, result):
        item.setText(COL_VERDICT, result.verdict)
        item.setForeground(COL_VERDICT, QColor(COLOURS.get(result.verdict, "#6b7280")))
        item.setText(COL_STEPS, str(len(result.steps)))
        item.setText(COL_TIME, f"{result.duration:.3f}")
        item.setExpanded(result.verdict != PASSED)
        if result.error.strip() and not item.childCount():
            item.addChild(QTreeWidgetItem([result.error.strip().splitlines()[-1], result.verdict, "", ""]))

    def _on_finished(self, report):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.runner = None
        self.report = report
        if report is None:
            self.status.setText("The run failed; see the log.")
            return
        for hook in (report.setup, report.teardown):
            if hook is not None and hook.verdict != PASSED:
                self._write(f"  {hook.title}: {hook.verdict}" + (f" - {hook.error.strip().splitlines()[-1]}"
                                                                 if hook.error.strip() else ""))
        try:
            self.report_paths = save_reports(report, Path(report.path).parent / "reports")
            where = f"   Report: {self.report_paths[0]}"
            self.report_btn.setEnabled(True)
        except OSError as exc:
            self.report_paths, where = None, f"   The report could not be written: {exc}"
        counts = report.counts()
        summary = (f"{report.verdict.upper()}: {counts['passed']} passed, {counts['failed']} failed, "
                   f"{counts['error']} error, {counts['skipped']} skipped in {report.duration:.1f} s")
        colour = COLOURS.get(report.verdict, "#6b7280")
        self.status.setText(f"<b style='color:{colour}'>{summary}</b>{where}")
        self._write(f"{summary}{'  (stopped)' if report.stopped else ''}{where}")

    def open_report(self):
        if self.report_paths:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.report_paths[0])))


class MemorySettings(dict):
    """Settings kept for this window only, where no QSettings is given."""

    def value(self, key, default=None, type=None):
        return self.get(key, default)

    def setValue(self, key, value):
        self[key] = value


def step_text(step) -> str:
    return f"{step.description}  -  {step.detail}" if step.detail else step.description
