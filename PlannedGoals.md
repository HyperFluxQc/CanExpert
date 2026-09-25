# Planned goals

What CAN Expert is missing to work and feel like Vector CANoe, ranked from the biggest gap to the
smallest. Written on 2026-09-20 after a full read of the codebase; every claim about the current
behaviour is tied to the file it comes from.

These are suggestions, not decisions. Nothing here changes the configuration JSON format, the panel
database XML, or the `_script.py` mechanism: where a feature has to remember something, it stores it in
its own file or in QSettings. Three items would genuinely be better with one new optional configuration
field, and they say so.

**Status:** tier 1 items 2-8, tier 2 items 11-18, tier 3 items 19-29 and 32, tier 4 items 33, 35-38
and 40, and tier 5 item 41 (J1939) are **implemented** (items 9 and 10, CAN FD and several channels, were left out on purpose; item 1
was built and then removed — see it below). Each one is marked; the rest is untouched.

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
spells out the DTC status bits, writes each code as a scan tool does (P0101-00) and shows its text from the
ODX, PDX or CDD file loaded in the ODX tab (`ODX/dummy_ecu.odx-d` describes the Dummy ECU's DTCs).

### 7. Test feature set with reports — **DONE**
*Effort: large*

**Was:** absent. This is what separates a viewer from a validation tool.

**Now:** *Tools → Test* (`canexpert/testing/`) runs a test module - a Python file of `@testcase`
functions with `setup`, `teardown`, `before_each` and `after_each` - against the live bus with the UDS
functions the scripts use and `t.check / check_equal / check_range / expect_nrc / require / fail / skip /
log / wait / send / wait_for_frame / wait_for_signal`. The tree shows each case's verdict and every step as
it runs; Stop skips the rest and still tears down. Each run leaves an HTML report and a JUnit XML file
beside the module. `TestModules/dummy_ecu_checks.py` is an example against the Dummy ECU.

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

### 19. ISO-TP padding — the interop item to fix first — **DONE**
*Effort: small. Needs one optional configuration field.*

`uds_request` accepts a `padding` argument, but `uds_transport()` never sets it
([config.py:95](canexpert/config.py#L95)), so every frame CAN Expert sends is unpadded — and
TesterPresent is a bare 3 bytes ([can_bus.py:59](canexpert/can_bus.py#L59)). Many production ECUs
reject frames that are not padded to 8 bytes (0x00 or 0xAA). The simulator pads; the tester never
does. This is the most likely reason a real ECU would ignore CAN Expert.

**Now:** every frame a session sends - requests, the tester's flow control, TesterPresent - is padded
to 8 bytes, 0xCC by default (a byte that needs no stuff bits), or with another byte, or not at all. It is
kept per configuration name in the settings (`transport_settings.py`), not in the configuration file,
and shown in its own group of the configuration dialog.

### 20. The tester's own flow control is unreachable — **DONE**
*Effort: small*

`isotp_recv` takes `block_size` and `st_min`, but `uds_request` never passes them
([uds/client.py:49](canexpert/uds/client.py#L49)), so the tester always answers BS=0 / STmin=0. The
dummy ECU lets you configure this side; the client does not. Exposing it allows testing how an ECU
paces to a slow tester.

**Now:** the block size and STmin the tester asks for sit beside the padding, per configuration, and
reach `isotp_recv` through `uds_request`; the tests watch the simulated ECU pace its answer to them.

### 21. Logger accuracy, memory and exports — **DONE**
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

**Now:** the time axis comes from the adapter's timestamps and starts with the measurement (item 32);
each signal keeps at most a set number of samples (1,000,000 by default), dropping its oldest quarter;
the cursors add min, max, mean and σ of the samples between them; **Export...** writes CSV with a row
per sample or a column per signal, MDF 4 (a small writer in `mdf4.py`, checked against asammdf) or a
PNG, of everything, what is on screen, or the cursor range. Record-to-disk is the recording's job.

### 22. Channel setup: bit timing, listen-only, autobaud — **DONE**
*Effort: small to medium*

Bitrate is a fixed combo of four values ([config.py:161](canexpert/config.py#L161)). There is no custom
bit timing or sample point (python-can has `BitTiming`), no listen-only, no termination control, and
the activity scan listens for 0.3 s at one guessed bitrate with no baud detection.

**Now:** right-click a channel -> **Channel setup...** (`channel_setup.py`), kept per adapter channel:
sample point and SJW as a python-can `BitTiming` on the driver's clock, listen-only (Kvaser's silent
mode, Vector's `listen_only`; the session then sends nothing), a receive filter in the adapter, and bit
rate detection that listens at each common rate without acknowledging anything. The configuration's bit
rate takes any value. IXXAT gets neither sample point nor listen-only - python-can offers neither - and
the dialog says so. Termination is not reachable through python-can and is not offered.

### 23. Script event parity and a Write window — **DONE**
*Effort: medium*

The runtime has `on_start`, `on_stop`, `on_timer`, `on_message`, `on_signal` and `on_control`. CAPL
also has `on key`, `on errorFrame`, `on busOff`, `on envVar` / `on sysVar` and pre-start. Add at least
the bus-event handlers. Separately, script output and application logs share one Debug pane — CANoe's
Write window is its own thing. There are also no breakpoints or watch window; stepping through a panel
script would be a differentiator.

**Now:** `@on_key`, `@on_error_frame`, `@on_bus_state` and `@on_sysvar` (item 24); a **Write window**
with the script's `api.log` / `api.write` / `api.warn` and its errors, levels, search and save, apart
from the Debug log; and a **Script variables** tab watching the script's globals. Breakpoints and
stepping are not done, nor `on preStart`.

### 24. System variables — **DONE, switched off for now**
*Effort: medium*

CANoe glues panels, CAPL and tests together with system variables. Here a control binds only to a
script name or a DBC signal, so two panels cannot share a value and the logger cannot plot a computed
one. Script-side variables cost nothing; making them a *control binding* would touch the panel XML, so
that part stays optional.

**Now:** `sysvars.py` - `Namespace::Name` variables with a type, initial value, unit and comment,
set and read by scripts (`api.sysvar`, `@on_sysvar`), listed and typed into in the System Variables
window, plotted by the CAN Logger, definitions kept in the settings or a JSON file, values reset at each
measurement. As planned, controls are not bound to them in the panel XML; a script bridges the two.

**Switched off** (`canexpert/features.py`, `SYSTEM_VARIABLES = False`): without controls bound to them or
a link to bus signals they did little on their own, so they show nowhere until they can be bound. The
code stays and its tests switch it on; turning them back on is setting the switch to True.

### 25. Filters everywhere — **DONE**
*Effort: small to medium*

No filters in any window, and `bus.set_filters` is never called, so filtering cannot even be offloaded
to the adapter. Pass and stop lists by ID range, symbolic name, direction and channel.

**Now:** `frame_filter.py` gives the Trace and the CAN monitor one filter - identifiers, ranges,
names, Pass/Stop, and RX only / TX only on top - and the monitor's is rebuilt from the history when it
changes. Filtering in the adapter is the channel setup's receive filter. The channel column waits for
item 10.

### 26. Unsolicited-response services are only half implemented — **DONE**
*Effort: medium*

**Was:** `ROE` (0x86) and `RDBPI` (0x2A) could be sent, but the event and periodic responses they cause
were skipped as unrelated frames; `NRC 0x21 busyRepeatRequest` went back to the caller; Authentication
(0x29) and SecuredDataTransmission (0x84) were excluded.

**Now:** the session's CAN worker reassembles what the ECU sends by itself - with flow control for a
multi-frame event response - and a request hands over the replies that are not its answer, so nothing is
lost between or during exchanges. The UDS Console's *Periodic & events* tab starts and stops periodic data,
sets up and controls ResponseOnEvent and lists what arrives; scripts get `@on_periodic_data` and
`@on_response_event`. NRC 0x21 repeats the request (three times at most). `AUTH` (0x29) and `SDT` (0x84)
send their records as bytes, which completes the ISO 14229-1 service list.

### 27. Panel runtime — **DONE**
*Effort: medium*

Controls are placed at fixed pixel coordinates inside a scroll area
([panel/view.py:56](canexpert/panel/view.py#L56)): no scaling on window resize, no zoom, one panel at a
time, no floating or multiple panels. CANoe panels resize, dock and open several at once.

**Now:** every page of the loaded database is a window of the workspace - the first in the Database
window, the others tabbed beside it - that can be split off, floated and placed by the saved desktops.
`panel/page_window.py` draws a page at any zoom from its controls' designed geometry and fonts: Fit
follows the window, 50-200 % scroll, Ctrl + wheel steps; the zoom is remembered per database family and
page. One database is loaded at a time, as before.

### 28. Dummy ECU: editable DIDs, DTCs and NRCs — **DONE**
*Effort: medium*

DIDs and DTCs are hardcoded ([simulator/ecu.py:406](canexpert/simulator/ecu.py#L406)), `WDBI` accepts
only `F190`, and `RDTCI` implements only sub-functions 0x01 and 0x02. Add a DID-to-value table, an
editable DTC list with snapshot and extended records, per-service forced NRCs (to test the tester's own
error handling), and several simulated ECUs on one channel.

**Now:** the Dummy ECU window's **Data** tab edits the DID table (writable or not), the DTC table
with a snapshot and an extended data record each, and forced negative responses per service, live and
in its profile. `WDBI` writes any writable DID; `RDTCI` adds 0x04, 0x06 and 0x0A, so the UDS Console's
Snapshot and Extended data buttons work against it. Several dummy ECUs share a channel when their
identifiers differ: the lock goes by request ID.

### 29. ECU discovery scan — **DONE**
*Effort: small to medium, and very much an "expert tool" feature*

The activity scan only reports traffic or no traffic as a text suffix, and requires disconnecting
first. Add a real scan: sweep request IDs (0x7E0–0x7E7 and a custom range) with TesterPresent, list
every responder, probe the supported sessions and a DID sample, and offer bitrate detection.

**Now:** **Connection -> Scan for ECUs...** (`ecu_scan.py`): TesterPresent over an 11-bit range or
29-bit normal fixed addresses, every responder with its response identifier, the default and extended
sessions it takes (programming only when asked), its identification DIDs, CSV export, and a new
configuration from any ECU found. It runs beside a measurement with the session's TesterPresent paused,
and reaches the channel setup's bit rate detection.

### 30. OBD-II mode scanner
*Effort: medium — a quick win on the existing stack*

Services 01, 03 and 09 (live data, DTCs, VIN) in a small dedicated window. CANoe sells this as an
option and the UDS stack is already 80% of the way there.

### 31. Symbol explorer
*Effort: small*

No way to browse a DBC inside the app — messages, signals, bit layout, value tables, nodes, cycle
times. Only the designer's symbol list and the logger tree, both task-specific.

### 32. One measurement clock, with absolute / relative / delta display — **DONE**
*Effort: small, and a correctness fix*

Three clocks today: the CAN monitor uses `datetime.now()`
([main_window.py:301](canexpert/main_window.py#L301)), the logger uses monotonic-since-first-frame, and
the diagnostic monitor uses `datetime` again. Nothing can be correlated across windows.

**Now:** `clock.py` - one `MeasurementClock` started at connect, ECU check or replay. The CAN monitor
and the Diagnostic Window show each frame's own timestamp, the Trace's Relative and the Logger's time
axis count from the same start, and **View -> Time display** switches the monitors between Absolute and
Relative. A timestamp that is not a time of day is shown as seconds.

---

## Tier 4 — Small, cheap, high polish per hour

### 33. Keyboard shortcuts — **DONE**
*Effort: very small*

**Was:** not a single `setShortcut` or `QKeySequence` in the codebase.

**Now:** F9 / Shift+F9 connect and disconnect, Ctrl+1...Ctrl+7 switch the tool windows in the toolbar's
order, Ctrl+E the Form Designer, Ctrl+R / Ctrl+Shift+R / Ctrl+O record, stop and replay, Ctrl+N, Ctrl+Q,
F1; floating windows get the same keys. The Form Designer has its own (Ctrl+S, F5, F7...). Plain letters and
F5 stay free for the scripts' `@on_key`. The manual lists them (*Keyboard shortcuts*).

### 34. TesterPresent options
*Effort: very small. Optional configuration field.*

Fixed `02 3E 00`. Offer `3E 80` (suppressed positive response) for a quiet bus — with the caveat that
the node-status tree depends on the reply, so it must stay optional and disable node detection when
used.

### 35. Export and markers — **DONE**
*Effort: very small*

~~The CAN monitor cannot be saved or searched~~ — the CAN monitor is gone, the Trace covers it (find,
CSV export).

**Now:** **Connection → Insert marker...** (Ctrl+M, or Ctrl+Shift+M without a comment) marks a moment of
the measurement: a highlighted row in the Trace, a line across the Logger's graphs, a global marker in a BLF
recording (a comment line in ASC and TRC). Panel scripts call `api.marker()`, test modules `t.marker()`.
A replay in CAN Expert does not read the markers back.

### 36. A status strip showing the system state — **DONE**
*Effort: small*

**Was:** errors landed in a debug pane the user had to think to look at.

**Now:** the status bar shows the bus state and error frames, the diagnostic session and security state
read off the ECU's answers (whoever sent the request), and the last error - NRC, script error, bus off,
failed session - linked to the Log. The TX queue depth is left out: python-can has no portable way to read
it.

### 37. Packaging: the .exe that is promised but absent — **DONE**
*Effort: small*

**Was:** no `.spec`, no build script, no icon, no version resource and no installer in the repo.

**Now:** `CanExpert.spec` and `tools/build_windows.py` build CanExpert.exe and DummyECU.exe into one
folder with their icons (`tools/make_icons.py`) and a version resource from `canexpert.__version__`,
check that both start (`--smoke-test`) and zip it; CI builds the zip for `main` and `v*` tags and also
runs the tests on Windows. No installer: the zip unpacks and runs as it is.

### 38. About box with real information — **DONE**
*Effort: very small*

**Was:** a hardcoded text block with no version.

**Now:** `about.py` shows the version, the build date (the executable's), Python, Qt, python-can and the
other libraries, the Kvaser, Vector and IXXAT driver DLLs' versions, and the operating system, with
**Copy** for a bug report.

### 39. Configuration quick-switch in the toolbar
*Effort: very small*

A combo box instead of only the dock list, plus a clear indicator of which configuration and channel
are live.

### 40. Context help — **DONE**
*Effort: very small*

F1 opens the manual at the section of the window with the focus - a tool window's, the panel's, the
Configuration or CAN Channels panel's - also from a floating window; the Form Designer's F1 opens its own
section.

---

## Tier 5 — Only if users ask

| Item | Effort | Note |
|---|---|---|
| **TestExpert** — **DONE** | large | Not on the list at first: DiVa-like UDS conformance tests generated from a CDD, ODX or PDX file, a program of its own (`test_expert.py`), which found the Dummy ECU's NRC order and length deviations (fixed) |
| **TestExpert: sequences, plans, policy, coverage, discovery, deeper tests, comparison** — **DONE** | large | Pre-test and post-test sequences around the run, groups and tests (blocked tests, warnings); test plans and headless runs for CI; an NRC policy and accepted deviations; a coverage matrix and the ECU's identification; discovery against the description (and as a description); DID values against their fields, limits written, routines started and out of order, S3, response pending; runs compared. It found two more Dummy ECU deviations (fixed): a response pending did not lift the suppress bit, and routines had no stop or results |
| **41. J1939** — **DONE** | large | TP.CM/BAM transport, PGN decode, address claim. Now: `canexpert/j1939/` (identifiers, NAME, DM1/DM2, BAM and RTS/CTS), the J1939 window (nodes, faults, requests), the Trace's J1939 view, J1939 DBCs matched by PGN, `j1939` / `@on_pgn` in scripts and test modules, and the Dummy ECU as a J1939 node |
| **42. XCP / CCP with A2L** | very large | Measurement and calibration of internal ECU variables; what no DBC-based tool can do |
| **43. Localization** | medium | Qt has the tooling; CANoe ships EN/DE/JP/CN |
| **44. LIN, FlexRay, automotive Ethernet** | very large | Realistically out of scope. If ever: LIN on a Kvaser/Vector adapter, and only after CAN FD |

---

## Half-finished things found while reading

- ~~**Diagnostic Window**: needs ODX, `odxtools` and an active database session; encodes only "free
  parameters"~~ — merged into the UDS Console as its ODX tab; its monitor is replaced by the Trace's
  transport view (item 18).
- ~~**`workers` is a one-entry dict**~~ — one `worker` now.
- ~~Dead parameters: `isotp_recv`'s `block_size` and `st_min`, and `uds_request`'s `padding`~~ — reachable
  from the configuration dialog since items 19 and 20.
- ~~`message.timestamp` is emitted and never consumed~~ — every window uses it since tier 1 and item 32.
- ~~**Activity scan**~~ — removed; Scan for ECUs (item 29) and bit rate detection (item 22) answer it.
- ~~**`Configurations/config_test.json`**~~ and ~~**`requirements-build.txt`**~~ — deleted.
- ~~Dummy ECU: `RDTCI` only 0x01/0x02, `WDBI` only `F190`, DIDs and DTCs not user-editable~~ — item 28.

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

Tier 3 items **19, 20, 21, 22, 23, 24, 25, 27, 28, 29 and 32** are implemented, adding
`transport_settings.py`, `channel_setup.py`, `frame_filter.py`, `clock.py`, `sysvars.py`,
`write_window.py`, `ecu_scan.py`, `mdf4.py` and `panel/page_window.py`. Items 26 (unsolicited
responses), 30 (OBD-II scanner) and 31 (symbol explorer) are left for later, with tier 4.

None of it changed the configuration, database or script formats: the new settings - ISO-TP per
configuration, the channel setup, system variables, page zooms - live in CAN Expert's own settings.

Parts of tier 4 came along on the way: the Trace, Statistics, Data window and Logger export (35, except
markers), and the session, security and bus state are shown in the UDS Console and
Statistics (36, though not yet in one strip of the main window).

Then the features that had grown to overlap were cut back:

- the **CAN Monitor** tab of the Log is gone — the Trace shows every frame, with the same filter; the Log
  is the application's debug log;
- the **Diagnostic Window** is merged into the **UDS Console** as an **ODX** tab, and once an ODX file is
  loaded every answer in the console is decoded by it;
- **Scan Activity** is gone (Scan for ECUs and bit rate detection do its job), and so is the console's
  one-off **Tester present** button (the session sends TesterPresent);
- the **Transmit list** and the **Simulated nodes** are two tabs of one **Transmit** window, and both keep
  sending until that window is closed;
- the current values of signals are the **Data window**'s: the Logger's *Value* column and Statistics'
  *Last data* column are gone;
- the Form Designer's **Test panel** flashes through the main window's Flashing dialog, so the built-in
  sequence works there too;
- the first script API (`api.on`, `api.on_can`, `api.every`, `api.can.get_latest_messages`,
  `api.uds.*`) still works but is deprecated and out of the editor's completion;
- the `EZCan2/KvaserCAN` settings migration, `config_test.json`, `requirements-build.txt` and the one-entry
  `workers` dict are removed.

The Dummy ECU then became a fuller simulation: the messages of any DBC with a generator per signal
(`simulator/signals.py`), periodic data and ResponseOnEvent, a fault memory whose statuses follow faults
through operation cycles (`simulator/dtc.py`), transport errors on purpose, access rules per DID and
service with several security levels and a seed & key DLL, a bootloader after a failed flash with an
optional CRC-32 check, and ReadMemoryByAddress, WriteMemoryByAddress and InputOutputControlByIdentifier.
Its default traffic is what it always sent, so the panels and examples work as before.

The next things worth doing: **item 26** (a receive path for ResponseOnEvent and periodic data, and
retrying NRC 0x21), the tier 4 polish - **33** keyboard shortcuts and **37** packaging first - and
**item 7** if the tool is to be used for validation.
