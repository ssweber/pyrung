# Plan: User-guided state-space decomposition (`grouped_inputs` + `split_at`)

## Context

The prover's automatic optimizations (concrete elision, threshold absorption, hidden-event fast-forward) handle most Click programs today. For larger programs with independent subsystems or sequential phases, the combined state space can hit `Intractable` despite most of it being a meaningless cross-product. Two new `prove()` parameters let the user decompose the state space manually when automatic machinery isn't enough.

`grouped_inputs` is an **under-approximation** (cross-group interactions not explored — conditional proof). `split_at` is an **over-approximation** (all domain values explored, not just reachable — sound for `Proven`, possibly spurious for `Counterexample`).

---

## 1. Add `assumptions` field to `Proven`

**File:** `src/pyrung/core/analysis/prove/results.py`

Add `assumptions: tuple[str, ...] = ()` to `Proven`, between `caveats` and `journal`. Semantically distinct from `caveats` — assumptions are user-declared premises the proof depends on; caveats are prover-generated coverage notes.

```python
@dataclass(frozen=True)
class Proven:
    states_explored: int
    caveats: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()     # NEW
    journal: Journal | None = None
    aggressive_counterexample: Counterexample | None = None
```

No changes to `Counterexample` — spurious traces from `split_at` are communicated via `caveats`.

---

## 2. `split_at` — sequential decomposition

### 2a. Add parameter to `prove()` and `_build_explore_context()`

**File:** `src/pyrung/core/analysis/prove/__init__.py`

- `prove()` signature: add `split_at: list[str] | None = None`
- Pass through to `_build_explore_context()` and into `_PassContext`
- Post-BFS: if `split_at` active and result is `Counterexample`, append caveat: `"split_at=[...]: counterexample may exercise unreachable values of split tags"`

### 2b. Add `split_at` to `_PassContext`

**File:** `src/pyrung/core/analysis/prove/passes.py`

- Add field `split_at_tags: dict[str, tuple[Any, ...]] | None = None` to `_PassContext` (tag name → domain, populated during validation)
- Validation in `prove()` before pipeline runs:
  - Each split tag must exist in the program
  - Each split tag must be stateful (written by program logic, not an external input)
  - Reject if tag appears in any `rise()`/`fall()` atom (edge tracking breaks for ND tags)
  - Domain: Bool → `(False, True)`, Done-paired → `(False, PENDING, True)`, `choices=` → enumerate choice keys. No min/max ranges — error with `ValueError` if tag has no small domain

### 2c. New pipeline pass: `apply_split_at`

**File:** `src/pyrung/core/analysis/prove/passes.py`

Insert after `pilot_sweep`, before `diagnose_unwritten_tags`:

```python
def _pass_apply_split_at(ctx: _PassContext) -> None:
    if ctx.split_at_tags is None:
        return
    for tag_name, domain in ctx.split_at_tags.items():
        if tag_name in ctx.stateful_dims:
            del ctx.stateful_dims[tag_name]
            ctx.nondeterministic_dims[tag_name] = domain
            # journal: record as "split_at: user-directed decomposition"
```

Add to `_DEFAULT_PRE_BFS_PASSES` with `requires=frozenset({"classification"})`. Also add to `_unoptimized_passes()` — this is a user directive, NOT an optimization.

**Why this location:** After classification (so `stateful_dims` is populated) but before elision and compile_kernel (so downstream passes see the reduced state space and `stateful_names` excludes split tags).

### 2d. How split tags flow through the pipeline

After `apply_split_at`:
1. Split tags are in `nondeterministic_dims` with their full domain
2. `elide_scan_local_state` sees fewer stateful dims — upstream accumulators that only feed split tags may become elidable
3. `compile_kernel` derives `stateful_names` from `stateful_dims` — split tags excluded
4. In `freeze()`, `_partition_edge_bearing_inputs` classifies split tags as `free` (no rise/fall atoms) — they enter `free_input_names`
5. BFS: `_iter_input_assignments()` includes free inputs in cartesian product. Each scan starts with the split tag set to an ND value. The kernel executes (writing its own value), but the written value is NOT in the state key (free). Next dequeue re-enumerates all domain values.

**No changes needed** in `bfs.py`, `inputs.py`, or `kernel.py` — existing free-input machinery handles split tags.

---

## 3. `grouped_inputs` — parallel decomposition

### 3a. Orchestration in `prove()`

**File:** `src/pyrung/core/analysis/prove/__init__.py`

Add `grouped_inputs: list[list[str]] | None = None` parameter to `prove()`.

When set, dispatch to `_prove_grouped()` instead of normal path:

```python
def _prove_grouped(program, compiled_properties, is_batch, *,
                   grouped_inputs, scope, depth_budget, max_states,
                   joint_inputs, exclusive_inputs, settled, paced,
                   split_at, _skip_optimizations, journal):
    # Validate
    _validate_grouped_inputs(grouped_inputs, exclusive_inputs, joint_inputs,
                             program)
    
    group_results = []
    for group in grouped_inputs:
        group_set = frozenset(group)
        # Filter constraints to within-group
        group_exclusive = tuple(e for e in exclusive_inputs
                               if all(m in group_set for m in e))
        group_joint = tuple(j for j in joint_inputs
                           if all(m in group_set for m in j))
        # Recursive prove() call without grouped_inputs
        result = prove(program, *properties,
                      scope=scope, depth_budget=depth_budget,
                      max_states=max_states,
                      joint_inputs=group_joint,
                      exclusive_inputs=group_exclusive,
                      settled=settled, paced=paced,
                      split_at=split_at,
                      _skip_optimizations=_skip_optimizations,
                      journal=journal,
                      _active_input_group=group_set)  # internal param
    
    return _merge_grouped_results(group_results, grouped_inputs)
```

### 3b. Input filtering via `_active_input_group`

**File:** `src/pyrung/core/analysis/prove/passes.py`

Add `active_input_group: frozenset[str] | None = None` to `_PassContext`.

In `freeze()`, after computing `nondeterministic_dims`, filter:
```python
if self.active_input_group is not None:
    self.nondeterministic_dims = {
        k: v for k, v in self.nondeterministic_dims.items()
        if k in self.active_input_group
    }
```

This goes BEFORE the edge-bearing/free partition. Non-group ND inputs simply aren't in the dict, so they stay at default values and are never flipped.

### 3c. Validation

```python
def _validate_grouped_inputs(grouped_inputs, exclusive_inputs, joint_inputs, program):
    all_members = set()
    for group in grouped_inputs:
        for name in group:
            if name in all_members:
                raise ValueError(f"Input {name!r} appears in multiple groups")
            all_members.add(name)
    # Warn about cross-group exclusive/joint references
    for constraint in exclusive_inputs:
        groups_touched = {i for i, g in enumerate(grouped_inputs)
                         if any(m in g for m in constraint)}
        if len(groups_touched) > 1:
            warnings.warn(f"exclusive_inputs {constraint} spans multiple groups — redundant")
    # Same for joint_inputs
```

Ungrouped ND inputs (not in any group) are held at defaults — never flipped. Emit a caveat listing them so the user knows what was excluded.

### 3d. Result merging

```python
def _merge_grouped_results(group_results, grouped_inputs):
    for r in group_results:
        if isinstance(r, Counterexample):
            return r
        if isinstance(r, Intractable):
            return r
    # All Proven
    total_states = sum(r.states_explored for r in group_results)
    merged_caveats = tuple(c for r in group_results for c in r.caveats)
    groups_str = ", ".join(str(g) for g in grouped_inputs)
    assumption = f"grouped_inputs: cross-group interactions not explored. Groups: {groups_str}"
    return Proven(
        states_explored=total_states,
        caveats=merged_caveats,
        assumptions=(assumption,),
    )
```

---

## 4. Composition

Both parameters can be active simultaneously. `split_at` runs in the pipeline (moving tags from stateful → ND). `grouped_inputs` runs at orchestration level (separate `prove()` calls per group). If a split tag isn't in any input group, it stays in ND for all groups — correct behavior.

---

## 5. Files to modify

| File | Change |
|------|--------|
| `src/pyrung/core/analysis/prove/results.py` | Add `assumptions` to `Proven` |
| `src/pyrung/core/analysis/prove/__init__.py` | Add params to `prove()`, `_build_explore_context()`, add `_prove_grouped()`, `_validate_grouped_inputs()`, `_merge_grouped_results()`, `_validate_split_at()` |
| `src/pyrung/core/analysis/prove/passes.py` | Add `split_at_tags`, `active_input_group` to `_PassContext`, add `_pass_apply_split_at` pass, filter ND in `freeze()`, add pass to `_DEFAULT_PRE_BFS_PASSES` |

Files NOT modified: `bfs.py`, `inputs.py`, `kernel.py`, `events.py` — existing machinery handles both features.

---

## 6. Tests

**New file:** `tests/core/analysis/test_prove_decomposition.py`

### `split_at` tests
- Bool split tag makes previously-intractable program provable
- Int split tag with `choices=` explores all choice keys
- Split tag with unbounded domain (no choices, not Bool/Done) raises `ValueError`
- Split tag that is an external input: skip with warning
- Split tag with rise()/fall() usage: `ValueError`
- Counterexample includes spurious caveat text
- Proven result has no assumptions (sound over-approximation)
- Upstream accumulators elidable after split (state space shrinks)
- Agreement oracle: `split_at` honored under `_skip_optimizations=True`

### `grouped_inputs` tests
- Two independent subsystems proven separately
- Cross-group counterexample not found (under-approximation by design)
- `assumptions` field populated on merged Proven
- Cross-group exclusive_inputs generates warning
- Input in multiple groups raises `ValueError`
- One group Counterexample → returned immediately
- One group Intractable → returned
- Combined with `split_at`
- Combined with `paced`

---

## 7. Verification

1. `make lint` — type-check and format
2. `make test-prove` — full prover test suite (existing + new)
3. `make test-soundness` — agreement oracle with `--prove-agreement`
4. Manual: construct a two-subsystem program, verify `grouped_inputs` reduces state count vs. combined
5. Manual: construct a counter-gated program, verify `split_at` avoids intractable
