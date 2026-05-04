# Free Input Elision — Implementation Plan

## Context

The prove() BFS verifier includes all nondeterministic (ND) input values in the state key, even those without edge semantics. Two states differing only in "free" input values have identical successor sets — free inputs can take any value on any scan, so their current value doesn't constrain future behavior. Eliding free inputs from the state key merges these equivalent states, collapsing the BFS state space without under-approximation. Expected impact on PackML bench: ~52K states → ~4-8K (14x reduction).

The architecture already separates `nondeterministic_names` (state key membership) from `nondeterministic_dims` (enumeration domains). The entire change reduces to filtering `nondeterministic_names` at one call site.

## Edge-bearing partition

An ND input is **edge-bearing** (keep in state key) if it appears in:
1. A `rise()` or `fall()` expression atom — explicit edge
2. ShiftInstruction `clock_condition` — implicit rising edge via memory key
3. EventDrum/TimeDrum `jog_condition` or `jump_condition` — implicit rising edge via memory key
4. EventDrum per-step `events[i]` conditions — implicit edge via `_event_prev_key`

Everything else is **free** (omit from state key).

Oneshot: NOT edge-bearing. Oneshot memory keys (`_once_*`) are already in the state key via `memory_key_names`, and the BFS enumerates all ND values regardless.

## Changes

### 1. `src/pyrung/core/analysis/prove/expr.py` — add partition function

After `_collect_edge_input_tags` (line ~193), add two functions:

**`_walk_implicit_edge_inputs(program, nd_dims) -> frozenset[str]`** (~30 lines)
- Walk `program.rungs` + `program.subroutines` recursively (handle branches, ForLoopInstruction children)
- For ShiftInstruction: convert `clock_condition` → Expr via `_condition_to_expr`, collect refs via `_referenced_tags`, intersect with `nd_dims`
- For EventDrumInstruction: same for `jog_condition`, `jump_condition`, and each `events[i]`
- For TimeDrumInstruction: same for `jog_condition`, `jump_condition`

**`_partition_edge_bearing_inputs(all_exprs, nd_dims, program) -> frozenset[str]`**
- Union of `_collect_edge_input_tags(all_exprs, nd_dims)` + `_walk_implicit_edge_inputs(program, nd_dims)`

### 2. `src/pyrung/core/analysis/prove/passes.py` — filter nondeterministic_names

**In `freeze()`, line 216, change:**
```python
nondeterministic_names=tuple(sorted(self.nondeterministic_dims)),
```
**to:**
```python
nondeterministic_names=tuple(sorted(
    _partition_edge_bearing_inputs(self.all_exprs, self.nondeterministic_dims, self.program)
    | (frozenset(self.project or ()) & frozenset(self.nondeterministic_dims))
)),
```

Projected ND inputs (those in `project`) are always kept in the key — the user observes their values, so they must be distinguishable.

**Update `_detect_edge_caveats`** (line 47) to accept `program` and use `_partition_edge_bearing_inputs` instead of `_collect_edge_input_tags` directly. Update the call in `freeze()` (line 193) to pass `self.program`.

Add import: `from .expr import _partition_edge_bearing_inputs`

### 3. No changes to

- `kernel.py` — `_extract_state_key()` already iterates `nondeterministic_names`
- `_EdgeCompressor` / `_LiveInputCache` — use `nondeterministic_dims` for liveness, `nondeterministic_names` for key
- `_iter_input_assignments()` — uses `nondeterministic_dims` for enumeration
- `_bfs_explore()` — unchanged
- `elision.py` — concrete elision uses `nondeterministic_dims`, not names

### 4. Tests in `tests/core/analysis/test_prove.py`

New class `TestFreeInputElision`:

| Test | What it verifies |
|------|-----------------|
| `test_free_input_reduces_states` | Program with 2 ND bools, one free (xic only), one edge-bearing (rise). Free input not in `nondeterministic_names`. State count halved. |
| `test_shift_clock_is_edge_bearing` | ShiftInstruction clock ND input stays in names. |
| `test_drum_jog_is_edge_bearing` | Drum jog ND input stays in names. |
| `test_drum_event_is_edge_bearing` | EventDrum per-step event ND input stays in names. |
| `test_projected_free_input_kept` | Free input in `project` stays in names. |
| `test_all_edge_bearing_no_reduction` | All ND inputs use rise() → names unchanged. |
| `test_soundness_preserved` | Latch controlled by free input: same prove() result with and without elision. |

## Verification

1. `make test` — all existing tests pass (no regression from filtered names)
2. New tests in `TestFreeInputElision` pass
3. `make lint` — clean
4. Profile on PackML bench (if available): confirm state count reduction

## Soundness argument

1. Free inputs can take any value next scan — current value is not a constraint on successors
2. Edge-bearing inputs (rise/fall, shift clock, drum jog/jump/events) need prev values for correct edge detection — kept in key
3. Oneshot memory keys remain in key via `memory_key_names` — re-fire prevention preserved
4. Projected inputs remain in key — observation correctness preserved
5. Assignment enumeration unchanged — all ND values still explored at every BFS state
6. Over-approximation only (merges states that may be unreachable from each other), never under-approximation
