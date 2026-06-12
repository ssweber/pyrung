# Corridor Walker — Living Plan

The single consolidated document for the walker: theory, vocabulary, what's
built, settled direction, and staged execution. Update this file as
stages land.

**Status (2026-06-12):** the walker is the sole `how()` path in
`src/pyrung/core/analysis/walk/` (own `CLAUDE.md`). Entry: `plan_walk`,
called from `PLC._how_via_walk` (`core/runner.py`). Tests:
`tests/core/analysis/test_walk_*.py` via `make test-walk` (233 tests); full
suite green. Stages A–D landed 2026-06-10; four hardening arcs landed
2026-06-11; idx-chasing arc (Open #11 jump-table lever) landed 2026-06-12;
compound-goals must-stay arc landed 2026-06-12 (committed conjuncts
re-checked after every later goal's walk + reorder resolver,
`test_walk_compound_goals.py`).
The bank-aliasing fix landed the same day (memory
`bank-aliasing-unification-arc`) and the re-baseline on the regenerated
template DISSOLVED the `isCmdValid_Yes` frontier: `how(S_StateCurrent ==
4)` solves reachable=True in 3.5s through real command masks and the
commissioned jump table, unpatched. Frontier: Or-gate fixture (#8) +
recurring obligation (#9, `y_BurnerLoop`).

---

## Theory statement

The corridor walker rests on a provable structural argument, not just an
engineering bet.

A single-scan, no-interrupt PLC program is a deterministic function from
(state, inputs) → state′. PLC programs are producer-consumer hierarchies of
sequential corridors coupled through narrow handshake interfaces (ISA-88,
PackML, IEC 61131-3 SFC enforce this by design). The program is its own
executable model — forkable, steppable, fully observable. Forward progress is
ground truth (step and observe). Backward structure is exact (read the SP-tree
via `simplified()`/`cause()`/`why()`). To solve a reachability goal: factor
into subsystems via the coupling structure, walk each corridor forward using
backward structure to steer and recover, force coupling signals to decouple
timing, verify feasibility by summing achieved depths against handshake
deadlines.

This is lock-and-key maze solving in a structurally tractable slice: most gates
are one-state (interlocks — polynomial), the gates are readable
(simplified/cause — no search needed for key identification), and the timed
gates decompose (producer-consumer, not adversarial). The general gadget-maze
problem is PSPACE-complete (Demaine, Hendrickson, Lynch); PLC programs are in
the easy subclass because the standards enforce simple locks, readable
conditions, and hierarchical key ordering.

**Scope constraint:** single-scan PLC without interrupts. Multi-task PLCs with
priority-based preemption (S7-1500 OBs, ControlLogix periodic/event tasks)
break the deterministic-order guarantee and are out of scope. Extension would
require modeling interrupt semantics as additional nondeterminism.

---

## The planner, in POCL vocabulary

The walker is a **hierarchical planner** with three layers:

1. **Abstract** — collapse the full PLC state space to one governing tag's
   value graph (tiny: mode machines have single-digit values).
2. **Plan** — best-first search over that abstract value space (cheap).
3. **Refine** — for each abstract edge, find concrete inputs via interpreted
   simulation on forks (sound by construction — immune to static-analysis
   blindness).

Its working vocabulary is **partial-order causal-link (POCL) planning**,
executed forward on the real interpreter:

- An **open condition** is an unachieved goal `(tag, value)` — raised by
  target decomposition, a writer's enabling condition, a residual, or an
  oracle re-check.
- A **hold** is a causal link: (external input, value, the committed goal that
  depends on it) — a protection interval over the walker's *own hand*.
  External inputs are sticky and entirely under walker control, so there is no
  abstraction gap to be wrong about; holds never assert anything about the
  program.
- A **threat** is a steer that would break a hold. Threats are detected by
  construction (the `HoldStore`), not discovered after the fact by
  clobber-recovery.
- A **resolver** is one of four responses to a flaw: **establish** (walk a
  corridor / sub-goal), **reorder** (move the threatened goal), **divest**
  (the empirical white-knight probe: fork, release, settle, check the held
  goal survives), **reject** (record a nogood, abandon the branch).

Deliberately *not* borrowed: POCL's least-commitment ordering (would force
replays, surrendering the forward-fork oracle) and PDR's frame/induction
machinery (assumes a symbolic transition relation). From PDR we take the
mechanics only: one deepest-first queue of obligations, a global budget,
stale-item skipping, and nogood generalization by drop-and-retest (§Settled
direction).

Self-conflicts stay out of the program's ledger: `cause()` explains the
program; holds are the walker's hand. A cause()-named blocker that is a held
input is classified one layer up (`_classify_blockers(goals, holds)`) and
routed to divest or reorder — it never enters the `NoGoodStore`. Nogoods
record program facts only.

Everything in scope maps to one of four extensions of the core:

| Extension | What it does | Planning concept |
|---|---|---|
| **Widen the input** | Accept goals the engine punts on (Or/And decomposition, multi-tag factoring) | Better abstraction / goal preprocessing |
| **Widen the alphabet** | Succeed on more transitions (non-Bool inputs, multi-input steers, link-aware de-energize) | Richer action space for refinement |
| **Backtrack on failure** | Re-plan with learned constraints when refinement fails or execution diverges | Hierarchical backtracking |
| **Diagnose infeasibility** | Explain why no plan is feasible | Explanation generation |

The guiding question for every new mechanism: **does it extend the existing
engine's reach, or does it add a parallel path?** Prefer the former. The
guiding rules, post-consolidation:

- Anything readable from the SP-tree or PDG → ordering advice via the pass
  registry.
- Anything knowable only by running → learned nogood in the loop.
- Every new mechanism is a resolver, a flaw source, or a pass — **never a new
  loop**.

**Static analysis is a prior, never correctness-bearing** — it picks the
governing tag, narrows the steer alphabet, and sets the horizon. Correctness
comes from simulation. Validation is always interpreted.

### The oracle advantage

Unlike classical planners (which reason over a symbolic model that may diverge
from reality), the corridor walker operates on **the program itself as a
white-box oracle**: forkable, per-rung steppable, with full observability of
what was read/written. There is no abstraction gap — simulation IS the
program. This shapes the architecture:

| Layer | Role | Tools | Properties |
|-------|------|-------|------------|
| **Generate candidates** | Narrow the search space | `why()` (state-aware minimal), `simplified()` (structural), PDG (coarse) | Heuristic — may over-/under-generate |
| **Forward exploration** | Try candidates | `fork()` + step (the walker engine) | Ground truth — deterministic, sound |
| **Validate / explain** | Confirm cause or diagnose failure | `cause()` on scan log | Recorded truth — what actually happened |

The symbolic layer generates candidates; the interpreted layer validates them.
No CEGAR loop needed because the "refinement check" runs the real program —
spurious abstract paths are caught in one step, not iteratively refined away.

**Candidate generation tools (finest → coarsest):**

- **`why(tag)`** — backward SP-tree attribution from a snapshot. Gives the
  *minimal load-bearing contacts* explaining the current value. State-aware:
  prunes irrelevant formula branches given the actual fork state. Use when you
  need "what's holding this tag HERE" (steer prioritization, regression
  sub-goals, factoring).
- **`simplified(condition)`** — resolved Boolean form to input-level. Structural:
  all paths through the formula regardless of current state. Use when you need
  "what COULD make this true/false" (full regression, enabling-condition
  analysis).
- **PDG** — `upstream_slice`, `writers_of`, `condition_reads`. Coarsest static
  connectivity. Use for cone-narrowing, solve-order proposals, independence
  screening.

**Validation tool:**

- **`cause(tag)`** — recorded-mode causal analysis on the scan log. Gives
  trigger vs. enabler split: what *transitioned* the tag vs. what was already
  in place. Use after simulation to confirm which inputs were load-bearing, to
  extract nogoods from failures, and to produce actionable diagnosis.

---

## What's built

One interpreted best-first engine (`engine.py`), all of it replay-verified and
covered by `make test-walk`. Capabilities, compressed (mechanism details live
in docstrings and the Findings section):

- **Corridor core** — governing-tag selection with a simulation-probe fallback
  (`_probe_steps`: fork-steer-observe, immune to static blindness); steer
  alphabet (empty, pulse, drive-low, multi-input conjunctive groups, non-Bool
  set-value from pipeline domains) with helpful-steer ordering and
  release-then-pulse for edge-gated commands; time-folding to accumulator
  crossings (dt-knob for timers, acc-patch for per-scan counters, plateau
  guard as the soundness gate); dynamic reaction budget.
- **Goal handling** — And/Or decomposition; recursive prerequisite discovery
  through writer SP-trees and subroutine call gates (depth 6, cycle-checked);
  inequality prereq resolution (`gt/ge/lt/le` → concrete values); seal-in
  break (inverse regression for OTE/latch); idx-chasing for indirect-copy
  writers (invert `copy(block[idx], tag)` on the live snapshot, sub-goal the
  index register; calc-scratch pointers hopped via pipeline func-dep
  projections or the sole writer's calc expression); `avoid=` support;
  compound-target must-stays (committed conjuncts re-checked on the work
  fork after every later goal's walk — a regression fails the attempt with
  a `goal-regressed` node and `plan_walk` retries with the clobbering goal
  promoted ahead of the goal it broke, tried-set terminated, holds rolled
  back per attempt; the replay verify backstop returns a diagnosed Path
  naming the unmet conjunct instead of a bare None).
- **Recovery + learning** — oracle-driven re-check (projected
  `cause(tag, to=value)` mining triggers and blockers as sub-goals, bounded by
  `_MAX_RECHECK_ITERS=3`); `NoGoodStore` keyed `(from, to,
  frozenset(blocking))` with seen-key projection onto blocking-tag names and a
  blocker-clearing move; `_needs_decomposition` Tier-2 hinting with
  pre-clobber checkpoint.
- **Holds (prevention before recovery)** — `HoldStore` of (input, value,
  committed goal); selective release in `_steer_prefix` (protected names skip
  every implicit release); empirical divest probe for intended writes to
  protected inputs; per-branch tracking on `_Node.released`, reconciled at
  commit; `_commit_holds` at every corridor commit point; `Path.holds` +
  "Holds:" rendering. The recovery loop stays as backstop and is
  tripwire-covered (cross-guard 2 iters, serial-clobber 3 iters).
- **Independent-fork walks** (Tier 1, generalized) — pairwise-disjoint-cone
  independence gate, per-fork sub-walks, cone-filtered hold extraction,
  simultaneous multi-steer with multi-timer fold convergence; applied at all
  four serial-walking sites.
- **Physical layer** — Harness propagation through `fork()`; linked-feedback
  tags excluded from the steer alphabet (the walker steers enables, the
  Harness synthesizes feedback); `how(unlink=)` fault scenarios, mirrored onto
  verify/annotate forks via `_install_replay_harness`; profile-gated
  advancement (`_advance_time` recognizes ramping profiles as progress).
- **Plumbing** — BFS/waypoint fallback deleted; `how()` returns a verified
  `Path` or `Path(reachable=False)`; prover pipeline context consumed via
  `allow_partial=True`; walker in its own package with its own contract
  (`walk/CLAUDE.md`).

---

## Settled direction

One sentence: static advice in through a registry, one agenda loop in the
middle, verified plans and a triangle table out.

### One loop instead of four

The four solve loops differ only in where goals come from, not in what they
do. They dissolve into **goal sources** feeding one deepest-first agenda of
flaws (open conditions + threats), handled by the four resolvers
(§vocabulary). One global fork/scan budget; stale items skipped; honest
"budget exhausted" as the Phase-5 `NotFound` trigger. Resolver preference
order (e.g. divest-first for latch-heavy programs) is a tuning knob, not
structure — programs differ in which resolver wins, not in loop shape.

Three representation choices belong *inside* this consolidation — they are the
loop's data structures, not add-ons:

- **Plan as tree, not flat list.** Each solved goal returns a node
  `(goal, actions, holds, children)`; flatten once at `Path`-build time.
  Backjump drops the subtree for the goal that diverged; `NotFound(best
  partial plan, first failing edge)` prints the partial tree; the triangle
  table derives from the flattened tree. The agenda's
  `(goal, depth, provenance)` covers the search side; this is the output side.
- **One fold monitor.** `_apply_steer` (watch one governing value) and
  `_apply_steer_compound` (sequential goal-list iteration) are the same
  function parameterized by a `done(state)` predicate. The execution-monitoring
  items — path-sequence divergence, must-stay violation, deadline race —
  become monitors plugged into this one point, not three code paths.
  (Must-stay for compound targets landed 2026-06-12 as post-goal detection
  + reorder at the walk root, NOT yet as a steer-level monitor here —
  promoting it to this seam, so a single order can route around clobbering
  steers instead of reordering goals, is the parked follow-up; see Open
  Items #12.)
- **`Diagnosis` spec'd as a consumer, not a mechanism.** The return type reads
  the plan tree + holds + nogoods + pass journal; the global budget supplies
  the honest "budget exhausted" trigger. Distinguish `Unsolvable(cert)` (all
  steers exhausted + `cause()` shows each failure structural) from
  `NotFound(reasons)` (budget exhausted; carries best partial tree, first
  failing edge, accumulated nogoods). Spec the type during consolidation so
  the tree representation has to carry what diagnosis needs.

### Nogood generalization (the needle-mover)

Today's nogoods are exact cause()-named assignments; they rarely recur, so
`is_blocked` starves and seen-keys fragment. PDR's lesson: after learning a
failure, drop assignments and re-test — the simpler version that still fails
is the real nogood. PDR needs a SAT solver for the re-test; the walker forks
and runs. Broader nogoods prune more on deep interlock chains. This is the one
borrowed idea that extends reach on harder programs rather than cleaning code.

Residual risk: the store stays shared per `plan_walk` and add-only, so
accumulated blocking names fragment `seen`-keys for *unrelated* goals.
Generalization shrinks each blocking set but doesn't scope the store. If
fragmentation still bites: project per-goal — only nogoods whose `(from, to)`
involves the current governing tag.

The deferred `from_value` key-variance (D2) is a specific instance with a
concrete mechanism: for counter-like governing tags, recovery records nogoods
identical in blocking but differing only in the drifting from-value. Drop
the from-value and re-test on a fork at a different from-value; if the
failure persists, wildcard it — one generalized nogood replaces N exact
ones. Tripwire: a counter-valued governing tag where recovery accumulates
redundant exact-key nogoods.

### Triangle table (output, orthogonal)

Derived once from holds + steps at `Path`-build time. `kernel(i)` = conditions
that must still hold for steps i..n to remain valid (Fikes–Hart–Nilsson 1972 /
PLANEX). One structure gives:

- must-stay monitoring ("is the highest true kernel still true"),
- **window characterization** — the rows a hold spans are its timing window
  (opened when interlocks satisfied, closed by the nearest deadline crossing);
  the narrowest window is the plan's timing fragility, rendered as "flip
  CmdStart — you have 60 scans (~3 s)",
- **divest points** — the row a hold leaves IS the emergent waypoint/phase
  boundary (discovered by walking, not static analysis),
- divergence recovery (resume from the highest true kernel),
- operator-legible `how()` output.

Free: the data already exists; it is a table not yet built.

### Pass pipeline for the walker

Mirror `prove/`'s idiom — registered passes run once per `plan_walk`, freeze a
walk context (the deferred `_WalkContext` lands here), journal their decisions
(à la `_JournalBuilder`) so diagnosis can report which advice applied.

Build-once goes with freeze-once: `_build_jump_context` does a whole-program
SP-tree scan and is currently rebuilt at every recursion level × recovery
iteration × independent walk; everything in it except
`normal_dt`/`profile_fb_names` is static per walk. Same for `_governing`'s
`_probe_steps` results — memoize per tag. Both land in the frozen context.

Each pass declares its **kind**, and the kind is its proof obligation:

| Kind | Examples | Ablation property |
|---|---|---|
| Ordering | edge/level sort (steady enablers before triggers), destructive-writer scan (`~A → reset(B)` forces A-then-B), window suspects, flaw selection | Disable freely: same verdicts, more recovery iters/forks |
| Narrowing | steer alphabet, cone filters, `set_value_relevance` (set-steer flood cap) | Must be conservative (over-approximate); disabling only widens |
| Fold | the four fold-churn rungs | Each carries its own exactness argument; verify replay backstops; disabling regresses only in the refusing direction |
| Widening | `ack_cleared_inputs`, `transient_handshake` bundles | Adds candidate steers/goals only; every addition validated by the interpreted trial; disabling regresses only in the refusing direction |

The completeness matrix writes itself: one test parametrized over the registry,
disable each pass, assert by kind ("same verdict, or budget-exhausted" — under
finite budgets, slower can mean None, so ablation runs raise budgets or accept
exhaustion). Every new pass gets a matrix row by construction.

Structural guarantees:

- Passes get `(program, pdg)`, run before the walk, never again; no handle to
  the agenda, work fork, or stores. A heuristic physically cannot become a
  parallel path — the only door into the loop is advice.
- Runtime learning (nogoods, holds) stays out of the registry. Everything
  load-bearing lives on the loop side of the line; that is what keeps the
  ablation property true.

Soundness is untouched throughout: replay verification carries it, so no pass
can break it. Passes touch completeness only.

---

## Staging — all landed

### Stages A–D — ✅ LANDED 2026-06-10

- **A — context & build-once:** `_WalkContext`, `_JumpContext` once per walk, `_probe_steps` memo, budget counter.
- **B — one fold monitor:** `_apply_steer_fold(done, monitor)` unifying steer shapes.
- **C — agenda loop:** four solve loops → `_drive` frame stack; plan tree (`_PlanNode`); `_classify_blockers`; module split (base/physical/fold/steer/priors/explore/agenda/engine).
- **D1 — triangle table:** `TriangleTable`/`TriangleRow` in `graph.py`; kernels, windows, divest points.
- **D2 — fold-churn rungs:** original nogood-generalization blocked (noise can't reach store via `cause()`); redesigned as four fold-churn rungs (`test_walk_fold_churn.py`): unread, disjoint, modwrap-source, derived-crossings. `from_value` key-variance deferred.
- **D3 — pass registry:** `walk/passes.py` + ablation matrix (`test_walk_passes.py`).
- **D4 — backjump + diagnosis:** third `_explore` exit, segment-chained backjump, `Diagnosis` on `Path.diagnosis`; hold-blind re-explore resolved hold-aware.

### Hardening arcs — ✅ LANDED 2026-06-11

- **(d) Explore cost:** fork tag-index reuse (95→5ms); `set_value_relevance` cap; per-steer budget enforcement; `how(walk_seconds=)`.
- **(a) Handshakes:** `_scan_transient_rest`, `ack_cleared_inputs`, `transient_handshake` bundles. Milestone: `how(S_UnitModeCurrent==1)` → ground-truth pulse, 3.5s.
- **(c) Spin guard:** failed-goal dedup keyed (goal, projected state, store generation).
- **(e) RTC churn:** no fix needed (ticks 1-in-100 at dt=10ms).
- **Copy-source arc:** `return_early()` leak, copy-source binding (oracle+static), flatten honesty. `S_StateCurrent==2` solves 3.7s.
- **Writer-groups arc:** per-writer prereq groups, smallest-first ordering, inter-group probing; indirect-copy crash fix. Tripwire: 60 vs ~124 forks.
- **Ref-constant arc:** `ref_constant_order` pass; never-written copy-source registers deferred. Premise corrected: REF bank = ND inputs not init constants. Tripwire: ~110 vs ~1214 forks.

### Idx-chasing arc — ✅ LANDED 2026-06-12

Open #11's interpreted lever, at the copy-source binding site
(`_unsatisfied_condition_groups`, the `wv is None` writer skip):

- **Table inversion** (`_invert_indirect_source`): for `copy(block[idx],
  tag)` writers, enumerate the index register's candidates, evaluate each
  candidate's address on a `_SnapshotView` overlay, keep those whose table
  slot holds the goal value, sub-goal the *index register* (never the
  source bank). Best (current-first) value rides the union; each extra
  inverting value gets its own per-writer group (alternatives, not
  conjuncts).
- **Calc-scratch hop**: the template computes the pointer into scratch
  (`calc(S_StateRequested + 150, sm__jump_target_ds_idx)`). The hop
  consults pipeline `functional_dep_projections` first, falls back to the
  sole writer's single-source calc expression (`_single_calc_definition`),
  composing each hop into the address evaluation (3-hop bound).
- **Walk-only func-dep advice** (prove-side, `_PassContext.walk_only`):
  `detect_functional_dependencies` admits slice-elided and
  ordering-violating scratch under walk_only, recording advice-grade
  projections with no dims/elision bookkeeping — prove bit-identical,
  walker projections populated (7 on the live template, incl. the
  jump-table pointer and `isCmdValid__dh_base`). Pinned in
  `test_prove_passes.py::TestWalkOnlyFunctionalDepAdvice`.
- **Copy-source candidates**: the index register's candidate pool includes
  copy-from-tag writers' source snapshot values — the template writes
  `S_StateRequested` ONLY via `copy(sm__STATE*REF, …)`, no literals, so
  without this the chase is blind on exactly the live shape.
- Tests: `test_walk_copy_source.py` 13 total (+8 this arc) — IndirectRef /
  IndirectExprRef walks, calc-scratch end-to-end, both hop paths,
  copy-source candidates, multi-inverting-value groups, honest refusal.

### Post-arc frontier — re-baselined 2026-06-12 (bank-aliasing fix landed)

The tag-map aliasing fix landed (memory `bank-aliasing-unification-arc`:
`map_to` stamps slot identity, universal codegen slot emission,
`reset_banks()`) and the template was regenerated
(`CLICK (00C3157C)\pyrung_project`). All three PackML config banks now
carry real values on the twin (slot defaults on ds[151..167] jump table,
dh[101..109] command masks, dh[301..317] mode masks; the rows were
non-retentive in the CSV). probe_aliasing confirms ONE tag per register —
the raw `DS165`-style keys are absent from state entirely.

**Re-baseline results:**

- `how(S_StateCurrent == 4)` (probe_burner17): **reachable=True in 3.5s**
  (was honest budget NotFound @120s). 5-step textbook PackML recovery —
  alarms + C_Clear → wait → clear alarms → C_Reset → wait → IDLE, holds
  on C_Clear/C_Reset — through real `isCmdValid` masks and the jump table
  (`sm__JUMPRESETTING2IDLE=4`) unpatched. The `isCmdValid_Yes` Tier-2
  budget wall was an ARTIFACT of the blank config ROM; the Tier-2
  coupling hint lever for it is moot. Lever (ii) goal-directed value
  ordering: pressure relieved on this target (no current test-bed pain);
  keep parked until a fixture demands it.
- probe_idxchase_live (no DS165 patch needed): the chase binds
  `(S_StateRequested, [4, 15, 17])` — all three commissioned slots
  holding 4 invert (NOJUMPIDLE@154, JUMPRESETTING2IDLE@165,
  JUMPCOMPLETED2IDLE@167) — and `_unsatisfied_conditions` for
  `(sm__where2jump, 4)` resolves to `[(S_StateRequested, 4)]` directly.
- The honest-refusal shape (zero table → chase refuses) remains pinned in
  `test_walk_copy_source.py`; retentive registers still rest at type zero
  (ND inputs — the "zero from cold is honest" semantics are preserved for
  genuinely retentive config).

`y_BurnerLoop` @120s (probe13): mode handshake established in-walk;
blocked on rotate toggle (#9) — now the lead live frontier, with the #8
Or-gate fixture next.

---

## Future scope (beyond the stages)

- **Multi-corridor timing (Phase 6 Tiers 2–3).** Tier 1 (force-and-sum via
  independent-fork walks) is done and generalized. Tier 2 (force and check the
  deadline) has detection wired (`_needs_decomposition` +
  `_log_decomposition_hint` + pre-clobber checkpoint); the force-and-solve
  mechanism waits for a real mutual-interference test case. Tier 3 (iterate
  cyclic coupling to fixed point) needs the oscillation guard first (Open
  Items #1). Also here: reschedule (alternative linearizations),
  co-advance cyclic synchronization (true-deadlock diagnosis), convergence
  diagnosis via `cause()` on the clobber scan, divest-as-sync-edge.
- **Constructive regression** — `why(governing)` on the stuck fork as a new
  goal source (each non-input conjunctive root becomes a sub-walk goal),
  depth-bounded. Post-consolidation this is "add a flaw source", not a
  mechanism. Reduces dependence on pattern-specific passes being complete:
  state-aware AND structural, unlike the static extractor (all writers) or
  `cause()` mining (scan-log artifacts). Refinement: **frontier-terminated
  `why()`** — terminate the tree not at external inputs but at any tag the
  walker can already change (steerable, or solved earlier this walk), so
  regression yields the nearest actionable sub-goals instead of every
  non-input leaf. Not free: termination-at-inputs is hardcoded as
  `writers_of`-empty at three sites in `why.py`; needs a pluggable
  criterion threaded through.
- **Steer-history reuse** — try previously-successful steers first, keyed
  `(governing, from_value, to_value)`. Speculative, not binding: a stale
  steer just fails and normal exploration takes over. Architecture note:
  within-walk history is runtime state, so this enters as loop-side
  learning (a sibling of `NoGoodStore`/`HoldStore`), NOT a registry pass —
  the registry's frozen-advice rule forbids it. Only cross-walk reuse
  (history from prior `plan_walk` calls, frozen at walk start) could be a
  pass. Feeds #9 (see Open Items).
- **Symmetry transfer** — detect structural isomorphism (same SP-tree
  shape, same writer structure under tag renaming — common for repeated
  stations/axes/recipe steps) and transfer a solved steer sequence through
  the renaming. Steer-history reuse generalized from same-tag to
  same-structure; the PDG carries the connectivity for the check. High
  leverage on repeated-subsystem programs; no current test-bed pain
  demands it — wait for a fixture.
- **Cheap steer pre-screening** — before forking, evaluate a candidate
  steer against `simplified()` of the governing tag's next transition on
  the live state; skip steers that can't satisfy any branch. Conditional
  value: fork creation is ~5ms post-(d), but the real per-candidate cost
  is the post-fork stepping, so screening may still pay on flood shapes
  where the alphabet ≫ helpful steers. Measure before building.
- **Callable predicate (`expr=None`)** — one xfail: opaque predicates need
  expr decomposition or a try-after-walk adapter.
- ~~Dead BFS deletion~~ — ✅ done; helpers in `core/analysis/sp_values.py`.
- ~~Cheap trial~~ — ✅ satisfied at fork layer (95→5ms tag-index reuse).
- **Ack-cleared Ints** (user suggestion, 2026-06-11) — widen the
  ack-cleared-input idea to Ints/Words the program only ever
  reset/copy(0)/fill(0)s. Needs set-value domains for them
  (program-written tags are classified stateful, so they have no
  `nondeterministic_dims` entry — domains would come from
  reader-comparison inference).

---

## Validation status

| Target | Corridor type | Steer | Result | Notes |
|---|---|---|---|---|
| `StateCurrent==EXECUTE` from ABORTED | mode machine | input pulses | walk ~2 s, replay→6 | go/no-go |
| `_CurStep==5` from EXECUTE | task timer wait | empty (folded) | walk, replay→5 | folded via dt-knob |
| counter dwell 0→1 (synthetic) | per-scan counter | empty + pulse | folds via acc-patch | exact landing, replay-verified |
| `how(Ready, Done)` (two-step latch) | compound And | input pulses | walk 3 steps, 0.0 s | Or/And decomposition |
| `y_Burner` from cold (nested) | 3-layer timer-gated | CmdMode + CmdStart + 2 folds | walk 5 steps, ~1.3 s | recursive prereqs through 3 sub layers |
| `StateCurrent=="IDLE"` from cold | mode (string operand) | input pulses | walk 2 steps | simulation probe finds steps |
| inequality-gated transitions | analog/Int ND input | set-value | walk via pipeline domains | `nondeterministic_dims` steers |
| callable predicate (`expr=None`) | opaque | — | xfail | needs expr decomposition |
| linked feedback exclusion | Harness-driven fb | input steers | walk via enables | fb excluded from steer alphabet |
| `how(unlink=["Fb"])` fault | broken sensor | direct force | walk forces fb | bypasses physical chain delay |
| profile-gated (`Temp >= 5.0`) | analog ramp | hold + profile | walk ~500 scans | Harness ticks profile on fork |
| serial clobber | coupled latches | pulses + reset | walk recovers via oracle | `test_walk_decomposition` |
| cross-guard mutual clobber | coupled latches + 2 timers | holds + reset | walk recovers, ≤2 iters | `test_walk_nogood` |
| Int command protocol | multi-hop state machine | CmdReset + CmdStart | walk 3 actions | `test_walk_real_patterns` |
| return_early() flow gating | subroutine flow control | Enable pulse | walk reachable | `test_walk_real_patterns` |
| rendezvous (two SFCs) | independent subsystems | multi-steer (Tier 1) | walk 2 actions, 30 scans | `test_walk_real_patterns` |
| odd/even step sequencer | self-increment + even skip | Advance + fold | walk reachable | `test_walk_real_patterns` |
| deep call chain (5 levels) | 5-level prereqs, 3 sub scopes | CmdProd + CmdReset + CmdStart + fold | walk reachable | `test_walk_real_patterns` |
| holds prevention A/B | serial corridors sharing enables | holds + selective release | zero recovery iters | `test_walk_holds` |
| set-value flood (30 noise ND) | 3-step Mode corridor | multi + pulses | solves at 131 forks (ablated 635) | `test_walk_budget` |
| consumed-same-scan handshake | mode-request protocol | simultaneous bundle | walk 1 step | `test_walk_handshake` |
| PackML chain (ack-cleared + call gate) | 2-level transient | bundle {ChgReq, ProdMode} | walk 1 step | `test_walk_handshake` |
| **live** `S_UnitModeCurrent==1` | real PackML mode change | bundle {C_ProductionMode, C_UnitModeChgRequest} | walk 1 step, 2.9s | ground-truth pulse; re-confirmed post-aliasing-fix from mode 3 / state 9 |
| circularly-dead prereq | spin-guard shape | — | honest NotFound | `test_walk_spin_guard` |
| **live** `S_StateCurrent==2` | C_CtrlCmd command chain | pulse C_Clear | walk 2 steps, 3.7s | no bundle needed |
| **live** `S_StateCurrent==4` | mode-gated completion | alarms + C_Clear + C_Reset | walk 5 steps, 3.5s | post-aliasing-fix template; was budget NotFound on blank config ROM |
| ref-constant bank (14 REFs) | ref-goal flood | Arm ×4 + Go | ~110 forks (ablated ~1214) | `test_walk_ref_flood` |
| copy-source chain | mode → completion → state copy | Adv + {ProdMode, ChgReq} | walk reachable | `test_walk_copy_source` |
| two-writer goal | writer disjunction | AdvB + Kick | 60-fork budget (ablated ~124) | `test_walk_writer_groups` |
| indirect-copy writer | statically unresolvable | — | honest unreachable, no crash | `test_walk_copy_source` |
| **live** `y_BurnerLoop` | full chain | — | honest NotFound @120s | blocked on #9 rotate toggle |
| jump-table writer (plain + expr ptr) | indirect copy | Sel + Go | walk reachable | `test_walk_copy_source` idx-chase |
| calc-scratch pointer (template shape) | indirect via scratch | Sel + Go | walk reachable | hop via calc expr / func_deps |
| REF-fed index (no literal writers) | copy-source candidates | Arm + Go | walk reachable | the probe20 blindness, pinned |
| zero jump table | indirect copy | — | honest unreachable | chase refuses, no inverting index |
| **live** `(sm__where2jump, 4)` | commissioned table (native) | — | binds `(S_StateRequested, [4, 15, 17])` | probe_idxchase_live, post-aliasing-fix |
| compound clobber (mode resets step) | And-of-Compare conjuncts | reorder retry | walks 4 steps from either order | `test_walk_compound_goals` |
| conflicting conjunction (pinned step) | And-of-Compare | — | honest unsolvable, names conjunct | `test_walk_compound_goals` |
| **live** `(S_StateCurrent==4, S_UnitModeCurrent==1)` | compound state+mode | alarms + Clear/Reset + mode bundle | walk 6 steps, ~5s either order | probe_compound_goal, no reorder needed |
| full suite | all types | all steers | 233 pass | walker-only `how()` |

---

## Findings (so we don't re-derive)

- **`fork()` is a true checkpoint** — carries `.tags`, `.memory` (incl. `_frac:` timer
  fraction), time mode, dt, RTC, and `_harness` (feedback couplings + pending patches).
  Verified bit-identical continuation across 20+ scans after a mid-fraction fork.
  Backjump via `fork(scan_id)` (runner.py) rests on this.
- **Corridor source is NOT the old waypoint front-half.** `_order_waypoints`
  collapses coupled-tag SCCs into mega-waypoints (> `_MEGA_CONE_LIMIT`), and
  `_build_value_transitions` is empty for `copy`-written tags. The real graph
  comes from interpreted probing.
- **Steering uses the interpreted runner, not projected `cause()`/`effect()`.**
  The runner is the forward oracle (multi-scan, full state); `cause()` is
  reserved for the backward (divergence/nogood) direction at ScanLog fidelity.
- **External inputs are sticky** (hold last value); `patch()` clears the patch, not the
  tag. Edge-gated commands need release-then-pulse.
- **Verification is interpreted** (replay on a fresh fork) — no compiled-kernel
  agreement risk for the walk path.
- **No abstraction gap (the oracle advantage).** The "model" is the program on
  a fork — correct by construction. Tradeoff: nogoods generalize less freely
  (a nogood at S might not hold at S′), but never false positives. Closer to
  concolic execution / RRT than classical planning.
- **`why()` is the state-aware candidate generator** — minimal load-bearing
  contacts for the current fork state via SP-tree attribution; terminates at
  external inputs; handles latch seal-in vs. OTE differently; multi-tag tree
  structure IS the factoring structure.
- **`why()` and `projected_cause()` resolve subroutine rungs** (both had the
  same blindness, fixed: check `node.subroutine`, resolve from
  `program.subroutines`). `effect()` unaffected (rung_idx from simulation
  capture).
- **Multi-tag factoring uses writer-condition extraction, not `why()`.**
  `_unsatisfied_conditions` reads writer SP-trees + subroutine call gates
  directly — sufficient for producer-consumer hierarchies. `why()` stays
  available for static SCC decomposition if ever needed.
- **Dynamic prerequisite ordering is sufficient.** No explicit topological
  sort: walking A discovers B as its own prerequisite; `visited` prevents
  re-walks; depth bound 6 is conservative.
- **`cause()` is the validation/nogood oracle.** Trigger vs. enabler split on
  the scan log. The scan log IS the implication graph — no symbolic
  derivation. Exploration settled it over the SP-tree for blocking facts:
  `_unsatisfied_conditions` returns `[]` for guard-gated arms where
  `cause(tag, to=value)` cleanly names the blocker.
- **Pipeline `allow_partial` is safe for the walker** — infeasible tags are
  simply absent from dimension dicts; `always()`/`never()` Intractable gating
  unchanged.
- **`avoid_pred` receives `dict(tags)`, not the state object.**
- **Intermediate-result prerequisites must not block retry** — some writer
  conditions are corridor-internal (e.g. `Trans==1` set mid-fold); `continue`
  past failed prerequisites and retry `_explore`, which handles them via
  time-folding.
- **Pipeline domains are boundary-focused** — behavioral bisection gives
  expression partition values (comparison literals ± 1 + default), 5–10 per ND
  input; no extra thinning needed.
- **Tier 1 insertion is before the delegate corridor, not after** — for
  rendezvous, `_explore` succeeds on the delegate and the failure is
  downstream in residuals; preempt the serial walk when
  `governing != target_tag`.
- **Cone-filtered hold extraction prevents steer-release contamination** —
  only inputs causally connected to the prerequisite are collected from a
  sub-fork's actions. (Post-consolidation, first-class holds should make the
  cone filter deletable.)
- **`TagRole.INPUT` is too narrow for steerability** — "has any writer" ≠
  "not operator-driven". PackML acknowledge patterns (program resets the
  HMI request/mode bits, often via range resets) make the actual operator
  inputs PIVOTs; `ack_cleared_inputs` re-admits them. Watch for other
  acknowledge shapes (toggling, echo registers).
- **Resting value ≠ declared default.** Click projects can declare nonzero
  initial values (and retentive semantics make even honoring them suspect —
  separate codegen investigation pending); anything boundary-anchored in
  the walker must infer the rest from the program's own clearers.
- **Budget enforcement must reach inside the explore loop.** The agenda
  checks `budget.exhausted` between resolver steps, but one establish can
  run an entire explore (and its recursion) inside a single step — without
  the per-steer check, fork caps overshoot unboundedly and a wall-clock
  cap is meaningless.
- **The recovery oracle compensates for Or-blind prereq extraction on
  small programs** — cause() in unreachable mode names the never-observed
  state bool and recovery walks it (probe_orgate/orgate2 both solve
  today). The §2b Or-gate gap is only observable when recovery rounds are
  consumed elsewhere or the chain is deeper — hence the fixture
  requirement in Open Items #8.
- **Spin-guard generation dynamics**: keying failed goals on the add-only
  store's size means any new nogood (anywhere) re-opens all failed goals;
  the guard engages once the nogood set plateaus — which is exactly when
  the spinning starts (probe7: 3 stable nogoods over 8 iters).
- **`rung.execute` in isolation must contain `SubroutineReturnSignal`** —
  `return_early()` is caught by the executor's subroutine loop, so any
  analysis that executes a rung outside it (the projected oracle's
  candidate check was the one site) leaks the signal. Writes captured
  before the signal are exactly the real in-scan semantics.
- **Writer regression has a data-flow half.** Conditions name the gates;
  a `copy(SRC, tag)` writer's source-at-the-goal-value is an equal
  prerequisite. Both regression tools (the projected oracle, the static
  extractor) only carried the control-flow half until the copy-source
  arc; the handshake bundles had the concept first (transient sources).
- **The flattened plan must equal the executed work prefix.** A solved
  sub-goal's commits are on the work fork even when its parent goal
  later fails (boundary-unreachable conduits like `(Req, 4)` fail AFTER
  their children land the real work); dropping the subtree makes the
  Path lie and replay refuse. Failed nodes' own raw segments stay out.
- **Resting value can be path-dependent**: `C_CtrlCmd` rests at the last
  *valid* command (main R30 zeroes `C_CmdChgRequestBool` before R31's
  clear can fire; only invalid commands get cleared) — `_scan_transient_
  rest`'s refusal is correct, not conservative slack.
- **Cross-writer prereq union is a real budget sink** — merging
  `_unsatisfied_conditions` across all writers conjoins one writer's
  expensive requirements (Starting-SFC inits) with another's satisfied
  ones (Resetting); per-writer groups landed as Open Items #10.
- **Writer-groups fixture requirements**: the goal register must *step
  under a plain pulse* so it governs itself, and the cheap writer's gate
  must need more edges than the goal corridor has value transitions.
- **Indirect copy sources are not literals.** `copy(block[ptr], tag)`
  sources classify None (statically unresolvable); `_values_match` treats
  comparison TypeError as a non-match (premature refusal, never crash).
- **Reference-constant goals are a flood channel — and they are ND
  inputs, not init constants.** Zero program write sites → pipeline
  classifies nondeterministic input with full state alphabet. Each
  `(REF, v)` goal "solves" in one action by set-steering directly —
  goalpost-moving mutation. Classification: never-written tags read as
  copy/fill sources; ordering refs-last is completeness-neutral.
- **Slice elision beats functional-dep detection on jump-table scratch —
  fixed for walk mode by advice-grade projections.**
  `detect_functional_dependencies` originally considered only
  `stateful_dims` survivors; the template's pointer scratch
  (`sm__jump_target_ds_idx`, `isStateEnbl__mask_idx`,
  `isCmdValid__dh_base`…) is `elided=slice` first, so
  `functional_dep_projections` was EMPTY on the live template. Under
  `walk_only` the pass now also admits elided and ordering-violating
  candidates (the CopyOrJump loop rewrites the source after the scratch's
  writer, failing the sequential-scope check) and records them as
  **advice only** — no dims/elision bookkeeping, so prove behavior is
  bit-identical and BFS never sees them (walk_only runs no BFS). Live
  template: 7 projections, including `sm__jump_target_ds_idx =
  S_StateRequested + 150` and `isCmdValid__dh_base = C_CtrlCmd + 100`.
  The walker consults these first; the calc-expression fallback
  (`_single_calc_definition`) stays for non-affine single-tag shapes.
- **The live template's jump-target table was zero from cold — a
  twin-fidelity bug, FIXED 2026-06-12** (bank-aliasing unification:
  `map_to` stamps slot identity onto the banks; universal codegen slot
  emission carries name+default on the slot; one tag per register —
  the raw `DS165`-style keys no longer exist). Original symptom: `map_to`
  was metadata only; indirect reads (`ds[expr]`/`dh[expr]`) resolved
  through the raw block slot (default 0), a *different tag* from the
  semantic one (`sm__STATERESETTINGREF=15` while `DS115=0` in one
  snapshot) — the twin read all three PackML config banks as a blank ROM,
  and the `isCmdValid_Yes` budget frontier was an artifact of it
  (confirmed: dissolved on the regenerated template). Durable walker
  lesson: indirect reads resolve through the bank slot's identity, so
  twin fidelity for indirectly-read config is a codegen/tag_map property,
  not a walker property — when a frontier sits downstream of an indirect
  config read, check the config bank's values before trusting the
  refusal. Memory `tagmap-indirect-aliasing`.
- **Index registers can be literal-poor.** The template writes
  `S_StateRequested` only via `copy(sm__STATE*REF, …)` — the chase's
  candidate pool must include copy-from-tag writers' *source snapshot
  values* (the data-flow half applied to candidates), or table inversion
  is blind on exactly the PackML shape.
- **Program-state conjuncts can't be held — must-stay is detection +
  reorder.** Holds protect external inputs (the walker's own hand); a
  committed comparison goal on a *stateful* tag has no input to pin, so a
  later conjunct's corridor can silently break it (mode-resets-step
  shapes; the tumbler itself happens to be order-tolerant — both
  state+mode orders solve unaided). Pre-fix behavior: the break was caught
  only by the final replay verify → bare `None` → false "not reachable",
  no diagnosis. The threat taxonomy in §vocabulary ("threats detected by
  construction, not by clobber-recovery") holds for *input* holds only;
  stateful must-stays are the after-the-fact case by nature, and the
  reorder resolver — already in the vocabulary, previously unimplemented
  at the target level — is its repair.

---

## Open items / poke list

1. **Tier 3 convergence oscillation.** Cycle detection over (checkpoint,
   timing-guess) history; the current spin guard only catches
   identical-set-identical-state.
2. **Narrow-cut cardinality screening.** Screen on domain cardinality.
3. **Multi-corridor validation (partial).** Coupled subsystems with real
   handshake + deadline, walked with convergence repair (Tier 2/3).
4. **Input timing fragility.** Window characterization (D1) surfaces it;
   no further mechanism needed beyond visibility.
5. ~~Spin guard~~ — ✅ landed (`3d2ef01`); multi-corridor variant open with Tier 3.
6. **Seen-key fragmentation.** Mitigation if it bites: per-goal projection.
7. ~~Dead BFS code~~ — ✅ deleted; helpers in `sp_values.py`.
8. **(b) Or-gate writer-condition decomposition — BLOCKED on fixture.**
   `_extract_condition_values` drops Or-tags when branches constrain
   disjoint tags. Small programs solve via recovery oracle; the template's
   Or is satisfied from cold. Build the pre-fix NotFound fixture first
   (template snapshot without init's state-9/mode-3 seeds), then implement
   cheapest-branch Or decomposition.
9. **Recurring-obligation plan class (rotate pulse) — PARKED.**
   x_RotateSensor must toggle or the watchdog aborts at ~13s sim. Needs a
   periodic steer element. Ahead of it: search-shape cost (#11).
   Design direction (from concepts review, 2026-06-12): the periodic steer
   is not a separate mechanism — it is steer-history reuse (Future scope)
   stabilized into a cycle: same blocker, same fix, same interval, every
   recovery round. A promotion step detects that stability and schedules
   the steer proactively, converting reactive replay into a periodic
   obligation; `Physical(on_dwell=, off_dwell=)` then becomes a shortcut
   past discovery, not a prerequisite. Detection complement: a
   **multi-scan `cause()`** variant — blockers cleared in scan N and
   re-asserted in scan N+1 by a different writer are invisible to
   single-scan cause; the multi-scan trace names the period directly.
10. ~~Per-writer prereq groups~~ — ✅ landed (`256ff29`); corridor-level
    sibling cost folded into #11.
11. **REF-constant flood — levers (i) and (iii) ✅ landed; target ✅
    solved post-aliasing-fix.**
    (i) `ref_constant_order`; (iii) idx-chasing (2026-06-12, see
    §Idx-chasing arc): table inversion on the live snapshot at the
    copy-source binding site, calc-scratch hop (func-deps first,
    calc-expression fallback), copy-source candidate pool. Re-baselined
    on the regenerated (aliasing-fixed) template: the chase binds
    `(S_StateRequested, [4, 15, 17])` natively and
    `how(S_StateCurrent==4)` solves in 3.5s — the `isCmdValid_Yes`
    Tier-2 coupling hint is MOOT (artifact of the blank config ROM).
    (ii) goal-directed value ordering: PARKED, pressure relieved — no
    live target exhibits the sibling cost anymore; revisit only if a
    fixture demands it. Design note kept: the explore frontier already
    walks alternative value-graph routes when a transition dies — the
    gap is purely ordering, and generalized-nogood-pruned edges should
    feed it.
12. **Must-stay steer filtering — PARKED until a fixture demands it.**
    Compound-goal must-stay landed (2026-06-12) as post-goal detection +
    reorder at the walk root; the deeper lever is a must-stay monitor at
    the `_apply_steer_fold` seam (skip steers whose trial breaks a
    committed conjunct — same safe direction as hold conflicts: premature
    `None`, never a wrong plan), letting a single order route around
    clobbering steers where NO order works today. Reorder covers every
    current shape (mode-resets-step, mutual-clobber terminates honestly);
    build the fixture first: a target where each order's natural corridor
    clobbers the other conjunct but an alternative corridor preserves it.

---

## Research grounding

The individual mechanisms all have prior art. The novel contribution is
precisely scoped below.

### Prior art by mechanism

| Mechanism | Reference | Relationship |
|-----------|-----------|--------------|
| Corridor walk (directed forward search) | Directed model checking (Edelkamp, Lluch-Lafuente, Leue) | Heuristic-guided forward search over the executable system. Our PDB over the value graph is their abstraction-based heuristic. |
| Holds / protection intervals | POCL / causal-link planning: SNLP (McAllester–Rosenblitt), UCPOP (Penberthy–Weld) | A hold is a causal link; threats and the establish/reorder/divest/reject repertoire are causal-link threat resolution. We keep forward simulation — least-commitment ordering is deliberately not borrowed. |
| Agenda mechanics + nogood generalization | IC3/PDR (Bradley 2011; Eén–Mishchenko–Brayton 2011) | Deepest-first obligation queue, global budget, generalization by literal dropping — re-tested by forking instead of SAT. Frame/induction machinery not borrowed. |
| Triangle table | Fikes, Hart & Nilsson 1972 (STRIPS/PLANEX) | kernel(i) over holds+steps gives monitoring, windows, resume, operator output. |
| Helpful-steer ordering | FF helpful actions (Hoffmann–Nebel) | Applied via exact structure instead of delete-relaxation |
| Time-jump at crossings | Hidden-event acceleration / timed-automata event-driven simulation | |
| Causal diagnosis (`cause()`) | Halpern–Pearl actual causality; Beer–Ben-David–Chockler; causality checking (Leitner-Fischer, Leue) | They diagnose to explain; we diagnose to *act* (cause feeds back into the planner as a repair signal). |
| Regression sub-goals | System-R regression (Bonet, Geffner) | First unsatisfied subgoal, regress, progress through the achiever, repeat. |
| Nogood / precondition accumulation | Conflict-driven state-space search (Steinmetz–Hoffmann); CDCL (SAT) | The precondition set IS the no-good set. `cause()` replaces their conflict analysis (Algorithm 2). |
| Backjump to cause origin | Conflict-directed backjumping (CDCL / CSP); Steinmetz–Hoffmann for planning | |
| Factoring (causal graph decomposition) | Helmert causal graphs; star-topology decoupled search (Gnad–Hoffmann) | |
| Convergence / deadline diagnosis | Timed automata (Alur–Dill; UPPAAL); fault ascription (Leitner-Fischer, Leue) | Tier 2/3 feasibility checking |
| Lock-and-key / gadget-maze planning | Demaine, Hendrickson, Lynch; Hoffmann Grid benchmark | General problem PSPACE-complete; PLC programs are in the tractable subclass (one-state gates, readable locks, hierarchical ordering). |

### What's novel

The **closed loop** — actual-cause attribution (`cause()`) as the repair
signal in a solver-free forward planner over the executable program, aimed at
producing an operator-executable plan. The analyzer is Halpern–Pearl. The
planner is directed model checking with POCL bookkeeping. The regression is
System-R. Wiring them together without a solver, because the program is the
model: that's the contribution.

Classical planners need a solver to bridge the gap between model and reality.
When the model IS reality (forkable, per-rung steppable, deterministic), the
solver collapses to "try it and observe." The program runs on forks; `why()`
generates candidates by backward SP-tree attribution on the live state;
`cause()` validates by recorded-mode scan-log analysis; the walker steps
forward on the real interpreter.

### Key papers

- Steinmetz & Hoffmann (2016), *Towards Clause-Learning State Space Search*,
  AAAI — the conflict-driven learning loop; `cause()` replaces their Algorithm 2.
  https://fai.cs.uni-saarland.de/hoffmann/papers/aaai16.pdf
- Steinmetz & Hoffmann (2016), *State Space Search Nogood Learning*, AIJ —
  length-independent sound nogoods (trustworthy `Unsolvable`).
  https://www.sciencedirect.com/science/article/pii/S0004370216301448
- Steinmetz (2022), PhD thesis, *Conflict-Driven Learning in AI Planning
  State-Space Search* — convergence + trap learning.
  https://dblp.org/rec/phd/dnb/Steinmetz22.html
- Bradley (2011), *SAT-Based Model Checking without Unrolling* (IC3), VMCAI —
  the obligation queue + generalization-by-dropping we borrow as mechanics.
- McAllester & Rosenblitt (1991), *Systematic Nonlinear Planning* (SNLP) —
  causal links and threat resolution; the holds vocabulary.
- Fikes, Hart & Nilsson (1972), *Learning and Executing Generalized Robot
  Plans* — triangle tables / PLANEX execution monitoring.
- Lipovetzky & Geffner (2017), *Best-First Width Search* — novelty/memory bound
  (if a residual segment ever needs real search).
- Helmert (2006), *The Fast Downward Planning System* — causal graph
  decomposition, domain transition graphs.
- Timing/deadlines: timed-automata tradition (Alur–Dill; UPPAAL).
