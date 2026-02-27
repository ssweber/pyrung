# Architecture

How the engine works under the hood. For the DSL vocabulary (tags, rungs, instructions), see [Core Concepts](../getting-started/concepts.md). For the execution API, see [Runner](runner.md).

## The Redux model

pyrung is architected like Redux: state is immutable, logic is a pure function, and execution is consumer-driven.

```
Logic(CurrentState) → NextState
```

Every `step()` call takes the current `SystemState`, evaluates all rungs as pure functions, and produces a new `SystemState`. The old state is still accessible. This makes programs deterministic, testable, and debuggable — the same state plus the same inputs always produce the same next state.

## SystemState

```python
class SystemState(PRecord):
    scan_id   : int    # scan counter (resets to 0 on STOP→RUN/reboot)
    timestamp : float  # simulation clock (seconds)
    tags      : PMap   # tag values, keyed by name string
    memory    : PMap   # engine-internal state (edge detection, timer fractionals)
```

`tags` is everything user code touches. `memory` is internal engine bookkeeping — edge detection bits (`rise`/`fall`), timer fractional accumulators, etc.

`SystemState` is a [`PRecord`](https://pyrsistent.readthedocs.io/) from the pyrsistent library — a frozen, persistent data structure that shares structure between versions for memory efficiency. Each scan produces a new `SystemState` without modifying the previous one.

## Scan cycle

Every `step()` executes exactly one complete scan cycle through nine phases:

```
Phase 0  SCAN START     Dialect resets (e.g., Click auto-clears SC40/SC43/SC44)
Phase 1  APPLY PATCH    One-shot inputs from patch() written to context
Phase 2  READ INPUTS    InputBlock values copied from external source
Phase 3  APPLY FORCES   Pre-logic force pass (debug overrides)
Phase 4  EXECUTE LOGIC  Rungs evaluated top-to-bottom
Phase 5  APPLY FORCES   Post-logic force pass (re-assert force values)
Phase 6  WRITE OUTPUTS  OutputBlock values pushed to external sink
Phase 7  ADVANCE CLOCK  scan_id += 1, timestamp updated per TimeMode
Phase 8  SNAPSHOT       New SystemState committed
```

All writes within a scan are batched in a `ScanContext` and committed atomically at phase 8. Rungs see each other's writes immediately — a write in rung 3 is visible to rung 4 in the same scan.

## ScanContext

`ScanContext` is the mutable working space for a single scan. It holds pending tag writes, memory updates, and force state. The engine creates one at scan start and commits it at phase 8 to produce the next immutable `SystemState`.

User code never touches `ScanContext` directly — it's an internal detail of the scan cycle. The `runner.active()` context manager reads and writes through it transparently.

## Consumer-driven execution

The engine never runs unsolicited. The consumer drives execution at whatever granularity it needs:

```python
runner.step()                    # one complete scan
runner.run(cycles=100)           # N scans
runner.run_for(1.0)              # advance by simulation time
runner.run_until(~Motor)         # stop on condition
```

This inversion of control is what makes pyrung suitable for testing, GUIs, and debuggers. A pytest test calls `step()` and asserts. A VS Code extension calls `scan_steps_debug()` and renders decorations. A soft PLC calls `step()` in a loop driven by a Modbus server.

## Source location capture

During the DSL build phase (`Rung`, `rise()`, `out()`, operators, builder-style APIs), each element captures its source file and line number. This metadata enables mapping from engine objects back to user code for editor integration.

Captured metadata per element:
- `source_file: str | None`
- `source_line: int | None`
- `end_line: int | None` (for block contexts like `Rung` and `branch`; best-effort via AST `end_lineno`)

Builder flows (`shift(...).clock(...).reset(...)`, `count_up(...).reset(...)`, etc.) preserve the original callsite metadata through the chain.

If rungs are built in a loop, multiple rung objects may share source lines. The mapping is best-effort in this case; explicit DSL declarations maintain a clean one-to-one mapping.

## Debug stepping APIs

These APIs are used by the DAP adapter and are not part of the typical user workflow.

### `scan_steps()` — rung-boundary generator

```python
for rung_index, rung, ctx in runner.scan_steps():
    print(f"After rung {rung_index}: {dict(ctx._tags_pending)}")
# scan commits when generator is exhausted
```

Executes one scan, yielding after each top-level rung evaluation. The scan only commits atomically when the generator is fully exhausted. Partially consuming the generator leaves the runner in a partially-evaluated state.

### `scan_steps_debug()` — instruction-level stepping

```python
for step in runner.scan_steps_debug():
    print(step.rung_index, step.kind, step.source_line, step.enabled_state)
```

Yields `ScanStep` objects at rung, branch, subroutine, and instruction boundaries. This is the API the DAP adapter uses to drive execution with source location information. Same commit semantics as `scan_steps()`.

## Rung inspection

`inspect()` and `inspect_event()` return retained debug trace data. Currently populated only through `scan_steps_debug()` (including DAP stepping paths) — scans produced by `step()`/`run()` do not retain rung trace.

### `inspect(rung_id, scan_id=None)`

Returns a `RungTrace` for one rung in one scan:

- `RungTrace.scan_id` — committed scan id
- `RungTrace.rung_id` — top-level rung index (0-based)
- `RungTrace.events` — ordered tuple of `RungTraceEvent`

Each `RungTraceEvent` captures one debug boundary:

- `kind`: `"rung"` | `"branch"` | `"subroutine"` | `"instruction"`
- `source_file`, `source_line`, `end_line`
- `subroutine_name`, `depth`, `call_stack`
- `enabled_state`, `instruction_kind`
- `trace`: `TraceEvent | None`

If `scan_id` is omitted, uses `runner.playhead`. Missing/evicted scans raise `KeyError(scan_id)`. Existing scans with no retained trace raise `KeyError(rung_id)`.

### `inspect_event()`

Returns the latest debug-trace event as `(scan_id, rung_id, RungTraceEvent)` or `None`:

- Prefers in-flight events from an active `scan_steps_debug()` scan
- Falls back to latest committed retained debug-path event
- Returns `None` when no debug trace context exists
