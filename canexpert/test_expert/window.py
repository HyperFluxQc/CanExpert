"""
TestExpert's window: load a description (CDD, ODX, PDX, JSON, or the Dummy ECU's), connect to the ECU, choose
the generated tests and the sequences around them, and run them; each run leaves an HTML and a JUnit report,
and the traffic if asked. What the window holds is a test plan (plan.py): saved to a file, it runs again from
here or from the command line (cli.py); the window also keeps it for its next start.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import can
from PyQt5.QtCore import QEvent, QSize, Qt, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QDesktopServices, QKeySequence
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QToolBar,
    QToolButton,
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
from canexpert.test_expert.compare import (ResultsError, compare_runs, comparison_html, comparison_page,
                                           load_results, previous_results)
from canexpert.test_expert.description import EcuDescription
from canexpert.test_expert.discovery import Discovery, compare, discovery_html, discovery_page
from canexpert.test_expert.discovery_view import DiscoveryDialog, DiscoveryView
from canexpert.test_expert.dummy import dummy_description
from canexpert.test_expert.engine import PlanRun
from canexpert.test_expert.generator import Options, Suite, parse_routine_starts
from canexpert.test_expert.odx import load_description
from canexpert.test_expert.plan import Connection, KeySource, PlanError, TestPlan, is_plan_file, options_dict
from canexpert.test_expert.policy import Deviation, today
from canexpert.test_expert.policy_editor import PolicyEditor
from canexpert.test_expert.sequence_editor import SequenceEditor
from canexpert.test_expert.sequences import PRESETS, Attachment
from canexpert.test_expert.tester import Tester
from canexpert.test_expert.variant_dialog import IdentificationDialog
from canexpert.test_expert.variants import Identification, identify, is_odx
from canexpert.testing.report import COLOURS, summary_text
from canexpert.testing.runner import INFO, PASS, PASSED
from canexpert.testing.window import MemorySettings, step_text
from canexpert.ui_common import app_icon, app_settings, enable_maximize, is_dark_theme, toolbar_icon
from canexpert.uds.observer import SERVICE_NAMES

TEST_EXPERT_DIR = APP_DIR / "TestExpert"          # reports/ and the recordings of the runs
INTERFACES = ("kvaser", "vector", "ixxat", "pcan", "virtual", "socketcan")
BITRATES = ("125000", "250000", "500000", "1000000")
KEY_SOURCES = ("key = seed XOR mask", "seed & key DLL")
FILE_FILTER = "Diagnostic descriptions (*.cdd *.odx *.odx-d *.pdx *.json);;All files (*.*)"
PLAN_FILTER = "TestExpert plans (*.json);;All files (*.*)"
PREFIX = "test_expert/"                           # the settings TestExpert keeps
COL_NAME, COL_VERDICT, COL_STEPS, COL_TIME, COL_SEQUENCES = range(5)
# The toolbar: action name -> (label, icon, what it does); None: a separator.
TOOLBAR = (
    ("open", "Description", "open", "Open a description: a CDD, an ODX or PDX file, or a JSON description"),
    ("open_plan", "Open plan", "plan", "Open a test plan"),
    ("save_plan", "Save plan", "save", "Save the test plan: the description, the ECU, the settings, the tests left "
                                      "out, the sequences and the deviations"),
    None,
    ("connect", "Connect", "connect", "Open the channel of the ECU tab"),
    None,
    ("run", "Run", "run", "Run the ticked tests against the ECU"),
    ("stop", "Stop", "stop", "Stop after the current step (the clean-up still runs)"),
    None,
    ("discover", "Discover", "discover", "Ask the ECU what services, DIDs, routines and security levels it has, and "
                                         "compare them with the description"),
    ("compare", "Compare", "compare", "Compare two runs: regressions, fixes, other answers"),
    ("report", "Report", "report", "Open the last run's HTML report"),
    None,
    ("manual", "Manual", "manual", "TestExpert in the manual"),
)
SHORTCUTS = {"open": QKeySequence.Open, "open_plan": "Ctrl+Shift+O", "save_plan": QKeySequence.Save, "run": "F5",
             "stop": "Shift+F5", "manual": QKeySequence.HelpContents}
TARGET = Qt.UserRole        # a tests tree item's ("test", name), ("group", name) or ("step", test name, description)


def access_text(access) -> str:
    if access is None:
        return "no"
    sessions = ", ".join(f"{s:02X}" for s in sorted(access.sessions)) or "every session"
    levels = f", level {', '.join(f'{level:02X}' for level in sorted(access.levels))}" if access.levels else ""
    return sessions + levels


def field_text(item) -> str:
    """What a DID's field may hold: its valid values or text table, its scale and unit."""
    if item.texts:
        return ", ".join(f"{value}: {text}" for value, text in sorted(item.texts.items()))
    parts = []
    if item.valid:
        parts.append(", ".join(item.shown(low) if low == high else f"{item.shown(low)} to {item.shown(high)}"
                               for low, high in item.valid))
    elif item.numeric and ((item.scale, item.shift) != (1.0, 0.0) or item.unit):
        parts.append(f"x {item.scale:g} + {item.shift:g} {item.unit}".strip())
    return "; ".join(parts) or ("text" if item.encoding == "ascii" else "")


class TestExpertWindow(QMainWindow):
    run_event = pyqtSignal(str, object)
    run_finished = pyqtSignal(object)
    discovery_progress = pyqtSignal(int, int, str)
    discovery_finished = pyqtSignal(object)

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
        self.plan_path: Path | None = None           # the plan file the window's plan was read from or saved to
        self.identification = Identification()        # how the variants are told apart, where the file does not say
        self.discovery_options = TestPlan().discovery
        self.comparison = None
        self.discovery_result = None
        self._discovery_stop = threading.Event()
        self._report_folder = TEST_EXPERT_DIR / "reports"
        self._description_path = ""                   # the description's file; "": the Dummy ECU's
        self._items = {}
        self._group_items = {}
        self._running_item = None                     # the tests tree item of the test running
        self._cases_started = False
        self._hook_items = {}                         # "setup"/"teardown" -> the item of the run's own steps
        self._build()
        self.run_event.connect(self._on_event)
        self.run_finished.connect(self._on_finished)
        self.discovery_progress.connect(self._on_discovery_progress)
        self.discovery_finished.connect(self._on_discovery_finished)
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
        """The plan the window held when it was last closed, saved or not; else a new one."""
        plan = TestPlan()
        try:
            state = json.loads(self._value("plan_state", "") or "null")
            if state is not None:
                plan = TestPlan.from_dict(state)
        except (TypeError, ValueError):
            self._write("The plan kept from last time could not be read; a new one is used")
        path = Path(self._value("plan", "") or "")
        plan.path = path if path.is_file() else None
        self.apply_plan(plan)

    def _save_settings(self):
        """Keep the window's plan for the next start (with absolute paths: it is not in the plan's file)."""
        self._remember("plan_state", json.dumps(self.plan(portable=False).to_dict()))
        self._remember("plan", str(self.plan_path or ""))

    def _save_sequences(self):
        self._save_settings()

    # --- UI ------------------------------------------------------------------------------------------------

    def _build(self):
        self._build_actions()
        menu = self.menuBar().addMenu("&File")
        for entry in ("open", ("The &Dummy ECU", self.use_dummy, None),
                      ("Save description as &JSON...", self.save_description, None), None,
                      ("&New plan", self.new_plan, QKeySequence.New), "open_plan", "save_plan",
                      ("Save plan &as...", lambda: self.save_plan_as(), QKeySequence("Ctrl+Shift+S")), None,
                      ("E&xit", self.close, QKeySequence("Ctrl+Q"))):
            self._menu_entry(menu, entry)
        run_menu = self.menuBar().addMenu("&Run")
        for entry in ("connect", None, "run", "stop", None, "discover", "compare", "report"):
            self._menu_entry(run_menu, entry)
        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(self.actions["manual"])
        help_menu.addAction("&About").triggered.connect(self.show_about)
        self._build_toolbar()

        splitter = QSplitter(Qt.Horizontal)
        side = QTabWidget()
        side.addTab(self._description_tab(), "Description")
        side.addTab(self._connection_tab(), "ECU")
        side.addTab(self._options_tab(), "Settings")
        self.sequence_editor = SequenceEditor()
        self.sequence_editor.changed.connect(self._sequences_changed)
        side.addTab(self.sequence_editor, "Sequences")
        self.policy_editor = PolicyEditor()
        self.policy_editor.changed.connect(self._save_settings)
        side.addTab(self.policy_editor, "Deviations")
        self.side = side
        splitter.addWidget(side)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        bar = QHBoxLayout()
        self.status = QLabel("")
        bar.addWidget(self.status, 1)
        self.connection_label = QLabel("Not connected")
        self.connection_label.setStyleSheet("color: gray;")
        bar.addWidget(self.connection_label)
        right_layout.addLayout(bar)
        tests = QSplitter(Qt.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Test / step", "Verdict", "Steps", "Time (s)", "Sequences"])
        self.tree.setColumnWidth(COL_NAME, 480)
        self.tree.setColumnWidth(COL_VERDICT, 80)
        self.tree.setColumnWidth(COL_STEPS, 55)
        self.tree.setColumnWidth(COL_TIME, 70)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.results = QTabWidget()
        self.results.addTab(self.tree, "Tests")
        self.coverage_view = QTextBrowser()
        self.coverage_view.setPlaceholderText("After a run: where each service, DID and routine was checked")
        self.results.addTab(self.coverage_view, "Coverage")
        self.discovery_view = DiscoveryView()
        self.discovery_view.use_requested.connect(lambda: self.use_discovered())
        self.discovery_view.save_requested.connect(lambda: self.save_discovery())
        self.results.addTab(self.discovery_view, "Discovery")
        comparison = QWidget()
        comparison_layout = QVBoxLayout(comparison)
        comparison_layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        choose = QPushButton("Compare two runs...")
        choose.clicked.connect(lambda: self.compare_runs())
        self.save_comparison_btn = QPushButton("Save...")
        self.save_comparison_btn.setEnabled(False)
        self.save_comparison_btn.clicked.connect(lambda: self.save_comparison())
        row.addWidget(choose)
        row.addWidget(self.save_comparison_btn)
        row.addStretch()
        comparison_layout.addLayout(row)
        self.comparison_view = QTextBrowser()
        self.comparison_view.setPlaceholderText("After a run: what changed since the last run of the same "
                                                "description - or compare any two runs")
        comparison_layout.addWidget(self.comparison_view, 1)
        self.comparison_tab = comparison
        self.results.addTab(comparison, "Comparison")
        tests.addWidget(self.results)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        tests.addWidget(self.log)
        tests.setSizes([540, 150])
        right_layout.addWidget(tests, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([470, 850])
        self.setCentralWidget(splitter)

    def _build_actions(self):
        """The actions of the toolbar, which the menus and the ECU tab share."""
        slots = {"open": lambda: self.open_description(), "open_plan": lambda: self.open_plan(),
                 "save_plan": self.save_plan, "connect": self.toggle_connection, "run": lambda: self.run(),
                 "stop": self.stop, "discover": lambda: self.discover(), "compare": lambda: self.compare_runs(),
                 "report": self.open_report, "manual": self.open_manual}
        menu_texts = {"open": "&Open description...", "open_plan": "Open &plan...", "save_plan": "&Save plan",
                      "connect": "&Connect", "run": "&Run the ticked tests", "stop": "S&top",
                      "discover": "&Discover the ECU...", "compare": "Co&mpare two runs...",
                      "report": "Open the &report", "manual": "TestExpert in the &manual"}
        self.actions = {}
        self._icons = {}
        for entry in TOOLBAR:
            if entry is None:
                continue
            name, label, icon, tip = entry
            action = QAction(label, self)
            action.setIconText(label)
            action.setData(menu_texts[name])
            action.triggered.connect(lambda _checked=False, slot=slots[name]: slot())
            if name in SHORTCUTS:
                action.setShortcut(QKeySequence(SHORTCUTS[name]))
                tip = f"{tip}\nShortcut: {action.shortcut().toString(QKeySequence.NativeText)}"
            action.setToolTip(tip)
            action.setStatusTip(tip.splitlines()[0])
            self.actions[name] = action
            self._icons[name] = icon
        for name in ("stop", "report"):
            self.actions[name].setEnabled(False)
        self.run_action, self.stop_action = self.actions["run"], self.actions["stop"]
        self.report_action, self.discover_action = self.actions["report"], self.actions["discover"]
        self.connect_action = self.actions["connect"]
        self._refresh_icons()

    def _menu_entry(self, menu, entry):
        """A toolbar action by name (with its menu text), (text, slot, keys), or None for a separator."""
        if entry is None:
            menu.addSeparator()
        elif isinstance(entry, str):
            action = self.actions[entry]
            item = menu.addAction(action.icon(), action.data())
            item.setShortcut(action.shortcut())
            item.setShortcutContext(Qt.WidgetShortcut)     # the toolbar's action owns the key
            item.setToolTip(action.toolTip())
            item.triggered.connect(action.trigger)
            action.changed.connect(lambda item=item, action=action: self._follow(item, action))
        else:
            text, slot, keys = entry
            item = menu.addAction(text)
            item.triggered.connect(lambda _checked=False, slot=slot: slot())
            if keys is not None:
                item.setShortcut(QKeySequence(keys))

    @staticmethod
    def _follow(item, action):
        """A menu entry follows its toolbar action: enabled, icon, text."""
        item.setEnabled(action.isEnabled())
        item.setIcon(action.icon())
        if action is not None and action.data() and item.text() != action.data():
            item.setText(action.data())

    def _build_toolbar(self):
        toolbar = QToolBar("TestExpert", self)
        toolbar.setObjectName("test_expert_toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(28, 28))
        toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        toolbar.setStyleSheet("""
            QToolBar { spacing: 4px; padding: 4px 6px; border: none; }
            QToolBar QToolButton { padding: 5px 8px; border: 1px solid transparent; border-radius: 6px; }
            QToolBar QToolButton:hover { background: palette(midlight); }
            QToolBar QToolButton:pressed { background: palette(mid); }
        """)
        self.toolbar = toolbar
        for entry in TOOLBAR:
            if entry is None:
                toolbar.addSeparator()
                continue
            button = QToolButton()
            button.setDefaultAction(self.actions[entry[0]])
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(28, 28))
            button.setMinimumSize(76, 62)
            button.setAccessibleName(entry[1])
            toolbar.addWidget(button)
        self.addToolBar(toolbar)

    def _refresh_icons(self):
        """The toolbar's symbols in the colours of the theme in use (light or dark)."""
        dark = is_dark_theme(self)
        connected = getattr(self, "bus", None) is not None
        for name, action in self.actions.items():
            icon = "disconnect" if name == "connect" and connected else self._icons[name]
            action.setIcon(toolbar_icon(icon, dark))

    def changeEvent(self, event):
        if event.type() == QEvent.PaletteChange and hasattr(self, "actions"):
            self._refresh_icons()
        super().changeEvent(event)

    def _show_connection(self, connected: bool):
        action = self.connect_action
        action.setText("Disconnect" if connected else "Connect")
        action.setIconText(action.text())
        action.setData("&Disconnect" if connected else "&Connect")
        action.setToolTip("Close the channel" if connected else "Open the channel of the ECU tab")
        self._refresh_icons()

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
        self.variant_box = QWidget()
        variant_layout = QVBoxLayout(self.variant_box)
        variant_layout.setContentsMargins(0, 0, 0, 0)
        choose = QHBoxLayout()
        choose.addWidget(QLabel("Variant"))
        self.variant_combo = QComboBox()
        self.variant_combo.setToolTip("The file has several variants: the one to test")
        self.variant_combo.activated.connect(lambda _index: self.choose_variant(self.variant_combo.currentText()))
        choose.addWidget(self.variant_combo, 1)
        self.identify_btn = QPushButton("Identify")
        self.identify_btn.setToolTip("Ask the ECU which variant it is (connect first)")
        self.identify_btn.clicked.connect(lambda: self.identify_variant())
        choose.addWidget(self.identify_btn)
        variant_layout.addLayout(choose)
        told = QHBoxLayout()
        self.identify_box = QCheckBox("Identify it when connecting")
        self.identify_box.toggled.connect(lambda _on: self._save_settings())
        told.addWidget(self.identify_box)
        told.addStretch()
        self.told_btn = QPushButton("Told apart by...")
        self.told_btn.setToolTip("The DID that tells the variants apart, and each variant's value (for a CDD or a "
                                 "JSON description; an ODX file says it itself)")
        self.told_btn.clicked.connect(lambda: self.edit_identification())
        told.addWidget(self.told_btn)
        variant_layout.addLayout(told)
        self.variant_box.setVisible(False)
        layout.addWidget(self.variant_box)
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
        self.connect_btn = QToolButton()
        self.connect_btn.setDefaultAction(self.connect_action)
        self.connect_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.connect_btn.setIconSize(QSize(18, 18))
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
        self.plan_name = QLineEdit()
        self.plan_name.setPlaceholderText("the plan's file name")
        form.addRow("Plan name", self.plan_name)
        self.destructive = QCheckBox("Destructive tests")
        self.destructive.setToolTip("ECU reset, clearing every DTC, writing DIDs (their own value back, their "
                                    "limits and a value out of them)")
        self.lockout = QCheckBox("Security lockout")
        self.lockout.setToolTip("Wrong keys until the ECU locks out, then its delay")
        self.functional = QCheckBox("Functional requests")
        self.functional.setChecked(True)
        self.s3_test = QCheckBox("S3 session timeout")
        self.s3_test.setChecked(True)
        self.s3_test.setToolTip("A session ends after S3 without requests, and TesterPresent keeps it (a few seconds)")
        self.record = QCheckBox("Record the traffic (.blf)")
        for box in (self.destructive, self.lockout, self.functional, self.s3_test):
            box.toggled.connect(lambda _on: self.rebuild_tests())
        for box in (self.destructive, self.lockout, self.functional, self.s3_test, self.record):
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
        self.s3_seconds = QDoubleSpinBox()
        self.s3_seconds.setRange(0.5, 60)
        self.s3_seconds.setValue(5.0)
        self.s3_seconds.setSuffix(" s")
        self.s3_seconds.setToolTip("S3server: ISO 14229-2 fixes it at 5 s")
        self.start_routines = QLineEdit()
        self.start_routines.setPlaceholderText("none: routines are not started")
        self.start_routines.setToolTip("Routines a run may start, with the option record each is started with: "
                                       "0201; FF00: 44 00 01 00 00 00 00 00 04")
        self.start_routines.editingFinished.connect(self.rebuild_tests)
        self.start_routines.textChanged.connect(self._check_routine_starts)
        form.addRow("ECU reset time", self.reset_time)
        form.addRow("Margin over P2", self.margin)
        form.addRow("S3", self.s3_seconds)
        form.addRow("Routines to start", self.start_routines)
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
        self.reports_edit = QLineEdit()
        self.reports_edit.setPlaceholderText("TestExpert/reports")
        reports_browse = QPushButton("Browse...")
        reports_browse.clicked.connect(self._browse_reports)
        reports_row = QHBoxLayout()
        reports_row.addWidget(self.reports_edit, 1)
        reports_row.addWidget(reports_browse)
        form.addRow("Reports folder", reports_row)
        return page

    def _write(self, text):
        self.log.appendPlainText(f"{datetime.now().strftime('%H:%M:%S')}  {text}")

    # --- the description --------------------------------------------------------------------------------------

    def open_description(self, path=None, variant=None):
        """Read a description file (asked for when not given); variant: the one to read (default: the first)."""
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Diagnostic description", str(ODX_DIR), FILE_FILTER)
            if not path:
                return None
        try:
            description = load_description(path, variant or None)
        except Exception as exc:                            # a file none of the loaders can read
            self._write(f"{Path(path).name} could not be read: {type(exc).__name__}: {exc}")
            return None
        self._description_path = str(Path(path).resolve())
        self.set_description(description)
        self._save_settings()
        return description

    # --- variants ----------------------------------------------------------------------------------------------

    def choose_variant(self, variant):
        """Read the description's file again, as that variant."""
        if not self._description_path or variant == (self.description.variant if self.description else ""):
            return self.description
        self._write(f"Variant {variant}")
        return self.open_description(self._description_path, variant)

    def identify_variant(self):
        """Ask the ECU which variant it is, and test it as that one."""
        if self.description is None or len(self.description.variants) < 2:
            return None
        if self.mailbox is None:
            self._write("Connect to the ECU first.")
            return None
        if not is_odx(self._description_path) and (self.identification.did is None or not self.identification.values):
            if not self.edit_identification():
                return None
        plan = self.plan()
        tester = Tester(self.mailbox, plan.connection.transport(), plan.connection.functional_id)
        variant, detail = identify(self._description_path, tester, self.identification)
        if variant is None:
            self._write(f"The variant could not be told: {detail}")
            self.status.setText("The variant could not be told; see the log.")
            return None
        self._write(f"The ECU is the variant {variant}: {detail}")
        self.choose_variant(variant)
        return variant

    def edit_identification(self):
        dialog = IdentificationDialog(self.description.variants if self.description else [], self.identification, self)
        if not dialog.exec_():
            return None
        self.identification = dialog.identification()
        self._save_settings()
        return self.identification

    def use_dummy(self):
        self._description_path = ""
        self.set_description(dummy_description())
        self._save_settings()
        return self.description

    def set_description(self, description: EcuDescription):
        self.description = description
        self.variant_combo.blockSignals(True)
        self.variant_combo.clear()
        self.variant_combo.addItems(description.variants)
        self.variant_combo.setCurrentText(description.variant)
        self.variant_combo.blockSignals(False)
        self.variant_box.setVisible(len(description.variants) > 1)
        self.told_btn.setVisible(not is_odx(self._description_path))
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
        dids = branch("DIDs", [(f"{did:04X} {e.name} ({e.length if e.length else '?'} bytes)",
                                f"read: {access_text(e.read)}; write: {access_text(e.write)}")
                               for did, e in sorted(d.dids.items())])
        for index, entry in enumerate(e for _did, e in sorted(d.dids.items())):
            for item in entry.fields:
                dids.child(index).addChild(QTreeWidgetItem([f"{item.name} ({item.bits} bits, {item.encoding})",
                                                            field_text(item)]))
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
        return self.plan().make_options()

    def _routine_starts(self) -> str:
        """The routines to start as typed, or "" while they cannot be read."""
        text = self.start_routines.text().strip()
        try:
            parse_routine_starts(text)
        except ValueError:
            return ""
        return text

    def _check_routine_starts(self, text):
        try:
            parse_routine_starts(text)
            self.start_routines.setStyleSheet("")
            self.start_routines.setToolTip("Routines a run may start, with the option record each is started with: "
                                           "0201; FF00: 44 00 01 00 00 00 00 00 04")
        except ValueError as exc:
            self.start_routines.setStyleSheet("background: #fecaca;")
            self.start_routines.setToolTip(str(exc))

    def _browse_dll(self):
        path, _ = QFileDialog.getOpenFileName(self, "Seed & key DLL", "", "DLL (*.dll);;All files (*.*)")
        if path:
            self.dll_edit.setText(path)
            self.key_source.setCurrentIndex(1)

    def _browse_reports(self):
        path = QFileDialog.getExistingDirectory(self, "Reports folder", self.reports_edit.text() or str(TEST_EXPERT_DIR))
        if path:
            self.reports_edit.setText(path)

    # --- the plan --------------------------------------------------------------------------------------------

    def plan(self, path=None, portable=True) -> TestPlan:
        """What the window holds, as a test plan; path: where it is to be saved. A portable plan writes its
        paths relative to that folder; otherwise they stay absolute (the plan kept in the settings)."""
        target = Path(path) if path is not None else self.plan_path
        plan = TestPlan(path=target if portable else None)
        plan.name = self.plan_name.text().strip()
        plan.description = plan.relative(self._description_path) if self._description_path else ""
        plan.variant = self.description.variant if self.description and len(self.description.variants) > 1 else ""
        plan.identify = self.identify_box.isChecked()
        plan.identification = Identification(self.identification.did, dict(self.identification.values))
        try:
            bitrate = int(self.bitrate.currentText())
        except ValueError:                                  # a bit rate being typed
            bitrate = 500000
        plan.connection = Connection(self.interface.currentText(), self.channel.currentText().strip(),
                                     bitrate, self.request_id.value(),
                                     self.response_id.value(), self.functional_id.value(), self.extended.isChecked(),
                                     self.padding.value() if self.use_padding.isChecked() else None)
        plan.options = {"destructive": self.destructive.isChecked(), "lockout": self.lockout.isChecked(),
                        "functional": self.functional.isChecked(), "timing_margin_ms": self.margin.value(),
                        "reset_time": self.reset_time.value(), "attempts": self.attempts.value(),
                        "lockout_seconds": self.lockout_seconds.value(), "s3_test": self.s3_test.isChecked(),
                        "s3_seconds": self.s3_seconds.value(), "start_routines": self._routine_starts()}
        plan.options = {**options_dict(Options()), **plan.options}
        dll = self.dll_edit.text().strip()
        plan.key = KeySource("dll" if self.key_source.currentIndex() == 1 else "xor", self.mask.value(),
                             plan.relative(dll) if dll else "", self.variant.text().strip())
        plan.record = self.record.isChecked()
        reports = self.reports_edit.text().strip()
        plan.reports = plan.relative(reports) if reports else ""
        plan.excluded = [name for name, item in self._items.items() if item.checkState(COL_NAME) == Qt.Unchecked]
        plan.sequences = self.sequence_editor.sequences()
        plan.nrc_policy = self.policy_editor.policy()
        plan.deviations = self.policy_editor.deviations()
        plan.discovery = self.discovery_options
        plan.path = target
        return plan

    def apply_plan(self, plan: TestPlan):
        """Show a plan: its connection, settings and sequences, its description and the tests it leaves out."""
        self.plan_path = plan.path
        self.plan_name.setText(plan.name)
        connection = plan.connection
        self.interface.setCurrentText(connection.interface)
        self.channel.setEditText(str(connection.channel))
        self.bitrate.setCurrentText(str(connection.bitrate))
        self.request_id.setValue(connection.request_id)
        self.response_id.setValue(connection.response_id)
        self.functional_id.setValue(connection.functional_id if connection.functional_id is not None else 0x7DF)
        self.extended.setChecked(connection.extended)
        self.use_padding.setChecked(connection.padding is not None)
        self.padding.setValue(connection.padding if connection.padding is not None else 0xCC)
        options = plan.options
        for box, name in ((self.destructive, "destructive"), (self.lockout, "lockout"), (self.functional, "functional")):
            box.blockSignals(True)
            box.setChecked(bool(options.get(name, box.isChecked())))
            box.blockSignals(False)
        self.margin.setValue(int(options.get("timing_margin_ms", 50)))
        self.reset_time.setValue(float(options.get("reset_time", 1.0)))
        self.attempts.setValue(int(options.get("attempts", 3)))
        self.lockout_seconds.setValue(float(options.get("lockout_seconds", 10.0)))
        self.s3_test.blockSignals(True)
        self.s3_test.setChecked(bool(options.get("s3_test", True)))
        self.s3_test.blockSignals(False)
        self.s3_seconds.setValue(float(options.get("s3_seconds", 5.0)))
        self.start_routines.setText(str(options.get("start_routines", "") or ""))
        self.key_source.setCurrentIndex(1 if plan.key.kind == "dll" else 0)
        self.mask.setValue(plan.key.mask)
        self.dll_edit.setText(str(plan.resolve(plan.key.dll) or "") if plan.key.dll else "")
        self.variant.setText(plan.key.variant)
        self.record.setChecked(plan.record)
        self.reports_edit.setText(str(plan.resolve(plan.reports)) if plan.reports else "")
        self.sequence_editor.set_sequences(plan.sequences)
        self.policy_editor.set_policy(plan.nrc_policy)
        self.policy_editor.set_deviations(plan.deviations)
        self.discovery_options = plan.discovery
        self._items = {}                                    # the plan says what is left out, not the old tree
        self.identification = Identification(plan.identification.did, dict(plan.identification.values))
        self.identify_box.blockSignals(True)
        self.identify_box.setChecked(plan.identify)
        self.identify_box.blockSignals(False)
        description = plan.resolve(plan.description)
        if description is not None and description.is_file():
            if self.open_description(description, plan.variant) is None and \
                    (not plan.variant or self.open_description(description) is None):
                self.use_dummy()
        else:
            if description is not None:
                self._write(f"The plan's description {description} is not there: the Dummy ECU's is shown")
            self.use_dummy()
        excluded = set(plan.excluded)
        for name, item in self._items.items():
            item.setCheckState(COL_NAME, Qt.Unchecked if name in excluded else Qt.Checked)
        self._show_title()
        self._save_settings()

    def _show_title(self):
        name = self.plan_name.text().strip() or (self.plan_path.stem if self.plan_path else "")
        self.setWindowTitle(f"TestExpert - {name}" if name else "TestExpert")

    def new_plan(self):
        self.apply_plan(TestPlan())

    def open_plan(self, path=None):
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Test plan", str(TEST_EXPERT_DIR), PLAN_FILTER)
            if not path:
                return None
        try:
            plan = TestPlan.load(path)
        except PlanError as exc:
            self._write(f"{Path(path).name}: {exc}")
            return None
        self.apply_plan(plan)
        self._write(f"Plan {Path(path).name}: {self.description.name if self.description else ''}")
        return plan

    def save_plan(self):
        return self.save_plan_as(self.plan_path) if self.plan_path is not None else self.save_plan_as()

    def save_plan_as(self, path=None):
        if path is None:
            TEST_EXPERT_DIR.mkdir(parents=True, exist_ok=True)
            suggested = TEST_EXPERT_DIR / f"{self.plan_name.text().strip() or 'plan'}.json"
            path, _ = QFileDialog.getSaveFileName(self, "Save the test plan", str(suggested), PLAN_FILTER)
            if not path:
                return None
        plan = self.plan(path)
        try:
            plan.save(path)
        except OSError as exc:
            QMessageBox.warning(self, "TestExpert", f"The plan could not be saved:\n{exc}")
            return None
        self.plan_path = Path(path)
        self._show_title()
        self._save_settings()
        self._write(f"Plan saved: {path}  (run it without the window: test_expert.py \"{path}\" --run)")
        return Path(path)

    # --- the tests -------------------------------------------------------------------------------------------------

    def rebuild_tests(self):
        """The generated tests for the description and the options; what was unticked stays unticked."""
        if self.description is None:
            return
        unticked = {name for name, item in self._items.items() if item.checkState(COL_NAME) == Qt.Unchecked}
        self._running_item, self._hook_items = None, {}     # their items go with the tree
        plan = self.plan()
        self.suite = Suite(self.description, plan.make_options(), plan.sequences, plan.folder(), plan.nrc_policy)
        self.tree.clear()
        self._items = {}
        self._group_items = {}
        for group, cases in self.suite.groups().items():
            parent = QTreeWidgetItem([f"{group} ({len(cases)})", "", "", "", ""])
            parent.setFlags(parent.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
            parent.setData(COL_NAME, TARGET, ("group", group))
            self.tree.addTopLevelItem(parent)
            self._group_items[group] = parent
            for case in cases:
                item = QTreeWidgetItem([case.title.split(": ", 1)[1], "", "", "", ""])
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(COL_NAME, Qt.Unchecked if case.name in unticked else Qt.Checked)
                item.setData(COL_NAME, TARGET, ("test", case.name))
                if case.doc:
                    item.setToolTip(COL_NAME, case.doc)
                parent.addChild(item)
                self._items[case.name] = item
        self.sequence_editor.set_targets(list(self.suite.groups()), self.suite.titles())
        self.policy_editor.set_titles(self.suite.titles())
        self._show_sequences()
        self.status.setText(f"{len(self.suite.cases)} tests")

    # --- the sequences -----------------------------------------------------------------------------------

    def _sequences_changed(self):
        self._show_sequences()
        self._save_sequences()

    def _show_sequences(self):
        """Each test's and group's own sequences in the Sequences column; the run's and every test's in the
        status tip of the header."""
        attached = {}
        general = []
        for sequence in self.sequence_editor.sequences():
            if not sequence.enabled:
                continue
            for attachment in sequence.attachments:
                condition = " (if it did not pass)" if attachment.condition == "failed" and attachment.when == "after" \
                    else " (if it passed)" if attachment.condition == "passed" and attachment.when == "after" else ""
                text = f"{attachment.when}: {sequence.name}{condition}"
                if attachment.scope in ("group", "test"):
                    attached.setdefault((attachment.scope, attachment.target), []).append(text)
                else:
                    general.append(f"{text} ({'the run' if attachment.scope == 'run' else 'every test'})")
        for name, item in self._items.items():
            item.setText(COL_SEQUENCES, "; ".join(attached.get(("test", name), [])))
        for group, item in self._group_items.items():
            item.setText(COL_SEQUENCES, "; ".join(attached.get(("group", group), [])))
        header = self.tree.headerItem()
        header.setToolTip(COL_SEQUENCES, "Around the run and every test: " + ("; ".join(general) or "none"))

    def _tree_menu(self, position):
        menu = self.sequence_menu(self.tree.itemAt(position))
        if menu is not None:
            menu.popup(self.tree.viewport().mapToGlobal(position))
        return menu

    def sequence_menu(self, item):
        """The menu of an item of the tests tree: for a test or a group, sequences to run before or after it;
        for a failed step (or a test), accepting the deviation."""
        target = item.data(COL_NAME, TARGET) if item is not None else None
        if not target:
            return None
        if target[0] == "step":
            return self._step_menu(item, *target[1:])
        if target[0] == "hook":
            return self._run_menu("before" if target[1] == "setup" else "after")
        scope, name = target
        menu = QMenu(self)
        what = "test" if scope == "test" else "group"
        sequences = [sequence.name for sequence in self.sequence_editor.sequences()]
        for when, condition, text in (("before", "always", f"Before this {what}"),
                                      ("after", "always", f"After this {what}"),
                                      ("after", "failed", f"After this {what}, if it did not pass")):
            submenu = menu.addMenu(text)
            attachment = Attachment(when, scope, name, condition)
            for sequence in sequences:
                submenu.addAction(sequence).triggered.connect(
                    lambda _checked=False, sequence=sequence, attachment=attachment:
                    self.sequence_editor.attach(sequence, attachment))
            if sequences:
                submenu.addSeparator()
            for preset in PRESETS:
                submenu.addAction(f"New: {preset}").triggered.connect(
                    lambda _checked=False, preset=preset, attachment=attachment: self._attach_preset(preset, attachment))
        menu.addSeparator()
        menu.addAction(f"No sequences around this {what}").triggered.connect(
            lambda: self.sequence_editor.detach(scope, name))
        if scope == "test":
            menu.addSeparator()
            if self.policy_editor.find(name, "*") is None:
                menu.addAction("Accept every failure of this test...").triggered.connect(
                    lambda: self.accept_deviation(name, "*"))
            else:
                menu.addAction("No longer accept this test's failures").triggered.connect(
                    lambda: self.policy_editor.remove(name, "*"))
        return menu

    def _run_menu(self, when):
        """Sequences to run before or after the whole run."""
        menu = QMenu(self)
        submenu = menu.addMenu(f"{when.capitalize()} the run")
        attachment = Attachment(when, "run")
        for sequence in self.sequence_editor.sequences():
            submenu.addAction(sequence.name).triggered.connect(
                lambda _checked=False, name=sequence.name: self.sequence_editor.attach(name, attachment))
        for preset in PRESETS:
            submenu.addAction(f"New: {preset}").triggered.connect(
                lambda _checked=False, preset=preset: self._attach_preset(preset, attachment))
        return menu

    def _step_menu(self, item, test, step):
        menu = QMenu(self)
        if self.policy_editor.find(test, step) is not None:
            menu.addAction("No longer accept this deviation").triggered.connect(
                lambda: self.policy_editor.remove(test, step))
        elif item.text(COL_VERDICT) in ("fail", "accepted"):
            menu.addAction("Accept this deviation...").triggered.connect(
                lambda: self.accept_deviation(test, step, item))
        else:
            return None
        return menu

    def accept_deviation(self, test, step, item=None, comment=None):
        """Accept a failure (step "*": every failure of the test), with a comment asked for when not given."""
        if comment is None:
            comment, ok = QInputDialog.getText(self, "Accept the deviation",
                                               "Why is it accepted (a ticket, an agreement...)?")
            if not ok:
                return None
        deviation = Deviation(test, step, comment.strip(), today())
        self.policy_editor.add(deviation)
        if item is not None:
            item.setText(COL_VERDICT, "accepted")
            item.setForeground(COL_VERDICT, QColor(COLOURS["accepted"]))
        self._write(f"Accepted: {self.suite.titles().get(test, test) if self.suite else test}"
                    + (f" - {step}" if step != "*" else " - every failure") + (f" ({comment})" if comment else ""))
        return deviation

    def _attach_preset(self, preset, attachment):
        sequence = self.sequence_editor.add_sequence(preset, PRESETS[preset], [attachment])
        self.side.setCurrentWidget(self.sequence_editor)
        return sequence

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
        self._show_connection(True)
        self.connection_label.setText(f"Connected: {self.interface.currentText()} {self.channel.currentText()}")
        self._write(f"Connected to {self.interface.currentText()} {self.channel.currentText()}")
        if self.identify_box.isChecked() and self.description is not None and len(self.description.variants) > 1:
            self.identify_variant()
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
        self._show_connection(False)
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
        plan = self.plan()
        self._save_settings()
        self.runner = PlanRun(plan, self.description, self.mailbox, names,
                              on_event=lambda kind, data: self.run_event.emit(kind, data))
        self._report_folder = plan.report_folder(TEST_EXPERT_DIR / "reports")
        if self.record.isChecked():
            self._report_folder.mkdir(parents=True, exist_ok=True)
            self.recorder = Recorder(self._report_folder / f"traffic_{datetime.now().strftime('%Y%m%d-%H%M%S')}.blf")
        self._cases_started = False
        for name in names:
            item = self._items[name]
            item.takeChildren()
            for column in (COL_VERDICT, COL_STEPS, COL_TIME):
                item.setText(column, "")
        self._write(f"Running {len(names)} tests of {self.description.name}")
        self.run_action.setEnabled(False)
        self.stop_action.setEnabled(True)
        self.thread = threading.Thread(target=self._run, args=(self.runner,), daemon=True)
        self.thread.start()
        return self.thread

    def _run(self, run):
        report = None
        try:
            report = run.run()
        finally:
            self.run_finished.emit(report)

    def stop(self):
        if self.runner is not None:
            self.runner.stop()
        self._discovery_stop.set()

    # --- discovery -----------------------------------------------------------------------------------------

    def discover(self, options=None):
        """Ask the ECU what it has (options: DiscoveryOptions; None: ask in a dialog), on a thread."""
        if self.thread is not None and self.thread.is_alive():
            return None
        if self.mailbox is None:
            self._write("Connect to the ECU first.")
            return None
        if options is None:
            dialog = DiscoveryDialog(self.description, self.discovery_options, self)
            if not dialog.exec_():
                return None
            options = dialog.options()
        self.discovery_options = options
        self._save_settings()
        plan = self.plan()
        tester = Tester(self.mailbox, plan.connection.transport(), plan.connection.functional_id)
        self._discovery_stop.clear()
        discovery = Discovery(tester, self.description, options, progress=self.discovery_progress.emit,
                              stop=self._discovery_stop)
        self.run_action.setEnabled(False)
        self.discover_action.setEnabled(False)
        self.stop_action.setEnabled(True)
        self.results.setCurrentWidget(self.discovery_view)
        self.discovery_view.show_html("<p>Asking the ECU...</p>", usable=False)
        self._write(f"Discovering: sessions {', '.join(f'{s:02X}' for s in options.sessions)}, DIDs {options.dids}, "
                    f"routines {options.rids}")
        self.thread = threading.Thread(target=self._discover, args=(discovery,), daemon=True)
        self.thread.start()
        return self.thread

    def _discover(self, discovery):
        result = None
        try:
            result = discovery.run()
        finally:
            self.discovery_finished.emit(result)

    def _on_discovery_progress(self, done, total, text):
        self.status.setText(f"Discovering: {done} of {total} - {text}")

    def _on_discovery_finished(self, result):
        self.run_action.setEnabled(True)
        self.discover_action.setEnabled(True)
        self.stop_action.setEnabled(False)
        self.discovery_result = result
        if result is None:
            self.status.setText("The discovery failed; see the log.")
            self.discovery_view.show_html("<p>The discovery failed.</p>", usable=False)
            return
        findings = compare(result, self.description) if self.description is not None else []
        self.discovery_view.show_html(discovery_html(result, self.description))
        summary = (f"Discovery: {result.probes} requests in {result.duration:.1f} s, "
                   f"{len(findings)} difference{'s' if len(findings) != 1 else ''} with the description")
        self.status.setText(summary)
        self._write(summary)
        for note in result.notes:
            self._write(f"Discovery: {note}")

    def save_discovery(self, path=None):
        """The discovery as an HTML page, with its result as JSON beside it (same name)."""
        if self.discovery_result is None:
            return None
        if path is None:
            TEST_EXPERT_DIR.mkdir(parents=True, exist_ok=True)
            suggested = TEST_EXPERT_DIR / f"discovery_{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
            path, _ = QFileDialog.getSaveFileName(self, "Save the discovery", str(suggested), "HTML (*.html)")
            if not path:
                return None
        path = Path(path)
        path.write_text(discovery_page(self.discovery_result, self.description), encoding="utf-8")
        path.with_suffix(".json").write_text(json.dumps(self.discovery_result.to_dict(), indent=2), encoding="utf-8")
        self._write(f"Discovery saved: {path}")
        return path

    def use_discovered(self, path=None):
        """Save what was found as a JSON description, and test the ECU with it."""
        if self.discovery_result is None:
            return None
        name = f"{self.description.name} (discovered)" if self.description is not None else "Discovered ECU"
        found = self.discovery_result.to_description(name, self.description)
        if path is None:
            TEST_EXPERT_DIR.mkdir(parents=True, exist_ok=True)
            path, _ = QFileDialog.getSaveFileName(self, "Save what was found as a description",
                                                  str(TEST_EXPERT_DIR / "discovered.json"),
                                                  "TestExpert description (*.json)")
            if not path:
                return None
        found.save(path)
        self._write(f"Discovered description saved: {path} - add what discovery cannot find (sub-functions, "
                    "writing, routines' start) to it by hand")
        return self.open_description(path)

    def _hook_item(self):
        """The item of the steps the run takes before its first test (the pre-run sequences, the ECU's P2) or
        after its last (the post-run sequences)."""
        hook = "teardown" if self._cases_started else "setup"
        item = self._hook_items.get(hook)
        if item is None:
            item = QTreeWidgetItem(["Before the tests" if hook == "setup" else "After the tests", "", "", "", ""])
            item.setData(COL_NAME, TARGET, ("hook", hook))
            if hook == "setup":
                self.tree.insertTopLevelItem(0, item)
            else:
                self.tree.addTopLevelItem(item)
            self._hook_items[hook] = item
        return item

    def _on_event(self, kind, data):
        if kind == "case":
            self._cases_started = True
            self._running_item = self._items.get(data.name)
            if self._running_item is not None:
                self._running_item.setText(COL_VERDICT, "running")
                self.tree.scrollToItem(self._running_item)
        elif kind == "step":
            parent = self._running_item if self._running_item is not None else self._hook_item()
            step = QTreeWidgetItem([step_text(data), data.verdict, "", f"{data.time:.3f}"])
            step.setForeground(COL_VERDICT, QColor(COLOURS.get(data.verdict, "#6b7280")))
            owner = parent.data(COL_NAME, TARGET)
            if owner:
                step.setData(COL_NAME, TARGET, ("step", owner[1], data.description))
            parent.addChild(step)
            if parent is not self._running_item and data.verdict not in (PASS, INFO):
                parent.setExpanded(True)
        elif kind == "verdict":
            self._running_item = None
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
        self.run_action.setEnabled(True)
        self.stop_action.setEnabled(False)
        run, self.runner = self.runner, None
        self.report = report
        if self.recorder is not None:
            recording = self.recorder.path
            self.recorder.stop()
            self.recorder = None
            self._write(f"Traffic recorded: {recording}")
        if report is None:
            self.status.setText("The run failed; see the log.")
            return
        self.coverage_view.setHtml(run.coverage_html())
        for did, (name, value) in sorted(run.suite.identification.items()):
            self._write(f"ECU: {did:04X} {name} = {value}")
        try:
            self.report_paths = run.save(self._report_folder)
            self.report_action.setEnabled(True)
            where = f"   Report: {self.report_paths[0]}"
        except OSError as exc:
            self.report_paths, where = None, f"   The report could not be written: {exc}"
        if self.report_paths:
            self._compare_with_previous(self.report_paths[2])
        summary = summary_text(report)
        self.status.setText(f"<b style='color:{COLOURS.get(report.verdict, '#6b7280')}'>{summary}</b>")
        self._write(summary + where)

    # --- comparing runs ---------------------------------------------------------------------------------------

    def _compare_with_previous(self, results_path):
        """What changed since the last run of the same description, in the same folder."""
        try:
            after = load_results(results_path)
            previous = previous_results(Path(results_path).parent, after)
            if previous is None:
                return None
            return self.show_comparison(load_results(previous), after, quiet=True)
        except ResultsError:
            return None

    def compare_runs(self, before=None, after=None):
        """Compare two runs' results files (asked for when not given; the earlier one is "before")."""
        if before is None or after is None:
            TEST_EXPERT_DIR.mkdir(parents=True, exist_ok=True)
            paths, _ = QFileDialog.getOpenFileNames(self, "Two runs to compare (their .json results)",
                                                    str(self._report_folder), "TestExpert results (*.json)")
            if len(paths) != 2:
                if paths:
                    self._write("Choose two results files: the .json beside two reports.")
                return None
            before, after = paths
        try:
            runs = sorted((load_results(before), load_results(after)), key=lambda run: run.get("started", 0))
        except ResultsError as exc:
            self._write(str(exc))
            return None
        return self.show_comparison(*runs)

    def show_comparison(self, before, after, quiet=False):
        self.comparison = compare_runs(before, after)
        self.comparison_view.setHtml(comparison_html(self.comparison))
        self.save_comparison_btn.setEnabled(True)
        summary = self.comparison.summary()
        self._write(f"Since the last run: {summary}" if quiet else f"Comparison: {summary}")
        if not quiet or self.comparison.regressions:
            self.results.setCurrentWidget(self.comparison_tab)
        return self.comparison

    def save_comparison(self, path=None):
        if self.comparison is None:
            return None
        if path is None:
            suggested = self._report_folder / f"comparison_{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
            path, _ = QFileDialog.getSaveFileName(self, "Save the comparison", str(suggested), "HTML (*.html)")
            if not path:
                return None
        Path(path).write_text(comparison_page(self.comparison), encoding="utf-8")
        self._write(f"Comparison saved: {path}")
        return Path(path)

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
        self._save_settings()
        self.disconnect_ecu()
        super().closeEvent(event)


def main(argv=None) -> int:
    """TestExpert's command line (cli.py): the window, or a plan run without it."""
    from canexpert.test_expert.cli import main as command_line
    return command_line(argv)


def gui(arguments) -> int:
    """The window, with the plan or the description the command line names."""
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setWindowIcon(app_icon("test_expert"))
    window = TestExpertWindow(MemorySettings() if arguments.smoke_test else None)
    if arguments.file and is_plan_file(arguments.file):
        window.open_plan(arguments.file)
    elif arguments.file:
        window.open_description(arguments.file)
    if arguments.smoke_test:
        print("startup ok" if window.suite is not None else "no tests")
        return 0 if window.suite is not None else 1
    window.show()
    return app.exec_()

