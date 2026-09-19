"""
Firmware flashing: S-record (S19/S28/S37) and Intel HEX files, and the dialogs shared by the main window's
Flashing button and the Form Designer's Test panel (choose a file, confirm, progress with Cancel, result).
"""
from dataclasses import dataclass
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFileDialog, QMessageBox, QProgressDialog

FIRMWARE_FILE_FILTER = ("Firmware (*.s19 *.s28 *.s37 *.srec *.mot *.hex *.ihex);;"
                        "Motorola S-record (*.s19 *.s28 *.s37 *.srec *.mot);;Intel HEX (*.hex *.ihex);;All files (*.*)")


@dataclass
class Firmware:
    """A firmware image: contiguous (address, data) segments in ascending address order."""
    path: str
    segments: list[tuple[int, bytes]]

    @property
    def size(self) -> int:
        return sum(len(data) for _, data in self.segments)


def _srecord_data(lines: list[str]) -> list[tuple[int, bytes]]:
    records = []
    for number, line in enumerate(lines, 1):
        if not line.startswith("S") or len(line) < 4:
            raise ValueError(f"Line {number}: not an S-record")
        kind = line[1]
        try:
            raw = bytes.fromhex(line[2:])
        except ValueError:
            raise ValueError(f"Line {number}: invalid hexadecimal") from None
        if raw[0] != len(raw) - 1:
            raise ValueError(f"Line {number}: byte count does not match record length")
        if (sum(raw[:-1]) + raw[-1]) & 0xFF != 0xFF:
            raise ValueError(f"Line {number}: checksum error")
        address_length = {"1": 2, "2": 3, "3": 4}.get(kind)
        if address_length is None:
            continue  # S0 header, S5/S6 count, S7-S9 start address
        address = int.from_bytes(raw[1:1 + address_length], "big")
        data = raw[1 + address_length:-1]
        if data:
            records.append((address, data))
    return records


def _intel_hex_data(lines: list[str]) -> list[tuple[int, bytes]]:
    records, base = [], 0
    for number, line in enumerate(lines, 1):
        if not line.startswith(":"):
            raise ValueError(f"Line {number}: not an Intel HEX record")
        try:
            raw = bytes.fromhex(line[1:])
        except ValueError:
            raise ValueError(f"Line {number}: invalid hexadecimal") from None
        if len(raw) < 5 or len(raw) != raw[0] + 5:
            raise ValueError(f"Line {number}: byte count does not match record length")
        if sum(raw) & 0xFF:
            raise ValueError(f"Line {number}: checksum error")
        kind, data = raw[3], raw[4:-1]
        if kind == 0x00:
            if data:
                records.append((base + int.from_bytes(raw[1:3], "big"), data))
        elif kind == 0x01:
            break
        elif kind == 0x02:
            base = int.from_bytes(data, "big") << 4
        elif kind == 0x04:
            base = int.from_bytes(data, "big") << 16
    return records


def _read_lines(path: str | Path) -> list[str]:
    return [line.strip() for line in Path(path).read_text(encoding="ascii", errors="replace").splitlines()
            if line.strip()]


def parse_s19_s28_file(path: str | Path) -> list[tuple[int, bytes]]:
    """(address, data) of every S1/S2/S3 record, unmerged; [] if the file is missing. Bad records raise ValueError."""
    return _srecord_data(_read_lines(path)) if Path(path).exists() else []


def load_firmware(path: str | Path) -> Firmware:
    """Read an S-record (S19/S28/S37) or Intel HEX file, verifying checksums and merging contiguous records."""
    lines = _read_lines(path)
    if not lines:
        raise ValueError("Firmware file is empty")
    if lines[0].startswith(":"):
        records = _intel_hex_data(lines)
    elif lines[0].startswith("S"):
        records = _srecord_data(lines)
    else:
        raise ValueError("Unrecognised format; expected Motorola S-record or Intel HEX")
    if not records:
        raise ValueError("Firmware file contains no data records")
    segments: list[tuple[int, bytearray]] = []
    for address, data in sorted(records, key=lambda record: record[0]):
        if segments and address < segments[-1][0] + len(segments[-1][1]):
            raise ValueError(f"Overlapping data at 0x{address:X}")
        if segments and address == segments[-1][0] + len(segments[-1][1]):
            segments[-1][1].extend(data)
        else:
            segments.append((address, bytearray(data)))
    return Firmware(str(path), [(address, bytes(data)) for address, data in segments])



def choose_firmware(parent, start_dir):
    """Ask for a firmware file and load it (checksums verified). Returns a Firmware, or None."""
    path, _ = QFileDialog.getOpenFileName(parent, "Select the firmware file to flash", str(start_dir),
                                          FIRMWARE_FILE_FILTER)
    if not path:
        return None
    try:
        return load_firmware(path)
    except (OSError, ValueError) as exc:
        QMessageBox.critical(parent, "Flashing", f"Cannot read {Path(path).name}:\n{exc}")
        return None


def confirm_flash(parent, firmware, target="the ECU"):
    """'Are you sure?' with the file's address ranges."""
    ranges = "\n".join(f"0x{address:08X} - 0x{address + len(data) - 1:08X}  ({len(data)} bytes)"
                       for address, data in firmware.segments[:8])
    if len(firmware.segments) > 8:
        ranges += f"\n... {len(firmware.segments) - 8} more segment(s)"
    answer = QMessageBox.question(
        parent, "Flashing",
        f"Flash {Path(firmware.path).name} ({firmware.size} bytes) to {target}?\n\n{ranges}\n\n"
        "Keep the CAN connection and ECU power stable until flashing finishes.")
    return answer == QMessageBox.Yes


def progress_dialog(parent, firmware, cancel):
    dialog = QProgressDialog(f"Flashing {Path(firmware.path).name}...", "Cancel", 0, max(1, firmware.size), parent)
    dialog.setWindowTitle("Flashing")
    dialog.setWindowModality(Qt.WindowModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)
    dialog.canceled.connect(cancel)
    dialog.show()
    return dialog


def update_progress(dialog, done, total, text):
    if dialog is None:
        return
    dialog.setMaximum(max(1, total))
    dialog.setValue(max(0, min(done, total)))
    if text:
        dialog.setLabelText(text)


def close_progress(dialog):
    if dialog is not None:
        dialog.canceled.disconnect()
        dialog.close()
        dialog.deleteLater()


def report_result(parent, ok, text):
    if ok:
        QMessageBox.information(parent, "Flashing", text)
    else:
        QMessageBox.critical(parent, "Flashing", f"Flashing failed:\n{text}")
