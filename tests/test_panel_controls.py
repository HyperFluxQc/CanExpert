"""Panel control registry: every control builds, shows and reports values; formats, value tables, order."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path

from PyQt5.QtWidgets import QApplication

from canexpert.panel.database import PanelView, parse_application_database
from canexpert.panel.controls import CONTROLS, build, format_value, parse_states, states_from_choices

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parent.parent / "DBC" / "dummy_ecu.dbc"

SAMPLES = {"button": None, "switch": True, "checkbox": True, "radio": "Option 2", "combo": "Two", "slider": 42,
           "knob": 42, "spin": 42, "io_box": 42, "text_input": "hello", "value": 42, "display": 42, "gauge": 42,
           "progress_bar": 42, "led": True, "indicator": 1, "trend": 42, "output": "line", "label": "text",
           "group_box": "Title", "picture": None}
EXPECTED = {"switch": True, "checkbox": True, "radio": "Option 2", "combo": "Two", "slider": 42, "knob": 42, "spin": 42,
            "io_box": "42", "text_input": "hello", "value": "42", "display": "42", "gauge": 42, "progress_bar": 42,
            "led": True, "indicator": "1", "trend": 42, "output": "line", "label": "text", "group_box": "Title"}


class PanelControlsTest(unittest.TestCase):
    def test_every_control_builds_previews_and_round_trips_values(self):
        self.assertEqual(set(SAMPLES), set(CONTROLS))
        for kind, control in CONTROLS.items():
            with self.subTest(kind=kind):
                data = dict(control.defaults(), type=kind, label=kind, items="One, Two, Three")
                if kind == "radio":
                    data["items"] = "Option 1, Option 2"
                built, widget = build(kind, data, {})
                self.assertIs(built, control)
                control.preview(widget, data)
                self.assertFalse(widget.grab().isNull())                     # paints without errors
                emitted = []
                control.connect(widget, emitted.append)
                if kind == "output":
                    control.set_value(widget, data, None)                        # None clears the box
                    self.assertEqual(widget.text(), "")
                if SAMPLES[kind] is not None:
                    control.set_value(widget, data, SAMPLES[kind])
                if kind in EXPECTED:
                    self.assertEqual(control.get_value(widget), EXPECTED[kind])

    def test_user_input_is_reported(self):
        cases = {"button": (lambda w: w.click(), True), "switch": (lambda w: w.click(), True),
                 "radio": (lambda w: w.buttons[1].click(), "Option 2"), "knob": (lambda w: w.setValue(30), 30),
                 "spin": (lambda w: w.setValue(7), 7), "slider": (lambda w: w.setValue(9), 9)}
        for kind, (act, expected) in cases.items():
            with self.subTest(kind=kind):
                control = CONTROLS[kind]
                data = dict(control.defaults(), type=kind, items="Option 1, Option 2")
                _, widget = build(kind, data, {})
                emitted = []
                control.connect(widget, emitted.append)
                act(widget)
                self.assertEqual(emitted[-1], expected)

    def test_formats_and_value_tables(self):
        self.assertEqual(format_value(12.3456, {"decimals": 2, "unit": "bar"}), "12.35 bar")
        self.assertEqual(format_value(255, {"format": "hex", "unit": "x"}), "0xFF")
        self.assertEqual(format_value(5, {"format": "binary"}), "0b101")
        self.assertEqual(format_value(2.0, {}), "2")
        self.assertEqual(format_value(3, {"_choices": {3: "extended"}}), "extended")
        self.assertEqual(format_value(3, {"_choices": {3: "extended"}, "value_table": "False"}), "3")
        self.assertEqual(format_value("ready", {"unit": "V"}), "ready")
        states = parse_states("0=Off:#111111; 1=On; 2=Error:#c62828")
        self.assertEqual([(v, t) for v, t, _ in states], [("0", "Off"), ("1", "On"), ("2", "Error")])
        self.assertEqual(states[0][2].name(), "#111111")
        self.assertEqual(states_from_choices({2: "b", 1: "a"}).split("; ")[0].split(":")[0], "1=a")
        _, indicator = build("indicator", {"states": "0=Off; 1=On"}, {})
        CONTROLS["indicator"].set_value(indicator, {}, 1.0)
        self.assertEqual(indicator.text(), "On")
        CONTROLS["indicator"].set_value(indicator, {}, 7)
        self.assertEqual(indicator.text(), "7")                                  # unknown value shown as is

    def test_panel_uses_dbc_metadata_order_and_raw_mappings(self):
        folder = Path(tempfile.mkdtemp())
        path = folder / "panel.xml"
        path.write_text(f'''<application_database dbc_path="{DBC.as_posix()}"><pages><page>
<group_box label="Frame" x="0" y="0" width="300" height="200"/>
<indicator label="session" binding_type="dbc" binding_value="EcuStatus.Session" states=""/>
<value label="session_text" binding_type="dbc" binding_value="EcuStatus.Session"/>
<value label="temperature" binding_type="dbc" binding_value="EngineData.Temperature" decimals="1"/>
<switch label="enable" can_id="0x201" byte="0" bit="2"/>
<knob label="fan" can_id="0x202" byte="1" min="0" max="100"/>
<trend label="trend" min="" max="" binding_type="dbc" binding_value="EngineData.Temperature"/>
</page></pages></application_database>''')
        database = parse_application_database(path)
        self.assertEqual([w["kind"] for w in database["pages"][0]["widgets"]],
                         ["group_box", "indicator", "value", "value", "switch", "knob", "trend"])
        sent = []
        panel = PanelView(database, lambda *args: sent.append(args), self.fail)
        container = panel.widgets["Frame"].parent()
        self.assertIs(container.children()[0], panel.widgets["Frame"])        # group box stays at the back
        panel.on_message(0x301, bytes([1, 0, 3, 9, 0, 0, 0, 0]))
        panel.on_message(0x300, bytes([0x01, 0x2C, 0, 100, 0, 0, 0, 0]))
        self.assertEqual(panel.widgets["session"].text(), "extended")          # states from the value table
        self.assertEqual(panel.widgets["session_text"].text(), "extended")
        self.assertEqual(panel.widgets["temperature"].text(), "30.0 degC")      # unit from the DBC
        self.assertEqual(panel.widgets["trend"].value(), 30.0)
        panel.widgets["enable"].click()
        self.assertEqual(sent[-1][:2], (0x201, bytearray([4, 0, 0, 0, 0, 0, 0, 0])))
        panel.widgets["fan"].setValue(55)
        self.assertEqual(sent[-1][1][1], 55)
        self.assertEqual(panel.handlers(), {})


if __name__ == "__main__":
    unittest.main()
