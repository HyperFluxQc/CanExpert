"""
The workspace: the central area of the main window, where the panel and the analysis windows live.

It is the Qt Advanced Docking System (PyQtAds), which gives what plain Qt docks do not: drop guides
while dragging, several windows tabbed in one area, floating windows that keep their tabs, and saved
arrangements. The Configuration, CAN Channels and Log panels stay ordinary Qt docks around it, as the
fixed panels of CANoe do.
"""
from __future__ import annotations

import re

from PyQt5.QtCore import QByteArray, qUncompress
from PyQt5.QtGui import QGuiApplication
from PyQt5.QtWidgets import QWidget
# PyQtAds is a compiled binding against the Qt libraries PyQt5 ships, and importing PyQt5 is what puts
# those libraries on the search path. This import must therefore stay below the one above.
from PyQtAds import ads

# How the workspace behaves. They are static settings, so they are applied before the manager exists.
CONFIG_FLAGS = {
    ads.CDockManager.OpaqueSplitterResize: True,        # the panes resize while the splitter is dragged
    ads.CDockManager.FocusHighlighting: True,           # the window being worked in is marked
    ads.CDockManager.AllTabsHaveCloseButton: True,
    ads.CDockManager.DockAreaHasTabsMenuButton: True,   # the menu listing the tabs of one area
    ads.CDockManager.DragPreviewIsDynamic: True,        # the drag shows where the window would land
    ads.CDockManager.DragPreviewShowsContentPixmap: True,
    ads.CDockManager.FloatingContainerHasWidgetTitle: True,
    ads.CDockManager.MiddleMouseButtonClosesTab: True,
    ads.CDockManager.HideSingleCentralWidgetTitleBar: False,
}
# Where a window is put when it is opened for the first time.
AREAS = {
    "center": ads.CenterDockWidgetArea,                 # tabbed with what is already there
    "bottom": ads.BottomDockWidgetArea,
    "top": ads.TopDockWidgetArea,
    "left": ads.LeftDockWidgetArea,
    "right": ads.RightDockWidgetArea,
}
MIN_FLOATING = (480, 300)   # a floating window smaller than this shows nothing useful


def create_workspace(main_window) -> ads.CDockManager:
    """The dock manager, which becomes the central widget of the main window."""
    for flag, enabled in CONFIG_FLAGS.items():
        ads.CDockManager.setConfigFlag(flag, enabled)
    workspace = ads.CDockManager(main_window)
    workspace.setObjectName("workspace")
    return workspace


def make_pane(title: str, widget: QWidget, name: str = "") -> ads.CDockWidget:
    """A workspace window holding widget. Closing one hides it, so it keeps what it recorded."""
    pane = ads.CDockWidget(title)
    pane.setObjectName(name or f"pane_{title.lower().replace(' ', '_')}")
    pane.setWidget(widget)
    pane.setFeature(ads.CDockWidget.DockWidgetDeleteOnClose, False)
    pane.setFeature(ads.CDockWidget.DockWidgetForceCloseWithArea, False)
    return pane


def add_pane(workspace, pane, area: str = "center", beside=None):
    """Put a pane in the workspace: tabbed with beside ('center'), or splitting off it."""
    target = beside.dockAreaWidget() if beside is not None else None
    return workspace.addDockWidget(AREAS.get(area, ads.CenterDockWidgetArea), pane, target)


def put_back(workspace, pane, area: str = "center", beside=None, open_: bool = False):
    """Dock a window a restored state did not know, open or closed.

    PyQtAds leaves such a window out of the workspace and marks it closed, and closing a window it
    already thinks closed does nothing. Docked again but still marked closed, it would sit in a visible
    area with no tab - an empty strip - so it is opened for a moment, which puts the marks right, and
    then closed properly, its area hiding with it."""
    add_pane(workspace, pane, area, beside)
    pane.toggleView(True)
    if not open_:
        pane.toggleView(False)


def drop_empty_floating(workspace):
    """Remove the floating windows a restored state brought back with nothing in them (PyQtAds keeps
    one for every window ever floated, and saves them all)."""
    for floating in workspace.floatingWidgets():
        container = floating.dockContainer()
        if container is not None and not container.dockWidgets():
            floating.deleteLater()


def fit_on_screen(pane, minimum=MIN_FLOATING):
    """A floating window too small to show anything, or off every screen (a layout from another monitor
    setup), is given a usable size where it can be seen. A docked window is left alone."""
    container = pane.dockContainer()
    if container is None or not container.isFloating() or container.floatingWidget() is None:
        return
    window = container.floatingWidget()
    frame = window.geometry()
    screen = QGuiApplication.screenAt(frame.center()) or QGuiApplication.primaryScreen()
    if screen is None:
        return
    room = screen.availableGeometry()
    width = min(max(frame.width(), minimum[0]), room.width())
    height = min(max(frame.height(), minimum[1]), room.height())
    x = min(max(frame.x(), room.left()), room.right() + 1 - width)
    y = min(max(frame.y(), room.top()), room.bottom() + 1 - height)
    if (x, y, width, height) != (frame.x(), frame.y(), frame.width(), frame.height()):
        window.setGeometry(x, y, width, height)


def set_content(pane, widget: QWidget):
    """Put widget into a window made earlier, dropping what it held until now."""
    old = pane.takeWidget()
    if old is not None:
        old.deleteLater()
    pane.setWidget(widget)


def pane_names(state) -> set[str]:
    """The names of the windows a saved workspace state places (it is XML, compressed by default)."""
    data = bytes(state or b"")
    if data and not data.lstrip().startswith(b"<"):
        data = bytes(qUncompress(QByteArray(data)))
    return {name.decode() for name in re.findall(rb'<Widget Name="([^"]+)"', data)}
