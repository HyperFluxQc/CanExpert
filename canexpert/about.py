"""
The About box: CAN Expert's version, the libraries it runs on and the CAN adapter drivers installed - what a
bug report needs, with a button that copies it.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import importlib
import importlib.metadata
import os
import platform
import sys
from datetime import datetime

from PyQt5.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, Qt
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

import canexpert
from canexpert.ui_common import app_icon

# (label, distribution, module): the version comes from the distribution's metadata, else the module's
# __version__.
LIBRARIES = (("python-can", "python-can", "can"), ("cantools", "cantools", "cantools"),
             ("odxtools", "odxtools", "odxtools"), ("pyqtgraph", "pyqtgraph", "pyqtgraph"),
             ("PyQtAds", "PyQtAds", "PyQtAds"))
# The driver DLL each adapter's python-can interface loads, 64-bit name first.
DRIVERS = (("Kvaser CANlib", ("canlib32",)), ("Vector XL Driver Library", ("vxlapi64", "vxlapi")),
           ("IXXAT VCI", ("vcinpl2", "vcinpl")))
NOT_INSTALLED = "not installed"


def package_version(distribution: str, module: str | None = None) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        pass
    try:
        return str(getattr(importlib.import_module(module), "__version__")) if module else NOT_INSTALLED
    except (ImportError, AttributeError):
        return NOT_INSTALLED


def file_version(path: str) -> str | None:
    """The version resource of a Windows DLL ("5.44.0.0"), or None where there is none to read."""
    if sys.platform != "win32":
        return None
    try:
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(path, 0, size, buffer):
            return None
        info, length = ctypes.c_void_p(), ctypes.c_uint()
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(info), ctypes.byref(length)) or length.value < 52:
            return None
        words = (ctypes.c_uint32 * 13).from_address(info.value)     # VS_FIXEDFILEINFO
        most, least = words[2], words[3]                             # dwFileVersionMS, dwFileVersionLS
        return f"{most >> 16}.{most & 0xFFFF}.{least >> 16}.{least & 0xFFFF}"
    except (AttributeError, OSError, ValueError):
        return None


def driver_version(names) -> str:
    """Where the driver's DLL is found, the version it carries; else "not installed"."""
    for name in names:
        path = ctypes.util.find_library(name)
        if path:
            return file_version(path) or "installed"
    return NOT_INSTALLED


def build_date() -> str:
    """When the Windows program was built (its executable's date), or that CAN Expert runs from source."""
    if not getattr(sys, "frozen", False):
        return "running from source"
    try:
        return datetime.fromtimestamp(os.path.getmtime(sys.executable)).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "unknown"


def versions() -> list[tuple[str, str]]:
    """(component, version) for the About box, CAN Expert first."""
    rows = [("CAN Expert", canexpert.__version__), ("Built", build_date()),
            ("Python", f"{platform.python_version()} ({platform.architecture()[0]})"),
            ("Qt", QT_VERSION_STR), ("PyQt5", PYQT_VERSION_STR)]
    rows += [(label, package_version(distribution, module)) for label, distribution, module in LIBRARIES]
    rows += [(label, driver_version(names)) for label, names in DRIVERS]
    rows.append(("Operating system", platform.platform()))
    return rows


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About CAN Expert")
        self.rows = versions()
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(64, 64))
        header.addWidget(icon)
        title = QLabel(f"<b style='font-size: 16px;'>CAN Expert {canexpert.__version__}</b><br>"
                       "CAN and UDS tool: panel databases with Python scripts, Form Designer, Trace, CAN Logger,<br>"
                       "UDS Console, firmware flashing and a simulated ECU.")
        title.setTextFormat(Qt.RichText)
        header.addWidget(title, 1)
        layout.addLayout(header)
        self.table = QTableWidget(len(self.rows), 2)
        self.table.setHorizontalHeaderLabels(["Component", "Version"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        for row, (name, version) in enumerate(self.rows):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            item = QTableWidgetItem(version)
            if version == NOT_INSTALLED:
                item.setForeground(Qt.gray)
            self.table.setItem(row, 1, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumSize(520, 30 + 26 * len(self.rows))
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        copy = QPushButton("Copy")
        copy.setToolTip("Copy these versions, e.g. for a bug report")
        copy.clicked.connect(self.copy)
        buttons.addWidget(copy)
        buttons.addStretch()
        close = QPushButton("OK")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def text(self) -> str:
        return "\n".join(f"{name}: {version}" for name, version in self.rows)

    def copy(self):
        QApplication.clipboard().setText(self.text())
