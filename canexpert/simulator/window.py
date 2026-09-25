"""Dummy ECU window: connect the simulated ECU to a CAN channel (e.g. a Kvaser virtual channel) and set how it
answers - addressing, ISO-TP flow control, UDS timing, periodic data, security levels and access rules,
flashing and its bootloader, the application frames with a generator per signal, its data (the DIDs, the
DTCs with their faults and life cycle, services forced to answer with a negative response) and transport
errors on purpose. Changes apply at once, even while connected; the settings are remembered, and can be
saved and loaded as JSON profiles. Several dummy ECUs can share a channel when each has its own identifiers."""
from __future__ import annotations

import collections
import json
import sys
import threading
import time
from dataclasses import asdict, fields, replace

import can
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from canexpert.paths import DBC_DIR
from canexpert.simulator.fields import (
    parse_byte_list,
    parse_did_list,
    parse_name,
    parse_address_format,
    parse_ranges,
    format_ranges,
    stmin_text,
)
from canexpert.simulator.ecu import (
    DEFAULT_CONNECTION,
    DEFAULT_DIDS,
    PERIODIC_MODES,
    SESSION_NAMES,
    DummyEcu,
    EcuConfig,
    claim_channel,
    config_from_dict,
    load_profile,
    other_ecu_present,
    parse_channel,
    save_profile,
)
from canexpert.simulator.signals import KNOWN_GENERATORS, SignalSimulation
from canexpert.uds.client import NRC_NAMES
from canexpert.uds.isotp import flow_control_frame
from canexpert.ui_common import app_icon, app_settings
from canexpert.simulator.window_pages import Pages
from canexpert.simulator.window_tables import BUILTIN_DBC_TEXT, Tables

SETTINGS_KEY = "dummy_ecu/profile"
ERROR_STYLE = "background: #fde2e2;"




def _stamp() -> str:
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"


def saved_profile() -> tuple[EcuConfig, dict]:
    """The settings the window had when it was last closed. Settings remembered before the ECU had signals
    (no generators in them) also get the DIDs that came with them - the live, periodic and protected
    ones - beside the DIDs they had."""
    try:
        values = json.loads(app_settings().value(SETTINGS_KEY, "") or "{}")
        ecu = dict(values.get("ecu", {}))
        if ecu.get("dids") and "generators" not in ecu:
            known = {int(item["did"]) for item in ecu["dids"]}
            ecu["dids"] = list(ecu["dids"]) + [dict(item) for item in DEFAULT_DIDS if item["did"] not in known]
        return config_from_dict(ecu), dict(values.get("connection", {}))
    except (ValueError, TypeError, KeyError):
        return EcuConfig(), {}






class DummyEcuWindow(Pages, Tables, QMainWindow):
    def __init__(self, config: EcuConfig | None = None, connection: dict | None = None):
        super().__init__()
        self.setWindowTitle("Dummy ECU")
        self.setWindowIcon(app_icon("dummy_ecu"))
        self.resize(1280, 820)
        self._lines = collections.deque()          # log lines from the ECU thread, shown by a timer
        self._bus = self._lock = self._stop = self._thread = None
        self._loading = True                         # widgets being built or filled: no _apply
        self._dbc_path = ""
        self.ecu = DummyEcu(None, replace(config or EcuConfig()), log=self._log)
        self._build()
        self._fill(self.ecu.config, {**DEFAULT_CONNECTION, **(connection or {})})
        self._apply()                                # the ECU uses exactly what the widgets show
        self._update_labels()
        self._set_connected(False)
        self._flush_timer = QTimer(self)
        self._flush_timer.timeout.connect(self._flush_log)
        self._flush_timer.start(100)
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(250)
        QTimer.singleShot(0, self.detect_channels)

    # --- widgets ------------------------------------------------------------------------


    def _build(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._connection_bar())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._addressing_page(), "Addressing")
        self.tabs.addTab(self._flow_page(), "Flow control")
        self.tabs.addTab(self._uds_page(), "UDS")
        self.tabs.addTab(self._access_page(), "Access")
        self.tabs.addTab(self._flashing_page(), "Flashing")
        self.signals_tab = self._signals_page()
        self.tabs.addTab(self.signals_tab, "Signals")
        self.data_tab = self._data_page()
        self.tabs.addTab(self.data_tab, "Data")
        self.tabs.addTab(self._errors_page(), "Errors")
        settings = QWidget()
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.addWidget(self.tabs)
        buttons = QHBoxLayout()
        for text, slot in (("Load profile...", self.load_profile), ("Save profile...", self.save_profile),
                           ("Restore defaults", self.restore_defaults)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        settings_layout.addLayout(buttons)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(settings)
        splitter.addWidget(self._monitor())
        splitter.setSizes([700, 580])
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)


    # --- settings <-> widgets ---------------------------------------------------------------


    # --- tables ---------------------------------------------------------------------------------


    # --- reading the widgets -----------------------------------------------------------------------


    def _fill(self, config: EcuConfig, connection: dict | None = None):
        self._loading = True
        try:
            if connection:
                self.interface.setCurrentText(str(connection.get("interface", DEFAULT_CONNECTION["interface"])))
                self.channel.setEditText(str(connection.get("channel", DEFAULT_CONNECTION["channel"])))
                self.bitrate.setCurrentText(str(connection.get("bitrate", DEFAULT_CONNECTION["bitrate"])))
            self.request_id.setValue(config.request_id)
            self.functional_id.setValue(config.functional_id)
            self.response_id.setValue(config.response_id)
            self.extended_ids.setChecked(config.extended_ids)
            self.use_address_byte.setChecked(config.address_byte is not None)
            self.address_byte.setValue(config.address_byte or 0)
            self.use_padding.setChecked(config.padding is not None)
            self.padding.setValue(0xAA if config.padding is None else config.padding)
            self.j1939.setChecked(config.j1939)
            self.j1939_address.setValue(config.j1939_address)
            self.j1939_name.setText(f"{config.j1939_name:016X}")
            self.block_size.setValue(config.block_size)
            microseconds = 0xF1 <= config.st_min <= 0xF9
            self.st_min_unit.setCurrentIndex(1 if microseconds else 0)
            self.st_min.setRange(*((1, 9) if microseconds else (0, 127)))
            self.st_min.setValue(config.st_min - 0xF0 if microseconds else min(config.st_min, 127))
            self.flow_waits.setValue(config.flow_waits)
            self.wait_interval.setValue(config.wait_interval_ms)
            self.rx_buffer.setValue(config.rx_buffer)
            self.p2.setValue(config.p2_ms)
            self.p2_star.setValue(config.p2_star_ms)
            self.response_delay.setValue(config.response_delay_ms)
            self.pending_interval.setValue(round(config.pending_interval * 1000))
            self.s3.setValue(config.s3_timeout)
            self.programming_needs_extended.setChecked(config.programming_needs_extended)
            for spin, rate in zip(self.periodic_rates, config.periodic_rates_ms):
                spin.setValue(int(rate))
            self.periodic_own_id.setChecked(config.periodic_id is not None)
            self.periodic_id.setValue(0x6FF if config.periodic_id is None else config.periodic_id)
            self.security_level.setValue(config.security_level)
            self.seed_length.setValue(config.seed_length)
            self.key_mask.setValue(config.key_mask)
            self.key_dll.setText(config.key_dll)
            self.key_variant.setText(config.key_variant)
            self.max_attempts.setValue(config.max_attempts)
            self.lockout.setValue(config.lockout_seconds)
            self.block_data.setValue(max(1, config.max_block_length - 2))
            self.length_bytes.setValue(config.block_length_bytes)
            self.full_blocks.setChecked(config.full_blocks)
            self.data_formats.setText(", ".join(f"{value:02X}" for value in config.data_formats))
            self.address_format.setCurrentText("Any" if config.address_format is None
                                               else f"{config.address_format:02X}")
            self.memory_ranges.setText(format_ranges(config.memory_ranges))
            self.require_erase.setChecked(config.require_erase)
            self.erase_routine.setValue(config.erase_routine)
            self.erase_seconds.setValue(config.erase_seconds)
            self.check_routine.setValue(config.check_routine)
            self.self_test_routine.setValue(config.self_test_routine)
            self.self_test_seconds.setValue(config.self_test_seconds)
            self.image_crc.setCurrentIndex(max(0, self.image_crc.findData(config.image_crc)))
            self.version_from_image.setChecked(config.version_address is not None)
            self.version_address.setValue(0x20000 if config.version_address is None else config.version_address)
            self.version_length.setValue(config.version_length)
            self.allow_upload.setChecked(config.allow_upload)
            self.dump_path.setText(config.dump_path or "")
            self.broadcast.setChecked(config.broadcast_interval > 0)
            self.broadcast_interval.setValue(round(config.broadcast_interval * 1000) or 100)
            self.confirm_cycles.setValue(config.confirm_cycles)
            self.aging_cycles.setValue(config.aging_cycles)
            self.operation_cycle.setValue(config.operation_cycle_seconds)
            self.snapshot_dids.setText(", ".join(f"{did:04X}" for did in config.snapshot_dids))
            for name, spin in self.error_spins.items():
                spin.setValue(int(getattr(config, name)))
            self.error_refuse_nrc.setValue(config.error_refuse_nrc)
            self.errors_on_tester_present.setChecked(config.errors_on_tester_present)
            self._fill_tables(config)
        finally:
            self._loading = False

    def _read_config(self) -> EcuConfig:
        """Settings from the widgets; ValueError (and the field marked red) when a text field is invalid."""
        errors = []

        def parsed(widget, parse):
            text = widget.text() if isinstance(widget, QLineEdit) else widget.currentText()
            try:
                value = parse(text)
            except ValueError as exc:
                widget.setStyleSheet(ERROR_STYLE)
                widget.setToolTip(str(exc))
                errors.append(str(exc))
                return None
            widget.setStyleSheet("")
            widget.setToolTip("")
            return value

        data_formats = parsed(self.data_formats, parse_byte_list)
        address_format = parsed(self.address_format, parse_address_format)
        memory_ranges = parsed(self.memory_ranges, parse_ranges)
        snapshot_dids = parsed(self.snapshot_dids, parse_did_list)
        j1939_name = parsed(self.j1939_name, parse_name)
        try:
            dids, dtcs, forced_nrcs, levels, rules, messages, generators = self._read_tables()
        except ValueError as exc:
            errors.append(str(exc))
        if errors:
            raise ValueError("; ".join(errors))
        st_min = self.st_min.value() if self.st_min_unit.currentIndex() == 0 else 0xF0 + self.st_min.value()
        return EcuConfig(
            request_id=self.request_id.value(), functional_id=self.functional_id.value(),
            response_id=self.response_id.value(), extended_ids=self.extended_ids.isChecked(),
            address_byte=self.address_byte.value() if self.use_address_byte.isChecked() else None,
            padding=self.padding.value() if self.use_padding.isChecked() else None,
            block_size=self.block_size.value(), st_min=st_min, flow_waits=self.flow_waits.value(),
            wait_interval_ms=self.wait_interval.value(), rx_buffer=self.rx_buffer.value(),
            p2_ms=self.p2.value(), p2_star_ms=self.p2_star.value(), response_delay_ms=self.response_delay.value(),
            pending_interval=self.pending_interval.value() / 1000, s3_timeout=self.s3.value(),
            programming_needs_extended=self.programming_needs_extended.isChecked(),
            security_level=self.security_level.value() | 1, seed_length=self.seed_length.value(),
            key_mask=self.key_mask.value(), key_dll=self.key_dll.text().strip(),
            key_variant=self.key_variant.text().strip(), security_levels=levels,
            max_attempts=self.max_attempts.value(), lockout_seconds=self.lockout.value(), service_rules=rules,
            periodic_rates_ms=tuple(spin.value() for spin in self.periodic_rates),
            periodic_id=self.periodic_id.value() if self.periodic_own_id.isChecked() else None,
            data_formats=data_formats, address_format=address_format,
            max_block_length=self.block_data.value() + 2, block_length_bytes=self.length_bytes.value(),
            full_blocks=self.full_blocks.isChecked(), memory_ranges=memory_ranges,
            require_erase=self.require_erase.isChecked(), erase_routine=self.erase_routine.value(),
            check_routine=self.check_routine.value(), erase_seconds=self.erase_seconds.value(),
            self_test_routine=self.self_test_routine.value(), self_test_seconds=self.self_test_seconds.value(),
            allow_upload=self.allow_upload.isChecked(), image_crc=self.image_crc.currentData(),
            version_address=self.version_address.value() if self.version_from_image.isChecked() else None,
            version_length=self.version_length.value(),
            broadcast_interval=self.broadcast_interval.value() / 1000 if self.broadcast.isChecked() else 0,
            dbc_path=self._dbc_path, messages=messages, generators=generators,
            dump_path=self.dump_path.text().strip() or None,
            dids=dids, dtcs=dtcs, forced_nrcs=forced_nrcs,
            confirm_cycles=self.confirm_cycles.value(), aging_cycles=self.aging_cycles.value(),
            operation_cycle_seconds=self.operation_cycle.value(), snapshot_dids=snapshot_dids,
            error_refuse_nrc=self.error_refuse_nrc.value(),
            errors_on_tester_present=self.errors_on_tester_present.isChecked(),
            j1939=self.j1939.isChecked(), j1939_address=self.j1939_address.value(), j1939_name=j1939_name,
            **{name: spin.value() for name, spin in self.error_spins.items()},
        )

    def _apply(self, *_):
        """Copy the widgets into the running ECU's settings (they apply to the next frame)."""
        if self._loading:
            return
        try:
            config = self._read_config()
        except ValueError as exc:
            self.statusBar().showMessage(f"Not applied: {exc}")
            return
        data_changed = any(getattr(config, name) != getattr(self.ecu.config, name) for name in ("dids", "dtcs"))
        for item in fields(EcuConfig):
            setattr(self.ecu.config, item.name, getattr(config, item.name))
        problem = self.ecu.refresh(data=data_changed)
        if data_changed:                              # the fault memory starts again: no fault present
            self._refresh_dtc_status()
        if problem:
            self.statusBar().showMessage(f"Not applied: {problem}")
        else:
            self.statusBar().clearMessage()
        self._update_labels()

    def _update_labels(self):
        config = self.ecu.config
        self.address_byte.setEnabled(self.use_address_byte.isChecked())
        self.padding.setEnabled(self.use_padding.isChecked())
        self.broadcast_interval.setEnabled(self.broadcast.isChecked())
        self.wait_interval.setEnabled(config.flow_waits > 0)
        self.periodic_id.setEnabled(self.periodic_own_id.isChecked())
        self.version_address.setEnabled(self.version_from_image.isChecked())
        self.version_length.setEnabled(self.version_from_image.isChecked())
        self.error_refuse_nrc.setEnabled(config.error_refuse > 0)
        self.error_nrc_text.setText(NRC_NAMES.get(config.error_refuse_nrc, "unknown NRC"))
        obd = (not config.extended_ids and config.functional_id == 0x7DF and 0x7E8 <= config.response_id <= 0x7EF
               and config.request_id == config.response_id - 8)
        self.can_expert_hint.setText(
            f"In CAN Expert's configuration: SERVER ID {config.request_id:X} and ECU ID {config.response_id:X}."
            + (" SERVER ID 7DF works too: CAN Expert then sends UDS requests to ECU ID - 8." if obd else ""))
        waits = "31 00 00 " * config.flow_waits
        frame = flow_control_frame(config.block_size, config.st_min).hex(" ").upper()
        per_block = f"{config.block_size} frames per block" if config.block_size else "the whole message at once"
        self.flow_preview.setText(f"Sent after each first frame{' and each block' if config.block_size else ''}: "
                                  f"{waits}{frame} ({per_block}, at least {stmin_text(config.st_min)} apart). "
                                  f"Receive buffer: {config.receive_buffer} bytes.")
        self.timing_hint.setText(f"Announced in the DiagnosticSessionControl response: 50 xx "
                                 f"{config.p2_ms.to_bytes(2, 'big').hex(' ').upper()} "
                                 f"{(config.p2_star_ms // 10).to_bytes(2, 'big').hex(' ').upper()} "
                                 f"(P2 in ms, P2* in 10 ms units).")
        room = self.ecu._periodic_room()
        self.periodic_hint.setText(
            (f"Frames of ID {config.periodic_id:X}: the periodic identifier, then up to {room} data bytes (ISO "
             f"15765-3 type 2)." if config.periodic_id is not None else
             f"6A frames on the response ID: 04 6A 01 00 D7 for F201, up to {room} data bytes (ISO 15765-3 type 1)."))
        level = config.security_level
        key = "the key its DLL computes" if config.key_dll else f"key = each seed byte XOR {config.key_mask:02X}"
        self.security_hint.setText(f"requestSeed 27 {level:02X}, sendKey 27 {level + 1:02X}; {key}. The example "
                                   f"scripts' compute_key() uses A5.")
        maximum = config.max_block_length
        length = max(config.block_length_bytes, (maximum.bit_length() + 7) // 8)
        response = bytes([0x74, length << 4]) + maximum.to_bytes(length, "big")
        self.block_hint.setText(f"maxNumberOfBlockLength = {maximum} (0x{maximum:X}): the data plus the 0x36 SID and "
                                f"the block counter. Response: {response.hex(' ').upper()}. CAN Expert's Flashing() "
                                f"then sends TransferData blocks of {maximum - 2} bytes.")

    # --- the database of the application frames -----------------------------------------------------

    def choose_dbc(self, path: str) -> bool:
        """Send the messages of another DBC ("" = the built-in one): the signal tables start again."""
        engine = SignalSimulation()
        try:
            engine.load(path)
            engine.configure(KNOWN_GENERATORS)       # the built-in and the J1939 demo DBC's; others: none
        except ValueError as exc:
            QMessageBox.warning(self, "Dummy ECU", str(exc))
            return False
        self._dbc_path = path
        self.dbc_label.setText(path or BUILTIN_DBC_TEXT)
        self._fill_signal_tables(engine)
        self._apply()
        return True

    def _browse_dbc(self):
        path, _ = QFileDialog.getOpenFileName(self, "Messages the ECU sends", str(DBC_DIR),
                                              "CAN database (*.dbc *.arxml *.kcd *.sym);;All files (*.*)")
        if path:
            self.choose_dbc(path)

    def _browse_dll(self, line):
        path, _ = QFileDialog.getOpenFileName(self, "Seed & key DLL", "", "DLL (*.dll);;All files (*.*)")
        if path:
            line.setText(path)

    # --- connection ----------------------------------------------------------------------

    def _channel(self):
        text = self.channel.currentText().strip()
        if not text:
            raise ValueError("Choose a channel")
        return parse_channel(text.split()[0])

    def _connection(self) -> dict:
        try:
            bitrate = int(self.bitrate.currentText())
        except ValueError:
            bitrate = DEFAULT_CONNECTION["bitrate"]
        text = self.channel.currentText().strip()
        return {"interface": self.interface.currentText().strip(), "bitrate": bitrate,
                "channel": text.split()[0] if text else DEFAULT_CONNECTION["channel"]}

    def detect_channels(self):
        if self._thread:
            return
        interface = self.interface.currentText().strip()
        current = self.channel.currentText()
        self.channel.clear()
        try:
            configs = can.detect_available_configs(interfaces=[interface], timeout=2.0)
        except Exception as exc:
            configs = []
            self._log(f"{interface} channel detection: {exc}")
        for cfg in configs:
            name = cfg.get("device_name") or cfg.get("description") or ""
            self.channel.addItem(f"{cfg.get('channel', '')}  {name}".strip())
        token = current.split()[0] if current.strip() else ""
        matching = [index for index in range(self.channel.count()) if self.channel.itemText(index).split()[0] == token]
        if matching:
            self.channel.setCurrentIndex(matching[0])
        else:
            self.channel.setEditText(current)

    def toggle_connection(self):
        if self._thread:
            self.disconnect_ecu()
        else:
            self.connect_ecu()

    def connect_ecu(self) -> bool:
        try:
            channel, bitrate = self._channel(), int(self.bitrate.currentText())
        except ValueError as exc:
            QMessageBox.warning(self, "Dummy ECU", str(exc) if "channel" in str(exc) else "Enter a numeric bit rate.")
            return False
        interface = self.interface.currentText().strip()
        lock = claim_channel(interface, channel, self.ecu.config.request_id)
        if lock is None:
            QMessageBox.warning(self, "Dummy ECU", f"Another dummy ECU already answers requests to "
                                f"0x{self.ecu.config.request_id:X} on {interface} channel {channel}. Close it, or "
                                "give this one other identifiers (Addressing): two ECUs answering the same "
                                "requests break security access and flashing.")
            return False
        try:
            bus = can.Bus(interface=interface, channel=channel, bitrate=bitrate)
        except Exception as exc:
            lock.close()
            QMessageBox.warning(self, "Dummy ECU", f"Cannot open {interface} channel {channel}:\n{exc}")
            return False
        if other_ecu_present(bus, self.ecu.config) and QMessageBox.question(
                self, "Dummy ECU", f"Another ECU already answers on {interface} channel {channel} with "
                f"0x{self.ecu.config.response_id:X}, or sends the application frames this one would. Two ECUs "
                f"answering the same requests break security access and flashing; a second ECU on the channel "
                f"needs its own identifiers and its application frames off.\n\nConnect anyway?") != QMessageBox.Yes:
            bus.shutdown()
            lock.close()
            return False
        self._bus, self._lock, self._stop = bus, lock, threading.Event()
        self.ecu.bus = bus
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._set_connected(True)
        config = self.ecu.config
        self._log(f"Connected to {interface} channel {channel} at {bitrate} bit/s: requests 0x{config.request_id:X} "
                  f"(functional 0x{config.functional_id:X}), responses 0x{config.response_id:X}")
        return True

    def _serve(self):
        try:
            self.ecu.serve(self._stop)
        except Exception as exc:  # e.g. the adapter went away; the status timer then disconnects
            self._log(f"Stopped: {exc}")

    def disconnect_ecu(self):
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(2)
        self._bus.shutdown()
        self._lock.close()
        self.ecu.bus = None
        self._bus = self._lock = self._stop = self._thread = None
        self._set_connected(False)
        self._log("Disconnected")

    def _set_connected(self, connected: bool):
        for widget in (self.interface, self.channel, self.bitrate, self.detect_button):
            widget.setEnabled(not connected)
        self.connect_button.setText("Disconnect" if connected else "Connect")
        if connected:
            connection = self._connection()
            text = f"Connected: {connection['interface']} channel {connection['channel']}"
        else:
            text = "Disconnected"
        colour = "#15803d" if connected else "#9ca3af"
        self.connection_state.setText(f'<span style="color:{colour}">●</span> {text}')

    # --- status and log ------------------------------------------------------------------------

    def _refresh_status(self):
        if self._thread and not self._thread.is_alive():
            self.disconnect_ecu()
        ecu = self.ecu
        state, now = ecu.state, time.monotonic()
        labels = self.status_labels
        labels["Session"].setText(SESSION_NAMES.get(state.session, f"0x{state.session:02X}"))
        if now < state.locked_until:
            labels["Security"].setText(f"locked out for {state.locked_until - now:.0f} s")
        elif state.unlocked_levels:
            levels = ", ".join(f"{level:02X}" for level in sorted(state.unlocked_levels))
            labels["Security"].setText(f"unlocked (level {levels})")
        else:
            labels["Security"].setText("locked")
        if state.bootloader:
            labels["Application"].setText("not valid: the bootloader runs")
        else:
            labels["Application"].setText("valid" if state.application_valid else
                                          "not valid until checkProgrammingDependencies passes")
        labels["DTC setting"].setText("on" if state.dtc_setting_on else "off (ControlDTCSetting)")
        labels["Normal messages"].setText("on" if state.communication_enabled else "off (CommunicationControl)")
        transfer = state.transfer
        if transfer:
            labels["Transfer"].setText(f"{transfer['direction'].capitalize()} at 0x{transfer['address']:08X}: "
                                       f"{transfer['done']} / {transfer['size']} bytes")
        else:
            labels["Transfer"].setText("none")
        memory = state.memory
        size = sum(len(data) for data in memory.values())
        labels["Memory"].setText(f"{size} bytes downloaded in {len(memory)} segment(s)" if memory else "erased")
        version = ecu.dids.get(0xF195, b"") if not state.bootloader else b"BOOTLOADER"
        labels["Software version"].setText(version.decode("latin-1"))
        periodic = sorted(state.periodic.items())
        labels["Periodic data"].setText(", ".join(f"F2{identifier:02X} {PERIODIC_MODES[entry['mode']]}"
                                                  for identifier, entry in periodic) or "none")
        if state.events:
            kinds = ", ".join("DTC status" if event["type"] == 0x01 else f"DID {event['record'].hex().upper()}"
                              for event in state.events)
            labels["Events"].setText(f"{kinds}: {'active' if state.events_active else 'set up, not started'}")
        else:
            labels["Events"].setText("none")
        controls = []
        for did in sorted(state.io_controls):
            try:
                controls.append(f"{did:04X} = {ecu._did_value(did).hex(' ').upper()}")
            except Exception:
                controls.append(f"{did:04X}")
        labels["I/O control"].setText(", ".join(controls) or "none")
        labels["Operation cycle"].setText(str(ecu.dtc_memory.cycle))
        if self.tabs.currentWidget() is self.data_tab:
            self._refresh_dtc_status()
        if self.tabs.currentWidget() is self.signals_tab:
            self._refresh_signal_values()


    def _log(self, text):
        """Called from any thread; the text appears with the next timer tick."""
        self._lines.append(f"{_stamp()}  {text}")

    def _trace(self, direction, message):
        can_id = f"{message.arbitration_id:08X}" if message.is_extended_id else f"{message.arbitration_id:03X}"
        self._lines.append(f"{_stamp()}  {direction}  {can_id}  {bytes(message.data).hex(' ').upper()}")

    def _show_frames(self, enabled):
        self.ecu.trace = self._trace if enabled else None

    def _flush_log(self):
        batch = []
        while self._lines and len(batch) < 2000:
            batch.append(self._lines.popleft())
        if batch:
            self.log_view.appendPlainText("\n".join(batch))

    # --- actions ---------------------------------------------------------------------------------

    def reset_ecu(self):
        self.ecu.power_on()
        self._refresh_dtc_status()
        self._log("ECU reset to its factory state (default session, locked, original DIDs and DTCs, erased memory, "
                  "a valid application)")

    def new_operation_cycle(self):
        self.ecu.new_operation_cycle()
        self._refresh_dtc_status()

    def save_memory(self):
        if not self.ecu.state.memory:
            QMessageBox.information(self, "Dummy ECU", "Nothing has been downloaded yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save the ECU memory", "flashed.s19", "S-record (*.s19 *.s37)")
        if path:
            self.ecu.write_image(path)

    def _browse_dump(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save the flashed image to", self.dump_path.text() or "flashed.s19",
                                              "S-record (*.s19 *.s37)")
        if path:
            self.dump_path.setText(path)

    def load_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load a Dummy ECU profile", "", "Dummy ECU profile (*.json)")
        if not path:
            return
        try:
            config, connection = load_profile(path)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, "Dummy ECU", f"Cannot load {path}:\n{exc}")
            return
        self._fill(config, None if self._thread else connection)
        self._apply()
        self._log(f"Profile loaded: {path}")

    def save_profile(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save the Dummy ECU profile", "dummy_ecu_profile.json",
                                              "Dummy ECU profile (*.json)")
        if path:
            save_profile(path, self.ecu.config, self._connection())
            self._log(f"Profile saved: {path}")

    def restore_defaults(self):
        self._fill(EcuConfig(), None if self._thread else DEFAULT_CONNECTION)
        self._apply()

    def closeEvent(self, event):
        self.disconnect_ecu()
        app_settings().setValue(SETTINGS_KEY, json.dumps({"connection": self._connection(),
                                                          "ecu": asdict(self.ecu.config)}))
        super().closeEvent(event)


def run_window(config: EcuConfig | None = None, overrides: dict | None = None, connection: dict | None = None,
               smoke_test: bool = False) -> int:
    """Open the window with the last settings, a profile (config) and command-line overrides on top.
    smoke_test: only build the window, as a check that everything it needs is there."""
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(app_icon("dummy_ecu"))
    saved_config, saved_connection = saved_profile()
    window = DummyEcuWindow(replace(config or saved_config, **(overrides or {})),
                            {**saved_connection, **(connection or {})})
    if smoke_test:
        print("startup ok")
        return 0
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(run_window())
