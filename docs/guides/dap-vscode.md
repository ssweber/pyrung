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
run 2s                     # run for 2 seconds of sim time
```

`step` and `run` process breakpoints and logpoints during execution. If a breakpoint fires, the command stops early and reports it. Duration parsing accepts `ms`, `s`, `min`, `h` — same format as Physical delay declarations.

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

## pyrung-live

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
pyrung-live list                           # show active sessions
pyrung-live -s my_session help             # send a command
pyrung-live -s my_session step 5           # step 5 scans
pyrung-live -s my_session force Button true
pyrung-live -s my_session cause Light
```

Every console command works over the live connection — forces, patches, stepping, queries, record/replay. The response is printed to stdout. Exit code is 0 on success, 1 on error.

### Python library

```python
from pyrung.dap.live import send_command, list_sessions

sessions = list_sessions()          # ["logic", "pump_test"]
ok, text = send_command("logic", "step 5")
print(text)                         # "Stepped 5 scan(s), now at scan 10"
```

### How it works

The server is a single-threaded TCP listener that dispatches commands through the same `console.dispatch()` path as the Debug Console. Commands are serialized by the adapter's state lock — a live client waits if a scan is in progress.

Session discovery uses port files in the system temp directory (`<tempdir>/pyrung/pyrung-<name>.port`). The file is created on launch and removed on disconnect.

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

## Trace event

The adapter emits `pyrungTrace` after each stop with the current step and region evaluation details used by decorations.
