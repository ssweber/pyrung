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
checkpoint. Verification may instead mint an exact `ScanProgressReceipt` when
the assertion scan itself advanced the selected producer or frontier. If the
retained landing still owns that productive tip, the receipt banks that exact
landing without asking a second trend calculation to prove the same work. The
read is then discarded and the loop starts again from the committed snapshot.

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

`options.py` materializes these readings into exactly one `CandidateRead` per
world, and `orientation.py` applies the explicit current-world precedence to
return one `OrientationResult`: a `Bearing`, one typed research/composition
request, `NeedProbe`, or `Stuck`. Ordinary and WorkingTheory lowering consume
that same read; neither may rebuild it to obtain a more convenient answer. The
readers contribute facts, none chooses an action alone, and every complete read
expires after the next observation.

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
   `regression_requirements.py` matches one causal occurrence to that receipt
   and derives an occurrence-scoped requirement. `progress.py` restores the
   exact checkpoint and retries only the local act. The outer loop then
   performs a fresh current-world read.
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
   When the exact execution proves target, selected-producer, frontier,
   earned-work, or local-conductivity movement, verification records which
   scan was productive and whether the retained landing still owns that tip.
5. A suspicious excursion is the exceptional branch.
   `attempt_verification.py::resolve_excursion` invokes `investigate.py` at
   most once and passes the exact replay to `verify.verify_excursion_replay`,
   which continues the remaining gates.
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

For a pulse, call the source S0, the action/assertion scan S1, and the optional
one-scan look-ahead S2. `PulseHorizon.ASSERTION_SCAN` stops at S1 for temporal
setup and rearm; ordinary pulses may retain S2. A scan-progress receipt names
the exact productive scan separately from the retained landing, so an S1 win
cannot be manufactured by waiting for quiescence and an S2 regression cannot
erase what S1 taught us. Watched-tag settlement remains an explicit coast or
diagnostic/recovery operation where those owners need it; it is not a hidden
`steer(settle=True)` mode and cannot prove scan-level progress.

`overlay.py`, `pulse.py`, and `coast.py` implement the executable intervention
and observation receipts used by this layer. `cyclefold.py` may accelerate a
long coast only by skipping a proven cycle and landing back on a real recorded
scan.

### 3. Did the committed result become durable progress?

`progress.py` owns this question after a trial has passed verification:

1. An exact target, selected-producer, or frontier receipt whose landing owns
   its tip banks the retained fork as the new checkpoint. This is receipt
   consumption, not a second proof from a newly traversed trace tree.
2. A productive assertion scan whose look-ahead regressed remains causal
   evidence, but its landing is not adopted as the new working edge. Local
   conductivity and earned-work receipts likewise do not bypass the ordinary
   global regression policy merely because their narrower owner advanced.
3. Without a receipt-owned landing, an exposed frontier or satisfied channel
   bearing may reset the local trend baseline without declaring the departure
   durable. `departure.py` observes and classifies later channel motion;
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
safe enough to commit before its global meaning is known. A controlling theory
is not an exemption from this policy. Search, probe, pending-motion, and coast
budgets ensure that an unresolved world ends with a named frontier rather than
silent churn.

## Actual control flow

1. `pilot.py` owns the repeated event loop: it snapshots the runtime world and
   calls `Compass.orient`.
2. Compass reads trace, static catalogs, awaited-action evidence, constraints,
   and knowledge. `OrientationResult` permits one executable `Bearing`, one
   typed request (`ComposeCorrection`, conductivity/intrascan research, or
   boundary realization), `NeedProbe`, or `Stuck`.
3. `steer.execute` rejects stale bearings, installs their declarative
   prerequisites, and executes exactly one act through `verify.verify_gates`.
   A spin-shaped excursion is returned with its exact execution rather than
   investigated inside the gate.
4. `attempt_transition.py::transition_once` coordinates exactly one Bearing.
   Receipt-driven refinements live in `attempt_verification.py`; its
   `resolve_excursion` owns at most one investigation and returns the exact
   replay to verification. `attempt_transition.record_attempt` then applies
   all observations, including rejected attempts, exactly once before any
   further orientation.
5. `trial_commit.py::adopt_trial` atomically commits an accepted fork together
   with its `StepContext`, `CommittedAct`, and replay steps. A receipt-owned
   selected-producer or frontier landing becomes the next checkpoint directly;
   otherwise
   `progress.py` decides retention, pending continuation, investigation, or
   revert. Trend monitoring hands a detected channel departure to its terminal
   `_handle_channel_departure` generator without reconstructing the departure
   receipt.
6. `NeedProbe` is executed only by `skiff.py`; observations or an explicit
   exhaustion mark are applied before orientation runs again.
7. `Stuck` is terminal. No candidate list or route suffix survives an
   observation.

An expectation correction is checkpoint-local: exact receipt matching selects
the causal source, the source checkpoint is restored on a disposable state,
one corrected local act is executed and verified, and only its landing may
replace the live world. No later action is stored with that receipt.
`recovery.py::compose_corrections` owns this bounded regression transaction.

Every steer/run/observe cycle freezes one immutable `ExecutionReceipt` before
VERIFY. Its `ExecutionSpan` values point to the exact Epoch-owned kernel scans;
it also records the scan-entry configuration actually applied and the exact
`StopReceipt`. VERIFY, replay, progress, departure, and recording consume this
association rather than reconstructing physical ownership from mutable state.
Timer and counter presets are `ScanEntryConfiguration` values, not PilotRungs.
Each physical Epoch interval has one lineage-issued `EpochRef` which survives
live-tip resealing and fork inheritance; detached theory facts carry that typed
reference rather than Python object-ID owner tokens.

WorkingTheory handles a different problem: an executed scan can reveal the
next condition needed to keep the selected producer/frontier conductive. It
retains only causal facts and lifecycle state: the exact source/provisional
tip, selected claim, unresolved requirements, attempts, exclusions, and typed
`SETUP_FIRST`, `RETRY_TOGETHER`, or `RETRY_THROUGH_DEADLINE` intent. It never
retains a `Bearing`, action suffix, `CandidateRead`, world, checkpoint object,
fork, or branch iterator.

On every iteration Compass performs one fresh read at the theory's current tip
and lowers the typed need through the readers available in that world. One
correction is installed as configuration and yields immediately; Compass then
rereads the changed world before retrying, researching, or composing another
correction. ProgramStep context remains evidence for that reread and is not
automatically folded into the physical retry. The accepted landing becomes the
next tip only through an exact scan-progress receipt; PILOT never proves a
sequence and then replays it.

WorkingTheory retains the complete ordered effect observations from every
relevant attempt. Compass derives a `ConductivityFront` from those immutable
receipts: where the produced value appeared, which exact consumer read it, and
which later write displaced it. Conductivity is therefore a read model, not a
second stored verdict. `intrascan.py` uses the same occurrence order to walk
backward from a failed consumer or displacement. When a backward question
needs execution, `intrascan_counterfactual.py` may inject an analysis-only
value at one exact occurrence boundary on a disposable fork. That patch is a
"what if" instrument and can never be emitted as a production `Bearing`.

A `ConsumerBoundary` names the exact dynamic consumer occurrence where the
transaction's value was observed. The investigation scope retains one plain
`consumer_stop`: the exact accepted boundary where that consumer-bound run
yielded. It can authorize only a fresh reader's reconstruction of that exact
transaction; it cannot broaden a retry with sibling or future actions.
`ProgramTransaction` supplies the corresponding frozen identity for
program-owned motion. It normalizes an outer route, a later direct heading, or
an exact physically observed target write into the same channel/source/target
and effect identity. This correlates an actionless Coast across fresh reads but
does not itself grant execution authority.

Temporal Boolean structure is lazy. All atoms in one `AND` branch are one
logical obligation; that does not mean all resulting correctives are installed
in one physical act. `OR` alternatives are yielded depth-first, one complete
branch at a time, within the current read budget; no power-set or eager branch
product is retained across reads. Direct assignment is allowed only for
authority-approved, adjustable operands that are not already configured.
Program-owned or otherwise non-adjustable requirements stay facts for normal
availability, tide-table, trace, `AdvanceProfile`, and `program_step` readers
to navigate rather than being overwritten.

Do not let generic pre-orientation requirement recovery execute while a typed
temporal request is active, and do not leave its requirement active after exact
proof. Do not add a second projection, composer, progress monitor, or nested
`how(state)`: each would create another controller for the same scan evidence.

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
landed world again.

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

- User trace route: `target_route.py::prepare_target_route`
- Trace-tree traversal and unresolved interior identity:
  `trace_tree.py::TraceNode.iter_nodes` / `is_interior_frontier`
- Writer eligibility and order: `writer_selection.py::_rank_writers`
- Unlocked local trace alternatives: `trace.py::_select_trace_alternative`;
  complete-route ranking is separately owned by `rank_trace_choices`
- Permanent guard rejection: `tide_tables.py::guard_verdict`; trace supplies
  writer fire pins and consumes the complete-domain verdict
- Unsupported construct reporting: `trace_read.py::UnsupportedConstruct`,
  rendered by `recording.py`
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
- Exact assertion-scan observation and bounded diagnostic closure:
  `intrascan.py`. Its projection is the semantic oracle; diagnostic closure
  remains disposable and cannot install logic, mutate PILOT state, adopt a
  world, or retain a navigation future.
- Analysis-only execution at an exact occurrence boundary:
  `core/intrascan_counterfactual.py`. It owns `CounterfactualPatch` execution
  and its application receipt; PILOT may consume the evidence but may never
  promote the patch into temporary production logic.
- Conductivity read model: `conductivity.py`. Compass derives ordered fronts
  and attempt comparisons from WorkingTheory's exact effect receipts; the
  ledger stores the observations, not the derived front.
- Lazy temporal Boolean normalization: `temporal_need.py`; one top-level `AND`
  branch is yielded as one logical requirement set and `OR` alternatives are
  visited depth-first.
- Pure scalar/guard lowering: `intrascan_schedule.py`; it compiles only
  authority-approved current-world assignments. `requirement_evidence.py`
  owns active-requirement admission from exact execution evidence.
- Controlling theory knowledge: `working_theory.py` owns detached immutable
  claims, versions, attempt/progress receipts, temporal intent, consumer stops,
  normalized program-transaction identities, and lifecycle facts;
  `theory_reducer.py` owns lifecycle commands, validation, and the pure
  reducer. The live outer loop alone applies facts to
  `_PilotState.theory_state`; Compass consumes a detached `TheoryView` and
  resolves it through the same current-world `CandidateRead` as ordinary
  orientation. The ledger survives rollback but owns no executable future.
- Navigation act policy: `orientation.py::_orient_read` materializes one
  `navigation_contracts.ActPolicy`; `steer.execute` applies it
- Requirement and expectation receipt contracts plus exact receipt matching:
  `requirements.py`; failed-effect, guard, overwrite, and occurrence-source
  derivation: `requirement_derivation.py`;
  `regression_requirements.py` selects one exact later causal link and adapts it
  into those same requirement contracts; `progress.py` owns checkpoint restore
  and the handoff to local repair
- Candidate-read orchestration and wait-source choice:
  `options.py::_build_candidates` / `_select_wait`; pure action/hold admission:
  `candidate_policy.py`; trace/wait admission, exact operation batching, and
  prerequisite separation: `candidate_admission.py`; static route and chart
  materialization: `route_options.py`; completion and program-evidence wait
  materialization: `wait_options.py`
- Static chart-edge admission:
  `constrained_reachability.py::NavigationEvidence.static_edge_admission`
- Local trial gates and accepted execution evidence:
  `verify.py::verify_gates` / `verify_excursion_replay`
- Exact scan-level progress proof: verification mints
  `types.py::ScanProgressReceipt`; post-commit handling consumes it without
  retraversing the trace to re-prove its selected producer/frontier.
- Verification-time excursion orchestration:
  `attempt_verification.py::resolve_excursion`; verify reports the exact
  executed attempt, the refinement invokes
  `investigate.py::investigate_excursion` once, and verify judges that replay
- One-Bearing execution/verification/recording coordinator:
  `attempt_transition.py::transition_once`
- Committed operation context and atomic World adoption:
  `trial_commit.py::_build_step_context` / `adopt_trial`
- Physical planning versus proof: orientation's
  `TraceReadConstraints.from_context` may propose a coupling driver;
  `verify.py::_gate_dead_end` deliberately omits that model
- Trial-coast avoid observation: `coast.py::CoastSession.seek`
- Target-relative movement: `earned_work.py::EarnedWork.receipt`; verification owns the
  accepted trial's receipt
- Departure observation and classification:
  `departure.py::observe_departure` / pure `classify_departure`; `progress.py`
  decides whether to retain, settle, or discard the result, while
  `departure_state.py` owns pending-state and checkpoint bookkeeping
- Evidence classification: `outcome.py::assess_outcome`; consumers read the
  returned `TrialAssessment` axes directly
- Transition-knowledge update: `Compass.apply`, invoked by drive-loop
  observation commits and post-commit regression-nogood retention
- Coast-departure channel ownership: `coast.py::coast_departure_tags`
- Post-commit retention, recovery decisions, and checkpoint restore:
  `progress.py`; `_handle_channel_departure` is the terminal event-streaming
  owner after `_monitor_trend` detects a channel departure
- Confirmed-correction installation, promotion, contradiction/revocation, and
  symmetric checkpoint overlay rebasing: `correction_lifecycle.py`
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

- `api.py` — public target parsing, `pilot_events` / `pilot_how`, and public
  `Plan` assembly
- `drive_setup.py` — shared static/runtime preparation and target-context
  construction for one drive
- `target_route.py` — target-route selection, public route reporting, and
  linked-feedback obstruction diagnosis
- `pilot.py` — repeated event loop, terminal narration, and post-commit
  monitoring; it does not parse requests, execute an act, verify a trial, or
  adopt a World
- `attempt_transition.py` — execute, verify, record, and optionally adopt one
  Compass Bearing; no repetition or event-stream ownership
- `attempt_verification.py` — receipt-driven excursion replay, target-prefix
  certification, and transient-target promotion
- `trial_commit.py` — atomic accepted-World adoption, `StepContext`,
  `CommittedAct`, and replay-step recording
- `entry_execution.py` — import and route-bind the execution adjacent to Pilot
  invocation
- `recording.py` — event/plan rendering; no drive decisions
- `types.py` — cross-module records and protocols
- `__init__.py` — package exports
- `physical.py` — harness install, feedback-tag exclusion
- `multitarget.py` — multi-target incompatibility proof, ordering

Static reading and orientation:

- `trace.py` — backward-recursion engine for one constrained trace read
- `trace_read.py` — immutable trace requests, route choices, and unsupported
  construct contract
- `trace_tree.py` — trace result records and structural/frontier views
- `trace_routes.py` — complete route enumeration and ranking policy
- `writer_selection.py` — program-writer resolution, classification, and rank
- `program_facts.py` — static reference, edge, resting-value, and transient-rest
  facts used during drive setup
- `availability.py` — current-state writer availability
- `evidence.py` — pipeline roles, transition-route expansion
- `tide_tables.py` — finite table/calc preimages, guard verdicts
- `pipeline_graph.py` — static transition graphs and path search
- `static_expressions.py` — static-expression helpers
- `compass.py` — navigation facade, durable knowledge
- `orientation.py` — current-world read, result synthesis
- `options.py` — candidate-read orchestration and ranking
- `candidate_policy.py` — pure action admission and static hold-conflict proof
- `candidate_admission.py` — trace/wait admission, exact operation batching,
  and durable prerequisite-overlay separation
- `route_options.py` — static route/chart selection and route-owned overlay
  materialization
- `wait_options.py` — instruction-boundary, completion, and program-evidence
  wait prescription materialization
- `candidate_read.py` — immutable completed candidate, wait, prerequisite, and
  route readings consumed by orientation policy
- `theory_orientation.py` — WorkingTheory-specific lowering into one next act
- `constrained_reachability.py` — constrained reachability evidence
- `awaited_actions.py` — program-awaited actions and producer families
- `advance.py` — instruction-owned channels and boundaries
- `program_step.py` — one-producer counterfactual proof
- `bootstrap.py` — conservative cold-start designation and factual projection
  observation adapter
- `effects.py` — act-owned effect obligations, required-shape policy, exact
  execution-window observation, and detached recording snapshots
- `conductivity.py` — Compass-owned read model of immutable occurrence-ordered
  effect history and progress between attempts
- `intrascan.py` — exact assertion-scan observation, backward occurrence
  research, inert failed-effect derivation, and bounded diagnostic one-scan
  closure over disposable forks
- `core/intrascan_counterfactual.py` — analysis-only patches at exact dynamic
  occurrence boundaries; never an executable PILOT correction
- `temporal_need.py` — lazy current-world `AND`/`OR` requirement branches
- `intrascan_schedule.py` — pure authority-aware scalar schedule compilation
- `requirements.py` — inert failed-effect explanations, active-requirement and
  occurrence-source contracts, exact expectation receipts, and receipt matching
- `requirement_derivation.py` — exact guard, advance, overwrite, and strictly
  decreasing same-scan occurrence-source derivation over execution evidence
- `working_theory.py` — controlling detached facts, typed temporal intent,
  consumer stops, and normalized program transactions; it never stores
  navigation reads, acts, checkpoints,
  worlds, forks, PilotRungs, routes, or callables
- `theory_reducer.py` — typed WorkingTheory lifecycle commands, validation,
  and pure state reduction
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
- `progress.py` — retention, departure/recovery policy, investigation, reverts
- `departure_state.py` — pending-departure contracts, exact checkpoint and
  settlement bookkeeping, and pure earned-work assessment
- `correction_lifecycle.py` — confirmed-correction installation, promotion,
  causal revocation, and checkpoint overlay rebasing
- `regression_requirements.py` — exact accepted-expectation matching and
  delayed regression-to-requirement adaptation
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
- **scan progress receipt** — verification's proof that one exact accepted
  scan advanced the selected producer/frontier (or another explicitly named
  local owner), including whether the retained landing still owns that tip.
- **working theory** — rollback-stable causal facts and lifecycle state used to
  ask Compass what the current tip needs next; never a stored executable plan.
- **conductivity front** — Compass's occurrence-ordered view of how far one
  produced value traveled through its exact consumers before displacement or
  scan exit; derived from receipts rather than stored as a conclusion.
- **consumer boundary** — the exact dynamic read occurrence that consumed the
  transaction's produced value.
- **consumer stop** — the exact accepted boundary where one consumer-bound
  transaction yielded; never authority for unrelated future work.
- **program transaction** — a frozen channel/source/target/effect identity used
  to correlate the same program-owned transition across route-wrapped, direct,
  or exact observed-write representations. A fresh Compass read still supplies
  executable authority.
- **intrascan counterfactual patch** — an analysis-only value injection at one
  exact occurrence boundary on a disposable fork; evidence for traceback, not
  valid temporary production logic.
- **temporal need** — an exact intrascan condition to establish or compose with
  the next ordinary transaction. `AND` atoms form one logical obligation, `OR`
  branches are lazy, and corrective installations still yield one at a time.
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
make test-tumbler
```

`make test-pilot` intentionally runs only the bounded core PILOT suite. The
full generated-program suite remains an explicit `make test-tumbler` because a
performance regression there can consume minutes or exhaust memory before an
in-process pytest deadline gets another chance to run.

Before entering a deep Tumbler drive, use `make watch-pilot-burner` or
`make watch-pilot-completed`. These commands stream the same progress prose as
the DAP `how` console from a disposable worker. A separate parent enforces the
total wall budget, maximum silence between structured PILOT events, and
maximum silence between DAP-visible fragments, plus a worker-tree RSS cap that
matches pytest's 4 GB default. On timeout it prints the last DAP-visible
progress, recent event/scan/state receipts, current and peak memory, and the
worker's Python stacks before terminating the worker. Do not replace this with
a deadline checked inside the `pilot_events` loop: that deadline cannot fire
while an expensive operation is withholding the next event. Exit status 2
means a performance budget fired; status 3 means the configured stop-action
tripwire or `--stop-interpretation` receipt appeared.

For a cheap decision-level pass against the generated program, use
`watch_pilot_decisions.py` before a complete Tumbler drive:

```text
uv run python devtools/watch_pilot_decisions.py \
  --target y_BurnerLoop --no-avoid --max-scans 3000 \
  --wall-budget 30 --stall-budget 15 --output-budget 15 \
  --stop-interpretation retry_together
```

`[decision]` shows the current read's candidates, trace, route, holds,
`program_step` result, and frontier. `[interpretation]` names the exact scan,
projection scans, and whether the assertion projection came from the shared
execution cache. `[receipt]` adds writer and operation provenance when a
stop-action tripwire fires. Use `--stop-interpretation setup_first` or
`retry_together` to isolate temporal formation; omit it to watch the journey
continue. This runner is observational: its private hooks must not become a
second projection or navigation path.

For temporal changes, first run the focused timer-preset and scan-progress
tests. They must cover setup-first, same-transaction retry, retry through a
later deadline, edge rearm, consumer-bound stops, actionless
program transactions, lazy `OR`, logical `AND`, configured/program-owned
operands, one `CandidateRead` per world, and both values of
`landing_owns_tip`. Then run `make test-pilot`; only
after that cheap pass should `make test-tumbler` validate the generated Burner
and Completed journeys. Use the watch runners when a fixture stops emitting
useful work so the last exact scan, receipt, and correction are visible.

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
It is a fast decision-parity loop, complementary to the process-isolated
performance watcher and not a replacement for `make test-pilot`.
