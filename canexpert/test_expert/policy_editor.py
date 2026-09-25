"""
TestExpert's Deviations tab: the NRC policy - the negative response codes that pass in each situation - and
the accepted deviations, failures agreed on with a comment.
"""
from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canexpert.test_expert.policy import SITUATIONS, Deviation, NrcPolicy, nrc_text, parse_nrcs

NRC_SITUATION, NRC_ISO, NRC_ACCEPTED = range(3)
DEV_TEST, DEV_STEP, DEV_COMMENT, DEV_ADDED = range(4)
BAD = QColor("#fecaca")
CHANGED = QColor("#e0f2fe")


class PolicyEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._policy = NrcPolicy()
        self._deviations: list[Deviation] = []
        self._titles: dict[str, str] = {}
        self._filling = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Vertical)
        layout.addWidget(splitter)

        policy_box = QGroupBox("NRC policy")
        policy_layout = QVBoxLayout(policy_box)
        note = QLabel("The negative response codes that pass in each situation. ISO 14229-1's are the default; add "
                      "the ones your specification allows, or put its own in their place.")
        note.setWordWrap(True)
        policy_layout.addWidget(note)
        self.nrcs = QTableWidget(len(SITUATIONS), 3)
        self.nrcs.setHorizontalHeaderLabels(["Situation", "ISO", "Accepted"])
        self.nrcs.horizontalHeader().setSectionResizeMode(NRC_SITUATION, QHeaderView.Stretch)
        self.nrcs.setColumnWidth(NRC_ISO, 55)
        self.nrcs.setColumnWidth(NRC_ACCEPTED, 90)
        self.nrcs.verticalHeader().hide()
        self.nrcs.itemChanged.connect(self._nrc_changed)
        policy_layout.addWidget(self.nrcs)
        reset = QPushButton("Back to ISO 14229-1")
        reset.clicked.connect(self.reset_policy)
        row = QHBoxLayout()
        row.addWidget(reset)
        row.addStretch()
        policy_layout.addLayout(row)
        splitter.addWidget(policy_box)

        deviation_box = QGroupBox("Accepted deviations")
        deviation_layout = QVBoxLayout(deviation_box)
        note = QLabel("Failures agreed on: shown as accepted instead of failed. Right-click a failed step or a test "
                      "in the results to accept it.")
        note.setWordWrap(True)
        deviation_layout.addWidget(note)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Test", "Step", "Comment", "Date"])
        self.table.horizontalHeader().setSectionResizeMode(DEV_COMMENT, QHeaderView.Stretch)
        self.table.setColumnWidth(DEV_TEST, 130)
        self.table.setColumnWidth(DEV_STEP, 130)
        self.table.setColumnWidth(DEV_ADDED, 80)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.itemChanged.connect(self._comment_changed)
        deviation_layout.addWidget(self.table)
        remove = QPushButton("Remove")
        remove.clicked.connect(self.remove_selected)
        row = QHBoxLayout()
        row.addWidget(remove)
        row.addStretch()
        deviation_layout.addLayout(row)
        splitter.addWidget(deviation_box)
        splitter.setSizes([420, 280])
        self._fill_policy()

    # --- the NRC policy -------------------------------------------------------------------------------

    def set_policy(self, policy: NrcPolicy):
        self._policy = NrcPolicy(dict(policy.nrcs))
        self._fill_policy()

    def policy(self) -> NrcPolicy:
        return NrcPolicy(dict(self._policy.nrcs))

    def reset_policy(self):
        self._policy = NrcPolicy()
        self._fill_policy()
        self.changed.emit()

    def _fill_policy(self):
        self._filling = True
        for row, (situation, (text, iso)) in enumerate(SITUATIONS.items()):
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemIsEnabled)
            item.setData(Qt.UserRole, situation)
            self.nrcs.setItem(row, NRC_SITUATION, item)
            iso_item = QTableWidgetItem(", ".join(f"{nrc:02X}" for nrc in iso))
            iso_item.setFlags(Qt.ItemIsEnabled)
            iso_item.setToolTip(" or ".join(nrc_text(nrc) for nrc in iso))
            self.nrcs.setItem(row, NRC_ISO, iso_item)
            accepted = QTableWidgetItem(", ".join(f"{nrc:02X}" for nrc in self._policy.accepted(situation)))
            self.nrcs.setItem(row, NRC_ACCEPTED, accepted)
            self._decorate(row)
        self._filling = False

    def _decorate(self, row):
        situation = self.nrcs.item(row, NRC_SITUATION).data(Qt.UserRole)
        item = self.nrcs.item(row, NRC_ACCEPTED)
        try:
            nrcs = parse_nrcs(item.text())
            problem = "" if nrcs else "at least one NRC"
        except ValueError as exc:
            nrcs, problem = (), str(exc)
        if problem:
            item.setBackground(BAD)
            item.setToolTip(problem)
            return
        changed = nrcs != NrcPolicy.default(situation)
        item.setBackground(CHANGED if changed else QColor(0, 0, 0, 0))
        item.setToolTip(" or ".join(nrc_text(nrc) for nrc in nrcs))

    def _nrc_changed(self, item):
        if self._filling or item.column() != NRC_ACCEPTED:
            return
        situation = self.nrcs.item(item.row(), NRC_SITUATION).data(Qt.UserRole)
        self._filling = True
        self._decorate(item.row())
        self._filling = False
        try:
            nrcs = parse_nrcs(item.text())
        except ValueError:
            return
        if nrcs:
            self._policy.set(situation, nrcs)
            self.changed.emit()

    # --- accepted deviations --------------------------------------------------------------------------------

    def set_titles(self, titles: dict):
        """test name -> title, for showing the deviations."""
        self._titles = dict(titles)
        self._fill_deviations()

    def set_deviations(self, deviations):
        self._deviations = [Deviation(**deviation.to_dict()) for deviation in deviations]
        self._fill_deviations()

    def deviations(self) -> list[Deviation]:
        return [Deviation(**deviation.to_dict()) for deviation in self._deviations]

    def find(self, test: str, step: str) -> Deviation | None:
        return next((deviation for deviation in self._deviations if deviation.test == test and deviation.step == step),
                    None)

    def add(self, deviation: Deviation):
        existing = self.find(deviation.test, deviation.step)
        if existing is not None:
            existing.comment = deviation.comment
        else:
            self._deviations.append(deviation)
        self._fill_deviations()
        self.changed.emit()

    def remove(self, test: str, step: str):
        self._deviations = [d for d in self._deviations if not (d.test == test and d.step == step)]
        self._fill_deviations()
        self.changed.emit()

    def remove_selected(self):
        row = self.table.currentRow()
        if 0 <= row < len(self._deviations):
            deviation = self._deviations[row]
            self.remove(deviation.test, deviation.step)

    def _fill_deviations(self):
        self._filling = True
        self.table.setRowCount(0)
        for row, deviation in enumerate(self._deviations):
            self.table.insertRow(row)
            title = self._titles.get(deviation.test, deviation.test)
            for column, text in ((DEV_TEST, title), (DEV_STEP, "every failed step" if deviation.step == "*" else
                                                     deviation.step), (DEV_COMMENT, deviation.comment),
                                 (DEV_ADDED, deviation.added)):
                item = QTableWidgetItem(text)
                item.setToolTip(text if column != DEV_TEST else f"{title}\n{deviation.test}")
                if column != DEV_COMMENT:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.table.setItem(row, column, item)
        self._filling = False

    def _comment_changed(self, item):
        if self._filling or item.column() != DEV_COMMENT or item.row() >= len(self._deviations):
            return
        self._deviations[item.row()].comment = item.text().strip()
        self.changed.emit()
