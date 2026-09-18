"""Cancellable Python panel callbacks. Only the GUI thread touches Qt widgets."""
import queue
import sys
import threading
import time
from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal

from database_api import DatabaseAPI


class ScriptStopped(BaseException):
    pass


class ScriptRuntime(QObject):
    value_changed = pyqtSignal(str, object)
    logged = pyqtSignal(str)

    def __init__(self, bus, config, values, parent=None):
        super().__init__(parent)
        self.stop_event = threading.Event()
        self.events = queue.Queue(maxsize=2048)
        self.callbacks = {}
        self.can_callbacks = []
        self.timers = []
        self.values = dict(values)
        self.lock = threading.Lock()
        self.thread = None
        self.api = DatabaseAPI(bus, config.get("request_id", 0x7DF),
                               config.get("response_id", 0x7E8), log_cb=self.logged.emit)
        self.api._runtime = self
        self.api._extended = not config.get("identifier_11_bit", True)
        self.api._stop_event = self.stop_event

    def start(self, path):
        path = Path(path)
        if not path.exists():
            return
        source = path.read_text(encoding="utf-8-sig")
        code = compile(source, str(path), "exec")
        self.thread = threading.Thread(target=self._run, args=(code, path), daemon=True)
        self.thread.start()

    def _trace(self, frame, event, arg):
        if self.stop_event.is_set():
            raise ScriptStopped()
        return self._trace

    def _call(self, callback, *args):
        try:
            callback(*args)
        except Exception as exc:
            self.logged.emit(f"Script callback failed: {exc}")

    def _run(self, code, path):
        # A Python loop can be interrupted on disconnect. Blocking native calls
        # cannot be forcibly killed; the bus facade is revoked independently.
        sys.settrace(self._trace)
        try:
            namespace = {"__file__": str(path), "__name__": "canexpert_panel"}
            exec(code, namespace)
            startup = namespace.get("DatabaseMainFunction")
            if startup:
                startup(self.api)
            while not self.stop_event.is_set():
                try:
                    kind, name, value = self.events.get(timeout=0.02)
                    if kind == "control":
                        for callback in list(self.callbacks.get(name, [])):
                            self._call(callback, value)
                    elif kind == "can":
                        self.api.push_received_message(name, value)
                        for callback in list(self.can_callbacks):
                            self._call(callback, name, value)
                except queue.Empty:
                    pass
                now = time.monotonic()
                for timer in list(self.timers):
                    if now >= timer[0]:
                        timer[0] = now + timer[1]
                        self._call(timer[2])
        except ScriptStopped:
            pass
        except Exception as exc:
            self.logged.emit(f"Database script failed: {exc}")
        finally:
            sys.settrace(None)

    def post(self, kind, name, value):
        if self.stop_event.is_set():
            return
        if kind == "control":
            with self.lock:
                self.values[name] = value
        try:
            self.events.put_nowait((kind, name, value))
        except queue.Full:
            self.logged.emit("Script event queue full; event dropped")

    def get_value(self, name):
        with self.lock:
            return self.values.get(name)

    def set_value(self, name, value):
        if not self.stop_event.is_set():
            with self.lock:
                self.values[name] = value
            self.value_changed.emit(name, value)

    def stop(self):
        self.stop_event.set()
        self.api.set_bus(None)
        if self.thread:
            self.thread.join(timeout=1.0)


class ReceiveMailbox:
    """Bus facade for scripts; the CAN worker remains the sole hardware reader."""
    def __init__(self, bus, sent=None):
        self.bus = bus
        self.sent = sent
        self.messages = queue.Queue(maxsize=2048)
        self.lock = threading.Lock()
        self.closed = False

    def send(self, message):
        with self.lock:
            if self.closed:
                raise RuntimeError("CAN session is closed")
            self.bus.send(message)
        if self.sent:
            self.sent(message.arbitration_id, bytes(message.data))

    def push(self, message):
        if self.closed:
            return
        try:
            self.messages.put_nowait(message)
        except queue.Full:
            try:
                self.messages.get_nowait()
            except queue.Empty:
                pass
            self.messages.put_nowait(message)

    def recv(self, timeout=0.1):
        if self.closed:
            raise RuntimeError("CAN session is closed")
        try:
            return self.messages.get(timeout=min(timeout or 0, 0.1))
        except queue.Empty:
            return None

    def close(self):
        with self.lock:
            self.closed = True


def validate_config(config):
    """Normalize legacy configs and reject settings that cannot be operated."""
    import math
    cfg = dict(config)
    cfg.setdefault("name", "Default Configuration")
    cfg.setdefault("bitrate", 500000)
    cfg.setdefault("identifier_11_bit", True)
    cfg.setdefault("request_id", 0x7DF)
    cfg.setdefault("response_id", 0x7E8)
    cfg.setdefault("tester_present_interval_seconds", 2.0)
    cfg.setdefault("node_timeout_seconds", 6.0)
    cfg.setdefault("database_family", "")
    if not isinstance(cfg["name"], str) or not cfg["name"].strip():
        raise ValueError("Configuration name must be nonempty text")
    if not isinstance(cfg["database_family"], str):
        raise ValueError("Database family must be text")
    for key in ("identifier_11_bit", "extended_id"):
        if key in cfg and not isinstance(cfg[key], bool):
            raise ValueError(f"{key} must be true or false")
    for name in ("tester_present_interval_seconds", "node_timeout_seconds"):
        cfg[name] = float(cfg[name])
        if not math.isfinite(cfg[name]) or cfg[name] <= 0:
            raise ValueError(f"{name} must be positive")
    if cfg["node_timeout_seconds"] <= cfg["tester_present_interval_seconds"]:
        raise ValueError("Node timeout must exceed the TesterPresent interval")
    ids = cfg.get("response_ids") or ([*range(0x7E8, 0x7F0)] if cfg["request_id"] == 0x7DF and cfg["response_id"] == 0x7E8 else [cfg["response_id"]])
    if not isinstance(ids, list):
        raise ValueError("response_ids must be a list of numeric CAN IDs")
    cfg["response_ids"] = [int(i) for i in ids]
    maximum = 0x7FF if cfg["identifier_11_bit"] else 0x1FFFFFFF
    for value in [cfg["request_id"], cfg["response_id"], *cfg["response_ids"]]:
        if not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(f"CAN ID must be between 0 and {maximum:#x}")
    if int(cfg["bitrate"]) <= 0:
        raise ValueError("Bitrate must be positive")
    if cfg.get("extended_id") and not 0 <= int(cfg.get("extended_id_byte", -1)) <= 255:
        raise ValueError("Extended address must be a byte")
    return cfg
