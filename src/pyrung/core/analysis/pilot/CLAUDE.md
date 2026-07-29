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

Every rung consumes the instruction-owned `AdvanceProfile` contract. An
`AdvanceStep.progress` receipt is owner-declared evidence that an operation is
active when a quantized scalar cannot change on the next scan; fractional
accumulator state remains simulator execution state, not public PILOT
evidence.

1. `trace.py` follows writers, guards, copies, calculations, and
   instruction-owned cross-scan state through `advance.py`.
2. `availability.py`, `evidence.py`, `tide_tables.py`, and `currents.py` extend
   that read with current-state guards, pipeline structure, finite
   constant-backed tables, and program-awaited actions.
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
  `navigation.ActPolicy`; `steer.execute` applies it
- Option materialization and ranking: `options.py::_build_candidates`;
  `_select_wait` owns wait-source choice
- Static chart-edge admission:
  `navigation_evidence.py::NavigationEvidence.static_edge_admission`
- Local trial gates and accepted execution evidence: `verify.py::verify_gates`
- Committed operation context: `pilot.py::_step_context`
- Physical planning versus proof: orientation's
  `TraceReadConstraints.from_context` may propose a coupling driver;
  `verify.py::_gate_dead_end` deliberately omits that model
- Trial-coast avoid observation: `coast.py::CoastSession.seek`
- Target-relative movement: `gauge.py::Gauge.receipt`; verification owns the
  accepted trial's receipt
- Departure observation and classification: `detour.py::classify_departure`
- Evidence classification: `outcome.py::assess_outcome`;
  `classify_outcome` stays as the small ergonomic compatibility projection
- Transition-knowledge update: `Compass.apply`, invoked by the drive loop
- Coast-departure channel ownership: `_ops.py::coast_departure_tags`
- Post-commit retention, recovery, and correction installation: `progress.py`;
  `_handle_channel_departure` is the terminal event-streaming owner after
  `_monitor_trend` detects a channel departure
- Corrective hypothesis derivation: `corrections.py`
- Corrective hypothesis replay, neutralization-versus-masking, and
  confirmation: `investigate.py::build_replay_fn` and
  `_resolve_replay_attempt`
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

- `_World`: PLC fork, committed steps and contexts, active rungs, trend, and
  dwell accounting.
- `_PilotState` orchestration knowledge: seen keys, checkpoints, pending-departure
  recovery, gauge, correction receipts/revocations, and diagnostic history.
- `CompassKnowledge`: empirical transitions/tombstones, scoped nogoods, probe
  budgets/results, coast receipts, and static-edge evidence overlays.
- `_PilotContext`: static program analysis plus the current persistent
  `Compass` value.

Every production PILOT fork that may execute is created through
`_ops.py::fork_with_rungs`; public `PLC.fork()` does not implicitly inherit
PILOT holds. `Compass.apply` is the sole knowledge write path; runtime
instruments return `CompassObservation` values and never mutate the compass.
Knowledge scoping (tombstone locality, static-edge overlay narrowness) is
documented on `CompassEntry` and `StaticEdgeObservation`; recovery-floor and
nogood-identity policy on `PendingDeparture` and `_ops.py::_rung_identity`.

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

- `trace.py` — backward requirement tree, traversal, route enumeration,
  steerability, writer ranking, and alternative selection.
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
- `options.py` — phased private current-world readings, wait-source selection,
  and final option materialization and ranking.
- `navigation_evidence.py` — narrow constrained reachability evidence shared
  with verification and recovery; never returns an action.
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
  `_settle_cone` stays as the thin execution adapter around
  `CoastSession.settle`.
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
  recovery, the terminal channel-departure handler, correction installation,
  and reverts.
- `detour.py` — immutable channel-departure observation and typed
  classification for progress handling.
- `gauge.py` — conservative target-relative earned-work marks and reset
  boundaries.
- `causal.py` — recorded cause-chain queries and empirical program-write
  evidence.
- `investigate.py` — incident construction, hypothesis ranking, typed replay
  resolution, and confirmation.
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
- residual replay measurement: `devtools/profile_pilot_replay.py`; its scalar
  partitions are observational only — it must not retain a second per-scan
  log or enlarge `CoastReceipt`.
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
