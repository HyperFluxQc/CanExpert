"""ISO 14229 script functions: request bytes per service, results, the dummy ECU, scripts and the editor panel."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

import can
from PyQt5.QtWidgets import QApplication

from canexpert.simulator.ecu import DummyEcu, EcuConfig
from canexpert.can_bus import ReceiveMailbox
from canexpert.config import validate_config
from canexpert.panel.runtime import ScriptRuntime
from canexpert.uds.client import EXCLUDED_SERVICES, FUNCTIONS, UdsFunctions

APP = QApplication.instance() or QApplication([])
ISO_14229_SERVICES = {0x10, 0x11, 0x14, 0x19, 0x22, 0x23, 0x24, 0x27, 0x28, 0x29, 0x2A, 0x2C, 0x2E, 0x2F, 0x31,
                      0x34, 0x35, 0x36, 0x37, 0x38, 0x3D, 0x3E, 0x83, 0x84, 0x85, 0x86, 0x87}


class RecordingTransport:
    def __init__(self, reply=None):
        self.requests, self.reply = [], reply

    def __call__(self, payload, timeout, wait):
        self.requests.append((bytes(payload), wait))
        return self.reply(payload) if callable(self.reply) else self.reply


class RequestBytesTest(unittest.TestCase):
    def test_every_iso_14229_service_except_authentication_and_secured_data(self):
        covered = {entry.sid for entry in FUNCTIONS if entry.sid is not None}
        self.assertEqual(covered, ISO_14229_SERVICES - set(EXCLUDED_SERVICES))
        self.assertEqual(set(EXCLUDED_SERVICES), {0x29, 0x84})

    def test_request_bytes(self):
        transport = RecordingTransport()
        uds = UdsFunctions(transport)
        cases = [
            (lambda: uds.RDBI(0xFF99), "22 ff 99"),
            (lambda: uds.RDBI(0xF190, 0xF18C), "22 f1 90 f1 8c"),
            (lambda: uds.DSC(0x03), "10 03"),
            (lambda: uds.ER(0x01), "11 01"),
            (lambda: uds.SA(0x02, [0xAA, 0xBB]), "27 02 aa bb"),
            (lambda: uds.CC(0x03, 0x01), "28 03 01"),
            (lambda: uds.CC(0x04, 0x01, node_id=0x1234), "28 04 01 12 34"),
            (lambda: uds.TP(), "3e 00"),
            (lambda: uds.ATP(0x03), "83 03"),
            (lambda: uds.CDTCS(0x02), "85 02"),
            (lambda: uds.ROE(0x03, 0x02, [0xF1, 0x90], [0x22, 0xF1, 0x90]), "86 03 02 f1 90 22 f1 90"),
            (lambda: uds.LC(0x01, 0x12), "87 01 12"),
            (lambda: uds.RMBA(0x00010000, 16), "23 44 00 01 00 00 00 00 00 10"),
            (lambda: uds.RMBA(0x1234, 2, format=0x12), "23 12 12 34 02"),
            (lambda: uds.RSDBI(0x0100), "24 01 00"),
            (lambda: uds.RDBPI(0x02, 0xF201, 0xF202), "2a 02 01 02"),
            (lambda: uds.DDDI_DefineById(0xF300, [(0xF190, 1, 4)]), "2c 01 f3 00 f1 90 01 04"),
            (lambda: uds.DDDI_DefineByAddress(0xF301, [(0x1000, 4)], format=0x14), "2c 02 f3 01 14 00 00 10 00 04"),
            (lambda: uds.DDDI_Clear(0xF300), "2c 03 f3 00"),
            (lambda: uds.DDDI_Clear(), "2c 03"),
            (lambda: uds.WDBI(0xF190, "VIN"), "2e f1 90 56 49 4e"),
            (lambda: uds.WMBA(0x2000, [1, 2], format=0x12), "3d 12 20 00 02 01 02"),
            (lambda: uds.CDTCI(), "14 ff ff ff"),
            (lambda: uds.CDTCI(0x000100, memory_selection=0x01), "14 00 01 00 01"),
            (lambda: uds.RDTCI(0x02, 0xFF), "19 02 ff"),
            (lambda: uds.RDTCI(0x04, 0x010100, 0xFF), "19 04 01 01 00 ff"),
            (lambda: uds.IOCBI(0x0200, 0x03, [0x01], [0xFF]), "2f 02 00 03 01 ff"),
            (lambda: uds.RC(0x01, 0xFF00, [0x44]), "31 01 ff 00 44"),
            (lambda: uds.StartRoutine(0x0203), "31 01 02 03"),
            (lambda: uds.StopRoutine(0x0203), "31 02 02 03"),
            (lambda: uds.RoutineResults(0x0203), "31 03 02 03"),
            (lambda: uds.RD(0x00010000, 0x1000), "34 00 44 00 01 00 00 00 00 10 00"),
            (lambda: uds.RU(0x0100, 0x10, format=0x12, data_format=0x11), "35 11 12 01 00 10"),
            (lambda: uds.TD(0x100, [0xAA]), "36 00 aa"),
            (lambda: uds.RTE([0x12, 0x34]), "37 12 34"),
            (lambda: uds.RFT(0x02, "a.bin"), "38 02 00 05 61 2e 62 69 6e"),
            (lambda: uds.RFT(0x04, "a"), "38 04 00 01 61 00"),
            (lambda: uds.RFT(0x01, "a", size=0x100, size_length=2), "38 01 00 01 61 00 02 01 00 01 00"),
            (lambda: uds.UDS("22 F1 90"), "22 f1 90"),
            (lambda: uds.UDS([0x3E, 0x00]), "3e 00"),
        ]
        for call, expected in cases:
            with self.subTest(expected=expected):
                call()
                self.assertEqual(transport.requests[-1][0].hex(" "), expected)

    def test_suppress_sets_bit_and_does_not_wait(self):
        transport = RecordingTransport()
        result = UdsFunctions(transport).TP(suppress=True)
        self.assertEqual(transport.requests[-1], (bytes([0x3E, 0x80]), False))
        self.assertTrue(result and result.suppressed)

    def test_results(self):
        uds = UdsFunctions(RecordingTransport(lambda p: bytes([0x62]) + p[1:3] + b"VIN123"))
        vin = uds.RDBI(0xF190)
        self.assertTrue(vin)
        self.assertEqual((vin.data, vin.text, vin.error), (b"VIN123", "VIN123", ""))
        self.assertEqual(repr(vin), "22 F1 90 -> 62 F1 90 56 49 4E 31 32 33")
        negative = UdsFunctions(RecordingTransport(bytes([0x7F, 0x22, 0x31]))).RDBI(0x1234)
        self.assertFalse(negative)
        self.assertEqual((negative.nrc, negative.nrc_name, negative.error), (0x31, "requestOutOfRange",
                                                                            "NRC 0x31 requestOutOfRange"))
        silent = UdsFunctions(RecordingTransport(None)).DSC(0x03)
        self.assertTrue(silent.timeout and not silent and silent.error == "no response")
        download = UdsFunctions(RecordingTransport(bytes([0x74, 0x20, 0x04, 0x02]))).RD(0, 16)
        self.assertEqual(download.max_block_length, 0x402)
        counter = UdsFunctions(RecordingTransport(bytes([0x62, 0x01, 0x00, 0x00, 0x00, 0x01, 0x2C]))).RDBI(0x0100)
        self.assertEqual(counter.int, 300)


class DummyEcuFunctionsTest(unittest.TestCase):
    def setUp(self):
        channel = "udslib-" + str(uuid.uuid4())
        self.ecu_bus = can.Bus(interface="virtual", channel=channel)
        self.app_bus = can.Bus(interface="virtual", channel=channel)
        self.stop = threading.Event()
        ecu = DummyEcu(self.ecu_bus, EcuConfig(erase_seconds=0.05, broadcast_interval=0), log=lambda text: None)
        threading.Thread(target=ecu.serve, args=(self.stop,), daemon=True).start()
        self.mailbox = ReceiveMailbox(self.app_bus)
        self.pumping = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while self.pumping:
            message = self.app_bus.recv(0.01)
            if message:
                self.mailbox.push(message)

    def tearDown(self):
        self.stop.set()
        self.pumping = False
        time.sleep(0.05)
        self.ecu_bus.shutdown()
        self.app_bus.shutdown()

    def runtime(self, script):
        runtime = ScriptRuntime(self.mailbox, validate_config({"name": "t", "request_id": 0x7E0, "response_id": 0x7E8}),
                                {}, None)
        values = {}
        runtime.value_changed.connect(values.__setitem__)
        path = Path(tempfile.mkdtemp()) / "script.py"
        path.write_text(script)
        runtime.start(path)
        return runtime, values

    def test_script_functions_against_the_dummy_ecu(self):
        runtime, values = self.runtime('''
def DatabaseMainFunction(api):
    results = {}
    results["vin"] = RDBI(0xF190).text
    results["locked"] = SA(0x01).error                                   # default session
    results["session"] = bool(DSC(0x03))
    results["unlock"] = bool(SecurityUnlock(0x01, lambda seed: bytes(b ^ 0xA5 for b in seed)))
    results["write"] = bool(WDBI(0xF190, "ABCDEFGHJKLMNPRST"))
    results["new_vin"] = RDBI(0xF190).text
    results["dtcs"] = ReadDTCs(0xFF)
    results["clear"] = bool(CDTCI())
    results["after_clear"] = ReadDTCs(0xFF)
    results["unsupported"] = RSDBI(0xF190).nrc_name
    results["memory"] = RMBA(0x1000, 4).data                           # erased flash
    results["raw"] = UDS("22 F1 8C").data                               # after the SID: DID echo + record
    results["suppressed"] = bool(TP(suppress=True))
    api.ui.set_value("results", results)
''')
        try:
            deadline = time.monotonic() + 5
            while "results" not in values and time.monotonic() < deadline:
                APP.processEvents()
                time.sleep(0.01)
            results = values["results"]
        finally:
            runtime.stop()
        self.assertEqual(results["vin"], "WVWZZZ1KZAW000001")
        self.assertEqual(results["locked"], "NRC 0x7F serviceNotSupportedInActiveSession")
        self.assertTrue(results["session"] and results["unlock"] and results["write"] and results["clear"])
        self.assertEqual(results["new_vin"], "ABCDEFGHJKLMNPRST")
        self.assertEqual(results["dtcs"], [(0x010100, 0x09), (0xC10000, 0x08)])
        self.assertEqual(results["after_clear"], [])
        self.assertEqual(results["unsupported"], "serviceNotSupported")
        self.assertEqual(results["memory"], bytes([0xFF] * 4))
        self.assertEqual(results["raw"], bytes([0xF1, 0x8C]) + b"SN000123456")
        self.assertTrue(results["suppressed"])


class FunctionPanelTest(unittest.TestCase):
    def test_panel_lists_functions_and_inserts_calls(self):
        from canexpert.designer.form_designer import FormDesigner
        designer = FormDesigner()
        panel, editor = designer.uds_panel, designer.code_editor
        self.assertEqual(set(panel.items), {entry.name for entry in FUNCTIONS})
        editor.setPlainText("def on_read_clicked(api, value):\n    ")
        editor.moveCursor(editor.textCursor().End)
        panel.tree.setCurrentItem(panel.items["RDBI"])
        self.assertIn("RDBI(did, *more_dids, timeout=None)", panel.details.toPlainText())
        panel.insert_button.click()
        self.assertEqual(editor.toPlainText(), "def on_read_clicked(api, value):\n    value = RDBI(0xF190)")
        panel._insert(panel.items["DSC"])                                      # non-empty line: next line
        self.assertEqual(editor.toPlainText().splitlines()[-1], "    DSC(0x03)")
        editor.setPlainText("def on_x_clicked(api, value):")
        editor.moveCursor(editor.textCursor().End)
        panel._insert(panel.items["ER"])                                       # after "...:" one level deeper
        self.assertEqual(editor.toPlainText(), "def on_x_clicked(api, value):\n    ER(0x01)")
        panel.filter_edit.setText("2E")
        visible = [name for name, item in panel.items.items() if not item.isHidden()]
        self.assertEqual(visible, ["WDBI"])
        self.assertTrue(editor.check_syntax()[0])
        designer.close()


if __name__ == "__main__":
    unittest.main()
