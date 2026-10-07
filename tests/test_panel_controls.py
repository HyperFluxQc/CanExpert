"""Panel control registry: every control builds, shows and reports values; formats (ASCII too), value tables,
order."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import unittest
from pathlib import Path

from PyQt5.QtWidgets import QApplication

from canexpert.panel.database import parse_application_database
from canexpert.panel.view import PanelView
from canexpert.panel.controls import CONTROLS, ascii_text, build, format_value, parse_states, states_from_choices
from canexpert.uds.client import UdsResult

APP = QApplication.instance() or QApplication([])
DBC = Path(__file__).resolve().parent.parent / "DBC" / "dummy_ecu.dbc"
# Two 16-bit signals carrying two characters each: Motorola (big-endian) in bytes 0-1, Intel in bytes 2-3.
TEXT_DBC = """VERSION ""
BS_:
BU_: ECU
BO_ 1280 Text: 8 ECU
 SG_ Big : 7|16@0+ (1,0) [0|65535] "" Vector__XXX
 SG_ Little : 16|16@1+ (1,0) [0|65535] "" Vector__XXX
"""

SAMPLES = {"button": None, "switch": True, "checkbox": True, "radio": "Option 2", "combo": "Two", "slider": 42,
           "knob": 42, "spin": 42, "io_box": 42, "text_input": "hello", "value": 42, "display": 42, "gauge": 42,
           "progress_bar": 42, "led": True, "indicator": 1, "trend": 42, "output": "line", "label": "text",
           "group_box": "Title", "picture": None, "var_list": {"temperature": 25}}
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

    def test_ascii(self):
        ascii_box = {"format": "ascii"}
        self.assertEqual(format_value(b"\x31\x30", ascii_box), "10", "the bytes 0x31 0x30 spell 10")
        self.assertEqual(format_value(bytearray(b"10"), ascii_box), "10")
        self.assertEqual(format_value([0x31, 0x30], ascii_box), "10")
        self.assertEqual(format_value(0x3130, ascii_box), "10", "a number: its bytes, big-endian")
        self.assertEqual(format_value(0x3031, dict(ascii_box, _byte_order="little")), "10", "an Intel signal's")
        self.assertEqual(format_value(12592.0, ascii_box), "10")
        vin = UdsResult(b"\x22\xf1\x90", b"\x62\xf1\x90WVWZZZ1KZAW000001\x00\x00", 2)
        self.assertEqual(format_value(vin, ascii_box), "WVWZZZ1KZAW000001", "a UDS answer's data, padding left out")
        self.assertEqual(format_value(b"A\x01B\xff\xff", ascii_box), "A.B", "no printable character: a dot")
        self.assertEqual(format_value("ready", ascii_box), "ready", "text as it is")
        self.assertEqual((ascii_text(-5), ascii_text(1.5), ascii_text(True)), ("-5", "1.5", "True"))
        self.assertEqual(format_value(b"\x31\x30", {}), "31 30", "bytes in the other formats: hex")
        self.assertIn("ascii", dict((prop.key, prop.options) for prop in CONTROLS["io_box"].props)["format"])
        self.assertIn("ascii", dict((prop.key, prop.options) for prop in CONTROLS["value"].props)["format"])

    def test_an_ascii_io_box_on_the_panel(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "text.dbc").write_text(TEXT_DBC, encoding="utf-8")
        path = folder / "panel.xml"
        path.write_text('''<application_database dbc_path="text.dbc"><pages><page>
<io_box label="serial" binding_type="script" binding_value="serial" format="ascii" value_type="string"/>
<io_box label="big" binding_type="dbc" binding_value="Text.Big" format="ascii"/>
<value label="little" binding_type="dbc" binding_value="Text.Little" format="ascii"/>
<io_box label="number" binding_type="script" binding_value="number" value_type="integer"/>
</page></pages></application_database>''', encoding="utf-8")
        database = parse_application_database(path)
        sent, changed = [], []
        panel = PanelView(database, lambda *args: sent.append(args), self.fail)
        panel.control_changed.connect(lambda name, value: changed.append((name, value)))
        panel.set_value("serial", b"\x31\x30")
        self.assertEqual(panel.widgets["serial"].text(), "10")
        panel.on_message(0x500, bytes([0x31, 0x30, 0x31, 0x30, 0, 0, 0, 0]))
        self.assertEqual(panel.widgets["big"].text(), "10", "Text.Big: raw 0x3130")
        self.assertEqual(panel.widgets["little"].text(), "10", "Text.Little: raw 0x3031, little-endian")
        panel.widgets["serial"].setText("42")
        panel.widgets["serial"].editingFinished.emit()
        self.assertEqual(changed[-1], ("serial", b"42"), "typed text goes to the script as its bytes")
        panel.widgets["big"].setText("AB")
        panel.widgets["big"].editingFinished.emit()
        self.assertEqual(sent[-1][0], 0x500)
        self.assertEqual(bytes(sent[-1][1])[:2], b"AB", "and into the signal as they read")
        panel.widgets["number"].setText("0x10")
        panel.widgets["number"].editingFinished.emit()
        self.assertEqual(changed[-1], ("number", 16), "the other formats as before")

    def test_an_io_box_value_is_selected_and_copied(self):
        from PyQt5.QtCore import Qt
        from PyQt5.QtTest import QTest
        path = Path(tempfile.mkdtemp()) / "panel.xml"
        path.write_text('''<application_database><pages><page>
<io_box label="shown" binding_type="script" binding_value="shown" read_only="true"/>
<io_box label="typed" binding_type="script" binding_value="typed" value_type="string" y="40"/>
</page></pages></application_database>''', encoding="utf-8")
        changed = []
        panel = PanelView(parse_application_database(path), lambda *args: None, self.fail)
        panel.control_changed.connect(lambda name, value: changed.append((name, value)))
        self.addCleanup(panel.close)
        panel.show()
        shown, typed = panel.widgets["shown"], panel.widgets["typed"]

        # Read-only: not greyed out - its text is selected and copied - and nothing is typed into it or sent.
        self.assertTrue(shown.isEnabled() and shown.isReadOnly())
        panel.set_value("shown", 1234.5)
        shown.setFocus()
        shown.selectAll()
        shown.copy()
        self.assertEqual(QApplication.clipboard().text(), "1234.5")
        QTest.keyClicks(shown, "99")
        QTest.keyClick(shown, Qt.Key_Return)
        shown.editingFinished.emit()
        self.assertEqual(shown.text(), "1234.5")
        self.assertEqual(changed, [], "a read-only box sends nothing")
        # The value keeps changing, and what is selected stays selected.
        panel.set_value("shown", 99.25)
        self.assertEqual(shown.selectedText(), "99.25", "all of it stays all of it")
        shown.setSelection(0, 2)
        panel.set_value("shown", 1000)
        self.assertEqual(shown.selectedText(), "10", "a part: the same places")
        shown.setSelection(4, -3)                                   # selected from right to left
        panel.set_value("shown", 123456)
        self.assertEqual((shown.selectedText(), shown.cursorPosition()), ("234", 1))

        # A box to type into: clicked into, copied from and left, it sends nothing again.
        panel.set_value("typed", "abc")
        typed.setFocus()
        typed.selectAll()
        typed.copy()
        typed.editingFinished.emit()
        self.assertEqual(changed, [])
        # Typed into: a value coming meanwhile does not overwrite it; leaving the box sends it.
        typed.setCursorPosition(3)
        QTest.keyClicks(typed, "d")
        panel.set_value("typed", "xyz")
        self.assertEqual(typed.text(), "abcd")
        typed.editingFinished.emit()
        self.assertEqual(changed, [("typed", "abcd")])
        panel.set_value("typed", "xyz")
        self.assertEqual(typed.text(), "xyz", "then the values show again")
        QTest.keyClick(typed, Qt.Key_Return)
        self.assertEqual(changed, [("typed", "abcd"), ("typed", "xyz")], "Enter sends it, changed or not - once")

    def test_the_values_a_frame_or_the_script_changed_and_only_those(self):
        path = Path(tempfile.mkdtemp()) / "panel.xml"
        path.write_text(f'''<application_database dbc_path="{DBC.as_posix()}"><pages><page>
<value label="temperature" binding_type="dbc" binding_value="EngineData.Temperature"/>
<value label="status" binding_value="status"/>
<io_box label="input" binding_value="input" value_type="string"/>
</page></pages></application_database>''', encoding="utf-8")
        panel = PanelView(parse_application_database(path), lambda *args: None, self.fail)
        self.assertEqual(panel.changed_values(), {})
        panel.on_message(0x300, bytes([0x01, 0x2C, 0, 0, 0, 0, 0, 0]))   # EngineData: 30 degC
        self.assertEqual(panel.changed_values(), {"temperature": "30 degC"}, "what the frame changed")
        self.assertEqual(panel.changed_values(), {}, "once")
        panel.on_message(0x123, bytes(8))                                  # a frame no control shows
        self.assertEqual(panel.changed_values(), {})
        panel.set_value("status", "ready")                                 # the script's
        self.assertEqual(panel.changed_values(), {"status": "ready"})

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
