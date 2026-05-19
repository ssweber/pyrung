# Unified Event Settling with Simultaneity Groups

## 1. Current Architecture (What Exists)

Three phases, spread across five functions:

```
_settle_pending (orchestrator)
  ├─ _settle_exact_pending          phase 1: loop { _resolve_nearest_exact_hidden_event } until stable
  │    └─ _resolve_nearest_exact_hidden_event
  │         ├─ _advance_to_event_threshold    collect pending sources, advance ALL by min(scans)-1
  │         └─ _step_event_from_advance       step, fixup counters/drums, check _reset_during_event
  │
  ├─ _abstract_threshold_outcomes   phase 2: one branch per non-exact threshold spec
  │    └─ _materialize_abstract_threshold_outcome   pin threshold=acc_value, advance, step
  │
  └─ for each abstract branch:      phase 3: re-run phase 1 inside each abstract branch
       _settle_exact_pending
```

And separately, `_maybe_jump_hidden_event` uses `_advance_to_event_threshold` + `_step_event_from_advance` for jumps, then appends `_abstract_threshold_outcomes` as extra branches.

### What This Handles

- **Serial exact events**: A fires at scan 5, B at scan 10. The phase-1 loop resolves A, re-checks, resolves B. Correct.
- **Abstract threshold enables new timer**: Abstract branch materializes crossing (scans=1 via pinning), then phase 3 settles all exact events in that branch's state. New timers enabled by the crossing are visible to phase 3. Correct.
- **Convergent timers within abstract branch**: In the abstract branch, Timer A needs K scans and Timer B (just enabled) also needs K scans. Phase 3's settling loop hits them at the same `min(scans)`. Both advance, both step. Works — IF within-group interactions are handled (they aren't, see below).

### Three Gaps

**Gap 1 — Within-group interaction (simultaneity).** When `min(pending_scans)` is shared by sources A and B, `_advance_to_event_threshold` advances both, `_step_event_from_advance` steps once. If A's firing resets B's accumulator, `_reset_during_event` sees B's reversal and returns `True` → entire outcome is `None`. The valid "A fired, B got reset" branch is dropped. Root cause: `_reset_during_event` is all-or-nothing.

**Gap 2 — Multi-abstract ordering.** Abstract thresholds T1 and T2 generate independent branches from the same base state. If T1's crossing would affect T2's behavior (changes T2's delta, enable condition, or threshold tag), that interaction is missed. Each branch sees the pre-T1 / pre-T2 base state, never "T1 then T2."

**Gap 3 — Abstract threshold timing.** `_materialize_abstract_threshold_outcome` pins `threshold = acc_value`, which always gives `scans = 1`. The abstract crossing is always placed at the earliest possible scan. If the program's behavior depends on WHEN the abstract threshold fires relative to exact events (e.g., an abstract crossing at scan 5 enables a timer that converges with an exact event at scan 10, but the scan-1 materialization creates a timer that fires at scan 10 with different accumulated state), the single representative at scan 1 may miss the convergent interaction. This is the specific "timer starts later, reaches done at same time" gap.

---

## 2. New Architecture: Unified Settling

Replace the three-phase approach with a single recursive function that processes ALL event types — exact done, exact threshold, abstract threshold — in temporal order.

### Core Idea

Compute scan-to-crossing for every pending source (exact and abstract). Partition into simultaneity groups by scan count. Process the nearest group — which may contain a mix of exact and abstract sources. Exact sources resolve deterministically; abstract sources branch. For each resulting state, recurse to process remaining groups. The recursion terminates when no pending sources remain.

```
_settle_unified (new, replaces _settle_exact_pending + _abstract_threshold_outcomes)
  ├─ _collect_all_pending_sources      exact done + exact threshold + abstract threshold
  ├─ _partition_pending_sources        group by scan count
  ├─ _resolve_group                    handle nearest group (may branch for abstract sources)
  │    ├─ _advance_group_to_threshold  advance accumulators for this group
  │    ├─ _step_event_from_advance     step, fixup, per-source reset detection
  │    └─ _materialize_abstract_in_group  pin abstract thresholds in the group
  └─ recurse on each branch outcome
```

`_settle_pending` becomes thin: cache lookup, call `_settle_unified`, cache store, restore kernel.

`_maybe_jump_hidden_event` replaces its `_advance_to_event_threshold` + `_abstract_threshold_outcomes` pair with: advance to nearest group, edge-variant expansion on the stepping scan, then `_settle_unified` on each variant's post-step state.

---

## 3. Detailed Function Specifications

### 3.1 `_collect_all_pending_sources`

```python
def _collect_all_pending_sources(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    key: tuple[Any, ...],
) -> dict[tuple[str, str], _PendingSource]:
```

New dataclass:
```python
@dataclass(frozen=True)
class _PendingSource:
    kind: str
    acc_name: str
    scans: int
    is_abstract: bool  # True for non-exact threshold specs
    spec_index: int    # index into done_event_specs or threshold_event_specs
```

Logic — same scanning as current `_advance_to_event_threshold`, but also includes abstract thresholds:

```
for spec in context.done_event_specs:
    if key[spec.state_index] != PENDING: continue
    scans = _scans_until_done_event(spec.kind, spec.preset, ..., before_snap, kernel)
    if scans is not None:
        sources[(spec.kind, spec.acc_name)] = _PendingSource(
            kind=spec.kind, acc_name=spec.acc_name, scans=scans,
            is_abstract=False, spec_index=i,
        )

for spec in context.threshold_event_specs:
    vector = key[vector_offset + spec.vector_index]
    if vector[spec.atom_index]: continue  # already crossed

    if spec.mode == _THRESHOLD_MODE_EXACT:
        scans = _scans_until_threshold_event(spec, before_snap, kernel)
        if scans is not None:
            sources[(spec.kind, spec.acc_name)] = _PendingSource(
                kind=spec.kind, acc_name=spec.acc_name, scans=scans,
                is_abstract=False, spec_index=j,
            )
    else:
        # Abstract: compute scans WITHOUT pinning.
        # Use current threshold tag value if numeric; otherwise skip.
        scans = _scans_until_threshold_event(spec, before_snap, kernel)
        if scans is not None:
            sources[(spec.kind, spec.acc_name)] = _PendingSource(
                kind=spec.kind, acc_name=spec.acc_name, scans=scans,
                is_abstract=True, spec_index=j,
            )
```

**Key decision for abstract thresholds**: compute scans using the threshold tag's current value (whatever it is). If the tag is numeric, we get a concrete scan count and the abstract spec participates in the partition at its natural position. If non-numeric (symbolic, unset), `_scans_until_threshold_event` returns `None` and the spec falls through to the fallback (see §3.5).

When the tag IS numeric, the abstract threshold's scan count reflects "when would this accumulator cross the current tag value?" — which is the most natural representative for the partition. This replaces the pin-to-acc-value approach, which always gave scans=1.

When the tag is NOT numeric (the common abstract case — tag is a variable name, not a number), the scan count is unknowable and the spec must be handled separately (§3.5).

### 3.2 `_partition_pending_sources`

```python
def _partition_pending_sources(
    sources: dict[tuple[str, str], _PendingSource],
) -> tuple[_SimultaneityGroup, ...]:
```

New dataclass:
```python
@dataclass(frozen=True)
class _SimultaneityGroup:
    scans: int
    exact_sources: frozenset[tuple[str, str]]      # (kind, acc_name) pairs
    abstract_sources: frozenset[tuple[str, str]]    # (kind, acc_name) pairs
```

Pure function. Groups sources by `scans`, sorts ascending, splits each group into exact vs abstract members.

### 3.3 `_EventAdvanceState` — Extended

```python
@dataclass(frozen=True)
class _EventAdvanceState:
    pre_event_snapshot: _KernelSnapshot
    before_snap: _KernelSnapshot
    pre_advance_counter_acc: dict[str, int]
    pending_sources: set[tuple[str, str]]       # ALL sources advanced (not just firing group)
    next_event_scans: int
    firing_group: _SimultaneityGroup            # NEW: which group is being resolved
```

### 3.4 `_advance_group_to_threshold`

Replaces `_advance_to_event_threshold`. Same logic, but takes the full partition and uses the first group's `scans` as `next_event_scans`.

```python
def _advance_group_to_threshold(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    all_sources: dict[tuple[str, str], _PendingSource],
    groups: tuple[_SimultaneityGroup, ...],
) -> _EventAdvanceState | None:
```

**Important: this function does NOT pin abstract thresholds.** Pinning is the caller's responsibility and varies by group type (see §3.5). This function only advances accumulators.

Logic:
```
if not groups: return None
firing_group = groups[0]
next_event_scans = firing_group.scans
skipped_scans = max(next_event_scans - 1, 0)

pre_advance_counter_acc = {}
for (kind, acc_name) in all_sources:        # advance ALL, not just firing group
    if kind in {COUNT_UP, COUNT_DOWN, TIME_DRUM}:
        pre_advance_counter_acc[acc_name] = int(kernel.tags.get(acc_name, 0) or 0)
    _advance_hidden_progress(kind, acc_name, skipped_scans, before_snap, kernel)

return _EventAdvanceState(
    pre_event_snapshot=_snapshot_kernel(kernel),
    before_snap=before_snap,
    pre_advance_counter_acc=pre_advance_counter_acc,
    pending_sources=set(all_sources),
    next_event_scans=next_event_scans,
    firing_group=firing_group,
)
```

New helper `_pin_abstract_threshold`: finds the `_ThresholdEventSpec` for the given `(kind, acc_name)`, sets `kernel.tags[spec.threshold] = kernel.tags.get(spec.acc_name)`. Same logic as lines 786-791 of `_materialize_abstract_threshold_outcome`, extracted. Called by `_settle_unified` (not by this function).

**Why advance ALL sources, not just the firing group**: non-firing sources get partially advanced proportionally. When the recursion processes the next group, it needs correct accumulator values. The `before_snap` passed to the recursion will be the `pre_event_snapshot` from this advance, and the delta computation (`acc_after - acc_before`) will use the partially-advanced values. This matches current behavior.

### 3.5 `_settle_unified`

The core recursive function.

```python
def _settle_unified(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    edge_comp: _EdgeCompressor,
    total_additional_scans: int = 0,
    accumulated_caveats: tuple[str, ...] = (),
    depth: int = 0,
    max_depth: int | None = None,    # defaults to event_count + 1 at top-level call
) -> list[_HiddenEventOutcome]:
```

Logic:
```
if max_depth is None:
    max_depth = len(context.done_event_specs) + sum(
        1 for s in context.threshold_event_specs if s.mode == _THRESHOLD_MODE_EXACT
    ) + len([s for s in context.threshold_event_specs if s.mode != _THRESHOLD_MODE_EXACT]) + 1

if depth >= max_depth:
    # Reached recursion limit — return current state as terminal
    return [_HiddenEventOutcome(
        snapshot=_snapshot_kernel(kernel),
        key=edge_comp.state_key(kernel),
        additional_scans=total_additional_scans,
        caveats=accumulated_caveats,
    )]

key = edge_comp.state_key(kernel)
all_sources = _collect_all_pending_sources(context, kernel, before_snap, key)
groups = _partition_pending_sources(all_sources)

# ── Handle unresolvable abstract specs (non-numeric threshold tag) ──
# These couldn't join the partition. Generate branches for them separately,
# using the existing _materialize_abstract_threshold_outcome logic.
# Each such branch recurses independently.
unresolvable_abstract_outcomes = _materialize_unresolvable_abstracts(
    context, kernel, before_snap, key, edge_comp, all_sources,
)

if not groups and not unresolvable_abstract_outcomes:
    # Nothing pending — current state is terminal
    return [_HiddenEventOutcome(
        snapshot=_snapshot_kernel(kernel),
        key=edge_comp.state_key(kernel),
        additional_scans=total_additional_scans,
        caveats=accumulated_caveats,
    )]

outcomes: list[_HiddenEventOutcome] = []
seen_keys: set[tuple[Any, ...]] = set()
base_snap = _snapshot_kernel(kernel)

# ── Process the nearest group ──
if groups:
    advance = _advance_group_to_threshold(context, kernel, before_snap, all_sources, groups)
    if advance is not None:
        group = advance.firing_group

        if not group.abstract_sources:
            # ── Pure exact group: one deterministic outcome ──
            outcome = _step_event_from_advance(context, kernel, advance, edge_comp)
            if outcome is not None:
                # Recurse to settle remaining groups
                sub_outcomes = _settle_unified(
                    context, kernel,
                    before_snap=outcome.pre_event_snapshot,
                    edge_comp=edge_comp,
                    total_additional_scans=total_additional_scans + outcome.additional_scans,
                    accumulated_caveats=_merge_caveats(accumulated_caveats, outcome.caveats),
                    depth=depth + 1,
                    max_depth=max_depth,
                )
                for sub in sub_outcomes:
                    if sub.key not in seen_keys:
                        seen_keys.add(sub.key)
                        outcomes.append(sub)

        elif not group.exact_sources:
            # ── Pure abstract group: one branch per abstract source ──
            for source_key in group.abstract_sources:
                _restore_kernel(kernel, advance.pre_event_snapshot)
                outcome = _step_event_from_advance(context, kernel, advance, edge_comp)
                if outcome is None:
                    continue
                # Recurse to settle remaining groups in this branch
                sub_outcomes = _settle_unified(
                    context, kernel,
                    before_snap=outcome.pre_event_snapshot,
                    edge_comp=edge_comp,
                    total_additional_scans=total_additional_scans + outcome.additional_scans,
                    accumulated_caveats=_merge_caveats(
                        accumulated_caveats, outcome.caveats, _ABSTRACT_THRESHOLD_TRACE_CAVEAT,
                    ),
                    depth=depth + 1,
                    max_depth=max_depth,
                )
                for sub in sub_outcomes:
                    if sub.key not in seen_keys:
                        seen_keys.add(sub.key)
                        outcomes.append(sub)

        else:
            # ── Mixed group: exact + abstract in same group ──
            # The advance already pinned abstract thresholds and advanced
            # all accumulators. Step once — both exact and abstract sources
            # resolve in the same step. Abstract sources add caveats.
            outcome = _step_event_from_advance(context, kernel, advance, edge_comp)
            if outcome is not None:
                sub_outcomes = _settle_unified(
                    context, kernel,
                    before_snap=outcome.pre_event_snapshot,
                    edge_comp=edge_comp,
                    total_additional_scans=total_additional_scans + outcome.additional_scans,
                    accumulated_caveats=_merge_caveats(
                        accumulated_caveats, outcome.caveats, _ABSTRACT_THRESHOLD_TRACE_CAVEAT,
                    ),
                    depth=depth + 1,
                    max_depth=max_depth,
                )
                for sub in sub_outcomes:
                    if sub.key not in seen_keys:
                        seen_keys.add(sub.key)
                        outcomes.append(sub)

    _restore_kernel(kernel, base_snap)

# ── Process unresolvable abstract specs ──
# These are abstract thresholds whose tag is non-numeric, so they couldn't
# join the partition. They get the existing pin-to-acc materialization.
for ua_outcome in unresolvable_abstract_outcomes:
    _restore_kernel(kernel, ua_outcome.snapshot)
    sub_outcomes = _settle_unified(
        context, kernel,
        before_snap=ua_outcome.pre_event_snapshot or before_snap,
        edge_comp=edge_comp,
        total_additional_scans=total_additional_scans + ua_outcome.additional_scans,
        accumulated_caveats=_merge_caveats(accumulated_caveats, ua_outcome.caveats),
        depth=depth + 1,
        max_depth=max_depth,
    )
    for sub in sub_outcomes:
        if sub.key not in seen_keys:
            seen_keys.add(sub.key)
            outcomes.append(sub)

_restore_kernel(kernel, base_snap)
return outcomes
```

#### Pinning strategy by group type

`_advance_group_to_threshold` does NOT pin abstract thresholds — it only advances accumulators. Pinning is done by `_settle_unified` after the advance, and the strategy depends on the group composition:

```
_advance_group_to_threshold:  advance all accumulators by skipped_scans (NO pinning)
  → returns _EventAdvanceState with pre_event_snapshot

For the group:
  If pure exact: step once (no pinning needed)
  If pure abstract: for each spec, restore to pre_event_snapshot, pin that spec, step, recurse
  If mixed: pin all abstract specs in group, step once, recurse
    (mixed = exact sources MUST fire on this step anyway; abstract sources ride along)
```

**Pure abstract groups** — When a group contains multiple abstract sources, each could independently be the "reason" the group fires. The current `_abstract_threshold_outcomes` generates one branch per spec. The unified approach does the same: for each abstract source in the group, restore to `advance.pre_event_snapshot`, pin only THAT source's threshold, step, and recurse. Each branch sees the same pre-advance state with only one threshold pinned.

**Mixed groups** — All abstract thresholds in the group are pinned simultaneously because they share the same scan count as the exact sources. The exact sources determine the timing and MUST fire on this step. Abstract sources ride along. This is the convergent-timer case: exact Timer A and abstract threshold T both resolve at scan 10.

### 3.6 `_materialize_unresolvable_abstracts`

Handles abstract threshold specs whose threshold tag is non-numeric (can't compute scan count, can't join the partition).

```python
def _materialize_unresolvable_abstracts(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    key: tuple[Any, ...],
    edge_comp: _EdgeCompressor,
    resolved_sources: dict[tuple[str, str], _PendingSource],
) -> list[_HiddenEventOutcome]:
```

Logic: iterate `context.threshold_event_specs` where `mode != EXACT`, skip any that are in `resolved_sources` (they joined the partition), call `_materialize_abstract_threshold_outcome` for each remaining one. This is the same as current `_abstract_threshold_outcomes` minus the specs that got a numeric scan count.

### 3.7 `_detect_resets_in_group`

Replaces `_reset_during_event`. Returns which sources in the firing group got reset, instead of bool.

```python
def _detect_resets_in_group(
    context: _ExploreContext,
    pre_event_snapshot: _KernelSnapshot,
    kernel: ReplayKernel,
    firing_group: _SimultaneityGroup,
) -> frozenset[tuple[str, str]]:
```

Logic:
```
reset_sources: set[tuple[str, str]] = set()
group_members = firing_group.exact_sources | firing_group.abstract_sources

for spec in context.done_event_specs:
    if spec.kind == _DONE_KIND_TIME_DRUM: continue
    source_key = (spec.kind, spec.acc_name)
    if source_key not in group_members: continue

    pre_acc = int(pre_event_snapshot.tags.get(spec.acc_name, 0) or 0)
    post_acc = int(kernel.tags.get(spec.acc_name, 0) or 0)

    if spec.kind == _DONE_KIND_COUNT_DOWN:
        reversed_ = post_acc > pre_acc
    else:
        reversed_ = post_acc < pre_acc

    if reversed_:
        done_name = context.stateful_names[spec.state_index]
        if not kernel.tags.get(done_name):
            # Accumulator reversed AND Done didn't fire = external reset
            reset_sources.add(source_key)
        # If Done DID fire despite reversal = self-resetting pattern, NOT a reset

return frozenset(reset_sources)
```

### 3.8 `_step_event_from_advance` — Modified

```python
def _step_event_from_advance(
    context: _ExploreContext,
    kernel: ReplayKernel,
    advance: _EventAdvanceState,
    edge_comp: _EdgeCompressor,
) -> _HiddenEventOutcome | None:
```

New logic:
```
_step_kernel(context, kernel)
_fixup_unfired_counters(context, advance.before_snap, advance.pre_advance_counter_acc,
                         advance.pre_event_snapshot, kernel)
_fixup_unfired_drums(context, advance.before_snap, advance.pre_advance_counter_acc,
                      advance.pre_event_snapshot, kernel)

reset_set = _detect_resets_in_group(context, advance.pre_event_snapshot, kernel, advance.firing_group)
all_group_sources = advance.firing_group.exact_sources | advance.firing_group.abstract_sources

if reset_set == all_group_sources:
    # Every source in the group got reset — total invalidation
    return None

# Partial or no resets: outcome is valid.
# Sources that fired are in their correct post-fire state.
# Sources that got reset are in their correct post-reset state.
# Both are what the kernel computed deterministically.

return _HiddenEventOutcome(
    snapshot=_snapshot_kernel(kernel),
    key=edge_comp.state_key(kernel),
    additional_scans=advance.next_event_scans,
    pre_event_snapshot=advance.pre_event_snapshot,
)
```

### 3.9 `_settle_pending` — Simplified

```python
def _settle_pending(
    context: _ExploreContext,
    kernel: ReplayKernel,
    before_snap: _KernelSnapshot,
    edge_comp: _EdgeCompressor,
    cache: _HiddenEventCache | None = None,
) -> list[_HiddenEventOutcome]:
```

New body:
```
key = edge_comp.state_key(kernel)
cache_key = cache.plateau_key(...) if cache else None
# ... cache lookup (unchanged) ...

base_snap = _snapshot_kernel(kernel)
outcomes = _settle_unified(context, kernel, before_snap, edge_comp)
_restore_kernel(kernel, base_snap)

# ... cache store (unchanged) ...
return outcomes
```

All orchestration logic (phase 1 → phase 2 → phase 3) is gone. `_settle_unified` handles everything.

### 3.10 `_maybe_jump_hidden_event` — Updated

The structure stays the same. Changes:

1. Replace `_advance_to_event_threshold` with `_advance_group_to_threshold` (using `_collect_all_pending_sources` + `_partition_pending_sources`).

2. After the edge-variant expansion loop produces post-step outcomes, each outcome gets fed to `_settle_unified` to resolve remaining groups:

```python
# Current: outcome from _step_event_from_advance is terminal
# New: outcome from _step_event_from_advance feeds into _settle_unified

outcome = _step_event_from_advance(context, kernel, advance, edge_comp)
if outcome is not None:
    # Settle remaining events from the post-step state
    _restore_kernel(kernel, outcome.snapshot)
    settled = _settle_unified(
        context, kernel,
        before_snap=outcome.pre_event_snapshot,
        edge_comp=edge_comp,
        total_additional_scans=outcome.additional_scans,
    )
    for s in settled:
        if s.key not in seen_keys:
            seen_keys.add(s.key)
            outcomes.append(_HiddenEventOutcome(
                snapshot=s.snapshot,
                key=s.key,
                additional_scans=s.additional_scans,
                pre_event_snapshot=s.pre_event_snapshot or outcome.pre_event_snapshot,
                caveats=_merge_caveats(s.caveats, variant_caveats),
                event_inputs=variant_inputs if any_variant else None,
            ))
```

3. Remove the separate `_abstract_threshold_outcomes` call at the end (lines 987-991 of current code). The unified settling already handles abstract branches.

---

## 4. Functions Removed

| Function | Reason |
|---|---|
| `_settle_exact_pending` | Absorbed into `_settle_unified` |
| `_abstract_threshold_outcomes` | Absorbed into `_settle_unified` + `_materialize_unresolvable_abstracts` |
| `_resolve_nearest_exact_hidden_event` | Was glue between `_advance_to_event_threshold` + `_step_event_from_advance`; no longer needed |
| `_advance_to_event_threshold` | Replaced by `_advance_group_to_threshold` |
| `_reset_during_event` | Replaced by `_detect_resets_in_group` |

## 5. Functions Kept Unchanged

| Function | Why |
|---|---|
| `_scans_until_done_event` | Still the primitive for computing scan counts |
| `_scans_until_threshold_event` | Still the primitive for threshold scan counts |
| `_advance_hidden_progress` | Still the primitive for fast-forwarding accumulators |
| `_fixup_unfired_counters` | Per-source fixup logic is already correct (see §6) |
| `_fixup_unfired_drums` | Same |
| `_materialize_abstract_threshold_outcome` | Used by `_materialize_unresolvable_abstracts` for non-numeric threshold tags |
| `_has_pending_done` | Boolean check on state key, used by BFS loop |
| `_has_pending_hidden_event` | Same |
| `_hidden_progress_signature` | Used by cache key |
| `_HiddenEventCache.plateau_key` | Cache key computation (see §7) |
| `_timer_total` | Utility |
| `_resolve_done_preset` | Utility |
| `_progress_delta_and_current` | Utility |
| `_reset_during_event` | Keep temporarily during rollout; `_detect_resets_in_group` replaces it but existing tests may reference it |

## 6. Edge Cases

### 6.1 Within-group: A fires, resets B (same scan count)

`_detect_resets_in_group` returns `{B}`. Since `{B} ≠ firing_group`, outcome is valid. A is done, B is in post-reset state. Recursion re-evaluates B as newly pending (or not, if the reset cleared it entirely).

### 6.2 Within-group: mutual reset (A resets B, B resets A)

`_detect_resets_in_group` returns `{A, B} == firing_group`. Outcome is `None`. This state is unreachable via fast-forward — the mutual reset means the linear extrapolation assumption is wrong. The BFS will explore this region via concrete scanning instead.

### 6.3 Fixup functions on reset sources

`_fixup_unfired_counters` checks `pre_acc != post_acc` to detect if a counter fired during the step. A reset source's accumulator changed (went backwards), so `pre_acc != post_acc` is True → fixup skips it. Correct: the kernel already set the accumulator to its post-reset value.

`_fixup_unfired_drums` uses the same check. Same reasoning applies.

No changes needed in either function.

### 6.4 Threshold spec in firing group — threshold tag changed by another source

An exact threshold spec is in the firing group. Another source in the same group fires and changes the threshold tag's value. `_threshold_crossed` checks the post-step state, which has the updated tag. If the threshold is no longer crossed (because the tag moved), the spec didn't fire — but it's not "reset" either. `_detect_resets_in_group` only checks Done-spec accumulators, not threshold vectors. The uncrossed threshold will be re-evaluated when `_settle_unified` recurses and calls `_collect_all_pending_sources` with the new state.

### 6.5 Self-resetting timer re-arms

Timer A fires, resets itself, goes back to PENDING. `_detect_resets_in_group` sees the reversal, but Done DID fire → not counted as reset (self-resetting pattern carve-out). Outcome is valid. Recursion re-evaluates: A appears as pending again with fresh delta. `max_depth` bound prevents infinite re-resolution.

### 6.6 Timer fires, starts new timer — convergent case

Timer A fires at scan 10. A's Done enables Timer B with preset 5. In the next recursion: B is PENDING with 5 scans. Any other pending exact source C with 5 remaining scans joins B's group. Convergence detected naturally by recomputation.

### 6.7 Abstract threshold enables timer — convergent case

Abstract threshold T has a numeric threshold tag value, computes to scans=7. Exact Timer A also has scans=7. Same group. `_advance_group_to_threshold` advances both by 6, pins T's threshold to acc value. Step: both T and A should cross. If T's crossing enables Timer B, B is picked up in the next recursion. If B would also fire at the same scan, it was already PENDING and already in the group (or it starts post-step and gets picked up).

### 6.8 Abstract threshold — non-numeric tag (unresolvable)

`_collect_all_pending_sources` calls `_scans_until_threshold_event` which calls `_threshold_value`. If the tag value isn't numeric, returns `None`. The spec doesn't enter `all_sources`. `_materialize_unresolvable_abstracts` catches it and uses the existing pin-to-acc materialization. The resulting branch recurses through `_settle_unified` to resolve any exact events behind it.

### 6.9 Multiple abstract thresholds with ordering dependency (T1 → T2)

T1 and T2 both have numeric threshold tags. T1 computes to scans=5, T2 to scans=12. Different groups. `_settle_unified` resolves T1's group first. If T1's crossing changes T2's threshold tag, the recursion recomputes T2's scan count with the updated tag. T2 may move to a different group, or become unreachable, or converge with another source. All handled by recomputation.

If T1 and T2 have the same scan count: same group. For pure-abstract groups, each spec generates its own branch (§3.5 flow). For mixed groups, all abstract specs in the group are pinned simultaneously. The branching stays bounded by spec count.

### 6.10 Edge-input variants in `_maybe_jump_hidden_event`

Each `(combo, prev_combo)` pair produces a different kernel state before stepping. Different pairs may trigger different within-group interactions (A resets B with one set of edges, not with another). Each pair gets its own `_step_event_from_advance` → own `_detect_resets_in_group` → own recursion through `_settle_unified`. Correct: variant expansion stays at the outermost level.

### 6.11 Accumulated `before_snap` across recursion levels

Each recursion level receives the `pre_event_snapshot` from the previous level's step as its `before_snap`. This is correct for delta computation: `_scans_until_done_event` uses `before` and `kernel` to compute `acc_after - acc_before`. After a step, `pre_event_snapshot` is the state just before that step → one scan before the current kernel state. The delta is preserved correctly across recursion.

### 6.12 `pre_advance_counter_acc` across recursion levels

Each recursion level creates a fresh `_EventAdvanceState` via `_advance_group_to_threshold`, which captures `pre_advance_counter_acc` for that level's advance. The fixup functions use this level-local value. No cross-level leakage.

### 6.13 `pending_sources` dict key collision

`_collect_all_pending_sources` keys on `(kind, acc_name)`. Two specs can share the same accumulator (e.g., same timer driving two Done bits with different presets). The dict keeps the last one written. This matches current behavior in `_advance_to_event_threshold` where `pending_sources[(spec.kind, spec.acc_name)] = scans` also deduplicates. The shorter-scans spec should win — sort specs by scans ascending before inserting, or use `min` on collision:

```python
existing = sources.get((spec.kind, spec.acc_name))
if existing is None or scans < existing.scans:
    sources[(spec.kind, spec.acc_name)] = _PendingSource(...)
```

This ensures the earliest crossing for a given accumulator takes priority, which is correct: you want to advance to the first crossing, not a later one.

---

## 7. Cache Impact

`_HiddenEventCache.plateau_key` builds a signature from `_hidden_progress_signature` for each pending source. This captures `(kind, acc_name, before_acc, after_acc)` — which determines the per-scan delta, which determines the scan count, which determines the group partition. Two plateaus with identical progress signatures produce identical partitions.

The cache key does NOT need to explicitly include the group structure. It's a deterministic function of the existing signature components.

One addition: for abstract thresholds that joined the partition (numeric threshold tag), the threshold tag's value affects their scan count. The existing `plateau_key` already captures threshold tag values in `hidden_thresholds` for exact-mode specs. Extend this to also capture threshold tag values for non-exact specs when the tag is numeric:

```python
# In plateau_key, existing code for threshold_event_specs:
if spec.mode == _THRESHOLD_MODE_EXACT and isinstance(spec.threshold, str) ...:
    hidden_thresholds.append((spec.threshold, kernel.tags.get(spec.threshold)))

# Add: also for non-exact specs with numeric threshold tags
if spec.mode != _THRESHOLD_MODE_EXACT and isinstance(spec.threshold, str):
    val = kernel.tags.get(spec.threshold)
    if _is_numeric_literal(val) and spec.threshold not in seen_thresholds:
        seen_thresholds.add(spec.threshold)
        hidden_thresholds.append((spec.threshold, val))
```

---

## 8. Implementation Order

Each step is independently testable. Run existing tests after each step to verify no regressions.

**Key files:**
- All new functions go in `src/pyrung/core/analysis/prove/events.py`
- BFS call sites in `src/pyrung/core/analysis/prove/bfs.py` (lines 331, 356, 391, 402)
- Existing helpers already available: `_merge_caveats` (events.py:122), `_is_numeric_literal` (absorb.py), `_scans_until_threshold_event` returns `None` for non-numeric threshold tags (events.py:369)

1. **`_PendingSource` and `_SimultaneityGroup` dataclasses.** No behavior change.

2. **`_collect_all_pending_sources`** — extracts the scanning logic from `_advance_to_event_threshold` into a standalone function. Test: verify it produces the same `(kind, acc_name) → scans` mapping as the current code for exact sources. Verify it also includes abstract specs with numeric tags.

3. **`_partition_pending_sources`** — pure function, unit test in isolation. Input: dict of sources. Output: sorted tuple of groups with correct exact/abstract splits.

4. **`_detect_resets_in_group`** — new function alongside `_reset_during_event` (don't remove old one yet). Test: for single-source groups, verify it returns empty set when `_reset_during_event` returns False, and `{source}` when it returns True.

5. **Extend `_EventAdvanceState`** with `firing_group` field. Update `_advance_to_event_threshold` to populate it (use `_collect_all_pending_sources` + `_partition_pending_sources`). At this point `_advance_to_event_threshold` still works identically — the new field is just carried along.

6. **Update `_step_event_from_advance`** to use `_detect_resets_in_group` instead of `_reset_during_event`. Behavioral change: partial resets now produce valid outcomes instead of `None`. Test: construct a two-timer scenario where A resets B; verify outcome is non-None with A done and B reset. **Note:** this alone is not expected to flip `test_fuzz_time_drum_self_resetting_timer_combined_state` — that test also needs the full unified settling (step 10+) because the time drum + self-resetting timer interaction requires temporal ordering of the simultaneity groups, not just partial-reset tolerance.

7. **`_advance_group_to_threshold`** — new function that replaces `_advance_to_event_threshold`. Takes pre-computed sources and groups. Does NOT pin abstract thresholds (pinning is the caller's responsibility per §3.5). Test: verify it produces identical `_EventAdvanceState` as the old function for exact-only cases.

8. **`_materialize_unresolvable_abstracts`** — extracts the non-numeric abstract threshold logic from `_abstract_threshold_outcomes`. Test: verify it produces identical outcomes for abstract specs that can't compute scan counts.

9. **`_settle_unified`** — the recursive settling function. Start with exact-only support (no abstract branching). **Testing strategy:** add `_settle_unified` as a standalone function (not wired into the call graph yet). Write tests that call it directly on programs with only exact events and assert identical outcomes to `_settle_exact_pending`. Do not replace `_settle_pending` yet.

10. **Add abstract support to `_settle_unified`** — pure abstract groups, mixed groups, unresolvable abstracts. **Testing strategy:** same approach — call `_settle_unified` directly and assert identical outcomes to the current `_settle_pending` for the full existing test suite. Add new tests for convergent-timer cases (exact + abstract in same group). The xfail on `test_fuzz_time_drum_self_resetting_timer_combined_state` should be removed at this step — if the test still fails, the implementation has a bug.

11. **Simplify `_settle_pending`** to just cache + `_settle_unified` + restore. All existing tests should pass unchanged since the public interface is the same.

12. **Update `_maybe_jump_hidden_event`** to use `_advance_group_to_threshold` + `_settle_unified`. Remove the separate `_abstract_threshold_outcomes` call. **BFS integration note:** the BFS (`bfs.py`) has two call patterns — (a) lines 326-375: settle and jump are mutually exclusive (`elif` chain, gated on `any_unsettled`), (b) lines 376-415: both called on the same starting state, results combined into `_ev_outcomes`. Neither pattern passes jump outcomes back into `_settle_pending`, so no double-settle issue. After this step, jump outcomes are fully settled internally (via `_settle_unified` recursion), and the combined-results path still works correctly — each operation explores a different event-resolution path from the same starting state. No `bfs.py` changes needed.

13. **Update `_HiddenEventCache.plateau_key`** to include non-exact threshold tag values when numeric.

14. **Remove dead code**: `_settle_exact_pending`, `_abstract_threshold_outcomes`, `_resolve_nearest_exact_hidden_event`, `_advance_to_event_threshold`, `_reset_during_event`.

---

## 9. Invariants to Assert

These should be asserted in debug/test builds throughout `_settle_unified`:

- **Recursion depth never exceeds `max_depth`.** If it does, there's a re-arming loop that the bound didn't catch.
- **`_advance_group_to_threshold` returns `firing_group.scans >= 1`.** Scan count of 0 means the event already fired — should have been caught by the state key.
- **After each step, `edge_comp.state_key(kernel)` differs from the pre-step key OR a reset occurred.** If the key didn't change and no reset happened, the settling loop isn't making progress.
- **`all_sources` is non-empty when `groups` is non-empty.** Partition can't produce groups from nothing.
- **`seen_keys` deduplication prevents duplicate outcomes.** Same key from different branches means same reachable state — only one needs to be enqueued.
- **`base_snap` restore happens on every exit path.** The kernel must be returned to the caller in its original state.
