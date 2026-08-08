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

### Provisional tips and proof

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

Intrascan derives retry evidence, not a theorem that the retry will work. A
narrowed retry is executed once on an exact fork and that same execution passes
through normal verification; there is no proof-then-replay cycle. For one
observed `AND` guard, the retry shape contains only its missing steerable
leaves. For `OR`, branches are nominated lazily and tried one at a time. No
trace, chart, and retry budgets may multiply into speculative candidate
execution.

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
| Intrascan service | Diagnose one exact pulse window, classify temporal deadlines, execute at most one narrowed same-scan candidate per local alternative, and return the already-executed landing without adopting it |
| `effects.py` | Factual positive/negative occurrence obligations over exact execution windows |
| `requirements.py` | Derive exact logical requirements and occurrence deadlines from failed observations |
| `program_step.py` | Report whether the program will continue autonomously from a provisional tip |
| `progress.py` | Report landing durability; do not own rollback or recovery orchestration |
| `departure.py` | Report exact channel motion and its classification |
| `investigate.py` | Return justified refinements from bounded counterfactual evidence; do not commit/revert/retry the drive |
| `steer.py` / `verify.py` | Execute an ordinary world-bound Bearing once or continue judgment of an already-executed intrascan candidate; return one judged candidate landing fact |

`_transition_once` remains the ordinary execution seam. It orients or consumes
one supplied Bearing, executes it, records observations, and returns the judged
candidate landing without owning repetition, rollback, provisional-tip
selection, or global promotion. A sibling verification-continuation seam
accepts an `ExecutedIntrascanCandidate`, runs the remaining ordinary gates on
that exact fork, and never pulses it again. During migration current local
adoption is adapted into those results before ownership transfers to the
reducer.

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

`intrascan.py` is the semantic owner for exact pulse-window diagnosis and
same-scan retry execution. It is entered only after an ordinary attempted
pulse supplies exact evidence that its effect was transient or ineffective;
it is never a preflight step for every Compass candidate.

```text
PulseWindowQuestion
    exact failed attempt and source checkpoint/world
    selected act and fixed EffectExpectation
    before/assertion/after snapshots
    owner-bound execution projection
    active requirements
    exact prior-source and consumer occurrences

PulseWindowDiagnosis
    PERSISTENT
        durable landing or later-scan frontier; keep it and orient again

    RETRY_SHAPE
        BEFORE_CONSUMER deadline
        exact missing steerable leaves and lazy branch identity

    NEEDS_PHASE
        BEFORE_ASSERTION or DURATION deadline
        exact setup/rearm/owner requirement

    BUDGET_EXHAUSTED
        attempted local identities and unresolved frontier

    INCOMPLETE
        missing projection, source, consumer, deadline, or supported domain

    IMPOSSIBLE
        separate complete finite-domain proof, never retry exhaustion

IntrascanRetryQuestion
    exact theory root or provisional tip
    original selected act plus one RETRY_SHAPE
    fixed expectations, PilotRungs, configured inputs, and avoid constraints
    finite producer-local alternatives and budget

ExecutedIntrascanCandidate
    complete physical act
    exact disposable fork/world which executed it once
    owner-bound projection and effect observations
    verification inputs and transient attempt evidence
```

The retry service owns exactly one assertion scan. It cannot commit the live
world, coast, cross a program boundary, promote a tip, or queue another action.
A returned executed candidate is not yet a tip. Normal `verify.py` gates judge
that exact execution without replaying it; only WorkingTheory may adopt an
accepted landing as its provisional tip. Rejected alternatives discard their
forks and retain only detached attempt evidence.

A `NEEDS_PHASE` result returns control to WorkingTheory. An ordinary durable
setup may be established as one fresh Bearing. Instruction-owned rearm and
duration phases remain owned by `program_step`, `Crossings`, and
`AdvanceProfile`. After any accepted phase, WorkingTheory records what happened
and asks Compass again; it never stores the original pulse as the next action.

### Pulse-window diagnosis and same-scan retry

1. Read the already-executed attempt's exact projection; do not fork or search
   merely because the final target did not move.
2. If the selected level, latch, state, or newly exposed steerable frontier
   survives for a later scan, return `PERSISTENT`. The ordinary trial landing
   remains eligible for commit and fresh orientation.
3. If an instruction owner requires sustained assertion, return `DURATION`
   evidence through `NEEDS_PHASE`; do not widen the pulse.
4. Follow exact source occurrences backward when the pulse's effective window
   was armed or foreclosed before the assertion scan. Return a
   `BEFORE_ASSERTION` setup requirement; a same-scan co-action is too late.
5. When the selected pulse appeared and reached, or should have reached, one
   exact same-scan consumer before reset, overwrite, or displacement, inspect
   that consumer's exact false guard reads.
6. Derive only the missing steerable leaves whose deadline is
   `BEFORE_CONSUMER`. If an earlier same-scan program write supplied a false
   read, follow its exact `ReadOccurrence.source` with strictly decreasing
   ordinals until the actionable leaf or an ambiguity is reached.
7. Preserve one observed `AND` branch as a conjunctive retry shape. Nominate
   `OR` siblings lazily; do not enumerate unrelated candidates or a Cartesian
   product across consumers.
8. Restore/fork the theory's exact root or provisional tip, including hidden
   memory, forces, configured inputs, and PilotRungs. Apply the original act
   plus one nominated same-scan shape and execute the assertion scan once.
9. Capture the complete executed act, fork, ordered projection, effect
   observations, and verification inputs. Stop at the first locally coherent
   candidate landing and pass that same execution to the ordinary verification
   continuation.
10. Never execute the selected composite a second time. If verification
    accepts it, WorkingTheory may adopt that exact world as its provisional
    tip; if verification rejects it, discard the fork and record the exact
    attempt.
11. Return `BUDGET_EXHAUSTED` or `INCOMPLETE` without weakening uncertainty
    into proof. `IMPOSSIBLE` still requires a separately recorded complete
    finite-domain proof.

The execution projection remains the semantic oracle for explaining the
failed pulse and binding the retry to exact occurrences. It is not used to
declare the retry globally successful before verification. Endpoint state is
never sufficient by itself.

Performance is an explicit contract:

- successful or durably productive ordinary trials invoke no intrascan search;
- one qualifying failed act opens at most one producer-local retry search;
- every alternative executes at most one exact assertion scan;
- the accepted candidate execution is verified and adopted without replay;
- trace, chart-edge, and retry-shape budgets never multiply;
- all losing forks are discarded immediately and no runner enters the theory
  ledger.

## Compass integration

Compass remains read-only with respect to WorkingTheory.

With no active theory:

```text
read current world
-> select one causal candidate
-> return and execute one ordinary Bearing
-> verify the exact trial
-> keep durable progress and orient freshly
   or open a WorkingTheory only when exact temporal evidence requires persistent refinement
```

Compass never runs intrascan retry search while ranking candidates and never skips a
ranked act because a speculative composite could not be proved. A failed
ordinary trial records its exact observations before another orientation.
Only an ineffective pulse with actionable temporal evidence opens or refines a
theory; ordinary rejection and sibling selection keep their existing semantics.

With an active theory, a detached `TheoryView` travels through
`NavigationConstraints`. Compass uses its claim, requirements, attempts, and
root/tip choice when materializing one next experiment. It may freshly expose
the original steer after a setup phase. WorkingTheory recognizes that steer as
the same intent under a new version or provisional tip; it is not a duplicate
attempt. Compass never changes the theory and no selected future survives the
read.

An identical act at the identical tip, theory version, requirements, and
deadline evidence is a repeated semantic attempt and is suppressed. The same
scalar pulse at a new provisional tip or under newly refined temporal evidence
is a new attempt.

For a `BEFORE_CONSUMER` retry, the intrascan service may execute one bounded
same-scan composite and return its executed candidate landing directly to the
normal verification continuation. Verification judges that exact fork; it is
never re-executed merely to enter the ordinary gates.

An ambient terminal coast with no selected causal claim remains maintenance
and does not open a refinable theory.

`Stuck` from Compass becomes relative to the supplied theory version. If a
failed retry exposes new exact temporal evidence, WorkingTheory creates a new
version and continues. If every known obligation is discharged, the original
steer is still ineffective, and no new requirement, sibling explanation, or
untried semantic attempt remains, the theory's explanation is falsified and
the theory is abandoned. Its root is restored and a fresh Compass read may
select a sibling claim. Public terminal `Stuck` is produced only when the
broader current-world theory frontier is exhausted.

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
- every proposed first chart edge remains an ordinary executable candidate.
  Its real trial supplies the exact occurrence claim; only an ineffective
  transient pulse may invoke temporal forensics and a same-scan retry;
- structured chart navigation outranks broad raw-trace alternatives for that
  exact claim, while current-world admission can still reject a chart whose
  live producer is unavailable;
- a failed first move creates only a theory/version/source-local attempt or
  `first_edge_allowed` exclusion after its exact trial. A persistent
  requirement contradiction may exclude the edge in every position for that
  constrained read.

This keeps opaque-chart behavior as a compatibility subset while making direct
charts principled rather than target-special.

## WorkingTheory transitions

The reducer consumes typed facts owned by the specialist modules:

```text
ADVANCE
    append an immutable progress snapshot with an accepted provisional tip or phase receipt
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
    freeze the exact version tombstone and exhausted explanation
    ask Compass freshly for a sibling claim
```

An executed intrascan candidate enters this reducer only after the ordinary
verification continuation accepts the exact fork. Before that judgment it is a
candidate landing, not theory progress. Acceptance adopts the already-executed
world without replay; rejection discards it and records only detached attempt
evidence.

Establishing a setup requirement is theory progress even when target distance
does not improve, because it advances the provisional tip and discharges or
changes an exact temporal obligation. It does not imply that the original
pulse will run next. Compass rereads the tip and must expose that steer again.

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

### Stage 3 - Add bounded same-scan retry mechanics

- Move scalar/guard schedule compilation behind the intrascan service.
- Materialize producer-local retry shapes, steady PilotRungs, and conjunctive
  expectations on disposable exact-source forks.
- Add exact attempt identities and a bounded producer-local retry budget.
- Add `PREVENT` obligations and occurrence-scoped requirement observations.
- Use optional finite kernel enumeration only to nominate local alternatives;
  the exact fork projection remains the occurrence oracle.
- Do not route production Compass through the service yet.

### Stage 4 - Introduce WorkingTheory in shadow mode

- Add the theory records, reducer, and ledger to `_PilotState` knowledge.
- Derive shadow theory claims/versions/attempt dispositions from the existing
  loop without controlling world adoption or rollback.
- Assert that no Bearing, CandidateRead, OrientationRead, fork, or route suffix
  enters the ledger.
- Add deterministic reducer tests for every transition and successor link.

### Stage 5 - Classify temporal pulse deadlines without changing decisions

- Keep production Compass, Orientation, execution, commit, rollback, and
  candidate ordering byte-for-byte equivalent to Stage 4.
- Classify exact attempted pulse windows as `BEFORE_ASSERTION`,
  `BEFORE_CONSUMER`, `LATER_SCAN`, `DURATION`, or `UNKNOWN`.
- Prove that a surviving level, latch, state, or newly exposed steerable
  frontier produces `LATER_SCAN` and never invokes composition merely because
  the final target did not move.
- Derive `BEFORE_ASSERTION` only from an exact prior occurrence or owner-bound
  hidden-state receipt. A same-scan assignment must not masquerade as a phase
  which had to be established earlier.
- Derive `BEFORE_CONSUMER` from one exact transient producer/consumer window,
  including pulse consumption, false consumer leaves, overwrite,
  displacement, and reset.
- Classify timer/counter bases and autonomous operations as `DURATION` through
  their existing instruction owners rather than widening them into pulses.
- In shadow, nominate only producer-local missing steerable leaves. Keep `AND`
  shapes conjunctive and `OR` branches lazy; do not execute a Cartesian search
  or preflight unrelated Compass candidates.
- Add counters asserting zero intrascan forks for successful/durable ordinary
  trials and a fixed local bound for one qualifying ineffective pulse.
- Assert identical production Bearings, events, checkpoints, and worlds with
  shadow temporal diagnosis enabled or disabled.

### Stage 6 - Give WorkingTheory ownership of temporal pulse refinement

- Add a detached `TheoryView` to navigation constraints, but keep Compass as a
  fresh reader which returns one ordinary next Bearing rather than a proved
  composite.
- Open a controlling theory only when an exact ineffective pulse produces an
  actionable temporal obligation which must persist across attempts or a
  setup detour.
- Give WorkingTheory the exact rollback root, immutable versions, unresolved
  temporal requirements, one accepted provisional tip, and detached attempt
  history. It retains why the original steer is being retried, never the steer
  itself.
- For `BEFORE_ASSERTION`, restore the exact phase source, let Compass establish
  one ordinary durable setup, accept it as a provisional tip, and orient
  freshly. If the original scalar steer is exposed again, treat it as a new
  semantic attempt because its tip/version/evidence changed.
- For `BEFORE_CONSUMER`, let the intrascan service execute one bounded
  producer-local composite on an exact fork. Pass that already-executed
  candidate landing through the normal verification continuation and, if
  accepted, adopt that exact world without replay.
- For `LATER_SCAN`, retain the ordinary landing and move forward without
  opening a composition theory or reverting useful state.
- For `DURATION`, retain only the owner-declared phase evidence and ask Compass
  again; detailed rearm and autonomous-continuation ownership remains Stage 8.
- Suppress only a byte-identical semantic attempt at the identical root/tip,
  version, requirements, deadlines, and physical act. A repeated scalar steer
  under a refined theory or newer tip is not a duplicate.
- Refine when a failed retry exposes genuinely new exact evidence. When every
  known requirement is discharged, the steer remains ineffective, and no new
  requirement, sibling explanation, or untried semantic attempt remains,
  abandon the falsified theory and restore its root.
- Split theory-local expectation failures from theory-independent Compass act
  nogoods. Empirical exhaustion or budget exit abandons only the exact theory
  version and never proves the act globally impossible.
- Split read-only `chart_roles` discovery from opaque `pipeline_roles`, then
  split static discovery from live admission. Chart edges remain ordinary
  executable candidates; only their exact failed trials may enter temporal
  diagnosis. Preserve structured-chart precedence without pre-executing every
  edge.
- Activate first-edge-only route exclusion under exact theory/version/source
  scope after an exact attempted edge, not after speculative preflight.
- Keep the public orientation union unchanged and add an internal
  already-executed-candidate verification seam analogous to investigation.

### Stage 7 - Transfer checkpoint-local failed-effect recovery

- Extend the Stage 6 root/provisional-tip owner to checkpoint-local recovery:
  WorkingTheory chooses exact source restoration, accepts or rejects
  `_transition_once`'s judged landing as its provisional tip, and alone
  promotes a stable landing.
- Replace `_repair_one_active_requirement` and `_nested_guard_act` with theory
  refinement plus fresh Compass orientation and narrowed intrascan retries.
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

### Stage 8 - Transfer cross-scan phases and autonomous continuation

- Extend the Stage 7 root/tip owner with immutable progress snapshots and
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

### Stage 9 - Subsume departure, regression, and investigation orchestration

- Refactor progress/departure/investigation entry points to return typed facts
  or refinements instead of committing, reverting, or recursively retrying.
- Route those facts through the WorkingTheory reducer.
- Replace pending-departure orchestration with an open theory plus provisional
  tip and bounded evidence lifetime.
- Turn a regression of a proved TheoryReceipt into a linked successor theory.
- Delete `_investigate_and_revert` and the remaining special recovery loops only
  after equivalent event and acceptance coverage is green.

### Stage 10 - Harden lifetime, diagnostics, and pruning

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

- A persistent level with no edge-sensitive or duration-owned consumer remains
  useful on the following scan, invokes no retry search, and is not reverted.
- A newly exposed durable steerable frontier is ordinary forward progress even
  when target distance is unchanged.
- A timer/counter base produces `DURATION` and uses hold/coast rather than a
  widened pulse.
- A prior scan which armed, cleared, selected, or foreclosed the pulse produces
  `BEFORE_ASSERTION`; a same-scan co-action is rejected as too late.
- A consumed pulse whose exact consumer has one false steerable guard leaf
  produces a `BEFORE_CONSUMER` retry shape.
- A later writer overwrite, displacement, or reset retains the exact deadline
  and harmful occurrence.
- A selected producer made absent by an earlier write follows exact sources to
  the responsible temporal requirement.
- Recursive deadline inversion terminates only on strictly earlier ordinals.
- Missing `AND` leaves remain conjunctive; `OR` siblings are tried lazily one
  at a time without a Cartesian product.
- Conflicting assignments reject only that exact retry attempt.
- Repeated subroutine calls and branch occurrences retain dynamic identity.
- Incomplete projections and folded windows never establish a deadline,
  success, or prevention.
- One narrowed composite executes exactly once. Normal verification judges the
  same fork, and an accepted landing is adopted without replay.
- A locally coherent candidate rejected by an ordinary avoid, effect, safety,
  or dead-end gate is discarded and never becomes a provisional tip.
- Budget exhaustion produces no impossibility proof.
- Direct and opaque chart edges are tried as ordinary candidates; temporal
  retry begins only from their exact failed execution evidence.
- Successful/durable ordinary trials perform zero intrascan forks, and one
  qualifying failure cannot multiply trace, chart, and retry budgets.

### Theory lifecycle

- Immutable versions strengthen only on new exact evidence.
- Different Bearings under one version create attempts, not versions.
- Only one theory owns the provisional tip.
- An executed intrascan candidate is not a tip until ordinary verification
  accepts it; acceptance adopts the already-executed world without replay.
- Abandon restores the root and tombstones only the exact version.
- Prove promotes the full `_World`, including PilotRungs.
- A later regression creates a successor rather than mutating a closed theory.
- No stale Bearing or action suffix survives a world observation.
- A freshly rediscovered scalar steer at a newer provisional tip or refined
  version is a new semantic attempt; the identical steer at the identical
  tip/version/requirements/deadlines is suppressed.
- Establishing a prior setup advances the theory but never queues the original
  pulse; Compass must expose it again from the new tip.
- If all known requirements are discharged and the steer still fails without
  new exact evidence or an untried explanation, the theory is abandoned as
  falsified.
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
  same rule, preserve chart-over-broad-trace precedence, and are tried once
  before exact temporal evidence nominates a narrowed multi-input retry. A
  failed wildcard is rejected only as the current first move, with parity to
  the existing opaque chart fixtures.
- Motivating external acceptance: from a pristine scan-0 PLC,
  `how(HeelStep == 81)` reaches 81 by preserving durable intermediate state,
  establishing any prior temporal setup as its own phase, and retrying only
  the exact transient pulse whose same-scan consumer exposes missing
  PRODUCE/PREVENT requirements. This names the real case only at the
  end-to-end boundary.
- Repository acceptance expresses the same mechanics with neutral names and
  generic stepper/producer/consumer semantics. It must distinguish persistent
  later-scan progress, prior setup, same-scan retry, and owner-held duration;
  reject later same-scan overwriters; and contain no customer-specific tags,
  machine concepts, or external paths.
- Tumbler golden comparison at every stage which changes orientation ordering
  or event output.

## Completion criteria

The migration is complete when:

- `_pilot_loop_events` has one theory-centered repetition path;
- Compass is the only producer of one fresh ordinary next Bearing;
- the intrascan service is the only owner of exact pulse-window diagnosis and
  bounded same-scan retry execution;
- ordinary successful/durable trials bypass intrascan search entirely;
- a narrowed composite is executed once, verified on that exact fork, and
  adopted without replay only after acceptance;
- WorkingTheory is the only owner of refinement, provisional tips, restoration,
  promotion, abandonment, and successor creation;
- progress, departure, investigation, and recovery modules report evidence but
  do not run competing orchestration loops;
- exact positive/negative occurrences and requirement deadlines determine
  acceptance;
- useful later-scan state is retained, prior deadlines become separate phases,
  same-scan deadlines become narrowed retries, and duration remains with its
  instruction owner;
- no queued action, retained suffix, or predicted world survives an
  observation;
- `make lint` and `make test-pilot` pass, including Tumbler and the Heel
  end-to-end acceptance, while repository regression fixtures remain generic.
