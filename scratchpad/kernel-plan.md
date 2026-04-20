# Replay Kernel Compiler — Implementation Plan

## Context

pyrung replays historical PLC states by re-running the full 8-phase scan cycle (ScanContext, condition trees, Rung objects, debug hooks). This is expensive. The CircuitPy codegen already compiles `Program` into flat Python code — no Rung objects, no ScanContext. We want to reuse that compilation pipeline to produce a fast in-process replay kernel: `f(state_dict, dt, patches, forces) -> next_state_dict`.

During investigation we found one semantic difference between the two paths that should be fixed first: **instruction-internal conditions** (timer/counter reset, counter down, shift clock/reset, drum jog/reset) read live mutable state in the codegen but read a rung-entry frozen snapshot in the core engine.

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
- [ ] Add regression test: intra-rung write NOT visible to timer reset condition

### Phase 1: `ReplayKernel` + `CompiledKernel` dataclass
- [x] Create `src/pyrung/core/kernel.py`
- [x] `ReplayKernel` class (plain-dict state container)
- [x] `BlockSpec` dataclass (symbol, size, default, tag_type)
- [x] `CompiledKernel` dataclass (step_fn, referenced_tags, block_specs, edge_tags, source)

### Phase 2: Hardware-free `CodegenContext` + kernel renderer
- [x] Add `CodegenContext.for_kernel(program)` classmethod in `context.py`
- [x] Create `src/pyrung/circuitpy/codegen/render_kernel.py`
- [x] `_render_kernel_function(ctx)` — prologue (read from kernel), body (compile_rung), epilogue (write back + prev capture)
- [x] Inline helper functions (_clamp_int, _rise, _fall, etc.) in rendered source
- [x] Subroutine compilation support
- [x] `compile_kernel(program)` top-level entry point (exec + return CompiledKernel)
- [x] Basic smoke test: compile a trivial program, verify generated source compiles

### Phase 3: `CompiledPLC` wrapper
- [ ] Create `src/pyrung/core/compiled_plc.py`
- [ ] `__init__` compiles program, initializes ReplayKernel with tag defaults
- [ ] `step()` — 8-phase scan cycle (patches, pre-force, dt, logic, post-force, prev, advance)
- [ ] `patch()`, `force()`, `unforce()`, `clear_forces()`
- [ ] `current_state` property (dict view of tags + memory)
- [ ] `run()` / `run_for()` convenience methods

### Phase 4: Equivalence test harness
- [ ] Create `tests/core/test_kernel_equivalence.py`
- [ ] `assert_equivalent()` helper (runs both PLC and CompiledPLC, compares per-scan)
- [ ] Tier 1 tests: out, latch, reset
- [ ] Tier 1 tests: on_delay, off_delay (multiple time units)
- [ ] Tier 1 tests: count_up, count_down (with reset, with down)
- [ ] Tier 1 tests: edge detection (rise, fall)
- [ ] Tier 1 tests: copy, calc
- [ ] Tier 1 tests: branches (nested, condition snapshotting)
- [ ] Tier 1 tests: drums (event, time)
- [ ] Tier 1 tests: shift register
- [ ] Tier 1 tests: for-loop, call/return
- [ ] Tier 1 tests: blocks, indirect refs
- [ ] Tier 2 tests: multi-instruction programs with forces/patches
- [ ] Tier 3 tests: existing test programs reused

### Phase 5: Gap fixes
- [ ] Fix any semantic differences exposed by equivalence tests
- [ ] System points: exclude or stub
- [ ] Modbus: exclude from kernel (requires protocol stack)
- [ ] Verify `_prev` coverage matches

---

## Key files

| File | Role |
|------|------|
| `src/pyrung/circuitpy/codegen/compile/_core.py` | `compile_rung()`, `compile_condition()` — reused as-is for body |
| `src/pyrung/circuitpy/codegen/compile/_instructions_basic.py` | Timer/counter compilers — Phase 0 snapshot fix |
| `src/pyrung/circuitpy/codegen/compile/_instructions_block.py` | Drum/shift/search compilers — Phase 0 snapshot fix |
| `src/pyrung/circuitpy/codegen/context.py` | `CodegenContext` — add `for_kernel()` classmethod |
| `src/pyrung/circuitpy/codegen/render.py` | Existing renderer — reference for prologue/epilogue patterns |
| `src/pyrung/core/runner.py` | `PLC._scan_steps()` (line 1894), `_prepare_scan()` (1756), `_commit_scan()` (1794) |
| `src/pyrung/core/context.py` | `ScanContext`, `ConditionView` — reference for snapshot semantics |
| `src/pyrung/core/input_overrides.py` | `InputOverrideManager` — reference for force/patch application |
| `src/pyrung/core/kernel.py` | **NEW** — `ReplayKernel`, `CompiledKernel` |
| `src/pyrung/core/compiled_plc.py` | **NEW** — `CompiledPLC` wrapper |
| `src/pyrung/circuitpy/codegen/render_kernel.py` | **NEW** — kernel function renderer |
| `tests/core/test_kernel_equivalence.py` | **NEW** — oracle-based equivalence tests |

## Verification

1. `make lint` — all new code passes ruff + ty + codespell
2. `make test` — all existing tests still pass
3. Equivalence tests pass for all instruction types
4. Inspect generated kernel source (`CompiledKernel.source`) for a non-trivial program to verify it looks correct
