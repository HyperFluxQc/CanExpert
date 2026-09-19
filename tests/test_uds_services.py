"""UDS/ISO-TP transport tests over python-can's virtual interface; no hardware is contacted."""
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

import can

from panel_runtime import DatabaseAPI, ReceiveMailbox
from uds_services import (
    isotp_recv, isotp_send, load_firmware, uds_rdbi, uds_request, uds_request_download, uds_tester_present,
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
        namespace = {}
        exec(compile(EXAMPLE_SCRIPT.read_text(encoding="utf-8"), str(EXAMPLE_SCRIPT), "exec"), namespace)
        api = DatabaseAPI(self.mailbox, TESTER_ID, ECU_ID)
        self.assertTrue(namespace["Flashing"](api, firmware))
        for base, data in firmware_data.items():
            self.assertEqual(bytes(bootloader.memory[base + i] for i in range(len(data))), data)
        self.assertEqual(bootloader.log[:6], [0x10, 0x85, 0x28, 0x10, 0x27, 0x27])
        self.assertEqual(bootloader.log[-2:], [0x31, 0x11])


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
