# CAN Expert

A Python-based CAN interface application using Qt for GUI and python-can. Supports **Kvaser**, **Vector**, and **IXXAT** USB CAN interfaces. Includes periodic TesterPresent, node monitoring, and dated XML panels linked to Python scripts.

## Features

- **Multiple interfaces**: Kvaser, Vector, IXXAT (via python-can)
- **Node monitoring**: Sends configured periodic TesterPresent requests, lists responding nodes, and marks lost nodes with a red cross
- **Form Designer**: CANoe Panel Designer-style editor with 20 controls (gauges, LEDs, multi-state indicators, switches, knobs, trends...), DBC signal drag-and-drop, align/distribute, grid snap, undo/redo, a Python editor and a Test mode against the simulated ECU
- **Python in place of CAPL**: per-control handler functions and `@on_message`, `@on_signal`, `@on_timer`, `@on_start`, `@on_stop` event procedures
- **Dynamic UI**: Buttons send CAN messages; values are read from CAN and displayed in real time
- **Configuration Management**: Save and load interface settings; each configuration can use a different CAN interface
- **Channel Selection & Bitrate**: Configure CAN channel and speed per interface
- **CAN Logger**: CANoe-style graphics window; tick DBC signals to add one graph per signal on a shared time axis, with follow, pause, fit, X/Y axis locks, measurement cursors, a hover crosshair with time/value readout and CSV export
- **Firmware flashing**: While connected, the **Flashing** toolbar button asks for an S-record or Intel HEX file, asks for confirmation and runs the database script's `Flashing(api, firmware)` with a progress dialog; a sample ISO 14229 sequence and test images (`examples/firmware/demo_app.s19` / `.hex`) are in `examples/`, and the Form Designer's Test panel can flash the simulated ECU

## Requirements

- Python 3.10+
- PyQt5
- python-can
- One of: Kvaser CAN driver, Vector driver (Windows), or IXXAT VCI (Windows) as needed for your hardware

## Installation

```bash
pip install -r requirements.txt
```

*(A standalone installer/executable build may be added in a future release.)*

## Usage

1. The application lists configurations and restores the last selected one.
2. Create a configuration or double-click one to edit its CAN IDs, TesterPresent interval, node timeout and optional database family.
3. Select a CAN receiver and click **Connect**. The newest matching database is loaded before communication starts.
4. Responding ECU IDs appear beneath the receiver. A timed-out node receives a red cross and returns to green when it responds again.
5. Use the panel's controls; their named Python callbacks handle CAN sends and UI updates.
6. Click **Disconnect** to stop reception, periodic requests and the panel script.

Use **Form Designer** to create pages, drag controls into place, assign unique script bindings, and write `DatabaseMainFunction(api)`. Name versioned databases `family_YYYY-MM-DD.xml`; place their scripts beside them as `family_YYYY-MM-DD_script.py`.

See [Requirements implementation](REQUIREMENTS_STATUS.md) for the complete configuration schema, script API, database selection rules and acceptance tests. A runnable panel/script pair is in `examples/`. Existing user databases are preserved.

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
    vin = api.uds.rdbi(0xF190)                    # UDS over ISO-TP, multi-frame replies supported
    api.ui.set_value("status", vin.decode(errors="replace") if vin else "No VIN")


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
| Diagnostic and communication management | `DSC` 0x10, `ER` 0x11, `SA` 0x27, `CC` 0x28, `TP` 0x3E, `ATP` 0x83, `CDTCS` 0x85, `ROE` 0x86, `LC` 0x87 |
| Data transmission | `RDBI` 0x22, `RMBA` 0x23, `RSDBI` 0x24, `RDBPI` 0x2A, `DDDI_DefineById` / `DDDI_DefineByAddress` / `DDDI_Clear` 0x2C, `WDBI` 0x2E, `WMBA` 0x3D |
| Stored data transmission | `CDTCI` 0x14, `RDTCI` 0x19 |
| Input/output control | `IOCBI` 0x2F |
| Remote activation of routine | `RC` 0x31 |
| Upload/download | `RD` 0x34, `RU` 0x35, `TD` 0x36, `RTE` 0x37, `RFT` 0x38 |
| Helpers | `UDS("22 F1 90")` (any request), `SecurityUnlock(level, compute_key)`, `ReadDTCs(mask)`, `StartRoutine` / `StopRoutine` / `RoutineResults`, `UdsLog(True)` |

Authentication (0x29) and SecuredDataTransmission (0x84) are not included.

See [Requirements implementation](REQUIREMENTS_STATUS.md#panel-scripts) for the full API and [Firmware flashing](REQUIREMENTS_STATUS.md#firmware-flashing) for `Flashing(api, firmware)`.

## File structure

```
CanExpert/
├── main.py                 # Entry point
├── panel.py                # Panel database selection, parsing and rendering
├── panel_controls.py       # Control library shared by the designer and running panels
├── panel_runtime.py        # Script API and runtime, CAN mailbox, config validation
├── uds_services.py         # ISO-TP transport, UDS services, S-record/Intel HEX loading
├── form_designer.py        # Form Designer (layout tools, undo/redo, Test mode)
├── code_editor.py          # Python editor: highlighting, line numbers, completion, UDS functions panel
├── uds_library.py          # ISO 14229 service functions for scripts (RDBI, WDBI, DSC, ...)
├── flashing_ui.py          # Firmware file, confirmation and progress dialogs for Flashing
├── can_logger.py           # CAN Logger: CANoe-style graphs, one strip per signal
├── diagnostic_window.py    # ODX Diagnostic Window
├── ui_common.py            # Settings, toolbar icons, collapsible panels
├── dummy_ecu.py            # Simulated UDS ECU for testing without a vehicle
├── Databases/              # family_YYYY-MM-DD.xml + _script.py
├── DBC/                    # Sample DBC files
├── Configurations/         # config_*.json
├── examples/
├── tests/
├── DOCUMENTATION.md        # Developer docs + architecture diagrams
└── requirements.txt
```

## Dummy ECU (no vehicle needed)

`dummy_ecu.py` simulates a UDS ECU on any python-can interface. With the Kvaser Virtual CAN Driver, channels 0 and 1 are connected to each other, so run the ECU on one channel and CAN Expert on the other:

```bash
python dummy_ecu.py --interface kvaser --channel 1
```

In CAN Expert, choose the **Dummy ECU** configuration (SERVER ID `7E0`, ECU ID `7E8`, database family `showcase`, the showcase panel with every control and `Flashing()`), select the receiver `[kvaser] Ch 0` and click **Connect**. Run only one dummy ECU per channel: it refuses to start when another ECU already answers there (two answering ECUs break security access and flashing). ECU `0x7E8` appears as responding, the ECU broadcasts `0x300` (temperature 0.1 °C and pressure 0.01 bar, big-endian) and `0x301` (status), and accepts `0x200` (`01` start, `02` stop) and `0x201` (bit 0: logging) commands.

The ECU supports sessions, TesterPresent, ECUReset, S3 timeout, ReadDataByIdentifier (`F186` session, `F187` part number, `F18C` serial, `F190` VIN, `F195` software version, `0100` uptime), WriteDataByIdentifier for `F190`, SecurityAccess level 1 (key = seed XOR `A5`, the same as the example `compute_key()`), ControlDTCSetting, CommunicationControl, ReadDTCInformation and ClearDiagnosticInformation. It also implements the complete flashing sequence of `examples/example_2026-09-18_script.py`: copy the example panel to `Databases/`, set the configuration's database family to `example`, connect, click **Flashing** and pick `examples/firmware/demo_app.hex` (or `.s19`, or any S-record or Intel HEX file). Afterwards `F195` reports `APP-FLASHED-<crc32>`, and `--dump flashed.s19` writes the received image back to a file.

To see live graphs, open **CAN Logger**, load `DBC/dummy_ecu.dbc` and tick `EngineData.Temperature`, `EngineData.Pressure` or the `EcuStatus` signals.

Segmented requests (TransferData blocks, VIN writes) get real ISO-TP flow control: by default the ECU asks for 8 consecutive frames per block with STmin 1 ms, so the CAN log shows a `30 08 01` flow control frame after every 8 frames. Change it with `--block-size` (0 = no limit) and `--stmin` (milliseconds, or `0xF1`-`0xF9` for 100-900 µs), and add `--fc-wait N` to send N flow control WAIT frames (`31 00 00`) before each ContinueToSend. A request longer than `--max-block` gets flow control overflow (`32 00 00`).

Other options: `--request-id`, `--response-id`, `--functional-id`, `--extended-ids` (29-bit), `--address-byte`, `--max-block`, `--erase-seconds`, `--no-broadcast`. Run `python dummy_ecu.py --help` for details.

## Tests

```bash
python -B -m unittest discover -s tests -v
```

The tests use python-can's virtual interface; no hardware is required.
