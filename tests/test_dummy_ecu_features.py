"""The Dummy ECU's simulation: signals from a DBC with generators, periodic data and ResponseOnEvent, the fault
memory's life cycle, transport errors on purpose, access rules and security levels, the bootloader, and the
memory and I/O control services - each against the ECU over python-can's virtual interface."""
import tempfile
import threading
import time
import unittest
import uuid
import zlib
from pathlib import Path
from unittest.mock import patch

import can

from canexpert.flash_sequence import FlashProfile, image_crc, run_flash
from canexpert.flashing import Firmware
from canexpert.simulator import ecu as ecu_module
from canexpert.simulator.dtc import (CONFIRMED, FAILED_SINCE_CLEAR, PENDING, TEST_FAILED, TEST_FAILED_THIS_CYCLE,
                                     WARNING_INDICATOR, DtcMemory, status_text)
from canexpert.simulator.ecu import DummyEcu, EcuConfig, application_ids, config_from_dict
from canexpert.simulator.signals import DEFAULT_GENERATORS, SignalSimulation
from canexpert.uds.client import UdsFunctions, uds_request
from canexpert.uds.isotp import IsoTpError, isotp_recv

P0101, U0100 = 0x010100, 0xC10000
REQUEST, RESPONSE = 0x7E0, 0x7E8

CUSTOM_DBC = """VERSION ""

NS_ :

BS_:

BU_: Body

BO_ 1024 Doors: 8 Body
 SG_ Speed : 0|16@1- (0.5,-10) [-100|100] "km/h" Vector__XXX
 SG_ Open : 16|1@1+ (1,0) [0|1] "" Vector__XXX

BO_ 2566889472 Lights: 2 Body
 SG_ Level : 0|8@1+ (1,0) [0|255] "" Vector__XXX

BA_DEF_ BO_ "GenMsgCycleTime" INT 0 10000;
BA_DEF_DEF_ "GenMsgCycleTime" 0;
BA_ "GenMsgCycleTime" BO_ 1024 30;
"""


def quiet(**changes):
    """A configuration without application frames unless asked for."""
    changes.setdefault("broadcast_interval", 0)
    return EcuConfig(**changes)


class Bench:
    """One dummy ECU on a virtual channel, and a tester."""

    def __init__(self, test, config):
        channel = self.channel = "features-" + str(uuid.uuid4())
        self.tester = can.Bus(interface="virtual", channel=channel)
        test.addCleanup(self.tester.shutdown)
        bus = can.Bus(interface="virtual", channel=channel)
        test.addCleanup(bus.shutdown)
        self.logs = []
        self.ecu = DummyEcu(bus, config, log=self.logs.append)
        stop = threading.Event()
        test.addCleanup(stop.set)
        self.thread = threading.Thread(target=self.ecu.serve, args=(stop,), daemon=True)
        self.thread.start()
        self.uds = UdsFunctions(lambda payload, timeout=None, wait=True: uds_request(
            self.tester, bytes(payload), REQUEST, RESPONSE, timeout or 1.0, wait=wait))

    def request(self, payload, timeout=1.0):
        return uds_request(self.tester, bytes(payload), REQUEST, RESPONSE, timeout)

    def unlock(self, level=0x01, mask=0xA5, session=0x03):
        assert self.request([0x10, session])[:2] == bytes([0x50, session])
        seed = self.request([0x27, level])[2:]
        assert self.request([0x27, level + 1, *(byte ^ mask for byte in seed)]) == bytes([0x67, level + 1])

    def frames(self, seconds, predicate=lambda message: True):
        """The frames the tester sees for a while."""
        seen, deadline = [], time.monotonic() + seconds
        while time.monotonic() < deadline:
            message = self.tester.recv(0.01)
            if message is not None and predicate(message):
                seen.append(message)
        return seen


def written(tmpdir, text) -> str:
    path = Path(tmpdir) / "custom.dbc"
    path.write_text(text, encoding="utf-8")
    return str(path)


class SignalTest(unittest.TestCase):
    def engine(self, generators, source="", inputs=None):
        state = inputs if inputs is not None else {"running": False, "logging": False, "session": 1}
        engine = SignalSimulation(lambda: state)
        engine.load(source)
        engine.configure(generators)
        engine.started = 0.0
        return engine

    def frame(self, engine, now, can_id=0x300):
        frames = engine.due_frames(0.1, now)
        return next(bytes(frame.data) for frame in frames if frame.arbitration_id == can_id)

    def test_the_built_in_generators_send_what_the_ecu_always_sent(self):
        state = {"running": False, "logging": False, "session": 1}
        engine = self.engine(DEFAULT_GENERATORS, inputs=state)
        frames = {frame.arbitration_id: bytes(frame.data) for frame in engine.due_frames(0.1, 1.0)}
        self.assertEqual(frames, {0x300: bytes.fromhex("00d7006400000000"), 0x301: bytes.fromhex("0000010100000000")})
        state.update(running=True, logging=True, session=3)
        for step in range(1, 51):                               # five seconds at 100 ms: one time constant
            frames = {frame.arbitration_id: bytes(frame.data) for frame in engine.due_frames(0.1, 1.0 + step * 0.1001)}
        temperature = int.from_bytes(frames[0x300][:2], "big") / 10
        self.assertAlmostEqual(temperature, 21.5 + (85 - 21.5) * (1 - 2.718281828 ** -1), delta=1.0)
        self.assertEqual(frames[0x300][2:4], bytes.fromhex("00b4"), "1.8 bar at once")
        self.assertEqual(frames[0x301][:3], bytes([1, 1, 3]), "running, logging, the extended session")
        self.assertEqual(frames[0x301][3], 51, "the counter counts every frame")

    def test_each_generator(self):
        generator = lambda kind, low, high, period=0.0: {"signal": "EngineData.Temperature", "kind": kind,  # noqa: E731
                                                         "low": low, "high": high, "period": period}
        temperature = lambda data: int.from_bytes(data[:2], "big") / 10       # noqa: E731
        self.assertEqual(temperature(self.frame(self.engine([generator("constant", 42.0, 0)]), 1.0)), 42.0)
        ramp = self.engine([generator("ramp", 10.0, 20.0, 10.0)])
        self.assertEqual(temperature(self.frame(ramp, 2.5)), 12.5)
        self.assertEqual(temperature(self.frame(ramp, 12.5)), 12.5, "and again from Low")
        sine = self.engine([generator("sine", 10.0, 30.0, 4.0)])
        self.assertEqual(temperature(self.frame(sine, 1.0)), 30.0, "a quarter of a wave: the top")
        square = self.engine([generator("square", 5.0, 50.0, 2.0)])
        self.assertEqual([temperature(self.frame(square, now)) for now in (0.5, 1.5)], [50.0, 5.0])
        random_engine = self.engine([generator("random", 30.0, 31.0)])
        values = [temperature(self.frame(random_engine, 1.0 + step)) for step in range(20)]
        self.assertTrue(all(30.0 <= value <= 31.0 for value in values))
        self.assertGreater(len(set(values)), 1)
        counter = self.engine([{"signal": "EcuStatus.Counter", "kind": "counter", "low": 0, "high": 2}])
        self.assertEqual([self.frame(counter, 1.0 + step, 0x301)[3] for step in range(5)], [1, 2, 0, 1, 2])

    def test_values_stay_within_the_signals_bits(self):
        engine = self.engine([{"signal": "EngineData.Temperature", "kind": "constant", "low": 99999.0, "high": 0}])
        self.assertEqual(self.frame(engine, 1.0)[:2], b"\xff\xff")
        self.assertEqual(engine.value("EngineData.Temperature"), 6553.5)

    def test_a_dbc_of_your_own(self):
        with tempfile.TemporaryDirectory() as folder:
            path = written(folder, CUSTOM_DBC)
            engine = self.engine([{"signal": "Doors.Speed", "kind": "constant", "low": -20.5, "high": 0},
                                  {"signal": "Doors.Open", "kind": "square", "low": 0, "high": 1, "period": 1.0}],
                                 source=path)
        frames = engine.due_frames(0.1, 0.1)
        self.assertEqual({frame.arbitration_id for frame in frames}, {0x400, 0x18FFA000})
        lights = next(frame for frame in frames if frame.arbitration_id == 0x18FFA000)
        self.assertTrue(lights.is_extended_id)
        doors = next(bytes(frame.data) for frame in frames if frame.arbitration_id == 0x400)
        self.assertEqual(int.from_bytes(doors[:2], "little", signed=True), -21, "(-20.5 + 10) / 0.5")
        self.assertEqual(doors[2] & 1, 1)
        self.assertEqual({frame.arbitration_id for frame in engine.due_frames(0.1, 0.13)}, {0x400},
                         "Doors every 30 ms (its GenMsgCycleTime), Lights at the default 100 ms")
        engine.configure([], [{"message": "Lights", "on": False}, {"message": "Doors", "cycle_ms": 500}])
        self.assertEqual(engine.message_ids(), {0x400})
        self.assertEqual(len(engine.due_frames(0.1, 0.5)), 1)
        self.assertEqual(engine.due_frames(0.1, 0.6), [], "Doors now every 500 ms")

    def test_the_ecu_sends_them_and_says_which_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            config = EcuConfig(broadcast_interval=0.02, dbc_path=written(folder, CUSTOM_DBC), generators=[])
            self.assertEqual(application_ids(config), {0x400, 0x18FFA000})
            bench = Bench(self, config)
            seen = {message.arbitration_id for message in bench.frames(0.3)}
        self.assertTrue({0x400, 0x18FFA000} <= seen)
        self.assertNotIn(0x300, seen)
        self.assertEqual(application_ids(EcuConfig()), {0x300, 0x301})
        self.assertEqual(application_ids(EcuConfig(broadcast_interval=0)), set())

    def test_a_dbc_that_cannot_be_read_leaves_the_built_in_one(self):
        ecu = DummyEcu(None, EcuConfig(dbc_path="no-such.dbc"), log=lambda text: None)
        self.assertEqual(ecu.signals.message_ids(), {0x300, 0x301})
        ecu.config.dbc_path = "still-no-such.dbc"
        self.assertIn("Cannot read", ecu.refresh())


class IoControlTest(unittest.TestCase):
    def test_a_did_that_follows_a_signal_takes_it_over(self):
        bench = Bench(self, quiet(broadcast_interval=0.02))
        self.assertEqual(bench.request([0x22, 0x01, 0x01]), bytes.fromhex("620101 00d7"), "21.5 degC, live")
        self.assertEqual(bench.request([0x2F, 0x01, 0x01, 0x03, 0x03, 0xE8]), b"\x7f\x2f\x7f", "extended session")
        bench.request([0x10, 0x03])
        self.assertEqual(bench.request([0x2F, 0x01, 0x01, 0x03, 0x03, 0xE8]), bytes.fromhex("6f010103 03e8"))
        engine = [message for message in bench.frames(0.2, lambda m: m.arbitration_id == 0x300)]
        self.assertEqual(bytes(engine[-1].data[:2]), b"\x03\xe8", "the application frames carry 100.0 degC")
        self.assertEqual(bench.request([0x22, 0x01, 0x01]), bytes.fromhex("620101 03e8"))
        self.assertEqual(bench.request([0x2F, 0x01, 0x01, 0x03, 0x03]), b"\x7f\x2f\x13", "two bytes, like the DID")
        self.assertEqual(bench.request([0x2F, 0xF1, 0x90, 0x03, 0x00]), b"\x7f\x2f\x31", "no signal behind it")
        self.assertEqual(bench.request([0x2F, 0x01, 0x01, 0x02]), bytes.fromhex("6f010102 03e8"), "frozen")
        self.assertEqual(bench.request([0x2F, 0x01, 0x01, 0x00]), bytes.fromhex("6f010100 03e8"), "handed back")
        self.assertEqual(bench.ecu.state.io_controls, {})
        self.assertEqual(bench.request([0x2F, 0x01, 0x02, 0x01]), bytes.fromhex("6f010201 0064"), "its default: Low")
        bench.request([0x10, 0x01])                                   # the session ends: control goes back
        self.assertEqual(bench.ecu.signals.overridden(), {})


class PeriodicTest(unittest.TestCase):
    def test_periodic_data_arrives_at_its_rate_until_stopped(self):
        bench = Bench(self, quiet(periodic_rates_ms=(400, 100, 20)))
        self.assertEqual(bench.request([0x2A, 0x03, 0x01]), b"\x6a")
        self.assertEqual(bench.request([0x2A, 0x01, 0x02]), b"\x6a")
        periodic = bench.frames(0.4, lambda m: m.arbitration_id == RESPONSE and m.data[1] == 0x6A)
        fast = [m for m in periodic if m.data[2] == 0x01]
        slow = [m for m in periodic if m.data[2] == 0x02]
        self.assertEqual(bytes(fast[0].data), bytes.fromhex("046a0100d7aaaaaa"), "F201: 21.5 degC")
        self.assertGreater(len(fast), 3 * len(slow))
        self.assertGreaterEqual(len(slow), 1)
        self.assertEqual(bench.request([0x2A, 0x04, 0x01]), b"\x6a")
        periodic = bench.frames(0.5, lambda m: m.arbitration_id == RESPONSE and m.data[1] == 0x6A)
        self.assertEqual({m.data[2] for m in periodic}, {0x02}, "F201 stopped, F202 goes on (every 400 ms)")
        bench.request([0x10, 0x03])                                    # a new session ends it all
        self.assertEqual(bench.ecu.state.periodic, {})

    def test_what_cannot_be_sent_periodically(self):
        bench = Bench(self, quiet(dids=[{"did": 0xF203, "data": "0102030405060708"},
                                        {"did": 0xF204, "data": "01", "sessions": [3]}]))
        self.assertEqual(bench.request([0x2A, 0x03, 0x09]), b"\x7f\x2a\x31", "no F209")
        self.assertEqual(bench.request([0x2A, 0x03, 0x03]), b"\x7f\x2a\x31", "8 bytes do not fit a 6A frame")
        self.assertEqual(bench.request([0x2A, 0x03, 0x04]), b"\x7f\x2a\x31", "not in this session")
        self.assertEqual(bench.request([0x2A, 0x07, 0x04]), b"\x7f\x2a\x31", "no such transmission mode")

    def test_frames_of_their_own_id(self):
        bench = Bench(self, quiet(periodic_id=0x5E8, dids=[{"did": 0xF201, "data": "0102030405060708"}]))
        self.assertEqual(bench.request([0x2A, 0x01, 0x01]), b"\x7f\x2a\x31", "7 data bytes at most")
        bench.ecu.config.dids = [{"did": 0xF201, "data": "01020304050607"}]
        bench.ecu.load_data()
        self.assertEqual(bench.request([0x2A, 0x03, 0x01]), b"\x6a")
        frames = bench.frames(0.2, lambda m: m.arbitration_id == 0x5E8)
        self.assertEqual(bytes(frames[0].data), bytes.fromhex("0101020304050607"))


class ResponseOnEventTest(unittest.TestCase):
    def test_a_did_change_is_answered_unasked(self):
        bench = Bench(self, quiet())
        bench.request([0x10, 0x03])
        self.assertEqual(bench.request([0x86, 0x05, 0x02]), b"\x7f\x86\x24", "no event set up yet")
        self.assertEqual(bench.request([0x86, 0x03, 0x02, 0x01, 0x01, 0x22, 0x01, 0x01]),
                         bytes.fromhex("c6030002 0101 220101"))
        self.assertEqual(bench.request([0x86, 0x05, 0x02]), bytes.fromhex("c6050002"))
        self.assertEqual(bench.request([0x2F, 0x01, 0x01, 0x03, 0x01, 0xF4])[:1], b"\x6f")   # 50.0 degC
        event = isotp_recv(bench.tester, RESPONSE, REQUEST, 1.0)
        self.assertEqual(event, bytes.fromhex("620101 01f4"))
        self.assertIsNone(isotp_recv(bench.tester, RESPONSE, REQUEST, 0.3), "only on a change")
        report = bench.request([0x86, 0x04])
        self.assertEqual(report, bytes.fromhex("c60401 0302 0101 220101"))
        self.assertEqual(bench.request([0x86, 0x00, 0x02]), bytes.fromhex("c6000002"))
        bench.request([0x2F, 0x01, 0x01, 0x03, 0x02, 0x00])
        self.assertIsNone(isotp_recv(bench.tester, RESPONSE, REQUEST, 0.3), "stopped")
        self.assertEqual(bench.request([0x86, 0x06, 0x02]), bytes.fromhex("c6060002"))
        self.assertEqual(bench.ecu.state.events, [])

    def test_a_dtc_status_change_is_answered_unasked(self):
        bench = Bench(self, quiet(confirm_cycles=1))
        self.assertEqual(bench.request([0x86, 0x01, 0x02, 0x08, 0x19, 0x02, 0x08]),
                         bytes.fromhex("c6010002 08 190208"))
        bench.request([0x86, 0x05, 0x02])
        bench.ecu.set_fault(U0100, True)                              # already confirmed: no new bit of 08
        self.assertIsNone(isotp_recv(bench.tester, RESPONSE, REQUEST, 0.3))
        bench.request([0x14, 0xFF, 0xFF, 0xFF])                      # cleared, then confirmed again at once
        event = isotp_recv(bench.tester, RESPONSE, REQUEST, 1.0)
        self.assertEqual(event, bytes.fromhex("5902ff c10000af"), "19 02 08: the DTCs confirmed now")

    def test_only_22_or_19_can_answer_an_event(self):
        bench = Bench(self, quiet())
        self.assertEqual(bench.request([0x86, 0x03, 0x02, 0xF1, 0x90, 0x2E, 0xF1, 0x90]), b"\x7f\x86\x31")
        self.assertEqual(bench.request([0x86, 0x03, 0x02, 0x12, 0x34, 0x22, 0x12, 0x34]), b"\x7f\x86\x31",
                         "no DID 1234")


class FaultMemoryTest(unittest.TestCase):
    def memory(self, **changes):
        options = {"confirm_cycles": 2, "aging_cycles": 2}
        options.update(changes)
        return DtcMemory({P0101: 0x00, U0100: 0x00}, {P0101: (b"", b"\x05")}, **options)

    def test_a_fault_goes_pending_then_confirmed_then_ages_out(self):
        memory = self.memory()
        memory.set_fault(P0101, True)
        failing = TEST_FAILED | TEST_FAILED_THIS_CYCLE | PENDING | FAILED_SINCE_CLEAR
        self.assertEqual(memory.statuses[P0101], failing, status_text(memory.statuses[P0101]))
        memory.new_operation_cycle()                                   # failed in a second cycle: confirmed
        self.assertEqual(memory.statuses[P0101], failing | CONFIRMED | WARNING_INDICATOR)
        memory.set_fault(P0101, False)
        self.assertEqual(memory.statuses[P0101], TEST_FAILED_THIS_CYCLE | PENDING | CONFIRMED | FAILED_SINCE_CLEAR)
        memory.new_operation_cycle()                                   # it had failed in that cycle: still pending
        self.assertEqual(memory.statuses[P0101], PENDING | CONFIRMED | FAILED_SINCE_CLEAR)
        memory.new_operation_cycle()                                   # a clean cycle: no longer pending
        self.assertEqual(memory.statuses[P0101], CONFIRMED | FAILED_SINCE_CLEAR)
        memory.new_operation_cycle()                                   # the second clean one: aged out
        self.assertEqual(memory.statuses[P0101], FAILED_SINCE_CLEAR)
        self.assertEqual(memory.statuses[U0100], 0x00, "the other DTC stays as it was")
        self.assertEqual([change for change in memory.changes if change[0] == U0100], [],
                         "a new cycle whose test passes at once is no change")

    def test_the_snapshot_and_the_occurrence_counter(self):
        memory = self.memory(capture=lambda: bytes.fromhex("01 0101 00d7"))
        memory.set_fault(P0101, True)
        memory.set_fault(P0101, False)
        memory.set_fault(P0101, True)
        self.assertEqual(memory.records[P0101], (bytes.fromhex("01010100d7"), b"\x07"), "05 + two occurrences")
        memory.set_fault(U0100, True)
        self.assertEqual(memory.records[U0100][1], b"\x01", "a counter where there was none")

    def test_dtc_setting_off_freezes_the_statuses(self):
        memory = self.memory()
        memory.set_frozen(True)
        memory.set_fault(P0101, True)
        memory.new_operation_cycle()
        self.assertEqual(memory.statuses[P0101], 0x00)
        memory.set_frozen(False)                                      # back on: the tests run again
        self.assertTrue(memory.statuses[P0101] & TEST_FAILED)

    def test_clearing_forgets_but_a_present_fault_comes_back(self):
        memory = self.memory(confirm_cycles=1)
        memory.set_fault(P0101, True)
        self.assertTrue(memory.clear())
        self.assertEqual(memory.statuses[P0101], TEST_FAILED | TEST_FAILED_THIS_CYCLE | PENDING | CONFIRMED
                         | FAILED_SINCE_CLEAR | WARNING_INDICATOR)
        self.assertEqual(memory.statuses[U0100], 0x00)
        self.assertFalse(memory.clear(0x123456), "a DTC the ECU does not keep")

    def test_through_the_ecu(self):
        bench = Bench(self, quiet(confirm_cycles=2))
        bench.ecu.set_fault(P0101, True)
        self.assertEqual(bench.request([0x19, 0x02, 0x04]), bytes.fromhex("5902ff 010100 af"),
                         "the table has it confirmed: failing, it asks for the warning lamp too")
        snapshot = bench.request([0x19, 0x04, 0x01, 0x01, 0x00, 0xFF])
        self.assertEqual(snapshot[6:], bytes.fromhex("01 02 0101 00d7 0102 0064"), "the snapshot DIDs, now")
        self.assertEqual(bench.request([0x19, 0x06, 0x01, 0x01, 0x00, 0x01])[-1], 0x06, "occurrence 5 + 1")
        bench.request([0x11, 0x01])                                    # a reset ends the operation cycle
        time.sleep(0.6)
        status = bench.request([0x19, 0x02, 0xFF])
        self.assertEqual(status[3:7], bytes.fromhex("010100af"), "failed again in the next cycle: confirmed")
        self.assertEqual(bench.request([0x14, 0x01, 0x01, 0x00]), b"\x54", "one DTC")
        self.assertEqual(bench.request([0x14, 0x12, 0x34, 0x56]), b"\x7f\x14\x31")

    def test_the_operation_cycle_timer(self):
        bench = Bench(self, quiet(operation_cycle_seconds=0.1))
        time.sleep(0.35)
        self.assertGreaterEqual(bench.ecu.dtc_memory.cycle, 2)


class TransportErrorTest(unittest.TestCase):
    VIN = [0x22, 0xF1, 0x90]                                           # a 20-byte, segmented answer

    def test_refusals_and_missing_answers(self):
        bench = Bench(self, quiet(error_refuse=100))
        self.assertEqual(bench.request(self.VIN), b"\x7f\x22\x21", "busyRepeatRequest")
        self.assertEqual(bench.request([0x3E, 0x00]), b"\x7e\x00", "TesterPresent is spared")
        bench.ecu.config.errors_on_tester_present = True
        self.assertEqual(bench.request([0x3E, 0x00]), b"\x7f\x3e\x21")
        bench.ecu.config.error_refuse, bench.ecu.config.error_no_answer = 0, 100
        self.assertIsNone(bench.request(self.VIN, timeout=0.3))
        self.assertTrue(any("not answered (error on purpose)" in line for line in bench.logs))

    def test_an_answer_on_another_id(self):
        bench = Bench(self, quiet(error_wrong_id=100))
        with can.Bus(interface="virtual", channel=bench.channel) as sniffer:
            self.assertIsNone(bench.request([0x10, 0x01], timeout=0.3))
            seen = [message for message in iter(lambda: sniffer.recv(0.05), None)
                    if message.arbitration_id == RESPONSE + 1]
        self.assertEqual(bytes(seen[0].data[:3]), b"\x06\x50\x01")
        self.assertEqual(bench.request([0x3E, 0x00]), b"\x7e\x00", "TesterPresent is spared")

    def test_segmented_answers_that_go_wrong(self):
        for setting, message in (("error_drop_frame", "out of sequence|Timed out"),   # the last one: never comes
                                 ("error_wrong_sequence", "out of sequence"), ("error_stall", "Timed out")):
            with self.subTest(setting):
                bench = Bench(self, quiet(**{setting: 100}))
                with self.assertRaisesRegex(IsoTpError, message):
                    bench.request(self.VIN, timeout=2.0)
                self.assertEqual(bench.request([0x10, 0x01])[:2], b"\x50\x01", "short answers are not touched")


class AccessTest(unittest.TestCase):
    def test_a_did_readable_in_one_session_after_unlocking(self):
        bench = Bench(self, quiet())                                   # 0200: extended session, level 01
        self.assertEqual(bench.request([0x22, 0x02, 0x00]), b"\x7f\x22\x31")
        bench.request([0x10, 0x03])
        self.assertEqual(bench.request([0x22, 0x02, 0x00]), b"\x7f\x22\x33")
        bench.unlock()
        self.assertEqual(bench.request([0x22, 0x02, 0x00]), b"\x62\x02\x00CAL-0042")

    def test_levels_of_their_own_and_service_rules(self):
        config = quiet(security_levels=[{"level": 0x03, "seed_length": 2, "key_mask": 0x5A}],
                       service_rules=[{"sid": 0x2F, "sessions": [3], "level": 0x03},
                                      {"sid": 0x23, "sessions": [1]}])
        bench = Bench(self, config)
        self.assertEqual(bench.request([0x2F, 0x01, 0x01, 0x00]), b"\x7f\x2f\x7f", "not in the default session")
        bench.unlock(level=0x01)
        self.assertEqual(bench.request([0x2F, 0x01, 0x01, 0x00]), b"\x7f\x2f\x33", "level 01 is not level 03")
        seed = bench.request([0x27, 0x03])[2:]
        self.assertEqual(len(seed), 2)
        self.assertEqual(bench.request([0x27, 0x04, *(byte ^ 0xA5 for byte in seed)]), b"\x7f\x27\x35",
                         "level 03 has its own mask")
        seed = bench.request([0x27, 0x03])[2:]
        self.assertEqual(bench.request([0x27, 0x04, *(byte ^ 0x5A for byte in seed)]), b"\x67\x04")
        self.assertEqual(bench.request([0x27, 0x03]), b"\x67\x03\x00\x00", "already unlocked: a zero seed")
        self.assertEqual(bench.request([0x2F, 0x01, 0x01, 0x00])[:1], b"\x6f")
        self.assertEqual(bench.request([0x23, 0x44, 0, 1, 0, 0, 0, 0, 0, 4]), b"\x7f\x23\x7f", "23: default only")
        self.assertEqual(bench.ecu.state.unlocked_levels, {0x01, 0x03})
        bench.request([0x10, 0x02])
        self.assertEqual(bench.ecu.state.unlocked_levels, set(), "a new session locks every level")

    def test_a_seed_and_key_dll_decides_the_key(self):
        class Stub:
            def GenerateKeyEx(self, seed, seed_size, level, variant, key, key_size, out_size):
                answer = bytes((byte + level) & 0xFF for byte in bytes(seed[:seed_size]))
                for index, byte in enumerate(answer):
                    key[index] = byte
                out_size._obj.value = len(answer)
                return 0

        with patch.object(ecu_module, "load_library", lambda path: Stub()):
            bench = Bench(self, quiet(key_dll="seedkey.dll"))
            bench.request([0x10, 0x03])
            seed = bench.request([0x27, 0x01])[2:]
            self.assertEqual(bench.request([0x27, 0x02, *(byte ^ 0xA5 for byte in seed)]), b"\x7f\x27\x35")
            seed = bench.request([0x27, 0x01])[2:]
            self.assertEqual(bench.request([0x27, 0x02, *((byte + 1) & 0xFF for byte in seed)]), b"\x67\x02")
        bench = Bench(self, quiet(key_dll="no-such.dll"))
        bench.request([0x10, 0x03])
        bench.request([0x27, 0x01])
        self.assertEqual(bench.request([0x27, 0x02, 0, 0, 0, 0]), b"\x7f\x27\x22", "the ECU cannot check any key")
        self.assertTrue(any("does not exist" in line for line in bench.logs))

    def test_a_profile_keeps_it_and_refuses_what_is_wrong(self):
        config = config_from_dict({"security_levels": [{"level": 3, "seed_length": 2, "key_mask": 90}],
                                   "service_rules": [{"sid": 0x2F, "sessions": [3], "level": 3}],
                                   "periodic_rates_ms": [500, 100, 10], "snapshot_dids": [0x0101]})
        self.assertEqual((config.periodic_rates_ms, config.snapshot_dids), ((500, 100, 10), (0x0101,)))
        for broken in ({"security_levels": [{"level": 2}]}, {"service_rules": [{"sessions": [3]}]},
                       {"generators": [{"signal": "EngineData.Temperature", "kind": "wobble"}]},
                       {"image_crc": "md5"}, {"error_refuse": 150}):
            with self.subTest(broken), self.assertRaises(ValueError):
                config_from_dict(broken)


class BootloaderTest(unittest.TestCase):
    def flash(self, bench, data=b"\x01\x02\x03\x04", address=0x10000, check=b""):
        bench.request([0x10, 0x03])
        bench.request([0x10, 0x02])
        seed = bench.request([0x27, 0x01])[2:]
        bench.request([0x27, 0x02, *(byte ^ 0xA5 for byte in seed)])
        memory = [0x44, *address.to_bytes(4, "big"), *len(data).to_bytes(4, "big")]
        assert bench.request([0x31, 0x01, 0xFF, 0x00, *memory])[:1] == b"\x71"
        assert bench.request([0x34, 0x00, *memory])[:1] == b"\x74"
        assert bench.request([0x36, 0x01, *data]) == b"\x76\x01"
        assert bench.request([0x37]) == b"\x77"
        return bench.request([0x31, 0x01, 0xFF, 0x01, *check])

    def test_a_failed_flash_leaves_the_ecu_in_its_bootloader(self):
        bench = Bench(self, quiet(broadcast_interval=0.02, erase_seconds=0.01))
        bench.request([0x10, 0x03])
        bench.request([0x10, 0x02])
        seed = bench.request([0x27, 0x01])[2:]
        bench.request([0x27, 0x02, *(byte ^ 0xA5 for byte in seed)])
        bench.request([0x31, 0x01, 0xFF, 0x00, 0x44, 0, 1, 0, 0, 0, 0, 0, 4])   # erased, then abandoned
        self.assertFalse(bench.ecu.state.application_valid)
        self.assertEqual(bench.request([0x11, 0x01]), b"\x51\x01")
        time.sleep(0.6)
        self.assertTrue(bench.ecu.state.bootloader)
        self.assertEqual(bench.request([0x22, 0xF1, 0x95]), b"\x62\xf1\x95BOOTLOADER")
        self.assertEqual(bench.frames(0.2, lambda m: m.arbitration_id == 0x300), [], "no application")
        self.assertEqual(bench.request([0x19, 0x02, 0xFF]), b"\x7f\x19\x11", "the application's services")
        self.assertEqual(bench.request([0x22, 0x01, 0x01]), b"\x7f\x22\x31", "the application's data")
        self.assertEqual(bench.request([0x10, 0x02])[:2], b"\x50\x02", "programming, even from default")
        self.assertEqual(self.flash(bench), b"\x71\x01\xff\x01\x00")
        self.assertTrue(bench.ecu.state.application_valid)
        self.assertTrue(bench.ecu.state.bootloader, "until the next reset")
        bench.request([0x11, 0x01])
        time.sleep(0.6)
        self.assertFalse(bench.ecu.state.bootloader)
        self.assertTrue(bench.request([0x22, 0xF1, 0x95]).startswith(b"\x62\xf1\x95APP-FLASHED-"))
        self.assertTrue(bench.frames(0.2, lambda m: m.arbitration_id == 0x300), "the application runs again")

    def test_the_check_routine_checks_the_crc(self):
        data = b"CAN Expert image"
        bench = Bench(self, quiet(image_crc="option", erase_seconds=0.01))
        self.assertEqual(self.flash(bench, data), b"\x7f\x31\x13", "the CRC-32 is expected")
        crc = zlib.crc32(data).to_bytes(4, "big")
        self.assertEqual(bench.request([0x31, 0x01, 0xFF, 0x01, *bytes(4)]), b"\x71\x01\xff\x01\x01", "wrong")
        self.assertEqual(bench.request([0x31, 0x01, 0xFF, 0x01, *crc]), b"\x71\x01\xff\x01\x00")
        bench = Bench(self, quiet(image_crc="trailer", erase_seconds=0.01))
        self.assertEqual(self.flash(bench, data), b"\x71\x01\xff\x01\x01", "no CRC at its end")
        bench = Bench(self, quiet(image_crc="trailer", erase_seconds=0.01))
        self.assertEqual(self.flash(bench, data + zlib.crc32(data).to_bytes(4, "big")), b"\x71\x01\xff\x01\x00")

    def test_the_version_comes_from_the_image(self):
        bench = Bench(self, quiet(version_address=0x10004, version_length=8, erase_seconds=0.01))
        self.assertEqual(self.flash(bench, b"\x00\x00\x00\x00APP-2.1\xff\xff"), b"\x71\x01\xff\x01\x00")
        self.assertEqual(bench.request([0x22, 0xF1, 0x95]), b"\x62\xf1\x95APP-2.1")

    def test_the_built_in_sequence_sends_the_crc_when_asked(self):
        bench = Bench(self, quiet(image_crc="option", erase_seconds=0.01, version_address=0x20000,
                                  version_length=16))
        firmware = Firmware("app.s19", [(0x10000, bytes(range(200))), (0x20000, b"DEMO-3.0")])
        with patch("canexpert.flash_sequence.RESET_WAIT", 0.6):
            with self.assertRaisesRegex(Exception, "13|incorrectMessageLength"):
                run_flash(bench.uds, firmware, FlashProfile())
            bench.request([0x10, 0x01])
            run = run_flash(bench.uds, firmware, FlashProfile(check_crc=True))
        self.assertIn(("checkProgrammingDependencies (CRC-32 %08X)" % image_crc(firmware), "ok"), run.steps)
        self.assertEqual(bench.request([0x22, 0xF1, 0x95]), b"\x62\xf1\x95DEMO-3.0")


class MemoryServiceTest(unittest.TestCase):
    def test_read_and_write_memory_by_address(self):
        bench = Bench(self, quiet(memory_ranges=((0x10000, 0x1FFFF),)))
        self.assertEqual(bench.request([0x23, 0x44, 0, 1, 0, 0, 0, 0, 0, 4]), b"\x63\xff\xff\xff\xff", "erased")
        write = [0x3D, 0x44, 0, 1, 0, 2, 0, 0, 0, 3, 0xAA, 0xBB, 0xCC]
        self.assertEqual(bench.request(write), b"\x7f\x3d\x7f", "extended session")
        bench.request([0x10, 0x03])
        self.assertEqual(bench.request(write), b"\x7f\x3d\x33", "unlocked")
        bench.unlock()
        self.assertEqual(bench.request(write), bytes.fromhex("7d44 00010002 00000003"))
        self.assertEqual(bench.request([0x23, 0x44, 0, 1, 0, 0, 0, 0, 0, 6]), bytes.fromhex("63ffffaabbccff"))
        self.assertEqual(bench.request([0x3D, 0x44, 0, 1, 0, 1, 0, 0, 0, 2, 0x11, 0x22])[:1], b"\x7d")
        self.assertEqual(bench.request([0x23, 0x44, 0, 1, 0, 0, 0, 0, 0, 6]), bytes.fromhex("63ff1122bbccff"),
                         "merged with what was there")
        self.assertEqual(list(bench.ecu.state.memory), [0x10001])
        self.assertEqual(bench.request([0x23, 0x44, 0, 2, 0, 0, 0, 0, 0, 4]), b"\x7f\x23\x31", "outside")
        self.assertEqual(bench.request([0x3D, 0x44, 0, 1, 0, 0, 0, 0, 0, 3, 0x01]), b"\x7f\x3d\x13")


if __name__ == "__main__":
    unittest.main()
