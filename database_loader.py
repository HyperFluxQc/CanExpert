"""
Application Database Loader

Parses XML application databases (forms with buttons, values, checkboxes, etc.)
and returns a dict used by the main window to build the UI.
"""
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _parse_hex(hex_str: str) -> list[int]:
    """Parse hex string '01 02 03' into list of ints."""
    return [int(x, 16) for x in hex_str.replace(",", " ").split() if x.strip()]


def _parse_can_id(val: str) -> int:
    """Parse CAN ID from hex string (0x200) or decimal."""
    s = str(val).strip().lower()
    if s.startswith("0x"):
        return int(s, 16)
    return int(s)


def _get_attr(elem: ET.Element, key: str, default: Any = None, converter=None):
    val = elem.get(key, default)
    if val is None:
        return default
    if converter:
        try:
            return converter(val)
        except (ValueError, TypeError):
            return default
    return val


WIDGET_GROUPS = {
    "button": "buttons", "value": "values", "checkbox": "checkboxes",
    "slider": "sliders", "label": "labels", "text_input": "text_inputs",
    "gauge": "gauges", "progress_bar": "progress_bars", "led": "leds",
    "combo": "combos", "io_box": "io_boxes",
}
DATABASES_DIR = Path(__file__).resolve().parent / "Databases"


def select_database(databases_dir=DATABASES_DIR, family=""):
    """Newest YYYY-MM-DD or YYYYMMDD filename; undated legacy files rank last."""
    import re
    from datetime import date
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
                         ("byte_start", 0), ("byte_length", 1), ("byte", 0),
                         ("bit", 0), ("min", 0), ("max", 100)):
        data[key] = int(data.get(key, default))
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
    if data["min"] > data["max"] or data["width"] <= 0 or data["height"] <= 0:
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
        page = {"name": source.get("name", "Main")}
        page.update({group: [] for group in WIDGET_GROUPS.values()})
        widget_index = 0
        for elem in source.iter():
            if elem.tag in WIDGET_GROUPS:
                widget = parse_widget(elem)
                if pages is None and "x" not in elem.attrib and "y" not in elem.attrib:
                    widget.update(x=20, y=20 + widget_index * 40)
                page[WIDGET_GROUPS[elem.tag]].append(widget)
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
