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
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from canexpert.designer.canvas import FormCanvas
from canexpert.designer.code_editor import CodeEditor, UdsFunctionPanel
from canexpert.designer.side_panels import (BINDING_TYPE_DBC, BINDING_TYPE_SCRIPT, PropertyEditor, SymbolListPanel,
                                            WidgetPalette, control_name, default_handler_name)
from canexpert.flashing import choose_firmware, close_progress, confirm_flash, progress_dialog, report_result, \
    update_progress
from canexpert.panel.controls import CONTROLS, WIDGET_GROUPS
from canexpert.panel.database import DATABASES_DIR, parse_application_database, parse_widget
from canexpert.panel.runtime import SCRIPT_TEMPLATE
from canexpert.paths import EXAMPLE_FIRMWARE_DIR
from canexpert.ui_common import SplitterPanel, enable_maximize

# -----------------------------------------------------------------------------
# Test mode
# -----------------------------------------------------------------------------

class TestPanelDialog(QDialog):
    """Runs the panel and its script against the simulated ECU on a private virtual CAN bus."""
    ecu_log = pyqtSignal(str)

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
        config = validate_config({"name": "Test", "request_id": 0x7E0, "response_id": 0x7E8})
        self.runtime = ScriptRuntime(self.mailbox, config, self.panel.values(), self)
        self.runtime.value_changed.connect(self.panel.set_value)
        self.runtime.logged.connect(self._log)
        self.panel.control_changed.connect(lambda name, value: self.runtime.post("control", name, value))
        self.runtime.dbc = self.panel.dbc
        self.runtime.handlers = self.panel.handlers()
        self.flash_button = QPushButton("Flashing...")
        self.flash_button.setEnabled(False)
        self.flash_button.setToolTip("Flash a .s19/.hex file into the simulated ECU with the script's Flashing() "
                                     "(enabled when the script defines it)")
        self.flash_button.clicked.connect(self.open_flashing)
        self.flash_dialog = None
        self.runtime.flashing_available.connect(self.flash_button.setEnabled)
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

    def open_flashing(self):
        """Same flow as the main window's Flashing button, against the simulated ECU."""
        start = EXAMPLE_FIRMWARE_DIR if EXAMPLE_FIRMWARE_DIR.exists() else Path.home()
        firmware = choose_firmware(self, start)
        if firmware is not None and confirm_flash(self, firmware, "the simulated ECU"):
            self.start_flashing(firmware)

    def start_flashing(self, firmware):
        self.flash_button.setEnabled(False)
        self.flash_dialog = progress_dialog(self, firmware, self.runtime.cancel_flash)
        self._log(f"Flashing {Path(firmware.path).name}: {firmware.size} bytes in {len(firmware.segments)} segment(s)")
        self.runtime.start_flash(firmware)

    def _flash_finished(self, ok, text):
        dialog, self.flash_dialog = self.flash_dialog, None
        close_progress(dialog)
        self.flash_button.setEnabled(self.runtime.flash_function is not None)
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
            with self.runtime.lock:
                self.runtime.values.update(self.panel.values())
            self.runtime.post("can", message.arbitration_id, data)

    def done(self, result):
        dialog, self.flash_dialog = self.flash_dialog, None
        close_progress(dialog)
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
    """Main form designer dialog."""
    saved = pyqtSignal(str)

    def __init__(self, parent=None, db_id: str = "", db_name: str = "", description: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Form Designer")
        enable_maximize(self)
        self.setMinimumSize(900, 600)
        self.resize(1200, 780)
        self.database_dir = DATABASES_DIR
        self.db_id = db_id or f"new_{date.today().isoformat()}"
        self.db_name = db_name or self.db_id
        self.description = description

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

        # Middle: form and Python code editors
        code_page = QWidget()
        code_layout = QVBoxLayout(code_page)
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
        self.design_tabs = QTabWidget()
        self.design_tabs.addTab(self.canvas, "Form")
        self.design_tabs.addTab(code_page, "Python script")
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

        top_layout = QHBoxLayout()
        self.db_id_edit = QLineEdit(self.db_id)
        self.db_id_edit.setPlaceholderText("Database ID (e.g. engine_2026-09-18)")
        top_layout.addWidget(QLabel("Database ID:"))
        top_layout.addWidget(self.db_id_edit)
        self.db_name_edit = QLineEdit(self.db_name)
        self.db_name_edit.setPlaceholderText("Database name")
        top_layout.addWidget(QLabel("Name:"))
        top_layout.addWidget(self.db_name_edit)
        self.desc_edit = QLineEdit(self.description)
        self.desc_edit.setPlaceholderText("Description")
        top_layout.addWidget(QLabel("Description:"))
        top_layout.addWidget(self.desc_edit)
        top_layout.addWidget(QLabel("DBC path:"))
        self.dbc_path_edit = QLineEdit()
        self.dbc_path_edit.setPlaceholderText("Optional DBC for symbols")
        self.dbc_path_edit.setMinimumWidth(120)
        top_layout.addWidget(self.dbc_path_edit)
        self.symbol_list.dbc_loaded.connect(self.dbc_path_edit.setText)

        btn_layout = QHBoxLayout()
        for text, slot in (("Save", self.save), ("Load", self.load), ("New", self.new_form)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            btn_layout.addWidget(button)
        test_btn = QPushButton("Test panel...")
        test_btn.setToolTip("Run this panel and its script against the simulated ECU on a virtual CAN bus")
        test_btn.clicked.connect(self.test_panel)
        btn_layout.addWidget(test_btn)
        btn_layout.addStretch()

        layout = QVBoxLayout()
        layout.addLayout(top_layout)
        layout.addLayout(btn_layout)
        layout.addWidget(splitter)
        self.setLayout(layout)
        self._load_script()

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
        # The palette, the DBC symbols and the properties all act on the form; the script gets the room instead.
        on_form = self.design_tabs.widget(index) is self.canvas
        if not on_form:
            self.design_sizes = self.design_splitter.sizes()
        self.symbols_panel.setVisible(on_form)
        self.properties_panel.setVisible(on_form)
        if on_form:
            self.design_splitter.setSizes(self.design_sizes)
        else:
            words = []
            for page in self.canvas.pages:
                for data in page["widgets"]:
                    words += [control_name(data), str(data.get("handler", ""))]
            words += self.symbol_list.get_dbc_signals()
            self.code_editor.set_completion_words(words)

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
        self.design_tabs.setCurrentIndex(1)
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
        self.canvas.load_from_data({})
        self.properties.clear()
        self.db_id_edit.setText(f"new_{date.today().isoformat()}")
        self.db_name_edit.setText(f"new_{date.today().isoformat()}")
        self.desc_edit.clear()
        self.dbc_path_edit.clear()
        self.symbol_list._dbc_path = None
        self.symbol_list._dbc_db = None
        self.symbol_list.symbol_tree.clear()
        self.code_editor.setPlainText(SCRIPT_TEMPLATE)

    def load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Database", str(self.database_dir), "XML (*.xml)")
        if not path:
            return
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
            self.desc_edit.setText(self.description)
            dbc_el = root.find("dbc_path")
            dbc_path = root.get("dbc_path", "") or (dbc_el.text.strip() if dbc_el is not None and dbc_el.text else "")
            if dbc_path and not Path(dbc_path).is_absolute():
                dbc_path = str(self.database_dir / dbc_path)
            if dbc_path and Path(dbc_path).exists():
                self.dbc_path_edit.setText(dbc_path)
                self.symbol_list.load_dbc_path(dbc_path)
            else:
                self.dbc_path_edit.clear()

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

    def save(self):
        db_id = self.db_id_edit.text().strip() or "new"
        db_name = self.db_name_edit.text().strip() or db_id
        description = self.desc_edit.text().strip()

        if Path(db_id).name != db_id or any(c in db_id for c in '/\\:*?"<>|'):
            QMessageBox.warning(self, "Invalid database ID", "Use a filename stem such as engine_2026-09-18.")
            return
        path = self.database_dir
        path.mkdir(parents=True, exist_ok=True)
        filepath = path / f"{db_id}.xml"
        root = self._build_root(db_name, description)
        tree = ET.ElementTree(root)
        ET.indent(tree, space="    ")
        try:
            for elem in root.iter():
                if elem.tag in WIDGET_GROUPS:
                    parse_widget(elem)
            compile(self.code_editor.toPlainText(), str(self._script_path()), "exec")
            tree.write(filepath, encoding="utf-8", xml_declaration=True, default_namespace=None)
            if self._save_script():
                QMessageBox.information(self, "Saved", f"Saved to {filepath}\nScript: {self._script_path()}")
            else:
                QMessageBox.information(self, "Saved", f"Form saved to {filepath}")
            self.saved.emit(str(filepath))
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save: {e}")

    def test_panel(self, simulate_ecu=True):
        """Run the form as it is now (no need to save) in a test window."""
        if not self.check_syntax():
            self.design_tabs.setCurrentIndex(1)
            return None
        try:
            root = self._build_root(self.db_name_edit.text().strip() or "Test", self.desc_edit.text().strip())
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
