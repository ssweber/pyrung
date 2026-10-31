# Formal foundations for prove — citation map

Maps academic results to specific code, so future changes can check
whether they preserve the property each result guarantees.

---

## 1. Black-box timers (blackbox.py)

**Claim**: a `Proven` from the search holds on the PLC under every scan
timing and every preset value.

**Formal backing**: **Existential abstraction preserves safety**
(Clarke, Grumberg, Long, 1994 — "Model Checking and Abstraction"). The
abstract kernel replaces each timed tick with a nondeterministic choice
(`_bb_tick`, `_TimerOracle`): hold, or move to the limit, to a value a
comparison splits at, or (for an accumulator read as a value) to any value
in between.  Every concrete scan of length `dt >= 0` moves the accumulator
forward by some amount, and every such move is matched by one choice, so
the abstract system simulates the concrete one: every concrete run is an
abstract run, and an unreachable abstract bad state is concretely
unreachable.  Enable, reset and preset logic are the runtime's (the same
`compile_kernel` codegen path), so only the time step is abstracted.

Counters count rising edges, not time: they stay concrete, and a counter
nothing else reads is keyed no further than its preset (`count_clamps`),
an exact quotient (past the preset, one-way counting behaves alike until a
reset).

**What would break it**: a tick whose candidates omit a value the program
can tell apart (an accumulator compared with a tag whose value set is not
complete must be swept, `accumulator_reads`); a reduction that drops a
ticking accumulator from the key (the elision fast path must not take an
instruction's own read-back for write-before-read, `self_read_tags`); a
timed instruction compiled without the oracle.

---

## 2. Counterexample confirmation (blackbox.confirm)

**Claim**: every `Counterexample` is a concrete PLC run to a violation.

**Formal backing**: the confirmation step of **counterexample-guided
abstraction refinement** (Clarke, Grumberg, Jha, Lu, Veith, 2000 —
"Counterexample-Guided Abstraction Refinement"), without the refinement:
an abstract trace is replayed on the concrete PLC at the search's fixed
`dt`, waits folded, input changes slid to the scan the state changes on.
A trace the PLC follows is reported with the concrete steps; one it does
not is spurious *or* needs a timing the fixed `dt` does not give, and is
reported as unconfirmed (`Intractable` of kind `"timing"` or
`"property"`), never as a counterexample.

**What would break it**: accepting a replay that lands on a different
state than the one the property judged (under `settled=True`, a transient
state; `_Replay.accepts`).

---

## 3. How()'s domain prior: Done/Acc and threshold vectors (absorb.py)

Not a soundness claim: PILOT replays every route it plans.  Its world key
projects a timer's `(Done, Acc)` onto `{False, PENDING, True}`
(`_done_acc_state`) and a progress accumulator onto threshold-crossing bits
(`_ThresholdVectorSpec`).  These are the canonical partitions of a single
counter by its comparison points (the zone/region view of Dill, 1989), and
the exclusivity check (`_has_forbidden_data_read`, data independence after
Wolper, 1986) decides which accumulators and thresholds the prior may
hide.  The search uses none of it.

---

## 4. Plain BFS depth

With no hidden-event acceleration, `depth_budget` is the BFS depth: the
longest scan sequence explored.  A search cut short by it reports a
caveat on its `Proven`; it never reports a silent one.

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

## 7. Scope filtering

**Subtlety**: `_collect_all_exprs` scope-filters expressions before
classification; domains are inferred on the scoped expression set, not
the full program.

**Why this is sound**: **Cone-of-influence reduction and abstraction
commute for safety properties** (Clarke, Grumberg, Long, 1994 — "Model
Checking and Abstraction"). Comparisons outside the cone are irrelevant
to the property regardless.

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

1. Ball, Podelski, Rajamani. "Boolean and Cartesian Abstraction for Model Checking C Programs." TACAS, 2001.
2. Clarke, Grumberg, Jha, Lu, Veith. "Counterexample-Guided Abstraction Refinement." CAV, 2000.
3. Clarke, Grumberg, Long. "Model Checking and Abstraction." TOPLAS, 1994.
4. Cousot & Cousot. "Abstract Interpretation." POPL, 1977.
5. Dill. "Timing Assumptions and Verification of Finite-State Concurrent Systems." CAV, 1989.
6. Rival & Mauborgne. "Trace Partitioning in Abstract Interpretation Based Static Analyzers." ESOP, 2005.
7. Wolper. "Expressing Interesting Properties of Programs in Propositional Temporal Logic." POPL, 1986.
