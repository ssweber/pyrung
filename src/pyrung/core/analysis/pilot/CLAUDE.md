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

The exception is the user's trace-route lock. `_prepare_route` chooses it once
before the loop because `via=` and `avoid=` express user intent. A graph path or
learned transition is evidence for the next action only.

### Read before probing

Escalate according to what remains unreadable:

1. `trace.py` follows writers, guards, copies, calculations, and
   instruction-owned cross-scan state through `advance.py`.
2. `availability.py`, `evidence.py`, `tide_tables.py`, and `currents.py` extend
   that read with current-state guards, pipeline structure, finite
   constant-backed tables, and program-awaited actions.
3. An `AdvanceProfile` states one next operation: conditions to hold or pulse,
   and the observable boundary at which PILOT must read the world again.
4. `program_step.py` checks one exact producer in an unchanged fork and reports
   keep running, needs input, or unclear. It does not choose an action.
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
Provisional expiry rolls the world back without creating a nogood.

### Bound loops and name failures

Every repeated activity must either consume a finite budget or accumulate
durable knowledge that prevents byte-identical repetition.

- `max_scans` counts committed search work; accepted coast dwell is credited
  separately.
- Skiff retries use a per-world-key budget and continue only when
  `Compass.apply` reports new knowledge.
- Provisional program motion has a finite scan budget and a saved rollback
  boundary.
- Revert cycles outside provisional motion currently rely on accumulating
  nogoods or installed corrections rather than a separate counter.

A terminal result must name the outstanding frontier when one can be read.
Keep `reason`, `skiff_decline`, `avoid_names`, `lever_notes`, journey, and hold
receipts attached to the result path that discovered them.

### Give each decision one owner

Do not reproduce a decision in a second module for convenience. Shared callers
should consume the first owner's result.

- User trace route: `pilot.py::_prepare_route`
- Writer eligibility and order: `trace.py::_rank_writers`
- Instruction-owned channel lookup: `advance.py::AdvanceIndex`
- One exact producer's unchanged-world proof: `program_step.py::read_program_step`
- Current-world navigation result: `orientation.py::orient`, entered via the
  `compass.py::Compass.orient` facade
- Option materialization and ranking evidence: `options.py::_build_candidates`
- Local trial gates: `verify.py::verify_gates`
- Evidence classification: `outcome.py::assess_outcome`
- Transition-knowledge update: `Compass.apply`, invoked by the drive loop
- Post-commit retention, recovery, and correction installation: `progress.py`
- Corrective hypothesis derivation: `corrections.py`
- Corrective hypothesis replay and confirmation: `investigate.py`

## Actual control flow

1. `pilot.py` snapshots the runtime world and calls `Compass.orient`.
2. Compass reads trace, catalog, currents, constraints, and knowledge, then
   returns exactly one `Bearing`, `NeedProbe`, or `Stuck`.
3. `steer.execute` rejects stale bearings, installs their declarative
   prerequisites, and executes exactly one act through `verify.verify_gates`.
4. `pilot.py::_record_attempt` applies all observations, including rejected
   attempts, before any further orientation.
5. An accepted fork is committed and `progress.py` decides retention,
   provisional continuation, investigation, or revert.
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
- `_PilotState` orchestration knowledge: seen keys, checkpoints, provisional
  recovery, gauge, and diagnostic history.
- `CompassKnowledge`: empirical transitions/tombstones, scoped nogoods, probe
  budgets/declines, coast receipts, and static-edge evidence overlays.
- `_PilotContext`: static program analysis plus the current persistent
  `Compass` value.

`Compass.apply` returns a new compass and a `changed` flag. When no entry
changes, it returns the same object. Runtime instruments return
`CompassObservation` values and do not mutate the compass themselves.

## Soundness and behavior invariants

- Writers that can produce the requested value remain eligible.
  Availability and wake/clobber heuristics order; they do not reject.
- "Still needed" has separate meanings:
  `frontier_pairs` reports unresolved needs in the selected trace tree;
  `_writer_projection` checks a writer under its fire-time overlay;
  `_expr_availability` compares a guard with the live snapshot.
- Avoidance is enforced when choosing a route, before applying an action, and
  across every intermediate scan of a trial.
- Learned or static route edges are suggestions. A live trial still passes the
  same verification gates.
- A program-written tag may be removed from the steerable set by recorded
  evidence. Empirical evidence never creates a new lever.
- A correction is installed only in the exact guarded form that survived
  replay, and only one competing explanation is installed for an incident.
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
  reports the immediate boundary or unmet input.
- `navigation.py` — immutable evidence, act, result, target, constraint, and
  world-view contracts.

### Execution and observation

- `steer.py` — forked action/coast execution and invocation of trial gates.
- `_ops.py` — shared PLC operations, world keys, holds, pulses, coast adapters,
  and action-admission checks.
- `coast.py` — bump-driven coasts with exact-scan receipts.
- `cyclefold.py` — proven active-cycle skipping during long waits.
- `skiff.py` — finite isolated probes of unreadable frontiers.

### Judgment and recovery

- `verify.py` — avoid, target, spin, cycle, dead-end, and outcome gates.
- `outcome.py` — agency, bearing, progress, and frontier evidence.
- `progress.py` — checkpoints, provisional motion, regression recovery,
  correction installation, and reverts.
- `detour.py` — channel-departure classification for progress handling.
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
