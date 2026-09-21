"""
CAN Expert main window: configurations, CAN receivers with their ECU nodes, Connect/Disconnect (load
the newest matching panel database, send periodic TesterPresent, run the panel's Python script),
Flashing, the tool windows and the logs.
"""
import json
import re
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import can
from PyQt5.QtCore import QEvent, QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QKeySequence, QPalette
from PyQt5.QtWidgets import (
    QAbstractSpinBox,
    QAction,
    QActionGroup,
    QApplication,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canexpert.can_bus import SUPPORTED_INTERFACES, CanWorker, ChannelActivityScanner, ReceiveMailbox, channel_key
from canexpert.can_logger import CANLoggerWindow
from canexpert.channel_setup import load_setup, open_configured, save_setup
from canexpert.channel_setup_dialog import ChannelSetupDialog
from canexpert.config import (DEFAULT_CONFIGURATION, ConfigurationDialog, read_configurations, save_configuration,
                              uds_transport, validate_config)
from canexpert.data_window import DataWindow
from canexpert.designer.form_designer import FormDesigner
from canexpert.diagnostic_window import DiagnosticWindow
from canexpert.clock import TIME_DISPLAYS, MeasurementClock
from canexpert.flash_runner import FlashRunner
from canexpert.flash_sequence import FlashProfile
from canexpert.frame_filter import FilterBar, FrameFilter
from canexpert.flashing import (FlashDialog, choose_firmware, close_progress, progress_dialog, report_result,
                                update_progress)
from canexpert.help_window import show_manual
from canexpert.panel.database import load_application_database, select_database
from canexpert.panel.runtime import ScriptRuntime
from canexpert.panel.view import PanelView
from canexpert.paths import APP_DIR, CONFIG_DIR, DATABASES_DIR
from canexpert.recording import LOG_FILE_FILTER, Recorder, ReplayDialog
from canexpert.simulation_window import SimulationWindow
from canexpert.statistics_window import StatisticsWindow
from canexpert.symbols import SymbolDatabaseDialog, SymbolDatabases
from canexpert.sysvars import SystemVariables, SystemVariablesWindow
from canexpert.trace_window import TraceWindow
from canexpert.transport_settings import apply_transport, load_transport
from canexpert.transmit_window import TransmitWindow
from canexpert.uds_console import UdsConsoleWindow
from canexpert.ui_common import DockTitleBar, app_settings, line_icon, toolbar_icon
from canexpert.workspace import add_pane, create_workspace, make_pane
from canexpert.write_window import WriteWindow

# A question mark in a circle, for the manual button beside the Help menu.
MANUAL_ICON = ('<circle cx="12" cy="12" r="9"/><path d="M9.2 9.3a2.9 2.9 0 0 1 5.6 1c0 1.9-2.8 2.4-2.8 4"/>'
               '<path d="M12 17.4h.01" stroke-width="2.2"/>')
MONITOR_LINES = 5000                # lines the CAN monitor keeps
TIME_DISPLAY = "time_display"       # settings: Absolute or Relative, for the monitors
PANEL_ZOOM = "panel_zoom"           # settings: panel_zoom/<database>/<page> -> the page's zoom
LAST_CHANNEL = "last_channel"      # settings: the channel to select and check at the next start
FLASH_PROFILE = "flash_profile"    # settings: the built-in flashing sequence, as JSON
USED_CHANNELS = "used_channels"    # settings: the channels connected before, shown in bold
LAYOUT_GEOMETRY = "layout/geometry"
LAYOUT_STATE = "layout/state"
LAYOUT_WORKSPACE = "layout/workspace"
DESKTOPS = "layout/desktops"       # settings: name -> saved window arrangement (a "desktop")
FRAME_HISTORY = 20000              # frames kept so a window opened later can still show them
TOOL_PANES = ("trace", "logger", "data", "statistics", "transmit", "simulation", "console",
              "diagnostics", "write", "sysvars")   # the windows with a switch on the toolbar
WRITE_HISTORY = 5000               # Write window lines kept for when it is opened


class MainWindow(QMainWindow):
    """Main application window"""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CAN Expert")
        self.setGeometry(100, 100, 1000, 700)
        
        # Configuration management
        self.configurations = []
        self.active_config = None
        self.workers = {}
        self.can_bus = None
        self.app_database = None
        self.channel_activity = {}  # channel key -> traffic seen by the last activity scan
        self.connected_channel_config = None
        self.selected_channel_config = None
        self.activity_scanner = None
        self.script_runtime = None
        self.flash_dialog = None
        self.flash_runner = None   # the built-in flashing sequence while it runs
        self.script_flash = False  # whether the panel script offers a Flashing(api, firmware)
        self._auto_minimized = []  # dock title bars minimized on connect, restored on disconnect
        self._left_split = None    # Configuration / CAN Channels heights before they were minimized
        self.panel = None
        self.session_config = None
        self.node_states = {}
        self.channel_items = {}
        self.node_items = {}
        self.database_items = {}   # channel key -> the database entry offered under a responding channel
        self.session_generation = 0
        # Checks the ECUs with TesterPresent while no database is connected (after Disconnect, or on request).
        self.ecu_monitor = self.monitor_bus = self.monitor_channel = self.monitor_config = None
        self.last_channel, self.used_channels = None, set()
        self._read_channel_history()
        # What is on the bus, whoever opened it: the database session, the ECU check, or a replayed
        # file. Windows opened later read the history.
        self._settings = app_settings()
        self.symbols = SymbolDatabases(parent=self, settings=self._settings)
        # One clock for the monitor, the Trace, the Logger and the Diagnostic Window (clock.py).
        self.clock = MeasurementClock()
        # System variables (sysvars.py), the script's Write output, and the bus state scripts react to.
        self.sysvars = SystemVariables(self._settings, self)
        self.sysvars.changed.connect(self._on_sysvar_changed)
        self.sysvar_history = deque(maxlen=FRAME_HISTORY)      # (when, name, value) for a Logger opened later
        self.write_history = deque(maxlen=WRITE_HISTORY)       # (when, level, text) for a Write window opened later
        self._bus_state = None
        self._keys_watched = False
        self.time_display = self._settings.value(TIME_DISPLAY, TIME_DISPLAYS[0], type=str)
        if self.time_display not in TIME_DISPLAYS:
            self.time_display = TIME_DISPLAYS[0]
        self.frame_history = deque(maxlen=FRAME_HISTORY)
        self.recorder = None
        self.replay = None
        self.tool_panes = {}
        self._bus_state = "unknown"

        self.init_ui()
        self.load_configurations()
        self.node_timer = QTimer(self)
        self.node_timer.setInterval(100)
        self.node_timer.timeout.connect(self._update_nodes)
        self.node_timer.start()
        self.check_last_channel()

    # --- UI setup ---

    def init_ui(self):
        """Build CANoe-style main window: toolbar, status bar, dockable Configuration, CAN Channels, Database, Log."""
        # Status bar (status message)
        self.status_label = QLabel("No active connections")
        self._set_status("No active connections", "gray")
        self.statusBar().addPermanentWidget(self.status_label)

        toolbar = QToolBar("Main actions", self)
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(28, 28))
        toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        # A checked button keeps a pressed-in background with an accent line: a style sheet that names
        # any state replaces the style's own drawing of the checked one, which would leave a toggle
        # here looking identical on and off.
        toolbar.setStyleSheet("""
            QToolBar { spacing: 4px; padding: 6px; border: none; }
            QToolBar QToolButton { padding: 6px 8px; border: 1px solid transparent; border-radius: 6px; }
            QToolBar QToolButton:hover { background: palette(midlight); }
            QToolBar QToolButton:pressed { background: palette(mid); }
            QToolBar QToolButton:checked { background: palette(mid); border: 1px solid palette(dark);
                                           border-bottom: 3px solid palette(highlight); }
            QToolBar QToolButton:checked:hover { background: palette(midlight); }
        """)
        self._toolbar_actions = {}
        entries = [
            ("connect", "Connect", "Connect to the selected CAN receiver", self.on_connect_clicked),
            ("disconnect", "Disconnect", "Close the database; the ECUs are still checked with TesterPresent",
             self.disconnect_database),
            ("trace", "Trace", "Every frame of the measurement, decoded with the symbol databases",
             self.open_trace),
            ("logger", "CAN Logger", "Plot and export CAN signals", self.open_can_logger),
            ("data", "Data", "Every signal of the symbol databases with the value it holds now",
             self.open_data),
            ("statistics", "Statistics", "Frames per identifier, their rate and cycle time, and the bus load",
             self.open_statistics),
            ("transmit", "Transmit", "Send messages once or cyclically", self.open_transmit),
            ("simulation", "Simulation", "Send the messages of a database's nodes, as those ECUs would",
             self.open_simulation),
            ("console", "UDS Console", "Send any UDS service and read the fault memory (no ODX file needed)",
             self.open_uds_console),
            ("diagnostics", "Diagnostics", "Open ECU diagnostic services", self.open_diagnostic_window),
            ("write", "Write", "What the panel script writes, and its variables as it runs", self.open_write),
            ("sysvars", "System Variables", "Values shared by the script, the windows and you", self.open_sysvars),
            ("designer", "Form Designer", "Design panels and edit their Python scripts", self.open_form_designer),
            ("flashing", "Flashing", "Flash ECU firmware with the built-in sequence or the script's Flashing()",
             self.open_flashing),
        ]
        for name, label, hint, callback in entries:
            if name == "trace":
                toolbar.addSeparator()
            action = QAction(toolbar_icon(name), label, self)
            if name in TOOL_PANES:
                # A tool button works as a switch: it stays pressed while its pane is open, pressing it
                # again closes the pane, and closing the pane by its own button lets the toolbar go.
                action.setCheckable(True)
                action.toggled.connect(lambda shown, n=name, show=callback: self._toggle_tool(n, shown, show))
                hint = f"{hint}\nPress again to close the pane"
            else:
                action.triggered.connect(callback)
            action.setToolTip(hint)
            action.setStatusTip(hint)
            button = QToolButton()
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(28, 28))
            button.setMinimumSize(88, 66)
            button.setAccessibleName(label)
            toolbar_item = toolbar.addWidget(button)
            self._toolbar_actions[name] = action
            if name == "flashing":
                # Shown only while connected to a database.
                self.flashing_toolbar_item = toolbar_item
                toolbar_item.setVisible(False)
            if name == "connect":
                self.connect_btn = button
            elif name == "disconnect":
                self.disconnect_btn = button
                button.setEnabled(False)
        self.addToolBar(toolbar)

        # The centre is the workspace: the panel and the analysis windows, which tab together, float
        # and can be dragged onto each other. Configuration, CAN Channels and Log stay Qt docks around it.
        self.workspace = create_workspace(self)
        self._workspace_style = self.workspace.styleSheet()   # palette(...) colours, re-read per theme

        # Dock: Configuration (closable, collapsible)
        config_widget = QWidget()
        config_layout = QVBoxLayout()
        self.config_list = QListWidget()
        self.config_list.itemClicked.connect(self.on_config_selected)
        self.config_list.itemDoubleClicked.connect(self.edit_configuration)
        config_layout.addWidget(self.config_list)
        btn_row = QHBoxLayout()
        self.new_config_btn = QPushButton("New")
        self.new_config_btn.setToolTip("Create a CAN configuration")
        self.new_config_btn.clicked.connect(self.create_new_config)
        self.import_config_btn = QPushButton("Import")
        self.import_config_btn.clicked.connect(self.import_config)
        self.export_config_btn = QPushButton("Export")
        self.export_config_btn.clicked.connect(self.export_config)
        btn_row.addWidget(self.new_config_btn)
        btn_row.addWidget(self.import_config_btn)
        btn_row.addWidget(self.export_config_btn)
        config_layout.addLayout(btn_row)
        config_widget.setLayout(config_layout)
        self.config_dock = QDockWidget("Configuration", self)
        self.config_dock.setObjectName("dock_configuration")
        self.config_dock.setWidget(config_widget)
        self.config_dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.config_dock.setTitleBarWidget(DockTitleBar(self.config_dock, self, Qt.LeftDockWidgetArea))
        self.addDockWidget(Qt.LeftDockWidgetArea, self.config_dock)

        # Dock: CAN Channels (separate window, below Configuration on the left)
        channels_widget = QWidget()
        channels_layout = QVBoxLayout()
        self.channel_list = QTreeWidget()
        self.channel_list.setHeaderLabels(["CAN receivers and nodes"])
        self.channel_list.itemClicked.connect(self.on_channel_selected)
        self.channel_list.itemDoubleClicked.connect(self.on_channel_double_clicked)
        self.channel_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.channel_list.customContextMenuRequested.connect(self._channel_menu)
        channels_layout.addWidget(self.channel_list)
        ch_btn_layout = QHBoxLayout()
        self.refresh_channels_btn = QPushButton("Refresh")
        self.refresh_channels_btn.clicked.connect(self.refresh_channel_list)
        self.scan_activity_btn = QPushButton("Scan Activity")
        self.scan_activity_btn.clicked.connect(self.scan_channel_activity)
        ch_btn_layout.addWidget(self.refresh_channels_btn)
        ch_btn_layout.addWidget(self.scan_activity_btn)
        channels_layout.addLayout(ch_btn_layout)
        channels_widget.setLayout(channels_layout)
        self.channels_dock = QDockWidget("CAN Channels", self)
        self.channels_dock.setObjectName("dock_channels")
        self.channels_dock.setWidget(channels_widget)
        self.channels_dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.channels_dock.setTitleBarWidget(DockTitleBar(self.channels_dock, self, Qt.LeftDockWidgetArea))
        self.addDockWidget(Qt.LeftDockWidgetArea, self.channels_dock)
        self.splitDockWidget(self.config_dock, self.channels_dock, Qt.Vertical)

        # Dock: Database (shown when connected and DB loaded; contains application UI)
        # The loaded database's first page; its other pages get windows of their own (page_panes).
        self.app_db_container = QWidget()
        self.app_db_layout = QVBoxLayout()
        self.app_db_layout.setContentsMargins(0, 0, 0, 0)
        self.app_db_container.setLayout(self.app_db_layout)
        self.page_panes = []
        self.database_pane = make_pane("Database", self.app_db_container, "pane_database")
        add_pane(self.workspace, self.database_pane)
        self.database_pane.toggleView(False)   # shown once a database is loaded

        # Dock: Log (Debug + CAN Monitor)
        log_tabs = QTabWidget()
        self.debug_log = QPlainTextEdit()
        self.debug_log.setReadOnly(True)
        self.debug_log.setPlaceholderText("Application debug and status messages…")
        self.debug_log.setMaximumBlockCount(2000)
        log_tabs.addTab(self.debug_log, "Debug / Verbose")
        self.can_log = QPlainTextEdit()
        self.can_log.setReadOnly(True)
        self.can_log.setPlaceholderText("CAN traffic (TX/RX) for the connected channel…")
        self.can_log.setMaximumBlockCount(MONITOR_LINES)
        self.monitor_filter = FrameFilter()
        self.monitor_filter_bar = FilterBar("Filter the monitor: 7E8, 300-3FF, EngineData")
        self.monitor_filter_bar.changed.connect(self._on_monitor_filter_changed)
        monitor = QWidget()
        monitor_layout = QVBoxLayout(monitor)
        monitor_layout.setContentsMargins(0, 2, 0, 0)
        monitor_layout.addWidget(self.monitor_filter_bar)
        monitor_layout.addWidget(self.can_log, 1)
        log_tabs.addTab(monitor, "CAN Monitor")
        self.log_dock = QDockWidget("Log", self)
        self.log_dock.setObjectName("dock_log")
        self.log_dock.setWidget(log_tabs)
        self.log_dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.log_dock.setTitleBarWidget(DockTitleBar(self.log_dock, self, Qt.BottomDockWidgetArea))
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)

        self.create_menu()
        # The arrangement the window starts with, so Reset layout has somewhere to go back to.
        self._default_layout = (self.saveState(), self.workspace.saveState())
        self._workspace_state = None
        self.restore_layout()
        self.refresh_channel_list()
        self.log_verbose("Application started.")
        if self.symbols.errors:
            for error in self.symbols.errors:
                self.log_verbose(f"Symbol database: {error}")

    # --- The channels used before ---------------------------------------------------

    def _read_channel_history(self):
        """The channel used last and every channel connected before, as saved by _remember_channel()."""
        settings = app_settings()
        try:
            used = json.loads(settings.value(USED_CHANNELS, "[]") or "[]")
            last = json.loads(settings.value(LAST_CHANNEL, "null") or "null")
        except ValueError:
            used, last = [], None
        self.used_channels = {tuple(key) for key in used if isinstance(key, list)}
        self.last_channel = tuple(last) if isinstance(last, list) else None

    def _remember_channel(self, channel_config):
        """Keep the channel as the one to select and check at the next start."""
        key = channel_key(channel_config)
        self.last_channel = key
        self.used_channels.add(key)
        settings = app_settings()
        settings.setValue(LAST_CHANNEL, json.dumps(list(key)))
        settings.setValue(USED_CHANNELS, json.dumps([list(used) for used in self.used_channels]))

    def check_last_channel(self):
        """At startup: select the channel used last and start checking its ECUs with TesterPresent, so a
        responding ECU and the database it can load appear without connecting first."""
        item = self.channel_items.get(self.last_channel)
        if item is None or self.can_bus is not None or not self.active_config:
            return
        self.on_channel_selected(item)
        self.check_ecus(item.data(0, Qt.UserRole))

    def _matching_database(self):
        """The panel database Connect would load for the active configuration, or None."""
        if not self.active_config:
            return None
        try:
            return select_database(DATABASES_DIR, str(self.active_config.get("database_family", "")))
        except (OSError, ValueError) as exc:
            self.log_verbose(f"Database selection: {exc}")
            return None

    def _set_status(self, text: str, color: str = "gray"):
        """Update status label text and optional color (gray, green, red, orange)."""
        self.status_label.setText(text)
        colors = {"gray": "gray", "green": "green", "red": "red", "orange": "orange"}
        self.status_label.setStyleSheet(f"QLabel {{ color: {colors.get(color, 'gray')}; }}")

    def _time_str(self) -> str:
        """Return current time as HH:MM:SS:mmm."""
        now = datetime.now()
        return now.strftime("%H:%M:%S") + f":{now.microsecond // 1000:03d}"

    def log_verbose(self, msg: str):
        """Append a message to the debug/verbose log."""
        if getattr(self, "debug_log", None) is None:
            return
        line = f"[{self._time_str()}] {msg}"
        self.debug_log.appendPlainText(line)

    def _monitor_line(self, timestamp, direction: str, arbitration_id: int, data) -> str:
        hex_str = " ".join(f"{b:02X}" for b in bytes(data)[:8])
        return f"{self.clock.text(timestamp, self.time_display)}  {direction:>3}  ID: 0x{arbitration_id:X}  {hex_str}"

    def _monitor_passes(self, direction: str, arbitration_id: int) -> bool:
        rule = self.monitor_filter
        return rule.empty or rule.passes(direction, arbitration_id, self.symbols.name(arbitration_id))

    def log_can(self, direction: str, arbitration_id: int, data: list | bytes, timestamp: float | None = None):
        """Append a CAN message to the CAN monitor (time, direction TX or RX, ID, hex data), if it passes
        the monitor's filter. The time is the frame's own, so it matches the Trace."""
        if getattr(self, "can_log", None) is None or not self._monitor_passes(direction, arbitration_id):
            return
        self.can_log.appendPlainText(self._monitor_line(time.time() if timestamp is None else timestamp,
                                                        direction, arbitration_id, data))

    def set_time_display(self, display: str):
        """Absolute (time of day) or Relative (seconds since the measurement started) in the monitors."""
        self.time_display = display
        self._settings.setValue(TIME_DISPLAY, display)
        for action in self._time_display_actions:
            action.setChecked(action.text() == display)
        self._on_monitor_filter_changed(self.monitor_filter)

    def _on_monitor_filter_changed(self, rule):
        """A new filter applies to what was already seen too: the monitor is rebuilt from the history."""
        self.monitor_filter = rule
        lines = [self._monitor_line(timestamp, direction, can_id, data)
                 for timestamp, direction, can_id, data, _extended in list(self.frame_history)
                 if self._monitor_passes(direction, can_id)]
        self.can_log.setPlainText("\n".join(lines[-MONITOR_LINES:]))
        self.can_log.moveCursor(self.can_log.textCursor().End)

    # --- Channel list ---

    def refresh_channel_list(self):
        self.channel_list.clear()
        self.channel_items.clear()
        self.node_items.clear()
        self.can_channels = []
        for interface, label in SUPPORTED_INTERFACES:
            try:
                for cfg in can.detect_available_configs(interfaces=[interface], timeout=2.0):
                    cfg["interface"] = interface
                    self.can_channels.append(cfg)
            except Exception as exc:
                self.log_verbose(f"{label} detection: {exc}")
        for in_use in (self.connected_channel_config, self.monitor_channel if self.ecu_monitor else None):
            if in_use and not any(channel_key(c) == channel_key(in_use) for c in self.can_channels):
                self.can_channels.append(in_use)
        self.database_items.clear()
        for cfg in self.can_channels:
            item = QTreeWidgetItem([self._channel_label(cfg)])
            item.setData(0, Qt.UserRole, cfg)
            key = channel_key(cfg)
            if key in self.used_channels:  # channels connected before stand out
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
            self.channel_list.addTopLevelItem(item)
            self.channel_items[key] = item
        if not self.can_channels:
            self.channel_list.addTopLevelItem(QTreeWidgetItem(["No CAN receivers found"]))
        remembered = self.channel_items.get(self.last_channel)
        if remembered is not None and self.can_bus is None:
            self.channel_list.setCurrentItem(remembered)
            self.selected_channel_config = remembered.data(0, Qt.UserRole)
        self._update_nodes()

    def _channel_label(self, cfg):
        label = f"[{cfg['interface']}] Ch {cfg.get('channel', 0)}: {cfg.get('device_name', cfg.get('description', 'CAN receiver'))}"
        serial = cfg.get("serial") or cfg.get("unique_hardware_id")
        if serial:
            label += f" ({serial})"
        active = self.channel_activity.get(channel_key(cfg))
        if active is not None:
            label += " — traffic" if active else " — no traffic"
        if load_setup(self._settings, cfg).listen_only:
            label += " [listen-only]"
        if self.connected_channel_config and channel_key(cfg) == channel_key(self.connected_channel_config):
            label += " [Connected]"
        elif self.ecu_monitor and channel_key(cfg) == channel_key(self.monitor_channel):
            label += " [Checking ECUs]"
        return label

    def _label_channels(self):
        for item in self.channel_items.values():
            item.setText(0, self._channel_label(item.data(0, Qt.UserRole)))

    def _channel_checked(self, key):
        """True while ECU replies on this channel are being watched: a database session or the ECU check."""
        if self.can_bus is not None and self.connected_channel_config is not None:
            if key == channel_key(self.connected_channel_config):
                return True
        return self.ecu_monitor is not None and key == channel_key(self.monitor_channel)

    def _update_nodes(self):
        now, responding = time.monotonic(), set()
        for (channel, can_id), state in self.node_states.items():
            parent = self.channel_items.get(channel)
            if parent is None:
                continue
            item = self.node_items.get((channel, can_id))
            if item is None:
                item = QTreeWidgetItem(parent)
                self.node_items[(channel, can_id)] = item
                parent.setExpanded(True)
            if not self._channel_checked(channel):
                symbol, status, colour = "○", "Not checked", "gray"
            elif now - state["last_seen"] > state["timeout"]:
                symbol, status, colour = "✗", "Lost connection", "red"
            else:
                symbol, status, colour = "●", "Responding", "green"
                responding.add(channel)
            item.setText(0, f"{symbol} ECU 0x{can_id:X} — {status}")
            item.setForeground(0, QColor(colour))
            item.setData(0, Qt.UserRole, parent.data(0, Qt.UserRole))
        self._update_databases(responding)

    def _update_databases(self, responding):
        """Offer the database that Connect would load under every channel with a responding ECU."""
        database = self._matching_database() if responding else None
        for key, parent in self.channel_items.items():
            item = self.database_items.get(key)
            if database is None or key not in responding:
                if item is not None:
                    parent.removeChild(item)
                    del self.database_items[key]
                continue
            if item is None:
                item = QTreeWidgetItem(parent)
                self.database_items[key] = item
                parent.setExpanded(True)
            loaded = (self.app_database or {}).get("source_path") == str(database.resolve())
            item.setText(0, f"▣ {database.stem} — {'loaded' if loaded else 'double-click to load'}")
            item.setForeground(0, QColor("#1566ae"))
            item.setData(0, Qt.UserRole, parent.data(0, Qt.UserRole))  # double-click connects this channel

    def scan_channel_activity(self):
        """Scan channels for CAN activity (when disconnected)."""
        if self.can_bus:
            QMessageBox.information(
                self, "Info",
                "Disconnect first to scan for activity on other channels."
            )
            return
        if not getattr(self, "can_channels", None) or not self.can_channels:
            self.refresh_channel_list()
        if not self.can_channels:
            return
        bitrate = 500000
        if self.active_config:
            bitrate = int(self.active_config.get("bitrate", 500000))
        self.scan_activity_btn.setEnabled(False)
        self.status_label.setText("Scanning channels for activity...")
        self.activity_scanner = ChannelActivityScanner(self.can_channels, bitrate)
        self.activity_scanner.channel_activity.connect(self.on_activity_scan_result)
        self.activity_scanner.finished.connect(self.on_activity_scan_finished)
        self.activity_scanner.start()

    def on_activity_scan_result(self, result: list):
        """Show on each channel whether the scan saw traffic."""
        channels = self.activity_scanner.channels if self.activity_scanner else []
        self.channel_activity = {channel_key(cfg): active for cfg, active in zip(channels, result)}
        self._label_channels()

    def on_activity_scan_finished(self):
        """Re-enable scan button after scan completes."""
        self.scan_activity_btn.setEnabled(True)
        self.status_label.setText("Activity scan complete")
        self.activity_scanner = None

    def on_channel_selected(self, item, column=0):
        cfg = item.data(0, Qt.UserRole)
        if cfg:
            self.selected_channel_config = cfg
            self.last_channel = channel_key(cfg)
            app_settings().setValue(LAST_CHANNEL, json.dumps(list(self.last_channel)))
            self.status_label.setText(f"Selected {cfg['interface']} channel {cfg.get('channel', 0)}")

    def on_channel_double_clicked(self, item, column=0):
        self.on_channel_selected(item, column)
        if self.can_bus is None:
            self.on_connect_clicked()

    def _show_can_channels_dock(self):
        """Show CAN Channels dock (e.g. after user closed it or after disconnect)."""
        self.channels_dock.setVisible(True)
        self.channels_dock.raise_()

    def _show_config_dock(self):
        """Show Configuration dock."""
        self.config_dock.setVisible(True)
        self.config_dock.raise_()

    def _show_log_dock(self):
        """Show Log (CAN Monitor / Debug) dock."""
        self.log_dock.setVisible(True)
        self.log_dock.raise_()

    def create_menu(self):
        """Create the menu bar"""
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu('File')
        
        new_config_action = file_menu.addAction('New Configuration')
        new_config_action.triggered.connect(self.create_new_config)
        
        import_config_action = file_menu.addAction('Import Configuration')
        import_config_action.triggered.connect(self.import_config)
        
        export_config_action = file_menu.addAction('Export Configuration')
        export_config_action.triggered.connect(self.export_config)

        file_menu.addSeparator()
        file_menu.addAction('Show Configuration').triggered.connect(self._show_config_dock)
        file_menu.addAction('Show CAN Channels').triggered.connect(self._show_can_channels_dock)
        file_menu.addAction('Show CAN Monitor').triggered.connect(self._show_log_dock)

        file_menu.addSeparator()
        exit_action = file_menu.addAction('Exit')
        exit_action.triggered.connect(self.close)

        # Connection menu: the session, and what is written to or read from a file
        measurement_menu = menubar.addMenu('Connection')
        for name in ("connect", "disconnect"):
            measurement_menu.addAction(self._toolbar_actions[name])
        measurement_menu.addSeparator()
        self._record_action = measurement_menu.addAction('Record to file...')
        self._record_action.triggered.connect(self.start_recording)
        self._stop_record_action = measurement_menu.addAction('Stop recording')
        self._stop_record_action.setEnabled(False)
        self._stop_record_action.triggered.connect(self.stop_recording)
        measurement_menu.addAction('Replay a recorded file...').triggered.connect(self.replay_log)

        # Tools menu
        tools_menu = menubar.addMenu('Tools')
        tools_menu.addAction('Form Designer').triggered.connect(self.open_form_designer)
        for name in TOOL_PANES:
            tools_menu.addAction(self._toolbar_actions[name])   # checked while the pane is open
        tools_menu.addSeparator()
        tools_menu.addAction('Symbol databases...').triggered.connect(self.edit_symbol_databases)

        # View menu
        view_menu = menubar.addMenu('View')

        refresh_config_action = view_menu.addAction('Refresh Configurations')
        refresh_config_action.triggered.connect(self.load_configurations)
        refresh_channels_action = view_menu.addAction('Refresh Channels')
        refresh_channels_action.triggered.connect(self.refresh_channel_list)
        view_menu.addSeparator()
        time_menu = view_menu.addMenu('Time display')
        time_group = QActionGroup(self)
        self._time_display_actions = []
        for display in TIME_DISPLAYS:
            action = time_menu.addAction(display)
            action.setCheckable(True)
            action.setChecked(display == self.time_display)
            action.setToolTip("Time of day" if display == "Absolute" else "Seconds since the measurement started")
            action.triggered.connect(lambda _checked, d=display: self.set_time_display(d))
            time_group.addAction(action)
            self._time_display_actions.append(action)
        view_menu.addSeparator()
        self._desktop_menu = view_menu.addMenu('Desktops')
        view_menu.addAction('Save desktop as...').triggered.connect(lambda: self.save_desktop())
        view_menu.addAction('Reset layout').triggered.connect(self.reset_layout)
        self._refresh_desktop_menu()

        # Options menu - Theme
        options_menu = menubar.addMenu('Options')
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        self.light_mode_action = options_menu.addAction('Light Mode')
        self.light_mode_action.setCheckable(True)
        self.light_mode_action.triggered.connect(lambda: self.apply_theme('light'))
        theme_group.addAction(self.light_mode_action)
        self.dark_mode_action = options_menu.addAction('Dark Mode')
        self.dark_mode_action.setCheckable(True)
        self.dark_mode_action.triggered.connect(lambda: self.apply_theme('dark'))
        theme_group.addAction(self.dark_mode_action)

        # Help menu on the far right (corner widget; avoid nesting a second QMenuBar)
        help_corner = QWidget()
        help_corner_layout = QHBoxLayout(help_corner)
        help_corner_layout.setContentsMargins(0, 0, 6, 0)
        help_btn = QToolButton()
        help_btn.setText("Help")
        help_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        help_btn.setPopupMode(QToolButton.InstantPopup)
        help_menu = QMenu(help_btn)
        help_menu.addAction("User manual", self.open_manual)
        help_menu.addAction("About", self.show_about)
        help_btn.setMenu(help_menu)
        self.manual_btn = QToolButton()
        self.manual_btn.setAutoRaise(True)
        self.manual_btn.setIconSize(QSize(18, 18))
        self.manual_btn.setAccessibleName("User manual")
        self.manual_btn.setToolTip("User manual: how to use the main window, Form Designer, CAN Logger and "
                                   "Diagnostic Window")
        self.manual_btn.clicked.connect(self.open_manual)
        help_corner_layout.addWidget(self.manual_btn)
        help_corner_layout.addWidget(help_btn)
        menubar.setCornerWidget(help_corner, Qt.TopRightCorner)
        # Last, so every button that follows the theme already exists.
        self.apply_theme(app_settings().value("theme", "light", type=str), restore=True)

    def _refresh_manual_icon(self):
        self.manual_btn.setIcon(line_icon(MANUAL_ICON, self.palette().color(QPalette.WindowText)))

    def open_manual(self):
        """Show the user manual (docs/USER_MANUAL.md)."""
        return show_manual(self)

    def show_about(self):
        """Show About dialog with app info."""
        dlg = QDialog(self)
        dlg.setWindowTitle("About CAN Expert")
        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)
        layout.addWidget(QLabel("CAN Expert"))
        layout.addWidget(QLabel("CAN and UDS tool: panel databases with Python scripts, Form Designer, CAN Logger,\n"
                                "Diagnostic Window, firmware flashing and a simulated ECU."))
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(dlg.accept)
        layout.addWidget(ok_btn, 0, Qt.AlignCenter)
        dlg.exec_()

    def apply_theme(self, theme: str, restore: bool = False):
        """Apply light or dark theme to the application."""
        app = QApplication.instance()
        # The style's own palette, not QPalette(): a default one copies the palette in use, so switching
        # back to light would keep the dark theme's white text on light buttons.
        palette = app.style().standardPalette()
        if theme == 'dark':
            palette.setColor(QPalette.Window, QColor(53, 53, 53))
            palette.setColor(QPalette.WindowText, Qt.white)
            palette.setColor(QPalette.Base, QColor(35, 35, 35))
            palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
            palette.setColor(QPalette.ToolTipBase, Qt.white)
            palette.setColor(QPalette.ToolTipText, Qt.white)
            palette.setColor(QPalette.Text, Qt.white)
            palette.setColor(QPalette.Button, QColor(53, 53, 53))
            palette.setColor(QPalette.ButtonText, Qt.white)
            palette.setColor(QPalette.BrightText, Qt.red)
            palette.setColor(QPalette.Link, QColor(42, 130, 218))
            palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
            palette.setColor(QPalette.HighlightedText, Qt.black)
            # Shades used by panel headers, frames and toolbar hover; Qt's defaults are light-theme greys.
            palette.setColor(QPalette.Light, QColor(80, 80, 80))
            palette.setColor(QPalette.Midlight, QColor(66, 66, 66))
            palette.setColor(QPalette.Mid, QColor(38, 38, 38))
            palette.setColor(QPalette.Dark, QColor(30, 30, 30))
            palette.setColor(QPalette.Shadow, QColor(15, 15, 15))
            self.dark_mode_action.setChecked(True)
        else:
            self.light_mode_action.setChecked(True)
        app.setPalette(palette)
        # A widget with its own stylesheet keeps the palette it was polished with, which left the toolbar
        # labels white on the light theme; re-polishing picks the new colours up.
        for owner in (self, *self.findChildren(QWidget)):  # includes the tool windows, which are children
            if owner.styleSheet():
                for widget in (owner, *owner.findChildren(QWidget)):  # the children inherit the stylesheet
                    widget.style().unpolish(widget)
                    widget.style().polish(widget)
        for name, action in self._toolbar_actions.items():
            action.setIcon(toolbar_icon(name, dark=theme == "dark"))
        self._refresh_manual_icon()
        # The workspace's own style sheet is written in palette(...) colours, which are read when it is
        # set: setting it again is what makes the windows follow the theme.
        self.workspace.setStyleSheet(self._workspace_style)
        if not restore:
            settings = app_settings()
            settings.setValue("theme", theme)

    def load_configurations(self):
        """List the configurations of the Configurations folder and reselect the last one used."""
        self.config_list.clear()
        self.configurations, errors = read_configurations(CONFIG_DIR)
        for error in errors:
            self.log_verbose(error)
        if not self.configurations:
            self.configurations = [validate_config(DEFAULT_CONFIGURATION)]
        for config in self.configurations:
            self.config_list.addItem(QListWidgetItem(config["name"]))
        last_name = app_settings().value("last_configuration", "", type=str)
        selected = next((i for i, cfg in enumerate(self.configurations) if cfg["name"] == last_name), 0)
        self.config_list.setCurrentRow(selected)
        self.on_config_selected(self.config_list.item(selected))
        self.log_verbose(f"Loaded {len(self.configurations)} configuration(s).")

    def open_form_designer(self):
        """Open the Form Designer dialog."""
        designer = FormDesigner(self)
        designer.saved.connect(lambda p: self.load_configurations())
        designer.exec_()

    # --- The workspace: tool panes, saved layouts and desktops ---

    def tool_widget(self, name):
        """The widget of a tool window that was opened, else None (nothing is created here)."""
        pane = self.tool_panes.get(name)
        return pane.widget() if pane is not None else None

    def open_tool(self, name, title, factory, area="center"):
        """Show a tool in the workspace, building it the first time. Returns (widget, is new)."""
        pane, created = self.tool_panes.get(name), False
        if pane is None:
            widget = factory()
            pane = make_pane(title, widget, f"pane_{name}")
            add_pane(self.workspace, pane, area, beside=self.database_pane if area != "center" else None)
            self.tool_panes[name] = pane
            created = True
            action = self._toolbar_actions.get(name)
            if action is not None and action.isCheckable():
                # However the window is opened or closed - its tab's close button, a saved desktop,
                # Reset layout - the toolbar button follows.
                pane.viewToggled.connect(action.setChecked)
            if isinstance(widget, QDialog):
                # Esc in an embedded dialog would hide it inside its window and leave an empty one;
                # close the window and keep the widget ready for the next time it is opened.
                widget.finished.connect(lambda _result, p=pane, w=widget: (p.toggleView(False), w.show()))
            self._apply_layout()      # place it where the saved arrangement wants it
        pane.toggleView(True)
        pane.setAsCurrentTab()
        return pane.widget(), created

    def _toggle_tool(self, name, shown, show):
        """The toolbar switch of a tool window: open it, or close the one that is open."""
        pane = self.tool_panes.get(name)
        if shown:
            show()
        elif pane is not None:
            pane.toggleView(False)   # hidden, not destroyed: reopening shows what it recorded meanwhile

    # --- the script's side: Write window, system variables, keys ---

    def write_message(self, level: str, text: str):
        """A line of the panel script's output. Errors go to the Debug log as well."""
        entry = (time.time(), level, text)
        self.write_history.append(entry)
        window = self.tool_widget("write")
        if window is not None:
            window.add(*entry)
        if level == "error":
            self.log_verbose(text)

    def script_watch(self):
        """(the script's globals, the names CAN Expert put there), for the Write window's watch."""
        runtime = self.script_runtime
        return (runtime.namespace, runtime.hidden_names) if runtime is not None else ({}, ())

    def open_write(self):
        """Write window: the script's output and its variables."""
        window, created = self.open_tool("write", "Write", lambda: WriteWindow(self, self.clock, self.script_watch),
                                         "bottom")
        if created:
            for entry in list(self.write_history):
                window.add(*entry)
        return window

    def open_sysvars(self):
        """System variables: the values the script, the windows and the user share."""
        window, _ = self.open_tool("sysvars", "System Variables", lambda: SystemVariablesWindow(self.sysvars, self))
        return window

    def _on_sysvar_changed(self, name, value, when):
        """A system variable changed: numeric ones are kept for the Logger, which plots them."""
        if isinstance(value, str):
            return
        definition = self.sysvars.definition(name)
        unit = definition.unit if definition is not None else ""
        self.sysvar_history.append((when, name, value, unit))
        logger = self.tool_widget("logger")
        if logger is not None:
            logger.on_sysvar(name, value, when, unit)

    def _watch_keys(self, on: bool):
        """While a measurement runs, key presses reach the script's @on_key handlers."""
        application = QApplication.instance()
        if on and not self._keys_watched:
            application.installEventFilter(self)
        elif not on and self._keys_watched:
            application.removeEventFilter(self)
        self._keys_watched = on

    def eventFilter(self, watched, event):
        if event.type() == QEvent.KeyPress and watched.isWindowType() and self.script_runtime is not None:
            self._key_pressed(event)
        return super().eventFilter(watched, event)

    def _key_pressed(self, event):
        """Hand a key to the script - unless it is being typed into a field, or a dialog is waiting."""
        if event.isAutoRepeat() or QApplication.activeModalWidget() is not None:
            return
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QAbstractSpinBox)) or \
                (isinstance(focus, (QPlainTextEdit, QTextEdit)) and not focus.isReadOnly()) or \
                (isinstance(focus, QComboBox) and focus.isEditable()):
            return
        text = event.text()
        key = text if len(text) == 1 and text.isprintable() and not text.isspace() else \
            QKeySequence(event.key()).toString()
        if key:
            self.script_runtime.post("key", key, None)

    def open_trace(self):
        """Trace window, with the frames already recorded."""
        trace, created = self.open_tool("trace", "Trace", lambda: TraceWindow(self, self.symbols, self.clock), "bottom")
        if created:
            for frame in list(self.frame_history):
                trace.add_frame(*frame)
            trace.flush()
        self._update_diagnostic_ids()
        return trace

    def _update_diagnostic_ids(self):
        """Which identifiers the Trace assembles in its transport view: the ones this configuration uses."""
        trace = self.tool_widget("trace")
        config = self.session_config or self.monitor_config or self.active_config
        if trace is None or not config:
            return
        transport = uds_transport(config)
        identifiers = {config.get("request_id"), transport["request_id"], *config.get("response_ids", [])}
        trace.set_diagnostic_ids({i for i in identifiers if i is not None}, transport["address_byte"])

    def open_can_logger(self):
        """CAN Logger window, filled with the signals of the frames already recorded."""
        logger, created = self.open_tool("logger", "CAN Logger",
                                         lambda: CANLoggerWindow(self, self.symbols, self.clock))
        if created:
            for timestamp, direction, can_id, data, _extended in list(self.frame_history):
                if direction == "RX":
                    logger.on_can_message(can_id, data, timestamp)
            for when, name, value, unit in list(self.sysvar_history):
                logger.on_sysvar(name, value, when, unit)
        return logger

    def open_data(self):
        """Data window, filled from the frames already recorded."""
        data, created = self.open_tool("data", "Data", lambda: DataWindow(self, self.symbols))
        if created:
            for frame in list(self.frame_history):
                data.on_frame(*frame)
            data.rebuild()
        return data

    def open_statistics(self):
        """Statistics window, counting from the frames already recorded."""
        statistics, created = self.open_tool("statistics", "Statistics",
                                             lambda: StatisticsWindow(self, self.symbols, self.session_bitrate))
        if created:
            for frame in list(self.frame_history):
                statistics.on_frame(*frame)
            statistics.refresh()
        return statistics

    def session_bitrate(self) -> int:
        """The bit rate the measurement runs at, for the bus load; 0 when nothing is connected."""
        config = self.session_config or self.active_config or {}
        return int(config.get("bitrate", 0)) if self.can_bus is not None else 0

    def open_transmit(self):
        """Transmit window: send messages once or cyclically."""
        widget, _ = self.open_tool("transmit", "Transmit",
                                   lambda: TransmitWindow(self, self.symbols, self.send_can_message,
                                                          app_settings()), "bottom")
        return widget

    def open_simulation(self):
        """Simulated nodes: send the messages of the symbol databases' nodes."""
        widget, _ = self.open_tool("simulation", "Simulated nodes",
                                   lambda: SimulationWindow(self, self.symbols, self.send_can_message,
                                                            app_settings()), "bottom")
        return widget

    def open_uds_console(self):
        """UDS Console window: any ISO 14229 service and the fault memory, without an ODX file."""
        widget, _ = self.open_tool("console", "UDS Console",
                                   lambda: UdsConsoleWindow(self, self.active_session))
        return widget

    def open_diagnostic_window(self):
        """ODX Diagnostic Window pane."""
        widget, _ = self.open_tool("diagnostics", "Diagnostics", lambda: DiagnosticWindow(self))
        return widget

    def edit_symbol_databases(self):
        """Add or remove the DBC files every window uses."""
        dialog = SymbolDatabaseDialog(self.symbols, self)
        dialog.exec_()
        self.log_verbose(f"Symbol databases: {len(self.symbols.messages())} message(s) "
                         f"from {len(self.symbols.databases)} file(s)")
        return dialog

    # --- Layouts (CANoe's desktops) ---

    def layout_state(self):
        """Everything about the arrangement: the docked panels, and the workspace windows."""
        return self.saveState(), self.workspace.saveState()

    def apply_layout_state(self, layout):
        """Put the panels and the workspace windows back as layout describes them."""
        panels, workspace = layout
        if panels:
            self.restoreState(panels)
        if workspace:
            self._workspace_state = workspace
            self.workspace.restoreState(workspace)

    def save_layout(self):
        panels, workspace = self.layout_state()
        self._settings.setValue(LAYOUT_GEOMETRY, self.saveGeometry())
        self._settings.setValue(LAYOUT_STATE, panels)
        self._settings.setValue(LAYOUT_WORKSPACE, workspace)

    def restore_layout(self):
        """Put the window, its panels and its workspace windows back where they were left."""
        geometry = self._settings.value(LAYOUT_GEOMETRY)
        if geometry:
            self.restoreGeometry(geometry)
        self.apply_layout_state((self._settings.value(LAYOUT_STATE), self._settings.value(LAYOUT_WORKSPACE)))

    def _apply_layout(self):
        """Re-apply the workspace arrangement after a window was added: a saved state only places the
        windows that existed when it was saved."""
        if self._workspace_state:
            self.workspace.restoreState(self._workspace_state)

    def desktops(self) -> list[str]:
        self._settings.beginGroup(DESKTOPS)
        names = sorted(self._settings.childGroups())
        self._settings.endGroup()
        return names

    def save_desktop(self, name=None):
        """Keep the current arrangement under a name, as CANoe keeps desktops."""
        if name is None:
            name, ok = QInputDialog.getText(self, "Save desktop", "Name of this window arrangement:")
            if not ok or not name.strip():
                return None
        name = name.strip()
        panels, workspace = self.layout_state()
        self._settings.setValue(f"{DESKTOPS}/{name}/panels", panels)
        self._settings.setValue(f"{DESKTOPS}/{name}/workspace", workspace)
        self._refresh_desktop_menu()
        self._set_status(f"Desktop '{name}' saved", "green")
        return name

    def apply_desktop(self, name):
        layout = (self._settings.value(f"{DESKTOPS}/{name}/panels"),
                  self._settings.value(f"{DESKTOPS}/{name}/workspace"))
        if any(layout):
            self.apply_layout_state(layout)
            self._set_status(f"Desktop '{name}'", "gray")

    def reset_layout(self):
        """Back to the arrangement the window starts with."""
        self.apply_layout_state(self._default_layout)
        for pane in self.tool_panes.values():
            pane.toggleView(False)
        self.database_pane.toggleView(self.app_database is not None)

    def _refresh_desktop_menu(self):
        menu = getattr(self, "_desktop_menu", None)
        if menu is None:
            return
        menu.clear()
        for name in self.desktops():
            menu.addAction(name, lambda checked=False, n=name: self.apply_desktop(n))
        if not self.desktops():
            menu.addAction("(none saved yet)").setEnabled(False)

    # --- Recording and offline replay ---

    def start_recording(self):
        """Write every frame of the measurement to a file; python-can picks the format from the name."""
        if self.recorder is not None:
            return None
        path, _ = QFileDialog.getSaveFileName(self, "Record the measurement to a file",
                                              str(APP_DIR / "measurement.blf"), LOG_FILE_FILTER)
        if not path:
            return None
        try:
            self.recorder = Recorder(path)
        except Exception as exc:                          # unwritable path, or a suffix python-can refuses
            QMessageBox.critical(self, "Recording", f"Cannot record to {Path(path).name}:\n{exc}")
            return None
        self._update_recording_actions()
        self._set_status(f"Recording to {Path(path).name}", "green")
        self.log_verbose(f"Recording to {path}")
        return self.recorder

    def stop_recording(self):
        """Close the recording file, if one is open."""
        recorder, self.recorder = self.recorder, None
        if recorder is None:
            return None
        try:
            recorder.stop()
        except Exception as exc:
            self.log_verbose(f"Closing the recording failed: {exc}")
        self._update_recording_actions()
        self.log_verbose(f"Recorded {recorder.count} frame(s) to {recorder.path}")
        self._set_status(f"Recorded {recorder.count} frame(s) to {recorder.name}", "gray")
        return recorder

    def _update_recording_actions(self):
        record = getattr(self, "_record_action", None)
        if record is not None:
            record.setEnabled(self.recorder is None)
            self._stop_record_action.setEnabled(self.recorder is not None)

    def replay_log(self, path=None):
        """Offline mode: play a recorded file back into the Trace window, the CAN Logger and the panels."""
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Replay a recorded file", str(APP_DIR), LOG_FILE_FILTER)
        if not path:
            return None
        if self.replay is not None:
            self.replay.close()                           # one replay at a time; it owns a reading thread
        self.clock.begin()                           # counted from the recording's first frame
        trace = self.open_trace()
        trace.set_source(f"Offline: {Path(path).name}")
        self.replay = ReplayDialog(path, self.replay_frames, self)
        self.replay.show()
        self.log_verbose(f"Replaying {path}")
        return self.replay

    def edit_configuration(self, item=None):
        if self.can_bus is None:
            self._open_configuration_dialog(dict(self.active_config or {}))

    def create_new_config(self):
        self._open_configuration_dialog({})

    def _open_configuration_dialog(self, config):
        dialog = ConfigurationDialog(self, config, CONFIG_DIR, settings=self._settings)
        dialog.accepted.connect(self.load_configurations)
        dialog.show()

    def import_config(self):
        """Copy a configuration file into the Configurations folder."""
        file_path, _ = QFileDialog.getOpenFileName(self, "Import Configuration", "", "JSON Files (*.json)")
        if not file_path:
            return
        try:
            save_configuration(json.loads(Path(file_path).read_text(encoding="utf-8")), CONFIG_DIR)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            QMessageBox.critical(self, "Error", f"Failed to import configuration: {exc}")
            return
        self.load_configurations()
        self.status_label.setText("Configuration imported successfully")

    def export_config(self):
        """Export current configuration"""
        if not self.active_config:
            QMessageBox.warning(self, "Warning", "No configuration selected")
            return
            
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Export Configuration", "", "JSON Files (*.json)"
        )
        
        if file_path:
            try:
                with open(file_path, 'w') as f:
                    json.dump(self.active_config, f, indent=2)
                self.status_label.setText("Configuration exported successfully")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export configuration: {str(e)}")
                
    # --- Connect / Disconnect / UDS ---

    def on_connect_clicked(self):
        """Connect: load the active configuration's panel database and run its script on the bus."""
        if self.can_bus is not None:
            return
        if self.activity_scanner and self.activity_scanner.isRunning():
            self._set_status("Wait for the activity scan to finish", "orange")
            return
        if not self.active_config or not self.selected_channel_config:
            QMessageBox.warning(self, "Connection", "Select a configuration and a CAN receiver first.")
            return
        self.stop_ecu_monitor()  # the session sends TesterPresent itself
        try:
            config = self.session_configuration()
            database = load_application_database(config["database_family"], DATABASES_DIR)
            if database is None:
                raise ValueError("No matching database. Create a panel in Form Designer first.")
            # Validate/build before opening hardware, so errors leave a usable UI.
            self.build_application_ui(database)
            cfg = self.selected_channel_config
            setup = load_setup(self._settings, cfg)
            self.can_bus = open_configured(cfg, config["bitrate"], setup, config)
            if setup.describe():
                self.log_verbose(f"Channel setup: {setup.describe()}")
            self.session_config = config
            self.connected_channel_config = dict(cfg)
            self._remember_channel(cfg)
            self.session_generation += 1
            generation = self.session_generation
            self.clock.begin(time.time())            # a new measurement: relative times count from here
            worker = CanWorker(self.can_bus, config, tester_present=not setup.listen_only)
            mailbox = ReceiveMailbox(self.can_bus, worker.message_sent.emit)
            worker.add_mailbox(mailbox)
            worker.message_received.connect(lambda msg, g=generation: self.on_can_message(msg) if g == self.session_generation else None)
            worker.message_sent.connect(lambda cid, data, g=generation: self.dispatch_frame(time.time(), "TX", cid, data) if g == self.session_generation else None)
            worker.error_occurred.connect(lambda error, g=generation: self._session_failed(error) if g == self.session_generation else None)
            worker.error_frame.connect(lambda ts, g=generation: self._on_error_frame(ts) if g == self.session_generation else None)
            worker.bus_status.connect(lambda status, g=generation: self._on_bus_status(status) if g == self.session_generation else None)
            self.workers["main"] = worker
            self.sysvars.reset()                   # every variable back to its initial value
            runtime = ScriptRuntime(mailbox, config, self.panel.values(), self, sysvars=self.sysvars)
            runtime.value_changed.connect(lambda name, value, g=generation: self.panel.set_value(name, value) if g == self.session_generation and self.panel else None)
            runtime.message.connect(lambda level, text, g=generation: self.write_message(level, text)
                                    if g == self.session_generation else None)
            runtime.flashing_available.connect(lambda ok, g=generation: self._set_flashing_available(ok) if g == self.session_generation else None)
            runtime.flash_progress.connect(lambda done, total, text, g=generation: self._on_flash_progress(done, total, text) if g == self.session_generation else None)
            runtime.flash_finished.connect(lambda ok, text, g=generation: self._on_flash_finished(ok, text) if g == self.session_generation else None)
            self.panel.control_changed.connect(lambda name, value: runtime.post("control", name, value))
            self.script_runtime = runtime
            self._bus_state = None
            self._watch_keys(True)
            worker.start()
            script_path = Path(database["source_path"]).with_name(Path(database["source_path"]).stem + "_script.py")
            runtime.dbc = self.panel.dbc
            runtime.handlers = self.panel.handlers()
            runtime.start(script_path)
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            self._set_flashing_available(False)
            self.flashing_toolbar_item.setVisible(True)
            self.config_list.setEnabled(False)
            self.database_pane.toggleView(True)
            self.database_pane.setAsCurrentTab()
            self.channels_dock.show()
            self._minimize_side_panels()
            self.refresh_channel_list()
            self._update_diagnostic_ids()
            self._set_status(f"Connected — {Path(database['source_path']).name}", "green")
            self.log_verbose(f"Loaded {database['source_path']}")
        except Exception as exc:
            self.on_disconnect_clicked()
            self._set_status(f"Connection failed: {exc}", "red")
            self.log_verbose(str(exc))

    def session_configuration(self) -> dict:
        """The selected configuration as a session uses it: validated, with its ISO-TP settings folded in
        (they live in the settings, not in the configuration file). ValueError when it is invalid."""
        config = validate_config(self.active_config)
        return apply_transport(config, load_transport(self._settings, config["name"]))

    def active_session(self):
        """(bus, worker, configuration) while connected, for the UDS console; else None."""
        worker = self.workers.get("main")
        if self.can_bus is None or worker is None or self.session_config is None:
            return None
        return self.can_bus, worker, self.session_config

    def _session_failed(self, error):
        self.log_verbose(error)
        self.on_disconnect_clicked()
        self._set_status(error, "red")

    def on_disconnect_clicked(self):
        self.session_generation += 1
        self._watch_keys(False)
        if self.flash_runner is not None:
            self.flash_runner.cancel()      # the bus is about to go away under it
        self._close_flash_dialog()
        self.flashing_toolbar_item.setVisible(False)
        if self.script_runtime:
            self.script_runtime.stop()  # runs @on_stop handlers, then revokes the bus
            self.script_runtime = None
        for worker in self.workers.values():
            worker.stop()
        self.workers.clear()
        if self.can_bus:
            try:
                self.can_bus.shutdown()
            except Exception as exc:
                self.log_verbose(str(exc))
        self.can_bus = None
        self.session_config = None
        self.connected_channel_config = None
        self.stop_recording()
        self._label_channels()
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.config_list.setEnabled(True)
        self._restore_side_panels()
        self.database_pane.toggleView(False)
        self.channels_dock.show()
        self._set_status("Disconnected", "gray")
        self.clear_application_ui()
        self._update_nodes()

    # --- ECU check while no database is connected ---

    def disconnect_database(self):
        """Toolbar Disconnect: close the database session, then keep checking its ECUs so the CAN Channels
        tree still shows which ones respond."""
        channel, config = self.connected_channel_config, self.session_config
        self.on_disconnect_clicked()
        if channel and config:
            self.start_ecu_monitor(channel, config)

    def start_ecu_monitor(self, channel_config, config):
        """Send TesterPresent on the channel at the configuration's interval and watch the ECU replies:
        each ECU shows Responding, or Lost connection after the node loss timeout."""
        self.stop_ecu_monitor()
        channel = channel_config.get("channel", 0)
        setup = load_setup(self._settings, channel_config)
        if setup.listen_only:
            self.log_verbose("ECU check not started: it sends TesterPresent, and the channel is set to "
                             "listen-only (right-click the channel, Channel setup...)")
            return
        try:
            bus = open_configured(channel_config, config["bitrate"], setup, config)
        except Exception as exc:
            self.log_verbose(f"ECU check not started: {exc}")
            return
        worker = CanWorker(bus, config)
        self.clock.begin(time.time())
        worker.message_received.connect(lambda msg, w=worker: self._on_monitor_message(w, msg))
        worker.message_sent.connect(lambda can_id, data, w=worker: self.dispatch_frame(time.time(), "TX", can_id, data)
                                    if w is self.ecu_monitor else None)
        worker.error_occurred.connect(lambda error, w=worker: self._monitor_failed(w, error))
        self.ecu_monitor, self.monitor_bus = worker, bus
        self.monitor_channel, self.monitor_config = dict(channel_config), config
        self._update_diagnostic_ids()
        worker.start()
        self._label_channels()
        self._update_nodes()
        self.log_verbose(f"Checking ECUs on {channel_config['interface']} channel {channel}: TesterPresent to "
                         f"0x{config['request_id']:X} every {config['tester_present_interval_seconds']:g} s "
                         f"(right-click the channel to stop)")

    def stop_ecu_monitor(self):
        worker, bus = self.ecu_monitor, self.monitor_bus
        if worker is None:
            return
        self.ecu_monitor = self.monitor_bus = None
        worker.stop()
        try:
            bus.shutdown()
        except Exception as exc:
            self.log_verbose(str(exc))
        self._label_channels()
        self._update_nodes()
        self.log_verbose("Stopped checking ECUs")

    def _on_monitor_message(self, worker, msg):
        """Every frame seen while the ECUs are checked: the trace and the logger see the whole bus,
        the node tree only the configured response identifiers."""
        config = self.monitor_config
        if worker is not self.ecu_monitor:
            return
        self.dispatch_frame(msg["timestamp"], "RX", msg["arbitration_id"], msg["data"],
                            msg.get("is_extended_frame", False))
        if msg["arbitration_id"] not in config["response_ids"]:
            return
        if msg.get("is_extended_frame", False) != (not config["identifier_11_bit"]):
            return
        self.node_states[(channel_key(self.monitor_channel), msg["arbitration_id"])] = {
            "last_seen": time.monotonic(), "timeout": config["node_timeout_seconds"]}
        self._update_nodes()

    def _monitor_failed(self, worker, error):
        if worker is self.ecu_monitor:
            self.log_verbose(f"ECU check stopped: {error}")
            self.stop_ecu_monitor()

    def _channel_menu(self, position):
        item = self.channel_list.itemAt(position)
        cfg = item.data(0, Qt.UserRole) if item else None
        if not cfg:
            return
        menu = QMenu(self)
        if self.ecu_monitor and channel_key(cfg) == channel_key(self.monitor_channel):
            menu.addAction("Stop checking ECUs", self.stop_ecu_monitor)
        elif self.can_bus is None and self.active_config:
            menu.addAction(f"Check ECUs with \"{self.active_config.get('name', '')}\"", lambda: self.check_ecus(cfg))
        menu.addSeparator()
        menu.addAction("Channel setup...", lambda: self.edit_channel_setup(cfg))
        menu.exec_(self.channel_list.viewport().mapToGlobal(position))

    def edit_channel_setup(self, channel_config):
        """Sample point, listen-only, receive filter and bit rate detection for one adapter channel."""
        key = channel_key(channel_config)
        in_use = self._channel_checked(key) or (self.can_bus is not None and self.connected_channel_config is not None
                                                and key == channel_key(self.connected_channel_config))
        bitrate = int((self.session_config or self.active_config or {}).get("bitrate", 500000))
        dialog = ChannelSetupDialog(channel_config, load_setup(self._settings, channel_config), bitrate, self,
                                    in_use=in_use)
        if dialog.exec_() == ChannelSetupDialog.Accepted:
            save_setup(self._settings, channel_config, dialog.setup)
            self._label_channels()
            self.log_verbose(f"Channel setup of [{channel_config['interface']}] Ch {channel_config.get('channel', 0)}: "
                             f"{dialog.setup.describe() or 'the defaults'}"
                             + (" - used from the next connection" if in_use else ""))
        return dialog

    def check_ecus(self, channel_config):
        """Start the ECU check on a channel with the selected configuration, without loading its database."""
        try:
            config = self.session_configuration()
        except ValueError as exc:
            self._set_status(f"Invalid configuration: {exc}", "red")
            return
        self.start_ecu_monitor(channel_config, config)

    def _minimize_side_panels(self):
        """Give the loaded database the room: collapse Configuration, CAN Channels and Log to strips."""
        self._left_split = [self.config_dock.height(), self.channels_dock.height()]
        for dock in (self.config_dock, self.channels_dock, self.log_dock):
            title_bar = dock.titleBarWidget()
            if not dock.isHidden() and not title_bar.is_minimized:
                title_bar.minimize()
                self._auto_minimized.append(title_bar)

    def _restore_side_panels(self):
        """Undo _minimize_side_panels; panels the user minimized or restored themselves are left alone."""
        for title_bar in self._auto_minimized:
            if title_bar.is_minimized:
                title_bar.restore()
        if self._auto_minimized and self._left_split and min(self._left_split) > 0:
            self.resizeDocks([self.config_dock, self.channels_dock], self._left_split, Qt.Vertical)
        self._auto_minimized = []
        self._left_split = None

    # --- Flashing ---

    def _set_flashing_available(self, available):
        """Whether the panel script offers a Flashing(); flashing itself needs only a connection."""
        self.script_flash = available
        action = self._toolbar_actions["flashing"]
        action.setEnabled(self.active_session() is not None and self.flash_dialog is None)
        action.setToolTip("Flash ECU firmware with the database script's Flashing()" if available else
                          "Flash ECU firmware with the built-in sequence\n"
                          "(the database script does not define Flashing(api, firmware))")

    def flash_profile(self):
        """The built-in sequence's settings, as they were last left."""
        try:
            return FlashProfile.from_dict(json.loads(self._settings.value(FLASH_PROFILE, "{}", type=str) or "{}"))
        except (TypeError, ValueError):
            return FlashProfile()

    def open_flashing(self):
        """Choose a firmware file, then flash it with the script's Flashing() or the built-in sequence."""
        if self.active_session() is None:
            return
        settings = app_settings()
        firmware = choose_firmware(self, settings.value("last_firmware_dir", str(APP_DIR), type=str))
        if firmware is None:
            return
        settings.setValue("last_firmware_dir", str(Path(firmware.path).parent))
        dialog = FlashDialog(firmware, self.flash_profile(), script_available=self.script_flash, parent=self)
        if dialog.exec_() != QDialog.Accepted:
            return
        self._settings.setValue(FLASH_PROFILE, json.dumps(dialog.profile.to_dict()))
        if dialog.use_script():
            self.start_flashing(firmware)
        else:
            self.start_built_in_flash(firmware, dialog.profile)

    def start_built_in_flash(self, firmware, profile):
        """Flash without a panel script: the ISO 14229 sequence the profile describes, on its own thread."""
        if self.active_session() is None or self.flash_dialog is not None:
            return
        self.flash_runner = FlashRunner(self.active_session, self)
        self.flash_runner.logged.connect(self.log_verbose)
        self.flash_runner.progress.connect(self._on_flash_progress)
        self.flash_runner.finished.connect(self._on_flash_finished)
        self._toolbar_actions["flashing"].setEnabled(False)
        self.flash_dialog = progress_dialog(self, firmware, self.flash_runner.cancel)
        self.log_verbose(f"Flashing {firmware.path}: {firmware.size} bytes in "
                         f"{len(firmware.segments)} segment(s), built-in sequence")
        if not self.flash_runner.start(firmware, profile):
            self._on_flash_finished(False, "Flashing could not be started.")

    def start_flashing(self, firmware):
        if self.script_runtime is None:
            return
        self._toolbar_actions["flashing"].setEnabled(False)
        self.flash_dialog = progress_dialog(self, firmware, self.script_runtime.cancel_flash)
        self.log_verbose(f"Flashing {firmware.path}: {firmware.size} bytes in {len(firmware.segments)} segment(s)")
        self.script_runtime.start_flash(firmware)

    def _on_flash_progress(self, done, total, text):
        update_progress(self.flash_dialog, done, total, text)

    def _close_flash_dialog(self):
        dialog, self.flash_dialog = self.flash_dialog, None
        close_progress(dialog)

    def _on_flash_finished(self, ok, text):
        self._close_flash_dialog()
        self.flash_runner = None
        self._set_flashing_available(self.script_flash)
        self.log_verbose(f"Flashing {'succeeded' if ok else 'failed'}: {text}")
        self._set_status(f"Flashing {'complete' if ok else 'failed'}", "green" if ok else "red")
        report_result(self, ok, text)

    def closeEvent(self, event):
        self._watch_keys(False)
        self.save_layout()          # before the panes go away, so they come back where they were
        self.node_timer.stop()
        self.stop_ecu_monitor()
        if self.replay is not None:
            self.replay.close()
        self.on_disconnect_clicked()
        if self.activity_scanner:
            self.activity_scanner.requestInterruption()
            self.activity_scanner.wait()
        event.accept()

    def clear_application_ui(self):
        """Remove the loaded database's pages: the Database window's content and the other page windows."""
        while self.app_db_layout.count():
            item = self.app_db_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for pane in self.page_panes:
            self.workspace.removeDockWidget(pane)
            pane.deleteLater()
        self.page_panes = []
        self.database_pane.setWindowTitle("Database")
        self.app_database = None
        self.panel = None

    # --- Application database UI ---

    def build_application_ui(self, app_db):
        """Each page of the database a window of the workspace, as CANoe's panels are: the first in the
        Database window, the others tabbed beside it, ready to be split off, floated or closed."""
        self.clear_application_ui()
        # The zoom of a page belongs to the database family, so a newer dated version keeps it.
        family = re.sub(r"_\d{4}-\d{2}-\d{2}$", "", Path(app_db.get("source_path") or "panel").stem)
        self.panel = PanelView(app_db, self.send_can_message, self.log_verbose, tabs=False,
                               zooms=self._page_zooms(family))
        self.app_db_layout.addWidget(self.panel)              # the description, when the database has one
        self.panel.setVisible(bool(self.panel.description))
        for index, (name, window) in enumerate(self.panel.page_windows):
            window.zoom_changed.connect(lambda text, n=name: self._settings.setValue(f"{PANEL_ZOOM}/{family}/{n}", text))
            if index == 0:
                self.app_db_layout.addWidget(window, 1)
                self.database_pane.setWindowTitle(name if len(self.panel.page_windows) > 1 else "Database")
                continue
            pane = make_pane(name, window, f"pane_page_{index}")
            add_pane(self.workspace, pane, beside=self.database_pane)
            self.page_panes.append(pane)
        if self.page_panes:
            self._apply_layout()
            self.database_pane.setAsCurrentTab()
        self.app_database = app_db

    def _page_zooms(self, family) -> dict:
        self._settings.beginGroup(f"{PANEL_ZOOM}/{family}")
        try:
            return {name: self._settings.value(name, "", type=str) for name in self._settings.childKeys()}
        finally:
            self._settings.endGroup()

    def send_can_message(self, can_id, data, extended=None):
        if self.can_bus is None:
            raise RuntimeError("Connect before sending CAN messages")
        if extended is None:
            extended = not self.session_config.get("identifier_11_bit", True)
        payload = bytes(data)
        if len(payload) > 8:
            raise ValueError("Classic CAN messages cannot exceed eight bytes")
        message = can.Message(arbitration_id=can_id, data=payload, is_extended_id=extended, check=True)
        self.can_bus.send(message)
        self.dispatch_frame(time.time(), "TX", can_id, payload, extended)

    def dispatch_frame(self, timestamp, direction, can_id, data, extended=False):
        """One frame of the measurement, from wherever: the monitor, the recording, and every window.

        The timestamp is the adapter's for received frames, so the trace and the logger share one clock.
        """
        data = bytes(data)
        self.clock.see(timestamp)
        self.frame_history.append((float(timestamp), direction, int(can_id), data, bool(extended)))
        if self.recorder is not None:
            try:
                self.recorder.write(timestamp, direction, can_id, data, extended)
            except Exception as exc:                      # a full disk must not take the measurement down
                self.log_verbose(f"Recording stopped: {exc}")
                self.stop_recording()
        self.log_can(direction, can_id, data, timestamp)
        # Every open window that wants frames declares on_frame(); nothing else needs to know who is open.
        for name in list(self.tool_panes):
            handler = getattr(self.tool_widget(name), "on_frame", None)
            if handler is not None:
                handler(timestamp, direction, can_id, data, extended)

    def _on_error_frame(self, timestamp):
        """An error frame: no data, so it is counted rather than listed."""
        if self.script_runtime is not None:
            self.script_runtime.post("error_frame", None, timestamp)
        statistics = self.tool_widget("statistics")
        if statistics is not None:
            statistics.on_error_frame(timestamp)

    def _on_bus_status(self, status):
        """The adapter's error state, read while the session runs."""
        statistics = self.tool_widget("statistics")
        if statistics is not None:
            statistics.on_bus_status(status)
        if status.get("state") == "bus off" and self._bus_state != "bus off":
            self.log_verbose("The adapter reports bus off: no frames are being sent or received")
            self._set_status("Bus off — check the wiring, the bit rate and the termination", "red")
        state = status.get("state", "unknown")
        if self._bus_state is not None and state != self._bus_state and self.script_runtime is not None:
            self.script_runtime.post("bus_state", None, state)      # @on_bus_state
        self._bus_state = state

    def replay_frames(self, frames):
        """Frames read back from a recorded file (offline mode): they reach the windows, not the bus."""
        for timestamp, direction, can_id, data, extended in frames:
            self.dispatch_frame(timestamp, direction, can_id, data, extended)

    def on_can_message(self, msg_dict):
        if self.session_config is None:
            return
        can_id, data = msg_dict["arbitration_id"], bytes(msg_dict["data"])
        if can_id in self.session_config["response_ids"] and msg_dict.get("is_extended_frame", False) == (not self.session_config["identifier_11_bit"]):
            self.node_states[(channel_key(self.connected_channel_config), can_id)] = {
                "last_seen": time.monotonic(), "timeout": self.session_config["node_timeout_seconds"]}
            self._update_nodes()
        self.dispatch_frame(msg_dict.get("timestamp") or time.time(), "RX", can_id, data,
                            msg_dict.get("is_extended_frame", False))
        if self.panel:
            try:
                self.panel.on_message(can_id, data)
            except Exception as exc:
                self.log_verbose(f"Panel decode: {exc}")
        if self.script_runtime:
            with self.script_runtime.lock:
                self.script_runtime.values.update(self.panel.values() if self.panel else {})
            self.script_runtime.post("can", can_id, data)

    # --- Configuration selection ---

    def on_config_selected(self, item):
        """Set active configuration when user clicks one in the list."""
        config_name = item.text()
        
        # Find the selected configuration
        for config in self.configurations:
            if config['name'] == config_name:
                self.active_config = config
                app_settings().setValue("last_configuration", config_name)
                self.status_label.setText(f"Active configuration: {config_name}")
                break
                
def main():
    """Start CAN Expert; with --smoke-test only build the main window."""
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    if "--smoke-test" in sys.argv:
        print("startup ok")
        return 0
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
