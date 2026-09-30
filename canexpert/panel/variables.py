"""
Structured variables: records of typed fields and arrays that a panel keeps, written the way they are thought of,

    Calib Data (memory 0x20001000, little-endian)
    * uint32 temperature
    * uint32 Axis
    * uint32 FOC[32]

or pasted from a C header (struct Calib_Data { uint32_t temperature; ... };). A header line names the variable,
with, in brackets, where it lives in the ECU - DID 0x0110 (ReadDataByIdentifier / WriteDataByIdentifier) or
memory 0x20001000 (ReadMemoryByAddress / WriteMemoryByAddress) - and its byte order (big-endian, the default,
or little-endian). Each field line has a type, a name and, for an array, [count]. Fields are packed: padding
the ECU keeps is written as a field (uint8 pad[3]).

Types: uint8 uint16 uint32 uint64, int8 int16 int32 int64, float32 float64, bool, char (char name[16] is a text
of 16 bytes) - and their C names (uint32_t, unsigned int, float, double...).

The panel shows a variable in a Variable List control, and a control named after a field ("Calib Data.FOC[3]")
shows that field; the script reaches it as api.var("Calib Data") (Variable).
"""
from __future__ import annotations

import difflib
import re
import struct
from dataclasses import dataclass, field

TYPES = {"uint8": ("B", 1), "uint16": ("H", 2), "uint32": ("I", 4), "uint64": ("Q", 8),
         "int8": ("b", 1), "int16": ("h", 2), "int32": ("i", 4), "int64": ("q", 8),
         "float32": ("f", 4), "float64": ("d", 8), "bool": ("?", 1), "char": ("c", 1)}
ALIASES = {"byte": "uint8", "u8": "uint8", "u16": "uint16", "u32": "uint32", "u64": "uint64",
           "i8": "int8", "i16": "int16", "i32": "int32", "i64": "int64", "s8": "int8", "s16": "int16",
           "s32": "int32", "s64": "int64", "f32": "float32", "f64": "float64", "float": "float32",
           "double": "float64", "real": "float32", "boolean": "bool", "_bool": "bool",
           "uint8_t": "uint8", "uint16_t": "uint16", "uint32_t": "uint32", "uint64_t": "uint64",
           "int8_t": "int8", "int16_t": "int16", "int32_t": "int32", "int64_t": "int64",
           "unsigned char": "uint8", "signed char": "int8", "short": "int16", "unsigned short": "uint16",
           "int": "int32", "unsigned": "uint32", "unsigned int": "uint32", "long": "int32",
           "unsigned long": "uint32", "long long": "int64", "unsigned long long": "uint64"}
BIG, LITTLE = "big", "little"
MAX_COUNT = 4096                      # the most elements an array may have
NAME = re.compile(r"[A-Za-z_]\w*$")
FIELD = re.compile(r"^(?P<type>(?:(?:unsigned|signed|long|short)\s+)*[A-Za-z_]\w*)\s+(?P<name>[A-Za-z_]\w*)"
                   r"\s*(?:\[\s*(?P<count>[^\]]*)\])?\s*;?\s*$")
C_STRUCT = re.compile(r"^(?:typedef\s+)?struct\s+(?P<name>[A-Za-z_]\w*)?\s*\{?\s*$")
PATH = re.compile(r"^(?P<name>[A-Za-z_]\w*)(?:\[(?P<index>\d+)\])?$")


def canonical_type(text: str) -> str | None:
    """uint32, uint32_t, 'unsigned int' -> uint32; None for a type it does not know."""
    word = " ".join(str(text).lower().split())
    return word if word in TYPES else ALIASES.get(word)


def suggest(word, candidates) -> str:
    matches = difflib.get_close_matches(str(word).lower(), [str(c).lower() for c in candidates], n=1, cutoff=0.6)
    if not matches:
        return ""
    original = next(c for c in candidates if str(c).lower() == matches[0])
    return f" Did you mean {original}?"


@dataclass
class Field:
    name: str
    type: str                         # a key of TYPES
    count: int | None = None          # an array's length; None for one value
    line: int = 0                     # where it is written (1-based), for the messages

    @property
    def size(self) -> int:
        return TYPES[self.type][1] * (self.count or 1)

    @property
    def text(self) -> bool:
        """char[N]: a text of N bytes, not N characters one by one."""
        return self.type == "char" and self.count is not None

    def default(self):
        zero = {"bool": False, "char": ""}.get(self.type, 0.0 if self.type.startswith("float") else 0)
        if self.text or self.count is None:
            return zero
        return [zero] * self.count

    def declaration(self) -> str:
        return f"{self.type} {self.name}" + (f"[{self.count}]" if self.count is not None else "")


@dataclass
class Structure:
    name: str
    fields: list = field(default_factory=list)
    byte_order: str = BIG
    did: int | None = None
    address: int | None = None
    line: int = 0

    @property
    def size(self) -> int:
        return sum(item.size for item in self.fields)

    def field(self, name) -> Field | None:
        return next((item for item in self.fields if item.name == name), None)

    def where(self) -> str:
        """DID 0x0110, memory 0x20001000, or "" for a variable only the script fills."""
        if self.did is not None:
            return f"DID 0x{self.did:04X}"
        return f"memory 0x{self.address:08X}" if self.address is not None else ""

    def describe(self) -> str:
        """Calib Data: 136 bytes, memory 0x20001000, little-endian."""
        parts = [f"{self.size} byte{'s' if self.size != 1 else ''}", self.where(), f"{self.byte_order}-endian"]
        return f"{self.name}: " + ", ".join(part for part in parts if part)

    def defaults(self) -> dict:
        return {item.name: item.default() for item in self.fields}

    def paths(self) -> list[str]:
        """Every value a panel control can show: temperature, FOC[0] ... FOC[31]; a text as one."""
        names = []
        for item in self.fields:
            if item.count is None or item.text:
                names.append(item.name)
            else:
                names += [f"{item.name}[{index}]" for index in range(item.count)]
        return names

    def flatten(self, values: dict):
        """(path, value) for every value a control can show: temperature, FOC[0] ... FOC[31]."""
        for item in self.fields:
            value = values.get(item.name, item.default())
            if item.count is None or item.text:
                yield item.name, value
            else:
                for index, element in enumerate(value):
                    yield f"{item.name}[{index}]", element

    def decode(self, data: bytes) -> dict:
        """The values the first size bytes of data hold. ValueError when there are fewer."""
        data = bytes(data)
        if len(data) < self.size:
            raise ValueError(f"{self.name} takes {self.size} bytes; {len(data)} came")
        order = "<" if self.byte_order == LITTLE else ">"
        values, offset = {}, 0
        for item in self.fields:
            code, width = TYPES[item.type]
            if item.text:
                raw = data[offset:offset + item.count]
                values[item.name] = raw.split(b"\x00", 1)[0].decode("latin-1")
            elif item.type == "char":
                values[item.name] = data[offset:offset + 1].decode("latin-1")
            else:
                unpacked = struct.unpack_from(f"{order}{item.count or 1}{code}", data, offset)
                values[item.name] = list(unpacked) if item.count is not None else unpacked[0]
            offset += item.size
        return values

    def encode(self, values: dict) -> bytes:
        """values (missing ones as their defaults) as the ECU keeps them. ValueError for one that does not fit."""
        order = "<" if self.byte_order == LITTLE else ">"
        out = bytearray()
        for item in self.fields:
            value = values.get(item.name, item.default())
            code, _width = TYPES[item.type]
            try:
                if item.text or item.type == "char":
                    raw = str(value).encode("latin-1")
                    size = item.count or 1
                    if len(raw) > size:
                        raise ValueError(f"{len(raw)} characters, and it holds {size}")
                    out += raw.ljust(size, b"\x00")
                elif item.count is not None:
                    items = list(value)
                    if len(items) != item.count:
                        raise ValueError(f"{len(items)} values, and it holds {item.count}")
                    out += struct.pack(f"{order}{item.count}{code}", *[convert(item.type, v) for v in items])
                else:
                    out += struct.pack(f"{order}{code}", convert(item.type, value))
            except (struct.error, TypeError, ValueError, UnicodeEncodeError) as exc:
                raise ValueError(f"{self.name}.{item.name}: {exc}") from None
        return bytes(out)

    def text(self) -> str:
        """The variable written as parse_variables reads it."""
        options = [item for item in (self.where(), "little-endian" if self.byte_order == LITTLE else "") if item]
        lines = [self.name + (f" ({', '.join(options)})" if options else "")]
        lines += [f"* {item.declaration()}" for item in self.fields]
        return "\n".join(lines)


def convert(type_name: str, value):
    """A value as a field of that type holds it. ValueError when it cannot (text that is no number, out of range)."""
    if type_name == "bool":
        if isinstance(value, str):
            text = value.strip().lower()
            if text not in ("0", "1", "true", "false", "on", "off", "yes", "no"):
                raise ValueError(f"{value!r} is neither true nor false")
            return text in ("1", "true", "on", "yes")
        return bool(value)
    if type_name == "char":
        return str(value)
    if type_name.startswith("float"):
        return float(value)
    if isinstance(value, str):
        value = int(value.strip(), 0)
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{value} is not a whole number")
        value = int(value)
    else:
        value = int(value)
    code, width = TYPES[type_name]
    low, high = (0, (1 << (8 * width)) - 1) if code.isupper() else (-(1 << (8 * width - 1)), (1 << (8 * width - 1)) - 1)
    if not low <= value <= high:
        raise ValueError(f"{value} is outside {type_name} ({low} to {high})")
    return value


def split_path(path: str):
    """'FOC[3]' -> ('FOC', 3); 'temperature' -> ('temperature', None). ValueError otherwise."""
    match = PATH.match(str(path).strip())
    if not match:
        raise ValueError(f"{path!r} is not a field (name or name[index])")
    return match.group("name"), int(match.group("index")) if match.group("index") is not None else None


def parse_variables(text: str):
    """(the structures written in text, the problems: [(line, message)]). Every problem is kept - a structure
    with one is still returned as far as it could be read."""
    structures, problems = [], []
    current = None
    for number, raw in enumerate(str(text or "").splitlines(), 1):
        line = re.sub(r"(//|#).*$", "", raw)
        line = re.sub(r"/\*.*?\*/", "", line).strip()
        if not line or line in ("{", "}", "};") or re.fullmatch(r"}\s*\w*\s*;?", line):
            continue
        body = re.sub(r"^[*\-•]\s*", "", line)
        match = FIELD.match(body)
        header = C_STRUCT.match(line)
        if header and (header.group("name") or line.startswith(("struct", "typedef"))):
            current = _structure(header.group("name") or "", "", number, structures, problems)
            continue
        if match and (canonical_type(match.group("type")) is not None or line[0] in "*-•" or body.endswith(";")):
            if current is None:
                problems.append((number, f"The field {match.group('name')} has no variable above it: write the "
                                         "variable's name on a line of its own first"))
                continue
            _field(match, number, current, problems)
            continue
        if line[0] in "*-•":
            problems.append((number, f"{body!r} is not a field: write type name, or type name[count]"))
            continue
        name, _bracket, options = line.partition("(")
        current = _structure(name.strip().rstrip(":").strip(), options.rsplit(")", 1)[0] if _bracket else "", number,
                             structures, problems)
        if _bracket and ")" not in options:
            problems.append((number, "The options are not closed: a ) is missing"))
    for item in structures:
        if not item.fields:
            problems.append((item.line, f"{item.name} has no field: write them under it, one a line, * uint32 name"))
    return structures, problems


def _structure(name, options, number, structures, problems):
    if not name:
        problems.append((number, "A variable needs a name"))
        name = f"Variable {len(structures) + 1}"
    for character in ".[]":
        if character in name:
            problems.append((number, f"{name!r}: a variable's name has no {character} - fields are reached as "
                                     "Name.field"))
    if any(item.name == name for item in structures):
        problems.append((number, f"There are two variables named {name}"))
    structure = Structure(name, line=number)
    for option in (part.strip() for part in options.split(",") if part.strip()):
        words = option.lower().replace("_", "-").split()
        try:
            if words[0] == "did" and len(words) == 2:
                structure.did = int(words[1], 0)
                if not 0 <= structure.did <= 0xFFFF:
                    raise ValueError
            elif words[0] in ("memory", "address", "at") and len(words) == 2 or option.startswith("@"):
                structure.address = int(words[-1].lstrip("@"), 0)
                if not 0 <= structure.address <= 0xFFFFFFFF:
                    raise ValueError
            elif words[0] in ("little-endian", "little", "le", "intel"):
                structure.byte_order = LITTLE
            elif words[0] in ("big-endian", "big", "be", "motorola"):
                structure.byte_order = BIG
            else:
                problems.append((number, f"{option!r} is not an option: write DID 0x0110, memory 0x20001000, "
                                         "little-endian or big-endian"))
        except ValueError:
            problems.append((number, f"{option!r}: the number is not one (write it in hex, 0x0110, or decimal), "
                                     "or it is too large"))
    if structure.did is not None and structure.address is not None:
        problems.append((number, f"{name} has a DID and a memory address: keep one"))
    structures.append(structure)
    return structure


def _field(match, number, structure, problems):
    type_text, name, count_text = match.group("type"), match.group("name"), match.group("count")
    type_name = canonical_type(type_text)
    if type_name is None:
        problems.append((number, f"{type_text} is not a type.{suggest(type_text, list(TYPES) + list(ALIASES))} "
                                 f"The types: {', '.join(TYPES)}"))
        return
    count = None
    if count_text is not None:
        try:
            count = int(count_text.strip(), 0)
        except ValueError:
            problems.append((number, f"[{count_text}]: an array's length is a number, such as [32]"))
            return
        if not 1 <= count <= MAX_COUNT:
            problems.append((number, f"[{count}]: an array holds 1 to {MAX_COUNT} values"))
            return
    if structure.field(name) is not None:
        problems.append((number, f"{structure.name} has two fields named {name}"))
        return
    structure.fields.append(Field(name, type_name, count, number))


def variables_text(structures) -> str:
    return "\n\n".join(item.text() for item in structures)


class Variable:
    """A structured variable as a panel script has it (api.var("Calib Data")): its fields as attributes
    (calib.temperature, calib.FOC[3]) or items (calib["FOC"][3]); read() and write() with the ECU (its DID or
    memory address); bytes() and decode(data) for any other way. Every change shows on the panel: in the
    Variable List, and in a control named after the field ("Calib Data.FOC[3]")."""

    def __init__(self, structure: Structure, functions=None, changed=None):
        object.__setattr__(self, "_structure", structure)
        object.__setattr__(self, "_functions", functions)          # the UDS functions: RDBI, RMBA...
        object.__setattr__(self, "_changed", changed or (lambda name, value: None))
        object.__setattr__(self, "_values", {})
        self.decode_values(structure.defaults(), quiet=True)

    # --- the values -------------------------------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._structure.name

    @property
    def structure(self) -> Structure:
        return self._structure

    def __getattr__(self, name):
        values = object.__getattribute__(self, "_values")
        if name in values:
            return values[name]
        raise AttributeError(f"{self._structure.name} has no field {name}."
                             f"{suggest(name, [item.name for item in self._structure.fields])}")

    def __setattr__(self, name, value):
        self.set(name, value)

    def __getitem__(self, path):
        field_name, index = split_path(path)
        value = self.__getattr__(field_name)
        return value if index is None else value[index]

    def __setitem__(self, path, value):
        self.set(path, value)

    def get(self, path, default=None):
        try:
            return self[path]
        except (AttributeError, IndexError, ValueError):
            return default

    def set(self, path, value, quiet=False):
        """Set a field, or an element (FOC[3]): converted to its type, and shown on the panel."""
        field_name, index = split_path(path)
        item = self._structure.field(field_name)
        if item is None:
            raise AttributeError(f"{self._structure.name} has no field {field_name}."
                                 f"{suggest(field_name, [f.name for f in self._structure.fields])}")
        if index is None and item.count is not None and not item.text:
            values = list(value)
            if len(values) != item.count:
                raise ValueError(f"{self.name}.{field_name} holds {item.count} values; {len(values)} came")
            array = self._values[field_name]
            for position, element in enumerate(values):
                list.__setitem__(array, position, convert(item.type, element))
            if not quiet:
                for position in range(item.count):
                    self._changed(f"{self.name}.{field_name}[{position}]", array[position])
            return
        if index is not None:
            if item.count is None or item.text:
                raise ValueError(f"{self.name}.{field_name} is not an array")
            if not 0 <= index < item.count:
                raise IndexError(f"{self.name}.{field_name}[{index}]: it has {item.count} values, [0] to "
                                 f"[{item.count - 1}]")
            converted = convert(item.type, value)
            list.__setitem__(self._values[field_name], index, converted)
        else:
            converted = str(value) if item.text else convert(item.type, value)
            if item.text and len(converted.encode("latin-1", errors="replace")) > item.count:
                raise ValueError(f"{self.name}.{field_name} holds {item.count} characters")
            self._values[field_name] = converted
        if not quiet:
            self._changed(f"{self.name}.{path}", converted)

    def values(self) -> dict:
        """A copy of every field's value."""
        return {name: list(value) if isinstance(value, list) else value for name, value in self._values.items()}

    def decode_values(self, values: dict, quiet=False):
        for item in self._structure.fields:
            value = values.get(item.name, item.default())
            if item.count is not None and not item.text:
                array = self._values.get(item.name)
                if not isinstance(array, _Array):
                    array = self._values[item.name] = _Array(self, item.name)
                list.__init__(array, value)          # in place: a script holding calib.FOC sees the new values
            else:
                self._values[item.name] = value
        if not quiet:
            self._changed(self.name, self.values())

    def decode(self, data: bytes):
        """Take the values the bytes hold (the first size bytes), and show them."""
        self.decode_values(self._structure.decode(data))
        return self

    def bytes(self) -> bytes:
        """The values as the ECU keeps them."""
        return self._structure.encode(self._values)

    # --- the ECU ----------------------------------------------------------------------------------------------

    def read(self):
        """Read the variable from the ECU - ReadDataByIdentifier or ReadMemoryByAddress - and show it. Returns
        the UDS result: true when it was read."""
        functions = self._uds()
        structure = self._structure
        if structure.did is not None:
            result = functions.RDBI(structure.did)
        else:
            result = functions.RMBA(structure.address, structure.size)
        if result:
            self.decode(result.data)
        return result

    def write(self):
        """Write the variable to the ECU - WriteDataByIdentifier or WriteMemoryByAddress. Returns the UDS result:
        true when the ECU took it."""
        functions = self._uds()
        structure = self._structure
        if structure.did is not None:
            return functions.WDBI(structure.did, self.bytes())
        return functions.WMBA(structure.address, self.bytes())

    def _uds(self):
        if self._structure.did is None and self._structure.address is None:
            raise ValueError(f"{self.name} has no DID and no memory address to read or write: give it one, "
                             f"{self.name} (DID 0x0110), or use decode() and bytes()")
        if self._functions is None:
            raise ValueError("Not connected to an ECU")
        return self._functions

    def __repr__(self):
        return f"{self.name}({', '.join(f'{k}={v!r}' for k, v in self._values.items())})"


class _Array(list):
    """An array field: setting an element converts it and shows it on the panel."""

    def __init__(self, variable, name):
        super().__init__()
        self._variable, self._name = variable, name

    def __setitem__(self, index, value):
        if isinstance(index, slice):
            raise TypeError("set the elements one by one, or the whole array")
        self._variable.set(f"{self._name}[{index if index >= 0 else len(self) + index}]", value)
