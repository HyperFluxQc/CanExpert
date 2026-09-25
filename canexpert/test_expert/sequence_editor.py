"""
TestExpert's Sequences tab: the pre-test and post-test sequences - their steps, and where each runs: before or
after the run, a group, a test, or every test.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from canexpert.test_expert.sequences import (CONDITIONS, EXPECTS, HINTS, KINDS, PRESETS, SCOPES, WHEN, Attachment,
                                             Sequence, SequenceStep)

STEP_KIND, STEP_VALUE, STEP_EXPECT, STEP_FUNCTIONAL = range(4)
RUN_WHEN, RUN_SCOPE, RUN_TARGET, RUN_CONDITION = range(4)
BAD = QColor("#fecaca")


def _combo(items, current=None, editable=False):
    """A combo box of (text, data) pairs, set to the item whose data is current."""
    combo = QComboBox()
    combo.setEditable(editable)
    for text, data in items:
        combo.addItem(text, data)
    if current is not None:
        index = combo.findData(current)
        if index >= 0:
            combo.setCurrentIndex(index)
        elif editable:
            combo.setEditText(str(current))
    return combo


class SequenceEditor(QWidget):
    """The sequences being edited; changed is emitted on every edit."""
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sequences: list[Sequence] = []
        self._groups: list[str] = []
        self._tests: dict[str, str] = {}             # test name -> title
        self._filling = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Vertical)
        layout.addWidget(splitter)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.new_btn = QToolButton()
        self.new_btn.setText("New")
        self.new_btn.setPopupMode(QToolButton.MenuButtonPopup)
        self.new_btn.setToolTip("A new sequence; the arrow offers ready-made ones")
        self.new_btn.clicked.connect(lambda: self.add_sequence())
        presets = QMenu(self.new_btn)
        for name in PRESETS:
            presets.addAction(name).triggered.connect(lambda _checked=False, name=name: self.add_preset(name))
        self.new_btn.setMenu(presets)
        bar.addWidget(self.new_btn)
        for text, slot in (("Rename...", self.rename_sequence), ("Duplicate", self.duplicate_sequence),
                           ("Remove", self.remove_sequence)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            bar.addWidget(button)
        bar.addStretch()
        top_layout.addLayout(bar)
        self.list = QListWidget()
        self.list.setToolTip("Untick a sequence to keep it without running it")
        self.list.currentRowChanged.connect(self._show_current)
        self.list.itemChanged.connect(self._list_item_changed)
        top_layout.addWidget(self.list)
        splitter.addWidget(top)

        middle = QWidget()
        middle_layout = QVBoxLayout(middle)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        self.steps_label = QLabel("Steps")
        middle_layout.addWidget(self.steps_label)
        self.steps = QTableWidget(0, 4)
        self.steps.setHorizontalHeaderLabels(["Step", "Value", "Answer", "Funct."])
        self.steps.horizontalHeaderItem(STEP_FUNCTIONAL).setToolTip("Functionally addressed (the functional ID)")
        self.steps.horizontalHeader().setSectionResizeMode(STEP_VALUE, QHeaderView.Stretch)
        for column, width in ((STEP_KIND, 110), (STEP_EXPECT, 105), (STEP_FUNCTIONAL, 48)):
            self.steps.setColumnWidth(column, width)
        self.steps.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.steps.itemChanged.connect(self._step_item_changed)
        middle_layout.addWidget(self.steps)
        step_bar = QHBoxLayout()
        self.add_step_btn = QToolButton()
        self.add_step_btn.setText("Add step")
        self.add_step_btn.setPopupMode(QToolButton.InstantPopup)
        kinds = QMenu(self.add_step_btn)
        for kind, text in KINDS.items():
            kinds.addAction(text).triggered.connect(lambda _checked=False, kind=kind: self.add_step(kind))
        self.add_step_btn.setMenu(kinds)
        step_bar.addWidget(self.add_step_btn)
        for text, slot in (("Remove", self.remove_step), ("Up", lambda: self.move_step(-1)),
                           ("Down", lambda: self.move_step(1))):
            button = QPushButton(text)
            button.clicked.connect(slot)
            step_bar.addWidget(button)
        step_bar.addStretch()
        middle_layout.addLayout(step_bar)
        splitter.addWidget(middle)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(QLabel("Runs"))
        self.runs = QTableWidget(0, 4)
        self.runs.setHorizontalHeaderLabels(["When", "Around", "Which", "Condition"])
        self.runs.horizontalHeader().setSectionResizeMode(RUN_TARGET, QHeaderView.Stretch)
        for column, width in ((RUN_WHEN, 70), (RUN_SCOPE, 100), (RUN_CONDITION, 120)):
            self.runs.setColumnWidth(column, width)
        self.runs.setSelectionBehavior(QAbstractItemView.SelectRows)
        bottom_layout.addWidget(self.runs)
        run_bar = QHBoxLayout()
        for text, slot in (("Add", self.add_attachment), ("Remove", self.remove_attachment)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            run_bar.addWidget(button)
        run_bar.addStretch()
        bottom_layout.addLayout(run_bar)
        splitter.addWidget(bottom)
        splitter.setSizes([150, 260, 170])
        self._show_current(-1)

    @contextmanager
    def _quiet(self):
        """Filling the widgets: their change signals are not edits."""
        filling, self._filling = self._filling, True
        try:
            yield
        finally:
            self._filling = filling

    # --- the sequences --------------------------------------------------------------------------------

    def set_sequences(self, sequences):
        self._sequences = [copy.deepcopy(sequence) for sequence in sequences]
        self._fill_list()
        self.list.setCurrentRow(0 if self._sequences else -1)

    def sequences(self) -> list[Sequence]:
        return [copy.deepcopy(sequence) for sequence in self._sequences]

    def set_targets(self, groups, tests: dict):
        """The groups and tests (name -> title) a sequence may be attached to."""
        self._groups, self._tests = list(groups), dict(tests)
        self._show_current(self.list.currentRow())

    def current(self) -> Sequence | None:
        row = self.list.currentRow()
        return self._sequences[row] if 0 <= row < len(self._sequences) else None

    def _fill_list(self):
        with self._quiet():
            row = self.list.currentRow()
            self.list.blockSignals(True)
            self.list.clear()
            for sequence in self._sequences:
                item = QListWidgetItem(self._list_text(sequence))
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Checked if sequence.enabled else Qt.Unchecked)
                if sequence.problems():
                    item.setForeground(QColor("#b91c1c"))
                    item.setToolTip("; ".join(sequence.problems()))
                self.list.addItem(item)
            self.list.setCurrentRow(min(row, len(self._sequences) - 1))
            self.list.blockSignals(False)

    def _list_text(self, sequence: Sequence) -> str:
        where = "; ".join(attachment.text(self._tests) for attachment in sequence.attachments) or "not attached"
        return f"{sequence.name}  ({len(sequence.steps)} step{'s' if len(sequence.steps) != 1 else ''}; {where})"

    def _list_item_changed(self, item):
        if self._filling:
            return
        row = self.list.row(item)
        if 0 <= row < len(self._sequences):
            self._sequences[row].enabled = item.checkState() == Qt.Checked
            self.changed.emit()

    def _unique(self, name: str) -> str:
        names = {sequence.name for sequence in self._sequences}
        candidate, number = name, 1
        while candidate in names:
            number += 1
            candidate = f"{name} {number}"
        return candidate

    def add_sequence(self, name="Sequence", steps=(), attachments=None) -> Sequence:
        sequence = Sequence(self._unique(name), [copy.deepcopy(step) for step in steps],
                            list(attachments) if attachments is not None else [Attachment("before", "each")])
        self._sequences.append(sequence)
        self._fill_list()
        self.list.setCurrentRow(len(self._sequences) - 1)
        self.changed.emit()
        return sequence

    def add_preset(self, name) -> Sequence:
        when = "after" if "reset" in name.lower() else "before"
        return self.add_sequence(name, PRESETS[name], [Attachment(when, "each")])

    def rename_sequence(self):
        sequence = self.current()
        if sequence is None:
            return
        name, ok = QInputDialog.getText(self, "Rename the sequence", "Name", text=sequence.name)
        if ok and name.strip():
            sequence.name = self._unique(name.strip()) if name.strip() != sequence.name else sequence.name
            self._fill_list()
            self.changed.emit()

    def duplicate_sequence(self):
        sequence = self.current()
        if sequence is not None:
            self.add_sequence(sequence.name, sequence.steps, copy.deepcopy(sequence.attachments))

    def remove_sequence(self):
        row = self.list.currentRow()
        if 0 <= row < len(self._sequences):
            del self._sequences[row]
            self._fill_list()
            self._show_current(self.list.currentRow())
            self.changed.emit()

    def attach(self, name: str, attachment: Attachment):
        """Attach the sequence called name there (the tests tree's menu)."""
        for sequence in self._sequences:
            if sequence.name == name and attachment not in sequence.attachments:
                sequence.attachments.append(attachment)
        self._fill_list()
        self._show_current(self.list.currentRow())
        self.changed.emit()

    def detach(self, scope: str, target: str):
        """Remove every sequence's attachment to that group or test."""
        for sequence in self._sequences:
            sequence.attachments = [a for a in sequence.attachments if not (a.scope == scope and a.target == target)]
        self._fill_list()
        self._show_current(self.list.currentRow())
        self.changed.emit()

    # --- the current sequence's steps and runs -------------------------------------------------------------

    def _show_current(self, _row):
        sequence = self.current()
        enabled = sequence is not None
        for widget in (self.steps, self.runs, self.add_step_btn):
            widget.setEnabled(enabled)
        self.steps_label.setText(f"Steps of {sequence.name}" if sequence else "Steps")
        self._fill_steps()
        self._fill_runs()

    def _fill_steps(self):
        sequence = self.current()
        with self._quiet():
            self._fill_step_rows(sequence)

    def _fill_step_rows(self, sequence):
        self.steps.setRowCount(0)
        for index, step in enumerate(sequence.steps if sequence else []):
            self.steps.insertRow(index)
            kind = _combo([(text, key) for key, text in KINDS.items()], step.kind)
            kind.currentIndexChanged.connect(lambda _i, row=index: self._step_kind_changed(row))
            self.steps.setCellWidget(index, STEP_KIND, kind)
            self.steps.setItem(index, STEP_VALUE, QTableWidgetItem(step.value))
            functional = QTableWidgetItem()
            functional.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            functional.setCheckState(Qt.Checked if step.functional else Qt.Unchecked)
            self.steps.setItem(index, STEP_FUNCTIONAL, functional)
            self._decorate_step(index)

    def _decorate_step(self, row):
        with self._quiet():
            self._decorate(row)

    def _decorate(self, row):
        sequence = self.current()
        step = sequence.steps[row]
        addressed = step.kind in ("request", "keep_alive")
        if step.kind == "request" and self.steps.cellWidget(row, STEP_EXPECT) is None:
            expect = _combo([(text, text) for text in EXPECTS], step.expect, editable=True)
            expect.setToolTip("positive, any answer, no answer, not checked - or an NRC: NRC 22")
            expect.currentTextChanged.connect(lambda _t, row=row: self._step_expect_changed(row))
            self.steps.setCellWidget(row, STEP_EXPECT, expect)
        elif step.kind != "request" and self.steps.cellWidget(row, STEP_EXPECT) is not None:
            self.steps.removeCellWidget(row, STEP_EXPECT)
        functional = self.steps.item(row, STEP_FUNCTIONAL)
        if addressed:
            functional.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            functional.setCheckState(Qt.Checked if step.functional else Qt.Unchecked)
        else:
            functional.setFlags(Qt.NoItemFlags)
            functional.setData(Qt.CheckStateRole, None)
        value = self.steps.item(row, STEP_VALUE)
        problem = step.problem()
        value.setBackground(BAD if problem else QColor(0, 0, 0, 0))
        value.setToolTip(problem or HINTS.get(step.kind, ""))

    def _step_kind_changed(self, row):
        sequence = self.current()
        if self._filling or sequence is None or row >= len(sequence.steps):
            return
        sequence.steps[row].kind = self.steps.cellWidget(row, STEP_KIND).currentData()
        self._decorate_step(row)
        self._fill_list()
        self.changed.emit()

    def _step_expect_changed(self, row):
        sequence = self.current()
        if self._filling or sequence is None or row >= len(sequence.steps):
            return
        sequence.steps[row].expect = self.steps.cellWidget(row, STEP_EXPECT).currentText().strip()
        self._decorate_step(row)
        self._fill_list()
        self.changed.emit()

    def _step_item_changed(self, item):
        sequence = self.current()
        if self._filling or sequence is None or item.row() >= len(sequence.steps):
            return
        step = sequence.steps[item.row()]
        if item.column() == STEP_VALUE:
            step.value = item.text().strip()
        elif item.column() == STEP_FUNCTIONAL:
            step.functional = item.checkState() == Qt.Checked
        self._decorate_step(item.row())
        self._fill_list()
        self.changed.emit()

    def add_step(self, kind="request", value=""):
        sequence = self.current()
        if sequence is None:
            return None
        defaults = {"request": "3E 00", "wait": "1", "keep_alive": "5", "session": "01", "unlock": "01",
                    "reset": "01", "frame": "", "script": ""}
        step = SequenceStep(kind, value or defaults.get(kind, ""))
        sequence.steps.append(step)
        self._fill_steps()
        self._fill_list()
        self.steps.setCurrentCell(len(sequence.steps) - 1, STEP_VALUE)
        self.changed.emit()
        return step

    def remove_step(self):
        sequence, row = self.current(), self._row(self.steps)
        if sequence is not None and 0 <= row < len(sequence.steps):
            del sequence.steps[row]
            self._fill_steps()
            self._fill_list()
            self.changed.emit()

    @staticmethod
    def _row(table) -> int:
        """The selected row, else the last (a click in a cell's combo box selects no row)."""
        return table.currentRow() if table.currentRow() >= 0 else table.rowCount() - 1

    def move_step(self, offset):
        sequence, row = self.current(), self.steps.currentRow()
        if sequence is None or not 0 <= row < len(sequence.steps) or not 0 <= row + offset < len(sequence.steps):
            return
        steps = sequence.steps
        steps[row], steps[row + offset] = steps[row + offset], steps[row]
        self._fill_steps()
        self.steps.setCurrentCell(row + offset, STEP_VALUE)
        self.changed.emit()

    def _fill_runs(self):
        sequence = self.current()
        with self._quiet():
            self._fill_run_rows(sequence)

    def _fill_run_rows(self, sequence):
        self.runs.setRowCount(0)
        for index, attachment in enumerate(sequence.attachments if sequence else []):
            self.runs.insertRow(index)
            when = _combo([(text, text) for text in WHEN], attachment.when)
            scope = _combo([(text, key) for key, text in SCOPES.items()], attachment.scope)
            condition = _combo([(text, key) for key, text in CONDITIONS.items()], attachment.condition)
            target = QComboBox()
            for column, widget in ((RUN_WHEN, when), (RUN_SCOPE, scope), (RUN_TARGET, target),
                                   (RUN_CONDITION, condition)):
                self.runs.setCellWidget(index, column, widget)
            self._fill_target(index, attachment)
            when.currentIndexChanged.connect(lambda _i, row=index: self._run_changed(row))
            scope.currentIndexChanged.connect(lambda _i, row=index: self._run_changed(row, scope_changed=True))
            target.currentIndexChanged.connect(lambda _i, row=index: self._run_changed(row))
            condition.currentIndexChanged.connect(lambda _i, row=index: self._run_changed(row))

    def _fill_target(self, row, attachment):
        target = self.runs.cellWidget(row, RUN_TARGET)
        target.blockSignals(True)
        target.clear()
        if attachment.scope == "group":
            for group in self._groups:
                target.addItem(group, group)
        elif attachment.scope == "test":
            for name, title in self._tests.items():
                target.addItem(title, name)
        if attachment.scope in ("group", "test"):
            index = target.findData(attachment.target)
            if index < 0 and attachment.target:              # a test the description no longer has
                target.addItem(f"{attachment.target} (not generated)", attachment.target)
                index = target.count() - 1
            target.setCurrentIndex(max(index, 0))
        target.setEnabled(attachment.scope in ("group", "test"))
        self.runs.cellWidget(row, RUN_CONDITION).setEnabled(attachment.when == "after")
        target.blockSignals(False)

    def _run_changed(self, row, scope_changed=False):
        sequence = self.current()
        if self._filling or sequence is None or row >= len(sequence.attachments):
            return
        attachment = sequence.attachments[row]
        attachment.when = self.runs.cellWidget(row, RUN_WHEN).currentData()
        attachment.scope = self.runs.cellWidget(row, RUN_SCOPE).currentData()
        attachment.condition = self.runs.cellWidget(row, RUN_CONDITION).currentData()
        if scope_changed:
            attachment.target = ""
            self._fill_target(row, attachment)
        target = self.runs.cellWidget(row, RUN_TARGET)
        attachment.target = (target.currentData() or "") if attachment.scope in ("group", "test") else ""
        self.runs.cellWidget(row, RUN_CONDITION).setEnabled(attachment.when == "after")
        self._fill_list()
        self.changed.emit()

    def add_attachment(self):
        sequence = self.current()
        if sequence is not None:
            sequence.attachments.append(Attachment("before", "each"))
            self._fill_runs()
            self._fill_list()
            self.changed.emit()

    def remove_attachment(self):
        sequence, row = self.current(), self._row(self.runs)
        if sequence is not None and 0 <= row < len(sequence.attachments):
            del sequence.attachments[row]
            self._fill_runs()
            self._fill_list()
            self.changed.emit()

