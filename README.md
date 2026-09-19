# CAN Expert

A Python-based CAN interface application using Qt for GUI and python-can. Supports **Kvaser**, **Vector**, and **IXXAT** USB CAN interfaces. Includes periodic TesterPresent, node monitoring, and dated XML panels linked to Python scripts.

## Features

- **Multiple interfaces**: Kvaser, Vector, IXXAT (via python-can)
- **Node monitoring**: Sends configured periodic TesterPresent requests, lists responding nodes, and marks lost nodes with a red cross
- **Application Database**: XML files define the UI (buttons, values, checkboxes, sliders) with CAN mappings
- **Dynamic UI**: Buttons send CAN messages; values are read from CAN and displayed in real time
- **Configuration Management**: Save and load interface settings; each configuration can use a different CAN interface
- **Channel Selection & Bitrate**: Configure CAN channel and speed per interface
- **Firmware flashing**: While connected, the **Flashing** toolbar button sends an S-record or Intel HEX file to the database script's `Flashing(api, firmware)`; a sample ISO 14229 sequence is in `examples/`

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

Also available: `label`, `text_input`, `io_box`, `combo`, `gauge`, `progress_bar`, `led`.

## Panel scripts

```python
def DatabaseMainFunction(api):
    api.on("start", lambda value: api.can.send(0x200, [1]))
    vin = api.uds.rdbi(0xF190)  # UDS over ISO-TP, multi-frame replies supported
    api.ui.set_value("status", vin.decode(errors="replace") if vin else "No VIN")
```

See [Requirements implementation](REQUIREMENTS_STATUS.md#panel-scripts) for the full API and [Firmware flashing](REQUIREMENTS_STATUS.md#firmware-flashing) for `Flashing(api, firmware)`.

## File structure

```
CanExpert/
├── main.py                 # Entry point
├── panel.py                # Panel database selection, parsing and rendering
├── panel_runtime.py        # Script API and runtime, CAN mailbox, config validation
├── uds_services.py         # ISO-TP transport, UDS services, S-record/Intel HEX loading
├── form_designer.py        # Form Designer
├── can_logger.py           # CAN Logger (DBC decoding, graphs)
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

In CAN Expert, use a configuration with **SERVER ID** `7E0` and **ECU ID** `7E8`, select the receiver `[kvaser] Ch 0` and click **Connect**. ECU `0x7E8` appears as responding, the ECU broadcasts `0x300` (temperature 0.1 °C and pressure 0.01 bar, big-endian) and `0x301` (status), and accepts `0x200` (`01` start, `02` stop) and `0x201` (bit 0: logging) commands.

The ECU supports sessions, TesterPresent, ECUReset, S3 timeout, ReadDataByIdentifier (`F186` session, `F187` part number, `F18C` serial, `F190` VIN, `F195` software version, `0100` uptime), WriteDataByIdentifier for `F190`, SecurityAccess level 1 (key = seed XOR `A5`, the same as the example `compute_key()`), ControlDTCSetting, CommunicationControl, ReadDTCInformation and ClearDiagnosticInformation. It also implements the complete flashing sequence of `examples/example_2026-09-18_script.py`: copy the example panel to `Databases/`, set the configuration's database family to `example`, connect, click **Flashing** and pick any S-record or Intel HEX file. Afterwards `F195` reports `APP-FLASHED-<crc32>`, and `--dump flashed.s19` writes the received image back to a file.

Other options: `--request-id`, `--response-id`, `--functional-id`, `--extended-ids` (29-bit), `--address-byte`, `--max-block`, `--erase-seconds`, `--no-broadcast`. Run `python dummy_ecu.py --help` for details.

## Tests

```bash
python -B -m unittest discover -s tests -v
```

The tests use python-can's virtual interface; no hardware is required.
