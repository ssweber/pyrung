# Debug API: Next Steps Plan

## Context

The debugger platform is now in a strong state:

- Phase 1 (Force): complete.
- Phase 2 (Source Location + DAP): complete for current scope, including `stepOut` and terminate capability.
- Phase 3: quick wins complete (`B1`, `B4`, `B5`, `B2`).

Recently completed:

- `A1`/`A2`: DAP `stepOut`, capability audit, and terminate capability.
- `B1`: `PLCRunner` history retention and query API (`at`, `range`, `latest`) with bounded/unbounded retention.
- `B4`: `runner.diff(scan_a, scan_b)` with changed-only tag diff, missing-as-`None`, deterministic ordering.
- `B5`: `runner.fork_from(scan_id)` with clean mutable debug/runtime state and preserved time config.
- `B2`: `runner.playhead`, `runner.seek(scan_id)`, `runner.rewind(seconds)` with eviction-safe playhead behavior.

The next objective is to complete the remaining Phase 3 APIs built on top of history.

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

---

## Next Work (Phase 3)

### B3. `runner.inspect(rung_id, scan_id=None)`

- Store and query rung-level traces by scan/rung.
- Use playhead when `scan_id` omitted.

### C3. Monitors

- `runner.monitor(tag, callback)` on value changes after commit.
- Handle enable/disable/remove with monitor IDs.

### C1/C2. Predicate breakpoints and labels

- `runner.when(predicate).pause()` and `.snapshot(label)`.
- Label lookup APIs (`history.find`, `history.find_all`) once label storage exists.

---

## Recommended Implementation Order

```text
B3  inspect               <- richer debugger rendering
C3  monitors              <- independent and useful
C1  predicate breakpoints <- depends on snapshot evaluation flow
C2  snapshot labels       <- extends C1 + history
```

---

## Key Files

| File | Role |
|---|---|
| `src/pyrung/core/runner.py` | `diff`, `fork_from`, playhead APIs, upcoming `inspect` |
| `src/pyrung/core/history.py` | Snapshot storage/query primitives |
| `src/pyrung/dap/adapter.py` | DAP command handling and capabilities |
| `tests/core/test_history.py` | History/diff/fork/playhead behavior tests |
| `tests/dap/test_adapter.py` | Step/continue/stepOut DAP tests |

---

## Verification

- `B4`: verify changed-only diff output, missing-as-None handling, deterministic key order.
- `B5`: verify exact snapshot seeding, clean debug mutable state, independent parent/fork progression.
- `B2`: verify seek/rewind semantics, playhead independence from execution tip, eviction-safe clamping.
- Regression: run history tests and DAP stepOut tests.
- Full gate: `make` (install + lint + test) should pass.
