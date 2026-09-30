"""
Checking a panel before it runs: its XML, its controls, its DBC bindings and its script. Each problem says
where it is - the file, line and column, and the line itself - what is wrong, in words, and what was
probably meant, so a typo shows up as that and not as a failed Connect.

check_panel_file() checks a database on disk with its script (the Form Designer opening one, the main window
connecting); check_panel() checks XML and a script held in memory (the Form Designer's Check panel and Test
panel, where a problem names its control rather than a line of XML nobody sees).

An error is what stops the panel from loading or its script from starting; a warning, what will not work as
written - a control left out, a property ignored, a signal or a control the script names that is not there.
"""
import ast
import builtins
import difflib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from xml.parsers import expat

from PyQt5.QtGui import QColor

from canexpert.panel.controls import APPEARANCE, CONTROLS, READ_ONLY
from canexpert.panel.database import parse_can_id, parse_hex_bytes, parse_widget
from canexpert.panel.runtime import script_globals
from canexpert.panel.variables import parse_variables, split_path
from canexpert.panel.view import control_key

ERROR, WARNING = "error", "warning"
FORM, SCRIPT, VARIABLES = "form", "script", "variables"    # where a problem is: the XML and its controls, the
#                                                             Python script, or the structured variables

# What a panel database holds besides its controls, with the attributes each one takes.
STRUCTURE = {"application_database": ("name", "dbc_path"), "description": (), "pages": (), "page": ("name",),
             "dbc_path": (), "variables": ()}
# The attributes of every control (parse_widget, the Form Designer), besides each one's own properties.
COMMON = ("type", "kind", "id", "name", "label", "text", "x", "y", "width", "height", "binding_type",
          "binding_value", "variable", "handler", "value_type", "unit", "min", "max", "can_id", "byte_start",
          "byte_length", "byte", "bit", "scale", "offset", "data")
KNOWN_ATTRIBUTES = {*COMMON, READ_ONLY.key, *(prop.key for prop in APPEARANCE),
                    *(prop.key for control in CONTROLS.values() for prop in control.props)}
BINDING_TYPES = ("script", "dbc")
TRUE_FALSE = ("1", "true", "yes", "on", "0", "false", "no", "off")
# What pyflakes finds that goes wrong when the script runs: a name that is not defined (a typo, mostly), a
# format string that cannot be filled... Style - an unused import, say - is left alone.
SCRIPT_MESSAGES = {"UndefinedName", "UndefinedLocal", "UndefinedExport", "DuplicateArgument", "TwoStarredExpressions",
                   "TooManyExpressionsInStarredAssignment", "DefaultExceptNotLast", "StringDotFormatInvalidFormat",
                   "StringDotFormatMissingArgument", "StringDotFormatMixingAutomatic", "PercentFormatInvalidFormat",
                   "PercentFormatMissingArgument", "PercentFormatMixedPositionalAndNamed",
                   "PercentFormatUnsupportedFormatCharacter", "PercentFormatExpectedMapping",
                   "PercentFormatExpectedSequence", "PercentFormatStarRequiresSequence",
                   "PercentFormatPositionalCountMismatch"}
XML_HINTS = {
    expat.errors.XML_ERROR_INVALID_TOKEN: "XML does not allow this character here. Often a quote is missing "
                                          "around a value, or a space between two attributes, or a value holds "
                                          "a & or a < (write &amp; and &lt;).",
    expat.errors.XML_ERROR_UNCLOSED_TOKEN: "A tag or a quoted value is never closed: a \" or a > is missing.",
    expat.errors.XML_ERROR_JUNK_AFTER_DOC_ELEMENT: "Everything belongs between <application_database> and "
                                                   "</application_database>: something follows its end.",
    expat.errors.XML_ERROR_DUPLICATE_ATTRIBUTE: "The same attribute is written twice on one element: keep one.",
    expat.errors.XML_ERROR_UNDEFINED_ENTITY: "An & starts a name XML does not know: write &amp; for an &.",
    expat.errors.XML_ERROR_UNBOUND_PREFIX: "A name has a colon in it: remove the colon, or the part before it.",
    expat.errors.XML_ERROR_SYNTAX: "A panel database is XML: <application_database ...> holding <pages>.",
    expat.errors.XML_ERROR_NO_ELEMENTS: "The file is empty, or ends before its elements are closed.",
}
EXCERPT_WIDTH = 100             # the characters of a long line shown around the place of a problem


@dataclass
class Problem:
    """One thing a check found."""
    severity: str               # ERROR or WARNING
    message: str                # what is wrong
    kind: str = FORM            # FORM or SCRIPT
    path: str = ""              # the file it is in; "" for a form in the Form Designer's memory
    line: int = 0               # 1-based; 0: not known
    column: int = 0             # 1-based; 0: the whole line
    source: str = ""            # that line of the file
    hint: str = ""              # what was probably meant, or how to put it right
    control: str = ""           # the control it is about, as the panel names it
    page: str = ""              # and its page
    position: tuple = ()        # (page, index on the page) of that control, for the Form Designer to select it;
    #                             for the variables, (line,) as the Form Designer's Variables tab shows them

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR

    def where(self) -> str:
        """showcase_2026-09-18.xml, line 12, column 5 - or, for a form in memory: page Main, control Speed."""
        parts = [Path(self.path).name] if self.path else \
            ["script"] if self.kind == SCRIPT else ["Variables"] if self.kind == VARIABLES else []
        if self.line:
            parts.append(f"line {self.line}" + (f", column {self.column}" if self.column else ""))
        elif self.kind == VARIABLES and self.position:
            parts.append(f"line {self.position[0]}")
        elif self.control:
            parts.append(f"page {self.page}, control {self.control}" if self.page else f"control {self.control}")
        return ", ".join(parts)

    def excerpt(self) -> list[str]:
        """The line, without its indentation (and only around the place, when it is long), with a ^ under
        the column."""
        if not self.source.strip():
            return []
        text = self.source.expandtabs(4)
        column = len(self.source[:max(self.column - 1, 0)].expandtabs(4))
        indent = len(text) - len(text.lstrip())
        text, column = text[indent:].rstrip(), max(column - indent, 0)
        if len(text) > EXCERPT_WIDTH:
            start = max(0, min(column - EXCERPT_WIDTH // 3, len(text) - EXCERPT_WIDTH))
            end = start + EXCERPT_WIDTH
            text = ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")
            column += (3 if start else 0) - start
        return [text] + ([" " * column + "^"] if self.column else [])

    def text(self) -> str:
        """The problem in full, as it is copied and logged."""
        where = self.where()
        lines = [f"{self.severity.capitalize()}{' - ' + where if where else ''}: {self.message}"]
        lines += ["    " + line for line in self.excerpt()]
        if self.hint:
            lines.append("    " + self.hint)
        return "\n".join(lines)


def summary(problems) -> str:
    """'2 errors and 1 warning', 'no problems'."""
    errors = sum(problem.is_error for problem in problems)
    warnings = len(problems) - errors
    parts = [f"{count} {word}{'s' if count != 1 else ''}" for count, word in ((errors, "error"), (warnings, "warning"))
             if count]
    return " and ".join(parts) or "no problems"


def typo_distance(a, b) -> int:
    """Letters to add, drop, change or swap with the next one to turn a into b (optimal string alignment)."""
    previous, current = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        before, previous, current = previous, current, [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                current[j] = min(current[j], before[j - 2] + 1)
    return current[len(b)]


def closest(word, candidates):
    """The candidate a typo most likely meant, else None: the same word in other capitals, else the one the
    fewest letters away (a third of the word's letters at most), else the only one it begins (a name cut short),
    else one that looks much the same."""
    word = str(word)
    lower = {}
    for candidate in candidates:
        lower.setdefault(str(candidate).lower(), str(candidate))
    key = word.lower()
    if key in lower:
        return lower[key] if lower[key] != word else None
    if not lower:
        return None
    distance, best = min((typo_distance(key, candidate), candidate) for candidate in lower)
    if distance <= max(1, len(key) // 3):
        return lower[best]
    longer = [candidate for candidate in lower if candidate.startswith(key)]
    if len(key) >= 3 and len(longer) == 1:
        return lower[longer[0]]
    matches = difflib.get_close_matches(key, list(lower), n=1, cutoff=0.8)
    return lower[matches[0]] if matches else None


def did_you_mean(word, candidates, form='"{}"'):
    match = closest(word, candidates)
    return f"Did you mean {form.format(match)}?" if match else ""


def number_hint(value, whole) -> str:
    """What a mistyped number probably was: the letter O or l for a digit, a decimal comma, a space."""
    text = str(value).strip()
    parse = int if whole else float
    candidates = [text.replace("O", "0").replace("o", "0"), text.replace("l", "1").replace("I", "1"),
                  text.replace(",", "."), text.replace(" ", "")]
    if whole and _parses(text, float) and float(text).is_integer():
        candidates.insert(0, str(int(float(text))))
    for candidate in candidates:
        if candidate != text and _parses(candidate, parse):
            return f'Did you mean "{candidate}"?'
    return "Write a whole number, such as 120." if whole else "Write a number, such as 12.5."


def _parses(text, parse) -> bool:
    try:
        parse(text)
        return True
    except (TypeError, ValueError):
        return False


def display_name(attributes) -> str:
    """How a problem names a control: its script binding or signal, its label, its text or its ID."""
    for key in ("binding_value", "variable", "label", "text", "id"):
        if str(attributes.get(key) or "").strip():
            return str(attributes[key]).strip()
    return str(attributes.get("type") or "control")


def script_file(database_path) -> Path:
    """The script of a panel database: family_YYYY-MM-DD_script.py beside family_YYYY-MM-DD.xml."""
    path = Path(database_path)
    return path.with_name(path.stem + "_script.py")


def check_panel_file(path, dbc=None) -> list[Problem]:
    """Check a panel database on disk and the script beside it. dbc: the DBC, when it is loaded already."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        return [Problem(ERROR, f"The file cannot be read: {exc.strerror or exc}", FORM, str(path))]
    script, script_text, problems = script_file(path), None, []
    if script.exists():
        try:
            script_text = script.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(Problem(ERROR, f"The script cannot be read: {exc}", SCRIPT, str(script)))
    return problems + check_panel(data, str(path), script_text, str(script), path.resolve().parent, dbc)


def check_panel(xml_data, path="", script_text=None, script_path="", base_dir=None, dbc=None) -> list[Problem]:
    """Check a panel's XML (bytes or text) and its script (None: none). path: the XML's file, "" for a form in
    memory; base_dir: where its DBC and pictures are looked for; dbc: its DBC, when it is loaded already.
    Errors come first, then warnings, each in the order of the files."""
    check = _Check(path, base_dir, dbc)
    try:
        root = check.parse(xml_data)
        if root is not None:
            check.check_structure(root)
            check.check_variables(root)
            check.load_dbc(root)
            check.check_controls(root)
        if script_text is not None:
            check.check_script(script_text, script_path)
    except Exception as exc:        # a fault of the check must not stop what asked for it: a Connect, a test
        check.problems.append(Problem(WARNING, f"The check could not finish: {exc}", FORM, check.path))
    return sorted(check.problems, key=lambda p: (not p.is_error, p.kind == SCRIPT, p.line, p.column))


class _Check:
    """One check: what was read so far, and the problems found."""

    def __init__(self, path, base_dir, dbc):
        self.path = str(path or "")
        self.base_dir = Path(base_dir) if base_dir else Path(path).resolve().parent if path else None
        self.dbc = dbc
        self.dbc_name = ""
        self.dbc_failed = False             # a DBC was named but could not be loaded: its signals are not checked
        self.problems = []
        self.lines = []                     # the XML's lines
        self.positions = {}                 # element -> (line, column) of its start tag, as expat saw it
        self.info = {}                      # control element -> (name, page name, (page, index))
        self.names = None                   # control name -> element, as the panel names them; None: no XML
        self.structures = {}                # the structured variables: name -> Structure
        self.handlers = {}                  # handler function name -> [(control element, control name)]
        self.script_path = ""
        self.script_lines = []

    # --- recording problems ------------------------------------------------------------------------------------

    def form(self, severity, message, elem=None, attribute=None, hint=""):
        line = column = 0
        source = ""
        if elem is not None and self.path:
            line, column = self.locate(elem, attribute)
            source = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
        name, page, position = self.info.get(elem, ("", "", ()))
        self.problems.append(Problem(severity, message, FORM, self.path, line, column, source, hint, name, page,
                                     position))

    def script(self, severity, message, line, column=0, hint=""):
        source = self.script_lines[line - 1] if 0 < line <= len(self.script_lines) else ""
        self.problems.append(Problem(severity, message, SCRIPT, self.script_path, line, column, source, hint))

    def locate(self, elem, attribute=None):
        """(line, column) of an element's start tag, or of one of its attributes (1-based)."""
        line, column = self.positions.get(elem, (0, 0))
        if not line:
            return 0, 0
        if attribute:
            pattern = re.compile(rf"(?<![\w.:-]){re.escape(attribute)}\s*=")
            for number in range(line, min(line + 40, len(self.lines)) + 1):
                text = self.lines[number - 1]
                start = column if number == line else 0
                match = pattern.search(text, start)
                if match:
                    return number, match.start() + 1
                if ">" in text[start:]:
                    break
        return line, column + 1

    # --- the XML -----------------------------------------------------------------------------------------------

    def parse(self, data):
        """The XML's root element, with where each element starts - or None, with the error, when XML cannot be
        read from it."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.lines = re.split(r"\r\n|\r|\n", data.decode("utf-8-sig", errors="replace"))
        parser = expat.ParserCreate()
        starts, open_elements = [], []

        def start(name, _attributes):
            starts.append((parser.CurrentLineNumber, parser.CurrentColumnNumber))
            open_elements.append((name, parser.CurrentLineNumber))

        parser.StartElementHandler = start
        parser.EndElementHandler = lambda _name: open_elements.pop()
        try:
            parser.Parse(data, True)
            root = ET.fromstring(data)
        except expat.ExpatError as exc:
            self.xml_error(exc.code, exc.lineno, exc.offset, open_elements)
            return None
        except ET.ParseError as exc:
            self.xml_error(exc.code, *exc.position, open_elements)
            return None
        self.positions = dict(zip(root.iter(), starts))
        return root

    def xml_error(self, code, line, offset, open_elements):
        reason = expat.errors.messages.get(code, "not XML")
        source = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
        message, hint = f"The XML cannot be read: {reason}", XML_HINTS.get(reason, "")
        if reason == expat.errors.XML_ERROR_TAG_MISMATCH and open_elements:
            name, opened = open_elements[-1]
            closing = re.match(r"</\s*([^\s>]*)", source[max(source.rfind("</", 0, offset + 2), 0):])
            if closing:
                message = f"</{closing.group(1)}> does not close <{name}>, which line {opened} opens"
                hint = did_you_mean(closing.group(1), [name], "</{}>") or \
                    f"Is a /> missing at the end of an element before it, or a </{name}>?"
        elif reason == expat.errors.XML_ERROR_NO_ELEMENTS and open_elements:
            name, opened = open_elements[-1]
            message = f"The file ends before <{name}>, which line {opened} opens, is closed"
            hint = f"Close it with </{name}> - or end the element with />."
        self.problems.append(Problem(ERROR, message, FORM, self.path, line, offset + 1, source, hint))

    def check_structure(self, root):
        if root.tag != "application_database":
            self.form(ERROR, f"The file begins with <{root.tag}>: a panel database begins with <application_database>",
                      root, hint=did_you_mean(root.tag, ["application_database"], "<{}>"))
        pages = root.find("pages")
        inside = set(pages.iter()) if pages is not None else None
        known = {*CONTROLS, *STRUCTURE}
        for elem in root.iter():
            if elem.tag in STRUCTURE:
                for key in elem.attrib:
                    if key not in STRUCTURE[elem.tag]:
                        self.form(WARNING, f'<{elem.tag}> has no attribute {key}: it is ignored', elem, key,
                                  did_you_mean(key, STRUCTURE[elem.tag]))
            elif elem is root:
                continue
            elif elem.tag not in known:
                self.form(WARNING, f"<{elem.tag}> is not a control CAN Expert knows: it is left out of the panel", elem,
                          hint=did_you_mean(elem.tag, known, "<{}>"))
            elif inside is not None and elem not in inside:
                self.form(WARNING, f"This {CONTROLS[elem.tag].label.lower()} is outside <pages>: it is not shown",
                          elem, hint="Move it into a <page>.")

    def check_variables(self, root):
        """The structured variables (<variables>): a line written wrong stops the panel from loading."""
        element = root.find("variables")
        text = element.text or "" if element is not None else ""
        structures, problems = parse_variables(text)
        self.structures = {item.name: item for item in structures}
        if element is None:
            return
        first, _column = self.positions.get(element, (0, 0))
        shift = 1 if text.startswith("\n") else 0           # the Form Designer shows them from the next line
        for number, message in problems:
            line = first + number - 1 if self.path and first else 0
            source = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
            self.problems.append(Problem(ERROR, message, VARIABLES, self.path, line, 0, source, "",
                                         position=(max(1, number - shift),)))

    def variable_path_problem(self, name):
        """For a name like "Calib Data.FOC[3]": (what is wrong with it as a field of a variable, hint) - ("", "")
        when nothing is; None when it names no variable at all."""
        variable, dot, path = str(name).partition(".")
        structure = self.structures.get(variable)
        if not dot or structure is None:
            return None
        fields = [f"{variable}.{item}" for item in structure.paths()]
        try:
            field_name, index = split_path(path)
        except ValueError as exc:
            return str(exc), did_you_mean(name, fields)
        item = structure.field(field_name)
        if item is None:
            return f"{variable} has no field {field_name}", did_you_mean(name, fields)
        if index is not None and (item.count is None or item.text):
            return f"{variable}.{field_name} is not an array", f'Write "{variable}.{field_name}".'
        if index is not None and not index < item.count:
            return (f"{variable}.{field_name} has {item.count} values: [0] to [{item.count - 1}]",
                    f'Did you mean "{variable}.{field_name}[{item.count - 1}]"?')
        return "", ""

    def load_dbc(self, root):
        value = root.get("dbc_path", "").strip()
        if not value:
            return
        path = Path(value)
        if not path.is_absolute() and self.base_dir is not None:
            path = self.base_dir / path
        self.dbc_name = path.name
        if self.dbc is not None:
            return
        if not path.is_file():
            self.dbc_failed = True
            there = [item.name for item in path.parent.glob("*.*")] if path.parent.is_dir() else []
            self.form(ERROR, f"The DBC {value} does not exist", root, "dbc_path",
                      did_you_mean(path.name, there) or f"It was looked for as {path.resolve()}. Choose it again on "
                                                        "the Form Designer's Database tab.")
            return
        try:
            import cantools
            self.dbc = cantools.database.load_file(str(path))
        except Exception as exc:
            self.dbc_failed = True
            self.form(ERROR, f"The DBC {path.name} cannot be read: {exc}", root, "dbc_path")

    # --- the controls ------------------------------------------------------------------------------------------

    def check_controls(self, root):
        """Every control as the panel builds them: page by page, in the order of the file."""
        pages = root.find("pages")
        controls = []
        for page_index, source in enumerate(list(pages) if pages is not None else [root]):
            page_name = source.get("name", "Main")
            index = 0
            for elem in source.iter():
                if elem.tag in CONTROLS:
                    self.info[elem] = (display_name(elem.attrib), page_name, (page_index, index))
                    controls.append((elem, page_index, index, self.check_values(elem)))
                    self.check_properties(elem)
                    index += 1
        self.names = {}
        for elem, page_index, index, definition in controls:
            if definition is None:
                continue
            try:
                key = control_key(definition, page_index, index, self.names)
            except ValueError:
                key = str(definition.get("binding_value") or definition.get("variable"))
                attribute = "binding_value" if elem.get("binding_value") else "variable"
                self.form(ERROR, f'Two controls are named "{key}"', elem, attribute,
                          f"The other one is {self.describe(self.names[key])}. A script reaches a control by its "
                          "name, so each needs its own.")
                continue
            self.names[key] = elem
            handler = str(definition.get("handler") or "").strip()
            if handler and not handler.isidentifier():
                self.form(WARNING, f'handler="{handler}" is not a Python function name', elem, "handler",
                          "Use letters, digits and _, not beginning with a digit: on_start_clicked.")
            elif handler:
                self.handlers.setdefault(handler, []).append((elem, key))
            problem = self.variable_path_problem(key)
            if problem and problem[0]:
                self.form(WARNING, f'This control is named "{key}": {problem[0]}', elem, "binding_value", problem[1])
            if elem.tag == "var_list":
                shown = str(definition.get("structure") or "").strip()
                if shown not in self.structures:
                    self.form(WARNING, f'This Variable List shows "{shown}", which is not a variable of the panel'
                              if shown else "This Variable List shows no variable", elem, "structure",
                              did_you_mean(shown, self.structures) or
                              "Define it on the Form Designer's Variables tab, then choose it in Properties.")
        self.check_bindings(controls)

    def describe(self, elem) -> str:
        line, _column = self.locate(elem)
        name, page, _position = self.info.get(elem, ("", "", ()))
        return f"on line {line}" if self.path and line else f"on page {page}" if page else name

    def check_values(self, elem):
        """Errors for the values parse_widget refuses - the panel cannot be loaded with them. The control's
        definition, as the panel reads it, else None."""
        attrs = elem.attrib
        before = len(self.problems)
        for key in ("x", "y", "width", "height", "byte_start", "byte_length", "byte", "bit"):
            if key in attrs and not _parses(attrs[key], int):
                self.form(ERROR, f'{key}="{attrs[key]}" is not a whole number', elem, key, number_hint(attrs[key], True))
        for key in ("min", "max", "scale", "offset"):
            if key in attrs and not (key in ("min", "max") and attrs[key] == "") and not _parses(attrs[key], float):
                self.form(ERROR, f'{key}="{attrs[key]}" is not a number', elem, key, number_hint(attrs[key], False))
        can_id = attrs.get("can_id", "")
        if can_id.strip():
            if not _parses(can_id, parse_can_id):
                self.form(ERROR, f'can_id="{can_id}" is not a CAN identifier', elem, "can_id",
                          "Write it in hex, such as 0x7E0, or in decimal, such as 2016.")
            elif not 0 <= parse_can_id(can_id) <= 0x1FFFFFFF:
                self.form(ERROR, f'can_id="{can_id}" is beyond 0x1FFFFFFF, the highest CAN identifier', elem, "can_id")
        if elem.tag == "button" and not _parses(attrs.get("data", "00"), parse_hex_bytes):
            self.form(ERROR, f'data="{attrs["data"]}" is not bytes in hex', elem, "data", "Write them as 01 02 03.")
        elif elem.tag == "button" and (len(parse_hex_bytes(attrs.get("data", "00"))) > 8 or
                                       any(not 0 <= b <= 255 for b in parse_hex_bytes(attrs.get("data", "00")))):
            self.form(ERROR, f'data="{attrs["data"]}" is more than eight bytes, or a value above FF', elem, "data")
        if len(self.problems) == before:
            self.check_ranges(elem)
        try:
            definition = parse_widget(elem)
        except Exception as exc:
            if len(self.problems) == before:
                self.form(ERROR, str(exc), elem)
            return None
        return definition if len(self.problems) == before else None

    def check_ranges(self, elem):
        attrs = elem.attrib
        for key in ("byte", "bit"):
            if not 0 <= int(attrs.get(key, 0)) < 8:
                self.form(ERROR, f'{key}="{attrs[key]}": a CAN byte or bit position is 0 to 7', elem, key)
        start, length = int(attrs.get("byte_start", 0)), int(attrs.get("byte_length", 1))
        if start < 0 or not 1 <= length <= 8 or start + length > 8:
            self.form(ERROR, f'byte_start="{start}" and byte_length="{length}" do not fit in the eight bytes of a '
                             "CAN frame", elem, "byte_start" if "byte_start" in attrs else "byte_length")
        minimum, maximum = attrs.get("min", 0), attrs.get("max", 100)
        if minimum != "" and maximum != "" and float(minimum) > float(maximum):
            self.form(ERROR, f'min="{minimum}" is more than max="{maximum}"', elem, "min", "Swap them.")
        for key in ("width", "height"):
            if key in attrs and int(attrs[key]) <= 0:
                self.form(ERROR, f'{key}="{attrs[key]}": a control needs a width and a height above 0', elem, key)

    def check_properties(self, elem):
        """Warnings for what the panel ignores or reads otherwise: an attribute no control has, a choice that is
        not offered, a number, colour or yes/no it cannot read, a state without a value, a missing picture."""
        control = CONTROLS[elem.tag]
        own = {prop.key: prop for prop in (*APPEARANCE, READ_ONLY, *control.props)}
        for key, value in elem.attrib.items():
            if key not in KNOWN_ATTRIBUTES:
                self.form(WARNING, f'{key}="{value}": a {control.label.lower()} has no property {key}, so it is '
                                   "ignored", elem, key, did_you_mean(key, [*COMMON, *own]) or
                          did_you_mean(key, KNOWN_ATTRIBUTES))
            elif key in own and key not in ("min", "max") and not (key == "value_type" and not control.interactive):
                self.check_property(elem, own[key], value)          # min and max: check_values
        binding_type = elem.get("binding_type")
        if binding_type is not None and binding_type not in BINDING_TYPES:
            self.form(WARNING, f'binding_type="{binding_type}" is neither script nor dbc: the control is bound to '
                               "nothing", elem, "binding_type", did_you_mean(binding_type, BINDING_TYPES))

    def check_property(self, elem, prop, value):
        key, text = prop.key, value.strip()
        if prop.editor == "choice" and value not in prop.options:
            self.form(WARNING, f'{key}="{value}" is not one of: {", ".join(prop.options)}', elem, key,
                      did_you_mean(value, prop.options))
        elif prop.editor in ("int", "float") and not _parses(value, float):
            self.form(WARNING, f'{key}="{value}" is not a number: the default, {prop.default}, is used', elem, key,
                      number_hint(value, False))
        elif prop.editor == "optional_float" and text and not _parses(value, float):
            self.form(WARNING, f'{key}="{value}" is not a number: it is taken as blank', elem, key,
                      number_hint(value, False))
        elif prop.editor == "bool" and text.lower() not in TRUE_FALSE:
            self.form(WARNING, f'{key}="{value}" is neither True nor False: it is taken as False', elem, key,
                      did_you_mean(value, ("True", "False")))
        elif prop.editor == "color" and text and not QColor(text).isValid():
            hint = "Write #RRGGBB, such as #16c60c." if text.startswith("#") else \
                did_you_mean(text, QColor.colorNames()) or "Write #RRGGBB, or a colour name such as red."
            self.form(WARNING, f'{key}="{value}" is not a colour: the default colour is used', elem, key, hint)
        elif prop.editor == "states":
            for part in (p.strip() for p in value.replace("\n", ";").split(";")):
                colour = part.split("=", 1)[1].partition(":")[2].strip() if "=" in part else ""
                if part and "=" not in part:
                    self.form(WARNING, f'The state "{part}" has no value: it is left out', elem, key,
                              "Write each state as value=text:colour, such as 1=On:#16c60c.")
                elif colour and not QColor(colour).isValid():
                    self.form(WARNING, f'"{colour}" in the state "{part}" is not a colour', elem, key,
                              did_you_mean(colour, QColor.colorNames()) or "Write #RRGGBB, such as #16c60c.")
        elif prop.editor == "file" and text:
            path = Path(text)
            if not path.is_absolute() and self.base_dir is not None:
                path = self.base_dir / path
            if not path.exists():
                self.form(WARNING, f"The picture {text} does not exist", elem, key, f"It was looked for as {path}.")

    def check_bindings(self, controls):
        """The DBC signals controls are bound to are in the panel's DBC."""
        without_dbc = []
        for elem, _page, _index, definition in controls:
            if definition is None or definition.get("binding_type") != "dbc":
                continue
            value = str(definition.get("binding_value") or "").strip()
            attribute = "binding_value" if elem.get("binding_value") else "variable"
            if "." not in value:
                self.form(WARNING, f'This {CONTROLS[elem.tag].label.lower()} is bound to the DBC signal "{value}", '
                                   "which is not written Message.Signal", elem, attribute)
            elif self.dbc is None:
                if not self.dbc_failed:
                    without_dbc.append(elem)
            else:
                problem = self.signal_problem(value)
                if problem:
                    self.form(WARNING, *problem[:1], elem, attribute, problem[1])
        if without_dbc:
            count = len(without_dbc)
            self.form(WARNING, f"{count} control{'s are' if count > 1 else ' is'} bound to DBC signals, but the panel "
                               "has no DBC: they stay empty", without_dbc[0], "binding_value",
                      "Choose the DBC on the Form Designer's Database tab.")

    # --- the DBC -----------------------------------------------------------------------------------------------

    def message_named(self, name):
        try:
            return self.dbc.get_message_by_name(name)
        except KeyError:
            return None

    def signal_problem(self, value):
        """(what is wrong, hint) when "Message.Signal" is not in the DBC, else None."""
        message_name, _dot, signal_name = value.partition(".")
        message = self.message_named(message_name)
        dbc = self.dbc_name or "the DBC"
        if not signal_name:
            return f'"{value}" is not written Message.Signal', ""
        if message is None:
            everything = [f"{m.name}.{s.name}" for m in self.dbc.messages for s in m.signals]
            return (f'"{value}" is not in {dbc}: it has no message {message_name}',
                    did_you_mean(value, everything) or did_you_mean(message_name, [m.name for m in self.dbc.messages]))
        if signal_name not in [signal.name for signal in message.signals]:
            match = closest(signal_name, [signal.name for signal in message.signals])
            return (f'"{value}" is not in {dbc}: {message_name} has no signal {signal_name}',
                    f'Did you mean "{message_name}.{match}"?' if match else "")
        return None

    # --- the script --------------------------------------------------------------------------------------------

    def check_script(self, text, path):
        self.script_path = str(path or "")
        self.script_lines = re.split(r"\r\n|\r|\n", text)
        filename = self.script_path or "<script>"
        try:
            compile(text, filename, "exec")
        except SyntaxError as exc:
            hint = "Indent each block the same way - four spaces - and do not mix tabs with spaces." \
                if isinstance(exc, IndentationError) else ""
            self.script(ERROR, f"{exc.msg}: the script cannot start", exc.lineno or 0, exc.offset or 0, hint)
            return
        except ValueError as exc:                 # a NUL character
            self.script(ERROR, f"{exc}: the script cannot start", 0)
            return
        tree = ast.parse(text, filename)
        defined, star = module_names(tree)
        in_functions = function_lines(tree)
        self.check_script_names(tree, defined, in_functions)
        if self.names is not None:
            self.check_handlers(defined, star)
            self.check_script_references(tree)

    def check_script_names(self, tree, defined, in_functions):
        """Names that are not defined, and the like (pyflakes, when it is there)."""
        try:
            from pyflakes.checker import Checker
        except ImportError:
            return
        known = script_globals()
        for message in sorted(Checker(tree, self.script_path or "<script>", builtins=known).messages,
                              key=lambda m: (m.lineno, m.col)):
            kind = type(message).__name__
            if kind not in SCRIPT_MESSAGES:
                continue
            text, hint = message.message % message.message_args, ""
            if kind == "UndefinedName":
                name = message.message_args[0]
                text = f"{name} is not defined"
                hint = did_you_mean(name, known | defined | set(dir(builtins)), "{}")
            if message.lineno in in_functions:
                self.script(WARNING, f"{text}: this fails when it runs", message.lineno, message.col + 1, hint)
            else:
                self.script(ERROR, f"{text}: the script stops here when it starts", message.lineno, message.col + 1,
                            hint)

    def check_handlers(self, defined, star):
        """The functions controls name as their handler are in the script."""
        if star:
            return
        for handler, uses in self.handlers.items():
            if handler in defined:
                continue
            for elem, name in uses:
                self.form(WARNING, f'The {CONTROLS[elem.tag].label.lower()} "{name}" calls {handler}(), which the '
                                   "script does not define", elem, "handler",
                          did_you_mean(handler, defined, "{}()") or
                          "Double-click the control in the Form Designer: the function is written for you.")

    def check_script_references(self, tree):
        """Controls, signals and messages the script names in quotes are on the panel and in its DBC."""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name):
                        self.check_decorator(decorator.func.id, decorator)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.check_call(node)

    def check_decorator(self, name, call):
        for argument in call.args:
            if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
                continue
            if name == "on_control":
                self.check_control_name(argument.value, argument)
            elif name == "on_variable":
                self.check_variable_name(argument.value, argument)
            elif name == "on_signal":
                self.check_signal(argument.value, argument)
            elif name == "on_message":
                self.check_message(argument.value, argument)

    def check_call(self, call):
        function = call.func
        first = call.args[0] if call.args else None
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            return
        on_api = isinstance(function.value, ast.Name) and function.value.id == "api"
        if function.attr in ("set_value", "get_value") and isinstance(function.value, ast.Attribute) and \
                function.value.attr == "ui":
            self.check_control_name(first.value, first)                     # api.ui.set_value("name", ...)
        elif on_api and function.attr == "on":
            self.check_control_name(first.value, first)                     # api.on("name", callback)
        elif on_api and function.attr == "var":
            self.check_variable_name(first.value, first)                    # api.var("Calib Data")
        elif on_api and function.attr in ("signal", "set_signal"):
            self.check_signal(first.value, first)
        elif on_api and function.attr == "send_message" and self.dbc is not None:
            message = self.message_named(first.value)
            if message is None:
                self.script(WARNING, f'{self.dbc_name or "The DBC"} has no message {first.value}', first.lineno,
                            first.col_offset + 1, did_you_mean(first.value, [m.name for m in self.dbc.messages]))
                return
            signals = [signal.name for signal in message.signals]
            for keyword in call.keywords:
                if keyword.arg and keyword.arg not in signals:
                    self.script(WARNING, f"{first.value} has no signal {keyword.arg}", keyword.value.lineno,
                                keyword.value.col_offset + 1, did_you_mean(keyword.arg, signals, "{}"))

    def check_variable_name(self, name, node):
        if name not in self.structures:
            self.script(WARNING, f'The panel has no variable "{name}"', node.lineno, node.col_offset + 1,
                        did_you_mean(name, self.structures) or "Define it on the Form Designer's Variables tab.")

    def check_control_name(self, name, node):
        if self.variable_path_problem(name) == ("", ""):
            return                                     # a variable's field: its Variable Lists show it
        if name not in self.names:
            self.script(WARNING, f'There is no control named "{name}" on the panel', node.lineno, node.col_offset + 1,
                        did_you_mean(name, self.names))

    def check_signal(self, value, node):
        if self.dbc is None:
            if not self.dbc_failed:
                self.script(WARNING, f'"{value}" is a DBC signal, and the panel has no DBC', node.lineno,
                            node.col_offset + 1, "Choose the DBC on the Form Designer's Database tab.")
            return
        problem = self.signal_problem(value)
        if problem:
            self.script(WARNING, problem[0], node.lineno, node.col_offset + 1, problem[1])

    def check_message(self, value, node):
        """@on_message("Name"): the script stops at the decorator when there is no such message."""
        text = value.strip()
        if text.lower().startswith("0x") and _parses(text, lambda t: int(t, 16)) or text.isdigit():
            return
        if text.lower().startswith("0x"):
            self.script(ERROR, f'"{value}" is not a CAN identifier: the script stops here when it starts', node.lineno,
                        node.col_offset + 1, "Write it in hex, such as 0x300, or give the message's name.")
        elif self.dbc is None:
            if not self.dbc_failed:
                self.script(ERROR, f'@on_message("{value}") needs the panel\'s DBC, and it has none: the script stops '
                                   "here when it starts", node.lineno, node.col_offset + 1,
                            "Choose the DBC on the Form Designer's Database tab, or write the identifier: 0x300.")
        elif self.message_named(text) is None:
            self.script(ERROR, f'{self.dbc_name or "The DBC"} has no message {value}: the script stops here when it '
                               "starts", node.lineno, node.col_offset + 1,
                        did_you_mean(text, [m.name for m in self.dbc.messages]))


def module_names(tree):
    """(the names a script defines at its top level - functions, classes, variables, imports -, whether it
    imports * and so may define any)."""
    names, star = set(), False

    def visit(statements):
        nonlocal star
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    star = star or alias.name == "*"
                    names.add((alias.asname or alias.name).split(".")[0])
            else:                                   # an assignment, or a block: if, for, while, with, try, match
                for field, value in ast.iter_fields(node):
                    if field in ("body", "orelse", "finalbody"):
                        visit(value)
                    elif field in ("handlers", "cases"):
                        for part in value:
                            names.update([part.name] if getattr(part, "name", None) else [])
                            visit(part.body)
                    else:
                        for part in value if isinstance(value, list) else [value]:
                            if isinstance(part, ast.AST):
                                names.update(child.id for child in ast.walk(part)
                                             if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store))

    visit(tree.body)
    return names, star


def function_lines(tree) -> set[int]:
    """The lines inside functions: what runs when a function is called, not when the script starts."""
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            lines.update(range(node.body[0].lineno, (node.end_lineno or node.body[-1].lineno) + 1))
        elif isinstance(node, ast.Lambda):
            lines.update(range(node.body.lineno, (node.body.end_lineno or node.body.lineno) + 1))
    return lines
