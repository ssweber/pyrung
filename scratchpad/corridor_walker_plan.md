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
agenda / engine — map in `walk/CLAUDE.md`). **Stage D complete:** D1
triangle table (kernels, windows, divest points on `Path.triangle`); D3
pass registry + ablation matrix (`walk/passes.py`, advice/journal on
`_WalkContext`, matrix in `test_walk_passes.py`); D4 third `_explore` exit
+ segment-chained backjump + `Diagnosis` on `Path.diagnosis`
(long-corridor capability tripwire), and the hold-blind post-serial
re-explore resolved hold-aware (suite-level A/B, zero shift). D2 nogood
generalization was ⛔ BLOCKED (the agreed tripwire premise is structurally
impossible against current `cause()` semantics — finding preserved in
Staging) and was **redesigned and landed as four fold-churn rungs**
(2026-06-10, commits `bec2164`/`026b3f2`/`f1b2b41`/`6fcad05`):
unread-churn exclusion, target-disjoint churn-cone exclusion, affine(-mod)
self-calc churn as exact fold source, and derived crossings (acc-mirror
thresholds), under a new `fold` pass kind with a stated ablation
obligation (`tests/core/analysis/test_walk_fold_churn.py`). The
`from_value` key-variance lead stays deferred. Walk suite at 158 tests;
full suite green (4324).

**Hardening arc (2026-06-11):** the walker was run against the live
Tumbler/Dryer PackML template (78 main rungs + 33 subs, 6,055 kernel tags)
as a test-bed and the burner-loop findings' dominance order
(`scratchpad/burnerloop_findings.md` §2/§7–9) was knocked down
tripwire-first: **(d) explore cost** (fork tag-index handoff 95.5→5.6ms;
`set_value_relevance` narrowing; per-steer budget enforcement +
`how(walk_seconds=)` wall-clock knob), **(a) consumed-same-scan
handshakes** (transient detection with resting-value inference,
`ack_cleared_inputs`, recursive handshake bundles — new `widening` pass
kind), **(c) recovery spin guard**. Milestone: `how(S_UnitModeCurrent ==
1)` on the live template returns the ground-truth simultaneous pulse
`{C_ProductionMode, C_UnitModeChgRequest}` in 3.5s, replay-verified —
pre-arc this was a false `unsolvable` certificate. See §Hardening arc
below. Walk suite at 199; full suite green (4265). **(b) Or-gate
writer-condition decomposition is BLOCKED on a fixture** (see Open Items
#8) and the **recurring-obligation plan class (the x_RotateSensor toggle)
stays parked** (Open Items #9) — those two plus the C_CtrlCmd
state-command chain are what stand between the walker and the full
`y_BurnerLoop` plan. Still awaiting review: `from_value` nogood
generalization, Tier 2/3, constructive regression.

**Copy-source arc (2026-06-11, second arc):** the C_CtrlCmd question
dissolved — the chain needs no bundles (one ack-cleared pulse fires it
in-scan; `how(S_StateCurrent == 2)` solves in 3.7s). The real wall was a
**false `unsolvable` on `how(S_StateCurrent == 4)`**: Resetting completes
only in production mode, and three stacked defects kept the mode
prerequisite nameless — a `return_early()` leak crashing every projected
`cause()` on the state machine (`911fb23`), copy-source blindness in
`projected_cause` (same commit), and copy-source blindness in
`_unsatisfied_conditions` (`fad12ff`), whose fix exposed a plan-tree
honesty gap (`_flatten_plan` dropped solved sub-goals under failed
conduit goals while their work-fork mutations stayed — replay rightly
refused; same commit). See §Copy-source arc below and
`burnerloop_findings.md` §10. Walk suite 203 + causal 82; the
`S_StateCurrent == 4` walk now descends the real completion machinery
and exhausts budget honestly — the named next lever is **per-writer
prerequisite groups** (Open Items #10).

**Writer-groups arc (2026-06-11, third arc):** Open Items #10 ✅ LANDED
(`256ff29`) plus an indirect-copy crash fix it surfaced (`306616c`).
`_unsatisfied_condition_groups` returns per-writer alternatives
alongside the exact historical union; `_establish` walks the
smallest-unsatisfied group first, probing the corridor between groups —
ordering, never pruning (remainder group; `writer_prereq_groups`
ablation row restores the serial union; single-group flows reduce
exactly). Calibrated tripwire `test_walk_writer_groups.py` (grouped
solves at 60 forks, union needs ~124). The fix: `copy(block[ptr], tag)`
sources classified `("literal", IndirectRef)` crashed `_governing` on
truth-testing — now None (statically unresolvable), `_values_match`
hardened. On the template, `how(S_StateCurrent == 4)` no longer detours
into the Starting SFC: it walks the jump-state copy chain (first
failing goal `sm__where2jump -> 4`, an honest indirect dead end) and
the remaining budget sink is **recovery blocker-mining across the
`sm__STATE*REF` init-constant bank** (probe16; Open Items #11). See
§Writer-groups arc and `burnerloop_findings.md` §11. Walk 210, full
4285.

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
  `Path` or `Path(reachable=False)`; the dead BFS/waypoint code is deleted
  outright (2026-06-10: `runner.py`'s `if False:` block, `_try_waypoint_plan`,
  `_replay_trace`, and `prove/waypoints.py`; the surviving SP-tree
  value-extraction helpers live in `core/analysis/sp_values.py`); prover
  pipeline context consumed via `allow_partial=True`; walker relocated to
  its own package with its own contract (`walk/CLAUDE.md`).

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
| Narrowing | steer alphabet, cone filters, `set_value_relevance` (set-steer flood cap) | Must be conservative (over-approximate); disabling only widens |
| Fold (landed with D2) | the four fold-churn rungs | Each carries its own exactness argument; verify replay backstops; disabling regresses only in the refusing direction |
| Widening (landed with the hardening arc) | `ack_cleared_inputs`, `transient_handshake` bundles | Adds candidate steers/goals only; every addition validated by the interpreted trial; disabling regresses only in the refusing direction |

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
- **D2. Nogood generalization.** ⛔ BLOCKED 2026-06-10, then ✅ REDESIGNED
  AND LANDED 2026-06-10 as the four fold-churn rungs (see **Landed
  redesign** below). The original design rests on a false premise about
  `cause()`; the tripwire-first rule did its job: writing the tripwire
  before the mechanism exposed that the starvation it guards against
  cannot occur in the current architecture. The blocked finding is kept
  below as history.

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
    tripwire target. **Still deferred** — separate checkpoint item,
    untouched by the landed redesign below.

  **Landed redesign (2026-06-10):** the fold plateau-exclusion gap became
  the new D2 scope, executed tripwire-first as four rungs, one commit
  each, all in `tests/core/analysis/test_walk_fold_churn.py` (the D2
  probe programs became the tests; probe scripts deleted). New pass kind
  `fold` with its stated ablation obligation: each fold pass carries its
  own exactness argument and the step-by-step verify replay backstops all
  of them — disabling restores the stricter plateau guard, so churn-free
  programs keep verdicts and churn programs regress only in the refusing
  direction. One existing-test edit across all four rungs (justified in
  the rung-1 commit): the churning-pulse react-budget test's churner was
  unread, so it gained a reader to keep exercising the visible-churn
  backstop.

  - *Rung 1 — `fold_unread_churn`* (`bec2164`): an unconditional
    self-referential-calc tag with no readers outside its own writer
    rungs is unobservable; it leaves the plateau guard
    (`_JumpContext.churn_excluded`). Implicit fault flags tolerated only
    when read by nothing; Harness-referenced and goal tags never
    excluded. A futile wait is now recognized on the first plateau probe.
  - *Rung 2 — `fold_disjoint_churn`* (`026b3f2`): a *read* churner whose
    entire downstream closure (reader rungs' writes, transitively,
    through subroutine calls; return-early guards honored) is disjoint
    from the union of the targets' upstream cones leaves the guard with
    its closure. Divergence stays confined to the disjoint cone; no
    targets declared (direct callers) excludes nothing.
  - *Rung 3 — `fold_modwrap_source`* (`f1b2b41`): unconditional
    affine(-mod) self-calc churn (`calc((T + c) % m, T)` / `calc(T + c,
    T)`) read by enabling comparisons becomes an exact fold source —
    excluded from visible items, patched in closed form during jumps
    (`(v + (skip−1)·c) % m`, the landing step's own calc supplies the
    final increment, landings bit-equal to stepping), comparisons joining
    the crossing set via first-truth-flip arithmetic on the modular
    recurrence (`_ModWrap`, `_nearest_mod_flip`). Linear forms ride the
    per-scan `_AccSource` machinery as synthesized sources. Two forced
    consequences: *mod-wrap limit-cycle futility* (`_nearest_skip` split
    into `_nearest_acc_crossing` + `_nearest_mod_flip`; with no
    accumulator crossing upcoming, one full modular period with no
    visible change bails the advance — inert steers had been burning the
    4000-iteration guard, 42 s → 0.26 s on the conjunct tripwire), and
    *clocks don't govern* (`_governing` skips free-running self-calcs —
    value-stepping a 5000-node corridor is hopeless; the fold rides the
    climb instead).
  - *Rung 4 — `fold_derived_crossings`* (`6fcad05`): thresholds read
    through an unconditional `copy(Acc, X)` / `calc(Acc ± k, X)` mirror
    translate exactly onto the source (`X cmp T` flips at
    `Acc cmp T − k`) and the mirror leaves the guard. Conservative by
    construction: the mirror flips 0–1 scans after the source crossing,
    so stopping one-before the source crossing always stops before any
    mirror reader flips. Any unresolvable read (data/exclusive read,
    compound/opaque condition, mirror on the operand side, non-literal
    threshold, conditional/convert/oneshot writer) refuses the mirror —
    today's refusal preserved. Clock *views* are also skipped for
    governing. Mirrors of rung-3 sources translate the same way.

  Residual limits, recorded: a mod-m source's comparison flips bound
  jumps to <m scans, so dwells longer than `_MAX_ADVANCE_ITERS` scans
  under mod-wrap noise still exhaust the advance guard (granularity, not
  soundness); mirror chains (mirror-of-mirror) and scaled mirrors
  (`Acc * a`) refuse as today; non-affine self-calcs (`(T*3+1) % 7`)
  stay visible churn.
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

### Hardening arc — the PackML test-bed (2026-06-11) — ✅ (d), (a), (c) LANDED

The first sustained run against a real program (the Tumbler/Dryer PackML
template). The burner-loop findings (`scratchpad/burnerloop_findings.md`)
established the dominance order; each item fell tripwire-first. Six
walker-side commits (plus the user's `0564814` closing the prover
pipeline's classify cost, 49.5s → 6.4s):

- **(d) Explore cost** — `9c30548` `fork()` takes the parent's
  known-tags index instead of re-walking the program AST (92% of fork
  cost; 95.5ms → 5.6ms on the template — the Future-scope "cheap trial"
  item satisfied at the fork layer). `f6cb103` the `set_value_relevance`
  narrowing pass (program-wide cones put all 39 non-Bool ND inputs ×
  domains ≈ 300 set steers in every alphabet; enabling-named inputs keep
  full domains, the remainder caps at `_MAX_SET_VALUE_STEERS=24`);
  **budget checks moved inside the explore loop** (per steer trial — one
  establish could previously blow arbitrarily past every cap, which is
  also why a wall-clock cap was meaningless before); wall-clock knob
  `how(walk_seconds=)` / `plan_walk(wall_budget_s=)` /
  `_walk_to_goal(fork_budget=, wall_budget_s=)` with the overrun in the
  exhaustion reason. Calibrated reproducer `test_walk_budget.py` (solves
  at 131 forks with the pass, needs 635 ablated).
- **(a) Consumed-same-scan handshakes** — `7bd89ea`/`e95cc33`/`6ca17be`.
  The PackML protocol produces and clears its handshake registers within
  one scan, so boundary goals for them are structurally unreachable and
  poisoned recovery into FALSE `unsolvable` certificates. Four
  mechanisms, all in `priors.py` + tests in `test_walk_handshake.py`:
  `_scan_transient_rest` (static proof a tag rests at one value at every
  boundary — resting value inferred from the clearers, NOT the declared
  default: the template's `C_UnitMode` initializes to 5 but rests at 0;
  cross-scope clearers qualify via fires-when-set call gates, the
  `rung(ReqBool == 1): call(mode_change)` shape); `ack_cleared_inputs`
  (widening — HMI Bools the program only ever resets, including range
  resets, have writers so `TagRole != INPUT` and were not steerable AT
  ALL; 17 command bits on the template); `transient_handshake` bundles
  (widening — recursive requirement expansion through producer rungs,
  call gates, and transient copy-sources, depth-bounded/cycle-guarded,
  emitting one simultaneous multi-input patch; multi steers now pass
  non-Bool values through); boundary goals for transients skipped in
  `_unsatisfied_conditions` and `_governing` never delegates to a
  transient tag. Milestone: `how(S_UnitModeCurrent == 1)` from cold on
  the live template → 1 step, `{C_ProductionMode: True,
  C_UnitModeChgRequest: True}`, 3.5s, replay-verified.
- **(c) Recovery spin guard** — `3d2ef01`. Recovery rounds at every level
  recreate each other's goals (~3^depth re-walks; probe7: the same goals
  recovered at iters 1/4/6 and 3/5/8). The agenda records failed goals
  keyed by (goal, nogood-projected state, store generation); a
  re-request matching all three fails immediately. Loop machinery, not a
  pass (runtime learning stays out of the registry); `_SPIN_GUARD`
  module switch for the test A/B only. Generation dynamics: any store
  growth invalidates all records, so the guard bites once the nogood set
  plateaus — exactly the probe7 shape. `test_walk_spin_guard.py`.
- **(e) RTC churn — checked, no fix needed.** `A_PLCDT_*` (unconditional
  copy from the system RTC) is claimed by no fold rung and stays in
  `_visible_items`, but the plateau probe samples one scan per
  `_advance_time` iteration and the RTC ticks 1-in-100 scans at dt=10ms:
  a tick on a probe scan costs one react count, reset by every
  productive jump. Empirical: probe6 folded through the 10s HeatDelay
  dwell. Latent gap recorded: at dt≈1s the RTC ticks every scan and
  pulse-steer folds (react cap 6) die program-wide — park until a real
  program hits it.

Contract note: all existing tests held throughout (zero edits except
additions); both oracle-backstop tripwires still exercise recovery with
the spin guard on. Walk suite 199, full 4265.

### Copy-source arc (2026-06-11, second arc) — ✅ LANDED

The C_CtrlCmd state-command chain question (the first task of this
session) dissolved on probing: `sm_map_cmd2_val` runs unconditionally,
so a single ack-cleared C_* pulse fires the whole command→validity→
request→jump chain within one scan — **no bundle needed**, and nothing
in the chain classifies transient (correctly: `isCmdValid_Yes` is an
OTE; `C_CtrlCmd` has multi-scope writers and genuinely rests at the
last *valid* command value, since main R30 zeroes `C_CmdChgRequestBool`
before R31's clear can fire). `how(S_StateCurrent == 2)` from cold
solves in 3.7s with a plain `C_Clear` pulse.

The real defect cluster sat behind `how(S_StateCurrent == 4)` — a false
`unsolvable` certificate (the corridor parks at Resetting(15) in Manual
mode; completion is `production_states` R11, call-gated
`S_UnitModeCurrent == 1`). Three fixes, tripwire-first:

- **`return_early()` leak** (`911fb23`) — `_rung_produces_value`
  executes candidate writer rungs in isolation; `sm_copy_or_jump_state`
  R8 ends in `return_early()`, so `SubroutineReturnSignal` escaped
  through every projected `cause()` touching the state machine, and
  `_recheck_prereqs`' blanket except turned it into "no-recovery-goals"
  → false certificate. Contained at the execute site (writes captured
  before the signal are the real in-scan semantics); the swallowed-
  exception path now logs (`aca836b`).
- **Copy-source binding, oracle side** (`911fb23`) — a `copy(SRC, tag)`
  writer was a candidate only when SRC already held the asked value;
  the source-at-value is now classified like a contact (enabling /
  proximate trigger / `BLOCKED_UPSTREAM` blocker) — the data-flow half
  of writer regression, benefiting every `cause(to=)` consumer.
- **Copy-source binding, static side + flatten honesty** (`fad12ff`) —
  `_unsatisfied_conditions` now merges `(source, goal_value)` for
  copy-from-tag writers (same snapshot + transient filtering, so
  consumed-same-scan sources stay out — bundles' territory, pinned by
  test). Landing it exposed that `_flatten_plan` dropped failed
  subtrees wholesale: sub-goals *committed to the work fork* (the mode
  bundle, corridor pulses) sat under boundary-unreachable conduit goals
  that later failed, so the Path lied about the executed prefix and
  replay refused. Flatten now descends failed nodes for their solved
  descendants; failed nodes' own raw segments stay out; replay stays
  the arbiter.

Tripwires: `test_walk_copy_source.py` (4 — distilled jump-state machine
walks end-to-end; binding + transient-filter units),
`TestProjectedCauseCopyWriters` (3, in `test_causal_prospective.py`).
One existing causal test repurposed with justification (copy-from-tag
at the wrong current value is now honestly projected). Walk 203, prove
549, causal 82 — zero other edits.

**Post-arc frontier** (probe14d, `S_StateCurrent == 4` @240s): honest
budget-exhausted NotFound *on the right chain* — holds `C_Clear` (for
`S_StateCompleteBool`) and `C_Reset` (for `S_Resetting`), best partial
plan 10 steps. The budget burns on search shape, not mechanism:
`_unsatisfied_conditions` returns the cross-writer UNION of prereqs
(production_states R3's `Blower__init`/`Rotate__init` ride along though
R11 alone suffices from Resetting → the walk depth-bounds inside the
Starting SFC), and the corridor explores irrelevant 15→10/12/13
branches. Next lever: per-writer prerequisite groups (Open Items #10).

**Where the full `y_BurnerLoop` walk stands after the arc** (probe13,
`walk_seconds=120`): honest wall-clock NotFound, but the mode-change
handshake is now established *inside* the walk (holds at failure:
`C_ProductionMode=True` for `S_UnitModeCurrent`); first failing goal
`S_CurrStep_Dry` (no-recovery-goals), nogoods name `HeatDelay_Tmr_Done` /
`S_StateCurrent`. Three things remain between the walker and the full
plan, in expected order: the **C_CtrlCmd state-command chain**
(C_Clear → C_Reset → C_Start through `sm_ctrl_cmd2_state_request` — the
same handshake shape; check whether the landed machinery already fires it
and why not before building anything), the **(b) Or-gate** (Open Items
#8 — blocked on a fixture), and the **recurring-obligation plan class**
(Open Items #9 — the x_RotateSensor toggle; no static plan can survive
the rotate watchdog without it, so it caps any full plan at ~13s sim).

### Writer-groups arc (2026-06-11, third arc) — ✅ LANDED

Open Items #10 executed tripwire-first, two commits:

- **Per-writer prerequisite groups** (`256ff29`).
  `_unsatisfied_condition_groups` (priors.py) splits the extraction per
  matched writer — gate values, the copy-source binding, call-gate
  conditions, that writer's inequality prereqs — alongside the exact
  historical union (`_unsatisfied_conditions` is now a thin wrapper
  over it; the union's order/dedup semantics are reproduced
  bit-identically). Groups are genuine alternatives: fully satisfying
  any one group arms that writer. `_establish` orders groups
  smallest-unsatisfied-first, walks them with cross-group dedupe, runs
  the independent-fork attempt per group (not on the union), and
  probes the corridor between groups so a satisfied alternative ends
  the walk before an expensive sibling chain ever spawns sub-goals.
  Completeness posture: ordering only — pairs not covered by any group
  ride in a final remainder group; the `writer_prereq_groups` ordering
  pass ablates back to the serial union; a single group reduces to the
  previous flow exactly (the checkpoint fork is now lazy, taken before
  the first serial sub-request). Tripwire
  (`test_walk_writer_groups.py`, calibrated like `test_walk_budget`):
  two-writer goal — counter-latched inits (25 edges each) vs. a
  four-pulse stage corridor — grouped solves at a 60-fork budget (~22
  needed, 7-action plan through the cheap writer), ablated union
  exhausts it (~124 needed, 49-action plan through the expensive one),
  union still solves unbounded. Fixture lessons recorded in findings:
  the goal register must step under a plain pulse so it governs itself,
  and the cheap gate must need more edges than the goal corridor has
  transitions (else BFS ride-along solves it without prereqs).
- **Indirect-copy crash fix** (`306616c`, surfaced by the new ordering
  the moment it walked into `sm_copy_or_jump_state`'s machinery).
  `_written_value_for_tag` classified `copy(ds[idx], tag)`'s
  IndirectRef source as a "literal"; comparing it to a goal value
  builds an `IndirectCompare*` Condition that raises on truth-testing —
  phase C crashed in `_governing`. Non-tag non-scalar sources now
  return None (statically unresolvable; the projected oracle still
  executes such rungs), `_literal_write` gets the same refusal, and
  `_values_match` treats comparison TypeError as a non-match.
  Tripwires: `TestWrittenValueIndirect` (sp_values),
  `test_indirect_copy_writer_walks_without_crashing` (walk-level).

**Post-arc frontier** (probe16, `S_StateCurrent == 4`, walk-debug log):
the Starting-SFC detour is gone (no Blower/Rotate nogoods); the right
first leg lands by t=25s (corridor to 15 in 4 actions, holds C_Clear +
C_Reset); R8/R11 copy-source bindings spawn `(S_StateRequested, 4)` →
`(sm__where2jump, 4)`, which dies honestly (indirect writer — no static
prereqs, no recovery goals) and is the recorded first failing goal. The
budget now burns in **recovery blocker-mining across the
`sm__STATE*REF` constant bank**: cause() names the REF registers as
blockers, each becomes a goal, and each "solves" in 1 action because
pulsing `Test_Simulate_1st_Scan` re-runs the init loads — sound but
operator-meaningless detours at ~45ms/fork, with the spin guard only
catching repeats once nogoods plateau (14 hits between t=84s and
t=120s). Next levers in Open Items #11.

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
- **Dead BFS deletion** — ✅ DONE 2026-06-10. Deleted: `runner.py`'s
  `if False:` BFS fallback, `_try_waypoint_plan`, `_replay_trace` (only
  dead-path callers), and `prove/waypoints.py` (~2,300 lines) with its
  machinery test file. The four SP-tree value-extraction helpers moved to
  their neutral home `core/analysis/sp_values.py` (imported by walk/priors
  and prove/seeding; helper unit tests in `test_sp_values.py`). The
  how()-behavior tests from the waypoint era — which exercise the live
  walker — moved unchanged to `test_walk_how_e2e.py`. Note: the prover's
  own `prove/bfs.py` (`_bfs_explore`) is the verifier's engine and is
  untouched.
- **Cheap trial** — ✅ effectively satisfied 2026-06-11 at the fork layer
  (`9c30548`): `fork()` reuses the parent's tag index instead of
  re-walking the program AST, 95.5ms → 5.6ms on the 6k-tag template. A
  true `with plc.trial():` snapshot/restore remains possible if the
  residual ~5ms ever dominates again.
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
| set-value flood (30 noise ND inputs) | 3-step Mode corridor | multi + pulses under fork budget | solves at 131 forks; ablated needs 635 | `test_walk_budget`; wall-clock knob honest exhaustion |
| consumed-same-scan handshake | mode-request protocol | simultaneous bundle {Req, ModeSel=2} | walk 1 step | `test_walk_handshake`; pre-fix = FALSE unsolvable |
| PackML chain (ack-cleared + call gate + copy-source, rest=0/default=5) | 2-level transient regression | bundle {ChgReq, ProdMode} | walk 1 step | `test_walk_handshake`; both widening passes load-bearing |
| **live template** `S_UnitModeCurrent==1` from cold | real PackML mode change | bundle {C_ProductionMode, C_UnitModeChgRequest} | walk 1 step, 3.5s | probe11; replay-verified ground-truth pulse |
| circularly-dead prereq shared by 3 parents | spin-guard shape | — | honest NotFound, recovery iters strictly drop | `test_walk_spin_guard`; learn-then-retry still solves |
| **live template** `S_StateCurrent==2` from cold | C_CtrlCmd command chain | pulse C_Clear | walk 2 steps, 3.7s | probe14; no bundle needed — ack-cleared pulses fire the chain in-scan |
| live template `S_StateCurrent==4` from cold | mode-gated completion | — | honest budget NotFound on the right chain (was FALSE unsolvable) | probe16 @240s; holds C_Clear/C_Reset, leg to 15 in 4 actions by t=25s; Starting-SFC detour gone post-#10; blocked on the REF-constant recovery flood (Open #11) |
| copy-source chain (distilled jump-state machine) | mode handshake → completion → state copy | Adv pulse + {ProdMode, ChgReq} bundle | walk reachable, replay-verified | `test_walk_copy_source`; pre-fix false unsolvable |
| two-writer goal (cheap stage vs. counter-latched inits) | writer disjunction | AdvB pulses + Kick | grouped solves @60-fork budget (~22 needed); ablated union exhausts (~124) | `test_walk_writer_groups`; Open #10 tripwire |
| indirect-copy writer (`copy(blk[ptr], Dest)`) | statically unresolvable write | — | honest unreachable, no crash | `test_walk_copy_source`; pre-fix TypeError in `_governing` |
| live template `y_BurnerLoop` from cold | full chain | — | honest wall-clock NotFound @120s | probe13; mode handshake established in-walk; blocked on Open #11 + rotate toggle |
| full suite | all types | all steers | test-walk 210 pass; full 4285 | walker-only `how()` |

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
  Path lie and replay refuse. Failed nodes' own raw segments may be
  never-applied explore traces — those stay out.
- **Resting value can be path-dependent**: `C_CtrlCmd` rests at the last
  *valid* command (main R30 zeroes `C_CmdChgRequestBool` before R31's
  clear can fire; only invalid commands get cleared) — `_scan_transient_
  rest`'s refusal is correct, not conservative slack.
- **Cross-writer prereq union is a real budget sink** — merging
  `_unsatisfied_conditions` across all writers conjoins one writer's
  expensive requirements (Starting-SFC inits) with another's satisfied
  ones (Resetting); the agenda walks the union serially (probe14d:
  depth-bounds inside the Starting SFC while R11's branch was one call
  gate away). Per-writer groups landed as Open Items #10 (`256ff29`).
- **Writer-groups fixture requirements** (what the tripwire taught):
  the goal register must *step under a plain pulse* so it governs
  itself — otherwise `_governing` delegates to the richest writer gate
  and the prereq path never engages; and the cheap writer's gate must
  need more edges than the goal corridor has value transitions, or the
  BFS solves it by ride-along (one multi-pulse per corridor node moves
  both the gate and the goal).
- **Indirect copy sources are not literals.** `copy(block[ptr], tag)`
  made `_written_value_for_tag` return `("literal", IndirectRef)`; an
  IndirectRef's `==`/`!=` builds a deferred Condition that raises on
  truth-testing, so the first comparison against a goal value crashed
  the walk (`_governing`, phase C). Non-tag non-scalar sources now
  classify None — statically unresolvable, the interpreted oracle still
  executes the rung — and `_values_match` treats comparison TypeError
  as a non-match (the contract is premature refusal, never a crash).
- **Init-constant goals are a recovery flood channel** (probe16) —
  cause() blocker-mining names the `sm__STATE*REF` reference registers;
  each becomes a walk goal, and each "solves" in one action because
  pulsing `Test_Simulate_1st_Scan` re-runs the init loads — sound but
  operator-meaningless, ~45ms/fork on the 6k-tag template, spin guard
  engaging only after nogoods plateau. The pipeline already knows these
  tags (`init_constant_projections`, richness 1) — ordering material,
  Open Items #11.

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
5. **Spin guard (termination)** — ✅ LANDED 2026-06-11 (`3d2ef01`, the
   single-goal form): failed goals keyed by (goal, nogood-projected
   state, store generation) fail fast on re-request. The multi-corridor
   variant (all corridors individually solved but convergence infeasible
   after rescheduling = coordination contradiction) remains open with
   Tier 3.
6. **Seen-key fragmentation.** Shared add-only nogood store partitions
   `seen` for unrelated goals as blocking names accumulate. Mitigation if it
   bites: per-goal projection (§Nogood generalization). Note the spin
   guard shares the same projection — fragmentation would weaken both.
7. **Dead BFS code** — ✅ deleted 2026-06-10; SP-tree helpers relocated to
   `core/analysis/sp_values.py` (§Future scope).
8. **(b) Or-gate writer-condition decomposition — BLOCKED on a fixture.**
   `_extract_condition_values` keeps an Or-tag only when every branch
   constrains it, so `Or(S_Idle, S_Stopped, S_Aborted)` (disjoint tags
   per branch) vanishes from prereqs and no state-machine goal is
   spawned. BUT: small Or-gate programs solve today via the recovery
   oracle (probe_orgate/orgate2), and the current template has the Or
   satisfied from cold (init seeds S_StateCurrent=9 → S_Aborted true).
   The §2b evidence came from the PRE-fix template. Per the
   tripwire-first rule: build the pre-fix NotFound fixture first (the
   template snapshot WITHOUT init's state-9/mode-3 seed rungs —
   burnerloop_findings §6), watch the walk fail on it, then implement
   cheapest-branch Or decomposition in `_unsatisfied_conditions`
   (mirroring `_extract_goals`'s goal-level Or policy). Do not implement
   without the fixture.
9. **Recurring-obligation plan class (the rotate pulse) — PARKED, now
   load-bearing.** x_RotateSensor must toggle (on-dwell <2s, off-dwell
   <10s) or the rotate stuck-sensor watchdogs abort at ~13s sim — before
   Heat_xCall at ~18s. No static hold satisfies a periodic obligation, so
   every full `y_BurnerLoop` plan is capped at the watchdog regardless of
   other progress. This needs a new alphabet/plan element (a periodic
   steer: `(input, period_on, period_off)` held for a corridor's
   duration, realized as repeating actions in the Path and folded
   compatibly — the fold must treat the toggle as scheduled patches, like
   harness pending patches already constrain jumps). Park was conditioned
   on (d)/(a)/(b): (d)+(a) landed, (b) is fixture-blocked. The C_CtrlCmd
   chain question is ANSWERED (copy-source arc: ack-cleared pulses fire
   it, no bundle; the state corridor now walks the right chain) — what
   remains ahead of the toggle is the search-shape cost, now #11
   (#10 landed).
10. **Per-writer prerequisite groups (writer disjunction)** — ✅ LANDED
    2026-06-11 (`256ff29`; §Writer-groups arc; tripwire
    `test_walk_writer_groups.py`, calibrated 60-fork budget). The
    Starting-SFC detour on the template is gone (probe16: no
    Blower/Rotate nogoods). The corridor-level sibling cost
    (goal-directed value ordering — the 15→10/12/13 branches) was NOT
    built here; it is folded into #11's lever list.
11. **Recovery goal flood on init-constant tags (the `sm__STATE*REF`
    bank) — the named next lever (2026-06-11, probe16).** Post-#10,
    `how(S_StateCurrent == 4)` walks the right chain and then burns the
    budget in recovery blocker-mining: cause() names the PackML
    state-reference registers as blockers, each becomes a goal, each
    "solves" in 1 action (pulsing `Test_Simulate_1st_Scan` re-runs the
    init loads) — sound, operator-meaningless, ~45ms/fork, spin guard
    engaging only after nogoods plateau (14 hits in the probe16 tail).
    Candidate levers, in suspected order: (i) order blocker-mined
    recovery goals on pipeline init-constants LAST
    (`init_constant_projections` richness 1 — an ordering pass over
    recovery goal order, completeness-neutral); (ii) goal-directed
    value ordering in the corridor (the 15→10/12/13 cost carried over
    from #10). Tripwire first: distill a REF-constant-flood fixture (a
    goal whose recovery names a bank of init-loaded constants, with a
    first-scan re-trigger bit making them technically writable) and
    watch the walk burn budget on it before building (i). Also seen in
    the probe16 log: an `isCmdValid_Yes` Tier-2 coupling hint
    (`isCmdValid__result`/`C_CmdChgRequestBool`, ~200-tag shared cone).

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
