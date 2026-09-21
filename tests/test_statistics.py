"""Statistics: counts per identifier, rates, cycle times, bus load, error frames and the bus state."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtWidgets import QApplication

from canexpert import can_bus
from canexpert.statistics_window import (COL_COUNT, COL_CYCLE, COL_ID, COL_MAX, COL_MIN, COL_NAME,
                                         Statistics, StatisticsWindow, frame_bits)
from canexpert.symbols import SymbolDatabases

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parents[1] / "DBC" / "dummy_ecu.dbc"


class FrameBitsTest(unittest.TestCase):
    def test_a_frame_costs_its_fields_its_data_and_the_stuffing(self):
        # 47 bits of fields plus 8 per data byte, plus the worst case for bit stuffing.
        self.assertEqual(frame_bits(0), 47 + (47 - 13) // 4)
        self.assertEqual(frame_bits(8), 47 + 64 + (47 + 64 - 13) // 4)
        self.assertGreater(frame_bits(8, extended=True), frame_bits(8))   # 29-bit identifiers cost more

    def test_a_full_bus_is_a_hundred_percent(self):
        statistics = Statistics(window=1.0)
        # 500 kbit/s, one 8-byte frame every 250 us: that is more than the bus can carry.
        for index in range(4000):
            statistics.add(1000.0 + index * 0.00025, "RX", 0x100, bytes(8))
        self.assertGreater(statistics.bus_load(500000, now=1001.0), 100.0)


class StatisticsTest(unittest.TestCase):
    def setUp(self):
        self.statistics = Statistics(window=1.0)

    def test_counts_rates_and_cycle_times_per_identifier(self):
        for index in range(11):                      # every 100 ms for a second
            self.statistics.add(1000.0 + index * 0.1, "RX", 0x300, b"\x01\x02")
        self.statistics.add(1000.5, "TX", 0x7E0, b"\x02\x3e\x00")
        rows = {row["id"]: row for row in self.statistics.rows(now=1001.0)}
        self.assertEqual(rows[0x300]["count"], 11)
        self.assertEqual(rows[0x300]["direction"], "RX")
        self.assertAlmostEqual(rows[0x300]["cycle"], 100.0, places=3)
        self.assertAlmostEqual(rows[0x300]["min"], 100.0, places=3)
        self.assertAlmostEqual(rows[0x300]["max"], 100.0, places=3)
        self.assertAlmostEqual(rows[0x300]["rate"], 11.0, places=3)      # within the one-second window
        self.assertEqual(rows[0x7E0]["direction"], "TX")

    def test_a_jittery_cycle_shows_its_spread(self):
        for timestamp in (1000.0, 1000.1, 1000.35, 1000.4):
            self.statistics.add(timestamp, "RX", 0x200, b"\x00")
        row = self.statistics.rows(now=1000.5)[0]
        self.assertAlmostEqual(row["min"], 50.0, places=3)
        self.assertAlmostEqual(row["max"], 250.0, places=3)
        self.assertAlmostEqual(row["cycle"], (100 + 250 + 50) / 3, places=3)

    def test_rates_are_read_against_the_newest_frame_when_the_data_is_not_live(self):
        # A replayed file is timed by the clock of the recording; measuring against today's clock
        # would show every rate as zero.
        self.statistics.add(1000.0, "RX", 0x100, b"")
        self.statistics.add(1000.5, "RX", 0x100, b"")
        self.assertAlmostEqual(self.statistics.reference_time(), 1000.5, places=3)
        self.assertGreater(self.statistics.rows()[0]["rate"], 0.0)

    def test_the_rate_follows_the_bus_rather_than_averaging_from_the_start(self):
        for index in range(50):                      # a busy first second
            self.statistics.add(1000.0 + index * 0.02, "RX", 0x100, b"\x00")
        self.assertAlmostEqual(self.statistics.rows(now=1001.0)[0]["rate"], 50.0, places=3)
        # ... and nothing for the next ten seconds
        self.assertEqual(self.statistics.rows(now=1011.0)[0]["rate"], 0.0)
        self.assertEqual(self.statistics.rows(now=1011.0)[0]["count"], 50)   # the total is still there

    def test_both_directions_of_one_identifier(self):
        self.statistics.add(1000.0, "RX", 0x123, b"\x01")
        self.statistics.add(1000.1, "TX", 0x123, b"\x02")
        self.assertEqual(self.statistics.rows(now=1000.2)[0]["direction"], "RX/TX")

    def test_reset_clears_everything(self):
        self.statistics.add(1000.0, "RX", 0x100, b"\x00")
        self.statistics.error_frames = 3
        self.statistics.reset()
        self.assertEqual((self.statistics.rows(), self.statistics.total, self.statistics.error_frames), ([], 0, 0))


class StatisticsWindowTest(unittest.TestCase):
    def setUp(self):
        self.bitrate = 500000
        self.window = StatisticsWindow(symbols=SymbolDatabases([str(DBC)], settings=None),
                                       bitrate=lambda: self.bitrate)
        self.addCleanup(self.window.close)

    def rows(self):
        tree = self.window.tree
        return [[tree.topLevelItem(row).text(column) for column in range(tree.columnCount())]
                for row in range(tree.topLevelItemCount())]

    def feed(self, count=10, can_id=0x300, start=1000.0, step=0.1):
        for index in range(count):
            self.window.on_frame(start + index * step, "RX", can_id, b"\x01\x2c\x00\x64\x00\x00\x00\x00")

    def test_a_row_shows_the_identifier_its_name_and_its_numbers(self):
        self.feed()
        self.window.refresh()
        row = self.rows()[0]
        self.assertEqual(row[COL_ID], "300")
        self.assertEqual(row[COL_NAME], "EngineData")        # from the symbol databases
        self.assertEqual(row[COL_COUNT], "10")
        self.assertTrue(row[COL_CYCLE].startswith("100."), row[COL_CYCLE])
        self.assertEqual(row[COL_MIN], row[COL_MAX])

    def test_the_totals_report_the_bus_load_and_the_error_frames(self):
        self.feed(count=100, step=0.001)                      # 100 frames in 100 ms
        self.window.on_error_frame(1000.5)
        self.window.on_bus_status({"state": "error passive", "error_frames": 1})
        self.window.refresh()
        totals = self.window.totals.text()
        self.assertIn("100 frame(s)", totals)
        self.assertIn("bus load", totals)
        self.assertIn("1 error frame(s)", totals)
        self.assertIn("error passive", totals)

    def test_without_a_bit_rate_the_load_says_so_instead_of_lying(self):
        self.bitrate = 0
        self.feed()
        self.window.refresh()
        self.assertIn("set a bit rate", self.window.totals.text())

    def test_the_filter_and_the_export(self):
        self.feed(can_id=0x300)
        self.feed(can_id=0x7E0)
        self.window.refresh()
        self.assertEqual(len(self.rows()), 2)
        self.window.filter_edit.setText("EngineData")         # by name
        self.assertEqual([row[COL_ID] for row in self.rows()], ["300"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "statistics.csv"
            self.window.export_csv(path)
            exported = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(exported[0][COL_ID], "ID")
        self.assertEqual(len(exported), 2)                    # the header and the row shown
        self.window.filter_edit.setText("")
        self.window.reset()
        self.assertEqual(self.rows(), [])

    def test_freeze_stops_the_table_but_not_the_counting(self):
        self.feed()
        self.window.refresh()
        self.window.pause_cb.setChecked(True)
        self.feed(can_id=0x400)
        self.window.refresh()
        self.assertEqual(len(self.rows()), 1)
        self.window.pause_cb.setChecked(False)
        self.window.refresh()
        self.assertEqual(len(self.rows()), 2)                 # both were counted meanwhile


class ErrorFrameTest(unittest.TestCase):
    """The worker reports error frames and the adapter's state instead of dropping them."""

    def test_an_error_frame_is_counted_not_delivered_as_data(self):
        bus = can.Bus(interface="virtual", channel="stats-errors")
        other = can.Bus(interface="virtual", channel="stats-errors")
        self.addCleanup(bus.shutdown)
        self.addCleanup(other.shutdown)
        config = {"request_id": 0x7E0, "identifier_11_bit": True, "tester_present_interval_seconds": 10,
                  "response_ids": [0x7E8]}
        worker = can_bus.CanWorker(bus, config, tester_present=False)
        frames, errors = [], []
        worker.message_received.connect(frames.append)
        worker.error_frame.connect(errors.append)
        worker.start()
        self.addCleanup(worker.stop)
        other.send(can.Message(is_error_frame=True, arbitration_id=0, is_extended_id=False))
        other.send(can.Message(arbitration_id=0x300, data=b"\x01", is_extended_id=False))
        deadline = __import__("time").monotonic() + 10
        while __import__("time").monotonic() < deadline and not (frames and errors):
            APP.processEvents()
        self.assertEqual(len(errors), 1, "the error frame was not reported")
        self.assertEqual([frame["arbitration_id"] for frame in frames], [0x300])
        self.assertEqual(worker.error_frames, 1)

    def test_the_state_of_an_adapter_that_does_not_report_one(self):
        bus = can.Bus(interface="virtual", channel="stats-state")
        self.addCleanup(bus.shutdown)
        worker = can_bus.CanWorker(bus, {"request_id": 0x7E0}, tester_present=False)
        self.assertEqual(worker.state(), "error active")      # python-can's virtual bus reports one
        with patch.object(type(bus), "state", property(lambda self: can.BusState.ERROR)):
            self.assertEqual(worker.state(), "bus off")
        with patch.object(type(bus), "state", property(lambda self: can.BusState.PASSIVE)):
            self.assertEqual(worker.state(), "error passive")
        def refuse(self):
            raise NotImplementedError("this interface does not report a state")
        with patch.object(type(bus), "state", property(refuse)):
            self.assertEqual(worker.state(), "unknown")


if __name__ == "__main__":
    unittest.main()
