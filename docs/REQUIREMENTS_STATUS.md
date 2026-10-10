# Requirements implementation

This document describes the workflow implemented from `Requirements.docx`. CAN Expert is a focused CAN communication and Python panel application; full CANoe compatibility is not implied.

| Requirement | Implemented behavior | Acceptance coverage |
|---|---|---|
| 1 and 4.1 | Configurations, CAN receiver/node tree, the frames on the bus (Trace window), panel designer and script-controlled panels | End-to-end virtual CAN test and offscreen UI inspection |
| 2 | Configuration JSON files listed at startup | Configuration inventory test |
| 2.1 | Last selected configuration persisted in QSettings and restored; first available configuration is the fallback | Restart test |
| 2.2 | TesterPresent sent immediately on connection and at the configured interval, using the configured request ID, CAN identifier width and optional address byte | Repeated heartbeat and extended-address tests |
| 2.3 | Responding configured node IDs shown as children of the selected receiver; timeout produces a red cross; renewed traffic restores green status. After Disconnect (or with right-click **Check ECUs**) TesterPresent continues on the channel, so the status keeps following the ECU's replies; right-click **Stop checking ECUs** ends it (nodes then show Not checked) | Multiple-node, loss and recovery tests; ECU check after disconnect (responding, silent, back, stopped, handed back to a session) |
| 2.3.1 | The receiver used last is remembered and selected at the next start, and its ECUs are checked with TesterPresent immediately; the receiver in use (connected, or its ECUs checked) is shown in bold, and no other; a responding ECU also lists the database the active configuration would load, which double-clicking loads exactly as Connect does | Remembered-channel restart test; only-the-channel-in-use-in-bold test; database entry and double-click test |
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
- **Statistics**: per identifier the count, rate, average/min/max cycle time and share of the bus, and
  for the bus the total load, the error frames and the controller state (error active, error
  passive, bus off).
- **Data window**: every signal of the symbol databases with the value it holds now, physical and raw,
  with its unit, age and count.
- **Transmit window**, two tabs that keep sending until the window is closed: the **message list** (raw or
  database messages, one-shot or cyclic, edited signal by signal) and the **simulated nodes** (the
  messages of a database's sending nodes, put on the bus at their cycle times, so an ECU on the bench
  sees the traffic it expects).
- **UDS Console**: every ISO 14229 service (the same catalogue the scripts use) with a generated request
  form, the services of an ODX/PDX/CDD file with their answers decoded, session control, SecurityAccess
  (mask or `GenerateKeyEx` seed & key DLL) and a fault-memory tab. The P2/P2* timing an ECU announces is
  honoured by the requests that follow.
- **Test modules** (TestExpert's *Modules* tab): Python test cases with setup/teardown, run with the
  generated tests, a verdict per step, Stop, and HTML and JUnit XML reports.
- **TestExpert** (`test_expert.py`, TestExpert.exe): UDS conformance tests generated from a CDD, ODX or PDX
  description, as Vector DiVa does, run against the ECU with HTML and JUnit reports; the Dummy ECU passes them.
  Pre-test and post-test sequences (a hard reset after a test, an ignition frame before the run...), test plans
  run from the window or the command line (exit code, JUnit), an NRC policy and accepted deviations, a coverage
  matrix, discovery of what the ECU really has, DID values against their limits and text tables, routines, S3
  and response pending checks, and runs compared with each other.
- **J1939** (*Tools → J1939*): address claims and NAMEs, DM1/DM2 faults with DM11/DM3 clear, any PGN
  requested or sent with the transport protocol (BAM, RTS/CTS); the Trace's J1939 view; J1939 DBC messages
  from any source address; `j1939` and `@on_pgn` in scripts; the Dummy ECU as a J1939 node.
- **Markers** (Ctrl+M): a comment at a moment of the measurement, in the Trace, on the Logger's graphs and
  in BLF/ASC/TRC recordings; scripts use `api.marker()`.
- **Recording and replay**: BLF, ASC, CSV, LOG or TRC through python-can; a replayed file reaches the
  windows offline and never touches a bus.
- **Symbol databases**: one DBC list shared by the Trace, Data and Statistics windows, the CAN Logger and
  the Transmit window.
- **Workspace**: the tool windows are panes of the main window; the arrangement is saved and can be kept
  as named desktops. Every page of a panel database is a window of it, zoomed or fitted to the window.
- **Channel setup** (per adapter channel): sample point and SJW, listen-only, a receive filter in the
  adapter, and bit rate detection by listening at each common rate.
- **ISO-TP** (per configuration, in the settings): padding of every frame and the flow control the tester
  asks for.
- **Scan for ECUs**: TesterPresent over an 11-bit range or 29-bit normal fixed addresses, then the sessions
  each ECU accepts and its identification DIDs, beside a running measurement.
- **One measurement clock**: every window shows a frame's own timestamp, absolute or relative to the start
  of the measurement; the Trace also filters by direction.
- A **Write window** for the script's output and variables; script events for keys, error frames and the
  bus state.
- **CAN Logger exports**: CSV with a row per sample or a column per signal, MDF 4, PNG, of everything, the
  screen or the cursor range; statistics between the cursors; a cap on the samples kept.
- **Dummy ECU**: editable DIDs (live from a signal, or needing a session or a security level), DTCs whose
  status follows faults through operation cycles with the snapshot of the moment, forced negative
  responses; the messages of any DBC with a generator per signal; periodic data (0x2A), ResponseOnEvent
  (0x86), I/O control (0x2F) and memory by address (0x23/0x3D); several security levels with a mask or a
  seed & key DLL, and service rules; a bootloader after a failed flash, with an optional CRC-32 check;
  transport errors on purpose; several simulated ECUs on one channel.

## Configuration

Configurations live beside `main.py` in `Configurations/`, independent of the working directory. Double-click a configuration to edit it while disconnected. New settings are editable in the configuration dialog:

- `tester_present_interval_seconds`: positive interval, default 0.5 seconds.
- `node_timeout_seconds`: greater than the heartbeat interval, default 2 seconds. A node is shown as lost this long after its last frame; keep several heartbeats inside the window so one missed response is tolerated.
- `request_id` and `response_id`: numeric CAN IDs, entered as hexadecimal in the dialog. With the OBD functional request ID `0x7DF` and an ECU response ID `0x7E8`-`0x7EF`, TesterPresent monitoring stays functional while UDS requests from scripts, flashing and the UDS Console address the ECU physically at the response ID minus 8 (for example `0x7E0`), because multi-frame requests may not use a functional address (ISO 15765-2/-4).
- `response_ids`: optional list of monitored ECU IDs. When omitted, use `response_id`; the default OBD request/response pair `0x7DF`/`0x7E8` monitors `0x7E8` through `0x7EF`.
- `database_family`: optional database stem/family. Empty selects the newest database across the database directory.
- `identifier_11_bit`: standard or extended CAN frames.
- `extended_id` and `extended_id_byte`: optional UDS extended-address prefix for TesterPresent and every UDS frame sent by scripts or the UDS Console.
- `timeout_ms`: UDS response timeout for script and UDS Console requests, default 5000 ms.

Each connection uses a snapshot of one configuration and one selected receiver. Configurations can be edited for the next connection without changing an active session. Traffic from configured response IDs establishes node presence; lack of traffic for the configured timeout marks an established node lost. Unknown response IDs are still visible in the Trace window but are not added to the node tree. Monitoring continues after timeout to detect recovery.

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

## Panel database format

A panel database is XML: its pages, and on each page its controls, placed with `x`, `y`, `width` and `height`.
The Form Designer writes it, and it can be written by hand:

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

Controls can be driven by a script binding (`binding_value`), a DBC signal (`binding_type="dbc"`,
`binding_value="Message.Signal"`), or a raw CAN mapping:

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
| Display | Value Display (number format, decimals, DBC value-table text), 7-Segment Display, Gauge (warning/critical zones), Progress Bar (horizontal/vertical), LED (colours, blink), Multi-State Indicator (states from the DBC value table or `value=text:colour; ...`), Trend Graph, Output Box, Variable List |
| Decoration | Label, Group Box, Picture |

Every control also has appearance properties (text colour, background, font size, bold, tooltip; inputs can be
read-only). I/O boxes are white unless given a background, in either theme.

## Panel scripts

Set a control's **Binding type** to `script` and its **Binding** to a unique name such as `start`, `setpoint`, or `status`. If there is no binding, the displayed control label is its script name. Names are case-sensitive and must be unique across pages.

```python
def DatabaseMainFunction(api):
    api.ui.set_value("status", "Ready")

@on_control("start")
def start(api, value):
    api.can.send(0x200, [1])

@on_control("setpoint")
def setpoint(api, value):
    api.log(f"Setpoint: {value}")

@on_message(0x300)
def status(api, frame):
    api.ui.set_value("status", frame.data.hex())

@on_timer(1.0)
def tick(api):
    api.log("Panel timer")
```

- **Handler property**: a control calls the script function named in its Handler (the Form Designer creates `def on_<name>_<event>(api, value):` when you double-click the control). A function whose first parameter is named `api` receives the script API; other parameters receive the event's values.
- **Event decorators** (CAPL `on` procedures): `@on_start` and `@on_stop` (connect/disconnect; `@on_stop` runs while the bus is still open), `@on_timer(seconds)`, `@on_message(0x300)` or `@on_message("MessageName")` (argument `frame` with `id`, `data`, `signals`), `@on_signal("Message.Signal")` (called when the value changes; `every_update=True` for every frame), `@on_control("name")`, `@on_key("a", "F5")` (a key pressed in CAN Expert while the measurement runs, not while typing into a field; `"*"`: any), `@on_error_frame` (argument: its timestamp) and `@on_bus_state` (argument: `error active`, `error passive` or `bus off`, on a change), `@on_periodic_data(0xF201)` (arguments `data`, `identifier`: periodic data after `RDBPI`; none named: all) and `@on_response_event(0x22)` (argument: the response ResponseOnEvent sent, as bytes; none named: all).
- **UDS service functions**: every ISO 14229-1 service is a script function (Authentication `AUTH` and SecuredDataTransmission `SDT` take their records as bytes), e.g. `RDBI(0xFF99)` sends `22 FF 99`, `WDBI(did, data)`, `DSC(session)`, `SA(sub_function, key)`, `RC(sub_function, routine_id, data)`, `RD/TD/RTE`, `RDTCI(sub_function, ...)`, plus `UDS("raw hex")` and helpers (`SecurityUnlock`, `ReadDTCs`, `StartRoutine`...). They use the session's UDS transport and return a result that is true for a positive response, with `data` (after the SID and echoed parameters), `text`, `int`, `raw`, `nrc`, `nrc_name` and `error`. Sub-function services accept `suppress=True` (suppressPosRspMsgIndicationBit; sent without waiting). The Form Designer's script tab lists them by ISO 14229 functional unit and inserts calls.
- `api.signal("Message.Signal")`: latest received (or sent) physical value. `api.set_signal("Message.Signal", value)` and `api.send_message("Message", Signal=value, ...)`: encode with the panel's DBC and send; signals not given keep their last known values.
- Control values: buttons pass `True`, checkboxes a Boolean, sliders an integer, combo boxes their selected text. Editable fields submit when editing finishes; their selected value type controls conversion.
- `api.can.send(id, data)`: sends up to eight bytes using the active configuration's CAN identifier width; shorter script frames retain the legacy eight-byte padding behavior.
- UDS requests use the configuration's request/response IDs, identifier size, extended-address byte and UDS response timeout, over ISO-TP (multi-frame requests and replies, flow control, NRC 0x78 response pending). Frames received before a request are discarded, and the connection's TesterPresent is deferred while an exchange is in progress.
- `api.ui.get_value(name)` and `api.ui.set_value(name, value)`: read a cached value or enqueue a GUI update. Displays format numbers with their unit, decimals and DBC value-table text; an LED takes a Boolean; a multi-state indicator a state value; a trend graph appends a point; an output box appends a line (`None` clears it). Scripts must not access Qt widgets directly.
- `api.log(text)` or `api.write(text)` (CAPL's `write`), and `api.warn(text)`: a line in the Write window, a warning in its colour. Script errors go there too, and to the application's Debug log.
- `api.running` and `api.sleep(seconds)`: cooperative cancellation for older loop-based scripts. Prefer callbacks and return from `DatabaseMainFunction`; a startup loop prevents that script's queued callbacks from being processed.
- **Structured variables** (the Form Designer's Variables tab, kept in the database's optional `<variables>`): records of typed fields and arrays written as `Calib Data (memory 0x20001000, little-endian)` then `* uint32 FOC[32]`, in braces as in C (`MyList { uint32 data1; uint8 data2; }`, on one line or several), or as a pasted C struct, living in the ECU at a DID or a memory address. `api.var("Calib Data")` gives the script one: its fields as attributes and items (`.temperature`, `.FOC[3]`, `["FOC"][3]`), `.read()` / `.write()` with the ECU (RDBI/WDBI or RMBA/WMBA; true when the ECU answered), `.bytes()` / `.decode(data)`; a value that does not fit its type raises. `@on_variable("Calib Data")` is called with `(variable, field)` when a field is typed on the panel. A **Variable List** control shows one field by field and reads, edits and writes it; a control named after a field (`Idle.speed`) shows that field.
- **Read and Write**: `def Read(api):` and `def Write(api):` are run by the main window's **Read** and **Write** toolbar buttons - shown once the connected database's ECU answers, greyed while it does not or while one of them or a flashing runs, gone with the database - on the script thread, after the events queued before them. Returning `False` or a negative UDS result (`return api.var("Calib Data").write()`), or raising, is a failure, said with the NRC or the exception's message; anything else is success. The outcome is shown in the status bar with the database that ran it, in the Write window, and in the Log when it failed. Each button - **Reflash** too - first refreshes the database: a newer one of the family, or the loaded one saved since, takes its place in the running session (the adapter, the CAN worker and TesterPresent go on; the panel is built again and the new script started); one that cannot be loaded leaves the loaded one, and the command is not run.

The ISO 14229 functions, by functional unit:

| Functional unit (ISO 14229-1) | Functions |
|---|---|
| Diagnostic and communication management | `DSC` 0x10, `ER` 0x11, `SA` 0x27, `CC` 0x28, `AUTH` 0x29, `TP` 0x3E, `ATP` 0x83, `SDT` 0x84, `CDTCS` 0x85, `ROE` 0x86, `LC` 0x87 |
| Data transmission | `RDBI` 0x22, `RMBA` 0x23, `RSDBI` 0x24, `RDBPI` 0x2A, `DDDI_DefineById` / `DDDI_DefineByAddress` / `DDDI_Clear` 0x2C, `WDBI` 0x2E, `WMBA` 0x3D |
| Stored data transmission | `CDTCI` 0x14, `RDTCI` 0x19 |
| Input/output control | `IOCBI` 0x2F |
| Remote activation of routine | `RC` 0x31 |
| Upload/download | `RD` 0x34, `RU` 0x35, `TD` 0x36, `RTE` 0x37, `RFT` 0x38 |
| Helpers | `UDS("22 F1 90")` (any request), `SecurityUnlock(level, compute_key)`, `ReadDTCs(mask)`, `StartRoutine` / `StopRoutine` / `RoutineResults`, `UdsLog(True)` |

An ECU answering *busyRepeatRequest* (NRC 0x21) is asked again, three times at most.

**Deprecated.** The first script API is still there, so existing scripts keep working, but the Form Designer's completion no longer offers it and its docstrings name the replacement:

| Deprecated | Use instead |
|---|---|
| `api.on(name, callback)` - callback(value) for a named control | `@on_control(name)`, or the control's Handler |
| `api.on_can(callback)` - callback(arbitration_id, bytes) | `@on_message` |
| `api.every(seconds, callback)` | `@on_timer(seconds)` |
| `api.can.get_latest_messages()` | `@on_message` |
| `api.uds.request(payload)` (the reply bytes, or `None`) | `UDS(payload)` |
| `api.uds.tester_present()` | nothing: the session sends TesterPresent (or `TP()`) |
| `api.uds.rdbi(did)` (the data record) | `RDBI(did).data` |
| `api.uds.request_download`, `transfer_data`, `request_transfer_exit` | `RD`, `TD`, `RTE` |
| `api.uds.transfer_data_from_file(path, packet_size)`, `api.uds.parse_s19_s28(path)` | `Flashing(api, firmware)`, which gets the parsed image, or the built-in sequence |

Callbacks run serially off the GUI thread. Exceptions are logged. Disconnect cancels Python execution, stops timers and reception, revokes the script bus, and closes the CAN adapter. A blocking native/DLL call cannot be forcibly interrupted; it must return on its own. It cannot use the revoked session bus to transmit afterward. Database scripts are ordinary local Python code and have the user's process permissions.

The connection already schedules TesterPresent; database scripts do not need to run their own TesterPresent loop. Every frame of a session - requests, flow control, TesterPresent - is padded to 8 bytes (0xCC by default) and the tester asks for the block size and STmin of the configuration's ISO-TP settings; both are kept by CAN Expert per configuration, not in the configuration file. The UDS/firmware helpers are not the connection scheduler and are outside the requirements acceptance scope. ISO-TP is implemented for classic CAN (payloads up to 4095 bytes) and is tested against a simulated ECU; firmware programming against a real ECU is not certified. The UDS Console's ODX tab sends ODX-encoded requests of any length on a background thread and shows the complete reply, decoded by the ODX file.

## Firmware flashing

While a database is connected and its ECU answers, a **Reflash** button appears in the toolbar, and refreshes the database before it flashes (see *Read and Write* under *Panel scripts*); without a database, the **Flashing** button flashes with the built-in sequence over the ECU check of the chosen channel, to the ECU of the configuration the check uses. The Form Designer's **Test panel...** window has a **Flashing...** button with the same dialog and both ways, against the simulated ECU. The dialog names the ECU - the configuration and its request and response identifiers. There are two ways to flash, and the dialog offers whichever are available: the built-in ISO 14229 sequence, which needs nothing but a connection, and the database script's own function, offered when the script defines:

```python
def Flashing(api, firmware):
    ...
    return True
```

Clicking it asks which Motorola S-record (`.s19`, `.s28`, `.s37`, `.srec`, `.mot`) or Intel HEX (`.hex`, `.ihex`) file to use. The file is checked (record checksums, overlapping data) and contiguous records are merged into segments. The Flashing dialog then shows the file, its size and address ranges and asks which way to flash; with the script, `Flashing(api, firmware)` runs on the script thread with a progress dialog (Cancel requests a stop) and a final success or error message:

- `firmware.path`, `firmware.size`, and `firmware.segments`: a list of `(address, bytes)` in ascending address order.
- `api.progress(done, total, message)` updates the progress dialog.
- `api.flash_cancelled` becomes true when the user presses Cancel; the script decides where it is safe to stop.
- Returning `False` or raising an exception reports failure with that message; anything else reports success.

`examples/firmware/demo_app.s19` and `demo_app.hex` are the same two-segment test image (2 KB at 0x00010000, 64 bytes at 0x00020000). To try flashing without a vehicle: open `examples/example_2026-09-18.xml` (or the showcase) in the Form Designer, click **Test panel...** and then **Flashing...**; or connect the main window to `dummy_ecu.py` with the **Dummy ECU** configuration, click **Reflash** and pick either file (the Dummy ECU window's **Save memory as S-record...** gives the received image back). The Dummy ECU window's Flashing tab sets what the simulated bootloader accepts: TransferData size (maxNumberOfBlockLength), data and address/length formats, memory ranges, full blocks, routine IDs and erase time.

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
| `check_crc` | off | The CRC-32 of the image (its segments in address order) sent as the dependency check's option record, for a bootloader that checks it |

Each segment is erased, downloaded (RequestDownload, TransferData blocks, RequestTransferExit) and then the whole image checked, so a two-segment file produces two erases. The block size is `min(maxNumberOfBlockLength, 4095) - 2` (the service identifier and the block counter come off it), narrowed further by `block_size` when it is set. Cancel stops after the block being sent.

Whatever happens, a report is written beside the firmware as `<firmware>.flash-report.txt`: the image and its address ranges, the profile it ran with, every step with its answer, and the result. A run that fails keeps the steps that did happen.

### With a script

`examples/example_2026-09-18_script.py` (and the showcase script) contain a complete ISO 14229-1 sequence written with the UDS functions: extended session (0x10 03), ControlDTCSetting off (0x85 02), CommunicationControl (0x28 03 01), programming session (0x10 02), SecurityAccess seed/key (0x27), then per segment RoutineControl eraseMemory (0x31 01 FF00), RequestDownload (0x34), TransferData blocks sized from maxNumberOfBlockLength (0x36), RequestTransferExit (0x37), and finally checkProgrammingDependencies (0x31 01 FF01) and ECUReset (0x11 01). Replace its `compute_key()` placeholder and routine identifiers with your bootloader's. The configuration must use the ECU's physical request/response IDs, because multi-frame requests are not allowed on the functional 0x7DF address. This sequence is tested against a simulated bootloader and against `dummy_ecu.py` over the Kvaser Virtual CAN Driver, not a real ECU.

## Designer and bindings

New forms receive a date in their default filename. Preserve or supply that suffix when naming a version. Switching between Form and Database code preserves the code buffer. Saving validates widget fields and Python syntax. Widget kind and value type are separate, so value controls no longer disappear on save. Existing raw CAN IDs, payloads, scales, offsets and byte/bit positions are retained.

DBC bindings use `Message.Signal`. Relative DBC paths resolve against the XML directory, and the Form Designer saves the path relative to it when the DBC lies in that directory or beside it (under its parent folder), absolute otherwise. Numeric values are decoded for presentation; input controls encode their bound signal into the message while retaining other known values. Legacy checkbox/slider mappings retain other known bits/bytes in the same frame. Controls without a CAN/DBC mapping can operate entirely through scripts.

The designer offers 20 controls (input, display and decoration categories), multi-select (Ctrl+click or a rubber band), align/same size/distribute relative to the last-selected control, a 10 px grid with snap, a resize handle, bring to front/send to back (saved as document order, so group boxes stay behind their contents), copy/cut/paste/duplicate, arrow-key nudging, undo/redo (Ctrl+Z / Ctrl+Y), every control moved by holding the left button (a read-only one too), Find / Find and replace / Go to line with line numbers in the script and the variables (Ctrl+F, Ctrl+H, F3, Ctrl+G), and DBC signal drag-and-drop (a display, or with Ctrl an input; value tables become indicators or combo boxes). **Test panel...** runs the unsaved form and script against the simulated ECU on a private virtual bus. On a running panel, an I/O box's value is selected and copied - a read-only one's too, kept selected as the value changes.

Example panels and scripts live under `examples/`; `showcase_2026-09-18` uses every control with `DBC/dummy_ecu.dbc`, and its ECU information page reads what the Dummy ECU tells over UDS (identification DIDs, session, uptime, live values by DID, the fault memory) - at Connect, with its Read all button and with the toolbar's Read (`Read(api)`). Copy them to `Databases/` and select family `example` to try them. They do not replace existing user databases automatically.

## Verification and limits

Run from the project directory:

```
python -B -m unittest discover -s tests -v
```

The tests isolate settings and databases in temporary directories, replace hardware discovery, and communicate through python-can's virtual interface. CI runs them on supported Python versions. No hardware is required or contacted by these tests. Adapter drivers, electrical bus conditions, and timing on an actual vehicle still require a hardware acceptance run.
