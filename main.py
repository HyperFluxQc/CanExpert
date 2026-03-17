#!/usr/bin/env python3
"""
CAN Expert - Main Application

Connect to a CAN channel, run UDS discovery to get a database ID, then load and display
an application database (forms with buttons, values, checkboxes, etc.).
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import can
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread, QSettings, QSize
from PyQt5.QtGui import QColor, QPalette, QIcon, QPixmap, QPainter, QPen, QBrush, QFont, QPainterPath
from PyQt5.QtWidgets import (
    QAction,
    QActionGroup,
    QApplication,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenuBar,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStyle,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QComboBox,
)
from PyQt5.QtWidgets import QSpinBox, QCheckBox  # noqa: F401 - used by config

from database_loader import load_application_database, decode_value_from_can_data
from form_designer import FormDesigner
from uds_discovery import send_uds_and_wait_response
from can_logger import CANLoggerWindow
from diagnostic_window import DiagnosticWindow

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

DEFAULT_BITRATE = 500000
DEFAULT_DID = 0xF1F0
DEFAULT_REQUEST_ID = 0x7DF
CONFIG_DIR = Path("Configurations")
DATABASES_DIR = Path("Databases")

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

class UdsDiscoveryWorker(QThread):
    """Worker that connects to CAN, sends UDS request (DID from config), and parses database ID from response."""
    database_id_ready = pyqtSignal(str)
    discovery_failed = pyqtSignal(str)
    discovery_finished = pyqtSignal()

    def __init__(self, channel, bitrate: int, connection_db_path: str = "connection_database.json", bus=None,
                 interface: str = "kvaser", connection_config: dict = None, **bus_kwargs):
        super().__init__()
        self.channel = channel
        self.bitrate = bitrate
        self.connection_db_path = connection_db_path
        self.bus = bus
        self.interface = interface
        self.bus_kwargs = bus_kwargs
        self.connection_config = connection_config or {}

    def run(self):
        try:
            with open(self.connection_db_path, "r") as f:
                connection_db = json.load(f)
        except Exception as e:
            self.discovery_failed.emit(f"Failed to load connection database: {e}")
            self.discovery_finished.emit()
            return

        own_bus = False
        if self.bus is None:
            try:
                self.bus = create_can_bus(self.interface, self.channel, self.bitrate, **self.bus_kwargs)
                own_bus = True
            except Exception as e:
                self.discovery_failed.emit(f"Failed to connect to CAN: {e}")
                self.discovery_finished.emit()
                return

        did = self.connection_config.get("did", 0xF1F0)
        timeout_ms = self.connection_config.get("timeout_ms", 5000)
        timeout_seconds = timeout_ms / 1000.0
        identifier_11_bit = self.connection_config.get("identifier_11_bit", True)
        extended_id = self.connection_config.get("extended_id", False)

        request_id = self.connection_config.get("request_id")
        response_id = self.connection_config.get("response_id")
        extended_id_byte = self.connection_config.get("extended_id_byte") if extended_id else None
        try:
            db_id = send_uds_and_wait_response(
                self.bus,
                connection_db,
                did=did,
                timeout_seconds=timeout_seconds,
                identifier_11_bit=identifier_11_bit,
                extended_id_uds=extended_id,
                extended_id_byte=extended_id_byte,
                request_id=request_id,
                response_id=response_id,
            )
            if db_id:
                self.database_id_ready.emit(db_id)
            else:
                self.discovery_failed.emit("No valid database ID in UDS response")
        except Exception as e:
            self.discovery_failed.emit(str(e))
        finally:
            if own_bus:
                try:
                    self.bus.shutdown()
                except Exception:
                    pass
        self.discovery_finished.emit()


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
    error_occurred = pyqtSignal(str)
    connection_status = pyqtSignal(bool)
    
    def __init__(self):
        super().__init__()
        self.interface = None
        self.channel = None
        self.running = False
        self.bus = None
        self.config = None
        
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
            self.error_occurred.emit(f"Using existing CAN bus on channel {ch}")
            return

        try:
            iface = (config or {}).get("interface", "kvaser")
            kwargs = {k: (config or {})[k] for k in ("unique_hardware_id", "serial", "app_name") if k in (config or {})}
            self.bus = create_can_bus(iface, ch, bitrate, **kwargs)
            self.connection_status.emit(True)
            self.error_occurred.emit(f"Connected to CAN channel {ch}")
        except Exception as e:
            self.connection_status.emit(False)
            self.error_occurred.emit(f"Failed to connect to CAN channel {ch}: {str(e)}")
            self.running = False
            
    def run(self):
        """Main thread loop"""
        if not self.bus:
            return
            
        while self.running:
            try:
                message = self.bus.recv(timeout=0.1)
                if message:
                    # Convert to dictionary for signal emission
                    msg_dict = {
                        'timestamp': message.timestamp,
                        'arbitration_id': message.arbitration_id,
                        'is_extended_frame': message.is_extended_frame,
                        'is_remote_frame': message.is_remote_frame,
                        'is_error_frame': message.is_error_frame,
                        'dlc': message.dlc,
                        'data': list(message.data),
                        'channel': self.channel
                    }
                    self.message_received.emit(msg_dict)
            except Exception as e:
                self.error_occurred.emit(f"Error receiving message: {str(e)}")
                time.sleep(0.1)
                
    def stop(self):
        """Stop the worker thread"""
        self.running = False
        if self.bus:
            self.bus.shutdown()
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

        self.did_edit = QLineEdit()
        self.did_edit.setPlaceholderText("e.g. F1F0 or 0xF1F0 (UDS ReadDataByIdentifier DID)")
        self.did_edit.setText("F1F0")
        config_layout.addRow("DID (hex):", self.did_edit)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(500, 60000)
        self.timeout_spin.setSingleStep(500)
        self.timeout_spin.setSuffix(" ms")
        self.timeout_spin.setValue(5000)
        config_layout.addRow("Timeout:", self.timeout_spin)

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

    def _parse_did(self, text: str) -> int:
        s = str(text).strip().upper().replace("0X", "")
        if not s:
            return 0xF1F0
        return int(s, 16) & 0xFFFF

    def _parse_id(self, text: str) -> int | None:
        """Parse hex ID (11-bit or 29-bit). Returns int or None if invalid."""
        s = str(text).strip().upper().replace("0X", "")
        if not s:
            return None
        try:
            return int(s, 16) & 0x1FFFFFFF
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
        if self.config.get("did") is not None:
            did = self.config["did"]
            if isinstance(did, int):
                self.did_edit.setText(f"{did:04X}")
            else:
                self.did_edit.setText(str(did).strip())
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
        config = {
            "name": self.name_edit.currentText().strip() or "Unnamed",
            "bitrate": int(self.bitrate_combo.currentText()),
            "identifier_11_bit": identifier_11_bit,
            "did": self._parse_did(self.did_edit.text()),
            "timeout_ms": self.timeout_spin.value(),
            "extended_id": extended_id,
        }
        if request_id is not None:
            config["request_id"] = request_id
        if response_id is not None:
            config["response_id"] = response_id
        if extended_id_byte is not None:
            config["extended_id_byte"] = extended_id_byte
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config_file = CONFIG_DIR / f"config_{config['name']}.json"
        try:
            with open(config_file, "w") as f:
                json.dump(config, f, indent=2)
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
        self.value_widgets = {}
        self.uds_worker = None
        self.channel_activity = []
        self.connected_channel_config = None
        self.selected_channel_config = None
        self.channel_discovered_db = {}
        self.message_count = 0
        self.activity_scanner = None

        self.init_ui()
        self.load_configurations()

    # --- UI setup ---

    def init_ui(self):
        """Build CANoe-style main window: toolbar, status bar, dockable Configuration, CAN Channels, Database, Log."""
        # Status bar (status message)
        self.status_label = QLabel("No active connections")
        self._set_status("No active connections", "gray")
        self.statusBar().addPermanentWidget(self.status_label)

        # Toolbar: Vector CANoe-style big square icon buttons (Play, Stop, custom icons)
        style = self.style()
        icon_size = QSize(36, 36)
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setIconSize(icon_size)
        toolbar.setToolButtonStyle(Qt.ToolButtonIconOnly)
        toolbar.setStyleSheet(
            "QToolBar QToolButton { padding: 4px; min-width: 44px; min-height: 44px; }"
        )

        btn_size = QSize(44, 44)
        self.connect_btn = QToolButton()
        self.connect_btn.setDefaultAction(
            QAction(style.standardIcon(QStyle.SP_MediaPlay), "Connect", self, triggered=self.on_connect_clicked)
        )
        self.connect_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.connect_btn.setFixedSize(btn_size)
        self.connect_btn.setIconSize(icon_size)
        toolbar.addWidget(self.connect_btn)

        self.disconnect_btn = QToolButton()
        self.disconnect_btn.setDefaultAction(
            QAction(style.standardIcon(QStyle.SP_MediaStop), "Disconnect", self, triggered=self.on_disconnect_clicked)
        )
        self.disconnect_btn.setEnabled(False)
        self.disconnect_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.disconnect_btn.setFixedSize(btn_size)
        self.disconnect_btn.setIconSize(icon_size)
        toolbar.addWidget(self.disconnect_btn)
        toolbar.addSeparator()

        form_btn = QToolButton()
        form_btn.setDefaultAction(QAction(self._icon_form_designer(), "Form Designer", self, triggered=self.open_form_designer))
        form_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        form_btn.setFixedSize(btn_size)
        form_btn.setIconSize(icon_size)
        toolbar.addWidget(form_btn)

        logger_btn = QToolButton()
        logger_btn.setDefaultAction(QAction(self._icon_can_logger(), "CAN Logger", self, triggered=self.open_can_logger))
        logger_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        logger_btn.setFixedSize(btn_size)
        logger_btn.setIconSize(icon_size)
        toolbar.addWidget(logger_btn)

        diag_btn = QToolButton()
        diag_btn.setDefaultAction(QAction(self._icon_diagnostic(), "Diagnostic Window", self, triggered=self.open_diagnostic_window))
        diag_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        diag_btn.setFixedSize(btn_size)
        diag_btn.setIconSize(icon_size)
        toolbar.addWidget(diag_btn)

        self.addToolBar(toolbar)

        # Central area: empty placeholder (docks sit around it)
        central = QWidget()
        central.setMinimumSize(400, 300)
        self.setCentralWidget(central)

        # Dock: Configuration (closable, collapsible)
        config_widget = QWidget()
        config_layout = QVBoxLayout()
        self.config_list = QListWidget()
        self.config_list.itemClicked.connect(self.on_config_selected)
        config_layout.addWidget(self.config_list)
        btn_row = QHBoxLayout()
        self.new_config_btn = QPushButton("New Configuration")
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
        self.channel_list = QListWidget()
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

    def _icon_form_designer(self):
        """Icon: sheet with text lines, buttons, and magnifying glass on top."""
        pm = QPixmap(36, 36)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(60, 60, 60), 1))
        p.setBrush(QBrush(QColor(255, 255, 240)))
        p.drawRoundedRect(4, 8, 22, 26, 2, 2)
        p.setPen(QPen(QColor(80, 80, 80), 1))
        p.drawLine(8, 12, 20, 12)
        p.drawLine(8, 15, 18, 15)
        p.drawLine(8, 18, 22, 18)
        p.setBrush(QBrush(QColor(220, 220, 220)))
        p.setPen(QPen(QColor(100, 100, 100), 1))
        p.drawRoundedRect(8, 21, 7, 5, 1, 1)
        p.drawRoundedRect(17, 21, 7, 5, 1, 1)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(60, 60, 60), 1))
        p.drawEllipse(14, 4, 12, 12)
        p.drawLine(24, 12, 30, 18)
        p.end()
        return QIcon(pm)

    def _icon_can_logger(self):
        """Icon: simple graph (axes + rising line)."""
        pm = QPixmap(36, 36)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(60, 60, 60), 2))
        p.drawLine(6, 28, 6, 8)
        p.drawLine(6, 28, 30, 28)
        p.setPen(QPen(QColor(0, 100, 200), 2))
        path = QPainterPath()
        path.moveTo(8, 26)
        path.lineTo(12, 20)
        path.lineTo(16, 22)
        path.lineTo(22, 12)
        path.lineTo(28, 16)
        p.drawPath(path)
        p.end()
        return QIcon(pm)

    def _icon_diagnostic(self):
        """Icon: bold text 'DIAG'."""
        pm = QPixmap(36, 36)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setPen(QColor(40, 40, 40))
        f = QFont()
        f.setBold(True)
        f.setPixelSize(12)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignCenter, "DIAG")
        p.end()
        return QIcon(pm)

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
        """Populate channel list from all supported interfaces (Kvaser, Vector, IXXAT)."""
        self.channel_list.clear()
        self.can_channels = []
        try:
            for iface_key, iface_label in SUPPORTED_INTERFACES:
                try:
                    configs = can.detect_available_configs(interfaces=[iface_key], timeout=2.0)
                    for cfg in configs:
                        cfg["interface"] = iface_key
                        self.can_channels.append(cfg)
                        ch = cfg.get("channel", 0)
                        name = cfg.get("device_name", cfg.get("description", iface_label))
                        serial = cfg.get("serial", 0) or cfg.get("unique_hardware_id", "")
                        discovered = self.channel_discovered_db.get(_channel_key(cfg))
                        label = f"[{iface_label}] Ch {ch}: {name}"
                        if serial:
                            label += f" ({serial})"
                        if discovered:
                            label += f" → DB {discovered}"
                        item = QListWidgetItem(label)
                        item.setData(Qt.UserRole, cfg)
                        idx = len(self.can_channels) - 1
                        active = (isinstance(self.channel_activity, list) and idx < len(self.channel_activity) and self.channel_activity[idx])
                        conn_ch = getattr(self, "connected_channel_config", None)
                        is_connected = conn_ch and self._channel_config_match(conn_ch, cfg)
                        if is_connected:
                            item.setForeground(QColor("green"))
                            item.setText(label + " [Connected]")
                        elif discovered or active:
                            item.setForeground(QColor("green"))
                            if not discovered and active:
                                item.setText(label + " [Activity]")
                            else:
                                item.setText(label)
                        else:
                            item.setForeground(QColor("gray"))
                        self.channel_list.addItem(item)
                except Exception:
                    pass
        except Exception as e:
            item = QListWidgetItem(f"Error: {e}")
            item.setForeground(QColor("red"))
            self.channel_list.addItem(item)
        if not self.can_channels:
            item = QListWidgetItem("No CAN channels found (Kvaser/Vector/IXXAT)")
            item.setForeground(QColor("orange"))
            self.channel_list.addItem(item)

    def _channel_config_match(self, a: dict, b: dict) -> bool:
        """Return True if both configs refer to the same channel."""
        if a.get("interface") != b.get("interface"):
            return False
        if a.get("channel") != b.get("channel"):
            return False
        if a.get("unique_hardware_id") != b.get("unique_hardware_id"):
            return False
        return True

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
        self.activity_scanner.scan_finished.connect(self.on_activity_scan_finished)
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

    def on_channel_selected(self, item):
        """Store selected channel for connection (channel is not part of config)."""
        cfg = item.data(Qt.UserRole)
        if cfg is not None:
            self.selected_channel_config = cfg
            ch = cfg.get("channel", 0)
            iface_label = next((l for k, l in SUPPORTED_INTERFACES if k == cfg.get("interface")), "CAN")
            db = self.channel_discovered_db.get(_channel_key(cfg))
            if db:
                self.status_label.setText(f"Selected {iface_label} Ch {ch} (DB {db}) — double-click to load")
            else:
                self.status_label.setText(f"Selected {iface_label} Channel {ch} — Connect or double-click to discover")

    def on_channel_double_clicked(self, item):
        """Double-click: load database for this channel (if discovered) or connect to discover."""
        cfg = item.data(Qt.UserRole)
        if cfg is None:
            return
        self.selected_channel_config = cfg
        db_id = self.channel_discovered_db.get(_channel_key(cfg))
        if db_id:
            self._load_database_for_channel(cfg, db_id)
        else:
            if self.active_config:
                self.on_connect_clicked()
            else:
                QMessageBox.warning(self, "Warning", "Select a configuration first, then double-click a channel.")

    def _load_database_for_channel(self, channel_cfg: dict, db_id: str):
        """Connect to channel and load database without sending UDS (already discovered)."""
        if not self.active_config:
            QMessageBox.warning(self, "Warning", "Select a configuration first (for bitrate)")
            return
        iface = channel_cfg.get("interface", "kvaser")
        channel = channel_cfg.get("channel", 0)
        bitrate = int(self.active_config.get("bitrate", 500000))
        bus_kwargs = {k: channel_cfg[k] for k in ("unique_hardware_id", "serial", "app_name") if k in channel_cfg}
        try:
            self.can_bus = create_can_bus(iface, channel, bitrate, **bus_kwargs)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to connect: {e}")
            return
        self.connected_channel_config = dict(channel_cfg)
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        app_db = load_application_database(db_id)
        if not app_db:
            self._set_status(f"Database {db_id} not found", "orange")
            return
        self.app_database = app_db
        self.build_application_ui(app_db)
        self.log_verbose(f"Loaded database '{db_id}' (double-click).")
        self._set_status(f"Connected - Database: {db_id}", "green")
        self.channels_dock.hide()
        self.database_dock.show()
        self.message_count = 0
        self.refresh_channel_list()
        worker = CanWorker()
        worker.setup_connection(channel=channel, bus=self.can_bus, config=self.active_config)
        worker.message_received.connect(self.on_can_message)
        worker.error_occurred.connect(lambda msg: self.status_label.setText(msg))
        worker.start()
        self.workers["main"] = worker

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
        settings = QSettings("EZCan2", "KvaserCAN")
        saved_theme = settings.value("theme", "light", type=str)
        self.apply_theme(saved_theme, restore=True)

        # Help menu (far right via corner widget)
        help_menubar = QMenuBar(self)
        help_menu = help_menubar.addMenu('Help')
        help_menu.addAction('About').triggered.connect(self.show_about)
        menubar.setCornerWidget(help_menubar, Qt.TopRightCorner)

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
        if not restore:
            settings = QSettings("EZCan2", "KvaserCAN")
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
                        config = json.load(f)
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
                "did": DEFAULT_DID,
                "timeout_ms": 5000,
                "extended_id": False,
            }
            self.configurations.append(default_config)
            item = QListWidgetItem(default_config["name"])
            self.config_list.addItem(item)
        self.log_verbose(f"Loaded {len(self.configurations)} configuration(s).")
            
    def open_form_designer(self):
        """Open the Form Designer dialog."""
        designer = FormDesigner(self)
        designer.saved.connect(lambda p: self.load_configurations())
        designer.exec_()

    def open_can_logger(self):
        """Open the CAN Logger window."""
        if not getattr(self, "_can_logger_window", None) or not self._can_logger_window.isVisible():
            self._can_logger_window = CANLoggerWindow(self)
        self._can_logger_window.show()
        self._can_logger_window.raise_()
        self._can_logger_window.activateWindow()

    def open_diagnostic_window(self):
        """Open the Diagnostic Window."""
        if not getattr(self, "_diagnostic_window", None) or not self._diagnostic_window.isVisible():
            self._diagnostic_window = DiagnosticWindow(self)
        self._diagnostic_window.show()
        self._diagnostic_window.raise_()
        self._diagnostic_window.activateWindow()

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
                    config = json.load(f)
                    
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
        """Connect to selected channel, send UDS ReadDataByIdentifier, load app database when ID is received."""
        if not self.active_config:
            QMessageBox.warning(self, "Warning", "Select a configuration first")
            return
        if not self.selected_channel_config:
            QMessageBox.warning(self, "Warning", "Select a channel from the CAN Channels list first")
            return
        cfg = self.selected_channel_config
        iface = cfg.get("interface", "kvaser")
        channel = cfg.get("channel", 0)
        bitrate = int(self.active_config.get("bitrate", 500000))
        bus_kwargs = {k: cfg[k] for k in ("unique_hardware_id", "serial", "app_name") if k in cfg}
        self.connect_btn.setEnabled(False)
        self._set_status("Connecting and sending UDS request...", "orange")
        self.log_verbose(f"Connecting to {iface} channel {channel} at {bitrate} bps…")

        try:
            self.can_bus = create_can_bus(iface, channel, bitrate, **bus_kwargs)
        except Exception as e:
            self.connect_btn.setEnabled(True)
            QMessageBox.critical(self, "Error", f"Failed to connect to CAN: {e}")
            self._set_status("Connection failed", "red")
            return

        self.connected_channel_config = dict(cfg)
        did = self.active_config.get("did", 0xF1F0)
        self.log_verbose(f"Sending UDS ReadDataByIdentifier DID=0x{did:04X}…")
        self.uds_worker = UdsDiscoveryWorker(
            channel,
            bitrate,
            bus=self.can_bus,
            interface=iface,
            connection_config=self.active_config,
            **bus_kwargs,
        )
        self.uds_worker.database_id_ready.connect(self.on_uds_database_id)
        self.uds_worker.discovery_failed.connect(self.on_uds_failed)
        self.uds_worker.discovery_finished.connect(self.on_uds_finished)
        self.uds_worker.start()

    def on_uds_database_id(self, db_id: str):
        """UDS response received - store discovered DB for channel, load application database and build UI."""
        if self.connected_channel_config:
            self.channel_discovered_db[_channel_key(self.connected_channel_config)] = db_id
        self.log_verbose(f"UDS response: database ID {db_id}.")
        self._set_status(f"Database ID: {db_id} - Loading...", "orange")
        app_db = load_application_database(db_id)
        if not app_db:
            self._set_status(f"Database {db_id} not found (create Databases/{db_id}.xml)", "orange")
            return
        self.app_database = app_db
        self.build_application_ui(app_db)
        self.log_verbose(f"Loaded database '{db_id}' successfully.")
        self._set_status(f"Connected - Database: {db_id}", "green")
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        self.channels_dock.hide()
        self.database_dock.show()
        self.message_count = 0
        self.refresh_channel_list()

        channel = self.connected_channel_config.get("channel", 0) if self.connected_channel_config else 0
        self.log_verbose("CAN receive worker started.")
        worker = CanWorker()
        worker.setup_connection(channel=channel, bus=self.can_bus, config=self.active_config)
        worker.message_received.connect(self.on_can_message)
        worker.error_occurred.connect(lambda msg: self.status_label.setText(msg))
        worker.start()
        self.workers["main"] = worker

    def on_uds_failed(self, msg: str):
        """UDS discovery failed."""
        self.log_verbose(f"UDS discovery failed: {msg}")
        self._set_status(f"Discovery failed: {msg}", "red")
        self.connect_btn.setEnabled(True)
        if self.can_bus:
            try:
                self.can_bus.shutdown()
            except Exception:
                pass
            self.can_bus = None

    def on_uds_finished(self):
        """UDS worker finished (cleanup if needed)."""
        self.uds_worker = None

    def on_disconnect_clicked(self):
        """Disconnect and stop workers."""
        self.log_verbose("Disconnecting…")
        for w in self.workers.values():
            w.stop()
        self.workers.clear()
        self.connected_channel_config = None
        self.refresh_channel_list()
        if self.can_bus:
            try:
                self.can_bus.shutdown()
            except Exception:
                pass
            self.can_bus = None
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.database_dock.hide()
        self.channels_dock.show()
        self._set_status("Disconnected", "gray")
        self.clear_application_ui()

    def clear_application_ui(self):
        """Remove all widgets from application database panel."""
        while self.app_db_layout.count():
            item = self.app_db_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.value_widgets.clear()
        self.app_database = None

    @staticmethod
    def _get_can_id(w: dict) -> int:
        """Return CAN ID from a widget dict, or 0 if not set."""
        cid = w.get("can_id")
        return int(cid) if cid else 0

    # --- Application database UI ---

    def build_application_ui(self, app_db: dict):
        """Build dynamic UI from application database (supports pages and XY placement)."""
        self.clear_application_ui()

        if app_db.get("description"):
            desc = QLabel(app_db["description"])
            desc.setStyleSheet("font-weight: bold; margin: 4px;")
            self.app_db_layout.addWidget(desc)

        if app_db.get("pages"):
            self._build_pages_ui(app_db["pages"])
        else:
            self._build_flat_ui(app_db)

        self.app_db_layout.addStretch()

    def _build_pages_ui(self, pages: list):
        """Build UI with one tab per page; widgets placed at (x, y) on each page."""
        tabs = QTabWidget()
        for page in pages:
            page_name = page.get("name", "Page")
            container = QWidget()
            container.setMinimumSize(600, 400)
            for b in page.get("buttons", []):
                pb = QPushButton(b["label"])
                can_id = self._get_can_id(b)
                if can_id:
                    data_bytes = b.get("data_bytes", [0] * 8)
                    pb.clicked.connect(lambda checked, cid=can_id, d=data_bytes: self.send_can_message(cid, d))
                # else: variable-only, script uses api.ui.set_value(variable, ...)
                pb.setParent(container)
                pb.move(b.get("x", 0), b.get("y", 0))
                pb.show()
            for v in page.get("values", []):
                lbl = QLabel("--")
                lbl.setMinimumWidth(80)
                lbl.setParent(container)
                lbl.move(v.get("x", 0), v.get("y", 0))
                lbl.show()
                can_id = self._get_can_id(v)
                if can_id:
                    self.value_widgets[can_id] = self.value_widgets.get(can_id, [])
                    self.value_widgets[can_id].append((v, lbl))
                # else: variable-only, script uses api.ui.set_value to update
            for c in page.get("checkboxes", []):
                cb = QCheckBox(c["label"])
                can_id = self._get_can_id(c)
                if can_id:
                    byte_idx = c.get("byte", 0)
                    bit_idx = c.get("bit", 0)
                    cb.stateChanged.connect(
                        lambda state, cid=can_id, bid=byte_idx, bit=bit_idx: self.send_checkbox_state(cid, bid, bit, state)
                    )
                cb.setParent(container)
                cb.move(c.get("x", 0), c.get("y", 0))
                cb.show()
            for s in page.get("sliders", []):
                sl = QSlider(Qt.Horizontal)
                sl.setRange(s.get("min", 0), s.get("max", 100))
                sl.setValue(0)
                sl.setFixedWidth(120)
                can_id = self._get_can_id(s)
                if can_id:
                    byte_idx = s.get("byte", 0)
                    sl.valueChanged.connect(lambda val, cid=can_id, bid=byte_idx: self.send_slider_value(cid, bid, val))
                sl.setParent(container)
                sl.move(s.get("x", 0), s.get("y", 0))
                sl.show()
            for lbl_def in page.get("labels", []):
                lbl = QLabel(lbl_def.get("text", ""))
                lbl.setParent(container)
                lbl.move(lbl_def.get("x", 0), lbl_def.get("y", 0))
                lbl.show()
            for g in page.get("gauges", []):
                gl = QLabel(f"{g.get('label', '')} --")
                gl.setMinimumWidth(g.get("width", 100))
                gl.setParent(container)
                gl.move(g.get("x", 0), g.get("y", 0))
                gl.show()
                can_id = self._get_can_id(g)
                if can_id:
                    self.value_widgets[can_id] = self.value_widgets.get(can_id, [])
                    self.value_widgets[can_id].append((dict(g, byte_start=0, byte_length=4, scale=1, offset=0, type=g.get("value_type", "float")), gl))
            for p in page.get("progress_bars", []):
                prog = QSlider(Qt.Horizontal)
                prog.setRange(p.get("min", 0), p.get("max", 100))
                prog.setValue(0)
                prog.setFixedSize(p.get("width", 120), p.get("height", 24))
                prog.setParent(container)
                prog.move(p.get("x", 0), p.get("y", 0))
                prog.show()
            for led_def in page.get("leds", []):
                led_lbl = QLabel(led_def.get("off_text", "OFF"))
                led_lbl.setStyleSheet("background: #444; color: #fff; padding: 4px; border-radius: 4px;")
                led_lbl.setParent(container)
                led_lbl.move(led_def.get("x", 0), led_def.get("y", 0))
                led_lbl.show()
            for c in page.get("combos", []):
                combo = QComboBox()
                items = [s.strip() for s in (c.get("items") or "").split(",") if s.strip()]
                combo.addItems(items)
                combo.setFixedSize(c.get("width", 100), c.get("height", 28))
                combo.setParent(container)
                combo.move(c.get("x", 0), c.get("y", 0))
                combo.show()
            for io in page.get("io_boxes", []):
                io_edit = QLineEdit()
                io_edit.setPlaceholderText(io.get("label", ""))
                io_edit.setFixedSize(io.get("width", 80), io.get("height", 28))
                io_edit.setParent(container)
                io_edit.move(io.get("x", 0), io.get("y", 0))
                io_edit.show()
            tabs.addTab(container, page_name)
        self.app_db_layout.addWidget(tabs)

    def _build_flat_ui(self, app_db: dict):
        """Legacy: single area with grouped buttons/values/checkboxes/sliders."""
        if app_db.get("buttons"):
            btn_group = QGroupBox("Buttons")
            btn_layout = QVBoxLayout()
            for b in app_db["buttons"]:
                pb = QPushButton(b["label"])
                can_id = self._get_can_id(b)
                if can_id:
                    data_bytes = b.get("data_bytes", [0] * 8)
                    pb.clicked.connect(lambda checked, cid=can_id, d=data_bytes: self.send_can_message(cid, d))
                btn_layout.addWidget(pb)
            btn_group.setLayout(btn_layout)
            self.app_db_layout.addWidget(btn_group)

        if app_db.get("values"):
            val_group = QGroupBox("Values")
            val_layout = QFormLayout()
            for v in app_db["values"]:
                lbl = QLabel("--")
                lbl.setMinimumWidth(80)
                val_layout.addRow(f"{v['label']} ({v.get('unit', '')}):", lbl)
                can_id = self._get_can_id(v)
                if can_id:
                    self.value_widgets[can_id] = self.value_widgets.get(can_id, [])
                    self.value_widgets[can_id].append((v, lbl))
            val_group.setLayout(val_layout)
            self.app_db_layout.addWidget(val_group)

        if app_db.get("checkboxes"):
            cb_group = QGroupBox("Options")
            cb_layout = QVBoxLayout()
            for c in app_db["checkboxes"]:
                cb = QCheckBox(c["label"])
                can_id = self._get_can_id(c)
                if can_id:
                    byte_idx = c.get("byte", 0)
                    bit_idx = c.get("bit", 0)
                    cb.stateChanged.connect(
                        lambda state, cid=can_id, bid=byte_idx, bit=bit_idx: self.send_checkbox_state(cid, bid, bit, state)
                    )
                cb_layout.addWidget(cb)
            cb_group.setLayout(cb_layout)
            self.app_db_layout.addWidget(cb_group)

        if app_db.get("sliders"):
            sl_group = QGroupBox("Sliders")
            sl_layout = QFormLayout()
            for s in app_db["sliders"]:
                sl = QSlider(Qt.Horizontal)
                sl.setRange(s.get("min", 0), s.get("max", 100))
                sl.setValue(0)
                can_id = self._get_can_id(s)
                if can_id:
                    byte_idx = s.get("byte", 0)
                    sl.valueChanged.connect(lambda val, cid=can_id, bid=byte_idx: self.send_slider_value(cid, bid, val))
                sl_layout.addRow(s["label"], sl)
            sl_group.setLayout(sl_layout)
            self.app_db_layout.addWidget(sl_group)

    def send_can_message(self, can_id: int, data: list):
        """Send CAN message."""
        if not self.can_bus:
            return
        data = list(data)[:8]
        data.extend([0] * (8 - len(data)))
        self.log_can("TX", can_id, data)
        msg = can.Message(arbitration_id=can_id, data=data, is_extended_id=False)
        self.can_bus.send(msg)

    def send_checkbox_state(self, can_id: int, byte_idx: int, bit_idx: int, state: int):
        """Send checkbox state as CAN message (simplified: single byte)."""
        if not self.can_bus:
            return
        data = [0] * 8
        if state == Qt.Checked:
            data[byte_idx] = 1 << bit_idx
        self.log_can("TX", can_id, data)
        msg = can.Message(arbitration_id=can_id, data=data, is_extended_id=False)
        self.can_bus.send(msg)

    def send_slider_value(self, can_id: int, byte_idx: int, value: int):
        """Send slider value as CAN message."""
        if not self.can_bus:
            return
        data = [0] * 8
        data[byte_idx] = min(255, max(0, value))
        self.log_can("TX", can_id, data)
        msg = can.Message(arbitration_id=can_id, data=data, is_extended_id=False)
        self.can_bus.send(msg)

    def on_can_message(self, msg_dict: dict):
        """Update value displays when a CAN message is received."""
        self.message_count += 1
        if self.message_count % 50 == 1 and self.app_database:
            self.status_label.setText(f"Connected (DB: {self.app_database['name']}) - {self.message_count} msgs")
        can_id = msg_dict.get("arbitration_id")
        data = msg_dict.get("data", [])
        self.log_can("RX", can_id, data)
        if getattr(self, "_can_logger_window", None) and self._can_logger_window.isVisible():
            self._can_logger_window.on_can_message(can_id, data)
        if getattr(self, "_diagnostic_window", None) and self._diagnostic_window.isVisible():
            self._diagnostic_window.on_can_message(can_id, data, "RX")
        if can_id not in self.value_widgets:
            return
        for v, lbl in self.value_widgets[can_id]:
            val = decode_value_from_can_data(
                data, v["byte_start"], v["byte_length"], v["scale"], v["offset"], v.get("type", "float")
            )
            unit = v.get("unit", "")
            lbl.setText(f"{val} {unit}".strip())

    # --- Configuration selection ---

    def on_config_selected(self, item):
        """Set active configuration when user clicks one in the list."""
        config_name = item.text()
        
        # Find the selected configuration
        for config in self.configurations:
            if config['name'] == config_name:
                self.active_config = config
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
    main()