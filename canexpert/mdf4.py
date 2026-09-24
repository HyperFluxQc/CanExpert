"""
A small ASAM MDF 4.10 writer: the measurement format CANoe, CANape and most analysis tools read.

Only what the CAN Logger exports: floating-point signals, each with its own time axis (signals arrive
at their own moments, so each is a data group with a master time channel and its value), a unit, and
the moment the measurement started. It writes sorted, uncompressed data blocks, which every MDF 4
reader takes, and needs nothing beyond the standard library - the alternative, asammdf, brings pandas,
lxml and half a dozen compression libraries with it.

Block layout follows ASAM MDF 4.1: a 64-byte identification block, then blocks of a 24-byte header
(id, reserved, length, link count), their links as file offsets, and their data, 8-byte aligned.
"""
from __future__ import annotations

import struct
import time
from pathlib import Path

PROGRAM = b"CANExpt "                 # id_prog: 8 characters


class _Block:
    def __init__(self, kind: str, links: list, data: bytes = b""):
        self.kind, self.links, self.data = kind, links, data
        self.address = 0

    @property
    def size(self) -> int:
        return 24 + 8 * len(self.links) + len(self.data)

    def pack(self) -> bytes:
        header = struct.pack("<4s4xQQ", self.kind.encode("ascii"), self.size, len(self.links))
        links = b"".join(struct.pack("<Q", link.address if isinstance(link, _Block) else 0) for link in self.links)
        return header + links + self.data


def _text(kind: str, text: str) -> _Block:
    """A TX (plain text) or MD (XML) block: zero-terminated UTF-8, padded to 8 bytes."""
    raw = text.encode("utf-8") + b"\0"
    return _Block(kind, [], raw + b"\0" * (-len(raw) % 8))


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _channel(name_block, unit_block, byte_offset: int, master: bool, next_channel=None) -> _Block:
    data = struct.pack("<BBBBIIIIBxH6d",
                       2 if master else 0,        # cn_type: master or fixed-length value
                       1 if master else 0,        # cn_sync_type: time
                       4,                         # cn_data_type: IEEE 754 floating point, little endian
                       0, byte_offset, 64,        # bit offset, byte offset, bit count
                       0, 0, 0, 0,                # flags, invalidation bit, precision, attachments
                       0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    #       next channel, composition, name, source, conversion, data, unit, comment
    return _Block("##CN", [next_channel, None, name_block, None, None, None, unit_block, None], data)


def write_mdf4(path, signals, start_time: float | None = None, comment: str = "CAN Expert"):
    """Write signals - (name, unit, times, values) with times in seconds from start_time - as MDF 4.10.

    start_time is the measurement's start as a Unix time (UTC); now when it is not known. Signals
    without samples are left out. Returns the number of signals written."""
    start_ns = int((time.time() if start_time is None else start_time) * 1e9)
    blocks = []
    hd_comment = _text("##MD", f"<HDcomment><TX>{_xml_escape(comment)}</TX></HDcomment>")
    fh_comment = _text("##MD", "<FHcomment><TX>Exported by the CAN Logger</TX><tool_id>CAN Expert</tool_id>"
                               "<tool_vendor>CAN Expert</tool_vendor><tool_version>1</tool_version></FHcomment>")
    history = _Block("##FH", [None, fh_comment], struct.pack("<QhhB3x", start_ns, 0, 0, 0))
    header = _Block("##HD", [None, history, None, None, None, hd_comment],
                    struct.pack("<QhhBBBxdd", start_ns, 0, 0, 0, 0, 0, 0.0, 0.0))
    blocks += [header, hd_comment, history, fh_comment]

    previous_group = None
    written = 0
    for name, unit, times, values in signals:
        count = min(len(times), len(values))
        if not count:
            continue
        records = bytearray()
        for t, v in zip(list(times)[:count], list(values)[:count]):
            records += struct.pack("<dd", float(t), float(v))
        data = _Block("##DT", [], bytes(records))
        value_name, value_unit = _text("##TX", name), (_text("##TX", unit) if unit else None)
        value = _channel(value_name, value_unit, 8, master=False)
        time_name, time_unit = _text("##TX", "time"), _text("##TX", "s")
        master = _channel(time_name, time_unit, 0, master=True, next_channel=value)
        acquisition = _text("##TX", name)
        group = _Block("##CG", [None, master, acquisition, None, None, None],
                       struct.pack("<QQHH4xII", 0, count, 0, 0, 16, 0))
        data_group = _Block("##DG", [None, group, data, None], struct.pack("<B7x", 0))
        if previous_group is None:
            header.links[0] = data_group
        else:
            previous_group.links[0] = data_group
        previous_group = data_group
        blocks += [data_group, group, acquisition, master, time_name, time_unit, value, value_name]
        blocks += [value_unit] if value_unit else []
        blocks.append(data)
        written += 1

    address = 64                               # the identification block comes first
    for block in blocks:
        block.address = address
        address += block.size
    identification = (b"MDF     " + b"4.10    " + PROGRAM + b"\0" * 4 + struct.pack("<H", 410) + b"\0" * 30
                      + struct.pack("<HH", 0, 0))
    with open(Path(path), "wb") as handle:
        handle.write(identification)
        for block in blocks:
            handle.write(block.pack())
    return written
