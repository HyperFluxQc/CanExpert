"""
The main window's measurement: Connect (the database, the adapter, the CAN worker and the panel script) and
Disconnect, and the one path every frame takes - into the history, the recording, the panel, the script, the
status strip and every open tool window. What a check of the panel finds is shown at Connect: why it failed,
when the panel is the reason, else the panel's problems, once for each version of its files.
"""
import time
from datetime import date
from pathlib import Path

import can
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QInputDialog, QMessageBox

from canexpert.can_bus import CanWorker, ReceiveMailbox, SwitchedBus, channel_key
from canexpert.channel_setup import load_setup, open_configured
from canexpert.config import uds_transport, validate_config
from canexpert.j1939_window import address_setting
from canexpert.panel.check import ERROR, FORM, Problem, check_panel_file, script_file, summary
from canexpert.panel.database import parse_application_database, select_database, split_database_id
from canexpert.panel.problems_dialog import ProblemsDialog
from canexpert.panel.runtime import ScriptRuntime
from canexpert.transport_settings import apply_transport, load_transport
from canexpert.status_strip import DiagnosticState
from canexpert.workspace import fit_on_screen

MARKER_HISTORY = 1000              # markers kept for a window opened later
OFFLINE_SETTING = "offline"        # settings: the kill switch was on - CAN Expert starts off the bus
SCRIPT_LOAD_WAIT = 3.0             # seconds a refreshed database's script is given to define its functions


def database_version(path) -> tuple:
    """What tells one version of a panel database from another: the file, and when it and its script were
    last written."""
    path = Path(path).resolve()
    stamps = []
    for item in (path, script_file(path)):
        try:
            stamps.append(item.stat().st_mtime_ns)
        except OSError:                             # no script, or the file gone
            stamps.append(None)
    return (str(path), *stamps)


def database_rank(path) -> tuple:
    """Where a database file stands among the versions of its family, as select_database() orders them: its
    date (an undated one before all), then its name."""
    path = Path(path)
    return split_database_id(path.stem)[1] or date.min, path.name


class Session:
    """Connect, Disconnect and the frames of the measurement, for MainWindow (main_window.py)."""

    def on_connect_clicked(self):
        """Connect: load the active configuration's panel database and run its script on the bus."""
        if self.can_bus is not None or self.flash_dialog is not None:   # a Flashing over the ECU check runs
            return
        if self.offline:
            self._set_status("Off the bus: release Kill CAN (Ctrl+F9) to connect", "red")
            return
        if not self.active_config or not self.selected_channel_config:
            QMessageBox.warning(self, "Connection", "Select a configuration and a CAN receiver first.")
            return
        self.stop_ecu_monitor()  # the session sends TesterPresent itself
        database_path, panel_failure = None, False     # panel_failure: a failure now would be the panel's
        try:
            config = self.session_configuration()
            database_path = select_database(self.databases_dir, config["database_family"])
            if database_path is None:
                raise ValueError("No matching database. Create a panel in Form Designer first.")
            panel_failure = True
            database = parse_application_database(database_path)
            # Validate/build before opening hardware, so errors leave a usable UI.
            self.build_application_ui(database)
            panel_failure = False
            cfg = self.selected_channel_config
            setup = load_setup(self._settings, cfg)
            # Behind the kill switch: Kill CAN closes the adapter and opens it again, the session going on.
            self.can_bus = SwitchedBus(open_configured(cfg, config["bitrate"], setup, config))
            self._reopen_bus = lambda c=dict(cfg), b=config["bitrate"], s=setup, f=config: open_configured(c, b, s, f)
            if setup.describe():
                self.log_verbose(f"Channel setup: {setup.describe()}")
            self.session_config = config
            transport = uds_transport(config)
            self._diagnostic_answers = (transport["response_id"], transport["extended"], transport["address_byte"])
            self.connected_channel_config = dict(cfg)
            self._remember_channel(cfg)                 # the chosen interface, in bold
            self._forget_nodes(keep=channel_key(cfg))   # what other interfaces showed goes
            self.session_generation += 1
            generation = self.session_generation
            self.clock.begin(time.time())            # a new measurement: relative times count from here
            worker = CanWorker(self.can_bus, config, tester_present=not setup.listen_only,
                               unsolicited=not setup.listen_only)
            worker.message_received.connect(lambda msg, g=generation: self.on_can_message(msg) if g == self.session_generation else None)
            worker.message_sent.connect(lambda stamp, cid, data, extended, g=generation: self.dispatch_frame(stamp, "TX", cid, data, extended) if g == self.session_generation else None)
            worker.error_occurred.connect(lambda error, g=generation: self._session_failed(error) if g == self.session_generation else None)
            worker.error_frame.connect(lambda ts, g=generation: self._on_error_frame(ts) if g == self.session_generation else None)
            worker.bus_status.connect(lambda status, g=generation: self._on_bus_status(status) if g == self.session_generation else None)
            worker.unsolicited.connect(lambda ts, payload, g=generation: self._on_unsolicited(ts, payload) if g == self.session_generation else None)
            self.worker = worker
            if self.sysvars is not None:
                self.sysvars.reset()               # every variable back to its initial value
            self._bus_state = None
            self._watch_keys(True)
            worker.start()
            self._set_flashing_available(False)    # until the script says whether it has a Flashing()
            self._set_database_functions(frozenset())
            panel_failure = True
            self._start_script(database)
            panel_failure = False
            self._database_version = database_version(database_path)
            self._toolbar_actions["connect"].setEnabled(False)
            self._toolbar_actions["disconnect"].setEnabled(True)
            self.status_strip.connected()
            self.config_list.setEnabled(False)
            self.edit_config_btn.setEnabled(False)          # the configuration in use stays as it is
            self.database_pane.toggleView(True)
            self.database_pane.setAsCurrentTab()
            fit_on_screen(self.database_pane)
            self.channels_dock.show()
            self._minimize_side_panels()
            self.refresh_channel_list()
            self._update_diagnostic_ids()
            self._set_status(f"Connected — {Path(database['source_path']).name}", "green")
            self.log_verbose(f"Loaded {database['source_path']}")
            self.report_panel_problems(database_path)
        except Exception as exc:
            self.on_disconnect_clicked()
            self._set_status(f"Connection failed: {exc}", "red")
            self.log_verbose(str(exc))
            if panel_failure:
                self.report_panel_problems(database_path, failure=exc)

    def _start_script(self, database):
        """The database's script on the session, through a mailbox of its own on the CAN worker; the panel is
        built already. What it says reaches the window while it is the one running: one stopped - Disconnect,
        or a refreshed database in its place - says nothing more."""
        mailbox = ReceiveMailbox(self.can_bus, self.worker.message_sent.emit)
        self.worker.add_mailbox(mailbox)
        runtime = ScriptRuntime(mailbox, self.session_config, self.panel.values(), self, sysvars=self.sysvars)
        runtime.mailbox = mailbox
        runtime.no_tester_present = self.worker.no_tester_present     # while its Flashing() runs
        runtime.j1939.address = address_setting(self._settings)        # the J1939 window's

        def running(slot):
            return lambda *arguments: slot(*arguments) if runtime is self.script_runtime else None

        runtime.value_changed.connect(running(lambda name, value: self.panel.set_value(name, value)
                                              if self.panel else None))
        runtime.message.connect(running(self.write_message))
        runtime.flashing_available.connect(running(self._set_flashing_available))
        runtime.functions_available.connect(running(self._set_database_functions))
        runtime.flash_progress.connect(running(self._on_flash_progress))
        runtime.flash_finished.connect(running(self._on_flash_finished))
        runtime.function_finished.connect(running(self._on_function_finished))
        runtime.marker_requested.connect(running(self.add_marker))     # api.marker()
        runtime.dbc = self.panel.dbc
        runtime.handlers = self.panel.handlers()
        runtime.set_variables(database.get("variables", []))
        self.script_runtime = runtime
        runtime.start(script_file(database["source_path"]))
        return runtime

    def _stop_script(self):
        """The running script stopped - its @on_stop handlers first, while the bus is still there - and its
        mailbox taken off the worker. A Read or Write it was running is over."""
        runtime, self.script_runtime = self.script_runtime, None
        self._function_running = None
        if runtime is None:
            return
        runtime.stop()
        mailbox = getattr(runtime, "mailbox", None)
        if mailbox is not None:
            if self.worker is not None:
                self.worker.remove_mailbox(mailbox)
            mailbox.close()

    def _panel_input(self, name, value):
        """A control of the panel changed by the user: to the script running now."""
        if self.script_runtime is not None:
            self.script_runtime.post("control", name, value)

    def report_panel_problems(self, path, failure=None):
        """What a check of the panel database and its script finds (check.py), in the Panel check window and
        the log. After a Connect the panel stopped: why, in full. After one that worked: its problems - once for
        each version of the files, not at every Connect."""
        path = Path(path)
        problems = check_panel_file(path, dbc=self.panel.dbc if self.panel is not None else None)
        if failure is not None:
            if not any(problem.is_error for problem in problems):
                problems.insert(0, Problem(ERROR, str(failure), FORM, str(path)))
            heading = f"The panel {path.name} cannot be loaded"
            note = ("Connect stopped here. Double-click a problem to open the panel in the Form Designer at that "
                    "place; put it right, save, and connect again.")
        else:
            version = database_version(path)
            if not problems or version in self._panels_reported:
                return None
            self._panels_reported.add(version)
            heading = f"The panel {path.name} has {summary(problems)}"
            note = ("It is running; what is listed will not work as written. Double-click a problem to open the "
                    "panel in the Form Designer at that place.")
        for problem in problems:
            self.log_verbose(problem.text())
        if self.problems_dialog is None:
            self.problems_dialog = ProblemsDialog(self, "Open in Form Designer")
            self.problems_dialog.go_to.connect(self.open_panel_problem)
        self._problems_path = path
        return self.problems_dialog.show_problems(problems, heading, note)

    def open_panel_problem(self, problem):
        """A problem of the panel database, in the Form Designer: the panel opened there, at that place."""
        designer = self.open_form_designer()
        path = self._problems_path.resolve()
        if (designer.database_dir.resolve(), designer._loaded_id) != (path.parent, path.stem) and \
                not designer.load(path):
            return
        designer.go_to_problem(problem)

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

    def set_offline(self, offline: bool):
        """The kill switch (Kill CAN, Ctrl+F9). On: CAN Expert off the bus at once, the database staying loaded -
        the session's adapter is closed (SwitchedBus), so nothing is sent or received: no TesterPresent, the
        Transmit window's messages and nodes paused, the script's frames and requests refused while it goes on
        running; a reflash and any ECU scan stop, and so does the ECU check. Nothing opens an adapter
        until the switch is released: not Connect, not the ECU check, not a scan. Kept in the settings, so CAN
        Expert starts off the bus if it was left so. Off: the session's adapter is opened again and all of it goes
        on where it was; without a session, the ECU check starts again as at startup."""
        from canexpert.ecu_scan import EcuScanDialog
        offline = bool(offline)
        action = self._toolbar_actions["kill"]
        if action.isChecked() != offline:
            action.setChecked(offline)                   # the button follows; it calls back here
            return
        self.offline = offline
        self._settings.setValue(OFFLINE_SETTING, offline)
        transmit = self.tool_widget("transmit")
        if transmit is not None:
            transmit.pause_sending(offline)
        if offline:
            for scan in self.findChildren(EcuScanDialog):
                scan.halt("CAN Expert went off the bus (Kill CAN)")
            self._stop_bus_work()
            self.stop_ecu_monitor()
            if self.can_bus is not None:
                self.can_bus.cut_off()
                self.status_strip.set_bus("off the bus")
            self._toolbar_actions["connect"].setEnabled(False)
            kept = " The database stays loaded." if self.can_bus is not None else ""
            self.log_verbose(f"Kill CAN: off the bus - nothing is sent or received until it is released.{kept}")
            self._set_status(f"Off the bus: nothing is sent or received{' - the database stays loaded' if kept else ''}"
                             " (Kill CAN, Ctrl+F9, to go back on)", "red")
        elif self.can_bus is not None:
            try:
                self.can_bus.restore(self._reopen_bus())
            except Exception as exc:                     # the adapter is gone, or busy elsewhere
                self.on_disconnect_clicked()
                self.log_verbose(f"Kill CAN released, but the adapter could not be opened again: {exc}")
                self._set_status(f"Back on the bus failed: {exc} - the session is closed", "red")
            else:
                if self.script_runtime is not None:
                    self.script_runtime.bus_restored()
                self.log_verbose("Kill CAN released: back on the bus, the session going on")
                self._set_status(f"Back on the bus — {Path(self.app_database['source_path']).name}"
                                 if self.app_database else "Back on the bus", "green")
            self._toolbar_actions["connect"].setEnabled(self.can_bus is None)
        else:
            self._toolbar_actions["connect"].setEnabled(True)
            self.log_verbose("Kill CAN released: back on the bus")
            self._set_status("Back on the bus", "gray")
            self.check_last_channel()
        self._label_channels()
        self._update_nodes()

    def _stop_bus_work(self):
        """What cannot go on off the bus: a reflash (cancelled)."""
        if self.flash_runner is not None:
            self.flash_runner.cancel()
        if self.script_runtime is not None and self.flash_dialog is not None:
            self.script_runtime.cancel_flash()

    def on_disconnect_clicked(self):
        self.session_generation += 1
        self._watch_keys(False)
        if self.flash_runner is not None:
            self.flash_runner.cancel()      # the bus is about to go away under it
        self._close_flash_dialog()
        self._stop_script()             # runs @on_stop handlers, then revokes the bus
        self._ecu_seen = False          # Read, Write and Reflash go with the database
        self._database_version = None
        self._set_database_functions(frozenset())
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
        self._toolbar_actions["connect"].setEnabled(not self.offline)
        self._toolbar_actions["disconnect"].setEnabled(False)
        self.status_strip.disconnected()
        self.config_list.setEnabled(True)
        self.edit_config_btn.setEnabled(True)
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

    def refresh_database(self) -> bool:
        """Before Read, Write and Reflash: the database Connect would load now takes the place of the one loaded
        when it is newer - a later one of the family, or the loaded one written since, its panel or its script.
        The session goes on: the adapter, the CAN worker and TesterPresent; the panel is built again and the new
        script started, the old one's @on_stop handlers run first. Nothing restarts when nothing is newer.
        False when the newer one cannot be loaded - the one loaded stays, and the Panel check window says why -
        or the session is gone."""
        loaded = self.app_database
        if loaded is None or self.active_session() is None:
            return False
        try:
            path = select_database(self.databases_dir, self.session_config["database_family"])
        except (OSError, ValueError) as exc:
            self.log_verbose(f"Database selection: {exc}")
            path = None
        if path is None or database_version(path) == self._database_version:
            return True                                 # nothing newer: the one loaded is the one to use
        if path.resolve() != Path(loaded["source_path"]) and \
                database_rank(path) <= database_rank(loaded["source_path"]):
            return True                                 # the one loaded is gone, and those left are older
        name, loaded_name = path.name, Path(loaded["source_path"]).name
        try:
            database = parse_application_database(path)
            script = script_file(path)
            if script.exists():                         # a script that does not compile stays out
                compile(script.read_text(encoding="utf-8-sig"), str(script), "exec")
            self.build_application_ui(database)
        except Exception as exc:
            try:
                if self.app_database is not loaded:     # the panel was taken down: the loaded one comes back
                    self.build_application_ui(loaded)
            except Exception as again:
                self._session_failed(f"{loaded_name} could not be shown again: {again}")
                return False
            self.log_verbose(f"{name} cannot be loaded: {exc}")
            self._set_status(f"{name} cannot be loaded - {loaded_name} stays: {exc}", "red")
            self.report_panel_problems(path, failure=exc)
            return False
        version = database_version(path)
        self._stop_script()
        try:
            runtime = self._start_script(database)
        except Exception as exc:                        # unreadable since it was compiled: as at Connect
            self._session_failed(f"Connection failed: {exc}")
            self.report_panel_problems(path, failure=exc)
            return False
        self._database_version = version
        runtime.loaded.wait(SCRIPT_LOAD_WAIT)           # Flashing(), Read() and Write(), known before they are used
        self._set_flashing_available(runtime.flash_function is not None)
        self._set_database_functions(runtime.functions())
        self.log_verbose(f"Database refreshed: {path} in place of {loaded_name}" if name != loaded_name else
                         f"Database refreshed: {name} has changed since it was loaded")
        self._set_status(f"Connected — {name} (refreshed)", "green")
        self.report_panel_problems(path)
        self._update_nodes()
        return True

    def database_function(self, name) -> bool:
        """Read or Write on the toolbar: the database refreshed (refresh_database), then its script's Read(api) or
        Write(api) run on the script's thread. How it ends is said in the status bar, the Write window and, when
        it failed, the Log."""
        if self.active_session() is None or self._function_running or self.flash_dialog is not None:
            return False
        if not self.refresh_database() or self.script_runtime is None:
            return False
        if not self.script_runtime.call_function(name):
            self._on_function_finished(name, False, f"{name} could not start: the database script is not running")
            return False
        self._function_running = name
        self._set_status(f"{name} running...", "orange")
        self._update_database_buttons()
        return True

    def _on_function_finished(self, name, ok, text):
        self._function_running = None
        self.write_message("info" if ok else "error", text)     # an error goes to the Log as well
        if ok:
            self.log_verbose(text)
        database = Path(self.app_database["source_path"]).name if self.app_database else ""
        self._set_status(f"{text} — {database}" if database else text, "green" if ok else "red")  # which one ran it
        self._update_database_buttons()

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
        """Send a frame on the session's bus - from any thread: the Transmit window's cyclic rows are sent by a
        thread of their own. The windows see the frame from the window's thread (frame_sent)."""
        bus = self.can_bus
        if bus is None:
            raise RuntimeError("Connect before sending CAN messages")
        if extended is None:
            extended = not (self.session_config or {}).get("identifier_11_bit", True)
        payload = bytes(data)
        if len(payload) > 8:
            raise ValueError("Classic CAN messages cannot exceed eight bytes")
        message = can.Message(arbitration_id=can_id, data=payload, is_extended_id=extended, check=True)
        with self.send_lock:
            bus.send(message)
        self.frame_sent.emit(time.time(), can_id, payload, bool(extended))

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

    def insert_marker(self, text=None):
        """Ctrl+M: a marker at this moment of the measurement, with a comment asked for (None: cancelled)."""
        when = time.time()                          # the moment the key was pressed, not the dialog's end
        if text is None:
            text, ok = QInputDialog.getText(self, "Insert marker", "Comment:",
                                            text=f"Marker {self._marker_count + 1}")
            if not ok:
                return None
        return self.add_marker(when, text)

    def quick_marker(self):
        """Ctrl+Shift+M: a numbered marker at once."""
        return self.add_marker(time.time(), "")

    def add_marker(self, timestamp, text):
        """A marker - from the keys, the panel script's api.marker() or a test module's t.marker() - in the
        Trace and on the Logger's graphs (also those opened later), in the recording where its format holds
        one, and in the Log."""
        self._marker_count += 1
        text = " ".join(str(text).split()) or f"Marker {self._marker_count}"
        marker = (float(timestamp), text)
        self.marker_history.append(marker)
        if self.recorder is not None:
            try:
                self.recorder.write_marker(*marker)
            except Exception as exc:                  # a full disk must not take the measurement down
                self.log_verbose(f"The marker was not recorded: {exc}")
        for name in list(self.tool_panes):
            handler = getattr(self.tool_widget(name), "on_marker", None)
            if handler is not None:
                handler(*marker)
        self.log_verbose(f"Marker at {self.clock.text(timestamp, self.time_display)}: {text}")
        return marker

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
            changed = self.panel.changed_values() if self.panel else {}
            if changed:                           # what the frame changed on the panel, for api.ui.get_value()
                with self.script_runtime.lock:
                    self.script_runtime.values.update(changed)
            self.script_runtime.post("can", can_id, data)
