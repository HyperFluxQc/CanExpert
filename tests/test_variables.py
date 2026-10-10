"""Structured variables: written as they are thought of (or as a C struct), laid out in bytes either way round,
kept by a script (api.var) and shown on a panel (the Variable List, controls named after a field) - and read and
written with the ECU, by DID and by memory address."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import struct
import tempfile
import time
import unittest
from pathlib import Path

from PyQt5.QtWidgets import QApplication

from canexpert.panel.database import parse_application_database
from canexpert.panel.variables import Variable, convert, parse_variables, split_path, variables_text

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent
CALIB = """Calib Data (memory 0x20001000, little-endian)
* uint32 temperature
* uint32 Axis
* uint32 FOC[32]
"""


def spin_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class DefinitionTest(unittest.TestCase):
    def test_the_notation_of_the_request(self):
        (calib,), problems = parse_variables(CALIB)
        self.assertEqual(problems, [])
        self.assertEqual((calib.name, calib.address, calib.did, calib.byte_order), ("Calib Data", 0x20001000, None,
                                                                                    "little"))
        self.assertEqual([field.declaration() for field in calib.fields],
                         ["uint32 temperature", "uint32 Axis", "uint32 FOC[32]"])
        self.assertEqual(calib.size, 136)
        self.assertEqual(calib.describe(), "Calib Data: 136 bytes, memory 0x20001000, little-endian")
        self.assertEqual(calib.paths()[:3] + calib.paths()[-1:], ["temperature", "Axis", "FOC[0]", "FOC[31]"])
        again, problems = parse_variables(variables_text([calib]))
        self.assertEqual((again[0], problems), (calib, []), "written out, it reads back the same")

    def test_a_c_struct_pasted_and_other_ways_to_write_it(self):
        structures, problems = parse_variables("""
typedef struct Motor {      // from motor.h
    uint16_t rpm;           /* per minute */
    int8_t trim[4];
    unsigned int flags;
    char name[8];
    float gain;
    double ratio;
    bool on;
} Motor;

Idle (DID 0x0110)
- u16 speed
Level: (@0x1000, big-endian)
  i32 offset
""")
        self.assertEqual(problems, [])
        motor, idle, level = structures
        self.assertEqual([f.declaration() for f in motor.fields], ["uint16 rpm", "int8 trim[4]", "uint32 flags",
                                                                   "char name[8]", "float32 gain", "float64 ratio",
                                                                   "bool on"])
        self.assertEqual((motor.size, motor.byte_order), (2 + 4 + 4 + 8 + 4 + 8 + 1, "big"))
        self.assertEqual((idle.did, idle.size), (0x0110, 2))
        self.assertEqual((level.name, level.address, level.fields[0].type), ("Level", 0x1000, "int32"))

    def test_what_is_written_wrong_says_where_and_what_was_meant(self):
        _structures, problems = parse_variables("""* uint32 lost
Calib (DID 0x0110, memory 0x10, sideways)
* uint23 x
* uint32 y[0]
* uint32 z[many]
* float t
* float t
this is not a field!
Calib
A.B
""")
        messages = {}
        for line, message in problems:
            messages[line] = messages.get(line, "") + message + " | "
        self.assertIn("has no variable above it", messages[1])
        self.assertIn("'sideways' is not an option", messages[2])
        self.assertIn("uint23 is not a type. Did you mean uint32?", messages[3])
        self.assertIn("an array holds 1 to 4096 values", messages[4])
        self.assertIn("an array's length is a number", messages[5])
        self.assertIn("Calib has two fields named t", messages[7])
        self.assertIn("There are two variables named Calib", messages[9])
        self.assertIn("a variable's name has no .", messages[10])
        self.assertIn("has a DID and a memory address", messages[2])
        self.assertTrue(any("this is not a field! has no field" in message for _line, message in problems))

    def test_in_braces_as_in_c(self):
        structures, problems = parse_variables("""MyList { uint32 data1; uint8 data2; }
Idle (DID 0x0110, little-endian) {
    uint16 speed;       // per minute
    uint8 gear
}
Pos
{
    int16 x, y, z;      /* three at once */
}
/* a comment
   over lines */
typedef struct {
    const uint16_t rpm;
} Motor;
struct Motor;
A { u8 a; } B { u16 b; }
""")
        self.assertEqual(problems, [])
        self.assertEqual([item.name for item in structures], ["MyList", "Idle", "Pos", "Motor", "A", "B"])
        my_list, idle, pos, motor = structures[:4]
        self.assertEqual([field.declaration() for field in my_list.fields], ["uint32 data1", "uint8 data2"])
        self.assertEqual((my_list.size, my_list.paths(), my_list.where()), (5, ["data1", "data2"], ""))
        self.assertEqual((idle.did, idle.byte_order, [field.line for field in idle.fields]), (0x0110, "little", [3, 4]))
        self.assertEqual([field.declaration() for field in pos.fields], ["int16 x", "int16 y", "int16 z"])
        self.assertEqual(([field.declaration() for field in motor.fields], motor.line), (["uint16 rpm"], 12))
        self.assertEqual(my_list.decode(bytes([0, 0, 1, 2, 3])), {"data1": 0x102, "data2": 3})

    def test_what_is_written_wrong_in_braces_says_where(self):
        _structures, problems = parse_variables("""MyList { uint32 data1; uint23 data2; }
Outer {
    struct Inner { uint8 b; } inner;
    MyList rows[4];
    uint8 on : 1;
    uint8 kept;
}
{ uint8 lost; }
} MyList;
typedef struct { uint8 x; };
Open {
    uint8 a;
""")
        messages = {}
        for line, message in problems:
            messages[line] = messages.get(line, "") + message + " | "
        self.assertIn("uint23 is not a type. Did you mean uint32?", messages[1])
        self.assertIn("Inner is inside Outer: a variable holds fields, not other variables", messages[3])
        self.assertIn("MyList is a variable, not a type", messages[4])
        self.assertIn("bit fields are not read", messages[5])
        self.assertIn("A { with no variable's name before it", messages[8])
        self.assertIn("A } with no { before it", messages[9])
        self.assertIn("A variable needs a name: typedef struct { ... } Name;", messages[10])
        self.assertIn("Open: the { is not closed - a } is missing", messages[11])
        self.assertEqual(len(problems), 8, "each said once")
        _structures, problems = parse_variables("Cal { u32 a; }\n/* not closed\nOther { u8 b; }")
        self.assertEqual(problems, [(2, "This /* comment is not closed: a */ is missing")])

    def test_bytes_either_way_round(self):
        (calib,), _ = parse_variables(CALIB)
        data = struct.pack("<34I", 25, 2, *range(32))
        values = calib.decode(data + b"\xAA")                       # what follows is not the variable's
        self.assertEqual((values["temperature"], values["Axis"], values["FOC"][31]), (25, 2, 31))
        self.assertEqual(calib.encode(values), data)
        (big,), _ = parse_variables(CALIB.replace("little-endian", "big-endian"))
        self.assertEqual(big.encode(values)[:4], b"\x00\x00\x00\x19")
        with self.assertRaisesRegex(ValueError, "takes 136 bytes; 4 came"):
            calib.decode(b"\x00" * 4)
        (mixed,), _ = parse_variables("M\n* char name[6]\n* bool on\n* int16 trim\n* float32 gain")
        raw = mixed.encode({"name": "abc", "on": True, "trim": -2, "gain": 1.5})
        self.assertEqual(raw, b"abc\x00\x00\x00\x01\xff\xfe" + struct.pack(">f", 1.5))
        self.assertEqual(mixed.decode(raw), {"name": "abc", "on": True, "trim": -2, "gain": 1.5})
        with self.assertRaisesRegex(ValueError, "M.trim: 40000 is outside int16"):
            mixed.encode({"trim": 40000})
        with self.assertRaisesRegex(ValueError, "M.name: 7 characters, and it holds 6"):
            mixed.encode({"name": "toolong"})
        self.assertEqual((convert("uint8", "0x10"), convert("bool", "on"), convert("float32", "2")), (16, True, 2.0))
        self.assertEqual((split_path("FOC[3]"), split_path("Axis")), (("FOC", 3), ("Axis", None)))


class VariableTest(unittest.TestCase):
    def setUp(self):
        (self.calib,), _ = parse_variables(CALIB)
        self.changes = []
        self.calls = []
        self.variable = Variable(self.calib, self, lambda name, value: self.changes.append((name, value)))

    # The UDS functions a Variable uses, recorded.
    def RMBA(self, address, size):
        self.calls.append(("RMBA", address, size))
        return type("Result", (), {"__bool__": lambda s: True, "data": struct.pack("<34I", 7, 8, *range(32))})()

    def WMBA(self, address, data):
        self.calls.append(("WMBA", address, bytes(data)))
        return True

    def test_fields_arrays_and_what_the_panel_is_told(self):
        variable = self.variable
        self.assertEqual((variable.temperature, variable["FOC[5]"], len(variable.FOC)), (0, 0, 32))
        foc = variable.FOC
        variable.FOC[3] = "0x10"
        variable.temperature = 25
        variable["Axis"] = 2.0
        self.assertEqual(self.changes, [("Calib Data.FOC[3]", 16), ("Calib Data.temperature", 25),
                                        ("Calib Data.Axis", 2)])
        self.assertEqual(variable.bytes()[:12], struct.pack("<3I", 25, 2, 0))
        self.assertTrue(variable.read())
        self.assertEqual(self.calls[-1], ("RMBA", 0x20001000, 136))
        self.assertEqual((variable.temperature, foc[31]), (7, 31), "an array held stays the variable's")
        self.assertEqual(self.changes[-1][0], "Calib Data", "a whole read: the whole variable")
        variable.write()
        self.assertEqual(self.calls[-1], ("WMBA", 0x20001000, struct.pack("<34I", 7, 8, *range(32))))
        with self.assertRaisesRegex(AttributeError, "no field Axes. Did you mean Axis?"):
            variable.Axes
        with self.assertRaisesRegex(IndexError, r"FOC\[32\]: it has 32 values, \[0\] to \[31\]"):
            variable.FOC[32] = 1
        with self.assertRaisesRegex(ValueError, "is outside uint32"):
            variable.temperature = -1
        (local,), _ = parse_variables("Local\n* uint8 x")
        with self.assertRaisesRegex(ValueError, "no DID and no memory address"):
            Variable(local, self).read()
        self.assertIn("Calib Data(temperature=7", repr(variable))


PANEL = f"""<application_database name="Vars">
    <variables>
        {CALIB.replace(chr(10), chr(10) + "        ")}
        Idle (DID 0x0110)
        * uint16 speed
    </variables>
    <pages><page name="Main">
        <var_list id="1" structure="Calib Data" format="hex" x="0" y="0" width="340" height="220"/>
        <var_list id="2" structure="Idle" x="350" y="0" width="300" height="100"/>
        <gauge id="3" binding_value="Idle.speed" label="Idle" min="0" max="2000" x="350" y="110"/>
        <var_list id="4" structure="Nothing" x="0" y="230"/>
        <output id="5" binding_value="log" x="350" y="330" width="300" height="60"/>
    </page></pages>
</application_database>"""


class PanelTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / "vars_2026-09-30.xml"
        self.path.write_text(PANEL, encoding="utf-8")

    def lists(self, panel):
        return {panel.definitions[key]["structure"]: widget for key, widget in panel.widgets.items()
                if panel.definitions[key]["kind"] == "var_list"}

    def test_the_database_keeps_them_and_says_what_is_wrong(self):
        database = parse_application_database(self.path)
        self.assertEqual([item.name for item in database["variables"]], ["Calib Data", "Idle"])
        self.path.write_text(PANEL.replace("* uint16 speed", "* uint61 speed"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Variables, line 7: uint61 is not a type"):
            parse_application_database(self.path)

    def test_a_variable_in_braces_on_the_panel(self):
        from canexpert.panel.view import PanelView
        self.path.write_text(PANEL.replace("""        Idle (DID 0x0110)
        * uint16 speed""", """        Idle (DID 0x0110) { uint16 speed; uint8 gear; }"""), encoding="utf-8")
        database = parse_application_database(self.path)
        idle = database["variables"][1]
        self.assertEqual((idle.name, idle.did, [field.name for field in idle.fields]), ("Idle", 0x0110, ["speed", "gear"]))
        panel = PanelView(database, lambda *args: None, lambda text: None)
        panel.set_value("Idle", {"speed": 800, "gear": 3})
        shown = self.lists(panel)["Idle"]
        self.assertEqual([shown.tree.topLevelItem(i).text(0) for i in range(2)], ["speed", "gear"])
        self.assertEqual(panel.controls["Idle.speed"].get_value(panel.widgets["Idle.speed"]), 800)
        self.path.write_text(PANEL.replace("""        Idle (DID 0x0110)
        * uint16 speed""", """        Idle (DID 0x0110) { uint16 speed; uint8 gear }
        Broken { uint61 x; }"""), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Variables, line 7: uint61 is not a type"):
            parse_application_database(self.path)

    def test_the_panel_shows_them_and_reports_what_is_typed(self):
        from canexpert.panel.view import PanelView
        panel = PanelView(parse_application_database(self.path), lambda *args: None, lambda text: None)
        lists = self.lists(panel)
        calib = lists["Calib Data"]
        self.assertEqual([calib.tree.topLevelItem(i).text(0) for i in range(3)], ["temperature", "Axis", "FOC"])
        self.assertEqual(calib.tree.topLevelItem(2).childCount(), 32)
        self.assertEqual([button.text() for button in calib.buttons], ["Read", "Write"])
        self.assertIn("No variable named 'Nothing'", lists["Nothing"].title.text())
        panel.set_value("Calib Data", {"temperature": 255, "Axis": 2, "FOC": list(range(32))})
        self.assertEqual((calib._items["temperature"].text(2), calib._items["FOC[31]"].text(2)), ("0xFF", "0x1F"),
                         "in the list's format")
        panel.set_value("Idle", {"speed": 800})                    # a whole variable: its fields' controls too
        self.assertEqual(panel.controls["Idle.speed"].get_value(panel.widgets["Idle.speed"]), 800)
        panel.set_value("Calib Data.FOC[3]", 7)
        self.assertEqual(calib._items["FOC[3]"].text(2), "0x7")
        typed = []
        panel.control_changed.connect(lambda name, value: typed.append((name, value)))
        calib._items["Axis"].setText(2, "0x20")
        calib._items["temperature"].setText(2, "warm")              # not a number: back to what it held
        calib.buttons[0].click()
        self.assertEqual(typed, [("Calib Data.Axis", "0x20"), ("Calib Data", "read")])
        self.assertEqual(calib._items["temperature"].text(2), "0xFF")
        self.assertIn("'warm'", calib._items["temperature"].toolTip(2))

    def test_read_and_written_with_the_simulated_ecu(self):
        from canexpert.designer.form_designer import TestPanelDialog
        database = parse_application_database(ROOT / "examples" / "calibration_2026-09-30.xml")
        script = (ROOT / "examples" / "calibration_2026-09-30_script.py").read_text(encoding="utf-8")
        dialog = TestPanelDialog(database, script, True)
        self.addCleanup(dialog.close)
        dialog.ecu.write_memory(0x10000, struct.pack("<34I", 25, 2, *range(32)))
        lists = self.lists(dialog.panel)
        log = dialog.panel.widgets["log"]
        self.assertTrue(spin_until(lambda: "Idle speed 800 rpm" in log.toPlainText()), log.toPlainText())
        self.assertEqual((lists["Calib Data"]._items["temperature"].text(2), lists["Idle"]._items["speed"].text(2)),
                         ("25", "800"), "read at the start: by memory and by DID")
        dialog.panel.widgets["unlock"].click()
        self.assertTrue(spin_until(lambda: "unlocked: yes" in log.toPlainText()), log.toPlainText())
        lists["Idle"]._items["speed"].setText(2, "900")
        self.assertTrue(spin_until(lambda: "Idle.speed = 900" in log.toPlainText()), "@on_variable")
        lists["Idle"].buttons[1].click()
        self.assertTrue(spin_until(lambda: dialog.ecu.dids[0x0110] == b"\x03\x84"), dialog.log_view.toPlainText())
        lists["Calib Data"]._items["FOC[3]"].setText(2, "0x1234")
        lists["Calib Data"].buttons[1].click()
        self.assertTrue(spin_until(lambda: dialog.ecu.read_memory(0x10000 + 20, 4) == b"\x34\x12\x00\x00"),
                        dialog.log_view.toPlainText())
        self.assertEqual(dialog.panel.controls["Idle.speed"].get_value(dialog.panel.widgets["Idle.speed"]), 900)

    def test_a_script_asking_for_a_variable_the_panel_has_not(self):
        from canexpert.config import validate_config
        from canexpert.panel.runtime import ScriptRuntime
        runtime = ScriptRuntime(None, validate_config({"name": "T", "request_id": 0x7E0, "response_id": 0x7E8}), {})
        runtime.set_variables(parse_application_database(self.path)["variables"])
        self.assertEqual(runtime.api.var("Idle").structure.did, 0x0110)
        with self.assertRaisesRegex(KeyError, "no variable 'Calib'. Did you mean Calib Data?"):
            runtime.api.var("Calib")


if __name__ == "__main__":
    unittest.main()
