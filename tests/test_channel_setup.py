"""Channel setup: sample point and SJW, listen-only, receive filters, and finding a bus's bit rate."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from canexpert import channel_setup
from canexpert.channel_setup import (ChannelSetup, SetupError, bit_timing, bus_options, detect_bitrate,
                                     load_setup, open_configured, parse_filters, range_masks, save_setup,
                                     session_filters)
from canexpert.channel_setup_dialog import ChannelSetupDialog
from canexpert.config import ConfigurationDialog

APP = QApplication.instance() or QApplication([])
KVASER = {"interface": "kvaser", "channel": 0, "serial": 11}


def spin_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.01)
    return False


def lets_through(filters, identifier, extended=False):
    return any(item["extended"] == extended and (identifier & item["can_mask"]) == (item["can_id"] & item["can_mask"])
               for item in filters)


class FilterTest(unittest.TestCase):
    def test_a_range_lets_exactly_its_identifiers_through(self):
        for first, last in ((0x300, 0x3FF), (0x7E0, 0x7E7), (0x301, 0x3FE), (0x123, 0x123), (0, 0x7FF)):
            filters = range_masks(first, last, False)
            passed = [identifier for identifier in range(0x800) if lets_through(filters, identifier)]
            self.assertEqual(passed, list(range(first, last + 1)), f"{first:X}-{last:X}")

    def test_an_aligned_range_is_one_identifier_and_mask(self):
        self.assertEqual(range_masks(0x300, 0x3FF, False), [{"can_id": 0x300, "can_mask": 0x700, "extended": False}])

    def test_the_text_a_user_types(self):
        filters = parse_filters("7E8, 300-3FF; 18DAF100x")
        self.assertTrue(lets_through(filters, 0x7E8))
        self.assertTrue(lets_through(filters, 0x345))
        self.assertTrue(lets_through(filters, 0x18DAF100, extended=True))
        self.assertFalse(lets_through(filters, 0x7E9))
        self.assertFalse(lets_through(filters, 0x7E8, extended=True))
        self.assertEqual(parse_filters("  "), [])

    def test_text_that_is_not_identifiers_is_refused_with_the_culprit(self):
        for text, culprit in (("7E8, hello", "hello"), ("3FF-300", "backwards")):
            with self.assertRaises(SetupError) as raised:
                parse_filters(text)
            self.assertIn(culprit, str(raised.exception))
        with self.assertRaises(SetupError):
            parse_filters(", ".join(f"{identifier:X}" for identifier in range(0x100, 0x100 + 2 * 40, 2)))

    def test_a_session_always_lets_its_ecu_answers_through(self):
        config = {"identifier_11_bit": True, "response_id": 0x7E8, "response_ids": [0x7E8, 0x7E9]}
        filters = session_filters(ChannelSetup(filters="300-3FF"), config)
        self.assertTrue(all(lets_through(filters, identifier) for identifier in (0x7E8, 0x7E9, 0x300)))
        self.assertFalse(lets_through(filters, 0x100))
        self.assertIsNone(session_filters(ChannelSetup(), config), "no filter means everything")


class TimingTest(unittest.TestCase):
    def test_a_sample_point_becomes_the_adapters_bit_timing(self):
        timing = bit_timing("kvaser", 500_000, ChannelSetup(sample_point=87.5))
        self.assertEqual((timing.bitrate, timing.f_clock), (500_000, 16_000_000))
        self.assertAlmostEqual(timing.sample_point, 87.5)
        self.assertIs(bus_options("kvaser", 500_000, ChannelSetup(sample_point=87.5))["timing"].__class__,
                      can.BitTiming)
        self.assertIsNone(bit_timing("kvaser", 500_000, ChannelSetup()), "0 leaves it to the adapter")

    def test_the_sjw_can_be_set_on_top(self):
        timing = bit_timing("vector", 250_000, ChannelSetup(sample_point=80, sjw=1))
        self.assertEqual(timing.sjw, 1)
        self.assertEqual(timing.bitrate, 250_000)

    def test_what_python_can_cannot_do_is_said(self):
        with self.assertRaises(SetupError) as raised:
            bit_timing("ixxat", 500_000, ChannelSetup(sample_point=80))
        self.assertIn("ixxat", str(raised.exception))
        with self.assertRaises(SetupError):
            bus_options("ixxat", 500_000, ChannelSetup(listen_only=True))
        with self.assertRaises(SetupError):
            bit_timing("kvaser", 500_000, ChannelSetup(sample_point=10))

    def test_listen_only_is_each_adapters_own_switch(self):
        self.assertEqual(bus_options("kvaser", 500_000, ChannelSetup(listen_only=True)), {"driver_mode": False})
        self.assertEqual(bus_options("vector", 500_000, ChannelSetup(listen_only=True)), {"listen_only": True})
        self.assertEqual(bus_options("kvaser", 500_000, ChannelSetup()), {})


class OpeningTest(unittest.TestCase):
    def setUp(self):
        self.channel = {"interface": "virtual", "channel": "setup-" + str(uuid.uuid4())}
        self.other = can.Bus(interface="virtual", channel=self.channel["channel"])
        self.addCleanup(self.other.shutdown)

    def test_a_listen_only_channel_receives_and_refuses_to_send(self):
        bus = open_configured(self.channel, 500_000, ChannelSetup(listen_only=True))
        self.addCleanup(bus.shutdown)
        self.other.send(can.Message(arbitration_id=0x123, data=b"\x01", is_extended_id=False))
        self.assertEqual(bus.recv(1).arbitration_id, 0x123)
        with self.assertRaises(can.CanOperationError):
            bus.send(can.Message(arbitration_id=0x7E0, data=b"\x02\x3e\x00", is_extended_id=False))

    def test_the_receive_filter_is_applied_with_the_session_answers(self):
        config = {"identifier_11_bit": True, "response_id": 0x7E8, "response_ids": [0x7E8]}
        bus = open_configured(self.channel, 500_000, ChannelSetup(filters="300-3FF"), config)
        self.addCleanup(bus.shutdown)
        for identifier in (0x100, 0x345, 0x7E8):
            self.other.send(can.Message(arbitration_id=identifier, data=b"\x00", is_extended_id=False))
        seen = []
        while (message := bus.recv(0.2)) is not None:
            seen.append(message.arbitration_id)
        self.assertEqual(seen, [0x345, 0x7E8])

    def test_a_bad_setup_is_refused_before_the_adapter_is_opened(self):
        with patch.object(channel_setup, "open_channel") as opened:
            with self.assertRaises(SetupError):
                open_configured(self.channel, 500_000, ChannelSetup(filters="nonsense"))
        opened.assert_not_called()


class FakeBus:
    """A bus at one bit rate: clean frames at the bus's own rate, error frames at any other."""

    def __init__(self, bitrate, bus_rate):
        self.frames = [can.Message(arbitration_id=0x100, data=b"\x01", is_extended_id=False)] * 10 \
            if bitrate == bus_rate else [can.Message(is_error_frame=True)] * 3

    def recv(self, timeout=None):
        return self.frames.pop() if self.frames else None

    def shutdown(self):
        pass


class DetectTest(unittest.TestCase):
    def detect(self, bus_rate, channel=KVASER):
        opened = []

        def opener(channel_config, bitrate, **options):
            opened.append((bitrate, options))
            return FakeBus(bitrate, bus_rate)

        with patch.object(channel_setup, "open_channel", opener):
            bitrate, report = detect_bitrate(channel, listen_time=0.1)
        return bitrate, report, opened

    def test_the_bit_rate_that_carries_clean_frames_is_found(self):
        bitrate, report, opened = self.detect(250_000)
        self.assertEqual(bitrate, 250_000)
        self.assertEqual(report[0][:3], (500_000, 0, 3))              # 500 kbit/s tried first: error frames
        self.assertTrue(all(options == {"driver_mode": False} for _, options in opened),
                        "every guess is made listen-only, so a wrong one disturbs nothing")

    def test_a_quiet_bus_gives_no_answer(self):
        bitrate, report, _ = self.detect(bus_rate=None)
        self.assertIsNone(bitrate)
        self.assertEqual(len(report), len(channel_setup.DETECT_BITRATES))

    def test_an_adapter_without_listen_only_is_not_guessed_at(self):
        with self.assertRaises(SetupError):
            detect_bitrate({"interface": "ixxat", "channel": 0})


class SettingsAndDialogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat)

    def test_each_channel_keeps_its_own_setup(self):
        save_setup(self.settings, KVASER, ChannelSetup(sample_point=75, listen_only=True))
        self.assertEqual(load_setup(self.settings, KVASER), ChannelSetup(sample_point=75, listen_only=True))
        self.assertEqual(load_setup(self.settings, dict(KVASER, channel=1)), ChannelSetup())

    def test_the_dialog_shows_the_timing_it_will_use(self):
        dialog = ChannelSetupDialog(KVASER, ChannelSetup(), 500_000)
        self.addCleanup(dialog.close)
        self.assertIn("the adapter chooses", dialog.timing_label.text())
        dialog.sample_point_spin.setValue(87.5)
        self.assertIn("TSEG1 13, TSEG2 2", dialog.timing_label.text())
        dialog.filter_edit.setText("300-3FF")
        dialog.listen_only_cb.setChecked(True)
        dialog._accept()
        self.assertEqual(dialog.setup, ChannelSetup(sample_point=87.5, listen_only=True, filters="300-3FF"))

    def test_the_dialog_refuses_what_cannot_be_done(self):
        dialog = ChannelSetupDialog(KVASER, ChannelSetup(), 500_000)
        self.addCleanup(dialog.close)
        dialog.filter_edit.setText("7E8, hello")
        dialog._accept()
        self.assertIn("hello", dialog.message.text())
        self.assertEqual(dialog.result(), 0)
        ixxat = ChannelSetupDialog({"interface": "ixxat", "channel": 0}, ChannelSetup(), 500_000)
        self.addCleanup(ixxat.close)
        self.assertFalse(ixxat.listen_only_cb.isEnabled())
        self.assertFalse(ixxat.detect_btn.isEnabled())

    def test_finding_the_bit_rate_from_the_dialog(self):
        dialog = ChannelSetupDialog(KVASER, ChannelSetup(), 500_000)
        self.addCleanup(dialog.close)
        with patch.object(channel_setup, "open_channel", lambda cfg, rate, **options: FakeBus(rate, 125_000)):
            detector = dialog.find_bitrate(listen_time=0.05)
            self.assertTrue(spin_until(lambda: detector.isFinished()))
            APP.processEvents()
        self.assertIn("Traffic at 125 kbit/s", dialog.detect_label.text())
        self.assertIn("the configuration uses 500 kbit/s", dialog.detect_label.text())

    def test_a_channel_in_use_cannot_be_probed(self):
        dialog = ChannelSetupDialog(KVASER, ChannelSetup(), 500_000, in_use=True)
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.detect_btn.isEnabled())
        self.assertIn("Disconnect first", dialog.detect_label.text())

    def test_the_configuration_takes_any_bit_rate(self):
        directory = Path(self.temp.name) / "Configurations"
        dialog = ConfigurationDialog(config={"name": "Truck"}, directory=directory, settings=self.settings)
        self.addCleanup(dialog.close)
        dialog.bitrate_combo.setCurrentText("83333")
        self.assertEqual(dialog._read()["bitrate"], 83333)
        dialog.bitrate_combo.setCurrentText("fast")
        with self.assertRaises(ValueError):
            dialog._read()


if __name__ == "__main__":
    unittest.main()
