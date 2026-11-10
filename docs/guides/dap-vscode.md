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
- The pyrung VS Code extension. Download `pyrung-debug-0.12.0.vsix` from the [GitHub releases](https://github.com/ssweber/pyrung/releases) page, then install it:

```bash
code --install-extension pyrung-debug-0.12.0.vsix
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

Watch evaluation uses the same visible state as the Variables panel during stepping, including pending mid-scan values.

## Debug Console

The Debug Console accepts typed commands for PLC execution, inspection, analysis, and session capture. Use `help` to print this catalog.

### Command reference

<!-- dap-console-help:start -->
```text
execution:
  autoreload [off]
  continue
  mode [run|stop]                                       (PLC RUN/STOP mode)
  pause
  reload
  run <N | duration>  (e.g. 10, 500ms, 2 s)
  speed [N|max]                                         (1 = real time; 0 or max = unpaced)
  step [N]
  stop                                                  (cancels a running analysis)

data:
  bounds [clear]                                        (runtime constraint log)
  clear_forces
  force <tag> <value>
  get <tag> [tag2 ...]
  link <feedback> <[~]enable[:trigger]> key=value ...   (bench wiring)
  links [bench on|off | enable <tag> | disable <tag>]
  monitor <tag>
  note <text>
  patch <tag> <value>
  restore <tag>                                         (drop a bench change)
  unforce <tag>
  unlink <tag>                                          (cut on the bench)
  unmonitor <tag>

analysis:
  check [RULE_OR_CATEGORY ...]
  log [N]

  dataview <text | i: | p: | t: | upstream:tag>
  downstream <tag>
  upstream <tag>
  structures

  cause <tag>[@scan|:value]
  effect <tag>[@scan|:value]
  recovers <tag>
  why <tag> [tag2 ...]
  how <expression>[, <expression>...] [avoid <expression>[, <expression>...]]  (runs planner)
  prove always|never <expression> [--settled] [--paced]  (exhaustive, needs annotations)

  simplified [tag]

capture:
  harness <install|remove|status>                       needs physical+link=
  record <action> | record stop
  replay <filepath> [--harness current|recorded|off]

review:
  accept <id>
  candidates [list]
  deny <id>
  spec [list] | spec test <filepath>
  suppress <id>

other:
  help
```
<!-- dap-console-help:end -->

### Values, forces, patches, and bounds

```text
get Button Counter        # read one or more current values
force Button true          # persistent override, held across scans
unforce Button             # remove a force
clear_forces               # remove all forces
patch Button true          # one-shot input, consumed after one scan
bounds                     # review runtime min/max/choices violations
bounds clear               # clear the accumulated violation log
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

`step` and `run` process breakpoints and logpoints during execution. If a breakpoint fires, the command stops early and reports it. Duration parsing accepts `ms`, `s`, `m`, `min`, `h`, and `d`, including compact compound durations such as `1h30min`.

Long-running `how` and `prove` requests hold the Debug Console's state lock. Cancel them from another terminal with `pyrung live stop`. Entering `stop` in the same Debug Console cannot preempt its active request.

### Causal queries

```text
cause Light                # why did Light last change?
cause Light@5              # why did Light change at scan 5?
cause Light:true           # how could Light reach true? (projected)
effect Button              # what did Button's last change cause?
effect Button:true         # what would happen if Button became true?
recovers Light             # can Light return to its resting value?
why Light Button           # explain current values without scan history
how Running avoid Fault    # find a verified input-change sequence
```

### Program inspection and verification

```text
check                      # run every static ladder check
check COIL TAG             # select rules or categories
structures                 # list UDTs and named arrays
prove always ~Fault        # exhaustively verify an invariant
prove never MotorA, MotorB # verify a bad combination is unreachable
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

### Notes and log

Annotate the timeline with free-text notes and review recent activity.

```text
note testing startup sequence   # attach a note to the current scan
note momentary start press      # another note before the next action
log                             # show last 20 scans of activity
log 50                          # show last 50 scans
```

`log` shows everything that happened in the scan window. That covers patches, force changes, tag transitions and notes.

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

On stop, the transcript is printed as plain text, with one command per line and a `# action:` comment header. The transcript is the session file format. Paste it back into the console, or save it to a file for replay.

```text
replay session.txt                            # use the current program harness
replay session.txt --harness recorded         # replay recorded harness inputs
replay session.txt --harness off              # ignore harness inputs
```

Replay executes each command in order. If a breakpoint fires or a command fails, replay halts and reports the line. The default `current` harness mode installs the harness declared by the current program and skips recorded harness inputs. `recorded` removes the current harness and applies provenance-tagged harness inputs from the transcript. `off` removes the harness and skips those inputs.

Stopping a recording also mines candidate invariants from the captured scan range. Review them before turning accepted formulas into tests:

```text
candidates                     # list mined candidates and their IDs
accept c-01                    # keep a candidate
deny c-02                      # discard this occurrence
suppress c-03                  # discard and suppress the formula in later captures
spec                           # list accepted formulas
spec test tests/test_specs.py  # generate pytest tests for accepted formulas
```

### Autoharness in the debug session

If your program uses [Physical annotations](physical-harness.md) with `physical=` and `link=` declarations, the adapter installs the autoharness automatically at launch. A banner appears in the Debug Console:

```text
Harness: 3 feedback loop(s) (2 delay, 1 ramp); `harness status` for details
```

A force still wins over the harness. Force a feedback tag to hold it whatever the harness computes.

```text
harness status             # show couplings, pending patches, active state
harness remove             # disable the harness for this session
harness install            # re-install after removal
```

### Bench links

Bench links change the feedback wiring for this session without editing the program. You can add a link, replace one the program declares, or cut one to simulate a failed sensor or a stuck contactor. They take the same keys as a link in a nickname comment. Tags may be names or CLICK addresses:

```text
link X002 Y001 on_delay=2s off_delay=500ms     # Y001 drives X002 after 2 s, drops 500 ms after
link DF5 Y003 ramp_on=0.8 ramp_off=-0.05       # analog feedback
link Sorted State:2 on_delay=1s                # value trigger
unlink X002        # cut the program's link (X002 is a free input), or remove a bench link
restore X002       # drop the bench change: back to what the program declares
links              # list program and bench links
links bench off    # set every bench change aside without forgetting it; `links bench on` again
links disable X002 # set one aside; `links enable X002` again
```

A link added while its enable is already on starts its on-delay from zero, so the feedback follows only after the on-delay.

Bench links live in the debug session, not the runner, so `reload` keeps them. A recording captures the link verbs, and replay runs them only with `--harness current`.

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

Commands chained with `;` run in order over one connection and halt on the first error. The exit code is 0 on success and 1 on error.

### Python library

```python
from pyrung.dap.live import send_command, list_sessions

sessions = list_sessions()          # ["logic", "pump_test"]
ok, text = send_command("logic", "step 5")
print(text)                         # "Stepped 5 scan(s), now at scan 10"
```

### Pair commissioning

One person drives VS Code (breakpoints, Data View, Graph View). Another person runs `pyrung live` from a terminal. Both work on the same PLC state. Commands from the live client show `(live)` in the `log` output, so you can tell who did what:

```text
scan 10:
  patch StartBtn True
  Running: False -> True
scan 11:  (live)
  force EntrySensor True
  State: 0 -> 1
```

The live client can do everything the Debug Console can, including forces, patches, causal queries and recording. Stepping from either side advances the same scan counter.

## Hot-reload

Hot-reload re-executes the program file and preserves PLC state. Tags, memory, timer accumulators, counter values and forces all carry over. History clears. This is the same experience as downloading a new program to a running PLC.

### Manual reload

```text
reload                         # from the Debug Console
pyrung live reload             # from another terminal
```

Scan ID and timestamp continue from where they were. Debug registrations (breakpoints, monitors) are cleared since rung indices may have shifted.

### Auto-reload on file save

```text
autoreload                     # watch the program directory
autoreload off                 # stop watching
```

`autoreload` polls every `.py` file under the program directory once per second. When any of them changes, the program reloads automatically after a short debounce. Set `"autoReload": true` in `launch.json` to enable the same behavior when the session starts.

If `continue` is running when a file change is detected, the watcher skips the reload and logs a message. Pause first, then save to trigger the reload.

### Tag type changes

If a tag changes type between reloads (e.g. `Bool('X')` becomes `Int('X')`), the old value is dropped and the tag gets its new default, with a warning.

New tags added to the program get their defaults. Removed tags become harmless orphans in state.

### Errors

If the program file has a syntax error or the runner can't be discovered, the reload aborts and the old runner stays active. Fix the file and reload again.

## Simulator controls

A debug session can stand in for a CLICK PLC. Other tools read and write it over Modbus TCP and drive it through a few custom DAP requests. All of it is off unless asked for.

Launch arguments (all optional):

```json
{
  "type": "pyrung",
  "request": "launch",
  "program": "${workspaceFolder}/run.py",
  "modbus": {"host": "127.0.0.1", "port": 502},
  "speed": 1.0,
  "startMode": "stop",
  "keepForcesOnRun": true
}
```

- `modbus` starts a Modbus TCP server inside the session. Reads return the current tag values. Writes are one-scan patches, so a value sticks until logic writes it, as on a real PLC. X inputs are read-only over Modbus, as on a real CLICK. Drive them with `pyrungPatch` or `pyrungForce`. Addresses map through the program's TagMap. The TagMap is either a `mapping` in the launched file or the `mapping` and `blocks` that generated projects keep in `plc/tags.py`. If the port cannot be bound, the launch still succeeds. The Debug Console prints the reason, and `pyrungModbusInfo` reports it.
- `speed` paces Continue so simulation time runs at wall time x `speed`. The scan period itself never changes. `0`, the default, runs unpaced. When the program cannot keep up, pacing does not burst to catch up. Stepping and `run N` / `run <duration>` are never paced.
- `startMode` puts the PLC in `"stop"` or `"run"` mode at launch. Without it the runner keeps the mode it was built with.
- `keepForcesOnRun` sets `PLC.keep_forces_on_run`, so forces survive STOP -> RUN. Without it the program's own setting is kept (off by default). Reloads keep it.

The Modbus server refuses a write to the STOP command bit (SC50) with exception code 2 (illegal data address). The mode does not change. A rung that turns SC50 on still stops the PLC.

Wherever a request or console command takes a tag (`pyrungForce`, `pyrungUnforce`, `pyrungPatch`, `pyrungSetMode`, and the `force` / `unforce` / `patch` verbs), a CLICK address such as `X001` or `DS10` works too. Tag names are tried first, so a tag literally named `X003` keeps working. A target that is neither a tag nor a mapped address is an error.

Anyone can drive, and the last command wins. While Continue runs, a step or `run` from VS Code or `pyrung live` pauses Continue first and then executes.

The Debug Console verbs `mode [run|stop]` and `speed [N|max]` set the PLC mode and the pacing.

## Data View

The Data View panel (in the debug sidebar) shows watched tags with live values, types, and editing controls.

### Adding tags

Right-click a tag name in the editor and select **Add to Data View**. Structured tags (UDTs, named arrays) auto-promote to expandable groups with collapsible member rows.

### Editing values

- **Bool tags**: click True/False buttons to stage, double-click to write immediately.
- **Choice tags**: selecting from the dropdown writes immediately, with no "Write Values" click needed.
- **Other types**: type a value, then click **Write Values** to patch all pending values in one scan.
- **Force**: click the Force button to lock a tag to its staged value across scans. Click again to unforce.

### Tag flag badges

Badges appear next to the tag name when flags are set:

- **RO** — read-only tag. Editing controls are locked by default. Click the lock icon to unlock them for debugging. Click it again to lock them.
- **P** — public tag. It is part of the intended API surface (setpoints, mode commands, status bits).

### Public filter

A **Public** checkbox sits above the tag table. It starts disabled and greyed out ("Start debugger to enable"). Once the debugger sends tag metadata, the checkbox becomes active. When it is checked, only tags declared `public=True` are shown. Group headers hide when none of their members are public. Unchecking restores all rows. The filter resets when the debug session ends.

## History

The History panel (in the debug sidebar) has two tabs: **Tags** and **Chain**.

### Tags tab

Shows tag values at each retained scan. The slider scrubs through the scan cache, and tag values update live as you drag. Values update automatically during `continue` runs. Right-click a tag in the Data View or Graph View to add it here.

Each entry is a scan on which a tag changed. A long `run_for()` does not record which scans those are. When you open the panel, it steps the run's scans again, newest first. Each request steps for up to about 3 seconds. A block of scans it has not stepped yet shows as one range with the net change across it. The range does not say which scan made the change. **Load older** reads further back.

### Chain tab

Run causal queries interactively. Type a query in the input field using the same syntax as the `cause`/`effect`/`recovers` console commands:

```
cause:MotorOut
effect:StartBtn@1
recovers:FaultLatch
cause:Running:True
```

The result renders inline as chain steps with triggers, enablers and fidelity indicators. It runs the same `plc.cause()`, `plc.effect()` and `plc.recovers()` a test calls.

## Graph View

**pyrung: Open Graph View** opens an interactive tag dependency graph in the editor area. The graph shows tags as nodes and rungs as edges connecting them, laid out left to right.

### Node roles

Tags are colored by role.

- Blue is for inputs (nothing writes them).
- Amber is for pivots (read and written).
- Green is for terminals (written, nothing reads them).
- Grey is for isolated tags (no connections).

Rung nodes show the rung index and source location.

### Interactions

- **Click** a tag to highlight its direct neighbors.
- **Double-click** a tag to slice the graph to its upstream and downstream dependencies.
- **Right-click** a tag for a context menu with these entries: slice upstream, slice downstream, add to Data View, add to History, copy name, hide.
- **Right-click** a rung to go to source or copy rung info.
- **Drag** a node to pin its position (persisted in workspace state).
- **Search** filters tags by name with abbreviation matching (typing `btn` finds `StartButton`).
- **Role toggles** (I/P/T/X buttons) show or hide nodes by role.
- **Rung Order** sorts the layout vertically by execution order for a ladder-like top-down view.
- **Reset** clears all pins, hidden nodes, filters, and slices.

During debugging, live value badges appear on tag nodes. The graph rescopes automatically when you switch editor tabs, showing only the rungs and tags from the active file.
