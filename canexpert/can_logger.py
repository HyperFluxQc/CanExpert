"""
CAN Logger: a CANoe-style graphics window. Load a DBC, tick signals in the list and each one
gets its own strip chart; all strips share one time axis. Measurement cursors with the statistics
between them, follow/pause, fit, exact time and value ranges, line or dot drawing, a cap on the
samples kept, and export as CSV (a row per sample or a column per signal), MDF 4 or a picture -
of everything, what is on screen, or what lies between the cursors.
"""
import bisect
import csv
import math
import time
from pathlib import Path

from PyQt5.QtCore import QEvent, QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QIcon, QPalette, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox,
    QMenu,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canexpert.mdf4 import write_mdf4
from canexpert.paths import DBC_DIR
from canexpert.ui_common import SplitterPanel, enable_maximize, is_dark_theme, line_icon, style_toggle

try:
    import numpy as np
    import pyqtgraph as pg
    HAS_PG = True
except ImportError:
    np = None
    HAS_PG = False

try:
    import cantools
    HAS_CANTOOLS = True
except ImportError:
    HAS_CANTOOLS = False

# Curve colors: light mode (readable on white), dark mode (bright on dark)
_CURVE_COLORS_LIGHT = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]
_CURVE_COLORS_DARK = ["#5eb3f6", "#ff6b6b", "#51cf66", "#ffd43b", "#cc92e2", "#e599b3", "#ffa8c5", "#adb5bd", "#d8e057", "#45b5d9"]

# Symbols of the small tool buttons, drawn in a 24 x 24 box (see ui_common.line_icon).
TOOL_ICONS = {
    "clear": '<path d="M5 7h14M10 4h4"/><path d="M7 7l1 13h8l1-13"/><path d="M10.5 10.5v6M13.5 10.5v6"/>',
    "pause": '<path d="M9.5 5v14M14.5 5v14" stroke-width="2.6"/>',
    "play": '<path d="M8 5l11 7-11 7z"/>',
    "follow": '<path d="M3 12h12"/><path d="M11 7l5 5-5 5"/><path d="M20 4v16"/>',
    "fit": '<path d="M4 10V4h6M14 4h6v6M20 14v6h-6M10 20H4v-6"/>',
    "lock_x": '<path d="M10 7V6a2 2 0 0 1 4 0v1"/><rect x="8.5" y="7" width="7" height="5.5" rx="1.2"/>'
              '<path d="M3 18h18"/><path d="M6.5 15.5 4 18l2.5 2.5"/><path d="M17.5 15.5 20 18l-2.5 2.5"/>',
    "lock_y": '<path d="M13 9V8a2 2 0 0 1 4 0v1"/><rect x="11.5" y="9" width="7" height="5.5" rx="1.2"/>'
              '<path d="M6 3v18"/><path d="M3.5 5.5 6 3l2.5 2.5"/><path d="M3.5 18.5 6 21l2.5-2.5"/>',
    "cursors": '<path d="M8 7v14M16 7v14"/><path d="M5.5 4h5l-2.5 3z" fill="currentColor"/>'
               '<path d="M13.5 4h5l-2.5 3z" fill="currentColor"/>',
    "combine": '<rect x="3" y="4" width="18" height="16" rx="2"/>'
               '<path d="M4.5 16l4-5 3 3 3-6 3 4 2-3"/><path d="M4.5 19h15"/>',
}

CURVE_STYLES = ("Line", "Line + dots", "Dots")
CURSOR_DASH = [30, 10]      # dash and gap of the measurement cursors, in pixels

STRIP_MIN_HEIGHT = 110      # px per signal graph; more strips than fit make the graph area scroll
REDRAW_INTERVAL_MS = 50     # curves; the value column refreshes every VALUE_REFRESH_TICKS redraws
VALUE_REFRESH_TICKS = 4
AXIS_WIDTH = 64             # fixed left-axis width keeps all strips' time axes aligned
COL_SIGNAL, COL_VALUE, COL_UNIT, COL_C1, COL_C2, COL_DELTA, COL_MIN, COL_MAX, COL_MEAN, COL_STD = range(10)
CURSOR_COLUMNS = (COL_C1, COL_C2, COL_DELTA, COL_MIN, COL_MAX, COL_MEAN, COL_STD)
DEFAULT_SAMPLE_LIMIT = 1_000_000     # per signal: 16 MB of time and value, about 3 hours at 100 Hz
EXPORT_FORMATS = {"Values in rows (CSV)": "long_csv", "One column per signal (CSV)": "wide_csv",
                  "MDF 4 (.mf4)": "mdf4", "Picture of the graphs (PNG)": "png"}
EXPORT_RANGES = ("Everything recorded", "What is on screen", "Between the cursors")


def _curve_args(color, style):
    """pyqtgraph plot() arguments for a drawing style: a step line, a line with a dot per sample, or dots."""
    dots = {"symbol": "o", "symbolSize": 5, "symbolPen": None, "symbolBrush": color}
    if style == "Dots":
        return {"pen": None, **dots}
    if style == "Line + dots":
        return {"pen": pg.mkPen(color, width=1.5), **dots}
    return {"pen": pg.mkPen(color, width=1.5), "stepMode": "right"}


def _is_dark(widget) -> bool:
    """Dark theme, so the graphs match the rest of the window even when it changes while the logger is open."""
    return is_dark_theme(widget)


def _format(value) -> str:
    if value is None:
        return ""
    return f"{value:.6g}" if isinstance(value, float) else str(value)


class GraphOptionsDialog(QDialog):
    """Graph options: how signals are drawn, the follow window, and exact time and value ranges."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Graph options")
        layout = QVBoxLayout()
        self.setLayout(layout)  # do not use QVBoxLayout(self) to avoid reparent layout on close
        form = QFormLayout()
        self.style_combo = QComboBox()
        self.style_combo.addItems(CURVE_STYLES)
        self.style_combo.setToolTip("Line holds each value until the next one, as an ECU signal does; "
                                    "Dots marks every received sample")
        form.addRow("Draw signals as:", self.style_combo)
        self.window_spin = QDoubleSpinBox()
        self.window_spin.setRange(0.5, 3600.0)
        self.window_spin.setValue(10.0)
        self.window_spin.setSuffix(" s")
        form.addRow("Follow time window:", self.window_spin)

        self.fixed_x_cb = QCheckBox("Fixed time range (turns Follow off)")
        self.x_start, self.x_end = self._range_row(form, self.fixed_x_cb, "Time", " s", 0.1)
        self.autoscale_cb = QCheckBox("Autoscale each graph's Y axis to its data")
        self.autoscale_cb.setChecked(True)
        form.addRow(self.autoscale_cb)
        self.fixed_y_cb = QCheckBox("Fixed value range for every graph")
        self.y_start, self.y_end = self._range_row(form, self.fixed_y_cb, "Value", "", 1.0)
        self.fixed_y_cb.toggled.connect(lambda fixed: self.autoscale_cb.setEnabled(not fixed))
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1_000, 100_000_000)
        self.limit_spin.setSingleStep(100_000)
        self.limit_spin.setGroupSeparatorShown(True)
        self.limit_spin.setValue(DEFAULT_SAMPLE_LIMIT)
        self.limit_spin.setSuffix(" samples")
        self.limit_spin.setToolTip("Beyond this the oldest samples of a signal are dropped, so a long measurement "
                                   "cannot fill the memory; record to a file to keep everything")
        form.addRow("Keep per signal at most:", self.limit_spin)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch()
        for text, slot in (("OK", self.accept), ("Cancel", self.reject)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)

    def _range_row(self, form, checkbox, label, suffix, step):
        """A "from ... to ..." pair of spin boxes, enabled by checkbox."""
        spins = []
        row = QHBoxLayout()
        for index in range(2):
            spin = QDoubleSpinBox()
            spin.setRange(-1e9, 1e9)
            spin.setDecimals(4)
            spin.setSingleStep(step)
            spin.setSuffix(suffix)
            spin.setEnabled(False)
            checkbox.toggled.connect(spin.setEnabled)
            row.addWidget(QLabel("from" if index == 0 else "to"))
            row.addWidget(spin)
            spins.append(spin)
        form.addRow(checkbox)
        form.addRow(f"{label} range:", row)
        return spins

    def ranges(self):
        """((time start, time end) or None, (value start, value end) or None)."""
        x = (self.x_start.value(), self.x_end.value()) if self.fixed_x_cb.isChecked() else None
        y = (self.y_start.value(), self.y_end.value()) if self.fixed_y_cb.isChecked() else None
        return (x if x is None or x[0] < x[1] else None), (y if y is None or y[0] < y[1] else None)


class ExportDialog(QDialog):
    """What to export: the format, the time range and which signals."""

    def __init__(self, logger):
        super().__init__(logger)
        self.setWindowTitle("Export")
        layout = QVBoxLayout()
        self.setLayout(layout)
        form = QFormLayout()
        self.format_combo = QComboBox()
        self.format_combo.addItems(EXPORT_FORMATS)
        form.addRow("Format:", self.format_combo)
        self.range_combo = QComboBox()
        self.range_combo.addItems(EXPORT_RANGES)
        if not logger.cursors_btn.isChecked():                       # no cursors, nothing between them
            self.range_combo.model().item(2).setEnabled(False)
        form.addRow("Time range:", self.range_combo)
        self.plotted_only_cb = QCheckBox("Only the signals with a graph")
        form.addRow("", self.plotted_only_cb)
        self.format_combo.currentTextChanged.connect(self._on_format)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addStretch()
        for text, slot in (("Export...", self.accept), ("Cancel", self.reject)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)

    def _on_format(self, text):
        picture = EXPORT_FORMATS[text] == "png"                      # a picture is what is on screen
        self.range_combo.setEnabled(not picture)
        self.plotted_only_cb.setEnabled(not picture)

    def kind(self) -> str:
        return EXPORT_FORMATS[self.format_combo.currentText()]


class _Series:
    """Growable (time, value) storage; the views handed to pyqtgraph stay valid while appending.

    At most limit samples are kept: when it is reached the oldest quarter goes, so a long measurement
    runs in bounded memory and the dropping costs nothing per sample on average."""
    __slots__ = ("t", "v", "n", "limit", "dropped")

    def __init__(self, limit=DEFAULT_SAMPLE_LIMIT):
        self.n = 0
        self.limit = limit
        self.dropped = 0
        if np is not None:
            self.t, self.v = np.empty(1024), np.empty(1024)
        else:
            self.t, self.v = [], []

    def drop_oldest(self, count: int):
        count = min(count, self.n)
        if count <= 0:
            return
        if np is None:
            del self.t[:count], self.v[:count]
        else:
            self.t[:self.n - count] = self.t[count:self.n]
            self.v[:self.n - count] = self.v[count:self.n]
        self.n -= count
        self.dropped += count

    def set_limit(self, limit: int):
        self.limit = max(1000, int(limit))
        if self.n > self.limit:
            self.drop_oldest(self.n - self.limit)

    def append(self, t: float, v: float):
        if self.n >= self.limit:
            self.drop_oldest(max(1, self.limit // 4))
        if np is None:
            self.t.append(t)
            self.v.append(v)
        else:
            if self.n == len(self.t):
                self.t = np.concatenate([self.t, np.empty(self.n)])
                self.v = np.concatenate([self.v, np.empty(self.n)])
            self.t[self.n] = t
            self.v[self.n] = v
        self.n += 1

    def times(self):
        return self.t[:self.n]

    def values(self):
        return self.v[:self.n]

    def last(self):
        return float(self.v[self.n - 1]) if self.n else None

    def at(self, t: float):
        """Sample-and-hold value at time t (the last sample at or before t)."""
        if not self.n:
            return None
        index = (int(np.searchsorted(self.times(), t, "right")) if np is not None
                 else bisect.bisect_right(self.t, t)) - 1
        return float(self.v[index]) if index >= 0 else None

    def points(self):
        return zip(self.t[:self.n], self.v[:self.n])

    def between(self, start: float, end: float):
        """(times, values) of the samples from start to end, both included."""
        times = self.times()
        if np is not None:
            first, last = int(np.searchsorted(times, start, "left")), int(np.searchsorted(times, end, "right"))
        else:
            first, last = bisect.bisect_left(times, start), bisect.bisect_right(times, end)
        return times[first:last], self.values()[first:last]


class CANLoggerWindow(QDialog):
    """CANoe-style graphics window: tick DBC signals to add one strip chart per signal."""

    def __init__(self, parent=None, symbols=None, clock=None):
        super().__init__(parent)
        self.setWindowTitle("CAN Logger")
        self.clock = clock                   # the measurement's (clock.py): time 0 is when it started
        enable_maximize(self)
        self.setMinimumSize(900, 550)
        self.resize(1200, 750)
        self.db = None
        self.dbc_path = None
        # The application's symbol databases, when it has any: Load DBC... then adds to that list and
        # every window sees the same symbols.
        self.symbols = symbols
        self._series = {}           # "Message.Signal" -> _Series, for every decoded signal
        self._units = {}
        self._items = {}            # "Message.Signal" -> QTreeWidgetItem
        self._decoders = {}         # frame id -> (message, [(signal name, display name)])
        self._plotted = []          # checked signals, in the order they were ticked
        self._colors = {}           # "Message.Signal" -> palette index while plotted
        self._plots = {}            # "Message.Signal" -> (PlotItem, curve)
        self._groups = {}           # "Message.Signal" -> the signal whose graph it is drawn in
        self._group_plots = []      # one (PlotItem, cursor 1, cursor 2) per graph, top to bottom
        self._hover = {}            # graph row -> (dotted vertical line, dotted horizontal line, readout)
        self._new_curve_data = set()     # signals whose graph needs redrawing
        self._new_values = set()         # signals whose Value column needs refreshing
        self._t0 = None
        self._curve_style = CURVE_STYLES[0]
        self._x_range = None        # fixed time range from Graph options, else None
        self._y_range = None        # fixed value range for every graph, else None
        self._autoscale = True
        self._window_seconds = 10.0
        self._sample_limit = DEFAULT_SAMPLE_LIMIT
        self._cursor_pos = [0.0, 0.0]
        self._syncing_cursors = False
        self._ticks = 0
        self._build_ui()
        if symbols is not None:
            symbols.changed.connect(lambda: self.load_databases(symbols.paths))
            if symbols.paths:
                self.load_databases(symbols.paths)
        self._timer = QTimer(self)
        self._timer.setInterval(REDRAW_INTERVAL_MS)
        self._timer.timeout.connect(self._redraw)
        self._timer.start()

    # --- UI -----------------------------------------------------------------------------

    def _tool_button(self, name, tip, checkable=False, checked=False, clicked=None, toggled=None):
        """A small CANoe-style tool button; its symbol follows the theme (see _refresh_tool_icons)."""
        button = QToolButton()
        button.setAutoRaise(True)
        button.setIconSize(QSize(18, 18))
        button.setToolTip(tip)
        button.setAccessibleName(tip.split(":")[0])
        button.setCheckable(checkable)
        button.setChecked(checked)
        if clicked is not None:
            button.clicked.connect(clicked)
        if toggled is not None:
            button.toggled.connect(toggled)
        self._tool_buttons[name] = button
        return style_toggle(button)

    @staticmethod
    def _separator():
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _refresh_tool_icons(self):
        colour = self.palette().color(QPalette.WindowText)
        for name, button in self._tool_buttons.items():
            symbol = "play" if name == "pause" and button.isChecked() else name
            body = TOOL_ICONS[symbol].replace('fill="currentColor"', f'fill="{colour.name()}"')
            button.setIcon(line_icon(body, colour))
            style_toggle(button)          # the style sheet's palette(...) is resolved when it is set

    def _build_ui(self):
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.load_btn = QPushButton("Load DBC...")
        self.load_btn.clicked.connect(self._load_dbc)
        bar.addWidget(self.load_btn)
        export_btn = QPushButton("Export...")
        export_btn.setToolTip("CSV, MDF 4 or a picture - of everything, what is on screen, or between the cursors")
        export_btn.clicked.connect(self.show_export)
        bar.addWidget(export_btn)
        self._tool_buttons = {}
        self.clear_btn = self._tool_button("clear", "Clear: discard recorded data and restart the time axis at 0",
                                           clicked=self.clear_data)
        bar.addWidget(self.clear_btn)
        bar.addWidget(self._separator())
        self.pause_btn = self._tool_button("pause", "Pause: freeze the display; recording continues",
                                           checkable=True, toggled=self._on_pause_toggled)
        bar.addWidget(self.pause_btn)
        self.follow_btn = self._tool_button("follow", "Follow: scroll with the newest data (time window in "
                                            "Graph options)", checkable=True, checked=True)
        bar.addWidget(self.follow_btn)
        self.fit_btn = self._tool_button("fit", "Fit: show all recorded data", clicked=self.fit_all)
        bar.addWidget(self.fit_btn)
        bar.addWidget(self._separator())
        self.lock_x_btn = self._tool_button("lock_x", "Lock X: mouse zoom and pan leave the time axis alone "
                                            "(Follow still scrolls)", checkable=True,
                                            toggled=self._apply_axis_locks)
        bar.addWidget(self.lock_x_btn)
        self.lock_y_btn = self._tool_button("lock_y", "Lock Y: mouse zoom and pan leave the value axes alone "
                                            "(autoscale keeps them fitted)", checkable=True, checked=True,
                                            toggled=self._apply_axis_locks)
        bar.addWidget(self.lock_y_btn)
        bar.addWidget(self._separator())
        self.cursors_btn = self._tool_button("cursors", "Cursors: two measurement cursors across all graphs",
                                             checkable=True, toggled=self._on_cursors_toggled)
        bar.addWidget(self.cursors_btn)
        self.combine_btn = self._tool_button("combine", "Combine: draw every signal in one graph "
                                             "(right-click a signal to group them by hand)",
                                             checkable=True, toggled=self._on_combine_toggled)
        bar.addWidget(self.combine_btn)
        self._refresh_tool_icons()
        bar.addWidget(self._separator())
        options_btn = QPushButton("Graph options...")
        options_btn.clicked.connect(self._show_graph_options)
        bar.addWidget(options_btn)
        # Selectable status/error line for easy copy-paste
        self.path_status = QLineEdit()
        self.path_status.setReadOnly(True)
        self.path_status.setText("No DBC loaded")
        self.path_status.setStyleSheet("QLineEdit { border: none; background: transparent; color: gray; }")
        self.path_status.setMinimumWidth(200)
        bar.addWidget(self.path_status, 1)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(True)

        # Left: DBC signal list, as in CANoe
        signals_group = QWidget()
        signals_group.setMinimumWidth(0)
        signals_layout = QVBoxLayout(signals_group)
        filter_row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter signals...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.filter_edit)
        self.plotted_only_cb = QCheckBox("Plotted only")
        self.plotted_only_cb.toggled.connect(self._apply_filter)
        filter_row.addWidget(self.plotted_only_cb)
        signals_layout.addLayout(filter_row)
        self.signal_tree = QTreeWidget()
        self.signal_tree.setHeaderLabels(["Signal", "Value", "Unit", "Cursor 1", "Cursor 2", "Δ",
                                          "Min", "Max", "Mean", "σ"])
        self.signal_tree.headerItem().setToolTip(COL_MEAN, "Mean of the samples between the cursors")
        self.signal_tree.headerItem().setToolTip(COL_STD, "Standard deviation of the samples between the cursors")
        self.signal_tree.setColumnWidth(COL_SIGNAL, 230)
        self.signal_tree.setColumnWidth(COL_VALUE, 80)
        self.signal_tree.setColumnWidth(COL_UNIT, 50)
        for column in CURSOR_COLUMNS:
            self.signal_tree.setColumnWidth(column, 70)
            self.signal_tree.setColumnHidden(column, True)
        self.signal_tree.itemChanged.connect(self._on_item_changed)
        self.signal_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.signal_tree.customContextMenuRequested.connect(self._signal_menu)
        signals_layout.addWidget(self.signal_tree)
        splitter.addWidget(SplitterPanel("Signals (tick to add a graph)", signals_group, Qt.Horizontal))

        # Right: one strip chart per ticked signal, sharing the time axis
        graph_group = QWidget()
        graph_group.setMinimumWidth(0)
        graph_layout = QVBoxLayout(graph_group)
        self.cursor_label = QLabel("")
        self.cursor_label.setVisible(False)
        graph_layout.addWidget(self.cursor_label)
        self.graph_stack = QStackedWidget()
        self.placeholder = QLabel("Load a DBC and tick signals in the list.\nEach signal gets its own graph.")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet("color: gray;")
        self.graph_stack.addWidget(self.placeholder)
        if HAS_PG:
            self.graph = pg.GraphicsLayoutWidget()
            self.graph.ci.setSpacing(0)
            self.graph_scroll = QScrollArea()
            self.graph_scroll.setWidgetResizable(True)
            self.graph_scroll.setWidget(self.graph)
            self.graph.scene().sigMouseMoved.connect(self._on_mouse_moved)
            self.graph.installEventFilter(self)  # hide the hover readout when the mouse leaves
            self.graph_stack.addWidget(self.graph_scroll)
        else:
            self.graph = None
            self.placeholder.setText("Install pyqtgraph for plotting: pip install pyqtgraph")
        graph_layout.addWidget(self.graph_stack)
        splitter.addWidget(SplitterPanel("Graphics", graph_group, Qt.Horizontal))
        splitter.setSizes([420, 780])
        layout.addWidget(splitter)

        if not HAS_CANTOOLS:
            self.load_btn.setEnabled(False)
            self.path_status.setText("Install cantools: pip install cantools")

    def _theme_colors(self):
        """Background, axis, grid, text, cursor and curve colors for the current theme."""
        if _is_dark(self):
            return {"background": QColor(18, 18, 18), "axis": QColor(200, 200, 200), "text": QColor(220, 220, 220),
                    "cursor": QColor(255, 255, 255), "curves": _CURVE_COLORS_DARK}
        # White would vanish on the light background, so the cursors take the foreground colour there.
        return {"background": QColor(255, 255, 255), "axis": QColor(60, 60, 60), "text": QColor(0, 0, 0),
                "cursor": QColor(20, 20, 20), "curves": _CURVE_COLORS_LIGHT}

    def _color(self, display_name):
        curves = self._theme_colors()["curves"]
        return curves[self._colors[display_name] % len(curves)]

    def _swatch(self, color):
        pixmap = QPixmap(12, 12)
        pixmap.fill(QColor(color))
        return QIcon(pixmap)

    def _apply_graph_theme(self):
        """Re-color graphs and swatches for the current light/dark theme."""
        if self._plotted:
            self._rebuild_strips()
        for name in self._plotted:
            self._items[name].setIcon(COL_SIGNAL, self._swatch(self._color(name)))

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_graph_theme()
        self._refresh_tool_icons()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange, QEvent.ApplicationPaletteChange) and self._tool_buttons:
            self._refresh_tool_icons()
            # The graphs follow a theme change too, but only once this event is delivered: rebuilding the
            # plot items while Qt is still updating them crashes.
            QTimer.singleShot(0, self._apply_graph_theme)

    # --- DBC ------------------------------------------------------------------------------

    def _load_dbc(self):
        default_dir = DBC_DIR
        path, _ = QFileDialog.getOpenFileName(
            self, "Load DBC file", str(default_dir),
            "DBC files (*.dbc);;All files (*.*)",
        )
        if path:
            self.load_dbc_from_path(path)

    def load_dbc_from_path(self, path: str | Path):
        """Load one DBC. With the application's symbol databases, the file joins that list instead, so the
        Trace window and the transmit list see it too."""
        if self.symbols is not None:
            self.symbols.add(path)     # changed() comes back as load_databases()
        else:
            self.load_databases([path])

    def load_databases(self, paths):
        """Show the signals of these DBC files; the ticked signals and the recorded data are reset."""
        if not HAS_CANTOOLS:
            return
        databases, problems = [], []
        for path in paths:
            try:
                databases.append((Path(path), cantools.database.load_file(str(path))))
            except Exception as exc:
                prefix = "DBC file error" if "cantools" in type(exc).__module__ else "Load error"
                problems.append(f"{prefix} in {Path(path).name}: {exc}")
        self.db = databases[-1][1] if databases else None
        self.dbc_path = str(databases[-1][0]) if databases else None
        self.path_status.setText("; ".join(problems) if problems else
                                 (", ".join(path.name for path, _ in databases) or "No DBC loaded"))
        colour = " color: red;" if problems else ""
        self.path_status.setStyleSheet(f"QLineEdit {{ border: none; background: transparent;{colour} }}")
        self._plotted.clear()
        self._colors.clear()
        self._groups.clear()
        self._items.clear()
        self._units.clear()
        self._decoders.clear()
        self.clear_data()
        self.signal_tree.blockSignals(True)
        self.signal_tree.clear()
        messages = [message for _, database in databases for message in database.messages]
        for msg in sorted(messages, key=lambda m: m.name.lower()):
            parent = QTreeWidgetItem(self.signal_tree, [f"{msg.name}  (0x{msg.frame_id:X})"])
            parent.setFlags(Qt.ItemIsEnabled)
            names = []
            for sig in sorted(msg.signals, key=lambda s: s.name.lower()):
                display_name = f"{msg.name}.{sig.name}"
                item = QTreeWidgetItem(parent, [sig.name, "", sig.unit or ""])
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                item.setCheckState(COL_SIGNAL, Qt.Unchecked)
                item.setData(COL_SIGNAL, Qt.UserRole, display_name)
                item.setToolTip(COL_SIGNAL, display_name)
                self._items[display_name] = item
                self._units[display_name] = sig.unit or ""
                names.append((sig.name, display_name))
            self._decoders.setdefault(msg.frame_id, (msg, names))   # the first database wins
        self.signal_tree.blockSignals(False)
        self._apply_filter()
        self._rebuild_strips()

    def _apply_filter(self, *_):
        text = self.filter_edit.text().strip().lower()
        plotted_only = self.plotted_only_cb.isChecked()
        for index in range(self.signal_tree.topLevelItemCount()):
            parent = self.signal_tree.topLevelItem(index)
            visible_children = has_plotted = 0
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                name = child.data(COL_SIGNAL, Qt.UserRole)
                visible = (not text or text in name.lower()) and (not plotted_only or name in self._plotted)
                child.setHidden(not visible)
                visible_children += visible
                has_plotted += name in self._plotted
            parent.setHidden(visible_children == 0)
            if text or plotted_only:
                parent.setExpanded(visible_children > 0)
            elif has_plotted:
                parent.setExpanded(True)  # keep ticked signals (and their values) in view

    # --- strips -------------------------------------------------------------------------

    def _on_item_changed(self, item, column):
        name = item.data(COL_SIGNAL, Qt.UserRole)
        if column != COL_SIGNAL or not name:
            return
        checked = item.checkState(COL_SIGNAL) == Qt.Checked
        if checked and name not in self._plotted:
            used = set(self._colors.values())
            self._colors[name] = next(i for i in range(len(self._plotted) + 1) if i not in used)
            self._plotted.append(name)
            self._groups[name] = self._plotted[0] if self.combine_btn.isChecked() else name
            item.setIcon(COL_SIGNAL, self._swatch(self._color(name)))
        elif not checked and name in self._plotted:
            self._plotted.remove(name)
            del self._colors[name]
            self._groups.pop(name, None)
            for other, group in list(self._groups.items()):     # a graph named after it keeps its signals
                if group == name:
                    self._groups[other] = self._plotted[0] if self._plotted else other
            item.setIcon(COL_SIGNAL, QIcon())
            for column_index in CURSOR_COLUMNS:
                item.setText(column_index, "")
        else:
            return
        self._rebuild_strips()
        self._apply_filter()

    def set_signal_plotted(self, display_name: str, plotted: bool = True):
        """Tick or untick a signal in the list (adds or removes its graph)."""
        self._items[display_name].setCheckState(COL_SIGNAL, Qt.Checked if plotted else Qt.Unchecked)

    # --- which signals share a graph ------------------------------------------------------

    def graph_groups(self):
        """The graphs, in the order they were first ticked: a list of lists of signal names."""
        order = []
        for name in self._plotted:
            group = self._groups.get(name, name)
            if group not in order:
                order.append(group)
        return [[name for name in self._plotted if self._groups.get(name, name) == group] for group in order]

    def set_graph_group(self, name: str, other: str | None = None):
        """Put a signal in the graph of another signal, or (other=None) back in a graph of its own."""
        self._groups[name] = self._groups.get(other, other) if other else name
        self.combine_btn.setChecked(len(self.graph_groups()) <= 1 and len(self._plotted) > 1)
        self._rebuild_strips()

    def _on_combine_toggled(self, combined):
        """Combine: every signal in one graph; off again gives each one its own."""
        first = self._plotted[0] if self._plotted else None
        for name in self._plotted:
            self._groups[name] = first if combined else name
        self._rebuild_strips()

    def _signal_menu(self, position):
        """Right-click a plotted signal: choose which graph it is drawn in."""
        item = self.signal_tree.itemAt(position)
        name = item.data(COL_SIGNAL, Qt.UserRole) if item is not None else None
        if not name or name not in self._plotted:
            return
        menu = QMenu(self)
        menu.addAction("Graph of its own", lambda: self.set_graph_group(name))
        for group in self.graph_groups():
            others = [other for other in group if other != name]
            if others:
                label = ", ".join(other.split(".", 1)[1] for other in others[:3])
                menu.addAction(f"Draw together with {label}", lambda o=others[0]: self.set_graph_group(name, o))
        menu.exec_(self.signal_tree.viewport().mapToGlobal(position))

    def _rebuild_strips(self):
        """One PlotItem per graph - a graph holds one signal, or several sharing its axes - stacked with
        linked time axes and the measurement cursors across all of them."""
        if not HAS_PG:
            return
        previous_range = None
        if self._group_plots:
            previous_range = self._group_plots[0][0].getViewBox().viewRange()[0]
        self.graph.clear()
        self._plots.clear()
        self._group_plots.clear()
        self._hover.clear()
        self.graph_stack.setCurrentIndex(1 if self._plotted else 0)
        if not self._plotted:
            return
        theme = self._theme_colors()
        groups = self.graph_groups()
        self.graph.setBackground(theme["background"])
        self.graph.setMinimumHeight(STRIP_MIN_HEIGHT * len(groups) + 30)
        first = None
        last_row = len(groups) - 1
        for row, group in enumerate(groups):
            plot = self.graph.addPlot(row=row, col=0)
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.setDownsampling(auto=True, mode="peak")
            plot.setClipToView(True)
            plot.hideButtons()  # pyqtgraph's hover "A" auto-range button; the Fit button covers it
            plot.getViewBox().setMouseEnabled(x=not self.lock_x_btn.isChecked(), y=not self.lock_y_btn.isChecked())
            plot.getViewBox().sigRangeChangedManually.connect(self._on_manual_range)
            if self._autoscale:
                plot.enableAutoRange(axis="y")
            left = plot.getAxis("left")
            left.setWidth(AXIS_WIDTH)
            colour = self._color(group[0]) if len(group) == 1 else theme["axis"]
            left.setPen(pg.mkPen(colour))
            left.setTextPen(pg.mkPen(colour))
            left.setLabel(self._axis_label(group), color=colour if len(group) == 1 else theme["text"].name())
            bottom = plot.getAxis("bottom")
            bottom.setPen(pg.mkPen(theme["axis"]))
            bottom.setTextPen(pg.mkPen(theme["text"]))
            if row == last_row:
                plot.setLabel("bottom", "Time", units="s", color=theme["text"].name())
            else:
                bottom.setStyle(showValues=False)
                bottom.setHeight(4)
            if len(group) > 1:
                # A shared graph says which curve is which; a single one is named by its axis.
                legend = plot.addLegend(offset=(-8, 8), labelTextColor=theme["text"])
                legend.setParentItem(plot.getViewBox())
            for name in group:
                curve = plot.plot(name=name.split(".", 1)[1] if len(group) > 1 else None,
                                  **_curve_args(self._color(name), self._curve_style))
                self._plots[name] = (plot, curve)
                self._update_curve(name)
            cursors = []
            for index in range(2):
                pen = pg.mkPen(theme["cursor"], width=1)
                pen.setDashPattern(CURSOR_DASH)
                line = pg.InfiniteLine(self._cursor_pos[index], angle=90, movable=True, pen=pen,
                                       label=f"#{index + 1}" if row == 0 else None,
                                       labelOpts={"position": 0.95, "color": theme["cursor"],
                                                  "fill": theme["background"], "movable": False})
                line.setVisible(self.cursors_btn.isChecked())
                line.sigPositionChanged.connect(lambda ln, i=index: self._on_cursor_moved(i, ln.value()))
                plot.addItem(line, ignoreBounds=True)
                cursors.append(line)
            if first is None:
                first = plot
            else:
                plot.setXLink(first)
            self._group_plots.append((plot, cursors[0], cursors[1]))
            self._hover[row] = self._make_hover_items(plot, theme)
        if previous_range is not None:
            first.setXRange(*previous_range, padding=0)
        self._apply_fixed_ranges()
        self._update_cursor_readout()

    def _axis_label(self, group):
        """The left axis of a graph: the signal and its unit, or the units the signals share."""
        if len(group) == 1:
            # "Message.Signal" is labelled with the signal; a system variable keeps its Namespace::Name.
            name = group[0] if "::" in group[0] else group[0].split(".", 1)[-1]
            unit = self._units.get(group[0])
            return f"{name} [{unit}]" if unit else name
        units = sorted({self._units.get(name, "") for name in group} - {""})
        return " / ".join(units) if units else ""

    def _apply_fixed_ranges(self):
        """Show exactly the time and value ranges set in Graph options."""
        if not self._group_plots:
            return
        if self._x_range is not None:
            self._group_plots[0][0].setXRange(*self._x_range, padding=0)
        if self._y_range is not None:
            for plot, *_rest in self._group_plots:
                plot.disableAutoRange(axis="y")
                plot.setYRange(*self._y_range, padding=0)

    def _make_hover_items(self, plot, theme):
        """Dotted crosshair and a bottom-right time/value readout, shown while the mouse is over the graph."""
        pen = pg.mkPen(theme["text"], width=1, style=Qt.DotLine)
        lines = []
        for angle in (90, 0):
            line = pg.InfiniteLine(angle=angle, movable=False, pen=pen)
            line.setAcceptedMouseButtons(Qt.NoButton)  # never steal drags from cursors or panning
            line.setAcceptHoverEvents(False)
            line.setVisible(False)
            plot.addItem(line, ignoreBounds=True)
            lines.append(line)
        background = QColor(theme["background"])
        background.setAlpha(200)
        readout = pg.TextItem("", color=theme["text"], anchor=(1, 1), fill=pg.mkBrush(background))
        view = plot.getViewBox()
        readout.setParentItem(view)  # pixel position in the view, independent of zoom and scrolling
        readout.setZValue(1000)
        readout.setVisible(False)
        place = lambda *_: readout.setPos(view.width() - 4, view.height() - 4)  # noqa: E731
        view.sigResized.connect(place)
        place()
        return lines[0], lines[1], readout

    def _on_mouse_moved(self, scene_pos):
        for row, (plot, *_rest) in enumerate(self._group_plots):
            vertical, horizontal, readout = self._hover[row]
            view = plot.getViewBox()
            inside = view.sceneBoundingRect().contains(scene_pos)
            if inside:
                point = view.mapSceneToView(scene_pos)
                vertical.setValue(point.x())
                horizontal.setValue(point.y())
                units = sorted({self._units.get(name, "") for name in self.graph_groups()[row]} - {""})
                unit = units[0] if len(units) == 1 else ""
                readout.setText(f"{point.x():.3f} s   {point.y():.4g}{' ' + unit if unit else ''}")
            for item in (vertical, horizontal, readout):
                item.setVisible(inside)

    def _hide_hover(self):
        for items in self._hover.values():
            for item in items:
                item.setVisible(False)

    def eventFilter(self, watched, event):
        if watched is self.graph and event.type() == QEvent.Leave:
            self._hide_hover()
        return super().eventFilter(watched, event)

    def _update_curve(self, name):
        entry, series = self._plots.get(name), self._series.get(name)
        if entry is None:
            return
        if series is None or not series.n:
            entry[1].setData([], [])
            return
        entry[1].setData(series.times(), series.values())

    # --- data -----------------------------------------------------------------------------

    def on_can_message(self, arb_id: int, data: bytes | list, timestamp: float | None = None):
        """Called by the main window for every received frame; decode with the DBC and append to the series.

        timestamp is the adapter's (or the recorded one when a file is replayed); without it the frame is
        timed as it arrives here, which includes the delay through the GUI thread.
        """
        decoder = self._decoders.get(arb_id)
        if decoder is None:
            return
        message, names = decoder
        try:
            decoded = message.decode(bytes(data), decode_choices=False, allow_truncated=True)
        except Exception:
            return
        t = self._graph_time(timestamp)
        for signal_name, display_name in names:
            value = decoded.get(signal_name)
            if isinstance(value, (int, float)):
                series = self._series.get(display_name)
                if series is None:
                    series = self._series[display_name] = _Series(self._sample_limit)
                series.append(t, float(value))
                self._new_curve_data.add(display_name)
                self._new_values.add(display_name)

    def _graph_time(self, timestamp):
        """A timestamp as a time on the graph's axis."""
        now = float(timestamp) if timestamp is not None else time.monotonic()
        if self._t0 is None:
            start = self.clock.start if self.clock is not None else None
            # The measurement's start when it is on the same clock as the frame, so a time on the graph is
            # the same number as the Trace's Relative time; the first frame otherwise.
            self._t0 = start if start is not None and timestamp is not None and start <= now else now
        return now - self._t0

    def on_sysvar(self, name: str, value, timestamp=None, unit: str = ""):
        """A system variable changed: it can be plotted like a signal, from the System variables branch.
        Text variables have nothing to plot and are left out."""
        if isinstance(value, str):
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if name not in self._items:
            self._add_sysvar_item(name, unit)
        series = self._series.get(name)
        if series is None:
            series = self._series[name] = _Series(self._sample_limit)
        series.append(self._graph_time(timestamp), number)
        self._new_curve_data.add(name)
        self._new_values.add(name)

    def _add_sysvar_item(self, name, unit):
        branch = next((self.signal_tree.topLevelItem(index) for index in range(self.signal_tree.topLevelItemCount())
                       if self.signal_tree.topLevelItem(index).data(COL_SIGNAL, Qt.UserRole + 1) == "sysvars"), None)
        self.signal_tree.blockSignals(True)
        try:
            if branch is None:
                branch = QTreeWidgetItem(self.signal_tree, ["System variables"])
                branch.setFlags(Qt.ItemIsEnabled)
                branch.setData(COL_SIGNAL, Qt.UserRole + 1, "sysvars")
                branch.setExpanded(True)
            item = QTreeWidgetItem(branch, [name, "", unit])
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            item.setCheckState(COL_SIGNAL, Qt.Unchecked)
            item.setData(COL_SIGNAL, Qt.UserRole, name)
            item.setToolTip(COL_SIGNAL, f"System variable {name}")
            self._items[name] = item
            self._units[name] = unit
        finally:
            self.signal_tree.blockSignals(False)
        self._apply_filter()

    def on_frame(self, timestamp, direction, can_id, data, extended=False):
        """A frame of the measurement: only received ones carry signal values to plot."""
        if direction == "RX":
            self.on_can_message(can_id, data, timestamp)

    def clear_data(self):
        self._series.clear()
        self._new_curve_data.clear()
        self._new_values.clear()
        self._t0 = None
        for item in self._items.values():
            for column in (COL_VALUE, *CURSOR_COLUMNS):
                item.setText(column, "")
        for name in self._plots:
            self._update_curve(name)

    def _latest_time(self):
        return max((s.t[s.n - 1] for s in self._series.values() if s.n), default=None)

    def _redraw(self):
        if self.pause_btn.isChecked():
            return
        self._ticks += 1
        for name in self._new_curve_data & self._plots.keys():
            self._update_curve(name)
        self._new_curve_data.clear()
        if self._ticks % VALUE_REFRESH_TICKS == 0:
            for name in self._new_values:
                item = self._items.get(name)
                if item is not None:
                    item.setText(COL_VALUE, _format(self._series[name].last()))
            self._new_values.clear()
            self._update_cursor_readout()
        if self.follow_btn.isChecked() and self._group_plots:
            latest = self._latest_time()
            if latest is not None:
                first = self._group_plots[0][0]
                first.setXRange(max(0.0, latest - self._window_seconds), max(latest, self._window_seconds),
                                padding=0)

    def _on_pause_toggled(self, paused):
        self.pause_btn.setToolTip("Resume: show what was recorded meanwhile" if paused
                                  else "Pause: freeze the display; recording continues")
        self.pause_btn.setAccessibleName("Resume" if paused else "Pause")
        self._refresh_tool_icons()
        if not paused:  # catch up with what was recorded while paused
            self._new_curve_data.update(self._series)
            self._new_values.update(self._series)

    def _on_manual_range(self, mask=None):
        if mask is None or mask[0]:
            self.follow_btn.setChecked(False)  # the user moved the time axis; stop scrolling

    def _apply_axis_locks(self, *_):
        """Lock X / Lock Y: stop mouse zoom and pan from changing that axis on every graph."""
        lock_x, lock_y = self.lock_x_btn.isChecked(), self.lock_y_btn.isChecked()
        for plot, *_rest in self._group_plots:
            plot.getViewBox().setMouseEnabled(x=not lock_x, y=not lock_y)
            if lock_y and self._autoscale and self._y_range is None:
                plot.enableAutoRange(axis="y")  # back to fitted values after a manual Y zoom

    def fit_all(self):
        self.follow_btn.setChecked(False)
        self._x_range = self._y_range = None      # Fit shows everything, so the fixed ranges are dropped
        for plot, *_ in self._group_plots:
            plot.enableAutoRange(axis="x")
            if self._autoscale:
                plot.enableAutoRange(axis="y")
            plot.autoRange()

    # --- cursors ----------------------------------------------------------------------------

    def _on_cursors_toggled(self, enabled):
        if enabled and self._group_plots:
            start, end = self._group_plots[0][0].getViewBox().viewRange()[0]
            self._cursor_pos = [start + (end - start) / 3, start + 2 * (end - start) / 3]
            self._move_cursor_lines()
            self.follow_btn.setChecked(False)  # measuring needs a still picture
        for _, line_a, line_b in self._group_plots:
            line_a.setVisible(enabled)
            line_b.setVisible(enabled)
        for column in CURSOR_COLUMNS:
            self.signal_tree.setColumnHidden(column, not enabled)
        self.cursor_label.setVisible(enabled)
        self._update_cursor_readout()

    def _move_cursor_lines(self):
        self._syncing_cursors = True
        try:
            for _, line_a, line_b in self._group_plots:
                line_a.setValue(self._cursor_pos[0])
                line_b.setValue(self._cursor_pos[1])
        finally:
            self._syncing_cursors = False

    def _on_cursor_moved(self, index, value):
        if self._syncing_cursors:
            return
        self._cursor_pos[index] = value
        self._move_cursor_lines()
        self._update_cursor_readout()

    def cursor_values(self, name):
        """(value at cursor 1, value at cursor 2) for a signal."""
        series = self._series.get(name)
        if series is None:
            return None, None
        return tuple(series.at(t) for t in self._cursor_pos)

    def cursor_statistics(self, name):
        """{"min", "max", "mean", "std", "count"} of a signal's samples between the cursors, or None."""
        series = self._series.get(name)
        if series is None:
            return None
        start, end = sorted(self._cursor_pos)
        _times, values = series.between(start, end)
        values = [float(value) for value in values]
        if not values:
            return None
        mean = sum(values) / len(values)
        return {"min": min(values), "max": max(values), "mean": mean, "count": len(values),
                "std": math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))}

    def _update_cursor_readout(self):
        if not self.cursors_btn.isChecked():
            return
        t1, t2 = self._cursor_pos
        self.cursor_label.setText(f"Cursor 1: {t1:.3f} s     Cursor 2: {t2:.3f} s     Δt: {t2 - t1:.3f} s")
        for name in self._plotted:
            v1, v2 = self.cursor_values(name)
            item = self._items[name]
            item.setText(COL_C1, _format(v1))
            item.setText(COL_C2, _format(v2))
            item.setText(COL_DELTA, _format(v2 - v1) if v1 is not None and v2 is not None else "")
            stats = self.cursor_statistics(name) or {}
            for column, key in ((COL_MIN, "min"), (COL_MAX, "max"), (COL_MEAN, "mean"), (COL_STD, "std")):
                item.setText(column, _format(stats.get(key)))

    # --- options and export ------------------------------------------------------------------

    def _show_graph_options(self):
        dialog = GraphOptionsDialog(self)
        dialog.style_combo.setCurrentText(self._curve_style)
        dialog.autoscale_cb.setChecked(self._autoscale)
        dialog.window_spin.setValue(self._window_seconds)
        dialog.limit_spin.setValue(self._sample_limit)
        # Start from what is on screen, so a range can be fine-tuned instead of typed from scratch.
        x_range, y_range = self._x_range, self._y_range
        if self._group_plots:
            view = self._group_plots[0][0].getViewBox().viewRange()
            x_range, y_range = x_range or tuple(view[0]), y_range or tuple(view[1])
        for checkbox, values, spins in ((dialog.fixed_x_cb, x_range, (dialog.x_start, dialog.x_end)),
                                        (dialog.fixed_y_cb, y_range, (dialog.y_start, dialog.y_end))):
            for spin, value in zip(spins, values or (0.0, 0.0)):
                spin.setValue(value)
        dialog.fixed_x_cb.setChecked(self._x_range is not None)
        dialog.fixed_y_cb.setChecked(self._y_range is not None)
        if dialog.exec_() != QDialog.Accepted:
            return
        self._curve_style = dialog.style_combo.currentText()
        self._autoscale = dialog.autoscale_cb.isChecked()
        self._window_seconds = dialog.window_spin.value()
        self.set_sample_limit(dialog.limit_spin.value())
        self._x_range, self._y_range = dialog.ranges()
        if self._x_range is not None:
            self.follow_btn.setChecked(False)     # a fixed time range cannot scroll with the data
        if self._y_range is not None:
            self._autoscale = False
        self._rebuild_strips()

    def set_sample_limit(self, limit: int):
        """At most limit samples per signal from now on; signals holding more lose their oldest."""
        self._sample_limit = max(1000, int(limit))
        for series in self._series.values():
            series.set_limit(self._sample_limit)
        self._new_curve_data.update(self._series)

    def dropped_samples(self) -> int:
        return sum(series.dropped for series in self._series.values())

    def visible_range(self):
        """(start, end) of the time axis on screen, or None without a graph."""
        if not self._group_plots:
            return None
        return tuple(self._group_plots[0][0].getViewBox().viewRange()[0])

    def export_range(self, choice: str):
        """The time range an EXPORT_RANGES choice stands for; None for everything."""
        if choice == EXPORT_RANGES[1]:
            return self.visible_range()
        if choice == EXPORT_RANGES[2]:
            return tuple(sorted(self._cursor_pos))
        return None

    def _export_series(self, names, time_range):
        """(name, unit, times, values) for export, cut to the time range."""
        result = []
        for name in names:
            series = self._series.get(name)
            if series is None or not series.n:
                continue
            times, values = series.between(*time_range) if time_range else (series.times(), series.values())
            result.append((name, self._units.get(name, ""), list(times), list(values)))
        return result

    def export(self, path, kind: str = "long_csv", time_range=None, names=None) -> int:
        """Write the recorded data: kind is a value of EXPORT_FORMATS; time_range (start, end) or None for
        everything; names the signals, all decoded ones by default. Returns the signals written."""
        names = list(self._series) if names is None else list(names)
        if kind == "png":
            if self.graph is None or not self._group_plots:
                raise ValueError("There is no graph to take a picture of")
            if not self.graph.grab().save(str(path), "PNG"):
                raise OSError(f"Could not write {path}")
            return len(self._plotted)
        data = self._export_series(names, time_range)
        if kind == "mdf4":
            # The graph's time 0 is _t0; MDF times count from the file's start, so that is the start -
            # when it is a time of day at all (a replayed file may count from its own zero).
            start = self._t0 if self._t0 is not None and self._t0 >= 1e9 else None
            return write_mdf4(path, data, start_time=start, comment="CAN Logger export")
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if kind == "wide_csv":
                # One row per moment any signal changed; each column holds its signal's value at that
                # moment, as the ECU holds it until the next sample (empty before the first).
                writer.writerow(["Time"] + [f"{name} [{unit}]" if unit else name for name, unit, _t, _v in data])
                moments = sorted({t for _name, _unit, times, _values in data for t in times})
                positions = [0] * len(data)
                current = [""] * len(data)
                for moment in moments:
                    for index, (_name, _unit, times, values) in enumerate(data):
                        while positions[index] < len(times) and times[positions[index]] <= moment:
                            current[index] = values[positions[index]]
                            positions[index] += 1
                    writer.writerow([moment] + current)
            else:
                writer.writerow(["Time", "Signal", "Value"])
                for name, _unit, times, values in data:
                    for t, v in zip(times, values):
                        writer.writerow([t, name, v])
        return len(data)

    def save_csv(self, path):
        """Everything decoded, one row per sample (kept for scripts and older callers)."""
        return self.export(path, "long_csv")

    def show_export(self):
        dialog = ExportDialog(self)
        if dialog.exec_() != QDialog.Accepted:
            return None
        kind = dialog.kind()
        suffix = {"mdf4": "MDF 4 files (*.mf4)", "png": "PNG images (*.png)"}.get(kind, "CSV files (*.csv)")
        path, _ = QFileDialog.getSaveFileName(self, "Export", "", f"{suffix};;All files (*.*)")
        if not path:
            return None
        names = self._plotted if dialog.plotted_only_cb.isChecked() else None
        try:
            written = self.export(path, kind, self.export_range(dialog.range_combo.currentText()), names)
        except (OSError, ValueError) as exc:
            self.path_status.setText(f"Export failed: {exc}")
            return None
        self.path_status.setText(f"Exported {written} signal(s) to {Path(path).name}")
        return path
