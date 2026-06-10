# Corridor Walker — Living Plan

The single consolidated document for the walker: theory, vocabulary, what's
built, settled direction, and staged execution. Absorbs
`walker-consolidation-recap.md` and `walker-fable-feedback.md` (both deleted);
the original brief and mechanism catalog are retired. Update this file as
stages land.

**Status (2026-06-10):** the walker is the sole `how()` path and lives in its
own package, `src/pyrung/core/analysis/walk/` (own `CLAUDE.md` carrying the
walker contract and module map). Entry: `plan_walk`, called from
`PLC._how_via_walk` (`core/runner.py`). Tests:
`tests/core/analysis/test_walk_*.py` via `make test-walk` (85 tests); full
suite green (4251). Holds (prevention before recovery) landed. **Stages A–C
landed (consolidation complete):** A — per-walk threading bundled into
`_WalkContext`, `_JumpContext` built once per walk, `_probe_steps` memoized
per tag, fork/scan budget counter installed; B —
`_apply_steer`/`_apply_steer_compound` are thin adapters over
`_apply_steer_fold(done, monitor)`, the execution-monitoring seam; C — the
four solve loops dissolved into goal sources feeding one deepest-first
agenda (`_drive`), the plan tree born at solve time and flattened once at
Path build, budget exhaustion an honest NotFound, `_classify_blockers`
keeping nogoods program-facts-only, and `engine.py` split into modules along
the agenda seams (base / physical / fold / steer / priors / explore /
agenda / engine — map in `walk/CLAUDE.md`). **Stage D complete (3 of 4
landed; 1 blocked):** D1 triangle table (kernels, windows, divest points on
`Path.triangle`); D3 pass registry + ablation matrix (`walk/passes.py`,
advice/journal on `_WalkContext`, matrix in `test_walk_passes.py`); D4
third `_explore` exit + segment-chained backjump + `Diagnosis` on
`Path.diagnosis` (long-corridor capability tripwire), and the hold-blind
post-serial re-explore resolved hold-aware (suite-level A/B, zero shift).
D2 nogood generalization is ⛔ BLOCKED — probing showed the agreed tripwire
premise is structurally impossible against current `cause()` semantics (see
the D2 finding in Staging); two redesign leads recorded (fold
plateau-exclusion gap; `from_value` key variance). Walk suite at 123 tests.
**PAUSED at the post-D4 review checkpoint** — D2 redesign and Future scope
(dead BFS deletion, Tier 2/3, constructive regression) await review.

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
  break (inverse regression for OTE/latch); `avoid=` support.
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
- **Plumbing** — BFS/waypoint fallback removed: `how()` returns a verified
  `Path` or `Path(reachable=False)`; old BFS code behind `if False:` in
  `runner.py` pending deletion; prover pipeline context consumed via
  `allow_partial=True`; walker relocated to its own package with its own
  contract (`walk/CLAUDE.md`).

The structural debt this history left — and what the consolidation removes:
the same solve loop exists four times (`plan_walk` compound goals,
`_walk_to_goal` prereq tail, `_recover_via_oracle`, `_check_residuals`),
differing only in where goals come from; `_try_independent_walks` is inserted
at four sites; per-walk-immutable state is threaded by hand through eight
parameters (the dropped-`nogoods` bug, bitten twice, is this fragility's
signature failure).

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
| Narrowing | steer alphabet, cone filters | Must be conservative (over-approximate); disabling only widens |

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

## Staging

Four stages, ordered by risk and dependency. A, B, D are individually
land-able and revertible; C is the rewrite and cannot half-land. Each stage
states its **contract** — what the existing 85-test suite is allowed to do.

The test-contract rule used throughout: **verdicts, action counts, and holds
rendering are contracts; recovery-iteration counts are implementation echoes**
— allowed to shift with a one-line justification, with two exceptions that
must keep their *direction*: the A/B prevention contrasts (hold-aware needs
zero recovery iters where hold-blind must recover or fail), and the
oracle-backstop tripwires (cross-guard, serial-clobber) must still exercise
the recovery loop. If a stage accidentally prevents those clobbers, write new
programs that still require recovery — the backstop must stay covered.

### Stage A — context & build-once (mechanical) — ✅ LANDED 2026-06-10

Bundle the per-walk-immutable threading (pdg, program, known, ext_inputs,
edge_ext, nd_domains, explore_context, atom_index, domain_sources, nogoods,
holds) into `_WalkContext`; keep genuinely per-call values (work fork, goal,
depth, visited, budget remaining) explicit. Build `_JumpContext` once per
walk; memoize `_probe_steps` per tag. Install the global fork/scan budget
counter on the context now — nothing exhausts it yet, but Stage C's `NotFound`
trigger is then ready.

**Contract:** bit-identical — verdicts, action lists, iteration counts. No
test edits allowed. **Why first:** removes the parameter-threading failure
class outright and shrinks every later diff.

**Landed:** contract held — 85 walk + 672 prove + full 4251 green with zero
test edits. `_walk_to_goal` kept as a thin compat entry (old signature, used
by tests; builds the context once and delegates to `_walk_goal`); recursion
runs ctx-first (`_walk_goal`/`_walk_goal_inner`). One preserved quirk, now
explicit: the post-serial-prereq re-explore in `_walk_goal_inner` runs
hold-blind (`holds=None` — it predates holds and never received the store);
`_explore`'s `holds` is keyword-only so every call site declares its mode.
Stage C should decide whether that site stays hold-blind.

### Stage B — one fold monitor (mechanical-ish) — ✅ LANDED 2026-06-10

Collapse `_apply_steer` / `_apply_steer_compound` into one fold parameterized
by `done(state)`. No new monitors yet — just the seam they will plug into.

**Contract:** same plans on the suite; a fold count that legitimately shifts
gets a justification in the commit message. Separate from A/C because it is
independently revertible and makes the loop rewrite smaller.

**Landed:** contract held — same plans, no fold-count shifts (85 walk + 672
prove + full 4251 green, zero test edits). Core is
`_apply_steer_fold(ctx, runner, steer, done, monitor, react_cap, cap)`:
`done(state)` checks completion after every prefix segment and fold round;
`monitor(state)` picks the next `(tag, from_value)` for `_advance_time` (or
`None` = no progress). The old names stay as thin adapters documenting the
two monitor shapes (single-governing watch; sequential goal-list with the
anti-stall guard in the monitor closure).

### Stage C — the agenda loop (the rewrite) — ✅ LANDED 2026-06-10

The four solve loops dissolve into goal sources feeding one agenda:

- Items are `(flaw, depth, provenance)`; provenance names the goal source
  (target decomposition, writer SP-tree, oracle re-check, latch-break, threat
  detection).
- The four resolvers, with the uniform strategy order inside *establish*:
  satisfied-check → independence gate + merge-holds → corridor explore →
  sub-goal recursion → nogood + retry.
- `_classify_blockers` routes self-conflicts to divest/reorder; nogoods stay
  program-facts-only.
- The plan tree is born here; flatten once at `Path` build.
- Budget exhaustion returns honest `NotFound`.

**Contract:** verdict-level equivalence on all 85 — everything reachable stays
reachable with a verified plan; action counts and holds rendering match;
iteration-count shifts justified per the rule above. **Risk:** the one stage
that can't half-land; if it stalls, A and B still stand alone.

**Landed** as a three-commit series, contract held (85 walk + 672 prove +
full 4251 green at each step; action counts and holds rendering matched; no
iteration-count shifts):

- **C1 — the loop.** `_drive` is a frame stack of resolver pipelines
  (generators) yielding `_Request(goal, depth, provenance)` items —
  deepest-first by construction, stale items skip via the satisfied-check,
  budget checked before every resolver step, exhaustion unwinds to an honest
  budget-exhausted `Path(reachable=False)`. The skeletons became pipelines:
  `_solve_targets` (root), `_establish`, `_recover`, `_residuals`. The plan
  tree (`_PlanNode`) records commits chronologically; failed subtrees keep
  segments (for D4 diagnosis) but contribute nothing; `_flatten_plan` is the
  source of `Path` steps. Scheduler registers holds on goal-frame completion.
- **C2 — `_classify_blockers`.** Held-input blockers route to the recovery
  divest probe (`_divest_blocker`); a hold whose recorded goal is already
  broken is a dead causal link and releases immediately (the clobber case);
  live holds get an empirical fork-write-settle probe. Nogood keys now carry
  program facts only. Serial-clobber tripwire: same 3 recovery iters, same
  5-step plan, held `Input_A` resolved via divest instead of seen-key
  refinement on walker-hand state. Both tripwires still exercise recovery.
- **C3 — module split along the agenda seams.** `engine.py` (3,400 lines) →
  base / physical / fold / steer / priors / explore / agenda / engine
  (~200–1,000 lines each; map + dependency order in `walk/CLAUDE.md`).
  Only test edit of the whole consolidation: caplog targets the package
  parent logger (`pyrung.core.analysis.walk`) since moved code logs under
  per-module names; assertion contents unchanged.

The preserved hold-blind post-serial re-explore (Stage A note) is now an
explicit `holds=None` at the one call site in `_establish` — decide its fate
alongside D-stage work.

### Stage D — reach-extenders (independent, on the consolidated substrate)

- **D1. Triangle table.** ✅ LANDED 2026-06-10. First because it's free (data
  exists) and it validates that the plan tree carries what consumers need.
  Brings window characterization and divest-point rendering with it.

  **Landed:** `TriangleTable`/`TriangleRow` in `graph.py`, derived once at
  Path-build time (`_build_triangle_table`) from the flattened steps + the
  `HoldStore`'s new release journal (divests were previously log-only; the
  journal rolls back with `snapshot`/`restore` in speculative sections).
  Holds are matched to value runs of their input's write history —
  backwards, so the surviving hold claims the last matching run and divested
  holds claim earlier ones. `kernel(i)` = input conditions required at entry
  to step *i* (`kernel(n+1)` = the post-plan must-stay set);
  `highest_true_kernel(tags)` is the divergence resume point;
  `narrowest_window()` is the timing-fragility row; divest points render as
  a "Divests:" line on `str(path)` and the full table on
  `str(path.triangle)`. Monitoring/rendering output only — no walk decision
  reads it. Contract held: zero edits to existing tests; 13 new tests in
  `test_walk_triangle.py` (full suite 4264 green).
- **D2. Nogood generalization.** ⛔ BLOCKED 2026-06-10 — the agreed tripwire
  design rests on a false premise about `cause()`; deferred to the post-D4
  review checkpoint. The tripwire-first rule did its job: writing the
  tripwire before the mechanism exposed that the starvation it guards
  against cannot occur in the current architecture.

  **Agreed tripwire design (review checkpoint, 2026-06-10):** a target gated
  by a chain of interlocks where each recovery round's `cause()` blocking
  set includes a **noise dimension** — a free-running per-scan counter
  (`Cycle`) read by one enabling comparison — so every failure names a
  slightly different exact assignment (`{Guard_i=…, Cycle=17}`,
  `{…, Cycle=23}`, …). Exact-membership `is_blocked` never fires, seen-keys
  fragment, and the walk burns all recovery rounds → fails today. After
  drop-and-retest generalization, `Cycle` drops out (the re-test still fails
  without it), the `{Guard_i}` core recurs, pruning fires, and the walk
  solves. Assertions: before D2 the walk fails with
  `recovery_iters == _MAX_RECHECK_ITERS` and a fragmented store; after, it
  solves with strictly fewer rounds and at least one `is_blocked` hit.

  **Finding (2026-06-10, probes in `scratchpad/probe_d2_*.py`):** the noise
  dimension cannot reach the nogood store through `cause()`, for two
  structural reasons:

  1. *Noise can't be a blocker.* `projected_cause` classifies a leaf's
     needed value as a blocker only when that value was **never observed**
     in history (`_has_observed_transition`); otherwise it is proximate
     (a trigger), and proximate lists are discarded for blocked rungs. A
     free-running tag's values are continuously observed, so it is always
     proximate — and `needed_value = not cond_value` Booleanizes anyway, so
     `{Cycle=17}`-style assignments never exist. Every recovery round in
     the cross-guard/serial-clobber dynamics runs in **unreachable mode**
     (probe-verified, hold-aware and hold-blind), where only blockers are
     mined. Variance requires observedness; blocker status requires
     unobservedness — mutually exclusive.
  2. *Trigger-borne noise postdates the solve.* Projected-mode rounds (the
     only place a free-runner can be named, as a trigger) require every
     real fact already observed — i.e. they come after a round whose
     learned real-fact projection has already been re-explored. Any
     corridor solvable via projection + blocker-clearing solves on that
     earlier round; noise structurally cannot precede the solve. (Decoy
     constructions that force projected mode early were probed too: a
     viable decoy rung masks the real blockers permanently, killing the
     after-solve.)

  Corollary: the seen-key fragmentation D2 was to prevent cannot occur
  today either — volatile tag names cannot enter `blocking_tag_names()`.
  And exact `is_blocked` is not starved in practice: the probes show it
  firing on real re-derived configs (sets evolve with progress, which is
  tracking, not noise).

  **What the investigation surfaced instead (checkpoint material):**

  - *A real reach gap, in the fold:* any non-accumulator tag written every
    scan (heartbeat bit, free-running `calc` counter — common in real PLC
    programs) defeats the plateau guard (`_visible_items`), making
    time-folding unavailable program-wide; long dwells then exhaust the
    pulse react budget and corridors die (probe 1: walk fails on plain
    cross-guard + an unconditional `calc((Cycle+1)%2, Cycle)` rung with
    100 ms timers; works at 30 ms because dwells fit inside
    `_PULSE_REACT_CAP`). Candidate fix: extend the plateau exclusion set to
    self-referential-calc tags (`_calc_self_referential` already identifies
    them) the way accumulators are excluded — with the same monotonicity
    care `_nearest_skip` applies.
  - *A real exact-key variance channel:* `from_value` in the nogood key.
    For a multi-valued (counter-like) governing tag, recovery re-attempts
    record keys identical in blocking but differing in the drifting
    `from_value` — genuine store fragmentation. A redesigned D2 would
    generalize there (wildcard-from after a fork-and-run re-test shows the
    failure persists across drifting from-values), with a counter-valued
    tripwire target.
- **D3. Pass registry + ablation matrix.** ✅ LANDED 2026-06-10. Before more
  heuristics accrete. The journal it produces feeds D4's `Diagnosis`.

  **Landed:** `walk/passes.py` — `WALK_PASSES` registry where each pass
  declares its kind (`ordering` | `narrowing`), `run_walk_passes(program,
  pdg)` runs once per walk and freezes a `_WalkAdvice` + `_WalkJournal`
  onto `_WalkContext` (`advice`/`journal` fields; `None` = all-enabled,
  bit-identical pre-registry behavior). Initial population routes the
  existing alphabet advice through the registry: `cone_filter` (narrowing —
  disabled, every external Bool is a candidate), `steer_polarity`
  (narrowing — disabled, every candidate gets pulse *and* low; the
  conservative direction is both forms), `helpful_order` (ordering —
  disabled, sorted order). `_steer_alphabet` takes the keyword-only
  `advice` handle; the only door into the loop is that frozen value —
  passes get `(program, pdg)` only, no agenda/fork/store handles. The
  structural context builders (input/edge collection, jump context,
  harness exclusions) stay plain code; harness feedback exclusions are
  journaled as notes. Ablation matrix (`test_walk_passes.py`): one test
  parametrized over the registry × three walkable programs (cross-guard,
  shared-gate, seal-release), asserting by kind — plus alphabet-level
  superset/reorder units, registry-shape checks, and journal coverage.
  Contract held: zero edits to existing tests; 15 new (walk 113, prove
  672, full 4279 green).
- **D4. Backjump + third `_explore` exit + `Diagnosis`.** ✅ LANDED
  2026-06-10. The third exit (reached-governing-but-diverged, carrying a
  `cause()` payload) is the backtracking trigger; backjump = drop the
  diverged subtree + `fork(scan_id)` re-entry (each `_Node`'s fork is
  already the checkpoint — keep parent pointers instead of dropping on
  `popleft`); `Diagnosis`/`NotFound` read tree + holds + nogoods + journal.
  Last because it consumes everything the others build.

  **Landed:**

  - *Third exit* — `_explore_corridor` returns found / stuck (no steer
    moved the governing value, no clearing move fired: structural) /
    diverged (children existed; the deepest node IS the checkpoint — its
    `path` makes it self-sufficient, so no parent pointers were needed).
    `_explore` stays as the steps-or-None wrapper.
  - *Backjump* (`_backjump` in agenda.py — a resolver, not a loop) —
    speculative end-to-end: re-entry runs on forks of the diverged
    checkpoint with a detached plan node and a holds snapshot; nothing
    touches the work fork until a full re-entry succeeds, and adoption
    replays onto work with a checked landing (work may have drifted).
    Re-entry is a *fresh corridor search* from the checkpoint, chained up
    to `_MAX_BACKJUMP_SEGMENTS` (bounded like recovery's rounds), with one
    oracle-recovery shot from the deepest checkpoint when re-entry gets
    stuck. The reach-extension this buys: long value corridors beyond one
    `_explore`'s node/corridor caps are walked segment by segment — the
    capability tripwire (`test_walk_diagnosis.py`) walks a 25-step counter
    corridor that fails with the chain ablated
    (`_MAX_BACKJUMP_SEGMENTS=0`) and solves with it, replay-verified.
    Backjump fires only after today's failure paths are exhausted, so it
    only ever adds solutions.
  - *Diagnosis* (`Diagnosis` in graph.py, `_diagnose` in engine.py — a
    consumer, not a mechanism) — failed plan nodes carry
    `failure`/`blockers`; the builder reads tree + holds + nogoods +
    journal and distinguishes `unsolvable` (every failed leaf structural:
    explore-stuck / no-recovery-goals — a certificate, not a proof) from
    `not-found` (diverged, recovery-exhausted, bounds, or budget). Surfaces
    on `Path.diagnosis` for both the budget-exhausted Path and the
    walk-root failure (which now returns a diagnosis-carrying
    `Path(reachable=False)` instead of `None`); renders under
    "Unreachable:" on `str(path)`.
  - *Hold-blind decision resolved* (the Stage A/C open item): the
    post-serial re-explore in `_establish` is now **hold-aware** — the
    suite-level A/B showed zero behavioral shift either way (verdicts,
    iteration counts, holds rendering all unchanged; both oracle-backstop
    tripwires still exercise recovery), so prevention-before-recovery
    consistency wins. Pinned by `test_post_serial_reexplore_is_hold_aware`
    (no agenda explore may run hold-blind when a store exists).

  Contract held: zero edits to existing tests; 10 new (walk 123, prove
  672, full suite green).

D-items are independent; reorder if a real program blocks on one of them.

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
  mechanism.
- **Callable predicate (`expr=None`)** — one xfail: opaque predicates need
  expr decomposition or a try-after-walk adapter.
- **Dead BFS deletion** — old waypoint/BFS code behind `if False:` in
  `runner.py`, plus `waypoints.py` itself; delete once D4 lands. The SP-tree
  helpers the walker imports from `prove/waypoints.py`
  (`_written_value_for_tag`, `_extract_condition_values`,
  `_has_arithmetic_writer`, `_extract_required_values`) get a neutral home
  then.
- **Cheap trial** — `with plc.trial():` snapshot/restore instead of
  `fork()`-per-candidate, if fork cost ever dominates.

---

## Validation status

| Target | Corridor type | Steer | Result | Notes |
|---|---|---|---|---|
| `StateCurrent==EXECUTE` from ABORTED | mode machine | input pulses | walk ~2 s, replay→6 | go/no-go |
| `_CurStep==5` from EXECUTE | task timer wait | empty (folded) | walk, replay→5 | folded via dt-knob; old BFS = wrong "unreachable" |
| counter dwell 0→1 (synthetic) | per-scan counter | empty + pulse | folds via acc-patch | `test_walk` — up & down, exact landing, replay-verified |
| `how(Ready, Done)` (two-step latch) | compound And | input pulses | walk 3 steps, 0.0 s | Or/And decomposition |
| `y_Burner` from cold (nested) | 3-layer timer-gated | CmdMode + CmdStart + 2 folds | walk 5 steps, 1598 scans, ~1.3 s | recursive prereqs through 3 subroutine layers |
| `StateCurrent=="IDLE"` from cold | mode (string operand) | input pulses | walk 2 steps | simulation probe finds StateCurrent steps |
| inequality-gated transitions | analog/Int ND input | set-value | walk via pipeline domains | `nondeterministic_dims` steers |
| callable predicate (`expr=None`) | opaque | — | xfail | needs expr decomposition |
| linked feedback exclusion | Harness-driven fb | input steers | walk via enables | fb tags excluded from steer alphabet |
| `how(unlink=["Fb"])` fault | broken sensor | direct force | walk forces fb | bypasses physical chain delay; mirrored on verify fork |
| profile-gated (`Temp >= 5.0`) | analog ramp | hold + profile | walk ~500 scans | Harness ticks profile on fork |
| serial clobber (Latch_A/Latch_B share Input_B cone) | coupled latches | pulses + reset | walk recovers via oracle re-check | `test_walk_decomposition` |
| cross-guard mutual clobber | coupled latches + 2 timers | holds + reset | walk recovers, ≤2 recovery iters | `test_walk_nogood`; naive loop returned `reachable=False` |
| Int command protocol (Stopped→Idle→Execute) | multi-hop state machine | CmdReset + CmdStart pulses | walk 3 actions | `test_walk_real_patterns` |
| return_early() flow gating | subroutine flow control | Enable pulse | walk reachable | `test_walk_real_patterns` |
| rendezvous (two SFCs, simultaneous hold) | independent subsystems | multi-steer (Tier 1) | walk 2 actions, 30 scans | `test_walk_real_patterns` |
| odd/even step sequencer (CurStep%2 auto-advance) | self-increment + even skip | Advance pulse + fold | walk reachable | `test_walk_real_patterns` |
| deep call chain (Mode→State→SFC→Step→Output) | 5-level prereqs, 3 sub scopes | CmdProd + CmdReset + CmdStart + fold + Confirm | walk reachable | `test_walk_real_patterns` |
| holds prevention A/B | serial corridors sharing enables | holds + selective release | zero recovery iters (hold-blind must recover or fail) | `test_walk_holds`; divest, conflict-skip honesty, rendering |
| full suite | all types | all steers | test-walk 85 pass; test-prove 672 + 4 xfail | walker-only `how()` |

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

---

## Open items / poke list

1. **Tier 3 convergence oscillation.** The fixed-point iteration over cyclic
   coupling can limit-cycle if the timing-update map is non-monotone. Need
   cycle detection over (checkpoint, timing-guess) history; the current spin
   guard only catches identical-set-identical-state.
2. **Narrow-cut cardinality screening.** A two-tag interface carrying a
   Boolean plus a multi-valued channel looks narrow but behaves wide. Screen
   on domain cardinality. Not fatal (walk fails and cause explains), saves
   wasted attempts.
3. **Multi-corridor validation (partial).** Rendezvous validates independent
   subsystems. Still missing: coupled subsystems with a real handshake and
   deadline, walked including a convergence repair (Tier 2/3).
4. **Input timing fragility.** Plans assume inputs land on the planned scan;
   tight deadline windows could break. Window characterization (D1) surfaces
   it; no further mechanism needed beyond visibility.
5. **Spin guard (termination).** Nogood set unchanged + identical state +
   still failing = not an ordering problem; report the contradiction.
   Multi-corridor variant: all corridors individually solved but convergence
   infeasible after rescheduling = coordination contradiction.
6. **Seen-key fragmentation.** Shared add-only nogood store partitions
   `seen` for unrelated goals as blocking names accumulate. Mitigation if it
   bites: per-goal projection (§Nogood generalization).
7. **Dead BFS code** — delete after D4; relocate the waypoints SP-tree
   helpers then (§Future scope).

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
