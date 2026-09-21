"""The Data window: every signal with the value it holds now, raw and physical."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import csv
import tempfile
import time
import unittest
from pathlib import Path

from PyQt5.QtWidgets import QApplication

from canexpert.data_window import COL_AGE, COL_COUNT, COL_PHYSICAL, COL_RAW, COL_SIGNAL, COL_UNIT, DataWindow, SignalValues
from canexpert.symbols import SymbolDatabases

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parents[1] / "DBC" / "dummy_ecu.dbc"
# EngineData: Temperature 0.1 degC per bit, Pressure 0.01 bar per bit, both big-endian.
ENGINE = b"\x01\x2c\x00\x64\x00\x00\x00\x00"


class SignalValuesTest(unittest.TestCase):
    def setUp(self):
        self.symbols = SymbolDatabases([str(DBC)], settings=None)
        self.values = SignalValues(self.symbols)

    def test_a_frame_gives_every_signal_its_physical_and_raw_value(self):
        names = self.values.add(1000.0, 0x300, ENGINE)
        self.assertEqual(sorted(names), ["EngineData.Pressure", "EngineData.Temperature"])
        rows = {row["name"]: row for row in self.values.rows(now=1000.5)}
        temperature = rows["EngineData.Temperature"]
        self.assertAlmostEqual(temperature["physical"], 30.0, places=6)   # 0x012C * 0.1
        self.assertEqual(temperature["raw"], 0x012C)                      # the bits as they arrived
        self.assertEqual(temperature["unit"], "degC")
        self.assertEqual(temperature["id"], 0x300)
        self.assertAlmostEqual(temperature["age"], 0.5, places=6)
        self.assertEqual(temperature["count"], 1)

    def test_a_frame_no_database_describes_is_ignored(self):
        self.assertEqual(self.values.add(1000.0, 0x999, b"\x01"), [])
        self.assertEqual(self.values.rows(), [])

    def test_a_frame_that_does_not_fit_the_definition_is_ignored(self):
        self.assertEqual(self.values.add(1000.0, 0x300, b"\x01"), [])      # too short for EngineData

    def test_the_newest_value_wins_and_the_count_adds_up(self):
        self.values.add(1000.0, 0x300, ENGINE)
        self.values.add(1001.0, 0x300, b"\x00\xc8\x00\x64\x00\x00\x00\x00")
        row = {row["name"]: row for row in self.values.rows(now=1001.0)}["EngineData.Temperature"]
        self.assertAlmostEqual(row["physical"], 20.0, places=6)
        self.assertEqual(row["count"], 2)
        self.assertAlmostEqual(row["age"], 0.0, places=6)


class DataWindowTest(unittest.TestCase):
    def setUp(self):
        self.symbols = SymbolDatabases([str(DBC)], settings=None)
        self.window = DataWindow(symbols=self.symbols)
        self.addCleanup(self.window.close)

    def rows(self):
        tree = self.window.tree
        return {tree.topLevelItem(row).text(COL_SIGNAL):
                [tree.topLevelItem(row).text(column) for column in range(tree.columnCount())]
                for row in range(tree.topLevelItemCount())}

    def test_a_received_signal_shows_its_value_unit_and_raw_bits(self):
        self.window.on_frame(time.time(), "RX", 0x300, ENGINE)
        self.window.refresh()
        row = self.rows()["EngineData.Temperature"]
        self.assertEqual(row[COL_PHYSICAL], "30")
        self.assertEqual(row[COL_UNIT], "degC")
        self.assertEqual(row[COL_RAW], "300")
        self.assertEqual(row[COL_COUNT], "1")
        self.assertLess(float(row[COL_AGE]), 1.0)

    def test_received_only_or_every_signal_of_the_databases(self):
        self.window.on_frame(time.time(), "RX", 0x300, ENGINE)
        self.assertEqual(sorted(self.rows()), ["EngineData.Pressure", "EngineData.Temperature"])
        self.window.received_only_cb.setChecked(False)
        self.assertEqual(sorted(self.rows()), sorted(self.symbols.signal_names()))
        self.assertEqual(self.rows()["EcuStatus.Running"][COL_PHYSICAL], "")   # never arrived
        self.window.received_only_cb.setChecked(True)
        self.assertEqual(len(self.rows()), 2)

    def test_the_filter_and_the_export(self):
        self.window.on_frame(time.time(), "RX", 0x300, ENGINE)
        self.window.filter_edit.setText("pressure")
        self.assertEqual(list(self.rows()), ["EngineData.Pressure"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "data.csv"
            self.window.export_csv(path)
            exported = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(exported[0][COL_SIGNAL], "Signal")
        self.assertEqual(len(exported), 2)
        self.window.filter_edit.setText("")
        self.window.clear()
        self.assertEqual(self.rows(), {})

    def test_a_new_database_brings_its_signals(self):
        window = DataWindow(symbols=SymbolDatabases([], settings=None))
        self.addCleanup(window.close)
        window.received_only_cb.setChecked(False)
        self.assertEqual(window.tree.topLevelItemCount(), 0)
        window.symbols.set_paths([str(DBC)])
        self.assertEqual(window.tree.topLevelItemCount(), len(window.symbols.signal_names()))


if __name__ == "__main__":
    unittest.main()
