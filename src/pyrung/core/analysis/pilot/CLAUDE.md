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
             values; the loop applies them (_record_attempt + the skiff call
             sites) unconditionally, before ASSESS can revert the world —
             always as bearings, never plan steps.
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
  The conductor.
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
- `compass.py` — the knowledge store: the learned transition table
  (`record` / `contradict` / `record_no_change` / `find_path` / `off_path_actions` /
  `seed_routes`, the driver/observation types); `CompassObservation` + `Compass.apply` —
  the RECORD-phase write path instruments return values into.
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
  `Outcome.CONFIRMED`.
- `progress.py` — trend monitoring, checkpoint lifecycle (frontier capture, self-defeat
  release), regression recovery; the progress-not-departure bearing screen.
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
  events, aliases).
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
**knowledge commits, the world reverts.** The compass never rolls back — a checkpoint today is
`_Checkpoint(key, fork, trend, frontier)` with no compass snapshot, and that is correct: roll
back probe marks and the skiff's singles→pairs escalation never terminates. Every step below
preserves this line.

0. **Two open findings in the investigation/ranking territory** (the compass bridge itself has
   landed — see `causal.py` in the module map). The investigation **replay window is too short**
   to see slow consequences (it once accepted a first-scan-simulation oscillation that wrecks the
   state machine one scan after the window closes — ranking now keeps it from winning, but the
   window is still blind); and the burner's offline `A_Alm100_Status` free-word decline appears
   **iteration-order dependent** (some runs decline at scan ~10, others sail past to Execute) —
   route-choice instability worth pinning down.

1. **One entry type.** An edge's lifecycle is smeared across the parallel `_transitions` /
   `_probed` dicts (`contradict` deletes from one, writes the other). Unify into one
   `CompassEntry` per `(tag, from_val, cause)` with a provenance field
   (SEEDED / OBSERVED / CONFIRMED / NO_CHANGE / CONTRADICTED); `contradict` *demotes* to a
   CONTRADICTED tombstone instead of deleting — a falsified seeded edge is negative knowledge,
   not a blank.
2. **Compass as a persistent value.** The entry table becomes a `pyrsistent` PMap of PRecords,
   advanced by an evolver in RECORD whose `.persistent()` at the commit point *is* the next
   compass; `_PilotContext` carries a compass value replaced once per iteration, not a shared
   mutable. Honesty becomes structure: `CONFIRMED` provenance constructible only via a factory
   owned by `outcome.py` (today's discipline, made grep-able). Keying decision: canonicalize at
   RECORD (bool→int; `hash(True)==hash(1)` makes loose equality mostly free) and keep
   `_values_match` where genuine fuzz lives (graph BFS, `ANY_FROM`). **Not a perf lever** —
   tables are tiny and off the hot path; don't let anyone "optimize" it back.
3. **World/Knowledge split of `_PilotState`.** Every field falls cleanly on one side: World
   (reverts) = `work`, `steps`, `step_contexts`, `best_trend`; Knowledge (commits) = compass,
   `nogoods`, `seen_keys`, `letrun_tried`, `journey`, `hold_log`, `skiff_decline`, and
   `forced_holds` (survive revert, re-installed onto the fork — the `fork_onto` pattern). Make
   World a persistent value and a checkpoint becomes a pointer; revert becomes
   `state.world = checkpoint.world` + hold re-install, deleting `revert_to`'s scan-cutoff
   filtering (a hand-reconstruction of what a pointer gives for free, currently kept in
   agreement with `build_replay_fn`'s cutoff by comment). *This* — not the compass — is where
   "checkpoints become pointers" applies.
4. **Named phases.** (Module moves LANDED — `compass.py` now keeps only the knowledge store;
   static graph building (`CompassGraph`, edge/action-lookup bridging) and opaque-pipeline
   detection (`detect_opaque_loop` / `detect_opaque_pipelines`) moved to `statics.py`; see the
   module map.) Still open: promote the loop to ORIENT / ACT / VERIFY / RECORD / ASSESS, with
   Compass a noun (never a phase) and the reading-escalation ladder inside ORIENT (one call
   site); ASSESS names what `progress.py` already is. The writer-availability layer was carved
   out of `trace.py` into `availability.py` at the captain's direction (a move-only split — the
   `_WriterAvailability` verdict, its classifiers, the guard-reduction helpers, and
   `_equality_gated_coil`); the **recursion core remains singular** in `trace.py` (`_trace_back`,
   `_trace_expression`, `_rank_writers`, route enumeration, `TraceNode`/`TraceAction`,
   `frontier_pairs`, the steerability classifications) — that part is the most gate-protected code
   here, and splitting it further would be churn, not architecture.

Each step lands green against the existing boundary gates (burner Starting→Execute, the
sandbox gate pair) with no new instruments.
