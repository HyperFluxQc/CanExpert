"""
CAN access: opening a python-can bus, CanWorker (the only reader of a session's bus, which also sends the
periodic TesterPresent and picks up the diagnostic responses nobody asked for) and ReceiveMailbox (a bus facade
for code off the GUI thread).
"""
from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager

import can
from PyQt5.QtCore import QThread, pyqtSignal

from canexpert.config import uds_transport
from canexpert.uds.isotp import N_CR_TIMEOUT, flow_control_frame, parse_first_frame

SUPPORTED_INTERFACES = [("kvaser", "Kvaser"), ("vector", "Vector"), ("ixxat", "IXXAT")]
_ADAPTER_OPTIONS = ("unique_hardware_id", "serial", "app_name")  # IXXAT hardware ID, Vector serial / app name
_SETUP_OPTIONS = ("timing", "driver_mode", "listen_only")       # from a channel setup (channel_setup.py)
# How python-can opens each adapter without acknowledging or sending anything. IXXAT has no such option.
LISTEN_ONLY_OPTIONS = {"kvaser": {"driver_mode": False},           # canlib's silent mode
                       "vector": {"listen_only": True},
                       "virtual": {}}


def channel_key(channel_config: dict) -> tuple:
    """Identifies a channel: interface, channel number and adapter."""
    return (channel_config.get("interface", "kvaser"), channel_config.get("channel", 0),
            channel_config.get("unique_hardware_id", ""), channel_config.get("serial", ""))


def create_can_bus(interface: str, channel, bitrate: int, **options) -> can.BusABC:
    """Open a python-can bus; options other than the adapter's and a channel setup's are ignored."""
    return can.Bus(interface=interface, channel=channel, bitrate=bitrate,
                   **{key: value for key, value in options.items() if key in _ADAPTER_OPTIONS + _SETUP_OPTIONS})


def open_channel(channel_config: dict, bitrate, **setup_options) -> can.BusABC:
    """Open a channel as listed by can.detect_available_configs(); its other keys (device name, ...) are
    dropped. setup_options are what a channel setup adds (bit timing, listen-only)."""
    options = {key: channel_config[key] for key in _ADAPTER_OPTIONS if key in channel_config}
    options.update(setup_options)
    return create_can_bus(channel_config["interface"], channel_config.get("channel", 0), int(bitrate), **options)


BUS_STATES = {"ACTIVE": "error active", "PASSIVE": "error passive", "ERROR": "bus off"}
STATUS_INTERVAL = 0.5   # how often the adapter's error state is read and reported


def is_tester_present_answer(payload: bytes) -> bool:
    """The ECU's answer to the session's own TesterPresent: 7E 00, or a negative response to 3E."""
    payload = bytes(payload)
    return payload[:1] == b"\x7e" or (payload[:1] == b"\x7f" and payload[1:2] == b"\x3e")


class UnsolicitedAssembler:
    """Reassembles the ISO-TP messages an ECU sends on its response ID without being asked - periodic data
    (6A <identifier> <data>) and ResponseOnEvent answers - one frame at a time, answering a first frame with
    the session's flow control as a tester must, or the ECU gives up on the message.

    transport: canexpert.config.uds_transport(); send(can.Message) puts a flow control frame on the bus.
    """

    def __init__(self, transport: dict, send):
        self.request_id, self.response_id = transport["request_id"], transport["response_id"]
        self.extended = bool(transport.get("extended"))
        self.address_byte, self.padding = transport.get("address_byte"), transport.get("padding")
        self.block_size, self.st_min = transport.get("block_size", 0), transport.get("st_min", 0)
        self.send = send
        self.message = None      # the multi-frame message in progress: total, data, next sequence, frames left

    @property
    def busy(self) -> bool:
        """A multi-frame message is on its way: a request now would cut across it."""
        return self.message is not None and time.monotonic() <= self.message["deadline"]

    def reset(self):
        self.message = None

    def push(self, message) -> bytes | None:
        """Take one received frame; returns the message it completes, else None."""
        if message.arbitration_id != self.response_id or bool(message.is_extended_id) != self.extended:
            return None
        data = bytes(message.data)
        if self.address_byte is not None:
            data = data[1:]
        if not data:
            return None
        room = 7 - (self.address_byte is not None)
        now = time.monotonic()
        if self.message and now > self.message["deadline"]:
            self.message = None                        # N_Cr expired: the rest of that message is lost
        kind = data[0] >> 4
        if kind == 0x0:
            self.message = None
            length = data[0] & 0x0F
            return data[1:1 + length] if 0 < length <= min(room, len(data) - 1) else None
        if kind == 0x1:
            first = parse_first_frame(data, room)
            if first is None:
                return None
            self.message = {"total": first[0], "data": bytearray(first[1]), "next": 1, "left": self.block_size,
                            "deadline": now + N_CR_TIMEOUT}
            self._flow_control()
            return None
        if kind != 0x2 or not self.message:
            return None
        message = self.message
        if data[0] & 0x0F != message["next"]:
            self.message = None                        # out of sequence: the message is dropped
            return None
        message["data"] += data[1:]
        if len(message["data"]) >= message["total"]:
            self.message = None
            return bytes(message["data"][:message["total"]])
        message["next"] = (message["next"] + 1) & 0x0F
        message["left"] -= 1
        if message["left"] == 0:                       # end of a block: the ECU waits for flow control
            message["left"] = self.block_size
            self._flow_control()
        message["deadline"] = now + N_CR_TIMEOUT
        return None

    def _flow_control(self):
        data = flow_control_frame(self.block_size, self.st_min)
        if self.address_byte is not None:
            data = bytes([self.address_byte]) + data
        if self.padding is not None:
            data = data.ljust(8, bytes([self.padding]))
        self.send(can.Message(arbitration_id=self.request_id, data=data, is_extended_id=self.extended))


class CanWorker(QThread):
    """Reads the bus and delivers every frame to the GUI (message_received) and to the mailboxes; sends
    TesterPresent at the configuration's interval, deferred while a mailbox is in a UDS exchange.

    Error frames do not carry data, so they go to error_frame() instead; bus_status() reports the
    adapter's error state (error active, error passive, bus off) while the session runs.

    With unsolicited=True, diagnostic responses nobody waits for - periodic data after 0x2A, the answers
    ResponseOnEvent (0x86) sends when its event happens - go to unsolicited(time, payload): between requests
    the worker reassembles them itself (with flow control); during one, the mailbox waiting for its answer
    hands over the replies that are not it, and at its end the frames it did not read (an event the answer
    set off, sent right behind it).
    """
    message_received = pyqtSignal(dict)
    message_sent = pyqtSignal(int, bytes)
    error_frame = pyqtSignal(float)
    bus_status = pyqtSignal(dict)
    error_occurred = pyqtSignal(str)
    unsolicited = pyqtSignal(float, bytes)

    def __init__(self, bus, config, tester_present=True, unsolicited=False):
        super().__init__()
        self.bus, self.config = bus, config  # config: validated (canexpert.config.validate_config)
        self.running = True
        self.tester_present = tester_present   # off in tests that must see no heartbeat on the bus
        self.report_unsolicited = unsolicited  # off where nothing listens, and on a listen-only channel
        self.error_frames = 0
        self.mailboxes = []
        self.assembler = None
        self._unread = queue.SimpleQueue()     # frames an exchange left unread, from the mailboxes' threads
        if unsolicited:
            self.assembler = UnsolicitedAssembler(uds_transport(config), self._send_flow_control)

    def add_mailbox(self, mailbox):
        mailbox.unsolicited = self.report
        mailbox.unread = self._unread.put
        self.mailboxes = [*self.mailboxes, mailbox]

    def report(self, payload, timestamp=None):
        """A diagnostic response nobody waited for; the answers to the session's TesterPresent are not news."""
        payload = bytes(payload)
        if self.report_unsolicited and payload and not is_tester_present_answer(payload):
            self.unsolicited.emit(time.time() if timestamp is None else timestamp, payload)

    def _send_flow_control(self, message):
        self.bus.send(message)
        self.message_sent.emit(message.arbitration_id, bytes(message.data))

    def _unsolicited_frame(self, message):
        """Between requests a frame of the response ID belongs to no one: reassemble what the ECU sends."""
        payload = self.assembler.push(message)
        if payload is not None:
            self.report(payload)

    def _take_unread(self):
        """The frames an exchange that just ended left unread: they came after its answer, so no one has them."""
        while True:
            try:
                frames = self._unread.get_nowait()
            except queue.Empty:
                return
            if self.assembler is not None:
                for message in frames:
                    self._unsolicited_frame(message)

    def remove_mailbox(self, mailbox):
        self.mailboxes = [m for m in self.mailboxes if m is not mailbox]

    def run(self):
        cfg = self.config
        heartbeat = bytes([2, 0x3E, 0])
        if cfg.get("extended_id"):
            heartbeat = bytes([cfg["extended_id_byte"]]) + heartbeat
        if cfg.get("isotp_padding") is not None:              # padded like the session's other requests
            heartbeat = heartbeat.ljust(8, bytes([cfg["isotp_padding"]]))
        next_heartbeat = 0.0 if self.tester_present else float("inf")
        next_status = 0.0
        while self.running:
            try:
                self._take_unread()
                now = time.monotonic()
                exchange = any(m.in_transaction for m in self.mailboxes) or (self.assembler and self.assembler.busy)
                if now >= next_heartbeat and exchange:
                    next_heartbeat = now + 0.05  # a request interleaved with a multi-frame exchange would abort it
                elif now >= next_heartbeat:
                    self.bus.send(can.Message(arbitration_id=cfg["request_id"], data=heartbeat,
                                              is_extended_id=not cfg["identifier_11_bit"]))
                    self.message_sent.emit(cfg["request_id"], heartbeat)
                    next_heartbeat = now + cfg["tester_present_interval_seconds"]
                if now >= next_status:
                    next_status = now + STATUS_INTERVAL
                    self.bus_status.emit({"state": self.state(), "error_frames": self.error_frames})
                message = self.bus.recv(timeout=min(0.05, max(0.001, next_heartbeat - time.monotonic())))
                if message is not None and message.is_error_frame:
                    self.error_frames += 1
                    self.error_frame.emit(message.timestamp)
                elif message and not message.is_remote_frame:
                    # A mailbox in an exchange reads the frame itself; the list keeps every mailbox getting it.
                    claimed = any([mailbox.push(message) for mailbox in self.mailboxes])
                    if self.assembler is not None and claimed:
                        self.assembler.reset()
                    elif self.assembler is not None:
                        self._unsolicited_frame(message)
                    self.message_received.emit({"timestamp": message.timestamp, "arbitration_id": message.arbitration_id,
                                                "is_extended_frame": message.is_extended_id, "data": list(message.data)})
            except Exception as exc:
                self.error_occurred.emit(f"CAN session failed: {exc}")
                self.running = False

    def state(self) -> str:
        """The adapter's error state in words, or "unknown" where python-can does not report one."""
        try:
            return BUS_STATES.get(getattr(self.bus.state, "name", ""), "unknown")
        except Exception:                      # most interfaces, including virtual, have no state
            return "unknown"

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
        self._count_lock = threading.Lock()   # the exchange count, and the frames pushed while it changes
        # Set by the CAN worker: where uds_request() hands the replies that are not its answer, and where an
        # exchange's end hands the frames it left unread.
        self.unsolicited = None
        self.unread = None

    @contextmanager
    def transaction(self):
        """Mark a request/response exchange; the CAN worker defers TesterPresent meanwhile and leaves the
        frames to this mailbox. At the end, the frames still queued - they came after the answer - go back to
        the worker, which then reads them as it reads every frame between exchanges."""
        with self._count_lock:
            self._transactions += 1
        try:
            yield
        finally:
            with self._count_lock:
                self._transactions -= 1
                unread = list(self.messages.queue) if not self._transactions and self.unread else []
            if unread:
                self.unread(unread)

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

    def push(self, message) -> bool:
        """Queue a received frame; when full, the oldest frame is dropped. Returns whether an exchange was
        running, which then reads the frame or hands it back at its end."""
        if self.closed:
            return False
        with self._count_lock:
            while True:
                try:
                    self.messages.put_nowait(message)
                    return self._transactions > 0
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
