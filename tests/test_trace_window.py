"""The Trace window: symbolic rows, filters, time modes, find and export."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import csv
import tempfile
import unittest
from pathlib import Path

from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from canexpert.symbols import SymbolDatabases
from canexpert.trace_window import COL_DATA, COL_DIR, COL_ID, COL_NAME, COL_TIME, TraceWindow, parse_filter

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parents[1] / "DBC" / "dummy_ecu.dbc"
# Never the user's own settings: these tests add and remove databases.
STORE = QSettings(str(Path(tempfile.mkdtemp()) / "settings.ini"), QSettings.IniFormat)


class FilterTest(unittest.TestCase):
    def test_identifiers_ranges_and_names(self):
        self.assertEqual(parse_filter("7E0"), ([(0x7E0, 0x7E0)], []))
        self.assertEqual(parse_filter("300-3FF"), ([(0x300, 0x3FF)], []))
        self.assertEqual(parse_filter("3FF-300"), ([(0x300, 0x3FF)], []))   # given the other way round
        self.assertEqual(parse_filter("7E0, 100-1FF, EngineData"),
                         ([(0x7E0, 0x7E0), (0x100, 0x1FF)], ["enginedata"]))
        self.assertEqual(parse_filter("  "), ([], []))


class TraceWindowTest(unittest.TestCase):
    def setUp(self):
        self.symbols = SymbolDatabases([str(DBC)], settings=STORE)
        self.trace = TraceWindow(symbols=self.symbols)
        self.addCleanup(self.trace.close)
        self.frames = [(1000.0, "RX", 0x300, b"\x01\x2c\x00\x64\x00\x00\x00\x00", False),
                       (1000.25, "TX", 0x7E0, b"\x02\x3e\x00", False),
                       (1000.5, "RX", 0x301, b"\x01\x00\x03\x07\x00\x00\x00\x00", False),
                       (1001.0, "RX", 0x18DAF110, b"\x03\x7f\x22\x31", True)]
        for frame in self.frames:
            self.trace.add_frame(*frame)
        self.trace.flush()

    def rows(self, column):
        return [self.trace.tree.topLevelItem(row).text(column) for row in range(self.trace.tree.topLevelItemCount())]

    def test_frames_become_rows_with_their_symbolic_name(self):
        self.assertEqual(self.trace.tree.topLevelItemCount(), 4)
        self.assertEqual(self.rows(COL_ID), ["300", "7E0", "301", "18DAF110x"])
        self.assertEqual(self.rows(COL_NAME), ["EngineData", "", "EcuStatus", ""])
        self.assertEqual(self.rows(COL_DIR), ["RX", "TX", "RX", "RX"])
        self.assertEqual(self.trace.tree.topLevelItem(1).text(COL_DATA), "02 3E 00")
        self.assertIn("4 frame(s)", self.trace.status.text())

    def test_a_row_opens_into_its_signals(self):
        item = self.trace.tree.topLevelItem(0)
        self.assertEqual(item.childIndicatorPolicy(), item.ShowIndicator)
        item.setExpanded(True)                       # the signals are decoded only now
        signals = {item.child(index).text(COL_NAME): item.child(index).text(COL_DATA)
                   for index in range(item.childCount())}
        self.assertEqual(set(signals), {"Temperature", "Pressure"})
        self.assertEqual(signals["Temperature"], "30 degC")   # 0x012C * 0.1
        # A frame no database describes has nothing to open.
        unknown = self.trace.tree.topLevelItem(1)
        self.assertNotEqual(unknown.childIndicatorPolicy(), unknown.ShowIndicator)

    def test_time_modes(self):
        self.trace.time_combo.setCurrentText("Relative")
        self.assertEqual(self.rows(COL_TIME), ["0.000000", "0.250000", "0.500000", "1.000000"])
        self.trace.time_combo.setCurrentText("Delta")
        self.assertEqual(self.rows(COL_TIME), ["0.000000", "0.250000", "0.250000", "0.500000"])
        self.trace.time_combo.setCurrentText("Absolute")
        # These timestamps are seconds from a recording's own zero, not times of day, and say so.
        self.assertEqual(self.rows(COL_TIME)[0], f"{self.frames[0][0]:.3f}")
        self.trace.add_frame(1_700_000_000.5, "RX", 0x200, b"\x09")
        self.trace.flush()
        self.assertEqual(len(self.rows(COL_TIME)[-1].split(":")), 3, "a time of day is shown as one")

    def test_relative_counts_from_the_start_of_the_measurement(self):
        from canexpert.clock import MeasurementClock
        clock = MeasurementClock()
        clock.begin(self.frames[0][0] - 2.0)                   # the measurement started 2 s before the first frame
        trace = TraceWindow(symbols=self.symbols, clock=clock)
        self.addCleanup(trace.close)
        for frame in self.frames:
            trace.add_frame(*frame)
        trace.flush()
        trace.time_combo.setCurrentText("Relative")
        self.assertEqual(trace.tree.topLevelItem(0).text(COL_TIME), "2.000000")

    def test_pass_and_stop_filters(self):
        self.trace.filter_edit.setText("300-3FF")
        self.assertEqual(self.rows(COL_ID), ["300", "301"])
        self.assertIn("2 of 4", self.trace.status.text())
        self.trace.filter_mode.setCurrentText("Stop")
        self.assertEqual(self.rows(COL_ID), ["7E0", "18DAF110x"])
        self.trace.filter_mode.setCurrentText("Pass")
        self.trace.filter_edit.setText("EcuStatus")           # by symbolic name
        self.assertEqual(self.rows(COL_ID), ["301"])
        self.trace.filter_edit.setText("")
        self.assertEqual(len(self.rows(COL_ID)), 4)

    def test_pause_keeps_recording_but_freezes_the_view(self):
        self.trace.pause_btn.setChecked(True)
        self.trace.add_frame(1002.0, "RX", 0x200, b"\x09")
        self.trace.flush()
        self.assertEqual(self.trace.tree.topLevelItemCount(), 4)
        self.assertEqual(len(self.trace.frames), 5)
        self.trace.pause_btn.setChecked(False)                # resuming shows what arrived meanwhile
        self.assertEqual(self.trace.tree.topLevelItemCount(), 5)

    def test_find_wraps_and_reports_a_miss(self):
        self.trace.search_edit.setText("EcuStatus")
        found = self.trace.find_next()
        self.assertIsNotNone(found)
        self.assertEqual(found.text(COL_ID), "301")
        self.assertFalse(self.trace.follow_btn.isChecked())   # a found row stays in view
        self.assertIsNotNone(self.trace.find_next(), "the search wraps around")
        self.trace.search_edit.setText("nothing like this")
        self.assertIsNone(self.trace.find_next())
        self.assertIn("not found", self.trace.status.text())

    def test_export_writes_the_rows_now_shown(self):
        self.trace.filter_edit.setText("300")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trace.csv"
            self.trace.export_csv(path)
            rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(rows[0], ["Time", "Direction", "ID", "Name", "DLC", "Data"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][2:4], ["300", "EngineData"])

    def test_clear_and_identifier_colours(self):
        first = self.trace.tree.topLevelItem(0).foreground(COL_ID).color().name()
        same = self.trace._colour(0x300).name()
        self.assertEqual(first, same)
        self.assertNotEqual(self.trace._colour(0x300).name(), self.trace._colour(0x301).name())
        self.trace.clear()
        self.assertEqual(self.trace.tree.topLevelItemCount(), 0)
        self.assertEqual(len(self.trace.frames), 0)

    def test_new_databases_make_the_rows_symbolic(self):
        plain = TraceWindow(symbols=SymbolDatabases([], settings=STORE))
        self.addCleanup(plain.close)
        plain.add_frame(*self.frames[0])
        plain.flush()
        self.assertEqual(plain.tree.topLevelItem(0).text(COL_NAME), "")
        plain.symbols.set_paths([str(DBC)])                   # changed() rebuilds the rows
        self.assertEqual(plain.tree.topLevelItem(0).text(COL_NAME), "EngineData")


class SymbolDatabaseTest(unittest.TestCase):
    def test_messages_signals_and_decoding(self):
        symbols = SymbolDatabases([str(DBC)], settings=STORE)
        self.assertEqual([message.name for message in symbols.messages()], ["EcuStatus", "EngineData"])
        self.assertEqual(symbols.name(0x300), "EngineData")
        self.assertEqual(symbols.name(0x999), "")
        self.assertIn("EngineData.Temperature", symbols.signal_names())
        self.assertEqual(symbols.unit("EngineData.Temperature"), "degC")
        self.assertEqual(symbols.decode(0x300, b"\x01\x2c\x00\x64\x00\x00\x00\x00")["Temperature"], 30)
        self.assertEqual(symbols.decode(0x999, b"\x00"), {})

    def test_a_file_that_cannot_be_read_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as folder:
            broken = Path(folder) / "broken.dbc"
            broken.write_text("this is not a database", encoding="utf-8")
            symbols = SymbolDatabases([str(broken), str(DBC)], settings=STORE)
        self.assertEqual(len(symbols.errors), 1)
        self.assertIn("broken.dbc", symbols.errors[0])
        self.assertTrue(symbols)                              # the good file is still loaded
        self.assertEqual(symbols.name(0x300), "EngineData")

    def test_adding_and_removing_keeps_the_list_unique(self):
        symbols = SymbolDatabases([], settings=STORE)
        changes = []
        symbols.changed.connect(lambda: changes.append(len(symbols.paths)))
        symbols.add(str(DBC), remember=False)
        symbols.add(str(DBC), remember=False)
        self.assertEqual(symbols.paths, [str(DBC)])
        symbols.remove(str(DBC), remember=False)
        self.assertEqual(symbols.paths, [])
        self.assertEqual(changes, [1, 1, 0])


if __name__ == "__main__":
    unittest.main()
