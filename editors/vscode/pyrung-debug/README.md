# pyrung Debugger for VS Code

Debug pyrung ladder logic programs directly in VS Code using the Debug Adapter Protocol.

## Features

- Source-line breakpoints, conditional breakpoints, and hit-count breakpoints
- Logpoints and snapshot logpoints (`Snapshot: label`)
- Data breakpoints for monitored tags
- Monitor values in the Variables panel under `PLC Monitors`
- Trace decorations and inline condition annotations
- Rapid auto-step mode (`next` / `stepIn` / `scan`) for live Watch and inline updates
- Debug Console force commands (`force`, `remove_force`, `clear_forces`)

## Requirements

- VS Code 1.85+
- Python 3.x with `pyrung` installed:
  ```
  pip install pyrung
  ```

## Getting Started

1. Install the extension.
2. Open a Python file that constructs a pyrung `Program` or `PLCRunner`.
3. Press **F5** — the extension provides a default launch configuration.

Or add one manually to `.vscode/launch.json`:

```json
{
  "name": "Debug PLC Logic",
  "type": "pyrung",
  "request": "launch",
  "program": "${file}"
}
```

Set `"pythonPath"` if your pyrung install is in a virtualenv:

```json
{
  "name": "Debug PLC Logic",
  "type": "pyrung",
  "request": "launch",
  "program": "${file}",
  "pythonPath": "${workspaceFolder}/.venv/bin/python"
}
```

## Breakpoints

- **Line breakpoint**: click the gutter
- **Conditional breakpoint**: right-click gutter, uses the pyrung condition DSL (`Fault & (MotorTemp > 100)`)
- **Hit count**: fires on every Nth hit
- **Logpoint**: right-click gutter, set a log message
- **Snapshot logpoint**: set log message to `Snapshot: my_label`

## Monitors

Use the Command Palette:

- `pyrung: Add Monitor` — watch a tag value across scans
- `pyrung: Remove Monitor` — stop watching
- `pyrung: Find Label` — jump to a snapshot label

Monitors appear in the Variables panel (`PLC Monitors` scope) and the `pyrung: Debug Events` output channel. The status bar shows `M:<count>` during a session.

Right-click a monitored variable to set a **data breakpoint** that stops when its value changes.

## Rapid Step Mode

Repeatedly sends step requests while paused for continuous Watch and inline feedback.

- `pyrung: Toggle Rapid Step` — start/stop
- `pyrung: Configure Rapid Step` — set mode (`next`, `stepIn`, `scan`) and interval

Status bar shows `R:...` when active. Manual debug controls stop rapid mode automatically.

## Debug Console Commands

```
force TagName value
remove_force TagName
unforce TagName
clear_forces
```

Use the Watch panel for expression evaluation.

## More Information

- [DAP guide](https://github.com/ssweber/pyrung/blob/main/docs/guides/dap-vscode.md)
- [pyrung documentation](https://github.com/ssweber/pyrung)

## License

MPL-2.0
