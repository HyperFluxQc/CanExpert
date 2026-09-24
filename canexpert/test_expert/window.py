"""
TestExpert's window: load a description (CDD, ODX, PDX, JSON, or the Dummy ECU's), connect to the ECU, choose
the generated tests and run them; each run leaves an HTML and a JUnit report, and the traffic if asked.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import can
from PyQt5.QtCore import Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QKeySequence
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canexpert.can_bus import CanWorker, ReceiveMailbox, create_can_bus
from canexpert.config import read_configurations, uds_transport, validate_config
from canexpert.paths import APP_DIR, CONFIG_DIR, ODX_DIR
from canexpert.recording import Recorder
from canexpert.simulator.ecu import parse_channel
from canexpert.simulator.widgets import HexSpinBox
from canexpert.test_expert.description import EcuDescription
from canexpert.test_expert.dummy import dummy_description
from canexpert.test_expert.generator import Options, Suite
from canexpert.test_expert.odx import load_description
from canexpert.test_expert.tester import Tester
from canexpert.testing.report import COLOURS, save_reports
from canexpert.testing.runner import PASSED, Runner
from canexpert.testing.window import MemorySettings, step_text
from canexpert.ui_common import app_icon, app_settings, enable_maximize
from canexpert.uds.observer import SERVICE_NAMES
from canexpert.uds.seed_key import dll_key, xor_key

TEST_EXPERT_DIR = APP_DIR / "TestExpert"          # reports/ and the recordings of the runs
INTERFACES = ("kvaser", "vector", "ixxat", "pcan", "virtual", "socketcan")
BITRATES = ("125000", "250000", "500000", "1000000")
KEY_SOURCES = ("key = seed XOR mask", "seed & key DLL")
FILE_FILTER = "Diagnostic descriptions (*.cdd *.odx *.odx-d *.pdx *.json);;All files (*.*)"
PREFIX = "test_expert/"                           # the settings TestExpert keeps
COL_NAME, COL_VERDICT, COL_STEPS, COL_TIME = range(4)


def access_text(access) -> str:
    if access is None:
        return "no"
    sessions = ", ".join(f"{s:02X}" for s in sorted(access.sessions)) or "every session"
    levels = f", level {', '.join(f'{level:02X}' for level in sorted(access.levels))}" if access.levels else ""
    return sessions + levels


class TestExpertWindow(QMainWindow):
    run_event = pyqtSignal(str, object)
    run_finished = pyqtSignal(object)

    def __init__(self, settings=None):
        super().__init__()
        self.setWindowTitle("TestExpert")
        self.setWindowIcon(app_icon("test_expert"))
        enable_maximize(self)
        self.resize(1320, 860)
        self.settings = settings if settings is not None else app_settings()
        self.description: EcuDescription | None = None
        self.suite: Suite | None = None
        self.bus = self.worker = self.mailbox = None
        self.runner = self.thread = self.report = self.report_paths = self.recorder = None
        self._items = {}
        self._running_item = None
        self._build()
        self.run_event.connect(self._on_event)
        self.run_finished.connect(self._on_finished)
        self._restore()

    # --- settings ----------------------------------------------------------------------------------------

    def _value(self, key, default, kind=str):
        try:
            return self.settings.value(PREFIX + key, default, type=kind)
        except TypeError:                                  # MemorySettings takes no type
            value = self.settings.value(PREFIX + key, default)
            return kind(value) if value is not None else default

    def _remember(self, key, value):
        self.settings.setValue(PREFIX + key, value)

    def _restore(self):
        self.interface.setCurrentText(self._value("interface", "kvaser"))
        self.channel.setEditText(self._value("channel", "0"))
        self.bitrate.setCurrentText(self._value("bitrate", "500000"))
        for key, widget, default in (("request_id", self.request_id, 0x7E0), ("response_id", self.response_id, 0x7E8),
                                     ("functional_id", self.functional_id, 0x7DF), ("padding", self.padding, 0xCC),
                                     ("mask", self.mask, 0xA5)):
            widget.setValue(int(self._value(key, default, int)))
        self.extended.setChecked(self._value("extended", "false") in ("true", True))
        self.use_padding.setChecked(self._value("use_padding", "true") in ("true", True))
        self.dll_edit.setText(self._value("dll", ""))
        self.key_source.setCurrentIndex(int(self._value("key_source", 0, int)))
        path = Path(self._value("description", "") or "")
        if path.is_file():
            self.open_description(path)
        else:
            self.use_dummy()

    def _save_settings(self):
        for key, value in (("interface", self.interface.currentText()), ("channel", self.channel.currentText()),
                           ("bitrate", self.bitrate.currentText()), ("request_id", self.request_id.value()),
                           ("response_id", self.response_id.value()), ("functional_id", self.functional_id.value()),
                           ("padding", self.padding.value()), ("mask", self.mask.value()),
                           ("extended", "true" if self.extended.isChecked() else "false"),
                           ("use_padding", "true" if self.use_padding.isChecked() else "false"),
                           ("dll", self.dll_edit.text()), ("key_source", self.key_source.currentIndex())):
            self._remember(key, value)

    # --- UI ------------------------------------------------------------------------------------------------

    def _build(self):
        menu = self.menuBar().addMenu("&File")
        for text, slot, keys in (("&Open description...", lambda: self.open_description(), QKeySequence.Open),
                                 ("The &Dummy ECU", self.use_dummy, None),
                                 ("&Save description as JSON...", self.save_description, QKeySequence.Save),
                                 ("E&xit", self.close, QKeySequence("Ctrl+Q"))):
            action = menu.addAction(text)
            action.triggered.connect(lambda _checked=False, slot=slot: slot())
            if keys is not None:
                action.setShortcut(QKeySequence(keys))
        help_menu = self.menuBar().addMenu("&Help")
        manual = help_menu.addAction("TestExpert in the &manual")
        manual.setShortcut(QKeySequence.HelpContents)
        manual.triggered.connect(self.open_manual)
        help_menu.addAction("&About").triggered.connect(self.show_about)

        splitter = QSplitter(Qt.Horizontal)
        side = QTabWidget()
        side.addTab(self._description_tab(), "Description")
        side.addTab(self._connection_tab(), "ECU")
        side.addTab(self._options_tab(), "Settings")
        splitter.addWidget(side)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        bar = QHBoxLayout()
        self.run_btn = QPushButton("Run")
        self.run_btn.setToolTip("Run the ticked tests against the ECU")
        self.run_btn.clicked.connect(lambda: self.run())
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        self.report_btn = QPushButton("Open report")
        self.report_btn.setEnabled(False)
        self.report_btn.clicked.connect(self.open_report)
        for button in (self.run_btn, self.stop_btn, self.report_btn):
            bar.addWidget(button)
        self.status = QLabel("")
        bar.addWidget(self.status, 1)
        self.connection_label = QLabel("Not connected")
        self.connection_label.setStyleSheet("color: gray;")
        bar.addWidget(self.connection_label)
        right_layout.addLayout(bar)
        tests = QSplitter(Qt.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Test / step", "Verdict", "Steps", "Time (s)"])
        self.tree.setColumnWidth(COL_NAME, 520)
        self.tree.setColumnWidth(COL_VERDICT, 80)
        self.tree.setColumnWidth(COL_STEPS, 55)
        tests.addWidget(self.tree)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        tests.addWidget(self.log)
        tests.setSizes([540, 150])
        right_layout.addWidget(tests, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 900])
        self.setCentralWidget(splitter)

    def _description_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.description_label = QLabel("No description")
        self.description_label.setWordWrap(True)
        layout.addWidget(self.description_label)
        row = QHBoxLayout()
        for text, slot in (("Open...", lambda: self.open_description()), ("Dummy ECU", self.use_dummy),
                           ("Save as JSON...", self.save_description)):
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, slot=slot: slot())
            row.addWidget(button)
        layout.addLayout(row)
        self.description_tree = QTreeWidget()
        self.description_tree.setHeaderLabels(["Description", "Where"])
        self.description_tree.setColumnWidth(0, 220)
        layout.addWidget(self.description_tree, 1)
        return page

    def _connection_tab(self):
        page = QWidget()
        form = QFormLayout(page)
        self.interface = QComboBox()
        self.interface.addItems(INTERFACES)
        self.channel = QComboBox()
        self.channel.setEditable(True)
        detect = QPushButton("Detect")
        detect.setToolTip("List the channels of this interface")
        detect.clicked.connect(self.detect_channels)
        channel_row = QHBoxLayout()
        channel_row.addWidget(self.channel, 1)
        channel_row.addWidget(detect)
        self.bitrate = QComboBox()
        self.bitrate.setEditable(True)
        self.bitrate.addItems(BITRATES)
        self.request_id, self.response_id, self.functional_id = (HexSpinBox(0x1FFFFFFF) for _ in range(3))
        self.extended = QCheckBox("29-bit identifiers")
        self.use_padding = QCheckBox("Pad frames to 8 bytes with")
        self.padding = HexSpinBox(0xFF)
        padding_row = QHBoxLayout()
        padding_row.addWidget(self.use_padding)
        padding_row.addWidget(self.padding)
        padding_row.addStretch()
        self.config_combo = QComboBox()
        self.config_combo.addItem("(choose one)", None)
        configurations, _errors = read_configurations(CONFIG_DIR) if CONFIG_DIR.is_dir() else ([], [])
        for config in configurations:
            self.config_combo.addItem(config["name"], config)
        self.config_combo.setToolTip("Take the identifiers and the bit rate of a CAN Expert configuration")
        self.config_combo.currentIndexChanged.connect(self._use_configuration)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.toggle_connection)
        form.addRow("Interface", self.interface)
        form.addRow("Channel", channel_row)
        form.addRow("Bit rate", self.bitrate)
        form.addRow("Request ID", self.request_id)
        form.addRow("Response ID", self.response_id)
        form.addRow("Functional ID", self.functional_id)
        form.addRow("", self.extended)
        form.addRow("", padding_row)
        form.addRow("CAN Expert configuration", self.config_combo)
        form.addRow("", self.connect_btn)
        return page

    def _options_tab(self):
        page = QWidget()
        form = QFormLayout(page)
        self.destructive = QCheckBox("Destructive tests")
        self.destructive.setToolTip("ECU reset, clearing every DTC, writing DIDs (their own value back)")
        self.lockout = QCheckBox("Security lockout")
        self.lockout.setToolTip("Wrong keys until the ECU locks out, then its delay")
        self.functional = QCheckBox("Functional requests")
        self.functional.setChecked(True)
        self.record = QCheckBox("Record the traffic (.blf)")
        for box in (self.destructive, self.lockout, self.functional):
            box.toggled.connect(lambda _on: self.rebuild_tests())
        for box in (self.destructive, self.lockout, self.functional, self.record):
            form.addRow("", box)
        self.attempts = QSpinBox()
        self.attempts.setRange(1, 20)
        self.attempts.setValue(3)
        self.lockout_seconds = QDoubleSpinBox()
        self.lockout_seconds.setRange(0, 600)
        self.lockout_seconds.setValue(10)
        self.lockout_seconds.setSuffix(" s")
        self.reset_time = QDoubleSpinBox()
        self.reset_time.setRange(0, 60)
        self.reset_time.setValue(1.0)
        self.reset_time.setSuffix(" s")
        self.margin = QSpinBox()
        self.margin.setRange(0, 5000)
        self.margin.setValue(50)
        self.margin.setSuffix(" ms")
        form.addRow("Wrong keys before the lockout", self.attempts)
        form.addRow("Lockout delay", self.lockout_seconds)
        form.addRow("ECU reset time", self.reset_time)
        form.addRow("Margin over P2", self.margin)
        self.key_source = QComboBox()
        self.key_source.addItems(KEY_SOURCES)
        self.mask = HexSpinBox(0xFF)
        self.dll_edit = QLineEdit()
        self.dll_edit.setPlaceholderText("GenerateKeyEx DLL")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse_dll)
        dll_row = QHBoxLayout()
        dll_row.addWidget(self.dll_edit, 1)
        dll_row.addWidget(browse)
        self.variant = QLineEdit()
        form.addRow("SecurityAccess key", self.key_source)
        form.addRow("XOR mask", self.mask)
        form.addRow("Seed & key DLL", dll_row)
        form.addRow("DLL variant", self.variant)
        return page

    def _write(self, text):
        self.log.appendPlainText(f"{datetime.now().strftime('%H:%M:%S')}  {text}")

    # --- the description --------------------------------------------------------------------------------------

    def open_description(self, path=None):
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Diagnostic description", str(ODX_DIR), FILE_FILTER)
            if not path:
                return None
        try:
            description = load_description(path)
        except Exception as exc:                            # a file none of the loaders can read
            self._write(f"{Path(path).name} could not be read: {type(exc).__name__}: {exc}")
            return None
        self._remember("description", str(path))
        return self.set_description(description)

    def use_dummy(self):
        self._remember("description", "")
        return self.set_description(dummy_description())

    def set_description(self, description: EcuDescription):
        self.description = description
        self.description_label.setText(f"<b>{description.name}</b><br>{description.summary()}"
                                       f"<br><span style='color:gray'>{description.source}</span>")
        self._fill_description()
        self.rebuild_tests()
        for warning in description.warnings:
            self._write(f"Description: {warning}")
        return description

    def _fill_description(self):
        d = self.description
        tree = self.description_tree
        tree.clear()

        def branch(title, rows):
            item = QTreeWidgetItem([f"{title} ({len(rows)})", ""])
            for text, where in rows:
                item.addChild(QTreeWidgetItem([text, where]))
            tree.addTopLevelItem(item)
            return item
        branch("Sessions", [(f"{s.id:02X} {s.name}", "from " + ", ".join(f"{x:02X}" for x in sorted(s.entered_from))
                             if s.entered_from else "from any") for s in sorted(d.sessions.values(), key=lambda s: s.id)])
        branch("Security levels", [(f"{level:02X}/{level + 1:02X} {name}", "") for level, name in sorted(d.security_levels.items())])
        branch("Services", [(f"{sid:02X} {SERVICE_NAMES.get(sid, s.name)}"
                             + (f" [{', '.join(f'{sub:02X}' for sub in sorted(s.sub_functions))}]" if s.sub_functions else ""),
                             access_text(s.access)) for sid, s in sorted(d.services.items())])
        branch("DIDs", [(f"{did:04X} {e.name} ({e.length if e.length else '?'} bytes)",
                         f"read: {access_text(e.read)}; write: {access_text(e.write)}") for did, e in sorted(d.dids.items())])
        branch("Routines", [(f"{rid:04X} {r.name}", "; ".join(f"{sub:02X}: {access_text(a)}" for sub, a in sorted(r.sub_functions.items())))
                            for rid, r in sorted(d.routines.items())])
        if d.warnings:
            branch("Warnings", [(warning, "") for warning in d.warnings])
        tree.topLevelItem(2).setExpanded(True)

    def save_description(self):
        if self.description is None:
            return None
        path, _ = QFileDialog.getSaveFileName(self, "Save the description", f"{self.description.name}.json",
                                              "TestExpert description (*.json)")
        if not path:
            return None
        self.description.save(path)
        self._write(f"Description saved: {path} (edit it and open it again to change what is expected)")
        return path

    # --- the tests -------------------------------------------------------------------------------------------------

    def options(self) -> Options:
        return Options(destructive=self.destructive.isChecked(), lockout=self.lockout.isChecked(),
                       functional=self.functional.isChecked(), key=self._key_function(),
                       timing_margin_ms=self.margin.value(), reset_time=self.reset_time.value(),
                       attempts=self.attempts.value(), lockout_seconds=self.lockout_seconds.value())

    def _key_function(self):
        if self.key_source.currentIndex() == 0:
            function = xor_key(self.mask.value())
            return lambda level, seed: function(seed)
        path = self.dll_edit.text().strip()
        if not path:
            return None
        cache = {}

        def key(level, seed):
            if level not in cache:
                cache[level] = dll_key(path, level, self.variant.text().strip())
            return cache[level](seed)
        return key

    def _browse_dll(self):
        path, _ = QFileDialog.getOpenFileName(self, "Seed & key DLL", "", "DLL (*.dll);;All files (*.*)")
        if path:
            self.dll_edit.setText(path)
            self.key_source.setCurrentIndex(1)

    def rebuild_tests(self):
        """The generated tests for the description and the options; what was unticked stays unticked."""
        if self.description is None:
            return
        unticked = {name for name, item in self._items.items() if item.checkState(COL_NAME) == Qt.Unchecked}
        self.suite = Suite(self.description, self.options())
        self.tree.clear()
        self._items = {}
        for group, cases in self.suite.groups().items():
            parent = QTreeWidgetItem([f"{group} ({len(cases)})", "", "", ""])
            parent.setFlags(parent.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            self.tree.addTopLevelItem(parent)
            for case in cases:
                item = QTreeWidgetItem([case.title.split(": ", 1)[1], "", "", ""])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(COL_NAME, Qt.Unchecked if case.name in unticked else Qt.Checked)
                if case.doc:
                    item.setToolTip(COL_NAME, case.doc)
                parent.addChild(item)
                self._items[case.name] = item
        self.status.setText(f"{len(self.suite.cases)} tests")

    def ticked(self) -> list[str]:
        return [name for name, item in self._items.items() if item.checkState(COL_NAME) == Qt.Checked]

    # --- the connection ----------------------------------------------------------------------------------------

    def detect_channels(self):
        interface = self.interface.currentText()
        self.channel.clear()
        try:
            found = can.detect_available_configs(interfaces=[interface])
        except Exception as exc:                            # a driver that is not installed
            self._write(f"{interface}: {exc}")
            found = []
        for config in found:
            self.channel.addItem(str(config.get("channel", "")))
        if not found:
            self.channel.setEditText("0" if interface != "virtual" else "test_expert")
        self._write(f"{interface}: {len(found)} channel(s)")

    def _use_configuration(self, index):
        config = self.config_combo.itemData(index)
        if not config:
            return
        transport = uds_transport(validate_config(config))
        self.request_id.setValue(transport["request_id"])
        self.response_id.setValue(transport["response_id"])
        self.extended.setChecked(transport["extended"])
        self.bitrate.setCurrentText(str(config.get("bitrate", 500000)))
        self._write(f"Identifiers of the CAN Expert configuration {config['name']}")

    def transport(self) -> dict:
        return {"request_id": self.request_id.value(), "response_id": self.response_id.value(), "timeout": 2.0,
                "extended": self.extended.isChecked(), "address_byte": None,
                "padding": self.padding.value() if self.use_padding.isChecked() else None, "block_size": 0,
                "st_min": 0}

    def toggle_connection(self):
        return self.disconnect_ecu() if self.bus is not None else self.connect_ecu()

    def connect_ecu(self, bus=None):
        """Open the channel (or use bus, the tests' virtual one) with a CAN worker that reads it."""
        if self.bus is not None:
            return True
        try:
            bitrate = int(self.bitrate.currentText())
            self.bus = bus or create_can_bus(self.interface.currentText(), parse_channel(self.channel.currentText()),
                                             bitrate)
        except Exception as exc:
            QMessageBox.warning(self, "TestExpert", f"Cannot open the channel:\n{exc}")
            return False
        config = validate_config({"name": "TestExpert", "request_id": self.request_id.value(),
                                  "response_id": self.response_id.value(),
                                  "identifier_11_bit": not self.extended.isChecked()})
        self.worker = CanWorker(self.bus, config, tester_present=False)
        self.worker.message_received.connect(self._record_received)
        self.worker.message_sent.connect(self._record_sent)
        self.mailbox = ReceiveMailbox(self.bus, self.worker.message_sent.emit)
        self.worker.add_mailbox(self.mailbox)
        self.worker.start()
        self._save_settings()
        self.connect_btn.setText("Disconnect")
        self.connection_label.setText(f"Connected: {self.interface.currentText()} {self.channel.currentText()}")
        self._write(f"Connected to {self.interface.currentText()} {self.channel.currentText()}")
        return True

    def disconnect_ecu(self):
        self.stop()
        if self.thread is not None:
            self.thread.join(5)
        if self.worker is not None:
            self.worker.stop()
        if self.bus is not None:
            try:
                self.bus.shutdown()
            except Exception:
                pass
        self.bus = self.worker = self.mailbox = None
        self.connect_btn.setText("Connect")
        self.connection_label.setText("Not connected")

    def _record_received(self, message):
        if self.recorder is not None:
            self.recorder.write(message["timestamp"], "RX", message["arbitration_id"], bytes(message["data"]),
                                message.get("is_extended_frame", False))

    def _record_sent(self, can_id, data):
        if self.recorder is not None:
            self.recorder.write(time.time(), "TX", can_id, bytes(data), can_id > 0x7FF)

    # --- running ---------------------------------------------------------------------------------------------

    def run(self, names=None):
        if self.thread is not None and self.thread.is_alive():
            return None
        if self.mailbox is None:
            self._write("Connect to the ECU first.")
            return None
        self.rebuild_tests()
        names = self.ticked() if names is None else list(names)
        if not names:
            self._write("Tick at least one test.")
            return None
        TEST_EXPERT_DIR.mkdir(parents=True, exist_ok=True)
        self.suite.tester = Tester(self.mailbox, self.transport(), self.functional_id.value())
        configuration = (f"{self.interface.currentText()} {self.channel.currentText()}, {self.bitrate.currentText()} "
                         f"bit/s, {self.request_id.value():X}/{self.response_id.value():X}")
        self.runner = Runner(self.suite.module(), on_event=lambda kind, data: self.run_event.emit(kind, data),
                             configuration=configuration)
        if self.record.isChecked():
            folder = TEST_EXPERT_DIR / "reports"
            folder.mkdir(parents=True, exist_ok=True)
            self.recorder = Recorder(folder / f"traffic_{datetime.now().strftime('%Y%m%d-%H%M%S')}.blf")
        for name in names:
            item = self._items[name]
            item.takeChildren()
            for column in (COL_VERDICT, COL_STEPS, COL_TIME):
                item.setText(column, "")
        self._write(f"Running {len(names)} tests of {self.description.name}")
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.thread = threading.Thread(target=self._run, args=(self.runner, names), daemon=True)
        self.thread.start()
        return self.thread

    def _run(self, runner, names):
        report = None
        try:
            report = runner.run(names)
        finally:
            self.run_finished.emit(report)

    def stop(self):
        if self.runner is not None:
            self.runner.stop()

    def _on_event(self, kind, data):
        if kind == "case":
            self._running_item = self._items.get(data.name)
            if self._running_item is not None:
                self._running_item.setText(COL_VERDICT, "running")
                self.tree.scrollToItem(self._running_item)
        elif kind == "step" and self._running_item is not None:
            step = QTreeWidgetItem([step_text(data), data.verdict, "", f"{data.time:.3f}"])
            step.setForeground(COL_VERDICT, QColor(COLOURS.get(data.verdict, "#6b7280")))
            self._running_item.addChild(step)
        elif kind == "verdict":
            item = self._items.get(data.name)
            if item is not None:
                item.setText(COL_VERDICT, data.verdict)
                item.setForeground(COL_VERDICT, QColor(COLOURS.get(data.verdict, "#6b7280")))
                item.setText(COL_STEPS, str(len(data.steps)))
                item.setText(COL_TIME, f"{data.duration:.3f}")
                item.setExpanded(data.verdict != PASSED)
                if data.error.strip() and data.verdict != PASSED:
                    item.setToolTip(COL_VERDICT, data.error.strip().splitlines()[-1])

    def _on_finished(self, report):
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.runner = None
        self.report = report
        if self.recorder is not None:
            recording = self.recorder.path
            self.recorder.stop()
            self.recorder = None
            self._write(f"Traffic recorded: {recording}")
        if report is None:
            self.status.setText("The run failed; see the log.")
            return
        try:
            self.report_paths = save_reports(report, TEST_EXPERT_DIR / "reports")
            self.report_btn.setEnabled(True)
            where = f"   Report: {self.report_paths[0]}"
        except OSError as exc:
            self.report_paths, where = None, f"   The report could not be written: {exc}"
        counts = report.counts()
        summary = (f"{report.verdict.upper()}: {counts['passed']} passed, {counts['failed']} failed, "
                   f"{counts['error']} error, {counts['skipped']} skipped in {report.duration:.1f} s")
        self.status.setText(f"<b style='color:{COLOURS.get(report.verdict, '#6b7280')}'>{summary}</b>")
        self._write(summary + where)

    def open_report(self):
        if self.report_paths:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.report_paths[0])))

    # --- help --------------------------------------------------------------------------------------------------

    def open_manual(self):
        from canexpert.help_window import show_manual
        window = show_manual(self)
        window.go_to_section("TestExpert")
        return window

    def show_about(self):
        from canexpert.about import AboutDialog
        dialog = AboutDialog(self)
        dialog.setWindowTitle("About TestExpert")
        dialog.exec_()
        return dialog

    def closeEvent(self, event):
        self.disconnect_ecu()
        super().closeEvent(event)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="TestExpert: UDS conformance tests from a CDD, ODX or PDX file")
    parser.add_argument("description", nargs="?", help="a .cdd, .odx, .pdx or .json description to open")
    parser.add_argument("--smoke-test", action="store_true", help="build the window and exit (the Windows build)")
    arguments = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setWindowIcon(app_icon("test_expert"))
    window = TestExpertWindow(MemorySettings() if arguments.smoke_test else None)
    if arguments.description:
        window.open_description(arguments.description)
    if arguments.smoke_test:
        print("startup ok" if window.suite is not None else "no tests")
        return 0 if window.suite is not None else 1
    window.show()
    return app.exec_()

