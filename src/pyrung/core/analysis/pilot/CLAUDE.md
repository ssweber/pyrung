# pilot/ — a harbor pilot for PLC programs

The user is the **captain** — decides the destination. The PLC program is the **ship** —
its own mass, inertia, timing, and logic. **PILOT** comes aboard, reads the charts, learns
the handling, and navigates the dangerous passage.

You share the helm: same register file, same scan cycle, no locks, no transactions. Your
actions ripple through rungs you haven't traced. The scan is atomic and you're on one side —
you set inputs, the PLC runs all its logic, you see the result, no mid-scan intervention. The
same input means different things in different states (Reset from STOPPED ≠ Reset from
EXECUTE). And some of the logic is unreadable — computed indices, runtime masks, indirect
addressing: you can see the rung but can't resolve it without running it. Every design rule
below falls out of these facts.

## The compass is a bearing, not a route

A compass does not plan a path. It holds a persistent *bearing* toward the target and
re-points as the state changes. PILOT is free to "sail around island" — lateral moves,
detours through the acceptance layers — but the compass always knows which way the target is.
When the loop wanders (oscillating, stuck on a distance plateau), the fix is almost always
**consult the compass**, not add another acceptance heuristic.

## Invariants (do not violate)

Each rule carries the fact that forces it.

- **Punt, never fabricate.** Some logic is genuinely unreadable, so a reader that cannot
  resolve an edge returns UNKNOWN — it never invents an edge, hold, alias, or value.
- **Complete domains only.** A rejection / pin / DEAD verdict is sound *only* over a
  provably-complete finite domain (prover `nd_domains`, declared `choices=`, Bool).
  Plausible-value fallbacks (`_index_values`, producible-literal chains) never reject —
  enumerating over an incomplete domain fabricates the proof. The invariant governs the
  *rejection arm*: `how()`'s Act may **propose** a heuristic value for a steerable free
  numeric word when the need arises from an invertible ordered comparison
  (`_heuristic_inequality_target`, trace.py) — the proposal is a trial like any other,
  replay-verified via Act→Verify, honestly reported as satisfying a *relation* (the value
  is an example, never a requirement), and structurally unreachable from `guard_verdict`,
  `_declared_domain`, `solve_calc_preimage`, and `nd_domains`. Heuristic values never
  reject, never pin, never prove DEAD.
- **Verify is the sole source of CONFIRMED.** You can't tell who wrote what — after a scan a
  register changed and it was you or the program. So every confirmed belief comes from the
  verify/outcome pipeline observing *who moved what*; nothing else promotes an edge.
- **Bearing, not route.** The same input means different things in different states and your
  actions ripple, so persistent knowledge is a re-pointable bearing, never a stored plan.
  Instruments *feed* the compass; the loop *reads* it fresh each iteration.
- **Legibility.** Every stall must be expressible as (concrete PLC state, frontier tag,
  outcome class). A mechanism that can stall somewhere you can't dump and point at is rejected.

**Cross-cutting — state-consistent writer selection.** A multi-writer pipeline tag
(`isStateEnbl_Yes`, `S_StateCompleteBool`) must be traced through the writer whose guard is
**already consistent with the held state**, not the one with the fewest open leaves (which
picks counterfactual branches). Enforced by the shared `_rank_writers` (trace.py), called from
*both* the transparent walk (`_trace_back`) and route enumeration (`enumerate_trace_choices`) —
a real shared function, not duplicated convention. The burner gate is the end-to-end check.
Two refinements ride the same ranking: a **maintenance writer** — fireable only by pressing a
*clear-only* lever off the natural path (`fill(1, CurStep)` gated `Or(xInit, xReset)`) — ranks
below any self-advancing value-step writer (kept as fallback, never the default route); and
the clear-only set itself (`compute_clear_only`, the ack-cleared momentary idiom — the program
clears it every scan, so its idiom is **pulse-and-release**) joins the pulse-treatment set in
candidates.py, never a prerequisite *hold*. Levers must serve the plan: a prerequisite hold
that statically defeats the tree's own frontier (`hold_defeats_needed` vs `frontier_pairs`) is
skipped at the install site, an investigation hypothesis whose holds change nothing
(`_hold_is_noop`) is rejected, and a watch tag that moved *toward* a checkpoint-frontier value
is progress, not a bearing departure. The ranking's own `_WriterAvailability` verdict
(`AVAILABLE_NOW` / `AFTER_PREREQ` / `UNKNOWN` / `UNAVAILABLE_FROM_HERE`, state-indexed against
the live snapshot) no longer dies inside the ranker: the chosen writer stamps it on its
`TraceNode`, `_collect_ordered` folds it worst-wins (the And-rule) down each path onto the
steerable leaf's `TraceAction.availability`, and candidates.py uses it as the **leading demotion
tier** when ordering command candidates — leaves serving reachable-from-here chains try before
UNKNOWN before UNAVAILABLE, sinking the command-leaf sprawl a cyclic state machine emits (every
unsatisfied leaf across the machine contributes a command at once) below the commands actually
fireable from the held state. Availability **orders, never rejects**: prescribed edges keep top
priority, blast/compass stay tie-breakers, and no candidate is dropped.

## The loop

```
Compass    — the knowledge store: static value-graph + learned transitions
             (compass.py). Consulted for a bearing → ranked candidates
             (candidates.py). Never executed as a plan.
Act        — steer toward the bearing: command pulse, prescribed batch, or
             zoom through timer/counter dwell (steer.py). Execution lives here.
Verify     — four-outcome classification of who moved what (verify.py →
             outcome.py). The sole source of CONFIRMED.
             1. I moved it where I wanted.   → confirmed edge
             2. I moved it wrong.            → bad edge; correct the compass
             3. The PLC moved it wrong.      → my command was a no-op; the
                                              program has its own agenda
             4. Nothing happened / frontier. → unmet prerequisite; trace why
Record     — the sole compass write path. Instruments never write: steer's
             try-verify wrappers and the skiff return CompassObservation
             values; the loop applies them (_record_attempt + the skiff tier
             _orient_escalate_skiff, ORIENT's last reading escalation)
             unconditionally, before ASSESS can revert the world — always as
             bearings, never plan steps.
Progress   — trend + checkpoint + revert (progress.py). "Distance" is the
             trace tree's unsatisfied-leaf count (TraceNode.unsatisfied_count):
             distinct unsatisfied, non-steerable prerequisites. Improved →
             checkpoint; plateau → re-orient (escalate a reading tier, never a
             new heuristic in Act); sustained decline → revert to checkpoint.
             A checkpoint (_Checkpoint) carries the launching frame's frontier
             (trace.frontier_pairs) — the coast frame that later regresses has
             no tree, so investigation reads its "needed" here. At revert,
             already-installed holds the frontier proves self-defeating are
             RELEASED, not faithfully re-installed.
Investigate— on regression: bounded incident (watch-tag motion TOWARD a
             frontier value is progress, not a departure) → hypotheses ranked
             by causal primacy (governing-departure chain membership, then
             temporal precedence) → admissibility (already-installed skipped;
             no-op holds and frontier-defeating holds rejected statically) →
             FIRST replay-confirmed hypothesis installed ALONE → revert
             (investigate.py, corrections.py). Hypotheses are competing
             explanations of one incident, never a bundle: the union of
             individually-replayed holds is an untested configuration, and a
             repeat regression escalates past installed incumbents.

Pre-pass, outside the loop:
Multi-goal — static mutual-exclusion prune + clobberer-first ordering →
             N single-target drives; final all-targets check is the honest
             oracle (multitarget.py).
```

**Scheduling: triggers, not positions.** The phase names say what *kind* of work something is,
not when it runs — VERIFY→RECORD→ASSESS run per-*trial* inside ACT's candidate loop, and
ORIENT's hardest tier fires after ACT is exhausted. What actually schedules the work is one
unconditional read plus three trigger-owned escalations:

| Escalation | Trigger | Owner |
|---|---|---|
| trace (transparent + value-graph) | every iteration — reading is free | `_prepare_iteration` |
| zoom / let-run | bearing points at a self-advancing frontier | `_try_zoom` / terminal let-run |
| skiff | stuck — no candidate, no bearing left to read | `_orient_escalate_skiff` |
| investigation | ASSESS sees a regression | `_investigate_and_revert` |

An escalation's loop position *is* its trigger condition — the skiff sits at the stuck exits
because "reading isn't enough anywhere else" is only knowable there. Do not "fix" an
escalation's position to make the ladder look sequential; that changes when it fires. Let-run
is deliberately two-natured: epistemically a *reading* (instrument #2 below), mechanically an
ACT tier — sometimes the only way to read the ship is to let it sail. The coherence test for
loop changes is not "is there one call site"; it is **"does every decision have exactly one
owner?"** (route: `_prepare_route`; writer: `_rank_writers`; candidate order:
`_build_candidates`; escalation: the table above; knowledge commit: `Compass.apply` at RECORD).

## The three instruments

All answer one question — *"I need `(tag = value)`; what must I do?"* — and differ only in how
much of the causal path is readable. **Read first; execute only when reading isn't enough:**
trace transparent → trace opaque-but-constant value graph → let-run dwell → sandbox.

### 1. trace — read the charts (trace.py)

Reads the map; runs nothing. Resolves a target backward to steerable inputs (a prerequisite
tree, `TraceNode`), escalating through three readings:

- **transparent walk** of writer conditions / copy / calc back to steerable inputs;
- **establish + preserve** — a retentive target (`tag not in rung.ote_writes`) must also survive
  competing writers (`_preserve_children`); a writer that *could* produce the value
  (`_can_produce` True) is **never** suppressed;
- **opaque-but-constant value navigation** — BFS the value space over declared-constant tables
  (`CompassGraph`); punts the moment a live word gates enablement.

Route choice reports a deterministic default on `Path.route` and redirects with `avoid=` / `via=`
(`_prepare_route`, the sole owner) — for any concrete equality target; a live relational target
(`State > 5`) drives without a route. A constant-table guard is rejected by the wired
`guard_verdict` arm (DEAD only over complete domains; PUNT → the sandbox), keyed on the writer's
fire-time pins (`_transition_fire_pins` / `solve_calc_preimage`).

**`avoid=` is a three-gate fan-out** (`via=` stays route-only). The user's contract: *avoid X = do
not take a path that depends on X — routes, operator actions, and observed scan states.* It is a
**union of exclusions** (`_AvoidPredicate` in types.py, built by `runner._compile_avoid`): each
condition is avoided independently (violation = OR across members), so `avoid=(A, B)` avoids either
while `avoid=And(A, B)` avoids only the joint state — every member keeps its printable name for the
decline. The three gates: (1) **route gate** — `_prepare_route` prunes routes `_route_forces` shows
forcing the predicate, and the per-arm OR-skip drops an avoided arm (`trace.py`); (2) **action gate**
— `steer._try_action_batch` rejects a candidate whose *applied overlay* trips the predicate on the
live snapshot **before** the pulse (so a momentary command is never pressed), `candidates` filters an
avoid-forcing prerequisite hold, and `_ops._hold_allowed` makes an investigation/correction hold that
drives an avoided tag inadmissible — all sharing `_ops._avoid_forces`; (3) **scan gate** —
`verify.verify_gates` vetoes the avoided predicate on the settled snapshot **and** on any transient
(pulse-scan / coast) snapshot, so there is no two-scan wink. An excluded trial nogoods its choice and
records the violated names (`_AttemptResult.avoid_names` → `_PilotState.avoid_names`); the terminal
decline names them via `_with_avoid_reason` (falling back to `_avoid_route_names` when the route gate
pruned silently). `avoid_pred=None` is byte-identical to the prior behavior.

### 2. let-run — read the current (steer.py)

When the bearing points at a **self-advancing frontier** — a timer or step-counter that
completes on its own under the held state — hold heading and let scans pass. The mechanism is
**zoom**: fork, install prerequisite holds, `run_until` the governing register hits its target
(with an ejection guard that stops on unexpected motion). Zoom results flow through the same
`verify_gates` as command pulses. Owns completion *dwell* (Starting→Execute).

### 3. sandbox — send out a skiff (sandbox.py)

When the map is genuinely **unreadable** — a live writer guard no static instrument produced a
plan for — run isolated experiments: fork, pin every tag outside the frontier's upstream cone,
apply the readable steerable context plus one unprobed candidate (singles, then pairs), step,
observe. **Wired** (`probe_live_guard_frontiers`, at both stuck exits): observed moves return
as observations the caller applies at its RECORD point; a pair carries a *composite* cause
proposed as a `prescribed_batch`; a contradict observation lets live no-change evidence
falsify a stale seeded edge. Probe candidates
are condition-read steerable tags (levers the program decides on) **plus** any steerable word
carrying a *declared* complete domain (`choices=` / `min`/`max`, `_declared_domain`) — an
external config word is a finite lever even when only data-read (a copy source). A steerable word
with **no** declared domain is a free word with no sound probe values: for an
enumeration-shaped gate (equality/mask/table selection) the skiff declines and
names it (`_frontier_free_words` → `state.skiff_decline`), nudging a `choices=` declaration rather
than guessing — but an *ordered-comparison* need on such a word gets the relational-lever
heuristic trial instead (trace's stage-3 boundary proposal, reported relationally with an
"e.g." value), so the decline stays the answer only where no comparison structure yields a
verifiable candidate. **Skiff results only ever feed the compass** (as observations applied at
RECORD) — a learned edge is a bearing, never a plan step.

The rejection arm and the sandbox gate on the **same** missing case — a guard over a
genuinely-live word. Everything softer stays static: a `stateMask & disabledMask` gate *looks*
runtime-computed but is constant-table-backed, so the oracle reads it. When a truly-live guard
appears, `guard_verdict` tries first and *punts*; the sandbox is its escalation.

## Module map

- `pilot.py` — the drive loop: iteration prep, candidate selection, route prep
  (`_prepare_route`), commit/revert, entry points (`pilot_events`, `pilot_how`, `pilot_drive`).
  The conductor. `_pilot_loop_events` is banner-sectioned by phase (ORIENT / ACT, with RECORD /
  VERIFY→ASSESS named at their commit points); `_orient_escalate_skiff` owns ORIENT's last
  reading tier (the skiff) for both stuck exits.
- `candidates.py` — compass bearing → ranked candidate list; prerequisite/command split;
  zoom prescription. Command candidates are ordered first by leaf writer-availability tier
  (`_availability_tier` over `TraceAction.availability`), then blast, then compass score —
  prescribed edges bypass all three.
- `trace.py` — backward trace engine (transparent static reader), route enumeration, the shared
  `_rank_writers` selector (state-consistent + maintenance-writer demotion), fire-time pins;
  the steerability classifications (`compute_steerable`, `compute_clear_only`) and
  `frontier_pairs` (the still_need extraction shared by display and checkpoint capture).
  The recursion core is singular here; the writer-availability layer was carved out to
  `availability.py` (imported at the top, re-exported to old callers).
- `availability.py` — the writer-availability layer carved out of `trace.py`: the
  `_WriterAvailability` verdict (`AVAILABLE_NOW` / `AFTER_PREREQ` / `UNKNOWN` /
  `UNAVAILABLE_FROM_HERE`) and its classifiers (`_writer_availability`, `_caller_availability` +
  its per-program `_caller_guard_ctx` lru wrapper, `_expr_availability`) plus the guard-reduction
  helpers that decide OR/And arms against a writer's own fire-time pins (`_reduce_guard_by_fire_pins`,
  `_reduce_guard_by_pin`, the `partial_eval` delegation `_partial_eval_guard` / `_guard_eval_atom`,
  the `_GUARD_CONTRADICTION` sentinel, `_simplified_expr_tags`), and the mode-flag governing-value
  aliasing `_equality_gated_coil`. Imports only lower layers (`simplified`, `sp_values`, `pdg`,
  `prove.expr`, `crossing`) plus a lazy hop into `evidence` — **never** `trace.py`, so there is no
  cycle. `trace.py`'s `_rank_writers` / `_trace_back` / `_route_conflict_tags` and the
  `TraceNode` / `TraceAction` availability fields read these by their bare (re-exported) names;
  `candidates.py` and `table_oracle.py` still import them from `trace`.
- `table_oracle.py` — constant-table predicate solvers: `guard_verdict` (three-valued rejection
  arm), `guard_satisfiable`, `solve_table_predicate`, `solve_calc_preimage`.
- `compass.py` — the knowledge store: the learned transition table as **one
  `CompassEntry` per `(tag, from_val, cause)`** (`record` / `contradict` /
  `record_no_change` / `find_path` / `off_path_actions` / `seed_routes`, the
  driver/observation types). An entry's `Provenance` *is* its lifecycle
  (SEEDED / OBSERVED / CONFIRMED live-and-traversable; NO_CHANGE / CONTRADICTED
  tombstones that traversal skips but still count as probe marks) — the old
  parallel `_transitions` / `_probed` dicts collapsed into one; `contradict`
  demotes a live edge to a CONTRADICTED tombstone (negative knowledge, not a
  blank). The entry table is a `pyrsistent` PMap of PRecords keyed
  `(tag, from_val, cause)` with bool→int canonicalized at write (`_canon`;
  `_values_match` stays where genuine fuzz lives — graph BFS, `ANY_FROM`);
  every write is a pure table op (`_table_record` / `_table_no_change` /
  `_table_contradict`), and `Compass.apply` — the RECORD-phase write path
  instruments return `CompassObservation` values into — folds a batch and
  **returns the next compass value**; the loop's single
  `ctx.compass = ctx.compass.apply(...)` assignment is the commit point.
  **Not a perf lever** — tables are tiny and off the hot path; the persistence
  is for the value semantics (knowledge never mutates under a holder), so don't
  "optimize" it back to shared dicts. CONFIRMED provenance is constructible
  only via `outcome.confirmed_entry` (`record` rejects it;
  `commit_confirmed` accepts only the prebuilt entry).
- `statics.py` — PILOT's static-analysis side: static value-graph building
  (`CompassGraph`, `build_compass_graphs`, `best_compass_plan`, the edge / action-lookup
  bridging helpers) and opaque-pipeline detection (`detect_opaque_loop` /
  `detect_opaque_pipelines`). Imported by `compass.py`; never imports it.
- `evidence.py` — static route/role expansion that trace reads
  (`roles_for_needed_tag`, `expand_pipeline_need`).
- `steer.py` — Act instrument: cone settlement, pulse execution, zoom through timer plateaus,
  try-verify wrappers (which *return* observations — Act never writes the compass), candidate
  value proposals.
- `sandbox.py` — isolated fork-pin-step experiments (`probe_live_guard_frontiers`).
- `verify.py` — gate pipeline for trial acceptance (SPIN, CYCLE, DEAD-END).
- `outcome.py` — four-outcome classifier (who moved what); the sole assigner of
  `Outcome.CONFIRMED` and the sole minter of CONFIRMED compass provenance
  (`confirmed_entry` — grep `Provenance.CONFIRMED`: the enum and this factory).
- `progress.py` — trend monitoring, checkpoint lifecycle (world-pointer capture via
  `state.snapshot_world`, frontier capture, self-defeat release), regression recovery via
  `state.load_world` (assignment, no scan-cutoff filtering); the progress-not-departure bearing
  screen.
- `investigate.py` — bounded incident investigation: deviation capture, hypothesis generation,
  causal-primacy ranking (`_rank_hypotheses`), admissibility (`_hold_is_noop`,
  `hold_defeats_needed`, already-installed skip), first-confirmed-wins replay. Antagonist
  suppression dispatches on **any causally-implicated** writer (`_implicated_writers` /
  `plc.cause` + a `_can_produce` producibility gate), never an instruction-class list;
  escalates to skiff nominations on a live-word punt.
- `corrections.py` — the "no steerable trigger → corrective hold" classifier over one vocabulary
  (FLIP / FREEZE / OSCILLATE): a coil-latch arm, an accumulator arm keyed off
  `accumulating_profile()`, and `break_guard_holds` (inverted-polarity guard suppression — a
  coordinated multi-lever set). Dispatch by instruction class + profile, never by name; every
  hypothesis replay-tested.
- `accumulators.py` — maps an ejecting consumer (Done bit / `Acc`) to its owning instruction's
  `accumulating_profile()`; `scans_to_eject` is two-tier (analytic, then empirical fork-and-run).
- `cyclefold.py` — folds active-hold soaks (installed oscillations, watchdog pets) that defeat
  the runner's plateau fold; fails closed (step, never mis-fold). Wired via `_coast_holding_state`.
- `multitarget.py` — static ME prune + clobberer-first ordering for `how(A, B, …)`; a pre-pass,
  not a loop phase. Prunes only what it can prove; everything else falls to the sequential drive.
- `causal.py` — cause-chain walkers, shared by gate pipeline, outcome classifier, and
  investigation: `chase_cause_roots` (steerable roots/holds) and `chase_chain_tags` (all chain
  tags — needed because an *absence*-caused ejection, a sensor that never moved starving a
  complement-reset watchdog, has no steerable mover at all). Both accept an opt-in `bridge=`
  (a duck-typed ctx exposing `.compass.graphs`): the **compass bridge** (`_bridge_pipeline_hop`)
  crosses the opaque-pipeline hop the recorded walk dead-ends on. Where `S_StateCurrent` is
  written by the jump-table indirect copy and `S_StateRequested` is a *held* enabler at the
  transfer scan (added by name, never recursed), the bridge consults the requesters' routes
  (`evidence.expand_routes`), confirms which route fired against recorded history
  (never fabricate an unconfirmed hop), and resumes the walk from that route's guard tags —
  reaching the starved watchdog directly. Wired only at investigation's ranking
  (`_rank_hypotheses`) and precise-cause (`_precise_cause`) sites; `bridge=None` is byte-identical
  to the prior behavior. Gate: `test_pilot_compass_bridge.py`.
- `physical.py` — harness/feedback install on forks.
- `types.py` — shared cross-boundary types (`_PilotContext`, `_PilotState`, `_IterationFrame`,
  events, aliases). Home of the World/Knowledge split: `_World` (a persistent `pyrsistent`
  PRecord — `work`, `steps`, `step_contexts`, `best_trend`) is the revertible half;
  `_PilotState.world` holds it and exposes the four fields by their bare names through
  read/write properties, so callers never touch `.world`. `_Checkpoint` points at a `_World`;
  `snapshot_world` freezes one (forking the runner) and `load_world` reverts to one by assignment.
- `_ops.py` — low-level PLC primitives: state-key projection, hold install (`ConditionalHold`),
  pulse application, delayed-effect settlement, `_coast_holding_state` / `_settle_cone`.

## Vocabulary (disambiguation)

- **hold** — a `ConditionalHold` (`_ops.py`): drives its tag *while* a guard holds, vs. pinning
  it to a fixed value.
- **widening** — two unrelated uses: `_try_widening` (steer.py) grows the candidate/action set;
  the value-lattice "Or-widens / And-narrows" (trace.py) is boolean-domain math.
- **cone** — the **upstream cone** is a tag *region* (`_cone_tags`); **cone settlement**
  (`_settle_cone` / `_coast_holding_state`) is the *operation* of coasting over it.
- **edge** — a **compass edge** (`CompassEdge`) is a learned transition; a **rise/fall edge**
  (`compute_edge_tags`) is a tag read through `rise()` / `fall()`.
- **pin** — a **fire-time pin** (`_transition_fire_pins`): the source value a writer forces to
  produce its result the scan it fires.
- **clear-only** — an ack-cleared momentary command (`compute_clear_only`): every program
  writer merely resets it to rest, so the operator supplies the active value and the idiom is
  pulse-and-release. Steerable, but never a hold and never a preferred init/reset route.
- **frontier** — the tree's outstanding non-steerable `(tag, value)` needs
  (`frontier_pairs`, BFS-ordered so the first value per tag is target-most and deeper ones
  are en-route stopovers). Distinct from a **frontier tag** in a stall dump (the unsatisfied
  leaf the loop points at).

## Boundary gates (the acceptance discipline)

Every new instrument or tier earns a hand-driveable, statically-punting, honestly-failing gate
program *before* the wiring, plus a strict xfail as the tripwire.

- **Trace + let-run** — the burner **Starting→Execute** transition end to end: trace surfaces
  `Blower__init==1` / `Rotate__init==1` as the frontier (via state-consistent selection),
  let-run coasts them to completion, distance → 0. Sandbox is *not* needed — if a change makes
  it look needed, the bug is in trace's writer selection.
- **Sandbox** (`test_pilot_sandbox_gate.py`) — the live-word mask gate.
  **Command-selected tier: passing** (mask picked among constant-table rows by Bool commands;
  every static read punts; the pair probe learns the joint edge and the verify pipeline confirms
  it live). **Free-word tier: a gate pair** (mask copied from an external word). Its resolution is
  not eventual reachability of the *undeclared* program — an unconstrained word has no complete
  domain — but two honest outcomes: (a) *undeclared* → an unreachable `Path` whose reason **names
  the word** and nudges `choices=` (threaded from the skiff's free-word detection through
  `state.skiff_decline` to the terminal stuck exit); (b) *declared* (`choices=` on the word) →
  the existing skiff resolves it with no new instrument — the declared values are sound probe
  candidates (`_declared_domain`), the pair probe learns the joint edge, verify confirms it live.
- **Free-word relational lever** (`test_pilot_free_word_lever.py`) — the fill shape:
  a step register advanced through a dwell gated on `PV < Lower`, both operands internal
  calc registers, `Lower = calc(SetPoint - Band)` with `Band` a steerable Real carrying
  **no** declared domain. Hand-driveable, statically punting (`_resolve_inequality_target`
  returns `None` on the literal-operand free-word atom), born strict-xfail and flipped when
  the stage-3 heuristic landed. Ordered comparisons only — the sandbox gate pair's
  equality/mask declines are untouched. The machine-local fill project is the live check
  (`scratchpad/burner/repro_fill_free_word.py`, not CI).
- **avoid= three gates** (`test_pilot_avoid_gates.py`) — hand-driveable, honestly-failing programs,
  one per gate: a **momentary command** (action gate must not press it — reaches via an alternate, or
  declines naming it with no alternate); a **route** (route gate picks the other arm — pinning
  coverage, the gate predates this); a **transient wink** (a step blips an avoided flag mid-coast then
  settles clear — the scan gate rejects it and the run declines); a **multi-avoid union** (`(A, B)` /
  `[A, B]` exclude either, `And(A, B)` only the joint); and the **hold admissibility seam**
  (`_hold_allowed` rejects a hold that drives an avoided tag — the one every corrective/prerequisite
  install site routes through). The DAP surface (`how … avoid A, B` = union) is pinned in
  `tests/dap/test_console.py`.
- **Self-defeating holds** (`test_pilot_self_defeating.py`) — unit tests prove
  `hold_defeats_needed`'s semantics with hand-fed `needed`; the seam test drives the REAL feed
  (terminal-letrun ejection, coast frame with no tree, `needed` from the checkpoint frontier)
  with only the investigation result stubbed. Born as a strict xfail that flipped when the
  checkpoint-frontier fix landed. The burner end-to-end (`how(y_BurnerLoop)` from cold,
  offline: mode+commands pulsed, doors held via investigation, rotate-sensor oscillation
  installed alone at the liveness regression, reached ~scan 2011) is the live check —
  machine-local (`scratchpad/burner/repro_regression.py`), not in CI.

## Future direction (delete each as it lands)

Everything above is how it stands today. Where it's heading. The anchor fact for all of it:
**knowledge commits, the world reverts.** The World/Knowledge split has LANDED (see `types.py` in
the module map): `_PilotState.world` is a persistent `_World` value (`work`, `steps`,
`step_contexts`, `best_trend`), a checkpoint is `_Checkpoint(key, world, trend, frontier)` — a
*pointer* to that world — and revert is `state.load_world(cp.world)`, plain assignment, no
scan-cutoff reconstruction. Everything not in `_World` (compass, `nogoods`, `seen_keys`,
`letrun_tried`, `journey`, `hold_log`, `skiff_decline`, `lever_notes`, and `forced_holds`) is
Knowledge: revert never touches it, so it commits; `forced_holds` re-installs onto the re-forked
runner (the `fork_onto` pattern). The compass never rolls back — roll back probe marks and the
skiff's singles→pairs escalation never terminates. Every step below preserves this line.

0. **Two open findings in the investigation/ranking territory** (the compass bridge itself has
   landed — see `causal.py` in the module map). The investigation **replay window is too short**
   to see slow consequences (it once accepted a first-scan-simulation oscillation that wrecks the
   state machine one scan after the window closes — ranking now keeps it from winning, but the
   window is still blind); and the burner's offline `A_Alm100_Status` free-word decline appears
   **iteration-order dependent** (some runs decline at scan ~10, others sail past to Execute) —
   route-choice instability worth pinning down.

1. **Named phases — LANDED (trimmed at the captain's direction).** The loop's five phases are
   now **named as structure, not carved into functions.** `_pilot_loop_events` opens with a
   phase map and carries `ORIENT` / `ACT` banners; the module docstrings name their phase
   (steer.py=ACT, verify.py + outcome.py=VERIFY, progress.py=ASSESS), and the RECORD /
   VERIFY→ASSESS commit points (`_record_attempt`, `_commit_and_monitor`) say so where they sit.
   Compass stays a **noun** (the knowledge store), Investigate is an **escalation inside ASSESS's
   regression arm** — both stated in the code. The one genuinely-architectural piece landed too:
   **ORIENT owns the reading-escalation ladder's last tier** via `_orient_escalate_skiff`
   (pilot.py), a single owned helper both stuck exits delegate to (probe → apply-at-RECORD → emit
   the `skiff` event → return whether to `continue`); the two sites differ only in the `reason`
   string and the event order is byte-identical to the old inlined form.
   **Deliberately NOT done** (judged churn, not architecture): carving the generator into
   per-phase functions, and renaming any function or module. Earlier tiers of the ladder (trace
   transparent → opaque-but-constant value graph) already live in one call — `_prepare_iteration`;
   the let-run dwell is an Act tier. Full "one call site for the whole ladder" is impossible
   without reordering events: the skiff fires only at a *stuck exit* (after candidates are
   exhausted / terminal let-run fails), a different loop point than where trace reads, so hoisting
   it into ORIENT prep would change *when* it fires. Naming it ORIENT's last tier + one owned
   helper is the largest honest consolidation. (Module moves LANDED earlier — `compass.py` keeps
   only the knowledge store; static graph building and opaque-pipeline detection moved to
   `statics.py`; the writer-availability layer split out of `trace.py` into `availability.py`. The
   **recursion core remains singular** in `trace.py` (`_trace_back`, `_trace_expression`,
   `_rank_writers`, route enumeration, `TraceNode`/`TraceAction`, `frontier_pairs`, the
   steerability classifications) — the most gate-protected code here; splitting it further would be
   churn.)

Each step lands green against the existing boundary gates (burner Starting→Execute, the
sandbox gate pair) with no new instruments.
