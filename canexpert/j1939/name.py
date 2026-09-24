"""
The J1939 NAME (J1939-81): the 64 bits a node claims its address with. The lower NAME wins an address both
claim, so the fields are ordered by how much they matter: the arbitrary-address-capable bit on top, the
identity number at the bottom. Sent little-endian in the Address Claimed message.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# (field, bit, width), lowest bit first
FIELDS = (("identity", 0, 21), ("manufacturer", 21, 11), ("ecu_instance", 32, 3), ("function_instance", 35, 5),
          ("function", 40, 8), ("vehicle_system", 49, 7), ("vehicle_system_instance", 56, 4),
          ("industry_group", 60, 3), ("arbitrary_address_capable", 63, 1))
INDUSTRY_GROUPS = {0: "Global", 1: "On-highway", 2: "Agricultural and forestry", 3: "Construction",
                   4: "Marine", 5: "Industrial, process control, stationary"}
# Functions 0-127 are the same in every industry group (J1939 Appendix B).
FUNCTIONS = {0: "Engine", 1: "Auxiliary power unit", 2: "Electric propulsion control", 3: "Transmission",
             4: "Battery pack monitor", 5: "Shift control", 6: "Power take-off", 7: "Axle - steering",
             8: "Axle - drive", 9: "Brakes - system controller", 10: "Brakes - steer axle",
             11: "Brakes - drive axle", 12: "Retarder - engine", 13: "Retarder - driveline",
             14: "Cruise control", 15: "Fuel system", 16: "Steering controller", 17: "Suspension - steer axle",
             18: "Suspension - drive axle", 19: "Instrument cluster", 20: "Trip recorder",
             21: "Cab climate control", 22: "Aerodynamic control", 23: "Vehicle navigation",
             24: "Vehicle security", 25: "Network interconnect ECU", 26: "Body controller",
             27: "Power take-off (secondary)", 28: "Off vehicle gateway", 29: "Virtual terminal",
             30: "Management computer", 31: "Propulsion battery charger", 32: "Headway controller",
             33: "System monitor", 34: "Hydraulic pump controller", 35: "Suspension - system controller",
             36: "Pneumatic - system controller", 37: "Cab controller", 38: "Tire pressure control",
             39: "Ignition control module", 40: "Seat control", 41: "Lighting - operator controls",
             60: "Engine emission aftertreatment", 129: "Off-board diagnostic-service tool"}


@dataclass
class Name:
    identity: int = 0
    manufacturer: int = 0
    ecu_instance: int = 0
    function_instance: int = 0
    function: int = 0
    vehicle_system: int = 0
    vehicle_system_instance: int = 0
    industry_group: int = 0
    arbitrary_address_capable: int = 0

    def to_int(self) -> int:
        value = 0
        for field, bit, width in FIELDS:
            number = int(getattr(self, field))
            if not 0 <= number < (1 << width):
                raise ValueError(f"NAME {field} {number} does not fit {width} bits")
            value |= number << bit
        return value

    @classmethod
    def from_int(cls, value: int) -> "Name":
        return cls(**{field: (value >> bit) & ((1 << width) - 1) for field, bit, width in FIELDS})

    def to_bytes(self) -> bytes:
        return self.to_int().to_bytes(8, "little")

    @classmethod
    def from_bytes(cls, data: bytes) -> "Name":
        return cls.from_int(int.from_bytes(bytes(data[:8]).ljust(8, b"\0"), "little"))

    def function_text(self) -> str:
        return FUNCTIONS.get(self.function, f"function {self.function}")

    def describe(self) -> str:
        """"Engine #0, manufacturer 1234, identity 0x0ABCD, on-highway"."""
        group = INDUSTRY_GROUPS.get(self.industry_group, f"industry group {self.industry_group}")
        return (f"{self.function_text()} #{self.function_instance}, manufacturer {self.manufacturer}, "
                f"identity 0x{self.identity:05X}, {group}")

    def as_dict(self) -> dict:
        return asdict(self)
