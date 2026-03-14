"""
CAN Logger: load a DBC file, log decoded signals from CAN traffic, plot time-series and export CSV.
"""
import csv
from pathlib import Path
from collections import defaultdict

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QLineEdit,
    QFileDialog,
    QScrollArea,
    QSplitter,
    QWidget,
    QCheckBox,
    QGroupBox,
    QFormLayout,
    QDoubleSpinBox,
)

from splitter_panel import SplitterPanel
from PyQt5.QtCore import QSettings

# Optional: pyqtgraph for plotting
try:
    import pyqtgraph as pg
    HAS_PG = True
except ImportError:
    HAS_PG = False

try:
    import cantools
    HAS_CANTOOLS = True
except ImportError:
    HAS_CANTOOLS = False

# Curve colors: light mode (readable on white), dark mode (bright on dark)
_CURVE_COLORS_LIGHT = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
_CURVE_COLORS_DARK = ["#5eb3f6", "#ff6b6b", "#51cf66", "#ffd43b", "#cc92e2", "#e599b3", "#ffa8c5", "#adb5bd", "#d8e057", "#45b5d9"]


def _get_theme() -> str:
    """Return 'light' or 'dark' from app settings."""
    s = QSettings("EZCan2", "KvaserCAN")
    return s.value("theme", "light", type=str) if s else "light"


class GraphOptionsDialog(QDialog):
    """Graph options: Y scale factor, autoscale."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Graph options")
        layout = QVBoxLayout()
        self.setLayout(layout)  # do not use QVBoxLayout(self) to avoid reparent layout on close
        form = QFormLayout()
        self.y_scale_spin = QDoubleSpinBox()
        self.y_scale_spin.setRange(0.01, 1000.0)
        self.y_scale_spin.setValue(1.0)
        self.y_scale_spin.setDecimals(3)
        form.addRow("Y scale factor:", self.y_scale_spin)
        self.autoscale_cb = QCheckBox("Autoscale to received data")
        self.autoscale_cb.setChecked(True)
        form.addRow(self.autoscale_cb)
        layout.addLayout(form)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        layout.addWidget(ok_btn)


class CANLoggerWindow(QDialog):
    """Window to load DBC, select signals, plot time-series and save CSV."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CAN Logger")
        self.setMinimumSize(900, 550)
        self.db = None
        self.dbc_path = None
        self._curves = {}
        self._checkboxes = {}
        self._data = defaultdict(list)
        self._graph_options = None
        self._y_scale = 1.0
        self._autoscale = True
        self._cursor_a = None
        self._cursor_b = None
        self._cursor_label = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Toolbar
        bar = QHBoxLayout()
        load_btn = QPushButton("Load DBC...")
        load_btn.clicked.connect(self._load_dbc)
        bar.addWidget(load_btn)
        save_btn = QPushButton("Save CSV...")
        save_btn.clicked.connect(self._save_csv)
        bar.addWidget(save_btn)
        options_btn = QPushButton("Graph options...")
        options_btn.clicked.connect(self._show_graph_options)
        bar.addWidget(options_btn)
        # Selectable status/error line for easy copy-paste
        self.path_status = QLineEdit()
        self.path_status.setReadOnly(True)
        self.path_status.setPlaceholderText("No DBC loaded")
        self.path_status.setText("No DBC loaded")
        self.path_status.setStyleSheet("QLineEdit { border: none; background: transparent; color: gray; }")
        self.path_status.setMinimumWidth(200)
        bar.addWidget(self.path_status)
        bar.addStretch()
        layout.addLayout(bar)

        # Content: graph + signal list (resizable, collapsible splitter with minimize)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(True)
        if HAS_PG:
            graph_group = QWidget()
            graph_group.setMinimumWidth(0)
            graph_layout = QVBoxLayout(graph_group)
            self.plot_widget = pg.PlotWidget()
            self.plot_widget.showGrid(x=True, y=True)
            self.plot_widget.setLabel("left", "Value")
            self.plot_widget.setLabel("bottom", "Time (s)")
            self.plot_widget.addLegend()
            self._cursor_a = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("b", width=2))
            self._cursor_b = pg.InfiniteLine(angle=90, movable=True, pen=pg.mkPen("r", width=2))
            self.plot_widget.addItem(self._cursor_a)
            self.plot_widget.addItem(self._cursor_b)
            self._cursor_label = pg.TextItem("", anchor=(0, 1))
            self.plot_widget.addItem(self._cursor_label)
            self._cursor_a.sigPositionChanged.connect(self._update_cursor_readout)
            self._cursor_b.sigPositionChanged.connect(self._update_cursor_readout)
            self._apply_graph_theme()
            graph_layout.addWidget(self.plot_widget)
            graph_panel = SplitterPanel("Graph", graph_group, Qt.Horizontal)
            splitter.addWidget(graph_panel)
        else:
            no_pg = QLabel("Install pyqtgraph for plotting: pip install pyqtgraph")
            no_pg.setMinimumWidth(0)
            no_pg_panel = SplitterPanel("Graph", no_pg, Qt.Horizontal)
            splitter.addWidget(no_pg_panel)

        # Right: signal checkboxes (resizable, collapsible)
        signals_group = QWidget()
        signals_group.setMinimumWidth(0)
        signals_layout = QVBoxLayout(signals_group)
        self.signals_scroll = QScrollArea()
        self.signals_scroll.setWidgetResizable(True)
        self.signals_container = QWidget()
        self.signals_inner = QVBoxLayout()
        self.signals_container.setLayout(self.signals_inner)
        self.signals_scroll.setWidget(self.signals_container)
        signals_layout.addWidget(self.signals_scroll)
        signals_panel = SplitterPanel("Signals (select to plot)", signals_group, Qt.Horizontal)
        splitter.addWidget(signals_panel)
        splitter.setSizes([700, 280])
        layout.addWidget(splitter)

        if not HAS_CANTOOLS:
            load_btn.setEnabled(False)
            self.path_status.setText("Install cantools: pip install cantools")

    def _theme_colors(self):
        """Return dict with background, axis, grid, text, cursor_a, cursor_b, and curve color list for current theme."""
        dark = _get_theme() == "dark"
        if dark:
            return {
                "background": QColor(35, 35, 35),
                "axis": QColor(220, 220, 220),
                "grid": QColor(80, 80, 80),
                "text": QColor(220, 220, 220),
                "cursor_a": QColor(100, 180, 255),
                "cursor_b": QColor(255, 120, 120),
                "curves": _CURVE_COLORS_DARK,
            }
        return {
            "background": QColor(255, 255, 255),
            "axis": QColor(0, 0, 0),
            "grid": QColor(200, 200, 200),
            "text": QColor(0, 0, 0),
            "cursor_a": QColor(0, 0, 200),
            "cursor_b": QColor(200, 0, 0),
            "curves": _CURVE_COLORS_LIGHT,
        }

    def _apply_graph_theme(self):
        """Apply light/dark theme to the graph (background, axes, grid, cursors, legend)."""
        if not HAS_PG or not getattr(self, "plot_widget", None):
            return
        c = self._theme_colors()
        self.plot_widget.setBackground(c["background"])
        plot_item = self.plot_widget.getPlotItem()
        for ax in ("left", "bottom"):
            axis = plot_item.getAxis(ax)
            axis.setPen(pg.mkColor(c["axis"]))
            axis.setTextPen(pg.mkColor(c["text"]))
        try:
            plot_item.legend.setLabelTextColor(c["text"])
        except Exception:
            pass
        if self._cursor_a:
            self._cursor_a.setPen(pg.mkPen(c["cursor_a"], width=2))
        if self._cursor_b:
            self._cursor_b.setPen(pg.mkPen(c["cursor_b"], width=2))
        if self._cursor_label:
            self._cursor_label.setColor(pg.mkColor(c["text"]))
        # Re-apply curve colors so they match theme
        curve_list = list(self._curves.items())
        for idx, (display_name, curve) in enumerate(curve_list):
            color = c["curves"][idx % len(c["curves"])]
            curve.setPen(pg.mkPen(color))

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_graph_theme()

    def _load_dbc(self):
        default_dir = Path(__file__).parent / "DBC"
        path, _ = QFileDialog.getOpenFileName(
            self, "Load DBC file", str(default_dir),
            "DBC files (*.dbc);;All files (*.*)",
        )
        if path:
            self.load_dbc_from_path(path)

    def load_dbc_from_path(self, path: str | Path):
        """Load DBC from path (called from file dialog or from main when config has DBC)."""
        if not HAS_CANTOOLS:
            return
        path = Path(path)
        try:
            self.db = cantools.database.load_file(str(path))
            self.dbc_path = str(path)
            self.path_status.setText(path.name)
            self.path_status.setStyleSheet("QLineEdit { border: none; background: transparent; }")
            self._data.clear()
            self._curves.clear()
            self._checkboxes.clear()
            # Clear plot
            if HAS_PG:
                self.plot_widget.clear()
                self.plot_widget.addItem(self._cursor_a)
                self.plot_widget.addItem(self._cursor_b)
                self.plot_widget.addItem(self._cursor_label)
            # Rebuild signal list
            while self.signals_inner.count():
                child = self.signals_inner.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()
            for msg in self.db.messages:
                for sig in msg.signals:
                    display_name = f"{msg.name}.{sig.name}"
                    cb = QCheckBox(display_name)
                    cb.stateChanged.connect(lambda *a, dn=display_name: self._on_signal_toggled(dn))
                    self._checkboxes[display_name] = cb
                    self.signals_inner.addWidget(cb)
            self.signals_inner.addStretch()
        except Exception as e:
            err_msg = str(e)
            if HAS_CANTOOLS and "cantools" in type(e).__module__:
                self.path_status.setText(f"DBC file error: {err_msg}")
            else:
                self.path_status.setText(f"Load error: {err_msg}")
            self.path_status.setStyleSheet("QLineEdit { border: none; background: transparent; color: red; }")
            self.db = None

    def _on_signal_toggled(self, display_name: str):
        if not HAS_PG:
            return
        if self._checkboxes[display_name].isChecked():
            if display_name not in self._curves:
                d = self._data.get(display_name, [])
                if d:
                    t = [x[0] for x in d]
                    v = [x[1] * self._y_scale for x in d]
                    colors = self._theme_colors()["curves"]
                    pen = pg.mkPen(colors[len(self._curves) % len(colors)])
                    curve = self.plot_widget.plot(t, v, name=display_name, pen=pen)
                    self._curves[display_name] = curve
        else:
            if display_name in self._curves:
                self.plot_widget.removeItem(self._curves[display_name])
                del self._curves[display_name]

    def _show_graph_options(self):
        layout_already = getattr(self, "_graph_options", None)
        if layout_already is not None:
            try:
                self._graph_options.close()
            except Exception:
                pass
        self._graph_options = GraphOptionsDialog(self)
        self._graph_options.y_scale_spin.setValue(self._y_scale)
        self._graph_options.autoscale_cb.setChecked(self._autoscale)
        if self._graph_options.exec_() == QDialog.Accepted:
            self._y_scale = self._graph_options.y_scale_spin.value()
            self._autoscale = self._graph_options.autoscale_cb.isChecked()
            for display_name, curve in list(self._curves.items()):
                if display_name in self._data and self._data[display_name]:
                    t, v = zip(*self._data[display_name])
                    curve.setData(list(t), [y * self._y_scale for y in v])
            if HAS_PG and self._autoscale and self._curves:
                self.plot_widget.autoRange()

    def _update_cursor_readout(self):
        if not HAS_PG or not self._cursor_a or not self._cursor_b or not self._cursor_label:
            return
        xa = self._cursor_a.value()
        xb = self._cursor_b.value()
        dx = xb - xa
        text = f"X1={xa:.3f}  X2={xb:.3f}  ΔX={dx:.3f}"
        curves = list(self._curves.values()) if self._curves else []
        first_curve = curves[0] if curves else None
        if first_curve is not None:
            xs = getattr(first_curve, "xData", None)
            ys = getattr(first_curve, "yData", None)
            if xs is not None and ys is not None and len(xs) and len(ys):
                try:
                    import numpy as np
                    xarr = np.asarray(xs)
                    yarr = np.asarray(ys)
                    idx_a = min(max(0, int(np.searchsorted(xarr, xa))), len(yarr) - 1)
                    idx_b = min(max(0, int(np.searchsorted(xarr, xb))), len(yarr) - 1)
                    ya = float(yarr[idx_a])
                    yb = float(yarr[idx_b])
                    text += f"  Y1={ya:.3f}  Y2={yb:.3f}  ΔY={yb - ya:.3f}"
                except Exception:
                    pass
        self._cursor_label.setText(text)
        self._cursor_label.setPos(xa, 0)

    def on_can_message(self, arb_id: int, data: bytes | list):
        """Called by main window when a CAN message is received; decode with DBC and append to series."""
        if not self.db or not HAS_CANTOOLS:
            return
        try:
            decoded = self.db.decode_message(arb_id, bytes(data[:8]))
        except Exception:
            return
        t = getattr(self, "_time_ref", None)
        if t is None:
            from datetime import datetime
            self._time_ref = datetime.now()
            t = 0.0
        else:
            from datetime import datetime
            t = (datetime.now() - self._time_ref).total_seconds()
        for msg in self.db.messages:
            if msg.frame_id != arb_id:
                continue
            for sig in msg.signals:
                if sig.name in decoded:
                    display_name = f"{msg.name}.{sig.name}"
                    val = decoded[sig.name]
                    self._data[display_name].append((t, val))
                    if display_name in self._checkboxes and self._checkboxes[display_name].isChecked():
                        if display_name not in self._curves and HAS_PG:
                            colors = self._theme_colors()["curves"]
                            pen = pg.mkPen(colors[len(self._curves) % len(colors)])
                            curve = self.plot_widget.plot(
                                [x[0] for x in self._data[display_name]],
                                [x[1] * self._y_scale for x in self._data[display_name]],
                                name=display_name, pen=pen
                            )
                            self._curves[display_name] = curve
                        elif display_name in self._curves:
                            d = self._data[display_name]
                            self._curves[display_name].setData(
                                [x[0] for x in d],
                                [x[1] * self._y_scale for x in d],
                            )
            break
        if HAS_PG and self._autoscale and self._curves:
            self.plot_widget.autoRange()

    def _save_csv(self):
        if not self._data:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV", "", "CSV files (*.csv);;All files (*.*)",
        )
        if not path:
            return
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Time", "Signal", "Value"])
            for display_name, points in self._data.items():
                for t, v in points:
                    w.writerow([t, display_name, v])
