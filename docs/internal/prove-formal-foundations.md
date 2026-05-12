# Formal foundations for prove — citation map

Maps academic results to specific code, so future changes can check
whether they preserve the property each result guarantees.

---

## 1. Exclusivity principle (absorb.py northstar docstring)

**Claim**: The concrete value of a threshold tag is irrelevant to
reachability if the tag is only used in threshold comparisons.

**Formal backing**: Two results compose.

- **Data independence** (Wolper, 1986 — "Expressing Interesting
  Properties of Programs in Propositional Temporal Logic"): if a
  variable is only tested for ordering/equality against constants and
  never used to derive new values, the program's control-flow behavior
  is independent of its concrete value. The exclusivity check
  (`_has_non_timer_data_read` at absorb.py:314) is the syntactic
  sufficient condition.

- **Time-abstracting bisimulation** (Tripakis & Yovine, 2001 —
  "Analysis of Timed Systems Using Time-Abstracting Bisimulations"):
  for reachability (safety) properties, the concrete timing of state
  transitions is irrelevant — only whether transitions are reachable.
  This justifies replacing any positive threshold with synthetic
  preset=1 in redundant absorption (`_find_redundant_acc_absorptions`
  at absorb.py:350).

**Together**: the exclusivity check ensures the threshold value doesn't
influence reachability through data-flow. Time-abstracting bisimulation
ensures it doesn't influence reachability through timing. B2 phase 1 is
provably sound.

**What would break it**: adding a code path where an absorbed threshold
tag's value flows into a copy/calc target (breaks data independence) or
where the BFS checks a time-sensitive property like "timer fires within
N scans" (breaks time-abstract semantics).

---

## 2. Event scheduler acceleration (events.py:65–92, 143–173)

**Claim**: `_scans_until_done_event` computes the number of scans to
skip and `_advance_hidden_progress` applies them in one step, without
visiting intermediate states.

**Formal backing**: **Flat acceleration for linear counter automata**
(Leroux & Sutre, 2005 — "Flat Counter Automata Almost Everywhere!").
Acceleration computes the transitive closure of a self-loop with a
linear update. For affine updates (Acc += constant delta), flat
acceleration is exact — not an approximation.

The `visited`-state check in `_maybe_jump_hidden_event` (events.py:275)
detects the self-loop. The acceleration is sound when the enabling
condition depends only on the discrete state (the visited-set key), not
on other hidden accumulators. When hidden timers are interdependent,
`_settle_pending` resolves cascades (see §3).

**What would break it**: a timer whose per-scan delta depends on another
hidden accumulator's concrete value (not just its crossed/uncrossed
status). Currently safe because timer instructions use a fixed dt or
count-by-1.

---

## 3. Settle-pending termination (events.py:242–263)

**Claim**: `_settle_pending` iterates at most `event_count + 1` times
to resolve all pending threshold crossings.

**Formal backing**: **Region construction** (Alur & Dill, 1994 — "A
Theory of Timed Automata"). The number of distinct timing regions is
bounded by the number of clock comparison constants. Each iteration
resolves at least one crossing, crossings are monotone (accumulators
don't decrement during settling), so the bound is tight.

**What would break it**: counters that can both increment and decrement
within a single settling sequence.

---

## 4. Done-bit three-valued abstraction (absorb.py:107–118)

**Claim**: collapsing timer Acc to `{False, PENDING, True}` preserves
reachability.

**Formal backing**: **Zone abstraction / Difference-Bound Matrices**
(Dill, 1989; Miné, 2001). The three values represent regions of a
single counter relative to a threshold boundary — the canonical
partition induced by the constraint `Acc >= Preset` plus the zero
boundary. This is the same structure as clock zones in UPPAAL, restricted
to monotone counters with known comparison points.

The threshold vector path (`_threshold_vector_key` at kernel.py:227)
generalizes this to a product of boolean predicates — one per threshold
comparison. This is a point in the product lattice of zone predicates.

Self-resetting counters/timers (e.g. ``count_up(C, 10).reset(C.Done)``)
do not violate this abstraction.  The threshold vector is re-extracted
from the concrete kernel state after every BFS step
(``_threshold_vector_key``), so when a reset drops the accumulator
below a threshold boundary the vector correctly reflects the
un-crossing.  No monotonicity assumption exists in the extraction path.

**Not** (0,1,∞) counter abstraction (Pnueli/Xu/Zuck), which tracks
cardinality of identical processes rather than regions of a single
counter.

---

## 5. Domain partition from comparisons (classify.py:481–491)

**Claim**: for relational comparisons (`<`, `>`, `>=`, `<=`), the domain
`{lit, lit-1, lit+1}` for each comparison literal is sufficient.

**Formal backing**: **Cartesian predicate abstraction** (Ball,
Podelski, Rajamani, 2001 — "Boolean and Cartesian Abstraction for
Model Checking C Programs"). The ±1 expansion computes the partition
induced by predicates `{x < lit, x == lit, x > lit}`. Each partition
cell is behaviorally equivalent under the program's comparisons.

For equality-only comparisons (A1 / eq-ne enum closure), the partition
is `{lit₁, lit₂, ..., OTHER}` — the standard equivalence-class
abstraction. The OTHER sentinel represents the top of the equivalence
class.

**Unifying framework**: Trace partitioning (Rival & Mauborgne, 2005 —
from the Astrée team). Both cases are instances of partitioning driven
by program-observable comparisons.

---

## 6. Structural domain propagation (classify.py:364–419)

**Claim**: `_collect_structural_domains` computes domains by iterating
until no new values are discovered.

**Formal backing**: **Abstract interpretation fixed-point** (Cousot &
Cousot, 1977). The lattice is value-sets ordered by ⊆, bounded at 1000
elements per tag. Finite-height lattice ⇒ Kleene iteration terminates
without widening. Convergence in at most `1000 × |tags|` iterations.

The computation is an **over-approximation**: it may include values that
aren't actually reachable due to control flow. This is sound for BFS
(no false negatives from domain inference) but may include unreachable
states.

**What could be slow**: long copy chains of depth D with K values each
need O(D × K) iterations. PLC programs have flat copy structures, so
this is not a practical concern.

---

## 7. Scope filtering + absorption ordering (classify.py:85–117)

**Subtlety**: `_collect_all_exprs` scope-filters expressions before
they're passed to threshold absorption. Absorption decisions are made
on the scoped expression set, not the full program.

**Why this is sound**: **Cone-of-influence reduction and abstraction
commute for safety properties** (Clarke, Grumberg, Long, 1992 — "Model
Checking and Abstraction"). Data-flow reads of a threshold tag outside
the cone are irrelevant to the property regardless. Absorbing within the
cone is sound because the cone contains all expressions that could
affect the property.

**Conservative alternative**: run the exclusivity check on the full
expression list (before scoping). This only makes absorption more
conservative — blocking some absorptions that are actually safe.

---

## 8. Pilot sweep vs. static rules

**Conclusion**: the pilot sweep (`_pilot_sweep_domains` at
classify.py:816) is concrete forward simulation for domain discovery.
It is not CEGAR — there's no abstraction refinement loop. The static
rules (A3 literal-write mining + A4 structural propagation) are strictly
better for PLC programs because they see all possible writes regardless
of path feasibility, while concrete execution suffers from coverage
limits.

The pilot sweep could be repurposed as a **validation pass** (à la
DART/SAGE abstract testing) — sanity-checking that static rules don't
miss reachable values — rather than a fallback for domain discovery.

---

## Papers referenced

1. Alur & Dill. "A Theory of Timed Automata." TCS, 1994.
2. Ball, Podelski, Rajamani. "Boolean and Cartesian Abstraction for Model Checking C Programs." TACAS, 2001.
3. Clarke, Grumberg, Long. "Model Checking and Abstraction." TOPLAS, 1994.
4. Cousot & Cousot. "Abstract Interpretation." POPL, 1977.
5. Dill. "Timing Assumptions and Verification of Finite-State Concurrent Systems." CAV, 1989.
6. Leroux & Sutre. "Flat Counter Automata Almost Everywhere!" ATVA, 2005.
7. Miné. "A New Numerical Abstract Domain Based on Difference-Bound Matrices." PADO, 2001.
8. Rival & Mauborgne. "Trace Partitioning in Abstract Interpretation Based Static Analyzers." ESOP, 2005.
9. Tripakis & Yovine. "Analysis of Timed Systems Using Time-Abstracting Bisimulations." FMSD, 2001.
10. Wolper. "Expressing Interesting Properties of Programs in Propositional Temporal Logic." POPL, 1986.
