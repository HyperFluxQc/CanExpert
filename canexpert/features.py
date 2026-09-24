"""
Features that are built but switched off for now - CAN Expert's #if 0.

A feature that is off shows nowhere: no toolbar button, no menu entry, no window, nothing in the panel
scripts' API or the editor's completion. Its code stays, and its tests switch it on, so it keeps working
and turning it back on is changing False to True here. The switches are read while the application
runs, not once at import.
"""

# System variables (sysvars.py): named values shared by the panel script, a window and the CAN Logger,
# with api.sysvar and @on_sysvar. Off until panel controls and bus signals can be bound to them.
SYSTEM_VARIABLES = False
