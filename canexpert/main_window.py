"""
CAN Expert main window: configurations, CAN receivers with their ECU nodes, Connect/Disconnect (load
the newest matching panel database, send periodic TesterPresent, run the panel's Python script),
Flashing, the tool windows and the logs.
"""
import json
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import can
from PyQt5.QtCore import QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canexpert.can_bus import (SUPPORTED_INTERFACES, CanWorker, ChannelActivityScanner, ReceiveMailbox, channel_key,
                               open_channel)
from canexpert.can_logger import CANLoggerWindow
from canexpert.config import (DEFAULT_CONFIGURATION, ConfigurationDialog, read_configurations, save_configuration,
                              validate_config)
from canexpert.designer.form_designer import FormDesigner
from canexpert.diagnostic_window import DiagnosticWindow
from canexpert.flashing import (choose_firmware, close_progress, confirm_flash, progress_dialog, report_result,
                                update_progress)
from canexpert.help_window import show_manual
from canexpert.panel.database import load_application_database, select_database
from canexpert.panel.runtime import ScriptRuntime
from canexpert.panel.view import PanelView
from canexpert.paths import APP_DIR, CONFIG_DIR, DATABASES_DIR
from canexpert.recording import LOG_FILE_FILTER, Recorder, ReplayDialog
from canexpert.symbols import SymbolDatabaseDialog, SymbolDatabases
from canexpert.trace_window import TraceWindow
from canexpert.transmit_window import TransmitWindow
from canexpert.uds_console import UdsConsoleWindow
from canexpert.ui_common import DockTitleBar, app_settings, line_icon, toolbar_icon

# A question mark in a circle, for the manual button beside the Help menu.
MANUAL_ICON = ('<circle cx="12" cy="12" r="9"/><path d="M9.2 9.3a2.9 2.9 0 0 1 5.6 1c0 1.9-2.8 2.4-2.8 4"/>'
               '<path d="M12 17.4h.01" stroke-width="2.2"/>')
LAST_CHANNEL = "last_channel"      # settings: the channel to select and check at the next start
USED_CHANNELS = "used_channels"    # settings: the channels connected before, shown in bold
PASSIVE = "passive_measurement"    # settings: a measurement only listens, never transmits
LAYOUT_GEOMETRY = "layout/geometry"
LAYOUT_STATE = "layout/state"
DESKTOPS = "layout/desktops"       # settings: name -> saved window arrangement (a "desktop")
FRAME_HISTORY = 20000              # frames kept so a window opened later can still show them


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
        # The measurement: what is on the bus, whoever opened it (a database session, a plain
        # measurement or the ECU check). Windows opened later read the history.
        self._settings = app_settings()
        self.symbols = SymbolDatabases(parent=self, settings=self._settings)
        self.frame_history = deque(maxlen=FRAME_HISTORY)
        self.recorder = None
        self.replay = None
        self.tool_docks = {}
        self.measurement_only = False
        self.passive_measurement = False

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
        toolbar.setStyleSheet("""
            QToolBar { spacing: 4px; padding: 6px; border: none; }
            QToolBar QToolButton { padding: 6px 8px; border-radius: 6px; }
            QToolBar QToolButton:hover { background: palette(midlight); }
            QToolBar QToolButton:pressed { background: palette(mid); }
        """)
        self._toolbar_actions = {}
        entries = [
            ("start", "Start", "Watch the selected receiver without loading a panel database", self.start_measurement),
            ("connect", "Connect", "Connect to the selected CAN receiver", self.on_connect_clicked),
            ("disconnect", "Disconnect", "Close the database; the ECUs are still checked with TesterPresent",
             self.disconnect_database),
            ("passive", "Passive", "Passive: a measurement only listens, CAN Expert never transmits", None),
            ("trace", "Trace", "Every frame of the measurement, decoded with the symbol databases",
             self.open_trace),
            ("logger", "CAN Logger", "Plot and export CAN signals", self.open_can_logger),
            ("transmit", "Transmit", "Send messages once or cyclically", self.open_transmit),
            ("console", "UDS Console", "Send any UDS service and read the fault memory (no ODX file needed)",
             self.open_uds_console),
            ("diagnostics", "Diagnostics", "Open ECU diagnostic services", self.open_diagnostic_window),
            ("designer", "Form Designer", "Design panels and edit their Python scripts", self.open_form_designer),
            ("flashing", "Flashing", "Flash ECU firmware using the database's Flashing() function", self.open_flashing),
        ]
        for name, label, hint, callback in entries:
            if name == "trace":
                toolbar.addSeparator()
            action = QAction(toolbar_icon(name), label, self)
            if callback is not None:
                action.triggered.connect(callback)
            if name == "passive":
                action.setCheckable(True)
                action.setChecked(self._settings.value(PASSIVE, False, type=bool))
                action.toggled.connect(self._on_passive_toggled)
                self.passive_action = action
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
            if name == "start":
                self.start_btn = button
            if name == "connect":
                self.connect_btn = button
            elif name == "disconnect":
                self.disconnect_btn = button
                button.setEnabled(False)
        self.addToolBar(toolbar)

        # Central area: empty placeholder (docks sit around it)
        central = QWidget()
        central.setMinimumSize(0, 0)
        central.setMaximumWidth(0)
        self.setCentralWidget(central)

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
        self.app_db_scroll = QScrollArea()
        self.app_db_scroll.setWidgetResizable(True)
        self.app_db_container = QWidget()
        self.app_db_layout = QVBoxLayout()
        self.app_db_container.setLayout(self.app_db_layout)
        self.app_db_scroll.setWidget(self.app_db_container)
        self.database_dock = QDockWidget("Database", self)
        self.database_dock.setObjectName("dock_database")
        self.database_dock.setWidget(self.app_db_scroll)
        self.database_dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.database_dock.setTitleBarWidget(DockTitleBar(self.database_dock, self, Qt.RightDockWidgetArea))
        self.addDockWidget(Qt.RightDockWidgetArea, self.database_dock)
        self.database_dock.hide()

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
        self.can_log.setMaximumBlockCount(5000)
        log_tabs.addTab(self.can_log, "CAN Monitor")
        self.log_dock = QDockWidget("Log", self)
        self.log_dock.setObjectName("dock_log")
        self.log_dock.setWidget(log_tabs)
        self.log_dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.log_dock.setTitleBarWidget(DockTitleBar(self.log_dock, self, Qt.BottomDockWidgetArea))
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)

        self.create_menu()
        # The arrangement the window starts with, so Reset layout has somewhere to go back to.
        self._default_state = self.saveState()
        self._layout_state = None
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

    def log_can(self, direction: str, arbitration_id: int, data: list | bytes):
        """Append a CAN message to the CAN monitor (direction TX or RX, ID, hex data)."""
        if getattr(self, "can_log", None) is None:
            return
        data = list(data) if not isinstance(data, (list, bytearray)) else list(data)
        hex_str = " ".join(f"{b:02X}" for b in data[:8])
        line = f"{self._time_str()}  {direction:>3}  ID: 0x{arbitration_id:X}  {hex_str}"
        self.can_log.appendPlainText(line)

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

        # Measurement menu: what runs on the bus, and what is written to or read from a file
        measurement_menu = menubar.addMenu('Measurement')
        for name in ("start", "connect", "disconnect", "passive"):
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
        tools_menu.addAction('Trace...').triggered.connect(self.open_trace)
        tools_menu.addAction('CAN Logger...').triggered.connect(self.open_can_logger)
        tools_menu.addAction('Transmit...').triggered.connect(self.open_transmit)
        tools_menu.addAction('UDS Console...').triggered.connect(self.open_uds_console)
        tools_menu.addAction('Diagnostic Window...').triggered.connect(self.open_diagnostic_window)
        tools_menu.addSeparator()
        tools_menu.addAction('Symbol databases...').triggered.connect(self.edit_symbol_databases)

        # View menu
        view_menu = menubar.addMenu('View')

        refresh_config_action = view_menu.addAction('Refresh Configurations')
        refresh_config_action.triggered.connect(self.load_configurations)
        refresh_channels_action = view_menu.addAction('Refresh Channels')
        refresh_channels_action.triggered.connect(self.refresh_channel_list)
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
        """The widget of a tool pane that was opened, else None (nothing is created here)."""
        dock = self.tool_docks.get(name)
        return dock.widget() if dock is not None else None

    def open_tool(self, name, title, factory, area=Qt.RightDockWidgetArea):
        """Show a tool in a pane of the main window, building it the first time. Returns (widget, is new)."""
        dock, created = self.tool_docks.get(name), False
        if dock is None:
            widget = factory()
            dock = QDockWidget(title, self)
            dock.setObjectName(f"dock_{name}")
            dock.setWidget(widget)
            dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable |
                             QDockWidget.DockWidgetFloatable)
            dock.setTitleBarWidget(DockTitleBar(dock, self, area))
            self.addDockWidget(area, dock)
            self.tool_docks[name] = dock
            created = True
            if isinstance(widget, QDialog):
                # Esc in an embedded dialog would hide it inside its pane and leave an empty one;
                # close the pane and keep the widget ready for the next time it is opened.
                widget.finished.connect(lambda _result, d=dock, w=widget: (d.close(), w.show()))
            self._apply_layout()      # place it where the saved arrangement wants it
        dock.show()
        dock.raise_()
        return dock.widget(), created

    def open_trace(self):
        """Trace pane: every frame of the measurement, with the frames already recorded."""
        trace, created = self.open_tool("trace", "Trace", lambda: TraceWindow(self, self.symbols),
                                        Qt.BottomDockWidgetArea)
        if created:
            for frame in list(self.frame_history):
                trace.add_frame(*frame)
            trace.flush()
        return trace

    def open_can_logger(self):
        """CAN Logger pane, filled with the signals of the frames already recorded."""
        logger, created = self.open_tool("logger", "CAN Logger",
                                         lambda: CANLoggerWindow(self, self.symbols))
        if created:
            for timestamp, direction, can_id, data, _extended in list(self.frame_history):
                if direction == "RX":
                    logger.on_can_message(can_id, data, timestamp)
        return logger

    def open_transmit(self):
        """Transmit pane: send messages once or cyclically."""
        widget, _ = self.open_tool("transmit", "Transmit",
                                   lambda: TransmitWindow(self, self.symbols, self.send_can_message,
                                                          app_settings()),
                                   Qt.BottomDockWidgetArea)
        return widget

    def open_uds_console(self):
        """UDS Console pane: any ISO 14229 service and the fault memory, without an ODX file."""
        widget, _ = self.open_tool("console", "UDS Console",
                                   lambda: UdsConsoleWindow(self, self.measurement_session))
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

    def save_layout(self):
        self._settings.setValue(LAYOUT_GEOMETRY, self.saveGeometry())
        self._settings.setValue(LAYOUT_STATE, self.saveState())

    def restore_layout(self):
        """Put the window and its panes back where they were left."""
        geometry, state = self._settings.value(LAYOUT_GEOMETRY), self._settings.value(LAYOUT_STATE)
        if geometry:
            self.restoreGeometry(geometry)
        if state:
            self._layout_state = state
            self.restoreState(state)

    def _apply_layout(self):
        """Re-apply the arrangement after a pane was added: restoreState only places panes that exist."""
        state = getattr(self, "_layout_state", None)
        if state:
            self.restoreState(state)

    def desktops(self) -> list[str]:
        self._settings.beginGroup(DESKTOPS)
        names = sorted(self._settings.childKeys())
        self._settings.endGroup()
        return names

    def save_desktop(self, name=None):
        """Keep the current arrangement under a name, as CANoe keeps desktops."""
        if name is None:
            name, ok = QInputDialog.getText(self, "Save desktop", "Name of this window arrangement:")
            if not ok or not name.strip():
                return None
        name = name.strip()
        self._settings.setValue(f"{DESKTOPS}/{name}", self.saveState())
        self._refresh_desktop_menu()
        self._set_status(f"Desktop '{name}' saved", "green")
        return name

    def apply_desktop(self, name):
        state = self._settings.value(f"{DESKTOPS}/{name}")
        if state:
            self._layout_state = state
            self.restoreState(state)
            self._set_status(f"Desktop '{name}'", "gray")

    def reset_layout(self):
        """Back to the arrangement the window starts with."""
        self._layout_state = self._default_state
        self.restoreState(self._default_state)
        for dock in self.tool_docks.values():
            dock.hide()
        self.database_dock.setVisible(self.app_database is not None)

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
        dialog = ConfigurationDialog(self, config, CONFIG_DIR)
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
        self._start_session(with_database=True)

    def start_measurement(self):
        """Start: open the selected receiver and watch it, with no panel database and no script.

        Nothing is transmitted on its own (TesterPresent belongs to a database session or to the ECU
        check); with Passive on, CAN Expert does not transmit at all.
        """
        self._start_session(with_database=False)

    def _start_session(self, with_database: bool):
        if self.can_bus is not None:
            return
        if self.activity_scanner and self.activity_scanner.isRunning():
            self._set_status("Wait for the activity scan to finish", "orange")
            return
        if not self.active_config or not self.selected_channel_config:
            QMessageBox.warning(self, "Connection", "Select a configuration and a CAN receiver first.")
            return
        self.stop_ecu_monitor()  # a database session sends TesterPresent itself; a measurement stays quiet
        passive = with_database is False and self.passive_action.isChecked()
        try:
            config = validate_config(self.active_config)
            database = None
            if with_database:
                database = load_application_database(config["database_family"], DATABASES_DIR)
                if database is None:
                    raise ValueError("No matching database. Create a panel in Form Designer first.")
                # Validate/build before opening hardware, so errors leave a usable UI.
                self.build_application_ui(database)
            cfg = self.selected_channel_config
            self.can_bus = open_channel(cfg, config["bitrate"], passive=passive)
            self.session_config = config
            self.connected_channel_config = dict(cfg)
            self.measurement_only = database is None
            self.passive_measurement = passive
            self._remember_channel(cfg)
            self.session_generation += 1
            generation = self.session_generation
            worker = CanWorker(self.can_bus, config, tester_present=database is not None)
            worker.message_received.connect(lambda msg, g=generation: self.on_can_message(msg) if g == self.session_generation else None)
            worker.message_sent.connect(lambda cid, data, g=generation: self.dispatch_frame(time.time(), "TX", cid, data) if g == self.session_generation else None)
            worker.error_occurred.connect(lambda error, g=generation: self._session_failed(error) if g == self.session_generation else None)
            self.workers["main"] = worker
            if database is not None:
                mailbox = ReceiveMailbox(self.can_bus, worker.message_sent.emit)
                worker.add_mailbox(mailbox)
                runtime = ScriptRuntime(mailbox, config, self.panel.values(), self)
                runtime.value_changed.connect(lambda name, value, g=generation: self.panel.set_value(name, value) if g == self.session_generation and self.panel else None)
                runtime.logged.connect(self.log_verbose)
                runtime.flashing_available.connect(lambda ok, g=generation: self._set_flashing_available(ok) if g == self.session_generation else None)
                runtime.flash_progress.connect(lambda done, total, text, g=generation: self._on_flash_progress(done, total, text) if g == self.session_generation else None)
                runtime.flash_finished.connect(lambda ok, text, g=generation: self._on_flash_finished(ok, text) if g == self.session_generation else None)
                self.panel.control_changed.connect(lambda name, value: runtime.post("control", name, value))
                self.script_runtime = runtime
            worker.start()
            if database is not None:
                script_path = Path(database["source_path"]).with_name(Path(database["source_path"]).stem + "_script.py")
                self.script_runtime.dbc = self.panel.dbc
                self.script_runtime.handlers = self.panel.handlers()
                self.script_runtime.start(script_path)
                self._set_flashing_available(False)
                self.flashing_toolbar_item.setVisible(True)
                self.database_dock.show()
                self.resizeDocks([self.config_dock, self.database_dock], [280, 700], Qt.Horizontal)
                self._minimize_side_panels()
            self.start_btn.setEnabled(False)
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            self.config_list.setEnabled(False)
            self.channels_dock.show()
            self.refresh_channel_list()
            if database is not None:
                self._set_status(f"Connected — {Path(database['source_path']).name}", "green")
                self.log_verbose(f"Loaded {database['source_path']}")
            else:
                mode = "passive, nothing is transmitted" if passive else "no database"
                self._set_status(f"Measurement running ({mode})", "green")
                self.log_verbose(f"Measurement started on {cfg['interface']} channel {cfg.get('channel', 0)} ({mode})")
        except Exception as exc:
            self.on_disconnect_clicked()
            self._set_status(f"Connection failed: {exc}", "red")
            self.log_verbose(str(exc))

    def _on_passive_toggled(self, passive):
        self._settings.setValue(PASSIVE, bool(passive))
        if self.can_bus is not None:
            self._set_status("Passive mode applies to the next measurement", "orange")

    def measurement_session(self):
        """(bus, worker, configuration) while a measurement runs, for the UDS console; else None."""
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
        self.measurement_only = False
        self.passive_measurement = False
        self.stop_recording()
        self._label_channels()
        self.start_btn.setEnabled(True)
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.config_list.setEnabled(True)
        self._restore_side_panels()
        self.database_dock.hide()
        self.channels_dock.show()
        self._set_status("Disconnected", "gray")
        self.clear_application_ui()
        self._update_nodes()

    # --- ECU check while no database is connected ---

    def disconnect_database(self):
        """Toolbar Disconnect: close the database session, then keep checking its ECUs so the CAN Channels
        tree still shows which ones respond. A measurement simply stops: it never asked anything of the bus."""
        channel, config = self.connected_channel_config, self.session_config
        had_database = not self.measurement_only
        self.on_disconnect_clicked()
        if channel and config and had_database:
            self.start_ecu_monitor(channel, config)

    def start_ecu_monitor(self, channel_config, config):
        """Send TesterPresent on the channel at the configuration's interval and watch the ECU replies:
        each ECU shows Responding, or Lost connection after the node loss timeout."""
        self.stop_ecu_monitor()
        channel = channel_config.get("channel", 0)
        try:
            bus = open_channel(channel_config, config["bitrate"])
        except Exception as exc:
            self.log_verbose(f"ECU check not started: {exc}")
            return
        worker = CanWorker(bus, config)
        worker.message_received.connect(lambda msg, w=worker: self._on_monitor_message(w, msg))
        worker.message_sent.connect(lambda can_id, data, w=worker: self.dispatch_frame(time.time(), "TX", can_id, data)
                                    if w is self.ecu_monitor else None)
        worker.error_occurred.connect(lambda error, w=worker: self._monitor_failed(w, error))
        self.ecu_monitor, self.monitor_bus = worker, bus
        self.monitor_channel, self.monitor_config = dict(channel_config), config
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
        if menu.actions():
            menu.exec_(self.channel_list.viewport().mapToGlobal(position))

    def check_ecus(self, channel_config):
        """Start the ECU check on a channel with the selected configuration, without loading its database."""
        try:
            config = validate_config(self.active_config)
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
        action = self._toolbar_actions["flashing"]
        action.setEnabled(available and self.flash_dialog is None)
        action.setToolTip("Flash ECU firmware using the database's Flashing() function" if available
                          else "The database script does not define Flashing(api, firmware)")

    def open_flashing(self):
        """Choose an S-record / Intel HEX file and pass it to the database script's Flashing()."""
        if self.script_runtime is None or self.script_runtime.flash_function is None:
            return
        settings = app_settings()
        firmware = choose_firmware(self, settings.value("last_firmware_dir", str(APP_DIR), type=str))
        if firmware is None:
            return
        settings.setValue("last_firmware_dir", str(Path(firmware.path).parent))
        if confirm_flash(self, firmware):
            self.start_flashing(firmware)

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
        self._set_flashing_available(self.script_runtime is not None and self.script_runtime.flash_function is not None)
        self.log_verbose(f"Flashing {'succeeded' if ok else 'failed'}: {text}")
        self._set_status(f"Flashing {'complete' if ok else 'failed'}", "green" if ok else "red")
        report_result(self, ok, text)

    def closeEvent(self, event):
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
        """Remove all widgets from application database panel."""
        while self.app_db_layout.count():
            item = self.app_db_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.app_database = None
        self.panel = None

    # --- Application database UI ---

    def build_application_ui(self, app_db):
        self.clear_application_ui()
        self.panel = PanelView(app_db, self.send_can_message, self.log_verbose)
        self.app_db_layout.addWidget(self.panel)
        self.app_database = app_db

    def send_can_message(self, can_id, data, extended=None):
        if self.can_bus is None:
            raise RuntimeError("Start a measurement or connect before sending CAN messages")
        if self.passive_measurement:
            raise RuntimeError("The measurement is passive: switch Passive off to transmit")
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
        self.frame_history.append((float(timestamp), direction, int(can_id), data, bool(extended)))
        if self.recorder is not None:
            try:
                self.recorder.write(timestamp, direction, can_id, data, extended)
            except Exception as exc:                      # a full disk must not take the measurement down
                self.log_verbose(f"Recording stopped: {exc}")
                self.stop_recording()
        self.log_can(direction, can_id, data)
        trace = self.tool_widget("trace")
        if trace is not None:
            trace.add_frame(timestamp, direction, can_id, data, extended)
        logger = self.tool_widget("logger")
        if logger is not None and direction == "RX":
            logger.on_can_message(can_id, data, timestamp)
        diagnostics = self.tool_widget("diagnostics")
        if diagnostics is not None:
            diagnostics.on_can_message(can_id, data, direction)

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
