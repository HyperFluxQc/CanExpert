"""Panel check: the typos in a panel's XML, its controls, its DBC bindings and its script, each with where it is and
what was probably meant - and no problem where there is none (the panels that come with CAN Expert)."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import shutil
import tempfile
import unittest
from pathlib import Path

from PyQt5.QtWidgets import QApplication

from canexpert.panel.check import (ERROR, FORM, SCRIPT, WARNING, Problem, check_panel, check_panel_file, closest,
                                   summary, typo_distance)
from canexpert.panel.database import parse_application_database
from canexpert.panel.view import PanelView

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent.parent
DBC = ROOT / "DBC" / "dummy_ecu.dbc"

PANEL = """<?xml version='1.0' encoding='utf-8'?>
<application_database name="Check" dbc_path="dummy_ecu.dbc">
    <pages>
        <page name="Main">
            <button id="1" x="10" y="10" binding_value="start" label="Start" handler="on_start_clicked"/>
            <value id="2" x="10" y="50" binding_type="dbc" binding_value="EngineData.Temperature" label="Temperature"/>
            <led id="3" x="10" y="90" binding_value="overheat" label="Overheat" on_color="#c62828"/>
            <output id="4" x="10" y="130" binding_value="log" label="Log"/>
        </page>
    </pages>
</application_database>
"""
PANEL_SCRIPT = """@on_start
def hello(api):
    api.ui.set_value("log", "started")


def on_start_clicked(api, value):
    vin = RDBI(0xF190)
    api.ui.set_value("log", vin.text)


@on_signal("EngineData.Temperature")
def temperature(api, value):
    api.ui.set_value("overheat", value > 80)
"""


class PanelCheckTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        shutil.copy(DBC, self.folder / "dummy_ecu.dbc")
        self.path = self.folder / "check_2026-09-29.xml"

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def check(self, panel=PANEL, script=PANEL_SCRIPT, *replacements):
        """The problems of PANEL and PANEL_SCRIPT on disk, with (old, new) replacements made in either."""
        for old, new in replacements:
            self.assertTrue(old in panel or old in script, old)
            panel, script = panel.replace(old, new), script.replace(old, new)
        self.path.write_text(panel, encoding="utf-8")
        self.path.with_name(self.path.stem + "_script.py").write_text(script, encoding="utf-8")
        return check_panel_file(self.path)

    def only(self, problems, severity=None):
        self.assertEqual(len(problems), 1, "\n\n".join(problem.text() for problem in problems))
        if severity:
            self.assertEqual(problems[0].severity, severity)
        return problems[0]

    def loads(self) -> bool:
        """Whether the panel on disk loads as Connect loads it: parsed, and its controls built."""
        try:
            PanelView(parse_application_database(self.path), lambda *args: None, lambda text: None)
            return True
        except Exception:
            return False

    def test_the_panels_that_come_with_can_expert_have_no_problems(self):
        for path in [*ROOT.glob("Databases/*.xml"), *ROOT.glob("examples/*.xml")]:
            self.assertEqual(check_panel_file(path), [], path)
        self.assertEqual(self.check(), [])

    def test_xml_that_cannot_be_read(self):
        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ('label="Start"', 'label="Start')), ERROR)
        self.assertEqual((problem.kind, problem.line), (FORM, 5))
        self.assertIn("The XML cannot be read", problem.message)
        self.assertIn("a quote is missing", problem.hint)
        self.assertEqual(Path(problem.path), self.path)
        self.assertIn('label="Start handler=', problem.source)
        text = problem.text()
        self.assertIn("check_2026-09-29.xml, line 5, column", text)
        caret = text.splitlines()[2]
        self.assertEqual(text.splitlines()[1][caret.index("^")], "o", "the ^ under on_start_clicked, where it fails")

        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ("</page>", "</pgae>")), ERROR)
        self.assertEqual(problem.message, "</pgae> does not close <page>, which line 4 opens")
        self.assertEqual(problem.hint, "Did you mean </page>?")
        self.assertEqual(problem.line, 9)

        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ("</application_database>", "")), ERROR)
        self.assertIn("ends before <application_database>, which line 2 opens, is closed", problem.message)

        problems = self.check(PANEL, PANEL_SCRIPT, ("<application_database", "<aplication_database"),
                              ("</application_database>", "</aplication_database>"))
        problem = self.only(problems, ERROR)
        self.assertIn("begins with <aplication_database>", problem.message)
        self.assertEqual(problem.hint, "Did you mean <application_database>?")
        self.assertFalse(self.loads())

    def test_controls_and_properties_it_does_not_know(self):
        problems = self.check(PANEL, PANEL_SCRIPT, ("<led ", "<lde "), ('on_color="#c62828"', 'on_colour="#c62828"'))
        tag = next(problem for problem in problems if "<lde>" in problem.message)
        self.assertEqual((tag.severity, tag.hint), (WARNING, "Did you mean <led>?"))
        self.assertIn("left out of the panel", tag.message)
        self.assertEqual((tag.line, tag.column), (7, 13))
        missing = next(problem for problem in problems if problem.kind == SCRIPT)
        self.assertEqual(missing.message, 'There is no control named "overheat" on the panel', "and so, the script's")
        self.assertEqual(missing.line, 13)
        self.assertEqual(len(problems), 2, "an unknown element's attributes are not reported one by one")

        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ('on_color="#c62828"', 'on_colour="#c62828"')), WARNING)
        self.assertEqual(problem.message, 'on_colour="#c62828": a led has no property on_colour, so it is ignored')
        self.assertEqual(problem.hint, 'Did you mean "on_color"?')
        self.assertEqual(problem.source[problem.column - 1:].split("=")[0], "on_colour", "the column: the attribute")
        self.assertEqual((problem.control, problem.page, problem.position), ("overheat", "Main", (0, 2)))

        problems = self.check(PANEL, PANEL_SCRIPT, ('on_color="#c62828"', 'on_color="#c6282" blink="yes please"'),
                              ('label="Temperature"', 'label="Temperature" format="asci"'))
        self.assertEqual([problem.message for problem in problems], [
            'format="asci" is not one of: auto, decimal, hex, binary, ascii',
            'on_color="#c6282" is not a colour: the default colour is used',
            'blink="yes please" is neither True nor False: it is taken as False'])
        self.assertEqual(problems[0].hint, 'Did you mean "ascii"?')
        self.assertTrue(self.loads(), "all warnings: the panel loads")

        problem = self.only(self.check(PANEL.replace("</pages>", '</pages><label text="Lost"/>'), PANEL_SCRIPT), WARNING)
        self.assertIn("outside <pages>: it is not shown", problem.message)

    def test_values_the_panel_cannot_take(self):
        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ('x="10" y="10"', 'x="1O" y="10"')), ERROR)
        self.assertEqual(problem.message, 'x="1O" is not a whole number')
        self.assertEqual(problem.hint, 'Did you mean "10"?')
        self.assertTrue(problem.source[problem.column - 1:].startswith('x="1O"'))
        self.assertFalse(self.loads())
        for replacement, message in (
                (('x="10" y="50"', 'x="10" y="50" can_id="0x7G0"'), 'can_id="0x7G0" is not a CAN identifier'),
                (('x="10" y="50"', 'x="10" y="50" min="90" max="10"'), 'min="90" is more than max="10"'),
                (('x="10" y="50"', 'x="10" y="50" byte="8"'), 'byte="8": a CAN byte or bit position is 0 to 7'),
                (('x="10" y="50"', 'x="10" y="50" scale="1,5"'), 'scale="1,5" is not a number'),
                (('x="10" y="10"', 'x="10" y="10" data="01 0X"'), 'data="01 0X" is not bytes in hex')):
            problem = self.only(self.check(PANEL, PANEL_SCRIPT, replacement), ERROR)
            self.assertEqual(problem.message, message)
            self.assertFalse(self.loads(), message)

    def test_two_controls_of_one_name(self):
        second = '<led binding_value="log"/>\n            <output id="4"'
        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ('<output id="4"', second)), ERROR)
        self.assertEqual(problem.message, 'Two controls are named "log"')
        self.assertEqual(problem.line, 9)
        self.assertIn("The other one is on line 8.", problem.hint)
        self.assertFalse(self.loads(), "the panel refuses it too")

    def test_dbc_bindings(self):
        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ('"EngineData.Temperature" label', '"EngineData.Temprature" label')),
                            WARNING)
        self.assertEqual(problem.message, '"EngineData.Temprature" is not in dummy_ecu.dbc: EngineData has no signal '
                                          'Temprature')
        self.assertEqual(problem.hint, 'Did you mean "EngineData.Temperature"?')
        self.assertTrue(self.loads(), "the control only stays empty")
        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ('"EngineData.Temperature" label', '"EngineDta.Temperature" label')),
                            WARNING)
        self.assertEqual(problem.hint, 'Did you mean "EngineData.Temperature"?')

        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ('dbc_path="dummy_ecu.dbc"', 'dbc_path="dumy_ecu.dbc"')), ERROR)
        self.assertEqual(problem.message, "The DBC dumy_ecu.dbc does not exist")
        self.assertEqual(problem.hint, 'Did you mean "dummy_ecu.dbc"?')
        self.assertFalse(self.loads())

        (self.folder / "broken.dbc").write_text("BO_ oops", encoding="utf-8")
        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ('dbc_path="dummy_ecu.dbc"', 'dbc_path="broken.dbc"')), ERROR)
        self.assertTrue(problem.message.startswith("The DBC broken.dbc cannot be read"), problem.message)

        problems = self.check(PANEL, PANEL_SCRIPT, (' dbc_path="dummy_ecu.dbc"', ""))
        self.assertEqual([problem.message for problem in problems], [
            "1 control is bound to DBC signals, but the panel has no DBC: they stay empty",
            '"EngineData.Temperature" is a DBC signal, and the panel has no DBC'])

    def test_the_script(self):
        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ("def hello(api):", "def hello(api)")), ERROR)
        self.assertEqual((problem.kind, problem.line, problem.message), (SCRIPT, 2, "expected ':': the script cannot start"))
        self.assertEqual(Path(problem.path).name, "check_2026-09-29_script.py")

        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ("RDBI(0xF190)", "RBDI(0xF190)")), WARNING)
        self.assertEqual((problem.line, problem.column), (7, 11))
        self.assertEqual(problem.message, "RBDI is not defined: this fails when it runs")
        self.assertEqual(problem.hint, "Did you mean RDBI?")
        problem = self.only(self.check(PANEL, PANEL_SCRIPT + "\nLIMIT = limt\n"), ERROR)
        self.assertEqual(problem.message, "limt is not defined: the script stops here when it starts")

        problem = self.only(self.check(PANEL, PANEL_SCRIPT, ("def on_start_clicked", "def on_strat_clicked")), WARNING)
        self.assertEqual((problem.kind, problem.line), (FORM, 5), "about the button: its handler")
        self.assertEqual(problem.message, 'The button "start" calls on_start_clicked(), which the script does not define')
        self.assertEqual(problem.hint, "Did you mean on_strat_clicked()?")

        problems = self.check(PANEL, PANEL_SCRIPT, ('set_value("log", vin.text)', 'set_value("lgo", vin.text)'),
                              ('@on_signal("EngineData.Temperature")', '@on_signal("EngineData.Temp")'))
        self.assertEqual([(problem.line, problem.message, problem.hint) for problem in problems], [
            (8, 'There is no control named "lgo" on the panel', 'Did you mean "log"?'),
            (11, '"EngineData.Temp" is not in dummy_ecu.dbc: EngineData has no signal Temp', "")])

        problem = self.only(self.check(PANEL, PANEL_SCRIPT + '\n\n@on_message("EngineDat")\ndef frame(api, frame):\n'
                                                       '    pass\n'), ERROR)
        self.assertEqual(problem.message, "dummy_ecu.dbc has no message EngineDat: the script stops here when it starts")
        self.assertEqual(problem.hint, 'Did you mean "EngineData"?')
        problem = self.only(self.check(PANEL, PANEL_SCRIPT + '\n\ndef send(api):\n    api.send_message("EngineData", '
                                                       'Temprature=20)\n'), WARNING)
        self.assertEqual(problem.message, "EngineData has no signal Temprature")
        self.assertEqual(problem.hint, "Did you mean Temperature?")
        self.assertEqual(self.check(PANEL, PANEL_SCRIPT + "\n\nfrom math import *\n"), [], "import *: names are not guessed")

    def test_a_form_in_memory_names_the_control(self):
        problems = check_panel(PANEL.replace('on_color="#c62828"', 'on_colour="#c62828"'), "", PANEL_SCRIPT, "panel_script.py",
                               self.folder)
        problem = self.only(problems, WARNING)
        self.assertEqual((problem.path, problem.line, problem.source), ("", 0, ""))
        self.assertEqual(problem.where(), "page Main, control overheat")
        problem = self.only(check_panel(PANEL, "", PANEL_SCRIPT.replace("RDBI(", "RBDI("), "panel_script.py", self.folder))
        self.assertEqual(problem.where(), "panel_script.py, line 7, column 11")

    def test_texts(self):
        self.assertEqual(summary([]), "no problems")
        errors = [Problem(ERROR, "a"), Problem(ERROR, "b"), Problem(WARNING, "c")]
        self.assertEqual(summary(errors), "2 errors and 1 warning")
        problem = Problem(ERROR, "x is wrong", FORM, "p.xml", 3, 40, " " * 8 + "a" * 150 + "\n", "Put it right.")
        lines = problem.text().splitlines()
        self.assertEqual(lines[0], "Error - p.xml, line 3, column 40: x is wrong")
        self.assertLessEqual(len(lines[1]), 4 + 106, "a long line: the part around the column")
        self.assertEqual(lines[3], "    Put it right.")
        self.assertEqual((typo_distance("lde", "led"), typo_distance("Presure", "Pressure")), (1, 1))
        self.assertEqual((closest("lde", ["led", "slider"]), closest("DBC", ["dbc"]), closest("zzz", ["led"])),
                         ("led", "dbc", None))

    def test_a_fault_of_the_check_is_a_warning_not_an_exception(self):
        from unittest.mock import patch
        from canexpert.panel import check
        with patch.object(check._Check, "check_controls", side_effect=RuntimeError("boom")):
            problem = self.only(self.check(), WARNING)          # a Connect or a test goes on
        self.assertEqual(problem.message, "The check could not finish: boom")

    def test_the_script_names_it_knows_are_the_runtimes(self):
        from canexpert.config import validate_config
        from canexpert.panel.runtime import ScriptRuntime, script_globals
        runtime = ScriptRuntime(None, validate_config({"name": "Check", "request_id": 0x7E0, "response_id": 0x7E8}), {})
        self.assertEqual(set(runtime._namespace(Path("x.py"))), script_globals())


if __name__ == "__main__":
    unittest.main()
