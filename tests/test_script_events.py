"""Script runtime: per-control handlers, CAPL-style decorators and DBC signal access, on a virtual bus."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import can
import cantools
from PyQt5.QtWidgets import QApplication

from panel_runtime import ReceiveMailbox, ScriptRuntime, validate_config

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parent.parent / "DBC" / "dummy_ecu.dbc"

SCRIPT = '''
events = []

def DatabaseMainFunction(api):
    events.append("main")

def on_start_clicked(api, value):
    api.ui.set_value("status", f"clicked {value}")

def plain_handler(value):
    api_free.append(value)

api_free = []

@on_start
def started(api):
    api.ui.set_value("started", True)

@on_stop
def stopped(api):
    api.can.send(0x7FF, [0xEE])

@on_message(0x300)
def engine_frame(api, frame):
    api.ui.set_value("frame", (hex(frame.id), round(frame.signals["Temperature"], 1)))

@on_message("EcuStatus")
def status_frame(frame):
    api_free.append(("status", frame.data[3]))

@on_signal("EngineData.Temperature")
def temperature(api, value):
    api.ui.set_value("changes", api.ui.get_value("changes") + [value] if api.ui.get_value("changes") else [value])

@on_signal("EcuStatus.Counter", every_update=True)
def counter(api, value):
    api.ui.set_value("counter", value)

@on_timer(0.05)
def tick(api):
    api.ui.set_value("ticks", (api.ui.get_value("ticks") or 0) + 1)

@on_control("mode")
def mode_changed(api, value):
    api.ui.set_value("mode_seen", value)
    api.set_signal("EngineData.Pressure", 2.5)
    api.send_message("EcuStatus", Running=1, Counter=7)
'''


def engine(temperature, pressure=1.0):
    return int(temperature * 10).to_bytes(2, "big") + int(pressure * 100).to_bytes(2, "big") + bytes(4)


class ScriptEventsTest(unittest.TestCase):
    def setUp(self):
        channel = "script-" + str(uuid.uuid4())
        self.app_bus = can.Bus(interface="virtual", channel=channel)
        self.peer = can.Bus(interface="virtual", channel=channel)
        self.mailbox = ReceiveMailbox(self.app_bus)
        self.values = {}
        self.logs = []
        self.runtime = ScriptRuntime(self.mailbox, validate_config({"name": "t"}), {}, None)
        self.runtime.value_changed.connect(lambda name, value: self.values.__setitem__(name, value))
        self.runtime.logged.connect(self.logs.append)
        self.runtime.dbc = cantools.database.load_file(str(DBC))
        self.runtime.handlers = {"start": "on_start_clicked", "plain": "plain_handler", "ghost": "missing_handler"}
        path = Path(tempfile.mkdtemp()) / "script.py"
        path.write_text(SCRIPT)
        self.runtime.start(path)
        self.wait(lambda: self.values.get("started"))

    def tearDown(self):
        self.runtime.stop()
        self.app_bus.shutdown()
        self.peer.shutdown()

    def wait(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            APP.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        self.fail("condition not reached")

    def frame(self, can_id, data):
        self.runtime.post("can", can_id, bytes(data))

    def test_handlers_start_timer_and_missing_handler(self):
        self.runtime.post("control", "start", True)
        self.wait(lambda: self.values.get("status") == "clicked True")
        self.wait(lambda: (self.values.get("ticks") or 0) >= 3)
        self.assertTrue(any("missing_handler" in line and "ghost" in line for line in self.logs))

    def test_message_and_signal_events(self):
        self.frame(0x300, engine(21.5))
        self.wait(lambda: self.values.get("frame") == ("0x300", 21.5))
        self.frame(0x300, engine(21.5))                                          # unchanged: no on_signal
        self.frame(0x300, engine(22.0))
        self.wait(lambda: self.values.get("changes") == [21.5, 22.0])
        for counter in (4, 4):
            self.frame(0x301, bytes([0, 0, 1, counter, 0, 0, 0, 0]))
        self.wait(lambda: self.values.get("counter") == 4)
        self.assertEqual(self.runtime.api.signal("EngineData.Temperature"), 22.0)
        self.assertIsNone(self.runtime.api.signal("EngineData.Unknown"))

    def test_set_signal_and_send_message_keep_other_signals(self):
        self.frame(0x300, engine(30.0, 1.0))
        self.wait(lambda: self.values.get("frame") == ("0x300", 30.0))
        self.runtime.post("control", "mode", "Sport")
        self.wait(lambda: self.values.get("mode_seen") == "Sport")
        first, second = self.peer.recv(1.0), self.peer.recv(1.0)
        self.assertEqual((first.arbitration_id, bytes(first.data)), (0x300, engine(30.0, 2.5)))  # temperature kept
        self.assertEqual(second.arbitration_id, 0x301)
        self.assertEqual(bytes(second.data)[:4], bytes([1, 0, 0, 7]))

    def test_on_stop_runs_while_the_bus_is_open(self):
        self.runtime.stop()
        message = self.peer.recv(1.0)
        self.assertEqual((message.arbitration_id, bytes(message.data)), (0x7FF, bytes([0xEE] + [0] * 7)))


if __name__ == "__main__":
    unittest.main()
