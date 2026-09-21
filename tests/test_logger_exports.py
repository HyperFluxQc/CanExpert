"""CAN Logger: bounded memory, statistics between the cursors, and exports (CSV both ways, MDF 4, PNG)."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import csv
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtWidgets import QApplication, QDialog

from canexpert import can_logger
from canexpert.can_logger import (COL_MAX, COL_MEAN, COL_MIN, COL_STD, EXPORT_RANGES, CANLoggerWindow, ExportDialog,
                                  _Series)
from canexpert.mdf4 import write_mdf4

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parent.parent / "DBC" / "dummy_ecu.dbc"
TEMP, PRESSURE = "EngineData.Temperature", "EngineData.Pressure"


def engine_frame(temperature, pressure):
    return int(round(temperature * 10)).to_bytes(2, "big") + int(round(pressure * 100)).to_bytes(2, "big") + bytes(4)


def read_mdf4(path):
    """The signals of an MDF 4 file as (name, unit, [(time, value), ...]): walks the blocks the way a reader
    does, so the test needs no MDF library."""
    raw = Path(path).read_bytes()
    assert raw[:8] == b"MDF     " and raw[8:12] == b"4.10", raw[:16]

    def block(address):
        kind, length, link_count = struct.unpack_from("<4s4xQQ", raw, address)
        links = struct.unpack_from(f"<{link_count}Q", raw, address + 24)
        return kind.decode(), links, raw[address + 24 + 8 * link_count:address + length]

    def text(address):
        return block(address)[2].split(b"\0", 1)[0].decode() if address else ""

    kind, links, data = block(64)
    assert kind == "##HD", kind
    start_ns = struct.unpack_from("<Q", data)[0]
    signals, group = [], links[0]
    while group:
        _, dg_links, _ = block(group)
        _, cg_links, cg_data = block(dg_links[1])
        count = struct.unpack_from("<QQ", cg_data)[1]
        _, master_links, _ = block(cg_links[1])
        _, value_links, _ = block(master_links[0])
        records = block(dg_links[2])[2]
        points = [struct.unpack_from("<dd", records, 16 * index) for index in range(count)]
        signals.append((text(value_links[2]), text(value_links[6]), points))
        group = dg_links[0]
    return start_ns, signals


class SeriesTest(unittest.TestCase):
    def test_the_oldest_quarter_goes_when_the_cap_is_reached(self):
        series = _Series(limit=1000)
        for index in range(1001):
            series.append(float(index), float(index))
        self.assertEqual(series.n, 751)                    # 1000 kept, 250 dropped, then the new one
        self.assertEqual(float(series.times()[0]), 250.0)
        self.assertEqual(float(series.times()[-1]), 1000.0)
        self.assertEqual(series.dropped, 250)

    def test_a_lower_cap_applies_at_once(self):
        series = _Series(limit=5000)
        for index in range(3000):
            series.append(float(index), 0.0)
        series.set_limit(2000)
        self.assertEqual(series.n, 2000)
        self.assertEqual(float(series.times()[0]), 1000.0)

    def test_the_samples_between_two_times(self):
        series = _Series()
        for index in range(10):
            series.append(index * 0.5, float(index))
        times, values = series.between(1.0, 2.0)
        self.assertEqual(list(times), [1.0, 1.5, 2.0])
        self.assertEqual(list(values), [2.0, 3.0, 4.0])


class LoggerTest(unittest.TestCase):
    def setUp(self):
        self.logger = CANLoggerWindow()
        self.addCleanup(self.logger.close)
        self.logger.load_dbc_from_path(DBC)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Temperature 20, 22, 24, 26 at 0, 1, 2, 3 s; pressure 1.0 then 2.0 at 0 and 2 s.
        for second, temperature, pressure in ((0, 20, 1.0), (1, 22, 1.0), (2, 24, 2.0), (3, 26, 2.0)):
            self.logger.on_can_message(0x300, engine_frame(temperature, pressure), 1_700_000_000.0 + second)

    def path(self, name):
        return Path(self.temp.name) / name

    def test_statistics_between_the_cursors(self):
        self.logger.set_signal_plotted(TEMP)
        self.logger.cursors_btn.setChecked(True)
        self.logger._cursor_pos = [2.5, 0.5]                        # either way round
        stats = self.logger.cursor_statistics(TEMP)
        self.assertEqual((stats["min"], stats["max"], stats["count"]), (22.0, 24.0, 2))
        self.assertAlmostEqual(stats["mean"], 23.0)
        self.assertAlmostEqual(stats["std"], 1.0)
        self.logger._update_cursor_readout()
        item = self.logger._items[TEMP]
        self.assertEqual([item.text(column) for column in (COL_MIN, COL_MAX, COL_MEAN, COL_STD)],
                         ["22", "24", "23", "1"])
        self.logger._cursor_pos = [10.0, 11.0]                      # nothing there
        self.assertIsNone(self.logger.cursor_statistics(TEMP))

    def test_a_long_measurement_is_kept_within_the_cap(self):
        self.logger.set_sample_limit(1000)
        for index in range(2000):
            self.logger.on_can_message(0x300, engine_frame(20, 1.0), 1_700_000_010.0 + index * 0.01)
        self.assertLessEqual(self.logger._series[TEMP].n, 1000)
        self.assertGreater(self.logger.dropped_samples(), 0)
        times = list(self.logger._series[TEMP].times())
        self.assertEqual(times, sorted(times))
        self.assertAlmostEqual(times[-1], 10.0 + 1999 * 0.01, places=6)

    def test_one_row_per_sample_for_a_time_range(self):
        written = self.logger.export(self.path("rows.csv"), "long_csv", (1.0, 2.0), [TEMP])
        self.assertEqual(written, 1)
        with open(self.path("rows.csv"), newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows, [["Time", "Signal", "Value"], ["1.0", TEMP, "22.0"], ["2.0", TEMP, "24.0"]])

    def test_one_column_per_signal_holds_each_value_until_the_next(self):
        self.logger.on_can_message(0x301, bytes([1, 0, 3, 7, 0, 0, 0, 0]), 1_700_000_000.5)   # EcuStatus at 0.5 s
        self.logger.export(self.path("wide.csv"), "wide_csv", None, [TEMP, "EcuStatus.Running"])
        with open(self.path("wide.csv"), newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["Time", f"{TEMP} [degC]", "EcuStatus.Running"])
        self.assertEqual(rows[1], ["0.0", "20.0", ""])             # Running has not been seen yet
        self.assertEqual(rows[2], ["0.5", "20.0", "1.0"])          # temperature held from 0 s
        self.assertEqual(rows[3], ["1.0", "22.0", "1.0"])
        self.assertEqual(len(rows), 1 + 5)

    def test_mdf4_holds_every_signal_with_its_unit_and_start(self):
        written = self.logger.export(self.path("run.mf4"), "mdf4", None, [TEMP, PRESSURE])
        self.assertEqual(written, 2)
        start_ns, signals = read_mdf4(self.path("run.mf4"))
        self.assertEqual(start_ns, 1_700_000_000 * 10 ** 9)          # the graph's time 0 as a time of day
        by_name = {name: (unit, points) for name, unit, points in signals}
        self.assertEqual(by_name[TEMP], ("degC", [(0.0, 20.0), (1.0, 22.0), (2.0, 24.0), (3.0, 26.0)]))
        self.assertEqual(by_name[PRESSURE][1][2], (2.0, 2.0))

    def test_mdf4_of_the_range_between_the_cursors(self):
        self.logger.cursors_btn.setChecked(True)
        self.logger._cursor_pos = [1.0, 2.0]
        self.logger.export(self.path("cut.mf4"), "mdf4", self.logger.export_range(EXPORT_RANGES[2]), [TEMP])
        _start, signals = read_mdf4(self.path("cut.mf4"))
        self.assertEqual(signals[0][2], [(1.0, 22.0), (2.0, 24.0)])

    def test_a_picture_of_the_graphs(self):
        with self.assertRaises(ValueError):
            self.logger.export(self.path("none.png"), "png")         # no graph yet
        self.logger.set_signal_plotted(TEMP)
        self.logger.export(self.path("graphs.png"), "png")
        self.assertEqual(self.path("graphs.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_the_dialog_offers_only_what_makes_sense(self):
        dialog = ExportDialog(self.logger)
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.range_combo.model().item(2).isEnabled(), "no cursors, nothing between them")
        dialog.format_combo.setCurrentText("Picture of the graphs (PNG)")
        self.assertFalse(dialog.range_combo.isEnabled())
        self.assertEqual(dialog.kind(), "png")

    def test_export_from_the_toolbar(self):
        target = self.path("from_dialog.csv")

        def choose(dialog):
            dialog.format_combo.setCurrentText("One column per signal (CSV)")
            return QDialog.Accepted

        with patch.object(ExportDialog, "exec_", choose), \
                patch.object(can_logger.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), "")):
            self.assertEqual(self.logger.show_export(), str(target))
        self.assertIn("Exported", self.logger.path_status.text())
        self.assertTrue(target.read_text().startswith("Time,"))


class Mdf4WriterTest(unittest.TestCase):
    def test_signals_without_samples_are_left_out_and_text_is_escaped(self):
        path = Path(tempfile.mkdtemp()) / "empty.mf4"
        written = write_mdf4(path, [("A", "", [], []), ("B <&>", "V", [0.0], [1.5])], start_time=0.0,
                             comment="a & b <c>")
        self.assertEqual(written, 1)
        _start, signals = read_mdf4(path)
        self.assertEqual(signals, [("B <&>", "V", [(0.0, 1.5)])])
        self.assertIn(b"a &amp; b &lt;c&gt;", path.read_bytes())
        self.assertEqual(len(path.read_bytes()) % 8, 0, "every block is 8-byte aligned")


if __name__ == "__main__":
    unittest.main()
