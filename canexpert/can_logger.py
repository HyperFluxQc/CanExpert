"""
CAN Logger: a CANoe-style graphics window. Load a DBC, tick signals in the list and each one
gets its own strip chart; all strips share one time axis. Measurement cursors, follow/pause,
fit, exact time and value ranges, line or dot drawing, and CSV export of everything received.
"""
import bisect
import csv
import time
from pathlib import Path

from PyQt5.QtCore import QEvent, QSize, Qt, QTimer
from PyQt5.QtGui import QColor, QIcon, QPalette, QPixmap
from PyQt5.QtWidgets import (
    QCheckBox,
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
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from canexpert.paths import DBC_DIR
from canexpert.ui_common import SplitterPanel, enable_maximize, is_dark_theme, line_icon

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
}

CURVE_STYLES = ("Line", "Line + dots", "Dots")
CURSOR_DASH = [30, 10]      # dash and gap of the measurement cursors, in pixels

STRIP_MIN_HEIGHT = 110      # px per signal graph; more strips than fit make the graph area scroll
REDRAW_INTERVAL_MS = 50     # curves; the value column refreshes every VALUE_REFRESH_TICKS redraws
VALUE_REFRESH_TICKS = 4
AXIS_WIDTH = 64             # fixed left-axis width keeps all strips' time axes aligned
COL_SIGNAL, COL_VALUE, COL_UNIT, COL_C1, COL_C2, COL_DELTA = range(6)


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


class _Series:
    """Growable (time, value) storage; the views handed to pyqtgraph stay valid while appending."""
    __slots__ = ("t", "v", "n")

    def __init__(self):
        self.n = 0
        if np is not None:
            self.t, self.v = np.empty(1024), np.empty(1024)
        else:
            self.t, self.v = [], []

    def append(self, t: float, v: float):
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


class CANLoggerWindow(QDialog):
    """CANoe-style graphics window: tick DBC signals to add one strip chart per signal."""

    def __init__(self, parent=None, symbols=None):
        super().__init__(parent)
        self.setWindowTitle("CAN Logger")
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
        self._plots = {}            # "Message.Signal" -> (PlotItem, curve, cursor 1, cursor 2)
        self._hover = {}            # "Message.Signal" -> (dotted vertical line, dotted horizontal line, readout)
        self._new_curve_data = set()     # signals whose graph needs redrawing
        self._new_values = set()         # signals whose Value column needs refreshing
        self._t0 = None
        self._curve_style = CURVE_STYLES[0]
        self._x_range = None        # fixed time range from Graph options, else None
        self._y_range = None        # fixed value range for every graph, else None
        self._autoscale = True
        self._window_seconds = 10.0
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
        return button

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

    def _build_ui(self):
        layout = QVBoxLayout(self)

        bar = QHBoxLayout()
        self.load_btn = QPushButton("Load DBC...")
        self.load_btn.clicked.connect(self._load_dbc)
        bar.addWidget(self.load_btn)
        save_btn = QPushButton("Save CSV...")
        save_btn.clicked.connect(self._save_csv)
        bar.addWidget(save_btn)
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
        self.signal_tree.setHeaderLabels(["Signal", "Value", "Unit", "Cursor 1", "Cursor 2", "Δ"])
        self.signal_tree.setColumnWidth(COL_SIGNAL, 230)
        self.signal_tree.setColumnWidth(COL_VALUE, 80)
        self.signal_tree.setColumnWidth(COL_UNIT, 50)
        for column in (COL_C1, COL_C2, COL_DELTA):
            self.signal_tree.setColumnWidth(column, 70)
            self.signal_tree.setColumnHidden(column, True)
        self.signal_tree.itemChanged.connect(self._on_item_changed)
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
            item.setIcon(COL_SIGNAL, self._swatch(self._color(name)))
        elif not checked and name in self._plotted:
            self._plotted.remove(name)
            del self._colors[name]
            item.setIcon(COL_SIGNAL, QIcon())
            for column_index in (COL_C1, COL_C2, COL_DELTA):
                item.setText(column_index, "")
        else:
            return
        self._rebuild_strips()
        self._apply_filter()

    def set_signal_plotted(self, display_name: str, plotted: bool = True):
        """Tick or untick a signal in the list (adds or removes its graph)."""
        self._items[display_name].setCheckState(COL_SIGNAL, Qt.Checked if plotted else Qt.Unchecked)

    def _rebuild_strips(self):
        """One PlotItem per plotted signal, stacked, with linked time axes and synced cursors."""
        if not HAS_PG:
            return
        previous_range = None
        if self._plots:
            previous_range = next(iter(self._plots.values()))[0].getViewBox().viewRange()[0]
        self.graph.clear()
        self._plots.clear()
        self._hover.clear()
        self.graph_stack.setCurrentIndex(1 if self._plotted else 0)
        if not self._plotted:
            return
        theme = self._theme_colors()
        self.graph.setBackground(theme["background"])
        self.graph.setMinimumHeight(STRIP_MIN_HEIGHT * len(self._plotted) + 30)
        first = None
        last_row = len(self._plotted) - 1
        for row, name in enumerate(self._plotted):
            color = self._color(name)
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
            left.setPen(pg.mkPen(color))
            left.setTextPen(pg.mkPen(color))
            signal_name = name.split(".", 1)[1]
            unit = self._units.get(name)
            left.setLabel(f"{signal_name} [{unit}]" if unit else signal_name, color=color)
            bottom = plot.getAxis("bottom")
            bottom.setPen(pg.mkPen(theme["axis"]))
            bottom.setTextPen(pg.mkPen(theme["text"]))
            if row == last_row:
                plot.setLabel("bottom", "Time", units="s", color=theme["text"].name())
            else:
                bottom.setStyle(showValues=False)
                bottom.setHeight(4)
            curve = plot.plot(**_curve_args(color, self._curve_style))
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
            self._plots[name] = (plot, curve, cursors[0], cursors[1])
            self._hover[name] = self._make_hover_items(plot, theme)
            self._update_curve(name)
        if previous_range is not None:
            first.setXRange(*previous_range, padding=0)
        self._apply_fixed_ranges()
        self._update_cursor_readout()

    def _apply_fixed_ranges(self):
        """Show exactly the time and value ranges set in Graph options."""
        if not self._plots:
            return
        if self._x_range is not None:
            next(iter(self._plots.values()))[0].setXRange(*self._x_range, padding=0)
        if self._y_range is not None:
            for plot, *_rest in self._plots.values():
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
        for name, (plot, *_rest) in self._plots.items():
            vertical, horizontal, readout = self._hover[name]
            view = plot.getViewBox()
            inside = view.sceneBoundingRect().contains(scene_pos)
            if inside:
                point = view.mapSceneToView(scene_pos)
                vertical.setValue(point.x())
                horizontal.setValue(point.y())
                unit = self._units.get(name)
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
        now = float(timestamp) if timestamp is not None else time.monotonic()
        if self._t0 is None:
            self._t0 = now
        t = now - self._t0
        for signal_name, display_name in names:
            value = decoded.get(signal_name)
            if isinstance(value, (int, float)):
                series = self._series.get(display_name)
                if series is None:
                    series = self._series[display_name] = _Series()
                series.append(t, float(value))
                self._new_curve_data.add(display_name)
                self._new_values.add(display_name)

    def clear_data(self):
        self._series.clear()
        self._new_curve_data.clear()
        self._new_values.clear()
        self._t0 = None
        for item in self._items.values():
            for column in (COL_VALUE, COL_C1, COL_C2, COL_DELTA):
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
        if self.follow_btn.isChecked() and self._plots:
            latest = self._latest_time()
            if latest is not None:
                first = next(iter(self._plots.values()))[0]
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
        for plot, *_rest in self._plots.values():
            plot.getViewBox().setMouseEnabled(x=not lock_x, y=not lock_y)
            if lock_y and self._autoscale and self._y_range is None:
                plot.enableAutoRange(axis="y")  # back to fitted values after a manual Y zoom

    def fit_all(self):
        self.follow_btn.setChecked(False)
        self._x_range = self._y_range = None      # Fit shows everything, so the fixed ranges are dropped
        for plot, *_ in self._plots.values():
            plot.enableAutoRange(axis="x")
            if self._autoscale:
                plot.enableAutoRange(axis="y")
            plot.autoRange()

    # --- cursors ----------------------------------------------------------------------------

    def _on_cursors_toggled(self, enabled):
        if enabled and self._plots:
            start, end = next(iter(self._plots.values()))[0].getViewBox().viewRange()[0]
            self._cursor_pos = [start + (end - start) / 3, start + 2 * (end - start) / 3]
            self._move_cursor_lines()
            self.follow_btn.setChecked(False)  # measuring needs a still picture
        for _, _, line_a, line_b in self._plots.values():
            line_a.setVisible(enabled)
            line_b.setVisible(enabled)
        for column in (COL_C1, COL_C2, COL_DELTA):
            self.signal_tree.setColumnHidden(column, not enabled)
        self.cursor_label.setVisible(enabled)
        self._update_cursor_readout()

    def _move_cursor_lines(self):
        self._syncing_cursors = True
        try:
            for _, _, line_a, line_b in self._plots.values():
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

    # --- options and export ------------------------------------------------------------------

    def _show_graph_options(self):
        dialog = GraphOptionsDialog(self)
        dialog.style_combo.setCurrentText(self._curve_style)
        dialog.autoscale_cb.setChecked(self._autoscale)
        dialog.window_spin.setValue(self._window_seconds)
        # Start from what is on screen, so a range can be fine-tuned instead of typed from scratch.
        x_range, y_range = self._x_range, self._y_range
        if self._plots:
            view = next(iter(self._plots.values()))[0].getViewBox().viewRange()
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
        self._x_range, self._y_range = dialog.ranges()
        if self._x_range is not None:
            self.follow_btn.setChecked(False)     # a fixed time range cannot scroll with the data
        if self._y_range is not None:
            self._autoscale = False
        self._rebuild_strips()

    def _save_csv(self):
        if not self._series:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV", "", "CSV files (*.csv);;All files (*.*)",
        )
        if path:
            self.save_csv(path)

    def save_csv(self, path):
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Time", "Signal", "Value"])
            for display_name, series in self._series.items():
                for t, v in series.points():
                    w.writerow([t, display_name, v])
