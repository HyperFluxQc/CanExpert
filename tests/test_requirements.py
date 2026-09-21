"""Requirements acceptance tests: no hardware or user settings are touched."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtWidgets import QApplication, QDialog, QMessageBox

from canexpert import can_bus
from canexpert import main_window as main
from canexpert.config import validate_config
from canexpert.designer.form_designer import FormDesigner
from canexpert.designer.side_panels import PropertyEditor
from canexpert.panel.database import parse_application_database, select_database
from canexpert.panel.view import PanelView

APP = QApplication.instance() or QApplication([])


def spin_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


PANEL = '''<application_database name="Acceptance"><pages><page name="Main">
<button id="1" label="Start" binding_value="start" x="10" y="10"/>
<value id="2" label="Status" binding_value="status" x="10" y="50"/>
<io_box id="3" label="Input" binding_value="input" value_type="string" x="10" y="90"/>
<checkbox id="4" label="Enable" binding_value="enable" x="10" y="130"/>
</page></pages></application_database>'''
SCRIPT = '''def DatabaseMainFunction(api):
    api.ui.set_value("status", "ready")
    api.on("start", lambda value: api.can.send(0x200, [0xAB]))
    api.on("input", lambda value: api.ui.set_value("status", value))
    api.on("enable", lambda value: api.can.send(0x201, [int(value)]))
    api.on_can(lambda can_id, data: api.ui.set_value("status", "response") if can_id == 0x7E8 else None)
'''

FLASH_SCRIPT = '''
def Flashing(api, firmware):
    api.progress(firmware.size, firmware.size, "done")
    api.ui.set_value("status", f"{firmware.segments[0][0]:X}:{firmware.size}")
    return True
'''


# What the main window sends by default: TesterPresent filled to 8 bytes with 0xCC.
PADDED_TESTER_PRESENT = b"\x02\x3e\x00" + b"\xcc" * 5


class RequirementsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.configs = self.root / "Configurations"
        self.databases = self.root / "Databases"
        self.configs.mkdir()
        self.databases.mkdir()
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.IniFormat)
        self.cfg = {"name": "Second", "request_id": 0x7E0, "response_id": 0x7E8,
                    "tester_present_interval_seconds": .06, "node_timeout_seconds": .2,
                    "database_family": "panel"}
        for name in ("First", "Second"):
            (self.configs/f"config_{name}.json").write_text(json.dumps(dict(self.cfg, name=name)))
        for version in ("2026-09-01", "2026-09-18"):
            (self.databases/f"panel_{version}.xml").write_text(PANEL)
        (self.databases/'panel_2026-09-18_script.py').write_text(SCRIPT)
        self.settings.setValue("last_configuration", "Second")
        self.channel = "acceptance-" + str(uuid.uuid4())
        self.bus_calls = []
        self.ecu = can.Bus(interface="virtual", channel=self.channel)
        self.patches = [
            patch.object(main, "CONFIG_DIR", self.configs),
            patch.object(main, "DATABASES_DIR", self.databases),
            patch.object(main, "app_settings", lambda: self.settings),
            patch.object(main.can, "detect_available_configs", return_value=[]),
            patch.object(can_bus, "create_can_bus", self.fake_can_bus),
        ]
        for item in self.patches:
            item.start()
        self.window = main.MainWindow()
        self.window.selected_channel_config = {"interface":"virtual", "channel":0}

    def fake_can_bus(self, interface, channel, bitrate, **options):
        """Stands in for the adapter with create_can_bus()'s real signature, so a bad call fails here too."""
        self.bus_calls.append({"interface": interface, "channel": channel, "bitrate": bitrate, **options})
        return can.Bus(interface="virtual", channel=self.channel)

    def tearDown(self):
        self.window.close()
        APP.processEvents()
        self.ecu.shutdown()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_full_connection_panel_and_node_lifecycle(self):
        # 2, 2.1: inventory and last-used selection.
        self.assertEqual(self.window.config_list.count(), 2)
        self.assertEqual(self.window.active_config["name"], "Second")
        self.window.on_connect_clicked()
        self.assertIsNotNone(self.window.can_bus, self.window.status_label.text())
        # The adapter is opened with the selected channel and the configuration's bit rate.
        self.assertEqual(self.bus_calls, [{"interface": "virtual", "channel": 0, "bitrate": 500000}])
        # 2.4, 2.5: database loaded before communication, newest filename date.
        self.assertTrue(self.window.app_database["source_path"].endswith("panel_2026-09-18.xml"))
        self.assertTrue(spin_until(lambda: self.window.panel.widgets["status"].text() == "ready"))
        # 2.2: repeated correctly addressed TesterPresent.
        heartbeats = [self.ecu.recv(.5), self.ecu.recv(.5)]
        self.assertTrue(all(m and m.arbitration_id == 0x7E0 and bytes(m.data) == PADDED_TESTER_PRESENT for m in heartbeats))
        # 2.3: response node beneath receiver, loss and recovery.
        self.ecu.send(can.Message(arbitration_id=0x7E8, data=[2,0x7E,0], is_extended_id=False))
        self.assertTrue(spin_until(lambda: bool(self.window.node_items)))
        node = next(iter(self.window.node_items.values()))
        self.assertIsNotNone(node.parent())
        self.assertIn("Responding", node.text(0))
        self.assertTrue(spin_until(lambda: "Lost connection" in node.text(0)))
        self.assertEqual(node.foreground(0).color().name(), "#ff0000")
        self.ecu.send(can.Message(arbitration_id=0x7E8, data=[2,0x7E,0], is_extended_id=False))
        self.assertTrue(spin_until(lambda: "Responding" in node.text(0)))
        # 3, 4, 4.2: named controls execute Python callbacks and update UI.
        self.window.panel.widgets["start"].click()
        sent = []
        def has_button_frame():
            message = self.ecu.recv(0)
            if message:
                sent.append(message)
            return any(m.arbitration_id == 0x200 and m.data[0] == 0xAB for m in sent)
        self.assertTrue(spin_until(has_button_frame))
        field = self.window.panel.widgets["input"]
        field.setText("user text")
        field.editingFinished.emit()
        self.assertTrue(spin_until(lambda: self.window.panel.widgets["status"].text() == "user text"))
        runtime = self.window.script_runtime
        worker = self.window.workers["main"]
        self.window.on_disconnect_clicked()
        self.assertFalse(worker.isRunning())
        self.assertFalse(runtime.thread.is_alive())
        self.assertIsNone(self.window.can_bus)
        self.assertTrue(self.window.connect_btn.isEnabled())

    def test_form_roundtrip_and_script_buffer(self):
        designer = FormDesigner()
        designer.database_dir = self.databases
        designer.db_id_edit.setText("saved_2026-09-18")
        designer.canvas.add_widget("value")
        button = designer.canvas.add_widget("button")
        button.update(can_id=0, data_bytes=[0xAB,0xCD], binding_value="send")
        designer.code_editor.setPlainText(SCRIPT)
        designer.design_tabs.setCurrentIndex(1)
        designer.design_tabs.setCurrentIndex(0)
        designer.design_tabs.setCurrentIndex(1)
        self.assertEqual(designer.code_editor.toPlainText(), SCRIPT)
        with patch.object(QMessageBox, "information"), patch.object(QMessageBox, "critical") as error:
            designer.save()
            error.assert_not_called()
        loaded = parse_application_database(self.databases/'saved_2026-09-18.xml')
        self.assertEqual(len(loaded["pages"][0]["values"]), 1)
        self.assertEqual(loaded["pages"][0]["buttons"][0]["data_bytes"], [0xAB,0xCD])
        self.assertEqual(loaded["pages"][0]["buttons"][0]["can_id"], 0)
        editor = PropertyEditor()
        editor.load_widget({"type":"io_box","value_type":"float"})
        editor._on_change("value_type", "integer")
        self.assertEqual(editor.get_data()["type"], "io_box")
        self.assertEqual(editor.get_data()["value_type"], "integer")
        designer.close()

    def test_missing_or_invalid_database_is_recoverable(self):
        self.window.active_config["database_family"] = "missing"
        self.window.on_connect_clicked()
        self.assertIsNone(self.window.can_bus)
        self.assertTrue(self.window.connect_btn.isEnabled())
        self.assertFalse(self.window.disconnect_btn.isEnabled())
        self.window.active_config["database_family"] = "panel"
        (self.databases/'panel_2026-09-18.xml').write_text('<broken>')
        self.window.on_connect_clicked()
        self.assertIsNone(self.window.can_bus)
        self.assertTrue(self.window.connect_btn.isEnabled())

    def test_dates_and_family_filter(self):
        (self.databases/'other_2027-01-01.xml').write_text(PANEL)
        (self.databases/'panel_2026-99-99.xml').write_text(PANEL)
        (self.databases/'panel_20260920.xml').write_text(PANEL)
        self.assertEqual(select_database(self.databases).name, 'other_2027-01-01.xml')
        self.assertEqual(select_database(self.databases,'panel').name, 'panel_20260920.xml')
        with self.assertRaises(ValueError):
            select_database(self.databases, '../outside')

    def test_bad_settings_rejected(self):
        for update in ({'tester_present_interval_seconds':0}, {'node_timeout_seconds':.01},
                       {'request_id':-1}, {'request_id':0x800}, {'node_timeout_seconds':float('nan')}):
            with self.assertRaises(ValueError):
                validate_config(dict(self.cfg, **update))

    def test_configuration_selection_survives_restart(self):
        self.window.on_config_selected(self.window.config_list.item(0))
        self.window.close()
        self.window = main.MainWindow()
        self.assertEqual(self.window.active_config['name'], 'First')

    def test_configuration_editor_persists_heartbeat_and_family(self):
        self.window._open_configuration_dialog(dict(self.cfg))
        dialog = self.window.findChild(main.ConfigurationDialog)
        dialog.name_edit.setCurrentText('Edited')
        dialog.heartbeat_spin.setValue(1.25)
        dialog.node_timeout_spin.setValue(4.5)
        dialog.database_family_edit.setText('new_family')
        dialog.response_ids_edit.setText('7E8, 7E9')
        with patch.object(QMessageBox, 'warning') as warning:
            dialog.save_config()
            warning.assert_not_called()
        saved = json.loads((self.configs/'config_Edited.json').read_text())
        self.assertEqual(saved['tester_present_interval_seconds'], 1.25)
        self.assertEqual(saved['node_timeout_seconds'], 4.5)
        self.assertEqual(saved['response_ids'], [0x7e8, 0x7e9])
        self.assertEqual(saved['database_family'], 'new_family')
        self.assertEqual(self.window.config_list.count(), 3)

    def channels(self, *channels):
        """Make can.detect_available_configs() report these channels, and list them in the window."""
        def detect(interfaces=None, timeout=None):
            return [dict(cfg) for cfg in channels if cfg["interface"] in (interfaces or [])]

        patcher = patch.object(main.can, "detect_available_configs", side_effect=detect)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window.refresh_channel_list()
        return [self.window.channel_items[can_bus.channel_key(cfg)] for cfg in channels]

    def test_the_channel_used_last_is_remembered_shown_in_bold_and_checked_at_startup(self):
        first, second = self.channels({"interface": "kvaser", "channel": 0}, {"interface": "kvaser", "channel": 1})
        self.assertFalse(first.font(0).bold())
        self.window.on_channel_selected(second)
        self.window.on_connect_clicked()                                     # channel 1 is now the one used
        self.assertIsNotNone(self.window.can_bus)
        self.window.on_disconnect_clicked()
        self.window.close()

        self.window = main.MainWindow()                                      # as if the application restarted
        items = self.window.channel_items
        self.assertEqual(self.window.selected_channel_config["channel"], 1)
        self.assertTrue(items[("kvaser", 1, "", "")].font(0).bold())
        self.assertFalse(items[("kvaser", 0, "", "")].font(0).bold())
        self.assertIsNotNone(self.window.ecu_monitor, "TesterPresent should start on the remembered channel")
        heartbeat = self.ecu.recv(1.0)
        self.assertEqual((heartbeat.arbitration_id, bytes(heartbeat.data)), (0x7E0, PADDED_TESTER_PRESENT))

    def test_a_responding_ecu_offers_its_database_for_a_double_click(self):
        channel = self.channels({"interface": "kvaser", "channel": 0})[0]
        key = can_bus.channel_key(channel.data(0, Qt.UserRole))
        self.window.check_ecus(channel.data(0, Qt.UserRole))
        self.ecu.send(can.Message(arbitration_id=0x7E8, data=[2, 0x7E, 0], is_extended_id=False))
        self.assertTrue(spin_until(lambda: self.window.database_items))
        entry = next(iter(self.window.database_items.values()))
        self.assertIn("panel_2026-09-18", entry.text(0))
        self.assertIn("double-click to load", entry.text(0))

        self.window.on_channel_double_clicked(entry)                         # the same as clicking Connect
        self.assertIsNotNone(self.window.can_bus, self.window.status_label.text())
        self.assertTrue(self.window.app_database["source_path"].endswith("panel_2026-09-18.xml"))
        self.assertIsNone(self.window.ecu_monitor, "the session takes the channel over")
        self.assertTrue(spin_until(lambda: "loaded" in self.window.database_items[key].text(0)))
        self.assertIs(self.window.database_items[key].parent(), self.window.channel_items[key])  # the tree was rebuilt

    def test_the_manual_button_opens_the_user_manual(self):
        button = self.window.manual_btn
        self.assertEqual(button.text(), "")                                  # a symbol at the top right
        self.assertFalse(button.icon().isNull())
        self.assertEqual(button.accessibleName(), "User manual")
        self.assertIs(button.parent().parent(), self.window.menuBar().cornerWidget().parent())
        manual = self.window.open_manual()
        self.addCleanup(manual.close)
        self.assertTrue(manual.isVisible())
        self.assertIn("CAN Expert", manual.browser.toPlainText())
        self.assertIs(self.window.open_manual(), manual)                     # one window, raised again

    def test_switching_theme_keeps_every_label_readable(self):
        from PyQt5.QtGui import QPalette
        from PyQt5.QtWidgets import QToolButton
        self.addCleanup(APP.setPalette, APP.palette())                       # the palette is application-wide
        button = self.window.findChild(QToolButton)                          # a toolbar button, styled by a stylesheet
        self.window.apply_theme("dark")
        self.assertLess(APP.palette().color(QPalette.Window).lightness(), 128)
        self.assertGreater(button.palette().color(QPalette.ButtonText).lightness(), 128)   # light text on dark
        self.window.apply_theme("light")
        self.assertGreater(APP.palette().color(QPalette.Window).lightness(), 128)
        self.assertLess(button.palette().color(QPalette.ButtonText).lightness(), 128)      # dark text on light

    def test_configuration_import_and_export(self):
        source = self.root / "external.json"
        source.write_text(json.dumps(dict(self.cfg, name="Imported")))
        with patch.object(main.QFileDialog, "getOpenFileName", return_value=(str(source), "")):
            self.window.import_config()
        self.assertTrue((self.configs / "config_Imported.json").exists())
        self.assertIn("Imported", [cfg["name"] for cfg in self.window.configurations])
        target = self.root / "exported.json"
        with patch.object(main.QFileDialog, "getSaveFileName", return_value=(str(target), "")):
            self.window.export_config()
        self.assertEqual(json.loads(target.read_text())["name"], self.window.active_config["name"])
        broken = self.root / "broken.json"
        broken.write_text("{ not json")
        with patch.object(main.QFileDialog, "getOpenFileName", return_value=(str(broken), "")), \
                patch.object(QMessageBox, "critical") as error:
            self.window.import_config()
            error.assert_called()
        self.assertEqual(len(self.window.configurations), 3)                 # unchanged by the bad file

    def test_extended_identifier_and_address_heartbeat(self):
        self.window.active_config.update(identifier_11_bit=False, request_id=0x18DA10F1,
                                         response_id=0x18DAF110, response_ids=[0x18DAF110],
                                         extended_id=True, extended_id_byte=0x10)
        self.window.on_connect_clicked()
        message = self.ecu.recv(.5)
        self.assertTrue(message.is_extended_id)
        self.assertEqual(message.arbitration_id, 0x18DA10F1)
        self.assertEqual(bytes(message.data), bytes([0x10,2,0x3E,0]) + b'\xcc' * 4)   # padded to 8 bytes
        self.ecu.send(can.Message(arbitration_id=0x18DAF110, data=[0xf1,2,0x7e,0], is_extended_id=True))
        self.assertTrue(spin_until(lambda: bool(self.window.node_items)))

    def test_python_loop_cancels_and_reconnects(self):
        path = self.databases/'panel_2026-09-18_script.py'
        path.write_text('def DatabaseMainFunction(api):\n    while True:\n        count = 1\n')
        self.window.on_connect_clicked()
        runtime = self.window.script_runtime
        self.assertTrue(spin_until(lambda: runtime.thread.is_alive()))
        self.window.on_disconnect_clicked()
        self.assertFalse(runtime.thread.is_alive())
        path.write_text(SCRIPT)
        self.window.on_connect_clicked()
        self.assertTrue(spin_until(lambda: self.window.panel.widgets['status'].text() == 'ready'))
        self.assertNotEqual(self.window.script_runtime, runtime)

    def test_script_timer_and_callback_errors_are_isolated(self):
        path = self.databases/'panel_2026-09-18_script.py'
        path.write_text('''def DatabaseMainFunction(api):
    api.every(0.05, lambda: api.ui.set_value("status", "tick"))
    api.on("start", lambda value: 1 / 0)
''')
        self.window.on_connect_clicked()
        self.assertTrue(spin_until(lambda: self.window.panel.widgets['status'].text() == 'tick'))
        self.window.panel.widgets['start'].click()
        self.assertTrue(spin_until(lambda: 'Script callback failed' in self.window.debug_log.toPlainText()))
        self.assertIsNotNone(self.window.can_bus)

    def test_designer_load_preserves_types_and_raw_mapping(self):
        from PyQt5.QtWidgets import QFileDialog
        source = self.databases/'roundtrip_2026-09-18.xml'
        source.write_text('''<application_database name="Roundtrip"><pages><page name="Main">
<value id="1" type="integer" label="Count" can_id="0x123" byte_start="2" byte_length="2" scale="2" offset="1"/>
<checkbox id="2" label="Enable" can_id="0x222" byte="1" bit="3"/>
</page></pages></application_database>''')
        designer = FormDesigner()
        with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(source), '')), patch.object(QMessageBox, 'critical') as error:
            designer.load()
            error.assert_not_called()
        with patch.object(QMessageBox, 'information'), patch.object(QMessageBox, 'critical') as error:
            designer.save()
            error.assert_not_called()
        page = parse_application_database(source)['pages'][0]
        self.assertEqual(page['values'][0]['value_type'], 'integer')
        self.assertEqual(page['values'][0]['byte_start'], 2)
        self.assertEqual(page['checkboxes'][0]['bit'], 3)
        designer.close()

    def test_multiple_nodes_and_close_cleanup(self):
        self.window.active_config['response_ids'] = [0x7e8, 0x7e9]
        self.window.on_connect_clicked()
        for can_id in (0x7e8, 0x7e9):
            self.ecu.send(can.Message(arbitration_id=can_id, data=[2,0x7e,0], is_extended_id=False))
        self.assertTrue(spin_until(lambda: len(self.window.node_items) == 2))
        runtime = self.window.script_runtime
        worker = self.window.workers['main']
        self.window.close()
        self.assertFalse(worker.isRunning())
        self.assertFalse(runtime.thread.is_alive())

    def test_no_stale_heartbeat_after_disconnect(self):
        self.window.on_connect_clicked()
        self.assertIsNotNone(self.ecu.recv(.5))
        self.window.on_disconnect_clicked()
        while self.ecu.recv(0):
            pass
        self.assertIsNone(self.ecu.recv(.15))

    def test_raw_controls_preserve_shared_frame_bits_and_can_id_zero(self):
        path = self.databases/'raw.xml'
        path.write_text('''<application_database><pages><page>
<checkbox label="one" can_id="0" byte="0" bit="0"/>
<checkbox label="two" can_id="0" byte="0" bit="1"/>
</page></pages></application_database>''')
        sent = []
        panel = PanelView(parse_application_database(path), lambda *args: sent.append(args), self.fail)
        panel.widgets['one'].setChecked(True)
        panel.widgets['two'].setChecked(True)
        self.assertEqual(sent[-1][0], 0)
        self.assertEqual(sent[-1][1][0], 3)
        panel.widgets['one'].setChecked(False)
        self.assertEqual(sent[-1][1][0], 2)

    def test_dbc_binding_receives_and_encodes_without_feedback(self):
        (self.databases/'test.dbc').write_text('''VERSION ""
NS_ :
BS_:
BU_: ECU
BO_ 256 Test: 2 ECU
 SG_ Speed : 0|8@1+ (1,0) [0|255] "" ECU
 SG_ Enable : 8|1@1+ (1,0) [0|1] "" ECU
VAL_ 256 Enable 0 "Off" 1 "On";
''')
        path = self.databases/'dbc.xml'
        path.write_text('''<application_database dbc_path="test.dbc"><pages><page>
<value label="display" binding_type="dbc" binding_value="Test.Speed"/>
<slider label="command" binding_type="dbc" binding_value="Test.Speed" min="0" max="255"/>
</page></pages></application_database>''')
        sent = []
        panel = PanelView(parse_application_database(path), lambda *args: sent.append(args), self.fail)
        panel.on_message(256, bytes([42,1]))
        self.assertEqual(panel.widgets['display'].text(), '42')
        self.assertEqual(panel.widgets['command'].value(), 42)
        self.assertEqual(sent, [])
        panel.widgets['command'].setValue(43)
        self.assertEqual(bytes(sent[-1][1]), bytes([43,1]))

    def test_invalid_script_cleans_up_connection(self):
        (self.databases/'panel_2026-09-18_script.py').write_text('def broken(')
        self.window.on_connect_clicked()
        self.assertIsNone(self.window.can_bus)
        self.assertEqual(self.window.workers, {})
        self.assertIsNone(self.window.script_runtime)
        self.assertTrue(self.window.connect_btn.isEnabled())

    def test_diagnostic_multi_frame_exchange(self):
        from types import SimpleNamespace
        from canexpert.diagnostic_window import DiagnosticWindow
        self.window.on_connect_clicked()
        dialog = DiagnosticWindow(self.window)
        request = bytes([0x2E, 0xF1, 0x90]) + b"WVWZZZ1KZAW000001"
        reply = bytes([0x6E, 0xF1, 0x90]) + bytes(range(10))
        dialog._current_service = SimpleNamespace(encode_request=lambda **k: request,
                                                  decode_message=lambda r: "decoded reply")
        dialog._send_request()
        state = {"total": None, "data": b"", "replied": False}
        def ecu_step():
            message = self.ecu.recv(0)
            if message and message.arbitration_id == 0x7E0:
                data = bytes(message.data)
                kind = data[0] >> 4
                if kind == 1:
                    state["total"] = ((data[0] & 0xF) << 8) | data[1]
                    state["data"] = data[2:]
                    self.ecu.send(can.Message(arbitration_id=0x7E8, data=[0x30, 0, 0], is_extended_id=False))
                elif kind == 2 and state["total"]:
                    state["data"] += data[1:]
                    if len(state["data"]) >= state["total"]:
                        self.ecu.send(can.Message(arbitration_id=0x7E8, data=bytes([0x10, len(reply)]) + reply[:6],
                                                  is_extended_id=False))
                elif kind == 3 and not state["replied"]:
                    state["replied"] = True
                    self.ecu.send(can.Message(arbitration_id=0x7E8, data=bytes([0x21]) + reply[6:],
                                              is_extended_id=False))
            return "decoded reply" in dialog.monitor_log.toPlainText()
        self.assertTrue(spin_until(ecu_step, timeout=5), dialog.monitor_log.toPlainText())
        self.assertEqual(state["data"][:state["total"]], request)
        self.assertIn(f"Response (13 bytes): {reply.hex(' ')}", dialog.monitor_log.toPlainText())
        self.assertTrue(dialog.send_btn.isEnabled())
        self.assertEqual(self.window.workers["main"].mailboxes[1:], [])

    def test_flashing_button_calls_database_flashing(self):
        from canexpert.flashing import Firmware
        item, action = self.window.flashing_toolbar_item, self.window._toolbar_actions["flashing"]
        self.assertFalse(item.isVisible())
        (self.databases/'panel_2026-09-18_script.py').write_text(SCRIPT + FLASH_SCRIPT)
        self.window.on_connect_clicked()
        self.assertTrue(item.isVisible())
        self.assertTrue(spin_until(action.isEnabled))
        firmware = Firmware("app.s19", [(0x1000, b"\x01\x02\x03"), (0x2000, b"\x04")])
        with patch.object(main.QMessageBox, "information") as information:
            self.window.start_flashing(firmware)
            self.assertFalse(action.isEnabled())
            self.assertTrue(spin_until(lambda: information.called))
        self.assertEqual(self.window.panel.widgets["status"].text(), "1000:4")
        self.assertIsNone(self.window.flash_dialog)
        self.assertTrue(action.isEnabled())
        self.window.on_disconnect_clicked()
        self.assertFalse(item.isVisible())

    def test_ecus_are_still_checked_after_disconnect(self):
        import threading
        from canexpert.simulator.ecu import DummyEcu, EcuConfig
        ecu = DummyEcu(self.ecu, EcuConfig(broadcast_interval=0), log=lambda text: None)

        def run_ecu():
            stop = threading.Event()
            thread = threading.Thread(target=ecu.serve, args=(stop,), daemon=True)
            thread.start()
            return stop, thread

        stop, thread = run_ecu()
        try:
            self.window.on_connect_clicked()
            node = lambda: next(iter(self.window.node_items.values()), None)  # noqa: E731
            self.assertTrue(spin_until(lambda: node() is not None and "Responding" in node().text(0)))
            self.window.disconnect_database()
            self.assertIsNone(self.window.can_bus)
            self.assertIsNotNone(self.window.ecu_monitor)
            channel = node().parent()
            self.assertIn("[Checking ECUs]", channel.text(0))
            for _ in range(8):                                          # 0.4 s: twice the node loss timeout
                time.sleep(0.05)
                APP.processEvents()
                self.assertIn("Responding", node().text(0))
            self.assertIn("TX  ID: 0x7E0  02 3E 00", self.window.can_log.toPlainText())
            stop.set()                                                  # the ECU goes silent
            thread.join(1)
            self.assertTrue(spin_until(lambda: "Lost connection" in node().text(0)))
            stop, thread = run_ecu()                                    # and comes back
            self.assertTrue(spin_until(lambda: "Responding" in node().text(0)))
            self.window.stop_ecu_monitor()
            self.assertIn("Not checked", node().text(0))
            self.assertNotIn("[Checking ECUs]", channel.text(0))
            self.window.check_ecus(self.window.selected_channel_config)  # right-click: Check ECUs
            self.assertTrue(spin_until(lambda: "Responding" in node().text(0)))
            self.window.on_connect_clicked()                            # the session takes over
            self.assertIsNone(self.window.ecu_monitor)
            self.assertIn("[Connected]", node().parent().text(0))             # the tree was rebuilt
        finally:
            stop.set()
            thread.join(1)

    def test_flashing_the_dummy_ecu_with_a_functional_request_id(self):
        import threading
        from canexpert.simulator.ecu import DummyEcu, EcuConfig
        from canexpert.flashing import load_firmware
        examples = Path(__file__).resolve().parent.parent / "examples"
        (self.databases/'panel_2026-09-18_script.py').write_text(
            (examples / "example_2026-09-18_script.py").read_text(encoding="utf-8"))
        self.window.active_config["request_id"] = 0x7DF                        # like the "test" configuration
        stop = threading.Event()
        ecu_bus = can.Bus(interface="virtual", channel=self.channel)
        dump = self.root / "flashed.s19"
        ecu = DummyEcu(ecu_bus, EcuConfig(erase_seconds=0.05, broadcast_interval=0, dump_path=str(dump)),
                       log=lambda text: None)
        threading.Thread(target=ecu.serve, args=(stop,), daemon=True).start()
        try:
            self.window.on_connect_clicked()
            self.assertTrue(spin_until(self.window._toolbar_actions["flashing"].isEnabled))
            firmware = load_firmware(examples / "firmware" / "demo_app.hex")
            results = []
            with patch.object(main, "report_result", lambda parent, ok, text: results.append((ok, text))):
                self.window.start_flashing(firmware)
                self.assertTrue(spin_until(lambda: results, 20))
            self.assertEqual(results, [(True, "Flashing complete")])
            self.assertEqual(load_firmware(dump).segments, firmware.segments)
        finally:
            self.window.on_disconnect_clicked()
            stop.set()
            time.sleep(0.05)
            ecu_bus.shutdown()

    def test_flashing_offers_the_built_in_sequence_when_the_script_has_no_flashing(self):
        self.window.on_connect_clicked()
        action = self.window._toolbar_actions["flashing"]
        self.assertTrue(self.window.flashing_toolbar_item.isVisible())
        self.assertTrue(spin_until(lambda: self.window.panel.widgets["status"].text() == "ready"))
        self.assertTrue(action.isEnabled(), "the built-in sequence needs no panel script")
        self.assertFalse(self.window.script_flash)
        self.assertIn("built-in sequence", action.toolTip())
        self.assertIn("does not define Flashing", action.toolTip())

    def test_the_built_in_sequence_is_what_runs_when_it_is_the_one_chosen(self):
        from canexpert.flash_sequence import FlashProfile
        from canexpert.flashing import Firmware
        self.window.on_connect_clicked()
        self.assertTrue(spin_until(lambda: self.window.panel.widgets["status"].text() == "ready"))
        firmware = Firmware("app.s19", [(0x1000, b"\x01\x02\x03")])
        started = []

        def start(runner, image, profile):        # instead of really flashing
            started.append((image, profile))
            return True

        with patch.object(main, "choose_firmware", lambda *arguments: firmware), \
                patch.object(main.FlashDialog, "exec_", lambda dialog: QDialog.Accepted), \
                patch.object(main.FlashRunner, "start", start):
            self.window.open_flashing()
        self.assertEqual(started, [(firmware, FlashProfile())])
        self.assertIsNotNone(self.window.flash_dialog, "the progress dialog carries the Cancel button")
        with patch.object(main.QMessageBox, "critical"):
            self.window._on_flash_finished(False, "Cancelled")
        self.assertIsNone(self.window.flash_dialog)
        self.assertTrue(self.window._toolbar_actions["flashing"].isEnabled())

    def test_side_panels_minimize_while_connected(self):
        side = [dock.titleBarWidget() for dock in (self.window.config_dock, self.window.channels_dock, self.window.log_dock)]
        self.window.on_connect_clicked()
        self.assertIsNotNone(self.window.can_bus)
        self.assertTrue(all(bar.is_minimized for bar in side))
        self.assertFalse(self.window.database_pane.isClosed())    # the panel is a workspace window
        self.window.on_disconnect_clicked()
        self.assertFalse(any(bar.is_minimized for bar in side))                 # back for the next connection
        side[2].minimize()                                                       # the user's own choice is kept
        self.window.on_connect_clicked()
        self.window.on_disconnect_clicked()
        self.assertEqual([bar.is_minimized for bar in side], [False, False, True])

    def test_windows11_caption_buttons(self):
        from PyQt5.QtCore import Qt
        from canexpert.ui_common import CaptionButton, SplitterPanel
        bar = self.window.channels_dock.titleBarWidget()
        self.assertIsInstance(bar.min_btn, CaptionButton)
        self.assertEqual((bar.min_btn.kind, bar.close_btn.kind), (CaptionButton.MINIMIZE, CaptionButton.CLOSE))
        self.assertEqual(bar.min_btn.size().width(), 30)
        bar.minimize()
        self.assertEqual(bar.min_btn.kind, CaptionButton.RESTORE)
        self.assertEqual(bar.min_btn.size().width(), 22)                         # fits the thin strip
        self.assertTrue(bar.close_btn.isHidden())
        bar.restore()
        self.assertEqual((bar.min_btn.kind, bar.min_btn.size().width()), (CaptionButton.MINIMIZE, 30))
        panel = SplitterPanel("Panel", QMessageBox())
        panel._minimize()
        self.assertEqual(panel._min_btn.kind, CaptionButton.RESTORE)
        for button in (bar.min_btn, bar.close_btn, panel._min_btn):              # every paint state renders
            for hovered in (False, True):
                button.setAttribute(Qt.WA_UnderMouse, hovered)
                self.assertFalse(button.grab().isNull())

    def test_the_iso_tp_settings_of_a_configuration_reach_the_session(self):
        from canexpert.transport_settings import TransportSettings, save_transport
        name = self.window.active_config["name"]
        save_transport(self.settings, name, TransportSettings(padding=False, block_size=4, st_min=0x02))
        self.window.on_connect_clicked()
        session = self.window.session_config
        self.assertEqual((session["isotp_padding"], session["isotp_block_size"], session["isotp_st_min"]),
                         (None, 4, 2))
        heartbeat = self.ecu.recv(2)
        self.assertEqual(bytes(heartbeat.data), b"\x02\x3e\x00", "padding switched off for this configuration")
        config_file = self.configs / f"config_{name}.json"
        self.assertNotIn("isotp", config_file.read_text(encoding="utf-8"))

    def test_a_listen_only_channel_sends_nothing_at_all(self):
        from canexpert.channel_setup import ChannelSetup, save_setup
        channel = self.window.selected_channel_config
        save_setup(self.settings, channel, ChannelSetup(listen_only=True))
        self.window.on_connect_clicked()
        self.assertIsNotNone(self.window.can_bus)
        self.assertIsNone(self.ecu.recv(0.4), "no TesterPresent on a listen-only channel")
        with self.assertRaises(can.CanOperationError):
            self.window.send_can_message(0x200, b"\x01")
        self.ecu.send(can.Message(arbitration_id=0x7E8, data=b"\x02\x7e\x00", is_extended_id=False))
        self.assertTrue(spin_until(lambda: "RX" in self.window.can_log.toPlainText()), "it still receives")
        self.window.on_disconnect_clicked()
        self.window.check_ecus(channel)
        self.assertIsNone(self.window.ecu_monitor, "the ECU check is TesterPresent, so it is not started")
        self.assertIn("listen-only", self.window.debug_log.toPlainText())

    def test_the_channel_setup_is_edited_from_the_channel(self):
        from canexpert.channel_setup import load_setup
        channel = {"interface": "virtual", "channel": 0}
        self.window.channel_items = {}
        def edit(dialog):                                   # the user ticking listen-only and pressing OK
            dialog.listen_only_cb.setChecked(True)
            dialog._accept()
            return dialog.Accepted
        with patch.object(main.ChannelSetupDialog, "exec_", edit):
            self.window.edit_channel_setup(channel)
        self.assertTrue(load_setup(self.settings, channel).listen_only)
        self.assertIn("[listen-only]", self.window._channel_label(channel))

    def test_the_can_monitor_has_its_own_filter_and_shows_each_frames_own_time(self):
        from canexpert.clock import absolute_text
        window, base = self.window, 1_700_000_000.0
        window.dispatch_frame(base + 0.25, "RX", 0x7E8, b"\x02\x7e\x00")
        window.dispatch_frame(base + 0.50, "TX", 0x7E0, b"\x02\x3e\x00")
        window.dispatch_frame(base + 0.75, "RX", 0x300, b"\x01")

        def lines():
            return [line for line in window.can_log.toPlainText().splitlines() if "ID: 0x" in line]

        self.assertIn(absolute_text(base + 0.25), window.can_log.toPlainText(), "the frame's time, not the drawing's")
        window.monitor_filter_bar.text_edit.setText("7E0-7EF")
        self.assertEqual([line.split("ID: ")[1].split()[0] for line in lines()], ["0x7E8", "0x7E0"],
                         "a new filter applies to what was already seen")
        window.monitor_filter_bar.direction_combo.setCurrentText("RX only")
        self.assertEqual(len(lines()), 1)
        window.dispatch_frame(base + 1.0, "RX", 0x301, b"\x02")
        self.assertEqual(len(lines()), 1, "a frame the filter stops is not added")
        window.monitor_filter_bar.mode_combo.setCurrentText("Stop")
        self.assertEqual([line.split("ID: ")[1].split()[0] for line in lines()], ["0x300", "0x301"])

    def test_every_window_counts_from_the_same_measurement_start(self):
        window = self.window
        window.on_connect_clicked()
        start = window.clock.start
        self.assertIsNotNone(start, "connecting starts the measurement")
        window.set_time_display("Relative")
        self.assertEqual(self.settings.value("time_display"), "Relative")
        engine = b"\x01\x2c\x00\x64\x00\x00\x00\x00"

        logger = window.open_can_logger()
        logger.load_dbc_from_path(Path(__file__).resolve().parents[1] / "DBC" / "dummy_ecu.dbc")
        trace = window.open_trace()
        trace.time_combo.setCurrentText("Relative")
        diagnostics = window.open_diagnostic_window()
        window.dispatch_frame(start + 1.5, "RX", 0x300, engine)
        window.dispatch_frame(start + 2.25, "RX", 0x7E8, b"\x02\x7e\x00")
        trace.flush()

        self.assertIn("1.500   RX  ID: 0x300", window.can_log.toPlainText())   # direction is right-aligned
        rows = {trace.tree.topLevelItem(row).text(2): trace.tree.topLevelItem(row).text(0)
                for row in range(trace.tree.topLevelItemCount())}
        self.assertEqual(rows["300"], "1.500000")
        series = logger._series["EngineData.Temperature"]
        self.assertAlmostEqual(float(series.t[0]), 1.5, places=6)      # the same number on the graph
        self.assertIn("2.250  RX  ID=0x7E8", diagnostics.monitor_log.toPlainText())
        window.set_time_display("Absolute")
        self.assertNotIn("1.500  ", window.can_log.toPlainText(), "the monitor is redrawn in the new display")

    def test_the_script_writes_to_its_own_window_hears_keys_and_shares_variables(self):
        from PyQt5.QtCore import QEvent
        from PyQt5.QtGui import QKeyEvent
        (self.databases/'panel_2026-09-18_script.py').write_text(SCRIPT + """
@on_start
def hello(api):
    api.log("written by the script")
    api.sysvar.set("Bench::Ready", 1)

@on_key("k")
def key(api, key):
    api.log(f"key {key}")
""")
        self.window.on_connect_clicked()
        self.assertTrue(self.window._keys_watched, "keys reach the script while the measurement runs")
        write = self.window.open_write()
        self.assertTrue(spin_until(lambda: any("written by the script" in line for line in write.lines())))
        self.assertNotIn("written by the script", self.window.debug_log.toPlainText(),
                         "the Debug log stays the application's")
        self.window.show()
        APP.processEvents()
        QApplication.sendEvent(self.window.windowHandle(), QKeyEvent(QEvent.KeyPress, Qt.Key_K, Qt.NoModifier, "k"))
        self.assertTrue(spin_until(lambda: any("key k" in line for line in write.lines())), write.lines())
        self.assertEqual(self.window.sysvars.get("Bench::Ready"), 1)
        logger = self.window.open_can_logger()                     # opened later: filled from the history
        self.assertIn("Bench::Ready", logger._items)
        self.window.on_disconnect_clicked()
        self.assertFalse(self.window._keys_watched)

    def test_default_node_loss_timing(self):
        cfg = validate_config({"name": "Defaults"})
        self.assertEqual(cfg["node_timeout_seconds"], 2.0)
        self.assertEqual(cfg["tester_present_interval_seconds"], 0.5)
        dialog = main.ConfigurationDialog(self.window, {"name": "Legacy"}, settings=self.settings)
        self.assertEqual(dialog.node_timeout_spin.value(), 2.0)
        self.assertEqual(dialog.heartbeat_spin.value(), 0.5)

    def test_tool_windows_can_be_maximized(self):
        from PyQt5.QtCore import Qt
        from canexpert.can_logger import CANLoggerWindow
        from canexpert.diagnostic_window import DiagnosticWindow
        for window in (FormDesigner(self.window), CANLoggerWindow(self.window), DiagnosticWindow(self.window)):
            flags = window.windowFlags()
            self.assertTrue(flags & Qt.WindowMaximizeButtonHint, type(window).__name__)
            self.assertTrue(flags & Qt.WindowCloseButtonHint, type(window).__name__)
            self.assertFalse(flags & Qt.WindowContextHelpButtonHint, type(window).__name__)
            window.close()

    def test_designer_widgets_reach_top_left_corner(self):
        from PyQt5.QtCore import QEvent, QPoint, Qt
        from PyQt5.QtGui import QMouseEvent

        def mouse(viewport, kind, pos, buttons):
            event = QMouseEvent(kind, pos, viewport.mapToGlobal(pos), Qt.LeftButton, buttons, Qt.NoModifier)
            APP.sendEvent(viewport, event)

        def drag(view, start, end):
            viewport = view.viewport()
            mouse(viewport, QEvent.MouseButtonPress, start, Qt.LeftButton)
            for step in range(1, 11):
                mouse(viewport, QEvent.MouseMove, start + (end - start) * step / 10, Qt.LeftButton)
            mouse(viewport, QEvent.MouseButtonRelease, end, Qt.NoButton)

        for size in ((1800, 1200), (1000, 700)):  # canvas larger and smaller than the 800 x 600 page
            designer = FormDesigner(self.window)
            designer.resize(*size)
            designer.show()
            canvas, view = designer.canvas, designer.canvas.graphics_view
            self.assertTrue(spin_until(lambda: view.mapFromScene(0, 0) == QPoint(0, 0), 1), size)
            canvas.add_widget_at("button", 200, 150)
            APP.processEvents()
            widget = canvas._current_widgets()[0]
            grab = view.mapFromScene(210, 160)
            drag(view, grab, grab - QPoint(400, 400))                  # past the top-left corner
            self.assertEqual((widget["x"], widget["y"]), (0, 0), size)
            self.assertEqual(view.mapFromScene(0, 0), QPoint(0, 0), size)
            drag(view, view.mapFromScene(10, 10), view.mapFromScene(10, 10) + QPoint(300, 300))
            self.assertEqual((widget["x"], widget["y"]), (300, 300), size)
            canvas.add_widget_at("button", 1500, 900)                  # beyond the initial page
            self.assertGreaterEqual(canvas.scene.sceneRect().right(), 1600)
            self.assertGreaterEqual(canvas.scene.sceneRect().bottom(), 1000)
            designer.close()

    def test_all_display_and_input_widget_types(self):
        path = self.databases/'controls.xml'
        path.write_text('''<application_database><pages><page>
<gauge label="gauge" min="0" max="100"/>
<progress_bar label="progress"/>
<led label="led"/>
<combo label="choice" items="one,two"/>
<text_input label="text"/>
<label text="Repeated caption"/><label text="Repeated caption"/>
</page></pages></application_database>''')
        panel = PanelView(parse_application_database(path), lambda *a: None, self.fail)
        events = []
        panel.control_changed.connect(lambda *args: events.append(args))
        panel.set_value('gauge', 35)
        panel.set_value('progress', 80)
        panel.set_value('led', True)
        self.assertEqual(panel.widgets['gauge'].value(),35)
        self.assertEqual(panel.widgets['progress'].value(),80)
        self.assertEqual(panel.widgets['led'].text(),'ON')
        panel.widgets['choice'].setCurrentText('two')
        panel.widgets['text'].setText('typed text')
        panel.widgets['text'].editingFinished.emit()
        self.assertIn(('choice','two'),events)
        self.assertIn(('text','typed text'),events)


if __name__ == '__main__':
    unittest.main()
