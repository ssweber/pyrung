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

**Status:** done

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

**Status:** done

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

**Status:** done

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

**Status:** done

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

**Status:** done

`OrientationWorld` and `CandidateRead` are threaded as a pair and
`OrientationRead` is reconstructed at several result sites.

- Construct one `OrientationRead` immediately after `read_candidates(world)`.
- Pass that exact read to ordinary and WorkingTheory lowerers.
- Each root-route alternative and recursive producer orientation must still
  own a distinct read.
- Do not create an orientation session/controller.

### 8. Carry OrientationWorld farther through candidate orchestration

**Status:** done

The typed candidate-reader boundary immediately unpacks `world` back into
`frame/state/context`, which are then threaded through the private orchestration
phases.

- Prefer `OrientationWorld` in orchestration helpers that genuinely consume the
  trio.
- Unpack only at low-level algorithms that need one member.
- Never retain the world as later-world authority; `state` and `context` remain
  mutable synchronous handles.

### 9. Stop exploding NavigationConstraints into _PilotContext

**Status:** deferred: coherent attempt was too broad

`orientation.orient` copies the fields of `NavigationConstraints` into parallel
`_PilotContext` fields using a field-by-field `hasattr` block. Other paths then
reconstruct partial constraints.

A coherent attempt confirmed the authority reduction was real, but required
simultaneous changes across candidate admission, route and wait readers,
`TraceRead`, theory policy, verification, and structural fixtures. Revisit
after candidate/route/wait admission accepts the existing world/read receipt
end-to-end, or after post-read verification and departure read constraints
from `Bearing.orientation.world`; either seam would let the parallel context
fields be removed incrementally.

- Likely owner: `OrientationWorld.constraints` with a safe default.
- `_PilotContext` should retain stable drive/program facts, not ephemeral
  current-read constraints.
- Migrate structural fixtures incrementally; do not add another wrapper.

### 10. Stop unpacking DriveSetup into a one-caller context factory

**Status:** done

`drive_setup._make_pilot_context` has a broad parameter list and one production
caller. `prepare_target_context` unpacks most of an existing `DriveSetup` only
for the helper to assemble `_PilotContext`.

- Prefer accepting `DriveSetup` plus genuinely target-local values, or inline
  the one-caller helper if that is clearer after inspection.
- Preserve the optional work/Compass overrides and do not turn `DriveSetup`
  into mutable per-target state.

### 11. Pass the accepted trial to departure observation

**Status:** done

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

## Ownership and transition vocabulary

Tasks 7-12 and the deferred buckets already cover the overlapping
`OrientationRead`, `OrientationWorld`, constraints, `DriveSetup`, accepted
trial, replay, phase-projection, `ExecutionReceipt`, and executed-source-key
work. The tasks below record only the remaining seams; they must not recreate
those broader migrations under new names.

### 13. Name physical source identity once

**Status:** ready, narrow behavior-preserving vocabulary

`theory_evidence` and `theory_orientation` independently derive the
requirement-free/physical source key and compare its overlay delta around
`TheoryBoundaryIdentity`.

- Put the exact physical-key and overlay-delta operations beside the existing
  world-key/boundary identity policy, then reuse them at those call sites.
- Preserve separate navigable, proof, historical, and exact-world identities;
  this is not a generic `Owner` protocol.
- Keep drive formation and reducer admission as independent defensive checks.
- Focused tests: a requirement-only change preserves physical identity;
  state, rung, Epoch, or occurrence changes do not; overlay delta remains
  exact.

### 14. Carry the exact TheoryAttemptReceipt through RecordTheoryAttempt

**Status:** ready, behavior-preserving object carry

The execution boundary creates `_TheoryTransitionEvidence`, lifecycle fact
construction copies nearly the same fields into `RecordTheoryAttempt`, and the
reducer reconstructs `TheoryAttemptReceipt` for the ledger.

- Construct the exact `TheoryAttemptReceipt` at the execution/evidence seam and
  let `RecordTheoryAttempt` carry it to reducer validation and storage.
- Keep interpretation/refinement facts distinct and keep reducer validation at
  the mutation boundary.
- Focused tests: idempotent replay, conflicting replay, default observation
  boundary, stale source/version rejection, and stored receipt identity.
- Do not build a generic receipt pipeline or base receipt hierarchy.

### 15. Let controlled setup carry its stored attempt receipt

**Status:** later, depends on Task 14

`_record_controlled_setup_attempt` records an attempt and then copies the same
identity/evidence fields into `_ControlledSetupAttempt` for completion.

- Carry the exact ledger-stored `TheoryAttemptReceipt` plus only genuinely
  setup-local completion values.
- Preserve the distinction between recording and completion admission.
- Do not introduce a setup session/controller or bypass reducer ownership.

### 16. Reuse the existing retention owners

**Status:** ready, narrow behavior-preserving deletion

Recovery manually repeats the policy already owned by
`_retain_active_requirement`, while failed-effect receipts are appended with
the same exact-retention rule at multiple sites.

- Route active-requirement retention through its existing owner.
- Add one narrow `_retain_failed_effect_receipt` only if both current consumers
  can use it without changing ordering or equality semantics.
- Focused tests: duplicate retry, navigation-equal but distinct requirements,
  and equal versus distinct exact failed-effect receipts.
- Do not generalize this into a collection, ledger, or ownership API.

### 17. Characterize temporal-checkpoint admission identity

**Status:** characterize first

Temporal checkpoint admission currently mixes owner identity, object identity,
detached boundary equality, and later `CheckpointRef` resolution across
recovery, evidence, drive, and recording paths.

- Lock the intended admission matrix before selecting one canonical predicate
  or contract.
- Required cases: same owner with a refreshed world, distinct owner with the
  same boundary, same scan with an overlay change, rollback into a new Epoch,
  and ambiguous live resolution.
- Preserve execution-time resolution back to current live requirements; do not
  cache or carry a detached requirement as executable authority.

### 18. Name the occurrence-identity modes

**Status:** later, characterize first

Occurrence comparisons in `theory_evidence`, `requirements`, `conductivity`,
and `theory_orientation` use several deliberate projections but express them
as bespoke tuple/equality code.

- First characterize the modes: historical exact identity, scheduled-retry
  identity, and cross-attempt structural identity.
- Required cases include scan, ordinal, run order, call invocation, Epoch,
  value, and enabled-state changes.
- Name only the proven modes; do not collapse replay selectors into them and do
  not introduce a generic occurrence/owner framework.

### 19. Split theory_orientation by behavior, not authority

**Status:** later, after Tasks 7-9

Once the read/world/constraints boundaries are stable, move cohesive policy
families out of `theory_orientation.py` without changing decision ownership:

- candidate-admission preservation policy, including
  `_act_preserves_requirements`;
- temporal retry, rearm, scheduling, and correction composition;
- intrascan stage/frontier/boundary/traceback behavior;
- the small pending-configuration/overlay reader.

`theory_orientation.py` must remain the precedence and orchestration owner.
Move one behavior family per change with focused parity tests. Do not create a
WorkingTheory manager/facade, a stateful session, or a style-only file split.

## Defensive boundaries to preserve

Some repetition is intentional boundary revalidation, not consolidation debt:

- `assert_temporal_need_current` validates a detached request;
- snapshot resolution materializes current live requirements;
- live composition/setup rechecks the mutable world before execution;
- the reducer independently revalidates source, version, ownership, and Epoch
  at the mutation boundary.

Likewise, Epoch ownership, execution-receipt authority, requirement freshness,
expectation matching, and speculative-world identity are related vocabulary,
not one interchangeable concept. No task above authorizes a generic `Owner`
protocol, a generic receipt pipeline, cached live-requirement resolution, a
central WorkingTheory manager, or line-count/style-only restructuring.

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
