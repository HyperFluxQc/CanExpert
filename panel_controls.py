"""
Panel controls shared by the Form Designer preview and the running panel.

Each control kind has one registry entry: palette label and category, default size, editable
properties, and how it is built, shows values and reports user input. Painted controls (gauge,
LED, multi-state indicator, toggle switch, knob, trend...) are defined here as well.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PyQt5.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPixmap, QRadialGradient
from PyQt5.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDial,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLCDNumber,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

try:
    import pyqtgraph as pg
except ImportError:
    pg = None

ACCENT = "#0f6cbd"  # Windows 11 accent blue
STATE_COLORS = ["#5f6368", "#2e7d32", "#f9a825", "#c62828", "#1565c0", "#6a1b9a", "#00838f", "#ef6c00"]


# -----------------------------------------------------------------------------
# Property helpers (values arrive as Python types from the designer, strings from XML)
# -----------------------------------------------------------------------------

def num(data, key, default=0.0):
    try:
        return float(data.get(key, default))
    except (TypeError, ValueError):
        return default


def optional_num(data, key):
    value = data.get(key, "")
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer(data, key, default=0):
    return int(round(num(data, key, default)))


def flag(data, key, default=False):
    value = data.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def color(data, key, default):
    value = QColor(str(data.get(key) or default))
    return value if value.isValid() else QColor(default)


def item_list(data, key="items"):
    return [s.strip() for s in str(data.get(key, "") or "").split(",") if s.strip()]


def parse_states(text):
    """'0=Off:#5f6368; 1=On:#2e7d32' -> [(value, label, QColor)]; missing colours come from STATE_COLORS."""
    states = []
    for index, part in enumerate(p.strip() for p in str(text or "").replace("\n", ";").split(";")):
        if not part or "=" not in part:
            continue
        value, rest = part.split("=", 1)
        label, _, colour = rest.partition(":")
        qcolor = QColor(colour.strip()) if colour.strip() else QColor(STATE_COLORS[index % len(STATE_COLORS)])
        states.append((value.strip(), label.strip(), qcolor if qcolor.isValid() else QColor(STATE_COLORS[0])))
    return states


def states_from_choices(choices):
    """DBC value table -> states text for a multi-state indicator."""
    return "; ".join(f"{int(value)}={label}:{STATE_COLORS[(index + 1) % len(STATE_COLORS)] if index else STATE_COLORS[0]}"
                     for index, (value, label) in enumerate(sorted(choices.items())))


def _same_value(state_value, value):
    try:
        return float(state_value) == float(value)
    except (TypeError, ValueError):
        return str(state_value).strip().lower() == str(value).strip().lower()


def format_value(value, data):
    """Value text for displays: DBC value-table text, number format, decimals and unit."""
    if value is None:
        return ""
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    choices = data.get("_choices")
    if numeric and choices and flag(data, "value_table", True):
        for key, label in choices.items():
            if _same_value(key, value):
                return str(label)
    if not numeric:
        return str(value)
    fmt = str(data.get("format", "auto") or "auto")
    decimals = optional_num(data, "decimals")
    if fmt == "hex":
        text = f"0x{int(value):X}"
    elif fmt == "binary":
        text = f"0b{int(value):b}"
    elif decimals is not None:
        text = f"{float(value):.{max(0, int(decimals))}f}"
    elif isinstance(value, float) and not value.is_integer():
        text = f"{value:.6g}"
    else:
        text = str(int(value)) if isinstance(value, float) else str(value)
    unit = str(data.get("unit", "") or "")
    return f"{text} {unit}" if unit and fmt not in ("hex", "binary") else text


def apply_appearance(widget, data, interactive):
    """Font, colours, tooltip and read-only state shared by every control."""
    font = QFont(widget.font())
    size = num(data, "font_size", 0)
    if size > 0:
        font.setPointSizeF(size)
    font.setBold(flag(data, "bold"))
    widget.setFont(font)
    if data.get("tooltip"):
        widget.setToolTip(str(data["tooltip"]))
    palette = widget.palette()
    text_colour = QColor(str(data.get("text_color") or ""))
    if data.get("text_color") and text_colour.isValid():
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
            palette.setColor(role, text_colour)
    background = QColor(str(data.get("background") or ""))
    if data.get("background") and background.isValid():
        for role in (QPalette.Window, QPalette.Base, QPalette.Button):
            palette.setColor(role, background)
        widget.setAutoFillBackground(True)
    widget.setPalette(palette)
    if interactive and flag(data, "read_only"):
        widget.setEnabled(False)


# -----------------------------------------------------------------------------
# Painted widgets
# -----------------------------------------------------------------------------

class GaugeWidget(QWidget):
    """Round dial: 240-degree scale with ticks, value arc, needle, value text and optional
    warning (amber) and critical (red) thresholds."""

    def __init__(self, minimum=0.0, maximum=100.0, unit="", decimals=None, warning=None, critical=None,
                 title="", parent=None):
        super().__init__(parent)
        self._min, self._max = float(minimum), float(maximum) if maximum > minimum else float(minimum) + 1
        self._value = self._min
        self.unit, self.decimals, self.warning, self.critical, self.title = unit, decimals, warning, critical, title
        self.setMinimumSize(60, 60)

    def value(self):
        return self._value

    def setValue(self, value):
        self._value = float(value)
        self.update()

    def minimum(self):
        return self._min

    def maximum(self):
        return self._max

    def _angle(self, value):
        fraction = (min(max(value, self._min), self._max) - self._min) / (self._max - self._min)
        return 225.0 - 270.0 * fraction  # degrees, counter-clockwise from 3 o'clock

    def _zone_colour(self, value):
        if self.critical is not None and value >= self.critical:
            return QColor("#c62828")
        if self.warning is not None and value >= self.warning:
            return QColor("#f9a825")
        return QColor(ACCENT)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        side = min(self.width(), self.height()) - 8
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        text = self.palette().color(QPalette.WindowText)
        track = QColor(text)
        track.setAlpha(45)
        thickness = max(4.0, side * 0.08)
        arc = rect.adjusted(thickness, thickness, -thickness, -thickness)
        painter.setPen(QPen(track, thickness, Qt.SolidLine, Qt.FlatCap))
        painter.drawArc(arc, int(-45 * 16), int(270 * 16))
        for threshold, colour in ((self.warning, "#f9a825"), (self.critical, "#c62828")):
            if threshold is not None and self._min < threshold < self._max:
                start = self._angle(threshold)
                end = self._angle(self.critical if colour == "#f9a825" and self.critical is not None else self._max)
                zone = QColor(colour)
                zone.setAlpha(90)
                painter.setPen(QPen(zone, thickness, Qt.SolidLine, Qt.FlatCap))
                painter.drawArc(arc, int(end * 16), int((start - end) * 16))
        value_angle = self._angle(self._value)
        painter.setPen(QPen(self._zone_colour(self._value), thickness, Qt.SolidLine, Qt.FlatCap))
        painter.drawArc(arc, int(value_angle * 16), int((225 - value_angle) * 16))
        centre = rect.center()
        radius = arc.width() / 2
        tick_font = QFont(self.font())
        tick_font.setPixelSize(max(8, int(side * 0.075)))
        painter.setFont(tick_font)
        for step in range(6):
            fraction = step / 5
            angle = math.radians(225 - 270 * fraction)
            inner, outer = radius - thickness * 1.2, radius - thickness * 0.55
            painter.setPen(QPen(text, 1.2))
            painter.drawLine(QPointF(centre.x() + inner * math.cos(angle), centre.y() - inner * math.sin(angle)),
                             QPointF(centre.x() + outer * math.cos(angle), centre.y() - outer * math.sin(angle)))
            if side < 120:
                continue  # too small for scale numbers; the ticks and value text are enough
            label_radius = radius - thickness * 2.4
            label = f"{self._min + (self._max - self._min) * fraction:g}"
            label_rect = QRectF(centre.x() + label_radius * math.cos(angle) - side / 6,
                                centre.y() - label_radius * math.sin(angle) - side / 18, side / 3, side / 9)
            painter.drawText(label_rect, Qt.AlignCenter, label)
        needle = math.radians(value_angle)
        length = radius - thickness * 1.4
        painter.setPen(QPen(text, max(1.5, side / 90), Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(centre, QPointF(centre.x() + length * math.cos(needle), centre.y() - length * math.sin(needle)))
        painter.setBrush(text)
        painter.drawEllipse(centre, side / 40 + 2, side / 40 + 2)
        # Value and title sit in the open bottom of the dial, below the min/max scale labels.
        value_font = QFont(self.font())
        value_font.setPixelSize(max(9, int(side * 0.11)))
        value_font.setBold(True)
        painter.setFont(value_font)
        painter.setPen(text)
        shown = format_value(self._value, {"decimals": self.decimals if self.decimals is not None else "",
                                           "unit": self.unit})
        painter.drawText(QRectF(rect.left(), centre.y() + side * 0.24, rect.width(), side * 0.13), Qt.AlignCenter, shown)
        if self.title:
            painter.setFont(tick_font)
            painter.drawText(QRectF(rect.left(), centre.y() + side * 0.37, rect.width(), side * 0.12), Qt.AlignCenter,
                             self.title)


class LedWidget(QWidget):
    """Round lamp with optional text beside it; can blink while on."""

    def __init__(self, on_colour, off_colour, on_text="ON", off_text="OFF", blink=False, parent=None):
        super().__init__(parent)
        self.on_colour, self.off_colour = QColor(on_colour), QColor(off_colour)
        self.on_text, self.off_text, self.blink = on_text, off_text, blink
        self._on = False
        self._lit = True
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._toggle_blink)

    def _toggle_blink(self):
        self._lit = not self._lit
        self.update()

    def is_on(self):
        return self._on

    def set_on(self, on):
        self._on = bool(on)
        self._lit = True
        if self._on and self.blink:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def text(self):
        return self.on_text if self._on else self.off_text

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        diameter = max(8, min(self.height(), self.width()) - 6)
        text = self.text()
        x = 3 if text else (self.width() - diameter) / 2
        lamp = QRectF(x, (self.height() - diameter) / 2, diameter, diameter)
        lit = self._on and self._lit
        base = self.on_colour if lit else self.off_colour
        gradient = QRadialGradient(lamp.center() - QPointF(diameter * 0.18, diameter * 0.18), diameter * 0.7)
        gradient.setColorAt(0, base.lighter(170 if lit else 125))
        gradient.setColorAt(1, base.darker(115))
        painter.setPen(QPen(base.darker(160), 1))
        painter.setBrush(gradient)
        painter.drawEllipse(lamp)
        if text:
            painter.setPen(self.palette().color(QPalette.WindowText))
            painter.drawText(QRectF(lamp.right() + 6, 0, self.width() - lamp.right() - 6, self.height()),
                             Qt.AlignVCenter | Qt.AlignLeft, text)


class StateIndicator(QWidget):
    """Multi-state indicator: a rounded tile whose text and colour follow the value (CANoe Switch/Indicator)."""

    def __init__(self, states, parent=None):
        super().__init__(parent)
        self.states = states or [("0", "Off", QColor(STATE_COLORS[0])), ("1", "On", QColor(STATE_COLORS[1]))]
        self._index = 0
        self._unknown = None

    def set_state(self, value):
        for index, (state_value, _, _) in enumerate(self.states):
            if _same_value(state_value, value):
                self._index, self._unknown = index, None
                break
        else:
            self._unknown = str(value)
        self.update()

    def value(self):
        return self.states[self._index][0] if self._unknown is None else self._unknown

    def text(self):
        return self.states[self._index][1] if self._unknown is None else self._unknown

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        fill = QColor("#9e9e9e") if self._unknown is not None else self.states[self._index][2]
        painter.setPen(QPen(fill.darker(130), 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        luminance = 0.299 * fill.red() + 0.587 * fill.green() + 0.114 * fill.blue()
        painter.setPen(QColor("#000000") if luminance > 160 else QColor("#ffffff"))
        painter.drawText(self.rect(), Qt.AlignCenter, self.text())


class ToggleSwitch(QAbstractButton):
    """Windows 11-style on/off switch with its label to the right."""

    def __init__(self, text="", on_colour=ACCENT, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setText(text)
        self.on_colour = QColor(on_colour)
        self.setCursor(Qt.PointingHandCursor)

    def sizeHint(self):
        return QSize(60 + self.fontMetrics().horizontalAdvance(self.text()), 24)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        text = self.palette().color(QPalette.WindowText)
        height = min(20.0, self.height() - 4.0)
        track = QRectF(2, (self.height() - height) / 2, height * 2, height)
        if self.isChecked():
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.on_colour if self.isEnabled() else QColor("#9e9e9e"))
        else:
            painter.setPen(QPen(text, 1))
            painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(track, height / 2, height / 2)
        knob = height * (0.6 if self.isChecked() else 0.5)
        knob_x = track.right() - height / 2 if self.isChecked() else track.left() + height / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#ffffff") if self.isChecked() else text)
        painter.drawEllipse(QPointF(knob_x, track.center().y()), knob / 2, knob / 2)
        painter.setPen(text)
        painter.drawText(QRectF(track.right() + 8, 0, self.width() - track.right() - 8, self.height()),
                         Qt.AlignVCenter | Qt.AlignLeft, self.text())


class KnobWidget(QWidget):
    """Rotary knob with its value shown underneath."""
    valueChanged = pyqtSignal(int)

    def __init__(self, minimum, maximum, unit="", parent=None):
        super().__init__(parent)
        self.unit = unit
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.dial = QDial()
        self.dial.setRange(minimum, maximum)
        self.dial.setNotchesVisible(True)
        self.dial.setWrapping(False)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.dial, 1)
        layout.addWidget(self.label)
        self.dial.valueChanged.connect(self._changed)
        self._changed(self.dial.value())

    def _changed(self, value):
        self.label.setText(f"{value} {self.unit}".strip())
        self.valueChanged.emit(value)

    def value(self):
        return self.dial.value()

    def setValue(self, value):
        self.dial.setValue(int(round(float(value))))


class SevenSegmentDisplay(QFrame):
    """Large 7-segment numeric display with its unit, on a dark panel."""

    def __init__(self, digits, segment_colour, unit="", parent=None):
        super().__init__(parent)
        self.setObjectName("sevenSegment")
        self.setStyleSheet("#sevenSegment { background: #101418; border-radius: 6px; }")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        self.lcd = QLCDNumber(max(1, digits))
        self.lcd.setSegmentStyle(QLCDNumber.Flat)
        self.lcd.setFrameShape(QFrame.NoFrame)
        palette = self.lcd.palette()
        palette.setColor(QPalette.WindowText, QColor(segment_colour))
        self.lcd.setPalette(palette)
        layout.addWidget(self.lcd, 1)
        self.unit_label = QLabel(unit)
        self.unit_label.setStyleSheet(f"color: {QColor(segment_colour).name()}; background: transparent;")
        self.unit_label.setVisible(bool(unit))
        layout.addWidget(self.unit_label, 0, Qt.AlignBottom)
        self._text = ""

    def show_text(self, text):
        self._text = text
        self.lcd.display(text)

    def text(self):
        return self._text


class RadioGroup(QWidget):
    """Radio buttons from a list of items; reports the selected item's text."""
    selected = pyqtSignal(str)

    def __init__(self, items, vertical, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.group = QButtonGroup(self)
        self.buttons = []
        for index, text in enumerate(items or ["Option 1", "Option 2"]):
            button = QRadioButton(text)
            self.group.addButton(button, index)
            layout.addWidget(button)
            self.buttons.append(button)
        layout.addStretch()
        self.group.buttonToggled.connect(lambda button, checked: checked and self.selected.emit(button.text()))

    def value(self):
        button = self.group.checkedButton()
        return button.text() if button else ""

    def select(self, value):
        for index, button in enumerate(self.buttons):
            if button.text() == str(value) or (str(value).lstrip("-").isdigit() and int(value) == index):
                button.setChecked(True)
                return


class TrendWidget(QWidget):
    """Small scrolling graph of one value (the last N seconds)."""

    def __init__(self, window_seconds, colour, minimum=None, maximum=None, unit="", parent=None):
        super().__init__(parent)
        self.window = max(1.0, window_seconds)
        self.points = deque()
        self._last = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if pg is None:
            self.plot = None
            self.fallback = QLabel("Install pyqtgraph for trends")
            layout.addWidget(self.fallback)
            return
        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.setMouseEnabled(False, False)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setBackground(self.palette().color(QPalette.Base))
        text = self.palette().color(QPalette.Text)
        for side in ("left", "bottom"):
            axis = self.plot.getAxis(side)
            axis.setPen(pg.mkPen(text))
            axis.setTextPen(pg.mkPen(text))
        if unit:
            self.plot.setLabel("left", unit)
        if minimum is not None and maximum is not None and maximum > minimum:
            self.plot.setYRange(minimum, maximum, padding=0)
        self.plot.setXRange(-self.window, 0, padding=0)
        self.curve = self.plot.plot(pen=pg.mkPen(QColor(colour), width=1.5), stepMode="right")
        layout.addWidget(self.plot)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._redraw)
        self._timer.start()

    def append(self, value, when=None):
        self._last = float(value)
        self.points.append((time.monotonic() if when is None else when, self._last))

    def value(self):
        return self._last

    def _redraw(self):
        now = time.monotonic()
        while self.points and self.points[0][0] < now - self.window - 1:
            self.points.popleft()
        if self.points:
            # The extra point holds the last value up to "now", so the line reaches the right edge.
            self.curve.setData([t - now for t, _ in self.points] + [0.0],
                               [v for _, v in self.points] + [self.points[-1][1]])


class PictureWidget(QLabel):
    """Image scaled to the control while keeping its aspect ratio."""

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self._pixmap = QPixmap()
        self.set_path(path)

    def set_path(self, path):
        self._pixmap = QPixmap(str(path)) if path else QPixmap()
        if self._pixmap.isNull():
            self.setText("No image" if not path else f"Image not found:\n{Path(str(path)).name}")
            self.setStyleSheet("QLabel { border: 1px dashed palette(mid); color: palette(mid); }")
        else:
            self.setStyleSheet("")
            self._rescale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if not self._pixmap.isNull():
            self.setPixmap(self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class OutputBox(QPlainTextEdit):
    """Read-only text area the script writes lines to (like CANoe's CAPL output view)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(1000)

    def text(self):
        return self.toPlainText()


# -----------------------------------------------------------------------------
# Registry
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Prop:
    key: str
    label: str
    editor: str = "str"          # str, int, float, optional_float, bool, choice, color, states, file
    default: object = ""
    options: tuple = ()
    minimum: float = -1e9
    maximum: float = 1e9


APPEARANCE = (
    Prop("text_color", "Text colour", "color"),
    Prop("background", "Background", "color"),
    Prop("font_size", "Font size (pt, 0 = default)", "float", 0, minimum=0, maximum=96),
    Prop("bold", "Bold", "bool", False),
    Prop("tooltip", "Tooltip"),
)
READ_ONLY = Prop("read_only", "Read-only", "bool", False)
NUMBER_FORMAT = (Prop("format", "Number format", "choice", "auto", ("auto", "decimal", "hex", "binary")),
                 Prop("decimals", "Decimals (blank = auto)", "optional_float", ""))


class Control:
    kind = ""
    label = ""
    category = "Display"         # Input, Display or Decoration (palette sections)
    group = ""                   # list name in parsed databases, e.g. "buttons"
    size = (100, 30)
    uses_label = True
    interactive = False
    in_palette = True
    event = "changed"            # handler name suffix: on_<name>_<event>
    props = ()

    def defaults(self):
        values = {prop.key: prop.default for prop in self.props}
        values.update(width=self.size[0], height=self.size[1])
        return values

    def create(self, data, ctx):
        raise NotImplementedError

    def connect(self, widget, emit):
        """Call emit(value) when the user changes the control."""

    def set_value(self, widget, data, value):
        widget.setText(format_value(value, data))

    def get_value(self, widget):
        for method in ("isChecked", "value", "currentText", "text"):
            if hasattr(widget, method):
                return getattr(widget, method)()
        return None

    def preview(self, widget, data):
        """Sample value shown in the designer."""


def _range(data, default_max=100):
    minimum, maximum = num(data, "min", 0), num(data, "max", default_max)
    return (minimum, maximum) if maximum > minimum else (minimum, minimum + 1)


class Button(Control):
    kind, label, category, group, interactive, event = "button", "Button", "Input", "buttons", True, "clicked"
    size = (100, 32)

    def create(self, data, ctx):
        return QPushButton(str(data.get("label", "Button")))

    def connect(self, widget, emit):
        widget.clicked.connect(lambda checked=False: emit(True))


class Switch(Control):
    kind, label, category, group, interactive = "switch", "Toggle Switch", "Input", "switches", True
    size = (140, 28)
    props = (Prop("on_color", "On colour", "color", ACCENT),)

    def create(self, data, ctx):
        return ToggleSwitch(str(data.get("label", "")), color(data, "on_color", ACCENT))

    def connect(self, widget, emit):
        widget.toggled.connect(emit)

    def set_value(self, widget, data, value):
        widget.setChecked(bool(value))

    def preview(self, widget, data):
        widget.setChecked(True)


class CheckBox(Control):
    kind, label, category, group, interactive = "checkbox", "Checkbox", "Input", "checkboxes", True

    def create(self, data, ctx):
        return QCheckBox(str(data.get("label", "")))

    def connect(self, widget, emit):
        widget.toggled.connect(emit)

    def set_value(self, widget, data, value):
        widget.setChecked(bool(value))


class Radio(Control):
    kind, label, category, group, interactive = "radio", "Radio Buttons", "Input", "radios", True
    size = (220, 30)
    uses_label = False
    props = (Prop("items", "Items (comma-separated)", "str", "Option 1, Option 2, Option 3"),
             Prop("orientation", "Orientation", "choice", "horizontal", ("horizontal", "vertical")))

    def create(self, data, ctx):
        return RadioGroup(item_list(data), data.get("orientation") == "vertical")

    def connect(self, widget, emit):
        widget.selected.connect(emit)

    def set_value(self, widget, data, value):
        widget.select(value)

    def get_value(self, widget):
        return widget.value()


class Combo(Control):
    kind, label, category, group, interactive = "combo", "Combo Box", "Input", "combos", True
    props = (Prop("items", "Items (comma-separated)", "str", "One, Two, Three"),)

    def create(self, data, ctx):
        widget = QComboBox()
        widget.addItems(item_list(data))
        return widget

    def connect(self, widget, emit):
        widget.currentTextChanged.connect(emit)

    def set_value(self, widget, data, value):
        widget.setCurrentText(format_value(value, dict(data, unit="")))


class Slider(Control):
    kind, label, category, group, interactive = "slider", "Slider", "Input", "sliders", True
    size = (160, 30)
    props = (Prop("min", "Min", "float", 0), Prop("max", "Max", "float", 100),
             Prop("orientation", "Orientation", "choice", "horizontal", ("horizontal", "vertical")))

    def create(self, data, ctx):
        widget = QSlider(Qt.Vertical if data.get("orientation") == "vertical" else Qt.Horizontal)
        minimum, maximum = _range(data)
        widget.setRange(int(minimum), int(maximum))
        return widget

    def connect(self, widget, emit):
        widget.valueChanged.connect(emit)

    def set_value(self, widget, data, value):
        widget.setValue(int(round(float(value))))

    def preview(self, widget, data):
        widget.setValue(int((widget.minimum() + widget.maximum()) / 2))


class Knob(Control):
    kind, label, category, group, interactive = "knob", "Knob", "Input", "knobs", True
    size = (90, 100)
    uses_label = False
    props = (Prop("min", "Min", "float", 0), Prop("max", "Max", "float", 100), Prop("unit", "Unit"))

    def create(self, data, ctx):
        minimum, maximum = _range(data)
        return KnobWidget(int(minimum), int(maximum), str(data.get("unit", "") or ""))

    def connect(self, widget, emit):
        widget.valueChanged.connect(emit)

    def set_value(self, widget, data, value):
        widget.setValue(value)

    def preview(self, widget, data):
        widget.setValue((widget.dial.minimum() + widget.dial.maximum()) / 3)


class Spin(Control):
    kind, label, category, group, interactive = "spin", "Numeric Up/Down", "Input", "spins", True
    size = (110, 28)
    uses_label = False
    props = (Prop("min", "Min", "float", 0), Prop("max", "Max", "float", 100), Prop("step", "Step", "float", 1),
             Prop("decimals", "Decimals", "int", 0, minimum=0, maximum=6), Prop("unit", "Unit"))

    def create(self, data, ctx):
        widget = QDoubleSpinBox()
        widget.setDecimals(integer(data, "decimals", 0))
        minimum, maximum = _range(data)
        widget.setRange(minimum, maximum)
        widget.setSingleStep(num(data, "step", 1) or 1)
        widget.setKeyboardTracking(False)  # report committed values, not every keystroke
        if data.get("unit"):
            widget.setSuffix(f" {data['unit']}")
        widget.setProperty("_integer", integer(data, "decimals", 0) == 0)
        return widget

    def connect(self, widget, emit):
        widget.valueChanged.connect(lambda value: emit(int(round(value)) if widget.property("_integer") else value))

    def set_value(self, widget, data, value):
        widget.setValue(float(value))

    def get_value(self, widget):
        return int(round(widget.value())) if widget.property("_integer") else widget.value()


class IoBox(Control):
    kind, label, category, group, interactive = "io_box", "I/O Box", "Input", "io_boxes", True
    props = (Prop("unit", "Unit"), Prop("value_type", "Value type", "choice", "float", ("float", "integer", "string")),
             *NUMBER_FORMAT)

    def create(self, data, ctx):
        widget = QLineEdit()
        widget.setPlaceholderText(str(data.get("label", "")))
        return widget

    def connect(self, widget, emit):
        widget.editingFinished.connect(lambda: emit(widget.text()))


class TextInput(IoBox):
    kind, label, group, in_palette = "text_input", "Text Input", "text_inputs", False
    props = (Prop("value_type", "Value type", "choice", "string", ("string", "integer", "float")),)


class Value(Control):
    kind, label, group = "value", "Value Display", "values"
    props = (Prop("unit", "Unit"), Prop("value_type", "Display type", "choice", "float", ("float", "integer")),
             *NUMBER_FORMAT, Prop("value_table", "Show DBC value-table text", "bool", True))

    def create(self, data, ctx):
        widget = QLabel("--")
        widget.setAlignment(Qt.AlignCenter)
        widget.setFrameShape(QFrame.StyledPanel)
        widget.setFrameShadow(QFrame.Sunken)
        return widget

    def preview(self, widget, data):
        widget.setText(format_value(12.5, data) if data.get("unit") else "--")


class Display(Control):
    kind, label, group = "display", "7-Segment Display", "displays"
    size = (160, 60)
    props = (Prop("unit", "Unit"), Prop("digits", "Digits", "int", 6, minimum=1, maximum=20),
             Prop("segment_color", "Segment colour", "color", "#00e676"), *NUMBER_FORMAT)

    def create(self, data, ctx):
        return SevenSegmentDisplay(integer(data, "digits", 6), color(data, "segment_color", "#00e676"),
                                   str(data.get("unit", "") or ""))

    def set_value(self, widget, data, value):
        widget.show_text(format_value(value, dict(data, unit="", _choices=None)))

    def preview(self, widget, data):
        widget.show_text(format_value(12.3, dict(data, unit="")))


class Gauge(Control):
    kind, label, group = "gauge", "Gauge", "gauges"
    size = (160, 160)
    props = (Prop("min", "Min", "float", 0), Prop("max", "Max", "float", 100), Prop("unit", "Unit"),
             Prop("decimals", "Decimals (blank = auto)", "optional_float", ""),
             Prop("warning", "Warning from (blank = none)", "optional_float", ""),
             Prop("critical", "Critical from (blank = none)", "optional_float", ""))

    def create(self, data, ctx):
        minimum, maximum = _range(data)
        return GaugeWidget(minimum, maximum, str(data.get("unit", "") or ""), optional_num(data, "decimals"),
                           optional_num(data, "warning"), optional_num(data, "critical"), str(data.get("label", "")))

    def set_value(self, widget, data, value):
        widget.setValue(float(value))

    def preview(self, widget, data):
        widget.setValue(widget.minimum() + (widget.maximum() - widget.minimum()) * 0.35)


class ProgressBar(Control):
    kind, label, group = "progress_bar", "Progress Bar", "progress_bars"
    size = (160, 24)
    props = (Prop("min", "Min", "float", 0), Prop("max", "Max", "float", 100), Prop("unit", "Unit"),
             Prop("orientation", "Orientation", "choice", "horizontal", ("horizontal", "vertical")),
             Prop("bar_color", "Bar colour", "color", ""))

    def create(self, data, ctx):
        widget = QProgressBar()
        widget.setOrientation(Qt.Vertical if data.get("orientation") == "vertical" else Qt.Horizontal)
        minimum, maximum = _range(data)
        widget.setRange(int(minimum), int(maximum))
        widget.setValue(widget.minimum())
        widget.setFormat(("%v " + str(data.get("unit", "") or "")).strip())
        if data.get("bar_color") and QColor(str(data["bar_color"])).isValid():
            palette = widget.palette()
            palette.setColor(QPalette.Highlight, QColor(str(data["bar_color"])))
            widget.setPalette(palette)
        return widget

    def set_value(self, widget, data, value):
        widget.setValue(int(round(float(value))))

    def preview(self, widget, data):
        widget.setValue(int(widget.minimum() + (widget.maximum() - widget.minimum()) * 0.6))


class Led(Control):
    kind, label, group = "led", "LED", "leds"
    size = (100, 26)
    uses_label = False
    props = (Prop("on_text", "On text", "str", "ON"), Prop("off_text", "Off text", "str", "OFF"),
             Prop("on_color", "On colour", "color", "#16c60c"), Prop("off_color", "Off colour", "color", "#3a3a3a"),
             Prop("blink", "Blink while on", "bool", False))

    def create(self, data, ctx):
        return LedWidget(color(data, "on_color", "#16c60c"), color(data, "off_color", "#3a3a3a"),
                         str(data.get("on_text", "ON")), str(data.get("off_text", "OFF")), flag(data, "blink"))

    def set_value(self, widget, data, value):
        if isinstance(value, str):
            value = value.strip().lower() not in ("", "0", "false", "off", "no")
        widget.set_on(bool(value))

    def get_value(self, widget):
        return widget.is_on()

    def preview(self, widget, data):
        widget.set_on(True)


class Indicator(Control):
    kind, label, group = "indicator", "Multi-State Indicator", "indicators"
    size = (120, 32)
    uses_label = False
    props = (Prop("states", "States (value=text:colour; ...)", "states",
                  "0=Off:#5f6368; 1=On:#2e7d32; 2=Error:#c62828"),)

    def create(self, data, ctx):
        return StateIndicator(parse_states(data.get("states", "")))

    def set_value(self, widget, data, value):
        widget.set_state(value)

    def preview(self, widget, data):
        if len(widget.states) > 1:
            widget.set_state(widget.states[1][0])


class Trend(Control):
    kind, label, group = "trend", "Trend Graph", "trends"
    size = (240, 120)
    uses_label = False
    props = (Prop("window", "Time window (s)", "float", 10, minimum=1, maximum=3600), Prop("unit", "Unit"),
             Prop("line_color", "Line colour", "color", ACCENT),
             Prop("min", "Y min (blank = auto)", "optional_float", ""),
             Prop("max", "Y max (blank = auto)", "optional_float", ""))

    def create(self, data, ctx):
        return TrendWidget(num(data, "window", 10), color(data, "line_color", ACCENT), optional_num(data, "min"),
                           optional_num(data, "max"), str(data.get("unit", "") or ""))

    def set_value(self, widget, data, value):
        widget.append(float(value))

    def preview(self, widget, data):
        now = time.monotonic()
        for step in range(40):
            widget.append(50 + 30 * math.sin(step / 5), now - widget.window + step * widget.window / 40)
        if widget.plot is not None:
            widget._redraw()


class Output(Control):
    kind, label, group = "output", "Output Box", "outputs"
    size = (260, 120)
    uses_label = False

    def create(self, data, ctx):
        return OutputBox()

    def set_value(self, widget, data, value):
        """Each value is appended as a line; None (or "") clears the box."""
        if value is None or value == "":
            widget.clear()
        else:
            widget.appendPlainText(str(value))

    def preview(self, widget, data):
        widget.setPlainText("Script output appears here")


class Label(Control):
    kind, label, category, group = "label", "Label", "Decoration", "labels"
    uses_label = False
    props = (Prop("text", "Text", "str", "Label"),
             Prop("align", "Alignment", "choice", "left", ("left", "center", "right")),
             Prop("word_wrap", "Word wrap", "bool", False))

    def create(self, data, ctx):
        widget = QLabel(str(data.get("text", data.get("label", "Label"))))
        align = {"center": Qt.AlignCenter, "right": Qt.AlignRight | Qt.AlignVCenter}.get(
            data.get("align"), Qt.AlignLeft | Qt.AlignVCenter)
        widget.setAlignment(align)
        widget.setWordWrap(flag(data, "word_wrap"))
        return widget


class GroupBox(Control):
    kind, label, category, group = "group_box", "Group Box", "Decoration", "group_boxes"
    size = (240, 160)

    def create(self, data, ctx):
        return QGroupBox(str(data.get("label", "Group")))

    def set_value(self, widget, data, value):
        widget.setTitle(str(value))

    def get_value(self, widget):
        return widget.title()


class Picture(Control):
    kind, label, category, group = "picture", "Picture", "Decoration", "pictures"
    size = (160, 120)
    uses_label = False
    props = (Prop("image", "Image file", "file"),)

    def create(self, data, ctx):
        return PictureWidget(resolve_path(data.get("image", ""), ctx.get("base_dir")))

    def set_value(self, widget, data, value):
        widget.set_path(value)

    def get_value(self, widget):
        return None


def resolve_path(path, base_dir):
    if not path:
        return ""
    path = Path(str(path))
    if not path.is_absolute() and base_dir:
        path = Path(base_dir) / path
    return str(path)


CONTROLS = {control.kind: control for control in (
    Button(), Switch(), CheckBox(), Radio(), Combo(), Slider(), Knob(), Spin(), IoBox(), TextInput(),
    Value(), Display(), Gauge(), ProgressBar(), Led(), Indicator(), Trend(), Output(),
    Label(), GroupBox(), Picture(),
)}
WIDGET_GROUPS = {kind: control.group for kind, control in CONTROLS.items()}
CATEGORIES = ("Input", "Display", "Decoration")


def build(kind, data, ctx=None):
    """Create a configured control widget (unknown kinds fall back to a label)."""
    control = CONTROLS.get(kind) or CONTROLS["label"]
    widget = control.create(data, ctx or {})
    apply_appearance(widget, data, control.interactive)
    return control, widget
