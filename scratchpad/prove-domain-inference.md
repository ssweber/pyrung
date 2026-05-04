# Prove domain inference — structural rules for bounding unbounded tags

Scratchpad for new pre-BFS passes that infer finite domains from program
structure alone (writers, comparisons, transfer shape). No name or comment
heuristics.

## How prove works (read this first)

`prove(logic, condition)` exhaustively checks a property over all reachable
states via BFS. Each BFS "state" is a tuple of tag values across all
dimensions. The state space is the product of all dimension sizes — 3
tags with 10 values each = 1000 states.

To keep the state space tractable, prove uses two strategies:

1. **Domain inference** (classify.py): bound each tag to a finite set of
   values. A Bool is `{True, False}`, an Int with `choices={0,1,2}` is
   `{0,1,2}`, etc. Tags without finite domains are "infeasible" and prove
   returns `Intractable`.

2. **Threshold absorption** (absorb.py): collapse timer/counter
   accumulators from concrete progressions (0, 10, 20, ..., 1000) into
   abstract states (False, PENDING, True). This turns an N-valued
   accumulator into a 3-valued Done bit, dramatically reducing dimensions.

Key files:
- `src/pyrung/core/analysis/prove/classify.py` — domain inference,
  dimension classification, `_extract_value_domain()`
- `src/pyrung/core/analysis/prove/absorb.py` — accumulator absorption,
  threshold abstraction (has a northstar docstring explaining the
  exclusivity principle — read it)
- `src/pyrung/core/analysis/prove/passes.py` — pre-BFS pass pipeline
- `tests/core/analysis/test_prove_passes.py` — pass-level tests
- `tests/core/analysis/test_prove.py` — integration tests

Run tests: `make test` (never `uv run pytest` directly).

## Validation

After each rule is implemented, run the existing test suite (`make test`)
and re-run the  project lock to check progress:

```
cd C:\Users\Sam\desktop\NAME\pyrung_output
uv run pyrung lock main
```

The lock command reports unbounded tags with hints. The goal is to reduce
these from ~50 to only tags that genuinely need human annotation (analog
inputs with no structural comparisons).

## Problem

`pyrung lock main` on a real project reports ~50 unbounded tags.
Most fall into two buckets: "no domain constraint" (external inputs without
`min=/max=/choices=`) and "threshold abstraction blocked" (timer accumulators
whose preset tags aren't recognized as stable — but stability is the wrong
framing; see B2).

---

## A. Domain inference rules (classify.py territory)

### A1. eq/ne-only enum closure [done]

If a tag is only ever read in `== literal` / `!= literal` comparisons and
never used in arithmetic or data-flow, its abstract domain is
`{literals..., OTHER}`.

`OTHER` is a single sentinel representing every value not in the literal
set — all such values are indistinguishable to the program.

**Scope guard**: a tag that also appears in `% 2` or `+ 1` (like `_CurStep`
in task_example.py:208) does NOT qualify. This rule helps pure-switch tags
like `MLkOK`, `Sts_MaterialInterlock`.

**Where**: `_extract_value_domain()` in classify.py:136. Add a check:
if all atoms are eq/ne with literal operands AND `_has_forbidden_data_read`
is False, return `sorted(literals) + (OTHER,)`.

**Fixes**: `MLkOK`, `Sts_MaterialInterlock`, and any other pure-switch
external inputs.


### A2. Internal + zero writers => singleton domain {default} [done]

If a tag is not external and has no entries in `graph.writers_of`, it is a
constant at its default value. Domain = `(default,)`.

This also feeds `_is_stable_threshold` in absorb.py — an unwritten internal
tag is trivially stable.

**Where**: early exit in `_classify_dimensions_from_graph()` (classify.py:338
loop) before the role/writer checks. Also add to `_is_stable_threshold()`
in absorb.py:365.

**Fixes**: any internal tags that happen to lack writers and currently fall
through to the infeasible path. Also unblocks threshold absorption when such
a tag is used as a preset.


### A3. Literal-write domain mining [done]

If every write to a tag is `copy(literal, T)` (no expression writes, no
tag-sourced copies), the domain is `{default} | {all written literals}`.
`fill(literal, block.select(...))` counts as a bulk `copy(literal, T)` for
each tag in the selected range.

Detection: walk `_all_write_targets` sites, check each source. If all
sources are numeric/bool literals, collect them. For `FillInstruction`,
the source is a single literal applied to every target in the range.

**Where**: new helper called from `_extract_value_domain()` or as a
standalone pass before classification. Needs access to write-site sources,
not just targets.

**Fixes**: `A_P2_PwrLossDebounce_Ts` and similar init-only config tags that
are `copy(constant, tag)` in an init rung and never touched again.
`Dump_Limit_Ts`, `Dump_PanWatchdog_Ts`, etc. if their writes are all
literal copies.


### A4. Acyclic domain propagation (fixed-point) [already noted, extended]

Extend the current copy/calc inheritance into a fixed-point pass over the
write graph:

- `copy(Src, Dest)` with no convert => Dest inherits Src's domain
- `calc(Src % k, Dest)` => Dest domain is `0..k-1`
- `calc(Src + 1, Dest)` where Src == Dest (self-increment) => combine with
  reset writes to bound the range
- Multiple writers => union of inferred domains

Iterate until no new domains are discovered (fixpoint).

**Where**: new pass in the pipeline between `classify_dimensions` and
`pilot_sweep`. Operates on the `ProgramGraph` write edges.

**Fixes**: `*__StoredStep` (inherits from `_CurStep` via copy),
`_valstepisodd` (from `% 2` => domain `{0, 1}`), and transitively any tag
downstream of a bounded source.


### A5. Comparison-derived external input domain [already noted]

For external inputs, if all comparisons are against literals (or against
tags whose domains are already known), derive the partition domain from
those values. This is the existing `_extract_value_domain` logic — it
already works for the literal case. The gap is tag-vs-tag comparisons
where the partner is still infeasible on first pass.

Solved by A4 (propagation fixpoint) — once the partner gets a domain, the
external input can derive its boundary values.

**Fixes**: sensor/config inputs compared against other bounded tags.

---

## B. Threshold absorption rules (absorb.py territory)

### B1. Monotone eq/ne abstraction for int-progress tags [new]

Currently `_diagnose_unstable_atom()` in absorb.py:399 only accepts
`>` / `>=`. For reset-plus-+1 progress tags, `== k` and `!= k` are
still eventable.

A progress counter that resets to 0 and increments by 1 passes through
every integer. For comparison `== k`, the counter crosses from `k-1` to
`k` (enters) and from `k` to `k+1` (exits). This can be modeled as
bucket abstraction: `{<k, =k, >k}`.

For `!= k`, the crossing events are the same — the comparison flips at
the same boundaries.

**Where**: extend `_threshold_atom_for_progress()` in absorb.py:379 to
accept eq/ne atoms for `_PROGRESS_KIND_INT_UP` tags, emitting crossing
events at `k` and `k+1`. Requires corresponding changes to the BFS event
scheduler.

**Fixes**: all 6 `*__StoredStep` tags and `CycleTmr__StoredStep`,
`DS741` (if recognized as int-progress). Alternative to A4 for these
specific tags, but A4 is more general.


### B2. Threshold value is irrelevant to reachability [already noted, reframed]

The concrete value of a threshold tag does not affect which states are
reachable — only WHEN crossings occur, and prove doesn't model time.

For `Rung(timer.Acc >= Limit_Ts): copy(1, Error)` — whether `Limit_Ts`
is 100 or 4000, or even changes mid-run via Modbus, the reachable states
are the same: `{Error=False, Error=True}`. The BFS already explores both
"timer fires" and "timer doesn't fire" regardless.

We are NOT assuming the user is well-behaved (only changing presets at
stable transitions). We're saying: the threshold is irrelevant. Any
positive value leads to the same crossing. The accumulator either reaches
it or doesn't, and the BFS explores both paths.

The real gate for absorption is not stability — it's exclusivity: is the
threshold tag ONLY used in threshold comparisons? If the program also
does `copy(Limit_Ts, SomeOtherTag)` or `calc(Limit_Ts * 2, X)`, the
concrete value matters. The existing `_has_forbidden_data_read` check
already enforces this.

**Two absorption paths, different readiness:**

The *redundant Acc absorption* path (`_find_redundant_acc_absorptions`)
already discards the threshold value (synthetic preset=1). The stability
check (`_is_stable_dynamic_preset`) is purely unnecessary — the
exclusivity check (`_has_non_timer_data_read`) is the only gate that
matters. This can be relaxed now.

The *threshold vector* path (`_find_threshold_absorptions`) stores the
concrete threshold in `ThresholdAtomSpec` and looks it up per BFS state.
Relaxing `_is_stable_threshold` here requires either keeping the
threshold tag as a BFS dimension (needs a finite domain) or reworking
the event scheduler to handle unknown thresholds. The stability check
remains as a pragmatic implementation constraint, not a soundness
requirement.

**Phase 1 (done)**: relax `_is_stable_dynamic_preset` in redundant
absorption. Immediate win for timers whose Acc is only compared against
the preset boundary.

**Phase 2 (later)**: rework the threshold vector scheduler to handle
unknown thresholds, then relax `_is_stable_threshold`.

**Fixes (phase 1)**: timer Acc/preset pairs where the Acc is only
compared against the Done-triggering boundary — the Acc and preset tag
both get absorbed, synthetic preset=1.

**Fixes (phase 2)**: all `*_tmr_Acc` blocked by `*_Ts` not stable:
`SFCExample_tmr_Acc`, `Dump_tmr_Acc`, `Locks_tmr_Acc`, `Tilt_tmr_Acc`,
`ModbusMgr_tmr_Acc`, `Lift_tmr_Acc`, `CycleTmr_tmr_Acc`, and their
`*__CurStep_tmr_Acc` variants. Also absorbs the `_Ts` tags themselves
out of the state space.


### ~~B3. Stable-threshold via reset coupling~~ [subsumed by B2 reframe]

### ~~B4. Single-writer internal stability~~ [subsumed by B2 reframe]

B3 and B4 were about proving stability. With B2 reframed, stability
isn't the gate — exclusivity is. B3/B4 are unnecessary.

---

## C. Cross-cutting

### C1. A2 feeds B2

A2 (internal + zero writers = constant) gives those tags a singleton
domain AND makes them trivially threshold-absorbable. Implement A2
first — it simplifies the B2 exclusivity check by removing unwritten
internal tags from consideration entirely.

### C2. A3 complements B2

Literal-write mining (A3) discovers the finite set of values a threshold
tag can take. B2 absorbs it out of the state space entirely if it's
threshold-only. Together they cover the case where a config tag is loaded
with `copy(constant, tag)` and then used as a timer preset.

### C3. What this simplifies or removes

The structural rules + exclusivity reframe don't just add capability —
they reduce existing complexity:

**Pilot sweep** (`_pass_pilot_sweep` / `_pilot_sweep_domains`, ~120 LOC):
Discovers domains by running the compiled kernel forward across all
nondeterministic input combinations. Fragile (`max_combos=100_000`
silently bails on realistic programs), expensive at runtime. A3 and A4
statically handle the same cases. Plan: implement A3 + A4, run tests
with pilot sweep disabled, remove if green.

**Stability checks** (`_is_stable_dynamic_preset`, `_is_stable_threshold`,
`_diagnose_unstable_atom`): the stability framing required users to
annotate threshold tags with `readonly=True` or `final=True` to enable
absorption. The exclusivity reframe makes these annotations unnecessary
for threshold-only tags — the program structure already tells us enough.
Phase 1 (redundant absorption) eliminates `_is_stable_dynamic_preset`
entirely.

**User annotation burden**: `choices=`, `min=/max=`, `readonly=True` were
required on tags that the program structure can bound. After A1-A4, most
discrete tags self-bound. After B2, threshold-only tags self-absorb.
Remaining annotations are only for truly ambiguous cases (analog inputs
with no structural comparisons).

**Hint diagnostics**: `_build_infeasible_hints` currently suggests "add
choices=, min=/max=, or readonly=True" for every unbounded tag. With the
structural rules, fewer tags reach this point, and the hints that remain
are more actionable — they represent tags that genuinely need human input,
not structural information the compiler could have inferred.

### C4. Priority order

Suggested implementation order (low-risk first, highest leverage):

1. **A2** — internal + zero writers (trivial, no false positives)
2. **A3** — literal-write mining (structural, easy to validate)
3. **B2 phase 1** — relax `_is_stable_dynamic_preset` (redundant absorption
   path only — synthetic preset=1, exclusivity is the real gate, no
   scheduler changes needed)
4. **A4** — acyclic propagation (general, covers StoredStep cascade)
5. **A1** — eq/ne enum closure (needs careful scope guard for data-flow)
6. **B1** — monotone eq/ne buckets (needs BFS event scheduler changes)
7. **B2 phase 2** — rework threshold vector scheduler, then relax
   `_is_stable_threshold` (bigger change, deferred)

---

## D. Motivating examples

### DS741

Has a tiny structural write set: two `copy(literal, DS741)` sites.
A3 (literal-write mining) infers its domain directly without metadata.

### SFCExample step machine

- `_CurStep`: has literal comparisons (`== 1`, `== 3`) AND arithmetic
  (`% 2`, `+ 1`). A1 does NOT apply (data-flow usage). A4 handles it
  via `% 2` => `{0,1}` for `_valstepisodd`, and propagation from copy
  to `_StoredStep`.
- `_StoredStep`: no literal comparisons, only `!= _CurStep`. Solved by
  A4 (inherits _CurStep domain via copy).
- `_valstepisodd`: `calc(_CurStep % 2, _valstepisodd)`. A4 infers
  domain `{0, 1}`.
- `SFCExample_Limit_Ts`: external, not written, used only as a timer
  preset. B2 phase 1 absorbs it via exclusivity — the concrete value is
  irrelevant to reachability. Both the preset tag and `SFCExample_tmr_Acc`
  disappear from the state space.

---

## E. Lock file abstraction

Currently `write_lock()` dumps fully concrete values for every projected
tag in every reachable state. No abstraction layer.

### E1. Default projection: terminal Bools only

Change the default projection from "all terminal tags" to "terminal Bool
tags only." Non-Bool terminals (Reals, Ints used as analog values) are
noise in the lock diff.

In the reachable state dicts, only include tags whose value is True.
False is the implied default — omitting it makes each state read as
"what's ON." A state like `{"Alarm": true, "Pump": true}` is instantly
readable vs `{"Alarm": true, "Pump": true, "Valve": false, "Heater": false, ...}`.

Users can still `project=` to include non-Bool tags explicitly.

### E2. Abstract value categories for non-Bool projections

When a non-Bool tag IS projected, collapse concrete values into abstract
categories before serialization:

- **Real**: `{ZERO, NEGATIVE, POSITIVE}` or user-defined bands
- **Int compared relationally**: `{>= TagA, < TagB, OTHER}` — the
  comparison predicates active in that state, not the concrete value
- **Int with choices**: use the choice label, not the raw number

This makes the lock file stable across minor constant changes and
human-readable without cross-referencing tag definitions.

### E3. Analog values: test, don't prove

Prove's sweet spot is discrete/Boolean logic where exhaustive exploration
is tractable. The structural inference rules (A1-A4, B1-B2) extend that
to step machines, timers, and configuration tags that are structurally
finite. That covers the real programs.

For truly unbounded Reals (analog sensors, PID setpoints), exhaustive
verification isn't the right tool. These get covered by `dt=` testing
with representative input scenarios — which is already how industrial
PLCs are validated (exhaustive discrete logic verification, simulation
of analog paths).

Implication: the lock file defaults to terminal Bools (E1), prove
handles discrete logic exhaustively, and analog coverage comes from the
test suite. Tags that remain unbounded after A1-A4 + B1-B2 aren't bugs
in prove — they're signals that the tag belongs in a test, not a proof.
