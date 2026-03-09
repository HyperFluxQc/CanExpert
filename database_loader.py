"""
Application Database Loader

Parses XML application databases (forms with buttons, values, checkboxes, etc.)
and returns a dict used by the main window to build the UI.
"""
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Helpers
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


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------


def load_application_database(db_id: str, databases_dir: str | Path = "Databases") -> dict | None:
    """
    Load application database by ID. Searches for {db_id}.xml in databases_dir.
    Returns parsed structure or None if not found.
    """
    base = Path(databases_dir)
    candidates = [
        base / f"{db_id}.xml",
        Path(f"{db_id}.xml"),
        Path(f"Databases/{db_id}.xml"),
    ]
    for path in candidates:
        if path.exists():
            return parse_application_database(path)
    return None


def parse_application_database(path: str | Path) -> dict | None:
    """Parse application database XML file."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError:
        return None

    db_name = root.get("name", path.stem)
    result = {
        "name": db_name,
        "description": "",
        "buttons": [],
        "values": [],
        "checkboxes": [],
        "sliders": [],
        "labels": [],
        "text_inputs": [],
    }

    desc = root.find("description")
    if desc is not None and desc.text:
        result["description"] = desc.text.strip()

    pages_el = root.find("pages")
    if pages_el is not None:
        result["pages"] = []
        for page_el in pages_el.findall("page"):
            page = _parse_page(page_el)
            result["pages"].append(page)
        return result

    # Legacy flat structure (no pages)
    def _xy(elem):
        return _get_attr(elem, "x", 0, int), _get_attr(elem, "y", 0, int)

    for btn in root.findall(".//button"):
        b = _parse_button(btn)
        b["x"], b["y"] = _xy(btn)
        result["buttons"].append(b)

    for val in root.findall(".//value"):
        v = _parse_value(val)
        v["x"], v["y"] = _xy(val)
        result["values"].append(v)

    for cb in root.findall(".//checkbox"):
        c = _parse_checkbox(cb)
        c["x"], c["y"] = _xy(cb)
        result["checkboxes"].append(c)

    for sl in root.findall(".//slider"):
        s = _parse_slider(sl)
        s["x"], s["y"] = _xy(sl)
        result["sliders"].append(s)

    for lbl in root.findall(".//label"):
        l = _parse_label(lbl)
        l["x"], l["y"] = _xy(lbl)
        result["labels"].append(l)

    for ti in root.findall(".//text_input"):
        result["text_inputs"].append({
            "id": _get_attr(ti, "id", "0"),
            "label": _get_attr(ti, "label", ""),
            "type": _get_attr(ti, "type", "string"),
        })

    return result


def _parse_button(elem: ET.Element) -> dict:
    b = {
        "id": _get_attr(elem, "id", "0"),
        "label": _get_attr(elem, "label", "Button"),
        "type": _get_attr(elem, "type", "push_button"),
        "can_id": _get_attr(elem, "can_id", 0, _parse_can_id),
        "data": _get_attr(elem, "data", "00 00 00 00 00 00 00 00"),
    }
    if isinstance(b["data"], str):
        b["data_bytes"] = _parse_hex(b["data"])
    else:
        b["data_bytes"] = b["data"] if isinstance(b["data"], list) else [0] * 8
    b["x"] = _get_attr(elem, "x", 0, int)
    b["y"] = _get_attr(elem, "y", 0, int)
    return b


def _parse_value(elem: ET.Element) -> dict:
    v = {
        "id": _get_attr(elem, "id", "0"),
        "label": _get_attr(elem, "label", "Value"),
        "unit": _get_attr(elem, "unit", ""),
        "type": _get_attr(elem, "type", "float"),
        "can_id": _get_attr(elem, "can_id", 0, _parse_can_id),
        "byte_start": _get_attr(elem, "byte_start", 0, int),
        "byte_length": _get_attr(elem, "byte_length", 1, int),
        "scale": _get_attr(elem, "scale", 1.0, float),
        "offset": _get_attr(elem, "offset", 0.0, float),
    }
    v["x"] = _get_attr(elem, "x", 0, int)
    v["y"] = _get_attr(elem, "y", 0, int)
    return v


def _parse_checkbox(elem: ET.Element) -> dict:
    c = {
        "id": _get_attr(elem, "id", "0"),
        "label": _get_attr(elem, "label", "Checkbox"),
        "can_id": _get_attr(elem, "can_id", 0, _parse_can_id),
        "byte": _get_attr(elem, "byte", 0, int),
        "bit": _get_attr(elem, "bit", 0, int),
    }
    c["x"] = _get_attr(elem, "x", 0, int)
    c["y"] = _get_attr(elem, "y", 0, int)
    return c


def _parse_slider(elem: ET.Element) -> dict:
    s = {
        "id": _get_attr(elem, "id", "0"),
        "label": _get_attr(elem, "label", "Slider"),
        "min": _get_attr(elem, "min", 0, int),
        "max": _get_attr(elem, "max", 100, int),
        "type": _get_attr(elem, "type", "integer"),
        "can_id": _get_attr(elem, "can_id", 0, _parse_can_id),
        "byte": _get_attr(elem, "byte", 0, int),
    }
    s["x"] = _get_attr(elem, "x", 0, int)
    s["y"] = _get_attr(elem, "y", 0, int)
    return s


def _parse_label(elem: ET.Element) -> dict:
    return {
        "id": _get_attr(elem, "id", "0"),
        "text": _get_attr(elem, "text", ""),
        "type": _get_attr(elem, "type", "static"),
        "x": _get_attr(elem, "x", 0, int),
        "y": _get_attr(elem, "y", 0, int),
    }


def _parse_page(page_el: ET.Element) -> dict:
    """Parse one page element into dict with buttons, values, ..., each with x,y."""
    page = {
        "name": page_el.get("name", "Page"),
        "buttons": [],
        "values": [],
        "checkboxes": [],
        "sliders": [],
        "labels": [],
    }
    for btn in page_el.findall("button"):
        page["buttons"].append(_parse_button(btn))
    for val in page_el.findall("value"):
        page["values"].append(_parse_value(val))
    for cb in page_el.findall("checkbox"):
        page["checkboxes"].append(_parse_checkbox(cb))
    for sl in page_el.findall("slider"):
        page["sliders"].append(_parse_slider(sl))
    for lbl in page_el.findall("label"):
        page["labels"].append(_parse_label(lbl))
    return page


def decode_value_from_can_data(data: list | bytes, byte_start: int, byte_length: int, scale: float, offset: float, value_type: str) -> str | int | float:
    """Decode a value from CAN message data."""
    data = list(data) if hasattr(data, "__iter__") and not isinstance(data, str) else []
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
