"""
Panel databases: selection of the newest dated XML, parsing into the shared widget schema,
CAN value decoding, and PanelView, which renders a panel and bridges script/DBC bindings.
"""
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from PyQt5.QtCore import pyqtSignal, QSignalBlocker
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QTabWidget, QScrollArea, QLabel

from canexpert.panel.controls import CONTROLS, WIDGET_GROUPS, build, states_from_choices
from canexpert.paths import DATABASES_DIR


# -----------------------------------------------------------------------------
# Database selection and parsing
# -----------------------------------------------------------------------------

def _parse_hex(hex_str: str) -> list[int]:
    """Parse hex string '01 02 03' into list of ints."""
    return [int(x, 16) for x in hex_str.replace(",", " ").split() if x.strip()]


def _parse_can_id(val: str) -> int:
    """Parse CAN ID from hex string (0x200) or decimal."""
    s = str(val).strip().lower()
    if s.startswith("0x"):
        return int(s, 16)
    return int(s)


def _number(value):
    number = float(value)
    return int(number) if number.is_integer() else number


def select_database(databases_dir=DATABASES_DIR, family=""):
    """Newest YYYY-MM-DD or YYYYMMDD filename; undated legacy files rank last."""
    base = Path(databases_dir)
    if family and (Path(family).name != family or any(c in family for c in '/\\:')):
        raise ValueError("Database family must be a filename stem, not a path")
    candidates = []
    for path in base.glob("*.xml"):
        stem = path.stem
        match = re.search(r"(?:^|_)(\d{4})-?(\d{2})-?(\d{2})$", stem)
        stamp = date.min
        prefix = stem
        if match:
            try:
                stamp = date(*map(int, match.groups()))
            except ValueError:
                continue
            prefix = stem[:match.start()].rstrip("_")
        if family and family not in (stem, prefix):
            continue
        candidates.append((stamp, path.name, path))
    return max(candidates)[2] if candidates else None


def load_application_database(db_id="", databases_dir=DATABASES_DIR):
    path = select_database(databases_dir, str(db_id))
    return parse_application_database(path) if path else None


def parse_widget(elem):
    """One lossless schema shared by the designer and runtime."""
    data = dict(elem.attrib)
    kind = elem.tag
    value_type = data.get("value_type", data.get("type", "string" if kind == "text_input" else "float"))
    if value_type in WIDGET_GROUPS:
        value_type = "string" if kind == "text_input" else "float"
    data.update(kind=kind, type=kind, value_type=value_type)
    for key, default in (("x", 0), ("y", 0), ("width", 100), ("height", 30),
                         ("byte_start", 0), ("byte_length", 1), ("byte", 0), ("bit", 0)):
        data[key] = int(data.get(key, default))
    for key, default in (("min", 0), ("max", 100)):
        raw = data.get(key, default)
        data[key] = "" if raw == "" else _number(raw)  # blank = automatic (trend axes)
    for key, default in (("scale", 1.0), ("offset", 0.0)):
        data[key] = float(data.get(key, default))
    if "can_id" in data and str(data["can_id"]).strip():
        data["can_id"] = _parse_can_id(data["can_id"])
        if not 0 <= data["can_id"] <= 0x1FFFFFFF:
            raise ValueError("CAN ID must be between 0 and 0x1FFFFFFF")
    else:
        data.pop("can_id", None)
    if not 0 <= data["byte"] < 8 or not 0 <= data["bit"] < 8:
        raise ValueError("CAN byte and bit positions must be between 0 and 7")
    if data["byte_start"] < 0 or not 1 <= data["byte_length"] <= 8 or data["byte_start"] + data["byte_length"] > 8:
        raise ValueError("CAN value must fit within eight bytes")
    numeric_range = all(isinstance(data[k], (int, float)) for k in ("min", "max"))
    if (numeric_range and data["min"] > data["max"]) or data["width"] <= 0 or data["height"] <= 0:
        raise ValueError("Invalid widget range or dimensions")
    data.setdefault("id", "")
    data.setdefault("label", data.get("text", kind.title()))
    data.setdefault("unit", "")
    data.setdefault("binding_type", "script")
    data.setdefault("binding_value", data.get("variable", ""))
    if kind == "button":
        data["data_bytes"] = _parse_hex(data.get("data", "00"))
        if len(data["data_bytes"]) > 8 or any(not 0 <= b <= 255 for b in data["data_bytes"]):
            raise ValueError("Button data must contain at most eight bytes")
    return data


def parse_application_database(path):
    path = Path(path)
    root = ET.parse(path).getroot()
    if root.tag != "application_database":
        raise ValueError("Expected an application_database XML root")
    result = {"name": root.get("name", path.stem), "description": root.findtext("description", "").strip(),
              "source_path": str(path.resolve()), "dbc_path": root.get("dbc_path", ""), "pages": []}
    pages = root.find("pages")
    for source in list(pages) if pages is not None else [root]:
        page = {"name": source.get("name", "Main"), "widgets": []}  # widgets: document (z) order
        page.update({group: [] for group in WIDGET_GROUPS.values()})
        widget_index = 0
        for elem in source.iter():
            if elem.tag in WIDGET_GROUPS:
                widget = parse_widget(elem)
                if pages is None and "x" not in elem.attrib and "y" not in elem.attrib:
                    widget.update(x=20, y=20 + widget_index * 40)
                page[WIDGET_GROUPS[elem.tag]].append(widget)
                page["widgets"].append(widget)
                widget_index += 1
        result["pages"].append(page)
    return result


def decode_value_from_can_data(data: list | bytes, byte_start: int, byte_length: int, scale: float, offset: float, value_type: str) -> str | int | float:
    """Decode a value from CAN message data."""
    if not isinstance(data, (list, bytes, bytearray)):
        return 0
    if byte_start + byte_length > len(data):
        return 0
    raw = 0
    for i in range(byte_length):
        raw = (raw << 8) | (data[byte_start + i] & 0xFF)
    val = raw * scale + offset
    if value_type == "integer":
        return int(val)
    if value_type == "float":
        return round(val, 2)
    return str(val)


# -----------------------------------------------------------------------------
# PanelView
# -----------------------------------------------------------------------------

class PanelView(QWidget):
    """Runs a panel: builds its controls, forwards user input to CAN/DBC/script, shows received values."""
    control_changed = pyqtSignal(str, object)

    def __init__(self, database, send, log, parent=None):
        super().__init__(parent)
        self.send, self.log = send, log
        self.widgets = {}
        self.definitions = {}
        self.controls = {}
        self.dbc = None
        self.frames = {}
        source = database.get("source_path")
        base_dir = Path(source).parent if source else None
        dbc_path = database.get("dbc_path")
        if dbc_path:
            import cantools
            path = Path(dbc_path)
            if not path.is_absolute() and base_dir is not None:
                path = base_dir / path
            self.dbc = cantools.database.load_file(str(path))
        layout = QVBoxLayout(self)
        if database.get("description"):
            layout.addWidget(QLabel(database["description"]))
        tabs = QTabWidget()
        layout.addWidget(tabs)
        for page_index, page in enumerate(database.get("pages", [])):
            container = QWidget()
            right, bottom = 600, 400
            definitions = page.get("widgets") or [w for group in WIDGET_GROUPS.values() for w in page.get(group, [])]
            for index, definition in enumerate(definitions):
                kind = definition.get("kind") or definition.get("type") or "label"
                script_binding = definition.get("binding_type", "script") == "script"
                explicit_name = (definition.get("binding_value") or definition.get("variable")) if script_binding else ""
                key = explicit_name or definition.get("label") or definition.get("id")
                key = key or f"{page_index}.{kind}.{index}"
                if key in self.widgets:
                    if explicit_name:
                        raise ValueError(f"Duplicate control name '{key}'; use unique script bindings")
                    key = f"{page_index}.{kind}.{index}.{key}"
                self._apply_dbc_metadata(kind, definition)
                control, widget = build(kind, definition, {"base_dir": base_dir})
                widget.setParent(container)
                widget.setMinimumSize(1, 1)
                widget.setGeometry(definition.get("x", 0), definition.get("y", 0),
                                   definition.get("width", 100), definition.get("height", 30))
                self.widgets[key] = widget
                self.definitions[key] = definition
                self.controls[key] = control
                right = max(right, widget.x() + widget.width() + 20)
                bottom = max(bottom, widget.y() + widget.height() + 20)
                control.connect(widget, lambda value, k=key: self._changed(k, value))
            container.setMinimumSize(right, bottom)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(container)
            tabs.addTab(scroll, page.get("name", "Main"))

    def _apply_dbc_metadata(self, kind, definition):
        """DBC-bound controls take the signal's unit and value table (text for values, states for indicators)."""
        if self.dbc is None or definition.get("binding_type") != "dbc" or "." not in str(definition.get("binding_value")):
            return
        message_name, signal_name = str(definition["binding_value"]).split(".", 1)
        try:
            signal = self.dbc.get_message_by_name(message_name).get_signal_by_name(signal_name)
        except KeyError:
            self.log(f"Unknown DBC signal {definition['binding_value']}")
            return
        if signal.unit and not definition.get("unit"):
            definition["unit"] = signal.unit
        if signal.choices:
            choices = {int(value): str(label) for value, label in signal.choices.items()}
            definition["_choices"] = choices
            default_states = CONTROLS["indicator"].defaults()["states"]
            if kind == "indicator" and definition.get("states", "") in ("", default_states):
                definition["states"] = states_from_choices(choices)

    def handlers(self):
        """Control name -> handler function name, for the script runtime."""
        return {key: str(d["handler"]).strip() for key, d in self.definitions.items() if str(d.get("handler", "")).strip()}

    def values(self):
        return {key: self.controls[key].get_value(widget) for key, widget in self.widgets.items()}

    def set_value(self, name, value):
        widget = self.widgets.get(name)
        if widget is None:
            self.log(f"Unknown panel control: {name}")
            return
        blocker = QSignalBlocker(widget)
        try:
            self.controls[name].set_value(widget, self.definitions[name], value)
        except (ValueError, TypeError, OverflowError) as exc:
            self.log(f"Invalid value for {name}: {exc}")
        finally:
            del blocker

    def _changed(self, name, value):
        definition = self.definitions[name]
        try:
            kind = definition["kind"]
            if kind in ("io_box", "text_input"):
                typ = definition.get("value_type", "string")
                if typ == "integer":
                    value = int(value, 0)
                elif typ == "float":
                    value = float(value)
            if definition.get("binding_type") == "dbc":
                if self.dbc is None:
                    raise ValueError("Load the panel's DBC before using a DBC binding")
                msg_name, signal = definition["binding_value"].split(".", 1)
                msg = self.dbc.get_message_by_name(msg_name)
                values = msg.decode(self.frames.get(msg.frame_id, bytes(msg.length)), decode_choices=False)
                values[signal] = value
                payload = msg.encode(values)
                self.send(msg.frame_id, payload, msg.is_extended_frame)
                self.frames[msg.frame_id] = payload
            elif definition.get("can_id") is not None:
                can_id = definition["can_id"]
                payload = bytearray(self.frames.get(can_id, bytes(8)).ljust(8, b"\x00"))
                if kind == "button":
                    payload = bytes(definition["data_bytes"])
                elif kind in ("checkbox", "switch"):
                    byte, bit = definition["byte"], definition["bit"]
                    payload[byte] = (payload[byte] & ~(1 << bit)) | (int(bool(value)) << bit)
                elif kind in ("slider", "knob", "spin"):
                    payload[definition["byte"]] = max(0, min(255, int(value)))
                self.send(can_id, payload)
                self.frames[can_id] = bytes(payload)
            self.control_changed.emit(name, value)
        except Exception as exc:
            self.log(f"Control {name}: {exc}")

    def on_message(self, can_id, data):
        self.frames[can_id] = bytes(data)
        decoded = {}
        if self.dbc:
            try:
                msg = self.dbc.get_message_by_frame_id(can_id)
                decoded = {f"{msg.name}.{k}": v for k, v in msg.decode(bytes(data), decode_choices=False).items()}
            except (KeyError, ValueError):
                pass
        for name, definition in self.definitions.items():
            binding = definition.get("binding_value")
            if definition.get("binding_type") == "dbc" and binding in decoded:
                self.set_value(name, decoded[binding])
            elif definition.get("can_id") == can_id and self.controls[name].category == "Display":
                value = decode_value_from_can_data(data, definition["byte_start"], definition["byte_length"],
                                                   definition["scale"], definition["offset"], definition["value_type"])
                self.set_value(name, value)
