"""
Symbol databases: the DBC files the application knows about, shared by every window.

A panel database keeps its own DBC (the designer binds controls to it), but the Trace window, the
CAN Logger and the transmit list need symbols without a panel, so the list lives with the application
and is remembered in the settings. Nothing here reads or writes a configuration or panel file.
"""
from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from canexpert.paths import DBC_DIR
from canexpert.ui_common import app_settings

try:
    import cantools
    HAS_CANTOOLS = True
except ImportError:
    HAS_CANTOOLS = False

SETTING = "symbol_databases"   # settings: the DBC paths, one per line


def read_paths(settings=None) -> list[str]:
    """The DBC paths saved by write_paths(); missing files are dropped."""
    settings = settings or app_settings()
    stored = settings.value(SETTING, "", type=str) or ""
    return [line for line in stored.splitlines() if line.strip() and Path(line).exists()]


def write_paths(paths, settings=None) -> None:
    (settings or app_settings()).setValue(SETTING, "\n".join(str(path) for path in paths))


class SymbolDatabases(QObject):
    """The loaded DBC files and the decoding every window shares.

    Frame identifiers are resolved against the databases in order, so the first file that defines a
    message wins; a file that cannot be read is kept in errors instead of failing the others.
    """
    changed = pyqtSignal()

    def __init__(self, paths=None, parent=None, settings=None):
        super().__init__(parent)
        self.settings = settings or app_settings()
        self.paths: list[str] = []
        self.databases: list[tuple[str, object]] = []
        self.errors: list[str] = []
        self._by_frame: dict[int, object] = {}
        self.set_paths(paths if paths is not None else read_paths(self.settings))

    # --- the list ---------------------------------------------------------------------

    def set_paths(self, paths, remember=False):
        """Load these DBC files, replacing the current ones."""
        self.paths, self.databases, self.errors, self._by_frame = [], [], [], {}
        for path in [str(p) for p in paths]:
            if path in self.paths:
                continue
            self.paths.append(path)
            if not HAS_CANTOOLS:
                self.errors.append("Install cantools to use DBC files: pip install cantools")
                continue
            try:
                database = cantools.database.load_file(path)
            except Exception as exc:                      # cantools raises many different errors
                self.errors.append(f"{Path(path).name}: {exc}")
                continue
            self.databases.append((path, database))
            for message in database.messages:
                self._by_frame.setdefault(message.frame_id, message)
        if remember:
            write_paths(self.paths, self.settings)
        self.changed.emit()

    def add(self, path, remember=True):
        self.set_paths([*self.paths, str(path)], remember)

    def remove(self, path, remember=True):
        self.set_paths([p for p in self.paths if p != str(path)], remember)

    def __bool__(self):
        return bool(self.databases)

    # --- symbols ----------------------------------------------------------------------

    def message(self, frame_id: int):
        """The DBC message for a frame identifier, or None."""
        return self._by_frame.get(frame_id)

    def name(self, frame_id: int) -> str:
        message = self._by_frame.get(frame_id)
        return message.name if message is not None else ""

    def messages(self) -> list:
        """Every message of every loaded database, sorted by name."""
        return sorted(self._by_frame.values(), key=lambda message: message.name.lower())

    def decode(self, frame_id: int, data) -> dict:
        """{signal name: physical value} for a received frame; {} when nothing decodes it."""
        message = self._by_frame.get(frame_id)
        if message is None:
            return {}
        try:
            return message.decode(bytes(data), decode_choices=False, allow_truncated=True)
        except Exception:                                  # a frame that does not fit the definition
            return {}

    def signal_names(self) -> list[str]:
        """"Message.Signal" for every signal of every database."""
        return [f"{message.name}.{signal.name}" for message in self.messages() for signal in message.signals]

    def unit(self, full_name: str) -> str:
        message_name, _, signal_name = full_name.partition(".")
        for message in self._by_frame.values():
            if message.name == message_name:
                for signal in message.signals:
                    if signal.name == signal_name:
                        return signal.unit or ""
        return ""


class SymbolDatabaseDialog(QDialog):
    """Add and remove the DBC files the application uses."""

    def __init__(self, symbols: SymbolDatabases, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Symbol databases")
        self.resize(620, 320)
        self.symbols = symbols
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("DBC files used by the Trace window, the CAN Logger and the transmit list."))
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        self.status = QLabel("")
        self.status.setStyleSheet("color: gray;")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.add_btn = QPushButton("Add DBC...")
        self.add_btn.clicked.connect(self.add_file)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.remove_selected)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.add_btn)
        buttons.addWidget(self.remove_btn)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self):
        self.list.clear()
        self.list.addItems(self.symbols.paths)
        messages = len(self.symbols.messages())
        problems = "; ".join(self.symbols.errors)
        self.status.setText(problems or (f"{messages} message(s) from {len(self.symbols.databases)} file(s)"
                                         if messages else "No database loaded"))
        self.status.setStyleSheet("color: red;" if problems else "color: gray;")

    def add_file(self):
        start = DBC_DIR if Path(DBC_DIR).exists() else Path.home()
        path, _ = QFileDialog.getOpenFileName(self, "Add DBC file", str(start),
                                              "DBC files (*.dbc);;All files (*.*)")
        if path:
            self.symbols.add(path)
            self.refresh()

    def remove_selected(self):
        for item in self.list.selectedItems():
            self.symbols.remove(item.text())
        self.refresh()
