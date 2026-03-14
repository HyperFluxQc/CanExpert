"""
CAN Analysis window: load a DBC file, browse messages/signals, and optionally view decoded data / graphs.
"""
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QFileDialog,
    QTreeWidget,
    QTreeWidgetItem,
    QGroupBox,
    QSplitter,
    QPlainTextEdit,
    QWidget,
    QMenu,
)

try:
    import cantools
    HAS_CANTOOLS = True
except ImportError:
    HAS_CANTOOLS = False


class CanAnalysisWindow(QDialog):
    """Window to load a DBC file and browse messages/signals; placeholder for graph view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CAN Analysis (DBC)")
        self.setMinimumSize(700, 500)
        self.db = None
        self.dbc_path = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Toolbar
        bar = QHBoxLayout()
        self.open_btn = QPushButton("Load DBC file...")
        self.open_btn.clicked.connect(self._load_dbc)
        bar.addWidget(self.open_btn)
        self.path_label = QLabel("No DBC loaded")
        self.path_label.setStyleSheet("color: gray;")
        bar.addWidget(self.path_label)
        bar.addStretch()
        layout.addLayout(bar)

        if not HAS_CANTOOLS:
            layout.addWidget(QLabel("Install cantools to load DBC files: pip install cantools"))
            self.open_btn.setEnabled(False)
            layout.addStretch()
            return

        # Content: tree (messages/signals) + graph placeholder (collapsible)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(True)

        # Left: messages and signals tree
        tree_group = QGroupBox("Messages & Signals")
        tree_group.setMinimumWidth(0)
        tree_layout = QVBoxLayout()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "ID / Start", "Length", "Unit"])
        self.tree.setColumnWidth(0, 200)
        self.tree.header().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.header().customContextMenuRequested.connect(self._show_tree_column_menu)
        tree_layout.addWidget(self.tree)
        tree_group.setLayout(tree_layout)
        splitter.addWidget(tree_group)

        # Right: graph / decoded log placeholder
        right_group = QGroupBox("Graph & Decoded Data")
        right_group.setMinimumWidth(0)
        right_layout = QVBoxLayout()
        self.graph_placeholder = QPlainTextEdit()
        self.graph_placeholder.setReadOnly(True)
        self.graph_placeholder.setPlaceholderText(
            "Graph view: connect to CAN and record traffic to plot selected signals here.\n"
            "Load a DBC file to see messages and signals on the left."
        )
        right_layout.addWidget(self.graph_placeholder)
        right_group.setLayout(right_layout)
        splitter.addWidget(right_group)

        splitter.setSizes([350, 350])
        layout.addWidget(splitter)

    def _show_tree_column_menu(self, pos):
        """Context menu on tree header: toggle column visibility."""
        menu = QMenu(self)
        labels = ["Name", "ID / Start", "Length", "Unit"]
        for col in range(min(self.tree.columnCount(), len(labels))):
            act = menu.addAction(f"Show '{labels[col]}'")
            act.setCheckable(True)
            act.setChecked(not self.tree.isColumnHidden(col))
            act.triggered.connect(lambda checked, c=col: self.tree.setColumnHidden(c, not checked))
        menu.exec_(self.tree.header().mapToGlobal(pos))

    def _load_dbc(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open DBC file",
            str(Path.home()),
            "DBC files (*.dbc);;All files (*.*)",
        )
        if not path:
            return
        self._load_dbc_path(path)

    def _load_dbc_path(self, path: str):
        if not HAS_CANTOOLS:
            return
        try:
            self.db = cantools.database.load_file(path)
            self.dbc_path = path
            self.path_label.setText(Path(path).name)
            self.path_label.setStyleSheet("")
            self._fill_tree()
            self.graph_placeholder.clear()
            self.graph_placeholder.appendPlainText(f"DBC loaded: {len(self.db.messages)} messages.")
        except Exception as e:
            self.path_label.setText(f"Error: {e}")
            self.path_label.setStyleSheet("color: red;")
            self.db = None
            self.tree.clear()

    def _fill_tree(self):
        self.tree.clear()
        if not self.db:
            return
        for msg in self.db.messages:
            node = QTreeWidgetItem([msg.name, f"0x{msg.frame_id:X}", "", ""])
            for sig in msg.signals:
                unit = sig.unit or ""
                child = QTreeWidgetItem([
                    sig.name,
                    str(sig.start),
                    str(sig.length),
                    unit,
                ])
                node.addChild(child)
            self.tree.addTopLevelItem(node)
