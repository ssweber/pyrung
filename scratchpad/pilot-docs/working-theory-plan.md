# WorkingTheory and Temporal Pulse Forensics Plan

This plan supersedes the former init-constant recovery design and its landing
phases. The already-landed evidence machinery remains the starting point; the
remaining recovery-hardening work is folded into the theory lifecycle below.

## Purpose

Simplify PILOT around one persistent unit of intent without turning that
intent into a stored plan:

```text
observe
-> ask Compass for one ordinary Bearing
-> execute and observe it once
-> keep a durable landing and orient again
   or diagnose why a transient pulse was ineffective
-> open, advance, refine, prove, or abandon one WorkingTheory when persistence is needed
-> observe again
```

The difficult new capability is temporal pulse forensics. Given one exact
attempt, determine whether its asserted shape persisted for a later scan, had
to be sustained by an instruction owner, missed a requirement established by
an earlier scan, or reached an exact same-scan consumer while another required
leaf was false. Intrascan evidence explains the next experiment; it does not
prove a composite action before PILOT tries it.

WorkingTheory owns the purpose, rollback root, provisional progress, unresolved
temporal obligations, and exact attempt history. Compass owns one fresh
current-world direction. The intrascan service owns exact pulse-window
diagnosis and bounded retry-shape nomination. `program_step` and instruction
profiles own duration and autonomous continuation. Existing effect, causal,
departure, progress, and investigation modules remain evidence specialists
rather than separate orchestration loops.

Public `PLC.how()` and `OrientationResult = Bearing | NeedProbe | Stuck` remain
unchanged.

## Settled design

### One active theory

There is exactly one active WorkingTheory for a PILOT target. A knowledge-side
ledger retains immutable proved theories, abandoned versions, attempt
tombstones, and successor links. It does not retain several speculative PLC
worlds.

The active theory lives on `_PilotState`, not `_World`, `CompassKnowledge`, or
`_PilotContext`:

- reverting `_World` must not erase it;
- Compass knowledge is reusable across target legs and must not acquire an
  invocation-local checkpoint;
- Bearings and Orientation reads are exact-world values which expire after an
  observation.

### One exact claim

A theory is the claim that one selected producer can make one value and
consumer-relative shape effective at one exact program boundary. It often
opens only after an ordinary trial exposes a temporal requirement which must
survive a detour. Its stable identity is based on:

- source checkpoint owner and source world key;
- target-relative boundary/objective;
- selected producer and consumer;
- required effect shape;
- obligation polarity.

The first action is not part of theory identity. `A`, a prior setup phase
followed by a freshly rediscovered `A`, and a same-scan `A+B` retry may be
different attempts to make the same claim effective. Selecting another
producer or charted edge opens a sibling theory.

### Immutable versions

New exact evidence creates a new version:

```text
V1 = root claim
V2 = root claim + requirement A
V3 = root claim + requirements A + B
```

Trying another Bearing under unchanged evidence creates another attempt, not a
new version. A logical requirement such as `A OR B` remains one requirement;
different satisfying overlays are attempts within the same version.

### No queued work

A WorkingTheory may retain:

- its immutable root source;
- a provisional tip;
- exact requirements and occurrence obligations;
- proved phase receipts;
- attempt identities and finite budgets;
- parent/proved theory identities.

It may not retain:

- a future Bearing or NavigationAct;
- a candidate cursor or route suffix;
- a predicted PLC world;
- an action queue for a release/assert sequence.

Every action is freshly materialized from the exact current root or provisional
tip. Proved phase receipts say what has happened; they do not prescribe the
next action.

### Provisional tips and closure

An open theory has an immutable rollback root and may have a newer provisional
tip. Intermediate accepted movement is not promoted to an ordinary global
checkpoint.

The theory is `PROVED` only when:

1. every root effect obligation is fulfilled;
2. every active requirement is observed at its exact demanding occurrence;
3. `program_step` reaches a stable boundary or reports that the program needs
   input;
4. no departure, investigation, or requirement remains unresolved.

The proved landing is then promoted to an ordinary checkpoint carrying a
`TheoryReceipt` identity. A later regression never mutates the closed theory;
it opens a linked successor with the same root claim and new exact evidence.

### PilotRung lifetime

Closing a theory does not tear down its PilotRungs. The complete `_World`,
including active PilotRungs and ordinary PLC state changed by pulses, presets,
or program execution, is promoted with the landing checkpoint. The theory
receipt retains why each dynamic rung exists. Later lifecycle policy may revoke
or supersede a rung explicitly; there is no blanket theory-close cleanup.

### Nogoods and tombstones

Keep three negative-evidence scopes distinct:

| Evidence | Meaning | Owner |
| --- | --- | --- |
| Theory attempt tombstone | This Bearing did not prove this theory version | Theory ledger |
| Act nogood | This physical act is invalid in this executable world regardless of theory | `CompassKnowledge` |
| Static-edge contradiction | Complete evidence disproved this chart edge in its declared scope | Compass static overlay |

An expectation-relative failure must not create a global act nogood. The same
physical act may remain useful for a sibling producer-to-consumer claim.

Promotion is explicit and conservative. A complete-domain proof relative to a
TheoryClaim or active requirement stays in the theory ledger and constrains
only that `TheoryView`. It may enter `CompassKnowledge` or the static chart
overlay only when its proof is independent of the theory claim, requirements,
and occurrence deadline and establishes the physical act or edge contract for
the declared executable-world scope.

### Budget is not proof

`BUDGET_EXHAUSTED` is the ordinary unresolved intrascan result. It creates no
impossibility proof and no global nogood. `IMPOSSIBLE` is permitted only when a
complete finite domain was searched under complete dependency and execution
semantics. Structural ambiguity or an incomplete domain returns `INCOMPLETE`.

### Temporal deadlines, persistence, and retry

No immediate target motion is not itself a failed act. The first question is
whether the attempted shape remains useful after the assertion scan. If a
level, latch, state, or newly exposed steerable frontier survives for the next
scan, PILOT keeps that landing and performs an ordinary fresh orientation. It
does not revert merely because the final target has not moved yet.

When a pulse is ineffective, its exact evidence classifies each missing
requirement by deadline:

| Deadline class | Meaning | Next experiment |
| --- | --- | --- |
| `BEFORE_ASSERTION` | A prior scan had to arm, clear, select, rearm, or establish the required state before the pulse scan began | Establish that setup as its own phase, keep its proved landing as a provisional tip, then orient freshly |
| `BEFORE_CONSUMER` | The requirement must be true before one exact consumer occurrence later in the assertion scan | Retry the original pulse with one exact missing consumer shape present at the scan boundary |
| `LATER_SCAN` | The attempted shape remains conductive or durably useful for a later scan | Keep the landing and move forward; no intrascan retry |
| `DURATION` | A timer, counter, or program-owned operation requires the level to remain asserted across scans | Hand the duration to its instruction owner and hold/coast; do not flatten it into a composite pulse |
| `UNKNOWN` | The producer, consumer, prior occurrence, projection, or deadline is missing or ambiguous | Fail closed and retain typed unresolved evidence |

The assertion scan is therefore not always the beginning of the causal window.
Forensics may follow exact source identities into an earlier scan which armed
or foreclosed the pulse's effective window. A same-scan co-action cannot repair
a `BEFORE_ASSERTION` deadline which has already passed.

Intrascan derives retry evidence, not a theorem that the retry will work. The
normal executable fork trial remains the oracle. For one observed `AND` guard,
the retry shape contains only its missing steerable leaves. For `OR`, branches
are nominated lazily and tried one at a time. No trace, chart, and overlay
budgets may multiply into speculative candidate execution.

### Recovery lifetime and proof hardening

The final hardening commitments from the old recovery plan become ordinary
WorkingTheory rules rather than a separate recovery phase:

- Track exact one-shot instruction identity and prove a false-scan rearm before
  a later assertion. Release and assertion are distinct proved phases, with a
  fresh PLC read between them.
- Give every occurrence requirement an explicit lifetime:
  `ACTIVE | DISCHARGED | INVALIDATED | AMBIGUOUS`. A delayed requirement stays
  active across provisional tips until its real demanding occurrence observes
  it. It is never discharged merely because the landing value looks right.
- Retain boundary 0, expectation-bearing source checkpoints, and every
  active-corridor boundary referenced by an open theory or retained receipt.
  Pruning is reference-aware, not age-only.
- Key `TheoryAttemptReceipt` by checkpoint/world, exact obligation and writer,
  ordered phases, scope, deadlines, complete correction artifact, and theory
  version. Empirical failures create only theory-local attempt tombstones.
- Represent a complete-domain impossibility separately as `NogoodProof`; it
  must name the finite domain and completeness evidence. Budget exhaustion,
  receipt ambiguity, and incomplete projections cannot create one.
- Fail closed on ambiguous receipt matching, self-masking or self-defeating
  corrections, incompatible requirements, lost checkpoint references, and
  unknown scope. Preserve these as typed unresolved evidence for reporting and
  later refinement.

### Carried-forward exact recovery contracts

The landed recovery evidence keeps these contracts while orchestration moves
under WorkingTheory:

- Bootstrap designations are conservative watch targets, not promises. Only a
  designated effect which actually appears can become a failed-effect fact.
- Failure explanation never silently swaps the selected producer or consumer.
  Another producer is a sibling theory/candidate discovered by fresh Compass
  orientation.
- Compressed and retained timelines are indexes only. Exact owner-bound scan
  projections validate every candidate occurrence and deadline.
- Historical prevention selects the newest exact good-to-bad writer and the
  nearest executable checkpoint before it. History from before `how()` is
  eligible; scan 1 and boundary 0 have no special semantic status beyond their
  actual occurrence and availability.
- `Crossings` and `AdvanceProfile` remain the semantic owners for timer/counter
  inversion. The theory records their derived requirement and exact deadline;
  it does not reimplement instruction algebra.
- Exact within-scan ordinals remove any need for synthetic within-scan
  checkpoints.
- `RetainedReplay`, retained-prefix execution, retained-Bearing composition,
  and current-blocker replay are retired. Historical worlds and futures remain
  evidence only; no old action is replayed as a suffix.

## Ownership after the change

| Owner | Responsibility |
| --- | --- |
| WorkingTheory reducer | Open/advance/refine/prove/abandon; choose root versus tip; promote or restore worlds |
| Theory ledger | Immutable versions, receipts, successors, attempts, and local tombstones |
| Compass | Freshly read one supplied world and return one ordinary next Bearing, `NeedProbe`, or theory-local `Stuck` |
| Intrascan service | Diagnose one exact pulse window, classify temporal deadlines, and nominate one bounded retry shape without executing future candidates |
| `effects.py` | Factual positive/negative occurrence obligations over exact execution windows |
| `requirements.py` | Derive exact logical requirements and occurrence deadlines from failed observations |
| `program_step.py` | Report whether the program will continue autonomously from a provisional tip |
| `progress.py` | Report landing durability; do not own rollback or recovery orchestration |
| `departure.py` | Report exact channel motion and its classification |
| `investigate.py` | Return justified refinements from bounded counterfactual evidence; do not commit/revert/retry the drive |
| `steer.py` / `verify.py` | Execute and judge one world-bound Bearing exactly once; the ordinary trial is the retry oracle and returns a candidate landing fact |

`_transition_once` remains the execution seam. It orients or consumes one
supplied Bearing, executes it, records observations, and returns the judged
candidate landing without owning repetition, rollback, provisional-tip
selection, or global promotion. During migration its current local adoption is
adapted into that result before ownership transfers to the reducer.

## Core records

Place theory records in a dedicated `working_theory.py` module so `types.py`
does not become the new orchestration owner.

```text
TheoryClaim
    source identity
    BearingObjective
    positive/negative EffectObligations
    selected boundary identity

TheoryVersion
    theory_id
    version_id
    requirements
    parent_version

WorkingTheory
    current immutable version
    root checkpoint
    current immutable progress snapshot
    status = OPEN | PROVED | ABANDONED

TheoryProgressSnapshot
    provisional tip
    append-only proved phase receipts
    remaining local budget
    parent progress snapshot

TheoryAttemptReceipt
    theory/version identity
    source or tip world key
    complete act identity
    PilotRung identities
    result disposition

TheoryReceipt
    proved version
    root and promoted landing identities
    exact fulfilled obligations and requirement observations
    retained PilotRung identities

TheoryTombstone
    exact version and attempted artifact set
    termination = STUCK | BUDGET | CONFLICT | PROVED_IMPOSSIBLE

NogoodProof
    exact executable world and claim scope
    named complete finite domain
    completeness evidence
    proved rejected artifacts
```

Closed theory records are immutable. A regression of a proved receipt creates a
successor theory with a parent link.

## Positive and negative occurrence obligations

Extend `EffectObligation` with polarity:

```text
PRODUCE
    the selected producer must occur and satisfy its consumer-relative shape

PREVENT
    the selected harmful producer must not occur in the complete exact window
```

Negative factual dispositions are:

- `PREVENTED`: every owned scan in the window is exactly projected and the
  selected writer did not occur;
- `FIRED`: the writer occurred, retaining its occurrence and enabling reads;
- `UNKNOWN`: projection identity or window completeness is insufficient.

A theory witness is the conjunction of all positive obligations, negative
obligations, and occurrence-scoped requirements. This removes endpoint-based
bootstrap exceptions and makes historical prevention an ordinary successor
theory refinement.

Requirement satisfaction must also be occurrence-scoped. A condition is
satisfied when the exact demanding occurrence observed it; the value may
legitimately change later in the same transaction. Do not exempt an entire
writer because one effect from it survived.

## Intrascan service

Create `intrascan.py` as one semantic owner with replaceable internal search
strategies.

```text
IntrascanQuestion
    exact source checkpoint/world
    TheoryClaim / fixed EffectExpectation
    draft complete action artifact
    active requirements
    fixed PilotRungs and configured inputs
    avoid constraints
    finite search metadata and budget

IntrascanResult
    WITNESS
        complete overlay
        fixed pre-execution EffectExpectation
        detached occurrence report from exact replay

    NEEDS_PHASE
        exact rearm or program-owned boundary receipt

    BUDGET_EXHAUSTED
        attempted identities and unresolved frontier

    INCOMPLETE
        missing domain, ambiguous occurrence, or unsupported construct

    IMPOSSIBLE
        complete-domain proof only
```

The service owns exactly one assertion scan. It cannot commit the live world,
coast, cross a program boundary, or queue another action. A `NEEDS_PHASE`
result returns control to WorkingTheory, which retains only the proved phase
receipt and asks Compass again from the new provisional tip.

### One-scan closure algorithm

1. Restore/fork the exact question source, including hidden memory, forces,
   configured inputs, and PilotRungs.
2. Apply the complete candidate overlay at the scan boundary.
3. Execute exactly the assertion scan and build one owner-bound
   `ScanRungWriteProjection`.
4. Observe every positive and negative obligation.
5. If all obligations and requirements are satisfied, return `WITNESS`.
6. For `OVERWRITTEN` or `DISPLACED`, inspect the harmful write's exact enabling
   reads and complement its conductive guard.
7. For `ABSENT` or `STRANDED`, inspect the selected producer/consumer's exact
   false guard reads.
8. When a false required read was supplied by an earlier same-scan program
   write, follow its exact `ReadOccurrence.source` back to that writer and
   recursively derive the earlier prevention/guard requirement.
9. Compile compatible top-of-scan assignments and PilotRungs. Preserve
   per-alternative deadlines and reject only the exact conflicting composite.
10. Replay the strengthened overlay from the same exact source. Repeat within
    the explicit local budget.
11. Return `BUDGET_EXHAUSTED`, `INCOMPLETE`, or `IMPOSSIBLE` without weakening
    uncertainty into proof.

The executor projection is the semantic oracle. A prove-style compiled kernel
may enumerate or rank finite overlays, but endpoint state is never sufficient
to certify a witness. Every proposed witness must pass the exact ordered
projection check.

Search widening is internal to this service:

1. selected producer guard inputs;
2. active requirement operands;
3. co-actions and upstream definitions;
4. writers which can interfere before the consumer/deadline;
5. a wider finite steerable domain only when bounded and necessary.

This avoids both guard-only incompleteness and unrelated all-input solutions.

## Compass integration

Compass remains read-only with respect to WorkingTheory.

With no active theory:

```text
read current world
-> select one causal candidate
-> ask IntrascanService to close it
-> return one complete Bearing carrying a TheoryClaim
-> PILOT opens the theory before execution
```

If candidate closure returns `BUDGET_EXHAUSTED` or `INCOMPLETE` before a theory
exists, Compass retains only candidate/read-local unresolved evidence and
considers another current-world candidate. There is no theory version to
tombstone until PILOT has opened the selected complete claim.

With an active theory, a detached `TheoryView` travels through
`NavigationConstraints`. Compass uses its claim, requirements, attempts, and
root/tip choice when materializing the next Bearing. Compass never changes the
theory.

An ambient terminal coast with no selected causal claim remains maintenance
and does not open a refinable theory.

`Stuck` from Compass becomes relative to the supplied theory version. The
WorkingTheory owner tombstones that version and restores its root; a fresh
Compass read may select a sibling claim. Public terminal `Stuck` is produced
only when the broader current-world theory frontier is exhausted.

The recently landed `first_edge_allowed` graph seam is used only after this
theory-relative distinction exists. An attempt may exclude an edge as the
first move from one exact source without rejecting that edge later in another
world.

### Chart admission and completion

The chart integration itself was not deleted. Its production admission is
operational but intentionally narrow: `_infer_pipeline_roles_for_context`
returns no roles without an `opaque_loop`, iterates only tags in that loop, and
currently requires request tags. Once a role is admitted, the generic
route/graph engine already handles direct and affine/literal writers and
supplies executable route actions, charted completion waits, runtime edge
evidence, and `program_step` projections. Thus "opaque" describes today's
admission trigger and Trace non-inversion boundary, not a read-only or
diagnostic chart engine.

Do not restore the reverted direct-target exception. Generalize admission as a
separate concern from opaque trace boundaries:

- `pipeline_roles` continues to describe opaque internal pipelines and Trace's
  safe inversion boundary;
- a read-only `chart_roles` catalog is discovered once per drive from every
  prover-classified stepper channel, including direct literal state writers
  and stepper roles with no request-tag bridge;
- `SteppingEvidence.is_stepping(tag)` is the semantic basis for that discovery;
  opacity is not a prerequisite for a chart-based Bearing;
- current target/TheoryClaim relevance and live requirements admit edges from
  the catalog during fresh orientation; opening a theory does not rebuild the
  static catalog;
- every proposed first chart edge is converted into an exact occurrence claim
  and passed through the intrascan service, which composes its full producer
  guard/overlay before Compass may return it as a Bearing;
- structured chart navigation outranks broad raw-trace alternatives for that
  exact claim, while current-world admission can still reject a chart whose
  live producer is unavailable;
- a failed first move creates only a theory/version/source-local
  `first_edge_allowed` exclusion. A persistent requirement contradiction may
  exclude the edge in every position for that constrained read.

This keeps opaque-chart behavior as a compatibility subset while making direct
charts principled rather than target-special.

## WorkingTheory transitions

The reducer consumes typed facts owned by the specialist modules:

```text
ADVANCE
    append an immutable progress snapshot with a provisional tip or phase receipt
    keep the same theory/version
    ask Compass freshly from the tip

REFINE
    create a new immutable version with new exact evidence
    restore the exact root or phase source selected by the deadline
    ask Compass freshly for a replacement Bearing

PROVED
    promote the stable tip and full _World to an ordinary checkpoint
    freeze a TheoryReceipt
    close the active theory

ABANDON
    restore the theory root
    freeze the exact version tombstone
    ask Compass freshly for a sibling claim
```

`program_step` decides whether an accepted provisional tip continues under
program ownership. `KEEP_RUNNING` yields another fresh coast Bearing inside the
same theory. `NEEDS_INPUT` is a stable landing. `INTERRUPTED` feeds exact facts
back to the reducer. `UNCLEAR` remains unresolved evidence.

## Clean migration sequence

Each stage ends with `make lint` and `make test-pilot`. Do not begin the next
ownership transfer while the current stage has unexplained new failures.

### Stage 0 - Restore a known green baseline

- Diagnose and fix the existing `test_pilot_zero_net_deadline_composition`
  failure on `HEAD` before changing orchestration.
- Preserve the passing lower-level deadline-refinement behavior.
- Record the motivating `how(HeelStep == 81)` run from a pristine scan-0 world
  as an external end-to-end acceptance baseline, not as a golden action suffix
  or a source of domain-specific implementation rules.
- Minimize each discovered failure into generic repository fixtures named for
  their mechanics: direct stepper, same-scan overwrite, safety-writer
  prevention, timer preset, one-shot rearm, and consumer deadline. Do not copy
  customer tag names, machine vocabulary, or temporary project paths into
  reusable tests and diagnostics.

### Stage 1 - Extract one-scan forensics without changing decisions

- Add `intrascan.py` with a report-only question/result contract.
- Reuse `observe_execution_window`, `observe_expectation`, and existing
  requirement derivation on one exact assertion scan.
- Add direct tests for overwrite, displacement, absent producer, false
  consumer guard, repeated subroutine calls, and unavailable projections.
- Keep `pilot.py::_derive_attempt_requirements` behavior unchanged through an
  adapter and assert equivalent diagnostic snapshots.

### Stage 2 - Complete transitive same-scan requirement derivation

- Generalize the narrow `_refine_preserved_tag_deadlines` behavior into an
  occurrence-source walk: false read -> exact earlier write -> complemented
  writer guard -> earlier deadline.
- Preserve dynamic call identity, branch path, instruction occurrence, and
  strict ordinal decrease on every recursive hop.
- Normalize compatible Boolean alternatives without losing per-atom deadlines.
- Return `INCOMPLETE` on ambiguity, indirect uncertainty, or a non-decreasing
  cycle.

### Stage 3 - Add bounded overlay closure

- Move scalar/guard schedule compilation behind the intrascan service.
- Compose complete atomic overlays, steady PilotRungs, and conjunctive
  expectations.
- Add exact attempt identities and a bounded closure budget.
- Add `PREVENT` obligations and occurrence-scoped requirement observations.
- Use optional finite kernel enumeration only as a candidate generator; exact
  projection remains the witness.
- Do not route production Compass through the service yet.

### Stage 4 - Introduce WorkingTheory in shadow mode

- Add the theory records, reducer, and ledger to `_PilotState` knowledge.
- Derive shadow theory claims/versions/attempt dispositions from the existing
  loop without controlling world adoption or rollback.
- Assert that no Bearing, CandidateRead, OrientationRead, fork, or route suffix
  enters the ledger.
- Add deterministic reducer tests for every transition and successor link.

### Stage 5 - Let Compass return theory-owned complete Bearings

- Add a detached `TheoryView` to navigation constraints.
- Attach an explicit TheoryClaim to every non-maintenance causal Bearing.
- Route candidate completion through the intrascan service.
- Split theory-local expectation failures from theory-independent Compass act
  nogoods.
- Split read-only `chart_roles` discovery from opaque `pipeline_roles`, then
  split that static discovery from live orientation admission. Discover
  stepper channels once per drive from `SteppingEvidence`, including direct
  state writers and roles without request tags; admit relevant edges per target
  and TheoryClaim without a current-target-only exception or opacity
  requirement.
- Materialize each selected chart edge's complete producer guard through the
  intrascan service before it becomes a Bearing. Preserve structured-chart
  precedence over broad raw Trace within the selected claim.
- Activate first-edge-only route exclusion under exact theory/version scope.
- Retain all-edge rejection only for persistent requirement incompatibility,
  and prove opaque chart behavior remains unchanged as a subset.
- Keep the public orientation union unchanged.

### Stage 6 - Transfer checkpoint-local failed-effect recovery

- Transfer minimum root/provisional-tip ownership now: WorkingTheory chooses
  exact source restoration, accepts or rejects `_transition_once`'s judged
  landing as its provisional tip, and alone promotes a stable landing.
- Replace `_repair_one_active_requirement` and `_nested_guard_act` with theory
  refinement plus Compass/Intrascan closure.
- Make `_source_requirements`, receipt replacement, schedules, and dedupe keys
  theory-aware so separate claims at one checkpoint cannot mix.
- Convert bootstrap and historical program-guard prevention into ordinary
  positive/negative theory obligations.
- Preserve newest exact writer/nearest pre-writer checkpoint selection while
  deleting the standalone single-assignment history-rebase execution path and
  endpoint/bootstrap exceptions once their tests pass through the common path.
- Delete `RetainedReplay`, retained-prefix execution, retained-Bearing
  composition, current-blocker replay, and any remaining historical suffix
  owner after their evidence contracts pass through the common path.

### Stage 7 - Transfer cross-scan phases and autonomous continuation

- Extend the Stage 6 root/tip owner with immutable progress snapshots and
  append-only cross-scan phase receipts.
- Model one-shot rearm and program-owned boundaries as `NEEDS_PHASE`, followed
  by a fresh Compass read from the proved tip.
- Preserve the exact selected instruction's `memory_key("_oneshot")` at its
  source checkpoint. A failed disposable fork restores its armed source and
  does not invent a release. A genuinely spent source must execute one
  guard-false scan, prove that hidden key rearmed, reread the world/projection,
  and only then assert while retaining the other active corrections.
- Count a proved rearm phase as progress even when public tags and the ordinary
  world key do not move. Never flatten release and assertion into one overlay.
- Introduce `ACTIVE | DISCHARGED | INVALIDATED | AMBIGUOUS` as an isolated
  requirement-lifetime policy. Keep a delayed requirement active across a
  locally repaired tip and fresh Compass reads until its exact demanding
  occurrence observes it (or its owning operation/target closes it explicitly).
- Keep `LOCALLY_REPAIRED` distinct from `DISCHARGED`. Local repair proves the
  original whole-shape effect, timely correction, and no correction/avoid
  self-defeat; it does not imply that a delayed consumer has run.
- Replace `_RecoveryContinuation` with theory phase receipts.
- Promote only stable proved landings to global checkpoints.

### Stage 8 - Subsume departure, regression, and investigation orchestration

- Refactor progress/departure/investigation entry points to return typed facts
  or refinements instead of committing, reverting, or recursively retrying.
- Route those facts through the WorkingTheory reducer.
- Replace pending-departure orchestration with an open theory plus provisional
  tip and bounded evidence lifetime.
- Turn a regression of a proved TheoryReceipt into a linked successor theory.
- Delete `_investigate_and_revert` and the remaining special recovery loops only
  after equivalent event and acceptance coverage is green.

### Stage 9 - Harden lifetime, diagnostics, and pruning

- Add explicit PilotRung supersession/revocation receipts without removing
  rungs merely because a theory closed.
- Retain boundary 0, expectation sources, and active-corridor boundaries while
  any active requirement, unresolved incident, open theory, expectation
  receipt, theory receipt, or successor can refer to them. Prune only after the
  complete reference set is empty.
- Match receipt/source identity with epoch, exact occurrence and dynamic
  address, act/consumer, and source world; fail closed on ambiguity.
- Split `TheoryAttemptReceipt` from `NogoodProof`. Include exact
  checkpoint/world, selected obligation/writer, ordered phases, scopes,
  deadlines, corrections, avoid constraints, and theory version in the
  attempt key. Only an identical semantic replay is suppressed.
- Harden masking versus neutralization, correction self-defeat, incompatible
  requirements, scope invalidation/ambiguity, checkpoint loss, budget exits,
  and actionable unresolved reporting.
- Add events for theory open/version/attempt/advance/refine/prove/abandon and
  successor creation, plus exact writer, overwriter, consumer, displaced read,
  requirement, deadline, scope, checkpoint, local-repair, and discharge facts.
- Update `pilot/CLAUDE.md` to describe the one orchestration loop.
- Remove superseded helpers and compatibility adapters.

## Test gates

### Intrascan contracts

- Later writer overwrites the selected value.
- Consumer reads the selected value but another guard input has the wrong
  same-scan source.
- Selected producer is absent because an earlier writer changed its guard.
- Recursive deadline inversion terminates only on strictly earlier ordinals.
- Two- and three-component overlays are required for one witness.
- Conflicting assignments reject only that composite attempt.
- Repeated subroutine calls and branch occurrences retain dynamic identity.
- Incomplete projections and folded windows never prove success or prevention.
- Finite enumeration finds a witness but exact projection is still required.
- Budget exhaustion produces no impossibility proof.
- Direct and opaque chart edges both require exact producer/consumer witnesses;
  a statically plausible endpoint is insufficient.

### Theory lifecycle

- Immutable versions strengthen only on new exact evidence.
- Different Bearings under one version create attempts, not versions.
- Only one theory owns the provisional tip.
- Abandon restores the root and tombstones only the exact version.
- Prove promotes the full `_World`, including PilotRungs.
- A later regression creates a successor rather than mutating a closed theory.
- No stale Bearing or action suffix survives a world observation.
- Theory-relative failure does not create a global Compass nogood.
- Exact one-shot identity survives into phase receipts; a proved false rearm
  scan and fresh reread separate release from assertion.
- A disposable failed fork restores its armed source without an unnecessary
  release; a committed spent source performs the ordered rearm.
- Requirement lifetime covers `ACTIVE`, `DISCHARGED`, `INVALIDATED`, and
  `AMBIGUOUS`; Compass cannot silently weaken, retire, or omit an active one.
- `LOCALLY_REPAIRED` remains distinct from later `DISCHARGED`.
- Attempt identity changes with checkpoint/world, obligation/writer, phase
  schedule, scope, deadline, correction, avoid constraint, or theory version.
- Boundary 0 and every referenced source/corridor checkpoint survive pruning.
- Ambiguous receipts, masking/self-defeat, incompatible requirements, and
  budget exits remain typed unresolved results.
- Only a recorded complete-domain `NogoodProof` creates a proof-level nogood.

### Acceptance

- Existing bootstrap, alarm preset, delayed watchdog, zero-net deadline,
  successive hazard, direct chart, detour, and occurrence-identity fixtures.
- The committed-spent fixture proves the exact one-shot hidden key rearmed on a
  false scan even with zero public motion, rereads, and then asserts while
  retaining the preset correction.
- The disposable failed-fork fixture restores the armed source and performs no
  unnecessary release.
- A delayed consequence leaves the earlier steer locally successful, resolves
  its later cause to the exact earlier receipt, treats the discarded future as
  evidence only, and discharges its requirement at the real later consumer.
- Lifecycle, pruning, ambiguity, masking, self-defeat, incompatible
  composition, and budget-reporting fixtures exercise every hardening state.
- Diagnostic events identify the exact expected writer, overwriter, obliged
  consumer, displaced read, requirement, deadline, scope, causal checkpoint,
  and distinct local-repair/discharge receipts. `SURVIVED` proves the full
  consumer-relative shape.
- Exhaustion never repeats an identical semantic attempt and never promotes an
  empirical failure to a nogood.
- Direct target charts and non-target theory-channel charts are admitted by the
  same rule, compose multi-input guards, preserve chart-over-broad-trace
  precedence, reject a failed wildcard only as the current first move, and
  retain parity with the existing opaque chart fixtures.
- Motivating external acceptance: from a pristine scan-0 PLC,
  `how(HeelStep == 81)` reaches 81 by composing the several top-of-scan
  PRODUCE/PREVENT requirements exposed by the exact execution window,
  preserving the completion invariant, and following the conductive stepper
  route. This names the real case only at the end-to-end boundary.
- Repository acceptance expresses the same mechanics with neutral names and
  generic stepper semantics. It must require one atomic scan-0 overlay with
  multiple independent requirements, reject later same-scan overwriters, and
  continue across timer/program-owned boundaries without any Heel-specific
  tags or machine concepts.
- Tumbler golden comparison at every stage which changes orientation ordering
  or event output.

## Completion criteria

The migration is complete when:

- `_pilot_loop_events` has one theory-centered repetition path;
- Compass is the only producer of the next complete Bearing;
- the intrascan service is the only owner of bounded one-scan closure;
- WorkingTheory is the only owner of refinement, provisional tips, restoration,
  promotion, abandonment, and successor creation;
- progress, departure, investigation, and recovery modules report evidence but
  do not run competing orchestration loops;
- exact positive/negative occurrences and requirement deadlines determine
  acceptance;
- no queued action, retained suffix, or predicted world survives an
  observation;
- `make lint` and `make test-pilot` pass, including Tumbler and the Heel
  end-to-end acceptance, while repository regression fixtures remain generic.
