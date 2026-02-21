# Core Debug - Specification

> **Status:** Draft specification (milestone 11 scope)
> **Depends on:** `docs/getting-started/concepts.md`, `docs/guides/ladder-logic.md`

---

## Implementation Plan

The debug API is built incrementally, with each phase motivated by a real consumer.

### Phase 1 — Force

Foundation for all debugging. Enables manual override of tag values during scan execution.

### Phase 2 — Source Location Capture + VS Code DAP Integration

During the DSL build phase (`Rung`, `rise()`, `out()`, operators, builder-style APIs), capture source metadata at the user callsite. A shared helper walks stack frames so metadata resolves to user DSL lines, not framework internals.

Captured metadata:

- `source_file: str | None`
- `source_line: int | None`
- `end_line: int | None` (for block contexts such as `Rung`, `branch`, `forloop`; best-effort via AST `end_lineno`)

Build a VS Code extension using the Debug Adapter Protocol (DAP):

- `step()` maps to DAP step
- `scan_steps()` provides rung-boundary stepping for adapter internals
- Tag table maps to DAP variables panel
- Force commands map to debug console
- Inline decorations show live rung power state (green/grey) and evaluated condition/instruction values as annotations on the original source

This gives users a live, annotated view of their DSL code during scan execution, with no separate GUI required.

### Phase 3 — Debug APIs (driven by editor needs)

Build the rest of the debug API incrementally as each feature has a visible payoff in the editor:

- **History / time travel** → timeline slider, step backward
- **Breakpoints / snapshot labels** → gutter breakpoints, labeled snapshots
- **Monitors** → watch panel integration
- **Rung inspection** → detailed per-rung trace with condition/instruction evaluation results
- **Diff** → scan-to-scan change view
- **Fork** → branch-and-explore from historical state

Each engine feature is designed with the editor as its primary consumer, ensuring the API maps cleanly to how the extension needs to present it.

---

## Scope

Debugging and inspection APIs on top of `PLCRunner`:

- force (Click "override")
- source location capture for DSL elements
- VS Code extension via Debug Adapter Protocol
- breakpoints and snapshot labels
- monitors
- history and playhead navigation
- rung inspection
- diff and fork

---

## Terminology

- API term is **force**.
- In Click UI/docs this corresponds to **override (OVR)**.

---

## Public API

```python
# force
runner.add_force(tag, value)
runner.remove_force(tag)
runner.clear_forces()
with runner.force({tag_or_name: value, ...}):
    ...

# breakpoints
runner.when(predicate).pause()
runner.when(predicate).snapshot("label")

# monitors
runner.monitor(tag, callback)

# history
runner.history.at(scan_id)
runner.history.range(start_scan_id, end_scan_id)
runner.history.latest(n)
runner.history.find(label)
runner.history.find_all(label)

# time travel
runner.seek(scan_id)
runner.rewind(seconds)
runner.playhead

# inspection / diff / fork
runner.inspect(rung_id, scan_id=None)
runner.inspect_event()
runner.diff(scan_a, scan_b)
runner.fork_from(scan_id)
```

`tag` accepts `str` or `Tag`.

---

## Source Location Capture

During the DSL build phase, each `Rung`, condition, and instruction captures its source file and line number. This enables mapping from internal engine objects back to user code for editor integration.

```python
from pyrung.core._source import _capture_source, _capture_with_end_line

class Rung:
    def __init__(self, *conditions):
        self.source_file, self.source_line = _capture_source(depth=2)

    def __exit__(self, *_):
        self.end_line = _capture_with_end_line(
            self.source_file,
            self.source_line,
            context_name="Rung",
        )
```

Conditions (`rise()`, `fall()`, `any_of()`, `all_of()`, tag/expression comparisons, `|`/`&` combinations) and instruction emitters (`out()`, `latch()`, `copy()`, `run_function()`, `search()`, `call()`, etc.) capture source at construction time.

Builder flows also preserve original callsite metadata:

- `shift(...).clock(...).reset(...)`
- `count_up(...).reset(...)` / `count_down(...).reset(...)`
- `on_delay(...).reset(...)` / `off_delay(...)`
- `forloop(...)` capture block + emitted `ForLoopInstruction`
- `branch(...)` capture nested rung metadata

After a scan, trace data maps back to source:

```python
{
    "rung_0": {
        "line": 5, "end_line": 6, "powered": True,
        "conditions": [
            {"line": 5, "expr": "rise('start_button')", "value": True}
        ],
        "instructions": [
            {"line": 6, "expr": "out('motor_running')", "value": True}
        ]
    }
}
```

### Dynamic rung generation

If rungs are built in a loop, multiple rung objects may share source lines. The mapping is best-effort in this case; explicit DSL declarations maintain a clean one-to-one mapping.

---

## VS Code Extension

The extension uses the Debug Adapter Protocol (DAP) to expose PLCRunner debugging in VS Code.

### DAP mapping

| PLCRunner concept | VS Code / DAP feature |
|---|---|
| `step()` | Step button |
| `scan_steps()` | Rung-boundary stepping internals |
| `run()` / `run_until()` | Continue |
| Tag table | Variables panel |
| `add_force()` / `remove_force()` | Debug console commands |
| `when().pause()` | Breakpoints (gutter) |
| Scan history | Call stack / timeline |
| `inspect()` + `inspect_event()` | Inline decorations (unified core source) |

Minimum source contract expected by adapter:

- Use `source_file + source_line` as required mapping keys.
- Treat `end_line` as optional enhancement for range decorations.
- Handle `None` source metadata defensively (generated/dynamic code paths).
- Keep path normalization in the adapter (filesystem case/sep differences).
- Breakpoints set on instruction lines map to containing top-level rung boundaries in v1.

### Inline decorations

After each scan, the extension reads trace data and applies decorations to the source file:

- **Green highlight**: powered rungs
- **Grey highlight**: unpowered rungs
- **Inline annotations**: evaluated condition and instruction values as faded text
- **Red inline text**: the condition that caused an unpowered rung

### Architecture

The extension consists of:

- A **Debug Adapter** (Python or TypeScript) that wraps PLCRunner over stdin/stdout or socket
- A **decoration provider** that maps rung trace data to source line highlights
- A small **protocol** between the adapter and the runner for trace and state queries

DAP is an open protocol, so the debug adapter also works with Neovim, Emacs, and JetBrains with minimal changes.

Incremental note (Phase 3, 2026-02-21):

- The adapter emits `pyrungTrace` with:
  - `traceSource` (`"live"` or `"inspect"`)
  - `scanId`
  - `rungId`
- Trace emission uses a unified core model through `runner.inspect_event()`:
  - `"live"` when the returned event is from in-flight debug scan context
  - `"inspect"` when the returned event is from committed retained debug scan context
- Coverage remains intentionally debug-path-only (`scan_steps_debug()` and DAP stepping paths).

---

## Force Semantics

### Supported targets

- Any writable tag may be forced (`bool`, `int`, `float`, `str`).
- Read-only system points cannot be forced (`ValueError`).

### Persistence

- Forces persist across scans until removed.
- Multiple forces may be active.
- `with runner.force({...})` is temporary and restores the exact previous force map on exit (nested-safe).

### Scan cycle integration

Force is applied before and after code execution:

1. Read inputs
2. Apply force values
3. Process code
4. Apply force values
5. Write outputs

At each force pass, all prepared force values are written by the runtime system,
regardless of whether those variables are used in the task/program.

Mapped to core engine phases:

```
0. SCAN START
1. APPLY PATCH
2. READ INPUTS
3. APPLY FORCES (pre-logic)
4. EXECUTE LOGIC
5. APPLY FORCES (post-logic)
6. WRITE OUTPUTS
7. ADVANCE CLOCK
8. SNAPSHOT/HISTORY
```

### In-cycle behavior

- Pre-logic force pass writes prepared values before IEC code begins.
- IEC code may assign different values during processing.
- External client writes (if present in a runtime integration) may also overwrite values mid-cycle.
- Mid-cycle reads observe the current in-cycle value (including IEC assignments).
- Post-logic force pass writes prepared values again before output write.
- Therefore, force does **not** lock a variable to one value for the entire cycle; it reasserts at cycle boundaries.

### Force and patch

- `patch` remains one-shot and is consumed as usual.
- If a tag is both patched and forced in the same scan, the pre-logic force pass overwrites the patched value.

### Force and edge detection

- `rise()` / `fall()` evaluate whatever value is present when that condition is evaluated.
- Since forced variables may diverge mid-cycle, edges can reflect IEC assignments during the scan.
- Across scans, the committed post-force value is what carries into the next cycle.

---

## Breakpoints and Snapshot Labels

- Predicate type: `Callable[[SystemState], bool]`.
- Predicate is evaluated on each committed scan snapshot.
- `pause()` halts `run()`, `run_for()`, or `run_until()` after committing the triggering scan.
- `snapshot(label)` tags the triggering scan and does not halt.
- Each registration returns a handle with `remove()`, `enable()`, `disable()`, and `id`.
- Multiple breakpoints can be active simultaneously.

Label lookup:

- `history.find(label)` returns the most recent labeled snapshot or `None`.
- `history.find_all(label)` returns all matches oldest-to-newest.

---

## Monitors

```python
runner.monitor(tag, callback)
# callback(current_value, previous_value)
```

- Fires after each committed scan when value changed vs previous committed scan.
- Multiple monitors per tag are allowed.
- Returns a handle with `remove()`, `enable()`, `disable()`, and `id`.
- Callback exceptions propagate to caller.

---

## Rung Inspection

```python
runner.inspect(rung_id, scan_id=None) -> RungTrace
runner.inspect_event() -> tuple[int, int, RungTraceEvent] | None
```

- If `scan_id` is omitted, inspect uses `runner.playhead`.
- `rung_id` is rung order index (0-based) in the compiled program.
- Inspection is read-only.

Current `RungTrace` v1 shape:

- `scan_id`
- `rung_id`
- `events: tuple[RungTraceEvent, ...]`

`RungTraceEvent` includes:

- `kind` (`"rung" | "branch" | "subroutine" | "instruction"`)
- `source_file`, `source_line`, `end_line`
- `subroutine_name`, `depth`, `call_stack`
- `enabled_state`, `instruction_kind`
- `trace` (`TraceEvent | None`)

Missing scan/rung trace raises `KeyError`.

`inspect_event()` behavior:

- Returns `(scan_id, rung_id, event)` for the latest debug event.
- Prefers in-flight debug-path scan events when available.
- Falls back to latest retained committed debug-path event.
- Returns `None` when no debug trace context exists.

Planned future expansion:

- attempted writes and force re-apply events (pre-logic and post-logic)

The `RungTrace` schema will continue to evolve during Phase 3 based on editor rendering needs.

Incremental note (Phase 3, 2026-02-21):

- `inspect()` is currently trace-only and records data for scans executed through
  `scan_steps_debug()` (including DAP stepping paths).
- `inspect_event()` follows the same debug-path-only scope.
- Scans executed only via `step()`/`run()`/`run_for()`/`run_until()` may have no retained
  rung trace yet; in those cases `inspect()` raises `KeyError(rung_id)` after scan
  existence is validated.

---

## History and Playhead

History stores immutable `SystemState` snapshots (including initial state).

### Access

- `at(scan_id)` returns one snapshot (`KeyError` if absent/evicted).
- `range(start, end)` returns snapshots where `start <= scan_id < end`.
- `latest(n)` returns oldest-to-newest among the last `n`.

### Capacity

- Configurable `history_limit: int | None` (`None` = unbounded).
- When limit is exceeded, oldest scans are evicted (ring buffer behavior).
- Labels and traces for evicted scans are evicted too.
- If playhead points to an evicted scan, it moves to oldest retained scan.

### Time travel

- `playhead` is the current inspection `scan_id`.
- `seek(scan_id)` moves playhead to retained scan.
- `rewind(seconds)` moves backward from playhead timestamp to nearest retained scan with `timestamp <= target`.
- Execution is independent of playhead: `step()` always appends at history tip.

---

## Diff

```python
runner.diff(scan_a, scan_b) -> dict[str, tuple[Any, Any]]
```

- Compares tag values only (`tags`, not `memory`).
- Includes keys with changed values.
- Missing keys are treated as `None`.
- Deterministic key order (sorted by tag name).
- Missing scans raise `KeyError`.

---

## Fork

```python
alt_runner = runner.fork_from(scan_id)
```

Creates an independent runner with:

- same program logic
- same time-mode configuration
- initial state from the selected snapshot
- clean debug runtime state (no forces, breakpoints, monitors, labels, or pending patches)
- history containing only the fork snapshot initially

---

## Out of Scope (Milestone 11)

- on-disk history/session serialization
- remote debugger protocol
- GUI-specific rendering schema beyond `inspect()` trace data and VS Code decorations
