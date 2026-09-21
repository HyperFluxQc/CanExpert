"""
How a CAN channel is opened, beyond the configuration's bit rate: the sample point and SJW, listen-only,
which identifiers it lets through, and finding the bit rate of a bus nobody told you about.

This is CANoe's channel setup. It belongs to the adapter channel rather than to a configuration - the
same ECU configuration can be used on a bench where the sample point matters and a vehicle where it
does not - so it is kept in CAN Expert's settings under the channel, never in a configuration file.

What python-can can do differs per adapter, and this module says so rather than pretending:
- the sample point is set through a BitTiming for Kvaser (16 MHz clock) and Vector (16 MHz);
- listen-only is Kvaser's silent driver mode and Vector's listen_only;
- IXXAT takes neither through python-can, so both are refused for it with the reason;
- receive filters work everywhere: in the adapter where it can, in python-can otherwise.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, fields

import can

from canexpert.can_bus import LISTEN_ONLY_OPTIONS, channel_key, open_channel

SETTINGS_GROUP = "channel_setup"       # settings: channel_setup/<channel key> -> JSON
TIMING_CLOCKS = {"kvaser": 16_000_000, "vector": 16_000_000}       # the controller clock python-can expects
DETECT_BITRATES = (500_000, 250_000, 125_000, 1_000_000, 800_000, 100_000, 83_333, 50_000, 33_333)
MAX_FILTERS = 32                       # more than this and the ranges should be written wider


@dataclass
class ChannelSetup:
    sample_point: float = 0.0          # percent; 0 leaves the bit timing to the adapter
    sjw: int = 0                       # synchronisation jump width in time quanta; 0 = what the timing gives
    listen_only: bool = False          # receive without acknowledging or sending anything
    filters: str = ""                  # identifiers let through, e.g. "7E8, 300-3FF"; empty = everything

    @classmethod
    def from_dict(cls, values) -> "ChannelSetup":
        names = {item.name for item in fields(cls)}
        return cls(**{name: value for name, value in (values or {}).items() if name in names})

    def describe(self) -> str:
        """A few words for the channel list and the log."""
        parts = []
        if self.sample_point:
            parts.append(f"sample point {self.sample_point:g} %")
        if self.listen_only:
            parts.append("listen-only")
        if self.filters.strip():
            parts.append(f"filter {self.filters.strip()}")
        return ", ".join(parts)


class SetupError(ValueError):
    """A setting the adapter cannot take, with what to tell the user."""


def setup_key(channel_config: dict) -> str:
    return "|".join(str(part) for part in channel_key(channel_config))


def load_setup(settings, channel_config: dict) -> ChannelSetup:
    try:
        values = json.loads(settings.value(f"{SETTINGS_GROUP}/{setup_key(channel_config)}", "", type=str) or "{}")
        return ChannelSetup.from_dict(values)
    except (TypeError, ValueError):
        return ChannelSetup()


def save_setup(settings, channel_config: dict, setup: ChannelSetup):
    settings.setValue(f"{SETTINGS_GROUP}/{setup_key(channel_config)}", json.dumps(asdict(setup)))


# --- bit timing ---------------------------------------------------------------------------------

def bit_timing(interface: str, bitrate: int, setup: ChannelSetup) -> can.BitTiming | None:
    """The BitTiming for a sample point, on the clock the adapter's python-can driver expects;
    None when the setup leaves the timing to the adapter. SetupError when it cannot be done."""
    if not setup.sample_point:
        return None
    clock = TIMING_CLOCKS.get(interface)
    if clock is None:
        if interface == "virtual":
            clock = 16_000_000                   # nothing to set, but the arithmetic can still be shown
        else:
            raise SetupError(f"python-can does not take a sample point for {interface} adapters; "
                             "leave it at the adapter's default")
    try:
        timing = can.BitTiming.from_sample_point(f_clock=clock, bitrate=int(bitrate),
                                                 sample_point=float(setup.sample_point))
        if setup.sjw:
            timing = can.BitTiming(f_clock=clock, brp=timing.brp, tseg1=timing.tseg1, tseg2=timing.tseg2,
                                   sjw=int(setup.sjw))
    except ValueError as exc:
        raise SetupError(f"No bit timing gives {setup.sample_point:g} % at {bitrate} bit/s: {exc}") from None
    return timing


def timing_text(timing: can.BitTiming) -> str:
    return (f"BRP {timing.brp}, TSEG1 {timing.tseg1}, TSEG2 {timing.tseg2}, SJW {timing.sjw} - "
            f"sample point {timing.sample_point:.1f} % on a {timing.f_clock // 1_000_000} MHz clock")


# --- receive filters ------------------------------------------------------------------------------

def _parse_identifier(text: str) -> tuple[int, bool]:
    text = text.strip().lower()
    extended = text.endswith("x")
    value = int(text.rstrip("x").removeprefix("0x"), 16)
    return value, extended or value > 0x7FF


def range_masks(first: int, last: int, extended: bool) -> list[dict]:
    """The python-can filters (identifier and mask) that let exactly first..last through: the range cut
    into aligned power-of-two blocks, the way a network is cut into prefixes."""
    full = 0x1FFFFFFF if extended else 0x7FF
    result, start = [], first
    while start <= last:
        size = start & -start if start else full + 1          # the largest block aligned at start ...
        while start + size - 1 > last:                         # ... that stays inside the range
            size //= 2
        result.append({"can_id": start, "can_mask": full & ~(size - 1), "extended": extended})
        start += size
    return result


def parse_filters(text: str) -> list[dict]:
    """"7E8, 300-3FF, 18DAF100x" -> python-can can_filters. SetupError for text that is not identifiers."""
    filters = []
    for part in (piece.strip() for piece in text.replace(";", ",").split(",")):
        if not part:
            continue
        try:
            if "-" in part:
                (first, extended_a), (last, extended_b) = (_parse_identifier(p) for p in part.split("-", 1))
                if last < first:
                    raise SetupError(f"{part}: the range runs backwards")
                filters += range_masks(first, last, extended_a or extended_b)
            else:
                identifier, extended = _parse_identifier(part)
                filters += range_masks(identifier, identifier, extended)
        except SetupError:
            raise
        except ValueError:
            raise SetupError(f"{part!r} is not a hexadecimal identifier or range, e.g. 7E8 or 300-3FF") from None
    if len(filters) > MAX_FILTERS:
        raise SetupError(f"The filter needs {len(filters)} identifier/mask pairs; write wider ranges "
                         f"(at most {MAX_FILTERS})")
    return filters


def session_filters(setup: ChannelSetup, config: dict | None) -> list[dict] | None:
    """The receive filters for a session: the channel's own, plus the configuration's response
    identifiers, so TesterPresent and diagnostics keep working. None when nothing is filtered."""
    if not setup.filters.strip():
        return None
    filters = parse_filters(setup.filters)
    if config:
        extended = not config.get("identifier_11_bit", True)
        for identifier in {config.get("response_id"), *config.get("response_ids", [])} - {None}:
            filters += range_masks(identifier, identifier, extended)
    return filters


# --- opening a channel -------------------------------------------------------------------------------

class ListenOnlyBus:
    """A bus that receives and refuses to send, so nothing in CAN Expert can transmit by accident
    on a channel opened listen-only - the adapter is in its silent mode as well where it has one."""

    def __init__(self, bus):
        self._bus = bus
        self.listen_only = True

    def send(self, message, timeout=None):
        raise can.CanOperationError("The channel is open listen-only: nothing is sent")

    def __getattr__(self, name):
        return getattr(self._bus, name)


def bus_options(interface: str, bitrate: int, setup: ChannelSetup) -> dict:
    """The python-can keyword arguments the setup adds when opening the channel."""
    options = {}
    timing = bit_timing(interface, bitrate, setup)
    if timing is not None and interface in TIMING_CLOCKS:
        options["timing"] = timing
    if setup.listen_only:
        if interface not in LISTEN_ONLY_OPTIONS:
            raise SetupError(f"python-can cannot open {interface} adapters listen-only")
        options.update(LISTEN_ONLY_OPTIONS[interface])
    return options


def open_configured(channel_config: dict, bitrate, setup: ChannelSetup | None = None, config: dict | None = None):
    """Open a channel as its setup says: bit timing, listen-only, receive filters (with the
    configuration's response identifiers let through)."""
    setup = setup or ChannelSetup()
    options = bus_options(channel_config["interface"], int(bitrate), setup)
    filters = session_filters(setup, config)          # checked before the adapter is touched
    bus = open_channel(channel_config, bitrate, **options)
    if filters:
        bus.set_filters(filters)
    return ListenOnlyBus(bus) if setup.listen_only else bus


def detect_bitrate(channel_config: dict, candidates=DETECT_BITRATES, listen_time=0.4, cancelled=None):
    """Find a bus's bit rate by listening at each candidate: the first one that carries frames and no
    error frames. Listen-only throughout, so a wrong guess never puts an error frame on the bus.

    Returns (bitrate or None, [(bitrate, frames, error frames, problem), ...])."""
    interface = channel_config["interface"]
    if interface not in LISTEN_ONLY_OPTIONS:
        raise SetupError(f"Finding the bit rate needs listen-only, which python-can does not offer for "
                         f"{interface} adapters")
    report = []
    for bitrate in candidates:
        if cancelled and cancelled():
            break
        try:
            bus = open_channel(channel_config, bitrate, **LISTEN_ONLY_OPTIONS[interface])
        except Exception as exc:                  # the adapter refuses this bit rate
            report.append((bitrate, 0, 0, str(exc)))
            continue
        frames = errors = 0
        try:
            deadline = time.monotonic() + listen_time
            while time.monotonic() < deadline and frames < 5:
                message = bus.recv(timeout=0.05)
                if message is None:
                    continue
                if message.is_error_frame:
                    errors += 1
                else:
                    frames += 1
        finally:
            bus.shutdown()
        report.append((bitrate, frames, errors, ""))
        if frames >= 2 and not errors:
            return bitrate, report
    return None, report
