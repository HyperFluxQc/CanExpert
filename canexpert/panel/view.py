"""PanelView: runs a panel database - builds its controls, sends user input to CAN, DBC signals or the
script, and shows received values. Each page is a PanelWindow (page_window.py) that can zoom and fit its
window; they sit in tabs here, or become windows of their own in the main window's workspace."""
from pathlib import Path

from PyQt5.QtCore import QSignalBlocker, pyqtSignal
from PyQt5.QtWidgets import QLabel, QTabWidget, QVBoxLayout, QWidget

from canexpert.panel.controls import CONTROLS, WIDGET_GROUPS, build, states_from_choices
from canexpert.panel.database import decode_value_from_can_data
from canexpert.panel.page_window import DEFAULT_ZOOM, PanelPage, PanelWindow


class PanelView(QWidget):
    """Runs a panel: builds its controls, forwards user input to CAN/DBC/script, shows received values."""
    control_changed = pyqtSignal(str, object)

    def __init__(self, database, send, log, parent=None, tabs=True, zooms=None):
        """tabs: the pages in tabs inside this widget; with tabs=False they are left in page_windows for
        the caller to place (the main window makes each one a workspace window). zooms: page name ->
        the zoom it had last time."""
        super().__init__(parent)
        self.send, self.log = send, log
        self.widgets = {}
        self.definitions = {}
        self.controls = {}
        self.dbc = None
        self.frames = {}
        self.page_windows = []          # (page name, PanelWindow), in the database's order
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
        layout.setContentsMargins(0, 0, 0, 0)
        self.description = database.get("description") or ""
        if self.description:
            layout.addWidget(QLabel(self.description))
        tab_widget = QTabWidget() if tabs else None
        if tab_widget is not None:
            layout.addWidget(tab_widget)
        for page_index, page in enumerate(database.get("pages", [])):
            container = PanelPage()
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
                widget.setMinimumSize(1, 1)
                container.place(widget, definition.get("x", 0), definition.get("y", 0),
                                definition.get("width", 100), definition.get("height", 30))
                self.widgets[key] = widget
                self.definitions[key] = definition
                self.controls[key] = control
                control.connect(widget, lambda value, k=key: self._changed(k, value))
            name = page.get("name", "Main")
            window = PanelWindow(container, name, (zooms or {}).get(name, DEFAULT_ZOOM))
            self.page_windows.append((name, window))
            if tab_widget is not None:
                tab_widget.addTab(window, name)

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
