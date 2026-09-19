#!/usr/bin/env python3
"""Start CAN Expert: python main.py (--smoke-test only checks that the main window can be built)."""
import importlib.util
import sys

if sys.version_info < (3, 10):
    sys.exit(f"CAN Expert requires Python 3.10 or newer (this is {sys.version.split()[0]}, {sys.executable}).")
for module, package in (("can", "python-can"), ("PyQt5", "PyQt5")):  # cantools, pyqtgraph, odxtools are optional
    if importlib.util.find_spec(module) is None:
        sys.exit(f"Missing dependency: {package}\nInstall with: pip install -r requirements.txt")

from canexpert.main_window import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
