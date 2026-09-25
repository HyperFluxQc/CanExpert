"""
J1939 diagnostic messages (J1939-73) DM1 (active DTCs) and DM2 (previously active): the four lamps with their
flash state, then a DTC of four bytes each - the SPN (19 bits), the FMI (5 bits), the conversion method and
the occurrence count (7 bits).
"""
from __future__ import annotations

from dataclasses import dataclass, field

LAMPS = ("Malfunction indicator", "Red stop", "Amber warning", "Protect")      # the order of their bit pairs
FMI_TEXT = {
    0: "Above normal range - most severe", 1: "Below normal range - most severe",
    2: "Erratic, intermittent or incorrect", 3: "Voltage above normal or shorted to high source",
    4: "Voltage below normal or shorted to low source", 5: "Current below normal or open circuit",
    6: "Current above normal or grounded circuit", 7: "Mechanical system not responding or out of adjustment",
    8: "Abnormal frequency, pulse width or period", 9: "Abnormal update rate", 10: "Abnormal rate of change",
    11: "Root cause not known", 12: "Bad intelligent device or component", 13: "Out of calibration",
    14: "Special instructions", 15: "Above normal range - least severe", 16: "Above normal range - moderately severe",
    17: "Below normal range - least severe", 18: "Below normal range - moderately severe",
    19: "Received network data in error", 20: "Data drifted high", 21: "Data drifted low",
    31: "Condition exists",
}
# A few SPNs, for a readable fault list without a database.
SPN_TEXT = {
    84: "Wheel-based vehicle speed", 91: "Accelerator pedal position", 94: "Fuel delivery pressure",
    100: "Engine oil pressure", 102: "Intake manifold pressure", 105: "Intake manifold temperature",
    110: "Engine coolant temperature", 132: "Mass air flow", 158: "Keyswitch battery potential",
    168: "Battery potential", 190: "Engine speed", 520: "Actual retarder torque", 639: "J1939 network #1",
    651: "Injector cylinder #1", 1569: "Engine protection torque derate", 3226: "Aftertreatment NOx out",
}


@dataclass(frozen=True)
class J1939Dtc:
    spn: int
    fmi: int
    occurrences: int = 1
    conversion_method: int = 0

    def text(self) -> str:
        spn = SPN_TEXT.get(self.spn, f"SPN {self.spn}")
        return f"{spn}: {FMI_TEXT.get(self.fmi, f'FMI {self.fmi}')}"


@dataclass
class DiagnosticMessage:
    lamps: tuple = (3, 3, 3, 3)           # LAMPS' states: 0 off, 1 on, 2 error, 3 not available
    flash: tuple = (3, 3, 3, 3)           # 0 slow flash, 1 fast flash, 2 reserved, 3 no flash
    dtcs: list = field(default_factory=list)

    def lamps_text(self) -> str:
        on = [name for name, state in zip(LAMPS, self.lamps) if state == 1]
        return ", ".join(on) if on else "no lamp on"


def _pairs(byte: int) -> tuple:
    return tuple((byte >> shift) & 0x3 for shift in (6, 4, 2, 0))


def parse_dm(data: bytes) -> DiagnosticMessage:
    """A DM1 or DM2 message. The "no DTC" entry (SPN 0, FMI 0) and padding (all ones) are left out."""
    data = bytes(data)
    if len(data) < 2:
        raise ValueError("a DM1/DM2 message has at least the two lamp bytes")
    message = DiagnosticMessage(_pairs(data[0]), _pairs(data[1]))
    for offset in range(2, len(data) - 3, 4):
        b2, b3, b4, b5 = data[offset:offset + 4]
        if (b2, b3, b4, b5) == (0xFF, 0xFF, 0xFF, 0xFF):
            continue
        spn = b2 | (b3 << 8) | ((b4 >> 5) << 16)
        fmi = b4 & 0x1F
        if spn == 0 and fmi == 0:
            continue
        message.dtcs.append(J1939Dtc(spn, fmi, b5 & 0x7F, b5 >> 7))
    return message


def build_dm(dtcs, lamps=(0, 0, 0, 0), flash=(3, 3, 3, 3)) -> bytes:
    """A DM1 or DM2 message; with no DTC, the "no DTC" entry, eight bytes in all."""
    def pack(pairs):
        return sum((state & 0x3) << shift for state, shift in zip(pairs, (6, 4, 2, 0)))
    data = bytes([pack(lamps), pack(flash)])
    if not dtcs:
        return data + b"\x00\x00\x00\x00\xff\xff"
    for dtc in dtcs:
        if not 0 <= dtc.spn < (1 << 19) or not 0 <= dtc.fmi < 32:
            raise ValueError(f"SPN {dtc.spn} FMI {dtc.fmi} out of range")
        data += bytes([dtc.spn & 0xFF, (dtc.spn >> 8) & 0xFF, ((dtc.spn >> 16) << 5) | dtc.fmi,
                       ((dtc.conversion_method & 1) << 7) | min(dtc.occurrences, 0x7E)])
    return data
