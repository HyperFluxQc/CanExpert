"""
ODX, PDX and CDD files in the UDS Console: the services a file describes, a request built from their
parameters, and answers decoded with it. Needs odxtools.

Loaded once in the console's ODX tab, a file serves three times: its services can be sent from there with
named parameters, every answer the console gets - whichever tab sent the request - is decoded with it where
the file describes the service, and the fault memory tab shows each DTC's text from its DTC-DOPs.
"""
from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canexpert.paths import ODX_DIR

try:
    from odxtools import load_odx_file, load_pdx_file
    HAS_ODXTOOLS = True
except ImportError:
    HAS_ODXTOOLS = False

FILE_FILTER = "ODX/PDX/CDD (*.odx *.odx-d *.odx-c *.odx-e *.odx-f *.odx-v *.pdx *-cdd.xml *.xml);;All files (*.*)"


def load_database(path):
    """An odxtools database from an ODX (or CDD exported as ODX) or PDX file."""
    path = Path(path)
    return load_pdx_file(path) if path.suffix.lower() == ".pdx" else load_odx_file(path)


def first_layer(database):
    """The layer whose services are offered: the first ECU, else the first diagnostic layer."""
    layers = getattr(database, "ecus", None) or getattr(database, "diag_layers", None) or []
    return layers[0] if layers else None


def services(layer):
    """(service, parent service or None) of a layer, sub-services after their parent."""
    for service in getattr(layer, "services", None) or []:
        yield service, None
        for sub_service in getattr(service, "related_diag_comms", None) or []:
            yield sub_service, service


def dtc_display(code: int) -> str:
    """A 3-byte DTC as SAE J2012 writes it: P0101-00 - the system (P, C, B, U), the four characters, and the
    failure type byte."""
    high = (code >> 16) & 0xFF
    return f"{'PCBU'[high >> 6]}{(high >> 4) & 0x3}{high & 0xF:X}{(code >> 8) & 0xFF:02X}-{code & 0xFF:02X}"


def dtc_texts(layer) -> dict:
    """{trouble code: (display code, text)} from the DTC-DOPs of a layer, including those it inherits; the
    text is the DTC's TEXT, else its long or short name."""
    texts = {}
    spec = getattr(layer, "diag_data_dictionary_spec", None)
    for dop in getattr(spec, "dtc_dops", None) or []:
        for dtc in getattr(dop, "dtcs", None) or []:
            code = getattr(dtc, "trouble_code", None)
            if code is None:
                continue
            text = getattr(dtc, "text", None)
            text = str(text).strip() if text is not None else ""
            texts[int(code)] = (getattr(dtc, "display_trouble_code", None) or "",
                                text or getattr(dtc, "long_name", None) or getattr(dtc, "short_name", "") or "")
    return texts


def dtc_text(texts: dict, code: int):
    """(display code, text) for a DTC read from the ECU: its own entry, else the one for the DTC without its
    failure type byte (files that list 2-byte codes); None when the file does not describe it."""
    return texts.get(code) or texts.get(code >> 8)


def name_of(item) -> str:
    return getattr(item, "short_name", None) or getattr(item, "long_name", None) or str(item)


def decoded(layer, request: bytes, reply: bytes, service=None) -> str | None:
    """The answer as the ODX file reads it: with the service that built the request, or whichever service
    of the layer matches the request. None when the file has nothing to say about it."""
    try:
        if service is not None:
            return str(service.decode_message(reply))
        if layer is None:
            return None
        messages = layer.decode_response(reply, request)
        return "; ".join(str(message) for message in messages) or None
    except Exception as exc:                  # the reply stands; say why the ODX could not read it
        return f"not decoded: {exc}" if service is not None else None


class OdxTab(QWidget):
    """The console's ODX tab: load a file, pick a service, fill its parameters, send."""
    layer_changed = pyqtSignal()          # a file was loaded, or failed to load

    def __init__(self, send, parent=None):
        """send(payload, title, service) runs the request through the console's session."""
        super().__init__(parent)
        self.send = send
        self.layer = None
        self.service = None
        self._widgets = {}                    # parameter name -> (parameter, widget)
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.load_btn = QPushButton("Load ODX / CDD...")
        self.load_btn.clicked.connect(self._choose)
        bar.addWidget(self.load_btn)
        self.path_label = QLabel("No file loaded" if HAS_ODXTOOLS else "Install odxtools: pip install odxtools")
        self.path_label.setStyleSheet("color: gray;")
        bar.addWidget(self.path_label, 1)
        layout.addLayout(bar)
        self.load_btn.setEnabled(HAS_ODXTOOLS)

        splitter = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Service"])
        self.tree.itemSelectionChanged.connect(self._on_selected)
        splitter.addWidget(self.tree)
        form_side = QWidget()
        form_layout = QVBoxLayout(form_side)
        form_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_widget = QWidget()
        self.form = QFormLayout(form_widget)
        scroll.setWidget(form_widget)
        form_layout.addWidget(scroll, 1)
        self.send_btn = QPushButton("Send")
        self.send_btn.setToolTip("Encode the service with these parameters and send it")
        self.send_btn.clicked.connect(self.send_selected)
        self.send_btn.setEnabled(False)
        form_layout.addWidget(self.send_btn)
        splitter.addWidget(form_side)
        splitter.setSizes([260, 420])
        layout.addWidget(splitter, 1)

    # --- the file ----------------------------------------------------------------------------------

    def _choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load ODX / CDD / PDX", str(ODX_DIR), FILE_FILTER)
        if path:
            self.load(path)

    def load(self, path):
        """Load a file; its services fill the tree. False, with the reason shown, when it cannot be read."""
        try:
            self.set_layer(first_layer(load_database(path)), Path(path).name)
        except Exception as exc:
            self.layer = None
            self.tree.clear()
            self.path_label.setText(f"Cannot read {Path(path).name}: {exc}")
            self.path_label.setStyleSheet("color: red;")
            self.layer_changed.emit()
            return False
        return True

    def set_layer(self, layer, title=""):
        """Offer a layer's services (also how the tests give it one)."""
        self.layer = layer
        self.path_label.setText(title or "ODX loaded")
        self.path_label.setStyleSheet("")
        self.tree.clear()
        items = {}                            # by id(): odxtools' services are dataclasses, not all hashable
        for service, parent in services(layer):
            item = QTreeWidgetItem([name_of(service)])
            item.setData(0, Qt.UserRole, service)
            if parent is not None and id(parent) in items:
                items[id(parent)].addChild(item)
            else:
                self.tree.addTopLevelItem(item)
                items[id(service)] = item
        self.layer_changed.emit()

    # --- the request ---------------------------------------------------------------------------------

    def _on_selected(self):
        items = self.tree.selectedItems()
        if items:
            self.show_service(items[0].data(0, Qt.UserRole))

    def show_service(self, service):
        """The form for a service: a field per free parameter, the coded ones shown as such."""
        while self.form.count():
            child = self.form.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self._widgets = {}
        self.service = service
        request = getattr(service, "request", None)
        self.send_btn.setEnabled(request is not None)
        if request is None:
            self.form.addRow(QLabel("(No request for this service)"))
            return
        free = {parameter.short_name for parameter in getattr(service, "free_parameters", [])}
        for parameter in getattr(request, "parameters", None) or []:
            name = name_of(parameter)
            if name not in free:
                self.form.addRow(name + ":", QLabel("(coded/fixed)"))
                continue
            physical = getattr(getattr(parameter, "dop", None), "physical_type", None)
            base_type = str(getattr(getattr(physical, "base_data_type", None), "value", "")).upper()
            if "FLOAT" in base_type or "DOUBLE" in base_type:
                widget = QDoubleSpinBox()
                widget.setRange(-1e9, 1e9)
                widget.setDecimals(6)
            elif "SINT" in base_type or "A_INT" in base_type:
                widget = QSpinBox()
                widget.setRange(-2**31, 2**31 - 1)
            elif "UINT" in base_type:
                widget = QLineEdit("0")
            else:
                widget = QLineEdit()
            self._widgets[name] = (parameter, widget)
            self.form.addRow(name + ":", widget)

    def values(self) -> dict:
        values = {}
        for name, (_parameter, widget) in self._widgets.items():
            if hasattr(widget, "value"):
                values[name] = widget.value()
            else:
                try:
                    values[name] = int(widget.text(), 0)
                except ValueError:
                    values[name] = widget.text()
        return values

    def send_selected(self):
        """Encode the chosen service and hand it to the console."""
        if self.service is None:
            return None
        try:
            payload = bytes(self.service.encode_request(**self.values()))
        except Exception as exc:
            self.path_label.setText(f"Cannot encode {name_of(self.service)}: {exc}")
            self.path_label.setStyleSheet("color: red;")
            return None
        return self.send(payload, name_of(self.service), self.service)
