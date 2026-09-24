"""
J1939 identifiers (J1939-21): priority (3 bits), extended data page and data page, PDU format (PF), PDU
specific (PS) and source address. With PF below 240 (PDU1) the PS is the destination address and not part of
the PGN; from 240 up (PDU2) it is part of the PGN and the message goes to everyone.
"""
from __future__ import annotations

from dataclasses import dataclass

GLOBAL = 0xFF                 # destination: every node
NULL_ADDRESS = 0xFE           # source of a node that could not claim an address
TOOL_ADDRESS = 0xF9           # off-board diagnostic-service tool #1: CAN Expert's address by default

PGN_ACKNOWLEDGMENT = 0xE800   # 59392
PGN_REQUEST = 0xEA00          # 59904
PGN_TP_DT = 0xEB00            # 60160
PGN_TP_CM = 0xEC00            # 60416
PGN_ADDRESS_CLAIMED = 0xEE00  # 60928
PGN_DM1 = 0xFECA              # 65226 active diagnostic trouble codes
PGN_DM2 = 0xFECB              # 65227 previously active
PGN_DM3 = 0xFECC              # 65228 clear previously active
PGN_DM11 = 0xFED3             # 65235 clear active
PGN_SOFT = 0xFEDA             # 65242 software identification
PGN_CI = 0xFEEB               # 65259 component identification
PGN_VI = 0xFEEC               # 65260 vehicle identification

# The parameter groups a J1939 tool meets most, by PGN.
PGN_NAMES = {
    PGN_ACKNOWLEDGMENT: "Acknowledgment", PGN_REQUEST: "Request", PGN_TP_DT: "TP.DT", PGN_TP_CM: "TP.CM",
    PGN_ADDRESS_CLAIMED: "Address Claimed", 0xFED8: "Commanded Address", 0xEF00: "PropA", 0x1EF00: "PropA2",
    PGN_DM1: "DM1", PGN_DM2: "DM2", PGN_DM3: "DM3", PGN_DM11: "DM11", 0xFECD: "DM4", 0xFECE: "DM5",
    PGN_SOFT: "SOFT", PGN_CI: "CI", PGN_VI: "VI", 0xFDC5: "ECUID",
    0xF003: "EEC2", 0xF004: "EEC1", 0xF000: "ERC1", 0xF001: "EBC1", 0xF002: "ETC1", 0xF005: "ETC2",
    0xFEF1: "CCVS", 0xFEEE: "ET1", 0xFEEF: "EFL/P1", 0xFEF2: "LFE", 0xFEE5: "HOURS", 0xFEE0: "VD",
    0xFEF5: "AMB", 0xFEF6: "IC1", 0xFEF7: "VEP1", 0xFEFC: "DD", 0xFEE9: "LFC", 0xFEEA: "VW", 0xFEE6: "TD",
    0xFEFF: "WFI", 0xFEF8: "TRF1", 0xFE6C: "TCO1", 0xFEC1: "VDHR", 0xF00A: "EGF1", 0xFD7C: "DPFC1",
    0xFEBF: "EBC2", 0xFE70: "CVW",
}


@dataclass(frozen=True)
class J1939Id:
    priority: int
    pgn: int
    source: int
    destination: int          # GLOBAL for a PDU2 message


def is_pdu1(pgn: int) -> bool:
    """A PGN whose PDU specific byte is a destination address (PF below 240)."""
    return ((pgn >> 8) & 0xFF) < 240


def parse_id(can_id: int) -> J1939Id:
    """What a 29-bit identifier says under J1939."""
    priority = (can_id >> 26) & 0x7
    page = (can_id >> 24) & 0x3                     # extended data page, data page
    pdu_format, pdu_specific = (can_id >> 16) & 0xFF, (can_id >> 8) & 0xFF
    if pdu_format < 240:
        return J1939Id(priority, (page << 16) | (pdu_format << 8), can_id & 0xFF, pdu_specific)
    return J1939Id(priority, (page << 16) | (pdu_format << 8) | pdu_specific, can_id & 0xFF, GLOBAL)


def make_id(pgn: int, source: int, destination: int = GLOBAL, priority: int = 6) -> int:
    """The 29-bit identifier of a message: a PDU1 PGN carries the destination in its PDU specific byte."""
    pgn &= 0x3FFFF
    if is_pdu1(pgn):
        pgn = (pgn & 0x3FF00) | (destination & 0xFF)
    return ((priority & 0x7) << 26) | (pgn << 8) | (source & 0xFF)


def dbc_pgn(message) -> int | None:
    """The PGN of a DBC message defined as a J1939 parameter group (cantools: protocol "j1939"), which any
    frame of that PGN matches whatever its source address, priority or destination; None for any other."""
    if getattr(message, "protocol", None) != "j1939" or not getattr(message, "is_extended_frame", False):
        return None
    return parse_id(message.frame_id).pgn


def lookup(by_frame: dict, by_pgn: dict, can_id: int):
    """What describes a frame: its own identifier's entry, else - a 29-bit frame - its J1939 PGN's."""
    found = by_frame.get(can_id)
    if found is None and by_pgn and can_id > 0x7FF:
        found = by_pgn.get(parse_id(can_id).pgn)
    return found


def pgn_name(pgn: int) -> str:
    """"EEC1", "PropB" for the proprietary range, else ""."""
    if pgn in PGN_NAMES:
        return PGN_NAMES[pgn]
    if 0xFF00 <= pgn <= 0xFFFF:
        return "PropB"
    return ""


def address_text(address: int) -> str:
    return "Global" if address == GLOBAL else ("Null" if address == NULL_ADDRESS else f"{address:02X}")


def describe(can_id: int) -> str:
    """"EEC1 (PGN 61444) 00 → Global": what a frame is, for the Trace."""
    j = parse_id(can_id)
    name = pgn_name(j.pgn)
    return f"{name + ' ' if name else ''}(PGN {j.pgn}) {j.source:02X} → {address_text(j.destination)}"
