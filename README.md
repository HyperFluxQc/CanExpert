# CAN Expert

A Python-based CAN interface application using Qt for GUI and python-can. Supports **Kvaser**, **Vector**, and **IXXAT** USB CAN interfaces. Includes periodic TesterPresent, node monitoring, and dated XML panels linked to Python scripts.

## Features

- **Multiple interfaces**: Kvaser, Vector, IXXAT (via python-can)
- **Node monitoring**: Sends configured periodic TesterPresent requests, lists responding nodes, and marks lost nodes with a red cross
- **Application Database**: XML files define the UI (buttons, values, checkboxes, sliders) with CAN mappings
- **Dynamic UI**: Buttons send CAN messages; values are read from CAN and displayed in real time
- **Configuration Management**: Save and load interface settings; each configuration can use a different CAN interface
- **Channel Selection & Bitrate**: Configure CAN channel and speed per interface

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

## Legacy connection database (`connection_database.json`)

This file is used by the legacy RDBI discovery helper. The required connection workflow selects a dated database directly and does not require discovery.

Defines the UDS request and how to parse the response:

```json
{
  "uds_request": {
    "request_id": 2015,
    "response_id": 2024,
    "payload_hex": "03 22 F1 80",
    "timeout_seconds": 2.0
  },
  "response_parsing": {
    "database_id_bytes": [4, 5],
    "format": "decimal",
    "byte_order": "big"
  }
}
```

- **payload_hex**: UDS payload (e.g. `03 22 F1 80` = length 3, service 0x22, DID 0xF180)
- **database_id_bytes**: Byte indices in the response that contain the database ID
- **format**: `decimal`, `hex`, `bcd`, or `packed`

## Application Database (XML)

Place XML files in the `Databases/` folder, named by database ID (e.g. `1001.xml`).

### Example: `Databases/1001.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<application_database name="1001">
    <description>Temperature &amp; Control Panel</description>
    
    <buttons>
        <button id="1" label="Start System" can_id="0x200" data="01 00 00 00 00 00 00 00"/>
        <button id="2" label="Stop System" can_id="0x200" data="02 00 00 00 00 00 00 00"/>
    </buttons>
    
    <values>
        <value id="1" label="Temperature" unit="°C" can_id="0x300" byte_start="0" byte_length="2" scale="0.1" offset="0" type="float"/>
        <value id="2" label="Pressure" unit="bar" can_id="0x300" byte_start="2" byte_length="2" scale="0.01" offset="0" type="float"/>
    </values>
    
    <checkboxes>
        <checkbox id="1" label="Enable Logging" can_id="0x201" byte="0" bit="0"/>
    </checkboxes>
    
    <sliders>
        <slider id="1" label="Brightness" min="0" max="100" can_id="0x202" byte="0" type="integer"/>
    </sliders>
</application_database>
```

### Element attributes

| Element   | Attributes                                                                 | Description                          |
|-----------|----------------------------------------------------------------------------|--------------------------------------|
| **button**  | `can_id`, `data` (hex bytes)                                               | Sends CAN message when clicked       |
| **value**   | `can_id`, `byte_start`, `byte_length`, `scale`, `offset`, `type`            | Reads from CAN and displays          |
| **checkbox**| `can_id`, `byte`, `bit`                                                    | Sends bit state when toggled         |
| **slider**  | `can_id`, `byte`, `min`, `max`                                             | Sends byte value when changed       |

## File structure

```
CanExpert/
├── main.py
├── uds_discovery.py
├── database_loader.py
├── database_api.py
├── uds_services.py
├── form_designer.py (Form Designer dialog)
├── connection_database.json
├── Databases/
│   ├── 1001.xml
│   └── 1001_script.py
├── Configurations/
│   └── config_*.json
├── DOCUMENTATION.md   # Developer docs + architecture diagrams
└── requirements.txt
```
