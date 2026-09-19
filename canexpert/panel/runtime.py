"""
Panel script runtime: runs a database's Python script on a background thread and gives it the
DatabaseAPI (CAN, DBC signals, UDS over ISO-TP, DLL calls, UI values, flashing progress). Only the GUI
thread touches Qt widgets.
"""
from __future__ import annotations

import ctypes
import inspect
import math
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import can
from PyQt5.QtCore import QObject, pyqtSignal

from canexpert.config import uds_transport
from canexpert.flashing import load_firmware, parse_s19_s28_file
from canexpert.uds.client import UdsFunctions, uds_request


# -----------------------------------------------------------------------------
# Script API passed to DatabaseMainFunction(api) and Flashing(api, firmware)
# -----------------------------------------------------------------------------

class DatabaseAPI:
    """
    API passed to the script's DatabaseMainFunction(api), handlers and Flashing(api, firmware).
    - api.can.send(id, data), api.can.get_latest_messages()
    - api.uds.request(payload); api.uds.tester_present(), rdbi(did), request_download(format, addr, size),
      transfer_data(counter, data), request_transfer_exit(), transfer_data_from_file(path, packet_size)
      (the ISO 14229 functions RDBI, RD, TD, ... are also plain functions in the script)
    - api.dll.load(path), api.dll.call(path, name, *args)
    - api.ui.get_value(name), api.ui.set_value(name, value)
    - api.signal(name), api.set_signal(name, value), api.send_message(message, **signals)
    - api.log(msg), api.progress(done, total, message), api.flash_cancelled, api.sleep(seconds)
    """

    def __init__(self, can_bus=None, request_id: int = 0x7DF, response_id: int = 0x7E8, log_cb=None):
        self._bus = can_bus
        # uds_request() keyword arguments; the runtime sets them from the configuration (uds_transport)
        self._transport = {"request_id": request_id, "response_id": response_id, "timeout": 2.0,
                           "extended": False, "address_byte": None}
        self._log_cb = log_cb or (lambda s: None)
        self._latest_messages = []
        self._max_latest = 100
        self._dll_handles = {}
        self._runtime = None
        self._stop_event = None

        self.can = _CANApi(self)
        self.uds = _UDSApi(self)
        self.dll = _DLLApi(self)
        self.ui = _UIApi(self)

    def on(self, name, callback):
        """Register callback(value) for a named button/input event."""
        if self._runtime is None:
            raise RuntimeError("No script runtime")
        self._runtime.callbacks.setdefault(name, []).append(callback)

    def on_can(self, callback):
        """Register callback(arbitration_id, bytes) for incoming frames."""
        self._runtime.can_callbacks.append(callback)

    def every(self, seconds, callback):
        """Call callback() periodically while connected."""
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Timer interval must be positive")
        self._runtime.timers.append([time.monotonic() + seconds, seconds, callback])

    def sleep(self, seconds):
        """Cancellable sleep (returns early on disconnect); callbacks are preferred for waiting on events."""
        if self._stop_event:
            self._stop_event.wait(seconds)
        else:
            time.sleep(seconds)

    @property
    def running(self):
        return self._stop_event is None or not self._stop_event.is_set()

    def progress(self, done: int, total: int, message: str = ""):
        """Report flashing progress to the progress dialog."""
        if self._runtime is not None:
            self._runtime.flash_progress.emit(int(done), int(total), str(message))

    @property
    def flash_cancelled(self) -> bool:
        """True once the user pressed Cancel in the flashing progress dialog."""
        return self._runtime is not None and self._runtime.flash_cancel.is_set()

    def signal(self, name: str):
        """Latest physical value of a DBC signal ("Message.Signal"), or None before it was received."""
        return self._runtime.signal_values.get(name) if self._runtime else None

    def set_signal(self, name: str, value):
        """Encode one DBC signal into its message (the other signals keep their last values) and send it."""
        message_name, signal_name = name.split(".", 1)
        self.send_message(message_name, **{signal_name: value})

    def send_message(self, message_name: str, **signals):
        """Send a DBC message; signals not given keep their last received/sent value (else their initial value)."""
        runtime = self._runtime
        if runtime is None or runtime.dbc is None:
            raise RuntimeError("The panel has no DBC; set its DBC path in the Form Designer")
        message = runtime.dbc.get_message_by_name(message_name)
        values = runtime.message_values(message)
        values.update(signals)
        payload = message.encode(values, strict=False)
        if self._bus is None:
            return
        self._bus.send(can.Message(arbitration_id=message.frame_id, data=payload,
                                   is_extended_id=message.is_extended_frame))
        runtime.remember_frame(message, payload)

    def set_bus(self, bus):
        self._bus = bus

    def push_received_message(self, arbitration_id: int, data: list | bytes):
        self._latest_messages.append({"id": arbitration_id, "data": list(data)[:8]})
        if len(self._latest_messages) > self._max_latest:
            self._latest_messages.pop(0)

    def log(self, msg: str):
        self._log_cb(str(msg))


class _CANApi:
    def __init__(self, parent: DatabaseAPI):
        self._api = parent

    def send(self, can_id: int, data: list | bytes):
        """Send a CAN message."""
        if self._api._bus is None:
            return
        data = list(data)
        if len(data) > 8:
            raise ValueError("Classic CAN payload exceeds eight bytes")
        data.extend([0] * (8 - len(data)))
        msg = can.Message(arbitration_id=can_id, data=data, is_extended_id=self._api._transport["extended"])
        self._api._bus.send(msg)

    def get_latest_messages(self) -> list[dict]:
        """Return list of last received CAN messages: [{"id": int, "data": [bytes]}, ...]."""
        return list(self._api._latest_messages)


class _UDSApi:
    """UDS over ISO-TP with the session's transport. The methods besides request() are kept for older
    scripts; the ISO 14229 functions (RDBI, RD, TD, ...) cover every service."""

    def __init__(self, parent: DatabaseAPI):
        self._api = parent
        self.functions = UdsFunctions(self.request, parent.log)

    def request(self, payload: bytes | list, timeout: float | None = None, wait: bool = True) -> bytes | None:
        """Send any UDS request. Returns the reply (positive, or 0x7F negative) or None on timeout;
        wait=False only sends it."""
        transport = dict(self._api._transport)
        if timeout is not None:
            transport["timeout"] = timeout
        return uds_request(self._api._bus, bytes(payload), wait=wait, **transport)

    def tester_present(self, timeout: float | None = None) -> bool:
        return bool(self.functions.TP(timeout=timeout))

    def rdbi(self, did: int, timeout: float | None = None) -> bytes | None:
        """ReadDataByIdentifier. Returns the data record (without SID/DID echo) or None."""
        result = self.functions.RDBI(did, timeout=timeout)
        return result.data if result else None

    def request_download(self, format: int, address: int, size: int, timeout: float | None = None) -> bool:
        """RequestDownload (0x34). format is the address/length format, e.g. 0x44."""
        return bool(self.functions.RD(address, size, format, timeout=timeout))

    def transfer_data(self, sequence: int, data: bytes, timeout: float | None = None) -> bool:
        """TransferData (0x36). sequence is the block counter (0-255); data may span several frames."""
        return _acknowledged(self.functions.TD(sequence, data, timeout=timeout), sequence)

    def request_transfer_exit(self, timeout: float | None = None) -> bool:
        """RequestTransferExit (0x37)."""
        return bool(self.functions.RTE(timeout=timeout))

    def transfer_data_from_file(self, s19_or_s28_path: str | Path, packet_size: int,
                                progress_cb: Callable[[int, int], None] | None = None) -> tuple[bool, str]:
        """Download an S-record or Intel HEX file: per segment RequestDownload, TransferData blocks of
        packet_size bytes (at most the ECU's maxNumberOfBlockLength - 2) and RequestTransferExit.
        Returns (success, error message)."""
        try:
            firmware = load_firmware(s19_or_s28_path)
        except (OSError, ValueError) as exc:
            return False, str(exc)
        uds, sent = self.functions, 0
        for address, data in firmware.segments:
            download = uds.RD(address, len(data))
            if not download:
                return False, f"RequestDownload failed at 0x{address:X}: {download.error}"
            block = max(1, min(packet_size, (download.max_block_length or packet_size + 2) - 2))
            for counter, offset in enumerate(range(0, len(data), block), start=1):
                chunk = data[offset:offset + block]
                if not _acknowledged(uds.TD(counter, chunk), counter):
                    return False, f"TransferData failed at 0x{address + offset:X} (block {counter & 0xFF})"
                sent += len(chunk)
                if progress_cb:
                    progress_cb(sent, firmware.size)
            if not uds.RTE():
                return False, f"RequestTransferExit failed at 0x{address:X}"
        return True, ""

    @staticmethod
    def parse_s19_s28(path: str | Path) -> list[tuple[int, bytes]]:
        """(address, data) of every record of an S-record file."""
        return parse_s19_s28_file(path)


def _acknowledged(result, counter):
    """A positive TransferData response that echoes the block counter."""
    return bool(result) and result.raw[1:2] == bytes([counter & 0xFF])


class _DLLApi:
    def __init__(self, parent: DatabaseAPI):
        self._api = parent

    def load(self, dll_path: str | Path) -> bool:
        """Load a DLL. Returns True on success."""
        try:
            path = Path(dll_path)
            if not path.exists():
                return False
            h = ctypes.CDLL(str(path))
            self._api._dll_handles[str(path)] = h
            return True
        except Exception:
            return False

    def call(self, dll_path: str | Path, function_name: str, *args, restype=ctypes.c_int, argtypes=None) -> Any:
        """Call a function from a loaded DLL. Specify restype and argtypes for proper marshalling."""
        key = str(Path(dll_path))
        if key not in self._api._dll_handles:
            self.load(dll_path)
        dll = self._api._dll_handles.get(key)
        if dll is None:
            raise RuntimeError(f"DLL not loaded: {dll_path}")
        func = getattr(dll, function_name, None)
        if func is None:
            raise RuntimeError(f"Function not found: {function_name}")
        func.restype = restype
        if argtypes is not None:
            func.argtypes = argtypes
        return func(*args)


class _UIApi:
    """Panel control values; set_value() changes are shown by the GUI thread."""

    def __init__(self, parent: DatabaseAPI):
        self._api = parent

    def get_value(self, name: str):
        runtime = self._api._runtime
        return runtime.get_value(name) if runtime else None

    def set_value(self, name: str, value):
        if self._api._runtime:
            self._api._runtime.set_value(name, value)

    def get_widget(self, name: str):
        raise RuntimeError("Qt widgets belong to the GUI thread; use api.ui.get_value / set_value")


# Script of a new panel
SCRIPT_TEMPLATE = '''"""Panel script: Python in place of CAPL.

Controls call the function named in their Handler property (double-click a control in the
Form Designer to create one). Decorators work like CAPL "on" procedures:
    @on_start / @on_stop                    connect / disconnect
    @on_timer(1.0)                          every second
    @on_message(0x300) or ("EngineData")    a received frame: frame.id, frame.data, frame.signals
    @on_signal("EngineData.Temperature")    a DBC signal changed: value
    @on_control("start")                    a control named "start" was used: value
UDS services are plain functions: RDBI(0xF190) sends 22 F1 90 and returns a result (true when
positive; .data, .text, .int, .error). See the UDS functions panel beside the editor.
Name a function's first parameter api to receive the script API: api.signal("Msg.Sig"),
api.set_signal("Msg.Sig", value), api.send_message("Msg", Sig=value), api.can.send(id, data),
api.ui.set_value(name, value), api.log(text), api.uds..., api.every(seconds, callback).
Callbacks run one at a time on a background thread and stop on disconnect.
"""


def DatabaseMainFunction(api):
    api.log("Panel loaded")


# @on_timer(1.0)
# def every_second(api):
#     api.ui.set_value("status", "Running")


# @on_signal("EngineData.Temperature")
# def temperature_changed(api, value):
#     api.ui.set_value("temperature", value)


# Define Flashing to enable the Flashing toolbar button. firmware.segments is a list of
# (address, bytes); see examples/example_2026-09-18_script.py for an ISO 14229 sequence.
# def Flashing(api, firmware):
#     api.progress(0, firmware.size, "Starting")
#     return True
'''


# -----------------------------------------------------------------------------
# Script runtime
# -----------------------------------------------------------------------------

class ScriptStopped(BaseException):
    pass


class Frame:
    """A received CAN frame as passed to @on_message handlers."""
    __slots__ = ("id", "data", "signals")

    def __init__(self, can_id, data, signals):
        self.id, self.data, self.signals = can_id, data, signals  # signals: DBC name -> value, or {}

    def __repr__(self):
        return f"Frame(0x{self.id:X}, {self.data.hex(' ')}, {self.signals})"


_MISSING = object()


class ScriptRuntime(QObject):
    value_changed = pyqtSignal(str, object)
    logged = pyqtSignal(str)
    flashing_available = pyqtSignal(bool)
    flash_progress = pyqtSignal(int, int, str)
    flash_finished = pyqtSignal(bool, str)

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
        self.flash_function = None
        self.flash_cancel = threading.Event()
        self.dbc = None                 # panel DBC: signal decoding, @on_signal, api.set_signal
        self.handlers = {}              # control name -> handler function name (Form Designer)
        self.start_handlers, self.stop_handlers = [], []
        self.message_handlers = {}      # frame id -> [handler]
        self.signal_handlers = {}       # "Message.Signal" -> [(handler, every_update)]
        self.signal_values = {}
        self.last_frames = {}
        self._messages = None
        self._stop_done = threading.Event()
        self.api = DatabaseAPI(bus, log_cb=self.logged.emit)
        self.api._transport = uds_transport(config)
        self.api._runtime = self
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

    def _adapt(self, fn):
        """Call fn with the arguments it declares; a first parameter named api receives the script API."""
        try:
            parameters = list(inspect.signature(fn).parameters.values())
        except (TypeError, ValueError):
            return fn
        positional = [p for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        count = None if any(p.kind == p.VAR_POSITIONAL for p in parameters) else len(positional)
        wants_api = bool(positional) and positional[0].name == "api"
        api = self.api

        def call(*args):
            arguments = (api, *args) if wants_api else args
            return fn(*(arguments if count is None else arguments[:count]))
        return call

    def _frame_id(self, key):
        if isinstance(key, int):
            return key
        text = str(key).strip()
        if text.lower().startswith("0x"):
            return int(text, 16)
        if text.isdigit():
            return int(text)
        if self.dbc is None:
            raise ValueError(f"on_message('{text}') needs the panel's DBC")
        return self.dbc.get_message_by_name(text).frame_id

    def _namespace(self, path):
        """Script globals, including the CAPL-style event decorators."""
        runtime = self

        def on_start(fn):
            runtime.start_handlers.append(runtime._adapt(fn))
            return fn

        def on_stop(fn):
            runtime.stop_handlers.append(runtime._adapt(fn))
            return fn

        def on_timer(seconds):
            def register(fn):
                runtime.api.every(seconds, runtime._adapt(fn))
                return fn
            return register

        def on_message(*messages):
            def register(fn):
                for key in messages:
                    runtime.message_handlers.setdefault(runtime._frame_id(key), []).append(runtime._adapt(fn))
                return fn
            return register

        def on_signal(*names, every_update=False):
            def register(fn):
                for name in names:
                    runtime.signal_handlers.setdefault(name, []).append((runtime._adapt(fn), every_update))
                return fn
            return register

        def on_control(*names):
            def register(fn):
                for name in names:
                    runtime.callbacks.setdefault(name, []).append(runtime._adapt(fn))
                return fn
            return register

        # ISO 14229 service functions (RDBI, WDBI, DSC, ...) over the session's UDS transport.
        return {"__file__": str(path), "__name__": "canexpert_panel", "on_start": on_start, "on_stop": on_stop,
                "on_timer": on_timer, "on_message": on_message, "on_signal": on_signal, "on_control": on_control,
                **self.api.uds.functions.namespace()}

    def _register_handlers(self, namespace):
        for control, name in self.handlers.items():
            fn = namespace.get(name)
            if callable(fn):
                self.callbacks.setdefault(control, []).append(self._adapt(fn))
            else:
                self.logged.emit(f"Handler '{name}' for control '{control}' is not defined in the script")

    def message_values(self, message):
        """Current values of a DBC message's signals: last frame seen/sent, else the initial values."""
        last = self.last_frames.get(message.frame_id)
        if last is not None:
            try:
                return message.decode(last, decode_choices=False, allow_truncated=True)
            except Exception:
                pass
        return {signal.name: signal.initial if signal.initial is not None else 0 for signal in message.signals}

    def remember_frame(self, message, payload):
        self.last_frames[message.frame_id] = bytes(payload)
        try:
            decoded = message.decode(bytes(payload), decode_choices=False)
        except Exception:
            return
        self.signal_values.update({f"{message.name}.{name}": value for name, value in decoded.items()})

    def _on_frame(self, can_id, data):
        self.api.push_received_message(can_id, data)
        message, signals = None, {}
        if self.dbc is not None:
            if self._messages is None:
                self._messages = {m.frame_id: m for m in self.dbc.messages}
            message = self._messages.get(can_id)
            if message is not None:
                try:
                    signals = message.decode(bytes(data), decode_choices=False, allow_truncated=True)
                    self.last_frames[can_id] = bytes(data)
                except Exception:
                    signals = {}
        for callback in list(self.can_callbacks):
            self._call(callback, can_id, data)
        handlers = self.message_handlers.get(can_id)
        if handlers:
            frame = Frame(can_id, bytes(data), dict(signals))
            for handler in list(handlers):
                self._call(handler, frame)
        for name, value in signals.items():
            full_name = f"{message.name}.{name}"
            previous = self.signal_values.get(full_name, _MISSING)
            self.signal_values[full_name] = value
            for handler, every_update in list(self.signal_handlers.get(full_name, ())):
                if every_update or previous != value:
                    self._call(handler, value)

    def _run(self, code, path):
        # A Python loop can be interrupted on disconnect. Blocking native calls
        # cannot be forcibly killed; the bus facade is revoked independently.
        sys.settrace(self._trace)
        try:
            namespace = self._namespace(path)
            exec(code, namespace)
            self._register_handlers(namespace)
            flashing = namespace.get("Flashing")
            self.flash_function = flashing if callable(flashing) else None
            self.flashing_available.emit(self.flash_function is not None)
            startup = namespace.get("DatabaseMainFunction")
            if startup:
                startup(self.api)
            for handler in list(self.start_handlers):
                self._call(handler)
            while not self.stop_event.is_set():
                try:
                    kind, name, value = self.events.get(timeout=0.02)
                    if kind == "control":
                        for callback in list(self.callbacks.get(name, [])):
                            self._call(callback, value)
                    elif kind == "can":
                        self._on_frame(name, value)
                    elif kind == "flash":
                        self._flash(value)
                    elif kind == "stop":
                        for handler in list(self.stop_handlers):
                            self._call(handler)
                        self._stop_done.set()
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

    def _flash(self, firmware):
        """Run the database's Flashing(api, firmware); False or an exception reports failure."""
        self.flash_cancel.clear()
        try:
            result = self.flash_function(self.api, firmware)
            ok = result is not False
            message = "Flashing complete" if ok else "Flashing() reported failure"
        except Exception as exc:
            ok, message = False, str(exc) or type(exc).__name__
        self.flash_finished.emit(ok, message)

    def start_flash(self, firmware):
        if self.flash_function is None:
            raise RuntimeError("The database script does not define Flashing(api, firmware)")
        self.post("flash", None, firmware)

    def cancel_flash(self):
        """Request cooperative cancellation; the script checks api.flash_cancelled."""
        self.flash_cancel.set()

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
        # @on_stop handlers run first, while the bus is still usable (bounded wait for a busy script).
        if self.stop_handlers and self.thread is not None and self.thread.is_alive() and not self.stop_event.is_set():
            self._stop_done.clear()
            self.post("stop", None, None)
            self._stop_done.wait(1.0)
        self.stop_event.set()
        self.api.set_bus(None)
        if self.thread:
            self.thread.join(timeout=1.0)
