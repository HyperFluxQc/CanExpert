"""
CAN configurations (Configurations/config_<name>.json): defaults, validation, the UDS transport a
configuration implies, reading and saving the files, and the dialog that edits one.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from canexpert.paths import CONFIG_DIR
from canexpert.transport_settings import TransportGroup, load_transport, save_transport

# A node is reported lost after node_timeout_seconds without a reply. TesterPresent is sent several times
# per timeout window so one missed response does not cause a false loss.
DEFAULT_TESTER_PRESENT_INTERVAL = 0.5
DEFAULT_NODE_TIMEOUT = 2.0
DEFAULT_BITRATE = 500000
# Offered in the dialog; any other whole number of bits per second can be typed in.
BITRATE_PRESETS = (33333, 50000, 83333, 100000, 125000, 250000, 500000, 800000, 1000000)
OBD_FUNCTIONAL_ID = 0x7DF
DEFAULT_CONFIGURATION = {"name": "Default Configuration", "bitrate": DEFAULT_BITRATE, "identifier_11_bit": True,
                         "request_id": OBD_FUNCTIONAL_ID, "response_id": 0x7E8, "timeout_ms": 5000,
                         "extended_id": False}
_NAME_FORBIDDEN = '/\\:*?"<>|'


def validate_config(config):
    """Normalize legacy configs and reject settings that cannot be operated."""
    cfg = dict(config)
    cfg.setdefault("name", "Default Configuration")
    cfg.setdefault("bitrate", DEFAULT_BITRATE)
    cfg.setdefault("identifier_11_bit", True)
    cfg.setdefault("request_id", OBD_FUNCTIONAL_ID)
    cfg.setdefault("response_id", 0x7E8)
    cfg.setdefault("tester_present_interval_seconds", DEFAULT_TESTER_PRESENT_INTERVAL)
    cfg.setdefault("node_timeout_seconds", DEFAULT_NODE_TIMEOUT)
    cfg.setdefault("database_family", "")
    if not isinstance(cfg["name"], str) or not cfg["name"].strip():
        raise ValueError("Configuration name must be nonempty text")
    if not isinstance(cfg["database_family"], str):
        raise ValueError("Database family must be text")
    for key in ("identifier_11_bit", "extended_id"):
        if key in cfg and not isinstance(cfg[key], bool):
            raise ValueError(f"{key} must be true or false")
    for name in ("tester_present_interval_seconds", "node_timeout_seconds"):
        cfg[name] = float(cfg[name])
        if not math.isfinite(cfg[name]) or cfg[name] <= 0:
            raise ValueError(f"{name} must be positive")
    if cfg["node_timeout_seconds"] <= cfg["tester_present_interval_seconds"]:
        raise ValueError("Node timeout must exceed the TesterPresent interval")
    obd = cfg["request_id"] == OBD_FUNCTIONAL_ID and cfg["response_id"] == 0x7E8
    ids = cfg.get("response_ids") or ([*range(0x7E8, 0x7F0)] if obd else [cfg["response_id"]])
    if not isinstance(ids, list):
        raise ValueError("response_ids must be a list of numeric CAN IDs")
    cfg["response_ids"] = [int(i) for i in ids]
    maximum = 0x7FF if cfg["identifier_11_bit"] else 0x1FFFFFFF
    for value in [cfg["request_id"], cfg["response_id"], *cfg["response_ids"]]:
        if not isinstance(value, int) or not 0 <= value <= maximum:
            bits = 11 if cfg["identifier_11_bit"] else 29
            raise ValueError(f"CAN IDs must be between 0 and {maximum:X} with {bits}-bit identifiers")
    if int(cfg["bitrate"]) <= 0:
        raise ValueError("Bitrate must be positive")
    if cfg.get("extended_id") and not 0 <= int(cfg.get("extended_id_byte", -1)) <= 255:
        raise ValueError("Extended address must be a byte")
    return cfg


def diagnostic_request_id(config):
    """Request ID for UDS exchanges (scripts, flashing, Diagnostic Window).

    The OBD functional ID 0x7DF may not carry multi-frame requests (ISO 15765-2), so when the ECU
    answers on 0x7E8-0x7EF it is addressed physically at response ID - 8 (ISO 15765-4), e.g. 0x7E0.
    TesterPresent monitoring keeps using the configured request ID.
    """
    request, response = config.get("request_id", OBD_FUNCTIONAL_ID), config.get("response_id", 0x7E8)
    if config.get("identifier_11_bit", True) and request == OBD_FUNCTIONAL_ID and 0x7E8 <= response <= 0x7EF:
        return response - 8
    return request


def uds_transport(config) -> dict:
    """uds_request() keyword arguments for a configuration: IDs, reply timeout, identifier size, address
    byte, and the padding and flow control a session adds (canexpert.transport_settings.apply_transport)."""
    return {
        "request_id": diagnostic_request_id(config),
        "response_id": config.get("response_id", 0x7E8),
        "timeout": config.get("timeout_ms", 2000) / 1000.0,
        "extended": not config.get("identifier_11_bit", True),
        "address_byte": config.get("extended_id_byte") if config.get("extended_id") else None,
        "padding": config.get("isotp_padding"),
        "block_size": config.get("isotp_block_size", 0),
        "st_min": config.get("isotp_st_min", 0),
    }


def read_configurations(directory) -> tuple[list[dict], list[str]]:
    """The valid configurations of directory's config_*.json files, and one message per invalid file."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    configurations, errors = [], []
    for path in sorted(directory.glob("config_*.json")):
        try:
            configurations.append(validate_config(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append(f"Error loading config {path.name}: {exc}")
    return configurations, errors


def save_configuration(config, directory) -> dict:
    """Validate config and write it to directory/config_<name>.json; ValueError if it is invalid."""
    config = validate_config(config)
    if any(c in config["name"] for c in _NAME_FORBIDDEN):
        raise ValueError(f"Configuration name cannot contain any of {' '.join(_NAME_FORBIDDEN)}")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"config_{config['name']}.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def _hex(text, what, maximum=0x1FFFFFFF):
    try:
        value = int(str(text).strip(), 16)
    except ValueError:
        raise ValueError(f"{what} must be hexadecimal, e.g. 7DF") from None
    if not 0 <= value <= maximum:
        raise ValueError(f"{what} must be between 0 and {maximum:X}")
    return value


class ConfigurationDialog(QDialog):
    """Create or edit a configuration; accepted() once it is saved to directory."""

    def __init__(self, parent=None, config=None, directory=CONFIG_DIR, settings=None):
        super().__init__(parent)
        self.setWindowTitle("CAN Connection Configuration")
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(480, 380)
        self.config = config or {}
        self.directory = directory
        if settings is None:
            from canexpert.ui_common import app_settings   # the dialog's only use of the application settings
            settings = app_settings()
        self.settings = settings
        self._build()
        self._fill()

    def _build(self):
        layout = QVBoxLayout(self)
        group = QGroupBox("Connection Configuration")
        form = QFormLayout(group)
        self.name_edit = QComboBox()
        self.name_edit.setEditable(True)
        form.addRow("Name:", self.name_edit)
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.setEditable(True)
        self.bitrate_combo.addItems([str(rate) for rate in BITRATE_PRESETS])
        self.bitrate_combo.setCurrentText(str(DEFAULT_BITRATE))
        self.bitrate_combo.setToolTip("Pick one, or type any bit rate; the sample point is set per channel "
                                      "(right-click the channel, Channel setup...)")
        form.addRow("Bitrate (bps):", self.bitrate_combo)
        self.id_size_combo = QComboBox()
        self.id_size_combo.addItem("11 bits (Standard)", 11)
        self.id_size_combo.addItem("29 bits (Extended)", 29)
        form.addRow("Identifier size:", self.id_size_combo)
        self.server_id_edit = QLineEdit("7DF")
        self.server_id_edit.setPlaceholderText("e.g. 7DF (11-bit) or 1DDAEDE9 (29-bit) – request sent to this ID")
        form.addRow("SERVER ID (hex):", self.server_id_edit)
        self.ecu_id_edit = QLineEdit("7E8")
        self.ecu_id_edit.setPlaceholderText("e.g. 7E8 (11-bit) – ECU response ID")
        form.addRow("ECU ID (hex):", self.ecu_id_edit)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(500, 60000)
        self.timeout_spin.setSingleStep(500)
        self.timeout_spin.setSuffix(" ms")
        self.timeout_spin.setValue(5000)
        self.timeout_spin.setToolTip("How long script and Diagnostic Window UDS requests wait for a reply")
        form.addRow("UDS response timeout:", self.timeout_spin)
        self.heartbeat_spin = QDoubleSpinBox()
        self.heartbeat_spin.setRange(0.05, 3600)
        self.heartbeat_spin.setDecimals(2)
        self.heartbeat_spin.setSuffix(" s")
        form.addRow("TesterPresent interval:", self.heartbeat_spin)
        self.node_timeout_spin = QDoubleSpinBox()
        self.node_timeout_spin.setRange(0.1, 86400)
        self.node_timeout_spin.setSuffix(" s")
        form.addRow("Node loss timeout:", self.node_timeout_spin)
        self.database_family_edit = QLineEdit()
        self.database_family_edit.setPlaceholderText("Blank = newest database; e.g. engine")
        form.addRow("Database family:", self.database_family_edit)
        self.response_ids_edit = QLineEdit()
        self.response_ids_edit.setPlaceholderText("Optional hex IDs, comma-separated; blank = ECU ID / OBD range")
        form.addRow("Monitored ECU IDs:", self.response_ids_edit)
        self.extended_id_cb = QCheckBox("Extended identifier (first data byte extends ID in UDS)")
        form.addRow(self.extended_id_cb)
        self.extended_id_byte_edit = QLineEdit("00")
        self.extended_id_byte_edit.setPlaceholderText("e.g. 01 or 0x01")
        self.extended_id_cb.toggled.connect(self.extended_id_byte_edit.setEnabled)
        form.addRow("Extended ID byte (hex):", self.extended_id_byte_edit)
        layout.addWidget(group)
        self.transport_group = TransportGroup(load_transport(self.settings, self.config.get("name", "")))
        layout.addWidget(self.transport_group)
        buttons = QHBoxLayout()
        save = QPushButton("Save Configuration")
        save.clicked.connect(self.save_config)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(save)
        buttons.addWidget(cancel)
        buttons.addStretch()
        layout.addLayout(buttons)

    def _fill(self):
        config = self.config
        self.heartbeat_spin.setValue(float(config.get("tester_present_interval_seconds", DEFAULT_TESTER_PRESENT_INTERVAL)))
        self.node_timeout_spin.setValue(float(config.get("node_timeout_seconds", DEFAULT_NODE_TIMEOUT)))
        self.database_family_edit.setText(config.get("database_family", ""))
        self.response_ids_edit.setText(", ".join(f"{v:X}" for v in config.get("response_ids", [])))
        if config.get("name"):
            self.name_edit.setCurrentText(config["name"])
        if config.get("bitrate"):
            self.bitrate_combo.setCurrentText(str(config["bitrate"]))
        if config.get("identifier_11_bit") is not None:
            self.id_size_combo.setCurrentIndex(0 if config["identifier_11_bit"] else 1)
        elif config.get("identifier_bits") == 29:
            self.id_size_combo.setCurrentIndex(1)
        for key, edit in (("request_id", self.server_id_edit), ("response_id", self.ecu_id_edit)):
            if config.get(key) is not None:
                value = config[key]
                edit.setText(f"{value:X}" if isinstance(value, int) else str(value).strip())
        if config.get("timeout_ms") is not None:
            self.timeout_spin.setValue(int(config["timeout_ms"]))
        self.extended_id_cb.setChecked(bool(config.get("extended_id", False)))
        self.extended_id_byte_edit.setEnabled(self.extended_id_cb.isChecked())
        byte = config.get("extended_id_byte")
        if isinstance(byte, int) and 0 <= byte <= 255:
            self.extended_id_byte_edit.setText(f"{byte:02X}")

    def _read(self):
        """The edited configuration; ValueError with a message for the user when a field is invalid."""
        config = dict(self.config)
        config.pop("did", None)  # only used by the removed database-ID discovery
        config.update({
            "name": self.name_edit.currentText().strip() or "Unnamed",
            "bitrate": self._bitrate(),
            "identifier_11_bit": self.id_size_combo.currentData() == 11,
            "timeout_ms": self.timeout_spin.value(),
            "extended_id": self.extended_id_cb.isChecked(),
            "tester_present_interval_seconds": self.heartbeat_spin.value(),
            "node_timeout_seconds": self.node_timeout_spin.value(),
            "database_family": self.database_family_edit.text().strip(),
            "response_ids": [_hex(v, "Monitored ECU IDs") for v in self.response_ids_edit.text().split(",") if v.strip()],
        })
        for key, edit, what in (("request_id", self.server_id_edit, "SERVER ID"), ("response_id", self.ecu_id_edit, "ECU ID")):
            if edit.text().strip():
                config[key] = _hex(edit.text(), what)
        if config["extended_id"]:
            config["extended_id_byte"] = _hex(self.extended_id_byte_edit.text(), "Extended ID byte", 0xFF)
        return config

    def _bitrate(self) -> int:
        try:
            bitrate = int(self.bitrate_combo.currentText().strip())
        except ValueError:
            raise ValueError("The bit rate must be a whole number of bits per second, e.g. 500000") from None
        if not 10_000 <= bitrate <= 1_000_000:
            raise ValueError("Classic CAN runs between 10000 and 1000000 bit/s")
        return bitrate

    def save_config(self):
        try:
            transport = self.transport_group.transport()
            config = save_configuration(self._read(), self.directory)
            save_transport(self.settings, config["name"], transport)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid configuration", str(exc))
            return
        except OSError as exc:
            QMessageBox.critical(self, "Error", f"Failed to save configuration: {exc}")
            return
        self.accept()
