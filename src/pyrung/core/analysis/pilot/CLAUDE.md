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

`avoid=` remains a constraint at every read/action/scan gate. Condition-like
avoids carry their runtime condition
into trial coasts, so folding lands on any avoided crossing. Opaque callable
avoids have no readable fold proof and are checked at endpoints, retained real
snapshots, and kernel scans the coast actually executes; skipped logical scans
are outside that narrower contract. `rise()`/`fall()` avoids are rejected
because they are transitions rather than snapshot states. A graph path, trace
alternative, or learned transition is evidence for the next action only.

### Read before probing

Escalate according to what remains unreadable:

Every rung consumes the instruction-owned `AdvanceProfile` contract: one next
operation, the observable boundary for the next read, and an optional
`AdvanceStep.progress` receipt. The receipt is owner-declared evidence that an
operation is active when a quantized scalar cannot change on the next scan;
fractional accumulator state remains simulator execution state, not public
PILOT evidence.

1. `trace.py` follows writers, guards, copies, calculations, and
   instruction-owned cross-scan state through `advance.py`.
2. `availability.py`, `evidence.py`, `tide_tables.py`, and `currents.py` extend
   that read with current-state guards, pipeline structure, finite
   constant-backed tables, and program-awaited actions.
3. `program_step.py` checks one exact producer in an otherwise-unchanged fork,
   plus one counterfactual input patch per required input, and reports keep
   running, needs input, interrupted pipeline motion, or unclear. It does not
   choose an action. Its immutable `ProgramStep` derives the exact observable
   channel motions proved by that projection; candidate construction may select
   one with an outer route-channel preference, while Orientation never
   reconstructs motion from `projected_changes`. Observed pipeline motion makes
   the reading interrupted even when the producer exposes no external input;
   the current transition owner must be observed before option ordering may
   select an alternative.
   A requirement read while the program is crossing a boundary it owns belongs
   to the world after that crossing, not to this one. The settled projection is
   the disproof: an input the program is genuinely stopped at is still required
   once its own motion finishes. Such a reading keeps running with the crossing
   itself as the immediate boundary, so the caller coasts to its landing and the
   drive loop reads the settled world again.
4. `skiff.py` runs isolated fork probes only for a genuinely unreadable
   frontier.

The line between exact-producer proof and skiff is who may return an action, not
who may probe: `program_step.py` only reports a reading; skiff may propose one.

An incomplete static read returns an unresolved requirement. It does not invent
an edge or silently convert uncertainty into impossibility.

### Try freely; reject only with proof

A proposed action is cheap because it is executed and checked on a fork.
Ranking may demote an action but must not remove one merely because it looks
unlikely.

A rejection is stronger: downstream code may never reconsider it. Static
rejection therefore requires a complete finite domain, such as Bool, prover
`nd_domains`, or declared `choices=`. `tide_tables.guard_verdict` owns that
completeness requirement before it can return permanent `GUARD_DEAD`;
`trace._writer_guard_verdict` supplies writer fire pins and memoizes the owned
verdict.

Failure to make progress is not proof that a transition is impossible.
Pending-departure expiry rolls the world back without creating a nogood.

### Bound loops and name failures

Every repeated activity must either consume a finite budget or accumulate
durable knowledge that prevents byte-identical repetition.

- `_PilotState.search_scan` owns both `max_scans` and pending-departure
  lifetimes; accepted instruction-owned coast dwell is credited separately.
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
- Trace-tree structural traversal and unresolved interior identity:
  `trace.py::TraceNode.iter_nodes` and
  `trace.py::TraceNode.is_interior_frontier`. Breadth-first order is the
  package default; callers that need left-to-right preorder request
  depth-first explicitly. Collector-specific rules, including the relational
  unsatisfied-frontier stop and ordered-action context fold, remain explicit.
- Writer eligibility and order: `trace.py::_rank_writers`
- Unlocked local trace alternatives:
  `trace.py::_select_trace_alternative` returns an immutable
  `_TraceSelection`. Each `_TraceAlternative` records its caller-supplied rank
  and literal facts: whether it violates avoid, has no dead end, or is the exact
  rejected action. Expression OR, call-site, table-enablement, and nested-writer
  readers consume that decision. Call-site and table ranks put alternatives
  with no dead end first; OR keeps its structural rank, and writers keep
  `_rank_writers` order. Alternative-specific root writer/OR locks remain
  binding while each complete route is read; `rank_trace_choices` separately
  owns complete-route ranking. Nested writers remain lazy.
  Subroutine call sites are distinct program contexts, so caller selection
  records exact rejection but does not redirect to a different caller because
  of it. The rejected caller remains the honest frontier for higher-level
  recovery.
- Permanent guard rejection: `tide_tables.py::guard_verdict`; trace supplies
  writer fire pins and consumes the complete-domain verdict
- Instruction-owned channel lookup: `advance.py::AdvanceIndex`
- One exact producer's counterfactual proof: `program_step.py::read_program_step`
- Current-world navigation result: `orientation.py::orient`, entered via the
  `compass.py::Compass.orient` facade
- Current-world continuation evidence: `options.py::_current_work_evidence`;
  orientation groups live-work alternatives ahead of fresh alternatives
- Target-relative Bearing objective: `orientation.py::_bearing`; the original
  `TargetSpec` and complete unresolved frontier travel unchanged through
  execution and verification, and recovery consumes that receipt rather than
  rebuilding intent from the global context
- Navigation act policy: `orientation.py::_orient_read` materializes one
  immutable `navigation.ActPolicy` from the selected private option or wait.
  `steer.execute` applies that declared policy without decoding provenance;
  `_ExecutedAttempt` carries the original `Bearing` beside physical
  `_PulseState` evidence, and recording renders the same policy. A
  `ChannelHeading` is navigation's declaration; a coast's selected landing
  remains execution evidence on `_PulseState`.
- Option materialization and ranking: `options.py::_build_candidates`
  orchestrates four explicit current-world reads, then
  `_assemble_candidate_read` creates the sole durable `CandidateRead`.
  `_RouteAndCompletionRead` keeps static route and charted-completion evidence with
  the admission that produced them; `_PrerequisiteSeparation` carries the
  updated admission, executable prerequisites, and the still-unselected
  instruction-owned boundary. Learned fallback is an explicit wait, single
  action, or batch variant, while `_read_current_fallback` remains a separate
  program-current reading. `_select_wait` alone chooses among learned motion,
  charted completion, and an instruction-owned boundary with the established
  prescription and candidate gates. The immutable `CandidateRead` composes the
  final trace admission, optional `RouteRead`, `WaitRead`, prerequisite
  artifact, options, learned-batch variant, and diagnosis. `WaitRead.prescription` is
  `WaitPrescription | None`, so a present wait is valid by construction while
  a declined read retains its reason, producer evidence, frontier, and details.
  Orientation, recording, option assembly, and tests consume those owners
  directly; `CandidateRead` does not flatten them into a second scalar surface.
- Static chart-edge admission:
  `navigation_evidence.py::NavigationEvidence.static_edge_admission` returns a
  typed `StaticEdgeAdmission` with machine-readable exclusions for static
  status, current-world pair/wait/Pulse nogoods, blocked required actions, and
  avoid. It evaluates the complete required action overlay, including
  co-actions. Options, frontier evidence, and detour give graph search only its
  `allowed` projection. Options keeps unavailable-producer edges local to its
  candidate read. Detour composes the recovery-only `ContinuationSafety`
  receipt, whose named facts say whether a route erases earned progress or
  reopens completed work.
- Local trial gates: `verify.py::verify_gates`
- Accepted trial verification: `verify.py::verify_gates` preserves the exact
  `_ExecutedAttempt` inside one `_AcceptedTrial`, then constructs a frozen,
  PLC-free `_ExecutionEvidence` only after spin recovery has selected the final
  pulse. That receipt owns read-only copies of the final before/after snapshots,
  VERIFY's `ChannelMotion`, the typed `CoastReceipt`, and the exact
  `tuple[BumpEvent, ...]` timeline; `accelerators` derives from
  `CoastReceipt.advances`. Its required verification is either the
  `TargetReached` marker or an `AssessedMotion` carrying the accepted state key,
  trend, and `TrialAssessment`; legacy `Outcome` is derived from that assessment.
  Progress and recording narrow the variant before consuming assessed-motion
  facts. Gauge and gate receipts remain verification-owned evidence around the
  attempt.
- Committed operation context: `pilot.py::_step_context` carries the original
  `ActPolicy` and the exact `_ExecutionEvidence` object from `_AcceptedTrial`
  into `_StepContext`. Commit adds only unresolved frontier tags and exact
  executable control rungs. Physical replay `_Step` values stay on
  `_CommittedAct`; `_StepContext` derives policy, snapshots, channel, timeline,
  accelerator, and steady-hold views from their owners rather than storing
  parallel fields.
- Physical planning versus proof: orientation's
  `TraceReadConstraints.from_context` may use the live harness to propose a
  coupling driver; `verify.py::_gate_dead_end` omits that model and
  `_ops.py::_has_pending_effects` reads the executed fork for continued motion
- Trial-coast avoid observation: `coast.py::CoastSession.seek`; execution arms
  trial-start-clear members, `CoastReceipt.avoided` derives exact firings from
  typed events, and VERIFY consumes that receipt before target acceptance
- Target-relative movement: `gauge.py::Gauge.receipt` preserves each
  component's source, landing, and earn direction. Its `GaugeMovement` and
  `any_forward` views are derived independently, so mixed forward/backward
  motion remains visible. `verify.py` owns the accepted trial receipt and
  replaces it only when spin recovery replaces the fork; outcome and commit
  consume that receipt intact. Pending-departure policy creates one new receipt
  for each new anchor/current pair. A typed
  `DepartureBasis.PILOT_CAUSED_REGRESSION` may override retention policy without
  rewriting factual gauge movement.
- Departure observation and classification:
  `detour.py::classify_departure` returns a short-lived `DepartureResult`.
  Its frozen `DepartureObservation` owns the channel landing, exact
  `CoastReceipt`, one `GaugeReceipt`, causal `DepartureReading`, and the typed
  static/current continuation evidence actually consulted. The mutable settled
  PLC fork remains only an adoption handle on the result; neither the
  observation nor pending recovery retains it or an executable route suffix.
- Evidence classification: `outcome.py::assess_outcome`
- Transition-knowledge update: `Compass.apply`, invoked by the drive loop
- Coast-departure channel ownership: `_ops.py::coast_departure_tags`
- Post-commit retention, recovery, and correction installation: `progress.py`;
  its `PendingDeparture` embeds the opening `DepartureObservation` and adds
  only mutable policy anchors, rollback owners, and its deadline
- Corrective hypothesis derivation: `corrections.py`
- Corrective hypothesis replay and confirmation: `investigate.py`
- Corrective operation lifetime: the instruction owner, carried through
  `trace.py::TraceAction.operation`; `_ops.py::_set_rungs` only compiles that
  receipt and preserves an already-active owner by its progress witness
- Temporary-logic execution ownership: `_ops.py::_rung_execution_receipt` over
  the same `_expand_pilot_rules` branches installed by `_set_rungs`

## Actual control flow

1. `pilot.py` snapshots the runtime world and calls `Compass.orient`.
2. Compass reads trace, catalog, currents, constraints, and knowledge;
   `OrientationResult` permits exactly `Bearing | NeedProbe | Stuck`.
3. `steer.execute` rejects stale bearings, installs their declarative
   prerequisites, and executes exactly one act through `verify.verify_gates`.
4. `pilot.py::_record_attempt` applies all observations, including rejected
   attempts, before any further orientation.
5. An accepted fork is committed and `progress.py` decides retention,
   pending continuation, investigation, or revert.
6. `NeedProbe` is executed only by `skiff.py`; observations or an explicit
   exhaustion mark are applied before orientation runs again.
7. `Stuck` is terminal. No candidate list or route suffix survives an
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
member. Trace uses those exact leaf rejections only to order unlocked local
OR, table-value, and nested-writer alternatives. It does not redirect between
subroutine callers, which are distinct lifecycle contexts rather than alternate
recipes. A multi-leaf branch is a distinct, still-untested joint artifact, and
a branch with a dead end cannot replace a rejected branch. Trace retains the
best rejected branch when no untried alternative without a dead end survives so
the frontier remains visible. Root writer/OR locks stay with each inferred
alternative while Trace reads it and are never redirected inside Trace. Each
ranked writer is read through a fresh `_WriterBuild`: an isolated `TraceNode`
shell and copy of the caller's visited set. It completes to an immutable
`_WriterAttempt`; `_TraceAlternative` classifies that attempt and
`_TraceSelection` names the selected or honestly blocked attempt. Only that
retained attempt's children and complete visited state are adopted into the
caller's tree. Trace-wide caller locks and guard memoization remain shared
evidence rather than writer-local build state.
Static route selection applies the same boundary: it excludes the exact
current-world ``(primary action, co-actions)`` Pulse artifact that failed, then
may select a sibling edge carrying the same primary action under different
co-actions. Pair-level consumers see only explicit pair rejections and
singleton Pulses.

`PendingDeparture` records a clean program departure whose progress is not yet
conclusive. It carries detour's opening `DepartureObservation` intact, then
names the post-settlement progress mark, stable owner of its rollback
checkpoint, owner of an optional saved-progress checkpoint, and finite
search-scan deadline.
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
Correction nogoods are identities of replay-confirmed executable
`PilotRung`s, composed from `_ops.py::_rung_identity`; guard and operation
boundary are part of the disproved artifact. Raw `(tag, value)` hypotheses do
not own a scope and cannot be compared with revoked evidence until
investigation materializes their guarded installed form.

## Soundness and behavior invariants

- Writer eligibility and ordering: `trace.py::_rank_writers`; availability is
  only a sort key, never a rejection
- "Still needed" has separate meanings:
  `frontier_pairs` reports unresolved needs in the selected trace tree;
  `_writer_projection` checks a writer under its fire-time overlay;
  `_expr_availability` compares a guard with the live snapshot.
- Avoidance is enforced when choosing a route and before applying an action.
  Condition-like members are also enforced across every logical scan of a
  pre-commit trial; opaque callables cover endpoints, retained real snapshots,
  and executed kernel scans only.
- Active-cycle detection, crossing arithmetic, and folded jumps read the same
  timed scalar coordinate: public accumulator plus its fractional remainder.
  The remainder proves continued execution to the simulator, while the
  operation's `AdvanceStep.progress` receipt is PILOT's observable evidence.
- Learned or static route edges are suggestions. A live trial still passes the
  same verification gates.
- `ChannelHeading` owns an immediate observable channel/value/boundary and its
  optional outer `RouteEdgeContext`. `Coast` consumes that heading whole through
  `ActPolicy.heading`; consumers do not receive scalar channel or route copies.
  `options.py::_boundary_heading` lowers an instruction-owned boundary directly
  to that typed object. Prerequisite separation carries it whole and
  `_select_wait` may compose route context onto it; only the explicitly
  pair-typed wait frontier projects its channel and target.
- Accepted execution evidence contains no PLC, transient pulse/action/wait
  snapshots, world keys, correction or assessment state, or replay steps.
  `_AcceptedTrial` retains the physical `_ExecutedAttempt` separately; committed
  consumers share only the PLC-free `_ExecutionEvidence`. Recording's
  ordinary-fold accelerator fallback remains necessary until ordinary fold
  receipts own exact edits.
- A planning trace may read the live harness to identify a physical driver.
  VERIFY's post-trial dead-end trace deliberately omits that proposal model;
  continued physical motion needs executed evidence or a live pending effect on
  the exact trial fork.
- Static charts enumerate every source match, exact edges before wildcard
  edges. `StaticEdgeAdmission` owns shared current-world exclusions while
  callers compose only their local evidence; graph APIs consume the Boolean
  projection. Specificity is precedence, never a pre-filter veto of a
  surviving wildcard route.
- A convergence lookup is an ordered multimap of primary-action alternatives.
  Chart construction fans each alternative into its own edge; only a route's
  `edge_gates` are simultaneous co-actions.
- Static lever ownership: `Compass.action_tags`; `CompassKnowledge` cannot
  express a new lever.
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
- Correction lifecycle ownership: `_ConfirmedCorrection`, `_CorrectionReceipt`,
  and the derived `_HoldLogEntry.tags` / `_StepContext.steady_holds` properties.
  Excursion verification rechecks corrected replay against `avoid=`. The shared
  installer rejects forged identities and already-owned rungs; prerequisite
  installation reuses an identical rung without claiming it. Installation
  banks active corrections into every revert anchor, and revocation removes
  them symmetrically.
- Coast channel ownership across execution and replay:
  `_ops.py::coast_departure_tags`
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
- `types.py` — cross-module protocols and world, trial, event, incident, and
  accepted execution-evidence records; recovery policy records stay with
  `progress.py`.
- `__init__.py` — package exports.
- `physical.py` — harness installation and feedback-tag exclusion.
- `multitarget.py` — conservative incompatibility proof and target ordering.

### Static reading and orientation

- `trace.py` — backward requirement tree, stable tree traversal, unresolved
  interior identity, route enumeration, steerability, writer ranking, isolated
  writer builds, and selected/blocked attempt adoption.
- `availability.py` — current-state writer availability used for ordering.
- `evidence.py` — pipeline-role inference and static transition-route expansion.
- `tide_tables.py` — finite constant-backed table and calculation preimages,
  plus complete-domain guard verdicts.
- `charts.py` — immutable static transition graphs, constrained path evidence,
  and opaque pipeline detection.
- `static_expressions.py` — low-level static-expression helpers shared by trace
  and tide readers.
- `compass.py` — thin immutable facade plus durable `CompassKnowledge`.
- `orientation.py` — current-world read, complete frame assembly, sole result
  synthesis, and terminal/probe policy.
- `options.py` — phased private current-world readings, explicit wait-source
  selection, and final evidence-rich option materialization and ranking.
- `navigation_evidence.py` — narrow constrained reachability evidence shared
  with verification and recovery, including typed static-edge admission; never
  returns an action.
- `currents.py` — structural program-awaited-action readings and producer
  families; Compass owns filtering and ambiguity policy.
- `advance.py` — unambiguous instruction-owned channel lookup and boundary
  estimates. Instruction semantics live in each instruction's `AdvanceProfile`.
- `program_step.py` — counterfactual proof for one exact producer; reports a
  boundary, unmet input, or interruption but never an action.
- `navigation.py` — immutable evidence, act, result, target, constraint,
  target-relative Bearing objective, and world-view contracts.

### Execution and observation

- `steer.py` — forked action/coast execution and invocation of trial gates.
- `_ops.py` — shared PLC operations, world keys, temporary-logic compilation and
  effective-owner receipts, pulses, coast adapters, and action-admission checks.
- `coast.py` — bump-driven coasts with exact-scan receipts, including typed
  trial-avoid firings owned by execution.
- `cyclefold.py` — proven active-cycle skipping during long waits.
- `skiff.py` — finite isolated probes of unreadable frontiers.

### Judgment and recovery

- `verify.py` — avoid, target, spin, cycle, dead-end, and outcome gates.
- `outcome.py` — agency, bearing, progress, and frontier evidence.
- `progress.py` — checkpoints, the `PendingDeparture` policy record, regression
  recovery, correction installation, and reverts.
- `detour.py` — immutable channel-departure observation and typed
  classification for progress handling; reads the executed Bearing objective,
  composes recovery-only continuation safety, preserves exact source receipts,
  and never reconstructs a target objective or retains a route suffix.
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
