# DAP Debugger in VS Code

pyrung includes a Debug Adapter Protocol (DAP) server that exposes PLC scan execution to VS Code.

## Features

- Source-line breakpoints
- Conditional breakpoints using the pyrung condition DSL
- Hit-count breakpoints
- Logpoints
- Snapshot logpoints with `Snapshot: label`
- Data breakpoints for monitored tags
- Monitor values in the Variables panel under `PLC Monitors`
- Custom debug events in Output channel `pyrung: Debug Events`
- Trace decorations and inline condition annotations

## Requirements

- VS Code with the Python extension
- `pyrung` installed: `pip install pyrung`
- The pyrung VS Code extension — download `pyrung-debug-0.1.0.vsix` from the [GitHub releases](https://github.com/ssweber/pyrung/releases) page, then install:

```bash
code --install-extension pyrung-debug-0.1.0.vsix
```

## Launch configuration

Add to `.vscode/launch.json`:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "pyrung DAP",
      "type": "pyrung",
      "request": "launch",
      "program": "${file}"
    }
  ]
}
```

## Breakpoints

- Stop on a rung: click gutter
- Conditional breakpoint: right-click gutter -> Add Conditional Breakpoint
- Hit count: edit breakpoint hit count (fires on every Nth hit: N, 2N, 3N...)
- Logpoint: right-click gutter -> Add Logpoint
- Snapshot logpoint: set log message to `Snapshot: my_label`
- Logpoints and snapshot logpoints fire during both Continue and stepping commands.

Condition expressions use the pyrung DSL, for example:

- `Fault`
- `~Fault`
- `MotorTemp > 100`
- `Fault, Pump`
- `Running | (Mode == 1)`
- `And(Fault, Pump)`
- `Or(Low, High)`

When you use `&` or `|` with comparisons, parenthesize the comparison terms:

- Valid: `Fault & (MotorTemp > 100)`
- Valid: `Running | (Mode == 1)`
- Invalid: `Fault & MotorTemp > 100`

## Monitors

Commands:

- `pyrung: Add Monitor`
- `pyrung: Remove Monitor`
- `pyrung: Find Label`

The status bar shows `M:<count>` while a pyrung debug session is active.

Monitors appear in:

- Variables panel scope: `PLC Monitors`
- Output channel: `pyrung: Debug Events`

## Data breakpoints

After adding a monitor, you can set a data breakpoint from the monitored variable to stop when its value changes.

## Watch expressions

Use the VS Code `Watch` panel for read-only expression evaluation.

- Bare tag/memory names return the current raw value (`Counter`, `Fault`, `Step[CurStep]`).
- Predicate expressions return `True`/`False` (`Fault & (MotorTemp > 100)`, `Mode == 1`).
- Unknown names fail with an explicit error so typos are visible.

Watch evaluation uses the same visible state as the Variables panel during stepping, including pending mid-scan values.

## Debug Console

The Debug Console accepts typed commands for all PLC operations. Use `help` to list them, or `Watch` for predicate evaluation.

### Forces and patches

```text
force Button true          # persistent override, held across scans
unforce Button             # remove a force
clear_forces               # remove all forces
patch Button true          # one-shot input, consumed after one scan
```

### Stepping and running

```text
step                       # advance one scan
step 5                     # advance 5 scans
run 100                    # run 100 scans
run 500ms                  # run for 500ms of sim time
run 2 s                    # run for 2 seconds (split unit ok)
continue                   # run continuously (background)
pause                      # stop a running continue
```

`step` and `run` process breakpoints and logpoints during execution. If a breakpoint fires, the command stops early and reports it. Duration parsing accepts `ms`, `s`, `min`, `h` — same format as Physical delay declarations.

`continue` starts a background scan loop (same as the VS Code Continue button). `pause` signals it to stop after the current scan.

### Causal queries

```text
cause Light                # why did Light last change?
cause Light@5              # why did Light change at scan 5?
cause Light:true           # how could Light reach true? (projected)
effect Button              # what did Button's last change cause?
effect Button:true         # what would happen if Button became true?
recovers Light             # can Light return to its resting value?
```

### DataView queries

```text
dataview Motor             # tags containing "Motor"
dataview i:                # all input tags
dataview p:Motor           # pivots containing "Motor"
dataview upstream:Light    # upstream dependencies of Light
dataview downstream:Button # downstream effects of Button
upstream Light             # shorthand for dataview upstream:Light
downstream Button          # shorthand for dataview downstream:Button
```

The query language supports role prefixes (`i:` inputs, `p:` pivots, `t:` terminals, `x:` isolated) and slice prefixes (`upstream:`, `downstream:`). Multiple tokens are applied left to right.

### Simplified form

```text
simplified                 # show all terminals' simplified Boolean forms
simplified MotorOut        # show one terminal, resolved to inputs
```

### Monitors

```text
monitor Button             # watch Button for value changes
unmonitor Button           # stop watching
```

Monitors also appear in the Variables panel under `PLC Monitors` and can be promoted to data breakpoints.

### Notes and log

Annotate the timeline with free-text notes and review recent activity.

```text
note testing startup sequence   # attach a note to the current scan
note momentary start press      # another note before the next action
log                             # show last 20 scans of activity
log 50                          # show last 50 scans
```

`log` shows patches, force changes, tag transitions, and notes — everything that happened in the scan window. When commands come from `pyrung live`, the scan header is tagged with `(live)` so you can tell who did what in a pair-programming session.

```text
scan 12  forces: EstopOK=True

scan 10:
  # testing startup sequence
scan 11:  (live)
  patch StartBtn True
  ConveyorMotor: False → True
  Running: False → True
```

### Session capture

Record a sequence of console commands as a replayable transcript.

```text
record start_machine       # begin recording action "start_machine"
patch State 1
run 500ms
patch State 2
step 1
record stop                # stop recording, print transcript
```

On stop, the transcript is printed as plain text — one command per line with a `# action:` comment header. The transcript is the session file format: paste it back into the console or save it to a file for replay.

If forces are active when recording starts, a warning is printed.

```text
replay session.txt         # feed a transcript file back through the console
```

Replay reads the file, skips `#` comment lines and blank lines, and executes each command in order. If a breakpoint fires or a command fails, replay halts and reports the line.

Commands executed during replay are captured normally — if a recording is active, they appear in the transcript. The `record`, `replay`, and `help` verbs themselves are never captured.

### Autoharness in the debug session

If your program uses [Physical annotations](physical-harness.md) with `physical=` and `link=` declarations, the adapter installs the autoharness automatically at launch. A banner appears in the Debug Console:

```text
Harness: 3 feedback loop(s) (2 bool, 1 analog) — `harness status` for details
```

No configuration needed — if you annotated your UDTs, the harness activates. Feedback patches land at the same pre-scan phase as manual patches and forces. Forces still win: force a feedback tag to hold it regardless of what the harness schedules.

```text
harness status             # show couplings, pending patches, active state
harness remove             # disable the harness for this session
harness install            # re-install after removal
```

`harness status` lists each coupling with its timing or profile. Value-triggered couplings show the trigger with `==`:

```text
Harness: active
  bool  Gripper_En -> Gripper_Fb_Contact  (on=5ms, off=5ms)
  bool  Gripper_En -> Gripper_Fb_Vacuum   (on=20ms, off=80ms)
  bool  Sorter_State==2 -> Sorter_BinSensor  (on=2000ms, off=500ms)
  analog  Heater_En -> Heater_Fb_Temp  profile=generic_thermal [active]
  analog  Oven_Mode==2 -> Oven_Temp  profile=zone_thermal [active]
```

When the harness applies patches, they appear in the Debug Console output prefixed with `[harness]`:

```text
[harness] Gripper_Fb_Contact=True
[harness] Heater_Fb_Temp=25.3
```

If a session recording is active, harness patches are captured with provenance tags (`harness:nominal` for bool feedback, `harness:analog:<profile>` for analog). In the transcript they appear as comment lines — visible for review, skipped on replay:

```text
# action: test_gripper_cycle
patch Cmd true
run 100ms
# harness:nominal: patch Gripper_Fb_Contact True
# harness:nominal: patch Gripper_Fb_Vacuum True
# harness:analog:generic_thermal: patch Heater_Fb_Temp 25.3
step 5
```

On replay, the harness re-synthesizes its own patches from the program state — the comment lines are documentation, not inputs.

## pyrung live

Attach to a running debug session from another terminal or process.

When the DAP adapter launches, it starts a TCP server on localhost and writes the port to a session file. The session name defaults to the program filename stem (`logic.py` → `logic`) or can be set explicitly in the launch configuration:

```json
{
  "type": "pyrung",
  "request": "launch",
  "program": "${file}",
  "session": "my_session"
}
```

### CLI usage

```text
pyrung live                                # list active sessions
pyrung live step 5                         # works when only one session is active
pyrung live -s my_session step 5           # explicit session when multiple are running
pyrung live "force Button true; step 5; cause Light"  # chain commands with ;
pyrung live -h                             # show all available commands
```

When only one session is active, `--session` can be omitted. With multiple sessions, the CLI lists them and asks you to pick one. Running with no arguments lists active sessions.

Commands can be chained with `;` in a single invocation — they run sequentially over one connection, halting on first error. Every console command works over the live connection — forces, patches, stepping, queries, record/replay. The response is printed to stdout. Exit code is 0 on success, 1 on error.

### Python library

```python
from pyrung.dap.live import send_command, list_sessions

sessions = list_sessions()          # ["logic", "pump_test"]
ok, text = send_command("logic", "step 5")
print(text)                         # "Stepped 5 scan(s), now at scan 10"
```

### Pair commissioning

One person drives VS Code (breakpoints, Data View, Graph View), another runs `pyrung live` from a terminal. Both hit the same PLC state. Commands from the live client show `(live)` in the `log` output so you can tell who did what:

```text
scan 10:
  patch StartBtn True
  Running: False → True
scan 11:  (live)
  force EntrySensor True
  State: 0 → 1
```

The live client can do everything the Debug Console can — forces, patches, causal queries, recording. Stepping from either side advances the same scan counter.

### How it works

The server is a single-threaded TCP listener that dispatches commands through the same `console.dispatch()` path as the Debug Console. Commands are serialized by the adapter's state lock — a live client waits if a scan is in progress.

Session discovery uses port files in the system temp directory (`<tempdir>/pyrung/pyrung-<name>.port`). The file is created on launch and removed on disconnect. Stale port files from crashed processes are pruned automatically when listing sessions.

## DAP to runner mapping

| VS Code action | Runner API |
|---------------|------------|
| Step Over / Into / Out / `pyrungStepScan` | `runner.scan_steps_debug()` |
| Continue | Adapter loop over `scan_steps_debug()` |
| Conditional breakpoints | Adapter expression parser + compiled predicates |
| Monitor values | `runner.monitor(tag, callback)` |
| Snapshot labels | `runner.history.find_labeled(label)` |
| Data breakpoints | Monitor-backed change listeners |
| Debug Console commands | `console.dispatch()` registry |

See [Architecture — Debug stepping APIs](architecture.md#debug-stepping-apis) for details on `scan_steps_debug()` and rung inspection.

## Data View

The Data View panel (in the debug sidebar) shows watched tags with live values, types, and editing controls.

### Adding tags

Right-click a tag name in the editor and select **Add to Data View**. Structured tags (UDTs, named arrays) auto-promote to expandable groups with collapsible member rows.

### Editing values

- **Bool tags**: click True/False buttons to stage, double-click to write immediately.
- **Choice tags**: selecting from the dropdown writes immediately — no "Write Values" click needed.
- **Other types**: type a value, then click **Write Values** to patch all pending values in one scan.
- **Force**: click the Force button to lock a tag to its staged value across scans. Click again to unforce.

### Tag flag badges

Badges appear next to the tag name when flags are set:

- **RO** — read-only tag. Editing controls are locked by default. Click the lock icon to unlock for debugging; click again to re-lock.
- **P** — public tag. Part of the intended API surface (setpoints, mode commands, status bits).

### Public filter

A **Public** checkbox sits above the tag table. It starts disabled and greyed out ("Start debugger to enable"). Once the debugger sends tag metadata, it enables. When checked, only tags declared `public=True` are shown — plumbing tags are hidden. Group headers hide when none of their members are public. Unchecking restores all rows. The filter resets when the debug session ends.

## History

The History panel (in the debug sidebar) has two tabs: **Tags** and **Chain**.

### Tags tab

Shows tag values at each retained scan. The slider scrubs through the scan cache; tag values update live as you drag. Values update automatically during `continue` runs. Right-click a tag to add it from the Data View or Graph View.

### Chain tab

Run causal queries interactively. Type a query in the input field using the same syntax as the `cause`/`effect`/`recovers` console commands:

```
cause:MotorOut
effect:StartBtn@1
recovers:FaultLatch
cause:Running:True
```

The result renders inline — chain steps with proximate causes, enabling conditions, and fidelity indicators. The query dispatches to the `pyrungCausal` DAP handler, which calls the same `plc.cause()`/`plc.effect()`/`plc.recovers()` methods available in tests.

## Graph View

**pyrung: Open Graph View** opens an interactive tag dependency graph in the editor area. The graph shows tags as nodes and rungs as edges connecting them, laid out left-to-right with dagre.

### Node roles

Tags are colored by role: blue for inputs (nothing writes them), amber for pivots (read and written), green for terminals (written, nothing reads them), grey for isolated (no connections). Rung nodes show the rung index and source location.

### Interactions

- **Click** a tag to highlight its direct neighbors.
- **Double-click** a tag to slice the graph to its upstream and downstream dependencies.
- **Right-click** a tag for a context menu: slice upstream, slice downstream, add to Data View, add to History, copy name, hide.
- **Right-click** a rung to go to source or copy rung info.
- **Drag** a node to pin its position (persisted in workspace state).
- **Search** filters tags by name with abbreviation matching (typing `btn` finds `StartButton`).
- **Role toggles** (I/P/T/X buttons) show or hide nodes by role.
- **Rung Order** sorts the layout vertically by execution order for a ladder-like top-down view.
- **Reset** clears all pins, hidden nodes, filters, and slices.

During debugging, live value badges appear on tag nodes. The graph rescopes automatically when you switch editor tabs, showing only the rungs and tags from the active file.

## Trace event

The adapter emits `pyrungTrace` after each stop with the current step and region evaluation details used by decorations.
