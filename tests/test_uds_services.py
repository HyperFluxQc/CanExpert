"""UDS/ISO-TP transport tests over python-can's virtual interface; no hardware is contacted."""
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import can

import uds_services
from panel_runtime import DatabaseAPI, ReceiveMailbox
from uds_library import UdsFunctions
from uds_services import (
    IsoTpError, isotp_recv, isotp_send, load_firmware, uds_rdbi, uds_request, uds_request_download,
    uds_tester_present,
)

EXAMPLE_SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "example_2026-09-18_script.py"


def srecord(address, data):
    """S3 record (4-byte address) with checksum."""
    body = bytes([len(data) + 5]) + address.to_bytes(4, "big") + data
    return "S3" + (body + bytes([~sum(body) & 0xFF])).hex().upper()


def intel_hex(kind, address, data):
    body = bytes([len(data)]) + address.to_bytes(2, "big") + bytes([kind]) + data
    return ":" + (body + bytes([-sum(body) & 0xFF])).hex().upper()


def write_file(lines, suffix):
    handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False)
    handle.write("\n".join(lines) + "\n")
    handle.close()
    return handle.name


class FakeBootloader:
    """ISO 14229 programming sequence as the example Flashing() expects it."""

    def __init__(self):
        self.memory, self.log, self.unlocked, self.download = {}, [], False, None

    def __call__(self, request):
        sid = request[0]
        self.log.append(sid)
        if sid == 0x10:
            return [bytes([0x50, request[1], 0x00, 0x32, 0x01, 0xF4])]
        if sid in (0x85, 0x28, 0x11):
            return [bytes([sid + 0x40, request[1]])]
        if sid == 0x27 and request[1] == 0x01:
            return [b"\x67\x01\x12\x34"]
        if sid == 0x27 and request[1] == 0x02:
            self.unlocked = request[2:] == bytes([0x12 ^ 0xA5, 0x34 ^ 0xA5])
            return [b"\x67\x02" if self.unlocked else b"\x7F\x27\x35"]
        if not self.unlocked:
            return [bytes([0x7F, sid, 0x33])]
        if sid == 0x31:
            return [b"\x7F\x31\x78", bytes([0x71, 0x01, request[2], request[3], 0x00])]
        if sid == 0x34:
            self.download = [int.from_bytes(request[3:7], "big"), 1]
            return [b"\x74\x20\x01\x02"]  # maxNumberOfBlockLength 0x0102
        if sid == 0x36:
            address, expected = self.download
            if request[1] != expected:
                return [b"\x7F\x36\x73"]
            for offset, value in enumerate(request[2:]):
                self.memory[address + offset] = value
            self.download = [address + len(request) - 2, (expected + 1) & 0xFF]
            return [bytes([0x76, request[1]])]
        if sid == 0x37:
            return [b"\x77"]
        return [bytes([0x7F, sid, 0x11])]


TESTER_ID, ECU_ID = 0x7E0, 0x7E8


class FakeEcu(threading.Thread):
    """Answers each ISO-TP request with handler(request) -> list of replies."""

    def __init__(self, bus, handler, request_id=TESTER_ID, response_id=ECU_ID, **transport):
        super().__init__(daemon=True)
        self.bus, self.handler = bus, handler
        self.request_id, self.response_id, self.transport = request_id, response_id, transport
        self.requests = []
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            request = isotp_recv(self.bus, self.request_id, self.response_id, 0.1, **self.transport)
            if request is None:
                continue
            self.requests.append(request)
            for reply in self.handler(request):
                isotp_send(self.bus, self.response_id, reply, self.request_id, **self.transport)


class UdsTransportTest(unittest.TestCase):
    def setUp(self):
        channel = "uds-" + str(uuid.uuid4())
        self.app_bus = can.Bus(interface="virtual", channel=channel)
        self.ecu_bus = can.Bus(interface="virtual", channel=channel)
        # Like the session CanWorker: one reader feeds the script mailbox.
        self.mailbox = ReceiveMailbox(self.app_bus)
        self.pumping = True
        self.pump = threading.Thread(target=self._pump, daemon=True)
        self.pump.start()
        self.ecus = []

    def _pump(self):
        while self.pumping:
            message = self.app_bus.recv(0.02)
            if message:
                self.mailbox.push(message)

    def start_ecu(self, handler, **kwargs):
        ecu = FakeEcu(self.ecu_bus, handler, **kwargs)
        ecu.start()
        self.ecus.append(ecu)
        return ecu

    def tearDown(self):
        for ecu in self.ecus:
            ecu.stop.set()
            ecu.join(1)
        self.pumping = False
        self.pump.join(1)
        self.app_bus.shutdown()
        self.ecu_bus.shutdown()

    def test_stale_reply_is_not_accepted(self):
        stale = can.Message(arbitration_id=ECU_ID, data=[6, 0x62, 0xF1, 0x90, 0xDE, 0xAD, 0x00], is_extended_id=False)
        self.mailbox.push(stale)
        self.start_ecu(lambda req: [bytes([0x62, 0xF1, 0x90, 0x12, 0x34])])
        self.assertEqual(uds_rdbi(self.mailbox, 0xF190, TESTER_ID, ECU_ID), b"\x12\x34")

    def test_multi_frame_request_and_reply_with_response_pending(self):
        vin = b"WVWZZZ1KZAW000001"
        ecu = self.start_ecu(lambda req: [b"\x7F\x2E\x78", b"\x6E" + req[1:3]] if req[0] == 0x2E
                             else [b"\x62\xF1\x90" + vin])
        reply = uds_request(self.mailbox, b"\x2E\xF1\x90" + vin, TESTER_ID, ECU_ID)
        self.assertEqual(reply, b"\x6E\xF1\x90")
        self.assertEqual(ecu.requests[0], b"\x2E\xF1\x90" + vin)
        self.assertEqual(uds_rdbi(self.mailbox, 0xF190, TESTER_ID, ECU_ID), vin)

    def test_29_bit_identifiers_and_address_byte(self):
        tester, ecu_id = 0x18DA10F1, 0x18DAF110
        self.start_ecu(lambda req: [b"\x7E\x00"], request_id=tester, response_id=ecu_id,
                       extended=True, address_byte=0x10)
        self.assertTrue(uds_tester_present(self.mailbox, tester, ecu_id, extended=True, address_byte=0x10))
        # An unrelated reply (TesterPresent) must not satisfy a ReadDataByIdentifier.
        self.assertIsNone(uds_rdbi(self.mailbox, 0xF190, tester, ecu_id, timeout=0.3, extended=True,
                                   address_byte=0x10))

    def test_reply_with_wrong_identifier_size_is_ignored(self):
        def reply_as_29_bit():
            if self.ecu_bus.recv(1.0):
                self.ecu_bus.send(can.Message(arbitration_id=ECU_ID, data=[2, 0x7E, 0], is_extended_id=True))
        responder = threading.Thread(target=reply_as_29_bit, daemon=True)
        responder.start()
        self.assertFalse(uds_tester_present(self.mailbox, TESTER_ID, ECU_ID, timeout=0.3))
        responder.join(1)

    def test_request_download_frames(self):
        ecu = self.start_ecu(lambda req: [b"\x74\x20\x01\x02"])
        self.assertTrue(uds_request_download(self.mailbox, 0x44, 0x1000, 0x200, TESTER_ID, ECU_ID))
        self.assertEqual(ecu.requests[0], bytes([0x34, 0x00, 0x44, 0, 0, 0x10, 0, 0, 0, 0x02, 0]))

    def test_mailbox_marks_transaction(self):
        seen = []
        self.start_ecu(lambda req: seen.append(self.mailbox.in_transaction) or [b"\x7E\x00"])
        self.assertTrue(uds_tester_present(self.mailbox, TESTER_ID, ECU_ID))
        self.assertEqual(seen, [True])
        self.assertFalse(self.mailbox.in_transaction)

    def test_example_flashing_sequence(self):
        firmware_data = {0x8000: bytes(range(256)) * 2 + b"tail", 0x9000: b"second segment"}
        lines = ["S0030000FC"]
        for base, data in firmware_data.items():
            lines += [srecord(base + offset, data[offset:offset + 32]) for offset in range(0, len(data), 32)]
        firmware = load_firmware(write_file(lines, ".s37"))
        self.assertEqual(firmware.segments, sorted(firmware_data.items()))
        bootloader = FakeBootloader()
        self.start_ecu(bootloader)
        api = DatabaseAPI(self.mailbox, TESTER_ID, ECU_ID)
        namespace = UdsFunctions(lambda payload, timeout, wait: api.uds.request(payload, timeout, wait)).namespace()
        exec(compile(EXAMPLE_SCRIPT.read_text(encoding="utf-8"), str(EXAMPLE_SCRIPT), "exec"), namespace)
        self.assertTrue(namespace["Flashing"](api, firmware))
        for base, data in firmware_data.items():
            self.assertEqual(bytes(bootloader.memory[base + i] for i in range(len(data))), data)
        self.assertEqual(bootloader.log[:6], [0x10, 0x85, 0x28, 0x10, 0x27, 0x27])
        self.assertEqual(bootloader.log[-3:], [0x31, 0x11, 0x22])          # check, reset, read new version


class Background(threading.Thread):
    """Runs target(*args, **kwargs) and keeps its result or exception."""

    def __init__(self, target, *args, **kwargs):
        super().__init__(daemon=True)
        self.call = lambda: target(*args, **kwargs)
        self.result = self.error = None
        self.start()

    def run(self):
        try:
            self.result = self.call()
        except Exception as exc:
            self.error = exc


class FlowControlTest(unittest.TestCase):
    """ISO 15765-2 flow control, frame by frame: the test plays the ECU on the other end."""

    def setUp(self):
        channel = "fc-" + str(uuid.uuid4())
        self.tester = can.Bus(interface="virtual", channel=channel)
        self.ecu = can.Bus(interface="virtual", channel=channel)
        self.sniffer = can.Bus(interface="virtual", channel=channel)

    def tearDown(self):
        for bus in (self.tester, self.ecu, self.sniffer):
            bus.shutdown()

    def from_ecu(self, *data):
        self.ecu.send(can.Message(arbitration_id=ECU_ID, data=bytes(data), is_extended_id=False))

    def send_first_frame(self, payload):
        sending = Background(isotp_send, self.tester, TESTER_ID, payload, ECU_ID)
        self.assertEqual(self.ecu.recv(1).data[0] >> 4, 0x1)
        return sending

    def test_sender_follows_block_size_and_stmin(self):
        payload = bytes(range(40))                                       # first frame + 5 consecutive frames
        sending = self.send_first_frame(payload)
        self.from_ecu(0x30, 2, 30)                                       # 2 frames per block, STmin 30 ms
        block = []
        for _ in range(2):
            block.append((self.ecu.recv(1), time.perf_counter()))
        self.assertGreaterEqual(block[1][1] - block[0][1], 0.025)
        self.assertIsNone(self.ecu.recv(0.15))                           # waits for the next flow control
        self.from_ecu(0x30, 0, 0)                                        # the rest without limit
        frames = [message for message, _ in block] + [self.ecu.recv(1) for _ in range(3)]
        sending.join(1)
        self.assertIsNone(sending.error)
        self.assertEqual([message.data[0] for message in frames], [0x21, 0x22, 0x23, 0x24, 0x25])
        self.assertEqual(bytes(range(6)) + b"".join(bytes(m.data[1:]) for m in frames), payload)

    def test_sender_handles_wait_overflow_and_missing_flow_control(self):
        sending = self.send_first_frame(bytes(20))
        for _ in range(3):
            self.from_ecu(0x31, 0, 0)                                    # WAIT: the ECU is not ready yet
            self.assertIsNone(self.ecu.recv(0.1))
        self.from_ecu(0x30, 0, 0)
        self.assertEqual([self.ecu.recv(1).data[0] for _ in range(2)], [0x21, 0x22])
        sending.join(1)
        self.assertIsNone(sending.error)
        for status, message in ((0x32, "overflow"), (0x37, "Invalid flow status 0x7")):
            sending = self.send_first_frame(bytes(20))
            self.from_ecu(status, 0, 0)
            sending.join(1)
            self.assertIsInstance(sending.error, IsoTpError)
            self.assertIn(message, str(sending.error))
            self.assertIsNone(self.ecu.recv(0.1))                        # nothing more after an abort
        with patch.object(uds_services, "MAX_FC_WAITS", 2):
            sending = self.send_first_frame(bytes(20))
            for _ in range(3):
                self.from_ecu(0x31, 0, 0)
            sending.join(1)
            self.assertIn("kept sending flow control WAIT", str(sending.error))
        with patch.object(uds_services, "N_BS_TIMEOUT", 0.2):
            with self.assertRaisesRegex(IsoTpError, "No flow control"):
                isotp_send(self.tester, TESTER_ID, bytes(20), ECU_ID)

    def test_receiver_sends_flow_control_after_every_block(self):
        payload = bytes(range(34))                                       # first frame + 4 consecutive frames
        receiving = Background(isotp_recv, self.tester, ECU_ID, TESTER_ID, 1.0, block_size=2, st_min=0x05)
        self.from_ecu(0x10, 34, *payload[:6])
        self.assertEqual(bytes(self.ecu.recv(1).data), b"\x30\x02\x05")
        for index in range(4):
            self.from_ecu(0x21 + index, *payload[6 + 7 * index:13 + 7 * index])
            if index == 1:
                self.assertEqual(bytes(self.ecu.recv(1).data), b"\x30\x02\x05")  # end of the first block
        receiving.join(1)
        self.assertEqual(receiving.result, payload)
        self.assertIsNone(self.ecu.recv(0.1))                            # none after the last frame

    def test_receiver_handles_unexpected_frames(self):
        receiving = Background(isotp_recv, self.tester, ECU_ID, TESTER_ID, 1.0)
        self.from_ecu(0x10, 20, *b"AAAAAA")
        self.ecu.recv(1)
        self.from_ecu(0x21, *b"AAAAAAA")
        self.from_ecu(0x10, 10, *b"BBBBBB")                              # a new first frame replaces it
        self.assertEqual(bytes(self.ecu.recv(1).data), b"\x30\x00\x00")
        self.from_ecu(0x21, *b"BBBB")
        receiving.join(1)
        self.assertEqual(receiving.result, b"B" * 10)

        receiving = Background(isotp_recv, self.tester, ECU_ID, TESTER_ID, 1.0)
        self.from_ecu(0x10, 20, *b"AAAAAA")
        self.ecu.recv(1)
        self.from_ecu(0x02, 0x7E, 0x00)                                  # so does a single frame
        receiving.join(1)
        self.assertEqual(receiving.result, b"\x7E\x00")

        receiving = Background(isotp_recv, self.tester, ECU_ID, TESTER_ID, 1.0)
        self.from_ecu(0x10, 20, *b"AAAAAA")
        self.ecu.recv(1)
        self.from_ecu(0x22, *b"AAAAAAA")                                 # sequence number 2 instead of 1
        receiving.join(1)
        self.assertIn("out of sequence", str(receiving.error))

        self.from_ecu(0x10, 7, 1, 2, 3, 4, 5, 6)                         # fits a single frame: invalid
        self.assertIsNone(isotp_recv(self.tester, ECU_ID, TESTER_ID, 0.2))
        self.assertIsNone(self.ecu.recv(0.1))                            # and not answered

    def test_messages_longer_than_4095_bytes_use_the_escape_sequence(self):
        payload = bytes(i & 0xFF for i in range(5000))
        sending = Background(isotp_send, self.ecu, ECU_ID, payload, TESTER_ID)
        self.assertEqual(isotp_recv(self.tester, ECU_ID, TESTER_ID, 2.0), payload)
        sending.join(1)
        self.assertEqual(bytes(self.sniffer.recv(1).data), bytes([0x10, 0x00, 0x00, 0x00, 0x13, 0x88, 0x00, 0x01]))


class FirmwareFileTest(unittest.TestCase):
    def test_intel_hex_with_extended_linear_address(self):
        path = write_file([intel_hex(4, 0, b"\x08\x00"), intel_hex(0, 0x0010, b"\x01\x02"),
                           intel_hex(0, 0x0012, b"\x03"), intel_hex(0, 0x0100, b"\xFF"),
                           intel_hex(1, 0, b"")], ".hex")
        firmware = load_firmware(path)
        self.assertEqual(firmware.segments, [(0x08000010, b"\x01\x02\x03"), (0x08000100, b"\xFF")])
        self.assertEqual(firmware.size, 4)

    def test_bad_checksum_and_overlap_are_rejected(self):
        bad = srecord(0x100, b"\x01\x02")
        bad = bad[:-2] + ("00" if bad[-2:] != "00" else "01")
        with self.assertRaisesRegex(ValueError, "checksum"):
            load_firmware(write_file([bad], ".s19"))
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            load_firmware(write_file([srecord(0x100, b"\x01\x02"), srecord(0x101, b"\x03")], ".s19"))
        with self.assertRaisesRegex(ValueError, "Unrecognised"):
            load_firmware(write_file(["hello"], ".hex"))


if __name__ == "__main__":
    unittest.main()
