# CAN Expert user manual

CAN Expert connects to a CAN bus, shows which ECUs answer, and runs a **panel** — a page of controls
driven by a Python script. It also has a Trace window for every frame on the bus, a CAN Logger to graph
DBC signals, a Transmit window to send messages and simulate nodes, a UDS Console for diagnostic
services, ODX services and fault memory, a Form Designer to build panels, recording and offline replay,
firmware flashing over UDS, and a simulated ECU so you can try everything without a vehicle.

Press the **?** button at the top right of the main window to open this manual at any time.

## Starting up

Run `python main.py`, or start the built executable.

The main window has a toolbar and four panels:

| Panel | What it holds |
|---|---|
| **Configuration** | Your connection configurations. The one used last is selected again. |
| **CAN Channels** | The CAN receivers found on this computer, the ECUs that answer on them, and the database each one can load. |
| **Database** | The panel of the loaded database, with its controls. It appears once you connect. |
| **Log** | The application's messages, *Debug* or *Verbose*. The frames themselves are in the Trace window. |

The tool windows — Trace, Statistics, Data, CAN Logger, Transmit, UDS Console, Write and Test — open in
the **workspace** in
the middle, together with the Database panel, where they can be tabbed, split and floated (see
*Arranging the windows*). Their toolbar buttons are switches: the button **stays pressed in** while its
window is open, pressing it again closes the window, and closing the window with its own **×** lets the
button go. A window that is closed keeps what it had, so reopening it shows everything recorded
meanwhile.

Configuration, CAN Channels and Log are fixed panels around the workspace. Each has a **–** button to
shrink it to a strip and **×** to close it; the *File* menu brings a closed one back. While a database
is loaded, they shrink automatically to leave the workspace room.

Options inside the windows work the same way: a button that switches something on — the toggles in the
Trace window, the CAN Logger and the Form Designer — stays pressed in with a coloured line under it for
as long as that option is active, so you can see at a glance what is switched on.

At startup CAN Expert selects the receiver you used last, shows every receiver you have connected to
before in **bold**, and starts asking its ECUs whether they are there (see *Checking ECUs* below).

## Configurations

A configuration describes how to talk to an ECU. Double-click one to edit it, or use **New**.

| Field | Meaning |
|---|---|
| **Name** | The name in the list, and the file name in `Configurations/`. |
| **Bitrate** | Bus speed. It must match the vehicle or bench (500000 is the most common). Pick one of the list or type any value, e.g. `83333`. |
| **Identifier size** | 11-bit (standard) or 29-bit (extended) CAN identifiers. |
| **SERVER ID** | The identifier requests are sent to, e.g. `7E0` for one ECU, or `7DF` to address every ECU (OBD). |
| **ECU ID** | The identifier the ECU answers on, e.g. `7E8`. |
| **UDS response timeout** | How long a request waits for the reply. |
| **TesterPresent interval** | How often the "are you there" request is sent. |
| **Node loss timeout** | Without a reply for this long, the ECU is reported lost. It must be longer than the interval. |
| **Database family** | Which panel to load: the `family` part of `Databases/family_YYYY-MM-DD.xml`. Blank loads the newest panel of any family. |
| **Monitored ECU IDs** | Extra identifiers to watch, when several ECUs answer. Blank watches the ECU ID (or `7E8`-`7EF` with SERVER ID `7DF`). |
| **Extended identifier** | For buses where the first data byte extends the address (ISO-TP extended addressing). |

With SERVER ID `7DF`, "are you there" requests stay on `7DF`, but real UDS requests go to the ECU's own
address (ECU ID − 8, so `7E0`), because a request spanning several frames may not be broadcast.

The **ISO-TP** group under the fields is kept by CAN Expert itself, per configuration name — never in the
configuration file:

| Field | Meaning |
|---|---|
| **Padding** | Fill every frame CAN Expert sends to 8 bytes with this byte — requests, flow control and TesterPresent alike. On by default with `CC`: many ECUs ignore diagnostic frames shorter than 8 bytes. |
| **Block size asked of the ECU** | When an ECU answers with a long message, how many consecutive frames it may send before waiting for the next flow control (*no limit* by default). |
| **STmin asked of the ECU** | The gap it must leave between them: `00`-`7F` milliseconds, `F1`-`F9` 100-900 µs. The dialog says which. |

**Import** and **Export** copy a configuration file in or out; the files live in `Configurations/`.

## Connecting

1. Pick a configuration.
2. Pick a receiver in **CAN Channels** (**Refresh** re-scans the computer).
3. Click **Connect**.

The matching database is loaded and its panel is built *before* the adapter is opened, so a broken panel
never leaves you half-connected. The channel is then marked **[Connected]**, and CAN Expert sends
TesterPresent at the configured interval.

**Double-clicking** a receiver, an ECU under it, or the database offered under it connects straight away —
the same as pressing Connect.

**Disconnect** stops the script and the traffic, and closes the adapter.

While you are connected, the **status bar** says how things stand:

| | |
|---|---|
| **Bus** | The adapter's error state: *error active*, *error passive* (orange) or *bus off* (red), with the error frames counted. *on* means the adapter does not report its state. |
| **Session** | The diagnostic session the ECU last confirmed: *default*, *extended*, *programming*... *unknown* until it answers a DiagnosticSessionControl or an ECUReset. |
| **Security** | *unlocked (level 1)* (green) once the ECU accepts a key, *locked* again after a new session or a reset. |
| **Last error** | The last negative response (`ReadDataByIdentifier: NRC 0x31 requestOutOfRange`), script error, bus off or failed session. Click it to show the Log. |

Session and security are read from the ECU's own answers, so they are right whether the request came
from the UDS Console, the panel script or the flashing sequence.

The frames themselves are in the **Trace** window, each with its own time — the adapter's for received
frames. **View → Time display** chooses between the time of day (**Absolute**) and seconds since the
measurement started (**Relative**) for the lines of the Write window and the UDS Console. The measurement
starts when you connect, start the ECU check or replay a file, and the Trace's *Relative* time and the
CAN Logger's time axis count from the same moment, so a line in the console, a row in the Trace and a
point on a graph line up.

### Checking ECUs

Under each receiver, every ECU that answers appears with its status:

| | Status | Meaning |
|---|---|---|
| ● | **Responding** | The ECU answered within the node loss timeout. |
| ✗ | **Lost connection** | It stopped answering. |
| ○ | **Not checked** | Nobody is asking: neither a session nor the ECU check is running on that channel. |

This keeps working **after Disconnect**: the channel is marked **[Checking ECUs]** and TesterPresent
carries on, so the list keeps telling you which ECUs are alive. **Right-click a channel** to *Stop
checking ECUs*, or to *Check ECUs* with the selected configuration without connecting.

Under a responding ECU, CAN Expert also lists the database that configuration would load
("showcase_2026-09-18 — double-click to load"). Double-click it to connect and load the panel.

One caution: TesterPresent keeps the ECU's diagnostic session alive. If the ECU was in an extended or
programming session, it stays there while it is being checked; stop the check to let it time out.

### Channel setup

**Right-click a channel → Channel setup...** sets how that adapter channel is opened. It belongs to the
channel, not to a configuration, and applies from the next connection.

- **Sample point** and **SJW**: the bit timing is worked out for the adapter's clock as you type —
  `BRP 2, TSEG1 13, TSEG2 2, SJW 2 - sample point 87.5 % on a 16 MHz clock`. *The adapter's default*
  leaves it to the driver.
- **Listen-only**: receive without acknowledging frames or sending anything — for a vehicle bus you must
  not disturb. No TesterPresent is sent, anything that tries to send says the channel is listen-only, and
  the channel shows **[listen-only]**.
- **Receive only**: identifiers and ranges the channel lets through (`7E8, 300-3FF, 18DAF100x`), in the
  adapter where it can. The configuration's ECU identifiers always get through, so diagnostics keep working.
- **Find the bit rate**: listens at each common bit rate — listen-only, so a wrong guess never puts an
  error frame on the bus — until one carries clean traffic.

python-can offers the sample point and listen-only for **Kvaser** and **Vector** adapters; for **IXXAT**
the dialog says they are not available rather than pretending.

## Scanning for ECUs

**Connection → Scan for ECUs...** (or right-click a channel → *Scan for ECUs on this channel...*) finds
what is on the bus, without disconnecting:

1. Choose the addressing — **11-bit identifiers** (`7E0` to `7E7` by default, any range) or **29-bit normal
   fixed addressing** (`18DA<target><tester>`, targets `00` to `FF`, tester `F1`) — and press **Scan**.
2. CAN Expert sends TesterPresent to each identifier and lists every ECU that answers, with the identifier
   it answered on.
3. Each one is then asked which of the **default** and **extended** sessions it accepts, and for its
   **identification**: VIN, part and serial numbers, software and hardware versions, supplier. The
   *programming* session is tried only when you tick it — on some ECUs it starts the bootloader. Every
   ECU is left in the default session.

While connected, the scan shares the session's bus and pauses its TesterPresent meanwhile, so an answer
is never credited to the wrong identifier. **New configuration from this ECU...** opens the configuration
dialog with its identifiers filled in, **Export...** saves the list as CSV, and **Find the bit rate** opens
the channel setup.

## Symbol databases

**Tools → Symbol databases...** holds the DBC files the whole application uses: the Trace window names
messages and decodes signals with them, the CAN Logger lists their signals, and the Transmit list can
send their messages. **Add DBC...** and **Remove** manage the list, which is remembered between runs. The
CAN Logger's own **Load DBC...** button adds to the same list.

Panels keep their own DBC (set in the Form Designer), so a panel is self-contained.

A message a J1939 DBC defines as a parameter group (`VFrameFormat` J1939PG, as `DBC/j1939_demo.dbc` does)
matches every frame of its PGN, whichever node sends it — the source address in the DBC's identifier is
only a placeholder.

## Trace window

**Tools → Trace...** shows every frame on the bus while you are connected, newest at the bottom.

| Column | Meaning |
|---|---|
| **Time** | Absolute clock time, seconds since the first frame (*Relative*), or since the frame above (*Delta*). |
| **Dir** | `RX` received, `TX` sent by CAN Expert. |
| **ID** | The identifier; 29-bit identifiers end in `x`. |
| **Name** | The message name from the symbol databases, when one describes it. |
| **DLC / Data** | Length and bytes. |

A row with a name has an arrow: open it to see the decoded signals with their units. Signals are decoded
only for rows you actually open, so a busy bus stays responsive.

The toolbar has **Clear**, **Pause** (freezes the view while recording continues), **Follow** (keeps the
newest frame in view) and **Colour** (gives each identifier its own colour).

**Filter** takes identifiers, ranges and names: `7E0, 300-3FF, EngineData`. *Pass* shows only what
matches, *Stop* hides it, and **RX only** / **TX only** keeps one direction on top of either. **Find next**
searches the rows shown, and **Export...** writes them to CSV.

**Transport** turns the list from CAN frames into the diagnostic messages they carry. The ISO 15765-2
frames of one request or response — single frame, or a first frame and its consecutive frames — become a
single row: the direction, the identifier, the service name (`ReadDataByIdentifier`, `NegativeResponse`),
the length, and the whole payload. Flow control frames disappear, because they carry nothing. The
identifiers it follows are the request and response identifiers of the configuration you connected with,
so a message that spans twenty frames reads as one line, the way CANoe's transport view shows it.

**J1939** (the truck button) reads the 29-bit frames the J1939 way: each is named by its parameter group,
its source and its destination — `EEC1 (PGN 61444) 00 → Global` — also where no symbol database knows it,
and the filter takes parameter group names (`CCVS`, `TP.DT`; a name that is also a hexadecimal number, such
as `EEC1`, is read as an identifier). With **Transport** as well, a message the J1939 transport protocol
carries in several frames — a BAM to everyone, or an RTS/CTS session with one node — is one row, opening into
its TP.CM and TP.DT frames.

## Statistics

**Tools → Statistics...** counts what is on the bus.

| Column | Meaning |
|---|---|
| **ID** | The identifier; 29-bit identifiers end in `x`. |
| **Name** | The message name from the symbol databases, when one describes it. |
| **Dir** | `RX`, `TX`, or `RX/TX` when both are seen. |
| **Count** | Frames counted since the window opened, or since **Reset**. |
| **Frames/s** | The rate over the last three seconds. |
| **Cycle (ms)** | The average time between frames, with **Min** and **Max** beside it — an easy way to see a message that is late or jittery. |
| **Bus load** | What this identifier alone takes of the bit rate, stuffing bits included. |

The bytes themselves are in the Trace window, and the values they carry in the Data window.

The line underneath adds it all up: frames, identifiers, the time they were seen over, the total **bus
load**, the number of **error frames** and the state of the controller — *error active*, *error passive*
or *bus off*. It turns red when error frames appear or the controller goes bus off, which is how a
broken wire or a wrong bit rate shows itself.

The bus load needs a bit rate; it comes from the configuration you connected with, and the totals say
*bus load: set a bit rate* when there is none.

**Freeze** stops the table refreshing while the counting goes on, **Reset** starts from nothing, the
filter box narrows to an identifier or a name, and **Export...** writes the table as CSV. Click a column
heading to sort by it.

A replayed file is counted at the times in the file rather than by the clock on the wall, so the rates
and cycle times are the ones the recording was made with.

## Data window

**Tools → Data...** is every signal of the symbol databases with the value it holds now — CANoe's Data
window.

| Column | Meaning |
|---|---|
| **Signal** | The signal name. |
| **Value** | The physical value, scaling and offset applied; a value table shows its text. |
| **Unit** | From the database. |
| **Raw** | The value as it is on the bus, before scaling. |
| **Age (s)** | How long ago the last frame carrying it arrived. |
| **Count** | How many times it has been received. |
| **ID** | The message it comes in. |

Signals that have never arrived are listed with empty values, so you can see what the database expects
and is not getting. **Received only** hides them. The filter box narrows the list, **Clear** forgets the
values received so far, and **Export...** writes what is shown as CSV. Frames CAN Expert sends are
decoded as well, so a message you transmit shows the values you put in it.

## Using a panel

Each page of the loaded database is a window of the workspace, as CANoe's panels are: the first page in
the **Database** window, the others tabbed beside it. Drag a page's tab to put two pages side by side, or
float one onto a second screen; the saved desktops keep where they are. Buttons, switches, sliders and
input boxes send what their script or DBC binding says; displays, gauges, LEDs and trends show what
arrives. Everything the panel does is written in its Python script — see *Writing panel scripts*.

**Zoom** at the top of every page: **Fit** scales the page to its window and follows it as the window is
resized; **50 %** to **200 %** keep it at that size and scroll. **Ctrl + mouse wheel** steps the zoom. A page
keeps its zoom, also in a newer dated version of the database.

## Form Designer

**Tools → Form Designer** builds and edits panels. A panel is two files in `Databases/`: the layout
`family_YYYY-MM-DD.xml` and its script `family_YYYY-MM-DD_script.py`.

The window has a menu bar, the **Symbols & controls** panel on the left, the **Form**, **Python script** and
**Database** tabs in the middle and **Properties** on the right. The title shows the database ID, with a
**\*** while there are unsaved changes.

| Menu | What it holds |
|---|---|
| **File** | **New** (Ctrl+N), **Open...** (Ctrl+O), **Open from the Databases folder** (each family's newest version first), **Save** (Ctrl+S), **Save as...** (Ctrl+Shift+S, offering today's version of the family), **Close**. |
| **Edit** | Undo, redo, cut, copy, paste, duplicate, delete, select all — on the form, or in the script when its tab is in front. |
| **Arrange** | Align, make the same size, distribute, bring to front, send to back, and the grid. |
| **Page** | Add, rename and remove pages. |
| **Script** | **Check syntax** (F7), **Handler of the selected control** (F4). |
| **Test** | **Test panel with the simulated ECU** (F5, also the **Test panel...** button at the right of the menu bar), or on an empty bus (Shift+F5). |
| **Help** | This section of the manual (F1). |

New, Open and closing the window ask whether to save changes first.

**Building a form**
- Drag a control from the palette onto the page. There are inputs (button, switch, checkbox, radio, combo,
  slider, knob, spin box, I/O box), displays (value, 7-segment, gauge, progress bar, LED, multi-state
  indicator, trend, output box) and decorations (label, group box, picture).
- Load a **DBC** to get the signal list, then drag a signal onto the page: the control is bound to it and
  takes its unit and value table.
- Select controls (click, or rubber-band several), then use the toolbar to align, distribute, make the same
  size, change the stacking order, undo and redo. The last selected control is the reference for aligning.
  The grid button snaps to the grid.
- **Properties** edits the selected control: its binding, name, label, position, size and appearance.
- **+ Add page** adds a page; panels can have several.

**Connecting a control to code** — give it a *Handler function* in Properties, or double-click the control:
the script tab opens with the function created for you.

**The script tab** — the editor has completion (Ctrl+Space) for the API, your control names and DBC
signals, a syntax check, and the **UDS functions** panel listing every ISO 14229 service with its
documentation; double-click one to insert a call.

**The Database tab** — what the panel is and where it goes:
- **Database ID**: the file name, `family_YYYY-MM-DD`. The line under it says where it will be saved and how
  a configuration finds it — a configuration whose *Database family* is `engine` loads the newest `engine_`
  version — and warns about a name that cannot be a file, a missing date, or an ID that already exists.
  **New version (today)** keeps the family and takes today's date, so saving leaves the previous version as
  it was.
- **Name** and **Description** — the description is shown at the top of the Database window.
- **DBC**: the panel's own DBC, with **Browse...** and **Remove**; its signals are the Symbols list. When it
  lies near the panel — `DBC/` beside `Databases/`, say — it is saved relative to the panel, so the panel
  keeps working when the whole folder is copied to another PC.
- **Contents**: the pages with their controls, and handlers named on controls but missing from the script.
- **Where it is used**: the configurations with this family, and whether a newer version in the folder is
  the one they actually load.

**Test panel...** runs the panel against a simulated ECU on a virtual bus, without touching your hardware.
Its **Flashing...** opens the same dialog as the main window's Flashing button (see *Firmware flashing*):
the script's `Flashing` when it defines one, or the built-in sequence, with its settings, progress and
report.

## Writing panel scripts

A panel script is plain Python. The function named in a control's *Handler* is called when it is used, and
decorators react to events:

```python
def on_start_clicked(api, value):          # Handler of the "start" button
    api.can.send(0x200, [0x01])

@on_signal("EngineData.Temperature")       # a DBC signal changed
def temperature(api, value):
    api.ui.set_value("overheat", value > 80)

@on_timer(1.0)                             # every second
def tick(api):
    api.ui.set_value("uptime", RDBI(0x0100).int)
```

More events, as CAPL has them:

```python
@on_key("F5")                              # a key pressed in CAN Expert ("*": any key)
def hotkey(api, key):
    api.set_signal("EngineCommand.Start", 1)

@on_error_frame                            # an error frame on the bus
def trouble(api, timestamp):
    api.warn("error frame")

@on_bus_state                              # error active, error passive or bus off
def state(api, state):
    api.ui.set_value("bus", state)

@on_periodic_data(0xF201)                  # periodic data, after RDBPI(0x03, 0xF201); none named: all
def temperature(api, data, identifier):
    api.ui.set_value("temperature", int.from_bytes(data, "big") / 10)

@on_response_event(0x22)                   # what ROE set up: 62 F1 90 ... when the DID changed
def vin_changed(api, response):
    api.ui.set_value("vin", response[3:].decode())

@on_pgn(0xFECA)                            # J1939 DM1 from any node, whole even when it came in a BAM
def faults(api, message):                  # message.pgn, .source, .destination, .data
    api.ui.set_value("faults", len(message.data))

software = j1939.request(0xFEDA, 0x00)     # J1939: request SOFT from node 00; None when nothing answers
j1939.send(0xEF00, [1, 2, 3], 0x00)        # send a PGN; more than 8 bytes go as a BAM or RTS/CTS session
```

Keys reach the script while a measurement runs, but not while you type into a field or a dialog is open.

Every ISO 14229 service is available as a function: `RDBI(0xF190)` sends `22 F1 90` and returns a result
that is true for a positive response, with `.data`, `.text`, `.int`, `.hex()`, `.nrc` and `.error`.
`api` gives you `api.can.send`, `api.ui`, `api.signal/set_signal/send_message`, `api.log` /
`api.write` and `api.warn`, `api.marker("comment")` (see *Markers*), `api.sleep` and `api.dll`. Callbacks run one at a time on a
background thread and stop when you disconnect.

Older scripts may use `api.on`, `api.on_can`, `api.every`, `api.can.get_latest_messages` and the
`api.uds` helpers (`request`, `rdbi`, `request_download`, ...). They still work, but they are
**deprecated** and completion no longer offers them: use `@on_control`, `@on_message` and `@on_timer`,
and the service functions (`UDS`, `RDBI`, `RD`, `TD`, `RTE`, ...) instead.

**Tools → Write** is the script's own window: what `api.log`, `api.write` and `api.warn` say, and the
script's errors, each line with its time and a colour for warnings and errors. It can show only warnings
and errors, find text, and save what it holds. Its **Script variables** tab lists the script's global
variables and their values while it runs. The Debug log stays the application's; script errors appear in
both.

The full API is in [Requirements implementation](REQUIREMENTS_STATUS.md#panel-scripts), and
`examples/` holds a runnable panel and script.

## CAN Logger

**Tools → CAN Logger...** graphs DBC signals while you are connected.

1. **Load DBC...** — the signals of the file appear in the list.
2. **Tick a signal** — it gets its own graph. All graphs share one time axis, so signals line up.

The list is for choosing and measuring; the value each signal holds now is in the Data window.

The toolbar uses small symbols:

| Button | Symbol | What it does |
|---|---|---|
| **Clear** | a bin | Throws the recorded data away and restarts time at 0. |
| **Pause** | two bars, a play triangle while paused | Freezes the picture; recording continues, and resuming catches up. |
| **Follow** | an arrow meeting the right edge | Scrolls with the newest data. |
| **Fit** | four corner brackets | Shows everything recorded. |
| **Lock X** | a padlock over the horizontal axis | Mouse zoom and pan leave the time axis alone. |
| **Lock Y** | a padlock beside the vertical axis | Mouse zoom and pan leave the value axes alone. |
| **Cursors** | two cursor markers | Two measurement cursors across all graphs, with the statistics between them. |
| **Combine** | two curves in one frame | Draws every ticked signal in one graph instead of one each. |

Every button keeps its name in the tooltip, so hovering tells you which is which.

**Several signals in one graph** — **Combine** puts them all together; to choose, right-click a signal in
the list and pick *Draw together with ...* to move it into another signal's graph, or *Graph of its own*
to take it back out. Signals sharing a graph share its value axis and get a legend naming them; the axis
is labelled with their unit when they agree on one. Comparing a request with what it produced —
throttle against engine speed, say — is what this is for.

Hovering a graph shows a dotted crosshair with the time and value under the mouse.

**Cursors** — turn them on and drag the two dashed lines marked **#1** and **#2**. The bar above the graphs
shows both times and Δt, and the signal list gains *Cursor 1*, *Cursor 2* and *Δ* columns for every signal,
and *Min*, *Max*, *Mean* and *σ* of its samples between the two cursors.

**Graph options...**
- *Draw signals as*: **Line** holds each value until the next one (how an ECU signal behaves),
  **Line + dots** adds a dot per received sample, **Dots** shows only the samples.
- *Follow time window*: how many seconds Follow keeps on screen.
- *Fixed time range*: type exact bounds, e.g. from `50.0134 s` to `55.2455 s`. It turns Follow off.
- *Autoscale* or a *Fixed value range* for every graph. **Fit** clears both fixed ranges.

**Export...** writes the data as
- **Values in rows (CSV)** — time, signal, value, one row per sample;
- **One column per signal (CSV)** — a row for every moment any signal changed, each column holding its
  signal's value until the next sample;
- **MDF 4 (.mf4)** — the measurement format CANoe, CANape and most analysis tools open, with units and the
  measurement's start time;
- **Picture of the graphs (PNG)**.

For the data formats choose **Everything recorded**, **What is on screen** or **Between the cursors**, and
all signals or only those with a graph.

**Graph options...** also sets how many samples each signal keeps (1,000,000 by default); beyond that the
oldest go, so a long measurement cannot fill the memory. To keep everything, record to a file.

Frames are timed by the adapter, so the Logger, the Trace window and a recorded file all agree.

## Transmit window

**Tools → Transmit** sends messages, once or over and over. It has two tabs, and both keep sending while
the other is in front — or while another window's tab covers the Transmit window. **Closing the window
stops everything** it was sending, so nothing keeps going out of sight.

### Messages

The **Messages** tab is CANoe's Interactive Generator.

- **Add** makes a raw row; type its identifier, data bytes and cycle time straight into the table.
- **Add from database...** picks a message from the symbol databases, with its identifier and length.
- **Edit signals...** (or double-clicking the data of a database row) opens the message signal by signal,
  with value tables as lists, and re-encodes the bytes.
- Tick **On** to send that row every *Cycle (ms)*; **Send now** sends the selected row once; **All off**
  stops everything. *Sent* counts what went out.
- **Save list...** and **Load list...** keep sets of rows as JSON files; the current list is remembered.

A row that cannot be sent — because nothing is connected, say — switches itself off and shows why,
instead of repeating the error.

### Simulated nodes

The **Simulated nodes** tab sends the messages of an ECU that is not on the bench, so the one that is
believes the rest of the car is there. It is CANoe's rest-bus simulation in small.

The tree comes from the symbol databases: a branch per sending node, with the messages it sends
underneath, each with the cycle time out of the database (100 ms when it says nothing).

- **Tick a message** to include it, or select a node and press **Tick node** to take all of its messages
  at once; **Untick node** drops them again.
- **Edit signals...** (or double-clicking a row) sets what the message carries, signal by signal, the
  same editor the Messages tab uses. *Cycle (ms)* can be typed over.
- **Start sending** puts every ticked message on the bus at its cycle time; the button stays pressed in
  while it runs and *Sent* counts what went out. **Send once** sends the selected message a single time.

What was ticked is remembered for the next time. A message that cannot go out — nothing connected, or the
adapter refusing — stops the simulation and says why, rather than filling the log.

## UDS Console

**Tools → UDS Console** sends diagnostic services using the session the main window has open; connect
first. It has four tabs over one log.

- **Services** lists every ISO 14229 service by functional unit, exactly as the panel scripts see them,
  without needing an ODX file. Pick one and its parameters appear as a form, with the defaults filled in;
  press **Send**. **or raw:** sends bytes you type, e.g. `22 F1 90`.
- **ODX** sends the services an ODX file describes. **Load ODX / CDD...** and pick an `.odx`, `.pdx` or CDD
  file (the default folder is `ODX/`); choose a service in the tree, fill its free parameters — fixed
  ones are shown as *(coded/fixed)* — and press **Send**.
- **Fault memory** reads the DTCs with their status bits spelled out (`confirmedDTC, testFailed`), counts
  them, reads a **Snapshot** or **Extended data** record for the selected DTC, and clears them all. Each
  DTC shows its code as it is written on a scan tool (`P0101-00`: the system letter, four characters and
  the failure type). With the ECU's ODX, PDX or CDD file loaded in the ODX tab — before or after reading —
  the **Description** column says what each DTC means, from the file's DTC texts. `ODX/dummy_ecu.odx-d`
  describes the Dummy ECU's two DTCs.
- **Periodic & events** asks the ECU to send data by itself and lists what it sends. **Start** sends the
  periodic identifiers you name (`F201 F202`) at the chosen rate (ReadDataByPeriodicIdentifier, 0x2A);
  **Stop** ends them (all of them, with the field empty). **Set up** arranges a ResponseOnEvent (0x86):
  the ECU answers `22 <DID>` when that DID changes, or `19 02 <mask>` when a DTC's status bits in the mask
  go on; **Start**, **Stop**, **Report** and **Clear** act on the events set up. The table counts each
  periodic identifier and each event's service, with the last data and when it came; event responses are
  also written to the log. CAN Expert answers a long event response with flow control as a tester must, so
  a 17-byte VIN arrives whole. Nothing is listed on a listen-only channel, where CAN Expert cannot answer.
- The log shows every request and its response, each line with its time (see *View → Time display*): a
  positive answer with its data as hex, as a number and as text; a negative one as
  `NRC 0x31 requestOutOfRange`. Once an ODX file is loaded, answers are also shown **decoded** by it, from
  whichever tab the request came. Requests and answers that span several frames are handled for you, and an
  ECU answering *busyRepeatRequest* (NRC 0x21) is asked again, up to three times, before you see the NRC.
- The first bar sets the **session** (DiagnosticSessionControl); the connection itself keeps the ECU awake
  with TesterPresent. Beside it a strip says what is going on: `Session: extended   P2 75 ms / P2* 4000 ms   Security:
  unlocked (level 1)`. P2 and P2* are what the ECU itself asked for in its answer to the session
  request, and every later request waits that long — an ECU that needs three seconds gets three seconds,
  and one that asks for less never makes CAN Expert less patient than the configuration says.
  Going back to the default session locks the ECU again, and the strip says so.
- The second bar unlocks **SecurityAccess**: give the level, then choose how the key is worked out.
  *key = seed XOR mask* is the rule the simulated ECU uses. *seed & key DLL* calls a real ECU's
  `GenerateKeyEx` DLL instead — **Browse...** to it and give the variant if it wants one. Nothing is
  sent when the DLL cannot be loaded; the log says what was wrong with it.

The frames of an exchange are in the Trace window; its **Transport** view shows each request and answer
as one row.

## Test modules

**Tools → Test** runs test cases written in Python against the ECU, as CANoe's test modules do, and
writes a report of every run. It opens `TestModules/dummy_ecu_checks.py`, the example, until you open
another module with **Open...**; the one used last is opened again.

Connect first. Tick the test cases to run and press **Run**: each one appears with its verdict — *passed*,
*failed*, *error* (the test itself broke) or *skipped* — and under it every step with its own verdict, as
it happens. **Stop** ends the run after the current step; the rest are skipped, but the module's clean-up
still runs. The module is read again before every run, so you can edit it in any editor and run it again
straight away (**Reload** shows the new list without running).

Every run writes two reports into `reports/` beside the module, named after it and the time:
an **HTML** page (**Open report**) with the verdict, the counts, and each test case's steps — the ones that
did not pass are opened — and a **JUnit XML** file that CI servers such as Jenkins or GitLab read.

A test module is a Python file:

```python
"""Dummy ECU checks"""                          # the first line is the module's title

def setup(t):                                    # before the test cases; if it fails, they are skipped
    t.require(DSC(0x01), "the ECU answers")

def teardown(t):                                 # after them, also when one failed or you pressed Stop
    DSC(0x01)

@testcase("The VIN has 17 characters")           # a test case, in the order the file lists them
def vin(t):
    vin = RDBI(0xF190)
    t.require(vin, "VIN read")                   # a failed require ends the test case
    t.check_equal(len(vin.data), 17, "length")   # a failed check fails it, and the next step still runs

@testcase("An unknown DID is refused")
def unknown(t):
    t.expect_nrc(RDBI(0x1234), 0x31)
```

`before_each(t)` and `after_each(t)` run around every test case. The UDS functions are the ones panel
scripts use (`RDBI`, `DSC`, `SecurityUnlock`, `UDS("22 F1 90")`...). What `t` offers:

| | |
|---|---|
| `t.check(condition, "step", detail)` | A step that passes when the condition is true — a positive UDS answer is; its detail shows the request and the answer |
| `t.check_equal(actual, expected, "step")`, `t.check_range(value, low, high, "step")` | The step's detail says what was expected and what came |
| `t.expect_nrc(result, 0x31, "step")` | Passes when the ECU answered with that negative response code |
| `t.require(condition, "step")` | A check that ends the test case when it fails |
| `t.fail("why")`, `t.skip("why")`, `t.log("text")` | Fail or skip the test case; a line in the report without a verdict |
| `t.wait(seconds)` | Wait, and stop at once when Stop is pressed |
| `t.send(0x200, [1, 2])` | Send a frame |
| `t.marker("before the reset")` | A marker in the measurement (Trace, Logger, recording) and a line in the report |
| `j1939.request(0xFEEC, 0x00)`, `j1939.send(pgn, data, 0x00)` | J1939, as in panel scripts: the answer (`.data`, `.source`, `.acknowledgment`) or `None` |
| `t.wait_for_frame(0x300, timeout=2)` | The next frame of that identifier (`frame.data`, `frame.signals` decoded with the symbol databases), or `None` |
| `t.wait_for_signal("EngineData.Temperature", lambda value: value > 80, timeout=5)` | The value of the signal in the next frame that carries it (and meets the condition), or `None` |

A wait takes the frames that arrive after it starts — or after the test's last `t.send()`, so an answer
that comes back before the wait begins is not missed.

## J1939

**Tools → J1939** (**Ctrl+9**) is for SAE J1939 networks — trucks, buses, agricultural and construction
machines — where every node has an address it claims with its 64-bit NAME, and data travels in parameter
groups (PGNs) on 29-bit identifiers. It watches the measurement's frames and takes part itself from **CAN
Expert's address** (F9, the off-board diagnostic tool, by default; remembered, and used by scripts and test
modules too). Connect first.

- **Network** lists the nodes that claimed an address: the address, what the NAME says (function —
  *Engine*, *Transmission*... —, manufacturer, identity, industry group) and when. **Request address
  claims** asks every node to claim again, so a node that was already there shows up.
- **Faults (DM1)** lists each node's active faults as it sends them in DM1: the SPN (the parameter, with a
  name for common ones), the FMI (what is wrong with it), the occurrence count and the lamps on (malfunction
  indicator, red stop, amber warning, protect). Pick a node to **Read previously active (DM2)**, **Clear
  active (DM11)** or **Clear previously active (DM3)**; the node's acknowledgment is written in the log.
- **Request and send** requests any PGN from a node (or from everyone, `FF`) and shows the answer — the
  software identification, the VIN or the component identification as text, faults as SPN/FMI, anything a
  symbol database describes as signals — or the node's refusal (NACK). **Send** puts a PGN with your data on
  the bus.

Messages longer than eight bytes use the transport protocol by themselves: a request answered with a BAM or
with an RTS/CTS session (CAN Expert answers the RTS with CTS and acknowledges the end), and data you send to
one node goes in an RTS/CTS session, to everyone in a BAM. The Trace window's **J1939** button shows the same
traffic by parameter group, and the CAN Logger plots the signals of a J1939 DBC from any source address.

## Firmware flashing

While connected, the **Flashing** toolbar button appears. There are two ways to flash, and the button
offers whichever are available.

1. Press **Flashing** and choose an S-record (`.s19`, `.s28`, `.s37`) or Intel HEX (`.hex`) file.
2. The dialog lists the file, its size and the address ranges to be written, and asks how to flash it:
   - **With the panel script's `Flashing(api, firmware)`** — offered when the loaded database's script
     defines one. What happens is then entirely up to the script, which is the way to handle a
     bootloader that does something unusual.
   - **With the built-in ISO 14229 sequence** — no script needed. This is the sequence most bootloaders
     want: extended session, DTCs off, normal messages off, programming session, security access, then
     for every segment an erase routine, RequestDownload, TransferData blocks and RequestTransferExit,
     and finally the dependency check routine, the messages and DTCs back on, an ECU reset and a read of
     the software version.
3. Watch the progress dialog; **Cancel** stops after the block being sent.

**Sequence settings...** opens what the built-in sequence uses, and every part of it can be changed to
match your ECU: the session numbers, whether DTCs and normal messages are switched off, the
SecurityAccess level and how the key is worked out (a mask, or a `GenerateKeyEx` DLL), the erase and
dependency check routine identifiers, the address and length format, the data format, how many bytes go
in one TransferData (*as much as the ECU allows* uses the maxNumberOfBlockLength it announces), the reset
type and the DID read afterwards, and whether the image's CRC-32 goes to the dependency check (for a
bootloader that checks it). **0** leaves a step out altogether. **Save profile...** keeps the
settings in a JSON file you can hand round with the firmware, and **Load profile...** reads one back; the
last settings used are remembered anyway.

When it is over — either way — a **report** is written beside the firmware file as
`<firmware>.flash-report.txt`: the file and its address ranges, the settings it ran with, every step with
its answer, and how it ended. A run that fails keeps the steps that did happen, which is what you want
when an ECU refuses halfway.

Keep the connection and ECU power stable until it finishes.

## Recording and replaying

**Connection → Record to file...** writes every frame to a file while you are connected; the format
follows the name you give it — `.blf` (Vector binary), `.asc` (Vector ASCII), `.csv`, `.log` or `.trc`.
**Stop recording** closes it, and so does Disconnect. The status bar says how many frames were written.

**Connection → Replay a recorded file...** plays a file back into the Trace window, the CAN Logger and
the panels, with no bus involved at all: the Trace header shows **Offline**. Choose the speed — real
time, 2x, 5x, 10x, or as fast as possible — then **Start**; **Stop** ends it. Nothing is transmitted, so
you can study a recording made in a vehicle at your desk.

### Markers

**Connection → Insert marker...** (**Ctrl+M**) marks this moment of the measurement with a comment — "door
opened", "engine started" — and **Quick marker** (**Ctrl+Shift+M**) marks it at once as *Marker 3*. A marker
is a highlighted row in the Trace window at its time among the frames (whatever the filter, and **Find**
finds it), a dashed line across every graph of the CAN Logger with its comment on the top one, and a line
in the Log. A Trace or a Logger opened later shows the markers too.

While recording, the marker goes into the file where the format has a place for it: a `.blf` gets a global
marker, which CANoe shows on its time axis, and an `.asc` or `.trc` a comment line. `.csv` and `.log` keep
the frames only. A replay in CAN Expert shows the frames, not the markers.

A panel script marks with `api.marker("comment")`, a test module with `t.marker("comment")` (see *Test
modules*).

## Arranging the windows

The middle of the main window is the **workspace**, where the pages of the loaded database and the
analysis windows — Trace, Statistics, Data, CAN Logger, Transmit, UDS Console, Write and Test — live.
Configuration, CAN Channels and Log stay
as fixed panels around it.

Workspace windows behave as they do in CANoe:

- **Drag a window by its tab** to move it. While you drag, drop marks appear: the ones in the middle of
  a window split that window above, below, left or right of it, or drop onto the centre to **tab** the
  two together; the ones at the edge of the workspace dock it against that edge instead.
- **Several windows in one area** share a tab bar. The **▾** button on the right of the area lists its
  tabs, the **⧉** button pulls the area out as a floating window, and **✕** closes it.
- **Drag a tab out of the window** to float it on its own; a floating window can hold several tabs, and
  dragging it back over the workspace docks it again. A floating window too small to use, or left on a
  screen that is no longer there, opens at a usable size on a screen you have.
- A **✕** on a tab closes that window. Closing keeps it: its toolbar button goes back to idle, and
  reopening it shows everything it recorded meanwhile.

The arrangement, including the window size, is saved when you close CAN Expert and restored next time.

**View → Save desktop as...** keeps the current arrangement under a name — one for analysis, one for
diagnostics, one for testing — and **View → Desktops** switches between them. A desktop stores both the
fixed panels and the workspace windows. **Reset layout** goes back to how the window starts.

## Trying it without a vehicle

`dummy_ecu.py` is a simulated ECU. With the Kvaser virtual driver its two channels are connected, so the
ECU runs on one and CAN Expert on the other.

1. Double-click `dummy_ecu.py` to open the **Dummy ECU** window.
2. Choose the interface and channel (**Detect** lists them; `kvaser` channel `1`) and press **Connect**.
3. In CAN Expert pick the **Dummy ECU** configuration, select `[kvaser] Ch 0` and connect.

The ECU answers sessions, security access, DIDs, DTCs, periodic data, events, memory and I/O control, and
the whole flashing sequence. Its window sets everything it does; every setting applies at once, even while
it runs, and the settings can be saved as profiles. The right side shows what the ECU is doing — session,
security levels unlocked, whether its application is valid, periodic data, events, I/O control, the
operation cycle — and **Show CAN frames** logs every frame, flow control included.

| Tab | What it sets |
|---|---|
| **Addressing**, **Flow control** | The identifiers, extended addressing and padding; block size, STmin, WAIT frames and receive buffer. |
| **UDS** | P2/P2*, the response delay, the S3 timeout, the sessions, and the rates of periodic data. |
| **Access** | Security levels and how each key is worked out; rules that allow a service only in some sessions or after unlocking a level. |
| **Flashing** | TransferData size and formats, memory ranges, the routines, and the **bootloader**. |
| **Signals** | The messages the ECU sends, and a generator for each signal. |
| **Data** | The DIDs, the DTCs with their faults, the fault memory's cycles, and forced negative responses. |
| **Errors** | Transport errors on purpose. |

### Its application frames

The **Signals** tab chooses the DBC whose messages the ECU sends. **Built-in** is `DBC/dummy_ecu.dbc` —
`0x300` EngineData and `0x301` EcuStatus, which the example panels use — and **Browse...** takes any other.
Each message is sent at its own period (the table's, else the DBC's `GenMsgCycleTime`, else the default
period) and can be switched off. Each signal gets a **generator**:

| Generator | What the signal does |
|---|---|
| Constant | Stays at *Low*. |
| Ramp | Climbs from *Low* to *High* in *Period* seconds, then starts again. |
| Sine | A wave between *Low* and *High*, one per *Period*. |
| Square | *High* for half of *Period*, *Low* for the other half. |
| Random | A new value between *Low* and *High* in every frame. |
| Counter | One more in every frame, from *Low* to *High* and round again. |
| Engine running | *High* while the engine runs (`0x200 01`), *Low* once it stops (`0x200 02`), approached with *Period* as time constant. |
| Logging | *High* while logging is on (`0x201`, bit 0). |
| Session | The diagnostic session. |

Values are physical, in the signal's unit; **Now** shows what goes out. The built-in database starts with
the ECU's usual traffic: the temperature warms up to 85 °C while the engine runs, the pressure follows,
and a counter counts. The frames stop while CommunicationControl switches normal messages off and while
the bootloader runs.

### Periodic data and events

The **F2xx** DIDs can be sent periodically: `2A 03 01` sends F201 fast, `2A 01 02` F202 slow, `2A 04`
stops them. The rates are on the **UDS** tab. The frames are `6A` messages on the response ID, or frames
of an identifier of their own. ResponseOnEvent answers unasked: `86 03 02 01 01 22 01 01` sets up an event
on DID `0101`, `86 01 02 08 19 02 08` one on DTCs becoming confirmed, `86 05 02` starts them and
`86 00 02` stops them. Periodic data and events end when the session changes.

### Security and access

The **Access** tab has the main security level and as many more as needed, each with its own seed length
and key: seed XOR a mask, or what a **seed & key DLL** (`GenerateKeyEx`) computes — give the UDS Console
the same DLL and it unlocks. Each level unlocks on its own, until the session changes. A DID can be
readable only in some **Sessions** (elsewhere NRC `0x31`) or after unlocking a **Level** (NRC `0x33`) —
DID `0200` shows both — and a **service rule** does the same for a whole service, with NRC `0x7F` and
`0x33`.

### Faults and the fault memory

Tick **Fault** beside a DTC on the **Data** tab and its test fails, as ISO 14229 describes: the DTC is
pending at once, confirmed once it has failed in the number of operation cycles set under *Fault memory*,
and it asks for the warning lamp. The snapshot record takes the **Snapshot DIDs** with their values at
that moment, and the occurrence counter — the extended data's first byte — counts one more. Untick it and
the DTC heals: no longer pending after a clean cycle, aged out after more. **Now** shows the status as it
is. An operation cycle ends with an ECUReset, **New operation cycle**, or a timer. ControlDTCSetting off
freezes every status; ClearDiagnosticInformation starts them again, and a fault still present comes
straight back.

### Memory and I/O control

A DID with a **Signal** answers that signal's value. InputOutputControlByIdentifier takes the signal
over in the extended session: `2F 01 01 03 03 E8` holds the temperature at 100.0 °C and the `0x300`
frames carry it, `2F 01 01 00` hands it back; *freezeCurrentState* and *resetToDefault* work too.
ReadMemoryByAddress (`23`) and WriteMemoryByAddress (`3D`, extended session and unlocked) read and write
the ECU's memory — the flashed image included.

### The bootloader

An erase or a download leaves the application invalid until checkProgrammingDependencies passes. An
ECUReset with an invalid application — a flash that failed or was abandoned — starts the **bootloader**:
no application frames, `F195` answers `BOOTLOADER`, and only what is needed to flash again is answered.
A good flash and a reset start the application again. The **Image check** can require the CRC-32 of the
image — as the check routine's option record, which the built-in flashing sequence sends when asked, or
in the image's last four bytes — and the software version can be read from the image itself (the demo
image has its name at `00020000`).

### J1939

On **Addressing**, **A J1939 node as well** makes the dummy ECU a J1939 node beside its UDS side: it claims its
**Source address** (00, engine #1, by default) with its **NAME**, and gives it up to a node claiming it with
a lower NAME (it then sends Cannot Claim Address and stays quiet). It sends DM1 every second with the active
faults of its fault memory — P0101 is SPN 132 FMI 2, U0100 SPN 639 FMI 9 —, in a BAM when there are several,
lighting the amber lamp while a fault is active. It answers requests for Address Claimed, DM1, DM2, SOFT
(its software version), VI (its VIN), CI and the PGNs it broadcasts, clears its faults on DM11 and DM3, and
sends a NACK for anything else asked of it alone. Choose `DBC/j1939_demo.dbc` on the Signals tab and it
sends EEC1, CCVS and ET1 from its address, with an engine speed, a vehicle speed and temperatures that move.

### Errors on purpose

The **Errors** tab gives each response a chance of going wrong: refused (NRC `0x21` busyRepeatRequest by
default), not answered, answered on another identifier, or — for a long response — a consecutive frame
dropped, out of sequence or late. TesterPresent is spared unless you tick it, so the ECU stays in CAN
Expert's node list while the other requests go wrong.

Several dummy ECUs can share a channel when each has its own identifiers (Addressing) and only one sends
the application frames. A second ECU answering the *same* requests is refused, because two ECUs answering
them break security access and flashing.

## Keyboard shortcuts

| Key | Does |
|---|---|
| **F9** / **Shift+F9** | Connect / Disconnect |
| **Ctrl+1** ... **Ctrl+9** | Trace, CAN Logger, Data, Statistics, Transmit, UDS Console, Write, Test, J1939 — the toolbar's order; pressed again, the window closes |
| **Ctrl+E** | Form Designer |
| **Ctrl+R** / **Ctrl+Shift+R** | Record to a file / Stop recording |
| **Ctrl+O** | Replay a recorded file |
| **Ctrl+M** / **Ctrl+Shift+M** | Insert a marker with a comment / a numbered marker at once |
| **Ctrl+N** | New configuration |
| **F1** | The manual, at the section of the window you are working in |
| **Ctrl+Q** | Exit |

The keys work in floating windows too. The toolbar buttons show theirs in their tooltips. In the Form
Designer, **F1** opens its own section, **F5** tests the panel with the simulated ECU and **F7** checks the
script. Plain letters and **F5** are left to the panel script's `@on_key`: keys CAN Expert uses itself
do not reach the script.

## Where things are kept

| Folder | Contents |
|---|---|
| `Configurations/` | `config_<name>.json`, one per configuration |
| `Databases/` | Panels: `family_YYYY-MM-DD.xml` and `family_YYYY-MM-DD_script.py` |
| `DBC/` | DBC files for the Trace window, the Logger, the Transmit list, the designer and panel bindings (`j1939_demo.dbc`: an engine's J1939 parameter groups) |
| `ODX/` | ODX, PDX and CDD files for the UDS Console's ODX tab (`dummy_ecu.odx-d`: the Dummy ECU's DTC texts) |
| `examples/` | A runnable panel and script, and demo firmware images |
| `TestModules/` | Test modules for the Test window (`dummy_ecu_checks.py` is the example); each run's reports go to `reports/` beside the module |

Recordings go wherever you save them; `.blf` is the most compact.

The window arrangement and its saved desktops, the theme, the symbol databases, the transmit list, the
simulated nodes' ticked messages, the flashing sequence settings, each configuration's ISO-TP settings, each
channel's setup, the panel zooms, the time display and the receiver used last are all remembered
between runs.

## If something does not work

**No CAN receivers found** — the adapter's driver is not installed or the adapter is not plugged in.
CAN Expert supports Kvaser, Vector and IXXAT through python-can. Press **Refresh** after connecting it.

**The ECU shows Lost connection** — check the bitrate, the SERVER ID and ECU ID, and that the ECU is
powered. The Trace window tells you whether anything is talking on a channel at all, and **Find the bit
rate** (*Channel setup*) whether the bit rate is right.

**"No matching database"** — the configuration's *Database family* does not match any file in
`Databases/`. Clear the field to load the newest panel, or build one in the Form Designer.

**The Trace shows identifiers but no names** — no symbol database describes those messages. Add the DBC
under *Tools → Symbol databases...*.

**The Flashing button stays greyed out** — flashing needs a connection; connect first. A script without
`Flashing(api, firmware)` only means the built-in sequence is the one offered.

**Graphs stay empty** — the Logger only draws signals from the loaded DBC that are actually received, and
only while you are connected.

**Light or dark** — *Options → Light Mode / Dark Mode*. The choice is remembered.

**Reporting a problem** — *Help → About* lists the versions of CAN Expert, Python, Qt, python-can and the
other libraries, and of the Kvaser, Vector and IXXAT drivers installed; **Copy** puts them on the
clipboard to paste into the report.
