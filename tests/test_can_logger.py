"""CAN Logger (CANoe-style graphics window): one strip per ticked signal, cursors, follow, pause, CSV."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtWidgets import QApplication

from canexpert import can_logger
from canexpert.can_logger import CANLoggerWindow, COL_C1, COL_C2, COL_DELTA, COL_VALUE, VALUE_REFRESH_TICKS

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parent.parent / "DBC" / "dummy_ecu.dbc"
TEMP, PRESSURE, RUNNING = "EngineData.Temperature", "EngineData.Pressure", "EcuStatus.Running"


def engine_frame(temperature, pressure):
    return int(temperature * 10).to_bytes(2, "big") + int(pressure * 100).to_bytes(2, "big") + bytes(4)


class CanLoggerTest(unittest.TestCase):
    def setUp(self):
        self.logger = CANLoggerWindow()
        self.logger.load_dbc_from_path(DBC)
        self.clock = 100.0
        self.patch = patch.object(can_logger.time, "monotonic", lambda: self.clock)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.logger.close()

    def feed(self, *frames):
        """frames: (seconds after the previous frame, arbitration id, data)."""
        for delay, arb_id, data in frames:
            self.clock += delay
            self.logger.on_can_message(arb_id, data)

    def redraw(self):
        for _ in range(VALUE_REFRESH_TICKS):
            self.logger._redraw()

    def test_the_toolbar_is_small_symbol_buttons(self):
        buttons = self.logger._tool_buttons
        self.assertEqual(list(buttons), ["clear", "pause", "follow", "fit", "lock_x", "lock_y", "cursors"])
        for button in buttons.values():
            self.assertEqual(button.text(), "")                                  # the symbol carries the meaning
            self.assertFalse(button.icon().isNull())
            self.assertTrue(button.toolTip().startswith(button.accessibleName()))  # "Fit: show all recorded data"
        self.assertEqual([button.isCheckable() for button in buttons.values()],
                         [False, True, True, False, True, True, True])
        pause = self.logger.pause_btn
        symbol = pause.icon().pixmap(18, 18).toImage()
        pause.setChecked(True)                                                   # Pause becomes Resume
        self.assertEqual(pause.accessibleName(), "Resume")
        self.assertTrue(pause.toolTip().startswith("Resume"))
        self.assertNotEqual(pause.icon().pixmap(18, 18).toImage(), symbol)
        pause.setChecked(False)
        self.assertEqual(pause.accessibleName(), "Pause")

    def test_each_ticked_signal_gets_its_own_graph(self):
        self.assertEqual(self.logger.graph_stack.currentIndex(), 0)            # placeholder
        self.logger.set_signal_plotted(TEMP)
        self.logger.set_signal_plotted(RUNNING)
        self.logger.set_signal_plotted(PRESSURE)
        self.assertEqual(self.logger._plotted, [TEMP, RUNNING, PRESSURE])
        self.assertTrue(self.logger._items[TEMP].parent().isExpanded())        # ticked signals stay in view
        self.assertEqual(list(self.logger._plots), [TEMP, RUNNING, PRESSURE])
        plots = [entry[0] for entry in self.logger._plots.values()]
        self.assertEqual(len({id(p) for p in plots}), 3)
        first = plots[0].getViewBox()
        self.assertTrue(all(p.getViewBox().linkedView(0) is first for p in plots[1:]))  # shared time axis
        self.assertTrue(plots[-1].getAxis("bottom").style["showValues"])
        self.assertFalse(plots[0].getAxis("bottom").style["showValues"])
        self.assertEqual(plots[0].getAxis("left").labelText, "Temperature [degC]")
        self.logger.set_signal_plotted(RUNNING, False)
        self.assertEqual(list(self.logger._plots), [TEMP, PRESSURE])
        for name in (TEMP, PRESSURE):
            self.assertFalse(self.logger._items[name].icon(0).isNull())             # color swatch
        self.assertTrue(self.logger._items[RUNNING].icon(0).isNull())

    def test_data_is_decoded_plotted_and_shown(self):
        self.logger.set_signal_plotted(TEMP)
        self.feed((0.0, 0x300, engine_frame(20.0, 1.0)), (0.5, 0x300, engine_frame(25.5, 1.5)),
                  (0.5, 0x301, bytes([1, 0, 3, 7, 0, 0, 0, 0])), (0.0, 0x123, bytes(8)))   # unknown ID ignored
        self.redraw()
        x, y = self.logger._plots[TEMP][1].getData()
        self.assertEqual(list(x), [0.0, 0.5])
        self.assertEqual(list(y), [20.0, 25.5])
        self.assertEqual(self.logger._items[TEMP].text(COL_VALUE), "25.5")
        self.assertEqual(self.logger._items[RUNNING].text(COL_VALUE), "1")     # recorded even if not plotted
        self.logger.set_signal_plotted(PRESSURE)                               # history appears at once
        self.assertEqual(list(self.logger._plots[PRESSURE][1].getData()[1]), [1.0, 1.5])

    def test_cursors_read_sample_and_hold_values(self):
        self.logger.set_signal_plotted(TEMP)
        self.feed((0.0, 0x300, engine_frame(20.0, 1.0)), (1.0, 0x300, engine_frame(30.0, 1.0)),
                  (1.0, 0x300, engine_frame(40.0, 1.0)))
        self.logger.cursors_btn.setChecked(True)
        self.assertFalse(self.logger.signal_tree.isColumnHidden(COL_C1))
        self.assertFalse(self.logger.follow_btn.isChecked())
        self.logger._on_cursor_moved(0, 0.5)
        self.logger._on_cursor_moved(1, 1.9)
        self.assertEqual(self.logger.cursor_values(TEMP), (20.0, 30.0))
        item = self.logger._items[TEMP]
        self.assertEqual((item.text(COL_C1), item.text(COL_C2), item.text(COL_DELTA)), ("20", "30", "10"))
        self.assertIn("Δt: 1.400 s", self.logger.cursor_label.text())
        self.logger.set_signal_plotted(PRESSURE)                               # new strip gets the cursors too
        self.assertEqual([line.value() for line in self.logger._plots[PRESSURE][2:]], [0.5, 1.9])
        self.logger._plots[PRESSURE][3].setValue(1.2)                          # dragging one moves all
        self.assertEqual(self.logger._plots[TEMP][3].value(), 1.2)

    def test_hover_crosshair_and_readout(self):
        from PyQt5.QtCore import QEvent, QPointF
        self.logger.set_signal_plotted(TEMP)
        self.logger.set_signal_plotted(PRESSURE)
        self.feed((0.0, 0x300, engine_frame(20.0, 1.0)), (4.0, 0x300, engine_frame(40.0, 2.0)))
        self.logger.show()
        self.redraw()
        view = self.logger._plots[TEMP][0].getViewBox()
        view.setRange(xRange=(0, 4), yRange=(20, 40), padding=0)
        APP.processEvents()
        self.logger._on_mouse_moved(view.mapViewToScene(QPointF(1.5, 30.0)))
        vertical, horizontal, readout = self.logger._hover[TEMP]
        self.assertTrue(vertical.isVisible() and horizontal.isVisible() and readout.isVisible())
        self.assertAlmostEqual(vertical.value(), 1.5, places=1)
        self.assertAlmostEqual(horizontal.value(), 30.0, places=0)
        time_text, value_text = readout.textItem.toPlainText().split("   ")
        self.assertRegex(time_text, r"^1\.5\d\d s$")
        self.assertTrue(value_text.endswith(" degC"))
        self.assertAlmostEqual(float(value_text.split()[0]), 30.0, places=0)
        self.assertEqual((readout.pos().x(), readout.pos().y()), (view.width() - 4, view.height() - 4))
        self.assertFalse(any(item.isVisible() for item in self.logger._hover[PRESSURE]))  # hovered graph only
        pressure_view = self.logger._plots[PRESSURE][0].getViewBox()
        self.logger._on_mouse_moved(pressure_view.mapViewToScene(pressure_view.viewRect().center()))
        self.assertFalse(vertical.isVisible())                                  # moved to the other graph
        self.assertTrue(self.logger._hover[PRESSURE][0].isVisible())
        self.logger.eventFilter(self.logger.graph, QEvent(QEvent.Leave))
        self.assertFalse(any(item.isVisible() for items in self.logger._hover.values() for item in items))

    def test_axis_locks(self):
        from PyQt5.QtCore import QPoint, QPointF, Qt
        from PyQt5.QtGui import QWheelEvent
        self.logger.set_signal_plotted(TEMP)
        self.feed(*[(0.5, 0x300, engine_frame(20.0 + i, 1.0)) for i in range(20)])
        self.logger.show()
        self.redraw()
        APP.processEvents()

        def wheel(view):
            viewport = self.logger.graph.viewport()
            center = self.logger.graph.mapFromScene(view.sceneBoundingRect().center())
            APP.sendEvent(viewport, QWheelEvent(QPointF(center), QPointF(viewport.mapToGlobal(center)), QPoint(),
                                                QPoint(0, 120), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False))
            return view.viewRange()

        def span(axis_range):
            return axis_range[1] - axis_range[0]

        view = self.logger._plots[TEMP][0].getViewBox()
        self.assertEqual(view.state["mouseEnabled"], [True, False])            # default: X free, Y locked
        before = view.viewRange()
        after = wheel(view)
        self.assertLess(span(after[0]), span(before[0]))                        # time zoomed
        self.assertEqual(after[1], before[1])                                   # values untouched
        self.assertFalse(self.logger.follow_btn.isChecked())                    # time moved by hand

        self.logger.follow_btn.setChecked(True)
        self.logger.lock_x_btn.setChecked(True)
        self.logger.lock_y_btn.setChecked(False)
        self.redraw()
        before = view.viewRange()
        after = wheel(view)
        self.assertEqual(after[0], before[0])                                   # time untouched
        self.assertLess(span(after[1]), span(before[1]))                        # values zoomed
        self.assertTrue(self.logger.follow_btn.isChecked())                     # a value zoom keeps Follow

        self.logger.set_signal_plotted(PRESSURE)                                # new graphs get the locks
        self.assertEqual(self.logger._plots[PRESSURE][0].getViewBox().state["mouseEnabled"], [False, True])
        self.logger.lock_y_btn.setChecked(True)                                 # relocking Y refits values
        view = self.logger._plots[TEMP][0].getViewBox()
        self.assertEqual(view.state["mouseEnabled"], [False, False])
        self.assertTrue(view.state["autoRange"][1])

    def test_follow_pause_and_fit(self):
        self.logger.set_signal_plotted(TEMP)
        self.feed(*[(1.0, 0x300, engine_frame(20.0 + i, 1.0)) for i in range(30)])
        self.redraw()
        start, end = self.logger._plots[TEMP][0].getViewBox().viewRange()[0]
        self.assertAlmostEqual(end, 29.0)
        self.assertAlmostEqual(end - start, 10.0)                               # default time window
        self.logger.pause_btn.setChecked(True)
        self.feed((1.0, 0x300, engine_frame(99.0, 1.0)))
        self.redraw()
        self.assertEqual(len(self.logger._plots[TEMP][1].xData), 30)     # display frozen
        self.logger.pause_btn.setChecked(False)
        self.redraw()
        self.assertEqual(len(self.logger._plots[TEMP][1].xData), 31)     # caught up
        self.logger.fit_all()
        self.assertFalse(self.logger.follow_btn.isChecked())

    def test_filter_and_plotted_only(self):
        self.logger.set_signal_plotted(PRESSURE)
        self.logger.filter_edit.setText("temp")
        visible = [name for name, item in self.logger._items.items() if not item.isHidden()]
        self.assertEqual(visible, [TEMP])
        self.logger.filter_edit.clear()
        self.logger.plotted_only_cb.setChecked(True)
        visible = [name for name, item in self.logger._items.items() if not item.isHidden()]
        self.assertEqual(visible, [PRESSURE])

    def test_csv_export_and_clear(self):
        self.feed((0.0, 0x300, engine_frame(20.0, 1.0)), (0.25, 0x300, engine_frame(21.0, 1.25)))
        path = Path(tempfile.mkdtemp()) / "log.csv"
        self.logger.save_csv(path)
        with open(path, newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ["Time", "Signal", "Value"])
        self.assertIn(["0.25", TEMP, "21.0"], rows)
        self.assertIn(["0.25", PRESSURE, "1.25"], rows)
        self.logger.clear_data()
        self.assertEqual(self.logger._series, {})
        self.assertEqual(self.logger._items[TEMP].text(COL_VALUE), "")


if __name__ == "__main__":
    unittest.main()
