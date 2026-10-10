"""Requirements acceptance tests: no hardware or user settings are touched."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import queue
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtWidgets import QAction, QApplication, QDialog, QMessageBox

from canexpert import can_bus
from canexpert import main_window as main
from canexpert.channel_setup_dialog import ChannelSetupDialog
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


# Read() and Write(), which the toolbar's Read and Write run: Write() takes long enough to see the buttons greyed.
READ_WRITE_SCRIPT = '''
calls = []

def Read(api):
    calls.append("read")
    api.ui.set_value("status", f"read {len(calls)}")

def Write(api):
    api.can.send(0x2AA, [0x57])
    api.sleep(0.3)
    return False
'''

# A Flashing() that takes half a second - about eight TesterPresent intervals - sending a frame now and then.
SLOW_FLASH_SCRIPT = '''
def Flashing(api, firmware):
    for step in range(10):
        api.can.send(0x123, [step])
        api.sleep(0.05)
    return True
'''


def tester_present_frames(frames):
    return [frame for frame in frames if frame.arbitration_id == 0x7E0 and bytes(frame.data[:3]) == b"\x02\x3E\x00"]


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
        worker = self.window.worker
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
        self.window.on_channel_double_clicked(second)                        # chosen...
        self.window.on_connect_clicked()                                     # ...and used
        self.assertIsNotNone(self.window.can_bus)
        self.window.on_disconnect_clicked()
        self.window.close()

        self.window = main.MainWindow()                                      # as if the application restarted
        items = self.window.channel_items
        self.assertEqual(self.window.selected_channel_config["channel"], 1)
        self.assertTrue(items[("kvaser", 1, "", "")].font(0).bold(), "in use: its ECUs are checked")
        self.assertFalse(items[("kvaser", 0, "", "")].font(0).bold())
        self.assertIsNotNone(self.window.ecu_monitor, "TesterPresent should start on the remembered channel")
        heartbeat = self.ecu.recv(1.0)
        self.assertEqual((heartbeat.arbitration_id, bytes(heartbeat.data)), (0x7E0, PADDED_TESTER_PRESENT))

    def test_the_interface_chosen_is_in_bold_with_what_answers_on_it(self):
        self.channels({"interface": "kvaser", "channel": 0}, {"interface": "kvaser", "channel": 1})
        first, second = ("kvaser", 0, "", ""), ("kvaser", 1, "", "")
        window, items = self.window, self.window.channel_items

        def bold():
            return [key for key, item in items.items() if item.font(0).bold()]

        def under(key):
            """The lines under an interface: its ECUs and the database Connect would load."""
            return [items[key].child(index).text(0) for index in range(items[key].childCount())]

        def offered(key):
            self.ecu.send(can.Message(arbitration_id=0x7E8, data=[2, 0x7E, 0], is_extended_id=False))  # an answer
            return any("double-click to load" in line for line in under(key))

        self.assertEqual(bold(), [])
        window.on_channel_selected(items[first])                       # a click only shows it
        self.assertEqual(bold(), [])
        self.assertIn("Double-click", window.status_label.text())
        window.on_channel_double_clicked(items[first])                 # a double-click chooses it
        self.assertEqual(bold(), [first])
        self.assertEqual(window.monitor_channel["channel"], 0, "its ECUs checked")
        self.assertTrue(spin_until(lambda: offered(first)))
        self.assertTrue(any("ECU 0x7E8" in line for line in under(first)))

        window.on_channel_double_clicked(items[second])                # another interface chosen
        self.assertEqual(bold(), [second], "the one chosen before is not in bold any more...")
        self.assertEqual(under(first), [], "...nor its ECU and database under it")
        self.assertEqual(under(second), [], "nothing has answered on the new one yet")
        self.assertEqual(window.monitor_channel["channel"], 1)
        self.assertTrue(spin_until(lambda: offered(second)), "once an ECU answers, its database is offered")

        database = next(items[second].child(index) for index in range(items[second].childCount())
                        if "double-click to load" in items[second].child(index).text(0))
        window.on_channel_double_clicked(database)                     # the database line: Connect
        self.assertIsNotNone(window.can_bus)
        self.assertEqual(bold(), [second])
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.No) as question:
            window.on_channel_double_clicked(items[first])             # connected: asked first
        self.assertIn("Disconnect it and use kvaser channel 0?", question.call_args[0][2])
        self.assertIsNotNone(window.can_bus, "No: still connected")
        self.assertEqual(bold(), [second])
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes):
            window.on_channel_double_clicked(items[first])
        self.assertIsNone(window.can_bus, "Yes: disconnected...")
        self.assertEqual(bold(), [first], "...and the other one chosen")
        self.assertEqual(under(second), [])
        self.assertEqual(window.monitor_channel["channel"], 0)
        window.set_offline(True)                                       # Kill CAN: none in bold
        self.assertEqual(bold(), [])
        window.set_offline(False)                                      # back: the one chosen, checked again
        self.assertEqual(bold(), [first])
        self.assertIsNotNone(window.ecu_monitor)

    def test_a_responding_ecu_offers_its_database_for_a_double_click(self):
        channel = self.channels({"interface": "kvaser", "channel": 0})[0]
        key = can_bus.channel_key(channel.data(0, Qt.UserRole))
        self.window.on_channel_double_clicked(channel)                       # chosen: its ECUs checked
        self.ecu.send(can.Message(arbitration_id=0x7E8, data=[2, 0x7E, 0], is_extended_id=False))
        self.assertTrue(spin_until(lambda: self.window.database_items))
        entry = next(iter(self.window.database_items.values()))
        self.assertIn("panel_2026-09-18", entry.text(0))
        self.assertIn("double-click to load", entry.text(0))

        self.window.on_channel_double_clicked(entry)                         # the same as clicking Connect
        self.assertIsNotNone(self.window.can_bus, self.window.status_label.text())
        self.assertTrue(self.window.app_database["source_path"].endswith("panel_2026-09-18.xml"))
        self.assertIsNone(self.window.ecu_monitor, "the session takes the channel over")

        def loaded():
            # The ECU goes on answering the session's TesterPresent: without an answer for its timeout (0.2 s
            # here) it is lost, and its database is no longer offered - which a slow machine got to first.
            self.ecu.send(can.Message(arbitration_id=0x7E8, data=[2, 0x7E, 0], is_extended_id=False))
            item = self.window.database_items.get(key)
            return item is not None and "loaded" in item.text(0)
        self.assertTrue(spin_until(loaded))
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
        worker = self.window.worker
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
        self.assertIsNone(self.window.worker)
        self.assertIsNone(self.window.script_runtime)
        self.assertTrue(self.window.connect_btn.isEnabled())

    def test_an_odx_service_goes_out_over_several_frames_and_its_answer_is_decoded(self):
        from types import SimpleNamespace
        self.window.on_connect_clicked()
        console = self.window.open_uds_console()
        request = bytes([0x2E, 0xF1, 0x90]) + b"WVWZZZ1KZAW000001"
        reply = bytes([0x6E, 0xF1, 0x90]) + bytes(range(10))
        service = SimpleNamespace(short_name="WriteVIN", request=SimpleNamespace(parameters=[]), free_parameters=[],
                                  encode_request=lambda **k: request, decode_message=lambda r: "decoded reply")
        console.odx.set_layer(SimpleNamespace(services=[service]), "bench.odx")
        console.odx.tree.setCurrentItem(console.odx.tree.topLevelItem(0))
        self.assertTrue(console.odx.send_btn.isEnabled())
        console.odx.send_selected()
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
            return "ODX: decoded reply" in console.log.toPlainText()
        self.assertTrue(spin_until(ecu_step, timeout=5), console.log.toPlainText())
        self.assertEqual(state["data"][:state["total"]], request)
        self.assertIn(reply.hex(" ").upper(), console.log.toPlainText())
        self.assertIn("WriteVIN:", console.log.toPlainText())
        self.assertEqual(self.window.worker.mailboxes[1:], [])

    def test_the_script_reads_what_a_frame_shows_on_the_panel(self):
        dbc = Path(__file__).resolve().parents[1] / "DBC" / "dummy_ecu.dbc"
        (self.databases / "panel_2026-09-18.xml").write_text(f'''<application_database dbc_path="{dbc.as_posix()}">
<pages><page name="Main">
<value id="1" label="Temp" binding_type="dbc" binding_value="EngineData.Temperature" x="10" y="10"/>
<value id="2" label="Copy" binding_value="copy" x="10" y="50"/>
</page></pages></application_database>''')
        (self.databases / "panel_2026-09-18_script.py").write_text('''
@on_message(0x300)
def copy(api, frame):
    api.ui.set_value("copy", api.ui.get_value("Temp"))
''')
        self.window.on_connect_clicked()
        self.assertIsNotNone(self.window.script_runtime)
        for raw, shown in ((0x012C, "30 degC"), (0x0136, "31 degC")):
            self.ecu.send(can.Message(arbitration_id=0x300, data=raw.to_bytes(2, "big") + bytes(6),
                                      is_extended_id=False))
            self.assertTrue(spin_until(lambda shown=shown: self.window.panel.widgets["copy"].text() == shown),
                            self.window.panel.widgets["copy"].text())
        self.window.on_disconnect_clicked()

    def test_reflash_calls_database_flashing(self):
        from canexpert.flashing import Firmware
        item, action = self.window.toolbar_buttons.items["reflash"][1], self.window._toolbar_actions["reflash"]
        self.assertFalse(item.isVisible())
        (self.databases/'panel_2026-09-18_script.py').write_text(SCRIPT + FLASH_SCRIPT)
        self.window.on_connect_clicked()
        self.answer()
        self.assertTrue(spin_until(lambda: item.isVisible() and action.isEnabled() and self.window.script_flash))
        firmware = Firmware("app.s19", [(0x1000, b"\x01\x02\x03"), (0x2000, b"\x04")])
        with patch.object(main.QMessageBox, "information") as information:
            self.window.start_flashing(firmware)
            self.assertFalse(action.isEnabled())
            self.assertTrue(spin_until(lambda: information.called))
        self.assertEqual(self.window.panel.widgets["status"].text(), "1000:4")
        self.assertIsNone(self.window.flash_dialog)
        self.answer()
        self.assertTrue(spin_until(action.isEnabled))
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
            self.assertIn(("TX", 0x7E0, b"\x02\x3e\x00"),
                          [(frame[1], frame[2], frame[3][:3]) for frame in self.window.frame_history])
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
            self.assertTrue(spin_until(self.window._toolbar_actions["reflash"].isEnabled))   # the ECU answers
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

    def test_reflash_offers_the_built_in_sequence_when_the_script_has_no_flashing(self):
        self.window.on_connect_clicked()
        action = self.window._toolbar_actions["reflash"]
        self.assertTrue(spin_until(lambda: self.window.panel.widgets["status"].text() == "ready"))
        self.answer()
        self.assertTrue(spin_until(action.isEnabled), "the built-in sequence needs no panel script")
        self.assertTrue(self.window.toolbar_buttons.items["reflash"][1].isVisible())
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
            self.window.open_reflash()
        self.assertEqual(started, [(firmware, FlashProfile())])
        self.assertIsNotNone(self.window.flash_dialog, "the progress dialog carries the Cancel button")
        with patch.object(main.QMessageBox, "critical"):
            self.window._on_flash_finished(False, "Cancelled")
        self.assertIsNone(self.window.flash_dialog)
        self.answer()
        self.assertTrue(spin_until(self.window._toolbar_actions["reflash"].isEnabled))

    def test_read_write_and_reflash_follow_the_database_and_its_ecu(self):
        buttons, actions = self.window.toolbar_buttons, self.window._toolbar_actions
        group = ("ecu_read", "ecu_write", "reflash")
        shown = lambda: [buttons.items[name][1].isVisible() for name in group]    # noqa: E731
        usable = lambda: [actions[name].isEnabled() for name in group]            # noqa: E731
        self.assertEqual(shown(), [False] * 3, "no database")
        order = ["|" if action.isSeparator() else next(name for name, (_label, item) in buttons.items.items()
                                                       if item is action)
                 for action in self.window.findChild(main.QToolBar).actions()]
        self.assertEqual(order[:8], ["connect", "disconnect", "kill", "|", "ecu_read", "ecu_write", "reflash", "|"],
                         "a group of their own, beside Kill CAN")
        self.assertEqual([buttons.items[name][0] for name in group],
                         ["Read (database)", "Write (database)", "Reflash (database)"], "as the View menu says")
        self.assertTrue(buttons.items["flashing"][1].isVisible(), "Flashing, without a database")
        self.assertFalse(actions["flashing"].isEnabled(), "not before an interface's ECUs are checked")
        self.window.on_connect_clicked()
        self.assertEqual(shown(), [False] * 3, "the database, but no answer from its ECU yet")
        self.assertFalse(buttons.items["flashing"][1].isVisible(), "Reflash flashes now")
        self.answer()
        self.assertTrue(spin_until(lambda: shown() == [True] * 3 and usable() == [True] * 3))
        self.assertTrue(spin_until(lambda: usable() == [False] * 3), "the ECU silent: greyed")
        self.assertEqual(shown(), [True] * 3, "and still there")
        self.answer()
        self.assertTrue(spin_until(lambda: usable() == [True] * 3), "it answers again")
        self.window.set_offline(True)                                   # Kill CAN: nothing is received
        self.assertEqual((shown(), usable()), ([True] * 3, [False] * 3))
        self.window.set_offline(False)
        self.answer()
        self.assertTrue(spin_until(lambda: usable() == [True] * 3))
        self.window.on_disconnect_clicked()                             # the database closed: they go
        self.assertEqual(shown(), [False] * 3)
        self.assertTrue(buttons.items["flashing"][1].isVisible())

    def test_read_and_write_run_the_database_functions(self):
        (self.databases / 'panel_2026-09-18_script.py').write_text(SCRIPT + READ_WRITE_SCRIPT)
        self.window.on_connect_clicked()
        read, write = self.window._toolbar_actions["ecu_read"], self.window._toolbar_actions["ecu_write"]
        self.answer()
        self.assertTrue(spin_until(lambda: read.isEnabled() and self.window._script_functions == {"Read", "Write"}))
        self.assertNotIn("does not define", read.toolTip())
        runtime = self.window.script_runtime
        read.trigger()
        self.assertTrue(spin_until(lambda: self.window.panel.widgets["status"].text() == "read 1"))
        self.assertTrue(spin_until(lambda: self.window.status_label.text() == "Read complete — panel_2026-09-18.xml"))
        self.assertIs(self.window.script_runtime, runtime, "nothing newer: the script was not started again")
        self.drain()
        self.answer()
        self.assertTrue(spin_until(write.isEnabled))
        write.trigger()
        self.assertFalse(read.isEnabled() or write.isEnabled(), "greyed while Write() runs")
        self.assertTrue(spin_until(lambda: self.window.status_label.text().startswith("Write() reported failure")))
        self.assertIn("Write() reported failure", self.window.debug_log.toPlainText(), "a failure is in the Log too")
        self.assertIn(0x2AA, [frame.arbitration_id for frame in self.drain()])
        self.assertEqual([entry[1:] for entry in self.window.write_history][-2:],
                         [("info", "Read complete"), ("error", "Write() reported failure")])
        self.answer()
        self.assertTrue(spin_until(read.isEnabled))
        self.window.on_disconnect_clicked()

    def test_a_database_without_read_says_so(self):
        self.window.on_connect_clicked()                                # SCRIPT has no Read() and no Write()
        read = self.window._toolbar_actions["ecu_read"]
        self.answer()
        self.assertTrue(spin_until(lambda: read.isEnabled() and "does not define Read(api)" in read.toolTip()),
                        "greyed only when the ECU does not answer: a newer database may have one")
        read.trigger()
        self.assertTrue(spin_until(lambda: self.window.status_label.text().startswith(
            "The database script does not define Read(api)")))
        self.window.on_disconnect_clicked()

    def test_read_loads_a_newer_database_first(self):
        (self.databases / 'panel_2026-09-18_script.py').write_text(
            SCRIPT + 'def Read(api):\n    api.ui.set_value("status", "old Read")\n')
        self.window.on_connect_clicked()
        bus, worker, runtime = self.window.can_bus, self.window.worker, self.window.script_runtime
        read = self.window._toolbar_actions["ecu_read"]
        self.answer()
        self.assertTrue(spin_until(read.isEnabled))
        (self.databases / "panel_2026-10-01.xml").write_text(PANEL)
        (self.databases / "panel_2026-10-01_script.py").write_text(
            'def Read(api):\n    api.ui.set_value("status", "new Read")\n')
        read.trigger()
        self.assertTrue(self.window.app_database["source_path"].endswith("panel_2026-10-01.xml"), "the newer one")
        self.assertTrue(spin_until(lambda: self.window.panel.widgets["status"].text() == "new Read"))
        self.assertIsNot(self.window.script_runtime, runtime)
        self.assertFalse(runtime.thread.is_alive(), "the old script stopped")
        self.assertIs(self.window.can_bus, bus, "the session went on: the same adapter")
        self.assertIs(self.window.worker, worker, "and the same worker - its TesterPresent with it")
        self.assertNotIn(runtime.mailbox, worker.mailboxes, "the old script's mailbox is off the worker")
        self.assertIn("Database refreshed: ", self.window.debug_log.toPlainText())
        self.assertTrue(spin_until(lambda: self.window.status_label.text() == "Read complete — panel_2026-10-01.xml"),
                        "which database ran it")

        script = self.databases / "panel_2026-10-01_script.py"          # the one loaded, written since
        script.write_text('def Read(api):\n    api.ui.set_value("status", "edited Read")\n')
        stamp = script.stat().st_mtime + 10
        os.utime(script, (stamp, stamp))
        self.answer()
        self.assertTrue(spin_until(read.isEnabled))
        read.trigger()
        self.assertTrue(spin_until(lambda: self.window.panel.widgets["status"].text() == "edited Read"))
        self.window.on_disconnect_clicked()

    def test_a_refresh_never_goes_back_to_an_older_database(self):
        self.window.on_connect_clicked()                                # panel_2026-09-18: the newest
        runtime = self.window.script_runtime
        (self.databases / "panel_2026-09-18.xml").rename(self.databases / "panel_2026-09-18.xml.bak")
        self.assertTrue(self.window.refresh_database(), "the one loaded is used")
        self.assertIs(self.window.script_runtime, runtime, "and goes on: 2026-09-01 is older")
        self.assertTrue(self.window.app_database["source_path"].endswith("panel_2026-09-18.xml"))
        self.window.on_disconnect_clicked()

    def test_a_newer_database_that_cannot_be_loaded_leaves_the_loaded_one(self):
        self.window.on_connect_clicked()
        runtime, read = self.window.script_runtime, self.window._toolbar_actions["ecu_read"]
        self.assertTrue(spin_until(lambda: self.window.panel.widgets["status"].text() == "ready"))
        (self.databases / "panel_2026-10-01.xml").write_text(PANEL)
        (self.databases / "panel_2026-10-01_script.py").write_text("def Read(api)\n    pass\n")   # no colon
        self.answer()
        self.assertTrue(spin_until(read.isEnabled))
        read.trigger()
        self.assertTrue(self.window.app_database["source_path"].endswith("panel_2026-09-18.xml"), "it stays")
        self.assertIs(self.window.script_runtime, runtime, "its script goes on")
        dialog = self.window.problems_dialog
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.heading.text(), "<b>The panel panel_2026-10-01.xml cannot be loaded</b>")
        self.assertIn("panel_2026-10-01.xml cannot be loaded", self.window.status_label.text())
        self.assertIsNone(self.window._function_running, "Read() was not run")
        dialog.close()

        # A panel that cannot be built: the one loaded is built again, and its controls still reach its script.
        (self.databases / "panel_2026-10-01_script.py").write_text("def Read(api):\n    pass\n")
        (self.databases / "panel_2026-10-01.xml").write_text(PANEL.replace('x="10" y="50"', 'x="10" y="5O"'))
        self.answer()
        self.assertTrue(spin_until(read.isEnabled))
        read.trigger()
        self.assertTrue(self.window.app_database["source_path"].endswith("panel_2026-09-18.xml"))
        self.assertIs(self.window.script_runtime, runtime)
        self.drain()
        self.window.panel.widgets["start"].click()                     # api.on("start"): sends 0x200
        sent = []
        self.assertTrue(spin_until(lambda: sent.extend(self.drain()) or 0x200 in [m.arbitration_id for m in sent]))
        self.window.on_disconnect_clicked()

    def test_reflash_flashes_with_the_refreshed_database(self):
        from canexpert.flashing import Firmware
        self.window.on_connect_clicked()                                # SCRIPT: no Flashing()
        self.assertTrue(spin_until(lambda: self.window.panel.widgets["status"].text() == "ready"))
        self.assertFalse(self.window.script_flash)
        (self.databases / "panel_2026-10-01.xml").write_text(PANEL)
        (self.databases / "panel_2026-10-01_script.py").write_text(SCRIPT + FLASH_SCRIPT)
        firmware, offered = Firmware("app.s19", [(0x1000, b"\x01\x02\x03")]), []

        def exec_(dialog):
            offered.append(dialog.script_radio.isEnabled() and dialog.script_radio.isChecked())
            return QDialog.Accepted

        with patch.object(main, "choose_firmware", lambda *arguments: firmware), \
                patch.object(main.FlashDialog, "exec_", exec_), \
                patch.object(main.QMessageBox, "information") as information:
            self.window.open_reflash()
            self.assertEqual(offered, [True], "the newer script's Flashing(), offered first")
            self.assertTrue(spin_until(lambda: information.called))
        self.assertTrue(self.window.app_database["source_path"].endswith("panel_2026-10-01.xml"))
        self.assertEqual(self.window.panel.widgets["status"].text(), "1000:3")
        self.window.on_disconnect_clicked()

    def test_flashing_without_a_database_flashes_over_the_ecu_check(self):
        import shutil
        import threading
        from canexpert.flashing import load_firmware
        from canexpert.simulator.ecu import DummyEcu, EcuConfig
        image = self.root / "demo_app.hex"                              # its report is written beside it
        shutil.copy(Path(__file__).resolve().parent.parent / "examples" / "firmware" / "demo_app.hex", image)
        firmware, dump = load_firmware(image), self.root / "flashed.s19"
        stop = threading.Event()
        ecu_bus = can.Bus(interface="virtual", channel=self.channel)
        ecu = DummyEcu(ecu_bus, EcuConfig(erase_seconds=0.05, broadcast_interval=0, dump_path=str(dump)),
                       log=lambda text: None)
        threading.Thread(target=ecu.serve, args=(stop,), daemon=True).start()
        action, offered, results = self.window._toolbar_actions["flashing"], [], []

        def exec_(dialog):
            offered.append((dialog.script_radio.isHidden(), dialog.use_script()))
            return QDialog.Accepted

        try:
            self.assertFalse(action.isEnabled(), "no interface's ECUs checked yet")
            self.window.check_ecus(self.window.selected_channel_config)
            self.assertTrue(action.isEnabled())
            with patch.object(main, "choose_firmware", lambda *arguments: firmware), \
                    patch.object(main.FlashDialog, "exec_", exec_), \
                    patch.object(main, "report_result", lambda parent, ok, text: results.append((ok, text))):
                self.window.open_flashing()
                self.assertFalse(action.isEnabled(), "greyed while it flashes")
                self.assertTrue(spin_until(lambda: results, 20))
            self.assertTrue(results[0][0], results[0][1])
            self.assertEqual(offered, [(True, False)], "the built-in sequence: no database, no script to offer")
            self.assertEqual(load_firmware(dump).segments, firmware.segments)
            self.assertIsNotNone(self.window.ecu_monitor, "the ECU check goes on")
            self.assertTrue(action.isEnabled())
            self.window.stop_ecu_monitor()
            self.assertFalse(action.isEnabled(), "nothing to flash over")
        finally:
            stop.set()
            time.sleep(0.05)
            ecu_bus.shutdown()

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
        self.assertTrue(spin_until(lambda: any(frame[1] == "RX" for frame in self.window.frame_history)),
                        "it still receives")
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
        with patch.object(ChannelSetupDialog, "exec_", edit):
            self.window.edit_channel_setup(channel)
        self.assertTrue(load_setup(self.settings, channel).listen_only)
        self.assertIn("[listen-only]", self.window._channel_label(channel))

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
        window.dispatch_frame(start + 1.5, "RX", 0x300, engine)
        window.dispatch_frame(start + 2.25, "RX", 0x7E8, b"\x02\x7e\x00")
        trace.flush()

        rows = {trace.tree.topLevelItem(row).text(2): trace.tree.topLevelItem(row).text(0)
                for row in range(trace.tree.topLevelItemCount())}
        self.assertEqual(rows["300"], "1.500000")
        series = logger._series["EngineData.Temperature"]
        self.assertAlmostEqual(float(series.t[0]), 1.5, places=6)      # the same number on the graph
        write = window.open_write()
        window.write_message("info", "hello")
        self.assertRegex(write.lines()[-1], r"^\d+\.\d{3}  hello$", "seconds since the start in Relative")
        window.set_time_display("Absolute")
        self.assertRegex(write.lines()[-1], r"^\d\d:\d\d:\d\d\.\d{3}  hello$", "redrawn in the new display")

    def test_the_script_writes_to_its_own_window_hears_keys_and_shares_variables(self):
        from PyQt5.QtCore import QEvent
        from PyQt5.QtGui import QKeyEvent
        (self.databases/'panel_2026-09-18_script.py').write_text(SCRIPT + """
@on_start
def hello(api):
    api.log("written by the script")

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
        # A key for the Form Designer, open beside the main window, is not the panel's.
        designer = self.window.open_form_designer()
        designer.canvas.graphics_view.setFocus()
        self.assertTrue(spin_until(lambda: APP.activeWindow() is designer))
        QApplication.sendEvent(designer.windowHandle(), QKeyEvent(QEvent.KeyPress, Qt.Key_K, Qt.NoModifier, "k"))
        designer.close()
        self.window.activateWindow()
        self.assertTrue(spin_until(lambda: APP.activeWindow() is self.window))
        QApplication.sendEvent(self.window.windowHandle(), QKeyEvent(QEvent.KeyPress, Qt.Key_K, Qt.NoModifier, "k"))
        keys = lambda: sum("key k" in line for line in write.lines())    # noqa: E731
        self.assertTrue(spin_until(lambda: keys() == 2), write.lines())
        spin_until(lambda: keys() > 2, 0.3)
        self.assertEqual(keys(), 2, "the main window's two, in order: not the one for the designer between them")
        self.window.on_disconnect_clicked()
        self.assertFalse(self.window._keys_watched)

    def test_system_variables_are_nowhere_while_switched_off(self):
        from canexpert import features
        self.assertFalse(features.SYSTEM_VARIABLES, "switched off for now (canexpert/features.py)")
        self.assertIsNone(self.window.sysvars)
        self.assertNotIn("sysvars", self.window._toolbar_actions)
        self.assertNotIn("sysvars", self.window._tool_slots)
        menus = [action.text() for action in self.window.findChildren(QAction)]
        self.assertNotIn("System Variables", menus)
        (self.databases / 'panel_2026-09-18_script.py').write_text(SCRIPT + """
@on_start
def names(api):
    api.log(f"on_sysvar there: {'on_sysvar' in globals()}, api.sysvar there: {hasattr(api, 'sysvar')}")
""")
        self.window.on_connect_clicked()
        write = self.window.open_write()
        self.assertTrue(spin_until(lambda: any("on_sysvar there: False, api.sysvar there: False" in line
                                               for line in write.lines())), write.lines())

    def test_system_variables_when_switched_on(self):
        from canexpert import features
        switched = patch.object(features, "SYSTEM_VARIABLES", True)
        switched.start()
        self.addCleanup(switched.stop)
        (self.databases / 'panel_2026-09-18_script.py').write_text(SCRIPT + """
@on_start
def ready(api):
    api.sysvar.set("Bench::Ready", 1)
""")
        window = main.MainWindow()
        self.addCleanup(window.close)
        window.selected_channel_config = {"interface": "virtual", "channel": 0}
        self.assertIn("sysvars", window._toolbar_actions)
        window.on_connect_clicked()
        self.assertTrue(spin_until(lambda: window.sysvars.get("Bench::Ready") == 1))
        self.assertTrue(spin_until(lambda: any(entry[1] == "Bench::Ready" for entry in window.sysvar_history)),
                        "the change reaches the main window, queued from the script thread")
        logger = window.open_can_logger()                          # opened later: filled from the history
        self.assertIn("Bench::Ready", logger._items)
        self.assertIsNotNone(window.open_sysvars())
        window.on_disconnect_clicked()

    def test_every_page_of_the_database_is_a_window_of_its_own(self):
        two_pages = PANEL.replace("</page></pages>", '</page><page name="Body">'
                                  '<checkbox id="9" label="Door" binding_value="door" x="10" y="10"/></page></pages>')
        (self.databases / "panel_2026-09-18.xml").write_text(two_pages)
        self.window.on_connect_clicked()
        self.assertEqual(self.window.database_pane.windowTitle(), "Main")
        self.assertEqual([pane.windowTitle() for pane in self.window.page_panes], ["Body"])
        body = self.window.page_panes[0]
        self.assertIs(body.dockManager(), self.window.workspace)
        self.assertTrue(body.isTabbed(), "tabbed beside the first page, as the pages used to be")
        self.window.panel.set_value("door", True)            # one panel behind every page window
        self.assertTrue(self.window.panel.widgets["door"].isChecked())
        body.setFloating()
        self.assertTrue(body.isFloating())
        self.window.panel.page_windows[1][1].zoom_combo.setCurrentText("150 %")
        self.assertEqual(self.settings.value("panel_zoom/panel/Body"), "150 %")

        self.window.on_disconnect_clicked()
        self.assertEqual(self.window.page_panes, [])
        self.assertEqual(self.window.database_pane.windowTitle(), "Database")
        self.window.on_connect_clicked()
        self.assertEqual(self.window.panel.page_windows[1][1].page.zoom, 1.5, "the page keeps its zoom")
        self.assertTrue(self.window.page_panes[0].isFloating(), "and comes back where it was")

    def test_a_scan_runs_beside_the_measurement(self):
        import threading
        from canexpert.simulator.ecu import DummyEcu, EcuConfig
        ecu_bus = can.Bus(interface="virtual", channel=self.channel)
        self.addCleanup(ecu_bus.shutdown)
        ecu = DummyEcu(ecu_bus, EcuConfig(broadcast_interval=0), log=lambda text: None)
        stop = threading.Event()
        self.addCleanup(stop.set)
        threading.Thread(target=ecu.serve, args=(stop,), daemon=True).start()
        self.window.on_connect_clicked()                 # TesterPresent to 7E0 every 60 ms, answered on 7E8
        dialog = self.window.open_ecu_scan()
        self.addCleanup(dialog.close)
        dialog.last_edit.setText("7E2")
        dialog.sessions_cb.setChecked(False)
        dialog.identification_cb.setChecked(False)
        scanner = dialog.start()
        self.assertIsNotNone(scanner, dialog.status.text())
        self.assertTrue(spin_until(lambda: scanner.isFinished() and dialog.start_btn.isEnabled(), 10))
        rows = [[dialog.tree.topLevelItem(row).text(column) for column in range(2)]
                for row in range(dialog.tree.topLevelItemCount())]
        # The session's own TesterPresent is paused during the sweep, so its answers are not taken for
        # answers to 7E1 and 7E2.
        self.assertEqual(rows, [["7E0", "7E8"]])
        self.assertEqual(self.window.worker.mailboxes[1:], [], "the scan's mailbox is gone")
        self.window._configuration_for(dialog.responders[0])        # a configuration for the ECU found
        configuration = next(widget for widget in APP.topLevelWidgets()
                             if type(widget).__name__ == "ConfigurationDialog" and widget.isVisible())
        self.addCleanup(configuration.close)
        self.assertEqual((configuration.server_id_edit.text(), configuration.ecu_id_edit.text()), ("7E0", "7E8"))

    def answer(self):
        """The ECU answers: its node is Responding - what Read, Write and Reflash wait for."""
        self.ecu.send(can.Message(arbitration_id=0x7E8, data=[2, 0x7E, 0], is_extended_id=False))

    def drain(self):
        """The frames the ECU's bus has received and not read yet."""
        frames = []
        message = self.ecu.recv(0)
        while message is not None:
            frames.append(message)
            message = self.ecu.recv(0)
        return frames

    def assert_quiet(self, seconds=0.5):
        """Nothing reaches the bus for a while (after what was on its way has arrived)."""
        spin_until(lambda: False, 0.2)
        self.drain()
        spin_until(lambda: False, seconds)
        self.assertEqual([hex(frame.arbitration_id) for frame in self.drain()], [], "nothing sent any more")

    def test_the_kill_switch_takes_can_expert_off_the_bus_and_keeps_the_database(self):
        from canexpert.channel_setup import load_setup
        from canexpert.transmit_window import default_row
        (self.databases/'panel_2026-09-18_script.py').write_text(SCRIPT + """
@on_timer(0.05)
def beat(api):
    api.can.send(0x321, [0xBE])             # the script sends on its own

@on_stop
def goodbye(api):
    api.can.send(0x322, [0xDE, 0xAD])       # and when it stops
""")
        kill = self.window._toolbar_actions["kill"]
        self.assertEqual((kill.text(), kill.shortcut().toString()), ("Kill CAN", "Ctrl+F9"))
        self.assertTrue(kill.isCheckable() and not kill.isChecked())
        self.assertIn("The database stays loaded and its script goes on", kill.toolTip(), "the tooltip says so")
        self.window.on_connect_clicked()                             # a session, and a message every 10 ms
        write = self.window.open_write()
        messages = self.window.open_transmit().messages
        messages.rows = [default_row("Beat", 0x123, b"\x01", 10)]
        messages.rows[0]["enabled"] = True
        messages._fill_table()
        messages._sync_cyclic()
        seen = set()
        everything = {0x123, 0x321, 0x7E0}                          # the Transmit window, the script, TesterPresent
        self.assertTrue(spin_until(lambda: seen.update(f.arbitration_id for f in self.drain()) or everything <= seen))
        database, panel, runtime = self.window.app_database, self.window.panel, self.window.script_runtime

        kill.trigger()                                                # Kill CAN
        self.assertTrue(self.window.offline and kill.isChecked())
        self.assert_quiet()                                           # nothing at all goes out
        self.assertIs(self.window.app_database, database, "the database stays loaded...")
        self.assertIs((self.window.panel, self.window.script_runtime)[0], panel)
        self.assertIs(self.window.script_runtime, runtime, "...and its script runs on")
        self.assertTrue(messages.rows[0]["enabled"], "the Transmit window's message waits, switched on")
        refused = lambda: [line for line in write.lines() if "off the bus" in line]    # noqa: E731
        self.assertTrue(spin_until(refused))
        spin_until(lambda: False, 0.3)
        self.assertEqual(len(refused()), 1, "the script's refused frames said once, not at every try")
        self.assertIn("database stays loaded", self.window.status_label.text())
        channel = self.window.channel_items[can_bus.channel_key(self.window.connected_channel_config)]
        self.assertIn("[Off the bus]", channel.text(0))
        self.assertFalse(channel.font(0).bold(), "not in use: the adapter is closed")
        self.assertFalse(self.window._toolbar_actions["connect"].isEnabled())
        self.assertTrue(self.window._toolbar_actions["disconnect"].isEnabled(), "the database can still be closed")
        calls = len(self.bus_calls)                                   # nothing opens the adapter
        self.window.on_connect_clicked()
        self.window.check_ecus(self.window.selected_channel_config)
        with self.assertRaises(ValueError):
            self.window._scan_bus(self.window.selected_channel_config)
        setup = ChannelSetupDialog(self.window.selected_channel_config,
                                   load_setup(self.settings, self.window.selected_channel_config), 500000,
                                   offline=True)
        self.addCleanup(setup.close)
        self.assertFalse(setup.detect_btn.isEnabled(), "nor the bit rate search")
        self.assertEqual(len(self.bus_calls), calls)
        self.assertIsNone(self.window.ecu_monitor)

        kill.trigger()                                                # released: the same session goes on
        self.assertFalse(self.window.offline or kill.isChecked())
        self.assertEqual(len(self.bus_calls), calls + 1, "the adapter opened again")
        seen.clear()
        self.assertTrue(spin_until(lambda: seen.update(f.arbitration_id for f in self.drain()) or everything <= seen),
                        f"all of it again: {sorted(map(hex, seen))}")
        self.assertIs(self.window.app_database, database)
        self.assertIn("Back on the bus", self.window.status_label.text())
        self.assertIn("[Connected]", self.window.channel_items[
            can_bus.channel_key(self.window.connected_channel_config)].text(0))

        kill.trigger()                                                # Disconnect while off the bus
        self.window.disconnect_database()
        self.assertIsNone(self.window.can_bus)
        self.assertIsNone(self.window.app_database)
        self.assert_quiet()                                           # not @on_stop's frame, not the ECU check
        self.assertIsNone(self.window.ecu_monitor)
        again = main.MainWindow()                                     # kept: it starts off the bus
        self.addCleanup(again.close)
        self.assertTrue(again.offline and again._toolbar_actions["kill"].isChecked())
        again.selected_channel_config = self.window.selected_channel_config
        again.on_connect_clicked()
        self.assertIsNone(again.can_bus)
        kill.trigger()                                                # released without a session: the ECU check
        self.assertFalse(self.settings.value("offline", True, type=bool))
        self.window.check_ecus(self.window.selected_channel_config)
        self.assertIsNotNone(self.window.ecu_monitor)
        scan = self.window.open_ecu_scan(self.window.selected_channel_config)
        self.addCleanup(scan.close)
        self.assertIsNotNone(scan.start())
        self.window.set_offline(True)                                 # a scan and the ECU check stop too
        self.assertIsNone(self.window.ecu_monitor)
        self.assertFalse(scan.scanner.isRunning())
        self.assertEqual(scan.status.text(), "The scan stopped: CAN Expert went off the bus (Kill CAN)")
        self.assert_quiet()
        self.window.set_offline(False)

    def test_no_tester_present_while_flashing(self):
        import threading
        from canexpert.flash_sequence import FlashProfile
        from canexpert.flashing import load_firmware
        from canexpert.simulator.ecu import DummyEcu, EcuConfig
        stop = threading.Event()
        ecu_bus = can.Bus(interface="virtual", channel=self.channel)
        ecu = DummyEcu(ecu_bus, EcuConfig(erase_seconds=0.3, broadcast_interval=0), log=lambda text: None)
        threading.Thread(target=ecu.serve, args=(stop,), daemon=True).start()
        self.addCleanup(lambda: (stop.set(), time.sleep(0.05), ecu_bus.shutdown()))
        tester_present = tester_present_frames
        self.window.on_connect_clicked()
        self.assertTrue(spin_until(lambda: tester_present(self.drain())), "TesterPresent while connected")
        import shutil
        copy = self.root / "demo_app.s19"                           # its report is written beside it
        shutil.copy(Path(__file__).resolve().parent.parent / "examples" / "firmware" / "demo_app.s19", copy)
        firmware = load_firmware(copy)
        results, during = [], []
        with patch.object(main, "report_result", lambda parent, ok, text: results.append((ok, text))):
            self.window.start_built_in_flash(firmware, FlashProfile())
            self.assertTrue(spin_until(lambda: during.extend(self.drain()) or results, 30))
        self.assertTrue(results[0][0], results)
        requests = [index for index, frame in enumerate(during) if frame.arbitration_id == 0x7E0
                    and not tester_present([frame])]
        self.assertGreater(len(requests), 20, "the reflash went out")
        # From its first request to its last - before it began, a TesterPresent was still due
        self.assertEqual(tester_present(during[requests[0]:requests[-1] + 1]), [], "and nothing else: no TesterPresent")
        self.assertTrue(spin_until(lambda: tester_present(self.drain())), "TesterPresent again once it is over")
        self.window.on_disconnect_clicked()

    def test_no_tester_present_while_the_scripts_flashing_runs(self):
        from canexpert.flashing import Firmware
        (self.databases / 'panel_2026-09-18_script.py').write_text(SCRIPT + SLOW_FLASH_SCRIPT)
        self.window.on_connect_clicked()
        self.assertTrue(spin_until(lambda: tester_present_frames(self.drain())), "TesterPresent while connected")
        firmware = Firmware("app.s19", [(0x1000, b"\x01\x02\x03")])
        results, during = [], []
        with patch.object(main, "report_result", lambda parent, ok, text: results.append((ok, text))):
            self.window.start_flashing(firmware)
            self.assertTrue(spin_until(lambda: during.extend(self.drain()) or results, 10))
        self.assertEqual(results, [(True, "Flashing complete")])
        flashing = [index for index, frame in enumerate(during) if frame.arbitration_id == 0x123]
        self.assertEqual(len(flashing), 10, "Flashing() ran")
        self.assertEqual(tester_present_frames(during[flashing[0]:flashing[-1] + 1]), [],
                         "and no TesterPresent while it did")
        self.assertTrue(spin_until(lambda: tester_present_frames(self.drain())), "TesterPresent again after it")

        # A flash that cannot start - its request dropped - ends at once: the dialog closes, TesterPresent goes on.
        with patch.object(main, "report_result", lambda parent, ok, text: results.append((ok, text))), \
                patch.object(self.window.script_runtime.events, "put_nowait", side_effect=queue.Full):
            self.window.start_flashing(firmware)
        self.assertEqual(len(results), 2)
        self.assertFalse(results[1][0])
        self.assertIn("could not start", results[1][1])
        self.assertIsNone(self.window.flash_dialog)
        self.answer()
        self.assertTrue(spin_until(self.window._toolbar_actions["reflash"].isEnabled))
        self.drain()
        self.assertTrue(spin_until(lambda: tester_present_frames(self.drain())), "TesterPresent was never paused")
        self.window.on_disconnect_clicked()

    def test_the_toolbar_buttons_can_be_shown_or_hidden(self):
        from PyQt5.QtCore import Qt
        buttons = self.window.toolbar_buttons
        self.assertEqual(self.window.findChild(main.QToolBar).contextMenuPolicy(), Qt.CustomContextMenu,
                         "right-click: the ticks")
        ticks = {action.text(): action for action in buttons.menu().actions() if action.isCheckable()}
        self.assertEqual(list(ticks), [label for label, _item in buttons.items.values()], "every button, in order")
        self.assertTrue(all(action.isChecked() for action in ticks.values()))
        trace = buttons.items["trace"][1]
        ticks["Trace"].setChecked(False)
        self.assertFalse(trace.isVisible())
        self.assertTrue(self.window._toolbar_actions["trace"].isEnabled(), "still in the Tools menu, with its key")
        self.assertEqual(json.loads(self.settings.value("toolbar/hidden")), ["trace"])
        again = main.MainWindow()                                   # kept for the next start
        self.addCleanup(again.close)
        self.assertFalse(again.toolbar_buttons.items["trace"][1].isVisible())
        flashing = buttons.items["flashing"][1]                     # Flashing: no database, and ticked
        self.assertTrue(flashing.isVisible())
        buttons.set_shown("flashing", False)
        self.assertFalse(flashing.isVisible(), "unticked")
        buttons.set_shown("flashing", True)
        self.assertTrue(flashing.isVisible())
        self.window.on_connect_clicked()
        self.assertFalse(flashing.isVisible(), "a database connected: Reflash flashes")
        self.window.on_disconnect_clicked()
        self.assertTrue(flashing.isVisible())
        reflash = buttons.items["reflash"][1]                       # Reflash: the database's ECU answers, and ticked
        buttons.set_shown("reflash", False)
        self.window.on_connect_clicked()
        self.answer()
        self.assertTrue(spin_until(buttons.items["ecu_read"][1].isVisible))
        self.assertFalse(reflash.isVisible(), "unticked: not even then")
        buttons.set_shown("reflash", True)
        self.assertTrue(reflash.isVisible())
        self.window.on_disconnect_clicked()
        self.assertFalse(reflash.isVisible())
        separators = [action for action in self.window.findChild(main.QToolBar).actions() if action.isSeparator()]
        self.assertTrue(separators[0].isVisible())
        for name in ("connect", "disconnect", "kill"):                 # the first group, all hidden
            buttons.set_shown(name, False)
        self.assertFalse(separators[0].isVisible(), "no separator with nothing before it")
        buttons.show_all()
        self.assertTrue(trace.isVisible() and separators[0].isVisible())
        view = next(action.menu() for action in self.window.menuBar().actions() if action.text() == "View")
        self.assertIn("Toolbar buttons", [action.text() for action in view.actions()])

    def test_the_selected_configuration_has_an_edit_button(self):
        self.assertEqual(self.window.edit_config_btn.text(), "Edit")
        self.window.edit_config_btn.click()
        dialog = next(item for item in self.window.findChildren(main.ConfigurationDialog) if item.isVisible())
        self.assertEqual(dialog.name_edit.currentText(), "Second", "the one selected")
        dialog.close()
        self.window.on_connect_clicked()
        self.assertFalse(self.window.edit_config_btn.isEnabled(), "not the one in use")
        self.assertIsNone(self.window.edit_configuration())
        self.window.on_disconnect_clicked()
        self.assertTrue(self.window.edit_config_btn.isEnabled())

    def test_default_node_loss_timing(self):
        cfg = validate_config({"name": "Defaults"})
        self.assertEqual(cfg["node_timeout_seconds"], 2.0)
        self.assertEqual(cfg["tester_present_interval_seconds"], 0.5)
        dialog = main.ConfigurationDialog(self.window, {"name": "Legacy"}, settings=self.settings)
        self.assertEqual(dialog.node_timeout_spin.value(), 2.0)
        self.assertEqual(dialog.heartbeat_spin.value(), 0.5)

    def test_a_panel_with_typos_says_where_they_are_at_connect(self):
        panel = self.databases / "panel_2026-09-18.xml"
        panel.write_text(PANEL.replace('label="Status" binding_value="status" x="10" y="50"',
                                       'label="Status" binding_value="status" x="10" y="5O"'))
        self.window.on_connect_clicked()
        self.assertIsNone(self.window.can_bus, "the panel cannot be loaded: no connection")
        dialog = self.window.problems_dialog
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.heading.text(), "<b>The panel panel_2026-09-18.xml cannot be loaded</b>")
        problem = dialog.problems[0]
        self.assertEqual((problem.line, problem.message, problem.hint),
                         (3, 'y="5O" is not a whole number', 'Did you mean "50"?'))
        self.assertIn('y="5O" is not a whole number', self.window.debug_log.toPlainText())

        panel.write_text(PANEL)                                 # the script's syntax stops Connect too, and says where
        (self.databases / "panel_2026-09-18_script.py").write_text(SCRIPT.replace("(api):", "(api)"))
        self.window.on_connect_clicked()
        self.assertIsNone(self.window.can_bus)
        problem = dialog.problems[0]
        self.assertEqual((problem.kind, problem.line, problem.message),
                         ("script", 1, "expected ':': the script cannot start"))
        self.assertEqual(dialog.go_button.text(), "Open in Form Designer")
        dialog.go_button.click()                                # the panel opened in the Form Designer, there
        designer = self.window.form_designer
        self.assertEqual(designer.db_id_edit.text(), "panel_2026-09-18")
        self.assertIs(designer.design_tabs.currentWidget(), designer.code_page)
        self.assertEqual(designer.code_editor.textCursor().blockNumber(), 0)
        designer.close()

    def test_a_panel_that_runs_with_warnings_says_so_once_for_each_version(self):
        panel = self.databases / "panel_2026-09-18.xml"
        panel.write_text(PANEL.replace("<checkbox ", "<chekbox "))
        self.window.on_connect_clicked()
        self.assertIsNotNone(self.window.can_bus, "warnings do not stop Connect")
        dialog = self.window.problems_dialog
        self.assertEqual(dialog.heading.text(), "<b>The panel panel_2026-09-18.xml has 2 warnings</b>")
        self.assertEqual([problem.hint for problem in dialog.problems],
                         ["Did you mean <checkbox>?", ""], "the checkbox is left out, and the script's use of it")
        dialog.close()
        self.window.on_disconnect_clicked()
        self.window.on_connect_clicked()
        self.assertFalse(dialog.isVisible(), "the same files: not every Connect")
        self.window.on_disconnect_clicked()
        stamp = panel.stat().st_mtime + 10
        os.utime(panel, (stamp, stamp))                         # a new version
        self.window.on_connect_clicked()
        self.assertTrue(dialog.isVisible())

    def test_tool_windows_can_be_maximized(self):
        from PyQt5.QtCore import Qt
        from canexpert.can_logger import CANLoggerWindow
        from canexpert.uds_console import UdsConsoleWindow
        for window in (FormDesigner(self.window), CANLoggerWindow(self.window), UdsConsoleWindow(self.window)):
            flags = window.windowFlags()
            self.assertTrue(flags & Qt.WindowMaximizeButtonHint, type(window).__name__)
            self.assertTrue(flags & Qt.WindowCloseButtonHint, type(window).__name__)
            self.assertFalse(flags & Qt.WindowContextHelpButtonHint, type(window).__name__)
            window.close()

    def test_the_form_designer_is_a_window_of_its_own(self):
        from PyQt5 import sip
        from PyQt5.QtCore import QEvent, QSize, Qt
        designer = self.window.open_form_designer()
        self.assertIsNone(designer.parent(), "not owned by the main window: a taskbar button of its own")
        self.assertFalse(designer.isModal(), "the main window stays usable beside it")
        self.assertIsNone(APP.activeModalWidget())
        for hint in (Qt.WindowMinimizeButtonHint, Qt.WindowMaximizeButtonHint, Qt.WindowCloseButtonHint):
            self.assertTrue(designer.windowFlags() & hint, hint)
        designer.showMaximized()
        designer.showMinimized()
        self.assertTrue(designer.isMinimized())
        self.assertIs(self.window.open_form_designer(), designer, "opened again: the one that is open")
        self.assertFalse(designer.isMinimized(), "restored...")
        self.assertTrue(designer.isMaximized(), "...as it was: maximized")
        designer.showNormal()
        designer.resize(740, 500)
        self.assertTrue(designer.close(), "nothing unsaved: it closes without asking")
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertTrue(sip.isdeleted(designer), "closed, it is gone...")
        designer = self.window.open_form_designer()
        self.assertEqual(designer.size(), QSize(740, 500), "...and the next one opens as the last one was left")
        designer.showMaximized()
        designer.close()
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        designer = self.window.open_form_designer()
        self.assertTrue(designer.isMaximized(), "maximized too")
        # Closing CAN Expert closes it - not without asking about what is not saved, shown to ask.
        designer.canvas.add_widget_at("button", 100, 100)
        designer.showMinimized()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel) as asked:
            self.assertFalse(self.window.close(), "Cancel: neither window closes")
        asked.assert_called_once()
        self.assertTrue(designer.isVisible())
        self.assertFalse(designer.isMinimized(), "the question is not asked from the taskbar")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Discard):
            self.assertTrue(self.window.close())
        self.assertFalse(designer.isVisible())

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
            with patch.object(QMessageBox, "question", return_value=QMessageBox.Discard) as asked:
                designer.close()                                       # on screen with changes: it asks first
            asked.assert_called_once()

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
