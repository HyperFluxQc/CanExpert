"""
CAN access: opening a python-can bus, CanWorker (the only reader of a session's bus, which also sends the
periodic TesterPresent), ReceiveMailbox (a bus facade for code off the GUI thread) and
ChannelActivityScanner.
"""
from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager

import can
from PyQt5.QtCore import QThread, pyqtSignal

SUPPORTED_INTERFACES = [("kvaser", "Kvaser"), ("vector", "Vector"), ("ixxat", "IXXAT")]
_ADAPTER_OPTIONS = ("unique_hardware_id", "serial", "app_name")  # IXXAT hardware ID, Vector serial / app name


def channel_key(channel_config: dict) -> tuple:
    """Identifies a channel: interface, channel number and adapter."""
    return (channel_config.get("interface", "kvaser"), channel_config.get("channel", 0),
            channel_config.get("unique_hardware_id", ""), channel_config.get("serial", ""))


def create_can_bus(interface: str, channel, bitrate: int, **options) -> can.BusABC:
    """Open a python-can bus; adapter options other than _ADAPTER_OPTIONS are ignored."""
    return can.Bus(interface=interface, channel=channel, bitrate=bitrate,
                   **{key: value for key, value in options.items() if key in _ADAPTER_OPTIONS})


def open_channel(channel_config: dict, bitrate) -> can.BusABC:
    """Open a channel as listed by can.detect_available_configs(); its other keys (device name, ...) are dropped."""
    options = {key: channel_config[key] for key in _ADAPTER_OPTIONS if key in channel_config}
    return create_can_bus(channel_config["interface"], channel_config.get("channel", 0), int(bitrate), **options)


class CanWorker(QThread):
    """Reads the bus and delivers every frame to the GUI (message_received) and to the mailboxes; sends
    TesterPresent at the configuration's interval, deferred while a mailbox is in a UDS exchange."""
    message_received = pyqtSignal(dict)
    message_sent = pyqtSignal(int, bytes)
    error_occurred = pyqtSignal(str)

    def __init__(self, bus, config):
        super().__init__()
        self.bus, self.config = bus, config  # config: validated (canexpert.config.validate_config)
        self.running = True
        self.mailboxes = []

    def add_mailbox(self, mailbox):
        self.mailboxes = [*self.mailboxes, mailbox]

    def remove_mailbox(self, mailbox):
        self.mailboxes = [m for m in self.mailboxes if m is not mailbox]

    def run(self):
        cfg = self.config
        heartbeat = bytes([2, 0x3E, 0])
        if cfg.get("extended_id"):
            heartbeat = bytes([cfg["extended_id_byte"]]) + heartbeat
        next_heartbeat = 0.0
        while self.running:
            try:
                now = time.monotonic()
                if now >= next_heartbeat and any(m.in_transaction for m in self.mailboxes):
                    next_heartbeat = now + 0.05  # a request interleaved with a multi-frame exchange would abort it
                elif now >= next_heartbeat:
                    self.bus.send(can.Message(arbitration_id=cfg["request_id"], data=heartbeat,
                                              is_extended_id=not cfg["identifier_11_bit"]))
                    self.message_sent.emit(cfg["request_id"], heartbeat)
                    next_heartbeat = now + cfg["tester_present_interval_seconds"]
                message = self.bus.recv(timeout=min(0.05, max(0.001, next_heartbeat - time.monotonic())))
                if message and not message.is_error_frame and not message.is_remote_frame:
                    for mailbox in self.mailboxes:
                        mailbox.push(message)
                    self.message_received.emit({"timestamp": message.timestamp, "arbitration_id": message.arbitration_id,
                                                "is_extended_frame": message.is_extended_id, "data": list(message.data)})
            except Exception as exc:
                self.error_occurred.emit(f"CAN session failed: {exc}")
                self.running = False

    def stop(self):
        self.running = False
        for mailbox in self.mailboxes:
            mailbox.close()
        self.wait()


class ReceiveMailbox:
    """Bus facade for scripts and UDS exchanges: send() goes to the adapter, recv() reads the frames the
    CanWorker pushes, so the worker stays the only reader of the hardware."""

    def __init__(self, bus, sent=None):
        self.bus = bus
        self.sent = sent
        self.messages = queue.Queue(maxsize=2048)
        self.lock = threading.Lock()
        self.closed = False
        self._transactions = 0

    @contextmanager
    def transaction(self):
        """Mark a request/response exchange; the CAN worker defers TesterPresent meanwhile."""
        with self.lock:
            self._transactions += 1
        try:
            yield
        finally:
            with self.lock:
                self._transactions -= 1

    @property
    def in_transaction(self):
        return self._transactions > 0

    def clear(self):
        """Discard queued frames so a new request only sees replies received after it."""
        while True:
            try:
                self.messages.get_nowait()
            except queue.Empty:
                return

    def send(self, message):
        with self.lock:
            if self.closed:
                raise RuntimeError("CAN session is closed")
            self.bus.send(message)
        if self.sent:
            self.sent(message.arbitration_id, bytes(message.data))

    def push(self, message):
        """Queue a received frame; when full, the oldest frame is dropped."""
        if self.closed:
            return
        while True:
            try:
                self.messages.put_nowait(message)
                return
            except queue.Full:
                try:
                    self.messages.get_nowait()
                except queue.Empty:
                    pass

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


class ChannelActivityScanner(QThread):
    """Opens each channel briefly and reports whether any frame arrives (one bool per channel)."""
    channel_activity = pyqtSignal(list)

    def __init__(self, channels: list, bitrate: int = 500000, listen_time: float = 0.3):
        super().__init__()
        self.channels = channels
        self.bitrate = bitrate
        self.listen_time = listen_time

    def run(self):
        result = []
        for channel_config in self.channels:
            if self.isInterruptionRequested():
                break
            try:
                bus = open_channel(channel_config, self.bitrate)
            except Exception:
                result.append(False)
                continue
            try:
                deadline = time.monotonic() + self.listen_time
                active = False
                while not active and time.monotonic() < deadline:
                    active = bus.recv(timeout=0.05) is not None
                result.append(active)
            finally:
                bus.shutdown()
        self.channel_activity.emit(result)
