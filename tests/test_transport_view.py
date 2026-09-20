"""The transport view: the ISO-TP messages the frames carried, not the frames themselves."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from pathlib import Path

from PyQt5.QtWidgets import QApplication

from canexpert.symbols import SymbolDatabases
from canexpert.trace_window import COL_DATA, COL_DLC, COL_ID, COL_NAME, TraceWindow
from canexpert.uds.observer import assemble, service_name

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parents[1] / "DBC" / "dummy_ecu.dbc"
TESTER, ECU = 0x7E0, 0x7E8
VIN = b"WVWZZZ1KZAW000001"


def frame(timestamp, direction, can_id, data):
    return (timestamp, direction, can_id, bytes(data), False)


def read_vin_exchange():
    """The frames of 22 F1 90 and its multi-frame answer, as they appear on the bus."""
    reply = b"\x62\xf1\x90" + VIN
    return [
        frame(1000.000, "TX", TESTER, b"\x03\x22\xf1\x90"),
        frame(1000.010, "RX", ECU, bytes([0x10, len(reply)]) + reply[:6]),
        frame(1000.012, "TX", TESTER, b"\x30\x08\x14"),          # flow control: continue, BS 8, STmin 20 ms
        frame(1000.032, "RX", ECU, b"\x21" + reply[6:13]),
        frame(1000.052, "RX", ECU, b"\x22" + reply[13:]),
    ]


class ServiceNameTest(unittest.TestCase):
    def test_requests_responses_and_negative_responses(self):
        self.assertEqual(service_name(b"\x22\xf1\x90"), "ReadDataByIdentifier")
        self.assertEqual(service_name(b"\x62\xf1\x90"), "ReadDataByIdentifier response")
        self.assertEqual(service_name(b"\x7f\x22\x31"),
                         "ReadDataByIdentifier negative response (requestOutOfRange)")
        self.assertEqual(service_name(b"\x10\x03"), "DiagnosticSessionControl")
        self.assertEqual(service_name(b""), "")
        self.assertEqual(service_name(b"\xab"), "service 0xAB")


class AssembleTest(unittest.TestCase):
    def test_a_single_frame_is_one_message(self):
        messages = assemble([frame(1000.0, "TX", TESTER, b"\x02\x3e\x00")])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].payload, b"\x3e\x00")
        self.assertEqual(messages[0].service, "TesterPresent")
        self.assertTrue(messages[0].complete)
        self.assertEqual(len(messages[0].frames), 1)

    def test_a_multi_frame_answer_is_put_back_together(self):
        messages = assemble(read_vin_exchange())
        self.assertEqual([message.service for message in messages],
                         ["ReadDataByIdentifier", "ReadDataByIdentifier response"])
        answer = messages[1]
        self.assertEqual(answer.payload, b"\x62\xf1\x90" + VIN)
        self.assertTrue(answer.complete)
        self.assertEqual(answer.can_id, ECU)
        self.assertAlmostEqual(answer.seconds, 0.042, places=6)      # first frame to last
        self.assertEqual([label for _t, label, _d in answer.frames],
                         ["first frame", "flow control: continue, block size 8, STmin 20 ms",
                          "consecutive frame 1", "consecutive frame 2"])
        self.assertEqual(answer.flow_control, [(1000.012, "continue", 8, 0x14)])

    def test_a_message_that_never_finished_says_so(self):
        messages = assemble(read_vin_exchange()[:3])                 # the consecutive frames never came
        answer = messages[1]
        self.assertFalse(answer.complete)
        self.assertIn("incomplete", answer.summary())
        self.assertIn("14 byte(s) missing", answer.summary())

    def test_consecutive_frames_from_before_the_trace_started_are_dropped(self):
        messages = assemble([frame(1000.0, "RX", ECU, b"\x21\x01\x02")])
        self.assertEqual(messages, [])

    def test_the_escape_sequence_of_a_long_message(self):
        payload = bytes(range(64)) * 80                              # 5120 bytes, beyond 4095
        frames = [frame(1000.0, "TX", TESTER, b"\x10\x00" + len(payload).to_bytes(4, "big") + payload[:2])]
        offset, sequence, timestamp = 2, 1, 1000.0
        while offset < len(payload):
            timestamp += 0.001
            frames.append(frame(timestamp, "TX", TESTER, bytes([0x20 | sequence]) + payload[offset:offset + 7]))
            offset, sequence = offset + 7, (sequence + 1) & 0x0F
        message = assemble(frames)[0]
        self.assertEqual(message.announced, len(payload))
        self.assertTrue(message.complete)
        self.assertEqual(message.payload, payload)

    def test_extended_addressing_drops_the_address_byte(self):
        messages = assemble([frame(1000.0, "TX", TESTER, b"\x10\x02\x3e\x00")], address_byte=0x10)
        self.assertEqual(messages[0].payload, b"\x3e\x00")
        self.assertEqual(messages[0].service, "TesterPresent")


class TransportViewTest(unittest.TestCase):
    def setUp(self):
        self.trace = TraceWindow(symbols=SymbolDatabases([str(DBC)], settings=None))
        self.addCleanup(self.trace.close)
        for item in read_vin_exchange():
            self.trace.add_frame(*item)
        self.trace.add_frame(1000.1, "RX", 0x300, b"\x01\x2c\x00\x64\x00\x00\x00\x00")   # not diagnostic
        self.trace.flush()

    def rows(self):
        tree = self.trace.tree
        return [[tree.topLevelItem(row).text(column) for column in range(tree.columnCount())]
                for row in range(tree.topLevelItemCount())]

    def test_without_diagnostic_identifiers_it_says_so_rather_than_guessing(self):
        self.trace.transport_btn.setChecked(True)
        self.assertIn("No diagnostic identifiers", self.rows()[0][COL_NAME])

    def test_the_frames_become_the_messages_they_carried(self):
        self.assertEqual(len(self.rows()), 6)                        # six frames
        self.trace.set_diagnostic_ids({TESTER, ECU})
        self.trace.transport_btn.setChecked(True)
        rows = self.rows()
        self.assertEqual(len(rows), 2)                               # request and answer
        self.assertEqual(rows[0][COL_NAME], "ReadDataByIdentifier")
        self.assertEqual(rows[1][COL_NAME], "ReadDataByIdentifier response")
        self.assertEqual(rows[1][COL_ID], f"{ECU:03X}")
        self.assertEqual(rows[1][COL_DLC], str(3 + len(VIN)))
        self.assertTrue(rows[1][COL_DATA].startswith("62 F1 90"))
        # The application frame is left out of the transport view: it carries no diagnostics.
        self.assertNotIn("300", [row[COL_ID] for row in rows])

    def test_a_message_opens_into_the_frames_that_carried_it(self):
        self.trace.set_diagnostic_ids({TESTER, ECU})
        self.trace.transport_btn.setChecked(True)
        answer = self.trace.tree.topLevelItem(1)
        labels = [answer.child(index).text(COL_NAME) for index in range(answer.childCount())]
        self.assertEqual(labels[0], "first frame")
        self.assertIn("flow control: continue, block size 8, STmin 20 ms", labels)
        self.assertEqual(labels[-1], "consecutive frame 2")
        self.assertEqual(answer.child(0).text(COL_DATA).split()[0], "10")   # the frame as it was seen

    def test_the_status_counts_messages_and_going_back_counts_frames(self):
        self.trace.set_diagnostic_ids({TESTER, ECU})
        self.trace.transport_btn.setChecked(True)
        self.assertIn("2 diagnostic message(s) from 6 frame(s)", self.trace.status.text())
        self.trace.transport_btn.setChecked(False)
        self.assertIn("6 frame(s)", self.trace.status.text())

    def test_frames_arriving_while_the_transport_view_is_on_are_assembled(self):
        self.trace.set_diagnostic_ids({TESTER, ECU})
        self.trace.transport_btn.setChecked(True)
        self.trace.add_frame(1001.0, "TX", TESTER, b"\x02\x10\x03")
        self.trace.flush()
        self.assertEqual(self.rows()[-1][COL_NAME], "DiagnosticSessionControl")


if __name__ == "__main__":
    unittest.main()
