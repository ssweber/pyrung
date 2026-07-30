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

`avoid=` remains a constraint at every read/action/scan gate; the exact
enforcement contract is in the soundness invariants below. A graph path, trace
alternative, or learned transition is evidence for the next action only.

### Read before probing

Escalate according to what remains unreadable:

Instructions and harness couplings that own cross-scan result channels expose
an `AdvanceProfile` contract. An `AdvanceStep.progress` receipt is
owner-declared evidence that an operation is active when a quantized scalar
cannot change on the next scan; fractional accumulator state remains simulator
execution state, not public PILOT evidence.

1. `trace.py` follows writers, guards, copies, calculations, and
   instruction-owned cross-scan state through `advance.py`.
2. `availability.py`, `evidence.py`, `tide_tables.py`, and
   `awaited_actions.py` extend that read with current-state guards, pipeline
   structure, finite constant-backed tables, and program-awaited actions.
3. `program_step.py` checks one exact producer in an otherwise-unchanged fork,
   plus one counterfactual input patch per required input, and reports keep
   running, needs input, interrupted pipeline motion, or unclear. It never
   chooses an action. Observed pipeline motion makes the reading interrupted
   even when the producer exposes no external input; the current transition
   owner must be observed before option ordering may select an alternative.
   A requirement read while the program is crossing a boundary it owns belongs
   to the world after that crossing, not to this one — an input the program is
   genuinely stopped at is still required once its own motion finishes. Such a
   reading keeps running with the crossing itself as the immediate boundary,
   so the caller coasts to its landing and the drive loop reads the settled
   world again.
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
- Current-world navigation result: `orientation.py::orient`, entered via the
  `compass.py::Compass.orient` facade
- Current-world continuation evidence: `options.py::_current_work_evidence`;
  orientation groups live-work alternatives ahead of fresh alternatives
- Target-relative Bearing objective: `orientation.py::_bearing`
- Navigation act policy: `orientation.py::_orient_read` materializes one
  `navigation_contracts.ActPolicy`; `steer.execute` applies it
- Option materialization and ranking: `options.py::_build_candidates`;
  `_select_wait` owns wait-source choice
- Static chart-edge admission:
  `constrained_reachability.py::NavigationEvidence.static_edge_admission`
- Local trial gates and accepted execution evidence: `verify.py::verify_gates`
- Committed operation context: `pilot.py::_step_context`
- Physical planning versus proof: orientation's
  `TraceReadConstraints.from_context` may propose a coupling driver;
  `verify.py::_gate_dead_end` deliberately omits that model
- Trial-coast avoid observation: `coast.py::CoastSession.seek`
- Target-relative movement: `earned_work.py::EarnedWork.receipt`; verification owns the
  accepted trial's receipt
- Departure observation and classification: `departure.py::classify_departure`
- Evidence classification: `outcome.py::assess_outcome`; consumers read the
  returned `TrialAssessment` axes directly
- Transition-knowledge update: `Compass.apply`, invoked by the drive loop
- Coast-departure channel ownership: `coast.py::coast_departure_tags`
- Post-commit retention, recovery, and correction installation: `progress.py`;
  `_handle_channel_departure` is the terminal event-streaming owner after
  `_monitor_trend` detects a channel departure
- Corrective hypothesis derivation: `corrections.py`
- Corrective hypothesis replay, neutralization-versus-masking, and
  confirmation: `investigate.py::build_replay_fn` and
  `_resolve_replay_attempt`
- Corrective operation lifetime: the instruction owner, carried through
  `trace.py::TraceAction.operation`; `overlay.py::_set_rungs` only compiles that
  receipt and preserves an already-active owner by its progress witness
- Temporary-logic execution ownership: `overlay.py::_rung_execution_receipt` over
  the same `_expand_pilot_rules` branches installed by `_set_rungs`

## Actual control flow

1. `pilot.py` snapshots the runtime world and calls `Compass.orient`.
2. Compass reads trace, static catalogs, awaited-action evidence, constraints,
   and knowledge;
   `OrientationResult` permits exactly `Bearing | NeedProbe | Stuck`.
3. `steer.execute` rejects stale bearings, installs their declarative
   prerequisites, and executes exactly one act through `verify.verify_gates`.
4. `pilot.py::_record_attempt` applies all observations, including rejected
   attempts, before any further orientation.
5. An accepted fork is committed and `progress.py` decides retention,
   pending continuation, investigation, or revert. Trend monitoring hands a
   detected channel departure to its terminal `_handle_channel_departure`
   generator without reconstructing the departure receipt.
6. `NeedProbe` is executed only by `skiff.py`; observations or an explicit
   exhaustion mark are applied before orientation runs again.
7. `Stuck` is terminal. No candidate list or route suffix survives an
   observation.

Passing verification means "eligible to commit and assess", not "durable
progress". Use distinct language for those two decisions.

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
`overlay.py::fork_with_rungs`; public `PLC.fork()` does not implicitly inherit
PILOT holds. `Compass.apply` is the sole knowledge write path; runtime
instruments return `CompassObservation` values and never mutate the compass.
Knowledge scoping (tombstone locality, static-edge overlay narrowness) is
documented on `CompassEntry` and `StaticEdgeObservation`; recovery-floor and
nogood-identity policy on `PendingDeparture` and `world_key.py::_rung_identity`.

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

## Navigation

Orchestration:

- `pilot.py` — drive loop, world commit, public entry points
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

- `verify.py` — trial gates
- `outcome.py` — evidence classification
- `progress.py` — retention, recovery, corrections, reverts
- `departure.py` — departure observation and classification
- `earned_work.py` — target-relative earned-work marks
- `causal.py` — recorded cause-chain queries
- `investigate.py` — hypothesis replay and confirmation
- `corrections.py` — corrective-hold hypothesis derivation

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
- **ladder rung** — a rung in the user's PLC program.
- **pilot rung** — a scoped piece of temporary PILOT steering represented by
  `PilotRung`. The `pilot_rungs` fields and helpers such as `_set_rungs`,
  `fork_with_rungs`, and `_rung_identity` refer to these objects, not ladder
  rungs.
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

Avoid extending the nautical metaphor in technical contracts. Words such as
captain, vessel, reef, shipyard, and waters add a translation step without
naming code abstractions.

## Compatibility boundaries

The following projections are deliberate compatibility seams. Keep their owner
and removal condition visible so an internal cleanup does not silently change
an event stream, replay identity, or lower-level API.

- `corrections.py::_best_forcing_holds` owns the pair-shaped forcing-hold
  projection. Remove it only after every correction consumer accepts
  `TraceAction` or another exact operation receipt.
- `recording.py::_build_plan_journal` infers accelerator edits from the scan log
  for ordinary runner folds whose receipts contain no exact edits. Remove that
  fallback when ordinary-fold receipts carry authoritative edits.
- `trace.py` retains the affine walker fallback when a registered reverse
  declines. Remove it only when registered reverse rules cover those affine
  writers.
- `navigation_contracts.py::ActSource` keeps serialized values `influence` and
  `learned`; recording keeps the `influence_prescribed` payload, and VERIFY
  keeps the public `influence-override-cycle` gate event. Version and migrate
  consumers before changing any of them.
- `recording.py` owns the `zoom`, `zoom_accepted`, and `zoom_rejected` events and
  their `zoom_*` payload keys. The public stall spelling is lowercase
  `zoom-stall`; uppercase spellings are internal gate labels, not public
  vocabulary. Change these only through an event schema migration.
- `recording.py` and `progress.py` serialize `rungs` and `revoked_rungs`.
  Renaming those payload keys requires an event schema migration.
- `recording.py` serializes internal downstream reach as `wake` and
  `wake_cap`. Renaming those payload keys requires an event schema migration.
- `coast.py::CoastReceipt` owns structured stop evidence with stable string
  `stop_reason` values; `cycle_fold_until` retains its Boolean return API.
  PILOT filters lower-runner `real_scans` and `folds` cycle-fold details.
  Remove that filter only after the lower runner normalizes those fields;
  migrate the other APIs explicitly.
- `world_key.py::_semantic_key` preserves the old
  `pyrung.core.analysis.pilot._ops` module token for `OperationReceipt`.
  Remove it only as an explicit world-key identity version.
- `Compass` pair observations and pair nogoods are intentional pair semantics,
  not tuple compatibility wrappers.

Some compact views are genuine facades rather than removal candidates:
`Compass` exposes graphs and action tags, `Pulse` exposes `action` and
`applied`, `_PilotState` setters preserve `_World` ownership,
and `LearnedBatchRead` presents learned batch evidence. Keep them while callers
need those narrower contracts.

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
