"""
The dialog that says how a description's variants are told apart when its file does not (a CDD, a JSON
description): the DID to read, and the value each variant answers.
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from canexpert.test_expert.variants import Identification


class IdentificationDialog(QDialog):
    def __init__(self, variants, identification: Identification, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Told apart by")
        layout = QVBoxLayout(self)
        note = QLabel("The file does not say how its variants are told apart. Give the DID to read - a variant "
                      "code, a hardware or software number - and the value each variant answers: its text, or its "
                      "bytes in hex (01 02).")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.did = QLineEdit(f"{identification.did:04X}" if identification.did is not None else "")
        self.did.setPlaceholderText("F1A0")
        form.addRow("DID", self.did)
        layout.addLayout(form)
        self.table = QTableWidget(len(variants), 2)
        self.table.setHorizontalHeaderLabels(["Variant", "Its value"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for row, variant in enumerate(variants):
            name = QTableWidgetItem(variant)
            name.setFlags(name.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(identification.values.get(variant, "")))
        layout.addWidget(self.table)
        self.problem = QLabel("")
        self.problem.setStyleSheet("color: #b91c1c;")
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(420, 320)

    def identification(self) -> Identification:
        text = self.did.text().strip().lower().removeprefix("0x")
        values = {self.table.item(row, 0).text(): self.table.item(row, 1).text().strip()
                  for row in range(self.table.rowCount()) if self.table.item(row, 1).text().strip()}
        return Identification(int(text, 16) if text else None, values)

    def _accept(self):
        try:
            did = self.identification().did
        except ValueError:
            self.problem.setText("The DID is four hex digits: F1A0")
            return
        if did is not None and not 0 <= did <= 0xFFFF:
            self.problem.setText("The DID is four hex digits: F1A0")
            return
        self.accept()
