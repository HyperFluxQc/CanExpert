"""
Diagnostic Window: load ODX/CDD/PDX, list sendable services (with sub-services),
build request form from ODX parameters and data choices, send UDS request, monitor Server/ECU CAN IDs.
"""
from pathlib import Path
from datetime import datetime

import can
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
    QFormLayout,
    QScrollArea,
    QWidget,
    QGroupBox,
    QPlainTextEdit,
    QSpinBox,
    QDoubleSpinBox,
    QLineEdit,
    QComboBox,
    QFrame,
    QSplitter,
    QMenu,
)

try:
    from odxtools import load_odx_file, load_pdx_file
    HAS_ODXTOOLS = True
except ImportError:
    HAS_ODXTOOLS = False

from splitter_panel import SplitterPanel


def _get_services_from_db(db):
    """Yield (service, parent_service_or_None) from database: top-level services and related_diag_comms as sub."""
    if not HAS_ODXTOOLS or db is None:
        return
    # Prefer ECU layer services; fallback to first diag_layer
    layers = getattr(db, "diag_layers", None) or []
    ecus = getattr(db, "ecus", None) or []
    if ecus:
        for ecu in ecus:
            svcs = getattr(ecu, "services", None) or []
            for s in svcs:
                yield (s, None)
                for sub in getattr(s, "related_diag_comms", None) or []:
                    yield (sub, s)
            break
    if not ecus and layers:
        layer = layers[0]
        svcs = getattr(layer, "services", None) or []
        for s in svcs:
            yield (s, None)
            for sub in getattr(s, "related_diag_comms", None) or []:
                yield (sub, s)


class DiagnosticWindow(QDialog):
    """Load ODX/CDD/PDX, show services tree, request form, send UDS, monitor Server/ECU traffic."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Diagnostic Window")
        self.setMinimumSize(850, 600)
        self.odx_db = None
        self.odx_path = None
        self._param_widgets = {}
        self._request_id = 0x7E0
        self._response_id = 0x7E8
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Toolbar
        bar = QHBoxLayout()
        load_btn = QPushButton("Load ODX / CDD...")
        load_btn.clicked.connect(self._load_odx)
        bar.addWidget(load_btn)
        self.path_label = QLabel("No file loaded")
        self.path_label.setStyleSheet("color: gray;")
        bar.addWidget(self.path_label)
        bar.addStretch()
        layout.addLayout(bar)

        if not HAS_ODXTOOLS:
            layout.addWidget(QLabel("Install odxtools: pip install odxtools"))
            load_btn.setEnabled(False)
            return

        # Content: services tree | request form + monitor (collapsible splitter with minimize)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(True)
        # Left: services tree
        left_inner = QWidget()
        left_inner.setMinimumWidth(0)
        left_layout = QVBoxLayout(left_inner)
        self.services_tree = QTreeWidget()
        self.services_tree.setHeaderLabels(["Service"])
        self.services_tree.setColumnWidth(0, 220)
        self.services_tree.header().setContextMenuPolicy(Qt.CustomContextMenu)
        self.services_tree.header().customContextMenuRequested.connect(self._show_services_column_menu)
        self.services_tree.itemSelectionChanged.connect(self._on_service_selected)
        left_layout.addWidget(self.services_tree)
        left_panel = SplitterPanel("Services", left_inner, Qt.Horizontal)
        splitter.addWidget(left_panel)

        # Right: request form + send + monitor
        right_widget = QWidget()
        right_widget.setMinimumWidth(0)
        right = QVBoxLayout()
        right_widget.setLayout(right)
        form_group = QGroupBox("Request (ODX parameters)")
        form_layout = QVBoxLayout()
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_container = QWidget()
        self.form_inner = QFormLayout()
        self.form_container.setLayout(self.form_inner)
        self.form_scroll.setWidget(self.form_container)
        form_layout.addWidget(self.form_scroll)
        form_group.setLayout(form_layout)
        right.addWidget(form_group, 1)

        send_row = QHBoxLayout()
        self.send_btn = QPushButton("Send UDS request")
        self.send_btn.clicked.connect(self._send_request)
        send_row.addWidget(self.send_btn)
        send_row.addStretch()
        right.addLayout(send_row)

        monitor_group = QGroupBox("CAN monitor (Server / ECU IDs only)")
        monitor_layout = QVBoxLayout()
        self.monitor_log = QPlainTextEdit()
        self.monitor_log.setReadOnly(True)
        self.monitor_log.setMaximumBlockCount(1000)
        monitor_layout.addWidget(self.monitor_log)
        monitor_group.setLayout(monitor_layout)
        right.addWidget(monitor_group, 0)
        right_panel = SplitterPanel("Request & monitor", right_widget, Qt.Horizontal)
        splitter.addWidget(right_panel)
        splitter.setSizes([280, 520])
        layout.addWidget(splitter)

    def _show_services_column_menu(self, pos):
        """Context menu on Services tree header: toggle column visibility."""
        menu = QMenu(self)
        for col in range(self.services_tree.columnCount()):
            act = menu.addAction("Show 'Service'")
            act.setCheckable(True)
            act.setChecked(not self.services_tree.isColumnHidden(col))
            act.triggered.connect(lambda checked, c=col: self.services_tree.setColumnHidden(c, not checked))
        menu.exec_(self.services_tree.header().mapToGlobal(pos))

    def _load_odx(self):
        default_dir = Path(__file__).parent / "ODX"
        path, _ = QFileDialog.getOpenFileName(
            self, "Load ODX / CDD / PDX", str(default_dir),
            "ODX/PDX/CDD (*.odx *.pdx *-cdd.xml *.xml);;All files (*.*)",
        )
        if path:
            self.load_odx_from_path(path)

    def load_odx_from_path(self, path: str | Path):
        """Load ODX/PDX from path (file dialog or main when config has ODX)."""
        if not HAS_ODXTOOLS:
            return
        path = Path(path)
        try:
            suffix = path.suffix.lower()
            if suffix == ".pdx":
                self.odx_db = load_pdx_file(path)
            else:
                self.odx_db = load_odx_file(path)
            self.odx_path = str(path)
            self.path_label.setText(path.name)
            self.path_label.setStyleSheet("")
            self._fill_services_tree()
            self._clear_request_form()
        except Exception as e:
            self.path_label.setText(f"Error: {e}")
            self.path_label.setStyleSheet("color: red;")
            self.odx_db = None
            self.services_tree.clear()

    def _fill_services_tree(self):
        self.services_tree.clear()
        if not self.odx_db:
            return
        parent_to_item = {}
        for obj, parent_svc in _get_services_from_db(self.odx_db):
            name = getattr(obj, "short_name", None) or getattr(obj, "long_name", None) or str(obj)
            item = QTreeWidgetItem([name])
            item.setData(0, Qt.UserRole, obj)
            if parent_svc is None:
                self.services_tree.addTopLevelItem(item)
                parent_to_item[obj] = item
            else:
                pitem = parent_to_item.get(parent_svc)
                if pitem:
                    pitem.addChild(item)
                else:
                    self.services_tree.addTopLevelItem(item)

    def _on_service_selected(self):
        items = self.services_tree.selectedItems()
        if not items:
            return
        item = items[0]
        obj = item.data(0, Qt.UserRole)
        if obj is None:
            return
        self._build_request_form(obj)

    def _clear_request_form(self):
        self._param_widgets.clear()
        while self.form_inner.count():
            child = self.form_inner.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _build_request_form(self, diag_service):
        self._clear_request_form()
        req = getattr(diag_service, "request", None)
        if req is None:
            self.form_inner.addRow(QLabel("(No request for this service)"))
            return
        params = getattr(req, "parameters", None) or []
        free_names = {p.short_name for p in getattr(diag_service, "free_parameters", [])}
        for param in params:
            pname = getattr(param, "short_name", None) or getattr(param, "long_name", None) or "?"
            if pname in free_names:
                # Simple widget by type
                dop = getattr(param, "dop_ref", None) or getattr(param, "dop", None)
                base_type = ""
                if dop:
                    base_type = (getattr(dop, "base_data_type", None) or getattr(dop, "physical_type", None) or "").upper()
                if "FLOAT" in base_type or "DOUBLE" in base_type:
                    w = QDoubleSpinBox()
                    w.setRange(-1e9, 1e9)
                    w.setDecimals(6)
                elif "UINT" in base_type or "A_UINT" in base_type:
                    w = QSpinBox()
                    w.setRange(0, 2**32 - 1)
                elif "SINT" in base_type or "A_INT" in base_type:
                    w = QSpinBox()
                    w.setRange(-2**31, 2**31 - 1)
                else:
                    w = QLineEdit()
                self._param_widgets[pname] = (param, w)
                self.form_inner.addRow(pname + ":", w)
            else:
                self.form_inner.addRow(pname + ":", QLabel("(coded/fixed)"))
        self._current_service = diag_service

    def _send_request(self):
        if not getattr(self, "_current_service", None):
            return
        main = self.parent()
        bus = getattr(main, "can_bus", None) if main else None
        if bus is None:
            self.monitor_log.appendPlainText("[No CAN bus] Connect from main window first.")
            return
        try:
            kwargs = {}
            for pname, (param, widget) in self._param_widgets.items():
                if hasattr(widget, "value"):
                    kwargs[pname] = widget.value()
                else:
                    try:
                        kwargs[pname] = int(widget.text(), 0)
                    except ValueError:
                        kwargs[pname] = widget.text()
            payload = self._current_service.encode_request(**kwargs)
        except Exception as e:
            self.monitor_log.appendPlainText(f"[Encode error] {e}")
            return
        cfg = getattr(main, "active_config", None)
        req_id = self._request_id
        if cfg:
            req_id = cfg.get("request_id") or cfg.get("server_id") or self._request_id
        msg = can.Message(arbitration_id=req_id, data=payload, is_extended_id=False)
        try:
            import can
            bus.send(msg)
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            self.monitor_log.appendPlainText(f"{ts}  TX  ID=0x{req_id:X}  {payload.hex()}")
        except Exception as e:
            self.monitor_log.appendPlainText(f"[Send error] {e}")

    def on_can_message(self, arb_id: int, data: bytes | list, direction: str = "RX"):
        """Called by main when a CAN message is received; only log if ID matches Server or ECU."""
        main = self.parent()
        if not main or not getattr(main, "active_config", None):
            return
        cfg = main.active_config
        req_id = cfg.get("request_id") or cfg.get("server_id") or 0x7E0
        resp_id = cfg.get("response_id") or cfg.get("ecu_id") or 0x7E8
        if arb_id != req_id and arb_id != resp_id:
            return
        data = bytes(data)[:8] if data else b""
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.monitor_log.appendPlainText(f"{ts}  {direction}  ID=0x{arb_id:X}  {data.hex()}")
