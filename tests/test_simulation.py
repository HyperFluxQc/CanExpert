"""Simulated nodes: the messages of a database's nodes, sent as those ECUs would."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtCore import QSettings, Qt
from PyQt5.QtWidgets import QApplication, QDialog

from canexpert import simulation_window as simulation
from canexpert.cyclic import CyclicSchedule
from canexpert.simulation_window import COL_CYCLE, COL_DATA, COL_NAME, SETTING, SimulationWindow
from canexpert.symbols import SymbolDatabases

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parents[1] / "DBC" / "dummy_ecu.dbc"


class CyclicScheduleTest(unittest.TestCase):
    def test_a_key_is_due_at_its_cycle_and_not_before(self):
        schedule = CyclicSchedule()
        schedule.start("a", now=100.0)
        self.assertTrue(schedule.due("a", 0.1, now=100.0))      # due at once when switched on
        self.assertFalse(schedule.due("a", 0.1, now=100.05))
        self.assertTrue(schedule.due("a", 0.1, now=100.10))
        self.assertTrue(schedule.due("a", 0.1, now=100.20))

    def test_a_late_tick_does_not_make_the_schedule_slide(self):
        schedule = CyclicSchedule()
        schedule.start("a", now=100.0)
        schedule.due("a", 0.1, now=100.0)
        self.assertTrue(schedule.due("a", 0.1, now=100.13))     # 30 ms late
        self.assertFalse(schedule.due("a", 0.1, now=100.15))    # still due at 100.20, not 100.23
        self.assertTrue(schedule.due("a", 0.1, now=100.20))

    def test_a_long_gap_does_not_queue_up_missed_sends(self):
        schedule = CyclicSchedule()
        schedule.start("a", now=100.0)
        schedule.due("a", 0.1, now=100.0)
        self.assertTrue(schedule.due("a", 0.1, now=200.0))      # the window was frozen for 100 s
        self.assertFalse(schedule.due("a", 0.1, now=200.05))    # one send, not a thousand

    def test_keys_are_independent_and_can_be_dropped(self):
        schedule = CyclicSchedule()
        schedule.start("a", now=100.0)
        schedule.start("b", now=100.0)
        self.assertTrue(schedule.due("a", 0.1, now=100.0))
        self.assertTrue(schedule.due("b", 0.5, now=100.0))
        self.assertTrue(schedule.due("a", 0.1, now=100.1))
        self.assertFalse(schedule.due("b", 0.5, now=100.1))
        schedule.drop("a")
        self.assertEqual(len(schedule), 1)


class SimulationWindowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat)
        self.sent = []
        self.symbols = SymbolDatabases([str(DBC)], settings=self.settings)
        self.window = SimulationWindow(symbols=self.symbols, send=self.send, settings=self.settings)
        self.addCleanup(self.window.close)

    def send(self, can_id, data, extended):
        self.sent.append((can_id, bytes(data), extended))

    def nodes(self):
        tree = self.window.tree
        return {tree.topLevelItem(row).text(COL_NAME):
                [tree.topLevelItem(row).child(child).text(COL_NAME)
                 for child in range(tree.topLevelItem(row).childCount())]
                for row in range(tree.topLevelItemCount())}

    def test_the_database_gives_the_nodes_and_what_they_send(self):
        # dummy_ecu.dbc: DummyECU sends EngineData and EcuStatus.
        nodes = self.nodes()
        self.assertIn("DummyECU", nodes)
        self.assertEqual(sorted(nodes["DummyECU"]), ["EcuStatus", "EngineData"])
        entry = self.window.messages["EngineData"]
        self.assertEqual(entry["cycle_ms"], 100)                # the cycle time out of the database
        self.assertEqual(len(entry["data"]), entry["message"].length)

    def test_a_ticked_message_is_sent_at_its_cycle_time(self):
        item = self.window._items["EngineData"]
        item.setCheckState(COL_NAME, Qt.Checked)
        self.window.start_btn.setChecked(True)
        self.window.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], 0x300)
        self.window.tick()                                      # not due yet
        self.assertEqual(len(self.sent), 1)
        self.window._schedule.start("EngineData")               # as the cycle time coming round does
        self.window.tick()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.window.messages["EngineData"]["sent"], 2)

    def test_nothing_is_sent_before_start_or_after_stop(self):
        self.window._items["EngineData"].setCheckState(COL_NAME, Qt.Checked)
        self.window.tick()
        self.assertEqual(self.sent, [], "ticking a message does not send it until Start")
        self.window.start_btn.setChecked(True)
        self.window.tick()
        self.assertEqual(len(self.sent), 1)
        self.window.start_btn.setChecked(False)
        self.window._schedule.start("EngineData")
        self.window.tick()
        self.assertEqual(len(self.sent), 1)

    def test_a_whole_node_can_be_ticked_at_once(self):
        node = self.window.tree.findItems("DummyECU", Qt.MatchExactly, COL_NAME)[0]
        self.window.tree.setCurrentItem(node)
        self.window._set_node(True)
        self.assertTrue(all(self.window.messages[name]["on"] for name in ("EngineData", "EcuStatus")))
        self.window.start_btn.setChecked(True)
        self.window.tick()
        self.assertEqual(sorted(frame[0] for frame in self.sent), [0x300, 0x301])
        self.window._set_node(False)
        self.assertFalse(any(self.window.messages[name]["on"] for name in ("EngineData", "EcuStatus")))

    def test_what_a_message_carries_can_be_set_signal_by_signal(self):
        message = self.window.messages["EngineData"]["message"]

        def edit(dialog):                                       # the user setting a value and pressing OK
            dialog.widgets["Temperature"].setValue(42.0)
            dialog._accept()
            return QDialog.Accepted

        self.window.tree.setCurrentItem(self.window._items["EngineData"])
        with patch.object(simulation.SignalEditor, "exec_", edit):
            self.window.edit_signals()
        data = self.window.messages["EngineData"]["data"]
        self.assertEqual(message.decode(data)["Temperature"], 42)
        self.assertEqual(self.window._items["EngineData"].text(COL_DATA), data.hex(" ").upper())
        self.window.send_selected()
        self.assertEqual(self.sent[-1][1], data)

    def test_the_cycle_time_can_be_changed(self):
        self.window._items["EngineData"].setText(COL_CYCLE, "20")
        self.assertEqual(self.window.messages["EngineData"]["cycle_ms"], 20)
        self.window._items["EngineData"].setText(COL_CYCLE, "not a number")
        self.assertEqual(self.window.messages["EngineData"]["cycle_ms"], 20)   # kept, and said in the status
        self.assertIn("must be a number", self.window.status.text())

    def test_a_message_that_cannot_be_sent_stops_the_simulation(self):
        def refuse(can_id, data, extended):
            raise RuntimeError("Connect before sending CAN messages")

        window = SimulationWindow(symbols=self.symbols, send=refuse, settings=self.settings)
        self.addCleanup(window.close)
        window._items["EngineData"].setCheckState(COL_NAME, Qt.Checked)
        window.start_btn.setChecked(True)
        window.tick()
        self.assertFalse(window.start_btn.isChecked(), "a simulation that cannot send must not spin")
        self.assertIn("Connect before sending", window.status.text())

    def test_the_ticked_messages_are_remembered(self):
        self.window._items["EcuStatus"].setCheckState(COL_NAME, Qt.Checked)
        self.assertIn("EcuStatus", self.settings.value(SETTING, "", type=str))
        again = SimulationWindow(symbols=self.symbols, send=self.send, settings=self.settings)
        self.addCleanup(again.close)
        self.assertTrue(again.messages["EcuStatus"]["on"])
        self.assertFalse(again.messages["EngineData"]["on"])

    def test_closing_the_window_stops_the_simulation(self):
        self.window.show()
        APP.processEvents()
        self.window._items["EngineData"].setCheckState(COL_NAME, Qt.Checked)
        self.window.start_btn.setChecked(True)
        self.window.close()
        self.assertFalse(self.window.start_btn.isChecked())

    def test_a_new_database_brings_its_nodes(self):
        window = SimulationWindow(symbols=SymbolDatabases([], settings=self.settings), send=self.send,
                                  settings=self.settings)
        self.addCleanup(window.close)
        self.assertEqual(window.tree.topLevelItemCount(), 0)
        window.symbols.set_paths([str(DBC)])
        self.assertGreater(window.tree.topLevelItemCount(), 0)
        self.assertIn("EngineData", window.messages)


class TransmitPaneTest(unittest.TestCase):
    """The transmit list and the simulated nodes, as the two tabs of the Transmit window."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat)
        from canexpert.transmit_pane import TransmitPane
        self.pane = TransmitPane(symbols=SymbolDatabases([str(DBC)], settings=self.settings),
                                 send=lambda *args: None, settings=self.settings)
        self.addCleanup(self.pane.close)
        self.pane.show()
        APP.processEvents()

    def test_switching_tabs_does_not_stop_either(self):
        from canexpert.transmit_window import default_row
        self.pane.messages.rows = [default_row("Start", 0x200, b"\x01", 50)]
        self.pane.messages._fill_table()
        self.pane.messages.rows[0]["enabled"] = True
        self.pane.nodes.start_btn.setChecked(True)
        self.pane.show_nodes()
        APP.processEvents()
        self.pane.tabs.setCurrentIndex(0)
        APP.processEvents()
        self.assertTrue(self.pane.messages.rows[0]["enabled"])
        self.assertTrue(self.pane.nodes.start_btn.isChecked())
        self.pane.stop_sending()
        self.assertFalse(self.pane.messages.rows[0]["enabled"])
        self.assertFalse(self.pane.nodes.start_btn.isChecked())

    def test_escape_in_a_tab_closes_the_window_rather_than_emptying_the_tab(self):
        closed = []
        self.pane.finished.connect(closed.append)
        self.pane.messages.reject()                        # what Esc does inside the tab
        self.assertTrue(self.pane.messages.isVisibleTo(self.pane))
        self.assertEqual(closed, [QDialog.Rejected])

    def test_a_page_qt_hides_while_destroying_it_does_not_raise(self):
        # When the garbage collector frees a window, Python clears its attributes before Qt's destructor
        # hides it; an exception there surfaced as a SystemError in whatever Qt call was running.
        from PyQt5.QtGui import QHideEvent
        from canexpert.transmit_window import TransmitWindow
        symbols = SymbolDatabases([str(DBC)], settings=self.settings)
        for page in (TransmitWindow(None, symbols, lambda *args: None, self.settings),
                     SimulationWindow(None, symbols, lambda *args: None, self.settings)):
            page.__dict__.clear()
            page.hideEvent(QHideEvent())


if __name__ == "__main__":
    unittest.main()
