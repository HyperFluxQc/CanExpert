"""UDS/ISO-TP transport tests over python-can's virtual interface; no hardware is contacted."""
import threading
import unittest
import uuid

import can

from panel_runtime import ReceiveMailbox
from uds_services import (
    isotp_recv, isotp_send, uds_rdbi, uds_request, uds_request_download, uds_tester_present,
)

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


if __name__ == "__main__":
    unittest.main()
