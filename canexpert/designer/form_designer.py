"""
Form Designer

Visual editor for panel databases, in the spirit of CANoe's Panel Designer: drag controls from the
palette (or DBC signals from the symbol list) onto pages, arrange them with multi-select, align,
distribute, grid snap, resize handles and undo/redo, set their properties, and write the panel's
Python script (per-control handlers and CAPL-style event decorators). Test mode runs the panel
against the simulated ECU on a virtual CAN bus.
"""
import re
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence, QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMenuBar, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QStatusBar, QTabWidget,
    QVBoxLayout, QWidget,
)

from canexpert.designer.canvas import FormCanvas
from canexpert.designer.code_editor import CodeEditor, UdsFunctionPanel
from canexpert.designer.side_panels import (BINDING_TYPE_DBC, BINDING_TYPE_SCRIPT, PropertyEditor, SymbolListPanel,
                                            WidgetPalette, control_name, default_handler_name)
from canexpert.flash_runner import FlashRunner
from canexpert.flash_sequence import FlashProfile
from canexpert.flashing import (FlashDialog, choose_firmware, close_progress, progress_dialog, report_result,
                                update_progress)
from canexpert.panel.controls import CONTROLS, WIDGET_GROUPS
from canexpert.config import read_configurations
from canexpert.panel.database import (DATABASES_DIR, parse_application_database, parse_widget, select_database,
                                      split_database_id)
from canexpert.panel.runtime import SCRIPT_TEMPLATE
from canexpert.paths import CONFIG_DIR, EXAMPLE_FIRMWARE_DIR
from canexpert.ui_common import SplitterPanel, enable_maximize

# -----------------------------------------------------------------------------
# Test mode
# -----------------------------------------------------------------------------

class TestPanelDialog(QDialog):
    """Runs the panel and its script against the simulated ECU on a private virtual CAN bus.

    It reads its bus itself (a timer, _receive) instead of through a CanWorker, and offers what the
    built-in flashing sequence needs of one: add_mailbox, remove_mailbox and message_sent."""
    ecu_log = pyqtSignal(str)
    message_sent = pyqtSignal(int, bytes)

    def __init__(self, database, script_text, simulate_ecu=True, parent=None):
        super().__init__(parent)
        import can
        from canexpert.can_bus import ReceiveMailbox
        from canexpert.config import validate_config
        from canexpert.panel.runtime import ScriptRuntime
        from canexpert.panel.view import PanelView
        self.setWindowTitle(f"Test panel - {database.get('name', '')}")
        enable_maximize(self)
        self.resize(1000, 720)
        layout = QVBoxLayout(self)
        note = QLabel("Test mode: a private virtual CAN bus" + (" with the simulated ECU (dummy_ecu.py)"
                                                                 if simulate_ecu else "") +
                      ". Nothing is sent to hardware.")
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)
        channel = f"designer-test-{uuid.uuid4()}"
        self.bus = can.Bus(interface="virtual", channel=channel)
        self._stop = threading.Event()
        self.ecu_bus = None
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.show_traffic = QCheckBox("Show CAN traffic")
        self.ecu_log.connect(self._log)
        if simulate_ecu:
            from canexpert.simulator.ecu import DummyEcu, EcuConfig
            self.ecu_bus = can.Bus(interface="virtual", channel=channel)
            self.ecu = DummyEcu(self.ecu_bus, EcuConfig(), log=lambda text: self.ecu_log.emit(f"ECU: {text}"))
            threading.Thread(target=self.ecu.serve, args=(self._stop,), daemon=True).start()
        self.panel = PanelView(database, self._send, self._log)
        self.mailbox = ReceiveMailbox(self.bus, lambda can_id, data: self._traffic("TX", can_id, data))
        self.config = validate_config({"name": "Test", "request_id": 0x7E0, "response_id": 0x7E8})
        self.runtime = ScriptRuntime(self.mailbox, self.config, self.panel.values(), self)
        self._mailboxes = []                  # the flashing sequence's, fed by _receive as the script's is
        self.message_sent.connect(lambda can_id, data: self._traffic("TX", can_id, data))
        self.flash_profile = FlashProfile()   # for this test only: the main window keeps the one in use
        self.flash_runner = None
        self.runtime.value_changed.connect(self.panel.set_value)
        self.runtime.logged.connect(self._log)
        self.panel.control_changed.connect(lambda name, value: self.runtime.post("control", name, value))
        self.runtime.dbc = self.panel.dbc
        self.runtime.handlers = self.panel.handlers()
        self.flash_button = QPushButton("Flashing...")
        self.flash_button.setToolTip("Flash a .s19/.hex file into the simulated ECU, with the script's Flashing() "
                                     "or the built-in sequence")
        self.flash_button.clicked.connect(self.open_flashing)
        self.flash_dialog = None
        self.runtime.flash_progress.connect(lambda done, total, text: update_progress(self.flash_dialog, done, total, text))
        self.runtime.flash_finished.connect(self._flash_finished)
        bar = QHBoxLayout()
        bar.addWidget(self.flash_button)
        bar.addStretch()
        layout.addLayout(bar)
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self.panel)
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addWidget(self.show_traffic)
        log_layout.addWidget(self.log_view)
        splitter.addWidget(log_box)
        splitter.setSizes([520, 160])
        layout.addWidget(splitter, 1)
        self._script_file = Path(tempfile.mkdtemp()) / "panel_test_script.py"
        self._script_file.write_text(script_text, encoding="utf-8")
        self.runtime.start(self._script_file)
        self._pump = QTimer(self)
        self._pump.setInterval(10)
        self._pump.timeout.connect(self._receive)
        self._pump.start()

    def _log(self, text):
        self.log_view.appendPlainText(str(text))

    # --- what the flashing sequence needs of a session's worker ---

    def add_mailbox(self, mailbox):
        self._mailboxes.append(mailbox)

    def remove_mailbox(self, mailbox):
        self._mailboxes = [item for item in self._mailboxes if item is not mailbox]

    # --- flashing ---

    def open_flashing(self):
        """Same dialog as the main window's Flashing button, against the simulated ECU."""
        start = EXAMPLE_FIRMWARE_DIR if EXAMPLE_FIRMWARE_DIR.exists() else Path.home()
        firmware = choose_firmware(self, start)
        if firmware is None:
            return
        dialog = FlashDialog(firmware, self.flash_profile, script_available=self.runtime.flash_function is not None,
                             parent=self, target="the simulated ECU")
        if dialog.exec_() != QDialog.Accepted:
            return
        self.flash_profile = dialog.profile
        if dialog.use_script():
            self.start_flashing(firmware)
        else:
            self.start_built_in_flash(firmware, dialog.profile)

    def start_built_in_flash(self, firmware, profile):
        """The built-in ISO 14229 sequence, as the main window runs it, on this panel's bus."""
        self.flash_button.setEnabled(False)
        self.flash_runner = FlashRunner(lambda: (self.bus, self, self.config), self)
        self.flash_runner.logged.connect(self._log)
        self.flash_runner.progress.connect(lambda done, total, text: update_progress(self.flash_dialog, done, total, text))
        self.flash_runner.finished.connect(self._flash_finished)
        self.flash_dialog = progress_dialog(self, firmware, self.flash_runner.cancel)
        self._log(f"Flashing {Path(firmware.path).name} with the built-in sequence")
        self.flash_runner.start(firmware, profile)

    def start_flashing(self, firmware):
        self.flash_button.setEnabled(False)
        self.flash_dialog = progress_dialog(self, firmware, self.runtime.cancel_flash)
        self._log(f"Flashing {Path(firmware.path).name}: {firmware.size} bytes in {len(firmware.segments)} segment(s)")
        self.runtime.start_flash(firmware)

    def _flash_finished(self, ok, text):
        dialog, self.flash_dialog = self.flash_dialog, None
        close_progress(dialog)
        self.flash_button.setEnabled(True)
        self.flash_runner = None
        self._log(f"Flashing {'succeeded' if ok else 'failed'}: {text}")
        report_result(self, ok, text)

    def _traffic(self, direction, can_id, data):
        if self.show_traffic.isChecked():
            self.log_view.appendPlainText(f"{direction} 0x{can_id:03X}  {bytes(data).hex(' ')}")

    def _send(self, can_id, data, extended=None):
        import can
        self.bus.send(can.Message(arbitration_id=can_id, data=bytes(data), is_extended_id=bool(extended)))
        self._traffic("TX", can_id, data)

    def _receive(self):
        for _ in range(500):
            message = self.bus.recv(0)
            if message is None:
                return
            data = bytes(message.data)
            self._traffic("RX", message.arbitration_id, data)
            self.panel.on_message(message.arbitration_id, data)
            self.mailbox.push(message)
            for mailbox in list(self._mailboxes):
                mailbox.push(message)
            with self.runtime.lock:
                self.runtime.values.update(self.panel.values())
            self.runtime.post("can", message.arbitration_id, data)

    def done(self, result):
        dialog, self.flash_dialog = self.flash_dialog, None
        close_progress(dialog)
        if self.flash_runner is not None:
            self.flash_runner.cancel()                    # the bus is about to go away under it
        self._pump.stop()
        self.runtime.stop()
        self.mailbox.close()
        self._stop.set()
        time.sleep(0.05)
        for bus in (self.bus, self.ecu_bus):
            if bus is not None:
                bus.shutdown()
        super().done(result)


# -----------------------------------------------------------------------------
# Designer dialog
# -----------------------------------------------------------------------------

class FormDesigner(QDialog):
    """Main form designer dialog: a menu bar, the Form, Python script and Database tabs, a status line."""
    saved = pyqtSignal(str)

    def __init__(self, parent=None, db_id: str = "", db_name: str = "", description: str = ""):
        super().__init__(parent)
        enable_maximize(self)
        self.setMinimumSize(900, 600)
        self.resize(1200, 780)
        self.database_dir = DATABASES_DIR
        self.db_id = db_id or f"new_{date.today().isoformat()}"
        self.db_name = db_name or self.db_id
        self.description = description
        self._loaded_id = None          # the ID of the file on disk this form came from, if any
        self._dirty = False

        self.symbol_list = SymbolListPanel()
        self.palette = WidgetPalette()
        self.canvas = FormCanvas()
        self.canvas.base_dir = self.database_dir
        self.properties = PropertyEditor(self.symbol_list)
        self.properties.base_dir = self.database_dir
        self.code_editor = CodeEditor()
        self.code_editor.setPlaceholderText("Panel script. Load from file or start from the template.")

        self.canvas.widget_selected.connect(self.on_widget_selected)
        self.canvas.selection_cleared.connect(self.properties.clear)
        self.canvas.geometry_changed.connect(self.properties.set_geometry_values)
        self.canvas.handler_requested.connect(lambda index: self.edit_handler(self.canvas._current_widgets()[index]))
        self.canvas.graphics_view.signal_dropped.connect(self._on_signal_dropped)
        self.properties.properties_changed.connect(self.on_properties_changed)
        self.properties.editing.connect(lambda key: self.canvas.checkpoint(key=("property", id(self.properties.widget_data), key)))
        self.properties.handler_requested.connect(self.edit_handler)
        self.symbol_list.symbol_selected.connect(self._on_symbol_selected)

        left_widget = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_split = QSplitter(Qt.Vertical)
        left_split.addWidget(self.symbol_list)
        left_split.addWidget(self.palette)
        left_split.setSizes([260, 420])
        left_layout.addWidget(left_split)
        left_widget.setLayout(left_layout)

        # Middle: the form, the Python script, and what the database is
        self.code_page = QWidget()
        code_layout = QVBoxLayout(self.code_page)
        code_layout.setContentsMargins(0, 0, 0, 0)
        code_bar = QHBoxLayout()
        check_btn = QPushButton("Check syntax")
        check_btn.clicked.connect(self.check_syntax)
        self.syntax_label = QLabel("Ctrl+Space: complete (API, control names, signals). Double-click a control "
                                   "on the Form tab to create its handler.")
        self.syntax_label.setStyleSheet("color: gray;")
        code_bar.addWidget(check_btn)
        code_bar.addWidget(self.syntax_label, 1)
        code_layout.addLayout(code_bar)
        self.uds_panel = UdsFunctionPanel()
        self.uds_panel.insert_requested.connect(self.code_editor.insert_snippet)
        code_split = QSplitter(Qt.Horizontal)
        code_split.addWidget(self.code_editor)
        code_split.addWidget(self.uds_panel)
        code_split.setStretchFactor(0, 1)
        code_split.setSizes([640, 330])
        code_layout.addWidget(code_split, 1)
        self.database_page = self._database_page()
        self.design_tabs = QTabWidget()
        self.design_tabs.addTab(self.canvas, "Form")
        self.design_tabs.addTab(self.code_page, "Python script")
        self.design_tabs.addTab(self.database_page, "Database")
        self.design_tabs.currentChanged.connect(self._on_tab_changed)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(True)
        left_widget.setMinimumWidth(0)
        self.design_tabs.setMinimumWidth(0)
        self.properties.setMinimumWidth(0)
        self.symbols_panel = SplitterPanel("Symbols & controls", left_widget, Qt.Horizontal)
        splitter.addWidget(self.symbols_panel)
        splitter.addWidget(SplitterPanel("Form / Code", self.design_tabs, Qt.Horizontal))
        self.properties_panel = SplitterPanel("Properties", self.properties, Qt.Horizontal)
        splitter.addWidget(self.properties_panel)
        self.design_splitter, self.design_sizes = splitter, [230, 680, 300]
        splitter.setSizes(self.design_sizes)

        self.status = QStatusBar()
        self.status.setSizeGripEnabled(False)
        layout = QVBoxLayout()
        layout.setMenuBar(self._menu_bar())
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status)
        self.setLayout(layout)
        self._load_script()

        # Anything that changes what would be saved makes the form "unsaved" (a * in the title).
        self.canvas.changed.connect(self._mark_dirty)
        self.code_editor.textChanged.connect(self._mark_dirty)
        for edit in (self.db_id_edit, self.db_name_edit, self.dbc_path_edit):
            edit.textChanged.connect(self._mark_dirty)
        self.desc_edit.textChanged.connect(self._mark_dirty)
        self.db_id_edit.textChanged.connect(self._refresh_id_hint)
        self._mark_clean()

    # --- the menu bar ----------------------------------------------------------------------

    def _action(self, menu, text, slot, shortcut=None, tip="", canvas_only=False):
        action = menu.addAction(text)
        action.triggered.connect(lambda _checked=False: slot())      # never hand Qt's "checked" to slot
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            if canvas_only:
                # The keys belong to the form: a text field or the script editor keeps its own Ctrl+C, Ctrl+Z...
                action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
                self.canvas.addAction(action)
        if tip:
            action.setStatusTip(tip)
            action.setToolTip(tip)
        return action

    def _menu_bar(self):
        bar = QMenuBar(self)
        self.menus = {}

        menu = self.menus["file"] = bar.addMenu("&File")
        self._action(menu, "&New", self.new_form, QKeySequence.New, "An empty panel and the script template")
        self._action(menu, "&Open...", lambda: self.load(), QKeySequence.Open, "Open a panel database (.xml)")
        self.recent_menu = menu.addMenu("Open from the &Databases folder")
        self.recent_menu.aboutToShow.connect(self._fill_open_menu)
        menu.addSeparator()
        self._action(menu, "&Save", self.save, QKeySequence.Save, "Save the panel and its script")
        self._action(menu, "Save &as...", self.save_as, "Ctrl+Shift+S", "Save under another database ID")
        menu.addSeparator()
        self._action(menu, "&Close", self.close, "Ctrl+W")

        menu = self.menus["edit"] = bar.addMenu("&Edit")
        self.edit_actions = {}
        for entry in (("undo", "&Undo", "Ctrl+Z"), ("redo", "&Redo", "Ctrl+Y"), None,
                      ("cut", "Cu&t", "Ctrl+X"), ("copy", "&Copy", "Ctrl+C"), ("paste", "&Paste", "Ctrl+V"),
                      ("duplicate", "D&uplicate", "Ctrl+D"), ("delete", "&Delete", "Delete"), None,
                      ("select_all", "Select &all", "Ctrl+A")):
            if entry is None:
                menu.addSeparator()
                continue
            key, text, shortcut = entry
            self.edit_actions[key] = self._action(menu, text, lambda k=key: self._edit(k), shortcut, canvas_only=True)
        menu.aboutToShow.connect(self._update_edit_menu)

        menu = self.menus["arrange"] = bar.addMenu("&Arrange")
        self.arrange_actions = {}
        for entry in (("left", "Align &left edges"), ("center", "Align centres &horizontally"),
                      ("right", "Align &right edges"), ("top", "Align &top edges"),
                      ("middle", "Align centres &vertically"), ("bottom", "Align &bottom edges"), None,
                      ("same_width", "Same &width"), ("same_height", "Same h&eight"), ("same_size", "Same &size"),
                      ("distribute_h", "Distribute hori&zontally"), ("distribute_v", "Distribute verti&cally"),
                      None, ("front", "Bring to &front"), ("back", "Send to bac&k")):
            if entry is None:
                menu.addSeparator()
                continue
            key, text = entry
            slot = {"front": self.canvas.bring_to_front, "back": self.canvas.send_to_back}.get(
                key, lambda k=key: self.canvas.align(k))
            self.arrange_actions[key] = self._action(menu, text, slot)
        menu.addSeparator()
        self.grid_action = menu.addAction("Show the &grid and snap to it")
        self.grid_action.setCheckable(True)
        self.grid_action.toggled.connect(self.canvas.set_grid)
        menu.aboutToShow.connect(self._update_arrange_menu)

        menu = self.menus["page"] = bar.addMenu("&Page")
        self._action(menu, "&Add page", self.canvas.add_page)
        self._action(menu, "&Rename page...", self.rename_page)
        self.remove_page_action = self._action(menu, "Re&move page", lambda: self.canvas.remove_page(
            self.canvas.current_page_index))
        menu.aboutToShow.connect(lambda: self.remove_page_action.setEnabled(len(self.canvas.pages) > 1))

        menu = self.menus["script"] = bar.addMenu("&Script")
        self._action(menu, "&Check syntax", self.check_syntax, "F7")
        self.handler_action = self._action(menu, "&Handler of the selected control", self._edit_selected_handler,
                                           "F4", "Go to the selected control's handler, creating it if needed")
        menu.aboutToShow.connect(lambda: self.handler_action.setEnabled(len(self.canvas.selection) == 1))

        menu = self.menus["test"] = bar.addMenu("&Test")
        self._action(menu, "Test panel with the &simulated ECU", lambda: self.test_panel(), "F5",
                     "Run the panel and its script against the simulated ECU on a virtual CAN bus")
        self._action(menu, "Test panel &without an ECU", lambda: self.test_panel(simulate_ecu=False), "Shift+F5",
                     "Run the panel and its script on an empty virtual CAN bus")

        menu = self.menus["help"] = bar.addMenu("&Help")
        self._action(menu, "Form Designer in the &manual", self.open_manual, "F1")

        test_btn = QPushButton("Test panel...")
        test_btn.setToolTip("Run this panel and its script against the simulated ECU on a virtual CAN bus (F5)")
        test_btn.clicked.connect(lambda: self.test_panel())      # with the simulated ECU, not clicked's False
        bar.setCornerWidget(test_btn, Qt.TopRightCorner)
        return bar

    def _edit_target(self):
        """What the Edit menu acts on: a focused text field, else the script or the form in front."""
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QPlainTextEdit)) and self.isAncestorOf(focus):
            return focus
        if self.design_tabs.currentWidget() is self.code_page:
            return self.code_editor
        return self.canvas if self.design_tabs.currentWidget() is self.canvas else None

    def _edit(self, key):
        target = self._edit_target()
        if target is self.canvas:
            {"undo": self.canvas.undo, "redo": self.canvas.redo, "cut": self.canvas.cut_selection,
             "copy": self.canvas.copy_selection, "paste": self.canvas.paste_at,
             "duplicate": self.canvas.duplicate_selection, "delete": self.canvas.delete_selection,
             "select_all": self.canvas.select_all}[key]()
        elif target is not None:
            if key == "delete":
                if isinstance(target, QLineEdit):
                    target.del_()
                else:
                    target.textCursor().removeSelectedText()
            elif key != "duplicate":
                getattr(target, {"select_all": "selectAll"}.get(key, key))()

    def _update_edit_menu(self):
        target = self._edit_target()
        on_canvas = target is self.canvas
        selected = bool(self.canvas.selection)
        states = {"undo": bool(self.canvas._undo), "redo": bool(self.canvas._redo), "cut": selected,
                  "copy": selected, "paste": bool(self.canvas._widget_clipboard), "duplicate": selected,
                  "delete": selected, "select_all": bool(self.canvas._current_widgets())} if on_canvas else \
            {key: target is not None and key != "duplicate" for key in self.edit_actions}
        for key, action in self.edit_actions.items():
            action.setEnabled(states[key])

    def _update_arrange_menu(self):
        count = len(self.canvas.selection)
        for key, action in self.arrange_actions.items():
            needed = 3 if key.startswith("distribute") else 1 if key in ("front", "back") else 2
            action.setEnabled(count >= needed)
        self.grid_action.blockSignals(True)
        self.grid_action.setChecked(self.canvas.show_grid)
        self.grid_action.blockSignals(False)

    def _edit_selected_handler(self):
        if len(self.canvas.selection) == 1:
            self.edit_handler(self.canvas._current_widgets()[self.canvas.selected_index])

    def rename_page(self):
        index = self.canvas.current_page_index
        name, ok = QInputDialog.getText(self, "Rename page", "Page name:", QLineEdit.Normal,
                                        self.canvas.pages[index]["name"])
        if ok:
            self.canvas.rename_page(index, name)

    def open_manual(self):
        from canexpert.help_window import show_manual
        window = show_manual(self)
        window.go_to_section("Form Designer")
        return window

    def _fill_open_menu(self):
        """The panels in the Databases folder, each family's newest version first."""
        self.recent_menu.clear()
        entries = []
        for path in Path(self.database_dir).glob("*.xml"):
            family, stamp = split_database_id(path.stem)
            entries.append((family.lower(), -(stamp.toordinal() if stamp else 0), path))
        for _family, _age, path in sorted(entries):
            self.recent_menu.addAction(path.stem, lambda p=path: self.load(p))
        if not entries:
            self.recent_menu.addAction(f"No panel in {self.database_dir}").setEnabled(False)

    # --- the Database tab --------------------------------------------------------------------

    def _database_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        identity = QGroupBox("Identity")
        form = QFormLayout(identity)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.db_id_edit = QLineEdit(self.db_id)
        self.db_id_edit.setPlaceholderText("family_YYYY-MM-DD, e.g. engine_2026-09-18")
        new_version = QPushButton("New version (today)")
        new_version.setToolTip("The same family with today's date: saving keeps the version you started from")
        new_version.clicked.connect(lambda: self.new_version())
        form.addRow("Database ID", _row(self.db_id_edit, new_version))
        self.id_hint = QLabel()
        self.id_hint.setWordWrap(True)
        self.id_hint.setTextFormat(Qt.RichText)
        form.addRow(self.id_hint)
        self.db_name_edit = QLineEdit(self.db_name)
        self.db_name_edit.setPlaceholderText("Database name")
        form.addRow("Name", self.db_name_edit)
        self.desc_edit = QPlainTextEdit(self.description)
        self.desc_edit.setPlaceholderText("Shown at the top of the Database window while it is loaded")
        self.desc_edit.setFixedHeight(70)
        form.addRow("Description", self.desc_edit)

        symbols = QGroupBox("Symbols")
        form = QFormLayout(symbols)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.dbc_path_edit = QLineEdit()
        self.dbc_path_edit.setPlaceholderText("No DBC: controls bind to script names and raw CAN bytes")
        self.dbc_path_edit.setMinimumWidth(120)
        self.symbol_list.dbc_loaded.connect(self.dbc_path_edit.setText)
        browse, remove = QPushButton("Browse..."), QPushButton("Remove")
        browse.clicked.connect(self.symbol_list._load_dbc)
        remove.clicked.connect(self.remove_dbc)
        form.addRow("DBC", _row(self.dbc_path_edit, browse, remove))
        form.addRow(_hint("The panel's own DBC: controls bound to Message.Signal decode and send with it, and "
                          "its signals are the Symbols list on the Form tab."))

        contents = QGroupBox("Contents")
        self.contents_label = QLabel()
        self.contents_label.setWordWrap(True)
        self.contents_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        QVBoxLayout(contents).addWidget(self.contents_label)
        usage = QGroupBox("Where it is used")
        self.usage_label = QLabel()
        self.usage_label.setWordWrap(True)
        self.usage_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        QVBoxLayout(usage).addWidget(self.usage_label)
        for group in (identity, symbols, contents, usage):
            layout.addWidget(group)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _current_id(self) -> str:
        return self.db_id_edit.text().strip() or "new"

    def new_version(self):
        """Today's version of the same family."""
        family, _stamp = split_database_id(self._current_id())
        self.db_id_edit.setText(f"{family or 'panel'}_{date.today().isoformat()}")

    def remove_dbc(self):
        self.dbc_path_edit.clear()
        self.symbol_list.clear_dbc()

    def _refresh_id_hint(self, *_):
        stem = self.db_id_edit.text().strip()
        if not stem or Path(stem).name != stem or any(c in stem for c in '/\\:*?"<>|'):
            self.id_hint.setText('<span style="color:#b91c1c">A database ID is a file name without folders or '
                                 '<code>/ \\ : * ? " &lt; &gt; |</code>, e.g. engine_2026-09-18.</span>')
            return
        family, stamp = split_database_id(stem)
        where = f"Saved as <b>{stem}.xml</b> and <b>{stem}_script.py</b> in {self.database_dir}."
        if stamp is None:
            note = (f'<br><span style="color:#b45309">No date at its end: configurations whose Database family is '
                    f'"{family}" load it, but after any dated version.</span>')
        else:
            note = (f'<br>Family <b>{family}</b>, version <b>{stamp.isoformat()}</b>: a configuration whose Database '
                    f'family is "{family}" loads the newest version.')
        if (Path(self.database_dir) / f"{stem}.xml").exists() and stem != self._loaded_id:
            note += '<br><span style="color:#b45309">A panel with this ID exists: saving replaces it.</span>'
        self.id_hint.setText(where + note)

    def _refresh_database_info(self):
        """The Contents and Where it is used groups."""
        self._refresh_id_hint()
        script = self.code_editor.toPlainText()
        defined = set(re.findall(r"^\s*def\s+(\w+)\s*\(", script, re.M))
        lines, handlers, missing = [], 0, []
        for page in self.canvas.pages:
            widgets = page["widgets"]
            dbc = sum(1 for data in widgets if data.get("binding_type") == BINDING_TYPE_DBC)
            lines.append(f"<b>{page['name']}</b>: {len(widgets)} control(s), {dbc} bound to DBC signals")
            for data in widgets:
                name = str(data.get("handler") or "").strip()
                if name:
                    handlers += 1
                    if name not in defined:
                        missing.append(name)
        lines.append(f"Handlers: {handlers} named" + (
            f', <span style="color:#b45309">not in the script: {", ".join(sorted(set(missing)))}</span>'
            if missing else ", all in the script"))
        self.contents_label.setText("<br>".join(lines))

        family, _stamp = split_database_id(self._current_id())
        try:
            configurations, _errors = read_configurations(CONFIG_DIR)
        except OSError:
            configurations = []
        users = [config.get("name", "") for config in configurations if config.get("database_family") == family]
        anyone = [config.get("name", "") for config in configurations if not config.get("database_family")]
        text = (f"Configurations with Database family \"{family}\": {', '.join(users)}" if users else
                f"No configuration has Database family \"{family}\" yet.")
        if anyone:
            text += f"<br>Without a family, these load the newest panel of any family: {', '.join(anyone)}"
        try:
            newest = select_database(self.database_dir, family)
        except ValueError:
            newest = None
        if newest is not None and newest.stem != self._current_id():
            text += (f'<br><span style="color:#b45309">The newest version in the folder is {newest.stem}: that '
                     f'one is what configurations load.</span>')
        self.usage_label.setText(text)

    # --- unsaved changes ------------------------------------------------------------------------

    def _mark_dirty(self, *_):
        if not self._dirty:
            self._dirty = True
            self._update_title()

    def _mark_clean(self):
        self._dirty = False
        self.code_editor.document().setModified(False)
        self._update_title()
        self._refresh_id_hint()

    def _update_title(self):
        self.setWindowTitle(f"Form Designer — {self._current_id()}{' *' if self._dirty else ''}")

    def _confirm_discard(self) -> bool:
        """Whether what is on screen may go: nothing unsaved, or the user saved or dropped it. A window
        that is not on screen asks nobody."""
        if not self._dirty or not self.isVisible():
            return True
        answer = QMessageBox.question(self, "Form Designer", f"Save the changes to {self._current_id()}?",
                                      QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                                      QMessageBox.Save)
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    def reject(self):
        """Esc, the window's close button and Close: not without asking about unsaved changes."""
        if self._confirm_discard():
            super().reject()

    # --- script -------------------------------------------------------------------------

    def _script_path(self) -> Path:
        """Path to the database script file for current db_id."""
        db_id = self.db_id_edit.text().strip() or "new"
        return self.database_dir / f"{db_id}_script.py"

    def _load_script(self):
        path = self._script_path()
        if path.exists():
            try:
                self.code_editor.setPlainText(path.read_text(encoding="utf-8"))
            except Exception as e:
                self.code_editor.setPlainText(SCRIPT_TEMPLATE)
                self.code_editor.appendPlainText(f"\n# Error loading script: {e}")
        else:
            self.code_editor.setPlainText(SCRIPT_TEMPLATE)

    def _save_script(self) -> bool:
        path = self._script_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(self.code_editor.toPlainText(), encoding="utf-8")
            return True
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save script: {e}")
            return False

    def check_syntax(self):
        ok, message, line = self.code_editor.check_syntax(str(self._script_path()))
        self.syntax_label.setText(message)
        self.syntax_label.setStyleSheet("color: green;" if ok else "color: red;")
        if line:
            self.code_editor.go_to_line(line)
        return ok

    def _on_tab_changed(self, index):
        # The palette, the DBC symbols and the properties all act on the form; the others get the room instead.
        page = self.design_tabs.widget(index)
        on_form = page is self.canvas
        if not on_form and not self.symbols_panel.isHidden():
            self.design_sizes = self.design_splitter.sizes()
        self.symbols_panel.setVisible(on_form)
        self.properties_panel.setVisible(on_form)
        if on_form:
            self.design_splitter.setSizes(self.design_sizes)
        elif page is self.code_page:
            words = []
            for form_page in self.canvas.pages:
                for data in form_page["widgets"]:
                    words += [control_name(data), str(data.get("handler", ""))]
            words += self.symbol_list.get_dbc_signals()
            self.code_editor.set_completion_words(words)
        elif page is self.database_page:
            self._refresh_database_info()

    def edit_handler(self, data):
        """Open (creating if needed) the handler function of a control in the Python script."""
        control = CONTROLS.get(data.get("type"), CONTROLS["label"])
        if not control.interactive:
            QMessageBox.information(self, "Handler", f"A {control.label} only displays values, so it has no handler.\n"
                                                     "Set it from the script with api.ui.set_value().")
            return
        name = str(data.get("handler") or "").strip() or default_handler_name(data)
        if not name.isidentifier():
            QMessageBox.warning(self, "Handler", f"'{name}' is not a valid Python function name.")
            return
        if data.get("handler") != name:
            self.canvas.checkpoint()
            data["handler"] = name
            if self.properties.widget_data is data:
                self.properties.load_widget(data, len(self.canvas.selection))
        code = self.code_editor.toPlainText()
        match = re.search(rf"^def {re.escape(name)}\s*\(", code, re.M)
        if match is None:
            label = control_name(data)
            stub = (f"\n\ndef {name}(api, value):\n"
                    f'    """{control.label} "{label}" {control.event}."""\n'
                    f'    api.log(f"{label}: {{value}}")\n')
            if not code.endswith("\n"):
                stub = "\n" + stub
            self.code_editor.moveCursor(QTextCursor.End)
            self.code_editor.insertPlainText(stub)
            code = self.code_editor.toPlainText()
            match = re.search(rf"^def {re.escape(name)}\s*\(", code, re.M)
        line = code[:match.start()].count("\n") + 1
        self.design_tabs.setCurrentWidget(self.code_page)
        self.code_editor.go_to_line(line + 2, 4)

    # --- canvas events --------------------------------------------------------------------------

    def on_widget_selected(self, index: int, data: dict):
        self.properties.load_widget(data, len(self.canvas.selection))

    def _on_symbol_selected(self, symbol: str):
        """Set current widget binding to the double-clicked DBC symbol."""
        w = self.canvas._current_widgets()
        idx = self.canvas.selected_index
        if 0 <= idx < len(w) and symbol:
            self.canvas.checkpoint()
            w[idx]["binding_type"] = BINDING_TYPE_DBC
            w[idx]["binding_value"] = symbol
            w[idx]["variable"] = symbol
            self.properties.load_widget(w[idx], len(self.canvas.selection))
            self.canvas._rebuild()

    def _on_signal_dropped(self, name, x, y, as_input):
        self.canvas.add_signal_control(name, x, y, self.symbol_list.signal_info(name), as_input)

    def on_properties_changed(self, data: dict):
        for i, w in enumerate(self.canvas._current_widgets()):
            if w is data:
                self.canvas.update_widget(i, data)
                break

    # --- files ----------------------------------------------------------------------------------

    def new_form(self):
        """An empty panel with the script template (after asking about unsaved changes)."""
        if not self._confirm_discard():
            return False
        self.canvas.load_from_data({})
        self.properties.clear()
        self.db_id_edit.setText(f"new_{date.today().isoformat()}")
        self.db_name_edit.setText(f"new_{date.today().isoformat()}")
        self.desc_edit.clear()
        self.remove_dbc()
        self.code_editor.setPlainText(SCRIPT_TEMPLATE)
        self._loaded_id = None
        self._mark_clean()
        self.status.showMessage("New panel", 5000)
        return True

    def load(self, path=None):
        """Open a panel database: path, or the one chosen in a file dialog (after asking about unsaved changes)."""
        if not self._confirm_discard():
            return False
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Load Database", str(self.database_dir), "XML (*.xml)")
            if not path:
                return False
        path = str(path)
        try:
            root = ET.parse(path).getroot()
            self.database_dir = Path(path).resolve().parent
            self.canvas.base_dir = self.properties.base_dir = self.database_dir
            self.db_id = Path(path).stem
            self.db_name = root.get("name", self.db_id)
            self.db_id_edit.setText(self.db_id)
            self.db_name_edit.setText(self.db_name)
            desc = root.find("description")
            self.description = desc.text.strip() if desc is not None and desc.text else ""
            self.desc_edit.setPlainText(self.description)
            dbc_el = root.find("dbc_path")
            dbc_path = root.get("dbc_path", "") or (dbc_el.text.strip() if dbc_el is not None and dbc_el.text else "")
            if dbc_path and not Path(dbc_path).is_absolute():
                dbc_path = str(self.database_dir / dbc_path)
            if dbc_path and Path(dbc_path).exists():
                self.dbc_path_edit.setText(dbc_path)
                self.symbol_list.load_dbc_path(dbc_path)
            else:
                self.remove_dbc()

            pages_el = root.find("pages")
            if pages_el is not None:
                data = {"pages": []}
                for page_el in pages_el.findall("page"):
                    # Document order is the z-order (group boxes stay behind their contents).
                    widgets = [parse_widget(elem) for elem in page_el.iter() if elem.tag in WIDGET_GROUPS]
                    data["pages"].append({"name": page_el.get("name", "Page"), "widgets": widgets})
                self.canvas.load_from_data(data)
            else:
                data = {tag: [] for tag in ["buttons", "values", "checkboxes", "sliders", "labels"]}
                for tag in data:
                    for elem in root.findall(".//" + {"checkboxes": "checkbox"}.get(tag, tag[:-1])):
                        data[tag].append(parse_widget(elem))
                self.canvas.load_from_data(data)
            self.properties.clear()
            self._load_script()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load: {e}")
            return False
        self._loaded_id = self.db_id
        self._mark_clean()
        self.status.showMessage(f"Opened {path}", 5000)
        return True

    def _build_root(self, db_name, description):
        data = self.canvas.get_data()
        dbc_path = self.dbc_path_edit.text().strip()
        root = ET.Element("application_database", name=db_name)
        if dbc_path:
            root.set("dbc_path", dbc_path)
        if description:
            ET.SubElement(root, "description").text = description
        pages_el = ET.SubElement(root, "pages")
        for page in data.get("pages", []):
            page_el = ET.SubElement(pages_el, "page", name=page.get("name", "Page"))
            for item in page.get("widgets", []):
                key = item.get("type", "")
                if key not in WIDGET_GROUPS:
                    continue
                attrs = {k: str(v) for k, v in item.items() if k not in ("data_bytes", "kind") and v is not None}
                if "binding_value" in item and item.get("binding_type") == BINDING_TYPE_SCRIPT:
                    attrs["variable"] = str(item.get("binding_value", item.get("variable", "")))
                ET.SubElement(page_el, key, attrs)
        return root

    def save(self) -> bool:
        """Write the panel and its script. True when both were written."""
        db_id = self.db_id_edit.text().strip() or "new"
        db_name = self.db_name_edit.text().strip() or db_id
        description = self.desc_edit.toPlainText().strip()

        if Path(db_id).name != db_id or any(c in db_id for c in '/\\:*?"<>|'):
            QMessageBox.warning(self, "Invalid database ID", "Use a filename stem such as engine_2026-09-18.")
            return False
        path = self.database_dir
        path.mkdir(parents=True, exist_ok=True)
        filepath = path / f"{db_id}.xml"
        if filepath.exists() and db_id != self._loaded_id and self.isVisible() and QMessageBox.question(
                self, "Save", f"{filepath.name} exists. Replace it and its script?") != QMessageBox.Yes:
            return False
        root = self._build_root(db_name, description)
        tree = ET.ElementTree(root)
        ET.indent(tree, space="    ")
        try:
            for elem in root.iter():
                if elem.tag in WIDGET_GROUPS:
                    parse_widget(elem)
            compile(self.code_editor.toPlainText(), str(self._script_path()), "exec")
            tree.write(filepath, encoding="utf-8", xml_declaration=True, default_namespace=None)
            if not self._save_script():
                return False
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save: {e}")
            return False
        self._loaded_id = db_id
        self._mark_clean()
        self.status.showMessage(f"Saved {filepath.name} and {self._script_path().name} in {path} at "
                                f"{time.strftime('%H:%M:%S')}")
        self.saved.emit(str(filepath))
        return True

    def save_as(self) -> bool:
        """Save under another database ID - today's version of the same family unless another is typed."""
        family, _stamp = split_database_id(self._current_id())
        name, ok = QInputDialog.getText(self, "Save as", "Database ID (family_YYYY-MM-DD):", QLineEdit.Normal,
                                        f"{family or 'panel'}_{date.today().isoformat()}")
        if not ok or not name.strip():
            return False
        self.db_id_edit.setText(name.strip())
        return self.save()

    def test_panel(self, simulate_ecu=True):
        """Run the form as it is now (no need to save) in a test window."""
        if not self.check_syntax():
            self.design_tabs.setCurrentWidget(self.code_page)
            return None
        try:
            root = self._build_root(self.db_name_edit.text().strip() or "Test", self.desc_edit.toPlainText().strip())
            temp = Path(tempfile.mkdtemp()) / "test_panel.xml"
            ET.ElementTree(root).write(temp, encoding="utf-8", xml_declaration=True)
            database = parse_application_database(temp)
            database["source_path"] = str(self.database_dir / "test_panel.xml")  # relative DBC/image paths
            dialog = TestPanelDialog(database, self.code_editor.toPlainText(), simulate_ecu, self)
        except Exception as e:
            QMessageBox.critical(self, "Test panel", f"Cannot run the panel: {e}")
            return None
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.show()
        return dialog


def _row(*widgets):
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    for index, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if index == 0 else 0)
    return row


def _hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: gray;")
    return label
