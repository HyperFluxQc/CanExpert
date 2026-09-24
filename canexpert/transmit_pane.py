"""
The Transmit window: messages sent by hand or cyclically, and the nodes of the databases simulated.

Two ways of putting messages on the bus, one window: the Messages tab is the transmit list (raw or
database messages, once or at a cycle time), the Simulated nodes tab sends a database's messages node by
node, as those ECUs would. Both keep sending while the other tab is shown, or while another window's
tab is in front; closing the window stops both (stop_sending, which the main window calls when the pane
is closed).
"""
from __future__ import annotations

from PyQt5.QtWidgets import QDialog, QTabWidget, QVBoxLayout

from canexpert.simulation_window import SimulationWindow
from canexpert.transmit_window import TransmitWindow
from canexpert.ui_common import enable_maximize


class TransmitPane(QDialog):
    """The transmit list and the simulated nodes, as two tabs of one window."""

    def __init__(self, parent=None, symbols=None, send=None, settings=None):
        super().__init__(parent)
        self.setWindowTitle("Transmit")
        enable_maximize(self)
        self.setMinimumSize(720, 340)
        self.resize(900, 480)
        # Each tab would stop sending when hidden, which a tab is whenever the other one is shown.
        self.messages = TransmitWindow(None, symbols, send, settings, stop_when_hidden=False)
        self.nodes = SimulationWindow(None, symbols, send, settings, stop_when_hidden=False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.messages, "Messages")
        self.tabs.addTab(self.nodes, "Simulated nodes")
        layout.addWidget(self.tabs)
        for page in (self.messages, self.nodes):
            # Esc in a tab would hide that tab's dialog and leave the tab empty; it closes the window instead.
            page.finished.connect(lambda _result, page=page: (page.setVisible(True), self.reject()))

    def show_nodes(self):
        self.tabs.setCurrentWidget(self.nodes)

    def stop_sending(self):
        """Nothing keeps sending once the window is closed."""
        self.messages.stop_all()
        self.nodes.start_btn.setChecked(False)
