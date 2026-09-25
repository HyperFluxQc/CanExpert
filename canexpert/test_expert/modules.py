"""
CAN Expert's test modules (canexpert.testing.runner) in a TestExpert run: each module is a group of the run,
after the generated tests - its setup before its first test case (a failure blocks the module's cases), its
before_each and after_each around each one, its teardown after its last (a failure there is a warning). Its UDS
functions (RDBI, DSC...) go through TestExpert's tester; wait_for_frame() and wait_for_signal() read the run's
bus, decoded with the plan's symbol databases (DBC...).

A module's test cases are named after its file and their function - "module_dummy_ecu_checks.identification" -
so a plan can leave one out; a module that cannot be read is a test case that fails, saying why.
"""
from __future__ import annotations

import queue
import re
import time
from dataclasses import dataclass
from pathlib import Path

from canexpert.testing.runner import TestModule, load_module, uds_names

GROUP_PREFIX = "Module: "
MAX_AGE = 60.0              # a frame's timestamp older than this is not on this computer's clock


@dataclass
class LoadedModule:
    path: Path
    module: TestModule | None = None
    error: str = ""                     # why the file could not be read

    @property
    def title(self) -> str:
        return self.module.title if self.module is not None else self.path.name

    @property
    def key(self) -> str:
        """The first part of its test cases' names."""
        return "module_" + (re.sub(r"[^a-z0-9]+", "_", self.path.stem.lower()).strip("_") or "file")


def load_modules(paths) -> list[LoadedModule]:
    """Read test module files (their code runs: a module is Python); one that cannot be read keeps why."""
    loaded = []
    for path in paths:
        path = Path(path)
        try:
            loaded.append(LoadedModule(path, load_module(path, uds_names())))
        except Exception as exc:        # the user's file: missing, or broken
            loaded.append(LoadedModule(path, None, f"{type(exc).__name__}: {exc}"))
    return loaded


class BusFrames:
    """The frames a module's wait_for_frame() reads: the tester's bus - read only then, the run being its only
    reader, one step at a time - each with the moment it arrived (its timestamp, when that is on this
    computer's clock), so a frame that waited in the queue is not taken for a new one."""

    def __init__(self, tester):
        self.tester = tester

    def get(self, timeout=None):
        message = self.tester.bus.recv(timeout)
        if message is None:
            raise queue.Empty
        now = time.monotonic()
        age = time.time() - (message.timestamp or 0)
        return (now - age if 0 <= age < MAX_AGE else now), message


class Symbols:
    """decode(can_id, data) -> (message name, {signal: value}) with symbol databases (canexpert.symbols)."""

    def __init__(self, paths=()):
        self.databases = None
        self.errors = []
        paths = [str(path) for path in paths]
        if paths:
            from canexpert.symbols import SymbolDatabases
            from canexpert.testing.window import MemorySettings
            self.databases = SymbolDatabases(paths, settings=MemorySettings())
            self.errors = list(self.databases.errors)

    def decode(self, can_id, data):
        if not self.databases:
            return "", {}
        return self.databases.name(can_id), self.databases.decode(can_id, data)
