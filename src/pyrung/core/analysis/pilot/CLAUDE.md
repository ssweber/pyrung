# pilot/ — steer a running PLC program

PILOT drives a program from its current state toward conditions chosen by the
user. It is a pilot, not a planner: it does not compute a sequence and then
execute it. The PLC continues to run, write registers, complete timers, and
invalidate assumptions between observations. PILOT therefore reads the current
world again before choosing each direction.

The concrete execution oracle is always a PLC fork. Static analysis proposes
what may work; trial verification and later progress handling decide what the
live result means.

Use WWTD — *what would the tech do?* — as the first-principles check: read the
ladder and trend first, make the smallest reversible intervention, then observe
what the program actually did.

## Working principles

### Recompute from the current world

Do not store a suffix of actions to execute later. Every iteration rebuilds the
trace and candidate set from the current snapshot, static evidence, accumulated
transition knowledge, active holds, avoid constraints, and world-keyed nogoods.

The exception is the user's explicit trace-route lock. `_prepare_route` chooses
it once before the loop because `via=` expresses positive user intent. `avoid=`
remains a constraint at every read/action/scan gate. An inferred root route is
one revocable commitment, not a queued suffix: re-trace it from each current
world until exact world-scoped rejections exhaust every live action, then revoke
it and re-enumerate root routes from that world. A graph path or learned
transition is evidence for the next action only.

### Read before probing

Escalate according to what remains unreadable:

1. `trace.py` follows writers, guards, copies, calculations, and
   instruction-owned cross-scan state through `advance.py`.
2. `availability.py`, `evidence.py`, `tide_tables.py`, and `currents.py` extend
   that read with current-state guards, pipeline structure, finite
   constant-backed tables, and program-awaited actions.
3. An `AdvanceProfile` states one next operation: conditions to hold or pulse,
   the observable boundary at which PILOT must read the world again, and an
   optional `AdvanceStep.progress` receipt. The receipt is owner-declared
   evidence that the operation is active when a quantized scalar (for example a
   seconds accumulator) cannot change on the next scan; fractional accumulator
   state remains simulator execution state, not public PILOT evidence.
4. `program_step.py` checks one exact producer in an unchanged fork and reports
   keep running, needs input, interrupted pipeline motion, or unclear. It does
   not choose an action. Observed pipeline motion makes the reading interrupted
   even when the producer exposes no external input; the current transition
   owner must be observed before option ordering may select an alternative.
5. `skiff.py` runs isolated fork probes only for a genuinely unreadable
   frontier.

An incomplete static read returns an unresolved requirement. It does not invent
an edge or silently convert uncertainty into impossibility.

### Try freely; reject only with proof

A proposed action is cheap because it is executed and checked on a fork.
Ranking may demote an action but must not remove one merely because it looks
unlikely.

A rejection is stronger: downstream code may never reconsider it. Static
rejection therefore requires a complete finite domain, such as Bool, prover
`nd_domains`, or declared `choices=`. In particular, callers using
`tide_tables.py` to prove a guard impossible must pass through
`trace._writer_guard_verdict`, which checks domain completeness first.

Failure to make progress is not proof that a transition is impossible.
Pending-departure expiry rolls the world back without creating a nogood.

### Bound loops and name failures

Every repeated activity must either consume a finite budget or accumulate
durable knowledge that prevents byte-identical repetition.

- `max_scans` and pending-departure lifetimes use the same committed `search_scan`
  coordinate. Accepted instruction-owned coast dwell is credited separately
  and cannot expire either bound.
- Skiff retries use a per-world-key budget and continue only when
  `Compass.apply` reports new knowledge.
- Pending program motion has a finite scan budget and exact rollback
  boundary.
- Revert cycles outside pending departure motion currently rely on accumulating
  nogoods or installed corrections rather than a separate counter.

A terminal result must name the outstanding frontier when one can be read.
Keep `reason`, `avoid_names`, `lever_notes`, journey, and hold receipts attached
to the result path that discovered them. Do not guess at a special cause when
probing produces no navigation evidence; stop and report the frontier.

### Give each decision one owner

Do not reproduce a decision in a second module for convenience. Shared callers
should consume the first owner's result.

- User trace route: `pilot.py::_prepare_route`
- Inferred root-route lifecycle: `orientation.py` selects only when no inferred
  commitment exists and returns `RouteExhausted`; `pilot.py` retains or revokes
  that one receipt and remembers world-scoped exhausted route identities
- Writer eligibility and order: `trace.py::_rank_writers`
- Instruction-owned channel lookup: `advance.py::AdvanceIndex`
- One exact producer's unchanged-world proof: `program_step.py::read_program_step`
- Current-world navigation result: `orientation.py::orient`, entered via the
  `compass.py::Compass.orient` facade
- Target-relative Bearing objective: `orientation.py::_bearing`; the original
  `TargetSpec` and complete unresolved frontier travel unchanged through
  execution and verification, and recovery consumes that receipt rather than
  rebuilding intent from the global context
- Option materialization and ranking evidence: `options.py::_build_candidates`
- Local trial gates: `verify.py::verify_gates`
- Evidence classification: `outcome.py::assess_outcome`
- Transition-knowledge update: `Compass.apply`, invoked by the drive loop
- Coast-departure channel ownership: `_ops.py::coast_departure_tags`; inferred
  pipeline channels remain sentinels, Gauge retains monotone progress
  coordinates, and an exact stateful target without a Gauge owner is the
  discrete fallback channel
- Post-commit retention, recovery, and correction installation: `progress.py`
- Corrective hypothesis derivation: `corrections.py`
- Corrective hypothesis replay and confirmation: `investigate.py`
- Corrective operation lifetime: the instruction owner, carried through
  `trace.py::TraceAction.operation`; `_ops.py::_set_rungs` only compiles that
  receipt and preserves an already-active owner by its progress witness
- Temporary-logic execution ownership: `_ops.py::_rung_execution_receipt`,
  produced from the same expanded branches `_set_rungs` installs; investigation,
  causal revocation, and recording consume its effective owner rather than
  re-evaluating raw guards

## Actual control flow

1. `pilot.py` snapshots the runtime world and calls `Compass.orient`.
2. Compass reads trace, catalog, currents, constraints, and knowledge, then
   returns exactly one `Bearing`, `NeedProbe`, or `Stuck`.
3. `steer.execute` rejects stale bearings, installs their declarative
   prerequisites, and executes exactly one act through `verify.verify_gates`.
4. `pilot.py::_record_attempt` applies all observations, including rejected
   attempts, before any further orientation.
5. A `RouteExhausted` result revokes only an inferred root-route commitment; an
   exhausted explicit `via=` lock is terminal. The next iteration re-enumerates
   inferred alternatives from its then-current world.
6. An accepted fork is committed and `progress.py` decides retention,
   pending continuation, investigation, or revert.
7. `NeedProbe` is executed only by `skiff.py`; observations or an explicit
   exhaustion mark are applied before orientation runs again.
8. `Stuck` is terminal. No candidate list or route suffix survives an
   observation.

Passing verification means "eligible to commit and assess", not "durable
progress". Use distinct language for those two decisions.

## World and knowledge

`types.py` and `compass.py` separate state that a revert may undo from
knowledge that must survive:

- `_World`: PLC fork, committed steps and contexts, active rungs, trend, and
  dwell accounting.
- `_PilotState` orchestration knowledge: seen keys, checkpoints, pending-departure
  recovery, gauge, correction receipts/revocations, and diagnostic history.
- `CompassKnowledge`: empirical transitions/tombstones, scoped nogoods, probe
  budgets/results, coast receipts, and static-edge evidence overlays.
- `_PilotContext`: static program analysis plus the current persistent
  `Compass` value.

`Compass.apply` returns a new compass and a `changed` flag. When no entry
changes, it returns the same object. Runtime instruments return
`CompassObservation` values and do not mutate the compass themselves.
Empirical transition and probe receipts are scoped to the exact executable
world, complete pre-transition snapshot, and applied action artifact that
proved them. A local tombstone cannot erase another recipe or co-action
context; within its own exact context it overrides deliberately global seeded
evidence. Static-edge overlays are narrower still: negative evidence attaches
only when the trial exercised that edge's exact action/co-action set while its
recorded concrete conditions held.

Trace alternatives consume the same evidence without taking ownership of it.
Orientation may project an exact current-world singleton Pulse rejection back
to its identical trace leaf; it must not project a rejected joint act onto one
member. Trace uses those exact leaf rejections only to order unlocked nested
writer/OR alternatives. A multi-leaf branch is a distinct, still-untested joint
artifact, and an unreadable branch is not a fallback. Trace retains the best
rejected branch when no pilotable alternative survives so the frontier remains
visible. Root writer/OR locks stay with the inferred/explicit route lifecycle
and are never redirected inside Trace.

`PendingDeparture` records a clean program departure whose progress is not yet
conclusive. It names the stable owner of its rollback checkpoint, the owner of
an optional saved-progress checkpoint, and a finite search-scan deadline.
The saved-progress owner is an irreversible recovery floor: expiry and
regression resolve its current executable artifact and may discard only work
after it. Until that owner exists, the opening rollback owner remains the floor.
Correction install/revoke may replace a checkpoint's executable artifact but
must preserve that owner. `TrialAssessment` and
gauge receipts carry the evidence; every observed unexpected departure still
enters the same incident, investigation, correction, and retry lifecycle before
pending policy is considered. `progress.py` first returns a plain
`DepartureDecision` (wait, promote, regress, or expire), then applies that
decision to the receipts owned by the pending record. The retained
`provisional_*` event names are compatibility vocabulary only; they do not name
an internal state or policy.

## Soundness and behavior invariants

- Writers that can produce the requested value remain eligible.
  Availability and wake/clobber heuristics order; they do not reject.
- "Still needed" has separate meanings:
  `frontier_pairs` reports unresolved needs in the selected trace tree;
  `_writer_projection` checks a writer under its fire-time overlay;
  `_expr_availability` compares a guard with the live snapshot.
- Avoidance is enforced when choosing a route, before applying an action, and
  across every intermediate scan of a trial.
- Active-cycle detection, crossing arithmetic, and folded jumps read the same
  timed scalar coordinate: public accumulator plus its fractional remainder.
  The remainder proves continued execution to the simulator, while the
  operation's `AdvanceStep.progress` receipt is PILOT's observable evidence.
- Learned or static route edges are suggestions. A live trial still passes the
  same verification gates.
- Static charts enumerate every source match, exact edges before wildcard
  edges. Callers own exclusions through `edge_allowed`: specificity is
  precedence, never a pre-filter veto of a surviving wildcard route.
- A convergence lookup is an ordered multimap of primary-action alternatives.
  Chart construction fans each alternative into its own edge; only a route's
  `edge_gates` are simultaneous co-actions.
- A program-written tag may be removed from the steerable set by recorded
  evidence. Empirical evidence never creates a new lever.
- A correction is installed only in the exact guarded form that survived
  replay, and only one competing explanation is installed for an incident.
- An accumulator correction asks only the owner that completed in the recorded
  incident for its reset operation. A plain trace handoff is a one-scan
  operation; an intermediate instruction contributes its own boundary and
  progress witness. Later opposite operations compose as temporal phases when
  their owner boundaries differ; bare contradictory holds still revoke.
- A corrective hold may succeed by advancing the target-relative gauge or by
  neutralizing its recorded regression while preserving both the incident's
  source context and its earned progress floor. Neutralization composes the
  incident-bounded changed-write receipt with the recorded and replacement
  causal spines: replay of the recorded writes while the source is preserved is
  masking, but a later operation may reuse every generic state-transition
  executor write when its causal spine no longer contains the recorded owner.
  Overwriting the original branch's result or erasing the progress coordinate
  that identified the incident is not correction. This contract is independent
  of whether the branch contains timers, latches, edges, comparisons, or
  ordinary logic. A hold does not have to finish the remaining route.
- Replay confirmation is probationary knowledge. If a later exact incident
  causally contradicts an active correction, `progress.py` revokes its receipt,
  removes its rungs, and excludes that correction at its origin. A
  replay-confirmed replacement is installed only after that removal, making the
  change an ownership handoff rather than two competing holds.
- `PilotRung` is executable form, not correction provenance. Only rungs named by
  active correction receipts may renegotiate their concrete value from a later
  incident boundary; prerequisites and route holds cannot enter the correction
  lifecycle merely because they compile to the same rung type.
- Every investigation returns one correction artifact containing the exact
  guarded rungs, causal sources, identity, and replay justification it proved;
  consumers do not reconstruct that artifact from parallel result fields.
  Excursion verification additionally rechecks the corrected replay against
  `avoid=`. The shared installer rejects forged identities and already-owned
  rungs, then records a lifecycle receipt containing that correction artifact
  without copying or recompiling it. Prerequisite installation likewise reuses
  an identical existing rung without claiming it; the first installer remains
  its sole owner. Hold-log tag summaries are derived from their exact rungs.
  Installation banks active correction artifacts into every revert anchor;
  revocation removes them from every anchor symmetrically. The runner, world
  key, checkpoint state, and receipt must therefore name the same rungs.
- A terminal coast consumes the same channel-owner set during execution and
  incident replay. Exact stateful target motion that Gauge does not own is a
  recorded channel departure; it cannot be flattened into a timeout merely
  because pipeline inference found no operator-request role.
- Coast predicates decide bump truth. Compiled conditions provide fold metadata
  only. Every reported crossing lands on a real recorded scan.
- Cycle folding, table inversion, producer recognition, and departure
  classification decline when their proof requirements are not met.
- Static multi-target rejection is conservative. Concrete execution and the
  final all-target check remain authoritative.

## Navigation

### Orchestration and package surface

- `pilot.py` — shared drive preparation, target-context construction, user
  route lock, event loop, knowledge application, world commit, terminal
  results, and public drive entry points.
- `recording.py` — pure event-payload, terminal-frontier, and plan-journal
  rendering; it does not make drive decisions.
- `types.py` — cross-module protocols and world, trial, event, and incident
  records.
- `__init__.py` — package exports.
- `physical.py` — harness installation and feedback-tag exclusion.
- `multitarget.py` — conservative incompatibility proof and target ordering.

### Static reading and orientation

- `trace.py` — backward requirement tree, route enumeration, steerability, and
  writer ranking.
- `availability.py` — current-state writer availability used for ordering.
- `evidence.py` — pipeline-role inference and static transition-route expansion.
- `tide_tables.py` — finite constant-backed table and calculation preimages.
- `charts.py` — immutable static transition graphs, constrained path evidence,
  and opaque pipeline detection.
- `static_expressions.py` — low-level static-expression helpers shared by trace
  and tide readers.
- `compass.py` — thin immutable facade plus durable `CompassKnowledge`.
- `orientation.py` — current-world read, complete frame assembly, sole result
  synthesis, and terminal/probe policy.
- `options.py` — private evidence-rich option materialization and ranking.
- `navigation_evidence.py` — narrow constrained reachability evidence shared
  with verification and recovery; never returns an action.
- `currents.py` — structural program-awaited-action readings and producer
  families; Compass owns filtering and ambiguity policy.
- `advance.py` — unambiguous instruction-owned channel lookup and boundary
  estimates. Instruction semantics live in each instruction's `AdvanceProfile`.
- `program_step.py` — read-only unchanged-world proof for one exact producer;
  reports the immediate boundary, unmet input, or a pipeline interruption that
  must be observed before another route is selected.
- `navigation.py` — immutable evidence, act, result, target, constraint,
  target-relative Bearing objective, and world-view contracts.

### Execution and observation

- `steer.py` — forked action/coast execution and invocation of trial gates.
- `_ops.py` — shared PLC operations, world keys, temporary-logic compilation and
  effective-owner receipts, pulses, coast adapters, and action-admission checks.
- `coast.py` — bump-driven coasts with exact-scan receipts.
- `cyclefold.py` — proven active-cycle skipping during long waits.
- `skiff.py` — finite isolated probes of unreadable frontiers.

### Judgment and recovery

- `verify.py` — avoid, target, spin, cycle, dead-end, and outcome gates.
- `outcome.py` — agency, bearing, progress, and frontier evidence.
- `progress.py` — checkpoints, pending departures, regression recovery,
  correction installation, and reverts.
- `detour.py` — channel-departure classification for progress handling; reads
  the executed Bearing objective and never reconstructs a target objective.
- `gauge.py` — conservative target-relative earned-work marks and reset
  boundaries.
- `causal.py` — recorded cause-chain queries and empirical program-write
  evidence.
- `investigate.py` — incident construction, hypothesis ranking, and replay.
- `corrections.py` — scoped corrective-hold hypothesis derivation.

Module docstrings define the current local contracts. If a change moves a
decision between modules, update both affected docstrings and this navigation
entry in the same change.

## Vocabulary

Keep the terms that distinguish the model, and define specialized terms in
plain language on first use.

- **pilot** — continuously steers a running program; never executes a stored
  plan.
- **bearing** — the next direction recomputed from the current world.
- **compass** — the `Compass` value containing static graph references and
  persistent transition knowledge.
- **coast** — hold the required inputs while scans pass.
- **skiff** — an isolated fork probe for an unreadable frontier.
- **tide table** — a finite solver for constant-backed transition-availability
  conditions.
- **current** — program-owned motion or the unique operator action that motion
  currently awaits.
- **frontier** — unresolved non-steerable requirements in the selected trace
  tree.
- **cone** — a region of tags upstream of a requirement; settling a cone is an
  execution operation.
- **gauge** — conservative target-relative evidence of earned work.

Avoid extending the nautical metaphor in technical contracts. Words such as
captain, vessel, reef, shipyard, and waters add a translation step without
naming code abstractions.

## Testing changes

Run:

```text
make test-pilot
make lint
```

Use the focused gate before the full suite when changing a risky invariant:

- rejection/domain completeness:
  `test_pilot_rejection_arm.py`, `test_pilot_sandbox_gate.py`
- trial gates and outcome attribution:
  `test_pilot_verify.py`, `test_pilot_outcome.py`
- compass learning and recording:
  `test_pilot_recording.py`, `test_pilot_nogood.py`
- waits, coasts, and cycle folding:
  `test_pilot_candidate_wait.py`, `test_pilot_coast.py`,
  `test_pilot_cyclefold.py`
- departures, gauges, and recovery:
  `test_pilot_detour_progress.py`, `test_pilot_detour_hold_release.py`,
  `test_pilot_gauge.py`, `test_pilot_investigate.py`
- avoid semantics:
  `test_pilot_avoid_gates.py`
- writer selection and unresolved-need semantics:
  `test_pilot_trace.py`, `test_pilot_needed_vocabulary.py`

New mechanisms that can reject, wait, probe, install a hold, or revert need a
small program that demonstrates both success and an honest failure mode.

When a Tumbler golden changes during a PILOT refactor, find the first changed
decision without waiting for the whole golden test:

```text
uv run python devtools/pilot_divergence.py \
  --target y_BurnerLoop \
  --golden tests/tumbler/golden/how_y_burnerloop_skeleton.json
```

Use `--target Sts_StateCurrent=6` for an equality target and `--fixture` for a
different module that exposes `logic`. The tool stops at the first changed
event and prints the preceding matching events, the expected event, the actual
event, elapsed time, and the raw scan. Exit status 0 means the complete
skeleton matches; 1 means it diverged; 2 means setup or the wall budget failed.
It is a fast diagnosis loop, not a replacement for `make test-tumbler`.
