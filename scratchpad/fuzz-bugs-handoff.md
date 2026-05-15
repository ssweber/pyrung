# Fuzz Bug Fix Handoff — 2026-05-08

## Bug 1: Timer absorption soundness (`test_timer_acc_downstream_absorption`)

**Status: Root cause NOT yet confirmed — initial hypothesis was wrong.**

- `_has_forbidden_data_read` in `absorb.py:264` only checks instruction `_reads` — this was the initial suspect but it turns out `_find_redundant_acc_absorptions` correctly rejects the Acc (returns empty frozenset). The atom `T0_Acc >= 10` IS found in `all_exprs` and `_is_acc_done_redundant` returns False because threshold 10 != preset 50.
- The real problem is elsewhere in the pipeline. Optimized prove explores only **1 state** and returns Proven, meaning something aggressively removes T0_Done/T0_Acc from the state space entirely.
- **Next step**: Debug the elision pipeline (`_run_elision_pipeline`). The abstract or concrete elision pass is likely classifying the timer tags as scan-local/elidable when they shouldn't be, because T0.Acc is used in a downstream rung condition. Check `_pass_abstract` and `_pass_concrete_batch` in `src/pyrung/core/analysis/prove/elision/`.
- Also check `_classify_dimensions` — B0 might be getting classified as combinational (since it's only written via OTE), and T0_Done/T0_Acc might be getting elided as a consequence.

## Bug 2: Compiled kernel tag materialization (`test_indirect_copy_tag_materialization`)

**Status: Root cause CONFIRMED, fix clear.**

- `CompiledPLC.__init__` at `compiled_plc.py:190-193` seeds initial state but **excludes block element names**:
  ```python
  seed = {t.name: t.default for t in self._known_tags_by_name.values()
          if t.name not in self._state.tags and t.name not in self._block_element_names}
  ```
- `PLC.__init__` at `runner.py:564-571` seeds ALL known tags including block elements.
- `DS1` is in `_known_tags_by_name` (it's in `referenced_tags`) AND in `_block_element_names`, so it gets excluded. DS2/DS3 are in `_block_element_names` but NOT in `_known_tags_by_name`, so they're unaffected.
- **Fix**: Remove the `and t.name not in self._block_element_names` exclusion. Since `_known_tags_by_name` already only contains individually-referenced tags, this is safe. Un-referenced block elements (DS2, DS3) won't be seeded because they're not in `_known_tags_by_name`.
- After seeding, `_live_block_tags` (line 197-199) will automatically pick up DS1 from `self._state.tags`, so `_committed_tags()` will include it.

## Files touched so far (read only, no edits yet)

- `src/pyrung/core/analysis/prove/absorb.py` — absorption logic
- `src/pyrung/core/analysis/prove/elision/` — abstract.py, concrete.py, __init__.py
- `src/pyrung/core/compiled_plc.py` — CompiledPLC init
- `src/pyrung/core/runner.py` — PLC init, tag seeding
- `src/pyrung/circuitpy/codegen/compile/_primitives.py` — guarded instruction codegen
- `src/pyrung/circuitpy/codegen/compile/_instructions_basic.py` — copy instruction codegen
- `tests/fuzz/test_soundness.py`, `tests/fuzz/test_parity.py` — reproducers

## Prior related work

- Commit `d510833` fixed subroutine tag seeding in interpreted PLC (similar pattern to Bug 2).
