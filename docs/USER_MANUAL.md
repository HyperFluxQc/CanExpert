# CAN Expert user manual

CAN Expert connects to a CAN bus, shows which ECUs answer, and runs a **panel** — a page of controls
driven by a Python script. It also has a Trace window for every frame on the bus, a CAN Logger to graph
DBC signals, a Transmit list to send messages, a UDS Console for diagnostic services and fault memory, a
Form Designer to build panels, an ODX Diagnostic Window, recording and offline replay, firmware flashing
over UDS, and a simulated ECU so you can try everything without a vehicle.

Press the **?** button at the top right of the main window to open this manual at any time.

## Starting up

Run `python main.py`, or start the built executable.

The main window has a toolbar and four panels:

| Panel | What it holds |
|---|---|
| **Configuration** | Your connection configurations. The one used last is selected again. |
| **CAN Channels** | The CAN receivers found on this computer, the ECUs that answer on them, and the database each one can load. |
| **Database** | The panel of the loaded database, with its controls. It appears once you connect. |
| **Log** | *Debug / Verbose* for application messages, *CAN Monitor* for the frames sent and received. |

The tool windows — Trace, CAN Logger, Transmit, UDS Console, Diagnostics — open as further panels of
the same window, so you can arrange them side by side (see *Arranging the windows*).

Each panel has a **–** button to shrink it to a strip and **×** to close it; the *File* menu brings a
closed one back. While a database is loaded, the side panels shrink automatically to leave it room.

A button that switches an option on — the toggles in the Trace window, the CAN Logger and the Form
Designer — stays pressed in with a coloured line under it for as long as that option is active, so you
can see at a glance what is switched on.

At startup CAN Expert selects the receiver you used last, shows every receiver you have connected to
before in **bold**, and starts asking its ECUs whether they are there (see *Checking ECUs* below).

## Configurations

A configuration describes how to talk to an ECU. Double-click one to edit it, or use **New**.

| Field | Meaning |
|---|---|
| **Name** | The name in the list, and the file name in `Configurations/`. |
| **Bitrate** | Bus speed. It must match the vehicle or bench (500000 is the most common). |
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

**Import** and **Export** copy a configuration file in or out; the files live in `Configurations/`.

## Connecting

1. Pick a configuration.
2. Pick a receiver in **CAN Channels** (**Refresh** re-scans the computer, **Scan Activity** listens on each
   one briefly and marks it *traffic* or *no traffic*).
3. Click **Connect**.

The matching database is loaded and its panel is built *before* the adapter is opened, so a broken panel
never leaves you half-connected. The channel is then marked **[Connected]**, and CAN Expert sends
TesterPresent at the configured interval.

**Double-clicking** a receiver, an ECU under it, or the database offered under it connects straight away —
the same as pressing Connect.

**Disconnect** stops the script and the traffic, and closes the adapter.

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
matches, *Stop* hides it. **Find next** searches the rows shown, and **Export...** writes them to CSV.

## Using a panel

The **Database** panel shows the controls of the loaded database. Buttons, switches, sliders and input
boxes send what their script or DBC binding says; displays, gauges, LEDs and trends show what arrives.
Everything the panel does is written in its Python script — see *Writing panel scripts*.

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

**Test panel...** runs the panel against a simulated ECU on a virtual bus, without touching your hardware —
including **Flashing...** if the script defines `Flashing`.

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

Every ISO 14229 service is available as a function: `RDBI(0xF190)` sends `22 F1 90` and returns a result
that is true for a positive response, with `.data`, `.text`, `.int`, `.hex()`, `.nrc` and `.error`.
`api` gives you `api.can`, `api.uds`, `api.ui`, `api.signal/set_signal/send_message`, `api.log`,
`api.every`, `api.sleep` and `api.dll`. Callbacks run one at a time on a background thread and stop when
you disconnect.

The full API is in [Requirements implementation](REQUIREMENTS_STATUS.md#panel-scripts), and
`examples/` holds a runnable panel and script.

## CAN Logger

**Tools → CAN Logger...** graphs DBC signals while you are connected.

1. **Load DBC...** — the signals of the file appear in the list.
2. **Tick a signal** — it gets its own graph. All graphs share one time axis, so signals line up.

The toolbar uses small symbols:

| Button | Symbol | What it does |
|---|---|---|
| **Clear** | a bin | Throws the recorded data away and restarts time at 0. |
| **Pause** | two bars, a play triangle while paused | Freezes the picture; recording continues, and resuming catches up. |
| **Follow** | an arrow meeting the right edge | Scrolls with the newest data. |
| **Fit** | four corner brackets | Shows everything recorded. |
| **Lock X** | a padlock over the horizontal axis | Mouse zoom and pan leave the time axis alone. |
| **Lock Y** | a padlock beside the vertical axis | Mouse zoom and pan leave the value axes alone. |
| **Cursors** | two cursor markers | Two measurement cursors across all graphs. |

Every button keeps its name in the tooltip, so hovering tells you which is which.

Hovering a graph shows a dotted crosshair with the time and value under the mouse.

**Cursors** — turn them on and drag the two dashed lines marked **#1** and **#2**. The bar above the graphs
shows both times and Δt, and the signal list gains *Cursor 1*, *Cursor 2* and *Δ* columns for every signal.

**Graph options...**
- *Draw signals as*: **Line** holds each value until the next one (how an ECU signal behaves),
  **Line + dots** adds a dot per received sample, **Dots** shows only the samples.
- *Follow time window*: how many seconds Follow keeps on screen.
- *Fixed time range*: type exact bounds, e.g. from `50.0134 s` to `55.2455 s`. It turns Follow off.
- *Autoscale* or a *Fixed value range* for every graph. **Fit** clears both fixed ranges.

**Save CSV...** writes everything decoded — time, signal, value — not only what is on screen.

Frames are timed by the adapter, so the Logger, the Trace window and a recorded file all agree.

## Transmit window

**Tools → Transmit...** sends messages, once or over and over — CANoe's Interactive Generator.

- **Add** makes a raw row; type its identifier, data bytes and cycle time straight into the table.
- **Add from database...** picks a message from the symbol databases, with its identifier and length.
- **Edit signals...** (or double-clicking the data of a database row) opens the message signal by signal,
  with value tables as lists, and re-encodes the bytes.
- Tick **On** to send that row every *Cycle (ms)*; **Send now** sends the selected row once; **All off**
  stops everything. *Sent* counts what went out.
- **Save list...** and **Load list...** keep sets of rows as JSON files; the current list is remembered.

A row that cannot be sent — because nothing is connected, say — switches itself off and shows why,
instead of repeating the error. Closing the pane stops every cyclic row.

## UDS Console

**Tools → UDS Console...** sends any ISO 14229 service without needing an ODX file, using the session
the main window has open.

- The tree lists every service by functional unit, exactly as the panel scripts see them. Pick one and its
  parameters appear as a form, with the defaults filled in; press **Send**.
- **or raw:** sends bytes you type, e.g. `22 F1 90`.
- The log shows the request and the response: a positive answer with its data as hex, as a number and as
  text; a negative one as `NRC 0x31 requestOutOfRange`.
- The bar at the top sets the **session** (DiagnosticSessionControl), sends a single **Tester present**,
  and unlocks **SecurityAccess** — give the level and the key rule (`key = seed XOR mask`, the rule the
  simulated ECU uses; replace it for a real ECU).
- **Fault memory** reads the DTCs with their status bits spelled out (`confirmedDTC, testFailed`), counts
  them, reads a **Snapshot** or **Extended data** record for the selected DTC, and clears them all.

## Diagnostic Window

**Tools → Diagnostic Window...** sends services described by an ODX file.

1. Connect from the main window first; the Diagnostic Window borrows that session.
2. **Load ODX / CDD...** and pick an `.odx`, `.pdx` or CDD file (the default folder is `ODX/`).
3. Choose a service in the tree. Its parameters appear as a form; fixed parameters are shown as
   *(coded/fixed)*.
4. Fill the free parameters and press **Send UDS request**.

The reply is shown decoded when the ODX file allows it, negative responses are shown as the service and
NRC, and the monitor below logs the frames to and from the ECU. Requests that span several frames are
handled for you.

## Firmware flashing

While connected, the **Flashing** toolbar button appears. It is enabled when the panel's script defines
`Flashing(api, firmware)`.

1. Press **Flashing** and choose an S-record (`.s19`, `.s28`, `.s37`) or Intel HEX (`.hex`) file.
2. Check the confirmation: it lists the file, its size and the address ranges to be written.
3. Watch the progress dialog; **Cancel** asks the script to stop at the next block.

What actually happens is up to the script, so it can match your bootloader. The example in
`examples/example_2026-09-18_script.py` is a complete ISO 14229 sequence: extended session, DTCs off,
normal messages off, programming session, security access, then per segment erase, RequestDownload,
TransferData blocks and RequestTransferExit, and finally a dependency check and ECU reset.

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

The tool windows are panels of the main window: drag one by its title bar to another edge, drop it on
another panel to tab them together, or pull it out to float. Each one has the same **–** and **×**
buttons as the built-in panels.

The arrangement, including the window size, is saved when you close CAN Expert and restored next time.

**View → Save desktop as...** keeps the current arrangement under a name — one for analysis, one for
diagnostics, one for testing — and **View → Desktops** switches between them. **Reset layout** goes back
to how the window starts.

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

Only one dummy ECU can run per channel: a second one is refused, because two ECUs answering the same
requests break security access and flashing.

## Where things are kept

| Folder | Contents |
|---|---|
| `Configurations/` | `config_<name>.json`, one per configuration |
| `Databases/` | Panels: `family_YYYY-MM-DD.xml` and `family_YYYY-MM-DD_script.py` |
| `DBC/` | DBC files for the Trace window, the Logger, the Transmit list, the designer and panel bindings |
| `ODX/` | ODX, PDX and CDD files for the Diagnostic Window |
| `examples/` | A runnable panel and script, and demo firmware images |

Recordings go wherever you save them; `.blf` is the most compact.

The window arrangement and its saved desktops, the theme, the symbol databases, the transmit list and the
receiver used last are all remembered between runs.

## If something does not work

**No CAN receivers found** — the adapter's driver is not installed or the adapter is not plugged in.
CAN Expert supports Kvaser, Vector and IXXAT through python-can. Press **Refresh** after connecting it.

**The ECU shows Lost connection** — check the bitrate, the SERVER ID and ECU ID, and that the ECU is
powered. **Scan Activity** tells you whether anything is talking on a channel at all.

**"No matching database"** — the configuration's *Database family* does not match any file in
`Databases/`. Clear the field to load the newest panel, or build one in the Form Designer.

**The Trace shows identifiers but no names** — no symbol database describes those messages. Add the DBC
under *Tools → Symbol databases...*.

**The Flashing button stays greyed out** — the panel's script has no `Flashing(api, firmware)` function.

**Graphs stay empty** — the Logger only draws signals from the loaded DBC that are actually received, and
only while you are connected.

**Light or dark** — *Options → Light Mode / Dark Mode*. The choice is remembered.
