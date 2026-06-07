# Corridor Walker — Living Plan

Companion to `corridor_walker_brief.md` (the original design hypotheses) and
`recovery_mechanisms.md` (the mechanism catalog). This file tracks what's
**done** and what's **left**, and sequences the work. Update the checkboxes as
we go.

**One-line status:** Engine built, wired, validated. Time-folding done. Steer
ordering + Drive-LOW done. `why()` now resolves subroutine rungs (prerequisite
for factoring). Next: multi-tag factoring (Phase 1), then backtracking with
`cause()`-based nogood learning (Phase 4).

---

## Unifying principle: hierarchical planner

The corridor walker is a **hierarchical planner** with three layers:

1. **Abstract** — collapse the full PLC state space to one governing tag's
   value graph (tiny: mode machines have single-digit values).
2. **Plan** — BFS over that abstract value space (cheap).
3. **Refine** — for each abstract edge, find concrete inputs via interpreted
   simulation on forks (sound by construction — immune to static-analysis
   blindness).

Everything in LEFT maps to one of three extensions of this core:

| Extension | What it does | Planning concept |
|---|---|---|
| **Widen the input** | Let the engine accept goals it currently punts on (Or/And decomposition, multi-tag factoring) | Better abstraction / goal preprocessing |
| **Widen the alphabet** | Let refinement succeed on more transitions (non-Bool inputs, multi-input steers, link-aware de-energize) | Richer action space for refinement |
| **Backtrack on failure** | When refinement fails or execution diverges, re-plan with learned constraints | Hierarchical backtracking |
| **Diagnose infeasibility** | When no abstract plan is feasible, explain why | Explanation generation |

The guiding question for every new mechanism: **does it extend the existing
engine's reach, or does it add a parallel path?** Prefer the former. A generic
backtracking loop that retries decompositions scales; hand-coded pattern
handlers grow with every new PLC idiom.

**Static analysis is a prior, never correctness-bearing** — it picks the
governing tag, narrows the steer alphabet, and sets the horizon. Correctness
comes from simulation.

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

## Architecture (decided)

`how()` first tries a **corridor walk** on the PLC runner; anything it can't
walk returns `None` and falls back to the existing waypoint/BFS planner
(unchanged, transitional).

> **Settled end-state:** **no BFS fallback for `how()`** — the walker returns a
> `Path` or a `Diagnosis`; BFS stays only for `always()`/`never()`/
> `reachable_states()`. Removing the fallback is gated on the walker covering
> (or diagnosing) the cases BFS currently catches.

**The engine = interpreted best-first search over the governing stateful tag's
value graph.** Each value is expanded by a **steer alphabet** (empty-step /
pulse-input); every edge is discovered by *simulation on forks*, so it's sound
by construction.

Files: `src/pyrung/core/analysis/prove/walk.py` (engine),
`src/pyrung/core/runner.py` `_how_via_bfs` (the early walk attempt, ~line 1037).

---

## DONE

- [x] **Reconnaissance** — confirmed/refuted every brief claim against the code
  (see "Findings" below).
- [x] **Milestone 0: walk a known corridor.** `plan_walk` reaches `StateCurrent==EXECUTE`
  from ABORTED via input-steer corridor; replay-verified to 6.
- [x] **Unified a→e corridor source** — one interpreted best-first engine with static
  priors (governing tag / steer alphabet / horizon). Subsumes direct-literal,
  copy-coupling, copy-chains, counters, and target→governing indirection.
- [x] **Governing-tag selection** (`_governing`) — derived coil delegates to the
  richest stateful tag that gates it; multi-value tag governs itself. A self-updating
  `calc` whose wrapper op hides the step (`(Step+1)%6`) governs its own corridor
  (`_calc_self_referential`).
- [x] **Steer alphabet** (`_steer_alphabet`) — empty + pulse-each-cone-input, with
  fallback to all external Bool inputs; edge-gated commands use release-then-pulse
  (external inputs are *sticky*, so a clean rising edge needs an explicit release).
- [x] **Time-as-horizon** — a held wait advances timers to the crossing, so "advance
  time" is just an empty steer with a long horizon.
- [x] **Time-folding (tesseract).** Held waits **jump** to the nearest actionable
  accumulator crossing instead of ticking: **timers** ride the dt knob (one real step
  covers N dt-scans); **per-scan counters** are patched forward by `(skip-1)*delta`
  with the jump scan's own `execute` supplying the final increment (phase-kept, since a
  counter is timeless and the dt knob can't move it). The **plateau guard**
  (only-accumulators-moved) is the soundness gate; the actionable-crossing set
  (`_nearest_skip` over read done-bits + acc comparisons) only sets *how far* to jump.
  Emitted as one `({}, scans)` entry recording **real elapsed scans**, replay-verified
  at normal dt.
- [x] **Dynamic reaction budget.** The per-steer fold cap became a *reaction* budget
  (consecutive churn scans before a plateau forms), so a pulse that merely *starts* a
  dwell folds too — previously the pulse cap stopped at 6 scans and `how()` fell back to
  BFS. Inert/oscillating pulses still bail fast; the empty steer is effectively
  unbounded. Productive folding always runs to `_EMPTY_CAP` regardless of the budget.
- [x] **`fork()` captures clock + mid-accumulation fraction** — verified: `_frac:` timer
  fraction lives in `.memory` and is carried; a forked runner stays bit-identical across
  20+ scans. The re-fork/replay backbone the recovery layer needs is sound. (Reviewer
  linchpin; backjump via `fork(scan_id)` rests on this.)
- [x] **Interpreted verification** — replay the assembled path on a fresh fork; only
  return valid `Path`s.
- [x] **Wired into `_how_via_bfs`** — tried before kernel compilation; on success it
  never compiles the kernel or runs BFS. Skipped when `avoid` is given (M0).
- [x] **Example conversion** — `examples/packml_bench.py` task converted to the
  resting Advance-flag/even-auto-advance pattern (cf. `examples/task_example.py`).
  `_CurStep` now *rests* at 1/3/5; modulo wrap (`% 6`) keeps it bounded for the prover.
- [x] **Real walker unit tests** — `tests/core/analysis/test_prove_walk.py`: counter
  acc-patch path up & down (reachability via the walker, exact-crossing landing in a
  handful of real steps, normal-dt replay), pulse-started fold, and churn-budget bail.
- [x] **Vacuous-test fix (`f9e128d`)** — `bool(Condition)` now raises `TypeError`,
  root-causing the silently-passing PackML `how()` assertions across the suite; also fixed
  `_scalar_eq` in `waypoints.py`/`absorb.py`.
- [x] PackML baseline + pass-pipeline tests pass; full prove suite green (681) after
  both folding and the cap-lift.

---

## LEFT — sequenced by planning concept

### Phase 1: Widen the input (let the engine accept more goals)

These are not "recovery" — they remove restrictions on what the existing engine
can even attempt. Highest leverage because they turn `None → fallback` into
`walk succeeds` without adding new mechanism code.

- ✅ **Or/And goal decomposition** — `_extract_goals` (replacing
  `_target_tag_value`) decomposes compound expressions via waypoints'
  `_extract_required_values`: `And` → collect all `(tag, value)` pairs and walk
  sequentially (chaining corridors on one fork); `Or` → pick the cheapest
  branch. Verification evaluates the full `expr`, not a single tag. Walker now
  solves `how(Ready, Done)` end-to-end (no BFS fallback). See
  `tests/core/analysis/test_prove_walk_nested.py` for a condensed "hard for
  walk" program (nested timer-gated state machines) — tripwire for
  prerequisite-corridor support (Phase 1 factoring).

- ☐ **Multi-tag factoring** — decompose multi-tag goals into a solve-order
  using the oracle layers:
  1. **Candidate structure**: `why()` on the compound goal from the current
     fork gives a unified causal tree; its conjunctive roots are independent
     inputs, shared internal nodes are coupling points. PDG
     `upstream_slice` gives the static connectivity prior.
  2. **Solve-order**: SCC-condense the causal sub-graph (Helmert-style);
     topological sort of the condensation = producer-consumer order. Each
     SCC-node becomes its own corridor walk, inheriting upstream
     postconditions as initial-state constraints on the fork.
  3. **Independence validation**: after solving each corridor, `cause()` on
     the scan log confirms no downstream tag was clobbered. If clobbered →
     re-order or flag as cyclic residue → Convergence (Phase 6).

  The *dynamic* fallback (when no steer advances the current governing tag,
  sub-walk to a prerequisite) is the per-edge special case of this static
  decomposition. The tripwire test is `test_prove_walk_nested` (3-layer
  timer-gated state machine).

### Phase 2: Widen the alphabet (let refinement succeed on more transitions)

These extend the action space so that `_apply_steer` / `_explore` can realize
transitions it currently can't express. They keep the engine's loop unchanged.

- ✅ **Helpful-steer ordering** — `_steer_alphabet` now orders candidates by
  relevance: inputs appearing in the enabling condition (`sp_tree()`) of the
  governing tag's target-value write-sites are tried first. Pure efficiency —
  no new coverage, just faster `_explore` convergence.

- ☐ **Non-Bool inputs** — analog setpoint / Int hold at a probed value.

- ✅ **Drive-LOW steers** — `_steer_alphabet` now generates `"low"` steers for
  inputs appearing as `xio`/`fall` in the enabling condition. `_steer_prefix`
  handles falling-edge inputs (high→low sequence for `fall()`-gated transitions).

- ☐ **Multi-input steers** — transitions needing two+ inputs simultaneously.

- ☐ **Link-aware de-energization** — obey `link=`: follow a needed-false
  feedback to its enable and de-energize the cause; `Physical.on_delay` as the
  crossing delay; force directly only for unlinked/declared-external tags. No
  strict mode — every forced tag is a visible, audited assumption.

### Phase 3: Execution monitoring (detect when the plan goes wrong)

Lightweight; triggers re-planning rather than being a mechanism itself.

- ☐ **Path-sequence divergence** — during commit, assert the governing value
  follows the planned value sequence. Mismatch = divergence → triggers
  backtracking (Phase 4). The path already implies the expected sequence; this
  is ~5 lines of checking, not a new data structure.

- ☐ **Must-stay violation** — assert held waypoints across the suffix, not just
  the final value.

- ☐ **Deadline-race** — when a divergence deadline is known, race it against
  the target crossing in the time-jump math (jump to whichever fires first).
  The crossing arithmetic is built; this adds the deadline as a competing entry.

### Phase 4: Backtracking on refinement failure (the recovery loop)

This is where the engine becomes a proper hierarchical planner with learning.
The key architectural choice: **one generic backtracking loop with learned
constraints**, not per-pattern handlers. Each "mechanism" is a strategy the loop
can invoke, but the loop itself is uniform.

The oracle layers apply here as: `why()` generates regression sub-goals
(what's holding the governing tag stuck); `cause()` extracts nogoods from
failed simulation (what actually blocked the transition). Together they replace
the symbolic nogood-learning and weakest-precondition machinery of classical
planners with empirical observation.

- ☐ **Third `_explore` exit** — today: success or `None`. Add:
  reached-governing-but-diverged (carrying a `cause()` payload from the scan
  log). This is the backtracking trigger.

- ☐ **Precondition accumulation (nogood learning)** — when a steer fails,
  `cause()` on the fork's scan log gives the trigger/enabler split: which tags
  were load-bearing for the failure. Record as a nogood keyed on
  `(from_value, to_value, frozenset(blocking_tags))`. Monotonic addition (only
  add, never relax) ensures termination since the tag-value space is finite.
  No symbolic implication graph — the scan log IS the implication graph.

- ☐ **Backjump to cause origin** — `fork(scan_id)` checkpoints; reuse
  `_find_backjump_target` (waypoints.py:2162). Go back to where the divergence
  originated, not just one step.

- ☐ **Constructive regression** — `why(governing_tag)` on the stuck fork gives
  the minimal load-bearing contacts. Each conjunctive root that is NOT an
  external input becomes a sub-walk goal. `simplified()` expands to full
  formula when `why()` roots are insufficient (rare: `why()` already prunes
  irrelevant branches). Depth-bounded (3 levels) to avoid combinatorial
  explosion.

- ☐ **Inverse regression** — the make-*false* path: `why(tag)` on a seal-in
  gives the latch's hold condition; the `_walk_reset_path` branch of `why()`
  already identifies reset rungs and their enabling conditions. Sub-walk to
  satisfy the reset condition. Structurally different from constructive because
  the targets are reset/release conditions, not enable conditions.

- ☐ **`seen` keyed on `(value, nogood_state)`** — else a re-walk can't
  re-enter a visited value with different learned constraints. The nogood set
  is the distinguisher.

- ☐ **Keep failed forks alive** — each `_Node`'s fork *is* the checkpoint;
  retain parent pointers instead of dropping on `popleft`.

### Phase 5: Diagnosis (explain infeasibility)

Depends on the causal-API prerequisite (copy/calc awareness in projected
cause/effect). This is what lets the walker return `Diagnosis` instead of
`None` — the gate for removing BFS fallback entirely.

The oracle architecture makes diagnosis concrete: `why()` on the stuck state
gives the minimal explanation of what's holding the governing tag; `cause()`
on each failed steer attempt gives "rung N blocked because tag X was Y, held
by rung M's seal-in." No symbolic unsolvability proof needed — exhaustive
steer trial + `cause()` on each failure IS the certificate for a deterministic
system from a specific state.

- ⚠️ **Prerequisite: projected cause/effect copy/calc awareness** —
  `_rung_writes_value_when_enabled` (projected.py:41) and `_infer_written_value`
  (projected.py:537) are Latch/Reset/Out-only. Lifting this has real blast
  radius (DAP, fuzz). Own change, own tests.

- ☐ **Trigger/enabler split via `cause()`** — recorded mode at full ScanLog
  fidelity (trigger = what transitioned; enabler = what was already wrong).

- ☐ **`effect()`-confirmed minimal cause** — fork-and-test each enabler.

- ☐ **Deadline extraction** — timer Done → annotate with preset: "establish
  enabler within N scans."

- ☐ **Diagnosis as return type** — `how()` returns `Path | Diagnosis`.
  `Diagnosis` carries: preconditions tried (from Phase 4 nogoods),
  contradicting enablers (from `cause()`), actionable blockers (from `why()`
  roots). Distinguish:
  - `Unsolvable(cert)` — all steers exhausted from this state + `cause()`
    shows each failure is structural (not timing-dependent). The certificate
    is the set of `cause()` payloads proving closure.
  - `NotFound(reasons)` — budget exhausted. Carries: best partial plan,
    first failing edge, accumulated nogoods. Always actionable: "got to
    value V but couldn't reach W; `why()` says tag X is blocking."
  Recognize transient/never-rests (`_stable_step_values`, waypoints.py:1449)
  → "unreachable: transient" instead of silent fall-through.

### Phase 6: Multi-corridor convergence (new scope)

Runs *above* the per-corridor layer: corridors each individually solvable but
not jointly satisfiable at their sync points within deadlines. Triggered by
Phase 1 factoring when `cause()` detects a clobber (corridor A's solution
overwrites a tag corridor B needed).

- ☐ Convergence diagnosis (relative timing across a sync edge). `cause()` on
  the clobber scan identifies which corridor wrote what and when.
- ☐ Divest-as-sync-edge.
- ☐ Reschedule (different linearization, not a precondition fix). Try
  alternative topological orderings from the Phase 1 condensation DAG.
- ☐ Co-advance cyclic synchronization (SCC of subsystems). When both orderings
  (A-before-B, B-before-A) yield clobbers → true deadlock; diagnose the
  mutual exclusion.

### Still-open odds & ends

- ☐ **`avoid=` support** — walk is skipped when `avoid` is given (M0); add
  avoid-state pruning to exploration.
- ☐ **Cheap trial** — `with plc.trial():` snapshot/restore instead of
  `fork()`-per-candidate (fork is ~ms; lookahead does many).

### Superseded / deliberately dropped

- ~~**Planner B as a per-segment scoped-BFS fallback**~~ — killed by *no BFS
  fallback*. A stuck hop is handled by Phase 4 backtracking.
- ~~**Split-horizon cap**~~ — replaced by the dynamic reaction budget (DONE).

---

## Findings (so we don't re-derive)

- **`fork()` is a true checkpoint** — carries `.tags`, `.memory` (incl. `_frac:` timer
  fraction), time mode, dt, and RTC. Verified bit-identical continuation across 20+ scans
  after a mid-fraction fork. Backjump via `fork(scan_id)` (runner.py) rests on this.
- **Corridor source is NOT the existing waypoint front-half.** `_order_waypoints`
  collapses the StateCurrent↔StateRequested↔StateEnableYes SCC into one cone-21
  mega-waypoint (> `_MEGA_CONE_LIMIT=18`), so today's `how(EXECUTE)` falls straight to
  the OOM-prone undecomposed BFS. And `_build_value_transitions("StateCurrent")` is
  **empty** because StateCurrent is `copy`-written. The real graph comes from chasing
  the copy coupling (or, generally, interpreted probing).
- **Steering can't use projected `cause()`/`effect()`.** Projected `cause` is single-hop
  and **copy/calc-blind** (Latch/Reset/Out only) → degenerate empty chains on the mode
  machine; projected `effect` is a single hypothetical scan. The interpreted runner is a
  strictly more faithful forward oracle → lookahead. `cause()` is reserved for the
  *backward* (divergence) direction, where recorded mode already works.
- **`events.py` crossing scheduler is welded to the BFS** (`_ExploreContext` +
  `ReplayKernel` + absorption pipeline) — not runner-consumable wholesale. The walker
  re-derived the held-wait crossing *arithmetic* from timer/counter instruction
  introspection (`.accumulator`/`.preset`/`.done_bit`), which is the simple slice (no
  co-firing / input-variant / abstract-threshold branching).
- **External inputs are sticky** (hold last value); `patch()` clears the patch, not the
  tag. Edge-gated commands need release-then-pulse.
- **Verification is interpreted** (replay on a fresh fork) — no compiled-kernel
  agreement risk for the walk path.
- **No abstraction gap (the oracle advantage).** Classical planners suffer
  because their model (PDDL operators, BDDs) can diverge from reality. Our
  "model" is the program running on a fork — correct by construction. The
  tradeoff: can't generalize as freely (a nogood at state S might not hold at
  S'), but never have false positives. Closer to concolic execution or
  sample-based motion planning (RRT) than classical AI planning.
- **`why()` is the state-aware candidate generator.** Unlike `simplified()`
  (structural, all formula paths) or PDG (coarsest connectivity), `why()`
  gives the *minimal load-bearing contacts* for the current fork state via
  SP-tree attribution. Terminates at external inputs. Handles stateful (latch
  seal-in) vs. stateless (OTE) differently. Gives a unified tree for multi-tag
  queries — the tree's structure IS the factoring structure.
- **`why()` resolves subroutine rungs.** The `_RungResolver` uses the PDG
  node's `subroutine` field to look up rungs in `program.subroutines[name]`
  instead of only seeing the main logic. Before this fix, `why()` silently
  skipped any writer inside a subroutine (the old `rung_index >= len(logic)`
  guard). Now `why(y_Burner)` from cold start produces the full 3-layer
  causal tree through `burner_prod_steps` and `burner_heat_task` — the
  factoring structure for Phase 1 is directly visible.
- **`cause()` is the validation/nogood oracle.** Recorded-mode causal analysis
  on the scan log. Gives trigger vs. enabler split. Use after simulation to
  extract nogoods (what blocked), confirm independence (no clobber), and
  produce actionable diagnosis. The scan log IS the implication graph — no
  symbolic derivation needed.

---

## Validation status

| Target | Corridor type | Steer | Result | Notes |
|---|---|---|---|---|
| `StateCurrent==EXECUTE` from ABORTED | mode machine | input pulses | walk ~2 s, replay→6 | go/no-go |
| `_CurStep==5` from EXECUTE | task timer wait | empty (folded) | walk, replay→5 | now **folded** via dt-knob (was ticked); old BFS = wrong "unreachable" |
| counter dwell 0→1 (synthetic) | per-scan counter | empty + pulse | folds via acc-patch | `test_prove_walk` — up & down, exact landing, replay-verified |
| `_CurStep==5` from cold/STOPPED | nested | — | None → fallback | needs Phase 1 factoring |
| `how(Ready, Done)` (two-step latch) | compound And | input pulses | walk 3 steps, 0.0 s | Phase 1 Or/And decomposition |
| `y_Burner` from cold (nested) | 3-layer timer-gated | — | None → fallback | needs Phase 1 factoring (`test_prove_walk_nested`) |
| `StateCurrent=="IDLE"` from cold | mode (string operand) | — | None → fallback | cold-start start-value not in graph |

---

## Still-open features (not yet placed in phases)

- ☐ **Fault-scenario override (`unlink=`)** — caller declares specific feedbacks
  as broken. Walker forces them directly, bypassing the link. The plan is the
  commissioning workaround for that fault. Domain-specific, no phase dependency.

- ☐ **Spin guard (termination)** — if the nogood set hasn't grown since the last
  attempt from this checkpoint, stop. Identical set + identical state + still
  failing = not an ordering problem; report the contradiction. Multi-corridor
  variant: each corridor individually solved but convergence still infeasible
  after rescheduling = coordination contradiction, not a precondition gap.

---

## Prior art & novelty

**Prior art by mechanism:**

| Mechanism | Reference |
|-----------|-----------|
| Corridor walk (directed forward search) | Directed model checking (Edelkamp, Lluch-Lafuente, Leue) |
| Helpful-steer ordering | FF helpful actions (Hoffmann-Nebel), applied via exact structure instead of delete-relaxation |
| Time-jump at crossings | Hidden-event acceleration / timed-automata event-driven simulation |
| Causal diagnosis (`cause()`) | Halpern-Pearl actual causality; "Explaining Counterexamples Using Causality" (Beer, Ben-David, Chockler); causality checking (Leitner-Fischer, Leue) |
| Minimal-cause confirmation | Halpern-Pearl AC3 minimality; polynomial approximation (Beer et al.) |
| Nogood / precondition accumulation | Conflict-driven state-space search (Steinmetz-Hoffmann); CDCL (SAT) |
| Backjump to cause origin | Conflict-directed backjumping (CDCL / CSP); Steinmetz-Hoffmann for planning |
| Constructive regression | System-R regression planner (Bonet-Geffner) — interleaved regression + forward progression |
| Factoring (causal graph decomposition) | Helmert causal graphs; star-topology decoupled search (Gnad-Hoffmann) |
| Convergence / deadline diagnosis | Timed-automata fault ascription (Leitner-Fischer, Leue) |

**What's novel:**

The individual mechanisms all have prior art. The **closed loop** is open
ground: actual-cause attribution (`cause()`) as the repair signal in a
solver-free forward planner, where **the program itself is the model** — no
PDDL encoding, no symbolic transition relation, no solver. The program runs on
forks; `why()` generates candidates by backward SP-tree attribution on the
live state; `cause()` validates by recorded-mode scan-log analysis; the walker
steps forward on the real interpreter.

The contribution is wiring Halpern-Pearl causality, System-R regression, and
conflict-driven learning together *without a solver*, because the executable
program makes one unnecessary. Classical planners need a solver to bridge the
gap between model and reality. When the model IS reality (forkable, per-rung
steppable, deterministic), the solver collapses to "try it and observe."

**Key papers (with URLs):**

- Steinmetz & Hoffmann (2016), *Towards Clause-Learning State Space Search*,
  AAAI — the conflict-driven learning loop; `cause()` replaces their Algorithm 2.
  https://fai.cs.uni-saarland.de/hoffmann/papers/aaai16.pdf
- Steinmetz & Hoffmann (2016), *State Space Search Nogood Learning*, AIJ —
  length-independent sound nogoods (trustworthy `Unsolvable`).
  https://www.sciencedirect.com/science/article/pii/S0004370216301448
- Steinmetz (2022), PhD thesis, *Conflict-Driven Learning in AI Planning
  State-Space Search* — convergence + trap learning.
  https://dblp.org/rec/phd/dnb/Steinmetz22.html
- Lipovetzky & Geffner (2017), *Best-First Width Search* — novelty/memory bound
  (if a residual segment ever needs real search).
- Helmert (2006), *The Fast Downward Planning System* — causal graph
  decomposition, domain transition graphs.
- Timing/deadlines: timed-automata tradition (Alur–Dill; UPPAAL).
