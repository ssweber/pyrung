# DAP Debugger in VS Code

pyrung includes a Debug Adapter Protocol (DAP) server that exposes PLC scan execution to VS Code. You get live rung-power highlighting, inline condition/instruction value annotations, step-through debugging, and a tag variable panel — all in your Python source file.

## What it looks like

While your program is running under the DAP adapter:

- **Green highlights** on powered rungs
- **Grey highlights** on unpowered rungs
- **Inline annotations** showing evaluated condition and instruction values
- **Red inline text** identifying which condition caused a rung to be unpowered
- **Variables panel** showing the full tag table
- **Debug console** accepting force commands

## Requirements

- VS Code with the Python extension
- `pyrung` installed: `pip install pyrung`
- The VS Code debugger extension for type `pyrung` (from `editors/vscode/pyrung-debug` in this repo)

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
      "program": "${file}",
      "stopOnEntry": false
    }
  ]
}
```

## Starting a debug session

1. Open your `logic.py` file in VS Code.
2. Set a breakpoint on a `Rung` line (optional).
3. Press **F5** (or **Run → Start Debugging**).
4. The DAP adapter launches and pauses on entry.

## Stepping

| Action | Behavior |
|--------|----------|
| **Step Over** (F10) | Advance to the next top-level rung boundary |
| **Step Into** (F11) | Advance to the next instruction boundary (enters subroutines) |
| **Step Out** (Shift+F11) | Run until the current frame/depth exits |
| **Continue** (F5) | Run until a source-line breakpoint or manual pause |

## DAP ↔ PLCRunner mapping

| VS Code / DAP feature | pyrung API |
|-----------------------|------------|
| Step Over / Into / Out | `runner.scan_steps_debug()` driven by adapter step policy |
| Continue | Adapter continue loop over `runner.scan_steps_debug()` |
| Variables panel | `SystemState.tags` |
| Debug console force | `runner.add_force()` / `runner.remove_force()` |
| Breakpoints (gutter) | DAP `setBreakpoints` mapped to captured source lines |
| Inline decorations | Unified core trace source via `runner.inspect_event()` |
| Call stack | Subroutine call stack from `ScanStep.call_stack` |

## Trace payload (`pyrungTrace`)

The adapter emits a custom event named `pyrungTrace` after each stop.

Payload includes:

- `traceVersion`: adapter trace schema version
- `traceSource`: `"live"` or `"inspect"`
- `scanId`: scan identity for the trace
- `rungId`: top-level rung identity for the trace
- `step`: current step metadata (`kind`, source location, call stack, etc.)
- `regions`: condition/instruction trace regions used for inline decorations

`traceSource` semantics:

- `"live"`: trace came from in-flight debug-path context via `runner.inspect_event()`.
- `"inspect"`: trace came from committed debug-path context via `runner.inspect_event()`.

This keeps adapter trace retrieval on one core API while preserving the existing payload shape.

Debug-path-only scope remains intentional: trace retention and in-flight trace events are populated by
`scan_steps_debug()` (including DAP stepping paths), not by `step()`/`run()`/`run_for()`/`run_until()`.

## Source location capture

During program construction, every `Rung`, condition, and instruction captures its source file and line number. This is what enables the DAP adapter to highlight the correct lines in your source file.

```python
# This rung knows it lives at line 12 of logic.py
with Rung(StartButton):        # source_file="logic.py", source_line=12
    latch(MotorRunning)        # source_line=13
```

The capture happens automatically — no annotations needed.

## Debug console commands

In the VS Code Debug Console:

```
> force Button True
> force Speed 42
> remove_force Button
> clear_forces
```

## Architecture

The debug adapter is a thin Python process that:

1. Spawns (or connects to) a `PLCRunner` with your program.
2. Translates DAP protocol messages to `runner.step()`, `scan_steps_debug()`, etc.
3. After each stop, emits `pyrungTrace` from `runner.inspect_event()` (in-flight and committed debug-path scans).
4. Provides `SystemState.tags` as the DAP variables response.

DAP is an open protocol — the adapter also works with Neovim (via `nvim-dap`), Emacs (`dap-mode`), and JetBrains IDEs with minimal changes.

!!! note "Planned features (Phase 3)"
    The following are not yet wired into the adapter UX:

    - **Timeline slider** — step backward through scan history
    - **Watch panel** — core `runner.monitor(tag, callback)` is available; adapter watch UI is pending
    - **Scan-to-scan diff** view
    - **Fork** — branch from historical state to explore "what if"

    See internal design notes in `docs/internal/debug-spec.md` for the full Phase 3 plan.
