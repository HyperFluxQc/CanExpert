#!/usr/bin/env python3
"""
CAN Expert - Main Application

Select a configuration and CAN receiver, load the newest matching panel database, then
connect: send periodic TesterPresent, monitor ECU nodes and run the panel's Python script.
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

if sys.version_info < (3, 10):
    print("CAN Expert requires Python 3.10 or newer.")
    print(f"Current interpreter: {sys.version.split()[0]} ({sys.executable})")
    sys.exit(1)

try:
    import can
except ImportError:
    print("Missing dependency: python-can")
    print("Install with: pip install -r requirements.txt")
    sys.exit(1)

try:
    from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread, QSize
except ImportError:
    print("Missing dependency: PyQt5")
    print("Install with: pip install -r requirements.txt")
    sys.exit(1)
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QStyle,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QComboBox,
    QTreeWidget,
    QTreeWidgetItem,
    QDoubleSpinBox,
    QSpinBox,
    QCheckBox,
)

from panel import PanelView, load_application_database
from form_designer import FormDesigner
from can_logger import CANLoggerWindow
from diagnostic_window import DiagnosticWindow
from panel_runtime import (DEFAULT_NODE_TIMEOUT, DEFAULT_TESTER_PRESENT_INTERVAL, ReceiveMailbox,
                           ScriptRuntime, validate_config)
from ui_common import app_settings, toolbar_icon
from uds_services import FIRMWARE_FILE_FILTER, load_firmware

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

DEFAULT_BITRATE = 500000
APP_DIR = Path(__file__).resolve().parent
CONFIG_DIR = APP_DIR / "Configurations"
DATABASES_DIR = APP_DIR / "Databases"

SUPPORTED_INTERFACES = [
    ("kvaser", "Kvaser"),
    ("vector", "Vector"),
    ("ixxat", "IXXAT"),
]


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _channel_key(cfg: dict) -> tuple:
    """Unique key for a channel (interface, channel, unique_hardware_id)."""
    return (
        cfg.get("interface", "kvaser"),
        cfg.get("channel", 0),
        cfg.get("unique_hardware_id", ""),
        cfg.get("serial", ""),
    )


def _channel_to_int(channel_str: str) -> int:
    """Parse 'Channel 0' -> 0."""
    s = str(channel_str).strip()
    for part in s.split():
        try:
            return int(part)
        except ValueError:
            continue
    return 0


def create_can_bus(interface: str, channel, bitrate: int, **kwargs) -> can.Bus:
    """Create a python-can Bus for the given interface. Channel can be int or interface-specific."""
    params = {"interface": interface, "channel": channel, "bitrate": bitrate}
    # Pass through interface-specific kwargs (e.g. unique_hardware_id for IXXAT, serial for Vector)
    for key in ("unique_hardware_id", "serial", "app_name"):
        if key in kwargs:
            params[key] = kwargs[key]
    return can.interface.Bus(**params)


# -----------------------------------------------------------------------------
# Background workers
# -----------------------------------------------------------------------------

class ChannelActivityScanner(QThread):
    """Scans CAN channels for activity by briefly opening each and listening."""
    channel_activity = pyqtSignal(list)
    scan_finished = pyqtSignal()

    def __init__(self, channels: list, bitrate: int = 500000, listen_time: float = 0.3):
        super().__init__()
        self.channels = channels
        self.bitrate = bitrate
        self.listen_time = listen_time

    def run(self):
        result = []
        for ch_info in self.channels:
            if self.isInterruptionRequested():
                break
            ch = ch_info.get("channel", 0)
            iface = ch_info.get("interface", "kvaser")
            kwargs = {k: ch_info[k] for k in ("unique_hardware_id", "serial", "app_name") if k in ch_info}
            try:
                bus = create_can_bus(iface, ch, self.bitrate, **kwargs)
                deadline = time.time() + self.listen_time
                got_message = False
                while time.time() < deadline:
                    msg = bus.recv(timeout=0.05)
                    if msg:
                        got_message = True
                        break
                bus.shutdown()
                result.append(got_message)
            except Exception:
                result.append(False)
        self.channel_activity.emit(result)
        self.scan_finished.emit()


class CanWorker(QThread):
    """Worker thread for CAN communication"""
    message_received = pyqtSignal(dict)
    message_sent = pyqtSignal(int, bytes)
    error_occurred = pyqtSignal(str)
    connection_status = pyqtSignal(bool)
    
    def __init__(self):
        super().__init__()
        self.interface = None
        self.channel = None
        self.running = False
        self.bus = None
        self.config = None
        self.mailboxes = []

    def add_mailbox(self, mailbox):
        """Deliver received frames to mailbox as well (script runtime, diagnostic requests)."""
        self.mailboxes = [*self.mailboxes, mailbox]

    def remove_mailbox(self, mailbox):
        self.mailboxes = [m for m in self.mailboxes if m is not mailbox]
        
    def setup_connection(self, channel, bitrate=500000, config=None, bus=None):
        """Setup CAN connection. Channel can be int or 'Channel N' string. Pass bus to reuse existing connection."""
        ch = channel if isinstance(channel, int) else _channel_to_int(channel)
        self.channel = ch
        self.bitrate = bitrate
        self.config = config
        self.running = True

        if bus is not None:
            self.bus = bus
            self.connection_status.emit(True)
            return

        try:
            iface = (config or {}).get("interface", "kvaser")
            kwargs = {k: (config or {})[k] for k in ("unique_hardware_id", "serial", "app_name") if k in (config or {})}
            self.bus = create_can_bus(iface, ch, bitrate, **kwargs)
            self.connection_status.emit(True)
        except Exception as e:
            self.connection_status.emit(False)
            self.error_occurred.emit(f"Failed to connect to CAN channel {ch}: {str(e)}")
            self.running = False
            
    def run(self):
        if not self.bus:
            return
        next_heartbeat = 0.0
        try:
            cfg = validate_config(self.config or {})
        except ValueError as exc:
            self.error_occurred.emit(str(exc))
            return
        while self.running:
            try:
                now = time.monotonic()
                if now >= next_heartbeat and any(m.in_transaction for m in self.mailboxes):
                    # A UDS exchange is in progress; interleaving a request would abort it.
                    next_heartbeat = now + 0.05
                elif now >= next_heartbeat:
                    payload = bytes([2, 0x3E, 0])
                    if cfg.get("extended_id"):
                        payload = bytes([cfg["extended_id_byte"]]) + payload
                    self.bus.send(can.Message(arbitration_id=cfg["request_id"], data=payload,
                                              is_extended_id=not cfg["identifier_11_bit"]))
                    self.message_sent.emit(cfg["request_id"], payload)
                    next_heartbeat = now + cfg["tester_present_interval_seconds"]
                message = self.bus.recv(timeout=min(0.05, max(0.001, next_heartbeat-time.monotonic())))
                if message and not message.is_error_frame and not message.is_remote_frame:
                    for mailbox in self.mailboxes:
                        mailbox.push(message)
                    self.message_received.emit({
                        "timestamp": message.timestamp, "arbitration_id": message.arbitration_id,
                        "is_extended_frame": message.is_extended_id, "data": list(message.data),
                        "dlc": message.dlc, "channel": self.channel})
            except Exception as exc:
                self.error_occurred.emit(f"CAN session failed: {exc}")
                self.running = False

    def stop(self):
        self.running = False
        for mailbox in self.mailboxes:
            mailbox.close()
        self.wait()


class ConfigurationDialog(QMainWindow):
    """Dialog for creating/editing CAN connection configurations (no channel, no filtering)."""

    def __init__(self, parent=None, config=None):
        super().__init__(parent)
        self.setWindowTitle("CAN Connection Configuration")
        self.setGeometry(300, 200, 480, 380)
        self.config = config or {}
        self.init_ui()
        self.load_config()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout()
        central_widget.setLayout(layout)

        config_group = QGroupBox("Connection Configuration")
        config_layout = QFormLayout()

        self.name_edit = QComboBox()
        self.name_edit.setEditable(True)
        config_layout.addRow("Name:", self.name_edit)

        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(["125000", "250000", "500000", "1000000"])
        self.bitrate_combo.setCurrentText("500000")
        config_layout.addRow("Bitrate (bps):", self.bitrate_combo)

        self.id_size_combo = QComboBox()
        self.id_size_combo.addItem("11 bits (Standard)", 11)
        self.id_size_combo.addItem("29 bits (Extended)", 29)
        config_layout.addRow("Identifier size:", self.id_size_combo)

        self.server_id_edit = QLineEdit()
        self.server_id_edit.setPlaceholderText("e.g. 7DF (11-bit) or 1DDAEDE9 (29-bit) – request sent to this ID")
        self.server_id_edit.setText("7DF")
        config_layout.addRow("SERVER ID (hex):", self.server_id_edit)

        self.ecu_id_edit = QLineEdit()
        self.ecu_id_edit.setPlaceholderText("e.g. 7E8 (11-bit) – ECU response ID")
        self.ecu_id_edit.setText("7E8")
        config_layout.addRow("ECU ID (hex):", self.ecu_id_edit)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(500, 60000)
        self.timeout_spin.setSingleStep(500)
        self.timeout_spin.setSuffix(" ms")
        self.timeout_spin.setValue(5000)
        self.timeout_spin.setToolTip("How long script and Diagnostic Window UDS requests wait for a reply")
        config_layout.addRow("UDS response timeout:", self.timeout_spin)
        self.heartbeat_spin = QDoubleSpinBox()
        self.heartbeat_spin.setRange(0.05, 3600)
        self.heartbeat_spin.setDecimals(2)
        self.heartbeat_spin.setSuffix(" s")
        self.heartbeat_spin.setValue(DEFAULT_TESTER_PRESENT_INTERVAL)
        config_layout.addRow("TesterPresent interval:", self.heartbeat_spin)
        self.node_timeout_spin = QDoubleSpinBox()
        self.node_timeout_spin.setRange(0.1, 86400)
        self.node_timeout_spin.setValue(DEFAULT_NODE_TIMEOUT)
        self.node_timeout_spin.setSuffix(" s")
        config_layout.addRow("Node loss timeout:", self.node_timeout_spin)
        self.database_family_edit = QLineEdit()
        self.database_family_edit.setPlaceholderText("Blank = newest database; e.g. engine")
        config_layout.addRow("Database family:", self.database_family_edit)
        self.response_ids_edit = QLineEdit()
        self.response_ids_edit.setPlaceholderText("Optional hex IDs, comma-separated; blank = ECU ID / OBD range")
        config_layout.addRow("Monitored ECU IDs:", self.response_ids_edit)

        self.extended_id_cb = QCheckBox("Extended identifier (first data byte extends ID in UDS)")
        self.extended_id_cb.setChecked(False)
        self.extended_id_cb.toggled.connect(self._on_extended_id_toggled)
        config_layout.addRow(self.extended_id_cb)

        self.extended_id_byte_edit = QLineEdit()
        self.extended_id_byte_edit.setPlaceholderText("e.g. 01 or 0x01")
        self.extended_id_byte_edit.setText("00")
        self.extended_id_byte_edit.setEnabled(False)
        config_layout.addRow("Extended ID byte (hex):", self.extended_id_byte_edit)

        config_group.setLayout(config_layout)
        layout.addWidget(config_group)

        button_layout = QHBoxLayout()
        self.save_btn = QPushButton("Save Configuration")
        self.save_btn.clicked.connect(self.save_config)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.close)
        button_layout.addWidget(self.save_btn)
        button_layout.addWidget(self.cancel_btn)
        button_layout.addStretch()
        layout.addLayout(button_layout)

    def _parse_id(self, text: str) -> int | None:
        """Parse hex ID (11-bit or 29-bit). Returns int or None if invalid."""
        s = str(text).strip().upper().replace("0X", "")
        if not s:
            return None
        try:
            value = int(s, 16)
            return value if 0 <= value <= 0x1FFFFFFF else None
        except ValueError:
            return None

    def _parse_extended_id_byte(self, text: str) -> int | None:
        """Parse hex byte (0-255). Returns int or None if invalid."""
        s = str(text).strip().upper().replace("0X", "")
        if not s:
            return None
        try:
            v = int(s, 16)
            return v if 0 <= v <= 255 else None
        except ValueError:
            return None

    def _on_extended_id_toggled(self, checked: bool):
        self.extended_id_byte_edit.setEnabled(checked)

    def load_config(self):
        self.heartbeat_spin.setValue(float(self.config.get("tester_present_interval_seconds", DEFAULT_TESTER_PRESENT_INTERVAL)))
        self.node_timeout_spin.setValue(float(self.config.get("node_timeout_seconds", DEFAULT_NODE_TIMEOUT)))
        self.database_family_edit.setText(self.config.get("database_family", ""))
        self.response_ids_edit.setText(", ".join(f"{v:X}" for v in self.config.get("response_ids", [])))
        if self.config.get("name"):
            self.name_edit.setCurrentText(self.config["name"])
        if self.config.get("bitrate"):
            idx = self.bitrate_combo.findText(str(self.config["bitrate"]))
            if idx >= 0:
                self.bitrate_combo.setCurrentIndex(idx)
        if self.config.get("identifier_11_bit") is not None:
            self.id_size_combo.setCurrentIndex(0 if self.config["identifier_11_bit"] else 1)
        elif self.config.get("identifier_bits") == 29:
            self.id_size_combo.setCurrentIndex(1)
        if self.config.get("request_id") is not None:
            rid = self.config["request_id"]
            if isinstance(rid, int):
                self.server_id_edit.setText(f"{rid:X}")
            else:
                self.server_id_edit.setText(str(rid).strip())
        if self.config.get("response_id") is not None:
            rid = self.config["response_id"]
            if isinstance(rid, int):
                self.ecu_id_edit.setText(f"{rid:X}")
            else:
                self.ecu_id_edit.setText(str(rid).strip())
        if self.config.get("timeout_ms") is not None:
            self.timeout_spin.setValue(int(self.config["timeout_ms"]))
        if self.config.get("extended_id") is not None:
            self.extended_id_cb.setChecked(bool(self.config["extended_id"]))
        self._on_extended_id_toggled(self.extended_id_cb.isChecked())
        if self.config.get("extended_id_byte") is not None:
            b = self.config["extended_id_byte"]
            if isinstance(b, int) and 0 <= b <= 255:
                self.extended_id_byte_edit.setText(f"{b:02X}")

    def save_config(self):
        request_id = self._parse_id(self.server_id_edit.text())
        response_id = self._parse_id(self.ecu_id_edit.text())
        identifier_11_bit = self.id_size_combo.currentData() == 11
        if request_id is not None and identifier_11_bit and request_id > 0x7FF:
            QMessageBox.warning(
                self,
                "Invalid ID",
                "Identifier size is set to 11 bits, but SERVER ID is greater than 0x7FF (2047).\n"
                "Either choose 29 bits (Extended) or use an 11-bit ID (e.g. 0x7DF)."
            )
            return
        if request_id is None and self.server_id_edit.text().strip():
            QMessageBox.warning(self, "Invalid SERVER ID", "SERVER ID must be a valid hex value (e.g. 7DF or 1DDAEDE9).")
            return
        if response_id is None and self.ecu_id_edit.text().strip():
            QMessageBox.warning(self, "Invalid ECU ID", "ECU ID must be a valid hex value (e.g. 7E8).")
            return
        if response_id is not None and identifier_11_bit and response_id > 0x7FF:
            QMessageBox.warning(
                self,
                "Invalid ID",
                "Identifier size is set to 11 bits, but ECU ID is greater than 0x7FF (2047).\n"
                "Either choose 29 bits (Extended) or use an 11-bit ECU ID (e.g. 0x7E8)."
            )
            return
        extended_id = self.extended_id_cb.isChecked()
        extended_id_byte = None
        if extended_id:
            extended_id_byte = self._parse_extended_id_byte(self.extended_id_byte_edit.text())
            if extended_id_byte is None:
                QMessageBox.warning(
                    self,
                    "Invalid Extended ID byte",
                    "Extended ID byte must be a valid hex value from 00 to FF (0-255)."
                )
                return
        try:
            response_ids = [int(v.strip(), 16) for v in self.response_ids_edit.text().split(",") if v.strip()]
        except ValueError:
            QMessageBox.warning(self, "Invalid configuration", "Monitored ECU IDs must be hexadecimal numbers.")
            return
        config = dict(self.config)
        config.pop("did", None)  # only used by the removed database-ID discovery
        config.update({
            "name": self.name_edit.currentText().strip() or "Unnamed",
            "bitrate": int(self.bitrate_combo.currentText()),
            "identifier_11_bit": identifier_11_bit,
            "timeout_ms": self.timeout_spin.value(),
            "extended_id": extended_id,
            "tester_present_interval_seconds": self.heartbeat_spin.value(),
            "node_timeout_seconds": self.node_timeout_spin.value(),
            "database_family": self.database_family_edit.text().strip(),
            "response_ids": response_ids,
        })
        if request_id is not None:
            config["request_id"] = request_id
        if response_id is not None:
            config["response_id"] = response_id
        if extended_id_byte is not None:
            config["extended_id_byte"] = extended_id_byte
        try:
            config = validate_config(config)
            if any(c in config["name"] for c in '/\\:*?"<>|'):
                raise ValueError("Configuration name cannot contain filename separators")
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid configuration", str(exc))
            return
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config_file = CONFIG_DIR / f"config_{config['name']}.json"
        try:
            with open(config_file, "w") as f:
                json.dump(config, f, indent=2)
            if self.parent():
                self.parent().load_configurations()
            self.close()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save configuration: {e}")


# Thin size for minimized docks (only icon strip visible)
DOCK_MINIMIZED_SIZE = 28


class DockTitleBar(QWidget):
    """Title bar for a dock with title, minimize (collapse to thin strip), and close."""
    def __init__(self, dock: QDockWidget, main_window: QMainWindow, area: Qt.DockWidgetArea, parent=None):
        super().__init__(parent)
        self.dock = dock
        self.main_window = main_window
        self.area = area
        self.is_minimized = False
        self.saved_size = 200  # fallback when restoring

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 2, 2)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignTop)  # when dock is a thin column, keep icon at top
        self.title_label = QLabel(dock.windowTitle())
        self.title_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.title_label)

        self.min_btn = QToolButton()
        self.min_btn.setToolTip("Minimize panel to a thin strip")
        self.min_btn.setIcon(self.main_window.style().standardIcon(QStyle.SP_TitleBarMinButton))
        self.min_btn.setIconSize(QSize(16, 16))
        self.min_btn.clicked.connect(self._toggle_minimized)
        layout.addWidget(self.min_btn)

        self.close_btn = QToolButton()
        self.close_btn.setToolTip("Close panel")
        self.close_btn.setIcon(self.main_window.style().standardIcon(QStyle.SP_TitleBarCloseButton))
        self.close_btn.setIconSize(QSize(16, 16))
        self.close_btn.clicked.connect(self.dock.close)
        layout.addWidget(self.close_btn)

        self.setLayout(layout)

    def _toggle_minimized(self):
        if self.is_minimized:
            self._restore()
        else:
            self._minimize()

    def _minimize(self):
        self.is_minimized = True
        # Save current size for restore
        if self.area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea):
            self.saved_size = max(80, self.dock.width())
        else:
            self.saved_size = max(80, self.dock.height())
        # Constrain to thin strip
        if self.area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea):
            self.dock.setMinimumWidth(DOCK_MINIMIZED_SIZE)
            self.dock.setMaximumWidth(DOCK_MINIMIZED_SIZE)
        else:
            self.dock.setMinimumHeight(DOCK_MINIMIZED_SIZE)
            self.dock.setMaximumHeight(DOCK_MINIMIZED_SIZE)
        self.dock.widget().hide()
        self._update_title_bar_appearance()

    def _restore(self):
        self.is_minimized = False
        if self.area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea):
            self.dock.setMinimumWidth(80)
            self.dock.setMaximumWidth(16777215)
        else:
            self.dock.setMinimumHeight(80)
            self.dock.setMaximumHeight(16777215)
        self.dock.widget().show()
        try:
            orientation = Qt.Horizontal if self.area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea) else Qt.Vertical
            self.main_window.resizeDocks([self.dock], [self.saved_size], orientation)
        except Exception:
            pass
        self._update_title_bar_appearance()

    def _update_title_bar_appearance(self):
        if self.is_minimized:
            self.min_btn.setIcon(self.main_window.style().standardIcon(QStyle.SP_TitleBarNormalButton))
            self.min_btn.setToolTip("Restore panel")
            self.title_label.hide()
            self.close_btn.hide()
        else:
            self.min_btn.setIcon(self.main_window.style().standardIcon(QStyle.SP_TitleBarMinButton))
            self.min_btn.setToolTip("Minimize panel to a thin strip")
            self.title_label.show()
            self.close_btn.show()


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
        self.uds_worker = None
        self.channel_activity = []
        self.connected_channel_config = None
        self.selected_channel_config = None
        self.channel_discovered_db = {}
        self.message_count = 0
        self.activity_scanner = None
        self.script_runtime = None
        self.flash_dialog = None
        self.panel = None
        self.session_config = None
        self.node_states = {}
        self.channel_items = {}
        self.node_items = {}
        self.session_generation = 0

        self.init_ui()
        self.load_configurations()
        self.node_timer = QTimer(self)
        self.node_timer.setInterval(100)
        self.node_timer.timeout.connect(self._update_nodes)
        self.node_timer.start()

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
            ("connect", "Connect", "Connect to the selected CAN receiver", self.on_connect_clicked),
            ("disconnect", "Disconnect", "Stop communication and disconnect", self.on_disconnect_clicked),
            ("designer", "Form Designer", "Design panels and edit their Python scripts", self.open_form_designer),
            ("logger", "CAN Logger", "Plot and export CAN signals", self.open_can_logger),
            ("diagnostics", "Diagnostics", "Open ECU diagnostic services", self.open_diagnostic_window),
            ("flashing", "Flashing", "Flash ECU firmware using the database's Flashing() function", self.open_flashing),
        ]
        for name, label, hint, callback in entries:
            if name == "designer":
                toolbar.addSeparator()
            action = QAction(toolbar_icon(name), label, self, triggered=callback)
            action.setToolTip(hint)
            action.setStatusTip(hint)
            button = QToolButton()
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(28, 28))
            button.setMinimumSize(96, 66)
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
        self.log_dock.setWidget(log_tabs)
        self.log_dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.log_dock.setTitleBarWidget(DockTitleBar(self.log_dock, self, Qt.BottomDockWidgetArea))
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)

        self.create_menu()
        self.refresh_channel_list()
        self.log_verbose("Application started.")

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
        if self.connected_channel_config and not any(_channel_key(c) == _channel_key(self.connected_channel_config) for c in self.can_channels):
            self.can_channels.append(self.connected_channel_config)
        for cfg in self.can_channels:
            label = f"[{cfg['interface']}] Ch {cfg.get('channel', 0)}: {cfg.get('device_name', cfg.get('description', 'CAN receiver'))}"
            serial = cfg.get("serial") or cfg.get("unique_hardware_id")
            if serial:
                label += f" ({serial})"
            if self.connected_channel_config and _channel_key(cfg) == _channel_key(self.connected_channel_config):
                label += " [Connected]"
            item = QTreeWidgetItem([label])
            item.setData(0, Qt.UserRole, cfg)
            self.channel_list.addTopLevelItem(item)
            self.channel_items[_channel_key(cfg)] = item
        if not self.can_channels:
            self.channel_list.addTopLevelItem(QTreeWidgetItem(["No CAN receivers found"]))
        self._update_nodes()

    def _update_nodes(self):
        now = time.monotonic()
        for (channel_key, can_id), state in self.node_states.items():
            parent = self.channel_items.get(channel_key)
            if parent is None:
                continue
            key = (channel_key, can_id)
            item = self.node_items.get(key)
            if item is None:
                item = QTreeWidgetItem(parent)
                self.node_items[key] = item
                parent.setExpanded(True)
            active = self.can_bus is not None and self.connected_channel_config is not None and channel_key == _channel_key(self.connected_channel_config)
            lost = state.get("disconnected", False) or not active or now - state["last_seen"] > state["timeout"]
            item.setText(0, f"{'✗' if lost else '●'} ECU 0x{can_id:X} — {'Lost connection' if lost else 'Responding'}")
            item.setForeground(0, QColor("red" if lost else "green"))
            item.setData(0, Qt.UserRole, parent.data(0, Qt.UserRole))

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
        """Update channel list with activity scan results."""
        self.channel_activity = result
        self.refresh_channel_list()

    def on_activity_scan_finished(self):
        """Re-enable scan button after scan completes."""
        self.scan_activity_btn.setEnabled(True)
        self.status_label.setText("Activity scan complete")
        self.activity_scanner = None

    def on_channel_selected(self, item, column=0):
        cfg = item.data(0, Qt.UserRole)
        if cfg:
            self.selected_channel_config = cfg
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

        # Tools menu
        tools_menu = menubar.addMenu('Tools')
        tools_menu.addAction('Form Designer').triggered.connect(self.open_form_designer)
        tools_menu.addAction('CAN Logger...').triggered.connect(self.open_can_logger)
        tools_menu.addAction('Diagnostic Window...').triggered.connect(self.open_diagnostic_window)
        
        # View menu
        view_menu = menubar.addMenu('View')
        
        refresh_config_action = view_menu.addAction('Refresh Configurations')
        refresh_config_action.triggered.connect(self.load_configurations)
        refresh_channels_action = view_menu.addAction('Refresh Channels')
        refresh_channels_action.triggered.connect(self.refresh_channel_list)

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
        # Restore saved preference
        settings = app_settings()
        saved_theme = settings.value("theme", "light", type=str)
        self.apply_theme(saved_theme, restore=True)

        # Help menu on the far right (corner widget; avoid nesting a second QMenuBar)
        help_corner = QWidget()
        help_corner_layout = QHBoxLayout(help_corner)
        help_corner_layout.setContentsMargins(0, 0, 6, 0)
        help_btn = QToolButton()
        help_btn.setText("Help")
        help_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        help_btn.setPopupMode(QToolButton.InstantPopup)
        help_menu = QMenu(help_btn)
        help_menu.addAction("About", self.show_about)
        help_btn.setMenu(help_menu)
        help_corner_layout.addWidget(help_btn)
        menubar.setCornerWidget(help_corner, Qt.TopRightCorner)

    def show_about(self):
        """Show About dialog with app info."""
        dlg = QDialog(self)
        dlg.setWindowTitle("About CAN Expert")
        layout = QVBoxLayout(dlg)
        layout.setSpacing(12)
        layout.addWidget(QLabel("CAN Expert"))
        layout.addWidget(QLabel("Connect to CAN, run UDS discovery, load application databases."))
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(dlg.accept)
        layout.addWidget(ok_btn, 0, Qt.AlignCenter)
        dlg.exec_()

    def apply_theme(self, theme: str, restore: bool = False):
        """Apply light or dark theme to the application."""
        app = QApplication.instance()
        palette = QPalette()
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
            self.dark_mode_action.setChecked(True)
        else:
            palette = QPalette()
            self.light_mode_action.setChecked(True)
        app.setPalette(palette)
        for name, action in self._toolbar_actions.items():
            action.setIcon(toolbar_icon(name, dark=theme == "dark"))
        if not restore:
            settings = app_settings()
            settings.setValue("theme", theme)

    def load_configurations(self):
        """Load available configurations from files"""
        self.config_list.clear()
        self.configurations = []
        
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        for filename in sorted(CONFIG_DIR.iterdir()):
            if filename.name.startswith('config_') and filename.name.endswith('.json'):
                try:
                    with open(filename, 'r') as f:
                        config = validate_config(json.load(f))
                        self.configurations.append(config)
                        
                        # Add to list
                        item = QListWidgetItem(config['name'])
                        self.config_list.addItem(item)
                except Exception as e:
                    self.log_verbose(f"Error loading config {filename.name}: {e}")
                    
        if not self.configurations:
            default_config = {
                "name": "Default Configuration",
                "bitrate": DEFAULT_BITRATE,
                "identifier_11_bit": True,
                "request_id": 0x7DF,
                "response_id": 0x7E8,
                "timeout_ms": 5000,
                "extended_id": False,
            }
            self.configurations.append(default_config)
            item = QListWidgetItem(default_config["name"])
            self.config_list.addItem(item)
        last_name = app_settings().value("last_configuration", "", type=str)
        selected = next((i for i, cfg in enumerate(self.configurations) if cfg["name"] == last_name), 0)
        if self.config_list.count():
            self.config_list.setCurrentRow(selected)
            self.on_config_selected(self.config_list.item(selected))
        self.log_verbose(f"Loaded {len(self.configurations)} configuration(s).")
            
    def open_form_designer(self):
        """Open the Form Designer dialog."""
        designer = FormDesigner(self)
        designer.saved.connect(lambda p: self.load_configurations())
        designer.exec_()

    def open_can_logger(self):
        """Open the CAN Logger window."""
        if not getattr(self, "_can_logger_window", None):
            self._can_logger_window = CANLoggerWindow(self)
        self._can_logger_window.show()
        self._can_logger_window.raise_()
        self._can_logger_window.activateWindow()

    def open_diagnostic_window(self):
        """Open the Diagnostic Window."""
        if not getattr(self, "_diagnostic_window", None):
            self._diagnostic_window = DiagnosticWindow(self)
        self._diagnostic_window.show()
        self._diagnostic_window.raise_()
        self._diagnostic_window.activateWindow()

    def edit_configuration(self, item):
        if self.can_bus is None:
            dialog = ConfigurationDialog(self, dict(self.active_config or {}))
            dialog.show()

    def create_new_config(self):
        """Create a new configuration"""
        dialog = ConfigurationDialog(self)
        dialog.show()
        
    def import_config(self):
        """Import a configuration from file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Import Configuration", "", "JSON Files (*.json)"
        )
        
        if file_path:
            try:
                with open(file_path, 'r') as f:
                    config = validate_config(json.load(f))
                if any(c in config["name"] for c in '/\\:*?"<>|'):
                    raise ValueError("Invalid configuration name")
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                filename = CONFIG_DIR / f"config_{config['name']}.json"
                with open(filename, 'w') as f:
                    json.dump(config, f, indent=2)
                    
                self.load_configurations()
                self.status_label.setText("Configuration imported successfully")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to import configuration: {str(e)}")
                
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
        if self.can_bus is not None:
            return
        self.message_count = 0
        if self.activity_scanner and self.activity_scanner.isRunning():
            self._set_status("Wait for the activity scan to finish", "orange")
            return
        if not self.active_config or not self.selected_channel_config:
            QMessageBox.warning(self, "Connection", "Select a configuration and a CAN receiver first.")
            return
        try:
            config = validate_config(self.active_config)
            database = load_application_database(config["database_family"], DATABASES_DIR)
            if database is None:
                raise ValueError("No matching database. Create a panel in Form Designer first.")
            # Validate/build before opening hardware, so errors leave a usable UI.
            self.build_application_ui(database)
            cfg = self.selected_channel_config
            kwargs = {k: cfg[k] for k in ("unique_hardware_id", "serial", "app_name") if k in cfg}
            self.can_bus = create_can_bus(cfg["interface"], cfg.get("channel", 0), int(config["bitrate"]), **kwargs)
            self.session_config = config
            self.connected_channel_config = dict(cfg)
            self.session_generation += 1
            generation = self.session_generation
            worker = CanWorker()
            worker.setup_connection(cfg.get("channel", 0), bus=self.can_bus, config=config)
            mailbox = ReceiveMailbox(self.can_bus, worker.message_sent.emit)
            worker.add_mailbox(mailbox)
            worker.message_received.connect(lambda msg, g=generation: self.on_can_message(msg) if g == self.session_generation else None)
            worker.message_sent.connect(lambda cid, data, g=generation: self.log_can("TX", cid, data) if g == self.session_generation else None)
            worker.error_occurred.connect(lambda error, g=generation: self._session_failed(error) if g == self.session_generation else None)
            self.workers["main"] = worker
            runtime = ScriptRuntime(mailbox, config, self.panel.values(), self)
            runtime.value_changed.connect(lambda name, value, g=generation: self.panel.set_value(name, value) if g == self.session_generation and self.panel else None)
            runtime.logged.connect(self.log_verbose)
            runtime.flashing_available.connect(lambda ok, g=generation: self._set_flashing_available(ok) if g == self.session_generation else None)
            runtime.flash_progress.connect(lambda done, total, text, g=generation: self._on_flash_progress(done, total, text) if g == self.session_generation else None)
            runtime.flash_finished.connect(lambda ok, text, g=generation: self._on_flash_finished(ok, text) if g == self.session_generation else None)
            self.panel.control_changed.connect(lambda name, value: runtime.post("control", name, value))
            self.script_runtime = runtime
            worker.start()
            script_path = Path(database["source_path"]).with_name(Path(database["source_path"]).stem + "_script.py")
            runtime.start(script_path)
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            self._set_flashing_available(False)
            self.flashing_toolbar_item.setVisible(True)
            self.config_list.setEnabled(False)
            self.database_dock.show()
            self.channels_dock.show()
            self.resizeDocks([self.config_dock, self.database_dock], [280, 700], Qt.Horizontal)
            self.refresh_channel_list()
            self._set_status(f"Connected — {Path(database['source_path']).name}", "green")
            self.log_verbose(f"Loaded {database['source_path']}")
        except Exception as exc:
            self.on_disconnect_clicked()
            self._set_status(f"Connection failed: {exc}", "red")
            self.log_verbose(str(exc))

    def _session_failed(self, error):
        self.log_verbose(error)
        self.on_disconnect_clicked()
        self._set_status(error, "red")

    def on_disconnect_clicked(self):
        self.session_generation += 1
        self._close_flash_dialog()
        self.flashing_toolbar_item.setVisible(False)
        if self.script_runtime:
            for worker in self.workers.values():
                for mailbox in worker.mailboxes:
                    mailbox.close()
            self.script_runtime.stop()
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
        for state in self.node_states.values():
            state["disconnected"] = True
        for item in self.channel_items.values():
            item.setText(0, item.text(0).replace(" [Connected]", ""))
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.config_list.setEnabled(True)
        self.database_dock.hide()
        self.channels_dock.show()
        self._set_status("Disconnected", "gray")
        self.clear_application_ui()
        self._update_nodes()

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
        start_dir = settings.value("last_firmware_dir", str(APP_DIR), type=str)
        path, _ = QFileDialog.getOpenFileName(self, "Select firmware file", start_dir, FIRMWARE_FILE_FILTER)
        if not path:
            return
        settings.setValue("last_firmware_dir", str(Path(path).parent))
        try:
            firmware = load_firmware(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Flashing", f"Cannot read {Path(path).name}:\n{exc}")
            return
        ranges = "\n".join(f"0x{address:08X} - 0x{address + len(data) - 1:08X}  ({len(data)} bytes)"
                           for address, data in firmware.segments[:8])
        if len(firmware.segments) > 8:
            ranges += f"\n... {len(firmware.segments) - 8} more segment(s)"
        answer = QMessageBox.question(
            self, "Flashing",
            f"Flash {Path(path).name} ({firmware.size} bytes) to the ECU?\n\n{ranges}\n\n"
            "Keep the CAN connection and ECU power stable until flashing finishes.")
        if answer == QMessageBox.Yes:
            self.start_flashing(firmware)

    def start_flashing(self, firmware):
        if self.script_runtime is None:
            return
        self._toolbar_actions["flashing"].setEnabled(False)
        dialog = QProgressDialog(f"Flashing {Path(firmware.path).name}...", "Cancel", 0, max(1, firmware.size), self)
        dialog.setWindowTitle("Flashing")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(self.script_runtime.cancel_flash)
        dialog.show()
        self.flash_dialog = dialog
        self.log_verbose(f"Flashing {firmware.path}: {firmware.size} bytes in {len(firmware.segments)} segment(s)")
        self.script_runtime.start_flash(firmware)

    def _on_flash_progress(self, done, total, text):
        if self.flash_dialog is None:
            return
        self.flash_dialog.setMaximum(max(1, total))
        self.flash_dialog.setValue(max(0, min(done, total)))
        if text:
            self.flash_dialog.setLabelText(text)

    def _close_flash_dialog(self):
        dialog, self.flash_dialog = self.flash_dialog, None
        if dialog is not None:
            dialog.canceled.disconnect()
            dialog.close()
            dialog.deleteLater()

    def _on_flash_finished(self, ok, text):
        self._close_flash_dialog()
        self._set_flashing_available(self.script_runtime is not None and self.script_runtime.flash_function is not None)
        self.log_verbose(f"Flashing {'succeeded' if ok else 'failed'}: {text}")
        self._set_status(f"Flashing {'complete' if ok else 'failed'}", "green" if ok else "red")
        if ok:
            QMessageBox.information(self, "Flashing", text)
        else:
            QMessageBox.critical(self, "Flashing", f"Flashing failed:\n{text}")

    def closeEvent(self, event):
        self.node_timer.stop()
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
            raise RuntimeError("Connect before sending CAN messages")
        if extended is None:
            extended = not self.session_config.get("identifier_11_bit", True)
        payload = bytes(data)
        if len(payload) > 8:
            raise ValueError("Classic CAN messages cannot exceed eight bytes")
        message = can.Message(arbitration_id=can_id, data=payload, is_extended_id=extended, check=True)
        self.can_bus.send(message)
        self.log_can("TX", can_id, payload)

    def on_can_message(self, msg_dict):
        if self.session_config is None:
            return
        self.message_count += 1
        can_id, data = msg_dict["arbitration_id"], bytes(msg_dict["data"])
        if can_id in self.session_config["response_ids"] and msg_dict.get("is_extended_frame", False) == (not self.session_config["identifier_11_bit"]):
            self.node_states[(_channel_key(self.connected_channel_config), can_id)] = {
                "last_seen": time.monotonic(), "timeout": self.session_config["node_timeout_seconds"]}
            self._update_nodes()
        self.log_can("RX", can_id, data)
        if self.panel:
            try:
                self.panel.on_message(can_id, data)
            except Exception as exc:
                self.log_verbose(f"Panel decode: {exc}")
        if self.script_runtime:
            with self.script_runtime.lock:
                self.script_runtime.values.update(self.panel.values() if self.panel else {})
            self.script_runtime.post("can", can_id, data)
        if getattr(self, "_can_logger_window", None):
            self._can_logger_window.on_can_message(can_id, data)
        if getattr(self, "_diagnostic_window", None):
            self._diagnostic_window.on_can_message(can_id, data, "RX")

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
    """Main application entry point"""
    app = QApplication(sys.argv)
    
    # Set application style
    app.setStyle('Fusion')
    
    # Create and show main window
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec_())

if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        app = QApplication(sys.argv)
        MainWindow()
        print("startup ok")
        sys.exit(0)
    main()
