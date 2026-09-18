# Requirements implementation

This document describes the workflow implemented from `Requirements.docx`. CAN Expert is a focused CAN communication and Python panel application; full CANoe compatibility is not implied.

| Requirement | Implemented behavior | Acceptance coverage |
|---|---|---|
| 1 and 4.1 | Configurations, CAN receiver/node tree, CAN monitor, panel designer and script-controlled panels | End-to-end virtual CAN test and offscreen UI inspection |
| 2 | Configuration JSON files listed at startup | Configuration inventory test |
| 2.1 | Last selected configuration persisted in QSettings and restored; first available configuration is the fallback | Restart test |
| 2.2 | TesterPresent sent immediately on connection and at the configured interval, using the configured request ID, CAN identifier width and optional address byte | Repeated heartbeat and extended-address tests |
| 2.3 | Responding configured node IDs shown as children of the selected receiver; timeout produces a red cross; renewed traffic restores green status | Multiple-node, loss and recovery tests |
| 2.4 | Database selected, parsed and panel built before the hardware connection is opened; invalid/missing database leaves Connect available | Failure/recovery tests |
| 2.5 | Newest valid date in the matching database filename selected deterministically | Date and family-selection tests |
| 3 | Buttons, checkboxes, sliders, combo boxes and editable text/I/O controls emit named events; scripts update values, labels, LEDs and progress/gauge controls | Script control and panel tests |
| 4 | Drag/drop designer retains control geometry, names, script bindings, DBC bindings and legacy byte mappings across save/load | XML round-trip tests |
| 4.2 | Database Python scripts run on a background thread with startup, input, CAN and timer callbacks | Callback, timer, error isolation and cancellation tests |

## Configuration

Configurations live beside `main.py` in `Configurations/`, independent of the working directory. Double-click a configuration to edit it while disconnected. New settings are editable in the configuration dialog:

- `tester_present_interval_seconds`: positive interval, default 2 seconds.
- `node_timeout_seconds`: greater than the heartbeat interval, default 6 seconds.
- `request_id` and `response_id`: numeric CAN IDs, entered as hexadecimal in the dialog.
- `response_ids`: optional list of monitored ECU IDs. When omitted, use `response_id`; the default OBD request/response pair `0x7DF`/`0x7E8` monitors `0x7E8` through `0x7EF`.
- `database_family`: optional database stem/family. Empty selects the newest database across the database directory.
- `identifier_11_bit`: standard or extended CAN frames.
- `extended_id` and `extended_id_byte`: optional UDS extended-address prefix for the periodic TesterPresent frame.

Each connection uses a snapshot of one configuration and one selected receiver. Configurations can be edited for the next connection without changing an active session. Traffic from configured response IDs establishes node presence; lack of traffic for the configured timeout marks an established node lost. Unknown response IDs are still visible in the CAN monitor but are not added to the node tree. Monitoring continues after timeout to detect recovery.

## Database selection

Put XML databases and their scripts in `Databases/` beside the application. Use names such as:

```
engine_2026-09-01.xml
engine_2026-09-01_script.py
engine_2026-09-18.xml
engine_2026-09-18_script.py
```

With family `engine`, the second version is chosen. Supported date suffixes are `_YYYY-MM-DD` and `_YYYYMMDD`; invalid dates are skipped. Date-only stems are also accepted. Dates in filenames determine order, not modification times. Undated legacy files rank below dated files. Ties use filename order. A full stem can select an exact version. The script must share the selected XML stem and end in `_script.py`.

The required workflow no longer depends on receiving an RDBI database ID. `connection_database.json` and `uds_discovery.py` remain as legacy helpers, not a prerequisite for connecting. A connection can monitor nodes even before any node responds.

## Panel scripts

Set a control's **Binding type** to `script` and its **Binding** to a unique name such as `start`, `setpoint`, or `status`. If there is no binding, the displayed control label is its script name. Names are case-sensitive and must be unique across pages.

```python
def DatabaseMainFunction(api):
    api.ui.set_value("status", "Ready")
    api.on("start", lambda value: api.can.send(0x200, [1]))
    api.on("setpoint", lambda value: api.log(f"Setpoint: {value}"))
    api.on_can(lambda can_id, data: api.ui.set_value("status", data.hex()))
    api.every(1.0, lambda: api.log("Panel timer"))
```

- `api.on(name, callback)`: callback receives the control value. Buttons pass `True`, checkboxes a Boolean, sliders an integer, combo boxes their selected text. Editable fields submit when editing finishes; their selected value type controls conversion.
- `api.on_can(callback)`: callback receives `(arbitration_id, bytes)`.
- `api.every(seconds, callback)`: periodic callback with no arguments.
- `api.can.send(id, data)`: sends up to eight bytes using the active configuration's CAN identifier width; shorter script frames retain the legacy eight-byte padding behavior.
- `api.can.get_latest_messages()`: recent received messages.
- `api.ui.get_value(name)` and `api.ui.set_value(name, value)`: read a cached value or enqueue a GUI update. Scripts must not access Qt widgets directly.
- `api.log(text)`: application debug log.
- `api.running` and `api.sleep(seconds)`: cooperative cancellation for older loop-based scripts. Prefer callbacks and return from `DatabaseMainFunction`; a startup loop prevents that script's queued callbacks from being processed.

Callbacks run serially off the GUI thread. Exceptions are logged. Disconnect cancels Python execution, stops timers and reception, revokes the script bus, and closes the CAN adapter. A blocking native/DLL call cannot be forcibly interrupted; it must return on its own. It cannot use the revoked session bus to transmit afterward. Database scripts are ordinary local Python code and have the user's process permissions.

The connection already schedules TesterPresent; database scripts do not need to run their own TesterPresent loop. The existing UDS/firmware convenience helpers are not the new connection scheduler and are outside the requirements acceptance scope. This update does not certify firmware programming or full ISO-TP/ODX functionality; the diagnostic window explicitly limits sends to single-frame requests.

## Designer and bindings

New forms receive a date in their default filename. Preserve or supply that suffix when naming a version. Switching between Form and Database code preserves the code buffer. Saving validates widget fields and Python syntax. Widget kind and value type are separate, so value controls no longer disappear on save. Existing raw CAN IDs, payloads, scales, offsets and byte/bit positions are retained.

DBC bindings use `Message.Signal`. Relative DBC paths resolve against the XML directory. Numeric values are decoded for presentation; input controls encode their bound signal into the message while retaining other known values. Legacy checkbox/slider mappings retain other known bits/bytes in the same frame. Controls without a CAN/DBC mapping can operate entirely through scripts.

An example panel and script live under `examples/`. Copy them to `Databases/` and select family `example` to try them. They do not replace existing user databases automatically.

## Verification and limits

Run from the project directory:

```
python -B -m unittest discover -s tests -v
```

The tests isolate settings and databases in temporary directories, replace hardware discovery, and communicate through python-can's virtual interface. CI runs them on supported Python versions. No hardware is required or contacted by these tests. Adapter drivers, electrical bus conditions, and timing on an actual vehicle still require a hardware acceptance run.
