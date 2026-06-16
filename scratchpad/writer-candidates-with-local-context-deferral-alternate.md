# Writer Candidates With Context-Aware Deferral

## Implementation Summary (2026-06-16)

Landed as `context_aware_groups` ordering advice.

- `_writer_candidates()` now preserves per-writer `full_conditions`,
  `satisfied`, `unsatisfied`, `all_writes`, and `writer_index`, with
  `_unsatisfied_condition_groups()` kept as the compatibility projection.
- Live branch-arm context is preserved for candidate scoring/monitors even
  when `_extract_condition_values()` correctly omits non-common `Or` arms from
  prerequisites. Example: `(S_Starting | S_Unholding | S_Unsuspending) &
  Rotate__init & Blower__init` keeps `S_Starting=True` as satisfied context
  when the snapshot is in Starting, without turning sibling arms into
  conjunctive prerequisites.
- Candidate-specific monitors promote selected satisfied guards into
  `_StepMonitors` so child walks preserve the chosen writer branch until the
  governing value lands.
- Disposition ordering is structural: direct must-stay conflicts reject the
  candidate for this attempt, write-footprint overlap defers it, and active
  must-stay overlap prefers it.
- `_eval_expr_from_state()` now handles `ArithAtom` and keeps `rise`/`fall`
  unknown under snapshot-only evaluation, avoiding edge-only branch guesses.
- `_filter_full()` skips non-predecessor self conditions just like the
  unsatisfied projection, preventing spurious must-stay guards on self-writing
  tags.
- The synthetic mutual-branch fixture and live burner candidate dump confirm
  the Starting writer for `S_StateCompleteBool=1` sorts first:
  satisfied=`S_Starting=True, S_UnitModeCurrent=1`;
  unsatisfied=`Blower__init=1, Rotate__init=1`.
- Full `make test` passed after the arc.

Status caveat: full `how(y_BurnerLoop)` still exhausts at 120s. The local
writer-choice issue is fixed, but the live run currently burns budget earlier
while trying to establish `S_Starting=True`; recurring rotate-sensor threat
work remains Open Item #9.

## Context

While pursuing `how(y_BurnerLoop)`, the walker enters the `S_Starting` branch. Later, for `S_StateCompleteBool=1`, the valid semantic writer is the Starting completion path: `S_Starting + Rotate__init + Blower__init`. But `S_Starting=True` is already satisfied, so `_unsatisfied_condition_groups()` collapses the group to `[Rotate__init, Blower__init]` (2 unsatisfied). Competing writers like `S_Clearing=True` (1 unsatisfied) look syntactically cheaper but semantically leave the branch context needed for `y_BurnerLoop` — going to Clearing or Resetting moves the walker *further* from the overall goal.

The root cause: `_unsatisfied_condition_groups()` returns bare `list[tuple[str, Any]]` per writer — dropping satisfied guard context. The sort key in `_establish()` uses `(deprioritized, len(group))` — purely syntactic, no context awareness.

## Design

### 1. `_WriterCandidate` type (new, in `priors.py`)

```python
@dataclass(frozen=True)
class _WriterCandidate:
    full_conditions: tuple[tuple[str, Any], ...]   # ALL enabling conditions (before sat/unsat split)
    satisfied: tuple[tuple[str, Any], ...]         # Already true in snapshot
    unsatisfied: tuple[tuple[str, Any], ...]       # Need to be walked
    all_writes: frozenset[str]                     # Writer rung's static write footprint
    writer_index: int                              # PDG rung_nodes index (diagnostic)
```

- `full_conditions` = SP-tree guards + copy-source binding + index binding + call gates (everything `_unsatisfied_condition_groups` already extracts per writer, before the snapshot filter)
- `satisfied` = subset currently true in snapshot
- `unsatisfied` = subset that needs walking (same content as today's per-writer group)
- `all_writes` = `RungNode.all_writes` (= `writes | implicit_writes`) for effect-conflict detection
- `writer_index` for diagnostic/debug trace; excluded from ordering

### 2. `_writer_candidates()` API (new, in `priors.py`)

```python
def _writer_candidates(
    tag, value, snapshot, pdg, program, nd_domains, known, func_deps
) -> tuple[list[tuple[str, Any]], list[_WriterCandidate]]:
    """Returns (union_prereqs, per_writer_candidates)."""
```

Same iteration as `_unsatisfied_condition_groups()` but preserves the full/satisfied/unsatisfied split per writer plus the write footprint from the PDG `RungNode`.

`_unsatisfied_condition_groups()` becomes a compatibility wrapper:
```python
def _unsatisfied_condition_groups(...):
    union, candidates = _writer_candidates(...)
    groups = [list(c.unsatisfied) for c in candidates if c.unsatisfied]
    return union, groups
```

### 3. Candidate-specific monitors

**The key mechanism.** When a candidate is selected for walking, its satisfied guards become must-stay guards in the child walk's `_StepMonitors`:

```python
def _candidate_monitors(
    inherited: _StepMonitors,
    candidate: _WriterCandidate,
    governing: str,
    gov_value: Any,
) -> _StepMonitors:
    """Promote candidate's satisfied guards to must-stay for the child walk."""
    if not candidate.satisfied:
        return inherited
    guard = _MustStay(
        must=candidate.satisfied,
        until=((governing, gov_value),),
    )
    return inherited.with_guard(guard)
```

This means: while walking `Rotate__init` and `Blower__init` as prereqs for the Starting completion writer, `S_Starting=True` is a must-stay guard. If any child action disturbs `S_Starting`, the `_apply_steer_fold` violation check prunes the branch.

This uses the existing `_StepMonitors`/`_MustStay` machinery — no second context system. The candidate's guards compose with inherited parent monitors via `with_guard()`.

### 4. Disposition-based ordering

Classify each candidate into a disposition before sorting:

```python
class _Disposition(IntEnum):
    PREFERRED = 0   # preserves or extends active must-stay context
    NORMAL = 1      # no evidence either way
    DEFERRED = 2    # write effects might disturb context, not proven
    REJECTED = 3    # direct structural conflict with must-stay
```

**Classification rules** (all structural, no name heuristics):

**REJECTED**: candidate has an unsatisfied condition `(tag, value)` where `tag` is protected by an active must-stay AND `value` conflicts with the must-stay's required value. This is a direct structural impossibility — satisfying this prereq would violate the must-stay. A single direct conflict is sufficient; no need to also check `all_writes`. Skipped for the current attempt only; does not enter nogoods.

**DEFERRED**: candidate's `all_writes` footprint overlaps `monitors.protected_tags()`. The writer *might* disturb must-stay state, but we don't know for certain (conditional writes, etc.). Soft deferral — tried after preferred/normal candidates, never pruned.

**PREFERRED**: candidate's satisfied conditions overlap with active `_StepMonitors.must_stay` `must` conditions. The candidate is literally in the branch the monitors are protecting. This is the strongest alignment signal — it comes from inherited structural context, not heuristics.

**NORMAL**: everything else.

**Within-tier ordering** uses a composite context score, not just `len(unsatisfied)`:

```python
def _candidate_sort_key(c, monitors, visited, deprioritized):
    return (
        _classify_disposition(c, monitors),
        any(t in deprioritized for t, _v in c.unsatisfied),
        -_context_score(c, monitors, visited),
        len(c.unsatisfied),
    )
```

**Context score priority** (what makes one PREFERRED candidate rank above another PREFERRED):

1. **Active must-stay overlap** (strongest): count of `candidate.satisfied` conditions that appear in `monitors.must_stay[*].must`. Directly measures how much this candidate preserves the inherited branch context.
2. **Candidate-monitor coherence**: count of satisfied guards that the candidate-specific monitors (Section 3) would preserve — i.e., how many of the candidate's own guard conditions form a meaningful protected context. For candidates at the same must-stay overlap, prefers the one with a richer self-consistent branch.
3. **`visited` overlap** (tie-breaker only): count of `candidate.satisfied` conditions that appear in `visited`. `visited` is a cycle guard, not an achievement record — useful as a tie-break when stronger signals are equal, but not proof of context.

```python
def _context_score(c, monitors, visited):
    must_set = set()
    for guard in monitors.must_stay:
        must_set.update(guard.must)
    score = 0
    score += 100 * sum(1 for cond in c.satisfied if cond in must_set)
    score += 10 * len(c.satisfied)  # richer self-context
    score += 1 * sum(1 for cond in c.satisfied if cond in visited)
    return score
```

**DEFERRED, not pruned**: candidates that might move away stay available — tried after compatible candidates exhaust. Between-group corridor probing catches early success from a deferred candidate if the preferred one fails.

### 5. Must-stay helpers on `_StepMonitors` (in `base.py`)

```python
def protected_tags(self) -> frozenset[str]:
    """Tags that active must-stay guards protect (the 'must' half)."""
    return frozenset(tag for guard in self.must_stay for tag, _v in guard.must)
```

Used by disposition classification for the effect-conflict check: `candidate.all_writes & monitors.protected_tags()`.

### 6. Pass registry integration

Add to `WALK_PASSES` in `passes.py`:
```python
_WalkPass(
    "context_aware_groups",
    "ordering",
    "Order writer alternatives by context alignment with the ancestor "
    "walk path and promote satisfied guards to candidate-specific monitors; "
    "disabled, groups sort by unsatisfied-condition count only.",
)
```

Gate behind `ctx.advice.has("context_aware_groups")`. When ablated:
- Sort key reverts to `(deprioritized, len(group))` (today's behavior)
- No candidate-specific monitors (only inherited parent monitors)
- `_unsatisfied_condition_groups()` wrapper path used directly

### 7. Remainder group and edge cases

- **Remainder group**: union pairs not covered by any writer stay as a bare list appended after all candidates. Sorts last naturally (no disposition, no alignment).
- **Fully-satisfied candidates**: empty `unsatisfied` means the writer is already armed. Probe the corridor directly instead of walking sub-goals. Today's `if group:` filter at `priors.py:1000` drops these — the new code should instead emit a candidate with empty unsatisfied and handle it as "probe immediately" in `_establish()`.
- **Idx-chase alternatives** (`priors.py:904-911`): each inverting index value becomes a separate `_WriterCandidate` with the same `full_conditions` except the index binding. Context alignment score is identical (correct — they're alternatives for the same writer).

## Integration points

| File | What changes | Lines |
|------|-------------|-------|
| `priors.py` | `_WriterCandidate` dataclass, `_writer_candidates()`, `_unsatisfied_condition_groups()` wrapper | ~776-1006 (refactor extraction loop, add satisfied capture) |
| `agenda.py` | `_candidate_monitors()`, disposition classification, sort key replacement, consume candidates in group loop, pass candidate monitors to child walks | ~1658-1870 |
| `base.py` | `_StepMonitors.protected_tags()` method | ~496 (add after `with_guard`) |
| `passes.py` | `context_aware_groups` pass in `WALK_PASSES` tuple | ~59 |

### Detailed `_establish()` changes (agenda.py ~1658-1870)

1. **Line ~1659**: Replace `_unsatisfied_condition_groups()` call with `_writer_candidates()` when `ctx.advice.has("context_aware_groups")` is True; fall back to old call otherwise.

2. **Line ~1758-1771**: Replace group sorting with candidate sorting using `_candidate_sort_key()`. Extract `.unsatisfied` from sorted candidates for the walk loop. Prepend any fully-satisfied candidates as immediate probe triggers.

3. **Line ~1777-1780**: When building `child_mon`, compose with `_candidate_monitors()` for the current candidate's satisfied guards. The existing `_child_monitors()` call stays — candidate monitors stack on top.

4. **Line ~1790-1800**: `_try_independent_walks()` receives the candidate-augmented monitors. No other changes to independent-fork logic.

5. **Line ~1832-1841**: Sub-goal `_Request` items receive the candidate-augmented monitors, so the must-stay violation check in `_apply_steer_fold()` enforces the candidate's branch context.

## The "pause/distance" lever (v2)

For v1, candidate-specific monitors + disposition ordering handles the PackML case: the aligned candidate is tried first, and its branch context is enforced during child walks. If a wrong candidate is selected (because alignment scoring was ambiguous), the must-stay violation prunes it quickly rather than letting it run to budget exhaustion.

For v2, if empirical evidence shows this is insufficient:
- **Speculative pre-probe**: Fork and walk one step of each candidate's first unsatisfied prereq; rank by whether the snapshot moved closer to ancestor goals.
- **Candidate suspension**: Park a stalling candidate and try the next. Requires snapshot/restore of holds, nogoods, and temporal rules for discarded probes.
- **Structurally exclusive derived predicates**: Use simplified Boolean forms (`simplified.py`) to detect mutual exclusion between candidate guards without simulation.
- **Plan-tree alignment**: Replace `visited` with a proper "achieved goals" index from `_PlanNode` segments.

## Test plan

### Synthetic test: `test_walk_context_groups.py`

Program with mutually exclusive branches and two writers for a completion bit:

```
Branch A: S_A (Bool)
Branch B: S_B (Bool)
Mutual exclusion: setting S_B=True clears S_A (via program structure, not names)

Completion bit: Done (Bool)
  Writer 1 (branch A): condition = S_A ∧ Init1 ∧ Init2   → sets Done=True
  Writer 2 (branch B): condition = S_B                    → sets Done=True

Walk: how(Done) starting from S_A=True, Init1=False, Init2=False, S_B=False
```

**With context-aware ordering:**
- Writer 1: satisfied={S_A=True}, alignment=1, disposition=PREFERRED, unsatisfied={Init1, Init2}
- Writer 2: satisfied={}, alignment=0, disposition=NORMAL (or DEFERRED if all_writes includes S_A)
- Writer 1 chosen → walks Init1, Init2 under must-stay(S_A=True) → Done fires

**Ablated (pass disabled):**
- Writer 2: 1 unsatisfied, sorted first → walks S_B=True → S_A cleared → Done in wrong branch

**Assertions:**
1. Context-aware: solves within default budget, uses Writer 1 path
2. Ablated: exhausts budget or takes measurably more forks/recovery
3. Candidate-specific monitors: walking Init1/Init2 under must-stay(S_A=True) succeeds
4. If parent committed S_A=True, Writer 2's effect on S_A is flagged as must-stay conflict → disposition=DEFERRED or REJECTED

### Unit tests for `_writer_candidates()`

- Projection matches old `_unsatisfied_condition_groups()` output (compatibility)
- Satisfied/unsatisfied decomposition is correct against snapshot
- `all_writes` matches PDG `RungNode.all_writes`
- Direct must-stay conflict classifies as DEFERRED/REJECTED
- Effect-based conflict (all_writes overlaps protected_tags) classifies correctly
- Unknown effects defer/order only, never prune

### Burner regression confirmation

- `how(y_BurnerLoop)` on the PackML template
- `S_StateCompleteBool=1` selects Starting completion path (S_Starting + Rotate__init + Blower__init)
- Clearing/Resetting alternatives are not selected first
- Temporal rotate learning still fires under the correct Starting context
- Solve time comparable or better than current

### Existing test compatibility

- `test_walk_writer_groups.py`: passes via compatibility wrapper
- `test_walk_passes.py`: new pass gets ablation row automatically
- `make test-walk`: full suite green

## Non-goals for v1

- No PackML-name heuristics — purely structural
- No complete mutual-exclusion solver from simplified forms
- No public API commitment on `_WriterCandidate`
- No second context system beyond `_StepMonitors`
- No temporal-rule redesign
- No candidate suspension / best-first agenda search
- No recursive upstream effect analysis
- Candidate probes (v2) need snapshot/restore for holds/nogoods/temporal rules — not needed for v1 since we order statically

## Staged implementation

**Stage 1** — Type + API + compatibility:
- `_WriterCandidate` in `priors.py`
- `_writer_candidates()` alongside `_unsatisfied_condition_groups()`
- `_unsatisfied_condition_groups()` refactored as thin wrapper
- Unit tests for the new function (decomposition correctness, projection equivalence)

**Stage 2** — Monitors + ordering:
- `_StepMonitors.protected_tags()` in `base.py`
- `_candidate_monitors()` in `agenda.py`
- Disposition classification + sort key in `_establish()`
- `context_aware_groups` pass in `passes.py`
- Handle fully-satisfied candidates (empty unsatisfied → immediate probe)

**Stage 3** — Test + validate:
- Synthetic mutual-exclusion test (`test_walk_context_groups.py`)
- Burner template regression (scratchpad confirmation)
- Ablation row (automatic via pass registry)
- `make test-walk` green
