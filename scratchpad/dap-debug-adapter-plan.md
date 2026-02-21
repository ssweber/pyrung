## Tightened Plan: DAP Adapter v1 (Keep `step()`, Add `scan_steps()`, Rung-Level Execution with Instruction-Line Breakpoint Mapping)

### Incremental Follow-up (2026-02-21): Hybrid Trace Source

- `pyrungTrace` emission now uses a hybrid source model:
  - live `ScanStep.trace` for mid-scan stops (step/continue/pause behavior unchanged)
  - `runner.inspect(scan_id, rung_id)` fallback when live step context is unavailable
- Trace payload now includes `traceSource`, `scanId`, and `rungId` for client correlation.
- Full adapter simplification still depends on future all-scan inspect coverage in core.

### Summary
Build a minimal, robust DAP adapter aligned with `spec/core/debug.md` Phase 2:
1. Keep `PLCRunner.step()` as-is (no rename now).
2. Add `PLCRunner.scan_steps()` for rung-boundary stepping.
3. Implement `pyrung.dap` (stdin/stdout DAP server) with solid concurrency and protocol behavior.
4. Support breakpoints set on rung lines and instruction lines by mapping both to containing rung indices.
5. Add a minimal declarative VS Code extension scaffold to launch the adapter.

### Public APIs / Interfaces
1. Add to `src/pyrung/core/runner.py`:
- `scan_steps(self) -> Generator[tuple[int, Rung, ScanContext], None, None]`
- Behavior:
  - Performs full pre-scan setup once.
  - Yields after each rung evaluation.
  - Performs full post-scan cleanup and commit when generator is exhausted.
2. Keep existing `step()` unchanged as public primary API.
- Implement `step()` by fully consuming `scan_steps()` for code-path parity.
3. Add new package interface:
- `python -m pyrung.dap`
- Console script `pyrung-dap = "pyrung.dap:main"` in `pyproject.toml`.

### Engine Changes (`src/pyrung/core/runner.py`)
1. Extract current scan-cycle body into generator-friendly structure:
- Shared pre-scan block:
  - `ScanContext` creation
  - `on_scan_start`
  - pending patch apply/clear
  - pre-logic force apply
  - dt calculation
  - `ctx.set_memory("_dt", dt)`
- Rung loop:
  - `for i, rung in enumerate(self._logic):`
  - `rung.evaluate(ctx)`
  - `yield i, rung, ctx`
- Shared post-scan block:
  - post-logic force apply
  - `_prev:*` updates
  - `on_scan_end`
  - `self._state = ctx.commit(dt=dt)`
2. `step()` delegates to full generator consumption.
3. `run()`, `run_for()`, `run_until()` continue calling `step()` (no behavior change).

### DAP Adapter Design (`src/pyrung/dap/`)
1. Files:
- `src/pyrung/dap/__init__.py` (exports `main`)
- `src/pyrung/dap/__main__.py`
- `src/pyrung/dap/protocol.py` (framing, envelopes, seq IDs)
- `src/pyrung/dap/adapter.py` (request handlers, runtime state)
2. Runtime state in adapter:
- `_runner: PLCRunner | None`
- `_scan_gen: Generator[...] | None`
- `_current_rung_index: int | None`
- `_current_rung: Rung | None`
- `_current_ctx: ScanContext | None`
- `_breakpoints_by_file: dict[str, set[int]]` (canonicalized file keys)
- `_breakpoint_rung_map: dict[str, dict[int, set[int]]]` (file->line->rung indices)
- `_forces_scope_ref`, `_tags_scope_ref`, `_memory_scope_ref`
- `_continue_thread: Thread | None`
- `_pause_event: threading.Event`
- `_stop_event: threading.Event`
3. Concurrency model:
- Reader thread parses inbound DAP messages into queue.
- Main thread handles request dispatch and all outbound writes.
- `continue` starts worker thread that advances rung-by-rung.
- Worker thread never writes stdout directly; it posts “emit stopped event” tasks back to main thread queue.
- `pause` only sets `_pause_event`; continue worker checks each rung and exits quickly.
4. Protocol correctness:
- `continue` request returns immediately with `allThreadsContinued`.
- Later `stopped` events emitted for `breakpoint` or `pause`.
- `setBreakpoints` returns verification result per requested line.
- Path canonicalization for keys:
  - absolute path
  - normalized separators
  - `normcase` on Windows for case-insensitive matching.

### Launch / Program Discovery
1. Replace raw `exec(read_text())` approach with `runpy.run_path`.
2. Launch semantics:
- `program` must be a Python file path.
- Execute via `runpy.run_path(str(path), run_name="__main__")`.
3. Discovery order:
- If namespace contains `runner` and it is `PLCRunner`, use it.
- Else if exactly one `PLCRunner` instance found, use it.
- Else if exactly one `Program` instance found, wrap with `PLCRunner(program)`.
- Else fail launch with explicit actionable error listing discovered candidates.

### Breakpoints and Instruction-Line Mapping
1. Build static rung line index at launch:
- For each top-level rung index, collect all breakable lines:
  - rung `source_line`
  - all nested branch rung `source_line`
  - instruction `source_line` values from that rung tree
- Map each collected line to containing top-level rung index.
2. `setBreakpoints` behavior:
- Accept lines set in file by editor.
- Verify `true` when line exists in breakable-line map.
- Store only verified lines.
3. Hit behavior with chosen mode (`Map to rung`):
- Stop when execution reaches rung boundary after evaluating that mapped rung.
- Report `stopped(reason="breakpoint")` with current rung’s source location.
4. Document limitation:
- Breakpoints on instruction lines pause at containing rung boundary, not at instruction micro-step.

### DAP Commands (v1 exact scope)
1. `initialize`, `configurationDone`, `disconnect`
2. `launch`
3. `threads` (single thread id=1)
4. `stackTrace`:
- Return current rung frame first.
- Optionally include surrounding rung frames for context.
5. `scopes`:
- Tags, Forces, Memory
6. `variables`:
- Tags:
  - between scans: `runner.current_state.tags`
  - mid-scan: overlay pending context writes on top of committed tags
- Forces: `runner.forces`
- Memory:
  - between scans: `current_state.memory`
  - mid-scan: overlay pending memory writes
7. `next`, `stepIn`:
- Same behavior (advance one rung boundary).
8. `continue`, `pause`
9. `setBreakpoints`
10. `evaluate` debug-console commands:
- `force <tag> <value>`
- `remove_force <tag>`
- `clear_forces`

### VS Code Extension Scaffold (`editors/vscode/pyrung-debug/`)
1. Create declarative extension:
- `package.json` with debugger contribution `type: "pyrung"`
- Launch config with required `program` path
- Python runtime invocation of adapter module
- Python-language breakpoints contribution
2. `extension.js` remains minimal activate/deactivate stubs.
3. No TypeScript build pipeline in v1.

### Documentation Updates
1. Keep `step()` wording in specs/docs (no rename yet).
2. Add `scan_steps()` as debug/advanced stepping API in:
- `spec/core/engine.md`
- `spec/core/debug.md`
3. Document breakpoint mapping behavior and current rung-boundary stepping limit.

### Test Plan
1. Core runner tests:
- `scan_steps()` yields once per rung.
- Generator exhaustion commits state exactly once.
- `step()` behavior remains unchanged vs current tests.
- Partial consumption does not commit until exhausted.
2. Adapter unit/integration tests:
- DAP framing round-trip for request/response envelopes.
- Launch discovery success/failure cases.
- `next` advances one rung and emits `stopped(step)`.
- `continue` hits verified breakpoint and emits `stopped(breakpoint)`.
- `pause` during continue emits `stopped(pause)` quickly.
- `setBreakpoints` verifies rung lines and instruction lines mapped to rungs.
- Variables reflect mid-scan pending writes.
- Evaluate force commands mutate runner force table as expected.
- Path normalization works on Windows-style and POSIX-style paths.
3. Manual smoke test:
- Launch sample DSL file in VS Code debug host.
- Set breakpoint on instruction line and confirm rung-boundary stop.
- Step through rungs and inspect variables/forces/memory.

### Assumptions and Defaults
1. Keep `step()` as stable primary API for now.
2. `scan_steps()` is additive and intended mainly for debugger integration.
3. Single-thread PLC model (DAP `threadId=1`).
4. Breakpoints are source-line based and mapped to top-level rung execution units.
5. No instruction-level pause semantics in v1.
6. Adapter executes user code in-process with trusted local scripts (developer workflow assumption).
