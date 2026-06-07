# Corridor Walker — Living Plan

Companion to `corridor_walker_brief.md` (the original design hypotheses) and
`recovery_mechanisms.md` (the mechanism catalog). This file tracks what's
**done** and what's **left**, and sequences the work. Update the checkboxes as
we go.

**One-line status:** Engine built, wired, validated. Time-folding done. Next:
widen what the engine can *accept* (goal decomposition, multi-tag factoring)
before adding backtracking/recovery.

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

- ☐ **Multi-tag factoring** — read the synchronization structure (narrow
  dependency-graph cuts → producer/consumer partial order). Linearize the DAG;
  solve corridors in producer-consumer order. Cyclic residue → Convergence.
  This is the structural move that lets the single-governing-tag engine handle
  multi-tag targets without ad-hoc nesting. The *dynamic* fallback (when no
  steer advances the current governing tag, sub-walk to a prerequisite) is the
  per-edge special case of this static decomposition.

### Phase 2: Widen the alphabet (let refinement succeed on more transitions)

These extend the action space so that `_apply_steer` / `_explore` can realize
transitions it currently can't express. They keep the engine's loop unchanged.

- ✅ **Helpful-steer ordering** — `_steer_alphabet` now orders candidates by
  relevance: inputs appearing in the enabling condition (`sp_tree()`) of the
  governing tag's target-value write-sites are tried first. Pure efficiency —
  no new coverage, just faster `_explore` convergence.

- ☐ **Non-Bool inputs** — analog setpoint / Int hold at a probed value.

- ☐ **Drive-LOW steers** — today only release-then-pulse (rising edge) exists.
  Need explicit LOW drive to enable transitions gated by `NOT input`.

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

- ☐ **Third `_explore` exit** — today: success or `None`. Add:
  reached-governing-but-diverged (carrying a cause payload). This is the
  backtracking trigger.

- ☐ **Precondition accumulation** — monotonic set of constraints learned from
  failed refinements (⇒ no oscillation, termination guarantee). A re-attempt
  of the same abstract plan carries the accumulated preconditions.

- ☐ **Backjump to cause origin** — `fork(scan_id)` checkpoints; reuse
  `_find_backjump_target` (waypoints.py:2162). Go back to where the divergence
  originated, not just one step.

- ☐ **Constructive regression** — recurse `simplified()` to inputs; sub-walk
  to establish each precondition. Stops at external inputs, not first-unobserved
  tag. (May be flag-gated: the interpreted walk doesn't strictly need full
  input-chain naming.)

- ☐ **Inverse regression** — the make-*false* path: break a seal-in / hold /
  satisfy a reset. Distinct from constructive because the leaves are different
  (reset conditions, not enable conditions).

- ☐ **`seen` keyed on `(value, precondition_state)`** — else a re-walk can't
  re-enter a visited value with different learned constraints.

- ☐ **Keep failed forks alive** — each `_Node`'s fork *is* the checkpoint;
  retain parent pointers instead of dropping on `popleft`.

### Phase 5: Diagnosis (explain infeasibility)

Depends on the causal-API prerequisite (copy/calc awareness in projected
cause/effect). This is what lets the walker return `Diagnosis` instead of
`None` — the gate for removing BFS fallback entirely.

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
  `Diagnosis` carries: preconditions tried, contradicting enablers, actionable
  blockers. Distinguish `Unsolvable(cert)` (order-independent contradiction) vs
  `NotFound(reasons)` (exhausted budget). Recognize transient/never-rests
  (`_stable_step_values`, waypoints.py:1449) → "unreachable: transient" instead
  of silent fall-through.

### Phase 6: Multi-corridor convergence (new scope)

Runs *above* the per-corridor layer: corridors each individually solvable but
not jointly satisfiable at their sync points within deadlines.

- ☐ Convergence diagnosis (relative timing across a sync edge).
- ☐ Divest-as-sync-edge.
- ☐ Reschedule (different linearization, not a precondition fix).
- ☐ Co-advance cyclic synchronization (SCC of subsystems).

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
