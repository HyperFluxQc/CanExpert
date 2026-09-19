"""Dummy ECU behaviour over python-can's virtual interface (the same code runs on Kvaser virtual channels)."""
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

import can

from dummy_ecu import DummyEcu, EcuConfig, claim_channel, other_ecu_present
from panel_runtime import DatabaseAPI, ReceiveMailbox
from uds_library import UdsFunctions
from uds_services import Firmware, IsoTpError, load_firmware, uds_rdbi, uds_request

EXAMPLE_SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "example_2026-09-18_script.py"
PHYSICAL, FUNCTIONAL, RESPONSE = 0x7E0, 0x7DF, 0x7E8


class DummyEcuTest(unittest.TestCase):
    def setUp(self):
        channel = self.channel = "dummy-" + str(uuid.uuid4())
        self.temp = tempfile.TemporaryDirectory()
        self.dump = Path(self.temp.name) / "flashed.s19"
        self.ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.app_bus = can.Bus(interface="virtual", channel=channel)
        self.ecu = DummyEcu(self.ecu_bus, EcuConfig(erase_seconds=0.05, s3_timeout=0.4, broadcast_interval=0.02,
                                                    dump_path=str(self.dump)), log=lambda text: None)
        self.stop = threading.Event()
        self.ecu_thread = threading.Thread(target=self.ecu.serve, args=(self.stop,), daemon=True)
        self.ecu_thread.start()
        self.mailbox = ReceiveMailbox(self.app_bus)
        self.broadcast_ids = []
        self.pumping = True
        self.pump = threading.Thread(target=self._pump, daemon=True)
        self.pump.start()

    def _pump(self):
        while self.pumping:
            message = self.app_bus.recv(0.01)
            if message:
                if message.arbitration_id in (0x300, 0x301):
                    self.broadcast_ids.append(message.arbitration_id)
                self.mailbox.push(message)

    def tearDown(self):
        self.stop.set()
        self.pumping = False
        self.ecu_thread.join(1)
        self.pump.join(1)
        self.ecu_bus.shutdown()
        self.app_bus.shutdown()
        self.temp.cleanup()

    def request(self, payload, request_id=PHYSICAL, timeout=1.0):
        return uds_request(self.mailbox, bytes(payload), request_id, RESPONSE, timeout)

    def test_example_flashing_updates_software_version(self):
        api = DatabaseAPI(self.mailbox, PHYSICAL, RESPONSE)
        logs = []
        api._log_cb = logs.append
        namespace = UdsFunctions(lambda payload, timeout, wait: api.uds.request(payload, timeout, wait)).namespace()
        exec(compile(EXAMPLE_SCRIPT.read_text(encoding="utf-8"), str(EXAMPLE_SCRIPT), "exec"), namespace)
        image = bytes((i * 13) & 0xFF for i in range(3000))
        firmware = Firmware("app.s19", [(0x10000, image), (0x20000, b"calibration")])
        self.assertEqual(uds_rdbi(self.mailbox, 0xF195, PHYSICAL, RESPONSE), b"APP-1.0.0")
        self.assertTrue(namespace["Flashing"](api, firmware))
        self.assertEqual(load_firmware(self.dump).segments, firmware.segments)
        self.assertTrue(logs[-1].startswith("Flashing complete, software version: APP-FLASHED-"), logs)
        self.assertTrue(uds_rdbi(self.mailbox, 0xF195, PHYSICAL, RESPONSE).startswith(b"APP-FLASHED-"))
        self.assertEqual(uds_rdbi(self.mailbox, 0xF186, PHYSICAL, RESPONSE), b"\x01")

    def test_session_and_security_rules(self):
        self.assertEqual(self.request([0x27, 0x01]), b"\x7F\x27\x7F")          # not in default session
        self.assertEqual(self.request([0x10, 0x02]), b"\x7F\x10\x22")          # extended session first
        self.assertEqual(self.request([0x34, 0x00, 0x44, 0, 0, 0, 0, 0, 0, 0, 1]), b"\x7F\x34\x7F")
        self.assertEqual(self.request([0x10, 0x03])[:2], b"\x50\x03")
        self.assertEqual(self.request([0x2E, 0xF1, 0x90, *b"X" * 17]), b"\x7F\x2E\x33")  # locked
        for expected_nrc in (0x35, 0x35, 0x36):
            self.assertEqual(self.request([0x27, 0x01])[:2], b"\x67\x01")
            self.assertEqual(self.request([0x27, 0x02, 0, 0, 0, 0]), bytes([0x7F, 0x27, expected_nrc]))
        self.assertEqual(self.request([0x27, 0x01]), b"\x7F\x27\x37")          # lockout delay

    def test_functional_addressing(self):
        self.assertEqual(self.request([0x3E, 0x00], FUNCTIONAL), b"\x7E\x00")
        self.assertIsNone(self.request([0x3E, 0x80], FUNCTIONAL, timeout=0.3))      # suppressPosRspMsgIndicationBit
        self.assertIsNone(self.request([0xBA], FUNCTIONAL, timeout=0.3))           # NRC 0x11 suppressed
        self.assertEqual(self.request([0xBA]), b"\x7F\xBA\x11")

    def test_s3_timeout_and_communication_control(self):
        self.assertEqual(self.request([0x10, 0x03])[:2], b"\x50\x03")
        self.assertEqual(self.request([0x28, 0x03, 0x01]), b"\x68\x03")
        time.sleep(0.1)
        self.broadcast_ids.clear()
        time.sleep(0.2)
        self.assertEqual(self.broadcast_ids, [])                                # normal traffic disabled
        time.sleep(0.4)                                                          # exceeds S3 = 0.4 s
        self.assertEqual(uds_rdbi(self.mailbox, 0xF186, PHYSICAL, RESPONSE), b"\x01")
        time.sleep(0.1)
        self.assertIn(0x300, self.broadcast_ids)                                 # default session restores it

    def test_a_second_ecu_on_the_channel_is_detected(self):
        channel = "lonely-" + str(uuid.uuid4())
        with can.Bus(interface="virtual", channel=channel) as empty:
            self.assertFalse(other_ecu_present(empty, EcuConfig(), listen=0.2))
        self.assertTrue(other_ecu_present(self.app_bus, EcuConfig(), listen=0.4))  # the ECU of setUp answers

    def test_one_dummy_ecu_per_channel_on_this_computer(self):
        channel = "lock-" + str(uuid.uuid4())
        first = claim_channel("kvaser", channel)
        self.assertIsNotNone(first)
        self.assertIsNone(claim_channel("kvaser", channel))                     # a second copy is refused
        first.close()
        again = claim_channel("kvaser", channel)
        self.assertIsNotNone(again)
        again.close()

    def test_flow_control_on_segmented_requests(self):
        self.ecu.config.block_size, self.ecu.config.st_min, self.ecu.config.flow_waits = 2, 0x05, 1
        with can.Bus(interface="virtual", channel=self.channel) as sniffer:
            dids = [0xF187, 0xF18C, 0xF190, 0xF195] * 4                          # 33 bytes: FF + 4 CFs
            reply = self.request(bytes([0x22]) + b"".join(did.to_bytes(2, "big") for did in dids))
            self.assertEqual(reply[:18], b"\x62\xF1\x87CANEXPERT-DUMMY")
            flow, deadline = [], time.monotonic() + 0.3
            while time.monotonic() < deadline:
                message = sniffer.recv(0.05)
                if message and message.arbitration_id == RESPONSE and message.data[0] >> 4 == 0x3:
                    flow.append(bytes(message.data[:3]))
        # WAIT, then ContinueToSend (BS 2, STmin 5 ms), after the first frame and again after 2 frames
        self.assertEqual(flow, [b"\x31\x00\x00", b"\x30\x02\x05"] * 2)
        with self.assertRaisesRegex(IsoTpError, "overflow"):                    # longer than maxNumberOfBlockLength
            self.request(bytes([0x36, 0x01]) + bytes(0x402))

    def test_dtc_services(self):
        self.assertEqual(self.request([0x19, 0x02, 0xFF]), b"\x59\x02\xFF\x01\x01\x00\x09\xC1\x00\x00\x08")
        self.assertEqual(self.request([0x14, 0xFF, 0xFF, 0xFF]), b"\x54")
        self.assertEqual(self.request([0x19, 0x01, 0xFF]), b"\x59\x01\xFF\x01\x00\x00")


if __name__ == "__main__":
    unittest.main()
