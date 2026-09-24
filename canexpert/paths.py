"""Folders CAN Expert reads and writes: next to main.py, or next to the executable when frozen (PyInstaller)."""
import sys
from pathlib import Path

APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
CONFIG_DIR = APP_DIR / "Configurations"
DATABASES_DIR = APP_DIR / "Databases"
DBC_DIR = APP_DIR / "DBC"
ODX_DIR = APP_DIR / "ODX"
EXAMPLES_DIR = APP_DIR / "examples"
DOCS_DIR = APP_DIR / "docs"
EXAMPLE_FIRMWARE_DIR = EXAMPLES_DIR / "firmware"
RESOURCES_DIR = Path(__file__).resolve().parent / "resources"    # the icons; inside the package, also when frozen
