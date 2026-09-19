"""
Panel databases: selection of the newest dated XML, parsing into the shared widget schema,
CAN value decoding, and PanelView, which renders a panel and bridges script/DBC bindings.
"""
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal, QSignalBlocker
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QTabWidget, QScrollArea,
                             QPushButton, QLabel, QCheckBox, QSlider,
                             QProgressBar, QComboBox, QLineEdit)


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


WIDGET_GROUPS = {
    "button": "buttons", "value": "values", "checkbox": "checkboxes",
    "slider": "sliders", "label": "labels", "text_input": "text_inputs",
    "gauge": "gauges", "progress_bar": "progress_bars", "led": "leds",
    "combo": "combos", "io_box": "io_boxes",
}
DATABASES_DIR = Path(__file__).resolve().parent / "Databases"


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


# -----------------------------------------------------------------------------
# PanelView
# -----------------------------------------------------------------------------

class PanelView(QWidget):
    control_changed = pyqtSignal(str, object)

    def __init__(self, database, send, log, parent=None):
        super().__init__(parent)
        self.send, self.log = send, log
        self.widgets = {}
        self.definitions = {}
        self.dbc = None
        self.frames = {}
        dbc_path = database.get("dbc_path")
        if dbc_path:
            import cantools
            path = Path(dbc_path)
            if not path.is_absolute():
                path = Path(database["source_path"]).parent / path
            self.dbc = cantools.database.load_file(str(path))
        layout = QVBoxLayout(self)
        if database.get("description"):
            layout.addWidget(QLabel(database["description"]))
        tabs = QTabWidget()
        layout.addWidget(tabs)
        for page_index, page in enumerate(database.get("pages", [])):
            container = QWidget()
            right, bottom = 600, 400
            for kind, group in WIDGET_GROUPS.items():
                for index, definition in enumerate(page.get(group, [])):
                    script_binding = definition.get("binding_type") == "script"
                    explicit_name = (definition.get("binding_value") or definition.get("variable")) if script_binding else ""
                    key = explicit_name or definition.get("label") or definition.get("id")
                    key = key or f"{page_index}.{kind}.{index}"
                    if key in self.widgets:
                        if explicit_name:
                            raise ValueError(f"Duplicate control name '{key}'; use unique script bindings")
                        key = f"{page_index}.{kind}.{index}.{key}"
                    widget = self._make_widget(kind, definition, container)
                    widget.setGeometry(definition.get("x", 0), definition.get("y", 0),
                                       definition.get("width", 100), definition.get("height", 30))
                    self.widgets[key] = widget
                    self.definitions[key] = definition
                    right = max(right, widget.x() + widget.width() + 20)
                    bottom = max(bottom, widget.y() + widget.height() + 20)
                    if kind == "button":
                        widget.clicked.connect(lambda checked, k=key: self._changed(k, True))
                    elif kind == "checkbox":
                        widget.toggled.connect(lambda value, k=key: self._changed(k, value))
                    elif kind == "slider":
                        widget.valueChanged.connect(lambda value, k=key: self._changed(k, value))
                    elif kind == "combo":
                        widget.currentTextChanged.connect(lambda value, k=key: self._changed(k, value))
                    elif kind in ("io_box", "text_input"):
                        widget.editingFinished.connect(lambda k=key, w=widget: self._changed(k, w.text()))
            container.setMinimumSize(right, bottom)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(container)
            tabs.addTab(scroll, page.get("name", "Main"))

    def _make_widget(self, kind, data, parent):
        if kind == "button":
            return QPushButton(data.get("label", "Button"), parent)
        if kind == "checkbox":
            return QCheckBox(data.get("label", ""), parent)
        if kind == "slider":
            widget = QSlider(Qt.Horizontal, parent)
            widget.setRange(data.get("min", 0), data.get("max", 100))
            return widget
        if kind in ("gauge", "progress_bar"):
            widget = QProgressBar(parent)
            widget.setRange(data.get("min", 0), data.get("max", 100))
            widget.setValue(widget.minimum())
            widget.setFormat("%v " + data.get("unit", ""))
            return widget
        if kind == "combo":
            widget = QComboBox(parent)
            widget.addItems([s.strip() for s in data.get("items", "").split(",") if s.strip()])
            return widget
        if kind in ("io_box", "text_input"):
            widget = QLineEdit(parent)
            widget.setPlaceholderText(data.get("label", ""))
            return widget
        widget = QLabel(data.get("text", "--"), parent)
        if kind == "led":
            widget.setText(data.get("off_text", "OFF"))
            widget.setStyleSheet("background: #444; color: white;")
        return widget

    def values(self):
        result = {}
        for key, widget in self.widgets.items():
            for method in ("isChecked", "value", "currentText", "text"):
                if hasattr(widget, method):
                    result[key] = getattr(widget, method)()
                    break
        return result

    def set_value(self, name, value):
        widget = self.widgets.get(name)
        if widget is None:
            self.log(f"Unknown panel control: {name}")
            return
        data = self.definitions[name]
        blocker = QSignalBlocker(widget)
        try:
            if data["kind"] == "led":
                on = bool(value)
                widget.setText(data.get("on_text", "ON") if on else data.get("off_text", "OFF"))
                widget.setStyleSheet(f"background: {'green' if on else '#444'}; color: white;")
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, (QSlider, QProgressBar)):
                widget.setValue(int(float(value)))
            elif isinstance(widget, QComboBox):
                widget.setCurrentText(str(value))
            else:
                widget.setText(str(value))
        except (ValueError, TypeError, OverflowError) as exc:
            self.log(f"Invalid value for {name}: {exc}")
        finally:
            del blocker

    def _changed(self, name, value):
        definition = self.definitions[name]
        try:
            if definition["kind"] in ("io_box", "text_input"):
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
                kind = definition["kind"]
                if kind == "button":
                    payload = bytes(definition["data_bytes"])
                elif kind == "checkbox":
                    byte, bit = definition["byte"], definition["bit"]
                    payload[byte] = (payload[byte] & ~(1 << bit)) | (int(bool(value)) << bit)
                elif kind == "slider":
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
            elif definition.get("can_id") == can_id and definition["kind"] == "value":
                value = decode_value_from_can_data(data, definition["byte_start"], definition["byte_length"],
                                                   definition["scale"], definition["offset"], definition["value_type"])
                self.set_value(name, f"{value} {definition.get('unit', '')}".strip())
