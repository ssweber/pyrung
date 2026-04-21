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

| VS Code action | Runner API |
|---------------|------------|
| Step Over / Into / Out / `pyrungStepScan` | `runner.scan_steps_debug()` |
| Continue | Adapter loop over `scan_steps_debug()` |
| Conditional breakpoints | Adapter expression parser + compiled predicates |
| Monitor values | `runner.monitor(tag, callback)` |
| Snapshot labels | `runner.history.find_labeled(label)` |
| Data breakpoints | Monitor-backed change listeners |

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

## Trace event

The adapter emits `pyrungTrace` after each stop with the current step and region evaluation details used by decorations.
