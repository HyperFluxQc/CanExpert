"""
The main window's tool windows: each one's pane in the workspace, opened on demand and filled with what
happened before it was opened - the Trace, the CAN Logger, the Data, Statistics and Transmit windows, the UDS
Console, the Write window, the Test window, the system variables and the Form Designer - and the keys a panel
script hears.
"""
import time

from PyQt5.QtCore import QEvent
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
)

from canexpert.can_logger import CANLoggerWindow
from canexpert.config import uds_transport
from canexpert.data_window import DataWindow
from canexpert.designer.form_designer import FormDesigner
from canexpert.statistics_window import StatisticsWindow
from canexpert.symbols import SymbolDatabaseDialog
from canexpert.sysvars import SystemVariablesWindow
from canexpert.trace_window import TraceWindow
from canexpert.transmit_pane import TransmitPane
from canexpert.uds_console import UdsConsoleWindow
from canexpert.testing.window import TestWindow
from canexpert.workspace import fit_on_screen, set_content
from canexpert.write_window import WriteWindow


class ToolWindows:
    """The tool windows of MainWindow (main_window.py)."""

    def open_form_designer(self):
        """Open the Form Designer dialog."""
        designer = FormDesigner(self)
        designer.saved.connect(lambda p: self.load_configurations())
        designer.exec_()

    def tool_widget(self, name):
        """The widget of a tool window that was opened, else None (nothing is created here)."""
        pane = self.tool_panes.get(name)
        return pane.widget() if pane is not None else None

    def open_tool(self, name, title, factory):
        """Show a tool in the workspace, building it the first time. Returns (widget, is new)."""
        pane, created = self.tool_panes.get(name), False
        if pane is None:
            widget = factory()
            pane = self._tool_slots[name]
            pane.setWindowTitle(title)
            set_content(pane, widget)
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
        pane.toggleView(True)
        pane.setAsCurrentTab()
        fit_on_screen(pane)
        return pane.widget(), created

    def _toggle_tool(self, name, shown, show):
        """The toolbar switch of a tool window: open it, or close the one that is open."""
        if self._settling:
            return
        pane = self.tool_panes.get(name)
        if shown:
            show()
        elif pane is not None:
            pane.toggleView(False)   # hidden, not destroyed: reopening shows what it recorded meanwhile

    def write_message(self, level: str, text: str):
        """A line of the panel script's output. Errors go to the Debug log as well."""
        entry = (time.time(), level, text)
        self.write_history.append(entry)
        window = self.tool_widget("write")
        if window is not None:
            window.add(*entry)
        if level == "error":
            self.log_verbose(text)
            self.status_strip.set_error(text)

    def script_watch(self):
        """(the script's globals, the names CAN Expert put there), for the Write window's watch."""
        runtime = self.script_runtime
        return (runtime.namespace, runtime.hidden_names) if runtime is not None else ({}, ())

    def open_write(self):
        """Write window: the script's output and its variables."""
        window, created = self.open_tool("write", "Write", lambda: WriteWindow(self, self.clock, self.script_watch,
                                                             display=lambda: self.time_display))
        if created:
            for entry in list(self.write_history):
                window.add(*entry)
        return window

    def open_tests(self):
        """Test window: a test module's test cases, run against the measurement's bus."""
        def decode(can_id, data):
            return self.symbols.name(can_id), self.symbols.decode(can_id, data)
        window, _ = self.open_tool("tests", "Test", lambda: TestWindow(
            self, self.active_session, decode, self._settings,
            time_text=lambda t: self.clock.text(t, self.time_display)))
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
        trace, created = self.open_tool("trace", "Trace", lambda: TraceWindow(self, self.symbols, self.clock))
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

    def open_transmit(self, nodes=False):
        """Transmit window: messages once or cyclically, and the simulated nodes (nodes=True shows that tab)."""
        pane, created = self.open_tool("transmit", "Transmit",
                                       lambda: TransmitPane(self, self.symbols, self.send_can_message, self._settings))
        if created:
            # Closing the window stops what it sends; a tab of another window in front of it does not.
            self.tool_panes["transmit"].viewToggled.connect(lambda shown, p=pane: None if shown else p.stop_sending())
        if nodes:
            pane.show_nodes()
        return pane

    def open_uds_console(self):
        """UDS Console window: any ISO 14229 service, the services of an ODX file, and the fault memory."""
        widget, _ = self.open_tool("console", "UDS Console",
                                   lambda: UdsConsoleWindow(self, self.active_session,
                                                            time_text=lambda t: self.clock.text(t, self.time_display)))
        return widget

    def edit_symbol_databases(self):
        """Add or remove the DBC files every window uses."""
        dialog = SymbolDatabaseDialog(self.symbols, self)
        dialog.exec_()
        self.log_verbose(f"Symbol databases: {len(self.symbols.messages())} message(s) "
                         f"from {len(self.symbols.databases)} file(s)")
        return dialog
