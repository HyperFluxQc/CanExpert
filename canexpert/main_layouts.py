"""
The main window's arrangement (CANoe's desktops): the docks and the workspace saved on close and restored at
the start, named desktops, Reset layout, and the panes put right after a saved state is applied.
"""
import re

from PyQt5.QtWidgets import QInputDialog, QWidget

from canexpert.workspace import add_pane, drop_empty_floating, make_pane, pane_names, put_back


LAYOUT_GEOMETRY = "layout/geometry"
LAYOUT_STATE = "layout/state"
LAYOUT_WORKSPACE = "layout/workspace"
DESKTOPS = "layout/desktops"       # settings: name -> saved window arrangement (a "desktop")
TOOL_AREAS = {"trace": "bottom", "transmit": "bottom", "write": "bottom"}   # the others: an area of their own
PAGE_PANE = re.compile(r"pane_page_(\d+)$")


class Layouts:
    """Saved layouts and desktops of MainWindow (main_window.py)."""

    def layout_state(self):
        """Everything about the arrangement: the docked panels, and the workspace windows."""
        return self.saveState(), self.workspace.saveState()

    def apply_layout_state(self, layout):
        """Put the panels and the workspace windows back as layout describes them."""
        panels, workspace = layout
        if panels:
            self.restoreState(panels)
        if workspace:
            for index in sorted({int(match.group(1)) for match in map(PAGE_PANE.match, pane_names(workspace))
                                 if match}):
                self._page_slot(index)          # so the arrangement can place the pages it knows
            self.workspace.restoreState(workspace)
            self._settle_panes()

    def _settle_panes(self):
        """After an arrangement was applied: it says where the windows go, not what is in them.

        A window it opened that has nothing to show - the Database with no database loaded, a tool not
        opened yet, a page the database does not have - is closed again, keeping its place. A window it
        did not know at all (saved before that window existed) goes back to its usual place (put_back).
        Floating windows it kept with nothing in them go."""
        self._settling = True
        try:
            for name, pane in self._tool_slots.items():
                if pane.dockAreaWidget() is None:
                    area = TOOL_AREAS.get(name, "center")
                    put_back(self.workspace, pane, area, beside=self.database_pane if area != "center" else None)
                elif name not in self.tool_panes:
                    pane.toggleView(False)
            for pane in self._page_slots:
                if pane.dockAreaWidget() is None:
                    put_back(self.workspace, pane, beside=self.database_pane, open_=pane in self.page_panes)
                elif pane not in self.page_panes:
                    pane.toggleView(False)
            if self.database_pane.dockAreaWidget() is None:
                put_back(self.workspace, self.database_pane, open_=self.app_database is not None)
            if self.app_database is None:
                self.database_pane.toggleView(False)
        finally:
            self._settling = False
        drop_empty_floating(self.workspace)

    def _page_slot(self, index):
        """The window for page index (1 onwards; page 0 is the Database window), made the first time."""
        while len(self._page_slots) < index:
            slot = make_pane(f"Page {len(self._page_slots) + 1}", QWidget(), f"pane_page_{len(self._page_slots) + 1}")
            add_pane(self.workspace, slot, beside=self.database_pane)
            slot.toggleView(False)
            self._page_slots.append(slot)
        return self._page_slots[index - 1]

    def save_layout(self):
        panels, workspace = self.layout_state()
        self._settings.setValue(LAYOUT_GEOMETRY, self.saveGeometry())
        self._settings.setValue(LAYOUT_STATE, panels)
        self._settings.setValue(LAYOUT_WORKSPACE, workspace)

    def restore_layout(self):
        """Put the window, its panels and its workspace windows back where they were left."""
        geometry = self._settings.value(LAYOUT_GEOMETRY)
        if geometry:
            self.restoreGeometry(geometry)
        self.apply_layout_state((self._settings.value(LAYOUT_STATE), self._settings.value(LAYOUT_WORKSPACE)))

    def desktops(self) -> list[str]:
        self._settings.beginGroup(DESKTOPS)
        names = sorted(self._settings.childGroups())
        self._settings.endGroup()
        return names

    def save_desktop(self, name=None):
        """Keep the current arrangement under a name, as CANoe keeps desktops."""
        if name is None:
            name, ok = QInputDialog.getText(self, "Save desktop", "Name of this window arrangement:")
            if not ok or not name.strip():
                return None
        name = name.strip()
        panels, workspace = self.layout_state()
        self._settings.setValue(f"{DESKTOPS}/{name}/panels", panels)
        self._settings.setValue(f"{DESKTOPS}/{name}/workspace", workspace)
        self._refresh_desktop_menu()
        self._set_status(f"Desktop '{name}' saved", "green")
        return name

    def apply_desktop(self, name):
        layout = (self._settings.value(f"{DESKTOPS}/{name}/panels"),
                  self._settings.value(f"{DESKTOPS}/{name}/workspace"))
        if any(layout):
            self.apply_layout_state(layout)
            self._set_status(f"Desktop '{name}'", "gray")

    def reset_layout(self):
        """Back to the arrangement the window starts with."""
        self.apply_layout_state(self._default_layout)
        for pane in self.tool_panes.values():
            pane.toggleView(False)
        self.database_pane.toggleView(self.app_database is not None)
        for pane in self.page_panes:
            pane.toggleView(True)

    def _refresh_desktop_menu(self):
        menu = getattr(self, "_desktop_menu", None)
        if menu is None:
            return
        menu.clear()
        for name in self.desktops():
            menu.addAction(name, lambda checked=False, n=name: self.apply_desktop(n))
        if not self.desktops():
            menu.addAction("(none saved yet)").setEnabled(False)
