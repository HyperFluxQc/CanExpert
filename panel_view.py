"""Render the shared panel schema and bridge script/DBC bindings to widgets."""
from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal, QSignalBlocker
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QTabWidget, QScrollArea,
                            QPushButton, QLabel, QCheckBox, QSlider,
                            QProgressBar, QComboBox, QLineEdit)

from database_loader import WIDGET_GROUPS, decode_value_from_can_data


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
