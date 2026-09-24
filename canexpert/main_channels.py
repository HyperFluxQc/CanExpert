"""
The main window's CAN Channels: the receivers found, the channels used before, the ECUs that answer on each
and the database each can load, the ECU check (TesterPresent while no database is connected), the scan for
ECUs and the channel setup.
"""
import json
import time

import can
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QMenu, QTreeWidgetItem

from canexpert.can_bus import SUPPORTED_INTERFACES, CanWorker, ReceiveMailbox, channel_key
from canexpert.channel_setup import ChannelSetup, load_setup, open_configured, save_setup
from canexpert.channel_setup_dialog import ChannelSetupDialog
from canexpert.ecu_scan import EcuScanDialog
from canexpert.panel.database import select_database


LAST_CHANNEL = "last_channel"      # settings: the channel to select and check at the next start
USED_CHANNELS = "used_channels"    # settings: the channels connected before, shown in bold


class Channels:
    """The CAN Channels panel and the ECU check of MainWindow (main_window.py)."""

    def _read_channel_history(self):
        """The channel used last and every channel connected before, as saved by _remember_channel()."""
        settings = self._settings
        try:
            used = json.loads(settings.value(USED_CHANNELS, "[]") or "[]")
            last = json.loads(settings.value(LAST_CHANNEL, "null") or "null")
        except ValueError:
            used, last = [], None
        self.used_channels = {tuple(key) for key in used if isinstance(key, list)}
        self.last_channel = tuple(last) if isinstance(last, list) else None

    def _remember_channel(self, channel_config):
        """Keep the channel as the one to select and check at the next start."""
        key = channel_key(channel_config)
        self.last_channel = key
        self.used_channels.add(key)
        settings = self._settings
        settings.setValue(LAST_CHANNEL, json.dumps(list(key)))
        settings.setValue(USED_CHANNELS, json.dumps([list(used) for used in self.used_channels]))

    def check_last_channel(self):
        """At startup: select the channel used last and start checking its ECUs with TesterPresent, so a
        responding ECU and the database it can load appear without connecting first."""
        item = self.channel_items.get(self.last_channel)
        if item is None or self.can_bus is not None or not self.active_config:
            return
        self.on_channel_selected(item)
        self.check_ecus(item.data(0, Qt.UserRole))

    def _matching_database(self):
        """The panel database Connect would load for the active configuration, or None."""
        if not self.active_config:
            return None
        try:
            return select_database(self.databases_dir, str(self.active_config.get("database_family", "")))
        except (OSError, ValueError) as exc:
            self.log_verbose(f"Database selection: {exc}")
            return None

    def refresh_channel_list(self):
        self.channel_list.clear()
        self.channel_items.clear()
        self.node_items.clear()
        self.can_channels = []
        for interface, label in SUPPORTED_INTERFACES:
            try:
                for cfg in can.detect_available_configs(interfaces=[interface], timeout=2.0):
                    cfg["interface"] = interface
                    self.can_channels.append(cfg)
            except Exception as exc:
                self.log_verbose(f"{label} detection: {exc}")
        for in_use in (self.connected_channel_config, self.monitor_channel if self.ecu_monitor else None):
            if in_use and not any(channel_key(c) == channel_key(in_use) for c in self.can_channels):
                self.can_channels.append(in_use)
        self.database_items.clear()
        for cfg in self.can_channels:
            item = QTreeWidgetItem([self._channel_label(cfg)])
            item.setData(0, Qt.UserRole, cfg)
            key = channel_key(cfg)
            if key in self.used_channels:  # channels connected before stand out
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
            self.channel_list.addTopLevelItem(item)
            self.channel_items[key] = item
        if not self.can_channels:
            self.channel_list.addTopLevelItem(QTreeWidgetItem(["No CAN receivers found"]))
        remembered = self.channel_items.get(self.last_channel)
        if remembered is not None and self.can_bus is None:
            self.channel_list.setCurrentItem(remembered)
            self.selected_channel_config = remembered.data(0, Qt.UserRole)
        self._update_nodes()

    def _channel_label(self, cfg):
        label = f"[{cfg['interface']}] Ch {cfg.get('channel', 0)}: {cfg.get('device_name', cfg.get('description', 'CAN receiver'))}"
        serial = cfg.get("serial") or cfg.get("unique_hardware_id")
        if serial:
            label += f" ({serial})"
        if load_setup(self._settings, cfg).listen_only:
            label += " [listen-only]"
        if self.connected_channel_config and channel_key(cfg) == channel_key(self.connected_channel_config):
            label += " [Connected]"
        elif self.ecu_monitor and channel_key(cfg) == channel_key(self.monitor_channel):
            label += " [Checking ECUs]"
        return label

    def _label_channels(self):
        for item in self.channel_items.values():
            item.setText(0, self._channel_label(item.data(0, Qt.UserRole)))

    def _channel_checked(self, key):
        """True while ECU replies on this channel are being watched: a database session or the ECU check."""
        if self.can_bus is not None and self.connected_channel_config is not None:
            if key == channel_key(self.connected_channel_config):
                return True
        return self.ecu_monitor is not None and key == channel_key(self.monitor_channel)

    def _update_nodes(self):
        now, responding = time.monotonic(), set()
        for (channel, can_id), state in self.node_states.items():
            parent = self.channel_items.get(channel)
            if parent is None:
                continue
            item = self.node_items.get((channel, can_id))
            if item is None:
                item = QTreeWidgetItem(parent)
                self.node_items[(channel, can_id)] = item
                parent.setExpanded(True)
            if not self._channel_checked(channel):
                symbol, status, colour = "○", "Not checked", "gray"
            elif now - state["last_seen"] > state["timeout"]:
                symbol, status, colour = "✗", "Lost connection", "red"
            else:
                symbol, status, colour = "●", "Responding", "green"
                responding.add(channel)
            item.setText(0, f"{symbol} ECU 0x{can_id:X} — {status}")
            item.setForeground(0, QColor(colour))
            item.setData(0, Qt.UserRole, parent.data(0, Qt.UserRole))
        self._update_databases(responding)

    def _update_databases(self, responding):
        """Offer the database that Connect would load under every channel with a responding ECU."""
        database = self._matching_database() if responding else None
        for key, parent in self.channel_items.items():
            item = self.database_items.get(key)
            if database is None or key not in responding:
                if item is not None:
                    parent.removeChild(item)
                    del self.database_items[key]
                continue
            if item is None:
                item = QTreeWidgetItem(parent)
                self.database_items[key] = item
                parent.setExpanded(True)
            loaded = (self.app_database or {}).get("source_path") == str(database.resolve())
            item.setText(0, f"▣ {database.stem} — {'loaded' if loaded else 'double-click to load'}")
            item.setForeground(0, QColor("#1566ae"))
            item.setData(0, Qt.UserRole, parent.data(0, Qt.UserRole))  # double-click connects this channel

    def on_channel_selected(self, item, column=0):
        cfg = item.data(0, Qt.UserRole)
        if cfg:
            self.selected_channel_config = cfg
            self.last_channel = channel_key(cfg)
            self._settings.setValue(LAST_CHANNEL, json.dumps(list(self.last_channel)))
            self.status_label.setText(f"Selected {cfg['interface']} channel {cfg.get('channel', 0)}")

    def on_channel_double_clicked(self, item, column=0):
        self.on_channel_selected(item, column)
        if self.can_bus is None:
            self.on_connect_clicked()

    def start_ecu_monitor(self, channel_config, config):
        """Send TesterPresent on the channel at the configuration's interval and watch the ECU replies:
        each ECU shows Responding, or Lost connection after the node loss timeout."""
        self.stop_ecu_monitor()
        channel = channel_config.get("channel", 0)
        setup = load_setup(self._settings, channel_config)
        if setup.listen_only:
            self.log_verbose("ECU check not started: it sends TesterPresent, and the channel is set to "
                             "listen-only (right-click the channel, Channel setup...)")
            return
        try:
            bus = open_configured(channel_config, config["bitrate"], setup, config)
        except Exception as exc:
            self.log_verbose(f"ECU check not started: {exc}")
            return
        worker = CanWorker(bus, config)
        self.clock.begin(time.time())
        worker.message_received.connect(lambda msg, w=worker: self._on_monitor_message(w, msg))
        worker.message_sent.connect(lambda can_id, data, w=worker: self.dispatch_frame(time.time(), "TX", can_id, data)
                                    if w is self.ecu_monitor else None)
        worker.error_occurred.connect(lambda error, w=worker: self._monitor_failed(w, error))
        self.ecu_monitor, self.monitor_bus = worker, bus
        self.monitor_channel, self.monitor_config = dict(channel_config), config
        self._update_diagnostic_ids()
        worker.start()
        self._label_channels()
        self._update_nodes()
        self.log_verbose(f"Checking ECUs on {channel_config['interface']} channel {channel}: TesterPresent to "
                         f"0x{config['request_id']:X} every {config['tester_present_interval_seconds']:g} s "
                         f"(right-click the channel to stop)")

    def stop_ecu_monitor(self):
        worker, bus = self.ecu_monitor, self.monitor_bus
        if worker is None:
            return
        self.ecu_monitor = self.monitor_bus = None
        worker.stop()
        try:
            bus.shutdown()
        except Exception as exc:
            self.log_verbose(str(exc))
        self._label_channels()
        self._update_nodes()
        self.log_verbose("Stopped checking ECUs")

    def _on_monitor_message(self, worker, msg):
        """Every frame seen while the ECUs are checked: the trace and the logger see the whole bus,
        the node tree only the configured response identifiers."""
        config = self.monitor_config
        if worker is not self.ecu_monitor:
            return
        self.dispatch_frame(msg["timestamp"], "RX", msg["arbitration_id"], msg["data"],
                            msg.get("is_extended_frame", False))
        if msg["arbitration_id"] not in config["response_ids"]:
            return
        if msg.get("is_extended_frame", False) != (not config["identifier_11_bit"]):
            return
        self.node_states[(channel_key(self.monitor_channel), msg["arbitration_id"])] = {
            "last_seen": time.monotonic(), "timeout": config["node_timeout_seconds"]}
        self._update_nodes()

    def _monitor_failed(self, worker, error):
        if worker is self.ecu_monitor:
            self.log_verbose(f"ECU check stopped: {error}")
            self.stop_ecu_monitor()

    def _channel_menu(self, position):
        item = self.channel_list.itemAt(position)
        cfg = item.data(0, Qt.UserRole) if item else None
        if not cfg:
            return
        menu = QMenu(self)
        if self.ecu_monitor and channel_key(cfg) == channel_key(self.monitor_channel):
            menu.addAction("Stop checking ECUs", self.stop_ecu_monitor)
        elif self.can_bus is None and self.active_config:
            menu.addAction(f"Check ECUs with \"{self.active_config.get('name', '')}\"", lambda: self.check_ecus(cfg))
        menu.addSeparator()
        menu.addAction("Scan for ECUs on this channel...", lambda: self.open_ecu_scan(cfg))
        menu.addAction("Channel setup...", lambda: self.edit_channel_setup(cfg))
        menu.exec_(self.channel_list.viewport().mapToGlobal(position))

    def open_ecu_scan(self, channel_config=None):
        """Find the ECUs of a channel: the connected one by default, over the running session."""
        dialog = EcuScanDialog(self, open_bus=lambda: self._scan_bus(channel_config),
                               new_configuration=self._configuration_for,
                               detect_bitrate=lambda _dialog: self.edit_channel_setup(
                                   channel_config or self.selected_channel_config))
        dialog.show()
        return dialog

    def _scan_bus(self, channel_config=None):
        """(bus, mailbox or None, close, padding) for a scan: a mailbox on the session or the ECU check when
        they run on that channel - their TesterPresent is paused meanwhile - else the channel itself."""
        wanted = channel_config or self.connected_channel_config or self.monitor_channel or self.selected_channel_config
        if wanted is None:
            raise ValueError("Select a CAN channel first.")
        running = [(self.can_bus, self.worker, self.session_config, self.connected_channel_config),
                   (self.monitor_bus, self.ecu_monitor, self.monitor_config, self.monitor_channel)]
        for bus, worker, config, channel in running:
            if bus is not None and worker is not None and channel is not None and \
                    channel_key(channel) == channel_key(wanted):
                if getattr(bus, "listen_only", False):
                    raise ValueError("The channel is open listen-only: a scan has to send TesterPresent.")
                mailbox = ReceiveMailbox(bus, worker.message_sent.emit)
                worker.add_mailbox(mailbox)
                return mailbox, mailbox, lambda w=worker, m=mailbox: (w.remove_mailbox(m), m.close()), \
                    config.get("isotp_padding")
        setup = load_setup(self._settings, wanted)
        if setup.listen_only:
            raise ValueError("The channel is set to listen-only (Channel setup): a scan has to send TesterPresent.")
        try:
            config = self.session_configuration()
        except ValueError:
            config = {"bitrate": 500000, "isotp_padding": 0xCC}
        # The channel's receive filter would hide the answers of ECUs it does not expect, so it is left out.
        bus = open_configured(wanted, config.get("bitrate", 500000),
                              ChannelSetup(setup.sample_point, setup.sjw, False, ""))
        return bus, None, bus.shutdown, config.get("isotp_padding")

    def _configuration_for(self, responder):
        """A new configuration for an ECU the scan found, in the ordinary configuration dialog."""
        self._open_configuration_dialog({
            "name": f"ECU {responder.response_id:X}", "request_id": responder.request_id,
            "response_id": responder.response_id, "identifier_11_bit": not responder.extended,
            "bitrate": int((self.session_config or self.active_config or {}).get("bitrate", 500000))})

    def edit_channel_setup(self, channel_config):
        """Sample point, listen-only, receive filter and bit rate detection for one adapter channel."""
        key = channel_key(channel_config)
        in_use = self._channel_checked(key) or (self.can_bus is not None and self.connected_channel_config is not None
                                                and key == channel_key(self.connected_channel_config))
        bitrate = int((self.session_config or self.active_config or {}).get("bitrate", 500000))
        dialog = ChannelSetupDialog(channel_config, load_setup(self._settings, channel_config), bitrate, self,
                                    in_use=in_use)
        if dialog.exec_() == ChannelSetupDialog.Accepted:
            save_setup(self._settings, channel_config, dialog.setup)
            self._label_channels()
            self.log_verbose(f"Channel setup of [{channel_config['interface']}] Ch {channel_config.get('channel', 0)}: "
                             f"{dialog.setup.describe() or 'the defaults'}"
                             + (" - used from the next connection" if in_use else ""))
        return dialog

    def check_ecus(self, channel_config):
        """Start the ECU check on a channel with the selected configuration, without loading its database."""
        try:
            config = self.session_configuration()
        except ValueError as exc:
            self._set_status(f"Invalid configuration: {exc}", "red")
            return
        self.start_ecu_monitor(channel_config, config)
