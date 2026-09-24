# CAN Expert

A Python-based CAN interface application using Qt for GUI and python-can. Supports **Kvaser**, **Vector**, and **IXXAT** USB CAN interfaces. Includes periodic TesterPresent, node monitoring, and dated XML panels linked to Python scripts.

## Features

- **Multiple interfaces**: Kvaser, Vector, IXXAT (via python-can)
- **Trace window**: every frame of the session with its symbolic message name, expandable into decoded signals, absolute/relative/delta time, pass and stop filters by identifier, range or name, find, colour per identifier and CSV export, and a **transport view** that turns the ISO 15765-2 frames of a diagnostic request or response into one row with its service name and whole payload
- **Statistics**: frames, rate, average/min/max cycle time and bus load per identifier, with the totals for the bus - bus load, error frames and the controller state (error active, error passive, bus off) - plus freeze, filter and CSV export
- **Data window**: every signal of the symbol databases with the value it holds now, physical and raw, with its unit, age and count; signals that never arrived are listed too
- **Transmit window** with two tabs that keep sending until the window is closed: the **message list** - raw or database messages, sent once or cyclically, edited signal by signal, saved as JSON (CANoe's Interactive Generator) - and the **simulated nodes** - the messages of a database's sending nodes, sent at their cycle times as those ECUs would, a rest-bus simulation for the ECU on the bench
- **UDS Console**: every ISO 14229 service without an ODX file, built from the same catalogue the panel scripts use, the services of an ODX/PDX/CDD file with their answers decoded, session control, SecurityAccess (key from a mask or a `GenerateKeyEx` seed & key DLL) and a fault-memory tab (read, snapshot, extended data, clear) that spells out the DTC status bits and shows each DTC's code (P0101-00) and, from the ODX file, its text; the P2/P2* timing the ECU announces is picked up and honoured by every later request; a **Periodic & events** tab starts periodic data (0x2A) and ResponseOnEvent (0x86) and lists what the ECU then sends by itself
- **Recording and offline replay**: write the session to BLF/ASC/CSV and play a file back into every window with no bus attached; **markers** with a comment (Ctrl+M, `api.marker()`, `t.marker()`) show in the Trace and on the Logger's graphs and go into BLF/ASC/TRC recordings
- **Symbol databases**: one list of DBC files shared by the Trace, Data and Statistics windows, the CAN Logger and the Transmit window
- **ISO-TP settings** per configuration, kept by CAN Expert rather than in the configuration file: every frame padded to 8 bytes (0xCC by default, as most ECUs require), and the block size and STmin the tester asks of the ECU
- **Channel setup** per adapter channel: sample point and SJW turned into the adapter's bit timing, listen-only (Kvaser and Vector), a receive filter in the adapter, and bit rate detection that listens without disturbing the bus; configurations take any bit rate
- **Scan for ECUs**: TesterPresent over an 11-bit range or 29-bit normal fixed addresses, then the sessions each ECU accepts and its VIN, part and serial numbers and versions - beside a running measurement - with a configuration made from any ECU found
- **One measurement clock**: the Trace, the Logger, the Write window and the UDS Console show each frame's own time, absolute or relative to the start of the measurement; the Trace also filters by direction
- **Write window** for the script's output and its variables; scripts react to keys, error frames and the bus state
- **Test modules**: test cases in Python against the live bus (`@testcase`, `setup`/`teardown`, `t.check`, `t.require`, `t.expect_nrc`, `t.wait_for_frame`, `t.wait_for_signal` and the UDS functions), with a verdict per step as it runs, Stop, and an HTML and a JUnit XML report of every run; an example module checks the Dummy ECU
- **Status bar** with the bus state, the diagnostic session and security state read off the ECU's answers, and the last error; **keyboard shortcuts** (F9 connect, Ctrl+1...7 tool windows, F1 help at the window you are in) and an **About** box listing every library and adapter driver version
- **Panel pages as windows**: every page of a database is a workspace window of its own that can be tabbed, split and floated, fitted to its window or zoomed
- **CANoe-style window system**: the Database panel and the analysis windows live in a workspace where they tab together, split, and float as windows of their own, with drop guides while dragging (Qt Advanced Docking System); the arrangement is remembered and can be saved as named desktops
- **Node monitoring**: Sends configured periodic TesterPresent requests, lists responding nodes, and marks lost nodes with a red cross
- **Form Designer**: CANoe Panel Designer-style editor with 20 controls (gauges, LEDs, multi-state indicators, switches, knobs, trends...), DBC signal drag-and-drop, align/distribute, grid snap, undo/redo, a Python editor, a Database tab (ID and versions, name, description, DBC, contents, the configurations that use it), a menu bar with the usual shortcuts, and a Test mode against the simulated ECU
- **Python in place of CAPL**: per-control handler functions and `@on_message`, `@on_signal`, `@on_timer`, `@on_start`, `@on_stop` event procedures
- **Dynamic UI**: Buttons send CAN messages; values are read from CAN and displayed in real time
- **Configuration Management**: Save and load interface settings; each configuration can use a different CAN interface
- **Channel Selection & Bitrate**: Configure CAN channel and speed per interface
- **CAN Logger**: CANoe-style graphics window; tick DBC signals to add one graph per signal on a shared time axis, or draw several in one graph with a legend and a shared value axis, with small symbol buttons for clear, pause, follow, fit, combine, the X/Y axis locks and the measurement cursors (with min, max, mean and standard deviation between them), a hover crosshair with time/value readout, and export as CSV (a row per sample or a column per signal), MDF 4 or PNG - of everything, what is on screen or the cursor range; a cap on the samples kept per signal keeps long measurements in bounded memory. **Graph options** chooses how signals are drawn (step line, line with a dot per sample, or dots only), the follow window, and exact time and value ranges (for example 50.0134 s to 55.2455 s)
- **Firmware flashing**: While connected, the **Flashing** toolbar button asks for an S-record or Intel HEX file and how to flash it: the database script's `Flashing(api, firmware)`, or the **built-in ISO 14229 sequence** (sessions, DTCs and normal messages off, security access, erase, RequestDownload / TransferData / RequestTransferExit per segment, dependency check, reset and version read) whose every step is editable in a dialog and can be saved as a profile file. Both report progress with a Cancel button and leave a `<firmware>.flash-report.txt` beside the image. Test images (`examples/firmware/demo_app.s19` / `.hex`) and a sample script sequence are in `examples/`, and the Form Designer's Test panel can flash the simulated ECU

## Requirements

- Python 3.10+
- PyQt5
- PyQtAds (the workspace windows)
- python-can
- One of: Kvaser CAN driver, Vector driver (Windows), or IXXAT VCI (Windows) as needed for your hardware

## Installation

```bash
pip install -r requirements.txt
```

### Windows programs

`python tools/build_windows.py` (after `pip install -r requirements-build.txt`) builds **CanExpert.exe** and
**DummyECU.exe** into `dist/CanExpert`, with their icons and version, beside the `Configurations`,
`Databases`, `DBC`, `ODX`, `examples` and `docs` folders they use. It checks that both start and zips the
folder as `dist/CanExpert-<version>-windows.zip`: unzip it anywhere and run `CanExpert.exe`, no Python
needed. The adapter drivers (Kvaser, Vector, IXXAT) are still installed separately. CI builds the same zip
for every push to `main` and every `v*` tag (the *Windows programs* job's artifact).

## Usage

1. The application lists configurations and restores the last selected one. The CAN receiver used last is selected again and shown in **bold** (as is every receiver connected before), and CAN Expert starts checking it with TesterPresent straight away.
2. An ECU that answers appears under its receiver, together with the database that configuration can load: **double-click that entry** (or the receiver) to load it, exactly as **Connect** does.
3. Create a configuration or double-click one to edit its CAN IDs, TesterPresent interval, node timeout and optional database family.
4. Or select a CAN receiver yourself and click **Connect**. The newest matching database is loaded before communication starts.
5. Responding ECU IDs appear beneath the receiver. A timed-out node receives a red cross and returns to green when it responds again. After **Disconnect** the ECUs are still checked: CAN Expert keeps sending TesterPresent at the configuration's interval (the channel shows **[Checking ECUs]**), so each ECU stays **Responding** while it answers and shows **Lost connection** when it stops. Right-click the channel to stop, or to **Check ECUs** with the selected configuration without connecting; unchecked ECUs show **Not checked**. Connecting again hands the channel back to the session.
6. Use the panel's controls; their named Python callbacks handle CAN sends and UI updates.
7. Click **Disconnect** to stop reception, periodic requests and the panel script.

Use **Form Designer** to create pages, drag controls into place, assign unique script bindings, and write `DatabaseMainFunction(api)`. Name versioned databases `family_YYYY-MM-DD.xml`; place their scripts beside them as `family_YYYY-MM-DD_script.py`.

The [user manual](docs/USER_MANUAL.md) walks through the main window, the channel setup, the ECU scan, the symbol databases, the Trace window, Statistics, the Data window, the panels, the Form Designer, the scripts and their Write window, the CAN Logger, the Transmit window and its simulated nodes, the UDS Console and its ODX services, firmware flashing, recording and replay, and arranging the windows; the **?** button at the top right of the main window opens it in the application. See [Requirements implementation](docs/REQUIREMENTS_STATUS.md) for the complete configuration schema, script API, database selection rules and acceptance tests. A runnable panel/script pair is in `examples/`. Existing user databases are preserved.

## Application Database (XML)

Place panel databases in `Databases/`, named `family_YYYY-MM-DD.xml`, with an optional script beside each one named `family_YYYY-MM-DD_script.py`. The newest date for the configuration's database family is loaded on Connect. The Form Designer creates and edits these files; `examples/` contains a runnable pair.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<application_database name="engine">
  <description>Temperature &amp; Control Panel</description>
  <pages>
    <page name="Main">
      <button label="Start" binding_value="start" x="20" y="20"/>
      <value label="Status" binding_value="status" x="20" y="60"/>
      <value label="Temperature" unit="°C" can_id="0x300" byte_start="0" byte_length="2" scale="0.1" x="20" y="100"/>
      <checkbox label="Enable Logging" can_id="0x201" byte="0" bit="0" x="20" y="140"/>
      <slider label="Brightness" min="0" max="100" can_id="0x202" byte="0" x="20" y="180"/>
    </page>
  </pages>
</application_database>
```

Controls can be driven by a script binding (`binding_value`), a DBC signal (`binding_type="dbc"`, `binding_value="Message.Signal"`), or a raw CAN mapping:

| Element | Raw CAN attributes | Behaviour |
|---|---|---|
| **button** | `can_id`, `data` (hex bytes) | Sends the frame when clicked |
| **value** | `can_id`, `byte_start`, `byte_length`, `scale`, `offset`, `value_type` | Decodes and displays received data |
| **checkbox** | `can_id`, `byte`, `bit` | Sends the bit state when toggled |
| **slider** | `can_id`, `byte`, `min`, `max` | Sends the byte value when changed |

All control types:

| Category | Controls |
|---|---|
| Input | Button, Toggle Switch, Checkbox, Radio Buttons, Combo Box, Slider (horizontal/vertical), Knob, Numeric Up/Down, I/O Box |
| Display | Value Display (number format, decimals, DBC value-table text), 7-Segment Display, Gauge (warning/critical zones), Progress Bar (horizontal/vertical), LED (colours, blink), Multi-State Indicator (states from the DBC value table or `value=text:colour; ...`), Trend Graph, Output Box |
| Decoration | Label, Group Box, Picture |

Every control also has appearance properties (text colour, background, font size, bold, tooltip; inputs can be read-only).

`examples/showcase_2026-09-18.xml` uses every control with `DBC/dummy_ecu.dbc`.

## Panel scripts

Python replaces CAPL. A control calls the function named in its **Handler** property (double-click it in the
Form Designer to create the function), and decorators work like CAPL `on` procedures:

```python
def on_start_clicked(api, value):                 # Handler of the "start" button
    vin = RDBI(0xF190)                            # UDS over ISO-TP, multi-frame replies supported
    api.ui.set_value("status", vin.text if vin else "No VIN")


@on_signal("EngineData.Temperature")              # DBC signal changed
def temperature(api, value):
    api.ui.set_value("overheat", value > 80)


@on_message(0x301)                                # any received frame: frame.id, frame.data, frame.signals
def status(api, frame):
    api.log(frame.signals)


@on_timer(1.0)
def every_second(api):
    api.set_signal("EngineCmd.Speed", 1200)       # encode into the DBC message and send
```

Every ISO 14229 service is a script function, listed with its documentation in the **UDS functions**
panel beside the script editor (double-click to insert a call). `RDBI(0xFF99)` sends `22 FF 99`:

```python
result = RDBI(0xFF99)
if result:                                        # positive response
    api.ui.set_value("value", result.int)         # also .data, .text, .hex()
else:
    api.log(result.error)                         # "NRC 0x31 requestOutOfRange" or "no response"
```

| Functional unit (ISO 14229-1) | Functions |
|---|---|
| Diagnostic and communication management | `DSC` 0x10, `ER` 0x11, `SA` 0x27, `CC` 0x28, `AUTH` 0x29, `TP` 0x3E, `ATP` 0x83, `SDT` 0x84, `CDTCS` 0x85, `ROE` 0x86, `LC` 0x87 |
| Data transmission | `RDBI` 0x22, `RMBA` 0x23, `RSDBI` 0x24, `RDBPI` 0x2A, `DDDI_DefineById` / `DDDI_DefineByAddress` / `DDDI_Clear` 0x2C, `WDBI` 0x2E, `WMBA` 0x3D |
| Stored data transmission | `CDTCI` 0x14, `RDTCI` 0x19 |
| Input/output control | `IOCBI` 0x2F |
| Remote activation of routine | `RC` 0x31 |
| Upload/download | `RD` 0x34, `RU` 0x35, `TD` 0x36, `RTE` 0x37, `RFT` 0x38 |
| Helpers | `UDS("22 F1 90")` (any request), `SecurityUnlock(level, compute_key)`, `ReadDTCs(mask)`, `StartRoutine` / `StopRoutine` / `RoutineResults`, `UdsLog(True)` |

Authentication (0x29) and SecuredDataTransmission (0x84) take their records as bytes: the certificates and
the cryptography are yours. An ECU answering *busyRepeatRequest* (NRC 0x21) is asked again, three times at
most. What the ECU sends by itself after `RDBPI` and `ROE` reaches `@on_periodic_data(0xF201)` and
`@on_response_event(0x22)`, and the UDS Console's *Periodic & events* tab.

See [Requirements implementation](docs/REQUIREMENTS_STATUS.md#panel-scripts) for the full API and [Firmware flashing](docs/REQUIREMENTS_STATUS.md#firmware-flashing) for `Flashing(api, firmware)`.

## File structure

```
CanExpert/
├── main.py                     # Start CAN Expert (--smoke-test: only check that it can start)
├── CanExpert.spec              # PyInstaller: the Windows programs (tools/build_windows.py runs it)
├── dummy_ecu.py                # Start the Dummy ECU (window, or --console)
├── canexpert/
│   ├── main_window.py          # Main window: configurations, receivers and ECU nodes, Connect, Flashing
│   ├── main_tools.py, main_layouts.py, main_channels.py, main_session.py   # its parts (mixins)
│   ├── can_bus.py              # Opening a bus, CanWorker (reader + TesterPresent), mailbox
│   ├── config.py               # Configuration defaults, validation, UDS transport, files, dialog
│   ├── paths.py                # Where the data folders are (also next to a frozen executable)
│   ├── flashing.py             # S-record / Intel HEX files and the flashing dialogs
│   ├── can_logger.py           # CAN Logger: CANoe-style graphs, one strip per signal
│   ├── trace_window.py         # Trace: every frame, symbolic, filtered, exportable
│   ├── transmit_pane.py        # The Transmit window: the message list and the simulated nodes
│   ├── transmit_window.py      # Transmit list: one-shot and cyclic messages
│   ├── simulation_window.py    # Simulated nodes: a database's messages sent as those ECUs would
│   ├── uds_console.py          # UDS Console: every ISO 14229 service, ODX services, the fault memory
│   ├── testing/                # Test modules: runner, HTML/JUnit reports, the Test window
│   ├── recording.py            # Recording to BLF/ASC/CSV and offline replay
│   ├── symbols.py              # The DBC files every window shares
│   ├── workspace.py            # The workspace: the docking system the windows live in
│   ├── odx_services.py         # ODX files and the UDS Console's ODX tab
│   ├── ui_common.py            # Settings, toolbar icons, caption buttons, dock and splitter panels
│   ├── panel/                  # database.py (files), view.py (running panel), controls.py, runtime.py
│   ├── designer/               # form_designer.py, canvas.py, side_panels.py, code_editor.py
│   ├── uds/                    # isotp.py (ISO 15765-2), client.py (requests + ISO 14229 functions)
│   └── simulator/              # ecu.py (the simulated ECU), signals.py (its frames), dtc.py (its fault
│                               #   memory), window.py (its window; window_pages.py, window_tables.py,
│                               #   fields.py, widgets.py)
├── Configurations/             # config_<name>.json, one per configuration
├── Databases/                  # <family>_<YYYY-MM-DD>.xml and matching _script.py
├── DBC/, ODX/                  # Default folders for DBC and ODX/PDX files (ODX/dummy_ecu.odx-d: its DTC texts)
├── examples/                   # Runnable panel + script pair, demo firmware
├── TestModules/                # Test modules (dummy_ecu_checks.py); reports/ of their runs
├── docs/                       # USER_MANUAL.md, DOCUMENTATION.md, REQUIREMENTS_STATUS.md
├── tests/                      # Hardware-free acceptance, UDS and UI tests
└── requirements.txt
```

## Dummy ECU (no vehicle needed)

`dummy_ecu.py` simulates a UDS ECU on any python-can interface. With the Kvaser Virtual CAN Driver, channels 0 and 1 are connected to each other, so run the ECU on one channel and CAN Expert on the other. Double-click `dummy_ecu.py` (or run it without options) to open the **Dummy ECU** window:

```bash
python dummy_ecu.py
```

Pick the interface and channel (**Detect** lists them; `kvaser` channel `1` by default) and click **Connect**; **Disconnect** releases the channel. Only one dummy ECU runs per channel: a second one is refused (two answering ECUs break security access and flashing). The right side shows the ECU's session, security, transfer progress, downloaded memory and software version, with **Reset ECU** (back to the factory state) and a log of every request; tick **Show CAN frames** to see each frame, flow control included.

Every setting applies at once, even while connected, and is remembered for the next start. **Save profile...** / **Load profile...** keep sets of settings as JSON files.

| Tab | Settings |
|---|---|
| Addressing | Physical request ID (tester → ECU), functional request ID, response ID (ECU → tester), 29-bit identifiers, extended addressing byte, padding byte |
| Flow control | Block size (BS), STmin (ms or 100-900 µs), WAIT frames before each ContinueToSend and their interval, receive buffer (longer requests get flow control overflow) |
| UDS | P2 and P2* announced by DiagnosticSessionControl, response delay (NRC 0x78 beyond P2) and pending interval, S3 timeout, programming session only from extended, the slow/medium/fast rates of periodic data (0x2A) and whether it goes out as `6A` frames or on an ID of its own |
| Access | SecurityAccess levels - the main one and more - each with its seed length and key (seed XOR a mask, or a `GenerateKeyEx` seed & key DLL), wrong keys allowed and lockout delay; rules allowing a service only in some sessions or after unlocking a level |
| Flashing | Data bytes per TransferData (the ECU announces them + 2 as maxNumberOfBlockLength in its RequestDownload response), size of that length field, full blocks required, accepted dataFormatIdentifier values, required addressAndLengthFormatIdentifier, memory ranges, erase before download, erase and check routine IDs, erase time, RequestUpload, file for the flashed image; the bootloader's image check (none, CRC-32 as the check routine's option record, or in the image's last four bytes) and where the software version is read from the image |
| Signals | The DBC whose messages are sent (built-in: `DBC/dummy_ecu.dbc`), each message on or off with its period, and a generator per signal: constant, ramp, sine, square, random, counter, the engine running, logging, the session |
| Data | DIDs (writable or not, following a signal, readable in some sessions or after unlocking a level), DTCs with their status, faults, snapshot and extended data, the fault memory's confirmation and aging cycles and snapshot DIDs, forced negative responses |
| Errors | The chance of each transport error on purpose: refused, not answered, answered on another ID, a consecutive frame dropped, out of sequence or late |

**How big are the TransferData blocks?** The ECU decides: it announces maxNumberOfBlockLength (data + the `0x36` SID + the block counter) in its RequestDownload response, and the tester sends blocks of that size minus 2. Set **Data per TransferData** to 256 or 512 to get `74 20 01 02` or `74 20 02 02`; CAN Expert's `Flashing()` follows it.

In CAN Expert, choose the **Dummy ECU** configuration (SERVER ID `7E0`, ECU ID `7E8`, database family `showcase`, the showcase panel with every control and `Flashing()`), select the receiver `[kvaser] Ch 0` and click **Connect**. ECU `0x7E8` appears as responding, the ECU broadcasts `0x300` (temperature 0.1 °C and pressure 0.01 bar, big-endian) and `0x301` (status), and accepts `0x200` (`01` start, `02` stop) and `0x201` (bit 0: logging) commands.

The ECU supports sessions, TesterPresent, ECUReset, S3 timeout, ReadDataByIdentifier (`F186` session, `F187` part number, `F18C` serial, `F190` VIN, `F195` software version, `0100` uptime, `0101`/`0102` the live temperature and pressure, `F201`/`F202` the same for periodic data, `0200` readable only in the extended session after unlocking), WriteDataByIdentifier for `F190`, SecurityAccess (by default level 1, key = seed XOR `A5`, the same as the example `compute_key()`), ReadDataByPeriodicIdentifier, ResponseOnEvent (on a DID change or a DTC status change), InputOutputControlByIdentifier (on the DIDs that follow a signal: the application frames carry what the tester set), ReadMemoryByAddress, WriteMemoryByAddress, ControlDTCSetting, CommunicationControl, ReadDTCInformation and ClearDiagnosticInformation. A DTC's **Fault** box makes its status follow ISO 14229's life cycle - pending, confirmed after operation cycles, aged out - with the snapshot taken at the moment of the fault. It also implements the complete flashing sequence of `examples/example_2026-09-18_script.py`: connect, click **Flashing** and pick `examples/firmware/demo_app.hex` (or `.s19`, or any S-record or Intel HEX file). Afterwards `F195` reports `APP-FLASHED-<crc32>`, or the version found in the image; **Save memory as S-record...** (or **Save image to** on the Flashing tab) writes the received image to a file, and RequestUpload (`0x35`) reads it back over UDS. A flash that fails leaves the application invalid, and the next ECUReset starts the bootloader until a good flash.

To see live graphs, open **CAN Logger**, load `DBC/dummy_ecu.dbc` and tick `EngineData.Temperature`, `EngineData.Pressure` or the `EcuStatus` signals.

Without a window, add `--console`, optionally with a profile saved from the window:

```bash
python dummy_ecu.py --console --channel 1 --config my_ecu.json
```

Console options: `--interface`, `--channel`, `--bitrate`, `--request-id`, `--response-id`, `--functional-id`, `--extended-ids` (29-bit), `--address-byte`, `--max-block`, `--block-size`, `--stmin`, `--fc-wait`, `--erase-seconds`, `--dbc FILE`, `--no-broadcast`, `--dump FILE`, `--force`. Given without `--console`, they preset the window. Run `python dummy_ecu.py --help` for details.

## Tests

```bash
python -B -m unittest discover -s tests -v
```

The tests use python-can's virtual interface; no hardware is required. CI runs them on Ubuntu (Python 3.10
and 3.13) and Windows (3.10); a failure or a crash shows as an annotation on the pull request, with the
test and its traceback.

With the Kvaser driver installed, one more script drives the real main window against `dummy_ecu.py` over the two virtual channels — opening the adapter, node status, a panel, flashing, the CAN Logger, the activity scan, the ECU check and reconnecting. It uses a temporary configuration and temporary settings, so nothing of yours changes:

```bash
python tests/kvaser_end_to_end.py
```
