# Corridor Walker — Plan

---

## Theory statement

The corridor walker rests on a provable structural argument, not just an
engineering bet.

A single-scan, no-interrupt PLC program is a deterministic function from
(state, inputs) -> state'. PLC programs are producer-consumer hierarchies of
sequential corridors coupled through narrow handshake interfaces (ISA-88,
PackML, IEC 61131-3 SFC enforce this by design). The program is its own
executable model -- forkable, steppable, fully observable. Forward progress is
ground truth (step and observe). Backward structure is exact (read the SP-tree
via `simplified()`/`cause()`/`why()`). To solve a reachability goal: factor
into subsystems via the coupling structure, walk each corridor forward using
backward structure to steer and recover, force coupling signals to decouple
timing, verify feasibility by summing achieved depths against handshake
deadlines.

This is lock-and-key maze solving in a structurally tractable slice: most gates
are one-state (interlocks -- polynomial), the gates are readable
(simplified/cause -- no search needed for key identification), and the timed
gates decompose (producer-consumer, not adversarial). The general gadget-maze
problem is PSPACE-complete (Demaine, Hendrickson, Lynch); PLC programs are in
the easy subclass because the standards enforce simple locks, readable
conditions, and hierarchical key ordering.

**Bounded-width claim (Lipovetzky-Geffner serialization).** The corridor
decomposition serializes the problem: each sub-problem's atomic width is
bounded because ISA-88/IEC 61131-3 gates involve O(1) variables. The
governing-value x blocking-key `seen` set is an implicit novelty measure
over this serialization. Completeness is structural for serializable
instances (bounded width), budget-gated for the rest.

**Scope constraint:** single-scan PLC without interrupts. Multi-task PLCs with
priority-based preemption (S7-1500 OBs, ControlLogix periodic/event tasks)
break the deterministic-order guarantee and are out of scope. Extension would
require modeling interrupt semantics as additional nondeterminism.

---

## The planner, in POCL vocabulary

The walker is a **hierarchical planner** with three layers:

1. **Abstract** -- collapse the full PLC state space to one governing tag's
   value graph (tiny: mode machines have single-digit values).
2. **Plan** -- best-first search over that abstract value space (cheap).
3. **Refine** -- for each abstract edge, find concrete inputs via interpreted
   simulation on forks (sound by construction -- immune to static-analysis
   blindness).

Its working vocabulary is **partial-order causal-link (POCL) planning**,
executed forward on the real interpreter:

- An **open condition** is an unachieved goal `(tag, value)` -- raised by
  target decomposition, a writer's enabling condition, a residual, or an
  oracle re-check.
- A **hold** is a causal link: (external input, value, the committed goal that
  depends on it) -- a protection interval over the walker's *own hand*.
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
stale-item skipping, and nogood generalization by drop-and-retest.

Self-conflicts stay out of the program's ledger: `cause()` explains the
program; holds are the walker's hand. A cause()-named blocker that is a held
input is classified one layer up (`_classify_blockers(goals, holds)`) and
routed to divest or reorder -- it never enters the `NoGoodStore`. Nogoods
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

- Anything readable from the SP-tree or PDG -> ordering advice via the pass
  registry.
- Anything knowable only by running -> learned nogood in the loop.
- Every new mechanism is a resolver, a flaw source, or a pass -- **never a new
  loop**.

**Static analysis is a prior, never correctness-bearing** -- it picks the
governing tag, narrows the steer alphabet, and sets the horizon. Correctness
comes from simulation. Validation is always interpreted.

### The oracle advantage

Unlike classical planners (which reason over a symbolic model that may diverge
from reality), the corridor walker operates on **the program itself as a
white-box oracle**: forkable, per-rung steppable, with full observability of
what was read/written. There is no abstraction gap -- simulation IS the
program. This shapes the architecture:

| Layer | Role | Tools | Properties |
|-------|------|-------|------------|
| **Generate candidates** | Narrow the search space | `why()` (state-aware minimal), `simplified()` (structural), PDG (coarse) | Heuristic -- may over-/under-generate |
| **Forward exploration** | Try candidates | `fork()` + step (the walker engine) | Ground truth -- deterministic, sound |
| **Validate / explain** | Confirm cause or diagnose failure | `cause()` on scan log | Recorded truth -- what actually happened |

The symbolic layer generates candidates; the interpreted layer validates them.
No CEGAR loop needed because the "refinement check" runs the real program --
spurious abstract paths are caught in one step, not iteratively refined away.

**Candidate generation tools (finest -> coarsest):**

- **`why(tag)`** -- backward SP-tree attribution from a snapshot. Gives the
  *minimal load-bearing contacts* explaining the current value. State-aware:
  prunes irrelevant formula branches given the actual fork state. Use when you
  need "what's holding this tag HERE" (steer prioritization, regression
  sub-goals, factoring).
- **`simplified(condition)`** -- resolved Boolean form to input-level. Structural:
  all paths through the formula regardless of current state. Use when you need
  "what COULD make this true/false" (full regression, enabling-condition
  analysis).
- **PDG** -- `upstream_slice`, `writers_of`, `condition_reads`. Coarsest static
  connectivity. Use for cone-narrowing, solve-order proposals, independence
  screening.

**Validation tool:**

- **`cause(tag)`** -- recorded-mode causal analysis on the scan log. Gives
  trigger vs. enabler split: what *transitioned* the tag vs. what was already
  in place. Use after simulation to confirm which inputs were load-bearing, to
  extract nogoods from failures, and to produce actionable diagnosis.

---

## Settled architecture

Static advice in through a pass registry, one agenda loop in the middle,
verified plans and a triangle table out. One deepest-first agenda of flaws
(open conditions + threats), four resolvers (establish, reorder, divest,
reject), plan tree flattened to `Path` at build time, `TriangleTable` /
`kernel(i)` derived from holds + steps (Fikes-Hart-Nilsson 1972 / PLANEX),
`Diagnosis` reading the plan tree + nogoods + journal to distinguish
`Unsolvable` from `NotFound`. Passes are registered, frozen before the walk,
and ablation-tested by kind.

Writer alternatives are carried as structured candidates, not bare
unsatisfied-condition lists: each candidate keeps its full enabling context,
the already-satisfied/live branch guards, the unsatisfied sub-goals, and the
writer's static write footprint. The `context_aware_groups` ordering pass uses
that context to prefer writers aligned with active must-stay state, defer
possible write conflicts, and promote the selected satisfied guards into child
`_StepMonitors`. The old per-writer group projection remains as an ablation
path and compatibility API.

### Nogood generalization (the open direction)

The next generalization step is PDR-shaped: after learning a failure, drop
facts and re-test -- the simpler version that still fails is the real nogood.
PDR needs a SAT solver for the re-test; the walker forks and runs. Broader
nogoods prune more on deep interlock chains. This is the one borrowed idea
that extends reach on harder programs rather than cleaning code.

Residual risk: the store stays shared per `plan_walk` and add-only, so
accumulated blocking names fragment `seen`-keys for *unrelated* goals.
Relation facts shrink scalar over-specialization but do not scope the store.
If fragmentation still bites: project per-goal -- only nogoods whose `(from,
to)` involves the current governing tag.

For counter-like governing tags, recovery records nogoods identical in blocking
but differing only in the drifting from-value. Drop the from-value and re-test
on a fork at a different from-value; if the failure persists, wildcard it -- one
generalized nogood replaces N exact ones. Tripwire: a counter-valued governing
tag where recovery accumulates redundant exact-key nogoods.

### Regression-triggered protective holds

When a committed progress goal regresses after a child frame completes,
`_check_progress_regression` (always-on, not debug-gated) traces the
actual cause via `cause()` -> `_walk_chain` to external-input roots, and
installs their pre-regression values as protective holds. The steer
release prefix then skips those inputs on subsequent corridors. Ownership
boundary: target-decomposition frames are handled by `_solve_targets`'
reorder loop; all deeper frames use the regression-hold path.

Mechanism (in `rules.mine_regression_holds`):
1. `work.cause(regressed_tag)` -- actual-cause chain of the regression
2. `_walk_chain` -- drill to external-input roots via `_is_actionable_root`
3. `root.from_value` -- the pre-regression value is the protective hold
4. `holds.protect(name, from_value, regressed_goal)` -- registered in `_drive`
5. `frunner.patch(protective_values)` + step -- immediate fix on the work fork

### Constructive regression (frontier-terminated why)

When explore, static prerequisite extraction, and oracle recovery all come up
empty, the agenda falls back to frontier-terminated `why()` on the work fork.
`why_cause` grows an optional `frontier` predicate: backward SP-tree
attribution terminates at any tag the walker can act on (ext inputs, edge ext,
multi-value ND domains, already-committed goals) rather than only at external
inputs. The conjunctive roots are the nearest actionable sub-goals -- being
state-aware AND structural, the walk follows the live branch of Or-gates that
the static extractor drops. Goals that would flip a protected hold are
filtered via `HoldStore.filter_conflicting`.

### Debug trace (`how(debug=True)`)

Structured event collector threaded through `_WalkContext.debug_sink`.
When enabled, captures: PDG upstream cone snapshots, oracle chain dumps,
goal lifecycle (start/resolved/failed with depth and provenance), hold
registrations, and budget exhaustion. Attaches to `Path.debug_trace`;
renders via `str()`. Zero cost when `debug=False`.

---

## Validation status

| Target | Corridor type | Steer | Result | Notes |
|---|---|---|---|---|
| `StateCurrent==EXECUTE` from ABORTED | mode machine | input pulses | walk ~2 s, replay->6 | go/no-go |
| `_CurStep==5` from EXECUTE | task timer wait | empty (folded) | walk, replay->5 | folded via dt-knob |
| counter dwell 0->1 (synthetic) | per-scan counter | empty + pulse | folds via acc-patch | exact landing, replay-verified |
| `how(Ready, Done)` (two-step latch) | compound And | input pulses | walk 3 steps, 0.0 s | Or/And decomposition |
| `y_Burner` from cold (nested) | 3-layer timer-gated | CmdMode + CmdStart + 2 folds | walk 5 steps, ~1.3 s | recursive prereqs through 3 sub layers |
| `StateCurrent=="IDLE"` from cold | mode (string operand) | input pulses | walk 2 steps | simulation probe finds steps |
| inequality-gated transitions | analog/Int ND input | set-value | walk via pipeline domains | `nondeterministic_dims` steers |
| linked feedback exclusion | Harness-driven fb | input steers | walk via enables | fb excluded from steer alphabet |
| `how(unlink=["Fb"])` fault | broken sensor | direct force | walk forces fb | bypasses physical chain delay |
| profile-gated (`Temp >= 5.0`) | analog ramp | hold + profile | walk ~500 scans | Harness ticks profile on fork |
| serial clobber | coupled latches | pulses + reset | walk recovers via oracle | `test_walk_decomposition` |
| cross-guard mutual clobber | coupled latches + 2 timers | holds + reset | walk recovers, <=2 iters | `test_walk_nogood` |
| Int command protocol | multi-hop state machine | CmdReset + CmdStart | walk 3 actions | `test_walk_real_patterns` |
| return_early() flow gating | subroutine flow control | Enable pulse | walk reachable | `test_walk_real_patterns` |
| rendezvous (two SFCs) | independent subsystems | multi-steer (Tier 1) | walk 2 actions, 30 scans | `test_walk_real_patterns` |
| odd/even step sequencer | self-increment + even skip | Advance + fold | walk reachable | `test_walk_real_patterns` |
| deep call chain (5 levels) | 5-level prereqs, 3 sub scopes | CmdProd + CmdReset + CmdStart + fold | walk reachable | `test_walk_real_patterns` |
| holds prevention A/B | serial corridors sharing enables | holds + selective release | zero recovery iters | `test_walk_holds` |
| set-value flood (30 noise ND) | 3-step Mode corridor | multi + pulses | solves at 131 forks (ablated 635) | `test_walk_budget` |
| consumed-same-scan handshake | mode-request protocol | simultaneous bundle | walk 1 step | `test_walk_handshake` |
| PackML chain (ack-cleared + call gate) | 2-level transient | bundle {ChgReq, ProdMode} | walk 1 step | `test_walk_handshake` |
| **live** `S_UnitModeCurrent==1` | real PackML mode change | bundle {C_ProductionMode, C_UnitModeChgRequest} | walk 1 step, 2.9s | ground-truth pulse |
| circularly-dead prereq | spin-guard shape | -- | honest NotFound | `test_walk_spin_guard` |
| **live** `S_StateCurrent==2` | C_CtrlCmd command chain | pulse C_Clear | walk 2 steps, 3.7s | no bundle needed |
| **live** `S_StateCurrent==4` | mode-gated completion | alarms + C_Clear + C_Reset | walk 5 steps, 3.5s | post-aliasing-fix template |
| ref-constant bank (14 REFs) | ref-goal flood | Arm x4 + Go | ~110 forks (ablated ~1214) | `test_walk_ref_flood` |
| copy-source chain | mode -> completion -> state copy | Adv + {ProdMode, ChgReq} | walk reachable | `test_walk_copy_source` |
| two-writer goal | writer disjunction | AdvB + Kick | 60-fork budget (ablated ~124) | `test_walk_writer_groups` |
| context-aware writer group | mutually exclusive branches | Init1 + Init2 | preserves active branch | `test_walk_context_groups` |
| indirect-copy writer | statically unresolvable | -- | honest unreachable, no crash | `test_walk_copy_source` |
| **live** `S_StateCompleteBool=1` | PackML completion writers | -- | Starting writer ranks first | satisfied `S_Starting=True` |
| **live** `y_BurnerLoop` | full chain | -- | honest NotFound @120s | frontier: upstream `S_Starting` / #9 rotate threat |
| jump-table writer (plain + expr ptr) | indirect copy | Sel + Go | walk reachable | `test_walk_copy_source` idx-chase |
| calc-scratch pointer (template shape) | indirect via scratch | Sel + Go | walk reachable | hop via calc expr / func_deps |
| REF-fed index (no literal writers) | copy-source candidates | Arm + Go | walk reachable | the probe20 blindness, pinned |
| zero jump table | indirect copy | -- | honest unreachable | chase refuses, no inverting index |
| **live** `(sm__where2jump, 4)` | commissioned table (native) | -- | binds `(S_StateRequested, [4, 15, 17])` | probe_idxchase_live |
| compound clobber (mode resets step) | And-of-Compare conjuncts | reorder retry | walks 4 steps from either order | `test_walk_compound_goals` |
| conflicting conjunction (pinned step) | And-of-Compare | -- | honest unsolvable, names conjunct | `test_walk_compound_goals` |
| **live** `(S_StateCurrent==4, S_UnitModeCurrent==1)` | compound state+mode | alarms + Clear/Reset + mode bundle | walk 6 steps, ~5s | probe_compound_goal |
| **live** `fill_stepNumber==4` | relation-gated fill dwell | tare + analog set-value | walk 24 actions | probe_fill_hold |
| full suite | all types | all steers | `make test` green | includes context-aware writer candidates |

---

## Findings (lessons learned)

Non-obvious things discovered during development. If it's in the code, it's
not here -- these are the ones that would cost hours to re-derive.

- **`fork()` is a true checkpoint** -- carries tags, memory (incl. timer
  fractions), time mode, dt, RTC, and harness. Backjump via `fork(scan_id)`
  rests on this.
- **External inputs are sticky** -- hold last value; `patch()` clears the
  patch, not the tag. Edge-gated commands need release-then-pulse.
- **Projected `cause()` must speak relations** -- numeric blockers like
  `pv < threshold` are recovery preconditions, not just scalar samples.
  `BlockingRelation` preserves the false comparison and carries candidate moves.
- **Budget enforcement must reach inside the explore loop** -- without per-steer
  checks inside `_explore`, fork caps overshoot unboundedly.
- **Writer regression has a data-flow half** -- a `copy(SRC, tag)` writer's
  source-at-the-goal-value is an equal prerequisite alongside control-flow
  conditions. Both regression tools only carried control-flow until the
  copy-source arc.
- **Threats are structurally invisible to the prerequisite chain** --
  `x_RotateSensor` is not in the PDG upstream cone of its victim. Prerequisites
  trace "what do I need"; threats trace "what undoes progress" -- fundamentally
  different cones. Regression detection during time-folding is the right seam.
- **Program-state conjuncts can't be held** -- holds protect external inputs
  (the walker's hand). Stateful must-stays need detection + reorder, not
  causal links. The threat taxonomy ("detected by construction") holds for
  input holds only.
- **Resting value != declared default; can be path-dependent** -- retentive
  semantics make declared defaults suspect; some tags rest at the last valid
  command, not a fixed value. Infer rest from the program's own clearers.
- **Twin fidelity for indirect reads is a codegen property** -- indirect reads
  resolve through the bank slot's identity. When a frontier sits downstream of
  indirect config, check the config bank values before trusting the refusal.
- **Cross-writer prereq union is a budget sink** -- merging unsatisfied
  conditions across all writers conjoins one writer's expensive requirements
  with another's satisfied ones. Context-aware per-writer candidates fix this.

---

## Future scope

### Recurring obligation (#9 -- rotate pulse)

The active frontier. `x_RotateSensor` must toggle or the watchdog aborts at
~13s sim. Needs a periodic steer element. Layers 1-3 of the regression-cause
cascade landed; layer 4 and the periodic steer mechanism are unbuilt.

**Design direction -- regression-cause cascade:**

1. **Regression detection** (during time-folding): track high-water marks on
   progress-relevant tags; pause when a progressing tag regresses.
2. **Single-scan `cause()`** at the regression scan: names the immediate cause
   (the abort command, the alarm).
3. **Recursive cause-chasing**: follow the chain deeper -- alarm -> watchdog
   timer Done -> timer condition.
4. **`cause(tag, find="oscillation")`** on the timer's input: the sensor was
   toggling and stopped, or never toggled. This isn't a transition at a single
   scan -- it's the absence of transitions over a window.

The regression-cause becomes a new flaw source: raise a hold or sub-goal to
prevent the threat. For the rotate sensor, the sub-goal is "keep
`x_RotateSensor` toggling" -- which is where the periodic steer mechanism
enters. `cause(tag, find="oscillation")` extends `cause()` with a `find=`
parameter for pattern detection over retained history.

### Multi-corridor timing (Tiers 2-3)

- **Tier 2** -- force-and-solve with deadline checking. Detection wired
  (`_needs_decomposition` + pre-clobber checkpoint); the force-and-solve
  mechanism waits for a real mutual-interference test case.
- **Tier 3** -- cyclic coupling convergence to fixed point. Needs the
  oscillation guard first. Also: reschedule (alternative linearizations),
  co-advance cyclic synchronization, convergence diagnosis via `cause()`.
- **Convergence oscillation** -- cycle detection over (checkpoint,
  timing-guess) history; the current spin guard only catches
  identical-set-identical-state.
- **Input timing fragility** -- window characterization surfaces it; no
  further mechanism needed beyond visibility.

### Goal handling extensions

- **Transform-chasing** -- regression across pack/unpack/copy-convert
  data-flow boundaries. Reversible cases add precise sub-goals; lossy or
  variable-width cases stay conservative (ordering/advice only).
- **Must-stay steer filtering** -- compose committed compound conjuncts into
  `_StepMonitors` to skip steers whose trial breaks one. Reorder covers every
  current shape; build the fixture first.
- **Callable predicate (`expr=None`)** -- opaque predicates need expr
  decomposition or a try-after-walk adapter.
- **Ack-cleared Ints/Words** -- widen the ack-cleared-input pattern beyond
  Bools. Needs set-value domains from reader-comparison inference.

### Search efficiency

- **Seen-key fragmentation** -- mitigation if it bites: project per-goal
  (only nogoods whose `(from, to)` involves the current governing tag).
- **Narrow-cut cardinality screening** -- screen on domain cardinality.
- **Cheap steer pre-screening** -- evaluate candidates against `simplified()`
  before forking. Measure before building.
- **Steer-history reuse** -- try previously-successful steers first, keyed
  `(governing, from_value, to_value)`. Architecture: within-walk history is
  loop-side learning (sibling of `NoGoodStore`/`HoldStore`), not a registry
  pass. Feeds #9.
- **Symmetry transfer** -- structural isomorphism detection across repeated
  subsystems; transfer solved steer sequences through renaming. Wait for a
  fixture.

---

## Research grounding

The **closed loop** is the novel contribution: actual-cause attribution
(`cause()`) as the repair signal in a solver-free forward planner over the
executable program, producing an operator-executable plan. Classical planners
need a solver to bridge model and reality; when the model IS reality
(forkable, steppable, deterministic), the solver collapses to "try it and
observe."

**Prior art by mechanism:** directed model checking (Edelkamp, Lluch-Lafuente,
Leue), POCL / causal links (SNLP, UCPOP), IC3/PDR obligation queue (Bradley),
triangle tables / PLANEX (Fikes-Hart-Nilsson), FF helpful actions
(Hoffmann-Nebel), Halpern-Pearl actual causality, System-R regression
(Bonet-Geffner), conflict-driven state-space search (Steinmetz-Hoffmann),
causal graph decomposition (Helmert), serialized width / BFWS
(Lipovetzky-Geffner), supervisory control (Ramadge-Wonham), macro-operators
(Botea et al.), timed automata (Alur-Dill, UPPAAL), gadget-maze complexity
(Demaine-Hendrickson-Lynch), assume-guarantee (Pnueli, Abadi-Lamport).
