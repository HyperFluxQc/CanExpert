"""
The Dummy ECU window's tables - the DIDs, the DTCs with their faults, forced negative responses, security
levels, access rules, the application messages and their signals' generators: built, filled from a
configuration, checked cell by cell and read back, with the values the running ECU holds shown live.
"""
from __future__ import annotations


from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QComboBox, QHeaderView, QPushButton, QTableWidget, QTableWidgetItem, QWidget

from canexpert.simulator.fields import (printable, forced_text, parse_sessions, format_sessions, number_text,
                                        parse_value_ranges, format_value_ranges)
from canexpert.simulator.ecu import SERVICE_NAMES, EcuConfig
from canexpert.simulator.signals import GENERATORS, SignalSimulation
from canexpert.simulator.widgets import hint
from canexpert.uds.dtc import status_text



BUILTIN_DBC_TEXT = "Built-in: DBC/dummy_ecu.dbc (0x300 EngineData, 0x301 EcuStatus)"
GENERATOR_TEXT = {"constant": "Constant", "ramp": "Ramp", "sine": "Sine", "square": "Square", "random": "Random",
                  "counter": "Counter", "running": "Engine running", "logging": "Logging", "session": "Session"}
# Columns of the tables
DID_DID, DID_DATA, DID_TEXT, DID_WRITABLE, DID_SIGNAL, DID_SESSIONS, DID_LEVEL, DID_VALID = range(8)
DTC_DTC, DTC_STATUS, DTC_NOW, DTC_FAULT, DTC_SNAPSHOT, DTC_EXTENDED = range(6)
SIG_NAME, SIG_UNIT, SIG_KIND, SIG_LOW, SIG_HIGH, SIG_PERIOD, SIG_NOW = range(7)
MSG_NAME, MSG_ID, MSG_PERIOD, MSG_SEND = range(4)


def signal_setup(config: EcuConfig) -> SignalSimulation:
    """The messages and signals of a configuration's DBC with its generators (the built-in DBC when the
    configured one cannot be read), for the Signals tab."""
    engine = SignalSimulation()
    try:
        engine.load(config.dbc_path)
    except ValueError:
        engine.load("")
    try:
        engine.configure(config.generators, config.messages)
    except ValueError:
        pass
    return engine


class Tables:
    """The tables of DummyEcuWindow (window.py)."""

    def _table(self, headers, stretch_column):
        """A table that fits the settings pane: stretch_column takes the room, the others their content."""
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        for column in range(len(headers)):
            header.setSectionResizeMode(column, QHeaderView.Stretch if column == stretch_column
                                        else QHeaderView.ResizeToContents)
        table.setMinimumHeight(150)
        table.itemChanged.connect(self._on_data_edited)
        return table

    def _table_buttons(self, table, add):
        add_button, remove_button = QPushButton("Add"), QPushButton("Remove")
        add_button.clicked.connect(lambda: (add(), self._apply()))
        remove_button.clicked.connect(lambda: self._remove_row(table))
        row = self._row(add_button, remove_button)
        row.add_button = add_button
        return row

    def _data_page(self):
        dids, form = self._group("DIDs: ReadDataByIdentifier (0x22) and WriteDataByIdentifier (0x2E)")
        self.did_table = self._table(["DID", "Data (hex)", "As text", "Writable", "Signal", "Sessions", "Level",
                                      "Valid (hex)"], stretch_column=DID_DATA)
        form.addRow(self.did_table)
        self.did_buttons = self._table_buttons(self.did_table, lambda: self._add_did({"did": 0x0000, "data": "00"}))
        form.addRow(self.did_buttons)
        form.addRow(hint("A writable DID takes a new value of the same length, in the extended or programming "
                         "session once security access is unlocked. Signal (Message.Signal): the DID answers "
                         "that signal's raw value in as many bytes as its data has, and 2F controls it. "
                         "Sessions: where it can be read (and written), empty for any - elsewhere NRC 0x31; "
                         "Level: the security level it needs - otherwise NRC 0x33. Valid: the values it may "
                         "be written with, its data as one number (0258-04B0) - others get NRC 0x31. F186 "
                         "(session) and 0100 (uptime) are always there; F2xx DIDs are the periodic ones."))
        dtcs, form = self._group("DTCs: ReadDTCInformation (0x19) and ClearDiagnosticInformation (0x14)")
        self.dtc_table = self._table(["DTC", "Status", "Now", "Fault", "Snapshot record 01 (hex)",
                                      "Extended data 01 (hex)"], stretch_column=DTC_SNAPSHOT)
        form.addRow(self.dtc_table)
        self.dtc_buttons = self._table_buttons(self.dtc_table, lambda: self._add_dtc(0x000000, 0x00, b"", b""))
        form.addRow(self.dtc_buttons)
        form.addRow(hint("Status: at power-on; Now: as it is. Tick Fault and the DTC's test fails: pending at "
                         "once, confirmed after the operation cycles below, with the snapshot of that moment "
                         "and one more occurrence (the extended data's first byte). Untick it and the DTC "
                         "heals: no longer pending after a cycle, aged out after more. Snapshot: the number "
                         "of identifiers, then each DID and its data (19 04). 19 01, 19 02 and 19 0A report "
                         "the DTCs and their status."))
        cycle, form = self._group("Fault memory")
        self.confirm_cycles = self._spin(1, 255, suffix=" cycles")
        self.aging_cycles = self._spin(1, 255, suffix=" cycles")
        self.operation_cycle = self._double(0, 3600, 1, " s")
        self.operation_cycle.setSpecialValueText("on ECUReset and the button")
        self.snapshot_dids = self._line("none: the table's snapshot is kept")
        new_cycle = QPushButton("New operation cycle")
        new_cycle.setToolTip("End this operation cycle (ignition off) and start the next (ignition on)")
        new_cycle.clicked.connect(self.new_operation_cycle)
        form.addRow("Confirmed after", self.confirm_cycles)
        form.addRow("Aged out after", self.aging_cycles)
        form.addRow("Operation cycle every", self._row(self.operation_cycle, new_cycle))
        form.addRow("Snapshot DIDs", self.snapshot_dids)
        form.addRow(hint("When a fault appears, the snapshot record takes these DIDs with their values at that "
                         "moment, e.g. 0101, 0102 (temperature and pressure). ControlDTCSetting off (85 02) "
                         "freezes every status."))
        forced, form = self._group("Forced negative responses")
        self.nrc_table = self._table(["Service", "NRC", "Meaning"], stretch_column=2)
        self.nrc_table.setMinimumHeight(110)
        form.addRow(self.nrc_table)
        self.nrc_buttons = self._table_buttons(self.nrc_table, lambda: self._add_forced(0x22, 0x22))
        form.addRow(self.nrc_buttons)
        form.addRow(hint("Every request of that service is answered 7F <service> <NRC> - to see how a tester "
                         "copes with a refusal."))
        return self._page(dids, dtcs, cycle, forced)

    @staticmethod
    def _cell(text, editable=True):
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        return item

    def _check_cell(self, checked):
        item = self._cell("")
        item.setFlags((item.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        return item

    def _append(self, table, cells):
        loading, self._loading = self._loading, True
        try:
            row = table.rowCount()
            table.insertRow(row)
            for column, cell in enumerate(cells):
                if isinstance(cell, QWidget):
                    table.setCellWidget(row, column, cell)
                else:
                    table.setItem(row, column, cell)
        finally:
            self._loading = loading

    def _add_did(self, item):
        data = bytes.fromhex(str(item.get("data", "")))
        level = int(item.get("level", 0) or 0)
        self._append(self.did_table, [self._cell(f"{int(item['did']):04X}"), self._cell(data.hex(" ").upper()),
                                      self._cell(printable(data), editable=False),
                                      self._check_cell(bool(item.get("writable"))),
                                      self._cell(str(item.get("signal", "") or "")),
                                      self._cell(format_sessions(item.get("sessions"))),
                                      self._cell(f"{level:02X}" if level else ""),
                                      self._cell(format_value_ranges(item.get("valid")))])

    def _add_dtc(self, dtc, status, snapshot, extended):
        now = self._cell(f"{status:02X}", editable=False)
        now.setToolTip(status_text(status))
        self._append(self.dtc_table, [self._cell(f"{dtc:06X}"), self._cell(f"{status:02X}"), now,
                                      self._check_cell(False), self._cell(snapshot.hex(" ").upper()),
                                      self._cell(extended.hex(" ").upper())])

    def _add_forced(self, sid, nrc):
        meaning = self._cell(forced_text(sid, nrc), editable=False)
        meaning.setToolTip(f"Every request of this service is answered 7F {sid:02X} {nrc:02X}")
        self._append(self.nrc_table, [self._cell(f"{sid:02X}"), self._cell(f"{nrc:02X}"), meaning])

    def _add_level(self, level, seed_length, key_mask, dll, variant):
        self._append(self.level_table, [self._cell(f"{level:02X}"), self._cell(str(seed_length)),
                                        self._cell(f"{key_mask:02X}"), self._cell(dll), self._cell(variant)])

    def _add_free_level(self):
        taken = {self.security_level.value()}
        for row in range(self.level_table.rowCount()):
            try:
                taken.add(int(self.level_table.item(row, 0).text(), 16))
            except ValueError:
                pass
        level = next(level for level in range(0x03, 0x80, 2) if level not in taken)
        self._add_level(level, 4, 0x5A, "", "")

    def _add_rule(self, sid, sessions, level):
        self._append(self.rule_table, [self._cell(f"{sid:02X}"), self._cell(format_sessions(sessions)),
                                       self._cell(f"{level:02X}" if level else ""),
                                       self._cell(SERVICE_NAMES.get(sid, f"service {sid:02X}"), editable=False)])

    def _add_message(self, entry):
        message = entry.message
        can_id = f"{message.frame_id:08X}x" if message.is_extended_frame else f"{message.frame_id:03X}"
        send = self._check_cell(entry.on)
        if not entry.sendable:
            send.setFlags(send.flags() & ~Qt.ItemIsEnabled)
            send.setToolTip("Multiplexed or longer than 8 bytes: not sent")
        period = round(entry.cycle * 1000) if entry.cycle and entry.cycle != (message.cycle_time or 0) / 1000 else 0
        self._append(self.message_table, [self._cell(message.name, editable=False), self._cell(can_id, editable=False),
                                          self._cell(str(period)), send])

    def _add_signal(self, engine, item):
        signal = engine.signal(item["signal"])
        kind = QComboBox()
        for name in GENERATORS:
            kind.addItem(GENERATOR_TEXT[name], name)
        kind.setCurrentIndex(max(0, kind.findData(item["kind"])))
        kind.currentIndexChanged.connect(lambda _index, combo=kind: self._generator_changed(combo))
        self._append(self.signal_table, [self._cell(item["signal"], editable=False),
                                         self._cell(signal.unit or "" if signal is not None else "", editable=False),
                                         kind, self._cell(number_text(item["low"])),
                                         self._cell(number_text(item["high"])),
                                         self._cell(number_text(item["period"])), self._cell("", editable=False)])

    def _fill_signal_tables(self, engine: SignalSimulation):
        loading, self._loading = self._loading, True
        try:
            for table in (self.message_table, self.signal_table):
                table.setRowCount(0)
            for entry in engine.messages:
                self._add_message(entry)
            for item in engine.generators():
                self._add_signal(engine, item)
        finally:
            self._loading = loading

    def _generator_changed(self, combo):
        for row in range(self.signal_table.rowCount()):
            if self.signal_table.cellWidget(row, SIG_KIND) is combo:
                self._show_generator(row)
        self._apply()

    def _show_generator(self, row):
        combo = self.signal_table.cellWidget(row, SIG_KIND) if row >= 0 else None
        if combo is not None:
            name = self.signal_table.item(row, SIG_NAME).text()
            self.generator_hint.setText(f"{name}: {GENERATORS[combo.currentData()]}.")

    def _remove_row(self, table):
        row = table.currentRow()
        if row >= 0:
            table.removeRow(row)
            self._apply()

    def _on_data_edited(self, item):
        if self._loading:
            return
        table = item.tableWidget()
        if table is self.dtc_table and item.column() == DTC_FAULT:
            self._fault_toggled(item)
            return
        loading, self._loading = self._loading, True      # the previews are the window's own writing
        try:
            if table is self.did_table and item.column() == DID_DATA:
                try:
                    preview = printable(bytes.fromhex(item.text()))
                except ValueError:
                    preview = ""
                self.did_table.item(item.row(), DID_TEXT).setText(preview)
            if table is self.nrc_table and item.column() in (0, 1):
                try:
                    preview = forced_text(int(self.nrc_table.item(item.row(), 0).text(), 16),
                                          int(self.nrc_table.item(item.row(), 1).text(), 16))
                except ValueError:
                    preview = ""
                self.nrc_table.item(item.row(), 2).setText(preview)
            if table is self.rule_table and item.column() == 0:
                try:
                    sid = int(item.text(), 16)
                    preview = SERVICE_NAMES.get(sid, f"service {sid:02X}")
                except ValueError:
                    preview = ""
                self.rule_table.item(item.row(), 3).setText(preview)
        finally:
            self._loading = loading
        self._apply()

    def _fault_toggled(self, item):
        """The Fault box: the fault behind the DTC appears or goes away in the running ECU."""
        try:
            dtc = int(self.dtc_table.item(item.row(), DTC_DTC).text(), 16)
            self.ecu.set_fault(dtc, item.checkState() == Qt.Checked)
        except (ValueError, KeyError):
            loading, self._loading = self._loading, True
            item.setCheckState(Qt.Unchecked)
            self._loading = loading
            self.statusBar().showMessage("Not applied: that DTC is not in the ECU's table yet (check the row)")
            return
        self._refresh_dtc_status()

    @staticmethod
    def _mark(item, error: str | None):
        if error:
            item.setBackground(QColor("#fde2e2"))
            raise ValueError(error)
        item.setData(Qt.BackgroundRole, None)

    def _number(self, table, row, column, what, maximum, optional=False):
        item = table.item(row, column)
        text = item.text().strip()
        if optional and not text:
            self._mark(item, None)
            return 0
        try:
            value = int(text, 16)
            valid = 0 <= value <= maximum
        except ValueError:
            valid = False
        self._mark(item, None if valid else f"{what} in row {row + 1} must be hexadecimal, at most {maximum:X}")
        return value

    def _decimal(self, table, row, column, what, low=None, high=None):
        item = table.item(row, column)
        try:
            value = float(item.text().strip().replace(",", "."))
            valid = (low is None or value >= low) and (high is None or value <= high)
        except ValueError:
            valid = False
        self._mark(item, None if valid else f"{what} in row {row + 1} must be a number"
                   + (f", {low} or more" if low is not None else ""))
        return value

    def _hex_data(self, table, row, column, what, required):
        item = table.item(row, column)
        try:
            raw = bytes.fromhex(item.text())
            valid = bool(raw) or not required
        except ValueError:
            valid = False
        self._mark(item, None if valid else f"{what} in row {row + 1} must be hexadecimal bytes, e.g. 57 56 57")
        return raw.hex()

    def _session_list(self, table, row, column):
        item = table.item(row, column)
        try:
            sessions = parse_sessions(item.text())
        except ValueError as exc:
            self._mark(item, f"Sessions in row {row + 1}: {exc}")
        self._mark(item, None)
        return sessions

    def _value_ranges(self, table, row, column):
        item = table.item(row, column)
        try:
            ranges = parse_value_ranges(item.text())
        except ValueError as exc:
            self._mark(item, f"Valid values in row {row + 1}: {exc} (hex ranges, e.g. 0258-04B0)")
            return []
        self._mark(item, None)
        return ranges

    def _read_tables(self):
        """The data tables as configuration lists; ValueError naming the bad cell."""
        dids = []
        for row in range(self.did_table.rowCount()):
            entry = {"did": self._number(self.did_table, row, DID_DID, "The DID", 0xFFFF),
                     "data": self._hex_data(self.did_table, row, DID_DATA, "The DID's data", True),
                     "writable": self.did_table.item(row, DID_WRITABLE).checkState() == Qt.Checked}
            signal = self.did_table.item(row, DID_SIGNAL).text().strip()
            sessions = self._session_list(self.did_table, row, DID_SESSIONS)
            level = self._number(self.did_table, row, DID_LEVEL, "The level", 0x7F, optional=True)
            valid = self._value_ranges(self.did_table, row, DID_VALID)
            entry.update({key: value for key, value in (("signal", signal), ("sessions", sessions),
                                                        ("level", level), ("valid", valid)) if value})
            dids.append(entry)
        dtcs = [{"dtc": self._number(self.dtc_table, row, DTC_DTC, "The DTC", 0xFFFFFF),
                 "status": self._number(self.dtc_table, row, DTC_STATUS, "The status", 0xFF),
                 "snapshot": self._hex_data(self.dtc_table, row, DTC_SNAPSHOT, "The snapshot", False),
                 "extended": self._hex_data(self.dtc_table, row, DTC_EXTENDED, "The extended data", False)}
                for row in range(self.dtc_table.rowCount())]
        forced = [{"sid": self._number(self.nrc_table, row, 0, "The service", 0xFF),
                   "nrc": self._number(self.nrc_table, row, 1, "The NRC", 0xFF)}
                  for row in range(self.nrc_table.rowCount())]
        levels = []
        for row in range(self.level_table.rowCount()):
            level = self._number(self.level_table, row, 0, "The level", 0x7F)
            if not level % 2:
                self._mark(self.level_table.item(row, 0), f"The level in row {row + 1} must be odd (requestSeed)")
            length = int(self._decimal(self.level_table, row, 1, "The seed length", 1, 64))
            levels.append({"level": level, "seed_length": length,
                           "key_mask": self._number(self.level_table, row, 2, "The key mask", 0xFF),
                           "dll": self.level_table.item(row, 3).text().strip(),
                           "variant": self.level_table.item(row, 4).text().strip()})
        rules = [{"sid": self._number(self.rule_table, row, 0, "The service", 0xFF),
                  "sessions": self._session_list(self.rule_table, row, 1),
                  "level": self._number(self.rule_table, row, 2, "The level", 0x7F, optional=True)}
                 for row in range(self.rule_table.rowCount())]
        messages = [{"message": self.message_table.item(row, MSG_NAME).text(),
                     "on": self.message_table.item(row, MSG_SEND).checkState() == Qt.Checked,
                     "cycle_ms": int(self._decimal(self.message_table, row, MSG_PERIOD, "The period", 0, 3600000))}
                    for row in range(self.message_table.rowCount())]
        generators = [{"signal": self.signal_table.item(row, SIG_NAME).text(),
                       "kind": self.signal_table.cellWidget(row, SIG_KIND).currentData(),
                       "low": self._decimal(self.signal_table, row, SIG_LOW, "Low"),
                       "high": self._decimal(self.signal_table, row, SIG_HIGH, "High"),
                       "period": self._decimal(self.signal_table, row, SIG_PERIOD, "The period", 0)}
                      for row in range(self.signal_table.rowCount())]
        return dids, dtcs, forced, levels, rules, messages, generators

    def _fill_tables(self, config: EcuConfig):
        for table in (self.did_table, self.dtc_table, self.nrc_table, self.level_table, self.rule_table):
            table.setRowCount(0)
        for item in config.dids:
            self._add_did(item)
        for item in config.dtcs:
            self._add_dtc(int(item["dtc"]), int(item.get("status", 0)), bytes.fromhex(item.get("snapshot", "")),
                          bytes.fromhex(item.get("extended", "")))
        for item in config.forced_nrcs:
            self._add_forced(int(item["sid"]), int(item["nrc"]))
        for item in config.security_levels:
            self._add_level(int(item["level"]), int(item.get("seed_length", 4)), int(item.get("key_mask", 0)),
                            str(item.get("dll", "") or ""), str(item.get("variant", "") or ""))
        for item in config.service_rules:
            self._add_rule(int(item["sid"]), item.get("sessions") or [], int(item.get("level", 0) or 0))
        engine = signal_setup(config)
        if engine.source != config.dbc_path:
            self._log(f"Cannot read {config.dbc_path}: the built-in database is used")
        self._dbc_path = engine.source
        self.dbc_label.setText(engine.source or BUILTIN_DBC_TEXT)
        self._fill_signal_tables(engine)

    def _refresh_dtc_status(self):
        """The Now and Fault columns: the running ECU's statuses and faults, however they changed."""
        statuses, faults = self.ecu.dtcs, self.ecu.dtc_memory.faults
        loading, self._loading = self._loading, True
        try:
            for row in range(self.dtc_table.rowCount()):
                try:
                    dtc = int(self.dtc_table.item(row, DTC_DTC).text(), 16)
                except ValueError:
                    continue
                status = statuses.get(dtc)
                item = self.dtc_table.item(row, DTC_NOW)
                text = "" if status is None else f"{status:02X}"
                if item.text() != text:
                    item.setText(text)
                    item.setToolTip("" if status is None else status_text(status))
                fault = Qt.Checked if dtc in faults else Qt.Unchecked
                if self.dtc_table.item(row, DTC_FAULT).checkState() != fault:
                    self.dtc_table.item(row, DTC_FAULT).setCheckState(fault)
        finally:
            self._loading = loading

    def _refresh_signal_values(self):
        signals = self.ecu.signals
        overridden = signals.overridden()
        loading, self._loading = self._loading, True
        try:
            for row in range(self.signal_table.rowCount()):
                key = self.signal_table.item(row, SIG_NAME).text()
                value = signals.value(key)
                text = number_text(value) + (" (I/O control)" if key in overridden else "")
                item = self.signal_table.item(row, SIG_NOW)
                if item.text() != text:
                    item.setText(text)
        finally:
            self._loading = loading
