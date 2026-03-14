"""
Diagnostic (ODX/CDD) window: load ODX or CDD files and browse diagnostic definitions.
"""
import xml.etree.ElementTree as ET
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
    QMenu,
)

try:
    import cantools
    HAS_CANTOOLS = True
except ImportError:
    HAS_CANTOOLS = False


def _add_xml_children(parent_item: QTreeWidgetItem, elem: ET.Element, depth: int = 0):
    """Recursively add XML elements to tree; limit depth to avoid huge ODX."""
    if depth > 8:
        return
    for child in elem:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        text = (child.text or "").strip() or child.get("ID") or child.get("SHORT-NAME") or ""
        row = [tag, text[:80] if text else ""]
        node = QTreeWidgetItem(parent_item, row)
        _add_xml_children(node, child, depth + 1)


class DiagnosticOdxWindow(QDialog):
    """Window to load ODX or CDD files and browse diagnostic content."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Diagnostic (ODX / CDD)")
        self.setMinimumSize(700, 500)
        self.file_path = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Toolbar
        bar = QHBoxLayout()
        self.open_btn = QPushButton("Load ODX / CDD file...")
        self.open_btn.clicked.connect(self._load_file)
        bar.addWidget(self.open_btn)
        self.path_label = QLabel("No file loaded")
        self.path_label.setStyleSheet("color: gray;")
        bar.addWidget(self.path_label)
        bar.addStretch()
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(True)

        # Left: structure tree
        tree_group = QGroupBox("Structure")
        tree_group.setMinimumWidth(0)
        tree_layout = QVBoxLayout()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Element", "Value"])
        self.tree.setColumnWidth(0, 220)
        self.tree.header().setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.header().customContextMenuRequested.connect(self._show_tree_column_menu)
        tree_layout.addWidget(self.tree)
        tree_group.setLayout(tree_layout)
        splitter.addWidget(tree_group)

        # Right: raw or summary
        right_group = QGroupBox("Details")
        right_group.setMinimumWidth(0)
        right_layout = QVBoxLayout()
        self.details_text = QPlainTextEdit()
        self.details_text.setReadOnly(True)
        self.details_text.setPlaceholderText("Load an ODX or CDD file to view diagnostic definitions.")
        right_layout.addWidget(self.details_text)
        right_group.setLayout(right_layout)
        splitter.addWidget(right_group)

        splitter.setSizes([350, 350])
        layout.addWidget(splitter)

    def _show_tree_column_menu(self, pos):
        """Context menu on tree header: toggle column visibility."""
        menu = QMenu(self)
        labels = ["Element", "Value"]
        for col in range(min(self.tree.columnCount(), len(labels))):
            act = menu.addAction(f"Show '{labels[col]}'")
            act.setCheckable(True)
            act.setChecked(not self.tree.isColumnHidden(col))
            act.triggered.connect(lambda checked, c=col: self.tree.setColumnHidden(c, not checked))
        menu.exec_(self.tree.header().mapToGlobal(pos))

    def _load_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open ODX / CDD file",
            str(Path.home()),
            "ODX/CDD files (*.odx *.xml *-cdd.xml *.cdd);;All files (*.*)",
        )
        if not path:
            return
        self._load_path(path)

    def _load_path(self, path: str):
        self.file_path = path
        suffix = Path(path).suffix.lower()
        self.path_label.setText(Path(path).name)
        self.path_label.setStyleSheet("")

        if suffix == ".dbc":
            self._load_cdd_cantools(path)
        else:
            self._load_odx_xml(path)

    def _load_odx_xml(self, path: str):
        """Load ODX (XML) and show tree + summary."""
        try:
            tree = ET.parse(path)
            root = tree.getroot()
            self.tree.clear()
            tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
            root_item = QTreeWidgetItem(self.tree, [tag, ""])
            _add_xml_children(root_item, root)
            self.tree.expandToDepth(2)

            # Summary in details
            summary = [f"File: {path}", f"Root: {tag}"]
            for elem in root.iter():
                t = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if t in ("DIAG-LAYER", "ECU-MEM", "PROTOCOL", "REQUEST", "RESPONSE"):
                    name = elem.get("SHORT-NAME") or elem.get("ID") or ""
                    if name:
                        summary.append(f"  {t}: {name}")
            self.details_text.setPlainText("\n".join(summary[:80]))
        except ET.ParseError as e:
            self.details_text.setPlainText(f"XML parse error: {e}")
            self.tree.clear()
        except Exception as e:
            self.details_text.setPlainText(f"Error: {e}")
            self.tree.clear()

    def _load_cdd_cantools(self, path: str):
        """If file is CDD (or DBC), try cantools and show summary."""
        if not HAS_CANTOOLS:
            self.details_text.setPlainText("Install cantools to load CDD/DBC: pip install cantools")
            return
        try:
            db = cantools.database.load_file(path)
            self.tree.clear()
            root_item = QTreeWidgetItem(self.tree, ["Database", path])
            for msg in db.messages:
                node = QTreeWidgetItem(root_item, [msg.name, f"0x{msg.frame_id:X}"])
                for sig in msg.signals:
                    QTreeWidgetItem(node, [sig.name, f"start={sig.start} len={sig.length}"])
            self.tree.expandToDepth(1)
            self.details_text.setPlainText(
                f"Loaded via cantools: {len(db.messages)} messages.\n"
                "CDD/DBC loaded as CAN database; for full ODX use an ODX file."
            )
        except Exception as e:
            self.details_text.setPlainText(f"Error loading file: {e}\nTrying as ODX XML...")
            self._load_odx_xml(path)
