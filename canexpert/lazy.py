"""
Libraries imported where they are first used rather than when CAN Expert starts: numpy and pyqtgraph (a panel's
trends, the CAN Logger) and odxtools (ODX services) were half a second of the 0.8 s its main window took to import.
"""
import importlib.util


def installed(name: str) -> bool:
    """Whether a module can be imported - found without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class LazyModule:
    """A module imported at the first use of one of its names. load() holds the import statement itself, so the
    Windows build - PyInstaller, which finds what to pack by its import statements - still packs the module."""

    def __init__(self, load):
        self._load = load
        self._module = None

    def __getattr__(self, name):
        if self._module is None:
            self._module = self._load()
        return getattr(self._module, name)
