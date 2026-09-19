"""
Firmware flashing dialogs shared by the main window's Flashing button and the Form Designer's
Test panel: choose an S-record / Intel HEX file, confirm, show progress (with Cancel), report.
"""
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFileDialog, QMessageBox, QProgressDialog

from canexpert.uds.isotp import FIRMWARE_FILE_FILTER, load_firmware


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
