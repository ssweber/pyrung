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
re-points as the state changes. PILOT is free to "fly around the mountain" — lateral moves,
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
  enumerating over an incomplete domain fabricates the proof.
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
Record     — outcomes, plus skiff and dwell observations, feed
             Compass.record / .contradict — always as bearings, never plan
             steps. (verify.py holds no record call; recording is separate,
             in the steer.py try-verify wrappers and the skiff.)
Progress   — trend + checkpoint + revert (progress.py). "Distance" is the
             trace tree's unsatisfied-leaf count (TraceNode.unsatisfied_count):
             distinct unsatisfied, non-steerable prerequisites. Improved →
             checkpoint; plateau → re-orient (escalate a reading tier, never a
             new heuristic in Act); sustained decline → revert to checkpoint.
Investigate— on regression: bounded incident → hypotheses → replay-test each →
             apply confirmed holds → revert (investigate.py, corrections.py).

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

Reads the map; runs nothing.

- **Transparent backward resolution** — walk writer conditions / copy / calc back to steerable
  inputs; output a prerequisite tree (`TraceNode`).
- **Establish + preserve** — a retentive target (latch/SET coil, or copy/calc into a held
  register: `tag not in rung.ote_writes`) must both be *established* and *persist*.
  `_preserve_children` surfaces the negation of any **provable** clobber guard as ordinary
  prerequisite leaves that ride the same candidate/route pipeline. Honesty boundary: a writer
  whose value *could* be the target (`_can_produce` True) is **never** suppressed.
- **Opaque-but-constant value navigation** — when a writer is an indirect/computed jump over
  *declared constants + affine index*, invert it statically and BFS the value space
  (`CompassGraph`). Valid only while the jump/enable tables are constants never rewritten; the
  moment enablement depends on a live word, trace returns UNKNOWN.
- **Route choice — report and redirect.** `how()` never reports ambiguous: for multiple routes
  it picks a deterministic default and records it on `Path.route`; the engineer redirects with
  `avoid=` / `via=`. `_prepare_route` (pilot.py) is the sole owner; the `via=` onto-arm
  preference (steer onto an internal Or-arm — the dual of the avoid skip) is implemented, not
  aspirational. Applies to any concrete equality target (Bool, word, `Bool==False`); a live
  relational target (`State > 5`) drives without a route.
- **Table-oracle rejection arm** — a writer gated by a constant-table predicate recomputed each
  scan from the transition's own fire-time pins is checked by `guard_verdict` (three-valued),
  **wired** into `_trace_back` writer admission: DEAD → reject (complete domains only), PUNT →
  the sandbox's escalation signal. Fire-time pins come from inverted copy/affine bindings and
  non-affine calc preimages (`_transition_fire_pins` / `solve_calc_preimage`).

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
observe. **Wired** (`probe_live_guard_frontiers`, at both stuck exits): observed moves record
into the compass; a pair records a *composite* cause proposed as a `prescribed_batch`;
`Compass.contradict` lets live no-change evidence falsify a stale seeded edge. Probes are
restricted to condition-read steerable tags. **Skiff results only ever feed `Compass.record`** —
a learned edge is a bearing, never a plan step.

The rejection arm and the sandbox gate on the **same** missing case — a guard over a
genuinely-live word. Everything softer stays static: a `stateMask & disabledMask` gate *looks*
runtime-computed but is constant-table-backed, so the oracle reads it. When a truly-live guard
appears, `guard_verdict` tries first and *punts*; the sandbox is its escalation.

## Module map

- `pilot.py` — the drive loop: iteration prep, candidate selection, route prep
  (`_prepare_route`), commit/revert, entry points (`pilot_events`, `pilot_how`, `pilot_drive`).
  The conductor.
- `candidates.py` — compass bearing → ranked candidate list; prerequisite/command split;
  zoom prescription.
- `trace.py` — backward trace engine (transparent static reader), route enumeration, the shared
  `_rank_writers` selector, fire-time pins.
- `table_oracle.py` — constant-table predicate solvers: `guard_verdict` (three-valued rejection
  arm), `guard_satisfiable`, `solve_table_predicate`, `solve_calc_preimage`.
- `compass.py` — the knowledge store: static value-graph + learned transition table
  (`record` / `contradict` / `find_path`), influence map.
- `evidence.py` — static route/role expansion that trace reads
  (`roles_for_needed_tag`, `expand_pipeline_need`).
- `steer.py` — Act instrument: cone settlement, pulse execution, zoom through timer plateaus,
  try-verify wrappers (which record observations), candidate value proposals.
- `sandbox.py` — isolated fork-pin-step experiments (`probe_live_guard_frontiers`).
- `verify.py` — gate pipeline for trial acceptance (SPIN, CYCLE, DEAD-END).
- `outcome.py` — four-outcome classifier (who moved what); the sole assigner of
  `Outcome.CONFIRMED`.
- `progress.py` — trend monitoring, checkpoint lifecycle, regression recovery.
- `investigate.py` — bounded incident investigation: deviation capture, hypothesis generation,
  replay-confirmed holds. Antagonist suppression dispatches on **any causally-implicated**
  writer (`_implicated_writers` / `plc.cause` + a `_can_produce` producibility gate), never an
  instruction-class list; escalates to skiff nominations on a live-word punt.
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
- `causal.py` — cause-chain walker (`chase_cause_roots`), shared by gate pipeline, outcome
  classifier, and investigation.
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
  it live). **Free-word tier: strict xfail** (mask copied from an unconstrained external word).

## Future direction (delete each as it lands)

Everything above is how it stands today. Where it's heading:

- **Compass as a persistent value.** Today `record` / `contradict` mutate nested dicts in
  place. Target: a `pyrsistent` PMap of `PRecord` entries, one per `(tag, from_val, cause)`
  (unifying today's parallel `_transitions` / `_probed`), advanced by an evolver-per-iteration
  whose `.persistent()` at the commit point *is* the next compass. This makes "one write path"
  structural instead of disciplinary; checkpoints become pointers and revert becomes
  reassignment (deleting progress.py's copy/reconstruct); honesty becomes PRecord field
  invariants (a `CONFIRMED` entry constructible only by the verify pipeline). **Negative
  knowledge commits too** — a rejected trial still commits its probe mark / contradiction, or
  the skiff's singles→pairs escalation never terminates.
- **Named phases.** Promote the loop to ORIENT / ACT / VERIFY / RECORD / ASSESS, with Compass a
  noun (never a phase), the reading-escalation ladder living inside ORIENT (one call site), and
  RECORD holding the commit invariant once.
- **Resolve the cone-settle smell.** The bare cone-settle fallback (`pilot.py`, last resort
  after zoom/command/widening/let-run are exhausted) mutates `state.work` directly and skips
  verify — the one execution outside the act→verify→record chain. Route it through `verify_gates`
  like terminal let-run, or drop it.
- **Free-word skiff tier → honest `choices=` decline.** Not value synthesis: an unconstrained
  external word has no complete domain, so name it and nudge a `choices=` / domain declaration
  (the single source of truth the prover / bounds / validators / sandbox all read) — never a
  `how()`-only override. Once declared, the existing machinery resolves it with no new instrument.
- **Crisper module responsibilities.** Some modules still overlap; the target is one
  responsibility per module, legible from its entry point.
