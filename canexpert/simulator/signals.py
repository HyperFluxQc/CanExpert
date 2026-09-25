"""
The dummy ECU's application traffic: the messages of a DBC, each signal driven by a generator.

The built-in database is DBC/dummy_ecu.dbc - 0x300 EngineData and 0x301 EcuStatus - and its default
generators reproduce what the ECU has always sent: the temperature warming up to 85 degC while the
engine runs (0x200 01 starts it, 02 stops it), the pressure with it, the status and a counter. Any DBC
can be loaded instead; each of its signals then gets a generator (constant, ramp, sine, square, random,
counter, or one of the ECU's own states) and each message its own period.

InputOutputControlByIdentifier (0x2F) takes a signal over with override() and hands it back with
release(). No Qt here: the ECU thread asks due_frames() what to send, the window reads value().
"""
from __future__ import annotations

import math
import random
import threading
import time

import can

try:
    import cantools
except ImportError:  # pragma: no cover - cantools is one of CAN Expert's requirements
    cantools = None

# The same database as DBC/dummy_ecu.dbc, so the ECU runs without the file (a frozen build, a copied
# dummy_ecu.py) and the panels that use the file still decode what it sends.
BUILTIN_DBC = """VERSION ""

NS_ :

BS_:

BU_: DummyECU

BO_ 768 EngineData: 8 DummyECU
 SG_ Temperature : 7|16@0+ (0.1,0) [0|6553.5] "degC" Vector__XXX
 SG_ Pressure : 23|16@0+ (0.01,0) [0|655.35] "bar" Vector__XXX

BO_ 769 EcuStatus: 8 DummyECU
 SG_ Running : 0|8@1+ (1,0) [0|1] "" Vector__XXX
 SG_ Logging : 8|8@1+ (1,0) [0|1] "" Vector__XXX
 SG_ Session : 16|8@1+ (1,0) [1|3] "" Vector__XXX
 SG_ Counter : 24|8@1+ (1,0) [0|255] "" Vector__XXX

VAL_ 769 Session 1 "default" 2 "programming" 3 "extended" ;
"""

# kind -> what it does, as the window explains it. Low, High and Period are the generator's settings.
GENERATORS = {
    "constant": "Constant: Low",
    "ramp": "Ramp from Low to High in Period seconds, then again from Low",
    "sine": "Sine wave between Low and High, one wave per Period",
    "square": "Square wave: High for the first half of Period, Low for the second",
    "random": "Random between Low and High, a new value in every frame",
    "counter": "Counter: one more in every frame, from Low up to High and round again",
    "running": "The engine: High while it runs (0x200 01), Low when stopped (0x200 02), "
               "approached with Period as time constant (0 = at once)",
    "logging": "Logging: High while it is on (0x201 bit 0), else Low",
    "session": "The diagnostic session: 1 default, 2 programming, 3 extended",
}
# What the ECU has always sent, as generators of the built-in database.
DEFAULT_GENERATORS = (
    {"signal": "EngineData.Temperature", "kind": "running", "low": 21.5, "high": 85.0, "period": 5.0},
    {"signal": "EngineData.Pressure", "kind": "running", "low": 1.0, "high": 1.8, "period": 0.0},
    {"signal": "EcuStatus.Running", "kind": "running", "low": 0.0, "high": 1.0, "period": 0.0},
    {"signal": "EcuStatus.Logging", "kind": "logging", "low": 0.0, "high": 1.0, "period": 0.0},
    {"signal": "EcuStatus.Session", "kind": "session", "low": 1.0, "high": 3.0, "period": 0.0},
    {"signal": "EcuStatus.Counter", "kind": "counter", "low": 0.0, "high": 255.0, "period": 0.0},
)
# DBC/j1939_demo.dbc's signals, moving on their own: an engine idling up and down, a vehicle speeding up.
J1939_DEMO_GENERATORS = (
    {"signal": "EEC1.EngineSpeed", "kind": "sine", "low": 700.0, "high": 1800.0, "period": 12.0},
    {"signal": "EEC1.ActualEnginePercentTorque", "kind": "sine", "low": 10.0, "high": 45.0, "period": 7.0},
    {"signal": "EEC1.DriversDemandEngPercentTorque", "kind": "sine", "low": 12.0, "high": 50.0, "period": 7.0},
    {"signal": "CCVS.WheelBasedVehicleSpeed", "kind": "ramp", "low": 0.0, "high": 90.0, "period": 60.0},
    {"signal": "ET1.EngineCoolantTemperature", "kind": "sine", "low": 82.0, "high": 94.0, "period": 30.0},
    {"signal": "ET1.EngineOilTemperature", "kind": "sine", "low": 90.0, "high": 105.0, "period": 40.0},
)
KNOWN_GENERATORS = DEFAULT_GENERATORS + J1939_DEMO_GENERATORS   # what a newly chosen DBC starts with


def read_database(source: str = ""):
    """The DBC at source, or the built-in one for "". ValueError saying why it cannot be used."""
    if cantools is None:
        raise ValueError("cantools is not installed (pip install cantools)")
    try:
        if source:
            database = cantools.database.load_file(source)
        else:
            database = cantools.database.load_string(BUILTIN_DBC, database_format="dbc")
    except Exception as exc:
        raise ValueError(f"Cannot read {source or 'the built-in database'}: {exc}") from None
    if not getattr(database, "messages", None):
        raise ValueError(f"{source} describes no CAN messages")
    return database


def check_generators(generators) -> None:
    """ValueError naming the entry that is wrong."""
    for item in generators:
        try:
            key, kind = str(item["signal"]), str(item.get("kind", "constant"))
            float(item.get("low", 0.0)), float(item.get("high", 0.0)), float(item.get("period", 0.0))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Generator {item}: needs a signal, and numbers for Low, High and Period") from None
        if kind not in GENERATORS:
            raise ValueError(f"{key}: unknown generator {kind!r}")


def raw_range(signal) -> tuple[int, int]:
    """The raw values the signal's bits can hold."""
    if signal.is_signed:
        return -(1 << (signal.length - 1)), (1 << (signal.length - 1)) - 1
    return 0, (1 << signal.length) - 1


def to_raw(signal, value: float):
    """The raw value a physical value is sent as, rounded and kept within the signal's bits."""
    if signal.is_float:
        return float(value)
    scale = signal.scale or 1
    low, high = raw_range(signal)
    try:
        raw = round((float(value) - signal.offset) / scale)
    except (OverflowError, ValueError):     # inf, nan
        raw = low
    return max(low, min(high, raw))


def to_physical(signal, raw) -> float:
    return float(raw) if signal.is_float else raw * (signal.scale or 1) + signal.offset


class _Signal:
    """One signal: its generator, and what it produced last."""

    def __init__(self, key, signal):
        self.key, self.signal = key, signal
        initial = getattr(signal, "initial", None)
        default = initial if isinstance(initial, (int, float)) else 0.0
        if signal.minimum is not None and default < signal.minimum:
            default = signal.minimum
        self.kind, self.low, self.high, self.period = "constant", float(default), float(default), 0.0
        self.value = None         # physical, as last sent or read
        self.raw = None
        self.count = 0            # the counter's position
        self.updated = 0.0
        self.override = None      # the raw value InputOutputControl holds it at


class _Message:
    def __init__(self, message):
        self.message = message
        self.on = True
        self.cycle = (message.cycle_time or 0) / 1000     # the DBC's GenMsgCycleTime, 0 = the default period
        self.next = 0.0
        # Not sent: multiplexed messages (which branch?) and CAN FD lengths.
        self.sendable = not message.is_multiplexed() and message.length <= 8


class SignalSimulation:
    """The values of the application signals, and the frames that carry them.

    inputs() returns the ECU's own states the generators can follow: {"running", "logging", "session"}.
    The ECU's frames thread calls due_frames(); the window configures it and reads values from its own thread.
    Times are time.perf_counter(): time.monotonic() moves in 15.6 ms steps on Windows before Python 3.13.
    """

    def __init__(self, inputs=None):
        self.inputs = inputs or (lambda: {"running": False, "logging": False, "session": 1})
        self.lock = threading.RLock()
        self.source = None           # "" = the built-in database, else its path; None = nothing loaded
        self.database = None
        self.messages: list[_Message] = []
        self.signals: dict[str, _Signal] = {}
        self.started = time.perf_counter()
        self._random = random.Random()

    # --- the database and the generators ----------------------------------------------------------

    def load(self, source: str = "") -> None:
        """Take the messages of a DBC ("" = the built-in one). ValueError when it cannot be read; the
        database in use until then stays."""
        database = read_database(source)
        with self.lock:
            self.database, self.source = database, source
            self.messages = [_Message(message) for message in database.messages]
            self.signals = {f"{message.name}.{signal.name}": _Signal(f"{message.name}.{signal.name}", signal)
                            for message in database.messages for signal in message.signals}
            self.started = time.perf_counter()

    def configure(self, generators=(), messages=()) -> None:
        """Generators by "Message.Signal" (a signal not listed stays at its initial value), and per
        message whether it is sent and at what period (cycle_ms 0: the DBC's, else the default period)."""
        check_generators(generators)
        wanted = {str(item["signal"]): item for item in generators}
        with self.lock:
            for key, entry in self.signals.items():
                item = wanted.get(key)
                if item is None:
                    continue
                kind = str(item.get("kind", "constant"))
                if kind != entry.kind:
                    entry.value, entry.count = None, 0
                entry.kind = kind
                entry.low, entry.high = float(item.get("low", 0.0)), float(item.get("high", 0.0))
                entry.period = max(0.0, float(item.get("period", 0.0)))
            settings = {str(item.get("message")): item for item in messages}
            for entry in self.messages:
                item = settings.get(entry.message.name)
                entry.on = bool(item.get("on", True)) if item else True
                cycle_ms = float(item.get("cycle_ms", 0) or 0) if item else 0.0
                entry.cycle = cycle_ms / 1000 if cycle_ms > 0 else (entry.message.cycle_time or 0) / 1000

    def generators(self) -> list[dict]:
        """Every signal with its generator, in database order - what configure() takes."""
        with self.lock:
            return [{"signal": key, "kind": entry.kind, "low": entry.low, "high": entry.high, "period": entry.period}
                    for key, entry in self.signals.items()]

    def message_ids(self) -> set[int]:
        """The identifiers the ECU sends application frames on."""
        with self.lock:
            return {entry.message.frame_id for entry in self.messages if entry.on and entry.sendable}

    # --- values ------------------------------------------------------------------------------------

    def _evaluate(self, entry: _Signal, now: float, advance: bool) -> float:
        """The signal's physical value now; advance=True for a frame (counters count, random draws)."""
        if entry.override is not None:
            return to_physical(entry.signal, entry.override)
        kind, low, high, period = entry.kind, entry.low, entry.high, entry.period
        elapsed = now - self.started
        if kind == "ramp":
            value = low + (high - low) * ((elapsed / period) % 1.0) if period > 0 else low
        elif kind == "sine":
            middle, amplitude = (low + high) / 2, (high - low) / 2
            value = middle + amplitude * math.sin(2 * math.pi * elapsed / period) if period > 0 else middle
        elif kind == "square":
            value = high if period > 0 and (elapsed / period) % 1.0 < 0.5 else low
        elif kind == "random":
            value = self._random.uniform(low, high) if advance or entry.value is None else entry.value
        elif kind == "counter":
            if advance:
                entry.count += 1
            span = max(1, int(round(high - low)) + 1)
            value = low + entry.count % span
        elif kind in ("running", "logging"):
            target = high if self.inputs().get(kind) else low
            if kind == "running" and period > 0 and entry.value is not None:
                value = entry.value + (target - entry.value) * (1 - math.exp(-max(0.0, now - entry.updated) / period))
            else:
                value = target
        elif kind == "session":
            value = float(self.inputs().get("session", 1))
        else:
            value = low
        entry.value, entry.updated = value, now
        return value

    def _current(self, key: str):
        """The signal's entry with its raw value brought up to now (a read: counters do not count)."""
        entry = self.signals.get(key)
        if entry is None:
            return None
        if entry.override is not None:
            entry.raw = entry.override
            return entry
        if entry.value is None or entry.kind not in ("random", "counter"):
            self._evaluate(entry, time.perf_counter(), advance=False)
        entry.raw = to_raw(entry.signal, entry.value)
        return entry

    def value(self, key: str):
        """The physical value of "Message.Signal" as it would be sent now (None: no such signal)."""
        with self.lock:
            entry = self._current(key)
            return None if entry is None else to_physical(entry.signal, entry.raw)

    def raw(self, key: str):
        """The raw value of "Message.Signal" as it would be sent now (None: no such signal)."""
        with self.lock:
            entry = self._current(key)
            return None if entry is None else entry.raw

    def signal(self, key: str):
        """The cantools signal of "Message.Signal", or None."""
        entry = self.signals.get(key)
        return entry.signal if entry is not None else None

    # --- InputOutputControlByIdentifier -----------------------------------------------------------

    def override(self, key: str, raw) -> None:
        """Hold the signal at a raw value until release(); the frames carry it."""
        with self.lock:
            entry = self.signals[key]
            low, high = raw_range(entry.signal)
            entry.override = raw if entry.signal.is_float else max(low, min(high, int(raw)))

    def release(self, key: str) -> None:
        """Back to the generator, which carries on from the value the signal was held at."""
        with self.lock:
            entry = self.signals.get(key)
            if entry is not None and entry.override is not None:
                entry.value, entry.updated = to_physical(entry.signal, entry.override), time.perf_counter()
                entry.override = None

    def default_raw(self, key: str):
        """What resetToDefault sets: the generator's Low."""
        with self.lock:
            entry = self.signals[key]
            return to_raw(entry.signal, entry.low)

    def overridden(self) -> dict[str, object]:
        with self.lock:
            return {key: entry.override for key, entry in self.signals.items() if entry.override is not None}

    # --- frames ------------------------------------------------------------------------------------

    def next_due(self, default_period: float) -> float | None:
        """When the next frame is due (time.perf_counter()), or None when no message is sent."""
        with self.lock:
            return min((entry.next for entry in self.messages
                        if entry.on and entry.sendable and (entry.cycle or default_period) > 0), default=None)

    def due_frames(self, default_period: float, now: float | None = None) -> list[can.Message]:
        """The frames whose period has come. Each message keeps its own schedule; one that fell far
        behind (a long pause) starts again from now instead of sending the backlog."""
        now = time.perf_counter() if now is None else now
        frames = []
        with self.lock:
            for entry in self.messages:
                period = entry.cycle or default_period
                if not entry.on or not entry.sendable or period <= 0 or now < entry.next:
                    continue
                entry.next = entry.next + period if entry.next + period > now else now + period
                message = entry.message
                raws = {}
                for signal in message.signals:
                    state = self.signals[f"{message.name}.{signal.name}"]
                    physical = self._evaluate(state, now, advance=True)
                    state.raw = state.override if state.override is not None else to_raw(signal, physical)
                    raws[signal.name] = state.raw
                try:
                    data = message.encode(raws, scaling=False, strict=False)
                except Exception:            # a signal layout cantools cannot encode: leave the message out
                    entry.sendable = False
                    continue
                frames.append(can.Message(arbitration_id=message.frame_id, data=data,
                                          is_extended_id=message.is_extended_frame))
        return frames
