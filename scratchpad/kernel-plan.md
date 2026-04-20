# Compiled Replay Cutover Plan

## End Goal

Use `CompiledPLC` as the replay-correct kernel runner, then cut it into `PLC` so normal historical state access uses compiled replay automatically when the program is kernel-supported.

The user-facing goal is unchanged APIs with seamless long, low-memory history. `CompiledPLC` is an internal stepping stone, not a new workflow.

Classic replay remains available as the automatic fallback for unsupported programs, and stays in place for trace/debug-heavy paths such as `replay_trace_at()`.

---

## Context

`pyrung` historically reconstructs PLC state by re-running the full 8-phase scan cycle with `ScanContext`, condition trees, rung objects, and debug plumbing. That is replay-correct, but expensive for older history reads.

The CircuitPy codegen path already compiles a `Program` into flat Python code over plain state containers. The revised direction is:

1. Keep compiling programs into a replay-capable kernel.
2. Wrap that kernel in `CompiledPLC` so it reproduces replay-critical scan semantics.
3. Use that engine inside `PLC` historical reconstruction without requiring users to opt into a new API.

During the earlier compiler work we also found and fixed an important semantic gap: instruction-internal helper conditions in codegen must read rung-entry snapshots, not live mutable state.

---

## Checklist

### Phase 0: Fix instruction-internal condition snapshot semantics in codegen
- [x] Add `_helper_condition_snapshots` field to `CodegenContext`
- [x] Add `_collect_helper_conditions()` to walk rung tree for instruction-internal conditions
- [x] Modify `compile_rung()` to pre-compute helper conditions at rung entry
- [x] Add `_get_condition_snapshot()` helper for instruction compilers
- [x] Update `_compile_on_delay_instruction` to use snapshot
- [x] Update `_compile_count_up_instruction` to use snapshot (reset + down)
- [x] Update `_compile_count_down_instruction` to use snapshot
- [x] Update `_compile_shift_instruction` to use snapshot (clock + reset)
- [x] Update `_compile_event_drum_instruction` to use snapshot (events + reset + jump + jog)
- [x] Update `_compile_time_drum_instruction` to use snapshot (reset + jump + jog)
- [x] All 2754 tests pass
- [x] Lint clean (ruff + ty + codespell)
- [x] Add regression test: intra-rung write NOT visible to timer reset condition

### Phase 1: `ReplayKernel` + `CompiledKernel` foundation
- [x] Create `src/pyrung/core/kernel.py`
- [x] Add `ReplayKernel` class (plain-dict state container)
- [x] Add `BlockSpec` dataclass (symbol, size, default, tag_type)
- [x] Add `CompiledKernel` dataclass (step_fn, referenced_tags, block_specs, edge_tags, source)

### Phase 2: Hardware-free kernel compilation
- [x] Add `CodegenContext.for_kernel(program)` in `context.py`
- [x] Create `src/pyrung/circuitpy/codegen/render_kernel.py`
- [x] Render kernel prologue/body/epilogue against `ReplayKernel`
- [x] Inline kernel helper functions (`_clamp_int`, `_rise`, `_fall`, etc.)
- [x] Support subroutine compilation in the kernel renderer
- [x] Add `compile_kernel(program)` as a supported entry point
- [x] Export `compile_kernel` from `pyrung.circuitpy.codegen`
- [x] Basic smoke test: compile a trivial program and verify generated source compiles

### Phase 3: `CompiledPLC` replay engine
- [x] Create `src/pyrung/core/compiled_plc.py`
- [x] Initialize from compiled kernel + `ReplayKernel` defaults
- [x] Support `step()`, `run()`, and `run_for()`
- [x] Support `patch()`, `force()`, `unforce()`, and `clear_forces()`
- [x] Expose `simulation_time` and `current_state`
- [x] Reproduce replay-critical scan semantics:
- [x] Patch drain ordering
- [x] Pre-force and post-force ordering
- [x] `_dt` updates
- [x] `_prev` capture from post-force final values
- [x] Block/tag synchronization
- [x] RTC/system-point derived values needed for replay parity
- [x] Transient fault/status clearing
- [x] Use `InputOverrideManager` plus a small runtime shim instead of `ScanContext`
- [x] Export `CompiledPLC` from `pyrung.core`

### Phase 4: Transparent historical replay cutover inside `PLC`
- [x] Add a cheap "is this program kernel-supported?" gate for replay internals
- [x] Update `runner.py` replay internals to choose compiled replay when supported
- [x] Keep automatic fallback to classic replay for unsupported programs
- [x] Route `PLC.replay_to()` through the compiled-or-classic facade
- [x] Route `History.at()` through the same unchanged facade
- [x] Keep `seek`, `rewind`, `diff`, and history-driven cause/effect reads on unchanged public APIs
- [x] Preserve correct behavior for DAP history consumers through the existing surface
- [x] Keep `replay_trace_at()` on the classic replay path for now
- [x] Avoid introducing any required caller-facing API changes in this slice

### Phase 5: Replay parity coverage
- [x] Add kernel bootstrap tests for `compile_kernel()` / `ReplayKernel`
- [x] Add `CompiledPLC` parity tests for patch/force ordering and `_prev` capture
- [x] Add coverage for timers, counters, branches with snapshot semantics, shift/drum basics, block-backed tags, and subroutine/call behavior
- [x] Add replay cutover tests verifying `PLC.replay_to()` and `History.at()` match classic replay for kernel-supported programs
- [x] Add automatic fallback tests for unsupported programs
- [x] Verify `replay_trace_at()` still works via the classic path
- [ ] Add broader benchmark coverage for `examples/click_conveyor.py`
- [ ] Add benchmark coverage for one busy real-world style program
- [ ] Record perf comparisons for single historical lookup and `_replay_range`

### Phase 6: Transitional cleanup
- [ ] Prove cache warming is no longer meaningfully helped by `hydrate()`
- [ ] Make `hydrate()` a compatibility wrapper or deprecate it
- [ ] Remove any remaining internal assumptions that classic replay is the default reconstruction path
- [ ] Decide whether a temporary feature flag is needed only if parity gaps appear

---

## What Landed In This Slice

- [x] `CompiledPLC` exists and is replay-correct enough to drive historical reconstruction.
- [x] `compile_kernel` is exported as a supported API.
- [x] `PLC` historical reconstruction now prefers compiled replay automatically for supported fixed-step `Program` instances.
- [x] Unsupported programs transparently fall back to the classic replay path.
- [x] Public `PLC` and `History` APIs remain unchanged.
- [x] Debug trace reconstruction is intentionally still on the classic replay engine.
- [ ] Benchmarks and `hydrate()` deprecation/removal are still follow-up work.

---

## Key Files

| File | Role |
|------|------|
| `src/pyrung/circuitpy/codegen/compile/_core.py` | `compile_rung()`, `compile_condition()` reused by kernel compilation |
| `src/pyrung/circuitpy/codegen/compile/_instructions_basic.py` | Timer/counter helpers and snapshot-sensitive instruction compilers |
| `src/pyrung/circuitpy/codegen/compile/_instructions_block.py` | Drum/shift/search helpers and snapshot-sensitive instruction compilers |
| `src/pyrung/circuitpy/codegen/context.py` | `CodegenContext.for_kernel()` |
| `src/pyrung/circuitpy/codegen/render.py` | Existing renderer used as reference for emitted program structure |
| `src/pyrung/circuitpy/codegen/render_kernel.py` | Kernel function renderer |
| `src/pyrung/circuitpy/codegen/__init__.py` | `compile_kernel` export |
| `src/pyrung/core/kernel.py` | `ReplayKernel`, `CompiledKernel`, `BlockSpec` |
| `src/pyrung/core/compiled_plc.py` | `CompiledPLC` wrapper |
| `src/pyrung/core/runner.py` | Replay cutover and compiled/classic replay selection |
| `src/pyrung/core/input_overrides.py` | Force/patch application model reused by compiled replay |
| `src/pyrung/core/__init__.py` | `CompiledPLC` export |
| `tests/core/test_compiled_replay.py` | Kernel bootstrap, parity, cutover, and fallback coverage |

## Verification

- [x] `make lint`
- [x] `uv run pytest tests/core tests/circuitpy/test_codegen.py -q`
- [x] Compiled replay cutover covered by focused replay parity tests
- [ ] Benchmark evidence captured for `click_conveyor.py` and another busy program
- [ ] `hydrate()` retirement decision documented from measured results
