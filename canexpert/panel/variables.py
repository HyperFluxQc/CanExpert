"""
Structured variables: records of typed fields and arrays that a panel keeps, written the way they are thought of,

    Calib Data (memory 0x20001000, little-endian)
    * uint32 temperature
    * uint32 Axis
    * uint32 FOC[32]

or in braces, as in C - on one line or on several:

    MyList (DID 0x0110) { uint32 data1; uint8 data2; }

or pasted from a C header (struct Calib_Data { uint32_t temperature; ... };, typedef struct { ... } Name;). The
name comes first, with, in brackets, where the variable lives in the ECU - DID 0x0110 (ReadDataByIdentifier /
WriteDataByIdentifier) or memory 0x20001000 (ReadMemoryByAddress / WriteMemoryByAddress) - and its byte order
(big-endian, the default, or little-endian). Each field has a type, a name and, for an array, [count]; a line
ends a field, and so does a ; (uint8 a, b; is two). Fields are packed: padding the ECU keeps is written as a
field (uint8 pad[3]). Comments: // and # to the end of the line, /* ... */.

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
# A declaration: a type (unsigned int...) and one name or more, each with its [count]: uint8 a, b[4]
DECLARATION = re.compile(r"^(?P<type>(?:(?:unsigned|signed|long|short)\s+)*[A-Za-z_]\w*)\s+(?P<names>[A-Za-z_].*)$")
DECLARATOR = re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*(?:\[\s*(?P<count>[^\]]*)\])?$")
QUALIFIERS = re.compile(r"^(?:(?:const|volatile|static)\s+)+")
STRUCT = re.compile(r"^(?:typedef\s+)?struct\b\s*")
C_DECLARATION = re.compile(r"^(?:typedef|struct|union|enum)\b")     # outside a variable's braces, ended by ;
STATEMENT = re.compile(r"[{};]|[^{};]+")
COMMENT = re.compile(r"/\*.*?(?P<closed>\*/|\Z)|//[^\n]*|#[^\n]*", re.S)
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
    reader = _Reader(structures, problems)
    text = re.sub(r"\r\n?", "\n", str(text or ""))
    for number, line in enumerate(_without_comments(text, problems).split("\n"), 1):
        for statement, end in _statements(line):
            reader.read(number, statement.strip(), end)
    reader.finish()
    for item in structures:
        if not item.fields:
            problems.append((item.line, f"{item.name} has no field: write them under it, one a line (* uint32 name), "
                                        f"or in braces: {item.name} {{ uint32 name; }}"))
    return structures, problems


def _without_comments(text, problems):
    """The text without its comments - // and # to the end of the line, /* ... */ over lines too - each line where
    it was. A /* not closed is said."""
    def blank(match):
        comment = match.group(0)
        if comment.startswith("/*") and not match.group("closed"):
            problems.append((text.count("\n", 0, match.start()) + 1, "This /* comment is not closed: a */ is missing"))
        return "\n" * comment.count("\n") or " "
    return COMMENT.sub(blank, text)


def _statements(line):
    """A line's statements: (text, what ends it) - a {, a }, a ; or the end of the line ("\n")."""
    text = ""
    for piece in STATEMENT.findall(line):
        if piece in ("{", "}", ";"):
            yield text, piece
            text = ""
        else:
            text = piece
    if text.strip():
        yield text, "\n"


def _declaration(text):
    """(type, [(name, count text or None)]) for a field's declaration - "uint8 a, b[4]" - or None."""
    match = DECLARATION.match(QUALIFIERS.sub("", text))
    if match is None:
        return None
    names = [DECLARATOR.match(part.strip()) for part in match.group("names").split(",")]
    if not all(names):
        return None
    return match.group("type"), [(name.group("name"), name.group("count")) for name in names]


_PASSED_OVER = object()               # what a } closed when it was no variable's: nothing to name


class _Reader:
    """parse_variables, statement by statement: a variable's name (with its options), then its fields - one a line,
    or between braces."""

    def __init__(self, structures, problems):
        self.structures, self.problems = structures, problems
        self.current = None           # the variable fields go to
        self.pending = None           # a variable named on a line of its own, no field yet: a { may follow
        self.depth = 0                # braces open
        self.block = None             # the variable whose braces are open; None for braces skipped
        self.opened = 0               # the line of the { open
        self.unnamed = []             # typedef struct { ... } Name; - waiting for the name after their }
        self.closed = (None, 0)       # the variable a } has just closed, and its line: "} Name;" may name it

    def read(self, number, text, end):
        just_closed, line = self.closed
        self.closed = (None, 0)
        if end == "{":
            self.open(number, text)
            return
        if line == number and just_closed is not None:      # } Name; - only an unnamed struct takes the name
            if just_closed in self.unnamed and text:
                self.name(just_closed, text, number)
            if end == "}":
                self.close(number)
            return
        if text:
            self.statement(number, text, end)
        if end == "}":
            self.close(number)

    def open(self, number, text):
        if self.depth:
            self.depth += 1
            if self.depth == 2 and self.block is not None:
                inner = self.header_name(text) or "this"
                self.problems.append((number, f"{inner} is inside {self.block.name}: a variable holds fields, not other "
                                              f"variables - write {inner} as a variable of its own"))
            return
        self.depth, self.opened = 1, number
        if text:
            self.block = self.header(number, text)
        elif self.pending is not None:                      # MyList on a line, then {
            self.block = self.pending
        else:
            self.block = None
            self.problems.append((number, "A { with no variable's name before it: write the name first, "
                                          "MyList { uint32 data1; uint8 data2; }"))
        self.current, self.pending = self.block, None

    def close(self, number):
        if not self.depth:
            self.problems.append((number, "A } with no { before it"))
            self.closed = (_PASSED_OVER, number)
            return
        self.depth -= 1
        # "} Name;" names an unnamed struct; after any other }, what follows on the line up to a ; is passed over
        self.closed = (self.block if not self.depth and self.block is not None else _PASSED_OVER, number)
        if not self.depth:
            self.block = self.current = None

    def statement(self, number, text, end):
        if self.depth > 1 or self.depth and self.block is None:
            return                                          # inside braces skipped: said once, at their {
        bullet = text[0] in "*-•"
        body = re.sub(r"^[*\-•]\s*", "", text)
        declaration = _declaration(body)
        if self.depth:                                      # between braces: fields only
            if declaration is None:
                self.problems.append((number, _not_a_field(body)))
            else:
                self.fields(number, declaration)
            return
        if end == ";" and C_DECLARATION.match(body):        # struct Motor; typedef uint16_t speed_t; - nothing to keep
            return
        if declaration is not None and (canonical_type(declaration[0]) is not None or bullet or end == ";"):
            if self.current is None:
                self.problems.append((number, f"The field {declaration[1][0][0]} has no variable above it: write the "
                                              "variable's name on a line of its own first"))
                return
            self.fields(number, declaration)
            return
        if bullet:
            self.problems.append((number, f"{body!r} is not a field: write type name, or type name[count]"))
            return
        self.current = self.pending = self.header(number, text)

    def fields(self, number, declaration):
        type_text, declarators = declaration
        for name, count in declarators:
            _field(type_text, name, count, number, self.current, self.problems, self.structures)
        self.pending = None

    @staticmethod
    def header_name(text):
        """The name in a variable's first line: MyList (DID 0x0110), struct MyList, MyList:."""
        struct = STRUCT.match(text)
        name = (text[struct.end():] if struct else text).partition("(")[0]
        return name.strip().rstrip(":=").strip()

    def header(self, number, text):
        struct = STRUCT.match(text)
        name, bracket, options = (text[struct.end():] if struct else text).partition("(")
        name = name.strip().rstrip(":=").strip()
        structure = _structure(name, options.rsplit(")", 1)[0] if bracket else "", number, self.structures,
                               self.problems, named_later=bool(struct))
        if struct and not name:
            self.unnamed.append(structure)
        if bracket and ")" not in options:
            self.problems.append((number, "The options are not closed: a ) is missing"))
        return structure

    def name(self, structure, text, number):
        """typedef struct { ... } Name;: the name after the }."""
        match = re.match(r"[A-Za-z_]\w*", text)
        if match is None:
            return
        self.unnamed.remove(structure)
        if any(item.name == match.group(0) for item in self.structures):
            self.problems.append((number, f"There are two variables named {match.group(0)}"))
        structure.name = match.group(0)

    def finish(self):
        if self.depth:
            what = f"{self.block.name}: the {{" if self.block is not None else "This {"
            self.problems.append((self.opened, f"{what} is not closed - a }} is missing"))
        for structure in self.unnamed:
            self.problems.append((structure.line, "A variable needs a name: typedef struct { ... } Name;"))


def _not_a_field(text):
    """Why a statement between a variable's braces is not a field."""
    if STRUCT.match(text) or text.split()[0] in ("union", "enum"):
        return f"{text!r}: a variable holds fields, not other variables - write it as a variable of its own"
    if re.search(r"\w\s*:\s*\d", text):
        return f"{text!r}: bit fields are not read - write the byte that holds them, uint8 flags"
    return f"{text!r} is not a field: write type name, or type name[count]"


def _structure(name, options, number, structures, problems, named_later=False):
    if not name and not named_later:
        problems.append((number, "A variable needs a name"))
    if not name:
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


def _field(type_text, name, count_text, number, structure, problems, structures=()):
    type_name = canonical_type(type_text)
    if type_name is None:
        if any(item.name == type_text for item in structures):
            problems.append((number, f"{type_text} is a variable, not a type: a variable holds fields of the types "
                                     f"{', '.join(TYPES)} - not other variables"))
        else:
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
