"""Markers during a measurement (Ctrl+M): rows in the Trace, lines on the CAN Logger's graphs, entries in the
recording where its format holds one, from the keys, a panel script's api.marker() and a test module's
t.marker()."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import tempfile
import time
import unittest
import uuid
import zlib
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtCore import QSettings
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QApplication, QInputDialog

from canexpert import can_bus
from canexpert import main_window as main
from canexpert.can_bus import ReceiveMailbox
from canexpert.config import validate_config
from canexpert.panel.runtime import ScriptRuntime
from canexpert.recording import Recorder, read_frames
from canexpert.testing.runner import load_module, Runner, uds_names
from canexpert.trace_window import COL_NAME, TraceWindow

APP = QApplication.instance() or QApplication([])
PANEL = '''<application_database name="Markers"><pages><page name="Main">
<value id="1" label="Status" binding_value="status" x="10" y="10"/>
</page></pages></application_database>'''


def spin_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


def temp_dir(test) -> Path:
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    return Path(directory.name)


class RecordingTest(unittest.TestCase):
    def record(self, suffix):
        path = temp_dir(self) / f"session{suffix}"
        recorder = Recorder(path)
        recorder.write(1000.5, "RX", 0x300, b"\x01\x02", False)
        stored = recorder.write_marker(1000.6, "Door opened")
        recorder.write(1000.75, "TX", 0x7E0, b"\x02\x3e\x00", False)
        recorder.stop()
        return path, stored

    def test_asc_blf_and_trc_keep_the_marker_and_still_read_back(self):
        for suffix in (".asc", ".blf", ".trc"):
            path, stored = self.record(suffix)
            self.assertTrue(stored, suffix)
            self.assertEqual([frame[2] for frame in read_frames(path)], [0x300, 0x7E0], suffix)
            raw = path.read_bytes()
            if suffix == ".blf":                                  # BLF compresses its objects (zlib)
                start = raw.index(bytes([0x78, 0x9C]))
                raw = zlib.decompressobj().decompress(raw[start:])
            self.assertIn(b"Door opened", raw, suffix)
        self.assertIn("Marker: Door opened", self.record(".asc")[0].read_text(encoding="utf-8"))

    def test_csv_has_no_place_for_one(self):
        path, stored = self.record(".csv")
        self.assertFalse(stored)
        self.assertEqual(len(read_frames(path)), 2)


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.trace = TraceWindow()
        self.addCleanup(self.trace.deleteLater)

    def names(self):
        return [self.trace.tree.topLevelItem(index).text(COL_NAME) for index in range(self.trace.tree.topLevelItemCount())]

    def test_a_marker_is_a_row_at_its_time_whatever_the_filter(self):
        self.trace.add_frame(10.0, "RX", 0x300, b"\x01")
        self.trace.add_frame(12.0, "RX", 0x301, b"\x02")
        self.trace.flush()
        self.trace.add_marker(11.0, "Door opened")
        self.trace.add_frame(13.0, "RX", 0x300, b"\x03")
        self.trace.flush()
        self.assertEqual(self.names(), ["", "Marker: Door opened", "", ""])
        marker = self.trace.tree.topLevelItem(1)
        self.assertTrue(marker.font(COL_NAME).bold())
        self.assertIn("1 marker", self.trace.status.text())
        self.assertIn("3 frame(s)", self.trace.status.text())
        self.trace.filter_edit.setText("301")                     # pass only 0x301: the marker stays
        self.assertEqual(self.names(), ["Marker: Door opened", ""])
        self.trace.search_edit.setText("door")
        self.assertIs(self.trace.find_next(), self.trace.tree.topLevelItem(0))
        self.trace.filter_edit.setText("")
        self.trace.time_combo.setCurrentText("Delta")
        self.assertEqual(self.trace.tree.topLevelItem(1).text(0), "1.000000", "since the frame above")
        self.trace.clear()
        self.assertEqual(self.names(), [])

    def test_the_transport_view_keeps_the_markers_too(self):
        self.trace.set_diagnostic_ids({0x7E0, 0x7E8})
        self.trace.add_frame(10.0, "TX", 0x7E0, b"\x02\x10\x03")
        self.trace.add_frame(10.1, "RX", 0x7E8, b"\x02\x50\x03")
        self.trace.add_marker(10.05, "Asked")
        self.trace.transport_btn.setChecked(True)
        self.trace.flush()
        self.assertEqual(self.names()[1], "Marker: Asked")
        self.assertEqual(len(self.names()), 3)


class MainWindowTest(unittest.TestCase):
    def setUp(self):
        root = temp_dir(self)
        configs, databases = root / "Configurations", root / "Databases"
        configs.mkdir()
        databases.mkdir()
        (configs / "config_Bench.json").write_text(json.dumps({
            "name": "Bench", "request_id": 0x7E0, "response_id": 0x7E8, "database_family": "panel",
            "tester_present_interval_seconds": 0.5, "node_timeout_seconds": 2}))
        (databases / "panel_2026-09-24.xml").write_text(PANEL)
        (databases / "panel_2026-09-24_script.py").write_text(
            "@on_control('status')\ndef mark(api, value):\n    api.marker(f'from the script: {value}')\n")
        self.root = root
        settings = QSettings(str(root / "settings.ini"), QSettings.IniFormat)
        settings.setValue("last_configuration", "Bench")
        channel = "markers-" + str(uuid.uuid4())
        patches = [patch.object(main, "CONFIG_DIR", configs), patch.object(main, "DATABASES_DIR", databases),
                   patch.object(main, "app_settings", lambda: settings),
                   patch.object(main.can, "detect_available_configs", return_value=[]),
                   patch.object(can_bus, "create_can_bus",
                                lambda *args, **options: can.Bus(interface="virtual", channel=channel))]
        for item in patches:
            item.start()
        self.window = main.MainWindow()
        self.window.selected_channel_config = {"interface": "virtual", "channel": 0}

        def close():
            self.window.close()
            APP.processEvents()
            for item in reversed(patches):
                item.stop()
        self.addCleanup(close)

    def test_markers_reach_the_trace_the_logger_and_the_recording(self):
        trace = self.window.open_trace()
        path = self.root / "measurement.asc"
        with patch.object(main.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
            self.window.start_recording()
        self.window.on_connect_clicked()
        with patch.object(QInputDialog, "getText", return_value=("Door opened", True)):
            marker = self.window.insert_marker()
        self.assertEqual(marker[1], "Door opened")
        with patch.object(QInputDialog, "getText", return_value=("", False)):
            self.assertIsNone(self.window.insert_marker(), "cancelled")
        self.assertEqual(self.window.quick_marker()[1], "Marker 2")
        trace.flush()
        self.assertEqual([marker[1] for marker in trace.markers], ["Door opened", "Marker 2"])
        logger = self.window.open_can_logger()                      # opened later: it gets them too
        self.assertEqual([text for _when, text in logger._markers], ["Door opened", "Marker 2"])
        self.assertIn("Marker at", self.window.debug_log.toPlainText())
        self.window.on_disconnect_clicked()                          # stops the recording
        self.assertIn("Marker: Door opened", path.read_text(encoding="utf-8"))

    def test_a_panel_script_inserts_one(self):
        self.window.on_connect_clicked()
        self.assertIsNotNone(self.window.script_runtime, self.window.status_label.text())
        self.window.script_runtime.post("control", "status", "on")
        self.assertTrue(spin_until(lambda: any(text == "from the script: on"
                                               for _when, text in self.window.marker_history)))

    def test_the_keys(self):
        menu_keys = {action.shortcut().toString(): action.text() for action in self.window._shortcut_actions}
        self.assertEqual(menu_keys[QKeySequence("Ctrl+M").toString()], "Insert marker...")
        self.assertEqual(menu_keys[QKeySequence("Ctrl+Shift+M").toString()], "Quick marker")


class LoggerTest(unittest.TestCase):
    def test_a_line_on_every_graph_and_its_comment_on_the_top_one(self):
        from canexpert.can_logger import CANLoggerWindow
        from canexpert.paths import DBC_DIR
        logger = CANLoggerWindow()
        self.addCleanup(logger.deleteLater)
        logger.load_dbc_from_path(DBC_DIR / "dummy_ecu.dbc")
        names = [name for name in logger._items if name.startswith("EngineData.")][:2]
        for name in names:
            logger.set_signal_plotted(name)
        logger.on_can_message(0x300, bytes(8), 100.0)
        logger.on_marker(100.5, "Door opened")

        def marker_lines():
            import pyqtgraph as pg
            return [[item for item in plot.items if isinstance(item, pg.InfiniteLine) and item.value() == 0.5]
                    for plot, _first, _second in logger._group_plots]
        self.assertEqual([len(lines) for lines in marker_lines()], [1] * len(logger._group_plots))
        self.assertEqual(marker_lines()[0][0].label.format, "Door opened")
        logger._rebuild_strips()                                     # e.g. a signal added: the lines stay
        self.assertEqual([len(lines) for lines in marker_lines()], [1] * len(logger._group_plots))
        logger.clear_data()
        self.assertEqual(logger._markers, [])


class ScriptAndTestModuleTest(unittest.TestCase):
    def test_api_marker(self):
        runtime = ScriptRuntime(ReceiveMailbox(None), validate_config({"name": "t"}), {}, None)
        seen = []
        runtime.marker_requested.connect(lambda when, text: seen.append(text))
        runtime.api.marker("halfway")
        APP.processEvents()
        self.assertEqual(seen, ["halfway"])

    def test_t_marker(self):
        path = temp_dir(self) / "marked.py"
        path.write_text("@testcase\ndef marked(t):\n    t.marker('before the reset')\n    t.check(True, 'done')\n")
        seen = []
        report = Runner(load_module(path, uds_names()), marker=lambda when, text: seen.append(text)).run()
        self.assertEqual(seen, ["before the reset"])
        self.assertEqual([(step.description, step.verdict) for step in report.cases[0].steps],
                         [("Marker: before the reset", "info"), ("done", "pass")])


if __name__ == "__main__":
    unittest.main()
