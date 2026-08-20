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

**Status:** done

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

**Status:** done

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

Implemented as one requirement-free world-key projection plus one pure
same-boundary overlay-delta operation. Drive formation and reducer admission
still invoke the boundary check independently.

### 14. Carry the exact TheoryAttemptReceipt through RecordTheoryAttempt

**Status:** done

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

Implemented as a receipt-only lifecycle fact. The exact frozen receipt now
normalizes its default observation boundary when constructed, crosses the
reducer boundary unchanged, and is stored by object identity after the reducer
independently revalidates version, source, execution, and investigation
ownership. Interpretation and refinement remain separate lifecycle facts.

### 15. Let controlled setup carry its stored attempt receipt

**Status:** done

`_record_controlled_setup_attempt` records an attempt and then copies the same
identity/evidence fields into `_ControlledSetupAttempt` for completion.

- Carry the exact ledger-stored `TheoryAttemptReceipt` plus only genuinely
  setup-local completion values.
- Preserve the distinction between recording and completion admission.
- Do not introduce a setup session/controller or bypass reducer ownership.

Implemented as a narrow carrier cleanup after Task 14. Controlled setup now
fetches and carries the exact receipt stored by the reducer; completion reads
its attempt identity, action pairs, configurations, consumer boundary, and
execution source from that receipt. Only the temporal request and genuinely
setup-local completion observations remain beside it. Recording still precedes
admission, and the reducer still independently validates both the attempt and
every later completion fact.

### 16. Reuse the existing retention owners

**Status:** done

Recovery manually repeats the policy already owned by
`_retain_active_requirement`, while failed-effect receipts are appended with
the same exact-retention rule at multiple sites.

- Route active-requirement retention through its existing owner.
- Add one narrow `_retain_failed_effect_receipt` only if both current consumers
  can use it without changing ordering or equality semantics.
- Focused tests: duplicate retry, navigation-equal but distinct requirements,
  and equal versus distinct exact failed-effect receipts.
- Do not generalize this into a collection, ledger, or ownership API.

Recovery now admits active requirements through `_retain_active_requirement`.
Both failed-effect consumers share `_retain_failed_effect_receipt`, retaining
the first exact identity in observation order while preserving distinct exact
receipts. Recording still precedes retention in both recovery paths.

### 17. Characterize temporal-checkpoint admission identity

**Status:** characterized — preserve the path-specific rules

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

The characterized matrix is intentionally not one equivalence relation:

| Path | Admission / resolution identity | Characterized consequence |
| --- | --- | --- |
| Recovery | `_CheckpointOwner` object identity | A refreshed World for one owner is not appended; a distinct owner at the same executed boundary is retained. |
| Evidence | `_CausalCheckpoint` object identity | A distinct checkpoint object is retained even when its owner or detached boundary is already present. |
| Drive and recording | exact `TheoryBoundaryIdentity` equality | Overlay changes and new Epochs are distinct; distinct checkpoint owners collapse when the executed boundary has the same Epoch owner. |
| Executable source resolution | `CheckpointRef` projection, then exact boundary; last retained match wins | A refreshed checkpoint replaces the older value for one reference. Distinct references can remain candidates for the same executed boundary. |
| Executable requirement resolution | unique active semantic-identity match | The detached snapshot never becomes executable authority; zero matches are historical, one returns the current live object, and multiple matches fail closed as ambiguous. |

The main drift is between provenance-local admission rules, not boundary
comparison itself: owner-based recovery can suppress an overlay/Epoch-refreshed
World, while object-based evidence can retain redundant views that boundary-
based recording would suppress. The resolver makes same-reference refreshes
deterministic, and exact boundary matching prevents a rollback re-execution
from impersonating its earlier Epoch.

Do not consolidate these rules yet. First decide whether recovery is allowed to
reuse a `_CheckpointOwner` across a changed executable boundary and whether
evidence intentionally preserves multiple objects for one owner. Until those
ownership lifetimes are explicit, a canonical admission predicate would hide a
policy choice. Keep live requirement resolution at execution time and retain
its ambiguity rejection independently of checkpoint admission.

### 18. Name the occurrence-identity modes

**Status:** characterized — preserve the mode boundaries

Occurrence comparisons in `theory_evidence`, `requirements`, `conductivity`,
and `theory_orientation` use several deliberate projections but express them
as bespoke tuple/equality code.

- First characterize the modes: historical exact identity, scheduled-retry
  identity, and cross-attempt structural identity.
- Required cases include scan, ordinal, run order, call invocation, Epoch,
  value, and enabled-state changes.
- Name only the proven modes; do not collapse replay selectors into them and do
  not introduce a generic occurrence/owner framework.

The current projections have this field matrix. "Include" means a change to
that field changes identity; "ignore" means it deliberately does not.

| Mode / use | Scan | Ordinal | Run order | Call invocation | Value | Enabled | Epoch / checkpoint owner |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Historical theory occurrence | include | include | include | include | include | include | owned by the enclosing requirement/receipt, not the occurrence projection |
| Scheduled-retry occurrence | ignore | include | include | include | ignore | ignore | ignore |
| Cross-attempt stopping writer | ignore | ignore | ignore | ignore | ignore | ignore | ignore |
| Cross-attempt produced front | ignore | ignore | ignore | ignore | include final produced value | ignore | ignore |
| Cross-attempt stopping read / requirement bridge | ignore | ignore | ignore | include | ignore | ignore | ignore |

Historical requirement identity separately includes both `EpochRef` and
`CheckpointRef`, while navigation identity deliberately drops them. Thus a
retry can be the same schedule without impersonating the earlier historical
receipt. The cross-attempt rows are a policy family, not one projection: stop
comparison asks whether the same structural writer stopped progress, front
comparison additionally asks whether the same value entered the flow, and
requirement-drift correlation retains dynamic call invocation to select the
same stopping read.

Replay `EffectOccurrenceSelector` identity must remain separate. It ignores
absolute scan, ordinal, run order, value, enabled state, and owners like some
cross-attempt projections, but additionally identifies a relocatable static
branch/instruction path and access index while retaining call invocation. It
answers where to replay an access, not whether two historical or cross-attempt
observations are the same.

No behavioral contradiction was found. The only vocabulary drift is the broad
phrase "structural identity": it currently covers three intentionally
different conductivity projections. A later naming-only cleanup is justified
only where bespoke code exactly duplicates one proven row (notably the
scheduled occurrence triple in `theory_orientation` and the historical tuple
decoded by conductivity). Do not introduce one generic occurrence identity or
merge the three conductivity projections.

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
