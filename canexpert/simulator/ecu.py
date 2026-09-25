#!/usr/bin/env python3
"""
Dummy UDS ECU (ISO 14229-1 over ISO 15765-2) for trying CAN Expert without a vehicle.

Kvaser's Virtual CAN Driver provides two connected channels. Run the ECU on one and
connect CAN Expert to the other:

    python dummy_ecu.py                           (the Dummy ECU window: settings, Connect, log)
    python dummy_ecu.py --console --channel 1     (no window)
    (CAN Expert: receiver "[kvaser] Ch 0", configuration SERVER ID 7E0, ECU ID 7E8)

What it simulates (each value is a setting in the window, or in a profile saved from it):
- Sessions (0x10, announcing P2/P2*), TesterPresent (0x3E), ECUReset (0x11), S3 session timeout,
  a processing delay answered with response pending (NRC 0x78) beyond P2
- ReadDataByIdentifier (0x22) / WriteDataByIdentifier (0x2E) on a table of DIDs, each writable or not,
  each readable in some sessions only or after unlocking a security level, each with the values it accepts
  (others written get NRC 0x31); a DID can follow a signal
- ReadDataByPeriodicIdentifier (0x2A): F2xx DIDs sent slow, medium or fast, as 6A frames on the response
  ID or as frames of their own ID; ResponseOnEvent (0x86) on a DID change or a DTC status change
- InputOutputControlByIdentifier (0x2F): a DID that follows a signal takes it over, and the application
  frames carry what the tester set
- ReadMemoryByAddress (0x23) / WriteMemoryByAddress (0x3D) on the ECU's memory
- SecurityAccess (0x27: several levels, each with its seed length and key - seed XOR mask, or a seed & key
  DLL - an attempt counter and lockout delay); rules requiring a session or a level for any service
- ControlDTCSetting (0x85), CommunicationControl (0x28), ClearDTC (0x14), and ReadDTCInformation (0x19:
  count, by status mask, snapshot record, extended data record, supported DTCs) on a table of DTCs
  whose status bits follow faults switched on and off, through operation cycles (dtc.py)
- A forced negative response per service, and transport errors on purpose: refusals, missing answers,
  answers on another ID, a consecutive frame dropped, out of sequence or late
- RoutineControl (0x31): start, stop and results; a self test in the extended session that runs for a while
  and can be stopped
- Flashing: RoutineControl erase / checkProgrammingDependencies (0x31, optionally checking the image's
  CRC-32), RequestDownload (0x34), RequestUpload (0x35), TransferData (0x36), RequestTransferExit (0x37),
  with the accepted data and address/length formats, maxNumberOfBlockLength, full blocks and memory
  ranges; an application left invalid by a failed flash keeps the ECU in its bootloader after a reset
- ISO-TP flow control on segmented requests: block size and STmin, optional WAIT frames, overflow for
  requests longer than the receive buffer
- Application frames: the messages of a DBC, each signal driven by a generator (signals.py) - by default
  0x300 temperature/pressure and 0x301 status; commands on 0x200/0x201
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import socket
import sys
import threading
import time
import zlib
from dataclasses import asdict, dataclass, field, fields, replace
from typing import NamedTuple

import can

from canexpert.simulator.dtc import DtcMemory
from canexpert.simulator.j1939_node import DEFAULT_NAME, J1939Node
from canexpert.simulator.signals import DEFAULT_GENERATORS, J1939_DEMO_GENERATORS, SignalSimulation, check_generators
from canexpert.uds.isotp import (FC_OVERFLOW, FC_WAIT, N_CR_TIMEOUT, IsoTpError, flow_control_frame, isotp_send,
                                 parse_first_frame)
from canexpert.uds.seed_key import SeedKeyError, generate_key, load_library

DEFAULT_SESSION, PROGRAMMING_SESSION, EXTENDED_SESSION = 0x01, 0x02, 0x03
SESSION_NAMES = {DEFAULT_SESSION: "default", PROGRAMMING_SESSION: "programming", EXTENDED_SESSION: "extended"}
# Negative responses an ECU must not send to functionally addressed requests (ISO 14229-1 7.5).
SUPPRESSED_FUNCTIONAL_NRCS = {0x11, 0x12, 0x31, 0x7E, 0x7F}
SERVICE_NAMES = {
    0x10: "DiagnosticSessionControl", 0x11: "ECUReset", 0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation", 0x22: "ReadDataByIdentifier", 0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess", 0x28: "CommunicationControl", 0x2A: "ReadDataByPeriodicIdentifier",
    0x2E: "WriteDataByIdentifier", 0x2F: "InputOutputControlByIdentifier", 0x31: "RoutineControl",
    0x34: "RequestDownload", 0x35: "RequestUpload", 0x36: "TransferData", 0x37: "RequestTransferExit",
    0x3D: "WriteMemoryByAddress", 0x3E: "TesterPresent", 0x85: "ControlDTCSetting", 0x86: "ResponseOnEvent",
}
# The sessions each service may be used in (NRC 0x7F in the others); a service not listed: in every session.
SERVICE_SESSIONS = {0x27: (EXTENDED_SESSION, PROGRAMMING_SESSION), 0x28: (EXTENDED_SESSION, PROGRAMMING_SESSION),
                    0x2A: (DEFAULT_SESSION, EXTENDED_SESSION), 0x2E: (EXTENDED_SESSION, PROGRAMMING_SESSION),
                    0x2F: (EXTENDED_SESSION,), 0x31: (EXTENDED_SESSION, PROGRAMMING_SESSION),
                    0x34: (PROGRAMMING_SESSION,),
                    0x35: (PROGRAMMING_SESSION, EXTENDED_SESSION), 0x3D: (EXTENDED_SESSION, PROGRAMMING_SESSION),
                    0x85: (EXTENDED_SESSION, PROGRAMMING_SESSION), 0x86: (DEFAULT_SESSION, EXTENDED_SESSION)}
# Sub-functions each service has (NRC 0x12 for the others).
SUB_FUNCTIONS = {0x10: (0x01, 0x02, 0x03), 0x11: (0x01, 0x03), 0x19: (0x01, 0x02, 0x04, 0x06, 0x0A),
                 0x28: (0x00, 0x01, 0x02, 0x03), 0x31: (0x01, 0x02, 0x03), 0x3E: (0x00,), 0x85: (0x01, 0x02),
                 0x86: (0x00, 0x01, 0x03, 0x04, 0x05, 0x06)}
# What a bootloader answers; the application's services get NRC 0x11 while it runs.
BOOT_SERVICES = {0x10, 0x11, 0x22, 0x23, 0x27, 0x28, 0x2E, 0x31, 0x34, 0x35, 0x36, 0x37, 0x3D, 0x3E, 0x85}
BOOT_VERSION = b"BOOTLOADER"
DEFAULT_CONNECTION = {"interface": "kvaser", "channel": "1", "bitrate": 500000}
# The ECU's data at power-on; the window edits them and a profile keeps them. Bytes are written as hex.
# A DID with a signal answers that signal's raw value, in as many bytes as its data has.
DEFAULT_DIDS = (
    {"did": 0xF187, "data": b"CANEXPERT-DUMMY".hex(), "writable": False},       # spare part number
    {"did": 0xF18C, "data": b"SN000123456".hex(), "writable": False},           # ECU serial number
    {"did": 0xF190, "data": b"WVWZZZ1KZAW000001".hex(), "writable": True},      # VIN, 17 bytes
    {"did": 0xF195, "data": b"APP-1.0.0".hex(), "writable": False},             # software version
    {"did": 0x0101, "data": "00d7", "writable": False, "signal": "EngineData.Temperature"},   # 0.1 degC
    {"did": 0x0102, "data": "0064", "writable": False, "signal": "EngineData.Pressure"},      # 0.01 bar
    {"did": 0xF201, "data": "00d7", "writable": False, "signal": "EngineData.Temperature"},   # periodic (2A 01)
    {"did": 0xF202, "data": "0064", "writable": False, "signal": "EngineData.Pressure"},      # periodic (2A 02)
    {"did": 0x0200, "data": b"CAL-0042".hex(), "writable": False, "sessions": [EXTENDED_SESSION], "level": 0x01},
    {"did": 0x0110, "data": "0320", "writable": True, "valid": [[0x0258, 0x04B0]]},   # idle speed, 600-1200 rpm
)
# snapshot: what follows the record number in 59 04 (number of identifiers, then DID and data - here
# one identifier, F40D vehicle speed, 50 km/h); extended: what follows it in 59 06 (occurrence counter).
DEFAULT_DTCS = (
    {"dtc": 0x010100, "status": 0x09, "snapshot": "01f40d32", "extended": "05"},   # P0101
    {"dtc": 0xC10000, "status": 0x08, "snapshot": "", "extended": ""},             # U0100
)
IMAGE_CHECKS = ("off", "option", "trailer")
# RoutineControl results: the routine is running, has run to its end, or was stopped.
ROUTINE_RUNNING, ROUTINE_DONE, ROUTINE_STOPPED = 0x01, 0x00, 0x02
PERIODIC_MODES = {0x01: "slow", 0x02: "medium", 0x03: "fast"}
STOP_SENDING = 0x04
MAX_PERIODIC = 16                     # periodic identifiers scheduled at once
MAX_EVENTS = 8                        # ResponseOnEvent events set up at once
EVENT_CHECK_INTERVAL = 0.1            # how often DIDs are compared for onChangeOfDataIdentifier
STALL_SECONDS = N_CR_TIMEOUT + 0.2    # a consecutive frame held back past the tester's N_Cr
ERROR_KINDS = ("error_refuse", "error_no_answer", "error_wrong_id", "error_drop_frame", "error_wrong_sequence",
               "error_stall")
# Services whose sub-function byte carries suppressPosRspMsgIndicationBit.
SUPPRESSIBLE = {0x10, 0x11, 0x19, 0x27, 0x28, 0x31, 0x3E, 0x85, 0x86}


class NegativeResponse(Exception):
    def __init__(self, nrc: int):
        super().__init__(f"NRC 0x{nrc:02X}")
        self.nrc = nrc


@dataclass
class EcuConfig:
    """How the ECU answers. The running ECU reads it for every frame, so changes apply at once."""
    # Addressing
    request_id: int = 0x7E0            # physical requests (tester -> ECU)
    functional_id: int = 0x7DF         # functional requests (tester -> every ECU)
    response_id: int = 0x7E8           # this ECU's responses (ECU -> tester)
    extended_ids: bool = False         # 29-bit identifiers
    address_byte: int | None = None    # ISO-TP extended addressing byte
    padding: int | None = 0xAA         # fills frames up to 8 bytes (None: frames as short as their content)
    # ISO-TP flow control on segmented requests
    block_size: int = 8                # BS: consecutive frames per block (0 = no limit)
    st_min: int = 0x01                 # STmin byte: 0x00-0x7F ms, 0xF1-0xF9 100-900 us
    flow_waits: int = 0                # WAIT frames sent before each ContinueToSend
    wait_interval_ms: int = 100        # time between those frames (the tester waits up to N_Bs, 1 s)
    rx_buffer: int = 0                 # longest request accepted, 0 = maxNumberOfBlockLength; longer: overflow
    # UDS timing and security
    p2_ms: int = 50                    # P2server_max, announced in the DiagnosticSessionControl response
    p2_star_ms: int = 5000             # P2*server_max, likewise
    response_delay_ms: int = 0         # processing time per request; beyond P2 the ECU sends NRC 0x78 first
    pending_interval: float = 2.0      # seconds between NRC 0x78 while busy (within P2*)
    s3_timeout: float = 5.0            # back to the default session after this long without requests
    programming_needs_extended: bool = True
    security_level: int = 0x01         # requestSeed sub-function (odd); sendKey is the next one
    seed_length: int = 4
    key_mask: int = 0xA5               # key = seed XOR key_mask, byte by byte
    key_dll: str = ""                  # ... or the key a seed & key DLL (GenerateKeyEx) computes, when set
    key_variant: str = ""
    security_levels: list = field(default_factory=list)   # more levels, each like the one above:
    # [{"level": 0x03, "seed_length": 4, "key_mask": 0x5A, "dll": "", "variant": ""}]
    max_attempts: int = 3              # wrong keys before the lockout delay
    lockout_seconds: float = 10.0
    service_rules: list = field(default_factory=list)     # [{"sid": 0x2F, "sessions": [3], "level": 1}]:
    # outside those sessions the service gets NRC 0x7F, without that level unlocked NRC 0x33
    # Periodic data (0x2A)
    periodic_rates_ms: tuple = (1000, 200, 50)            # slow, medium, fast
    periodic_id: int | None = None     # None: 6A frames on the response ID; else frames of this ID (no PCI)
    # Flashing
    data_formats: tuple = (0x00,)      # dataFormatIdentifier values accepted (0x00: no compression/encryption)
    address_format: int | None = None  # required addressAndLengthFormatIdentifier (None: any)
    max_block_length: int = 0x402      # maxNumberOfBlockLength: TransferData length including SID and counter
    block_length_bytes: int = 2        # bytes used for maxNumberOfBlockLength in the response
    full_blocks: bool = False          # every TransferData but the last must carry max_block_length - 2 bytes
    memory_ranges: tuple = ()          # ((first, last), ...) addresses open to erase/download/upload; () = any
    require_erase: bool = True
    erase_routine: int = 0xFF00
    check_routine: int = 0xFF01
    self_test_routine: int = 0x0201    # runs self_test_seconds in the extended session; stop and results
    self_test_seconds: float = 2.0
    erase_seconds: float = 1.0
    allow_upload: bool = True          # RequestUpload (0x35) reads the memory back
    image_crc: str = "off"             # the check routine: "off"; "option": the CRC-32 of the image is its
    # option record (31 01 FF01 xx xx xx xx); "trailer": the image's last four bytes are the CRC-32 of the rest
    version_address: int | None = None  # after a good check the software version (F195) is read from here
    version_length: int = 16
    # Application traffic and output
    broadcast_interval: float = 0.1    # period of the messages without their own; 0 = no application frames
    dbc_path: str = ""                 # the messages the ECU sends; "" = the built-in DBC/dummy_ecu.dbc
    messages: list = field(default_factory=list)          # [{"message": "EngineData", "on": True, "cycle_ms": 0}]
    generators: list = field(default_factory=lambda: [dict(item) for item in DEFAULT_GENERATORS])
    dump_path: str | None = None       # the flashed image is written here as S-records
    # J1939 (j1939_node.py): address claim, DM1, answers to requests, its address in 29-bit application frames
    j1939: bool = False
    j1939_address: int = 0x00          # the source address it claims (0x00: engine #1)
    j1939_name: int = DEFAULT_NAME     # the 64-bit NAME it claims it with
    # Data (restored at power-on and on ECUReset of the tables, not of what WriteDataByIdentifier wrote)
    dids: list = field(default_factory=lambda: [dict(item) for item in DEFAULT_DIDS])
    dtcs: list = field(default_factory=lambda: [dict(item) for item in DEFAULT_DTCS])
    forced_nrcs: list = field(default_factory=list)   # [{"sid": 0x22, "nrc": 0x22}]: that service always refused
    # The fault memory's life cycle
    confirm_cycles: int = 2            # operation cycles with a failure before a DTC is confirmed
    aging_cycles: int = 3              # passed cycles before a confirmed DTC ages out
    operation_cycle_seconds: float = 0.0   # an operation cycle ends this often; 0: on ECUReset and the button
    snapshot_dids: tuple = (0x0101, 0x0102)  # captured in the snapshot record when a fault appears
    # Transport errors on purpose: the chance, in percent, that a response gets each one
    error_refuse: int = 0              # a negative response instead (error_refuse_nrc)
    error_refuse_nrc: int = 0x21       # busyRepeatRequest
    error_no_answer: int = 0           # no response at all
    error_wrong_id: int = 0            # the response on response_id + 1
    error_drop_frame: int = 0          # one consecutive frame of a long response not sent
    error_wrong_sequence: int = 0      # one consecutive frame with the wrong sequence number
    error_stall: int = 0               # one consecutive frame late, past the tester's N_Cr
    errors_on_tester_present: bool = False

    @property
    def receive_buffer(self) -> int:
        return self.rx_buffer or self.max_block_length


class DataTables(NamedTuple):
    """The data tables of a configuration, checked and in the shape the ECU answers from."""
    dids: dict        # DID -> bytes (for a DID that follows a signal: its length, and its value without one)
    writable: set
    access: dict      # DID -> (signal "Message.Signal" or "", sessions it is read in (empty: any), level or 0)
    valid: dict       # DID -> [(low, high)]: the values (its data as a number) it may be written with
    statuses: dict    # DTC -> status byte at power-on
    records: dict     # DTC -> (snapshot record 01, extended data record 01)
    forced: dict      # SID -> forced NRC


def _sessions(value, what: str) -> tuple:
    sessions = tuple(int(session) for session in (value or ()))
    if any(not 0 < session <= 0x7F for session in sessions):
        raise ValueError(f"{what}: sessions are 01-7F")
    return sessions


def data_tables(config: EcuConfig) -> DataTables:
    """The tables of a configuration; ValueError naming the entry that is wrong."""
    dids, writable, access, valid = {}, set(), {}, {}
    for item in config.dids:
        try:
            did, data = int(item["did"]), bytes.fromhex(str(item.get("data", "")))
            level = int(item.get("level", 0) or 0)
            ranges = [(int(low), int(high)) for low, high in item.get("valid", ()) or ()]
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"DID entry {item}: needs a DID and its data as hex bytes") from None
        if not 0 <= did <= 0xFFFF or not data:
            raise ValueError(f"DID {did:04X}: a DID is 0000-FFFF and has at least one byte of data")
        if not 0 <= level <= 0x7F:
            raise ValueError(f"DID {did:04X}: the security level is 01-7F, or none")
        if any(low > high for low, high in ranges):
            raise ValueError(f"DID {did:04X}: a range of valid values ends before it starts")
        dids[did] = data
        if ranges:
            valid[did] = ranges
        if item.get("writable"):
            writable.add(did)
        access[did] = (str(item.get("signal", "") or ""), _sessions(item.get("sessions"), f"DID {did:04X}"), level)
    statuses, records = {}, {}
    for item in config.dtcs:
        try:
            dtc, status = int(item["dtc"]), int(item.get("status", 0))
            snapshot = bytes.fromhex(str(item.get("snapshot", "")))
            extended = bytes.fromhex(str(item.get("extended", "")))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"DTC entry {item}: needs a DTC, a status, and hex bytes for its records") from None
        if not 0 <= dtc <= 0xFFFFFF or not 0 <= status <= 0xFF:
            raise ValueError(f"DTC {dtc:06X}: a DTC is three bytes and its status one")
        statuses[dtc], records[dtc] = status, (snapshot, extended)
    forced = {}
    for item in config.forced_nrcs:
        try:
            sid, nrc = int(item["sid"]), int(item["nrc"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Forced NRC entry {item}: needs a service and an NRC") from None
        if not 0 <= sid <= 0xFF or not 0 <= nrc <= 0xFF:
            raise ValueError(f"Forced NRC {sid:X}/{nrc:X}: service and NRC are one byte each")
        forced[sid] = nrc
    return DataTables(dids, writable, access, valid, statuses, records, forced)


def security_levels(config: EcuConfig) -> dict:
    """requestSeed level -> {"seed_length", "key_mask", "dll", "variant"}: the main level and the others."""
    levels = {config.security_level: {"seed_length": config.seed_length, "key_mask": config.key_mask,
                                      "dll": config.key_dll, "variant": config.key_variant}}
    for item in config.security_levels:
        levels.setdefault(int(item["level"]), {"seed_length": int(item.get("seed_length", 4)),
                                               "key_mask": int(item.get("key_mask", 0)),
                                               "dll": str(item.get("dll", "") or ""),
                                               "variant": str(item.get("variant", "") or "")})
    return levels


def service_rules(config: EcuConfig) -> dict:
    """SID -> (sessions it is allowed in (empty: any), security level it needs (0: none))."""
    return {int(item["sid"]): (_sessions(item.get("sessions"), f"Service {int(item['sid']):02X}"),
                               int(item.get("level", 0) or 0))
            for item in config.service_rules}


def check_config(config: EcuConfig) -> None:
    """ValueError naming what is wrong in a configuration."""
    data_tables(config)
    check_generators(config.generators)
    for item in config.security_levels:
        try:
            level, length, mask = int(item["level"]), int(item.get("seed_length", 4)), int(item.get("key_mask", 0))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Security level {item}: needs a level, a seed length and a key mask") from None
        if not 1 <= level <= 0x7F or not level % 2 or not 1 <= length <= 64 or not 0 <= mask <= 0xFF:
            raise ValueError(f"Security level {level:02X}: an odd level 01-7F, a seed of 1-64 bytes, a mask 00-FF")
    try:
        rules = service_rules(config)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Service rule: {exc}") from None
    for sid, (_sessions_allowed, level) in rules.items():
        if not 0 <= sid <= 0xFF or not 0 <= level <= 0x7F:
            raise ValueError(f"Service rule {sid:02X}: the service is one byte, the level 01-7F or none")
    if len(config.periodic_rates_ms) != 3 or any(int(rate) <= 0 for rate in config.periodic_rates_ms):
        raise ValueError("Periodic rates: three periods in ms (slow, medium, fast), each above 0")
    if config.image_crc not in IMAGE_CHECKS:
        raise ValueError(f"Image check: one of {', '.join(IMAGE_CHECKS)}")
    for name in ERROR_KINDS:
        if not 0 <= int(getattr(config, name)) <= 100:
            raise ValueError(f"{name}: a percentage, 0-100")
    if not 0 <= int(config.j1939_address) <= 0xFD:
        raise ValueError("J1939 address: 00-FD (FE is the null address, FF everyone)")
    if not 0 <= int(config.j1939_name) < 1 << 64:
        raise ValueError("J1939 NAME: 64 bits")


def config_from_dict(values: dict) -> EcuConfig:
    """EcuConfig from saved values; unknown names are ignored, missing ones keep their default."""
    names = {item.name for item in fields(EcuConfig)}
    kwargs = {name: value for name, value in values.items() if name in names}
    for name in ("data_formats", "periodic_rates_ms", "snapshot_dids"):
        if name in kwargs:
            kwargs[name] = tuple(kwargs[name])
    if "memory_ranges" in kwargs:
        kwargs["memory_ranges"] = tuple(tuple(pair) for pair in kwargs["memory_ranges"])
    for name in ("dids", "dtcs", "forced_nrcs", "generators", "messages", "security_levels", "service_rules"):
        if name in kwargs:
            kwargs[name] = [dict(item) for item in kwargs[name]]
    config = EcuConfig(**kwargs)
    check_config(config)                  # a profile with a broken table is refused, not half-loaded
    return config


def load_profile(path) -> tuple[EcuConfig, dict]:
    """(settings, connection) from a profile saved by the window."""
    with open(path, encoding="utf-8") as handle:
        values = json.load(handle)
    return config_from_dict(values.get("ecu", {})), dict(values.get("connection", {}))


def save_profile(path, config: EcuConfig, connection: dict):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"connection": connection, "ecu": asdict(config)}, handle, indent=2)


def parse_channel(text):
    """Kvaser and most interfaces number their channels; virtual buses use names."""
    text = str(text).strip()
    return int(text) if text.isdigit() else text


@dataclass
class EcuState:
    session: int = DEFAULT_SESSION
    unlocked_levels: set = field(default_factory=set)    # requestSeed levels unlocked in this session
    seed: tuple | None = None                            # (level, seed) of the last requestSeed
    failed_attempts: int = 0
    locked_until: float = 0.0
    dtc_setting_on: bool = True
    communication_enabled: bool = True
    erased: list = field(default_factory=list)          # [(start, end)], end exclusive
    transfer: dict | None = None                         # the download or upload in progress
    memory: dict = field(default_factory=dict)            # address -> bytes of each completed download
    last_request: float = field(default_factory=time.monotonic)
    rebooting_until: float = 0.0
    application_valid: bool = True                       # False from an erase until a good dependency check
    bootloader: bool = False                             # started without a valid application
    periodic: dict = field(default_factory=dict)          # periodic identifier -> {"mode", "next"}
    events: list = field(default_factory=list)            # ResponseOnEvent: {"type", "window", "record", "service", "last"}
    events_active: bool = False
    io_controls: dict = field(default_factory=dict)       # DID -> the control parameter in force
    routines: dict = field(default_factory=dict)          # RID -> {"status", "until"}: what was started

    @property
    def unlocked(self) -> bool:
        return bool(self.unlocked_levels)


class _FrameTap:
    """The ECU's view of its bus: diagnostic frames also go to DummyEcu.trace when it is set."""

    def __init__(self, ecu):
        self.ecu = ecu

    def send(self, message, timeout=None):
        self.ecu.bus.send(message, timeout)
        self.ecu._trace("Tx", message)

    def recv(self, timeout=None):
        message = self.ecu.bus.recv(timeout)
        if message is not None:
            self.ecu._trace("Rx", message)
        return message


class _Mistaken:
    """The ECU's bus for one response it gets wrong on purpose: on another ID, and/or with one of its
    consecutive frames (number target) dropped, out of sequence or held back past N_Cr."""

    def __init__(self, io, offset: int, wrong_id: int | None, mode: str | None, target: int, log):
        self.io, self.offset, self.wrong_id, self.mode, self.target, self.log = io, offset, wrong_id, mode, target, log
        self.consecutive = 0

    def send(self, message, timeout=None):
        data = bytearray(message.data)
        if self.mode and len(data) > self.offset and data[self.offset] >> 4 == 0x2:
            self.consecutive += 1
            if self.consecutive == self.target:
                if self.mode == "drop":
                    self.log(f"Error on purpose: consecutive frame {self.target} not sent")
                    return
                if self.mode == "sequence":
                    data[self.offset] = 0x20 | ((data[self.offset] + 1) & 0x0F)
                    self.log(f"Error on purpose: consecutive frame {self.target} sent out of sequence")
                elif self.mode == "stall":
                    self.log(f"Error on purpose: consecutive frame {self.target} held back {STALL_SECONDS:.1f} s")
                    time.sleep(STALL_SECONDS)
        arbitration_id = message.arbitration_id if self.wrong_id is None else self.wrong_id
        self.io.send(can.Message(arbitration_id=arbitration_id, data=bytes(data),
                                 is_extended_id=message.is_extended_id), timeout)

    def recv(self, timeout=None):
        return self.io.recv(timeout)


class DummyEcu:
    def __init__(self, bus: can.BusABC | None, config: EcuConfig | None = None, log=print):
        self.bus = bus
        self.config = config or EcuConfig()
        self.log = log
        self.trace = None       # trace(direction, message) for every diagnostic frame, when set
        self._io = _FrameTap(self)
        self._started = time.monotonic()
        self._random = random.Random()
        self._libraries = {}    # seed & key DLLs, loaded once
        self.j1939 = J1939Node(self)
        self.power_on()

    def power_on(self):
        """Factory state: default session, the DIDs and DTCs of the configuration, erased memory, a valid
        application, and the signals starting afresh."""
        self.state = EcuState()
        self.running = False
        self.logging = False
        self._rx = None
        self._cycle_started = self._next_event_check = time.monotonic()
        self.signals = SignalSimulation(self._inputs)
        self.load_signals()
        self.load_data()
        self.j1939.reset()

    def _inputs(self) -> dict:
        return {"running": self.running, "logging": self.logging, "session": self.state.session}

    def load_data(self):
        """Take the DID and DTC tables of the configuration, as at power-on (the window calls it when they
        are edited). What WriteDataByIdentifier wrote, ClearDTC cleared and the faults did is forgotten."""
        tables = data_tables(self.config)
        self.dids, self.writable, self.did_access = tables.dids, tables.writable, tables.access
        self.did_valid = tables.valid
        self.dtc_memory = DtcMemory(tables.statuses, tables.records, self.config.confirm_cycles,
                                    self.config.aging_cycles, capture=self._capture_snapshot)
        self.dtc_memory.set_frozen(not self.state.dtc_setting_on)
        self.dtcs, self.dtc_records = self.dtc_memory.statuses, self.dtc_memory.records

    def load_signals(self) -> str:
        """Take the application traffic settings: the DBC (read again only when its path changed) and the
        generators. Returns "" or why the DBC could not be read (the one in use then stays)."""
        error = ""
        if self.signals.source != self.config.dbc_path:
            try:
                self.signals.load(self.config.dbc_path)
            except ValueError as exc:
                error = str(exc)
                self.log(f"{error}; the application frames stay as they were")
                if self.signals.source is None:
                    self.signals.load("")
        # The J1939 demo DBC's signals move unless the settings say otherwise (settings from before it had none).
        listed = {str(item.get("signal")) for item in self.config.generators}
        demo = [dict(item) for item in J1939_DEMO_GENERATORS if item["signal"] not in listed]
        try:
            self.signals.configure(list(self.config.generators) + demo, self.config.messages)
        except ValueError as exc:
            error = error or str(exc)
            self.log(f"Generators: {exc}")
        return error

    def refresh(self, data: bool = False) -> str:
        """Take settings the window changed: the application traffic and the fault memory's cycles, and
        with data=True the DID and DTC tables (as at power-on). Returns "" or what could not be taken."""
        if data:
            self.load_data()
        self.dtc_memory.confirm_cycles = max(1, int(self.config.confirm_cycles))
        self.dtc_memory.aging_cycles = max(1, int(self.config.aging_cycles))
        return self.load_signals()

    # --- transport ---------------------------------------------------------------

    def _trace(self, direction, message):
        config = self.config
        if self.trace and message.arbitration_id in (config.request_id, config.functional_id, config.response_id):
            self.trace(direction, message)

    def _payload(self, message: can.Message) -> bytes | None:
        data = bytes(message.data)
        if self.config.address_byte is not None:
            if not data or data[0] != self.config.address_byte:
                return None
            data = data[1:]
        return data or None

    def _send_frame(self, body: bytes, can_id: int | None = None):
        data = bytes(body)
        if self.config.address_byte is not None:
            data = bytes([self.config.address_byte]) + data
        if self.config.padding is not None:
            data = data.ljust(8, bytes([self.config.padding]))
        self._io.send(can.Message(arbitration_id=self.config.response_id if can_id is None else can_id, data=data,
                                  is_extended_id=self.config.extended_ids))

    def _chance(self, percent) -> bool:
        return percent > 0 and self._random.random() * 100 < percent

    def _mistaken_io(self, length: int):
        """The bus for a response of length bytes, with the transport errors the settings roll for it;
        None when it goes out right."""
        config = self.config
        offset = int(config.address_byte is not None)
        wrong_id = config.response_id + 1 if self._chance(config.error_wrong_id) else None
        mode = target = None
        room = 7 - offset
        if length > room:                                    # segmented: consecutive frames to get wrong
            frames = max(1, math.ceil((length - (room - 1)) / room))
            for name, kind in (("error_drop_frame", "drop"), ("error_wrong_sequence", "sequence"),
                               ("error_stall", "stall")):
                if self._chance(getattr(config, name)):
                    mode, target = kind, self._random.randint(1, frames)
                    break
        if wrong_id is None and mode is None:
            return None
        if wrong_id is not None:
            self.log(f"Error on purpose: response sent on 0x{wrong_id:X} instead of 0x{config.response_id:X}")
        return _Mistaken(self._io, offset, wrong_id, mode, target or 0, self.log)

    def respond(self, payload: bytes, errors: bool = False):
        """Send a response; with errors=True it may get the transport errors of the Errors settings."""
        io = (self._mistaken_io(len(payload)) if errors else None) or self._io
        try:
            isotp_send(io, self.config.response_id, payload, self.config.request_id,
                       self.config.extended_ids, self.config.address_byte, self.config.padding)
        except IsoTpError as exc:
            if io is self._io:
                raise
            self.log(f"The tester did not take the response that went wrong on purpose: {exc}")

    def on_message(self, message: can.Message):
        if message.is_error_frame or message.is_remote_frame:
            return
        diagnostic = message.arbitration_id in (self.config.request_id, self.config.functional_id)
        if not diagnostic or bool(message.is_extended_id) != self.config.extended_ids:
            if message.is_extended_id and self.j1939.on_frame(message):
                return
            self._application_frame(message)
            return
        functional = message.arbitration_id == self.config.functional_id
        data = self._payload(message)
        if data is None:
            return
        kind = data[0] >> 4
        if kind == 0x0:
            length = data[0] & 0x0F
            if 0 < length <= len(data) - 1:
                self._handle(data[1:1 + length], functional)
        elif kind == 0x1 and not functional:  # segmented requests are physical only
            first = parse_first_frame(data, 7 - (self.config.address_byte is not None))
            if first is None:
                return
            self._rx = None
            if first[0] > self.config.receive_buffer:
                self.log(f"ISO-TP: {first[0]}-byte request exceeds the {self.config.receive_buffer}-byte "
                         f"receive buffer; flow control overflow")
                self._send_frame(flow_control_frame(status=FC_OVERFLOW))
                return
            self._rx = {"total": first[0], "data": bytearray(first[1]), "next": 1, "left": self.config.block_size}
            self._continue_to_send()
            self._rx["deadline"] = time.monotonic() + N_CR_TIMEOUT
        elif kind == 0x2 and not functional and self._rx:
            if time.monotonic() > self._rx["deadline"]:
                self.log("ISO-TP: consecutive frame too late (N_Cr); message dropped")
                self._rx = None
                return
            if data[0] & 0x0F != self._rx["next"]:
                self.log("ISO-TP: consecutive frame out of sequence; message dropped")
                self._rx = None
                return
            self._rx["data"] += data[1:]
            if len(self._rx["data"]) >= self._rx["total"]:
                request = bytes(self._rx["data"][:self._rx["total"]])
                self._rx = None
                self._handle(request, False)
                return
            self._rx["next"] = (self._rx["next"] + 1) & 0x0F
            self._rx["left"] -= 1
            if self._rx["left"] == 0:  # block complete: the tester waits for the next flow control
                self._rx["left"] = self.config.block_size
                self._continue_to_send()
            self._rx["deadline"] = time.monotonic() + N_CR_TIMEOUT

    def _continue_to_send(self):
        """Flow control for the next block, after the configured WAIT frames."""
        for _ in range(self.config.flow_waits):
            self._send_frame(flow_control_frame(status=FC_WAIT))
            time.sleep(self.config.wait_interval_ms / 1000)
        self._send_frame(flow_control_frame(self.config.block_size, self.config.st_min))

    # --- UDS ---------------------------------------------------------------------

    def _check_service(self, sid: int):
        """What stands between a request and its service: a forced NRC, the bootloader, a service the ECU
        does not have, and the session and security rules of the Access settings."""
        forced = next((int(item["nrc"]) for item in self.config.forced_nrcs if int(item["sid"]) == sid), None)
        if forced is not None:                       # the user asked for this service to be refused
            raise NegativeResponse(forced)
        upload_off = sid == 0x35 and not self.config.allow_upload
        if getattr(self, f"_service_{sid:02x}", None) is None or upload_off:
            raise NegativeResponse(0x11)
        if self.state.bootloader and sid not in BOOT_SERVICES:
            raise NegativeResponse(0x11)
        if sid in SERVICE_SESSIONS and self.state.session not in SERVICE_SESSIONS[sid]:
            raise NegativeResponse(0x7F)             # before the request's length or sub-function
        rule = service_rules(self.config).get(sid)
        if rule:
            sessions, level = rule
            if sessions and self.state.session not in sessions:
                raise NegativeResponse(0x7F)
            if level and level not in self.state.unlocked_levels:
                raise NegativeResponse(0x33)

    def _handle(self, request: bytes, functional: bool):
        now = time.monotonic()
        if now < self.state.rebooting_until:
            return
        self.check_session_timeout(now)
        self.state.last_request = now
        sid = request[0]
        name = SERVICE_NAMES.get(sid, f"service 0x{sid:02X}")
        shown = f"{request[:8].hex(' ')}{'...' if len(request) > 8 else ''}"
        errors = sid != 0x3E or self.config.errors_on_tester_present
        try:
            self._check_service(sid)
            if errors and self._chance(self.config.error_refuse):
                self.log(f"<- {name} {shown}  => NRC 0x{self.config.error_refuse_nrc:02X} (error on purpose)")
                self.respond(bytes([0x7F, sid, self.config.error_refuse_nrc]), errors=True)
                return
            delay = self.config.response_delay_ms / 1000
            if delay > self.config.p2_ms / 1000 and sid in SUPPRESSIBLE and len(request) > 1 and request[1] & 0x80:
                # ISO 14229-1: a response pending lifts suppressPosRspMsgIndicationBit - the answer follows it
                request = bytes([sid, request[1] & 0x7F]) + request[2:]
            self._busy(delay, sid)
            reply = getattr(self, f"_service_{sid:02x}")(request)
        except NegativeResponse as exc:
            self.log(f"<- {name} {shown}  => NRC 0x{exc.nrc:02X}")
            if not (functional and exc.nrc in SUPPRESSED_FUNCTIONAL_NRCS):
                self.respond(bytes([0x7F, sid, exc.nrc]), errors=errors)
            return
        suppressed = reply is None
        if not suppressed and errors and self._chance(self.config.error_no_answer):
            self.log(f"<- {name} {shown}  => not answered (error on purpose)")
        else:
            if sid != 0x3E:
                self.log(f"<- {name} {shown}  => {'(suppressed)' if suppressed else reply[:8].hex(' ')}")
            if not suppressed:
                self.respond(reply, errors=errors)
        if sid == 0x11:
            self._reset()

    def _busy(self, seconds: float, sid: int, pending: bool = False):
        """Spend `seconds` on a request. Beyond P2 (or with pending=True) the tester is kept waiting with
        NRC 0x78 (response pending), repeated every pending_interval."""
        if seconds <= 0:
            return
        if not pending and seconds <= self.config.p2_ms / 1000:
            time.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while True:
            self.respond(bytes([0x7F, sid, 0x78]))
            time.sleep(max(0.0, min(self.config.pending_interval, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                return

    @staticmethod
    def _subfunction(request: bytes, minimum_length: int = 2):
        if len(request) < minimum_length:
            raise NegativeResponse(0x13)
        return request[1] & 0x7F, bool(request[1] & 0x80)

    def _require_session(self, *sessions):
        if self.state.session not in sessions:
            raise NegativeResponse(0x7F)

    def _require_unlocked(self):
        if not self.state.unlocked:
            raise NegativeResponse(0x33)

    def check_session_timeout(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        if self.state.session != DEFAULT_SESSION and now - self.state.last_request > self.config.s3_timeout:
            self.log("S3 timeout: back to default session")
            self._enter_default_session()

    def _stop_scheduled(self, why: str):
        """Periodic data and ResponseOnEvent end with the session they were set up in."""
        if self.state.periodic or self.state.events_active:
            self.log(f"Periodic data and ResponseOnEvent stopped: {why}")
        self.state.periodic.clear()
        self.state.events_active = False

    def _enter_default_session(self):
        self._stop_scheduled("back to the default session")
        self._release_io_controls()
        self.state.session = DEFAULT_SESSION
        self.state.unlocked_levels.clear()
        self.state.seed = None
        self.state.transfer = None
        self.state.erased.clear()
        self.state.routines.clear()
        self.state.dtc_setting_on = True
        self.dtc_memory.set_frozen(False)
        self.state.communication_enabled = True

    def _service_10(self, request):
        session, suppress = self._subfunction(request)
        if session not in SESSION_NAMES:
            raise NegativeResponse(0x12)
        if len(request) != 2:
            raise NegativeResponse(0x13)
        if (session == PROGRAMMING_SESSION and self.state.session == DEFAULT_SESSION
                and self.config.programming_needs_extended and not self.state.bootloader):
            raise NegativeResponse(0x22)  # enter the extended session first
        if session == DEFAULT_SESSION:
            self._enter_default_session()
        else:
            if session != self.state.session:
                self._stop_scheduled("the session changed")
                self.state.unlocked_levels.clear()
                self.state.seed = None
            self.state.session = session
        # sessionParameterRecord: P2server_max in ms, P2*server_max in units of 10 ms
        timing = self.config.p2_ms.to_bytes(2, "big") + (self.config.p2_star_ms // 10).to_bytes(2, "big")
        return None if suppress else bytes([0x50, session]) + timing

    def _service_11(self, request):
        reset_type, suppress = self._subfunction(request)
        if reset_type not in SUB_FUNCTIONS[0x11]:
            raise NegativeResponse(0x12)
        if len(request) != 2:
            raise NegativeResponse(0x13)
        return None if suppress else bytes([0x51, reset_type])

    def _reset(self):
        self._enter_default_session()
        self.state.rebooting_until = time.monotonic() + 0.5
        self.state.bootloader = not self.state.application_valid
        self.log("ECU reset" + (": the application is not valid, so the bootloader runs" if self.state.bootloader
                                else ""))
        self.new_operation_cycle()

    def new_operation_cycle(self):
        """An operation cycle ends and the next begins (the fault memory's pending and aging)."""
        self._cycle_started = time.monotonic()
        self.dtc_memory.new_operation_cycle()
        self.log(f"Operation cycle {self.dtc_memory.cycle}")

    # --- DTCs --------------------------------------------------------------------

    def set_fault(self, dtc: int, present: bool):
        """The fault behind a DTC appears or goes away (the window's Fault box)."""
        self.dtc_memory.set_fault(dtc, present)
        self.log(f"Fault {'present' if present else 'gone'}: DTC {dtc:06X}, status {self.dtcs[dtc]:02X}")

    def _capture_snapshot(self) -> bytes | None:
        """The snapshot record of this moment: the snapshot DIDs with their values (None: none of them)."""
        entries = []
        for did in self.config.snapshot_dids:
            try:
                entries.append(int(did).to_bytes(2, "big") + self._did_value(int(did)))
            except NegativeResponse:
                continue
        return bytes([len(entries)]) + b"".join(entries) if entries else None

    def _service_14(self, request):
        if len(request) not in (4, 5):
            raise NegativeResponse(0x13)
        if not self.dtc_memory.clear(int.from_bytes(request[1:4], "big")):
            raise NegativeResponse(0x31)
        return b"\x54"

    def _service_19(self, request):
        report, suppress = self._subfunction(request, 2)
        if report in (0x01, 0x02):                      # count / report by status mask
            if len(request) != 3:
                raise NegativeResponse(0x13)
            mask = request[2]
            matching = {dtc: status for dtc, status in self.dtcs.items() if status & mask}
            if report == 0x01:
                reply = bytes([0x59, 0x01, 0xFF, 0x01]) + len(matching).to_bytes(2, "big")
            else:
                reply = bytes([0x59, 0x02, 0xFF]) + b"".join(dtc.to_bytes(3, "big") + bytes([status])
                                                              for dtc, status in matching.items())
        elif report == 0x0A:                            # every DTC the ECU supports
            if len(request) != 2:
                raise NegativeResponse(0x13)
            reply = bytes([0x59, 0x0A, 0xFF]) + b"".join(dtc.to_bytes(3, "big") + bytes([status])
                                                          for dtc, status in self.dtcs.items())
        elif report in (0x04, 0x06):                    # snapshot / extended data record of one DTC
            if len(request) != 6:
                raise NegativeResponse(0x13)
            dtc, record = int.from_bytes(request[2:5], "big"), request[5]
            if dtc not in self.dtcs or record not in (0x01, 0xFF):
                raise NegativeResponse(0x31)
            snapshot, extended = self.dtc_records.get(dtc, (b"", b""))
            data = snapshot if report == 0x04 else extended
            reply = bytes([0x59, report]) + request[2:5] + bytes([self.dtcs[dtc]])
            if data:                                    # record 01 is the only one this ECU keeps
                reply += b"\x01" + data
        else:
            raise NegativeResponse(0x12)
        return None if suppress else reply

    def _service_85(self, request):
        setting, suppress = self._subfunction(request)
        if setting not in (0x01, 0x02):
            raise NegativeResponse(0x12)
        self.state.dtc_setting_on = setting == 0x01
        self.dtc_memory.set_frozen(not self.state.dtc_setting_on)
        return None if suppress else bytes([0xC5, setting])

    # --- data by identifier ----------------------------------------------------------

    def _signal_bytes(self, key: str, length: int) -> bytes | None:
        """A signal's raw value in length bytes (big-endian, two's complement below zero), or None."""
        raw = self.signals.raw(key)
        if raw is None:
            return None
        raw = int(round(raw))
        low, high = (-(1 << (8 * length - 1)), (1 << (8 * length)) - 1)
        raw = max(low, min(high, raw))
        return (raw & ((1 << (8 * length)) - 1)).to_bytes(length, "big")

    def _did_value(self, did: int) -> bytes:
        """What the DID holds now, without the session and security rules (0x31 for an unknown DID)."""
        if did == 0xF186:
            return bytes([self.state.session])
        if did == 0x0100:  # uptime in seconds
            return int(time.monotonic() - self._started).to_bytes(4, "big")
        if self.state.bootloader and did == 0xF195:
            return BOOT_VERSION
        if did in self.dids:
            signal = self.did_access.get(did, ("", (), 0))[0]
            if signal:
                if self.state.bootloader:
                    raise NegativeResponse(0x31)     # the application's data: not in the bootloader
                value = self._signal_bytes(signal, len(self.dids[did]))
                if value is not None:
                    return value
            return self.dids[did]
        raise NegativeResponse(0x31)

    def _check_did_access(self, did: int):
        """NRC 0x31 outside the DID's sessions, 0x33 without its security level."""
        _signal, sessions, level = self.did_access.get(did, ("", (), 0))
        if sessions and self.state.session not in sessions:
            raise NegativeResponse(0x31)
        if level and level not in self.state.unlocked_levels:
            raise NegativeResponse(0x33)

    def read_did(self, did: int) -> bytes:
        """What ReadDataByIdentifier answers for one DID, rules included."""
        value = self._did_value(did)
        self._check_did_access(did)
        return value

    def _service_22(self, request):
        if len(request) < 3 or len(request) % 2 == 0:
            raise NegativeResponse(0x13)
        reply = bytearray(b"\x62")
        for index in range(1, len(request), 2):
            did = int.from_bytes(request[index:index + 2], "big")
            reply += request[index:index + 2] + self.read_did(did)
        return bytes(reply)

    def _service_2e(self, request):
        if len(request) < 4:
            raise NegativeResponse(0x13)
        did = int.from_bytes(request[1:3], "big")
        signal, _sessions, level = self.did_access.get(did, ("", (), 0))
        if did not in self.dids or did not in self.writable or signal:   # a signal's DID: see 0x2F
            raise NegativeResponse(0x31)
        self._check_did_access(did)
        if not level:
            self._require_unlocked()
        if len(request) - 3 != len(self.dids[did]):        # a DID keeps its length, as the VIN its 17 bytes
            raise NegativeResponse(0x13)
        ranges = self.did_valid.get(did)
        value = int.from_bytes(request[3:], "big")
        if ranges and not any(low <= value <= high for low, high in ranges):
            raise NegativeResponse(0x31)                   # a value the DID does not take
        self.dids[did] = bytes(request[3:])
        return b"\x6E" + request[1:3]

    # --- periodic data and events ---------------------------------------------------------

    def _periodic_room(self) -> int:
        """Data bytes one periodic frame carries: 6A frames lose the PCI, 6A and the identifier."""
        address = int(self.config.address_byte is not None)
        return 8 - address - (1 if self.config.periodic_id is not None else 3)

    def _service_2a(self, request):
        if len(request) < 2:
            raise NegativeResponse(0x13)
        mode, identifiers = request[1], bytes(request[2:])
        if mode == STOP_SENDING:
            for identifier in (identifiers or list(self.state.periodic)):
                self.state.periodic.pop(identifier, None)
            return b"\x6A"
        if mode not in PERIODIC_MODES:
            raise NegativeResponse(0x31)
        if not identifiers:
            raise NegativeResponse(0x13)
        for identifier in identifiers:
            if len(self.read_did(0xF200 | identifier)) > self._periodic_room():
                raise NegativeResponse(0x31)             # too long for a periodic frame
        if len(set(self.state.periodic) | set(identifiers)) > MAX_PERIODIC:
            raise NegativeResponse(0x31)
        now = time.monotonic()
        for identifier in identifiers:
            self.state.periodic[identifier] = {"mode": mode, "next": now}
        return b"\x6A"

    def _send_periodic(self, now: float):
        for identifier, entry in list(self.state.periodic.items()):
            if now < entry["next"]:
                continue
            period = self.config.periodic_rates_ms[entry["mode"] - 1] / 1000
            entry["next"] = entry["next"] + period if entry["next"] + period > now else now + period
            try:
                data = self._did_value(0xF200 | identifier)
            except NegativeResponse:
                self.state.periodic.pop(identifier, None)
                continue
            if self.config.periodic_id is None:
                payload = bytes([0x6A, identifier]) + data
                self._send_frame(bytes([len(payload)]) + payload)
            else:
                self._send_frame(bytes([identifier]) + data, self.config.periodic_id)

    def _service_86(self, request):
        sub, suppress = self._subfunction(request)
        event_type = sub & 0x3F                     # bit 6: storeEvent, kept as it is
        state = self.state
        if event_type == 0x04:                      # reportActivatedEvents
            reply = bytes([0xC6, sub, len(state.events)]) + b"".join(
                bytes([event["type"], event["window"]]) + event["record"] + event["service"] for event in state.events)
            return None if suppress else reply
        if len(request) < 3:
            raise NegativeResponse(0x13)
        window = request[2]
        if event_type == 0x00:                      # stopResponseOnEvent
            state.events_active = False
        elif event_type == 0x05:                    # startResponseOnEvent
            if not state.events:
                raise NegativeResponse(0x24)
            for event in state.events:
                event["last"] = self._event_value(event)
            state.events_active = True
        elif event_type == 0x06:                    # clearResponseOnEvent
            state.events.clear()
            state.events_active = False
        elif event_type in (0x01, 0x03):            # onDTCStatusChange / onChangeOfDataIdentifier
            record_length = 1 if event_type == 0x01 else 2
            record, service = bytes(request[3:3 + record_length]), bytes(request[3 + record_length:])
            if len(record) != record_length or not service:
                raise NegativeResponse(0x13)
            if service[0] != (0x19 if event_type == 0x01 else 0x22):
                raise NegativeResponse(0x31)        # the service that answers the event
            if event_type == 0x03:
                self._did_value(int.from_bytes(record, "big"))
            events = [event for event in state.events
                      if not (event["type"] == event_type and (event_type == 0x01 or event["record"] == record))]
            if len(events) >= MAX_EVENTS:
                raise NegativeResponse(0x31)
            event = {"type": event_type, "window": window, "record": record, "service": service}
            event["last"] = self._event_value(event)
            state.events = events + [event]
            return None if suppress else bytes([0xC6, sub, 0, window]) + record + service
        else:
            raise NegativeResponse(0x12)
        return None if suppress else bytes([0xC6, sub, 0, window])

    def _event_value(self, event):
        if event["type"] != 0x03:
            return None
        try:
            return self._did_value(int.from_bytes(event["record"], "big"))
        except NegativeResponse:
            return None

    def _event_response(self, event):
        """The event happened: the service it names answers, unasked."""
        service = event["service"]
        try:
            reply = getattr(self, f"_service_{service[0]:02x}")(service)
        except NegativeResponse as exc:
            reply = bytes([0x7F, service[0], exc.nrc])
        if reply is None:
            return
        self.log(f"-> ResponseOnEvent: {reply[:8].hex(' ')}{'...' if len(reply) > 8 else ''}")
        try:
            self.respond(reply)
        except IsoTpError as exc:                   # a tester without ResponseOnEvent does not take it
            self.log(f"The tester did not take the event response: {exc}")

    def _fire_events(self, now: float):
        changes = []
        while self.dtc_memory.changes:
            changes.append(self.dtc_memory.changes.popleft())
        state = self.state
        if not state.events_active:
            return
        compare = now >= self._next_event_check
        if compare:
            self._next_event_check = now + EVENT_CHECK_INTERVAL
        for event in list(state.events):
            if event["type"] == 0x01:
                mask = event["record"][0]
                if any((new & ~old) & mask for _dtc, old, new in changes):
                    self._event_response(event)
            elif compare:
                value = self._event_value(event)
                if value != event["last"]:
                    event["last"] = value
                    self._event_response(event)

    # --- input/output control ---------------------------------------------------------------

    def _service_2f(self, request):
        if len(request) < 4:
            raise NegativeResponse(0x13)
        did, parameter, control_state = int.from_bytes(request[1:3], "big"), request[3], bytes(request[4:])
        signal = self.did_access.get(did, ("", (), 0))[0]
        if did not in self.dids or not signal or self.signals.signal(signal) is None:
            raise NegativeResponse(0x31)             # only a DID that follows a signal is an output here
        self._check_did_access(did)
        length = len(self.dids[did])
        if parameter == 0x00:                       # returnControlToECU
            self.signals.release(signal)
            self.state.io_controls.pop(did, None)
        elif parameter == 0x01:                     # resetToDefault
            self.signals.override(signal, self.signals.default_raw(signal))
        elif parameter == 0x02:                     # freezeCurrentState
            self.signals.override(signal, self.signals.raw(signal))
        elif parameter == 0x03:                     # shortTermAdjustment: controlState, maybe with a mask
            if len(control_state) not in (length, 2 * length):
                raise NegativeResponse(0x13)
            self.signals.override(signal, int.from_bytes(control_state[:length], "big",
                                                         signed=self.signals.signal(signal).is_signed))
        else:
            raise NegativeResponse(0x31)
        if parameter:
            self.state.io_controls[did] = parameter
        return b"\x6F" + request[1:4] + self._did_value(did)

    def _release_io_controls(self):
        """InputOutputControl ends with the session: every signal back to its generator."""
        for did in list(self.state.io_controls):
            signal = self.did_access.get(did, ("", (), 0))[0]
            if signal:
                self.signals.release(signal)
        if self.state.io_controls:
            self.log("InputOutputControl returned to the ECU")
        self.state.io_controls.clear()

    # --- security ---------------------------------------------------------------------

    def _expected_key(self, level: int, seed: bytes, settings: dict) -> bytes:
        path = settings.get("dll", "").strip()
        if not path:
            return bytes(byte ^ (settings["key_mask"] & 0xFF) for byte in seed)
        library = self._libraries.get(path)
        if library is None:
            library = self._libraries[path] = load_library(path)
        return generate_key(library, seed, level, settings.get("variant", ""))

    def _service_27(self, request):
        sub, _ = self._subfunction(request)
        levels = security_levels(self.config)
        if sub in levels:
            level, send_key = sub, False
        elif sub % 2 == 0 and sub - 1 in levels:
            level, send_key = sub - 1, True
        else:
            raise NegativeResponse(0x12)
        if time.monotonic() < self.state.locked_until:
            raise NegativeResponse(0x37)
        settings = levels[level]
        if not send_key:
            if len(request) != 2:
                raise NegativeResponse(0x13)
            length = settings["seed_length"]
            seed = bytes(length) if level in self.state.unlocked_levels else os.urandom(length)
            self.state.seed = (level, seed)
            return bytes([0x67, sub]) + seed
        if self.state.seed is None or self.state.seed[0] != level:
            raise NegativeResponse(0x24)
        seed = self.state.seed[1]
        self.state.seed = None
        try:
            expected = self._expected_key(level, seed, settings)
        except SeedKeyError as exc:
            self.log(f"SecurityAccess level {level:02X}: {exc}")
            raise NegativeResponse(0x22) from None
        if request[2:] != expected:
            self.state.failed_attempts += 1
            if self.state.failed_attempts >= self.config.max_attempts:
                self.state.failed_attempts = 0
                self.state.locked_until = time.monotonic() + self.config.lockout_seconds
                raise NegativeResponse(0x36)
            raise NegativeResponse(0x35)
        self.state.failed_attempts = 0
        self.state.unlocked_levels.add(level)
        return bytes([0x67, sub])

    def _service_28(self, request):
        control, suppress = self._subfunction(request)
        if control not in SUB_FUNCTIONS[0x28]:
            raise NegativeResponse(0x12)
        if len(request) != 3:                        # controlType and communicationType, no more
            raise NegativeResponse(0x13)
        self.state.communication_enabled = control == 0x00
        return None if suppress else bytes([0x68, control])

    def _service_3e(self, request):
        zero, suppress = self._subfunction(request)
        if zero != 0x00:
            raise NegativeResponse(0x12)
        if len(request) != 2:
            raise NegativeResponse(0x13)
        return None if suppress else b"\x7E\x00"

    # --- memory and flashing ----------------------------------------------------------------

    @staticmethod
    def _memory_range(request: bytes, offset: int):
        """(address, size) from the addressAndLengthFormatIdentifier at request[offset] and the fields after it:
        low nibble = memoryAddress bytes, high nibble = memorySize bytes."""
        if len(request) <= offset:
            raise NegativeResponse(0x13)
        fmt = request[offset]
        address_length, size_length = fmt & 0x0F, fmt >> 4
        if not address_length or not size_length:
            raise NegativeResponse(0x31)
        end = offset + 1 + address_length + size_length
        if len(request) != end:
            raise NegativeResponse(0x13)
        address = int.from_bytes(request[offset + 1:offset + 1 + address_length], "big")
        size = int.from_bytes(request[offset + 1 + address_length:end], "big")
        if size == 0:
            raise NegativeResponse(0x31)
        return address, size

    def _check_range(self, address: int, size: int):
        """NRC 0x31 unless the whole range lies inside one of the configured memory ranges."""
        ranges = self.config.memory_ranges
        if ranges and not any(first <= address and address + size - 1 <= last for first, last in ranges):
            raise NegativeResponse(0x31)

    def read_memory(self, address: int, size: int) -> bytearray:
        """size bytes from address: downloaded data where downloads completed, erased flash (0xFF) elsewhere."""
        data = bytearray(b"\xFF" * size)
        for start, segment in sorted(self.state.memory.items()):
            first, last = max(start, address), min(start + len(segment), address + size)
            if first < last:
                data[first - address:last - address] = segment[first - start:last - start]
        return data

    def write_memory(self, address: int, data: bytes):
        """Write data at address, merged with the segments it overlaps or touches."""
        end = address + len(data)
        touching = [start for start, segment in self.state.memory.items()
                    if start <= end and start + len(segment) >= address]
        first = min([address, *touching])
        last = max([end, *(start + len(self.state.memory[start]) for start in touching)])
        merged = self.read_memory(first, last - first)
        merged[address - first:end - first] = data
        for start in touching:
            del self.state.memory[start]
        self.state.memory[first] = bytes(merged)

    def _service_23(self, request):
        address, size = self._memory_range(request, 1)
        if size > 0xFFE:                             # 63 and the data in one ISO-TP message without escape
            raise NegativeResponse(0x31)
        self._check_range(address, size)
        return b"\x63" + bytes(self.read_memory(address, size))

    def _service_3d(self, request):
        if len(request) < 2:
            raise NegativeResponse(0x13)
        fmt = request[1]
        address_length, size_length = fmt & 0x0F, fmt >> 4
        if not address_length or not size_length:
            raise NegativeResponse(0x31)
        end = 2 + address_length + size_length
        address = int.from_bytes(request[2:2 + address_length], "big")
        size = int.from_bytes(request[2 + address_length:end], "big")
        data = bytes(request[end:])
        if len(request) <= end or len(data) != size:
            raise NegativeResponse(0x13)
        self._check_range(address, size)
        self._require_unlocked()
        self.write_memory(address, data)
        return b"\x7D" + request[1:end]

    def _invalidate_application(self):
        if self.state.application_valid:
            self.state.application_valid = False
            self.log("The application is not valid until checkProgrammingDependencies passes")

    def _check_image(self, option: bytes) -> tuple[bool, str]:
        """Whether the downloaded image passes checkProgrammingDependencies, and why not."""
        image = b"".join(bytes(self.state.memory[address]) for address in sorted(self.state.memory))
        if not image:
            return False, "nothing has been downloaded"
        mode = self.config.image_crc
        if mode == "option":
            if len(option) != 4:
                raise NegativeResponse(0x13)        # the CRC-32 is expected as the option record
            expected, actual = int.from_bytes(option, "big"), zlib.crc32(image)
        elif mode == "trailer":
            if len(image) <= 4:
                return False, "the image is too short to end with its CRC-32"
            expected, actual = int.from_bytes(image[-4:], "big"), zlib.crc32(image[:-4])
        else:
            return True, ""
        if expected != actual:
            return False, f"the image's CRC-32 is {actual:08X}, expected {expected:08X}"
        return True, ""

    def _software_version(self) -> bytes:
        """The software version of a newly flashed image: the text at version_address when it is set."""
        image = b"".join(bytes(self.state.memory[address]) for address in sorted(self.state.memory))
        if self.config.version_address is not None:
            text = bytes(self.read_memory(self.config.version_address, max(1, self.config.version_length)))
            text = text.rstrip(b"\x00\xFF")
            if text:
                return text
        return f"APP-FLASHED-{zlib.crc32(image):08X}".encode()

    def _routine_status(self, routine: int) -> int:
        entry = self.state.routines[routine]
        if entry["status"] == ROUTINE_RUNNING and entry["until"] is not None and time.monotonic() >= entry["until"]:
            entry["status"] = ROUTINE_DONE
        return entry["status"]

    def _service_31(self, request):
        control, suppress = self._subfunction(request, 4)
        routine = int.from_bytes(request[2:4], "big")
        if control not in SUB_FUNCTIONS[0x31]:
            raise NegativeResponse(0x12)
        flashing = routine in (self.config.erase_routine, self.config.check_routine)
        if not flashing and routine != self.config.self_test_routine:
            raise NegativeResponse(0x31)
        needed = PROGRAMMING_SESSION if flashing else EXTENDED_SESSION
        if self.state.session != needed:
            raise NegativeResponse(0x31)         # not a routine of this session
        if flashing:
            self._require_unlocked()
        if control != 0x01:                      # stop, results: of a routine started before
            if flashing and control == 0x02:
                raise NegativeResponse(0x12)     # erasing and checking run to their end
            if len(request) != 4:
                raise NegativeResponse(0x13)
            if routine not in self.state.routines:
                raise NegativeResponse(0x24)     # not started
            status = self._routine_status(routine)
            if control == 0x02:
                if status != ROUTINE_RUNNING:
                    raise NegativeResponse(0x24)     # nothing running to stop
                self.state.routines[routine]["status"] = status = ROUTINE_STOPPED
            return None if suppress else bytes([0x71, control, *request[2:4], status])
        if routine == self.config.self_test_routine:
            if len(request) != 4:
                raise NegativeResponse(0x13)
            if routine in self.state.routines and self._routine_status(routine) == ROUTINE_RUNNING:
                raise NegativeResponse(0x24)     # already running
            self.state.routines[routine] = {"status": ROUTINE_RUNNING,
                                            "until": time.monotonic() + max(0.0, self.config.self_test_seconds)}
            return None if suppress else bytes([0x71, 0x01, *request[2:4], ROUTINE_RUNNING])
        if routine == self.config.erase_routine:
            address, size = self._memory_range(request, 4)
            self._check_range(address, size)
            self._busy(self.config.erase_seconds, 0x31, pending=True)  # erasing takes longer than P2
            suppress = suppress and self.config.erase_seconds <= 0     # after a response pending, the answer
            self._invalidate_application()
            self.state.erased.append((address, address + size))
            for start in [a for a in self.state.memory if address <= a < address + size]:
                del self.state.memory[start]
            status = 0x00
        else:
            ok, why = self._check_image(bytes(request[4:]))
            status = 0x00 if ok else 0x01
            if ok:
                self.state.application_valid = True
                self.dids[0xF195] = self._software_version()
                self.log(f"checkProgrammingDependencies passed: software version "
                         f"{self.dids[0xF195].decode('latin-1')}")
                if self.config.dump_path:
                    self.write_image(self.config.dump_path)
            else:
                self.log(f"checkProgrammingDependencies failed: {why}")
        self.state.routines[routine] = {"status": status, "until": None}
        return None if suppress else bytes([0x71, 0x01, *request[2:4], status])

    def _start_transfer(self, request: bytes, direction: str) -> bytes:
        """Checks shared by RequestDownload and RequestUpload; returns the positive response."""
        self._require_unlocked()
        if self.state.transfer is not None:
            raise NegativeResponse(0x22)  # a transfer is already active
        if len(request) < 3:
            raise NegativeResponse(0x13)
        if request[1] not in self.config.data_formats:
            raise NegativeResponse(0x31)  # compression/encryption method not supported
        if self.config.address_format is not None and request[2] != self.config.address_format:
            raise NegativeResponse(0x31)
        address, size = self._memory_range(request, 2)
        self._check_range(address, size)
        if direction == "download" and self.config.require_erase and not any(
                start <= address and address + size <= end for start, end in self.state.erased):
            raise NegativeResponse(0x70)  # erase before download
        if direction == "download":
            self._invalidate_application()
        self.state.transfer = {
            "direction": direction, "address": address, "size": size, "done": 0, "next": 1, "last": None,
            "data": bytearray() if direction == "download" else self.read_memory(address, size), "block": b"",
        }
        maximum = self.config.max_block_length
        length = max(self.config.block_length_bytes, (maximum.bit_length() + 7) // 8)
        rsid = 0x74 if direction == "download" else 0x75
        return bytes([rsid, length << 4]) + maximum.to_bytes(length, "big")

    def _service_34(self, request):
        return self._start_transfer(request, "download")

    def _service_35(self, request):
        return self._start_transfer(request, "upload")

    def _service_36(self, request):
        transfer = self.state.transfer
        if transfer is None:
            raise NegativeResponse(0x24)
        if len(request) < 2:
            raise NegativeResponse(0x13)
        counter, block = request[1], request[2:]
        if counter == transfer["last"]:  # a repeated request (its response was lost): answer, do not write twice
            return bytes([0x76, counter]) + transfer["block"]
        if counter != transfer["next"]:
            raise NegativeResponse(0x73)
        room, remaining = self.config.max_block_length - 2, transfer["size"] - transfer["done"]
        if transfer["direction"] == "upload":
            if block:
                raise NegativeResponse(0x13)
            if remaining <= 0:
                raise NegativeResponse(0x24)  # everything sent: RequestTransferExit is expected
            reply = bytes(transfer["data"][transfer["done"]:transfer["done"] + room])
            transfer["done"] += len(reply)
        else:
            if not block or len(request) > self.config.max_block_length:
                raise NegativeResponse(0x13)
            if len(block) > remaining:
                raise NegativeResponse(0x71)  # more data than RequestDownload announced
            if self.config.full_blocks and len(block) != min(room, remaining):
                raise NegativeResponse(0x13)
            transfer["data"] += block
            transfer["done"] += len(block)
            reply = b""
        transfer["last"], transfer["next"], transfer["block"] = counter, (counter + 1) & 0xFF, reply
        return bytes([0x76, counter]) + reply

    def _service_37(self, request):
        transfer = self.state.transfer
        if transfer is None or transfer["done"] != transfer["size"]:
            raise NegativeResponse(0x24)
        if transfer["direction"] == "download":
            self.state.memory[transfer["address"]] = bytes(transfer["data"])
        self.state.transfer = None
        return b"\x77"

    def write_image(self, path):
        """Write the downloaded memory as S3 records."""
        lines = []
        for address in sorted(self.state.memory):
            data = self.state.memory[address]
            for offset in range(0, len(data), 32):
                chunk = bytes(data[offset:offset + 32])
                body = bytes([len(chunk) + 5]) + (address + offset).to_bytes(4, "big") + chunk
                lines.append("S3" + (body + bytes([~sum(body) & 0xFF])).hex().upper())
        with open(path, "w", encoding="ascii") as handle:
            handle.write("\n".join(lines) + "\n")
        self.log(f"Flashed image written to {path}")

    # --- application traffic ---------------------------------------------------------

    def _application_frame(self, message: can.Message):
        data = bytes(message.data)
        if message.arbitration_id == 0x200 and data:
            self.running = {0x01: True, 0x02: False}.get(data[0], self.running)
            self.log(f"Command 0x200: {'running' if self.running else 'stopped'}")
        elif message.arbitration_id == 0x201 and data:
            self.logging = bool(data[0] & 0x01)
            self.log(f"Command 0x201: logging {'on' if self.logging else 'off'}")

    def application_running(self, now: float | None = None) -> bool:
        """Whether the application sends its frames: not in the bootloader, not while resetting, not while
        CommunicationControl has normal messages off."""
        now = time.monotonic() if now is None else now
        state = self.state
        return not state.bootloader and state.communication_enabled and now >= state.rebooting_until

    # --- main loop ------------------------------------------------------------------------

    def tick(self, now: float | None = None):
        """What the ECU does by itself: application frames, periodic data, event responses, the operation
        cycle timer and the S3 timeout."""
        now = time.monotonic() if now is None else now
        self.j1939.tick(now)                        # its address is claimed before its frames go out
        if self.config.broadcast_interval > 0 and self.application_running(now):
            for frame in self.signals.due_frames(self.config.broadcast_interval, now):
                frame = self.j1939.application_frame(frame) if self.config.j1939 else frame
                if frame is not None:               # None: a J1939 node without an address stays quiet
                    self.bus.send(frame)
        self._send_periodic(now)
        self._fire_events(now)
        if 0 < self.config.operation_cycle_seconds <= now - self._cycle_started:
            self.new_operation_cycle()
        self.check_session_timeout(now)

    def serve(self, stop: threading.Event):
        while not stop.is_set():
            try:
                self.tick()
            except can.CanError:
                raise                     # the adapter went away: the window sees the thread end
            except Exception as exc:      # keep the simulator alive on anything else
                self.log(f"Error: {exc}")
            message = self._io.recv(timeout=0.01)
            if message is not None:
                try:
                    self.on_message(message)
                except Exception as exc:  # keep the simulator alive on transport errors
                    self.log(f"Error: {exc}")
                    self._rx = None


def application_ids(config: EcuConfig) -> set[int]:
    """The identifiers of the application frames an ECU with these settings sends."""
    if not config.broadcast_interval:
        return set()
    signals = SignalSimulation()
    try:
        signals.load(config.dbc_path)
    except ValueError:
        signals.load("")
    try:
        signals.configure(config.generators, config.messages)
    except ValueError:
        pass
    if config.j1939:                   # its 29-bit frames go out from its own address
        return {(can_id & ~0xFF) | config.j1939_address if can_id > 0x7FF else can_id
                for can_id in signals.message_ids()}
    return signals.message_ids()


def claim_channel(interface, channel, request_id=None):
    """Hold a localhost port as a lock while this ECU runs, or return None if another dummy ECU on this
    computer already holds it. (On Kvaser, programs sharing a channel do not see each other's frames, so
    only a lock can tell that a second copy was started.) With request_id the lock is for that ECU address
    only, so several ECUs with their own identifiers can share a channel."""
    key = f"{interface}:{channel}" if request_id is None else f"{interface}:{channel}:{request_id:X}"
    port = 47000 + zlib.crc32(key.encode()) % 2000
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no other socket may share the port
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        lock.bind(("127.0.0.1", port))
    except OSError:
        lock.close()
        return None
    return lock


def other_ecu_present(bus, config: EcuConfig, listen: float = 0.4) -> bool:
    """True when another ECU already answers on this ECU's response identifier - two ECUs answering the
    same requests break security access and flashing - or, when this one sends application frames too,
    when someone else already sends frames of the same identifiers. An ECU with its own identifiers and
    no application frames can join a channel that already has one."""
    probe = bytes([0x02, 0x3E, 0x00])  # functional TesterPresent
    if config.address_byte is not None:
        probe = bytes([config.address_byte]) + probe
    frames = application_ids(config)
    bus.send(can.Message(arbitration_id=config.functional_id, data=probe, is_extended_id=config.extended_ids))
    deadline = time.monotonic() + listen
    while time.monotonic() < deadline:
        message = bus.recv(0.05)
        if message is None or message.is_error_frame:
            continue
        if message.arbitration_id == config.response_id and bool(message.is_extended_id) == config.extended_ids:
            return True
        if message.arbitration_id in frames:
            return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    hex_int = lambda text: int(text, 16)  # noqa: E731
    parser.add_argument("--console", action="store_true", help="run in this console, without the window")
    parser.add_argument("--config", metavar="FILE", help="settings profile saved from the window (JSON)")
    parser.add_argument("--interface", help="python-can interface (default: kvaser)")
    parser.add_argument("--channel", help="channel (default: 1, the second Kvaser virtual channel)")
    parser.add_argument("--bitrate", type=int, help="bit rate (default 500000)")
    parser.add_argument("--request-id", type=hex_int, help="physical request ID, hex (default 7E0)")
    parser.add_argument("--response-id", type=hex_int, help="response ID, hex (default 7E8)")
    parser.add_argument("--functional-id", type=hex_int, help="functional request ID, hex (default 7DF)")
    parser.add_argument("--extended-ids", action="store_true", default=None, help="use 29-bit identifiers")
    parser.add_argument("--address-byte", type=hex_int, help="extended addressing byte, hex")
    parser.add_argument("--max-block", type=hex_int, help="maxNumberOfBlockLength, hex (default 402)")
    parser.add_argument("--block-size", type=int,
                        help="flow control BS: consecutive frames per block, 0 = no limit (default 8)")
    parser.add_argument("--stmin", type=lambda text: int(text, 0),
                        help="flow control STmin byte: 0-127 ms or 0xF1-0xF9 for 100-900 us (default 1)")
    parser.add_argument("--fc-wait", type=int, metavar="N",
                        help="send N flow control WAIT frames before each ContinueToSend (default 0)")
    parser.add_argument("--erase-seconds", type=float, help="simulated erase time (default 1)")
    parser.add_argument("--dbc", metavar="FILE", help="send the messages of this DBC (default: DBC/dummy_ecu.dbc)")
    parser.add_argument("--no-broadcast", action="store_true", help="do not send application frames")
    parser.add_argument("--dump", metavar="FILE", help="write the flashed image as S-records after flashing")
    parser.add_argument("--force", action="store_true", help="start even if another ECU already answers")
    parser.add_argument("--smoke-test", action="store_true", help="only build the window, then exit")
    args = parser.parse_args(argv)

    config, connection = load_profile(args.config) if args.config else (None, {})
    options = {"request_id": args.request_id, "response_id": args.response_id, "functional_id": args.functional_id,
               "extended_ids": args.extended_ids, "address_byte": args.address_byte, "max_block_length": args.max_block,
               "block_size": args.block_size, "st_min": args.stmin, "flow_waits": args.fc_wait,
               "erase_seconds": args.erase_seconds, "dump_path": args.dump, "dbc_path": args.dbc,
               "broadcast_interval": 0 if args.no_broadcast else None}
    overrides = {name: value for name, value in options.items() if value is not None}
    connection.update({key: value for key, value in (("interface", args.interface), ("channel", args.channel),
                                                     ("bitrate", args.bitrate)) if value is not None})
    if not args.console:
        from canexpert.simulator.window import run_window
        return run_window(config, overrides, connection, smoke_test=args.smoke_test)

    config = replace(config or EcuConfig(), **overrides)
    connection = {**DEFAULT_CONNECTION, **connection}
    interface, channel = connection["interface"], parse_channel(connection["channel"])
    lock = None if args.force else claim_channel(interface, channel, config.request_id)
    bus = can.Bus(interface=interface, channel=channel, bitrate=int(connection["bitrate"]))
    started = time.strftime("%H:%M:%S")

    def log(text):
        print(f"{time.strftime('%H:%M:%S')}  {text}", flush=True)

    if not args.force and (lock is None or other_ecu_present(bus, config)):
        print(f"Another ECU already answers on {interface} channel {channel}"
              f"{' (a dummy ECU is already running there)' if lock is None else ''}. Close the other dummy ECU "
              f"window first: two ECUs answering the same requests break security access and flashing. "
              f"(--force starts anyway.)", flush=True)
        bus.shutdown()
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                input("Press Enter to close...")  # keep a double-clicked window open long enough to read this
            except (EOFError, KeyboardInterrupt):
                pass
        return 1

    ecu = DummyEcu(bus, config, log)
    print(f"{started}  Dummy ECU on {interface} channel {channel}: requests 0x{config.request_id:X} "
          f"(functional 0x{config.functional_id:X}), responses 0x{config.response_id:X}. Ctrl+C to stop.", flush=True)
    stop = threading.Event()
    try:
        ecu.serve(stop)
    except KeyboardInterrupt:
        pass
    finally:
        bus.shutdown()
        if lock is not None:
            lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
