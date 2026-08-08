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

## The happy path

The ordinary loop is small:

```text
read current world -> choose one bearing -> try it on a fork -> verify
-> record observations -> commit -> assess progress -> read again
```

On an uncomplicated turn, `trace.py` exposes a steerable leaf or an owned
boundary, `options.py` materializes it, orientation returns one `Bearing`,
`steer.py` executes it, verification accepts it, and progress handling banks a
checkpoint. The read is then discarded and the loop starts again from the
committed snapshot.

The rest of PILOT exists mainly to answer three questions when that path is not
straightforward:

1. Can PILOT read what should happen next?
2. Did the attempted action produce a trustworthy result?
3. Did the committed result become durable progress?

These are separate escalation boundaries. Extending a partial trace is not the
same decision as accepting a trial, and accepting a trial is not the same
decision as retaining its world.

### The Compass instrument panel

Compass does not replace PILOT's specialized readers with one monolithic
reasoner. It is the persistent navigation-knowledge facade and the entry point
for one fresh current-world orientation. Several instruments may contribute to
that read:

| Question | Evidence owner |
| --- | --- |
| Which local condition or lever leads toward the target? | `trace.py`, static expressions, availability |
| Which charted transition is relevant here? | navigation evidence and pipeline graphs |
| Can one exact program operation advance under otherwise-unchanged controls? | `program_step.py`, `AdvanceProfile`, `AdvanceIndex` |
| Is the program genuinely stopped at an external handoff? | `program_step.py`, awaited-action evidence |
| What has already worked or failed in this executable world? | `CompassKnowledge` |
| What must the next experiment respect? | avoid, active requirements, holds, and an optional theory view |
| Is the frontier unreadable without isolated experimentation? | `skiff.py` |

`options.py` materializes these readings into one `CandidateRead`, and
`orientation.py` applies the explicit current-world precedence to return one
`Bearing | NeedProbe | Stuck`. The readers contribute facts; none chooses an
action alone. They need not form a rigid fallback stack, and every complete
read expires after the next observation.

`program_step.py` asks a narrower question than whether the whole target will
eventually be reached. For one exact selected producer or instruction-owned
operation, it reads whether unchanged controls mean `KEEP_RUNNING`,
`NEEDS_INPUT`, `INTERRUPTED`, or `UNCLEAR`. Its coast, handoff, and interruption
evidence remains part of every relevant fresh orientation, including one read
from a WorkingTheory's accepted provisional state. WorkingTheory does not
privately decide whether to act or let the program run.

## The three escalation questions

### 1. Can PILOT read what should happen next?

`trace.py` is the base reader. It walks backward through writers, guards,
copies, calculations, and accumulating instructions until it reaches a
steerable action, an instruction-owned boundary, or an unresolved frontier.
The following modules extend that read:

1. `advance.py` supplies owner-declared cross-scan boundaries and progress
   receipts. `availability.py` orders currently available writers without
   rejecting the others. `tide_tables.py` resolves finite table/calc preimages
   and may permanently reject a guard only from a complete finite domain.
2. `evidence.py`, `pipeline_graph.py`, `static_expressions.py`, and
   `constrained_reachability.py` add static transition, route, and reachability
   evidence around the backward trace.
3. `options.py` combines the target trace with charted completion,
   instruction-owned boundaries, persistent Compass knowledge, and
   program-awaited actions. For a charted edge with one exact producer,
   `program_step.py` projects that producer in an otherwise-unchanged fork and
   reports `KEEP_RUNNING`, `NEEDS_INPUT`, `INTERRUPTED`, or `UNCLEAR`. It reports
   a reading; it never chooses an action.
4. Compass knowledge may supply one empirically learned wait, action, or joint
   action when the current static read has no local bearing. Learned evidence
   still passes the ordinary live-trial gates.
5. `requirements.py` retains an accepted expectation's exact producer and
   consumer occurrences with its source checkpoint. On a later regression,
   `progress.py` may match one causal occurrence to that receipt, derive an
   occurrence-scoped requirement, restore the exact checkpoint, and retry only
   the local act. The outer loop then performs a fresh current-world read.
6. Only a genuinely unresolved `NeedProbe` reaches `skiff.py`. Skiff pins
   unrelated state, probes a finite action domain on isolated forks, and
   returns observations without committing or choosing an action. Probe rounds
   are bounded per world key; exhaustion makes the next complete orientation
   return `Stuck`.

These are layers of evidence, not a rigid call stack: several static readers
contribute during the same orientation. Within the completed read,
`orientation.py` applies the explicit act precedence: prescribed wait, learned
joint action, individual candidates, then widened atomic action. If no concrete
act remains, it requests a probe or gives program-owned motion a bounded
terminal coast/dwell before probing. Every observation restarts the read from
the current world.

### 2. Did the attempted action produce a trustworthy result?

`steer.py` executes exactly one `Pulse`, `BatchPulse`, `Coast`, or `Dwell` on a
fork. All modes converge on `verify.py`:

1. Avoid and banked-work gates run before target acceptance.
2. `effects.py` observes an act's selected producer-to-consumer obligation over
   the exact execution window. Proved effect violations are recorded before
   generic spin/dead-end judgment; Phase 3 alone neither rejects them nor
   creates action nogoods.
3. Spin, cycle, and dead-end gates reject locally provable failures.
4. `outcome.py` classifies agency, bearing effect, target-relative progress,
   and frontier change. Passing makes the fork eligible for commit; it does not
   prove durable progress.
5. A suspicious excursion is the exceptional branch.
   `pilot.py::_resolve_excursion` invokes `investigate.py` at most once and
   passes the exact replay to `verify.verify_excursion_replay`, which continues
   the remaining gates.
6. A rejected act records its observations and exact world-scoped nogood, then
   returns to orientation. PILOT never advances to a sibling from a retained
   candidate list.

The executed steer is the shared evidence source for this judgment. Its
before/assertion/after snapshots, owner-bound ordered projection, effect
observations, gate results, and outcome are reused by every reader that needs
them. A reader does not fork and rerun the steer merely to answer whether a
producer appeared, a consumer saw it, or a later write overwrote it. If an
accepted landing needs later scans to establish durability, `progress.py`
consumes those normal real monitoring observations; it does not replay the
original steer. Missing shared evidence stays unresolved.

`overlay.py`, `pulse.py`, and `coast.py` implement the executable intervention
and observation receipts used by this layer. `cyclefold.py` may accelerate a
long coast only by skipping a proven cycle and landing back on a real recorded
scan.

### 3. Did the committed result become durable progress?

`progress.py` owns this question after a trial has passed verification:

1. A clean target-relative improvement banks or refreshes a checkpoint.
2. An exposed frontier or satisfied channel bearing may reset the local trend
   baseline without yet declaring the departure durable.
3. `departure.py` settles and classifies observed channel motion.
   `earned_work.py` supplies conservative target-relative evidence for whether
   a pending departure should be promoted, kept pending, regressed, or expired.
4. A regression or anomalous departure invokes `causal.py` to read recorded
   causes. `corrections.py` produces hypotheses; `correction_candidates.py`
   ranks and materializes them; `investigate.py` replay-tests them;
   `progress.py` alone installs one confirmed correction and reverts to the
   selected checkpoint.
5. Pending-departure expiry rolls back without claiming the transition was
   impossible and without creating a nogood.

This third question accounts for most of the recovery machinery: a fork can be
safe enough to commit while its meaning remains unsettled. Search, probe,
pending-motion, and coast budgets ensure that an unresolved world ends with a
named frontier rather than silent churn.

## Actual control flow

1. `pilot.py` snapshots the runtime world and calls `Compass.orient`.
2. Compass reads trace, static catalogs, awaited-action evidence, constraints,
   and knowledge;
   `OrientationResult` permits exactly `Bearing | NeedProbe | Stuck`.
3. `steer.execute` rejects stale bearings, installs their declarative
   prerequisites, and executes exactly one act through `verify.verify_gates`.
   A spin-shaped excursion is returned with its exact execution rather than
   investigated inside the gate.
4. `pilot.py::_resolve_excursion` owns at most one investigation and passes its
   replay to `verify.verify_excursion_replay`, which continues the remaining
   gates after spin.
   `_record_attempt` then applies all observations, including rejected
   attempts, exactly once before any further orientation.
5. An accepted fork is committed and `progress.py` decides retention,
   pending continuation, investigation, or revert. Trend monitoring hands a
   detected channel departure to its terminal `_handle_channel_departure`
   generator without reconstructing the departure receipt.
6. `NeedProbe` is executed only by `skiff.py`; observations or an explicit
   exhaustion mark are applied before orientation runs again.
7. `Stuck` is terminal. No candidate list or route suffix survives an
   observation.

An expectation repair is one checkpoint-local transaction: exact receipt
matching selects the causal source, the source checkpoint is restored on a
disposable state, one corrected local act is executed and verified, and only
its landing may replace the live world. No later action is stored with that
receipt. Successive hazards therefore derive successive requirements and
compose through successive outer iterations, each followed by fresh
orientation. `recovery.py::compose_corrections` owns the bounded transaction;
its module contract defines the permitted recovery boundary.

Passing verification means "eligible to commit and assess", not "durable
progress". Use distinct language for those two decisions.

## Working principles

### Recompute from the current world

Do not store a suffix of actions to execute later. Every iteration rebuilds the
trace and candidate set from the current snapshot, static evidence, accumulated
transition knowledge, active holds, avoid constraints, and world-keyed nogoods.

`avoid=` remains a constraint at every read/action/scan gate; the exact
enforcement contract is in the soundness invariants below. A graph path, trace
alternative, or learned transition is evidence for the next action only.

### Read before probing

Use the evidence ladder in question 1 before returning `NeedProbe`. Static and
projected readers may use controlled forks, but isolated action search belongs
only to `skiff.py`.

Instructions and harness couplings that own cross-scan result channels expose
an `AdvanceProfile` contract. An `AdvanceStep.progress` receipt is
owner-declared evidence that an operation is active when a quantized scalar
cannot change on the next scan; fractional accumulator state remains simulator
execution state, not public PILOT evidence.

`program_step.py` checks one exact producer in an otherwise-unchanged fork,
plus one counterfactual input patch per required input. Observed pipeline motion
makes the reading interrupted even when the producer exposes no external input;
the current transition owner must be observed before option ordering may select
an alternative.

A requirement read while the program is crossing a boundary it owns belongs to
the world after that crossing, not to this one. An input the program is
genuinely stopped at is still required once its own motion finishes. A
mid-crossing reading therefore keeps running with the crossing as its immediate
boundary, so the caller coasts to the landing and the drive loop reads the
settled world again.

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
completeness requirement before it can return permanent `GUARD_DEAD`.

Failure to make progress is not proof that a transition is impossible.
Pending-departure expiry rolls the world back without creating a nogood.

### Bound loops and name failures

Every repeated activity must either consume a finite budget or accumulate
durable knowledge that prevents byte-identical repetition.

- `_PilotState.search_start_scan`, `search_scans`, and
  `remaining_search_scans` own the invocation-relative `max_scans` budget and
  pending-departure lifetimes. `_World.dwell_scans` credits accepted productive
  coast dwell; tentative fork scans remain search work until accepted.
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
consume the first owner's result. The owner's docstring states its contract;
this table only locates the owner.

- User trace route: `pilot.py::_prepare_route`
- Trace-tree traversal and unresolved interior identity:
  `trace.py::TraceNode.iter_nodes` / `is_interior_frontier`
- Writer eligibility and order: `trace.py::_rank_writers`
- Unlocked local trace alternatives: `trace.py::_select_trace_alternative`;
  complete-route ranking is separately owned by `rank_trace_choices`
- Permanent guard rejection: `tide_tables.py::guard_verdict`; trace supplies
  writer fire pins and consumes the complete-domain verdict
- Unsupported construct reporting: `trace.py::UnsupportedConstruct`, rendered
  by `recording.py`
- Instruction-owned channel lookup: `advance.py::AdvanceIndex`
- One exact producer's counterfactual proof: `program_step.py::read_program_step`
- Current-world navigation result and continuation evidence:
  `orientation.py::orient` / `_current_work_evidence`, entered via the
  `compass.py::Compass.orient` facade; orientation groups live-work
  alternatives ahead of fresh alternatives
- Target-relative Bearing objective: `orientation.py::_bearing`
- Cold-start selected-path designation: `bootstrap.py::bootstrap_designations`;
  per-appeared-occurrence source, transaction, and consumer classification
  remains owned by `ScanRungWriteProjection.observe_appeared_handoff`
- Selected steer obligation and window adapter: `effects.py`; `trace.py`
  retains the exact selected path, `options.py` mints one immutable
  `EffectExpectation`, and `ActPolicy` carries it unchanged. Required-shape
  policy stays in `effects.py`; factual `observed_shape` and appeared-write
  classification stay on `ScanRungWriteProjection`.
- One-assertion-scan forensics and bounded closure: `intrascan.py`; its report
  path interprets an already-executed scan, while its production-inert closure
  path may enumerate compatible atomic Boolean alternatives and execute a
  finite number of disposable one-scan forks. The exact execution projection
  remains the semantic oracle. Closure cannot install a correction, mutate
  PILOT state, adopt a world, or retain a navigation future, and production
  Compass/PILOT does not route through it yet. `pilot.py` retains receipt
  creation, deduplication, state mutation, and every subsequent decision.
- Pure intrascan scalar/guard schedule compilation: `intrascan_schedule.py`;
  `requirement_recovery.py` remains the production compatibility facade and
  owns current-world active-requirement admission helpers.
- Shadow-only theory recording: `working_theory.py` owns detached immutable
  claims, versions, attempt/progress receipts, lifecycle facts, and the pure
  reducer. `pilot.py::_transition_once` may return a detached shadow
  observation, but only the live outer loop applies facts to
  `_PilotState.theory_state`. The ledger is knowledge-side and survives world
  restore; disposable repair clones receive its immutable source value and do
  not merge child shadow facts. Shadow theory state cannot change navigation,
  trial acceptance, adoption, progress, rollback, or public events, and
  Compass does not consume it yet.
- Navigation act policy: `orientation.py::_orient_read` materializes one
  `navigation_contracts.ActPolicy`; `steer.execute` applies it
- Exact expectation receipt creation and matching: `requirements.py`;
  `progress.py::_regression_expectation_source` selects one exact causal link,
  restores its source checkpoint, and owns the handoff to local repair
- Option materialization and ranking: `options.py::_build_candidates`;
  `_select_wait` owns wait-source choice
- Static chart-edge admission:
  `constrained_reachability.py::NavigationEvidence.static_edge_admission`
- Local trial gates and accepted execution evidence:
  `verify.py::verify_gates` / `verify_excursion_replay`
- Verification-time excursion orchestration: `pilot.py::_resolve_excursion`;
  verify reports the exact executed attempt, PILOT invokes
  `investigate.py::investigate_excursion` once, and verify judges that replay
- Committed operation context: `pilot.py::_step_context`
- Physical planning versus proof: orientation's
  `TraceReadConstraints.from_context` may propose a coupling driver;
  `verify.py::_gate_dead_end` deliberately omits that model
- Trial-coast avoid observation: `coast.py::CoastSession.seek`
- Target-relative movement: `earned_work.py::EarnedWork.receipt`; verification owns the
  accepted trial's receipt
- Departure observation and classification:
  `departure.py::observe_departure` / pure `classify_departure`; `progress.py`
  alone adopts or discards the returned settled work
- Evidence classification: `outcome.py::assess_outcome`; consumers read the
  returned `TrialAssessment` axes directly
- Transition-knowledge update: `Compass.apply`, invoked by drive-loop
  observation commits and post-commit regression-nogood retention
- Coast-departure channel ownership: `coast.py::coast_departure_tags`
- Post-commit retention, recovery, and correction installation: `progress.py`;
  `_handle_channel_departure` is the terminal event-streaming owner after
  `_monitor_trend` detects a channel departure
- Corrective hypothesis production:
  `corrections.py::derive_correction_hypotheses`
- Corrective hypothesis identity, ranking, composition, executable scoping,
  and self-defeat classification: `correction_candidates.py`
- Corrective incidents, replay, neutralization-versus-masking, and excursion
  diagnosis: `investigation_replay.py`
- Corrective candidate composition and confirmation:
  `investigate.py::_resolve_replay_attempt` / `investigate_deviation`
- Bounded relational counterexample refinement and pinned suppression
  nominations: `refinement.py`
- Bounded corrective composition: `recovery.py::compose_corrections`
- Corrective operation lifetime: the instruction owner, carried through
  `trace.py::TraceAction.operation`; `overlay.py::_set_pilot_rungs` only compiles that
  receipt and preserves an already-active owner by its progress witness
- Temporary-logic execution ownership:
  `overlay.py::_pilot_rung_execution_receipt` over the same
  `_expand_pilot_rules` branches installed by `_set_pilot_rungs`

## World and knowledge

`types.py` and `compass.py` separate state that a revert may undo from
knowledge that must survive:

- `_World`: PLC fork, committed steps and contexts, pilot rungs, trend, and
  dwell accounting.
- `_PilotState` orchestration knowledge: seen keys, checkpoints, pending-departure
  recovery, earned work, correction receipts/revocations, and diagnostic history.
- `CompassKnowledge`: empirical transitions/tombstones, scoped nogoods, probe
  budgets/results, coast receipts, and static-edge evidence overlays.
- `_PilotContext`: static program analysis plus the current persistent
  `Compass` value.

Every production PILOT fork that may execute is created through
`overlay.py::fork_with_pilot_rungs`; public `PLC.fork()` does not implicitly inherit
PILOT holds. `Compass.apply` is the sole knowledge write path; runtime
instruments return `CompassObservation` values and never mutate the compass.
Knowledge scoping (tombstone locality, static-edge overlay narrowness) is
documented on `CompassEntry` and `StaticEdgeObservation`; recovery-floor and
nogood-identity policy on `PendingDeparture` and `world_key.py::_rung_identity`.

Public `PLC.history` is the sole historical query surface and spans the
committed execution lineage. Each fork boundary seals the epoch that actually
executed the inherited scans as an immutable `Epoch`: its inclusive scan
interval, synthesis overlay, clipped scan log, checkpoints, state window, and
firing timelines travel together. `CausalLineage` stores those records behind
the live runner's current epoch; an `EpochQuery` may lazily construct a private
disposable replay runner, but neither that runner nor a copied/frozen `PLC` is
historical identity. Cache residency is only a performance detail: a recorded
state may be returned directly or reconstructed under its owning epoch. Never
reconstruct an inherited scan under the current overlay; that changes writer
and occurrence identity. An expectation receipt binds the source world,
checkpoint owner, act, obligation shape, execution epoch/owner, and exact
producer occurrence. Regression matching fails closed when any of those
identities is missing or ambiguous.

## Soundness and behavior invariants

- Writer availability is only a sort key in `_rank_writers`, never a
  rejection.
- "Still needed" has separate meanings:
  `frontier_pairs` reports unresolved needs in the selected trace tree;
  `_writer_projection` checks a writer under its fire-time overlay;
  `_expr_availability` compares a guard with the live snapshot.
- Avoidance is enforced when choosing a route and before applying an action.
  Condition-like members carry their runtime condition into trial coasts, so
  folding lands on any avoided crossing, and are enforced across every logical
  scan of a pre-commit trial; opaque callables have no readable fold proof and
  cover endpoints, retained real snapshots, and executed kernel scans only.
  `rise()`/`fall()` avoids are rejected because they are transitions rather
  than snapshot states.
- Learned or static route edges are suggestions. A live trial still passes the
  same verification gates.
- A rejection excludes the exact current-world `(primary action, co-actions)`
  artifact that failed; a sibling edge carrying the same primary action under
  different co-actions remains untested. Pair-level consumers see only
  explicit pair rejections and singleton Pulses.
- Attribution, dead-end frontier filtering, and excursion replay consume the
  complete physical `ActPolicy.applied` artifact. Requested `action_pairs`
  remain policy and nogood identity; a non-empty physical artifact is never
  called program-owned without positive causal evidence.
- Recorded occurrence identity belongs to the immutable history epoch that
  executed it. Counterfactual replay may test whether that occurrence remains,
  but may not reinterpret the source scan under a later overlay.

## Navigation

Orchestration:

- `pilot.py` — drive loop, verification-time excursion orchestration, world
  commit, public entry points
- `recording.py` — event/plan rendering; no drive decisions
- `types.py` — cross-module records and protocols
- `__init__.py` — package exports
- `physical.py` — harness install, feedback-tag exclusion
- `multitarget.py` — multi-target incompatibility proof, ordering

Static reading and orientation:

- `trace.py` — backward requirement tree, writer ranking
- `availability.py` — current-state writer availability
- `evidence.py` — pipeline roles, transition-route expansion
- `tide_tables.py` — finite table/calc preimages, guard verdicts
- `pipeline_graph.py` — static transition graphs and path search
- `static_expressions.py` — static-expression helpers
- `compass.py` — navigation facade, durable knowledge
- `orientation.py` — current-world read, result synthesis
- `options.py` — option/wait materialization and ranking
- `constrained_reachability.py` — constrained reachability evidence
- `awaited_actions.py` — program-awaited actions and producer families
- `advance.py` — instruction-owned channels and boundaries
- `program_step.py` — one-producer counterfactual proof
- `bootstrap.py` — conservative cold-start designation and factual projection
  observation adapter
- `effects.py` — act-owned effect obligations, required-shape policy, exact
  execution-window observation, and detached recording snapshots
- `intrascan.py` — report-only exact assertion-scan observation, inert
  failed-effect derivation, and bounded production-inert one-scan closure over
  disposable forks
- `intrascan_schedule.py` — pure scalar schedule compilation and lazy Boolean
  guard-alternative enumeration for one-scan closure
- `requirements.py` — failed-effect explanations, active requirements, exact
  expectation receipts, and strictly decreasing same-scan occurrence-source
  walks
- `requirement_recovery.py` — production compatibility facade for intrascan
  schedules plus current-world active-requirement admission and preservation
- `working_theory.py` — detached shadow theory records and pure lifecycle
  reducer; it stores semantic identities only, never navigation reads, acts,
  checkpoints, worlds, forks, PilotRungs, routes, or callables
- `navigation_contracts.py` — immutable navigation contracts

Execution and observation:

- `steer.py` — execute one act through the trial gates
- `overlay.py` — guarded temporary-logic records, compilation, and PLC forks
- `world_key.py` — stable state, rung, and executable-world identities
- `coast.py` — trigger-observed coasts, exact receipts, departure tags, and delayed effects
- `pulse.py` — edge-aware intervention pulses
- `avoid.py` — avoid and hold-admission checks
- `cyclefold.py` — proven cycle skipping in long waits
- `skiff.py` — isolated probes of unreadable frontiers

Judgment and recovery:

- `verify.py` — trial gates, excursion detection, and replay judgment
- `outcome.py` — evidence classification
- `progress.py` — retention, recovery, corrections, reverts
- `departure.py` — departure observation and classification
- `earned_work.py` — target-relative earned-work marks
- `causal.py` — recorded cause-chain queries
- `investigation_replay.py` — bounded replay evidence, incident construction,
  regression comparison, and excursion diagnosis
- `investigate.py` — corrective candidate composition and confirmation, plus
  compatibility facades for replay imports; no drive-loop ownership
- `correction_candidates.py` — correction identity, ordering, composition,
  executable scoping, and self-defeat checks
- `refinement.py` — bounded relational refinement and pinned suppression evidence
- `recovery.py` — bounded corrective-composition transaction
- `corrections.py` — corrective-hold hypothesis production

Module docstrings define the current local contracts. If a change moves a
decision between modules, update both affected docstrings and this navigation
entry in the same change.

## Vocabulary

Keep the terms that distinguish the model, and define specialized terms in
plain language on first use.

- **pilot** — continuously steers a running program; never executes a stored
  plan.
- **bearing** — the next direction recomputed from the current world.
- **bearing coast** — coast toward the channel value declared by the current
  bearing.
- **compass** — the `Compass` value containing static graph references and
  persistent transition knowledge.
- **learned action** — an action prescribed by persistent transition
  knowledge rather than the current static trace.
- **ladder rung** — a rung in the user's PLC program.
- **pilot rung** — a scoped piece of temporary PILOT steering represented by
  `PilotRung`. The `pilot_rungs` fields and helpers such as
  `_set_pilot_rungs`, `fork_with_pilot_rungs`, and `_rung_identity` refer to
  these objects, not ladder rungs.
- **coast** — hold the required inputs while scans pass.
- **coast trigger** — a named predicate that records why a coast stopped or
  what it observed.
- **pen** — a nonterminal, re-arming `CoastSession` transition recorder. Each
  firing records an exact event but never ends the coast; watch semantic
  transitions, not raw accumulator churn.
- **downstream reach** — the size of an action tag's downstream PDG slice. It
  demotes broad actions in ranking but never rejects them.
- **channel** — the observable state-like boundary of an operation or coast,
  usually `PipelineRoles.channel_tag`. `coast_departure_tags` adds an exact
  stateful, non-earned-work target when no inferred pipeline owns it.
- **world key** — `_pilot_state_key`'s state projection plus the ordered
  identities of the active pilot rungs. It scopes nogoods, probes, coast
  receipts, and cycle detection.
- **nogood** — durable evidence that one exact `act_identity` failed in one
  world key. A failed joint act does not reject its individual members.
- **skiff** — an isolated fork probe for an unreadable frontier. It returns
  observations and commits nothing; the drive loop applies those observations
  and owns the per-world-key probe budget.
- **tide table** — a finite solver for constant-backed transition-availability
  conditions.
- **awaited action** — the unique operator action that program-owned motion is
  currently waiting for.
- **frontier** — unresolved non-steerable requirements in the selected trace
  tree.
- **cone** — a region of tags upstream of a requirement.
- **earned work** — conservative target-relative evidence of completed target work.
- **expectation receipt** — an accepted local act's exact source checkpoint,
  selected obligations, producer/consumer occurrences, and execution identity.
- **checkpoint-local repair** — restore the receipt's exact source on a
  disposable state, apply compatible active requirements, and execute only the
  original local act. A successful landing returns to fresh orientation; no
  action suffix is retained.

Avoid extending the nautical metaphor in technical contracts. Words such as
captain, vessel, reef, shipyard, and waters add a translation step without
naming code abstractions.

## Compact contracts

The following compact views are intentional contracts:

- `corrections.py::_best_forcing_holds` owns pair-shaped forcing holds because
  correction consumers use that exact pair contract.
- `coast.py::CoastReceipt` owns structured stop evidence with stable string
  `stop_reason` values; `cycle_fold_until` returns the compact Boolean terminal
  contract its callers consume.
- `Compass` pair observations and pair nogoods are intentional pair semantics,
  not removable tuple projections.

Other genuine facades include `Compass`'s graphs and action tags, `Pulse`'s
`action` and `applied` views, `_PilotState` setters that preserve `_World`
ownership, and `LearnedBatchRead`'s learned batch evidence. Keep them while
callers need those narrower contracts.

## Testing changes

Run:

```text
make lint
make test-pilot
```

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
It is a fast diagnosis loop, not a replacement for `make test-pilot`.
