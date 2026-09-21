"""Flashing without a panel script: the built-in ISO 14229 sequence, its profile and its report."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import threading
import time
import unittest
import uuid
import zlib
from pathlib import Path
from unittest.mock import patch

import can
from PyQt5.QtWidgets import QApplication, QDialog

from canexpert import flashing
from canexpert.can_bus import CanWorker
from canexpert.config import validate_config
from canexpert.flash_runner import FlashRunner
from canexpert.flash_sequence import (FlashCancelled, FlashError, FlashProfile, FlashRun, memory_record,
                                      run_flash)
from canexpert.flashing import FlashDialog, FlashProfileDialog, Firmware, load_firmware
from canexpert.simulator.ecu import DummyEcu, EcuConfig
from canexpert.uds.seed_key import SeedKeyError

APP = QApplication.instance() or QApplication([])


def spin_until(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(.005)
    return False


class Reply:
    """Stands in for a UdsResult."""

    def __init__(self, ok=True, data=b"", error=""):
        self.ok, self.data, self.error = ok, data, error
        self.max_block_length = None

    @property
    def text(self):
        return self.data.decode("latin-1").strip()

    def __bool__(self):
        return self.ok


class FakeUds:
    """A bootloader that answers every service, and remembers what it was asked."""

    def __init__(self, max_block_length=0x402, refuse=()):
        self.calls = []                       # (service, arguments) in the order they were sent
        self.blocks = []                      # (block counter, data) of every TransferData
        self.max_block_length = max_block_length
        self.refuse = set(refuse)             # services answered with a negative response
        self.status = {}                      # routine -> its routineStatusRecord

    def _record(self, service, *arguments):
        self.calls.append((service, arguments))
        if service in self.refuse:
            return Reply(False, error="NRC 0x22 conditionsNotCorrect")
        return Reply()

    def services(self):
        return [service for service, _ in self.calls]

    def arguments(self, service):
        return [arguments for name, arguments in self.calls if name == service]

    def DSC(self, session):
        return self._record("DSC", session)

    def CDTCS(self, setting):
        return self._record("CDTCS", setting)

    def CC(self, control, communication=0x01):
        return self._record("CC", control, communication)

    def SecurityUnlock(self, level, compute_key):
        return self._record("SecurityUnlock", level, compute_key(b"\x01\x02\x03\x04"))

    def StartRoutine(self, routine, data=b""):
        reply = self._record("StartRoutine", routine, bytes(data))
        reply.data = self.status.get(routine, b"\x00")
        return reply

    def RD(self, address, size, address_format=0x44, data_format=0x00):
        reply = self._record("RD", address, size, address_format, data_format)
        reply.max_block_length = self.max_block_length
        return reply

    def TD(self, counter, data=b""):
        reply = self._record("TD", counter, len(data))
        if reply:
            self.blocks.append((counter, bytes(data)))
        return reply

    def RTE(self, data=b""):
        return self._record("RTE")

    def ER(self, reset_type):
        return self._record("ER", reset_type)

    def RDBI(self, did):
        reply = self._record("RDBI", did)
        reply.data = b"V1.2.3"
        return reply


def image(*segments, name="firmware.s19"):
    return Firmware(name, [(address, bytes(data)) for address, data in segments])


BARE = FlashProfile(extended_session=0, stop_dtc=False, stop_communication=False, security_level=0,
                    erase_routine=0, check_routine=0, reset_type=0, version_did=0, restore_after=False)


class MemoryRecordTest(unittest.TestCase):
    def test_the_format_says_how_wide_the_address_and_the_size_are(self):
        self.assertEqual(memory_record(0x8000, 0x100, 0x44),
                         [0x44, 0x00, 0x00, 0x80, 0x00, 0x00, 0x00, 0x01, 0x00])
        self.assertEqual(memory_record(0x1234, 0x10, 0x12), [0x12, 0x12, 0x34, 0x10])   # 2-byte address, 1-byte size


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_a_profile_survives_a_trip_through_a_file(self):
        path = Path(self.temp.name) / "bootloader.json"
        profile = FlashProfile(programming_session=0x60, security_level=0x11, key_mask=0x5A, block_size=256,
                               erase_routine=0xFF10, key_variant="Body")
        profile.save(path)
        self.assertEqual(FlashProfile.load(path), profile)

    def test_names_a_profile_does_not_know_are_ignored(self):
        profile = FlashProfile.from_dict({"programming_session": 0x02, "something_else": 42})
        self.assertEqual(profile.programming_session, 0x02)

    def test_the_key_is_the_seed_through_the_mask_unless_a_dll_is_named(self):
        self.assertEqual(FlashProfile(key_mask=0xA5).compute_key()(b"\x01\x02"), bytes([0xA4, 0xA7]))
        with self.assertRaises(SeedKeyError):
            FlashProfile(key_dll="no-such-file.dll").compute_key()


class SequenceTest(unittest.TestCase):
    """What run_flash sends, against a bootloader that answers everything."""

    def setUp(self):
        self.uds = FakeUds()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_the_whole_sequence_is_sent_in_the_order_a_bootloader_expects(self):
        run = run_flash(self.uds, image((0x8000, bytes(100))), FlashProfile())
        self.assertEqual(self.uds.services(),
                         ["DSC", "CDTCS", "CC", "DSC", "SecurityUnlock", "StartRoutine", "RD", "TD", "RTE",
                          "StartRoutine", "CC", "CDTCS", "ER", "RDBI"])
        self.assertEqual([arguments[0] for arguments in self.uds.arguments("DSC")], [0x03, 0x02])
        self.assertEqual(self.uds.arguments("CDTCS"), [(0x02,), (0x01,)])      # off to flash, on afterwards
        self.assertEqual(self.uds.arguments("CC"), [(0x03, 0x01), (0x00, 0x01)])
        self.assertEqual(self.uds.arguments("ER"), [(0x01,)])
        self.assertEqual(self.uds.arguments("RDBI"), [(0xF195,)])
        self.assertTrue(run.ok)
        self.assertEqual(run.written, 100)

    def test_the_key_the_ecu_is_sent_comes_from_the_profile(self):
        run_flash(self.uds, image((0x8000, bytes(8))), FlashProfile(key_mask=0xFF))
        self.assertEqual(self.uds.arguments("SecurityUnlock"), [(0x01, bytes([0xFE, 0xFD, 0xFC, 0xFB]))])

    def test_a_seed_and_key_dll_that_is_not_there_is_said_before_anything_is_flashed(self):
        with self.assertRaises(FlashError) as raised:
            run_flash(self.uds, image((0x8000, bytes(8))), FlashProfile(key_dll="no-such-file.dll"))
        self.assertIn("does not exist", str(raised.exception))
        self.assertNotIn("RD", self.uds.services())

    def test_the_erase_routine_is_given_the_address_and_the_size(self):
        run_flash(self.uds, image((0x08004000, bytes(300))), FlashProfile())
        routine, record = self.uds.arguments("StartRoutine")[0]
        self.assertEqual(routine, 0xFF00)
        self.assertEqual(record, bytes([0x44, 0x08, 0x00, 0x40, 0x00, 0x00, 0x00, 0x01, 0x2C]))
        self.assertEqual(self.uds.arguments("StartRoutine")[1], (0xFF01, b""))   # the dependency check

    def test_the_dependency_check_can_be_given_the_images_crc(self):
        firmware = image((0x8000, b"abc"), (0x4000, b"de"))
        run_flash(self.uds, firmware, FlashProfile(check_crc=True))
        crc = zlib.crc32(b"deabc")                                               # the segments in address order
        self.assertEqual(self.uds.arguments("StartRoutine")[-1], (0xFF01, crc.to_bytes(4, "big")))

    def test_the_data_goes_out_in_blocks_the_ecu_allows(self):
        data = bytes(range(256)) * 10                      # 2560 bytes
        run = run_flash(self.uds, image((0x8000, data)), FlashProfile())
        # maxNumberOfBlockLength counts the service and the counter, so 0x402 leaves 1024 bytes of data.
        self.assertEqual([len(block) for _, block in self.uds.blocks], [1024, 1024, 512])
        self.assertEqual([counter for counter, _ in self.uds.blocks], [1, 2, 3])
        self.assertEqual(b"".join(block for _, block in self.uds.blocks), data)
        self.assertEqual(run.written, len(data))

    def test_a_smaller_block_size_in_the_profile_is_honoured(self):
        run_flash(self.uds, image((0x8000, bytes(150))), FlashProfile(block_size=64))
        self.assertEqual([len(block) for _, block in self.uds.blocks], [64, 64, 22])

    def test_a_block_size_larger_than_the_ecu_allows_is_cut_down_to_it(self):
        run_flash(self.uds, image((0x8000, bytes(3000))), FlashProfile(block_size=4000))
        self.assertEqual([len(block) for _, block in self.uds.blocks], [1024, 1024, 952])

    def test_an_ecu_that_announces_nothing_gets_what_iso_tp_carries(self):
        self.uds = FakeUds(max_block_length=None)
        run_flash(self.uds, image((0x8000, bytes(5000))), FlashProfile())
        self.assertEqual([len(block) for _, block in self.uds.blocks], [4093, 907])

    def test_every_segment_is_erased_and_downloaded_on_its_own(self):
        run_flash(self.uds, image((0x8000, bytes(10)), (0x9000, bytes(20))), FlashProfile())
        self.assertEqual(self.uds.services().count("RD"), 2)
        self.assertEqual(self.uds.services().count("RTE"), 2)
        self.assertEqual([address for address, _size, _fmt, _data in self.uds.arguments("RD")], [0x8000, 0x9000])
        self.assertEqual([counter for counter, _ in self.uds.blocks], [1, 1])   # the counter restarts per segment

    def test_the_steps_a_profile_leaves_out_are_not_sent(self):
        run_flash(self.uds, image((0x8000, bytes(10))), BARE)
        self.assertEqual(self.uds.services(), ["DSC", "RD", "TD", "RTE"])

    def test_a_refused_service_stops_the_run_and_says_which_one(self):
        self.uds = FakeUds(refuse={"RD"})
        run = FlashRun(FlashProfile(), image((0x8000, bytes(10))))
        with self.assertRaises(FlashError) as raised:
            run_flash(self.uds, run.firmware, run.profile, run=run)
        self.assertIn("RequestDownload", str(raised.exception))
        self.assertIn("NRC 0x22", str(raised.exception))
        self.assertNotIn("TD", self.uds.services())
        self.assertIn(("RequestDownload 0x00008000", "NRC 0x22 conditionsNotCorrect"), run.steps)

    def test_a_dependency_check_that_reports_trouble_is_a_failure(self):
        self.uds.status[0xFF01] = b"\x02"
        with self.assertRaises(FlashError) as raised:
            run_flash(self.uds, image((0x8000, bytes(10))), FlashProfile())
        self.assertIn("status 0x02", str(raised.exception))

    def test_cancelling_stops_between_two_steps(self):
        run = FlashRun(FlashProfile(), image((0x8000, bytes(4096))))
        with self.assertRaises(FlashCancelled):                       # two blocks out, then the user gives up
            run_flash(self.uds, run.firmware, run.profile, run=run,
                      cancelled=lambda: len(self.uds.blocks) >= 2)
        self.assertEqual(run.written, 2048, "the block being sent is finished, the next one is not started")
        self.assertNotIn("RTE", self.uds.services())

    def test_the_progress_follows_the_bytes_written(self):
        seen = []
        run_flash(self.uds, image((0x8000, bytes(2048))), FlashProfile(),
                  progress=lambda done, total, text: seen.append((done, total)))
        self.assertEqual(seen[0], (0, 2048))
        self.assertEqual(seen[-1], (2048, 2048))
        self.assertEqual([done for done, _ in seen], sorted(done for done, _ in seen))

    def test_the_report_says_what_was_done_and_how_it_ended(self):
        path = Path(self.temp.name) / "app.s19"
        path.write_bytes(b"")
        run = run_flash(self.uds, image((0x8000, bytes(40)), name=str(path)), FlashProfile())
        written = run.write_report()
        self.assertEqual(written, path.with_suffix(".flash-report.txt"))
        report = written.read_text(encoding="utf-8")
        self.assertIn("0x00008000 - 0x00008027  (40 bytes)", report)
        self.assertIn("programming_session = 2", report)              # the profile it ran with
        self.assertIn("RequestDownload 0x00008000: ok", report)
        self.assertIn("Software version (DID F195)", report)
        self.assertIn("complete", report)
        self.assertIn("40 of 40 bytes written", report)

    def test_the_report_of_a_run_that_failed_keeps_the_steps_that_did_happen(self):
        self.uds = FakeUds(refuse={"StartRoutine"})
        run = FlashRun(FlashProfile(), image((0x8000, bytes(40)), name=str(Path(self.temp.name) / "app.s19")))
        with self.assertRaises(FlashError):
            run_flash(self.uds, run.firmware, run.profile, run=run)
        report = run.write_report().read_text(encoding="utf-8")
        self.assertIn("DiagnosticSessionControl (programming): ok", report)
        self.assertIn("RoutineControl erase 0x00008000: NRC 0x22", report)
        self.assertIn("failed", report)
        self.assertIn("0 of 40 bytes written", report)


class DialogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.firmware = load_firmware(Path(__file__).resolve().parents[1] / "examples" / "firmware" / "demo_app.s19")

    def test_the_script_is_offered_when_there_is_one_and_the_sequence_when_there_is_not(self):
        with_script = FlashDialog(self.firmware, FlashProfile(), script_available=True)
        self.addCleanup(with_script.close)
        self.assertTrue(with_script.use_script())
        without = FlashDialog(self.firmware, FlashProfile(), script_available=False)
        self.addCleanup(without.close)
        self.assertFalse(without.use_script())
        self.assertFalse(without.script_radio.isEnabled())
        self.assertTrue(without.built_in_radio.isChecked())

    def test_editing_the_settings_chooses_the_built_in_sequence(self):
        dialog = FlashDialog(self.firmware, FlashProfile(), script_available=True)
        self.addCleanup(dialog.close)
        self.assertIn("block size from the ECU", dialog.summary.text())

        def edit(profile_dialog):                            # the user changing a value and pressing OK
            profile_dialog.block_size.setValue(512)
            profile_dialog.security_level.setText("11")
            profile_dialog._accept()
            return QDialog.Accepted

        with patch.object(FlashProfileDialog, "exec_", edit):
            dialog.edit_profile()
        self.assertEqual(dialog.profile.block_size, 512)
        self.assertEqual(dialog.profile.security_level, 0x11)
        self.assertFalse(dialog.use_script(), "changing the sequence settings means running the sequence")
        self.assertIn("512 bytes per block", dialog.summary.text())

    def test_the_fields_hold_every_setting_of_the_profile(self):
        profile = FlashProfile(extended_session=0x7F, programming_session=0x60, stop_dtc=False,
                               stop_communication=False, restore_after=False, security_level=0x09,
                               key_mask=0x3C, key_dll="C:/keys/seed.dll", key_variant="Body",
                               erase_routine=0xFF10, check_routine=0, address_format=0x24, data_format=0x11,
                               block_size=200, reset_type=0x03, version_did=0xF189, check_crc=True)
        dialog = FlashProfileDialog(profile)
        self.addCleanup(dialog.close)
        self.assertEqual(dialog.values(), profile)
        self.assertEqual(dialog.erase_routine.text(), "FF10")
        self.assertEqual(dialog.security_level.text(), "09")
        self.assertFalse(dialog.restore_after.isChecked())
        self.assertTrue(dialog.check_crc.isChecked())

    def test_a_value_that_is_not_hexadecimal_is_said_instead_of_accepted(self):
        dialog = FlashProfileDialog(FlashProfile())
        self.addCleanup(dialog.close)
        dialog.erase_routine.setText("oops")
        dialog._accept()
        self.assertIn("erase routine must be hexadecimal", dialog.message.text())
        self.assertEqual(dialog.result(), 0, "a dialog with a bad value must not close")
        self.assertEqual(dialog.profile.erase_routine, 0xFF00)

    def test_the_settings_can_be_kept_in_a_file_and_read_back(self):
        path = Path(self.temp.name) / "bootloader.json"
        dialog = FlashProfileDialog(FlashProfile())
        self.addCleanup(dialog.close)
        dialog.block_size.setValue(128)
        dialog.key_variant.setText("Body")
        with patch.object(flashing.QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), "")):
            dialog.save_profile()
        self.assertIn("Saved to bootloader.json", dialog.message.text())

        again = FlashProfileDialog(FlashProfile())
        self.addCleanup(again.close)
        with patch.object(flashing.QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), "")):
            again.load_profile()
        self.assertEqual(again.values().block_size, 128)
        self.assertEqual(again.values().key_variant, "Body")


def s19_file(path, address, data, per_line=16):
    """Write data as S1 records, the way a compiler's output looks."""
    lines = []
    for offset in range(0, len(data), per_line):
        chunk = data[offset:offset + per_line]
        body = bytes([3 + len(chunk)]) + (address + offset).to_bytes(2, "big") + chunk
        lines.append("S1" + (body + bytes([0xFF - (sum(body) & 0xFF)])).hex().upper())
    lines.append("S9030000FC")
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii")
    return Path(path)


class SimulatedEcuTest(unittest.TestCase):
    """The whole thing against the simulated ECU: a real bus, ISO-TP, and the firmware in its memory."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        channel = "flash-" + str(uuid.uuid4())
        self.bus = can.Bus(interface="virtual", channel=channel)
        self.ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.ecu = DummyEcu(self.ecu_bus, EcuConfig(broadcast_interval=0, erase_seconds=0.05),
                            log=lambda text: None)
        self.stop = threading.Event()
        threading.Thread(target=self.ecu.serve, args=(self.stop,), daemon=True).start()
        self.config = validate_config({"name": "Flash", "request_id": 0x7E0, "response_id": 0x7E8,
                                       "timeout_ms": 3000})
        self.worker = CanWorker(self.bus, self.config, tester_present=False)
        self.worker.start()
        self.runner = FlashRunner(lambda: (self.bus, self.worker, self.config))
        self.outcome = []
        self.runner.finished.connect(lambda ok, text: self.outcome.append((ok, text)))

    def tearDown(self):
        self.runner.cancel()
        self.worker.stop()
        self.stop.set()
        time.sleep(.05)
        self.bus.shutdown()
        self.ecu_bus.shutdown()

    def test_the_built_in_sequence_flashes_the_simulated_ecu(self):
        data = bytes(range(256)) * 10                        # 2560 bytes: three blocks
        path = s19_file(Path(self.temp.name) / "app.s19", 0x1000, data)
        firmware = load_firmware(path)
        self.assertTrue(self.runner.start(firmware, FlashProfile()))
        self.assertTrue(spin_until(lambda: self.outcome), "flashing did not finish")
        ok, text = self.outcome[0]
        self.assertTrue(ok, text)
        self.assertEqual(bytes(self.ecu.read_memory(0x1000, len(data))), data)
        self.assertIn("2560 bytes written", text)

        run = self.runner.run
        self.assertTrue(run.ok)
        # The ECU answers NRC 0x70 to a download over memory that was not erased, so reaching the data at
        # all proves the erase went through; its own record of it is gone with the reset at the end.
        self.assertIn(("RoutineControl erase 0x00001000", "ok"), run.steps)
        self.assertIn(("TransferData 0x00001000", "2560 bytes in 3 block(s)"), run.steps)
        version = dict(run.steps)["Software version (DID F195)"]
        self.assertTrue(version.startswith("APP-FLASHED-"), version)   # what the ECU reports after the reset
        report = path.with_suffix(".flash-report.txt")
        self.assertIn(f"Report: {report}", text)
        self.assertIn("Result: complete", report.read_text(encoding="utf-8"))

    def test_a_locked_ecu_that_refuses_the_key_ends_the_run_with_a_report(self):
        path = s19_file(Path(self.temp.name) / "app.s19", 0x1000, bytes(64))
        self.assertTrue(self.runner.start(load_firmware(path), FlashProfile(key_mask=0x11)))  # the ECU wants 0xA5
        self.assertTrue(spin_until(lambda: self.outcome), "flashing did not finish")
        ok, text = self.outcome[0]
        self.assertFalse(ok)
        self.assertIn("SecurityAccess", text)
        self.assertFalse(self.ecu.state.unlocked)
        self.assertIn("Result: failed", path.with_suffix(".flash-report.txt").read_text(encoding="utf-8"))

    def test_nothing_is_flashed_while_nothing_is_connected(self):
        runner = FlashRunner(lambda: None)
        self.assertFalse(runner.start(image((0x1000, bytes(8))), FlashProfile()))
        self.assertIsNone(runner.run)


if __name__ == "__main__":
    unittest.main()
