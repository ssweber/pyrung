# pilot/ — a harbor pilot for PLC programs

The user is the **captain** — decides the destination. The PLC program is the **ship** —
it has its own mass, inertia, timing, and logic. **PILOT** comes aboard, reads the
charts, learns the handling, and navigates the dangerous passage.

### Why this is hard (first principles)

- **You share the helm.** Same register file, same scan cycle, no locks, no transactions.
- **Your actions ripple everywhere.** One input hits rungs you haven't traced.
- **The scan is atomic and you're on one side.** You set inputs, the PLC runs all its
  logic, you see the result. No mid-scan intervention.
- **The same input means different things in different states.** Reset from STOPPED ≠
  Reset from EXECUTE.
- **You can't tell who wrote what.** After a scan, a register changed. Was it you or
  the program?
- **Some of the logic is unreadable.** Computed indices, runtime masks, indirect
  addressing. You can see the rung but can't resolve it without running it.
- **Time works against you.** Timers tick, watchdogs count, whether you're making
  progress or not.
- **The program doesn't know you exist.** Not adversarial, not cooperative. It just runs
  its logic every scan.

## The compass is a bearing, not a route

A compass does not plan a path. It gives the pilot a persistent *bearing* toward the
target and keeps re-pointing as the state changes. The pilot is free to "fly around the
mountain" — lateral moves, excursions, detours through the acceptance layers — but the
compass always knows which way the target is. When the loop is wandering (oscillating,
stuck on a distance plateau), the fix is almost always **consult the compass**, not add
another acceptance heuristic.

```
compass = trace + let-run + sandbox
```

All three instruments answer one question — *"I need `(tag = value)`; what must I do?"* —
and differ only in how much of the causal path is readable.

## The loop

```
Compass     — gives the bearing. Trace + let-run + sandbox, merged into one
              persistent direction.
Act         — steer toward it (command pulse or zoom through timer dwell).
Verify      — who moved what?

  1. I moved it where I wanted.        → Confirmed edge.
  2. I moved it wrong.                 → Bad edge. Correct the compass.
  3. The PLC moved it wrong.           → My command was a no-op; the program
                                         has its own agenda. Learn both.
  4. Nothing happened / new frontier.  → Unmet prerequisites. Trace back why —
                                         that's the real frontier.

Investigate — on regression (trend worsened after verify), build a bounded
              incident, propose hypotheses, replay-test each, apply confirmed
              holds, revert to checkpoint.

Fix what's fixable, accept what isn't.
Revert on sustained decline — checkpoint, try a different branch.
```

Outcome classification lives in `outcome.py`.
Investigation lives in `investigate.py`.

## The three instruments

### 1. `trace` — read the charts  (`trace.py`)

Reads the map; runs nothing. Three capabilities under one roof:

- **Transparent backward resolution** — walk writer conditions / copy / calc backward to
  steerable inputs. Output: a prerequisite tree (`TraceNode`).
- **Establish + Preserve** — the backward walk *establishes* a value (finds the writer
  that produces it). A **retentive** target (latch/SET coil, or copy/calc into a held
  register — `tag not in rung.ote_writes`) must also *persist*: any competing writer that
  **provably** drives the tag away from the target (`_can_produce(written, value)` False —
  the `reset(Running)` to a latch, a `copy(0, State)` to a `copy(5, State)`) would clobber
  it on a later scan. `_preserve_children` surfaces the **negation of each such writer's
  guard** as ordinary prerequisite leaves (`reset gated ~StopBtn` → `StopBtn=True`), which
  ride the normal candidate / widening / hold pipeline; `_expr_satisfied` elides the
  already-healthy ones, and De Morgan turns a compound reset guard into an `Or` of
  suppression options resolved like any route choice. This is the engineer reading *both*
  halves of a latch's boolean semantics — not a walk-style firm-hold registry. Honesty
  boundary: a writer whose value *could* be the target (`_can_produce` True — affine /
  aggregate / unknown) is **not** suppressed; trace never fabricates a hold it can't read.
- **Opaque-but-constant value navigation** — when a writer is an indirect/computed jump
  the backward walk can't follow (`ds[computed_idx]`), but the table is *declared
  constants + affine index*, invert it statically and BFS multi-hop over one register's
  value space (`CompassGraph`, `CompassPlan`, `expand_routes`).

Owns: transparent completion chains, retentive-value preservation, and constant commanded
value-jumps.

Route choice — report and redirect (no `choice=`): `how()` **never reports ambiguous**. For
a Bool target with more than one route it picks a deterministic default and records where it
went on `Path.route` (a `RouteTaken` carrying redirectable `RoutePivot`s); the engineer
redirects with `avoid=` (steer off a route) or `via=` (steer onto one), naming the condition
from the report. `_prepare_route` (pilot.py) owns this: `enumerate_trace_choices` lists the
routes, `avoid_pred`/`via_pred` prune them (`_route_forces` — avoid drops a route that forces
the predicate, via drops one that does not), then the cheapest survivor is locked
(`writer_route_eligible` retentive+input-gated routes preferred, `_trace_score` next,
`route_rung_order` breaking ties). The internal lock threaded through the loop is the
`route` param (a `TraceChoice`); `ctx.route` / `trace_back(route=...)`.

A single writer whose OR has **any fully-steerable arm** is still collapsed by
`enumerate_trace_choices` (`_or_ambiguity_over_inputs` / `_arm_fully_steerable`) — it returns
no routes, so `Path.route` is `None`: a bare input (`Or(Auto, Manual)`) **or an `And` of
inputs** (the manual-jog `And(Manual, DiverterBtn)` beside the internal auto-sort
`And(State==SORTING, IsLarge, Auto)`) is a route PILOT asserts directly and the trace's own
Or-scorer lands on the cheapest arm; `via=` still steers onto the internal arm via the
Or-scorer's via preference (the dual of the existing avoid skip). Multi-writer routes and an
OR over coils (`Or(ProdMode, MaintMode)`) become genuine `RouteTaken` pivots — salient when
gated by a non-steerable discriminator, hidden from the headline when trivially all-input.

Hard limit: the static read is valid **only while the jump/enable tables are constants
that are never rewritten**. The moment enablement depends on a live word (e.g.
`mask & A_CurDisabledStates_HEX`), trace must return UNKNOWN — never fabricate an edge.

### 2. `let-run` — read the current  (`_try_zoom` / `_letrun_zoom` in `steer.py`)

When the bearing points at a **self-advancing frontier** — a timer or step-counter that
completes on its own under the currently-held state (`Blower__init`→1 while `S_Starting`
drives the calls) — hold heading and let scans pass. Everything live, no isolation.

The primary mechanism is **zoom**: fork, install prerequisite holds, `run_until` the
governing register hits its target value (with an ejection guard that stops immediately
if the register goes somewhere unexpected). Zoom results flow through the same
`verify_gates` pipeline as command pulses — SPIN if nothing moved, CONFIRMED if the
governing register transitioned, AMBIENT_DRIFT if the program ejected.

A bare cone-settle fallback exists at the bottom of the loop as last resort when
neither zoom nor command candidates apply.

Owns: completion *dwell*. This is what closes automatic/completion transitions
(Starting→Execute).

### 3. `sandbox` — send out a skiff  (`sandbox.py`)

When the map is genuinely **unreadable** — a live writer guard (`live_guard` on the
TraceNode) or an opaque-cut pipeline governor no static instrument produced a plan
for — run isolated experiments: fork, pin every mutable tag outside the frontier's
upstream cone to its pre-scan value, apply the tree's readable steerable context plus
one unprobed candidate action (singles first, then pairs — a runtime-gated transition
often needs a command AND an enablement select in one window), step, observe.

**Wired** (`probe_live_guard_frontiers`, called from both stuck exits in the drive
loop): observed moves are recorded into the compass — a pair records a *composite*
cause (tuple of action pairs) that candidates propose as a `prescribed_batch` through
the same gate pipeline as any trial. Probes are restricted to condition-read steerable
tags (a lever the program decides on — never a data-only constant-table row), and
`Compass.contradict` lets live no-change evidence falsify a statically-seeded edge so
it cannot shadow a genuine skiff-learned one in `find_path`.

Invariant: skiff results only ever feed `Compass.record` — a learned edge is a
*bearing*, never a plan step. Confirmed edges come exclusively from the verify
pipeline. The static need→route bridging (`roles_for_needed_tag`,
`expand_pipeline_need`) lives in `evidence.py`; `sandbox.py` is purely the
fork-pin-step instrument.

Next tier (strict xfail in the gate file): a free external word feeding the guard —
no sound probe values exist and the unblock is a *sequence*, so it needs value
synthesis / establish staging, not more probing.

Note: the sandbox and the (unwired) rejection arm of `table_oracle.guard_satisfiable`
are gated on the **same missing case** — a writer/enable guard over a genuinely-live
word (not steerable, not constant, not finite-domain). Everything softer than that
stays static: a mask gate like `stateMask[State] & disabledMask[Mode]` *looks*
runtime-computed but is constant-table-backed, so the oracle reads it at level 2 (this
is why `how()` into a mode-disabled state never needed the skiff). The trigger that
routes trace to the oracle keys on soundly derivable *fire-time pins*
(`_transition_fire_pins` — inverted copy/affine-calc bindings plus the guard's own
required conjuncts), not on any writer silhouette; a transition whose pins aren't
derivable punts. When a truly-live guard appears, `guard_satisfiable` is what tries
first — and *punts* — and the sandbox is its escalation.

## Escalation rule

Read first; execute only when reading isn't enough.

1. `trace` (transparent) — cheapest, no execution.
2. `trace` (opaque-but-constant value graph) — still static.
3. `let-run` — when the surfaced frontier self-advances under the held state.
4. `sandbox` — only when trace returns UNKNOWN for a runtime-computed edge.

## Cross-cutting invariant: state-consistent writer selection

A multi-writer pipeline tag (`S_StateCompleteBool`, `isStateEnbl_Yes`) must be traced
through the writer whose guard is **already consistent with the held state**, not the
writer with the fewest open leaves. Minimizing open leaves picks counterfactual branches
(`S_Clearing` needing `S_StateCurrent=1`, or the runtime-mask rung) over the live one
(`S_Starting ∧ Blower__init ∧ Rotate__init`). This is the single change that makes the
real prerequisites surface, and it appears in two places (`isStateEnbl_Yes`,
`S_StateCompleteBool`).

## Boundary gates (the acceptance tests)

**Trace + let-run** — the burner **Starting→Execute** transition, end to end: trace
surfaces `Blower__init==1` / `Rotate__init==1` as the frontier (via state-consistent
writer selection), let-run coasts them to completion, and `sample_pilot_events.py`
drives distance → 0 (`y_BurnerLoop=True`). Sandbox is *not* needed for this case — if
a change makes it look needed, the bug is in trace's writer selection.

**Sandbox** — `tests/core/analysis/test_pilot_sandbox_gate.py`, two tiers of the
live-word mask gate (same `stateMask & disabledMask == 0` shape as the oracle-solved
case, but the disabled word is live). **Command-selected tier: PASSING** — the mask is
picked among constant-table rows by Bool commands (two writers, every static read
punts); the skiff's pair probe learns the joint edge, the compass proposes it as a
batch, the verify pipeline confirms live. **Free-word tier: strict xfail** — the mask
is copied from an unconstrained external word; needs value synthesis / establish
staging. Both tiers keep the honesty pins: hand-driveable ground truth,
`solve_table_predicate` punt, named-reason failures.

## Module map

- `pilot.py` — the drive loop: iteration prep, candidate selection, commit/revert,
  entry points (`pilot_events`, `pilot_how`, `pilot_drive`).  The conductor.
- `steer.py` — Act instrument: cone settlement, pulse execution, zoom through timer
  plateaus, try-verify wrappers, candidate value proposals.
- `verify.py` — gate pipeline for trial acceptance (SPIN, CYCLE, DEAD-END, outcome).
- `progress.py` — trend monitoring, checkpoint lifecycle, regression recovery.
- `candidates.py` — compass bearing → ranked candidate list, prerequisite/command split,
  zoom prescription.
- `outcome.py` — four-outcome classifier (who moved what).
- `investigate.py` — bounded incident investigation: deviation capture, hypothesis
  generation, replay-confirmed holds.  Draws hypotheses from `_precise_cause` (one
  cause-chain walk) and `corrections.correct_enablers`.
- `corrections.py` — the "no steerable trigger → corrective hold" classifier that
  consolidated the old `_latch_exposure_hypotheses` / `_done_boundary_hypotheses` /
  `_liveness_hypotheses` passes.  Two arms over one output vocabulary
  (FLIP / FREEZE / OSCILLATE): a coil-latch arm (flip a non-state guard) and an
  accumulator arm keyed off `accumulating_profile()` (oscillate a complement-reset
  watchdog, freeze a held advance or an `Acc > Target` threshold).  Dispatch is by
  instruction class and profile, never by name; every hypothesis is replay-tested.
  Known coverage limits (deliberate honesty, not idiom leaks): the menu is closed
  (no multi-lever or pulse-sequence fixes), and a reset/advance condition with more
  than one read is skipped rather than guessed.
- `accumulators.py` — accumulator resolver: maps an ejecting consumer tag (Done bit
  or `Acc` register) to its owning instruction's
  `accumulating_profile()` (`core/instruction/accumulating.py`).  `scans_to_eject`
  is two-tier — analytic for timers/counters, empirical (fork-and-run) fallback for
  anything whose `scans_until` is unknown (drums today, once they return a profile).
- `cyclefold.py` — folds "active-hold soaks": sub-cycles the pilot must keep
  animating every scan (installed oscillations, watchdog pets) that defeat both the
  runner's plateau fold and the dt-knob.  `detect_cycle` finds the smallest period
  where every tag is boundary-stable or a certified monotone accumulator; every
  unresolved path fails closed (step instead of fold) — mis-set caps cost
  performance, never correctness.  Wired via `_ops._coast_holding_state`.
- `multitarget.py` — static mutual-exclusion prune + clobberer-first ordering for
  multi-target `how(A, B, …)`.  Prunes only what it can prove (same-tag conflict,
  mutual retentive clobber universally quantified over establish routes); everything
  unprovable falls open to the sequential drive, and the final all-targets check is
  the honest oracle.
- `causal.py` — cause-chain walker (`chase_cause_roots`), shared by gate pipeline,
  outcome classifier, and investigation.
- `types.py` — shared cross-boundary types (`_PilotContext`, `_PilotState`,
  `_IterationFrame`, `_PulseState`, `_TrialResult`, `_AttemptResult`, events, aliases).
- `_ops.py` — low-level PLC manipulation primitives (state-key projection, hold
  installation, pulse application, delayed-effect settlement).
- `trace.py` — backward trace engine (transparent static reader), `_all_nodes` utility.
- `compass.py` — opaque-but-constant value graph, influence map.
- `evidence.py` — static route/role expansion that trace reads.
- `sandbox.py` — isolated fork-and-observe experiments.
- `physical.py` — harness/feedback install on forks.

### Phase map

```
Compass     →  candidates.py, trace.py, compass.py
Act         →  steer.py (pulse + zoom + try-verify wrappers)
Verify      →  verify.py (gate pipeline → outcome.py classifies)
Investigate →  investigate.py (regression → hypotheses → replay), corrections.py
Progress    →  progress.py (trend + checkpoints + regression recovery)
Multi-goal  →  multitarget.py (static ME prune + ordering for how(A, B, …))
Shared      →  types.py, _ops.py, causal.py, cyclefold.py
```

