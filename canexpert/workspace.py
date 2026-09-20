"""
The workspace: the central area of the main window, where the panel and the analysis windows live.

It is the Qt Advanced Docking System (PyQtAds), which gives what plain Qt docks do not: drop guides
while dragging, several windows tabbed in one area, floating windows that keep their tabs, and saved
arrangements. The Configuration, CAN Channels and Log panels stay ordinary Qt docks around it, as the
fixed panels of CANoe do.
"""
from __future__ import annotations

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
