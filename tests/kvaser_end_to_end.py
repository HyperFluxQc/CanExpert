"""
Hardware check: the real main window against dummy_ecu.py over the Kvaser Virtual CAN Driver.

The automated suite never touches an adapter, so this script covers what only a driver can show:
opening a channel, TesterPresent and node status, a panel database, flashing, the CAN Logger with live
traffic, the Trace window, the UDS console, the transmit list, recording and replaying a file, the
activity scan, the ECU check after Disconnect and reconnecting.

    python tests/kvaser_end_to_end.py

It needs the Kvaser driver with its two virtual channels, and no other dummy ECU running on channel 1.
Nothing of yours is changed: it uses a temporary Configurations folder and temporary settings.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from PyQt5.QtCore import QSettings, Qt                                      # noqa: E402
from PyQt5.QtWidgets import QApplication                                    # noqa: E402

from canexpert import main_window as main                                   # noqa: E402
from canexpert.can_logger import CANLoggerWindow                            # noqa: E402
from canexpert.channel_setup import detect_bitrate                          # noqa: E402
from canexpert.flash_sequence import FlashProfile                           # noqa: E402
from canexpert.flashing import load_firmware                                # noqa: E402
from canexpert.recording import read_frames                                 # noqa: E402
from canexpert.transmit_window import default_row                           # noqa: E402
from canexpert.uds.observer import service_name                             # noqa: E402

APP = QApplication.instance() or QApplication([])
CONFIGURATION = {"name": "Kvaser check", "bitrate": 500000, "identifier_11_bit": True, "request_id": 0x7E0,
                 "response_id": 0x7E8, "timeout_ms": 5000, "extended_id": False,
                 "tester_present_interval_seconds": 0.5, "node_timeout_seconds": 2.0,
                 "database_family": "showcase"}
checks = []


def check(name, ok, detail=""):
    checks.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -> ' + str(detail)) if detail else ''}", flush=True)


def spin(predicate, timeout=10.0):
    """Run the GUI event loop until predicate() is true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def main_check():
    temp = Path(tempfile.mkdtemp())
    configurations = temp / "Configurations"
    configurations.mkdir()
    (configurations / "config_Kvaser check.json").write_text(json.dumps(CONFIGURATION), encoding="utf-8")
    dump = temp / "flashed.s19"
    settings = QSettings(str(temp / "settings.ini"), QSettings.IniFormat)

    ecu = subprocess.Popen([sys.executable, str(REPO / "dummy_ecu.py"), "--console", "--channel", "1",
                            "--dump", str(dump)], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
    time.sleep(2.0)
    check("dummy ECU running on Kvaser channel 1", ecu.poll() is None)

    patches = [patch.object(main, "CONFIG_DIR", configurations), patch.object(main, "app_settings", lambda: settings)]
    for item in patches:
        item.start()
    window = main.MainWindow()
    node_text = lambda: next(iter(window.node_items.values())).text(0) if window.node_items else ""  # noqa: E731
    try:
        window.refresh_channel_list()
        channel0 = next((c for c in window.can_channels if c["interface"] == "kvaser" and c.get("channel") == 0), None)
        check("Kvaser channel 0 detected", channel0 is not None, channel0)
        window.selected_channel_config = channel0
        window.config_list.setCurrentRow(0)
        window.on_config_selected(window.config_list.item(0))

        window.on_connect_clicked()
        check("Connect opened the adapter", window.can_bus is not None, window.status_label.text())
        check("panel database loaded", window.panel is not None and window.app_database is not None,
              Path(window.app_database["source_path"]).name if window.app_database else None)
        check("ECU answers TesterPresent (node Responding)", spin(lambda: "Responding" in node_text()), node_text())
        check("periodic frames received",
              spin(lambda: sum(1 for frame in list(window.frame_history) if frame[1] == "RX") > 5))

        logger = window.open_can_logger()
        check("CAN Logger opens while connected", isinstance(logger, CANLoggerWindow))
        logger.load_dbc_from_path(REPO / "DBC" / "dummy_ecu.dbc")
        logger.set_signal_plotted("EngineData.Temperature")
        samples = lambda: getattr(logger._series.get("EngineData.Temperature"), "n", 0)  # noqa: E731
        check("CAN Logger records a live DBC signal", spin(lambda: samples() > 3, 8), f"{samples()} samples")

        check("Flashing enabled by the panel script", spin(lambda: window._toolbar_actions["flashing"].isEnabled()))
        firmware = load_firmware(REPO / "examples" / "firmware" / "demo_app.hex")
        results = []
        with patch.object(main, "report_result", lambda parent, ok, text: results.append((ok, text))):
            window.start_flashing(firmware)
            finished = spin(lambda: results, 90)
        check("flashing finished", finished and results and results[0][0], results)
        check("ECU received the image byte for byte",
              spin(lambda: dump.exists(), 10) and load_firmware(dump).segments == firmware.segments)

        # The same image again, with the built-in sequence instead of the panel script
        local = temp / "demo_app.hex"
        local.write_bytes((REPO / "examples" / "firmware" / "demo_app.hex").read_bytes())
        dump.unlink(missing_ok=True)
        results.clear()
        with patch.object(main, "report_result", lambda parent, ok, text: results.append((ok, text))):
            window.start_built_in_flash(load_firmware(local), FlashProfile())
            finished = spin(lambda: results, 120)
        check("the built-in sequence flashed the ECU", finished and results and results[0][0], results)
        check("the built-in sequence wrote the same image",
              spin(lambda: dump.exists(), 10) and load_firmware(dump).segments == firmware.segments)
        report = local.with_suffix(".flash-report.txt")
        check("a flash report was written beside the firmware",
              report.exists() and "Result: complete" in report.read_text(encoding="utf-8"), str(report))

        window.disconnect_database()
        check("ECU check runs after Disconnect", window.ecu_monitor is not None)
        check("ECU still Responding while checked", spin(lambda: "Responding" in node_text(), 5), node_text())
        window.stop_ecu_monitor()
        check("nodes show Not checked once stopped", spin(lambda: "Not checked" in node_text(), 3), node_text())

        bitrate, report = detect_bitrate(channel0, candidates=(500000, 250000), listen_time=0.6)
        check("the channel setup finds the bit rate of the ECU's traffic", bitrate == 500000, report)

        window.on_connect_clicked()
        check("reconnect works", window.can_bus is not None, window.status_label.text())
        check("ECU Responding again", spin(lambda: "Responding" in node_text()), node_text())

        # Tier 3 on the adapter: padded frames, and a scan beside the running session
        sent = [data for _t, direction, can_id, data, _x in list(window.frame_history)
                if direction == "TX" and can_id == CONFIGURATION["request_id"]]
        check("TesterPresent goes out padded to 8 bytes", bool(sent) and all(len(data) == 8 for data in sent),
              [data.hex(" ") for data in sent[-2:]])
        scan = window.open_ecu_scan()
        scan.last_edit.setText("7E3")
        scan.identification_cb.setChecked(True)
        scanner = scan.start()
        check("the ECU scan runs beside the session", scanner is not None, scan.status.text())
        spin(lambda: scanner.isFinished() and scan.start_btn.isEnabled(), 30)
        found = [(item.request_id, item.response_id) for item in scan.responders]
        check("the scan finds the dummy ECU, and only it", found == [(0x7E0, 0x7E8)], found)
        check("the scan reads its VIN", bool(scan.responders) and
              scan.responders[0].identification.get(0xF190) == "WVWZZZ1KZAW000001",
              scan.responders[0].identification if scan.responders else None)
        scan.close()

        # The trace, the console and the transmit list on live traffic
        window.symbols.set_paths([str(REPO / "DBC" / "dummy_ecu.dbc")])   # the names every window shares
        trace = window.open_trace()
        check("Trace shows the ECU frames with their symbolic name",
              spin(lambda: (trace.flush(), any(trace.tree.topLevelItem(row).text(3) == "EngineData"
                                               for row in range(trace.tree.topLevelItemCount())))[1], 8),
              trace.status.text())

        console = window.open_uds_console()
        console.run(lambda uds: uds.RDBI(0xF190), "RDBI")
        check("UDS console reads the VIN over ISO-TP",
              spin(lambda: "WVWZZZ1KZAW000001" in console.log.toPlainText(), 10))
        console.read_dtcs()
        check("UDS console reads the fault memory", spin(lambda: console.dtc_table.rowCount() > 0, 10),
              f"{console.dtc_table.rowCount()} DTC(s)")

        transmit = window.open_transmit().messages
        transmit.rows = [default_row("Start", 0x200, b"\x01", 50)]
        transmit._fill_table()
        transmit.rows[0]["enabled"] = True
        check("transmit list sends cyclically",
              spin(lambda: (transmit.tick(), transmit.rows[0]["sent"] > 2)[1], 5), transmit.status.text())
        transmit.stop_all()

        check("the Trace assembles the diagnostic messages it saw",
              (trace.transport_btn.setChecked(True),
               spin(lambda: any(service_name(message.payload).startswith("ReadDataByIdentifier")
                                for message in trace.transport_messages()), 8))[1],
              trace.status.text())
        trace.transport_btn.setChecked(False)

        statistics = window.open_statistics()
        check("Statistics counts the live traffic",
              spin(lambda: (statistics.refresh(), statistics.statistics.total > 20)[1], 8),
              statistics.totals.text())
        check("Statistics works out the bus load and reports the bus state",
              "bus load " in statistics.totals.text() and "bus: " in statistics.totals.text(),
              statistics.totals.text())

        data = window.open_data()
        check("the Data window decodes the live signals",
              spin(lambda: len(data.signals.values) > 0, 8), data.status.text())

        simulation = window.open_transmit(nodes=True).nodes
        simulation._items["EngineData"].setCheckState(0, Qt.Checked)
        simulation.start_btn.setChecked(True)
        check("a simulated node puts its messages on the bus",
              spin(lambda: (simulation.tick(), simulation.messages["EngineData"]["sent"] > 2)[1], 5),
              simulation.status.text())
        simulation.start_btn.setChecked(False)

        # Recording the live measurement, then replaying the file with no bus at all
        recording = temp / "session.asc"
        with patch.object(main.QFileDialog, "getSaveFileName", return_value=(str(recording), "")):
            window.start_recording()
        written = lambda: window.recorder.count if window.recorder else 0             # noqa: E731
        check("the measurement is recorded to a file", spin(lambda: written() > 20, 15), written())
        window.stop_recording()
        check("the recorded file reads back", len(read_frames(recording)) > 20, str(recording))

        window.on_disconnect_clicked()
        trace.clear()
        with patch.object(main.QFileDialog, "getOpenFileName", return_value=(str(recording), "")):
            replay = window.replay_log()
        replay.speed_combo.setCurrentIndex(replay.speed_combo.count() - 1)            # as fast as possible
        replay.start_replay()
        check("the file replays into the Trace offline",
              spin(lambda: (trace.flush(), len(trace.frames) > 20)[1], 20), f"{len(trace.frames)} frames")
        replay.close()

    finally:
        window.close()
        APP.processEvents()
        ecu.terminate()
        try:
            output = ecu.communicate(timeout=5)[0]
        except subprocess.TimeoutExpired:
            ecu.kill()
            output = ecu.communicate()[0]
        for item in patches:
            item.stop()

    print("\n--- dummy ECU log (last 10 lines) ---")
    print("\n".join((output or "").strip().splitlines()[-10:]))
    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED:", "; ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_check())
