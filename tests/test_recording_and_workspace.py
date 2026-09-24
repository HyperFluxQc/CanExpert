"""The frames every window shares, recording, offline replay, and the tool panes with their layout."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtCore import QEvent, QSettings
from PyQt5.QtWidgets import QAction, QApplication, QMainWindow

from PyQtAds import ads

from canexpert import can_bus, workspace
from canexpert import main_window as main
from canexpert.main_layouts import LAYOUT_STATE
from canexpert.recording import Recorder, read_frames
from canexpert.transmit_window import default_row

APP = QApplication.instance() or QApplication([])

PANEL = '''<application_database name="Bus"><pages><page name="Main">
<value id="1" label="Status" binding_value="status" x="10" y="10"/>
</page></pages></application_database>'''


def spin_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


class MeasurementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.configs = self.root / "Configurations"
        self.databases = self.root / "Databases"
        self.configs.mkdir()
        self.databases.mkdir()
        (self.databases / "panel_2026-09-18.xml").write_text(PANEL)
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.IniFormat)
        config = {"name": "Bus", "request_id": 0x7E0, "response_id": 0x7E8,
                  "tester_present_interval_seconds": .05, "node_timeout_seconds": .2,
                  "database_family": "panel"}
        (self.configs / "config_Bus.json").write_text(json.dumps(config))
        self.channel = "measure-" + str(uuid.uuid4())
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
        self.window.selected_channel_config = {"interface": "virtual", "channel": 0}

    def fake_can_bus(self, interface, channel, bitrate, **options):
        return can.Bus(interface="virtual", channel=self.channel)

    def tearDown(self):
        self.window.close()
        APP.processEvents()
        self.ecu.shutdown()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def send_from_ecu(self, can_id=0x300, data=b"\x01\x02"):
        self.ecu.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=False))

    # --- one path for every frame ---------------------------------------------------------------

    def test_a_received_frame_reaches_the_history_with_the_adapter_timestamp(self):
        self.window.on_connect_clicked()
        self.assertIsNotNone(self.window.can_bus, self.window.status_label.text())
        self.send_from_ecu(0x123, b"\xaa\xbb")
        self.assertTrue(spin_until(lambda: any(frame[2] == 0x123 for frame in self.window.frame_history)))
        frame = next(f for f in self.window.frame_history if f[2] == 0x123)
        self.assertEqual((frame[1], frame[3]), ("RX", b"\xaa\xbb"))
        self.assertAlmostEqual(frame[0], time.time(), delta=30)   # the adapter's clock, not the GUI's

    def test_a_window_opened_later_still_shows_what_was_received(self):
        self.window.on_connect_clicked()
        self.send_from_ecu(0x321, b"\x05")
        self.assertTrue(spin_until(lambda: any(frame[2] == 0x321 for frame in self.window.frame_history)))
        trace = self.window.open_trace()
        self.assertEqual(trace.tree.topLevelItemCount(), len(self.window.frame_history))
        self.assertTrue(any(trace.tree.topLevelItem(row).text(2) == "321"
                            for row in range(trace.tree.topLevelItemCount())))

    def test_the_ecu_check_also_feeds_the_trace(self):
        # Frames seen while only the ECUs are checked used to reach the CAN monitor alone.
        trace = self.window.open_trace()
        self.window.check_ecus({"interface": "virtual", "channel": 0})
        self.assertIsNotNone(self.window.ecu_monitor)
        self.send_from_ecu(0x456, b"\x07")
        self.assertTrue(spin_until(lambda: (trace.flush(), any(frame[2] == 0x456 for frame in trace.frames))[1]))

    def test_nothing_can_be_sent_before_connecting(self):
        with self.assertRaises(RuntimeError) as raised:
            self.window.send_can_message(0x200, b"\x01")
        self.assertIn("Connect", str(raised.exception))

    # --- recording and offline replay -------------------------------------------------------------

    def test_recording_writes_a_file_that_can_be_read_back(self):
        path = self.root / "session.asc"
        recorder = Recorder(path)
        recorder.write(1000.5, "RX", 0x300, b"\x01\x02\x03", False)
        recorder.write(1000.75, "TX", 0x7E0, b"\x02\x3e\x00", False)
        recorder.stop()
        self.assertEqual(recorder.count, 2)
        frames = read_frames(path)
        self.assertEqual([(frame[1], frame[2], frame[3]) for frame in frames],
                         [("RX", 0x300, b"\x01\x02\x03"), ("TX", 0x7E0, b"\x02\x3e\x00")])
        self.assertAlmostEqual(frames[1][0] - frames[0][0], 0.25, places=3)

    def test_the_session_is_recorded_and_replayed_into_the_windows(self):
        path = self.root / "measurement.asc"
        with patch.object(main.QFileDialog, "getSaveFileName", return_value=(str(path), "")):
            self.assertIsNotNone(self.window.start_recording())
        self.window.on_connect_clicked()
        self.send_from_ecu(0x300, b"\x11\x22")
        self.send_from_ecu(0x301, b"\x33")
        self.assertTrue(spin_until(lambda: self.window.recorder.count >= 2))
        self.window.on_disconnect_clicked()               # stops the recording as well
        self.assertIsNone(self.window.recorder)
        self.assertTrue(path.exists())

        # Offline: the file plays back into the trace, without any bus.
        with patch.object(main.QFileDialog, "getOpenFileName", return_value=(str(path), "")):
            dialog = self.window.replay_log()
        self.addCleanup(dialog.close)
        trace = self.window.tool_widget("trace")
        trace.clear()
        self.assertIn("Offline", trace.offline_label.text())
        dialog.speed_combo.setCurrentIndex(dialog.speed_combo.count() - 1)   # as fast as possible
        dialog.start_replay()
        self.assertTrue(spin_until(lambda: (trace.flush(), len(trace.frames) >= 2)[1]))
        self.assertIn(0x300, [frame[2] for frame in trace.frames])
        self.assertIsNone(self.window.can_bus, "replaying must not open a bus")

        # The menu item does the same: it asks for the file (Qt's "checked" must not stand in for it).
        action = next(item for item in self.window.findChildren(QAction) if item.text() == "Replay a recorded file...")
        with patch.object(main.QFileDialog, "getOpenFileName", return_value=(str(path), "")) as asked:
            action.trigger()
        asked.assert_called_once()
        self.assertIsNot(self.window.replay, dialog)
        self.addCleanup(self.window.replay.close)

    # --- the workspace ----------------------------------------------------------------------------

    def test_tool_windows_live_in_the_workspace(self):
        for name, opener in (("trace", self.window.open_trace), ("logger", self.window.open_can_logger),
                             ("transmit", self.window.open_transmit), ("console", self.window.open_uds_console)):
            widget = opener()
            pane = self.window.tool_panes[name]
            self.assertIs(pane.widget(), widget, name)
            self.assertIs(pane.dockManager(), self.window.workspace, name)
            self.assertFalse(pane.isClosed(), name)
            self.assertIs(opener(), widget, f"{name} must be reused, not rebuilt")
        self.assertIs(self.window.database_pane.dockManager(), self.window.workspace)

    def test_a_tool_button_stays_pressed_while_its_pane_is_open(self):
        action = self.window._toolbar_actions["trace"]
        self.assertTrue(action.isCheckable())
        self.assertFalse(action.isChecked())

        action.trigger()                                  # a press on the toolbar button
        self.assertTrue(action.isChecked())
        pane = self.window.tool_panes["trace"]
        self.assertFalse(pane.isClosed())

        action.trigger()                                  # pressing it again closes the window
        self.assertFalse(action.isChecked())
        self.assertTrue(pane.isClosed())

        # Closing it by its own tab button lets the toolbar button go, and what it recorded is kept.
        trace = self.window.open_trace()
        self.assertTrue(action.isChecked())
        trace.add_frame(1000.0, "RX", 0x321, b"\x01")
        trace.flush()
        pane.closeDockWidget()
        self.assertFalse(action.isChecked())
        self.assertIs(self.window.open_trace(), trace)
        self.assertEqual(len(trace.frames), 1)

    def test_every_pane_button_follows_its_pane(self):
        for name in self.window.tool_names:
            action = self.window._toolbar_actions[name]
            action.trigger()
            self.assertTrue(action.isChecked(), name)
            self.assertFalse(self.window.tool_panes[name].isClosed(), name)
            action.trigger()
            self.assertFalse(action.isChecked(), name)
            self.assertTrue(self.window.tool_panes[name].isClosed(), name)

    def test_the_transmit_window_keeps_sending_behind_other_windows_and_stops_when_closed(self):
        self.window.show()                                 # hiding behind a tab only happens to what is shown
        APP.processEvents()
        pane = self.window.open_transmit()
        self.assertEqual([pane.tabs.tabText(index) for index in range(pane.tabs.count())],
                         ["Messages", "Simulated nodes"])
        messages = pane.messages
        for page in (messages, pane.nodes):
            page._timer.stop()           # not connected: a send would fail and switch the row off by itself
        messages.rows = [default_row("Start", 0x200, b"\x01", 50)]
        messages._fill_table()
        messages.rows[0]["enabled"] = True
        pane.nodes.start_btn.setChecked(True)
        pane.show_nodes()                                  # the other tab in front
        self.window.open_trace()
        trace_pane = self.window.tool_panes["trace"]
        transmit_pane = self.window.tool_panes["transmit"]
        self.window.workspace.addDockWidget(ads.CenterDockWidgetArea, trace_pane, transmit_pane.dockAreaWidget())
        trace_pane.setAsCurrentTab()                       # another window's tab in front
        APP.processEvents()
        self.assertFalse(pane.isVisible())
        self.assertTrue(messages.rows[0]["enabled"], "hidden behind a tab is not closed")
        self.assertTrue(pane.nodes.start_btn.isChecked())
        transmit_pane.toggleView(False)                    # closed
        self.assertFalse(messages.rows[0]["enabled"])
        self.assertFalse(pane.nodes.start_btn.isChecked())

    def test_windows_tab_together_and_float(self):
        self.window.open_trace()
        trace = self.window.tool_panes["trace"]
        self.window.open_can_logger()
        logger = self.window.tool_panes["logger"]
        workspace = self.window.workspace

        # Dropping one window onto another's area tabs them, as dragging it there does.
        workspace.addDockWidget(ads.CenterDockWidgetArea, logger, trace.dockAreaWidget())
        self.assertTrue(trace.isTabbed() and logger.isTabbed())
        self.assertEqual([pane.windowTitle() for pane in trace.dockAreaWidget().dockWidgets()],
                         ["Trace", "CAN Logger"])
        logger.setAsCurrentTab()
        self.assertIs(trace.dockAreaWidget().currentDockWidget(), logger)

        # A window can be pulled out into a window of its own, and put back.
        logger.setFloating()
        APP.processEvents()
        self.assertTrue(logger.isFloating())
        self.assertFalse(logger.isClosed())
        self.assertIs(self.window.tool_widget("logger"), logger.widget())
        workspace.addDockWidget(ads.BottomDockWidgetArea, logger, trace.dockAreaWidget())
        APP.processEvents()
        self.assertFalse(logger.isFloating())

    def test_the_layout_is_remembered_and_desktops_can_be_saved(self):
        self.window.open_trace()
        self.window.save_desktop("Analysis")
        self.assertIn("Analysis", self.window.desktops())
        self.window.tool_panes["trace"].toggleView(False)
        self.window.apply_desktop("Analysis")
        self.assertFalse(self.window.tool_panes["trace"].isClosed())
        self.window.reset_layout()
        self.assertTrue(self.window.tool_panes["trace"].isClosed())
        # Closing writes the arrangement, and a new window restores it.
        self.window.open_trace()
        self.window.close()
        self.assertTrue(self.settings.value(LAYOUT_STATE))
        restored = main.MainWindow()
        self.assertIsNotNone(restored.open_trace())
        restored.close()          # closed here, not in a cleanup: the settings patch is still in place

    def test_an_empty_database_window_does_not_come_back_with_the_layout(self):
        self.window.on_connect_clicked()
        self.assertFalse(self.window.database_pane.isClosed())
        self.window.close()                                # the arrangement is saved with the database open
        restored = main.MainWindow()
        try:
            self.assertIsNone(restored.app_database)
            self.assertTrue(restored.database_pane.isClosed(), "no database loaded: nothing to show")
            restored.open_can_logger()                     # used to apply the saved arrangement again
            restored.open_trace()
            self.assertTrue(restored.database_pane.isClosed())
            self.assertFalse(restored.tool_panes["logger"].isClosed())
            restored.save_desktop("Measuring")
            restored.apply_desktop("Measuring")
            self.assertTrue(restored.database_pane.isClosed())
        finally:
            restored.close()

    def test_a_window_opens_where_the_layout_put_it_and_leaves_the_others_alone(self):
        self.window.show()
        self.assertNotIn("logger", self.window.tool_panes, "nothing is built before it is opened")
        self.window.open_can_logger()
        self.window.tool_panes["logger"].setFloating()
        self.window.close()
        restored = main.MainWindow()
        try:
            restored.show()
            restored.open_can_logger()
            logger = restored.tool_panes["logger"]
            APP.processEvents()
            self.assertTrue(logger.isFloating(), "where the saved arrangement put it")
            restored.workspace.addDockWidget(ads.BottomDockWidgetArea, logger, restored.database_pane.dockAreaWidget())
            APP.processEvents()
            self.assertFalse(logger.isFloating())
            restored.open_trace()                          # the first time this session
            restored.open_data()
            APP.processEvents()
            self.assertFalse(logger.isFloating(), "opening another window does not put this one back")
            self.assertFalse(logger.isClosed())
        finally:
            restored.close()

    def test_a_window_the_layout_did_not_know_reopens_docked(self):
        # A state only places the windows it knows; the others used to be left out and reopen floating.
        self.window.show()
        self.window.open_trace()
        self.window.reset_layout()
        self.window.open_trace()
        APP.processEvents()
        self.assertFalse(self.window.tool_panes["trace"].isFloating())
        self.window.save_desktop("Before the logger")
        self.window.open_can_logger()
        self.window.apply_desktop("Before the logger")
        self.assertTrue(self.window.tool_panes["logger"].isClosed(), "it was not open in that desktop")
        self.window.open_can_logger()
        APP.processEvents()
        self.assertFalse(self.window.tool_panes["logger"].isFloating())

    def visible_areas(self):
        return [name for name, pane in self.window.workspace.dockWidgetsMap().items()
                if pane.dockAreaWidget() is not None and pane.dockAreaWidget().isVisible()]

    def test_a_window_the_layout_did_not_know_leaves_no_empty_strip(self):
        # A layout saved before a window existed: PyQtAds marks the window closed, and it used to come back
        # docked in a visible area with no tab - an empty strip - because closing it again did nothing.
        self.window.show()
        workspace, logger = self.window.workspace, self.window._tool_slots["logger"]
        workspace.removeDockWidget(logger)
        older = self.window.layout_state()                     # a layout without the logger
        add_back = workspace.addDockWidget(ads.CenterDockWidgetArea, logger, None)
        self.assertIsNotNone(add_back)
        self.window.apply_layout_state(older)
        APP.processEvents()
        self.assertEqual(self.visible_areas(), [], "nothing loaded, nothing open: nothing to see")
        self.assertTrue(logger.isClosed())
        self.window.open_can_logger()
        APP.processEvents()
        self.assertEqual(self.visible_areas(), ["pane_logger"])
        self.assertFalse(logger.isFloating())
        self.assertTrue(logger.tabWidget().isVisible() and logger.widget().isVisible())

    def test_floating_windows_left_empty_go_and_a_sliver_is_made_usable(self):
        self.window.show()
        self.window.open_trace()
        trace = self.window.tool_panes["trace"]
        trace.setFloating()
        APP.processEvents()
        self.window.workspace.addDockWidget(ads.BottomDockWidgetArea, trace, self.window.database_pane.dockAreaWidget())
        APP.processEvents()                                    # back in the workspace: its floating window is empty
        self.window.apply_layout_state(self.window.layout_state())
        APP.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertEqual(self.window.workspace.floatingWidgets(), [], "the empty floating window is gone")

        trace.setFloating()
        APP.processEvents()
        floating = trace.dockContainer().floatingWidget()
        floating.setGeometry(20, 20, 600, 40)                   # a sliver, as a layout had saved it
        trace.toggleView(False)
        self.window.open_trace()
        APP.processEvents()
        self.assertGreaterEqual(floating.height(), min(300, APP.primaryScreen().availableGeometry().height()))
        floating.move(20000, 20000)                            # a monitor that is not there any more
        trace.toggleView(False)
        self.window.open_trace()
        APP.processEvents()
        self.assertTrue(APP.primaryScreen().availableGeometry().intersects(floating.geometry()))

    def test_the_pages_of_a_database_after_reset_layout(self):
        (self.databases / "panel_2026-09-18.xml").write_text(PANEL.replace(
            "</page></pages>", '</page><page name="Body"><value id="2" label="Door" binding_value="door" '
                               'x="10" y="10"/></page></pages>'))
        self.window.show()
        self.window.on_connect_clicked()
        body = self.window.page_panes[0]
        body.setFloating()
        self.window.reset_layout()                         # a layout from before the page window existed
        APP.processEvents()
        self.assertFalse(body.isClosed())
        self.assertFalse(body.isFloating(), "back beside the first page")
        self.assertFalse(self.window.database_pane.isClosed())
        self.window.on_disconnect_clicked()
        self.assertTrue(body.isClosed())
        self.assertTrue(self.window.database_pane.isClosed())


class LinuxWithoutX11Test(unittest.TestCase):
    """PyQtAds 3.8 keeps floating windows above the main window with X11 messages sent from a filter on it,
    without checking that X11 is there: under Wayland or headless (CI) the filter goes, or closing the window
    with a pane floating crashes."""

    def removed_filters(self, platform, qpa):
        removed = []

        class Window(QMainWindow):
            def removeEventFilter(self, watcher):
                removed.append(watcher)
                super().removeEventFilter(watcher)
        window = Window()
        self.addCleanup(window.deleteLater)
        with patch.object(workspace.sys, "platform", platform),                 patch.object(workspace.QGuiApplication, "platformName", return_value=qpa):
            manager = workspace.create_workspace(window)
        return removed, manager

    def test_the_filter_goes_where_there_is_no_x11(self):
        for qpa in ("offscreen", "wayland"):
            removed, manager = self.removed_filters("linux", qpa)
            self.assertEqual(removed, [manager], qpa)

    def test_it_stays_under_x11_and_elsewhere(self):
        self.assertEqual(self.removed_filters("linux", "xcb")[0], [])
        self.assertEqual(self.removed_filters("win32", "windows")[0], [])


if __name__ == "__main__":
    unittest.main()
