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
- [x] Add broader benchmark coverage for `examples/click_conveyor.py`
- [x] Add benchmark coverage for one busy real-world style program
- [x] Record perf comparisons for single historical lookup and `_replay_range`

### Phase 6: Transitional cleanup
- [x] Prove cache warming is no longer meaningfully helped by `hydrate()` — single lookup is 0.9–4 ms compiled; pre-warming is marginal
- [x] Remove `hydrate()` — compiled replay makes on-demand access fast enough
- [x] Rename classic → interpreted throughout (runner internals, test harness, CLI option)
- [x] No feature flag needed — parity harness is green, 2763 tests pass on `both` backend

---

## What Landed In This Slice

- [x] `CompiledPLC` exists and is replay-correct enough to drive historical reconstruction.
- [x] `compile_kernel` is exported as a supported API.
- [x] `PLC` historical reconstruction now prefers compiled replay automatically for supported fixed-step `Program` instances.
- [x] Unsupported programs transparently fall back to the classic replay path.
- [x] Public `PLC` and `History` APIs remain unchanged.
- [x] Debug trace reconstruction is intentionally still on the classic replay engine.
- [x] Benchmarks captured — compiled replay is 4–7x faster than classic.

---

## Benchmark Results

### Initial measurement (2026-04-20)

Measured via `scratchpad/bench_replay.py` (20 iterations, median). Compiled path uses `step()` for all scans.

| Program | Benchmark | Classic | Compiled | Speedup |
|---------|-----------|---------|----------|---------|
| click_conveyor (1k scans) | `replay_to` early | 8.9 ms | 2.1 ms | 4.2x |
| click_conveyor (1k scans) | `replay_to` mid | 60.4 ms | 14.3 ms | 4.2x |
| click_conveyor (1k scans) | `_replay_range` 50 scans | 75.1 ms | 17.8 ms | 4.2x |
| busy_synthetic (1k scans) | `replay_to` early | 19.6 ms | 3.5 ms | 5.6x |
| busy_synthetic (1k scans) | `replay_to` mid | 163.3 ms | 22.5 ms | 7.3x |
| busy_synthetic (1k scans) | `_replay_range` 50 scans | 199.4 ms | 27.8 ms | 7.2x |
| busy_synthetic (5k scans) | `replay_to` mid | 162.8 ms | 22.4 ms | 7.3x |

### After replay fast path (2026-04-21)

Added `step_replay()` — skips intermediate `SystemState` construction and dead `_prev:` memory writes. Profile showed those two accounted for 70% of per-step cost.

| Program | Benchmark | Classic | Compiled | Speedup |
|---------|-----------|---------|----------|---------|
| click_conveyor (1k scans) | `replay_to` early | 8.8 ms | 0.9 ms | 9.7x |
| click_conveyor (1k scans) | `replay_to` mid | 61.4 ms | 2.3 ms | 26.9x |
| click_conveyor (1k scans) | `_replay_range` 50 scans | 76.6 ms | 8.4 ms | 9.2x |
| busy_synthetic (1k scans) | `replay_to` early | 19.8 ms | 1.7 ms | 11.4x |
| busy_synthetic (1k scans) | `replay_to` mid | 165.1 ms | 4.0 ms | 40.9x |
| busy_synthetic (1k scans) | `_replay_range` 50 scans | 202.4 ms | 13.7 ms | 14.8x |
| busy_synthetic (5k scans) | `replay_to` mid | 167.1 ms | 4.0 ms | 41.8x |

### Investigation findings

- **Compilation cost**: 3.7 ms one-time, negligible — cached on PLC instance
- **Steady-state step() ratio**: 6.0x (classic 1.27 ms vs compiled 0.21 ms per step)
- **step_replay() per-step**: 22.8 μs vs step() 203.9 μs (89% faster)
- **Kernel itself**: 11.2 μs/call — only 8% of step() but 38% of step_replay()
- **Locals experiment**: 1.01x — dict access not a bottleneck at 144 accesses/scan
- **Dead work confirmed**: `_prev:` never appears in generated source; `_capture_previous_states` memory loop was purely dead during compiled execution

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
- [x] Benchmark evidence captured for `click_conveyor.py` and another busy program
- [x] `hydrate()` removed — compiled replay makes cache warming unnecessary
