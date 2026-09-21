"""Frame filters: identifiers, ranges, names and direction, as the Trace applies them."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest

from PyQt5.QtWidgets import QApplication

from canexpert.clock import absolute_text
from canexpert.frame_filter import FrameFilter
from canexpert.trace_window import COL_ID, TraceWindow

APP = QApplication.instance() or QApplication([])


class FrameFilterTest(unittest.TestCase):
    def test_pass_shows_what_matches_and_stop_hides_it(self):
        passing = FrameFilter.from_text("7E0-7EF, Engine")
        self.assertTrue(passing.passes("RX", 0x7E8))
        self.assertTrue(passing.passes("RX", 0x300, "EngineData"))
        self.assertFalse(passing.passes("RX", 0x300, "Body"))
        stopping = FrameFilter.from_text("7E0-7EF", mode="Stop")
        self.assertFalse(stopping.passes("RX", 0x7E8))
        self.assertTrue(stopping.passes("RX", 0x300))

    def test_the_direction_applies_on_top_of_either(self):
        rx = FrameFilter.from_text("", direction="RX only")
        self.assertTrue(rx.passes("RX", 0x100))
        self.assertFalse(rx.passes("TX", 0x100))
        tx_stop = FrameFilter.from_text("7E0", mode="Stop", direction="TX only")
        self.assertFalse(tx_stop.passes("TX", 0x7E0))
        self.assertTrue(tx_stop.passes("TX", 0x100))
        self.assertFalse(tx_stop.passes("RX", 0x100))

    def test_no_filter_at_all_is_empty(self):
        self.assertTrue(FrameFilter().empty)
        self.assertFalse(FrameFilter.from_text("", direction="TX only").empty)


class TraceDirectionTest(unittest.TestCase):
    def test_the_trace_can_show_one_direction(self):
        trace = TraceWindow()
        self.addCleanup(trace.close)
        trace.add_frame(1.0, "RX", 0x7E8, b"\x02\x7e\x00")
        trace.add_frame(2.0, "TX", 0x7E0, b"\x02\x3e\x00")
        trace.flush()
        self.assertEqual(trace.tree.topLevelItemCount(), 2)
        trace.direction_combo.setCurrentText("TX only")
        self.assertEqual([trace.tree.topLevelItem(row).text(COL_ID) for row in range(trace.tree.topLevelItemCount())],
                         ["7E0"])
        trace.direction_combo.setCurrentText("RX and TX")
        trace.filter_edit.setText("7E8")
        self.assertEqual(trace.tree.topLevelItemCount(), 1)


class ClockTextTest(unittest.TestCase):
    def test_a_time_of_day_and_seconds_from_a_recordings_zero(self):
        self.assertRegex(absolute_text(1_700_000_000.25), r"^\d\d:\d\d:\d\d\.250$")
        self.assertEqual(absolute_text(12.5), "12.500")


if __name__ == "__main__":
    unittest.main()
