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
- Rapid auto-step mode (`next` / `stepIn` / `scan`) for live Watch and inline updates

## Requirements

> **Pending publish** — the pyrung VS Code extension is not yet available on the marketplace. This section will be updated with install instructions when it ships. In the meantime, the extension can be built and installed locally from `editors/vscode/pyrung-debug`.

- VS Code with the Python extension
- `pyrung` installed: `pip install pyrung`
- The pyrung VS Code extension *(marketplace link pending)*

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
- `all_of(Fault, Pump)`
- `any_of(Low, High)`

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

## Rapid step mode

Rapid step mode repeatedly sends step requests while paused to produce frequent `stopped` states for Watch and inline feedback.

Commands:

- `pyrung: Toggle Rapid Step`
- `pyrung: Configure Rapid Step`

Defaults:

- Mode: `next`
- Interval: `100 ms`
- Enabled: `false`

Modes:

- `next`: stop at rung-level boundaries.
- `stepIn`: stop at finer-grained instruction/subroutine boundaries.
- `scan`: execute to the next scan boundary (`pyrungStepScan`), then stop.

Status bar:

- `R:...` is a dedicated rapid-step status item (separate from monitor `M:<count>`).
- Click the rapid item to start/stop rapid mode.

Behavior notes:

- If already paused, rapid mode starts stepping immediately.
- If running, rapid mode sends `pause`, waits for the next stop, then begins stepping.
- Manual debug controls (`Continue`, `Pause`, step commands, `Disconnect`, `Terminate`) stop rapid mode so normal debugger behavior takes over.
- Rapid mode is step-based and intentionally does not change true `Continue` behavior.

## Debug console force commands

The Debug Console is command-only for force operations:

```text
force TagName value
remove_force TagName
unforce TagName
clear_forces
```

Use `Watch` for predicate evaluation.

## DAP to runner mapping

- Step Over / Into / Out + `pyrungStepScan`: `runner.scan_steps_debug()`
- Continue: adapter continue loop over `scan_steps_debug()`
- Conditional source breakpoints: adapter expression parser + compiled predicates
- Monitor callbacks: `runner.monitor(tag, callback)`
- Snapshot labels: `runner.history.find(label)` / `runner.history.find_all(label)` for states,
  and `runner.history.find_labeled(label)` / `runner.history.find_all_labeled(label)` for metadata
  (`scanId`, `timestamp`, `rtcIso`, `rtcOffsetSeconds`)
- Data breakpoints: monitor-backed change listeners

## Trace event

The adapter emits `pyrungTrace` after each stop with the current step and region evaluation details used by decorations.
