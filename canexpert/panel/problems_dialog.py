"""The problems a panel check found (canexpert/panel/check.py), in a window of their own: each one with where it
is, and the one selected in full - its line with a mark under the place, and what was probably meant."""
from html import escape

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFontDatabase
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from canexpert.ui_common import enable_maximize


class ProblemsDialog(QDialog):
    """The list of problems, the selected one in full, and Copy all. go_to_text: the button that goes to the
    selected problem (double-click or Enter does the same) - None for no such button."""
    go_to = pyqtSignal(object)          # the Problem to go to

    def __init__(self, parent=None, go_to_text="Go to"):
        super().__init__(parent)
        enable_maximize(self)
        self.setWindowTitle("Panel check")
        self.resize(900, 540)
        self.problems = []
        layout = QVBoxLayout(self)
        self.heading = QLabel()
        self.heading.setWordWrap(True)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: gray;")
        layout.addWidget(self.heading)
        layout.addWidget(self.note)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["", "Where", "Problem"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.currentItemChanged.connect(self._show_selected)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setLineWrapMode(QPlainTextEdit.NoWrap)        # the ^ stays under its place
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        if "Consolas" in QFontDatabase().families():         # tells 0 from O, as a typo may need
            font.setFamily("Consolas")
        self.detail.setFont(font)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(self.detail)
        self.splitter.setSizes([300, 160])
        layout.addWidget(self.splitter, 1)

        buttons = QHBoxLayout()
        self.go_button = None
        if go_to_text:
            self.go_button = QPushButton(go_to_text)
            self.go_button.clicked.connect(self._go_to_selected)
            self.tree.itemActivated.connect(lambda _item, _column: self._go_to_selected())
            buttons.addWidget(self.go_button)
        self.copy_button = QPushButton("Copy all")
        self.copy_button.setToolTip("Every problem, in full, to the clipboard")
        self.copy_button.clicked.connect(self.copy_all)
        buttons.addWidget(self.copy_button)
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def show_problems(self, problems, heading, note=""):
        """Fill the window and bring it to the front."""
        self.problems = list(problems)
        self.setWindowTitle(f"Panel check - {heading}")
        self.heading.setText(f"<b>{escape(heading)}</b>")
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.tree.clear()
        style = self.style()
        icons = {True: style.standardIcon(QStyle.SP_MessageBoxCritical),
                 False: style.standardIcon(QStyle.SP_MessageBoxWarning)}
        for index, problem in enumerate(self.problems):
            item = QTreeWidgetItem(["", problem.where(), problem.message])
            item.setIcon(0, icons[problem.is_error])
            item.setToolTip(0, problem.severity.capitalize())
            item.setToolTip(2, problem.message)
            item.setData(0, Qt.UserRole, index)
            self.tree.addTopLevelItem(item)
        for column in (0, 1):
            self.tree.resizeColumnToContents(column)
        self.splitter.setVisible(bool(self.problems))
        self.copy_button.setEnabled(bool(self.problems))
        if self.go_button is not None:
            self.go_button.setEnabled(bool(self.problems))
        if self.problems:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
            if self.height() < 400:
                self.resize(max(self.width(), 900), 540)
        else:
            self.detail.clear()
            self.layout().activate()
            self.resize(560, self.sizeHint().height())     # "no problems found": no empty list below it
        self.show()
        self.raise_()
        self.activateWindow()
        return self

    def selected(self):
        """The problem selected in the list, else None."""
        item = self.tree.currentItem()
        return self.problems[item.data(0, Qt.UserRole)] if item is not None else None

    def _show_selected(self, *_):
        problem = self.selected()
        self.detail.setPlainText(problem.text() if problem is not None else "")

    def _go_to_selected(self):
        problem = self.selected()
        if problem is not None:
            self.go_to.emit(problem)

    def copy_all(self):
        QApplication.clipboard().setText("\n\n".join(problem.text() for problem in self.problems))
