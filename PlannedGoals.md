# Planned goals

What CAN Expert is missing to work and feel like Vector CANoe, ranked from the biggest gap to the
smallest. Written on 2026-09-20 after a full read of the codebase; every claim about the current
behaviour is tied to the file it comes from.

These are suggestions, not decisions. Nothing here changes the configuration JSON format, the panel
database XML, or the `_script.py` mechanism: where a feature has to remember something, it stores it in
its own file or in QSettings. Three items would genuinely be better with one new optional configuration
field, and they say so.

---

## Tier 1 — Structural gaps

These are the differences that make CAN Expert a panel tool with CAN underneath rather than a bus
analysis tool.

### 1. A measurement that starts without a panel database, and a passive mode
*Effort: medium*

**Today:** Connect refuses outright when no database matches
([main_window.py:726](canexpert/main_window.py#L726)), and connecting always transmits TesterPresent
([can_bus.py:69](canexpert/can_bus.py#L69)). There is no way to simply watch a vehicle bus.

**CANoe:** press Start and watch; a database, panels and scripts are all optional.

**Add:** split "measurement running" from "panel loaded", allow connecting with no database, and add a
listen-only / silent option (no TesterPresent, no ACK). Everything below this line becomes easier once
it exists.

### 2. A real Trace window
*Effort: large — the biggest visible payoff*

**Today:** a `QPlainTextEdit` printing raw hex, capped at 5000 lines, with no decoding, filtering,
search, export or colour ([main_window.py:313](canexpert/main_window.py#L313)).

**Add:** a table with time, channel, direction, ID, symbolic message name, DLC and data, with
expandable rows showing the decoded signals; relative / absolute / delta time; scroll lock; pass and
stop filters; search; copy and export; colour per ID. `cantools` already does the decoding.

### 3. Logging to file and offline replay
*Effort: medium — python-can does the heavy lifting*

**Today:** nothing reaches disk except the logger's decoded-signal CSV.

**Add:** BLF/ASC recording (python-can has the writers and readers) with triggers and a pre-trigger
buffer, and an offline mode that replays a file through the same decode path into the Trace, the
Logger and the panels. Offline analysis is half of what CANoe is used for.

### 4. Interactive Generator (transmit list)
*Effort: medium*

**Today:** the only way to send anything repeatedly is a panel script calling `api.every`.

**Add:** a transmit window of rows (raw messages or picked from a DBC), each with a cycle time,
one-shot and burst sending, signal-level editing with value tables, enable/disable per row, and the
list saved as its own file.

### 5. Databases assigned at the application level, not per panel
*Effort: medium — the enabler for items 2, 4 and 14*

**Today:** a DBC is attached inside a panel XML (`dbc_path`) and loaded again separately in the CAN
Logger. Nothing says "this bus speaks this database".

**Add:** an application-level database list (its own file or QSettings, not the configuration JSON)
that the Trace, Logger, transmit list, data window and panels all read.

### 6. A diagnostic console that does not need ODX, and a fault memory window
*Effort: medium — the best value per hour on this list*

**Today:** the Diagnostic Window needs `odxtools`, a loaded ODX file and a full database session
([diagnostic_window.py:31](canexpert/diagnostic_window.py#L31),
[:252](canexpert/diagnostic_window.py#L252)).

**Already in place:** the complete ISO 14229 catalogue with docstrings and signatures
([uds/client.py:450](canexpert/uds/client.py#L450)) and a ready-made browser widget
(`UdsFunctionPanel` in the designer).

**Add:** reuse both — pick a service, fill in the parameters, send, and see the request and response
decoded with NRC names, with a history and a resend. Then a fault memory window: read DTCs
(`ReadDTCs` already exists), status bits, snapshot and extended records, and clear, using ODX text
when it is available.

### 7. Test feature set with reports
*Effort: large*

**Today:** absent. This is what separates a viewer from a validation tool.

**Add:** a test tree that runs Python test cases against the live bus (reusing `ScriptRuntime` and the
UDS functions), with pass/fail per step, setup and teardown, and an HTML or JUnit report.

### 8. One docked workspace instead of separate dialogs, with saved desktops
*Effort: medium — the single biggest "looks like CANoe" item*

**Today:** the CAN Logger, Diagnostic Window and Form Designer are `QDialog`s floating outside the
main window, and there is no `saveState` or `saveGeometry` anywhere in the codebase: window size, dock
arrangement and splitter positions are rebuilt from scratch at every start.

**Add:** make the tool windows dockable panes of the main window, remember the layout, and offer named
desktops (Analysis / Diagnostics / Test).

---

## Tier 2 — Major features

### 9. CAN FD
*Effort: medium to large. Needs one optional configuration field, or QSettings.*

No `fd=` or `data_bitrate` anywhere; sends are capped at 8 bytes
([main_window.py:986](canexpert/main_window.py#L986)) and ISO-TP hardcodes 7 data bytes per frame
([uds/isotp.py:123](canexpert/uds/isotp.py#L123)). Needs the FD flag and data bitrate in the channel
setup, 64-byte frames, and ISO-TP FD (DLC padding rules, the FF escape). Most modern ECUs worth
flashing are FD.

### 10. Several channels open at once
*Effort: large — architectural, best done before the Trace and Logger settle*

`self.workers` is a dict with exactly one entry, `"main"`
([main_window.py:742](canexpert/main_window.py#L742)) — the intent is visible but unimplemented. CANoe
is multi-network by nature: a channel column in every window, per-channel databases, gateway views.

### 11. Bus statistics, error frames and bus-off
*Effort: medium*

Error frames are silently discarded ([can_bus.py:74](canexpert/can_bus.py#L74)); there is no bus load,
no frame counters, no TX/RX error counters, and no bus-off detection or recovery. Right now a wiring
fault looks exactly like a quiet bus.

### 12. Rest-bus simulation from a DBC
*Effort: medium to large*

`dummy_ecu.py` is a good hand-written UDS server, but it is not node simulation. CANoe generates a
node per DBC ECU and transmits its messages at their cycle times. Add: tick the nodes to simulate,
send their messages cyclically with editable signal values, and optionally run a Python script per
node.

### 13. Several signals in one graph
*Effort: medium*

The logger is strictly one strip chart per ticked signal (`_rebuild_strips`). CANoe overlays many
signals on one axis with a legend and supports multiple Y axes. Add drag-onto-graph, a legend,
per-signal colour, per-signal Y axis and graph groups.

### 14. A Data / Signal window
*Effort: small once item 5 exists*

A flat table of every signal with its current value, raw and physical, unit and time since the last
update. The logger's tree half does this, but only while the logger is open and only for its own DBC.

### 15. Session and security state, and seed & key
*Effort: small to medium*

Nothing in the UI shows the current diagnostic session or security state, and there is no
SecurityAccess dialog. The ECU announces P2 and P2\* in its DiagnosticSessionControl response — the
dummy ECU does — but `uds_request` ignores them and uses a fixed `timeout_ms` with a hardcoded 5 s
pending timeout. Add a status strip (session, security, P2/P2\*), a security dialog, and support for a
Vector-style `GenerateKeyEx` DLL through the existing `api.dll`.

### 16. All windows should see traffic outside a database session
*Effort: medium, and mostly falls out of item 1*

The Logger and the diagnostic monitor are fed only from `on_can_message`
([main_window.py:1010](canexpert/main_window.py#L1010)); the ECU-check path (`_on_monitor_message`,
[:855](canexpert/main_window.py#L855)) never reaches them. During "Checking ECUs" the logger shows
nothing, and a logger opened after connecting has already missed everything. One measurement bus plus
a rolling history buffer that late-opened windows can backfill from.

### 17. Flashing without a panel script, and a flash report
*Effort: medium*

Flashing only appears when the loaded database's script defines `Flashing()`
([main_window.py:922](canexpert/main_window.py#L922)). Add a built-in configurable sequence (session,
security, erase routine, RD/TD/RTE, check routine, reset) edited in a dialog and saved as its own
profile file — a small vFlash — keeping the script hook for unusual bootloaders. Write a flash log or
report file afterwards.

### 18. A transport and diagnostic layer in the Trace
*Effort: medium*

Multi-frame exchanges appear as loose frames. CANoe shows the assembled diagnostic message with the
service name and its parameters. All the ISO-TP logic already exists in `uds/isotp.py`; expose an
assembled view with N_Bs / N_Cr timing and flow-control detail.

---

## Tier 3 — Medium

### 19. ISO-TP padding — the interop item to fix first
*Effort: small. Needs one optional configuration field.*

`uds_request` accepts a `padding` argument, but `uds_transport()` never sets it
([config.py:95](canexpert/config.py#L95)), so every frame CAN Expert sends is unpadded — and
TesterPresent is a bare 3 bytes ([can_bus.py:59](canexpert/can_bus.py#L59)). Many production ECUs
reject frames that are not padded to 8 bytes (0x00 or 0xAA). The simulator pads; the tester never
does. This is the most likely reason a real ECU would ignore CAN Expert.

### 20. The tester's own flow control is unreachable
*Effort: small*

`isotp_recv` takes `block_size` and `st_min`, but `uds_request` never passes them
([uds/client.py:49](canexpert/uds/client.py#L49)), so the tester always answers BS=0 / STmin=0. The
dummy ECU lets you configure this side; the client does not. Exposing it allows testing how an ECU
paces to a slow tester.

### 21. Logger accuracy, memory and exports
*Effort: small each*

- `CanWorker` emits `message.timestamp` ([can_bus.py:77](canexpert/can_bus.py#L77)) and nothing reads
  it: the logger stamps with `time.monotonic()` at GUI-delivery time
  ([can_logger.py:678](canexpert/can_logger.py#L678)), so the X axis shows delivery jitter, not bus
  time.
- `_Series` grows forever ([can_logger.py:184](canexpert/can_logger.py#L184)); a long measurement eats
  RAM.
- CSV export is long-format only and always dumps everything.

Fix: hardware timestamps, a ring buffer or record-to-disk, wide CSV / MDF4, export of the visible
range, save the plot as PNG, and per-signal min / max / mean / σ between the cursors.

### 22. Channel setup: bit timing, listen-only, autobaud
*Effort: small to medium*

Bitrate is a fixed combo of four values ([config.py:161](canexpert/config.py#L161)). There is no custom
bit timing or sample point (python-can has `BitTiming`), no listen-only, no termination control, and
the activity scan listens for 0.3 s at one guessed bitrate with no baud detection.

### 23. Script event parity and a Write window
*Effort: medium*

The runtime has `on_start`, `on_stop`, `on_timer`, `on_message`, `on_signal` and `on_control`. CAPL
also has `on key`, `on errorFrame`, `on busOff`, `on envVar` / `on sysVar` and pre-start. Add at least
the bus-event handlers. Separately, script output and application logs share one Debug pane — CANoe's
Write window is its own thing. There are also no breakpoints or watch window; stepping through a panel
script would be a differentiator.

### 24. System variables
*Effort: medium*

CANoe glues panels, CAPL and tests together with system variables. Here a control binds only to a
script name or a DBC signal, so two panels cannot share a value and the logger cannot plot a computed
one. Script-side variables cost nothing; making them a *control binding* would touch the panel XML, so
that part stays optional.

### 25. Filters everywhere
*Effort: small to medium*

No filters in any window, and `bus.set_filters` is never called, so filtering cannot even be offloaded
to the adapter. Pass and stop lists by ID range, symbolic name, direction and channel.

### 26. Unsolicited-response services are only half implemented
*Effort: medium*

`ROE` (0x86) and `RDBPI` (0x2A) can be sent, but there is no receive path for the event or periodic
responses they cause: they arrive later as unrelated frames and are skipped. `NRC 0x21
busyRepeatRequest` is also returned to the caller instead of being retried. Authentication (0x29) and
SecuredDataTransmission (0x84) are excluded by design
([uds/client.py:488](canexpert/uds/client.py#L488)) — worth closing to claim full ISO 14229 coverage.

### 27. Panel runtime
*Effort: medium*

Controls are placed at fixed pixel coordinates inside a scroll area
([panel/view.py:56](canexpert/panel/view.py#L56)): no scaling on window resize, no zoom, one panel at a
time, no floating or multiple panels. CANoe panels resize, dock and open several at once.

### 28. Dummy ECU: editable DIDs, DTCs and NRCs
*Effort: medium*

DIDs and DTCs are hardcoded ([simulator/ecu.py:406](canexpert/simulator/ecu.py#L406)), `WDBI` accepts
only `F190`, and `RDTCI` implements only sub-functions 0x01 and 0x02. Add a DID-to-value table, an
editable DTC list with snapshot and extended records, per-service forced NRCs (to test the tester's own
error handling), and several simulated ECUs on one channel.

### 29. ECU discovery scan
*Effort: small to medium, and very much an "expert tool" feature*

The activity scan only reports traffic or no traffic as a text suffix, and requires disconnecting
first. Add a real scan: sweep request IDs (0x7E0–0x7E7 and a custom range) with TesterPresent, list
every responder, probe the supported sessions and a DID sample, and offer bitrate detection.

### 30. OBD-II mode scanner
*Effort: medium — a quick win on the existing stack*

Services 01, 03 and 09 (live data, DTCs, VIN) in a small dedicated window. CANoe sells this as an
option and the UDS stack is already 80% of the way there.

### 31. Symbol explorer
*Effort: small*

No way to browse a DBC inside the app — messages, signals, bit layout, value tables, nodes, cycle
times. Only the designer's symbol list and the logger tree, both task-specific.

### 32. One measurement clock, with absolute / relative / delta display
*Effort: small, and a correctness fix*

Three clocks today: the CAN monitor uses `datetime.now()`
([main_window.py:301](canexpert/main_window.py#L301)), the logger uses monotonic-since-first-frame, and
the diagnostic monitor uses `datetime` again. Nothing can be correlated across windows.

---

## Tier 4 — Small, cheap, high polish per hour

### 33. Keyboard shortcuts
*Effort: very small*

There is not a single `setShortcut` or `QKeySequence` in the codebase: no F-key Connect/Disconnect, no
Ctrl+S in the designer, no Esc, no menu accelerators.

### 34. TesterPresent options
*Effort: very small. Optional configuration field.*

Fixed `02 3E 00`. Offer `3E 80` (suppressed positive response) for a quiet bus — with the caveat that
the node-status tree depends on the reply, so it must stay optional and disable node detection when
used.

### 35. Export and markers
*Effort: very small*

The CAN monitor cannot be saved or searched. Add export to every list view, and "insert marker or
comment" during a measurement (CANoe's trigger and comment).

### 36. A status strip showing the system state
*Effort: small*

Bus state, session, security, last error, TX queue depth. Errors currently land in a debug pane the
user has to think to look at.

### 37. Packaging: the .exe that is promised but absent
*Effort: small*

`requirements-build.txt` installs PyInstaller and Pillow, but there is no `.spec`, no build script, no
icon, no version resource and no installer in the repo. For a tool colleagues will actually run, this
matters more than most features above it.

### 38. About box with real information
*Effort: very small*

It is a hardcoded text block with no version ([main_window.py:571](canexpert/main_window.py#L571)).
Show the version, build date, detected driver and DLL versions (Kvaser, Vector, IXXAT, python-can) and
a "copy support info" button.

### 39. Configuration quick-switch in the toolbar
*Effort: very small*

A combo box instead of only the dock list, plus a clear indicator of which configuration and channel
are live.

### 40. Context help
*Effort: very small*

F1 on the focused window jumps to the right manual section. The sections and `go_to_section` already
exist.

---

## Tier 5 — Only if users ask

| Item | Effort | Note |
|---|---|---|
| **41. J1939** | large | TP.CM/BAM transport, PGN decode, address claim |
| **42. XCP / CCP with A2L** | very large | Measurement and calibration of internal ECU variables; what no DBC-based tool can do |
| **43. Localization** | medium | Qt has the tooling; CANoe ships EN/DE/JP/CN |
| **44. LIN, FlexRay, automotive Ethernet** | very large | Realistically out of scope. If ever: LIN on a Kvaser/Vector adapter, and only after CAN FD |

---

## Half-finished things found while reading

- **Diagnostic Window**: needs ODX, `odxtools` and an active database session; encodes only "free
  parameters" with an `int(text, 0)` fallback; response decoding failures are swallowed by a bare
  `except: pass` ([diagnostic_window.py:294](canexpert/diagnostic_window.py#L294)); its monitor shows
  three IDs and no ISO-TP reassembly.
- **`workers` is a one-entry dict** — scaffolding from a multi-channel design that was never built.
- **Dead parameters**: `isotp_recv`'s `block_size` and `st_min`, and `uds_request`'s `padding`, are
  reachable in code but unreachable from the UI.
- **`message.timestamp`** is emitted and never consumed.
- **Activity scan** result is a string suffix on a tree label, not data anything else can use.
- **`Configurations/config_test.json`** still points at database family `FFFFFFFF…`, which was
  deleted, so that configuration always reports "No matching database".
- **`requirements-build.txt`** describes a build that does not exist in the repo.
- **Dummy ECU**: `RDTCI` only 0x01/0x02, `WDBI` only `F190`, DIDs and DTCs not user-editable.

---

## If only five get done

1. **Item 1** — measurement without a database, plus listen-only
2. **Item 2** — the Trace window
3. **Item 6** — ODX-free diagnostic console and fault memory
4. **Item 3** — logging to file and offline replay
5. **Item 8** — docked workspace and saved layout

Those five change the feel from "a panel tool with CAN underneath" to "a CANoe-like analysis tool", and
none of them touch the configuration, database or script formats. **Item 19** (ISO-TP padding) is a
half-hour fix worth slipping in regardless.
