# Planned goals

What CAN Expert is missing to work and feel like Vector CANoe, ranked from the biggest gap to the
smallest. Written on 2026-09-20 after a full read of the codebase; every claim about the current
behaviour is tied to the file it comes from.

These are suggestions, not decisions. Nothing here changes the configuration JSON format, the panel
database XML, or the `_script.py` mechanism: where a feature has to remember something, it stores it in
its own file or in QSettings. Three items would genuinely be better with one new optional configuration
field, and they say so.

**Status:** tier 1 items 2-6 and 8 and tier 2 items 11-18 are **implemented** (item 7, the test feature
set, and items 9 and 10, CAN FD and several channels, were left out on purpose; item 1 was built and
then removed — see it below). Each one is marked; the rest is untouched.

---

## Tier 1 — Structural gaps

These are the differences that make CAN Expert a panel tool with CAN underneath rather than a bus
analysis tool.

### 1. A measurement that starts without a panel database, and a passive mode — **REMOVED after trying it**
*Effort: medium*

**Was:** Connect refuses outright when no database matches
([main_window.py:726](canexpert/main_window.py#L726)), and connecting always transmits TesterPresent
([can_bus.py:69](canexpert/can_bus.py#L69)). There is no way to simply watch a vehicle bus.

**CANoe:** press Start and watch; a database, panels and scripts are all optional.

**Add:** split "measurement running" from "panel loaded", allow connecting with no database, and add a
listen-only / silent option (no TesterPresent, no ACK). Everything below this line becomes easier once
it exists.

**Now:** built as **Start** and **Passive**, then taken out again in use. Another tool already covers
reading a bus without a database, and a running measurement blocked Connect while the node tree showed
"Lost connection", because nothing sends TesterPresent during one. **Connect** is the only way to open a
channel again. What the work left behind is still there and is what the rest of tier 1 stands on: one
path for every frame, the frame history that fills a window opened later, and the adapter's timestamps.

### 2. A real Trace window — **DONE**
*Effort: large — the biggest visible payoff*

**Was:** a `QPlainTextEdit` printing raw hex, capped at 5000 lines, with no decoding, filtering,
search, export or colour ([main_window.py:313](canexpert/main_window.py#L313)).

**Add:** a table with time, channel, direction, ID, symbolic message name, DLC and data, with
expandable rows showing the decoded signals; relative / absolute / delta time; scroll lock; pass and
stop filters; search; copy and export; colour per ID. `cantools` already does the decoding.

**Now:** `canexpert/trace_window.py`, with all of that except the channel column (there is still only
one channel — item 10). Frames are buffered and flushed on a timer, and a row's signals are decoded only
when it is opened, so a busy bus stays responsive.

### 3. Logging to file and offline replay — **DONE**
*Effort: medium — python-can does the heavy lifting*

**Was:** nothing reaches disk except the logger's decoded-signal CSV.

**Add:** BLF/ASC recording (python-can has the writers and readers) with triggers and a pre-trigger
buffer, and an offline mode that replays a file through the same decode path into the Trace, the
Logger and the panels. Offline analysis is half of what CANoe is used for.

**Now:** `canexpert/recording.py`: **Record to file...** writes BLF, ASC, CSV, LOG or TRC, and **Replay
a recorded file...** plays one back into every window at real time up to as fast as possible, with no bus
open. Triggers and a pre-trigger buffer are still to do.

### 4. Interactive Generator (transmit list) — **DONE**
*Effort: medium*

**Was:** the only way to send anything repeatedly is a panel script calling `api.every`.

**Add:** a transmit window of rows (raw messages or picked from a DBC), each with a cycle time,
one-shot and burst sending, signal-level editing with value tables, enable/disable per row, and the
list saved as its own file.

**Now:** `canexpert/transmit_window.py`, with rows kept in the settings and saveable as JSON. A row
that cannot be sent (no measurement, or passive) switches itself off with the reason; closing the pane
stops every cyclic row.

### 5. Databases assigned at the application level, not per panel — **DONE**
*Effort: medium — the enabler for items 2, 4 and 14*

**Was:** a DBC is attached inside a panel XML (`dbc_path`) and loaded again separately in the CAN
Logger. Nothing says "this bus speaks this database".

**Add:** an application-level database list (its own file or QSettings, not the configuration JSON)
that the Trace, Logger, transmit list, data window and panels all read.

**Now:** `canexpert/symbols.py` holds the list in the settings, and the Trace window, the CAN Logger
and the transmit list all read it (**Tools ▸ Symbol databases...**). The Logger's own **Load DBC...**
adds to the same list, and it can show several files at once. Panels keep their own DBC.

### 6. A diagnostic console that does not need ODX, and a fault memory window — **DONE**
*Effort: medium — the best value per hour on this list*

**Was:** the Diagnostic Window needs `odxtools`, a loaded ODX file and a full database session
([diagnostic_window.py:31](canexpert/diagnostic_window.py#L31),
[:252](canexpert/diagnostic_window.py#L252)).

**Already in place:** the complete ISO 14229 catalogue with docstrings and signatures
([uds/client.py:450](canexpert/uds/client.py#L450)) and a ready-made browser widget
(`UdsFunctionPanel` in the designer).

**Add:** reuse both — pick a service, fill in the parameters, send, and see the request and response
decoded with NRC names, with a history and a resend. Then a fault memory window: read DTCs
(`ReadDTCs` already exists), status bits, snapshot and extended records, and clear, using ODX text
when it is available.

**Now:** `canexpert/uds_console.py` builds each request form from the function's own signature, sends
on a background thread over a private mailbox, and logs the response with its NRC name. It also has a
session and SecurityAccess bar and a fault-memory tab (read, count, snapshot, extended data, clear) that
spells out the DTC status bits. ODX text for DTCs is still to do.

### 7. Test feature set with reports — *not started (left out on purpose)*
*Effort: large*

**Today:** absent. This is what separates a viewer from a validation tool.

**Add:** a test tree that runs Python test cases against the live bus (reusing `ScriptRuntime` and the
UDS functions), with pass/fail per step, setup and teardown, and an HTML or JUnit report.

### 8. One docked workspace instead of separate dialogs, with saved desktops — **DONE**
*Effort: medium — the single biggest "looks like CANoe" item*

**Was:** the CAN Logger, Diagnostic Window and Form Designer are `QDialog`s floating outside the
main window, and there is no `saveState` or `saveGeometry` anywhere in the codebase: window size, dock
arrangement and splitter positions are rebuilt from scratch at every start.

**Add:** make the tool windows dockable panes of the main window, remember the layout, and offer named
desktops (Analysis / Diagnostics / Test).

**Now:** the middle of the main window is a workspace built on the Qt Advanced Docking System
(PyQtAds): the Database panel and the analysis windows tab together, split an area, float as windows of
their own and show drop guides while being dragged, which plain Qt docks cannot do. Configuration, CAN
Channels and Log stay fixed panels around it, keeping their minimise-to-a-strip buttons. The
arrangement and the window geometry are saved on close, and **View ▸ Save desktop as...** /
**Desktops** / **Reset layout** keep named arrangements of both halves. The Form Designer stays a
separate window, as CANoe's panel designer does.

Still missing from CANoe's window system: **auto-hide** (a window pinned to a side tab that slides out
on hover). That arrived in Qt-ADS 4.x and the Python binding is at 3.8.1, so it would need the
minimise-to-a-strip idea extended to workspace windows.

---

## Tier 2 — Major features

### 9. CAN FD — *not started (left out on purpose)*
*Effort: medium to large. Needs one optional configuration field, or QSettings.*

No `fd=` or `data_bitrate` anywhere; sends are capped at 8 bytes
([main_window.py:986](canexpert/main_window.py#L986)) and ISO-TP hardcodes 7 data bytes per frame
([uds/isotp.py:123](canexpert/uds/isotp.py#L123)). Needs the FD flag and data bitrate in the channel
setup, 64-byte frames, and ISO-TP FD (DLC padding rules, the FF escape). Most modern ECUs worth
flashing are FD.

### 10. Several channels open at once — *not started (left out on purpose)*
*Effort: large — architectural, best done before the Trace and Logger settle*

`self.workers` is a dict with exactly one entry, `"main"`
([main_window.py:742](canexpert/main_window.py#L742)) — the intent is visible but unimplemented. CANoe
is multi-network by nature: a channel column in every window, per-channel databases, gateway views.

### 11. Bus statistics, error frames and bus-off — **DONE**
*Effort: medium*

**Was:** error frames silently discarded ([can_bus.py:74](canexpert/can_bus.py#L74)); no bus load, no
frame counters, no TX/RX error counters, and no bus-off detection or recovery. A wiring fault looked
exactly like a quiet bus.

**Now:** `canexpert/statistics_window.py` — per identifier the count, rate, average/min/max cycle time,
share of the bus and last data; for the bus the total load, the error frames and the controller state
(*error active*, *error passive*, *bus off*), which turns the totals red. `CanWorker` counts error
frames and polls `BusState` (`error_frame` and `bus_status` signals) instead of dropping them. The bus
load counts the frame overhead and worst-case stuffing; a replayed file is measured at its own
timestamps, so a recording keeps its own rates.

### 12. Rest-bus simulation from a DBC — **DONE**
*Effort: medium to large*

**Was:** `dummy_ecu.py`, a good hand-written UDS server, but not node simulation.

**Now:** `canexpert/simulation_window.py` — a branch per sending node of the symbol databases with the
messages it sends, each with the cycle time out of the database and its data editable signal by signal.
Tick a message or a whole node, press **Start sending**, and they go out at their cycle times through
the shared `canexpert/cyclic.py` schedule (which the transmit list now uses too). What was ticked is
remembered; a send that fails stops the simulation instead of filling the log, and closing the window
stops it. A Python script per node is the part not done: the panel scripts already cover that ground.

### 13. Several signals in one graph — **DONE**
*Effort: medium*

**Was:** strictly one strip chart per ticked signal (`_rebuild_strips`).

**Now:** graph groups in `canexpert/can_logger.py`: **Combine** draws every signal in one graph, and the
right-click menu moves one signal into another's graph (*Draw together with ...*) or back out (*Graph of
its own*). Signals sharing a graph share its value axis and get a legend; the axis is labelled with their
unit when they agree on one. Cursors, the hover readout, the axis locks and Fit all work per graph.

### 14. A Data / Signal window — **DONE**
*Effort: small once item 5 exists*

**Was:** only the logger's signal tree, and only while the logger was open, for its own DBC.

**Now:** `canexpert/data_window.py` — every signal of the shared symbol databases with its physical and
raw value, unit, age and count, including the ones that have never arrived (so a database that is not
being fed shows it). Frames CAN Expert sends are decoded as well. Filter, **Received only**, clear and
CSV export.

### 15. Session and security state, and seed & key — **DONE**
*Effort: small to medium*

**Was:** nothing in the UI showed the session or the security state; the P2 and P2\* the ECU announces
in its DiagnosticSessionControl response were ignored in favour of a fixed `timeout_ms` and a hardcoded
5 s pending timeout.

**Now:** the UDS Console has a state strip — `Session: extended   P2 75 ms / P2* 4000 ms   Security:
unlocked (level 1)` — and `UdsFunctions` learns the announced timing in `DSC()` and carries it into every
later request, never shortening what the configuration allows. `canexpert/uds/seed_key.py` computes the
key from a mask or from a real ECU's `GenerateKeyEx` DLL (the Vector ABI), which the console and the
built-in flashing sequence both use; a DLL that refuses says why before anything is sent.

### 16. All windows should see traffic outside a database session — **DONE** (with tier 1)
*Effort: medium, and mostly falls out of item 1*

**Was:** the Logger and the diagnostic monitor were fed only from `on_can_message`; the ECU-check path
never reached them, so during "Checking ECUs" the Logger showed nothing and a window opened after
connecting had already missed everything.

**Now:** every frame — session, ECU check or replayed file — goes through `dispatch_frame()`, which
keeps the last 20000 in `frame_history`, writes the recording, logs to the CAN monitor and then hands
the frame to every open tool window. A window opened later is filled from that history, so it shows
what happened before it existed.

### 17. Flashing without a panel script, and a flash report — **DONE**
*Effort: medium*

**Was:** flashing only appeared when the loaded database's script defined `Flashing()`, and nothing was
written down about a run.

**Now:** `canexpert/flash_sequence.py` holds the sequence itself — session, DTCs and normal messages off,
programming session, security access, then per segment erase, RequestDownload, TransferData,
RequestTransferExit, then the dependency check, the restore, the reset and a version read — with
everything that differs between bootloaders in a `FlashProfile` edited in **Sequence settings...** and
saved as a JSON profile file. `flash_runner.py` runs it on a thread with its own mailbox and reports
through the same progress and finished path the script hook uses, so the Flashing button now offers both
ways and needs nothing but a connection. Whatever happens, a report lands beside the firmware as
`<firmware>.flash-report.txt`: the image, the profile, every step with its answer, and the result.

### 18. A transport and diagnostic layer in the Trace — **DONE**
*Effort: medium*

**Was:** multi-frame exchanges appeared as loose frames.

**Now:** the Trace's **Transport** button rebuilds the view from `canexpert/uds/observer.py`, which puts
the ISO 15765-2 frames of a request or a response back together — single, first and consecutive frames,
the escape sequence and extended addressing included, flow control dropped — and names the service from
the same catalogue the console uses. One row per diagnostic message, with its length and whole payload,
for the request and response identifiers of the configuration you connected with. The N_Bs / N_Cr timing
detail is the part not done.

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

## What has been done

Tier 1 items **2, 3, 4, 5, 6 and 8** are implemented, with `canexpert/trace_window.py`,
`transmit_window.py`, `uds_console.py`, `recording.py` and `symbols.py` as new modules and the tool
windows turned into panes of the main window. Item **7** (the test feature set) was deliberately left
out, and item **1** was built and then removed again in use.

Tier 2 items **11, 12, 13, 14, 15, 16, 17 and 18** are implemented, adding `statistics_window.py`,
`data_window.py`, `simulation_window.py`, `cyclic.py`, `flash_sequence.py`, `flash_runner.py`,
`uds/observer.py` and `uds/seed_key.py`, and extending the Trace, the CAN Logger, the UDS Console and the
flashing path. Items **9** (CAN FD) and **10** (several channels at once) were left out on purpose.

None of it changed the configuration, database or script formats.

The next things worth doing, in order: **item 19** (ISO-TP padding — a half-hour fix and the most likely
reason a real ECU ignores CAN Expert), **item 9** (CAN FD, which most modern ECUs need), **item 10**
(several channels at once, best done before more windows settle) and **item 7** if the tool is to be
used for validation.
