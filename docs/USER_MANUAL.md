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

The tool windows — Trace, Statistics, Data, CAN Logger, Transmit, UDS Console, Write, System
Variables — open in the **workspace** in
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

The window has the **Symbols & controls** panel on the left, the form in the middle and **Properties** on
the right.

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

**Save** writes both files; **Load** opens an existing panel; **New** starts an empty one.

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

@on_sysvar("Engine::TargetSpeed")          # a system variable changed
def target(api, value):
    api.set_signal("EngineCommand.Speed", value)
```

Keys reach the script while a measurement runs, but not while you type into a field or a dialog is open.

Every ISO 14229 service is available as a function: `RDBI(0xF190)` sends `22 F1 90` and returns a result
that is true for a positive response, with `.data`, `.text`, `.int`, `.hex()`, `.nrc` and `.error`.
`api` gives you `api.can.send`, `api.ui`, `api.signal/set_signal/send_message`, `api.sysvar`,
`api.log` / `api.write` and `api.warn`, `api.sleep` and `api.dll`. Callbacks run one at a time on a
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

## System variables

**Tools → System Variables** lists values the script, the windows and you share, named
`Namespace::Name` (`Engine::TargetSpeed`), as CANoe's system variables are.

- The script sets and reads them — `api.sysvar.set("Engine::TargetSpeed", 1200)`,
  `api.sysvar["Engine::TargetSpeed"]` — and reacts with `@on_sysvar`. Setting one that does not exist
  yet creates it.
- **Double-click a value** in the window to type a new one; the script hears it at once.
- **New...** defines one with its type (float, int, bool, text), initial value, unit and comment;
  **Edit...**, **Remove**, and **Load...** / **Save...** as a JSON file to share a set.
- The CAN Logger plots the numeric ones from its **System variables** branch, next to the bus signals.

Every variable starts again from its initial value when you connect.

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
first. It has three tabs over one log.

- **Services** lists every ISO 14229 service by functional unit, exactly as the panel scripts see them,
  without needing an ODX file. Pick one and its parameters appear as a form, with the defaults filled in;
  press **Send**. **or raw:** sends bytes you type, e.g. `22 F1 90`.
- **ODX** sends the services an ODX file describes. **Load ODX / CDD...** and pick an `.odx`, `.pdx` or CDD
  file (the default folder is `ODX/`); choose a service in the tree, fill its free parameters — fixed
  ones are shown as *(coded/fixed)* — and press **Send**.
- **Fault memory** reads the DTCs with their status bits spelled out (`confirmedDTC, testFailed`), counts
  them, reads a **Snapshot** or **Extended data** record for the selected DTC, and clears them all.
- The log shows every request and its response, each line with its time (see *View → Time display*): a
  positive answer with its data as hex, as a number and as text; a negative one as
  `NRC 0x31 requestOutOfRange`. Once an ODX file is loaded, answers are also shown **decoded** by it, from
  whichever tab the request came. Requests and answers that span several frames are handled for you.
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
type and the DID read afterwards. **0** leaves a step out altogether. **Save profile...** keeps the
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

## Arranging the windows

The middle of the main window is the **workspace**, where the pages of the loaded database and the
analysis windows — Trace, Statistics, Data, CAN Logger, Transmit, UDS Console, Write, System
Variables — live. Configuration, CAN Channels and Log stay
as fixed panels around it.

Workspace windows behave as they do in CANoe:

- **Drag a window by its tab** to move it. While you drag, drop marks appear: the ones in the middle of
  a window split that window above, below, left or right of it, or drop onto the centre to **tab** the
  two together; the ones at the edge of the workspace dock it against that edge instead.
- **Several windows in one area** share a tab bar. The **▾** button on the right of the area lists its
  tabs, the **⧉** button pulls the area out as a floating window, and **✕** closes it.
- **Drag a tab out of the window** to float it on its own; a floating window can hold several tabs, and
  dragging it back over the workspace docks it again.
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

The ECU answers sessions, security access, DIDs, DTCs and the whole flashing sequence. Its window sets
everything it does: addressing, ISO-TP flow control (block size, STmin, WAIT frames), UDS timing and
security, and the flashing rules (TransferData size, accepted formats, memory ranges, routines). Settings
apply immediately and can be saved as profiles. **Show CAN frames** logs every frame, flow control
included.

The **Data** tab edits what the ECU answers with, while it runs: the **DIDs** (data as hex, shown as text
where it is text, each writable or not), the **DTCs** with their status, snapshot record and extended data
record (so the UDS Console's *Snapshot* and *Extended data* buttons get answers), and **forced negative
responses** — a service always answered `7F <service> <NRC>`, to see how a tester copes with a refusal.
They are part of the ECU's profile.

Several dummy ECUs can share a channel when each has its own identifiers (Addressing) and only one sends
the periodic frames. A second ECU answering the *same* requests is refused, because two ECUs answering
them break security access and flashing.

## Where things are kept

| Folder | Contents |
|---|---|
| `Configurations/` | `config_<name>.json`, one per configuration |
| `Databases/` | Panels: `family_YYYY-MM-DD.xml` and `family_YYYY-MM-DD_script.py` |
| `DBC/` | DBC files for the Trace window, the Logger, the Transmit list, the designer and panel bindings |
| `ODX/` | ODX, PDX and CDD files for the UDS Console's ODX tab |
| `examples/` | A runnable panel and script, and demo firmware images |

Recordings go wherever you save them; `.blf` is the most compact.

The window arrangement and its saved desktops, the theme, the symbol databases, the transmit list, the
simulated nodes' ticked messages, the flashing sequence settings, each configuration's ISO-TP settings, each
channel's setup, the system variable definitions, the panel zooms, the time display and the receiver used
last are all remembered between runs.

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
