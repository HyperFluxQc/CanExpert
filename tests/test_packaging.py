"""What the Windows build and CI rely on: the version, the icons, the startup check of a built program, the
libraries imported only when first used, and the annotations a failed CI run leaves on the pull request."""
import importlib.util
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import re
import subprocess
import sys
import unittest
from pathlib import Path

from PyQt5.QtWidgets import QApplication

import canexpert
from canexpert.main_window import startup_problems
from canexpert.ui_common import app_icon

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


def tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OUTPUT = """test_ok (test_a.Case) ... ok
test_broken (test_a.Case) ... FAIL
test_raises (test_a.Case) ... ERROR

======================================================================
FAIL: test_broken (test_a.Case)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_a.py", line 9, in test_broken
    self.assertEqual(1, 2)
AssertionError: 1 != 2

======================================================================
ERROR: test_raises (test_a.Case)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_a.py", line 12, in test_raises
    raise KeyError("x")
KeyError: 'x'

----------------------------------------------------------------------
Ran 3 tests in 0.1s

FAILED (failures=1, errors=1)
"""
CRASH = """test_first (test_b.Case) ... ok
test_floats (test_b.Case) ... Fatal Python error: Segmentation fault

Current thread 0x00007f (most recent call first):
  File "/work/canexpert/workspace.py", line 88 in drop_empty_floating
  File "/work/tests/test_b.py", line 40 in test_floats
"""


class PackagingTest(unittest.TestCase):
    def test_the_version_is_one_windows_can_hold(self):
        self.assertRegex(canexpert.__version__, r"^\d+\.\d+\.\d+$")

    def test_both_programs_have_their_icon(self):
        for name in ("canexpert", "dummy_ecu", "test_expert"):
            icon = app_icon(name)
            self.assertFalse(icon.isNull(), name)
            self.assertIn(256, [size.width() for size in icon.availableSizes()], name)

    def test_the_startup_check_finds_everything_here(self):
        self.assertEqual(startup_problems(), [])

    def test_the_spec_and_the_build_script_agree(self):
        spec = (ROOT / "CanExpert.spec").read_text(encoding="utf-8")
        for name in ("CanExpert", "DummyECU", "TestExpert"):
            self.assertIn(f'"{name}"', spec)
        self.assertIn('contents_directory="."', spec, "the data folders sit beside the programs")
        self.assertIn('"docs/images"', spec, "the manual's pictures go with it")
        build = (ROOT / "tools" / "build_windows.py").read_text(encoding="utf-8")
        self.assertIn('"CanExpert.exe", "DummyECU.exe", "TestExpert.exe"', build)


class StartupTest(unittest.TestCase):
    """numpy, pyqtgraph and odxtools are imported where they are first used: half a second less to start."""

    def test_starting_imports_none_of_them(self):
        code = ("import sys, canexpert.main_window; "
                "print(sorted(name for name in ('numpy', 'odxtools', 'pyqtgraph') if name in sys.modules))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                                env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
        self.assertEqual(result.stdout.strip(), "[]", result.stderr)

    def test_a_lazy_module_is_imported_at_its_first_use(self):
        from canexpert.lazy import LazyModule, installed
        loads = []
        module = LazyModule(lambda: loads.append(True) or __import__("json"))
        self.assertEqual(loads, [], "not yet")
        self.assertEqual(module.dumps([1]), "[1]")
        self.assertEqual(module.loads("2"), 2)
        self.assertEqual(loads, [True], "once")
        self.assertTrue(installed("json"))
        self.assertFalse(installed("no_such_module_anywhere"))

    def test_the_build_still_finds_them(self):
        # PyInstaller packs what import statements name: the late imports are still statements, in functions.
        for path, module in (("canexpert/panel/controls.py", "pyqtgraph"), ("canexpert/can_logger.py", "numpy"),
                             ("canexpert/can_logger.py", "pyqtgraph"), ("canexpert/odx_services.py", "odxtools")):
            source = (ROOT / path).read_text(encoding="utf-8")
            self.assertRegex(source, rf"(?m)^ +(import {module}$|from {module} import )", f"{module} in {path}")

    def test_a_trend_and_the_logger_bring_them_in(self):
        from canexpert.can_logger import pg as logger_pg
        from canexpert.panel.controls import TrendWidget
        trend = TrendWidget(10, "#1f77b4")
        self.addCleanup(trend.deleteLater)
        self.assertIsNotNone(trend.plot, "a trend draws with pyqtgraph")
        self.assertTrue(callable(logger_pg.mkPen))


class AnnotationsTest(unittest.TestCase):
    def setUp(self):
        self.module = tool("ci_annotations")

    def test_failures_and_errors(self):
        found = self.module.annotations(OUTPUT)
        self.assertEqual([title for title, _ in found], ["FAIL: test_broken (test_a.Case)",
                                                          "ERROR: test_raises (test_a.Case)"])
        self.assertIn("AssertionError: 1 != 2", found[0][1])
        self.assertNotIn("Ran 3 tests", found[1][1])

    def test_a_crash_names_the_test_that_was_running(self):
        (title, message), = self.module.annotations(CRASH)
        self.assertEqual(title, "Crash while running test_floats (test_b.Case)")
        self.assertIn("drop_empty_floating", message)

    def test_messages_keep_their_lines_in_one_annotation(self):
        line = self.module.escape("a%b\nc")
        self.assertEqual(line, "a%25b%0Ac")
        self.assertFalse(re.search(r"\n", line))


if __name__ == "__main__":
    unittest.main()
