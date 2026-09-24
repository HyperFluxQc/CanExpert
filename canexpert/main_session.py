"""
The main window's measurement: Connect (the database, the adapter, the CAN worker and the panel script) and
Disconnect, and the one path every frame takes - into the history, the recording, the panel, the script, the
status strip and every open tool window.
"""
import time
from pathlib import Path

import can
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QMessageBox

from canexpert.can_bus import CanWorker, ReceiveMailbox, channel_key
from canexpert.channel_setup import load_setup, open_configured
from canexpert.config import uds_transport, validate_config
from canexpert.panel.database import load_application_database
from canexpert.panel.runtime import ScriptRuntime
from canexpert.transport_settings import apply_transport, load_transport
from canexpert.status_strip import DiagnosticState
from canexpert.workspace import fit_on_screen


class Session:
    """Connect, Disconnect and the frames of the measurement, for MainWindow (main_window.py)."""

    def on_connect_clicked(self):
        """Connect: load the active configuration's panel database and run its script on the bus."""
        if self.can_bus is not None:
            return
        if not self.active_config or not self.selected_channel_config:
            QMessageBox.warning(self, "Connection", "Select a configuration and a CAN receiver first.")
            return
        self.stop_ecu_monitor()  # the session sends TesterPresent itself
        try:
            config = self.session_configuration()
            database = load_application_database(config["database_family"], self.databases_dir)
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
            transport = uds_transport(config)
            self._diagnostic_answers = (transport["response_id"], transport["extended"], transport["address_byte"])
            self.connected_channel_config = dict(cfg)
            self._remember_channel(cfg)
            self.session_generation += 1
            generation = self.session_generation
            self.clock.begin(time.time())            # a new measurement: relative times count from here
            worker = CanWorker(self.can_bus, config, tester_present=not setup.listen_only,
                               unsolicited=not setup.listen_only)
            mailbox = ReceiveMailbox(self.can_bus, worker.message_sent.emit)
            worker.add_mailbox(mailbox)
            worker.message_received.connect(lambda msg, g=generation: self.on_can_message(msg) if g == self.session_generation else None)
            worker.message_sent.connect(lambda cid, data, g=generation: self.dispatch_frame(time.time(), "TX", cid, data) if g == self.session_generation else None)
            worker.error_occurred.connect(lambda error, g=generation: self._session_failed(error) if g == self.session_generation else None)
            worker.error_frame.connect(lambda ts, g=generation: self._on_error_frame(ts) if g == self.session_generation else None)
            worker.bus_status.connect(lambda status, g=generation: self._on_bus_status(status) if g == self.session_generation else None)
            worker.unsolicited.connect(lambda ts, payload, g=generation: self._on_unsolicited(ts, payload) if g == self.session_generation else None)
            self.worker = worker
            if self.sysvars is not None:
                self.sysvars.reset()               # every variable back to its initial value
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
            self._toolbar_actions["connect"].setEnabled(False)
            self._toolbar_actions["disconnect"].setEnabled(True)
            self.status_strip.connected()
            self._set_flashing_available(False)
            self.flashing_toolbar_item.setVisible(True)
            self.config_list.setEnabled(False)
            self.database_pane.toggleView(True)
            self.database_pane.setAsCurrentTab()
            fit_on_screen(self.database_pane)
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
        if self.can_bus is None or self.worker is None or self.session_config is None:
            return None
        return self.can_bus, self.worker, self.session_config

    def _session_failed(self, error):
        self.log_verbose(error)
        self.status_strip.set_error(error)
        self.on_disconnect_clicked()
        self._set_status(error, "red")

    def on_disconnect_clicked(self):
        self.session_generation += 1
        self._watch_keys(False)
        if self.flash_runner is not None:
            self.flash_runner.cancel()      # the bus is about to go away under it
        tests = self.tool_widget("tests")
        if tests is not None:
            tests.stop()                    # the running test case ends; its mailboxes close with the worker
        self._close_flash_dialog()
        self.flashing_toolbar_item.setVisible(False)
        if self.script_runtime:
            self.script_runtime.stop()  # runs @on_stop handlers, then revokes the bus
            self.script_runtime = None
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
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
        self._toolbar_actions["connect"].setEnabled(True)
        self._toolbar_actions["disconnect"].setEnabled(False)
        self.status_strip.disconnected()
        self.config_list.setEnabled(True)
        self._restore_side_panels()
        self.database_pane.toggleView(False)
        self.channels_dock.show()
        self._set_status("Disconnected", "gray")
        self.clear_application_ui()
        self._update_nodes()

    def disconnect_database(self):
        """Toolbar Disconnect: close the database session, then keep checking its ECUs so the CAN Channels
        tree still shows which ones respond."""
        channel, config = self.connected_channel_config, self.session_config
        self.on_disconnect_clicked()
        if channel and config:
            self.start_ecu_monitor(channel, config)

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
        """One frame of the measurement, from wherever: the history, the recording, and every window.

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

    def _on_unsolicited(self, timestamp, payload):
        """A diagnostic response the ECU sent by itself: periodic data (0x2A) or an event's (0x86)."""
        if self.script_runtime is not None:
            self.script_runtime.post("unsolicited", None, bytes(payload))   # @on_periodic_data, @on_response_event
        console = self.tool_widget("console")
        if console is not None:
            console.on_unsolicited(timestamp, bytes(payload))

    def _on_bus_status(self, status):
        """The adapter's error state, read while the session runs."""
        statistics = self.tool_widget("statistics")
        if statistics is not None:
            statistics.on_bus_status(status)
        if status.get("state") == "bus off" and self._bus_state != "bus off":
            self.log_verbose("The adapter reports bus off: no frames are being sent or received")
            self._set_status("Bus off — check the wiring, the bit rate and the termination", "red")
            self.status_strip.set_error("Bus off")
        state = status.get("state", "unknown")
        if self.session_config is not None:
            self.status_strip.set_bus(state, status.get("error_frames", 0))
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
        response_id, extended, address_byte = self._diagnostic_answers
        if can_id == response_id and bool(msg_dict.get("is_extended_frame", False)) == extended:
            payload = DiagnosticState.payload(data, address_byte)       # session, security, NRCs
            if payload:
                self.status_strip.on_response(payload)
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
