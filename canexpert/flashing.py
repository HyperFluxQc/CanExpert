"""
Firmware flashing: S-record (S19/S28/S37) and Intel HEX files, and the dialogs the main window's Flashing
button and the Form Designer's Test panel share - choose a file, choose how it is flashed and with which
settings, progress with Cancel, result.
"""
from dataclasses import dataclass, replace
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from canexpert.flash_sequence import MAX_BLOCK, FlashProfile
from canexpert.ui_common import enable_maximize

FIRMWARE_FILE_FILTER = ("Firmware (*.s19 *.s28 *.s37 *.srec *.mot *.hex *.ihex);;"
                        "Motorola S-record (*.s19 *.s28 *.s37 *.srec *.mot);;Intel HEX (*.hex *.ihex);;All files (*.*)")
PROFILE_FILE_FILTER = "Flashing profile (*.json);;All files (*.*)"


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


# ---------------------------------------------------------------------------------------------------
# Choosing how to flash, and the settings the built-in sequence uses
# ---------------------------------------------------------------------------------------------------

def _hex_text(value: int) -> str:
    return f"{value:02X}" if value < 0x100 else f"{value:04X}"


def _hex_field(value: int, width=70) -> QLineEdit:
    edit = QLineEdit(_hex_text(value))
    edit.setFixedWidth(width)
    return edit


class FlashProfileDialog(QDialog):
    """The settings of the built-in flashing sequence, and the profile file they can be kept in."""

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Flashing sequence")
        enable_maximize(self)
        self.resize(620, 560)
        self.profile = profile
        layout = QVBoxLayout(self)
        note = QLabel("What CAN Expert sends when the panel script has no Flashing(api, firmware).\n"
                      "A value of 0 leaves that step out.")
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)

        form = QFormLayout()
        self.extended_session = _hex_field(profile.extended_session)
        form.addRow("Session before security (hex):", self.extended_session)
        self.programming_session = _hex_field(profile.programming_session)
        form.addRow("Programming session (hex):", self.programming_session)
        self.stop_dtc = QCheckBox("Switch DTCs off while programming")
        form.addRow("", self.stop_dtc)
        self.stop_communication = QCheckBox("Switch normal messages off while programming")
        form.addRow("", self.stop_communication)
        self.restore_after = QCheckBox("Switch both back on when it is done")
        form.addRow("", self.restore_after)
        self.security_level = _hex_field(profile.security_level)
        form.addRow("SecurityAccess level (hex, 0 = none):", self.security_level)
        self.key_mask = _hex_field(profile.key_mask)
        form.addRow("key = seed XOR (hex):", self.key_mask)
        dll_row = QHBoxLayout()
        self.key_dll = QLineEdit(profile.key_dll)
        self.key_dll.setPlaceholderText("GenerateKeyEx DLL; the mask above is used while this is empty")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        dll_row.addWidget(self.key_dll, 1)
        dll_row.addWidget(browse)
        form.addRow("Seed & key DLL:", dll_row)
        self.key_variant = QLineEdit(profile.key_variant)
        form.addRow("DLL variant:", self.key_variant)
        self.erase_routine = _hex_field(profile.erase_routine)
        form.addRow("Erase routine (hex, 0 = none):", self.erase_routine)
        self.check_routine = _hex_field(profile.check_routine)
        form.addRow("Dependency check routine (hex, 0 = none):", self.check_routine)
        self.check_crc = QCheckBox("Send the image's CRC-32 to the dependency check")
        self.check_crc.setToolTip("31 01 <routine> and the CRC-32 of the image (its segments in address order), "
                                  "for a bootloader that checks it")
        form.addRow("", self.check_crc)
        self.address_format = _hex_field(profile.address_format)
        form.addRow("Address and length format (hex):", self.address_format)
        self.data_format = _hex_field(profile.data_format)
        form.addRow("Data format (hex):", self.data_format)
        self.block_size = QSpinBox()
        self.block_size.setRange(0, MAX_BLOCK - 2)
        self.block_size.setSpecialValueText("as much as the ECU allows")
        self.block_size.setSuffix(" bytes")
        form.addRow("Sent per TransferData:", self.block_size)
        self.reset_type = _hex_field(profile.reset_type)
        form.addRow("ECUReset type (hex, 0 = none):", self.reset_type)
        self.version_did = _hex_field(profile.version_did)
        form.addRow("Version DID read afterwards (hex, 0 = none):", self.version_did)
        layout.addLayout(form)

        self.message = QLabel("")
        self.message.setStyleSheet("color: gray;")
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        for text, slot, tip in (("Load profile...", self.load_profile, "Read these settings from a file"),
                                ("Save profile...", self.save_profile, "Keep these settings in a file")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        buttons.addWidget(box)
        layout.addLayout(buttons)
        self.set_profile(profile)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "Seed & key DLL", "", "DLL (*.dll);;All files (*.*)")
        if path:
            self.key_dll.setText(path)

    _NUMBERS = (("extended_session", "The session before security"),
                ("programming_session", "The programming session"),
                ("security_level", "The SecurityAccess level"), ("key_mask", "The key mask"),
                ("erase_routine", "The erase routine"), ("check_routine", "The dependency check routine"),
                ("address_format", "The address and length format"), ("data_format", "The data format"),
                ("reset_type", "The ECUReset type"), ("version_did", "The version DID"))
    _FLAGS = ("stop_dtc", "stop_communication", "restore_after", "check_crc")

    def values(self):
        """The profile the fields describe. ValueError, with what to tell the user, when one is not a number."""
        numbers = {}
        for name, what in self._NUMBERS:
            text = getattr(self, name).text().strip() or "0"
            try:
                numbers[name] = int(text, 16)
            except ValueError:
                raise ValueError(f"{what} must be hexadecimal, for example 0xFF00 written as FF00") from None
        return replace(self.profile, **numbers,
                       **{name: getattr(self, name).isChecked() for name in self._FLAGS},
                       key_dll=self.key_dll.text().strip(), key_variant=self.key_variant.text().strip(),
                       block_size=self.block_size.value())

    def set_profile(self, profile):
        """Put a profile into the fields."""
        self.profile = profile
        for name, _what in self._NUMBERS:
            getattr(self, name).setText(_hex_text(getattr(profile, name)))
        for name in self._FLAGS:
            getattr(self, name).setChecked(getattr(profile, name))
        self.key_dll.setText(profile.key_dll)
        self.key_variant.setText(profile.key_variant)
        self.block_size.setValue(min(profile.block_size, MAX_BLOCK - 2))

    def _accept(self):
        try:
            self.profile = self.values()
        except ValueError as exc:
            self.message.setStyleSheet("color: red;")
            self.message.setText(str(exc))
            return
        self.accept()

    def save_profile(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save the flashing profile", "", PROFILE_FILE_FILTER)
        if not path:
            return
        try:
            self.values().save(path)
        except (OSError, ValueError) as exc:
            self.message.setStyleSheet("color: red;")
            self.message.setText(str(exc))
            return
        self.message.setStyleSheet("color: gray;")
        self.message.setText(f"Saved to {Path(path).name}")

    def load_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load a flashing profile", "", PROFILE_FILE_FILTER)
        if not path:
            return
        try:
            self.set_profile(FlashProfile.load(path))
        except (OSError, ValueError) as exc:
            self.message.setStyleSheet("color: red;")
            self.message.setText(f"Cannot read {Path(path).name}: {exc}")
            return
        self.message.setStyleSheet("color: gray;")
        self.message.setText(f"Loaded {Path(path).name}")


class FlashDialog(QDialog):
    """What is about to be flashed, and how: the panel script's Flashing(), or the built-in sequence."""

    def __init__(self, firmware, profile, script_available=False, parent=None, target="the ECU"):
        super().__init__(parent)
        self.setWindowTitle("Flashing")
        self.resize(560, 340)
        self.profile = profile
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Flash {Path(firmware.path).name} ({firmware.size} bytes) to {target}?"))
        ranges = "\n".join(f"0x{address:08X} - 0x{address + len(data) - 1:08X}  ({len(data)} bytes)"
                           for address, data in firmware.segments[:8])
        if len(firmware.segments) > 8:
            ranges += f"\n... {len(firmware.segments) - 8} more segment(s)"
        addresses = QLabel(ranges)
        addresses.setStyleSheet("font-family: monospace;")
        layout.addWidget(addresses)
        layout.addSpacing(8)

        self.script_radio = QRadioButton("With the panel script's Flashing(api, firmware)")
        self.script_radio.setEnabled(script_available)
        self.script_radio.setChecked(script_available)
        layout.addWidget(self.script_radio)
        if not script_available:
            missing = QLabel("        the panel script does not define Flashing(api, firmware)")
            missing.setStyleSheet("color: gray;")
            layout.addWidget(missing)
        self.built_in_radio = QRadioButton("With the built-in ISO 14229 sequence")
        self.built_in_radio.setChecked(not script_available)
        layout.addWidget(self.built_in_radio)
        settings_row = QHBoxLayout()
        settings_row.addSpacing(24)
        self.settings_btn = QPushButton("Sequence settings...")
        self.settings_btn.setToolTip("Sessions, security, erase and check routines, block size, reset")
        self.settings_btn.clicked.connect(self.edit_profile)
        settings_row.addWidget(self.settings_btn)
        self.summary = QLabel("")
        self.summary.setStyleSheet("color: gray;")
        settings_row.addWidget(self.summary, 1)
        layout.addLayout(settings_row)

        layout.addStretch()
        layout.addWidget(QLabel("Keep the CAN connection and ECU power stable until flashing finishes."))
        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("Flash")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self._update_summary()

    def _update_summary(self):
        profile = self.profile
        self.summary.setText(
            f"session {profile.programming_session:02X}, "
            + (f"security {profile.security_level:02X}, " if profile.security_level else "no security, ")
            + (f"erase {profile.erase_routine:04X}, " if profile.erase_routine else "no erase, ")
            + (f"{profile.block_size} bytes per block" if profile.block_size else "block size from the ECU"))

    def edit_profile(self):
        """The sequence settings; choosing them means the built-in sequence is what is wanted."""
        dialog = FlashProfileDialog(self.profile, self)
        if dialog.exec_() == QDialog.Accepted:
            self.profile = dialog.profile
            self.built_in_radio.setChecked(True)
            self._update_summary()
        return dialog

    def use_script(self) -> bool:
        return self.script_radio.isChecked()
