"""
Database script API - passed to DatabaseMainFunction(api).
Provides: CAN send/receive, UDS over ISO-TP (any request, TesterPresent, RDBI, RequestDownload,
TransferData, RequestTransferExit), DLL calls, UI get/set.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

try:
    import can
except ImportError:
    can = None

from uds_services import (
    uds_request,
    uds_tester_present,
    uds_rdbi,
    uds_request_download,
    uds_transfer_data,
    uds_request_transfer_exit,
    uds_flash_from_file,
    parse_s19_s28_file,
)


class DatabaseAPI:
    """
    API injected into the user's DatabaseMainFunction(api).
    - api.can.send(id, data), api.can.get_latest_messages()
    - api.uds.request(payload), api.uds.tester_present(), api.uds.rdbi(did),
      api.uds.request_download(format, addr, size), api.uds.transfer_data_from_file(path, packet_size)
    - api.dll.load(path), api.dll.call(name, *args)
    - api.ui.get_value(name), api.ui.set_value(name, value), api.ui.get_widget(name)
    - api.log(msg)
    """

    def __init__(self, can_bus=None, request_id: int = 0x7DF, response_id: int = 0x7E8, widget_map=None, log_cb=None):
        self._bus = can_bus
        self._request_id = request_id
        self._response_id = response_id
        self._widget_map = widget_map or {}
        self._log_cb = log_cb or (lambda s: None)
        self._latest_messages = []
        self._max_latest = 100
        self._dll_handles = {}
        self._runtime = None
        self._extended = False
        self._address_byte = None
        self._uds_timeout = 2.0
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
        import math
        import time
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Timer interval must be positive")
        self._runtime.timers.append([time.monotonic() + seconds, seconds, callback])

    def sleep(self, seconds):
        """Cancellable sleep for legacy scripts; callbacks are preferred."""
        if self._stop_event:
            self._stop_event.wait(seconds)

    @property
    def running(self):
        return self._stop_event is None or not self._stop_event.is_set()

    def set_bus(self, bus):
        self._bus = bus

    def set_widget_map(self, widget_map: dict):
        self._widget_map = widget_map

    def set_log_callback(self, cb: Callable[[str], None]):
        self._log_cb = cb

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
        msg = can.Message(arbitration_id=can_id, data=data, is_extended_id=self._api._extended)
        self._api._bus.send(msg)

    def get_latest_messages(self) -> list[dict]:
        """Return list of last received CAN messages: [{"id": int, "data": [bytes]}, ...]."""
        return list(self._api._latest_messages)


class _UDSApi:
    """UDS over ISO-TP using the session's request/response IDs, identifier size and address byte."""

    def __init__(self, parent: DatabaseAPI):
        self._api = parent

    def _args(self, timeout):
        api = self._api
        return {
            "request_id": api._request_id,
            "response_id": api._response_id,
            "timeout": api._uds_timeout if timeout is None else timeout,
            "extended": api._extended,
            "address_byte": api._address_byte,
        }

    def request(self, payload: bytes | list, timeout: float | None = None) -> bytes | None:
        """Send any UDS request. Returns the reply (positive, or 0x7F negative) or None on timeout."""
        return uds_request(self._api._bus, bytes(payload), **self._args(timeout))

    def tester_present(self, timeout: float | None = None) -> bool:
        return uds_tester_present(self._api._bus, **self._args(timeout))

    def rdbi(self, did: int, timeout: float | None = None) -> bytes | None:
        """ReadDataByIdentifier. Returns the data record (without SID/DID echo) or None."""
        return uds_rdbi(self._api._bus, did, **self._args(timeout))

    def request_download(self, format: int, address: int, size: int, timeout: float | None = None) -> bool:
        """RequestDownload (0x34). format is the address/length format, e.g. 0x44."""
        return uds_request_download(self._api._bus, format, address, size, **self._args(timeout))

    def transfer_data(self, sequence: int, data: bytes, timeout: float | None = None) -> bool:
        """TransferData (0x36). sequence is the block counter (0-255); data may span several frames."""
        return uds_transfer_data(self._api._bus, sequence, data, **self._args(timeout))

    def request_transfer_exit(self, timeout: float | None = None) -> bool:
        """RequestTransferExit (0x37)."""
        return uds_request_transfer_exit(self._api._bus, **self._args(timeout))

    def transfer_data_from_file(
        self,
        s19_or_s28_path: str | Path,
        packet_size: int,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> tuple[bool, str]:
        """Flash an S19/S28 file in TransferData blocks of packet_size bytes. Returns (success, error_msg)."""
        return uds_flash_from_file(self._api._bus, s19_or_s28_path, packet_size,
                                   progress_cb=progress_cb, **self._args(None))

    @staticmethod
    def parse_s19_s28(path: str | Path) -> list[tuple[int, bytes]]:
        """Parse S19/S28 file; returns list of (address, data) blocks."""
        return parse_s19_s28_file(path)


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
    def __init__(self, parent: DatabaseAPI):
        self._api = parent

    def get_value(self, name: str):
        if self._api._runtime:
            return self._api._runtime.get_value(name)
        w = self._api._widget_map.get(name)
        if w is None:
            return None
        for method in ("isChecked", "value", "currentText", "text"):
            if hasattr(w, method):
                return getattr(w, method)()
        return None

    def set_value(self, name: str, value):
        if self._api._runtime:
            self._api._runtime.set_value(name, value)
            return
        w = self._api._widget_map.get(name)
        if w is not None:
            if hasattr(w, "setText"):
                w.setText(str(value))
            elif hasattr(w, "setValue"):
                w.setValue(value)

    def get_widget(self, name: str):
        if self._api._runtime:
            raise RuntimeError("Use ui.get_value/set_value; Qt widgets belong to the GUI thread")
        return self._api._widget_map.get(name)


# Template for user script
SCRIPT_TEMPLATE = '''"""Python panel script. Register callbacks and return from startup.
Name controls with their script binding (e.g. start, status).
Callbacks execute serially on a background thread and stop on disconnect.
"""


def DatabaseMainFunction(api):
    api.log("Panel loaded")
    # api.on("start", lambda value: api.can.send(0x200, [1]))
    # api.on("setpoint", lambda value: api.log(f"Setpoint: {value}"))
    # api.on_can(lambda can_id, data: api.ui.set_value("status", data.hex()))
    # api.every(1.0, lambda: api.ui.set_value("status", "Running"))
'''
