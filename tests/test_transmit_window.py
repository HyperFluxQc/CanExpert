"""The transmit list: rows, signal editing, one-shot and cyclic sending."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtWidgets import QApplication, QDialog

from canexpert import transmit_window
from canexpert.symbols import SymbolDatabases
from canexpert.transmit_window import (COL_CYCLE, COL_DATA, COL_DLC, COL_ID, COL_ON, SETTING, SignalEditor,
                                       TransmitWindow, rows_from_json, rows_to_json)

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parents[1] / "DBC" / "dummy_ecu.dbc"


class RowFileTest(unittest.TestCase):
    def test_rows_survive_a_save_and_load(self):
        rows = rows_from_json(rows_to_json([
            {"enabled": True, "name": "Cmd", "id": 0x200, "extended": False, "data": b"\x01\x02",
             "cycle_ms": 20, "message": "", "sent": 7}]))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["name"], rows[0]["id"], rows[0]["data"], rows[0]["cycle_ms"]),
                         ("Cmd", 0x200, b"\x01\x02", 20))
        self.assertTrue(rows[0]["enabled"])
        self.assertEqual(rows[0]["sent"], 0)      # the counter is not carried over

    def test_a_broken_row_is_skipped_not_fatal(self):
        rows = rows_from_json('[{"id": "not a number"}, {"name": "Good", "id": 5, "data": "ff"}]')
        self.assertEqual([row["name"] for row in rows], ["Good"])


class TransmitWindowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat)
        self.sent = []
        self.symbols = SymbolDatabases([str(DBC)], settings=self.settings)
        self.window = TransmitWindow(symbols=self.symbols, send=self.send, settings=self.settings)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.window.close)

    def send(self, can_id, data, extended):
        self.sent.append((can_id, bytes(data), extended))

    def test_a_raw_row_can_be_edited_in_the_table(self):
        self.window.add_raw()
        self.window.table.item(0, COL_ID).setText("7DF")
        self.window.table.item(0, COL_DATA).setText("02 10 03")
        self.window.table.item(0, COL_CYCLE).setText("250")
        row = self.window.rows[0]
        self.assertEqual((row["id"], row["data"], row["cycle_ms"]), (0x7DF, b"\x02\x10\x03", 250))
        self.assertEqual(self.window.table.item(0, COL_DLC).text(), "3")
        self.assertTrue(self.settings.value(SETTING, "", type=str), "the list is remembered")

    def test_invalid_input_is_reported_and_the_row_keeps_its_value(self):
        self.window.add_raw()
        self.window.table.item(0, COL_DATA).setText("zz")
        self.assertEqual(self.window.rows[0]["data"], b"\x00")
        self.assertIn("Data", self.window.status.text())
        self.window.table.item(0, COL_DATA).setText("01 02 03 04 05 06 07 08 09")
        self.assertEqual(self.window.rows[0]["data"], b"\x00")
        self.assertIn("eight bytes", self.window.status.text())

    def test_a_database_message_brings_its_identifier_and_signals(self):
        message = next(m for m in self.symbols.messages() if m.name == "EngineData")
        with patch.object(transmit_window.MessagePicker, "exec_", lambda self: QDialog.Accepted), \
             patch.object(transmit_window.MessagePicker, "selected", lambda self: message):
            self.window.add_from_database()
        row = self.window.rows[0]
        self.assertEqual((row["name"], row["id"], row["message"]), ("EngineData", 0x300, "EngineData"))
        self.assertEqual(self.window.table.item(0, COL_ID).text(), "300")
        self.assertEqual(len(row["data"]), message.length)

        # Editing a signal re-encodes the row's bytes.
        editor = SignalEditor(message, row["data"])
        editor.widgets["Temperature"].setValue(30.0)
        editor._accept()
        self.assertEqual(editor.result(), QDialog.Accepted)
        self.assertEqual(message.decode(editor.data)["Temperature"], 30)

        def edit(dialog):                                 # stands in for the user editing and pressing OK
            dialog.widgets["Temperature"].setValue(30.0)
            dialog._accept()
            return QDialog.Accepted

        with patch.object(transmit_window.SignalEditor, "exec_", edit):
            self.window.table.selectRow(0)
            self.window.edit_signals()
        self.assertEqual(self.window.rows[0]["data"], editor.data)

    def test_send_now_sends_once(self):
        self.window.add_raw()
        self.window.rows[0].update(id=0x123, data=b"\xaa")
        self.window.table.selectRow(0)
        self.window.send_selected()
        self.assertEqual(self.sent, [(0x123, b"\xaa", False)])
        self.assertEqual(self.window.rows[0]["sent"], 1)

    def test_a_ticked_row_repeats_at_its_cycle_time(self):
        self.window.add_raw()
        self.window.rows[0].update(id=0x200, data=b"\x01", cycle_ms=20)
        self.window.table.item(0, COL_ON).setCheckState(Qt.Checked)
        self.window.tick()
        self.window.tick()                                  # not due yet
        self.assertEqual(len(self.sent), 1)
        time.sleep(0.03)
        self.window.tick()
        self.assertEqual(len(self.sent), 2)
        self.window.stop_all()
        time.sleep(0.03)
        self.window.tick()
        self.assertEqual(len(self.sent), 2, "All off stops every cyclic row")

    def test_a_row_that_cannot_be_sent_switches_itself_off(self):
        def refuse(can_id, data, extended):
            raise RuntimeError("The measurement is passive")

        window = TransmitWindow(symbols=self.symbols, send=refuse, settings=self.settings)
        self.addCleanup(window.close)
        window.add_raw()
        window.table.item(0, COL_ON).setCheckState(Qt.Checked)
        window.tick()
        self.assertFalse(window.rows[0]["enabled"], "a failing row must not repeat its error")
        self.assertIn("passive", window.status.text())

    def test_closing_the_pane_stops_every_cyclic_row(self):
        self.window.show()                                # a pane that is closed is hidden, not destroyed
        APP.processEvents()
        self.window.add_raw()
        self.window.table.item(0, COL_ON).setCheckState(Qt.Checked)
        self.window.close()
        self.assertFalse(any(row["enabled"] for row in self.window.rows))

    def test_the_list_is_restored_next_time(self):
        self.window.add_raw()
        self.window.rows[0].update(name="Wake", id=0x2A0, data=b"\x0f")
        self.window.save_rows()
        again = TransmitWindow(symbols=self.symbols, send=self.send, settings=self.settings)
        self.addCleanup(again.close)
        self.assertEqual([(row["name"], row["id"], row["data"]) for row in again.rows],
                         [("Wake", 0x2A0, b"\x0f")])


if __name__ == "__main__":
    unittest.main()
