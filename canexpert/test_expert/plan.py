"""
Test plans: everything a TestExpert run needs, in one JSON file - the description, the ECU connection, the
settings, the key source, the tests left out, the sequences, the NRC policy, the accepted deviations, what
discovery asks, the variant and CAN Expert's test modules to run with the generated tests - so
the same run can be made again, from the window or without it (test_expert.py plan.json --run), on a bench or
a CI server.

Paths in a plan are kept relative to the plan's folder when they can be, so a plan and its CDD move together.
Identifiers are written as hexadecimal text ("0x7E0"); numbers are read too.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from canexpert.test_expert.description import EcuDescription
from canexpert.test_expert.discovery import DiscoveryOptions
from canexpert.test_expert.generator import Options
from canexpert.test_expert.policy import Deviation, NrcPolicy
from canexpert.test_expert.sequences import Sequence
from canexpert.test_expert.variants import Identification

PLAN_FORMAT = "TestExpert plan"
PLAN_VERSION = 1
OPTION_NAMES = [item.name for item in fields(Options) if item.name != "key"]


class PlanError(ValueError):
    """A file that is not a test plan, or one that cannot be used."""


def _number(value, default=0) -> int:
    """7E0 as 2016, "0x7E0" or "7E0" (hexadecimal text)."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return int(value.strip().lower().removeprefix("0x"), 16)
    return int(value)


def _hex(value) -> str | None:
    return None if value is None else f"0x{value:X}"


@dataclass
class Connection:
    interface: str = "kvaser"
    channel: str = "0"
    bitrate: int = 500000
    request_id: int = 0x7E0
    response_id: int = 0x7E8
    functional_id: int | None = 0x7DF
    extended: bool = False
    padding: int | None = 0xCC            # None: frames as short as their content

    def transport(self) -> dict:
        """The UDS transport of canexpert.config.uds_transport."""
        return {"request_id": self.request_id, "response_id": self.response_id, "timeout": 2.0,
                "extended": self.extended, "address_byte": None, "padding": self.padding, "block_size": 0,
                "st_min": 0}

    def text(self) -> str:
        return (f"{self.interface} {self.channel}, {self.bitrate} bit/s, "
                f"{self.request_id:X}/{self.response_id:X}")

    def to_dict(self) -> dict:
        return {"interface": self.interface, "channel": str(self.channel), "bitrate": self.bitrate,
                "request_id": _hex(self.request_id), "response_id": _hex(self.response_id),
                "functional_id": _hex(self.functional_id), "extended": self.extended,
                "padding": _hex(self.padding)}

    @classmethod
    def from_dict(cls, values: dict) -> "Connection":
        values = values or {}
        default = cls()
        functional = values.get("functional_id", default.functional_id)
        padding = values.get("padding", default.padding)
        return cls(str(values.get("interface", default.interface)), str(values.get("channel", default.channel)),
                   int(values.get("bitrate", default.bitrate)), _number(values.get("request_id"), default.request_id),
                   _number(values.get("response_id"), default.response_id),
                   None if functional is None else _number(functional),
                   bool(values.get("extended", default.extended)), None if padding is None else _number(padding))


@dataclass
class KeySource:
    """SecurityAccess keys: seed XOR mask, or a seed & key DLL (GenerateKeyEx) with its variant."""
    kind: str = "xor"                     # "xor" or "dll"
    mask: int = 0xA5
    dll: str = ""
    variant: str = ""

    def function(self, base_dir=None):
        """key(level, seed) -> bytes, or None without a DLL to call."""
        from canexpert.uds.seed_key import dll_key, xor_key
        if self.kind != "dll":
            function = xor_key(self.mask)
            return lambda level, seed: function(seed)
        if not self.dll:
            return None
        path = Path(self.dll)
        if not path.is_absolute() and base_dir is not None:
            path = Path(base_dir) / path
        cache = {}

        def key(level, seed):
            if level not in cache:
                cache[level] = dll_key(str(path), level, self.variant)
            return cache[level](seed)
        return key

    def to_dict(self) -> dict:
        return {"kind": self.kind, "mask": _hex(self.mask), "dll": self.dll, "variant": self.variant}

    @classmethod
    def from_dict(cls, values: dict) -> "KeySource":
        values = values or {}
        return cls(str(values.get("kind", "xor")), _number(values.get("mask"), 0xA5), str(values.get("dll", "")),
                   str(values.get("variant", "")))


@dataclass
class TestPlan:
    name: str = ""
    description: str = ""                 # a description file, or "" for the Dummy ECU's own
    connection: Connection = field(default_factory=Connection)
    options: dict = field(default_factory=lambda: {name: getattr(Options(), name) for name in OPTION_NAMES})
    key: KeySource = field(default_factory=KeySource)
    record: bool = False                  # a .blf of the run's traffic beside its reports
    reports: str = ""                     # the reports' folder; "": TestExpert/reports
    excluded: list = field(default_factory=list)       # names of the tests not run
    sequences: list = field(default_factory=list)      # Sequence
    nrc_policy: NrcPolicy = field(default_factory=NrcPolicy)
    deviations: list = field(default_factory=list)     # Deviation: failures accepted
    discovery: DiscoveryOptions = field(default_factory=DiscoveryOptions)
    identify: bool = False                # tell the ECU's variant when connecting (before a run without the window)
    identification: Identification = field(default_factory=Identification)   # for files that do not say how
    variant: str = ""                     # the variant of the description's file to read; "": its first
    modules: list = field(default_factory=list)        # CAN Expert test modules (.py) run after the generated tests
    symbols: list = field(default_factory=list)        # symbol databases (DBC...) the modules' frames are decoded with
    path: Path | None = None              # where it was read from or saved to (not saved)

    # --- files -----------------------------------------------------------------------------------------

    def folder(self) -> Path:
        return self.path.parent if self.path is not None else Path.cwd()

    def resolve(self, text: str) -> Path | None:
        """A path of the plan, relative to its folder."""
        if not text:
            return None
        path = Path(text)
        return path if path.is_absolute() else (self.folder() / path).resolve()

    def relative(self, path) -> str:
        """path as the plan writes it: relative to its folder when both are on the same drive."""
        if not path:
            return ""
        path = Path(path)
        if self.path is None:
            return str(path)
        try:
            return os.path.relpath(path.resolve(), self.folder().resolve()).replace("\\", "/")
        except ValueError:                        # another drive
            return str(path)

    def module_paths(self) -> list[Path]:
        return [self.resolve(module) for module in self.modules if module]

    def symbol_paths(self) -> list[Path]:
        return [self.resolve(database) for database in self.symbols if database]

    def load_description(self) -> EcuDescription:
        """The plan's description: its file, or the Dummy ECU's."""
        from canexpert.test_expert.dummy import dummy_description
        from canexpert.test_expert.odx import load_description
        path = self.resolve(self.description)
        if path is None:
            return dummy_description()
        if not path.is_file():
            raise PlanError(f"the description {path} is not there")
        return load_description(path, self.variant or None)

    def check(self):
        """PlanError naming a setting that cannot be read (the routines to start, the memory ranges)."""
        from canexpert.test_expert.generator import parse_routine_starts
        from canexpert.test_expert.services import parse_memory_range
        try:
            parse_routine_starts(self.options.get("start_routines", ""))
            parse_memory_range(self.options.get("download", ""))
            parse_memory_range(self.options.get("memory", ""))
        except ValueError as exc:
            raise PlanError(str(exc)) from None
        return self

    def make_options(self) -> Options:
        values = {name: self.options[name] for name in OPTION_NAMES if name in self.options}
        return Options(**values, key=self.key.function(self.folder()))

    def report_folder(self, default: Path) -> Path:
        return self.resolve(self.reports) or default

    # --- JSON ------------------------------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {"format": PLAN_FORMAT, "version": PLAN_VERSION, "name": self.name, "description": self.description,
                "variant": self.variant, "identify": self.identify, "identification": self.identification.to_dict(),
                "connection": self.connection.to_dict(), "options": dict(self.options), "key": self.key.to_dict(),
                "record": self.record, "reports": self.reports, "excluded": sorted(self.excluded),
                "sequences": [sequence.to_dict() for sequence in self.sequences],
                "nrc_policy": self.nrc_policy.to_dict(),
                "deviations": [deviation.to_dict() for deviation in self.deviations],
                "discovery": {"sessions": [f"{session:02X}" for session in self.discovery.sessions],
                              "dids": self.discovery.dids, "rids": self.discovery.rids,
                              "services": self.discovery.services, "security": self.discovery.security},
                "modules": list(self.modules), "symbols": list(self.symbols)}

    @classmethod
    def from_dict(cls, values: dict, path=None) -> "TestPlan":
        if not is_plan(values):
            raise PlanError("not a TestExpert plan")
        if int(values.get("version", 1)) > PLAN_VERSION:
            raise PlanError(f"a plan of a newer TestExpert (version {values.get('version')})")
        try:
            options = {name: getattr(Options(), name) for name in OPTION_NAMES}
            options.update({name: value for name, value in (values.get("options") or {}).items()
                            if name in OPTION_NAMES})
            return cls(name=str(values.get("name", "")), description=str(values.get("description", "")),
                       connection=Connection.from_dict(values.get("connection")), options=options,
                       key=KeySource.from_dict(values.get("key")), record=bool(values.get("record", False)),
                       reports=str(values.get("reports", "")),
                       excluded=[str(name) for name in values.get("excluded", ())],
                       sequences=[Sequence.from_dict(item) for item in values.get("sequences", ())],
                       nrc_policy=NrcPolicy.from_dict(values.get("nrc_policy")),
                       deviations=[Deviation.from_dict(item) for item in values.get("deviations", ())],
                       discovery=_discovery(values.get("discovery")), identify=bool(values.get("identify", False)),
                       identification=Identification.from_dict(values.get("identification")),
                       variant=str(values.get("variant", "") or ""),
                       modules=[str(module) for module in values.get("modules", ()) if module],
                       symbols=[str(database) for database in values.get("symbols", ()) if database],
                       path=Path(path) if path is not None else None)
        except (TypeError, ValueError, AttributeError) as exc:
            raise PlanError(f"the plan cannot be read: {exc}") from None

    def save(self, path=None):
        path = Path(path or self.path)
        old = self.path
        self.path = path
        if old is not None and old.parent.resolve() != path.parent.resolve():
            # Paths written relative to the old folder are rewritten for the new one.
            def moved(value):
                return self.relative((old.parent / value).resolve()) if value and not Path(value).is_absolute() \
                    else value
            for name in ("description", "reports"):
                setattr(self, name, moved(getattr(self, name)))
            self.modules = [moved(value) for value in self.modules]
            self.symbols = [moved(value) for value in self.symbols]
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path) -> "TestPlan":
        path = Path(path)
        try:
            values = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise PlanError(f"{path.name} cannot be read: {exc}") from None
        return cls.from_dict(values, path)


def _discovery(values) -> DiscoveryOptions:
    values = values or {}
    default = DiscoveryOptions()
    sessions = [_number(session) for session in values.get("sessions", ())] or default.sessions
    return DiscoveryOptions(sessions, str(values.get("dids", default.dids)), str(values.get("rids", default.rids)),
                            bool(values.get("services", default.services)), bool(values.get("security", default.security)))


def is_plan(values) -> bool:
    return isinstance(values, dict) and values.get("format") == PLAN_FORMAT


def is_plan_file(path) -> bool:
    """Whether a .json file is a test plan (rather than a description saved as JSON)."""
    path = Path(path)
    if path.suffix.lower() != ".json" or not path.is_file():
        return False
    try:
        return is_plan(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError):
        return False


def options_dict(options: Options) -> dict:
    values = asdict(options)
    values.pop("key", None)
    return values
