"""
Flashing without a panel script: the ISO 14229 sequence most bootloaders want, described by settings.

A panel script's Flashing(api, firmware) can do anything a particular bootloader needs, and stays the
way to handle an unusual one. This is the ordinary case written once - extended session, DTCs and
normal communication off, programming session, security access, then per segment erase,
RequestDownload, TransferData, RequestTransferExit, a dependency check and a reset - with the parts
that differ between ECUs as a profile that can be saved beside the firmware.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

from canexpert.uds.seed_key import SeedKeyError, dll_key, xor_key

# The longest message ISO-TP carries without the 2016 escape sequence older bootloaders lack.
MAX_BLOCK = 4095
RESET_WAIT = 1.0      # how long the ECU is left to start its application before it is asked anything


@dataclass
class FlashProfile:
    """What differs between bootloaders. Everything else the sequence does is the same for all of them."""
    extended_session: int = 0x03      # 0 leaves the session alone before the programming one
    stop_dtc: bool = True             # ControlDTCSetting off while programming
    stop_communication: bool = True   # CommunicationControl: no normal messages while programming
    programming_session: int = 0x02
    security_level: int = 0x01        # 0 skips SecurityAccess
    key_mask: int = 0xA5              # key = seed XOR mask, unless a DLL is given
    key_dll: str = ""                 # a seed & key DLL (GenerateKeyEx), used instead of the mask
    key_variant: str = ""
    erase_routine: int = 0xFF00       # 0 skips erasing
    check_routine: int = 0xFF01       # 0 skips the dependency check
    address_format: int = 0x44        # addressAndLengthFormatIdentifier: 4-byte address and size
    data_format: int = 0x00           # dataFormatIdentifier: no compression, no encryption
    block_size: int = 0               # bytes per TransferData; 0 asks the ECU (maxNumberOfBlockLength)
    reset_type: int = 0x01            # ECUReset: hardReset
    version_did: int = 0xF195         # read back after the reset, 0 to skip
    restore_after: bool = True        # DTCs and normal communication back on when it is done

    @classmethod
    def from_dict(cls, values: dict) -> "FlashProfile":
        names = {item.name for item in fields(cls)}
        return cls(**{name: value for name, value in (values or {}).items() if name in names})

    @classmethod
    def load(cls, path) -> "FlashProfile":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path):
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def compute_key(self):
        """compute_key(seed) -> key, from the seed & key DLL if one is named, else from the mask."""
        if self.key_dll.strip():
            return dll_key(self.key_dll.strip(), self.security_level, self.key_variant.strip())
        return xor_key(self.key_mask)


class FlashCancelled(Exception):
    """The user asked to stop between two steps."""


class FlashError(Exception):
    """A step the sequence cannot go on without."""


def memory_record(address: int, size: int, address_format: int) -> list[int]:
    """addressAndLengthFormatIdentifier, memoryAddress and memorySize, as RoutineControl erase wants."""
    address_length, size_length = address_format & 0x0F, address_format >> 4
    return [address_format, *address.to_bytes(address_length, "big"), *size.to_bytes(size_length, "big")]


class FlashRun:
    """One run of the sequence: what was done, and how it went."""

    def __init__(self, profile: FlashProfile, firmware, log=None):
        self.profile = profile
        self.firmware = firmware
        self.steps = []                    # (step, outcome) in the order they happened
        self.written = 0
        self.started = time.time()
        self.finished = None
        self.ok = False
        self._log = log or (lambda text: None)

    def record(self, step: str, outcome: str):
        self.steps.append((step, outcome))
        self._log(f"{step}: {outcome}")

    def report(self) -> str:
        """The run as a text report, for the file written beside the firmware."""
        lines = [f"CAN Expert flashing report - {datetime.fromtimestamp(self.started):%Y-%m-%d %H:%M:%S}",
                 f"Firmware: {self.firmware.path}",
                 f"Size: {self.firmware.size} bytes in {len(self.firmware.segments)} segment(s)",
                 *(f"  0x{address:08X} - 0x{address + len(data) - 1:08X}  ({len(data)} bytes)"
                   for address, data in self.firmware.segments),
                 "", "Profile:",
                 *(f"  {name} = {value}" for name, value in asdict(self.profile).items()),
                 "", "Steps:"]
        lines += [f"  {step}: {outcome}" for step, outcome in self.steps]
        seconds = (self.finished or time.time()) - self.started
        lines += ["", f"Result: {'complete' if self.ok else 'failed'} after {seconds:.1f} s, "
                      f"{self.written} of {self.firmware.size} bytes written"]
        return "\n".join(lines) + "\n"

    def write_report(self, path=None) -> Path:
        """Write the report beside the firmware (or wherever asked) and return where it went."""
        path = Path(path) if path else Path(self.firmware.path).with_suffix(".flash-report.txt")
        path.write_text(self.report(), encoding="utf-8")
        return path


def run_flash(uds, firmware, profile: FlashProfile, progress=None, cancelled=None, log=None,
              run: "FlashRun | None" = None) -> FlashRun:
    """Flash firmware with the ISO 14229 services of uds. Raises FlashError or FlashCancelled.

    A caller that wants the report of a run that fails half way passes the FlashRun in: the steps that
    did happen are in it either way.
    """
    run = run or FlashRun(profile, firmware, log)
    progress = progress or (lambda done, total, text: None)
    cancelled = cancelled or (lambda: False)
    total = firmware.size

    def check():
        if cancelled():
            raise FlashCancelled("Cancelled")

    def step(result, what: str):
        check()
        run.record(what, "ok" if result else result.error)
        if not result:
            raise FlashError(f"{what} failed: {result.error}")
        return result

    progress(0, total, "Pre-programming")
    if profile.extended_session:
        step(uds.DSC(profile.extended_session), "DiagnosticSessionControl (extended)")
    if profile.stop_dtc:
        step(uds.CDTCS(0x02), "ControlDTCSetting (off)")
    if profile.stop_communication:
        step(uds.CC(0x03, 0x01), "CommunicationControl (normal messages off)")
    step(uds.DSC(profile.programming_session), "DiagnosticSessionControl (programming)")
    if profile.security_level:
        try:
            compute_key = profile.compute_key()
        except SeedKeyError as exc:
            run.record("SecurityAccess", str(exc))
            raise FlashError(str(exc)) from None
        step(uds.SecurityUnlock(profile.security_level, compute_key), "SecurityAccess")

    for address, data in firmware.segments:
        where = f"0x{address:08X}"
        if profile.erase_routine:
            progress(run.written, total, f"Erasing {where}")
            step(uds.StartRoutine(profile.erase_routine,
                                  memory_record(address, len(data), profile.address_format)),
                 f"RoutineControl erase {where}")
        download = step(uds.RD(address, len(data), profile.address_format, profile.data_format),
                        f"RequestDownload {where}")
        # maxNumberOfBlockLength counts the 0x36 SID and the block counter, so two bytes come off it.
        announced = min(download.max_block_length or MAX_BLOCK, MAX_BLOCK) - 2
        block = max(1, min(profile.block_size or announced, announced))
        run.record(f"TransferData block size {where}", f"{block} bytes")
        for counter, offset in enumerate(range(0, len(data), block), start=1):
            check()
            chunk = data[offset:offset + block]
            result = uds.TD(counter, chunk)
            if not result:
                run.record(f"TransferData block {counter & 0xFF} at 0x{address + offset:08X}", result.error)
                raise FlashError(f"TransferData failed at 0x{address + offset:08X}: {result.error}")
            run.written += len(chunk)
            progress(run.written, total, f"Writing 0x{address + offset:08X}")
        run.record(f"TransferData {where}", f"{len(data)} bytes in {counter} block(s)")
        step(uds.RTE(), f"RequestTransferExit {where}")

    if profile.check_routine:
        progress(run.written, total, "Checking programming dependencies")
        check_result = step(uds.StartRoutine(profile.check_routine), "checkProgrammingDependencies")
        status = check_result.data[:1]     # routineStatusRecord: 0, or nothing said, is good news
        if status not in (b"", b"\x00"):
            run.record("checkProgrammingDependencies", f"status 0x{status[0]:02X}")
            raise FlashError(f"checkProgrammingDependencies reported status 0x{status[0]:02X}")
    if profile.restore_after:
        if profile.stop_communication:
            run.record("CommunicationControl (normal messages on)", "ok" if uds.CC(0x00, 0x01) else "refused")
        if profile.stop_dtc:
            run.record("ControlDTCSetting (on)", "ok" if uds.CDTCS(0x01) else "refused")
    if profile.reset_type:
        progress(run.written, total, "Resetting the ECU")
        step(uds.ER(profile.reset_type), "ECUReset")
    if profile.version_did:
        time.sleep(RESET_WAIT)             # the ECU needs a moment to start the new application
        version = uds.RDBI(profile.version_did)
        run.record(f"Software version (DID {profile.version_did:04X})",
                   version.text if version else version.error)
    run.ok = True
    run.finished = time.time()
    progress(run.written, total, "Complete")
    return run
