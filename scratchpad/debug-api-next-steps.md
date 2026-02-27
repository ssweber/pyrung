# Debug API: Next Steps Plan

## Context

The debugger platform is now in a strong state:

- Phase 1 (Force): complete.
- Phase 2 (Source Location + DAP): complete for current scope, including `stepOut` and terminate capability.
- Phase 3: complete for current scope (`B1`, `B4`, `B5`, `B2`, `B3`, `B3-follow-up`, `C3`, `C1`, `C2`).

Recently completed:

- `A1`/`A2`: DAP `stepOut`, capability audit, and terminate capability.
- `B1`: `PLCRunner` history retention and query API (`at`, `range`, `latest`) with bounded/unbounded retention.
- `B4`: `runner.diff(scan_a, scan_b)` with changed-only tag diff, missing-as-`None`, deterministic ordering.
- `B5`: `runner.fork_from(scan_id)` with clean mutable debug/runtime state and preserved time config.
- `B2`: `runner.playhead`, `runner.seek(scan_id)`, `runner.rewind(seconds)` with eviction-safe playhead behavior.
- `B3`: `runner.inspect(rung_id, scan_id=None)` with retained rung traces for debug-path scans.
- `B3-follow-up` (2026-02-21): unified DAP trace retrieval on core `runner.inspect_event()` for both
  in-flight debug stops and committed debug-path scans, with adapter hybrid fallback removed.
- `C3` (2026-02-21): monitors via `runner.monitor(tag, callback)` with registration handles
  (`id`, `remove`, `enable`, `disable`) and post-commit changed-value callbacks.
- `C1` (2026-02-21): predicate breakpoints via `runner.when(predicate).pause()` with registration
  handles and post-commit pause semantics for `run`/`run_for`/`run_until`.
- `C2` (2026-02-21): snapshot labels via `runner.when(predicate).snapshot(label)` plus
  `history.find(label)` / `history.find_all(label)`, dedup-per-scan, and eviction-aligned pruning.

The next objective is follow-up/editor integration work on top of the completed Phase 3 core APIs.

---

## Completed Items

### A1. `stepOut` handler

- Implemented in `src/pyrung/dap/adapter.py`.
- Covered by tests in `tests/dap/test_adapter.py`.

### A2. Capability audit

- `supportsStepOut` and `supportsTerminateRequest` now advertised.
- Terminate request handled consistently with adapter shutdown flow.

### B1. History storage on PLCRunner

- Implemented in `src/pyrung/core/history.py` and runner integration.
- Configurable `history_limit: int | None`.
- History appends once per committed scan.
- Queries available via `runner.history.at(scan_id)`, `.range(start, end)`, `.latest(n)`.

---

### B4. `runner.diff(scan_a, scan_b)`

- Compare `.tags` between two retained snapshots.
- Return `dict[str, tuple[Any, Any]]` for changed keys only.
- Missing keys treated as `None`.
- Deterministic sorted key order.

### B5. `runner.fork_from(scan_id)`

- Create new `PLCRunner` from retained snapshot.
- Keep same program logic and time configuration.
- Start with clean debug/runtime mutable state.
- Fork history starts with only the fork snapshot.

### B2. Playhead and time travel

- Add `runner.playhead`.
- Add `runner.seek(scan_id)`.
- Add `runner.rewind(seconds)`.
- Execution stays independent of playhead (`step()` appends at history tip).

### B3. `runner.inspect(rung_id, scan_id=None)`

- Stores retained rung-level trace data keyed by scan/rung.
- Defaults to `runner.playhead` when `scan_id` is omitted.
- Trace retention is pruned with history eviction.
- Current intentional scope: trace capture is debug-path only (`scan_steps_debug()`/DAP flow);
  scans created through `step()`/`run()`/`run_for()`/`run_until()` do not yet retain inspect trace.

### B3-follow-up. Unified core trace retrieval for DAP

- Added `runner.inspect_event() -> tuple[scan_id, rung_id, RungTraceEvent] | None`.
- In-flight debug scan trace state is maintained in core and reset on scan close/abort.
- Committed latest debug event fallback is maintained in core and invalidated by history eviction.
- DAP adapter now sources trace payloads via `inspect_event()` only.
- Removed adapter hybrid `ScanStep.trace` vs `inspect()` fallback branch and adapter-only committed
  trace bookkeeping.
- Preserved payload shape (`traceVersion`, `traceSource`, `scanId`, `rungId`, `step`, `regions`) and
  stepping/breakpoint semantics.

---

## Next Work

---

## Recommended Implementation Order

```text
Done:
C3  monitors
C1  predicate breakpoints
C2  snapshot labels
```

---

## Key Files

| File | Role |
|---|---|
| `src/pyrung/core/runner.py` | history/playhead/diff/fork + `inspect`/`inspect_event` trace APIs |
| `src/pyrung/core/history.py` | Snapshot storage/query primitives + label indexes (`find`/`find_all`) |
| `src/pyrung/dap/adapter.py` | DAP command handling and unified core trace consumption |
| `tests/core/test_history.py` | History/diff/fork/playhead behavior tests |
| `tests/core/test_monitors.py` | Monitor registration lifecycle and callback semantics |
| `tests/core/test_breakpoints_labels.py` | Predicate pause/snapshot behavior and label lookup semantics |
| `tests/core/test_inspect.py` | `inspect` + `inspect_event` debug trace retention/lifecycle tests |
| `tests/dap/test_adapter.py` | Step/continue/stepOut + trace payload/adapter behavior tests |

---

## Verification

- `B4`: verify changed-only diff output, missing-as-None handling, deterministic key order.
- `B5`: verify exact snapshot seeding, clean debug mutable state, independent parent/fork progression.
- `B2`: verify seek/rewind semantics, playhead independence from execution tip, eviction-safe clamping.
- `B3-follow-up`: verify in-flight + committed `inspect_event()`, aborted debug scan reset behavior,
  adapter unified trace emission, and trace eviction alignment.
- `C3`: verify monitor callback invocation on changed committed values, handle lifecycle behavior,
  and callback exception propagation.
- `C1/C2`: verify post-commit predicate pause behavior for `run*`, snapshot labeling, label dedup
  per scan, and eviction-aligned `history.find`/`find_all`.
- Full gate used for latest completion:
  - `uv run pytest -q`
  - `uv run mkdocs build --strict`
