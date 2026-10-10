"""
The pictures of the user manual (docs/images/*.png), taken from the programs themselves: CAN Expert connected to
the Dummy ECU on a virtual CAN bus, its tool windows, the Form Designer, TestExpert and the Dummy ECU window.

    python tools/make_screenshots.py                  every picture
    python tools/make_screenshots.py trace transmit   the ones named (PICTURES)

Nothing is shown on screen - Qt's offscreen platform draws them, with the system's fonts - and nothing of yours
is read or changed: the settings, configurations, databases and reports are copies in a temporary folder.
"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
if sys.platform == "win32":                     # the offscreen platform finds no fonts on Windows by itself
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

import importlib  # noqa: E402
import pkgutil  # noqa: E402
import shutil  # noqa: E402
import struct  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import can  # noqa: E402
from PyQt5.QtCore import QCoreApplication, QEvent, QSettings, Qt  # noqa: E402
from PyQt5.QtGui import QFont  # noqa: E402
from PyQt5.QtWidgets import QAbstractButton, QApplication  # noqa: E402

IMAGES = ROOT / "docs" / "images"
CHANNEL = "user-manual-pictures"
SHOWCASE = "showcase_2026-09-18"


def settle(seconds=0.0):
    """Let the programs run - the bus, the timers, deferred deletes - for a while."""
    deadline = time.monotonic() + seconds
    while True:
        QApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


def save(widget, name, size=None):
    """Draw a window (at size) into docs/images/<name>.png."""
    if size:
        widget.resize(*size)
    widget.show()
    widget.raise_()
    widget.activateWindow()
    settle(0.3)
    path = IMAGES / f"{name}.png"
    widget.grab().save(str(path))
    print(f"{path.relative_to(ROOT)}  {widget.width()} x {widget.height()}")


class Studio:
    """The temporary world the pictures are taken in: copies of the folders, private settings, the Dummy ECU on a
    virtual bus, and the patches that point CAN Expert at them."""

    def __init__(self):
        self.folder = Path(tempfile.mkdtemp(prefix="canexpert-pictures-"))
        for name in ("Configurations", "Databases", "DBC", "ODX", "TestModules", "examples"):
            if (ROOT / name).is_dir():
                shutil.copytree(ROOT / name, self.folder / name)
        self.settings = QSettings(str(self.folder / "settings.ini"), QSettings.IniFormat)
        self.stop = threading.Event()
        self.buses = []
        self.patches = []

    def start(self):
        from canexpert import can_bus
        from canexpert import main_window as main
        from canexpert.designer import form_designer
        from canexpert.simulator.ecu import DummyEcu, EcuConfig
        from canexpert.test_expert import window as test_expert_window
        # Every module's app_settings() reads the private file (the real ones are the registry on Windows).
        modules = [module for name, module in sys.modules.items()
                   if name.startswith("canexpert") and hasattr(module, "app_settings")]
        self.patches = [patch.object(module, "app_settings", lambda: self.settings) for module in modules]
        self.patches += [
            patch.object(main, "CONFIG_DIR", self.folder / "Configurations"),
            patch.object(main, "DATABASES_DIR", self.folder / "Databases"),
            patch.object(form_designer, "CONFIG_DIR", self.folder / "Configurations"),
            patch.object(form_designer, "DATABASES_DIR", self.folder / "Databases"),
            patch.object(test_expert_window, "TEST_EXPERT_DIR", self.folder / "TestExpert"),
            patch.object(main.can, "detect_available_configs", return_value=[]),
            patch.object(can_bus, "create_can_bus", lambda *args, **kwargs: self.bus()),
        ]
        for item in self.patches:
            item.start()
        self.ecu = DummyEcu(self.bus(), EcuConfig(), log=lambda text: None)
        threading.Thread(target=self.ecu.serve, args=(self.stop,), daemon=True).start()
        return self

    def bus(self):
        bus = can.Bus(interface="virtual", channel=CHANNEL)
        self.buses.append(bus)
        return bus

    def close(self):
        self.stop.set()
        time.sleep(0.1)
        for bus in self.buses:
            bus.shutdown()
        for item in reversed(self.patches):
            item.stop()
        shutil.rmtree(self.folder, ignore_errors=True)


# --- the pictures -----------------------------------------------------------------------------------------------------

def start_engine(panel):
    """The showcase panel's Run switch on (the Dummy ECU warms up), its VIN read and the extended session."""
    panel.widgets["run"].click()
    panel.widgets["hello"].click()
    settle(0.5)
    radio = panel.widgets["session"]
    for button in radio.findChildren(QAbstractButton):
        if button.text() == "Extended":
            button.click()


def main_window_pictures(studio, wanted):
    """The main window connected to the Dummy ECU, and its tool windows fed with what the bus carried."""
    from canexpert import main_window as main
    from canexpert.can_logger import CANLoggerWindow
    from canexpert.trace_window import TraceWindow
    from canexpert.transmit_window import TransmitWindow, default_row
    from canexpert.uds_console import UdsConsoleWindow
    studio.settings.setValue("last_configuration", "Dummy ECU")
    window = main.MainWindow()
    window.selected_channel_config = {"interface": "virtual", "channel": CHANNEL}
    dbc = studio.folder / "DBC" / "dummy_ecu.dbc"
    window.symbols.set_paths([str(dbc)])
    window.resize(1280, 760)                    # every toolbar button in view
    window.show()
    settle(0.5)

    if "connect_problem" in wanted:            # a typo in a newer version of the panel: Connect says where
        typo = studio.folder / "Databases" / "showcase_2026-09-30.xml"
        text = (studio.folder / "Databases" / f"{SHOWCASE}.xml").read_text(encoding="utf-8")
        typo.write_text(text.replace('x="30" y="380"', 'x="3O" y="380"'), encoding="utf-8")
        shutil.copy(studio.folder / "Databases" / f"{SHOWCASE}_script.py",
                    studio.folder / "Databases" / "showcase_2026-09-30_script.py")
        window.on_connect_clicked()
        save(window.problems_dialog, "connect_problem", (900, 430))
        window.problems_dialog.close()
        typo.unlink()
        (studio.folder / "Databases" / "showcase_2026-09-30_script.py").unlink()

    window.on_connect_clicked()
    if window.problems_dialog is not None:
        window.problems_dialog.close()
    settle(1)
    start_engine(window.panel)
    settle(10)                                  # the engine warms up: the gauge and the trend move
    if "main_window" in wanted:
        save(window, "main_window")
    if "trace" in wanted:
        trace = TraceWindow(None, window.symbols, window.clock)
        for frame in list(window.frame_history)[-400:]:
            trace.add_frame(*frame)
        trace.flush()
        save(trace, "trace", (1000, 520))
        trace.close()
    if "can_logger" in wanted:
        logger = CANLoggerWindow(None, window.symbols, window.clock)
        for timestamp, direction, can_id, data, _extended in list(window.frame_history):
            if direction == "RX":
                logger.on_can_message(can_id, data, timestamp)
        for name in ("EngineData.Temperature", "EngineData.Pressure"):
            if name in logger._items:
                logger.set_signal_plotted(name)
        settle(0.3)
        logger.fit_all()
        save(logger, "can_logger", (1100, 620))
        logger.close()
    if "transmit" in wanted:
        transmit = TransmitWindow(None, window.symbols, window.send_can_message, studio.settings)
        transmit.rows = [default_row("Heartbeat", 0x100, b"\x01", 10), default_row("Wake up", 0x3F0, b"\x02\x10\x03", 100),
                         default_row("Once", 0x7E0, b"\x02\x3E\x00", 1000)]
        transmit.rows[0]["enabled"] = transmit.rows[1]["enabled"] = True
        transmit._fill_table()
        transmit._sync_cyclic()
        settle(3)
        transmit.refresh()
        save(transmit, "transmit", (960, 300))
        transmit.stop_all()
        transmit.close()
    if "uds_console" in wanted:
        console = UdsConsoleWindow(None, window.active_session,
                                   time_text=lambda t: window.clock.text(t, window.time_display))
        console.raw_edit.setText("10 03")                   # a raw request...
        console.send_raw()
        settle(1)
        service = next(item for group in range(console.service_tree.topLevelItemCount())
                       for item in (console.service_tree.topLevelItem(group).child(index) for index
                                    in range(console.service_tree.topLevelItem(group).childCount()))
                       if item.data(0, Qt.UserRole) == "RDBI")
        console.service_tree.setCurrentItem(service)        # ...and a service with its form: the VIN
        console._widgets[console._parameters[0].name][1].setText("0xF190")
        console.send_service()
        settle(1)
        console.raw_edit.setText("19 02 FF")
        console.send_raw()
        settle(1)
        save(console, "uds_console", (1000, 640))
        console.close()
    window.on_disconnect_clicked()
    settle(0.3)
    window.close()


def designer_pictures(studio, wanted):
    """The Form Designer with the showcase panel, its test window, and the check of a panel with typos."""
    from canexpert.designer.form_designer import FormDesigner
    designer = FormDesigner()
    designer.resize(1280, 760)
    designer.show()
    designer.load(studio.folder / "Databases" / f"{SHOWCASE}.xml")
    if designer.problems_dialog is not None:
        designer.problems_dialog.close()
    designer.canvas.set_selection([1])                          # the gauge: its properties on the right
    designer.design_splitter.setSizes([200, 720, 360])      # room for the properties' values
    designer.status.clearMessage()                              # (the temporary folder's path)
    if "form_designer" in wanted:
        save(designer, "form_designer")
    if "find_replace" in wanted:                                # Find (Ctrl+F) in the script
        from PyQt5.QtGui import QTextCursor
        designer.design_tabs.setCurrentWidget(designer.code_page)
        designer.code_editor.moveCursor(QTextCursor.Start)
        designer._text_command("find")
        bar = designer.code_pane.find_bar
        bar.find_edit.setText("api.ui.set_value")
        bar.find()
        designer.code_editor.centerCursor()
        save(designer, "find_replace", (1080, 640))
        bar.close_bar()
        designer.design_tabs.setCurrentWidget(designer.canvas)
    if "test_panel" in wanted:
        dialog = designer.test_panel()
        settle(1)
        start_engine(dialog.panel)
        settle(8)
        save(dialog, "test_panel", (1000, 760))
        dialog.close()
    if "panel_check" in wanted:
        typos = studio.folder / "typos"
        typos.mkdir(exist_ok=True)
        text = (studio.folder / "Databases" / f"{SHOWCASE}.xml").read_text(encoding="utf-8")   # ../DBC as before
        (typos / f"{SHOWCASE}.xml").write_text(text.replace('<led type="led" id="7"', '<lde type="led" id="7"'),
                                               encoding="utf-8")
        script = (studio.folder / "Databases" / f"{SHOWCASE}_script.py").read_text(encoding="utf-8")
        (typos / f"{SHOWCASE}_script.py").write_text(script.replace("RDBI(", "RBDI(", 1), encoding="utf-8")
        designer.load(typos / f"{SHOWCASE}.xml")
        save(designer.problems_dialog, "panel_check", (900, 460))
        designer.problems_dialog.close()
    designer.close()


def test_expert_picture(studio, wanted):
    """TestExpert after a run of the first tests against the Dummy ECU."""
    from canexpert.test_expert.window import TestExpertWindow
    from canexpert.ui_common import MemorySettings
    window = TestExpertWindow(MemorySettings())
    window.s3_test.setChecked(False)
    window.connect_ecu(studio.bus())
    window.request_id.setValue(0x7E0)
    window.response_id.setValue(0x7E8)
    names = list(window._items)[:14]
    window.run(names)
    deadline = time.monotonic() + 60
    while (window.report is None or not window.run_action.isEnabled()) and time.monotonic() < deadline:
        settle(0.1)
    window.log.setPlainText(window.log.toPlainText().replace(str(studio.folder), r"C:\CAN Expert"))
    save(window, "test_expert", (1180, 740))
    window.disconnect_ecu()
    window.close()


def dummy_ecu_picture(studio, wanted):
    """The Dummy ECU's own window."""
    from canexpert.simulator.window import DummyEcuWindow
    window = DummyEcuWindow()
    settle(0.5)
    save(window, "dummy_ecu", (1000, 640))
    window.close()


def variables_pictures(studio, wanted):
    """The calibration example: its Variables tab, and its structured variables read from the simulated ECU."""
    from canexpert.designer.form_designer import FormDesigner
    designer = FormDesigner()
    designer.resize(1180, 700)
    designer.show()
    designer.load(studio.folder / "examples" / "calibration_2026-09-30.xml")
    if designer.problems_dialog is not None:
        designer.problems_dialog.close()
    designer.status.clearMessage()
    if "variables_tab" in wanted:
        designer.design_tabs.setCurrentWidget(designer.variables_page)
        save(designer, "variables_tab")
    if "variables" in wanted:
        dialog = designer.test_panel()
        dialog.ecu.write_memory(0x10000, struct.pack("<34I", 25, 2, *(1000 + 25 * i for i in range(32))))
        settle(1)
        calib = next(widget for key, widget in dialog.panel.widgets.items()
                     if dialog.panel.definitions[key].get("structure") == "Calib Data")
        calib.buttons[0].click()                                    # Read
        settle(1)
        calib.tree.topLevelItem(2).setExpanded(True)                # FOC, element by element
        save(dialog, "variables", (820, 680))
        dialog.close()
    designer._mark_clean()
    designer.close()


PICTURES = {"connect_problem": main_window_pictures, "main_window": main_window_pictures,
            "trace": main_window_pictures, "can_logger": main_window_pictures, "transmit": main_window_pictures,
            "uds_console": main_window_pictures, "form_designer": designer_pictures, "find_replace": designer_pictures,
            "test_panel": designer_pictures, "panel_check": designer_pictures, "test_expert": test_expert_picture,
            "dummy_ecu": dummy_ecu_picture, "variables_tab": variables_pictures, "variables": variables_pictures}


def main(names):
    unknown = set(names) - set(PICTURES)
    if unknown:
        raise SystemExit(f"No such picture: {', '.join(sorted(unknown))}. There are: {', '.join(PICTURES)}")
    wanted = set(names or PICTURES)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    if sys.platform == "win32":
        app.setFont(QFont("Segoe UI", 9))
    from canexpert.ui_common import app_icon
    app.setWindowIcon(app_icon())
    import canexpert
    for info in pkgutil.walk_packages(canexpert.__path__, "canexpert."):   # all of them, before their
        try:                                                               # app_settings are patched
            importlib.import_module(info.name)
        except Exception as exc:
            print(f"{info.name} not loaded: {exc}")
    IMAGES.mkdir(parents=True, exist_ok=True)
    studio = Studio().start()
    try:
        for take in dict.fromkeys(PICTURES[name] for name in PICTURES if name in wanted):
            take(studio, wanted)
    finally:
        studio.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
