"""
Database script API - passed to DatabaseMainFunction(api).
Provides: CAN send/receive, UDS (TesterPresent, RDBI, RequestDownload, TransferData), DLL calls, UI get/set.
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
    uds_tester_present,
    uds_rdbi,
    uds_request_download,
    uds_transfer_data,
    uds_flash_from_file,
    parse_s19_s28_file,
)


class DatabaseAPI:
    """
    API injected into the user's DatabaseMainFunction(api).
    - api.can.send(id, data), api.can.get_latest_messages()
    - api.uds.tester_present(), api.uds.rdbi(did), api.uds.request_download(format, addr, size), api.uds.transfer_data_from_file(path, packet_size)
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

        self.can = _CANApi(self)
        self.uds = _UDSApi(self)
        self.dll = _DLLApi(self)
        self.ui = _UIApi(self)

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
        data = list(data)[:8]
        data.extend([0] * (8 - len(data)))
        msg = can.Message(arbitration_id=can_id, data=data, is_extended_id=False)
        self._api._bus.send(msg)

    def get_latest_messages(self) -> list[dict]:
        """Return list of last received CAN messages: [{"id": int, "data": [bytes]}, ...]."""
        return list(self._api._latest_messages)


class _UDSApi:
    def __init__(self, parent: DatabaseAPI):
        self._api = parent

    def tester_present(self, timeout: float = 0.5) -> bool:
        return uds_tester_present(
            self._api._bus,
            request_id=self._api._request_id,
            response_id=self._api._response_id,
            timeout=timeout,
        )

    def rdbi(self, did: int, timeout: float = 2.0) -> bytes | None:
        """ReadDataByIdentifier. Returns response data or None."""
        return uds_rdbi(
            self._api._bus,
            did,
            request_id=self._api._request_id,
            response_id=self._api._response_id,
            timeout=timeout,
        )

    def request_download(self, format: int, address: int, size: int, timeout: float = 2.0) -> bool:
        """RequestDownload (0x34). E.g. format 0x44, address 0x1000, size 0x255."""
        return uds_request_download(
            self._api._bus,
            format,
            address,
            size,
            request_id=self._api._request_id,
            response_id=self._api._response_id,
            timeout=timeout,
        )

    def transfer_data(self, sequence: int, data: bytes, timeout: float = 0.5) -> bool:
        """TransferData (0x36). sequence 1-based, data up to 6 bytes."""
        return uds_transfer_data(
            self._api._bus,
            sequence,
            data,
            request_id=self._api._request_id,
            response_id=self._api._response_id,
            timeout=timeout,
        )

    def transfer_data_from_file(
        self,
        s19_or_s28_path: str | Path,
        packet_size: int,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> tuple[bool, str]:
        """UDS_TD: flash using S19/S28 file, chunked in packets of packet_size bytes. Returns (success, error_msg)."""
        return uds_flash_from_file(
            self._api._bus,
            s19_or_s28_path,
            packet_size,
            request_id=self._api._request_id,
            response_id=self._api._response_id,
            progress_cb=progress_cb,
        )

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

    def get_value(self, name: str) -> str | None:
        """Get current displayed value of a value widget by name/label."""
        for w in self._api._widget_map.values():
            if getattr(w, "text", None) is not None:
                return w.text()
        return None

    def set_value(self, name: str, value: str | int | float):
        """Set the displayed value of a value widget by name/label."""
        w = self._api._widget_map.get(name)
        if w is not None and hasattr(w, "setText"):
            w.setText(str(value))

    def get_widget(self, name: str) -> Any:
        """Get widget reference by name (id or label)."""
        return self._api._widget_map.get(name)


# Template for user script
SCRIPT_TEMPLATE = '''"""
Database script - runs when this database is loaded.
Define DatabaseMainFunction(api) - it will be called with the API object.
You can define your own functions and call them from DatabaseMainFunction.
"""
# API: api.can.send(id, data), api.can.get_latest_messages()
#       api.uds.tester_present(), api.uds.rdbi(did)
#       api.uds.request_download(format, address, size)
#       api.uds.transfer_data_from_file(s19_path, packet_size)
#       api.dll.load(path), api.dll.call(path, "FunctionName", ...)
#       api.ui.get_value(name), api.ui.set_value(name, value)
#       api.log("message")


def DatabaseMainFunction(api):
    """Called when the database is loaded. Use api to send CAN, UDS, call DLLs, etc."""
    api.log("Database script started")
    # Example: send CAN
    # api.can.send(0x200, [0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    # Example: UDS RDBI
    # data = api.uds.rdbi(0xF190)
    # Example: UDS RequestDownload + TransferData from file
    # ok, err = api.uds.transfer_data_from_file("firmware.s19", packet_size=4)
'''
