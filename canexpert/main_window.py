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
from pathlib import Path

import can
from PyQt5.QtCore import QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QKeySequence, QPalette
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from canexpert.config import (
    DEFAULT_CONFIGURATION,
    ConfigurationDialog,
    read_configurations,
    save_configuration,
    validate_config,
)
from canexpert.clock import TIME_DISPLAYS, MeasurementClock, absolute_text
from canexpert.flash_runner import FlashRunner
from canexpert.flash_sequence import FlashProfile
from canexpert.flashing import (FlashDialog, choose_firmware, close_progress, progress_dialog, report_result,
                                update_progress)
from canexpert.help_window import show_manual
from canexpert.panel.view import PanelView
from canexpert.paths import APP_DIR, CONFIG_DIR, DATABASES_DIR
from canexpert.recording import LOG_FILE_FILTER, Recorder, ReplayDialog
from canexpert.symbols import SymbolDatabases
from canexpert import features
from canexpert.sysvars import SystemVariables
from canexpert.about import AboutDialog
from canexpert.status_strip import StatusStrip
from canexpert.ui_common import DockTitleBar, app_icon, app_settings, line_icon, toolbar_icon
from canexpert.workspace import add_pane, create_workspace, fit_on_screen, make_pane, set_content
from canexpert.main_layouts import TOOL_AREAS
from canexpert.main_tools import ToolWindows
from canexpert.main_layouts import Layouts
from canexpert.main_channels import Channels
from canexpert.main_session import MARKER_HISTORY, Session

# A question mark in a circle, for the manual button beside the Help menu.
MANUAL_ICON = ('<circle cx="12" cy="12" r="9"/><path d="M9.2 9.3a2.9 2.9 0 0 1 5.6 1c0 1.9-2.8 2.4-2.8 4"/>'
               '<path d="M12 17.4h.01" stroke-width="2.2"/>')
TIME_DISPLAY = "time_display"       # settings: Absolute or Relative, for the Write window and the console
PANEL_ZOOM = "panel_zoom"           # settings: panel_zoom/<database>/<page> -> the page's zoom
FLASH_PROFILE = "flash_profile"    # settings: the built-in flashing sequence, as JSON
FRAME_HISTORY = 20000              # frames kept so a window opened later can still show them
ALL_TOOL_PANES = ("trace", "logger", "data", "statistics", "transmit", "console",
                  "write", "tests", "sysvars")   # the windows with a switch on the toolbar, when their feature is on
# Keys of the main window, which also work in its floating windows. F5 and the letters are left to the
# panel scripts' @on_key.
SHORTCUTS = {"connect": "F9", "disconnect": "Shift+F9", "trace": "Ctrl+1", "logger": "Ctrl+2", "data": "Ctrl+3",
             "statistics": "Ctrl+4", "transmit": "Ctrl+5", "console": "Ctrl+6", "write": "Ctrl+7",
             "tests": "Ctrl+8", "sysvars": "Ctrl+9", "designer": "Ctrl+E"}
# The manual's section for each tool window, for F1.
HELP_SECTIONS = {"trace": "Trace window", "logger": "CAN Logger", "data": "Data window", "statistics": "Statistics",
                 "transmit": "Transmit window", "console": "UDS Console", "write": "Writing panel scripts",
                 "tests": "Test modules", "sysvars": "Writing panel scripts"}


def tool_panes() -> tuple:
    """The tool windows the toolbar offers: those whose feature is switched on (features.py)."""
    return tuple(name for name in ALL_TOOL_PANES if name != "sysvars" or features.SYSTEM_VARIABLES)
WRITE_HISTORY = 5000               # Write window lines kept for when it is opened


class MainWindow(ToolWindows, Layouts, Channels, Session, QMainWindow):
    """Main application window"""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CAN Expert")
        self.setGeometry(100, 100, 1000, 700)
        self._settings = app_settings()      # the settings every part of the window reads and writes
        
        # Configuration management
        self.configurations = []
        self.active_config = None
        self.worker = None          # the session's CanWorker while connected
        self.can_bus = None
        self.app_database = None
        self.connected_channel_config = None
        self.selected_channel_config = None
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
        self.symbols = SymbolDatabases(parent=self, settings=self._settings)
        # One clock for the Trace, the Logger, the Write window and the UDS Console (clock.py).
        self.clock = MeasurementClock()
        # System variables (sysvars.py) when they are switched on, the script's Write output, and the bus
        # state scripts react to.
        self.tool_names = tool_panes()
        self.sysvars = SystemVariables(self._settings, self) if features.SYSTEM_VARIABLES else None
        if self.sysvars is not None:
            self.sysvars.changed.connect(self._on_sysvar_changed)
        self.sysvar_history = deque(maxlen=FRAME_HISTORY)      # (when, name, value) for a Logger opened later
        self.write_history = deque(maxlen=WRITE_HISTORY)       # (when, level, text) for a Write window opened later
        self.marker_history = deque(maxlen=MARKER_HISTORY)     # (when, comment) for a Trace or Logger opened later
        self._marker_count = 0
        self._bus_state = None
        self._keys_watched = False
        self.time_display = self._settings.value(TIME_DISPLAY, TIME_DISPLAYS[0], type=str)
        if self.time_display not in TIME_DISPLAYS:
            self.time_display = TIME_DISPLAYS[0]
        # What is on the bus, whoever opened it: the database session, the ECU check, or a replayed
        # file. Windows opened later read the history.
        self.frame_history = deque(maxlen=FRAME_HISTORY)
        self.recorder = None
        self.replay = None
        self.tool_panes = {}       # the tool windows opened so far (their panes are in _tool_slots from the start)
        self._diagnostic_answers = (None, False, None)   # response ID, extended, address byte of the session
        self._bus_state = "unknown"

        self.init_ui()
        self.load_configurations()
        self.node_timer = QTimer(self)
        self.node_timer.setInterval(100)
        self.node_timer.timeout.connect(self._update_nodes)
        self.node_timer.start()
        self.check_last_channel()

    @property
    def databases_dir(self):
        """The panel databases' folder, looked up here when it is needed (the tests point it elsewhere)."""
        return DATABASES_DIR

    # --- UI setup ---

    def init_ui(self):
        """Build CANoe-style main window: toolbar, status bar, dockable Configuration, CAN Channels, Database, Log."""
        # Status bar: the bus, the diagnostic session and security, the last error, and the connection
        self.status_strip = StatusStrip()
        self.status_strip.error_clicked.connect(self._show_log_dock)
        self.statusBar().addPermanentWidget(self.status_strip)
        self.status_label = QLabel("No active connections")
        self._set_status("No active connections", "gray")
        self.statusBar().addPermanentWidget(self.status_label)
        self._shortcut_actions = []    # the actions whose keys also work in floating windows

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
            ("transmit", "Transmit", "Send messages once or cyclically, or a database's nodes as those "
             "ECUs would", self.open_transmit),
            ("console", "UDS Console", "Send any UDS service, the services of an ODX file, and read the fault "
             "memory",
             self.open_uds_console),
            ("write", "Write", "What the panel script writes, and its variables as it runs", self.open_write),
            ("tests", "Test", "Run a test module's test cases against the bus, with a verdict per step and an HTML "
             "and JUnit report", self.open_tests),
            ("sysvars", "System Variables", "Values shared by the script, the windows and you", self.open_sysvars),
            ("designer", "Form Designer", "Design panels and edit their Python scripts", self.open_form_designer),
            ("flashing", "Flashing", "Flash ECU firmware with the built-in sequence or the script's Flashing()",
             self.open_flashing),
        ]
        entries = [entry for entry in entries if entry[0] not in ALL_TOOL_PANES or entry[0] in self.tool_names]
        for name, label, hint, callback in entries:
            if name == "trace":
                toolbar.addSeparator()
            action = QAction(toolbar_icon(name), label, self)
            if name in self.tool_names:
                # A tool button works as a switch: it stays pressed while its pane is open, pressing it
                # again closes the pane, and closing the pane by its own button lets the toolbar go.
                action.setCheckable(True)
                action.toggled.connect(lambda shown, n=name, show=callback: self._toggle_tool(n, shown, show))
                hint = f"{hint}\nPress again to close the pane"
            else:
                action.triggered.connect(callback)
            if name in SHORTCUTS:
                action.setShortcut(QKeySequence(SHORTCUTS[name]))
                hint = f"{hint}\nShortcut: {SHORTCUTS[name]}"
                self._shortcut_actions.append(action)
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
                action.setEnabled(False)     # the button follows its action; so do the key and the menu
        self.addToolBar(toolbar)

        # The centre is the workspace: the panel and the analysis windows, which tab together, float
        # and can be dragged onto each other. Configuration, CAN Channels and Log stay Qt docks around it.
        self.workspace = create_workspace(self)
        self._workspace_style = self.workspace.styleSheet()   # palette(...) colours, re-read per theme
        # A floating window is a window of its own: the main window's keys are given to it too.
        self.workspace.floatingWidgetCreated.connect(lambda floating: floating.addActions(self._shortcut_actions))

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
        ch_btn_layout.addWidget(self.refresh_channels_btn)
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
        self.page_panes = []       # the page windows holding a page of the loaded database
        self._page_slots = []      # every page window made so far; they are kept, empty, between databases
        self.database_pane = make_pane("Database", self.app_db_container, "pane_database")
        add_pane(self.workspace, self.database_pane)
        self.database_pane.toggleView(False)   # shown once a database is loaded
        # Every tool window has its pane from the start - empty and closed until it is first opened - so
        # a saved arrangement places it when it is applied. Placing windows made later would mean
        # applying the arrangement again, which moves and reopens everything else too.
        self._tool_slots = {}
        self._settling = False     # a layout is being put right: the toolbar switches do not act
        for name, label, _hint, _callback in entries:
            if name in self.tool_names:
                area = TOOL_AREAS.get(name, "center")
                slot = make_pane(label, QWidget(), f"pane_{name}")
                add_pane(self.workspace, slot, area, beside=self.database_pane if area != "center" else None)
                slot.toggleView(False)
                self._tool_slots[name] = slot

        # Dock: Log (application messages; the frames are the Trace's)
        self.debug_log = QPlainTextEdit()
        self.debug_log.setReadOnly(True)
        self.debug_log.setPlaceholderText("Application debug and status messages…")
        self.debug_log.setMaximumBlockCount(2000)
        self.log_dock = QDockWidget("Log", self)
        self.log_dock.setObjectName("dock_log")
        self.log_dock.setWidget(self.debug_log)
        self.log_dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.log_dock.setTitleBarWidget(DockTitleBar(self.log_dock, self, Qt.BottomDockWidgetArea))
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)

        self.create_menu()
        # The arrangement the window starts with, so Reset layout has somewhere to go back to.
        self._default_layout = (self.saveState(), self.workspace.saveState())
        self.restore_layout()
        self.refresh_channel_list()
        self.log_verbose("Application started.")
        if self.symbols.errors:
            for error in self.symbols.errors:
                self.log_verbose(f"Symbol database: {error}")

    # --- Status and log ---

    def _set_status(self, text: str, color: str = "gray"):
        """Update status label text and optional color (gray, green, red, orange)."""
        self.status_label.setText(text)
        colors = {"gray": "gray", "green": "green", "red": "red", "orange": "orange"}
        self.status_label.setStyleSheet(f"QLabel {{ color: {colors.get(color, 'gray')}; }}")

    def log_verbose(self, msg: str):
        """Append a message to the debug/verbose log."""
        if getattr(self, "debug_log", None) is None:
            return
        line = f"[{absolute_text(time.time())}] {msg}"
        self.debug_log.appendPlainText(line)

    def set_time_display(self, display: str):
        """Absolute (time of day) or Relative (seconds since the measurement started) in the Write window
        and the UDS Console."""
        self.time_display = display
        self._settings.setValue(TIME_DISPLAY, display)
        for action in self._time_display_actions:
            action.setChecked(action.text() == display)
        write = self.tool_widget("write")
        if write is not None:
            write.rebuild()

    # --- Side panels, menus, help and theme ---

    def _show_can_channels_dock(self):
        """Show CAN Channels dock (e.g. after user closed it or after disconnect)."""
        self.channels_dock.setVisible(True)
        self.channels_dock.raise_()

    def _show_config_dock(self):
        """Show Configuration dock."""
        self.config_dock.setVisible(True)
        self.config_dock.raise_()

    def _show_log_dock(self):
        """Show the Log (debug) dock."""
        self.log_dock.setVisible(True)
        self.log_dock.raise_()

    def create_menu(self):
        """Create the menu bar"""
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu('File')
        
        new_config_action = file_menu.addAction('New Configuration')
        new_config_action.setShortcut(QKeySequence.New)
        new_config_action.triggered.connect(self.create_new_config)
        
        import_config_action = file_menu.addAction('Import Configuration')
        import_config_action.triggered.connect(self.import_config)
        
        export_config_action = file_menu.addAction('Export Configuration')
        export_config_action.triggered.connect(self.export_config)

        file_menu.addSeparator()
        file_menu.addAction('Show Configuration').triggered.connect(self._show_config_dock)
        file_menu.addAction('Show CAN Channels').triggered.connect(self._show_can_channels_dock)
        file_menu.addAction('Show Log').triggered.connect(self._show_log_dock)

        file_menu.addSeparator()
        exit_action = file_menu.addAction('Exit')
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)

        # Connection menu: the session, and what is written to or read from a file
        measurement_menu = menubar.addMenu('Connection')
        for name in ("connect", "disconnect"):
            measurement_menu.addAction(self._toolbar_actions[name])
        measurement_menu.addSeparator()
        self._record_action = measurement_menu.addAction('Record to file...')
        self._record_action.setShortcut(QKeySequence("Ctrl+R"))
        self._record_action.triggered.connect(self.start_recording)
        self._stop_record_action = measurement_menu.addAction('Stop recording')
        self._stop_record_action.setShortcut(QKeySequence("Ctrl+Shift+R"))
        self._stop_record_action.setEnabled(False)
        self._stop_record_action.triggered.connect(self.stop_recording)
        replay_action = measurement_menu.addAction('Replay a recorded file...')
        replay_action.setShortcut(QKeySequence("Ctrl+O"))
        replay_action.triggered.connect(lambda: self.replay_log())
        self._shortcut_actions += [self._record_action, self._stop_record_action, replay_action]
        measurement_menu.addSeparator()
        marker_action = measurement_menu.addAction('Insert marker...')
        marker_action.setShortcut(QKeySequence("Ctrl+M"))
        marker_action.setToolTip("A marker with a comment at this moment: in the Trace, on the Logger's graphs "
                                 "and in the recording")
        marker_action.triggered.connect(lambda: self.insert_marker())
        quick_marker_action = measurement_menu.addAction('Quick marker')
        quick_marker_action.setShortcut(QKeySequence("Ctrl+Shift+M"))
        quick_marker_action.setToolTip("A numbered marker at once, without asking for a comment")
        quick_marker_action.triggered.connect(lambda: self.quick_marker())
        self._shortcut_actions += [marker_action, quick_marker_action]
        measurement_menu.addSeparator()
        measurement_menu.addAction('Scan for ECUs...').triggered.connect(lambda: self.open_ecu_scan())

        # Tools menu
        tools_menu = menubar.addMenu('Tools')
        tools_menu.addAction(self._toolbar_actions["designer"])
        for name in self.tool_names:
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
        self.context_help_action = help_menu.addAction("Help on this window", self.context_help)
        self.context_help_action.setShortcut(QKeySequence.HelpContents)
        self.context_help_action.setToolTip("The manual at the section of the window you are working in")
        self.addAction(self.context_help_action)         # a menu of a tool button gives its keys nothing
        self._shortcut_actions.append(self.context_help_action)
        help_menu.addAction("Keyboard shortcuts", lambda: self.open_manual("Keyboard shortcuts"))
        help_menu.addSeparator()
        help_menu.addAction("About", self.show_about)
        help_btn.setMenu(help_menu)
        self.manual_btn = QToolButton()
        self.manual_btn.setAutoRaise(True)
        self.manual_btn.setIconSize(QSize(18, 18))
        self.manual_btn.setAccessibleName("User manual")
        self.manual_btn.setToolTip("User manual: how to use the main window, the tool windows, the Form Designer "
                                   "and the UDS Console")
        self.manual_btn.clicked.connect(self.open_manual)
        help_corner_layout.addWidget(self.manual_btn)
        help_corner_layout.addWidget(help_btn)
        menubar.setCornerWidget(help_corner, Qt.TopRightCorner)
        # Last, so every button that follows the theme already exists.
        self.apply_theme(app_settings().value("theme", "light", type=str), restore=True)

    def _refresh_manual_icon(self):
        self.manual_btn.setIcon(line_icon(MANUAL_ICON, self.palette().color(QPalette.WindowText)))

    def open_manual(self, section=None):
        """Show the user manual (docs/USER_MANUAL.md), at a section if one is named."""
        window = show_manual(self)
        if section:
            window.go_to_section(section)
        return window

    def help_section(self, widget=None) -> str:
        """The manual's section for a widget (default: the one with the focus): its tool window's, the
        panel's, a side panel's, else the start of the manual."""
        widget = widget if widget is not None else QApplication.focusWidget()
        tools = {id(pane.widget()): name for name, pane in self.tool_panes.items()}
        panels = {id(self.app_db_container), *(id(pane.widget()) for pane in self.page_panes)}
        docks = {id(self.config_dock): "Configurations", id(self.channels_dock): "Connecting"}
        while widget is not None:
            if id(widget) in tools:
                return HELP_SECTIONS.get(tools[id(widget)], "Starting up")
            if id(widget) in panels:
                return "Using a panel"
            if id(widget) in docks:
                return docks[id(widget)]
            widget = widget.parentWidget()
        return "Starting up"

    def context_help(self):
        """F1: the manual at the section of the window being worked in."""
        return self.open_manual(self.help_section())

    def show_about(self):
        """The About box: the version of CAN Expert, its libraries and the adapter drivers."""
        dialog = AboutDialog(self)
        dialog.exec_()
        return dialog

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

    # --- Configurations ---

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
        event.accept()

    def clear_application_ui(self):
        """Remove the loaded database's pages: the Database window's content and the other page windows."""
        while self.app_db_layout.count():
            item = self.app_db_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for pane in self.page_panes:
            set_content(pane, QWidget())      # the window stays, closed, where it was: the next database's
            pane.toggleView(False)            # pages come back to the same places
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
            pane = self._page_slot(index)
            pane.setWindowTitle(name)
            set_content(pane, window)
            pane.toggleView(True)
            fit_on_screen(pane)
            self.page_panes.append(pane)
        if self.page_panes:
            self.database_pane.setAsCurrentTab()
        self.app_database = app_db

    def _page_zooms(self, family) -> dict:
        self._settings.beginGroup(f"{PANEL_ZOOM}/{family}")
        try:
            return {name: self._settings.value(name, "", type=str) for name in self._settings.childKeys()}
        finally:
            self._settings.endGroup()

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
                
def startup_problems() -> list[str]:
    """What a built CAN Expert would miss at run time (--smoke-test): the manual, the icon, the example DBC
    read by cantools, a python-can bus, odxtools. An empty list when all is there."""
    from canexpert.paths import DBC_DIR, DOCS_DIR
    problems = []
    if not (DOCS_DIR / "USER_MANUAL.md").exists():
        problems.append(f"no manual in {DOCS_DIR}")
    if app_icon().isNull():
        problems.append("no application icon")
    try:
        import cantools
        if (DBC_DIR / "dummy_ecu.dbc").exists():
            cantools.database.load_file(str(DBC_DIR / "dummy_ecu.dbc"))
    except Exception as exc:
        problems.append(f"cantools: {exc}")
    try:
        can.Bus(interface="virtual", channel="smoke-test").shutdown()
    except Exception as exc:
        problems.append(f"python-can: {exc}")
    try:
        __import__("odxtools")                  # imported only to see that it is there
    except Exception as exc:
        problems.append(f"odxtools: {exc}")
    return problems


def main():
    """Start CAN Expert; with --smoke-test only build the main window and check what it needs (exit 1: missing)."""
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon())               # every window of the application, dialogs included
    window = MainWindow()
    if "--smoke-test" in sys.argv:
        problems = startup_problems()
        print("\n".join(problems) or "startup ok")
        return 1 if problems else 0
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
