# CAN Expert – Developer Documentation

This document describes the architecture, threads and data flows of **CAN Expert** for developers who need to understand or extend the codebase. User-facing behaviour, the configuration schema and acceptance coverage are in [Requirements implementation](REQUIREMENTS_STATUS.md).

---

## 1. Overview

**CAN Expert** is a PyQt5 desktop application that:

- Connects to CAN hardware (**Kvaser**, **Vector**, **IXXAT**) via **python-can**
- Selects the newest dated **panel database** (`Databases/family_YYYY-MM-DD.xml`) for the active configuration and builds its UI before opening the adapter
- Sends periodic **TesterPresent** and shows responding ECUs beneath the selected receiver, marking lost nodes with a red cross
- Runs the panel's **Python script** (`DatabaseMainFunction(api)`) on a background thread with an API for CAN, UDS over ISO-TP, DLL calls and UI values
- Provides a **Form Designer**, a DBC-aware **CAN Logger**, and an ODX-driven **Diagnostic Window**

---

## 2. Module Map

```mermaid
flowchart LR
    main["main.py"]
    main --> panel["panel.py"]
    main --> panel_runtime["panel_runtime.py"]
    main --> form_designer["form_designer.py"]
    main --> can_logger["can_logger.py"]
    main --> diagnostic_window["diagnostic_window.py"]
    main --> ui_common["ui_common.py"]
    form_designer --> panel
    form_designer --> panel_runtime
    form_designer --> code_editor["code_editor.py"]
    code_editor --> uds_library["uds_library.py"]
    panel_runtime --> uds_library
    panel --> panel_controls["panel_controls.py"]
    form_designer --> panel_controls
    panel_runtime --> uds_services["uds_services.py"]
    diagnostic_window --> panel_runtime
    diagnostic_window --> uds_services
    can_logger --> ui_common
    form_designer --> ui_common
    diagnostic_window --> ui_common
    dummy_ecu["dummy_ecu.py"] --> uds_services
    dummy_ecu_window["dummy_ecu_window.py"] --> dummy_ecu
    dummy_ecu_window --> ui_common
```

| Module | Role |
|--------|------|
| **main.py** | Main window, configuration list and dialog, receiver/node tree, Connect/Disconnect, Flashing button and progress dialog, `CanWorker` (hardware reader + TesterPresent, also used for the ECU check that keeps node status live after Disconnect), `ChannelActivityScanner`, CAN and debug logs, theme. |
| **panel.py** | Panel databases: `select_database()` (newest dated file per family), XML → dict parsing (`parse_widget()`), `decode_value_from_can_data()`, and `PanelView`, which renders pages and controls, decodes raw/DBC-bound values and emits `control_changed(name, value)`. |
| **panel_controls.py** | Control registry shared by the designer preview and running panels: per control its palette entry, properties, construction, value display and input events; painted controls (gauge, LED, multi-state indicator, toggle switch, knob, 7-segment display, trend); `format_value()` and appearance handling. |
| **panel_runtime.py** | `DatabaseAPI` given to scripts (`api.on/on_can/every`, `api.signal/set_signal/send_message`, `api.can`, `api.uds`, `api.dll`, `api.ui`, `api.log`, `api.progress`), `SCRIPT_TEMPLATE`, `ScriptRuntime` (script thread, handler functions, CAPL-style event decorators, timers, flashing, cancellation), `ReceiveMailbox` (bus facade fed by `CanWorker`), `validate_config()`. |
| **uds_services.py** | ISO-TP transport (single, first, consecutive and flow-control frames), `uds_request()` and UDS helpers, `load_firmware()` for S-record/Intel HEX files, flashing helper. |
| **form_designer.py** | Panel designer: palette, DBC symbol tree (drag signals onto the form), canvas with multi-select, align/distribute, grid snap, resize handle, z-order, clipboard, keyboard and undo/redo; schema-driven property editor; handler stubs; Test mode against the simulated ECU; saves XML + `_script.py`. |
| **code_editor.py** | Python editor for panel scripts: syntax highlighting, line numbers, auto-indent, completion (API, control names, DBC signals, UDS functions), syntax check; `UdsFunctionPanel` lists the UDS functions by ISO 14229 functional unit and inserts calls. |
| **uds_library.py** | ISO 14229-1 service functions for scripts (`RDBI`, `WDBI`, `DSC`, `SA`, `RC`, `RD`/`TD`/`RTE`, ... all services except 0x29 and 0x84) returning `UdsResult`; injected into script globals by `ScriptRuntime`; `NRC_NAMES`. |
| **can_logger.py** | CANoe-style graphics window: DBC signal tree (filter, live values), one strip chart per ticked signal on a shared time axis, follow/pause/fit, Lock X / Lock Y for mouse zoom and pan, two measurement cursors with per-signal values and Δ, a dotted hover crosshair with a time/value readout, CSV export of all decoded data. |
| **diagnostic_window.py** | Loads ODX/PDX/CDD, builds request forms, runs UDS exchanges on a background thread, monitors request/response IDs. |
| **flashing_ui.py** | Flashing dialogs shared by the main window and the designer's Test panel: choose and load a firmware file, confirm with its address ranges, progress dialog with Cancel, result message. |
| **dummy_ecu.py** | Stand-alone simulated UDS ECU (sessions, security, DIDs, DTCs, flashing with RequestDownload/RequestUpload, ISO-TP flow control, periodic frames) for Kvaser virtual channels or any python-can interface. `EcuConfig` holds every setting and is read for each frame, so changes apply while running; `load_profile()`/`save_profile()` store it as JSON; `main()` opens the window, or runs headless with `--console`. |
| **dummy_ecu_window.py** | Dummy ECU window: connection (interface, channel detection, bit rate, Connect/Disconnect with the one-ECU-per-channel lock), settings tabs (addressing, flow control, UDS timing and security, flashing, periodic frames) applied live and remembered in QSettings, ECU status, log with an optional frame trace, JSON profiles. |
| **ui_common.py** | Shared Qt helpers: `app_settings()` (persistent QSettings, migrating the legacy `EZCan2/KvaserCAN` store once), `toolbar_icon()`, and `SplitterPanel` (collapsible titled panel). |

---

## 3. Files and Folders

```
CanExpert/
├── main.py                 # Entry point: python main.py
├── Configurations/         # config_<name>.json, one per configuration
├── Databases/              # <family>_<YYYY-MM-DD>.xml and matching _script.py
├── examples/               # Runnable panel + script pair (copy to Databases/)
├── DBC/, ODX/              # Default folders for DBC and ODX/PDX files (sample DBCs in DBC/)
├── tests/                  # Hardware-free acceptance and UDS transport tests
└── *.py                    # Modules listed above
```

---

## 4. Connect Flow

```mermaid
sequenceDiagram
    participant User
    participant MainWindow
    participant Loader as panel
    participant Worker as CanWorker
    participant Runtime as ScriptRuntime

    User->>MainWindow: Connect
    MainWindow->>MainWindow: validate_config(active config)
    MainWindow->>Loader: load_application_database(family)
    Loader-->>MainWindow: newest dated panel (or None → stay disconnected)
    MainWindow->>MainWindow: build PanelView
    MainWindow->>MainWindow: create_can_bus(interface, channel, bitrate)
    MainWindow->>Worker: start (bus, config) + add_mailbox(script mailbox)
    MainWindow->>Runtime: start(<stem>_script.py)
    loop while connected
        Worker->>Worker: TesterPresent every interval
        Worker-->>MainWindow: message_received (node tree, log, panel, tool windows)
        Worker-->>Runtime: frames via ReceiveMailbox
    end
```

Any failure before or during start-up calls `on_disconnect_clicked()`, which leaves Connect available. A `session_generation` counter discards signals from a previous session.

---

## 5. Threads and the Receive Path

`CanWorker` is the **only** reader of the hardware bus. Every received data frame is:

1. emitted to the GUI thread (`message_received`) for the node tree, CAN log, panel decoding, CAN Logger and Diagnostic Window, and
2. pushed into each registered `ReceiveMailbox` (`CanWorker.add_mailbox`).

A mailbox acts as a bus for code running off the GUI thread: `send()` goes straight to the adapter, `recv()` reads the mailbox queue. The panel script owns one mailbox for the whole session; each Diagnostic Window request registers a private mailbox for the duration of its exchange, so the two never compete for replies.

**UDS exchanges** (`uds_services.uds_request`):

- flush the mailbox first, so a reply queued before the request cannot answer it;
- run inside `mailbox.transaction()`; while any mailbox is in a transaction `CanWorker` defers its TesterPresent, because a single frame interleaved with a multi-frame request would abort it on the ECU;
- send the request as ISO-TP (ISO 15765-2): a single frame, or a first frame and consecutive frames paced by the ECU's flow control. A new ContinueToSend is awaited after every block of BS frames, and consecutive frames are at least STmin apart (timed with `perf_counter`, since `time.sleep()` can overshoot a 1 ms STmin by ~15 ms on Windows). WAIT restarts the N_Bs timeout (1 s, at most 16 WAITs), and overflow, an invalid flow status or no flow control abort with `IsoTpError`;
- receive the reply, answering a first frame with flow control (BS 0, STmin 0: the whole reply at once); a first frame that announces a length a single frame could carry is ignored, a new single or first frame replaces an unfinished message, and a wrong sequence number or more than N_Cr (1 s) between consecutive frames aborts;
- messages longer than 4095 bytes use the ISO 15765-2:2016 escape sequence (first-frame length 0, then 32 bits) in both directions;
- skip unrelated replies (e.g. TesterPresent) and extend the wait on NRC 0x78 (response pending).

Identifier size (11/29-bit) and the optional extended-address byte come from the configuration and apply to every frame.

---

## 6. Panel Scripts

`ScriptRuntime.start()` compiles `<stem>_script.py` and runs it on a daemon thread:

```mermaid
flowchart LR
    A["exec script"] --> B["DatabaseMainFunction(api)"]
    B --> C{"event loop"}
    C -->|control event| D["api.on callbacks"]
    C -->|CAN frame| E["api.on_can callbacks"]
    C -->|timer due| F["api.every callbacks"]
    D & E & F --> C
```

- Callbacks run serially; exceptions are logged and do not stop the loop.
- `api.ui.set_value()` emits `value_changed`, handled on the GUI thread by `PanelView.set_value()`.
- Disconnect sets the stop event (a `sys.settrace` hook raises inside Python code), closes the mailbox and revokes the script's bus. Blocking native calls cannot be interrupted.

Script API summary (see [Requirements implementation](REQUIREMENTS_STATUS.md#panel-scripts) for details):

| Call | Purpose |
|------|---------|
| `api.on(name, cb)`, `api.on_can(cb)`, `api.every(s, cb)` | Register callbacks |
| `api.can.send(id, data)`, `api.can.get_latest_messages()` | Raw CAN |
| `api.uds.request(payload)` | Any UDS request; returns positive or negative reply, or `None` |
| `api.uds.tester_present()`, `api.uds.rdbi(did)` | Common services (`rdbi` returns the data record without the DID echo) |
| `api.uds.request_download(fmt, addr, size)`, `api.uds.transfer_data(seq, data)`, `api.uds.request_transfer_exit()`, `api.uds.transfer_data_from_file(path, packet_size)` | Flashing |
| `api.progress(done, total, message)`, `api.flash_cancelled` | Flashing progress and cancellation |
| `api.dll.load(path)`, `api.dll.call(path, name, *args)` | Native libraries |
| `api.ui.get_value(name)`, `api.ui.set_value(name, value)`, `api.log(text)` | UI and logging |

UDS calls use the configuration's request/response IDs and its **UDS response timeout** (`timeout_ms`) unless a timeout is passed.

---

## 7. Firmware Flashing

```mermaid
sequenceDiagram
    participant User
    participant MainWindow
    participant Runtime as ScriptRuntime (script thread)
    participant ECU

    Runtime-->>MainWindow: flashing_available(True) after exec if Flashing() exists
    User->>MainWindow: Flashing button, choose .s19/.hex
    MainWindow->>MainWindow: load_firmware() (checksums, merged segments), confirm
    MainWindow->>Runtime: start_flash(firmware) → "flash" event
    Runtime->>ECU: Flashing(api, firmware): UDS over ISO-TP
    Runtime-->>MainWindow: flash_progress(done, total, text)
    Runtime-->>MainWindow: flash_finished(ok, message)
```

The button is visible only while connected and enabled only when the script defines `Flashing`. Flashing runs on the script thread, so other script callbacks wait until it finishes. Cancel sets `api.flash_cancelled`; disconnecting stops the script. The TesterPresent heartbeat keeps running between requests, which keeps the programming session alive. See [Firmware flashing](REQUIREMENTS_STATUS.md#firmware-flashing) for the sample ISO 14229 sequence.

---

## 8. Panel Database Format

```xml
<application_database name="engine" dbc_path="../DBC/engine.dbc">
  <description>...</description>
  <pages>
    <page name="Main">
      <button label="Start" binding_value="start" x="10" y="10"/>
      <value label="Status" binding_value="status" x="10" y="50"/>
      <value label="RPM" binding_type="dbc" binding_value="EngineData.RPM" x="10" y="90"/>
      <slider label="Fan" can_id="0x202" byte="0" min="0" max="100" x="10" y="130"/>
    </page>
  </pages>
</application_database>
```

Control types (see `panel_controls.CONTROLS`): `button`, `switch`, `checkbox`, `radio`, `combo`, `slider`, `knob`, `spin`, `io_box`, `text_input`, `value`, `display`, `gauge`, `progress_bar`, `led`, `indicator`, `trend`, `output`, `label`, `group_box`, `picture`. Controls may be driven by a script binding, a DBC `Message.Signal` binding (the panel takes the signal's unit and value table), or legacy raw `can_id`/byte/bit mappings. A `handler` attribute names the script function an input calls. Element order within a page is the z-order. `panel.parse_widget()` parses the common attributes; control-specific attributes are kept as-is and interpreted by `panel_controls`.

---

## 9. Settings

`ui_common.app_settings()` returns `QSettings("CanExpert", "CanExpert")`. On first use it copies any keys saved under the previous `EZCan2/KvaserCAN` name, so existing theme and last-configuration choices survive the rename.

---

## 10. Testing

```
python -B -m unittest discover -s tests -v
```

- `tests/test_requirements.py`: end-to-end sessions over python-can's virtual interface (configuration restore, heartbeat, node loss/recovery, the ECU check after Disconnect, database selection, scripts, designer round-trip, multi-frame Diagnostic Window exchange, Flashing button).
- `tests/test_uds_services.py`: ISO-TP flow control frame by frame (block size and STmin, WAIT, overflow, invalid flow status, N_Bs timeout, flow control after every received block, unexpected and invalid frames, the escape sequence), ISO-TP and UDS against a simulated ECU (stale-frame flush, multi-frame requests/replies, response pending, 29-bit IDs with address byte, RequestDownload encoding, heartbeat deferral flag), S-record/Intel HEX parsing, and the example `Flashing()` against a simulated bootloader.

- `tests/test_dummy_ecu.py`: the simulated ECU's session, security, functional addressing, S3 timeout, DTC, flow control (WAIT, block size, STmin, overflow) and flashing behaviour, and its settings: RequestDownload formats, memory ranges, block length and full blocks, RequestUpload read-back, security level/seed/mask, P2/P2* and response pending on a slow response.
- `tests/test_dummy_ecu_window.py`: the Dummy ECU window connecting and disconnecting on a virtual bus (channel lock included), settings applied while connected, invalid text fields not applied, the log and frame trace, profiles and remembered settings.

No hardware is contacted. For a manual end-to-end check, run `python dummy_ecu.py`, connect it to Kvaser virtual channel 1, and connect CAN Expert to Kvaser virtual channel 0. Adapter drivers, bus electrical conditions and ECU timing still need a hardware acceptance run.
