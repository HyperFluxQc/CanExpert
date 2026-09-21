# Requirements implementation

This document describes the workflow implemented from `Requirements.docx`. CAN Expert is a focused CAN communication and Python panel application; full CANoe compatibility is not implied.

| Requirement | Implemented behavior | Acceptance coverage |
|---|---|---|
| 1 and 4.1 | Configurations, CAN receiver/node tree, CAN monitor, panel designer and script-controlled panels | End-to-end virtual CAN test and offscreen UI inspection |
| 2 | Configuration JSON files listed at startup | Configuration inventory test |
| 2.1 | Last selected configuration persisted in QSettings and restored; first available configuration is the fallback | Restart test |
| 2.2 | TesterPresent sent immediately on connection and at the configured interval, using the configured request ID, CAN identifier width and optional address byte | Repeated heartbeat and extended-address tests |
| 2.3 | Responding configured node IDs shown as children of the selected receiver; timeout produces a red cross; renewed traffic restores green status. After Disconnect (or with right-click **Check ECUs**) TesterPresent continues on the channel, so the status keeps following the ECU's replies; right-click **Stop checking ECUs** ends it (nodes then show Not checked) | Multiple-node, loss and recovery tests; ECU check after disconnect (responding, silent, back, stopped, handed back to a session) |
| 2.3.1 | The receiver used last is remembered and selected at the next start, receivers connected before are shown in bold, and its ECUs are checked with TesterPresent immediately; a responding ECU also lists the database the active configuration would load, which double-clicking loads exactly as Connect does | Remembered-channel restart test; database entry and double-click test |
| 2.4 | Database selected, parsed and panel built before the hardware connection is opened; invalid/missing database leaves Connect available | Failure/recovery tests |
| 2.5 | Newest valid date in the matching database filename selected deterministically | Date and family-selection tests |
| 3 | Buttons, checkboxes, sliders, combo boxes and editable text/I/O controls emit named events; scripts update values, labels, LEDs and progress/gauge controls | Script control and panel tests |
| 4 | Drag/drop designer retains control geometry, names, script bindings, DBC bindings and legacy byte mappings across save/load | XML round-trip tests |
| 4.2 | Database Python scripts run on a background thread with startup, input, CAN and timer callbacks | Callback, timer, error isolation and cancellation tests |

## Analysis and diagnostics beyond the requirements

These were added for bench and vehicle work; none of them changes the configuration files, the panel
databases or the panel scripts. Anything that has to be remembered lives in the application settings or
in a file of its own.

- Every frame, from the session, the ECU check or a replayed file, goes through one path
  that keeps the last 20000 frames, writes the optional recording and feeds every open tool window.
  Received frames carry the adapter's timestamp.
- **Trace window**: symbolic names and decoded signals from the shared symbol databases, absolute,
  relative and delta time, pass/stop filters, find and CSV export, and a transport view that shows the
  diagnostic messages the ISO 15765-2 frames carry, one row each, with their service names.
- **Statistics**: per identifier the count, rate, average/min/max cycle time, share of the bus and last
  data, and for the bus the total load, the error frames and the controller state (error active, error
  passive, bus off).
- **Data window**: every signal of the symbol databases with the value it holds now, physical and raw,
  with its unit, age and count.
- **Transmit list**: raw or database messages, one-shot or cyclic, edited signal by signal.
- **Simulated nodes**: the messages of a database's sending nodes, put on the bus at their cycle times,
  so an ECU on the bench sees the traffic it expects.
- **UDS Console**: every ISO 14229 service (the same catalogue the scripts use) with a generated request
  form, session control, SecurityAccess (mask or `GenerateKeyEx` seed & key DLL) and a fault-memory tab,
  without an ODX file. The P2/P2* timing an ECU announces is honoured by the requests that follow.
- **Recording and replay**: BLF, ASC, CSV, LOG or TRC through python-can; a replayed file reaches the
  windows offline and never touches a bus.
- **Symbol databases**: one DBC list shared by the Trace window, the CAN Logger and the transmit list.
- **Workspace**: the tool windows are panes of the main window; the arrangement is saved and can be kept
  as named desktops. Every page of a panel database is a window of it, zoomed or fitted to the window.
- **Channel setup** (per adapter channel): sample point and SJW, listen-only, a receive filter in the
  adapter, and bit rate detection by listening at each common rate.
- **ISO-TP** (per configuration, in the settings): padding of every frame and the flow control the tester
  asks for.
- **Scan for ECUs**: TesterPresent over an 11-bit range or 29-bit normal fixed addresses, then the sessions
  each ECU accepts and its identification DIDs, beside a running measurement.
- **One measurement clock**: every window shows a frame's own timestamp, absolute or relative to the start
  of the measurement; the CAN monitor filters like the Trace, which also filters by direction.
- **System variables** shared by the script, the windows and the user; a **Write window** for the script's
  output and variables; script events for keys, error frames and the bus state.
- **CAN Logger exports**: CSV with a row per sample or a column per signal, MDF 4, PNG, of everything, the
  screen or the cursor range; statistics between the cursors; a cap on the samples kept.
- **Dummy ECU data**: editable DIDs, DTCs with snapshot and extended data, forced negative responses, and
  several simulated ECUs on one channel.

## Configuration

Configurations live beside `main.py` in `Configurations/`, independent of the working directory. Double-click a configuration to edit it while disconnected. New settings are editable in the configuration dialog:

- `tester_present_interval_seconds`: positive interval, default 0.5 seconds.
- `node_timeout_seconds`: greater than the heartbeat interval, default 2 seconds. A node is shown as lost this long after its last frame; keep several heartbeats inside the window so one missed response is tolerated.
- `request_id` and `response_id`: numeric CAN IDs, entered as hexadecimal in the dialog. With the OBD functional request ID `0x7DF` and an ECU response ID `0x7E8`-`0x7EF`, TesterPresent monitoring stays functional while UDS requests from scripts, flashing and the Diagnostic Window address the ECU physically at the response ID minus 8 (for example `0x7E0`), because multi-frame requests may not use a functional address (ISO 15765-2/-4).
- `response_ids`: optional list of monitored ECU IDs. When omitted, use `response_id`; the default OBD request/response pair `0x7DF`/`0x7E8` monitors `0x7E8` through `0x7EF`.
- `database_family`: optional database stem/family. Empty selects the newest database across the database directory.
- `identifier_11_bit`: standard or extended CAN frames.
- `extended_id` and `extended_id_byte`: optional UDS extended-address prefix for TesterPresent and every UDS frame sent by scripts or the Diagnostic Window.
- `timeout_ms`: UDS response timeout for script and Diagnostic Window requests, default 5000 ms.

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

The connection workflow does not read a database ID from the ECU; the earlier RDBI discovery helpers have been removed. A connection can monitor nodes even before any node responds.

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

- **Handler property**: a control calls the script function named in its Handler (the Form Designer creates `def on_<name>_<event>(api, value):` when you double-click the control). A function whose first parameter is named `api` receives the script API; other parameters receive the event's values.
- **Event decorators** (CAPL `on` procedures): `@on_start` and `@on_stop` (connect/disconnect; `@on_stop` runs while the bus is still open), `@on_timer(seconds)`, `@on_message(0x300)` or `@on_message("MessageName")` (argument `frame` with `id`, `data`, `signals`), `@on_signal("Message.Signal")` (called when the value changes; `every_update=True` for every frame), `@on_control("name")`, `@on_sysvar("Namespace::Name")` (a system variable changed; no name: any), `@on_key("a", "F5")` (a key pressed in CAN Expert while the measurement runs, not while typing into a field; `"*"`: any), `@on_error_frame` (argument: its timestamp) and `@on_bus_state` (argument: `error active`, `error passive` or `bus off`, on a change).
- **UDS service functions**: every ISO 14229-1 service except Authentication (0x29) and SecuredDataTransmission (0x84) is a script function, e.g. `RDBI(0xFF99)` sends `22 FF 99`, `WDBI(did, data)`, `DSC(session)`, `SA(sub_function, key)`, `RC(sub_function, routine_id, data)`, `RD/TD/RTE`, `RDTCI(sub_function, ...)`, plus `UDS("raw hex")` and helpers (`SecurityUnlock`, `ReadDTCs`, `StartRoutine`...). They use the session's UDS transport and return a result that is true for a positive response, with `data` (after the SID and echoed parameters), `text`, `int`, `raw`, `nrc`, `nrc_name` and `error`. Sub-function services accept `suppress=True` (suppressPosRspMsgIndicationBit; sent without waiting). The Form Designer's script tab lists them by ISO 14229 functional unit and inserts calls.
- `api.signal("Message.Signal")`: latest received (or sent) physical value. `api.set_signal("Message.Signal", value)` and `api.send_message("Message", Signal=value, ...)`: encode with the panel's DBC and send; signals not given keep their last known values.
- `api.on(name, callback)`: callback receives the control value. Buttons pass `True`, checkboxes a Boolean, sliders an integer, combo boxes their selected text. Editable fields submit when editing finishes; their selected value type controls conversion.
- `api.on_can(callback)`: callback receives `(arbitration_id, bytes)`.
- `api.every(seconds, callback)`: periodic callback with no arguments.
- `api.can.send(id, data)`: sends up to eight bytes using the active configuration's CAN identifier width; shorter script frames retain the legacy eight-byte padding behavior.
- `api.can.get_latest_messages()`: recent received messages.
- `api.uds.request(payload)`: sends any UDS request over ISO-TP (multi-frame requests and replies, flow control, NRC 0x78 response pending) and returns the positive or negative reply, or `None` on timeout. Helpers: `tester_present()`, `rdbi(did)` (data record without the DID echo), `request_download(format, address, size)`, `transfer_data(sequence, data)`, `request_transfer_exit()`, `transfer_data_from_file(path, packet_size)`. They use the configuration's request/response IDs, identifier size, extended-address byte and UDS response timeout. Frames received before a request are discarded, and the connection's TesterPresent is deferred while an exchange is in progress.
- `api.ui.get_value(name)` and `api.ui.set_value(name, value)`: read a cached value or enqueue a GUI update. Displays format numbers with their unit, decimals and DBC value-table text; an LED takes a Boolean; a multi-state indicator a state value; a trend graph appends a point; an output box appends a line (`None` clears it). Scripts must not access Qt widgets directly.
- `api.log(text)` or `api.write(text)` (CAPL's `write`), and `api.warn(text)`: a line in the Write window, a warning in its colour. Script errors go there too, and to the application's Debug log.
- `api.sysvar.get(name, default)`, `api.sysvar.set(name, value)` (defines a new variable), `api.sysvar[name]`, `api.sysvar.define(name, kind, initial, unit, comment)`: the system variables shared with the System Variables window and the CAN Logger. Values start from their initial ones at every connect.
- `api.running` and `api.sleep(seconds)`: cooperative cancellation for older loop-based scripts. Prefer callbacks and return from `DatabaseMainFunction`; a startup loop prevents that script's queued callbacks from being processed.

Callbacks run serially off the GUI thread. Exceptions are logged. Disconnect cancels Python execution, stops timers and reception, revokes the script bus, and closes the CAN adapter. A blocking native/DLL call cannot be forcibly interrupted; it must return on its own. It cannot use the revoked session bus to transmit afterward. Database scripts are ordinary local Python code and have the user's process permissions.

The connection already schedules TesterPresent; database scripts do not need to run their own TesterPresent loop. Every frame of a session - requests, flow control, TesterPresent - is padded to 8 bytes (0xCC by default) and the tester asks for the block size and STmin of the configuration's ISO-TP settings; both are kept by CAN Expert per configuration, not in the configuration file. The UDS/firmware helpers are not the connection scheduler and are outside the requirements acceptance scope. ISO-TP is implemented for classic CAN (payloads up to 4095 bytes) and is tested against a simulated ECU; firmware programming against a real ECU is not certified. The Diagnostic Window sends ODX-encoded requests of any length on a background thread and shows the complete reply.

## Firmware flashing

While connected, a **Flashing** button appears in the toolbar (the Form Designer's **Test panel...** window has the same **Flashing...** button, against the simulated ECU). There are two ways to flash, and the dialog offers whichever are available: the built-in ISO 14229 sequence, which needs nothing but a connection, and the database script's own function, offered when the script defines:

```python
def Flashing(api, firmware):
    ...
    return True
```

Clicking it asks which Motorola S-record (`.s19`, `.s28`, `.s37`, `.srec`, `.mot`) or Intel HEX (`.hex`, `.ihex`) file to use. The file is checked (record checksums, overlapping data) and contiguous records are merged into segments. A confirmation ("Flash demo_app.hex (2112 bytes) to the ECU?", with the address ranges) follows, then `Flashing(api, firmware)` runs on the script thread with a progress dialog (Cancel requests a stop) and a final success or error message:

- `firmware.path`, `firmware.size`, and `firmware.segments`: a list of `(address, bytes)` in ascending address order.
- `api.progress(done, total, message)` updates the progress dialog.
- `api.flash_cancelled` becomes true when the user presses Cancel; the script decides where it is safe to stop.
- Returning `False` or raising an exception reports failure with that message; anything else reports success.

`examples/firmware/demo_app.s19` and `demo_app.hex` are the same two-segment test image (2 KB at 0x00010000, 64 bytes at 0x00020000). To try flashing without a vehicle: open `examples/example_2026-09-18.xml` (or the showcase) in the Form Designer, click **Test panel...** and then **Flashing...**; or connect the main window to `dummy_ecu.py` with the **Dummy ECU** configuration, click **Flashing** and pick either file (the Dummy ECU window's **Save memory as S-record...** gives the received image back). The Dummy ECU window's Flashing tab sets what the simulated bootloader accepts: TransferData size (maxNumberOfBlockLength), data and address/length formats, memory ranges, full blocks, routine IDs and erase time.

### The built-in sequence

Without a script, `flash_sequence.run_flash()` sends what most bootloaders want, and a `FlashProfile` holds everything that differs between them - editable in **Sequence settings...** and saved as a JSON profile file (the settings last used are remembered):

| Setting | Default | What it does |
|---|---|---|
| `extended_session` | `0x03` | The session entered before security access; 0 leaves the session alone |
| `stop_dtc`, `stop_communication` | on | ControlDTCSetting 0x02 and CommunicationControl 0x03 0x01 while programming |
| `programming_session` | `0x02` | The session the download runs in |
| `security_level` | `0x01` | SecurityAccess requestSeed level (sendKey is the next one); 0 skips it |
| `key_mask` / `key_dll`, `key_variant` | `0xA5` | key = seed XOR mask, or a `GenerateKeyEx` DLL when one is named |
| `erase_routine`, `check_routine` | `0xFF00`, `0xFF01` | RoutineControl startRoutine identifiers; 0 skips that step |
| `address_format`, `data_format` | `0x44`, `0x00` | addressAndLengthFormatIdentifier and dataFormatIdentifier |
| `block_size` | 0 | Bytes per TransferData; 0 uses what the ECU announces |
| `reset_type`, `version_did` | `0x01`, `0xF195` | ECUReset and the DID read once it is back; 0 skips them |
| `restore_after` | on | DTCs and normal messages switched back on when it is done |

Each segment is erased, downloaded (RequestDownload, TransferData blocks, RequestTransferExit) and then the whole image checked, so a two-segment file produces two erases. The block size is `min(maxNumberOfBlockLength, 4095) - 2` (the service identifier and the block counter come off it), narrowed further by `block_size` when it is set. Cancel stops after the block being sent.

Whatever happens, a report is written beside the firmware as `<firmware>.flash-report.txt`: the image and its address ranges, the profile it ran with, every step with its answer, and the result. A run that fails keeps the steps that did happen.

### With a script

`examples/example_2026-09-18_script.py` (and the showcase script) contain a complete ISO 14229-1 sequence written with the UDS functions: extended session (0x10 03), ControlDTCSetting off (0x85 02), CommunicationControl (0x28 03 01), programming session (0x10 02), SecurityAccess seed/key (0x27), then per segment RoutineControl eraseMemory (0x31 01 FF00), RequestDownload (0x34), TransferData blocks sized from maxNumberOfBlockLength (0x36), RequestTransferExit (0x37), and finally checkProgrammingDependencies (0x31 01 FF01) and ECUReset (0x11 01). Replace its `compute_key()` placeholder and routine identifiers with your bootloader's. The configuration must use the ECU's physical request/response IDs, because multi-frame requests are not allowed on the functional 0x7DF address. This sequence is tested against a simulated bootloader and against `dummy_ecu.py` over the Kvaser Virtual CAN Driver, not a real ECU.

## Designer and bindings

New forms receive a date in their default filename. Preserve or supply that suffix when naming a version. Switching between Form and Database code preserves the code buffer. Saving validates widget fields and Python syntax. Widget kind and value type are separate, so value controls no longer disappear on save. Existing raw CAN IDs, payloads, scales, offsets and byte/bit positions are retained.

DBC bindings use `Message.Signal`. Relative DBC paths resolve against the XML directory. Numeric values are decoded for presentation; input controls encode their bound signal into the message while retaining other known values. Legacy checkbox/slider mappings retain other known bits/bytes in the same frame. Controls without a CAN/DBC mapping can operate entirely through scripts.

The designer offers 20 controls (input, display and decoration categories), multi-select (Ctrl+click or a rubber band), align/same size/distribute relative to the last-selected control, a 10 px grid with snap, a resize handle, bring to front/send to back (saved as document order, so group boxes stay behind their contents), copy/cut/paste/duplicate, arrow-key nudging, undo/redo (Ctrl+Z / Ctrl+Y), and DBC signal drag-and-drop (a display, or with Ctrl an input; value tables become indicators or combo boxes). **Test panel...** runs the unsaved form and script against the simulated ECU on a private virtual bus.

Example panels and scripts live under `examples/`; `showcase_2026-09-18` uses every control with `DBC/dummy_ecu.dbc`. Copy them to `Databases/` and select family `example` to try them. They do not replace existing user databases automatically.

## Verification and limits

Run from the project directory:

```
python -B -m unittest discover -s tests -v
```

The tests isolate settings and databases in temporary directories, replace hardware discovery, and communicate through python-can's virtual interface. CI runs them on supported Python versions. No hardware is required or contacted by these tests. Adapter drivers, electrical bus conditions, and timing on an actual vehicle still require a hardware acceptance run.
