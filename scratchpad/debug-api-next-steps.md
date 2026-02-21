# Debug API: Next Steps Plan

## Context

The debugger infrastructure is mature: `PLCDebugger` produces typed `ScanStep`/`TraceEvent` data via generators, the DAP adapter maps this to VS Code, and the VS Code extension renders inline decorations. Phase 1 (Force) is complete. Phase 2 (Source Location + DAP) is nearly complete — the main gap is `stepOut`. Phase 3 (the remaining debug API from the spec) has not been started.

The goal is to close the Phase 2 gap (`stepOut`) and then build the Phase 3 runner-level debug API incrementally, starting with the pieces that have the most payoff or unblock other features.

---

## Step-Out Semantics

**What does step-out mean for pyrung?**

The debugger has a tree of nesting levels — each `ScanStep` carries `depth` and `call_stack`:

| You're inside... | step-out means... |
|---|---|
| **Subroutine** (call_stack non-empty) | Run until call_stack shrinks (subroutine returns to caller) |
| **Branch** (depth > 0, no subroutine) | Run until depth decreases to parent rung level |
| **Top-level rung** (depth 0) | Finish the current scan cycle (advance until new scan starts) |

Implementation in the DAP adapter — `_on_stepOut`:
1. Record `origin_depth = current_step.depth` and `origin_stack_len = len(current_step.call_stack)`
2. Loop `_advance_one_step_locked()` until:
   - Step depth < origin_depth, OR
   - Step call_stack length < origin_stack_len, OR
   - Scan generator exhausts (new scan boundary)
3. Return `("stopped", reason="step")`

This follows the same pattern as `_on_next` and `_on_stepIn` — a synchronous loop with a stop condition.

Also add `"supportsStepOut": True` to capabilities in `_on_initialize`.

---

## Batch A — Close Phase 2 (DAP adapter polish)

### A1. `stepOut` handler
- File: `src/pyrung/dap/adapter.py`
- New method `_on_stepOut` with depth/call_stack tracking as above
- Add capability flag
- Tests in `tests/dap/test_adapter.py`

### A2. Capability audit
- Review DAP capabilities dict and declare what we support/don't
- Consider adding `supportsTerminateRequest` (maps to `_on_disconnect`)

---

## Batch B — History & Inspection (foundation for Phase 3)

These are the building blocks everything else depends on.

### B1. History storage on PLCRunner
- New `History` object (ring-buffer of `(scan_id, SystemState)`)
- Configurable `history_limit: int | None` (default `None` = unbounded)
- Each `_commit_scan` appends to history with incrementing `scan_id`
- API: `runner.history.at(scan_id)`, `.range()`, `.latest(n)`
- Eviction: oldest-first when limit exceeded
- File: new `src/pyrung/core/history.py` + runner integration

### B2. Playhead & time travel
- `runner.playhead` property (current inspection scan_id, defaults to latest)
- `runner.seek(scan_id)` — move playhead to retained scan
- `runner.rewind(seconds)` — move backward by timestamp delta
- Execution (`step()`) always appends at tip regardless of playhead
- File: extends `History` + runner properties

### B3. `runner.inspect(rung_id, scan_id=None)`
- Store per-rung trace data alongside history snapshots
- Return `RungTrace` with conditions, instructions, enabled_state
- Uses playhead when scan_id omitted
- File: extends `History` with trace storage

### B4. `runner.diff(scan_a, scan_b)`
- Compare `.tags` dicts from two historical snapshots
- Return `dict[str, tuple[Any, Any]]` for changed keys
- Missing keys treated as `None`; sorted by tag name
- Quick to implement once history exists
- File: method on runner or History

### B5. `runner.fork_from(scan_id)`
- Create new PLCRunner with same program logic + time config
- Initial state from historical snapshot
- Clean debug state (no forces/breakpoints/monitors)
- History contains only the fork snapshot
- File: method on runner

---

## Batch C — Breakpoints, Monitors, Labels

### C1. Predicate breakpoints (`runner.when(predicate).pause()`)
- `when()` returns a builder; `.pause()` / `.snapshot("label")` register it
- Predicates evaluated on each committed scan snapshot
- `.pause()` halts `run()`/`run_for()`/`run_until()`
- `.snapshot("label")` tags the scan without halting
- Returns handle with `remove()`, `enable()`, `disable()`, `id`
- File: new `src/pyrung/core/breakpoint.py` + runner integration

### C2. Snapshot labels
- `history.find(label)` / `history.find_all(label)` for labeled snapshots
- Labels stored alongside history entries; evicted with their scan

### C3. Monitors (`runner.monitor(tag, callback)`)
- Fires after each committed scan when tag value changed
- `callback(current_value, previous_value)`
- Returns handle with `remove()`, `enable()`, `disable()`, `id`
- Multiple monitors per tag allowed
- File: new `src/pyrung/core/monitor.py` or inline in runner

---

## Recommended Implementation Order

```
A1  stepOut               ← Quick win, closes Phase 2
A2  Capability audit      ← Tiny, do alongside A1
B1  History storage       ← Foundation for everything in Phase 3
B4  diff                  ← Trivial once B1 exists
B5  fork_from             ← Trivial once B1 exists
B2  Playhead / seek       ← Extends B1
B3  inspect               ← Extends B1+B2 with trace storage
C3  Monitors              ← Independent, no history dependency
C1  Predicate breakpoints ← Depends on B1 for snapshot access
C2  Snapshot labels       ← Extends C1 + B1
```

A1+A2 can be done immediately. B1 unblocks the majority of Phase 3.

---

## Key Files

| File | Role |
|---|---|
| `src/pyrung/dap/adapter.py` | DAP adapter — stepOut goes here |
| `src/pyrung/core/runner.py` | PLCRunner — history/inspect/diff/fork APIs |
| `src/pyrung/core/debugger.py` | PLCDebugger — trace generation engine |
| `src/pyrung/core/debug_trace.py` | Typed trace models |
| `tests/dap/test_adapter.py` | DAP adapter tests |
| `tests/core/test_debugger_refactor.py` | Core debugger tests |

---

## Verification

- **stepOut**: Test stepping out of subroutines, branches, and top-level rungs. Verify call_stack and depth transitions. Run `make test`.
- **History**: Test ring buffer eviction, scan_id monotonicity, capacity limits.
- **All**: `make` (install + lint + test) must pass green.
