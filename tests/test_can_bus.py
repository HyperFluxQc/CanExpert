"""Opening adapters: the channel dictionaries can.detect_available_configs() returns must open as they are."""
import unittest
from unittest.mock import patch

import can

from canexpert import can_bus
from canexpert.can_bus import channel_key, create_can_bus, open_channel

# What can.detect_available_configs() returns for the two Kvaser virtual channels.
KVASER = {"interface": "kvaser", "channel": 1, "device_name": "Kvaser Virtual CAN Driver", "serial": 0,
          "dongle_channel": 2}


class OpenChannelTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        patcher = patch.object(can_bus.can, "Bus", lambda **kwargs: self.calls.append(kwargs))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_detected_channel_opens_with_its_adapter_options_only(self):
        open_channel(KVASER, 500000)
        self.assertEqual(self.calls, [{"interface": "kvaser", "channel": 1, "bitrate": 500000, "serial": 0}])

    def test_adapter_options_are_forwarded_and_the_rest_dropped(self):
        create_can_bus("ixxat", 0, 250000, unique_hardware_id="HW1", app_name="CANexpert", device_name="ignored")
        self.assertEqual(self.calls, [{"interface": "ixxat", "channel": 0, "bitrate": 250000,
                                       "unique_hardware_id": "HW1", "app_name": "CANexpert"}])

    def test_a_virtual_channel_opens_by_name(self):
        open_channel({"interface": "virtual", "channel": "bench"}, "500000")
        self.assertEqual(self.calls, [{"interface": "virtual", "channel": "bench", "bitrate": 500000}])

    def test_channels_of_different_adapters_have_different_keys(self):
        other = dict(KVASER, serial=7)
        self.assertNotEqual(channel_key(KVASER), channel_key(other))
        self.assertEqual(channel_key(KVASER), channel_key(dict(KVASER, device_name="renamed")))


class RealBusTest(unittest.TestCase):
    def test_open_channel_really_opens_a_bus(self):
        bus = open_channel({"interface": "virtual", "channel": "open-channel-test"}, 500000)
        try:
            self.assertIsInstance(bus, can.BusABC)
        finally:
            bus.shutdown()


if __name__ == "__main__":
    unittest.main()
