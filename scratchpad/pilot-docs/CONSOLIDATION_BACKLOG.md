# PILOT Consolidation Backlog

This is the stable task ledger for the post-architecture consolidation phase.
It complements `CHARTER.md`; it does not replace the live code, tests, or
`src/pyrung/core/analysis/pilot/CLAUDE.md` as the authority for current
behavior.

The goal is to make PILOT easier to reason about by deleting duplicate
authority and using existing typed values more consistently. A task belongs
here when it can name an exact call chain, owner, deletion payoff, and focused
test surface.

## Ground rules

- Preserve the locked `how()` decision and narration behavior unless a task is
  explicitly marked **characterize first**.
- Prefer an existing receipt, read, world, or constraints value over a new
  wrapper.
- Two consumers, an exactness contract, or a real ownership seam are required
  before extracting a helper.
- Do not introduce a generic reader class, fluent pipeline, session/controller,
  or style-only module split.
- Land one homogeneous seam at a time, with the mandatory Pilot, Tumbler, and
  lint gates before each commit.

## Recently completed boundaries

- `55902c8` — removed the obsolete private recovery transaction.
- `ca8de4e` — corrected candidate/orientation dependency direction.
- `a46bbaf` — added `read_candidates(OrientationWorld) -> CandidateRead` as the
  sole production candidate-reader boundary.

## Ready: behavior-preserving deletion

### 1. Remove the second act authority from attempt recording

**Status:** done

`attempt_transition.record_attempt` receives `objective` and `act` even though
the exact act belongs to the attempt's Bearing. The objective is unused.
`requirement_evidence._retain_expectation_receipt` likewise receives an act
beside the accepted trial and records both as if they were independent.

- Truthful owner: `attempt.trial.attempt.bearing.act`.
- Delete: the unused objective parameter and both redundant act parameters.
- Improve: type `record_attempt` with the existing attempt result instead of
  `Any` where practical.
- Risk: loose test fixtures must model the truthful nested receipt; rejected or
  avoid-gated attempts still have no accepted execution.
- Focused tests: `test_pilot_attempt_transition.py`,
  `test_pilot_expectation_receipt_matching.py`,
  `test_pilot_transient_target_restore.py`.

### 2. Preserve the ordinary Bearing instead of rebuilding it

**Status:** done

`theory_orientation._theory_intrascan_frontier_bearing` obtains an ordinary
Bearing, extracts its candidate reading, and reconstructs nearly the same
Bearing through `orientation_reading._bearing`.

- Use `dataclasses.replace` on the ordinary Bearing.
- Change only the outer target/rationale/investigation selection required by
  the WorkingTheory handoff.
- Preserve the exact `OrientationRead`, prerequisites, entry configurations,
  stop condition, and future receipt fields automatically.
- Focused test: assert identity of the retained `OrientationRead` and equality
  of the execution-bearing fields.

### 3. Give proof-rejection scope one identity owner

**Status:** ready

`attempt_transition.transition_once` and
`theory_orientation._act_preserves_requirements` independently reconstruct the
same requirement-free world key, fallback behavior, `EvidenceScope`, and act
identity.

- Truthful owner: one small proof-scope helper beside world-key/evidence-scope
  identity policy.
- Delete both reconstruction blocks.
- Preserve the deliberate rule that active-requirement changes alone do not
  change proof-rejection scope.
- Focused tests: write/read round trip, changed snapshot release, configured
  and fallback key paths.

### 4. Reuse route command-effectiveness evidence

**Status:** ready

`route_options._compass_route_plan._first_edge_open` constructs an execution
overlay and checks that every edge action/co-action is effective, then calls
`_live_chart_completion_edge`, which reconstructs the overlay and repeats the
same owner-or-snapshot predicate.

- Prefer passing the already-built overlay when the caller has it; otherwise
  use one exact `_edge_commands_effective` predicate shared by both consumers.
- Preserve the distinction between PilotRung ownership and an effective value
  already present in the snapshot.
- Focused tests: rung-owned command, snapshot-owned command, missing co-action,
  and effective commands with true/false producer guards.

## Characterize first: duplicated policy has drifted

These tasks may change decisions. Lock the intended behavior with focused tests
before consolidating the implementation.

### 5. Unify learned-cause admission

**Status:** characterize first

Learned transition admission differs among
`route_options._learned_edge_allowed`,
`constrained_reachability._learned_reachable`, candidate fallback, and final
orientation admission.

Observed mismatches include:

- composite causes are not normalized consistently;
- member blocked/pair nogoods differ between readers;
- a joint overlay can violate an avoid predicate even when neither singleton
  does;
- whole-act Pulse nogoods may be noticed only after path selection.

The likely owner is one pure learned-cause admission function near
`NavigationEvidence`. It must evaluate the complete cause once and preserve
separate wait-edge identity.

Required tests: blocked composite member, collectively avoided overlay,
whole-act nogood with an alternate learned path, and identical wait-nogood
behavior in reachability and candidate reading.

### 6. Apply every awaited-action exclusion before uniqueness

**Status:** characterize first

Ordinary candidate construction judges awaited-action uniqueness before pair
nogoods, while departure admission applies pair nogoods before cardinality.
Two structural actions with one nogood can therefore produce different answers.

- Smallest change: include `key_nogoods` in the existing
  `_awaited_action_bearing` legality callback.
- Do not add another awaited-action reader abstraction.
- Required tests: one legal plus one nogood action, matching departure and
  orientation results, and blocked/avoid/nogood filtering before cardinality.

## Existing-object consolidation

### 7. Make one OrientationRead per completed read

**Status:** later, after the narrow deletions

`OrientationWorld` and `CandidateRead` are threaded as a pair and
`OrientationRead` is reconstructed at several result sites.

- Construct one `OrientationRead` immediately after `read_candidates(world)`.
- Pass that exact read to ordinary and WorkingTheory lowerers.
- Each root-route alternative and recursive producer orientation must still
  own a distinct read.
- Do not create an orientation session/controller.

### 8. Carry OrientationWorld farther through candidate orchestration

**Status:** later

The typed candidate-reader boundary immediately unpacks `world` back into
`frame/state/context`, which are then threaded through the private orchestration
phases.

- Prefer `OrientationWorld` in orchestration helpers that genuinely consume the
  trio.
- Unpack only at low-level algorithms that need one member.
- Never retain the world as later-world authority; `state` and `context` remain
  mutable synchronous handles.

### 9. Stop exploding NavigationConstraints into _PilotContext

**Status:** later, broader migration

`orientation.orient` copies the fields of `NavigationConstraints` into parallel
`_PilotContext` fields using a field-by-field `hasattr` block. Other paths then
reconstruct partial constraints.

- Likely owner: `OrientationWorld.constraints` with a safe default.
- `_PilotContext` should retain stable drive/program facts, not ephemeral
  current-read constraints.
- Migrate structural fixtures incrementally; do not add another wrapper.

### 10. Stop unpacking DriveSetup into a one-caller context factory

**Status:** later

`drive_setup._make_pilot_context` has a broad parameter list and one production
caller. `prepare_target_context` unpacks most of an existing `DriveSetup` only
for the helper to assemble `_PilotContext`.

- Prefer accepting `DriveSetup` plus genuinely target-local values, or inline
  the one-caller helper if that is clearer after inspection.
- Preserve the optional work/Compass overrides and do not turn `DriveSetup`
  into mutable per-target state.

### 11. Pass the accepted trial to departure observation

**Status:** later

`progress.py` unpacks an accepted trial's objective, source snapshot, timeline
occurrence, coast receipt, and execution, then passes those pieces beside the
execution they came from.

- Pass the accepted trial plus only genuinely derived channel/source
  classification.
- Preserve exact occurrence ownership; multiple matching timeline transitions
  must fail closed or have an explicit selection rule.

### 12. Give excursion replay its existing owners

**Status:** later, higher risk

`investigation_replay.investigate_excursion` has a broad signature largely
unpacked from the executed attempt, causal checkpoint, state, and context.

- Shape the boundary around those existing owners.
- Keep the pre-prerequisite source checkpoint distinct from the live effective
  overlay.
- Do not substitute live `state.work` for the checkpoint world.

## Deferred buckets

Reassess these only after the earlier tasks reduce the surrounding call chains:

- Let the existing `StopCondition` own execution stopping instead of threading
  horizon and consumer boundary beside it.
- Project WorkingTheory phase receipts once instead of repeatedly walking the
  ledger for active/superseded/pending overlay and configuration views.
- Use `ExecutionReceipt` as the post-freeze authority for facts still duplicated
  on `_PulseState`.
- Capture the executed source-world key once at the exact post-prerequisite,
  pre-execution boundary instead of reconstructing it during verification.

These are not commitments to new abstractions. Each must still demonstrate
deleted branching, deleted reconstruction, or removal of competing authority
before implementation.
