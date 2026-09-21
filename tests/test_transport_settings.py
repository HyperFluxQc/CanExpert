"""ISO-TP padding and the tester's own flow control (kept in the settings, never in the configuration file)."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from canexpert.can_bus import CanWorker
from canexpert.config import ConfigurationDialog, uds_transport, validate_config
from canexpert.simulator.ecu import DummyEcu, EcuConfig
from canexpert.transport_settings import (TransportSettings, apply_transport, load_transport, pad,
                                          save_transport)
from canexpert.uds.client import uds_request

APP = QApplication.instance() or QApplication([])


class Recorder(can.Listener):
    """Everything seen on the bus, for checking what CAN Expert put on it."""

    def __init__(self):
        self.frames = []

    def on_message_received(self, message):
        self.frames.append(message)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat)

    def test_padding_is_on_by_default(self):
        transport = load_transport(self.settings, "Never saved")
        self.assertTrue(transport.padding)
        self.assertEqual(transport.padding_byte, 0xCC)
        self.assertEqual((transport.block_size, transport.st_min), (0, 0))

    def test_what_is_saved_for_a_configuration_comes_back_for_it_alone(self):
        save_transport(self.settings, "Engine", TransportSettings(padding_byte=0x55, block_size=8, st_min=0xF3))
        self.assertEqual(load_transport(self.settings, "Engine"),
                         TransportSettings(padding_byte=0x55, block_size=8, st_min=0xF3))
        self.assertEqual(load_transport(self.settings, "Body"), TransportSettings())

    def test_values_iso_15765_does_not_allow_are_refused(self):
        for bad in (TransportSettings(padding_byte=0x100), TransportSettings(block_size=256),
                    TransportSettings(st_min=0x80), TransportSettings(st_min=0xFA)):
            with self.assertRaises(ValueError):
                bad.check()
        TransportSettings(st_min=0xF1).check()                  # 100 us is fine

    def test_a_damaged_entry_falls_back_to_the_defaults(self):
        self.settings.setValue("transport/Engine", "{not json")
        self.assertEqual(load_transport(self.settings, "Engine"), TransportSettings())
        self.settings.setValue("transport/Engine", json.dumps({"st_min": 0x99}))
        self.assertEqual(load_transport(self.settings, "Engine"), TransportSettings())

    def test_the_session_configuration_carries_them_to_every_request(self):
        config = validate_config({"name": "Engine", "request_id": 0x7E0, "response_id": 0x7E8})
        self.assertEqual({key: uds_transport(config)[key] for key in ("padding", "block_size", "st_min")},
                         {"padding": None, "block_size": 0, "st_min": 0}, "without them nothing changes")
        session = apply_transport(config, TransportSettings(block_size=4, st_min=0x05))
        transport = uds_transport(session)
        self.assertEqual((transport["padding"], transport["block_size"], transport["st_min"]), (0xCC, 4, 5))
        self.assertNotIn("isotp_padding", config, "the configuration itself is left alone")
        off = apply_transport(config, TransportSettings(padding=False))
        self.assertIsNone(uds_transport(off)["padding"])
        self.assertEqual(pad(b"\x02\x3e\x00", session), b"\x02\x3e\x00" + b"\xcc" * 5)
        self.assertEqual(pad(b"\x02\x3e\x00", off), b"\x02\x3e\x00")


class ConfigurationDialogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat)
        self.directory = Path(self.temp.name) / "Configurations"

    def test_the_dialog_keeps_them_in_the_settings_and_out_of_the_file(self):
        dialog = ConfigurationDialog(config={"name": "Engine"}, directory=self.directory, settings=self.settings)
        self.addCleanup(dialog.close)
        dialog.transport_group.padding_edit.setText("55")
        dialog.transport_group.block_size_spin.setValue(8)
        dialog.transport_group.st_min_edit.setText("0A")
        self.assertEqual(dialog.transport_group.st_min_label.text(), "10 ms")
        dialog.save_config()
        self.assertEqual(load_transport(self.settings, "Engine"),
                         TransportSettings(padding_byte=0x55, block_size=8, st_min=0x0A))
        saved = json.loads((self.directory / "config_Engine.json").read_text(encoding="utf-8"))
        self.assertFalse([key for key in saved if key.startswith("isotp")], saved)

    def test_the_dialog_shows_what_was_kept(self):
        save_transport(self.settings, "Engine", TransportSettings(padding=False, st_min=0xF5))
        dialog = ConfigurationDialog(config={"name": "Engine"}, directory=self.directory, settings=self.settings)
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.transport_group.padding_cb.isChecked())
        self.assertFalse(dialog.transport_group.padding_edit.isEnabled())
        self.assertEqual(dialog.transport_group.st_min_label.text(), "500 us")

    def test_a_bad_value_is_not_saved(self):
        dialog = ConfigurationDialog(config={"name": "Engine"}, directory=self.directory, settings=self.settings)
        self.addCleanup(dialog.close)
        dialog.transport_group.st_min_edit.setText("90")
        with patch("canexpert.config.QMessageBox.warning") as warning:
            dialog.save_config()
        self.assertIn("STmin", warning.call_args[0][2])
        self.assertFalse((self.directory / "config_Engine.json").exists())


class OnTheBusTest(unittest.TestCase):
    """What actually goes on the wire, against the simulated ECU."""

    def setUp(self):
        channel = "transport-" + str(uuid.uuid4())
        self.bus = can.Bus(interface="virtual", channel=channel)
        self.ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.spy_bus = can.Bus(interface="virtual", channel=channel)
        self.spy = Recorder()
        self.notifier = can.Notifier(self.spy_bus, [self.spy])
        self.ecu = DummyEcu(self.ecu_bus, EcuConfig(broadcast_interval=0), log=lambda text: None)
        self.stop = threading.Event()
        threading.Thread(target=self.ecu.serve, args=(self.stop,), daemon=True).start()
        self.config = validate_config({"name": "Engine", "request_id": 0x7E0, "response_id": 0x7E8})

    def tearDown(self):
        self.stop.set()
        self.notifier.stop()
        time.sleep(.05)
        for bus in (self.bus, self.ecu_bus, self.spy_bus):
            bus.shutdown()

    def sent_by_tester(self):
        time.sleep(.05)
        return [frame for frame in self.spy.frames if frame.arbitration_id == 0x7E0]

    def test_every_frame_of_an_exchange_is_padded(self):
        session = apply_transport(self.config, TransportSettings())
        reply = uds_request(self.bus, b"\x22\xf1\x90", **uds_transport(session))
        self.assertEqual(reply[3:], b"WVWZZZ1KZAW000001")      # a 20-byte answer: the tester sent flow control
        frames = self.sent_by_tester()
        self.assertGreaterEqual(len(frames), 2)
        self.assertTrue(all(len(frame.data) == 8 for frame in frames), [bytes(f.data).hex() for f in frames])
        self.assertEqual(bytes(frames[0].data), b"\x03\x22\xf1\x90" + b"\xcc" * 4)
        self.assertEqual(bytes(frames[1].data)[:3], b"\x30\x00\x00")
        self.assertEqual(bytes(frames[1].data)[3:], b"\xcc" * 5)

    def test_without_padding_the_frames_are_as_short_as_their_content(self):
        session = apply_transport(self.config, TransportSettings(padding=False))
        uds_request(self.bus, b"\x22\xf1\x90", **uds_transport(session))
        self.assertEqual(bytes(self.sent_by_tester()[0].data), b"\x03\x22\xf1\x90")

    def test_the_ecu_waits_for_flow_control_after_the_block_size_the_tester_asks_for(self):
        session = apply_transport(self.config, TransportSettings(block_size=1))
        reply = uds_request(self.bus, b"\x22\xf1\x90", **uds_transport(session))
        self.assertEqual(reply[3:], b"WVWZZZ1KZAW000001")
        flow_control = [bytes(frame.data) for frame in self.sent_by_tester() if frame.data[0] >> 4 == 0x3]
        # 20 bytes: a first frame and two consecutive frames; one per block means a flow control each time.
        self.assertEqual(len(flow_control), 2, flow_control)
        self.assertTrue(all(frame[:3] == b"\x30\x01\x00" for frame in flow_control))

    def test_the_ecu_spaces_its_frames_by_the_st_min_the_tester_asks_for(self):
        session = apply_transport(self.config, TransportSettings(st_min=0x32))
        uds_request(self.bus, b"\x22\xf1\x90", **uds_transport(session))
        flow_control = [bytes(frame.data) for frame in self.sent_by_tester() if frame.data[0] >> 4 == 0x3]
        self.assertEqual(flow_control[0][:3], b"\x30\x00\x32")
        consecutive = [frame for frame in self.spy.frames if frame.arbitration_id == 0x7E8 and frame.data[0] >> 4 == 2]
        self.assertEqual(len(consecutive), 2)
        # 50 ms apart. The virtual bus stamps frames with the wall clock, which on Windows can be one
        # 15.6 ms tick coarse, so the margin is that tick rather than a millisecond.
        self.assertGreaterEqual(consecutive[1].timestamp - consecutive[0].timestamp, 0.050 - 0.016)

    def test_tester_present_is_padded_like_the_rest(self):
        for transport, expected in ((TransportSettings(), b"\x02\x3e\x00" + b"\xcc" * 5),
                                    (TransportSettings(padding=False), b"\x02\x3e\x00")):
            self.spy.frames.clear()
            worker = CanWorker(self.bus, apply_transport(self.config, transport))
            worker.start()
            deadline = time.monotonic() + 3
            while not self.sent_by_tester() and time.monotonic() < deadline:
                time.sleep(.02)
            worker.stop()
            self.assertEqual(bytes(self.sent_by_tester()[0].data), expected)


if __name__ == "__main__":
    unittest.main()
