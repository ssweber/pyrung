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
- The pyrung VS Code extension. Download `pyrung-debug-X.Y.Z.vsix` from the [GitHub releases](https://github.com/ssweber/pyrung/releases) page, then install it:

```bash
code --install-extension pyrung-debug-X.Y.Z.vsix
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

The Debug Console accepts the typed commands in the [Console reference](console.md). Use `help` to print the list.

## Hot-reload

The `reload` command re-executes the program file and keeps PLC state. See [Reload](console.md#reload).

### Auto-reload on file save

```text
autoreload                     # watch the program directory
autoreload off                 # stop watching
```

`autoreload` polls every `.py` file under the program directory once per second. When any of them changes, the program reloads automatically after a short debounce. Set `"autoReload": true` in `launch.json` to enable the same behavior when the session starts.

If `continue` is running when a file change is detected, the watcher skips the reload and logs a message. Pause first, then save to trigger the reload.

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

The Debug Console verbs `mode [run|stop]` and `speed [N|max]` set the PLC mode and the pacing. Going from STOP to RUN resets non-retentive tags, clears patches, and clears forces unless `keepForcesOnRun` is set.

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
