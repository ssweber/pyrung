# PILOT WWTD, Temporal Forensics, and WorkingTheory Plan

This document is the migration plan for temporal pulse diagnosis and
WorkingTheory. It starts from PILOT's existing first principle:

> WWTD -- what would the tech do?

A technician reads the ladder and trend, makes the smallest reversible
intervention, and observes what the program actually did. The PLC is already
running; timers, counters, sequences, and instruction-owned operations may
continue between observations. PILOT therefore steers one observation at a
time rather than storing and executing a plan.

## The operational model

The ordinary loop remains small:

```text
READ
    observe the current world
    collect the relevant instrument readings
    choose one current-world Bearing, NeedProbe, or Stuck

TRY
    execute one Bearing on a fork
    observe exact reads, writes, motion, and verification gates

KEEP
    adopt only a gate-approved landing
    decide whether it is useful and durable, still moving, or should be reverted

READ AGAIN
    discard the old orientation and every unexecuted future
    retain only learned facts, active scoped logic, and safe checkpoints
```

Most turns require nothing more. WorkingTheory is not the ordinary loop and
intrascan is not a preflight solver for every candidate.

## How the Compass instruments fit together

Compass is the persistent navigation-knowledge facade and the entry point for
one fresh orientation. It is not a second orchestration loop. Several readers
may contribute to the same current-world read:

| Question                                                          | Evidence owner                                           |
| ----------------------------------------------------------------- | -------------------------------------------------------- |
| What local condition or lever leads toward the target?            | `trace.py`, static expressions, availability             |
| What charted transition is relevant here?                         | navigation evidence, pipeline graphs, chart catalogs     |
| Can one exact program producer continue under unchanged controls? | `program_step.py`, `AdvanceProfile`, `AdvanceIndex`      |
| Is the program stopped at an actual external handoff?             | `program_step.py`, awaited-action evidence               |
| What has worked or failed in this executable world before?        | `CompassKnowledge`                                       |
| What constraints must the next experiment respect?                | avoid, active requirements, holds, optional `TheoryView` |
| Is the remaining frontier unreadable without experimentation?     | `skiff.py`                                               |

`options.py` materializes those readings into one `CandidateRead`.
`orientation.py` applies explicit precedence and returns one
`Bearing | NeedProbe | Stuck`. No individual reader chooses an action by
itself, and the complete read expires after any observation.

`program_step` has a deliberately narrow but important contract. It does not
ask whether the whole target will eventually be reached. For one exact
selected producer or instruction-owned operation, it asks what happens when
controls remain otherwise unchanged:

- `KEEP_RUNNING`: an owned boundary or progress witness is moving; coast and
  observe it;
- `NEEDS_INPUT`: the settled operation is genuinely waiting at an external
  handoff;
- `INTERRUPTED`: real program motion made the attempted reading stale; preserve
  and observe that motion;
- `UNCLEAR`: make no forward claim.

WorkingTheory never bypasses these readers. A theory supplies remembered facts
and one exact state to a fresh ordinary orientation; it does not privately
select whether to act, coast, or probe.

## The new question: why was this pulse ineffective?

An ordinary pulse may appear not to work for very different reasons. Endpoint
state alone is insufficient: a value may be written, read, overwritten, and
read again within one scan.

### One steer execution is the shared evidence source

The ordinary steer has already paid to execute the selected action. Stage 5
must interpret that execution, not reproduce it. The attempt should expose one
shared evidence bundle containing, as available:

- the exact source world and applied physical act;
- before, assertion, and after snapshots;
- the assertion scan's owner-bound ordered read/write projection;
- selected effect observations, including producer, consumer, overwrite,
  reset, and displacement occurrences;
- verification gate results and the existing outcome/frontier assessment;
- the accepted candidate landing identity, when verification passed; and
- later progress/departure receipts produced by normal post-commit monitoring.

Effects and intrascan reuse the ordered projection to answer whether the
producer appeared, which consumer read it, and whether a later writer
overwrote or displaced it. Outcome reuses the same attempt to classify
immediate bearing effect and frontier change. Progress reuses the accepted
landing and subsequent real monitoring observations to decide durability.
Durability may require later real scans; it never requires re-executing the
original steer scan.

Stage 5 may consume an already-produced `program_step` reading when the
orientation involved an exact program producer. It does not rerun
`program_step` merely to obtain a label. A later fresh orientation from an
accepted landing may perform its normal bounded current-world
`program_step` read.

No Stage 5 consumer may reconstruct the steer, fork the source again, rerun
the assertion scan, start an extra progress monitor, or independently rebuild
the same owner-bound projection. Missing shared evidence produces
`UNRESOLVED`; it does not authorize speculative replay.

The technician's questions are:

1. Did the attempt leave useful state or expose a useful new frontier?
1. Is an instruction-owned operation already carrying the work forward?
1. If the pulse was ineffective, was the missing condition required before
   the scan began or at one consumer later in the same scan?
1. Is the evidence complete enough to make any of those claims?

Those questions produce five plain next-step interpretations:

| Interpretation      | Plain meaning                                                                                 | Next step                                                                          | Decisive evidence owner                                |
| ------------------- | --------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `KEEP_AND_REREAD`   | Useful state or a useful frontier survived                                                    | Keep the accepted landing and orient freshly                                       | `outcome.py`, `progress.py`                            |
| `COAST_TO_BOUNDARY` | The selected instruction-owned operation is advancing                                         | Let the program run to its exact boundary                                          | `program_step.py`, `AdvanceProfile`                    |
| `SETUP_FIRST`       | A requirement had to be established before the assertion scan                                 | Establish that setup as an ordinary phase, retain its accepted landing, and reread | requirements plus exact prior-source or owner evidence |
| `RETRY_TOGETHER`    | A named condition had to be true when an exact same-scan consumer read the pulse              | Try the original pulse with only the missing consumer shape                        | effects plus intrascan occurrence evidence             |
| `UNRESOLVED`        | Owner, projection, occurrence, deadline, or supported semantics are incomplete or conflicting | Stop guessing; preserve typed missing evidence                                     | the reader that found the gap                          |

The former names remain useful as supporting technical vocabulary:

- `LATER_SCAN` supports `KEEP_AND_REREAD`;
- `DURATION` is routed to the instruction owner and supports
  `COAST_TO_BOUNDARY`;
- `BEFORE_ASSERTION` supports `SETUP_FIRST`;
- `BEFORE_CONSUMER` supports `RETRY_TOGETHER`;
- `UNKNOWN` supports `UNRESOLVED`.

These are not five facts owned by `intrascan.py`. Two are progress or
instruction-ownership judgments, two are occurrence deadlines, and one is
evidence quality. Stage 5 combines specialist facts without moving their
semantic ownership.

## Intrascan's bounded role

Intrascan is the oscilloscope for an exact assertion scan. It reads an
already-executed attempt's owner-bound projection and answers factual questions:

- Did the selected producer occur?
- Did its value reach the intended consumer-relative shape?
- Which exact read was false when the consumer needed it?
- Which later writer overwrote, reset, or displaced the value?
- Did an earlier same-scan write supply that false read?
- Can the source walk reach an actionable steerable leaf while strictly moving
  to earlier ordinals?
- Is any occurrence, owner, projection, or supported expression ambiguous?

Stage 5 diagnosis executes no retry and performs no intrascan candidate search.
It consumes the real ordinary attempt and returns detached findings.

Stage 6 adds a separate retry seam for `RETRY_TOGETHER`:

```text
SameScanRetryQuestion
    exact root or accepted provisional source
    original selected pulse
    one exact missing consumer shape
    fixed expectations, active requirements, holds, and avoid constraints

ExecutedSameScanCandidate
    complete physical act
    exact disposable fork that executed it once
    ordered projection and effect observations
    inputs required to continue ordinary verification
```

The retry executor owns exactly one assertion scan. It cannot commit the live
world, coast, cross a program boundary, promote a checkpoint, or queue another
action. The returned candidate is verified on that same execution. Acceptance
may adopt that exact fork; rejection discards it. There is no
prove-then-replay cycle.

The current `close_intrascan` implementation remains a production-inert
laboratory while this split lands. Its exact projection, occurrence, Boolean
alternative, `PREVENT`, configured-input, PilotRung, and attempt-identity
mechanics are reusable. Its `WITNESS`/complete-overlay search contract is not
the production target.

## WorkingTheory: the technician's job card

Most actions do not open a theory. A WorkingTheory opens only when one exact
ineffective trial produces actionable temporal evidence that must survive a
fresh read or setup detour:

- `SETUP_FIRST`; or
- `RETRY_TOGETHER`.

Ordinary useful progress, ordinary program-owned continuation, ordinary
rejection, and incomplete evidence do not automatically open an active theory.
Unresolved or unattributed facts may remain in knowledge without becoming a
controlling theory.

A WorkingTheory records:

- the local claim: one selected producer can make one value and
  consumer-relative shape effective at one exact program boundary;
- the safe rollback root;
- one accepted provisional state, if useful setup has been established;
- exact requirements and occurrence deadlines learned so far;
- exact experiments already attempted under each evidence version;
- accepted phase receipts and remaining local budget.

It never records:

- a future `Bearing` or `NavigationAct`;
- a candidate cursor or route suffix;
- a predicted PLC world;
- an action queue such as release-then-assert;
- a disposable retry runner after its verification lifetime ends.

There is at most one active local theory for one `how()` target invocation.
Closed sibling theories may remain in the ledger. “For a target” describes
the invocation scope, not a target-wide theory that must exist throughout the
ordinary drive.

### Lifecycle in plain language

| Lifecycle term                                  | Technician meaning                                                                                                                                  |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Open                                            | Preserve the local question, rollback state, and first exact failure                                                                                |
| Advance                                         | Keep useful accepted setup and reread from it                                                                                                       |
| Refine                                          | Add genuinely new exact evidence; do not merely record another try                                                                                  |
| Validate (`PROVED` in the current record model) | The local producer-to-consumer claim worked, every active local obligation was observed at its occurrence, and the landing is acceptable and stable |
| Abandon                                         | The local explanation was falsified or its bounded experiments ended; restore the safe root without claiming global impossibility                   |

Validation is local. A theory can close while `how()` continues toward its
larger target. Reaching the final target is not the definition of proving every
local theory.

Establishing setup advances a theory but never queues the original pulse.
Compass must rediscover any next pulse from the accepted provisional state. A
rediscovered scalar pulse at a new state or under a refined evidence version is
a new semantic attempt; a byte-identical attempt at the identical source,
version, requirements, deadlines, and physical act is suppressed.

The first concrete physical action is attempt identity, not theory identity.
The current `selected_artifact_identity` field must be audited before control
transfer so it cannot make an initial action part of the stable claim by
accident.

## Knowledge, worlds, and mutation

The theory ledger is knowledge-side and survives `_World` restoration. It
stores detached semantic identities, facts, and receipts, not speculative
worlds.

Executable roots and accepted provisional states remain owned by `_PilotState`'s
checkpoint/world registry. The pure theory reducer may decide semantically:

- resume from boundary identity X;
- accept verified landing identity Y;
- promote stable landing identity Z; or
- abandon and restore the root.

A thin drive-side resolver validates those identities against retained worlds
and performs the actual restore, adopt, or promotion. The reducer does not
both remain detached and mutate a live PLC.

Knowledge and proof scopes stay distinct:

| Evidence                  | Meaning                                                                                 | Owner                  |
| ------------------------- | --------------------------------------------------------------------------------------- | ---------------------- |
| Theory attempt receipt    | This exact experiment had this result under this local evidence version                 | Theory ledger          |
| Theory-local tombstone    | Do not repeat this identical semantic experiment                                        | Theory ledger          |
| Act nogood                | This physical act is invalid in this exact executable world independently of the theory | `CompassKnowledge`     |
| Static-edge contradiction | Complete evidence disproved a chart edge in its declared scope                          | Compass static overlay |
| `NogoodProof`             | A named complete finite domain proves impossibility                                     | Separate proof record  |

Budget exhaustion is never impossibility. Ambiguous receipts, incomplete
projections, unsupported expressions, and missing checkpoints remain typed
unresolved evidence.

## Ownership boundaries

| Owner                        | Responsibility                                                                                     |
| ---------------------------- | -------------------------------------------------------------------------------------------------- |
| Compass                      | Persistent navigation catalogs/knowledge and the facade for one fresh read                         |
| `options.py`                 | Materialize the current readers into one `CandidateRead`                                           |
| `orientation.py`             | Apply current-world precedence and return one `Bearing`, `NeedProbe`, or `Stuck`                   |
| `program_step.py`            | Read whether one exact producer continues, waits for input, was interrupted, or is unclear         |
| `steer.py` / `verify.py`     | Execute one ordinary Bearing and judge its exact execution                                         |
| `effects.py`                 | Observe selected positive/negative producer-to-consumer obligations                                |
| `intrascan.py`               | Explain ordered facts inside one already-executed assertion scan                                   |
| Stage 5 shadow interpreter   | Combine specialist facts into one readable next-step interpretation without controlling production |
| Same-scan retry executor     | Execute at most one nominated local alternative per attempt and return its still-live candidate    |
| `outcome.py` / `progress.py` | Classify useful landing and durability                                                             |
| `departure.py`               | Classify exact observed channel motion                                                             |
| `investigate.py`             | Return bounded counterfactual evidence; do not own the outer drive                                 |
| WorkingTheory reducer        | Own local lifecycle decisions by detached identity                                                 |
| Drive-side theory resolver   | Resolve retained identities and perform authorized world mutation                                  |

`_transition_once` remains the ordinary execution seam. During migration it
still performs local adoption, but its target contract is to return one judged
candidate landing without owning repetition, theory lifecycle, or global
promotion. A sibling continuation seam accepts an already-executed same-scan
candidate and runs the remaining ordinary verification gates without pulsing
again.

## Honest status at current HEAD

The two most recent plan-only commits changed the intended Stage 5 after the
implementation commit named “stage 5 completion.” Current status is therefore:

| Stage | Status                                                       | Meaning                                                                                                        |
| ----- | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------- |
| 0     | Landed                                                       | Known requirement-aware baseline and exact recovery evidence restored                                          |
| 1     | Landed                                                       | Report-only one-scan forensics extracted behind a compatibility adapter                                        |
| 2     | Landed                                                       | Strictly decreasing same-scan occurrence-source walks implemented                                              |
| 3     | Mechanics landed; production contract superseded             | Bounded closure laboratory exists, but complete-overlay `WITNESS` search is not the revised production design  |
| 4     | Reducer prototype landed; lifecycle trigger reshaped         | Detached immutable shadow ledger is production-safe; ordinary accepted, program-owned, and unresolved attempts no longer open it |
| 5     | Landed                                                       | The five-way zero-retry combiner reuses accepted outcome, existing `program_step`, the already-produced intrascan report, and exact delayed-regression receipts without changing decisions |
| 6+    | Not started                                                  | No WorkingTheory production control or already-executed retry adoption                                         |

`close_intrascan`, `TheoryView`, chart-role discovery, first-edge exclusions,
and related seams remain useful inert experiments. Their existence does not
mean Stage 6 control has landed.

The first real-program check is intentionally cheap: the Burner drive's first
ordinary Bearing already composes `Cmd_UnitModeChgRequest`,
`Cmd_Mode_Production`, and the Clear route action through existing candidate
machinery. The shadow interpreter observes the accepted batch as
`KEEP_AND_REREAD`; it must not invent a `RETRY_TOGETHER` repair for work that
already succeeded.

Two timer-alarm reproducers pin the motivating deadline distinction. In
`aborted_on_first_scan`, the scan-0 preset read precedes `.Done`, and `.Done`
then enables the Alarm writer which overwrites the useful step result; because
that deadline precedes any steer, the interpretation is `SETUP_FIRST`. In
`alarmed_at_start`, Reset is provisionally accepted before post-commit
monitoring sees the same `.Done` overwrite. Stage 5 defers that accepted
interpretation, then joins the monitor's retained `ActiveRequirement` and
`FailedEffectReceipt` back to the original assertion scan and reports
`RETRY_TOGETHER`. This join consumes the harmful writer's existing ordered
projection; it must not call Compass, replay a scan, or rebuild `how(state)`.

Diagnostic follow-up, not a Stage-5 blocker: render the retained requirement
and original act as a direct technician instructionâ€”for example, â€œset
`WatchdogPresetMs > 10` firstâ€ or â€œretry `Reset=True` together with
`WatchdogPresetMs > 10`â€â€”instead of leaving that useful answer inside raw
supporting identities.

## Migration sequence

Every stage ends with lint and focused `make test-pilot` validation. Tumbler is
an explicit, process-isolated gate: use `make watch-pilot-burner` or
`make watch-pilot-completed` before full `make test-tumbler`, and require
Tumbler parity when event or orientation ordering can change. Do not transfer
ownership while the current stage has unexplained failures.

### Stage 4.5 -- Re-ground the contract

- Make this WWTD operational model the migration source of truth.
- Keep Stages 1-2 forensic behavior and tests.
- Treat current Stage 3 closure and Stage 4 shadow as prototypes to reshape,
  not APIs that later production must preserve.
- Make the theory opening trigger local and exceptional.
- Define the detached-reducer/drive-resolver mutation boundary.
- Keep chart generalization out of the initial temporal-control slice.

### Stage 5 -- Interpret temporal evidence without changing decisions

- Consume the shared evidence bundle from one already-executed ordinary steer;
  do not preflight candidates or reconstruct the steer.
- Assemble the relevant existing facts from outcome/progress, `program_step`,
  requirements, effects, and intrascan.
- Produce one readable `KEEP_AND_REREAD`, `COAST_TO_BOUNDARY`, `SETUP_FIRST`,
  `RETRY_TOGETHER`, or `UNRESOLVED` interpretation with exact supporting
  identities and a plain reason.
- Treat conflicting specialist facts as `UNRESOLVED`; do not create a hidden
  precedence rule merely to force a label.
- Execute zero new intrascan retry forks and no new candidate action.
  Existing read-only projections such as `program_step` retain their current
  bounded behavior.
- Build or acquire the assertion projection once per execution owner and share
  it among effect, requirement, and intrascan readers. Cache/index lookup may
  repeat; PLC execution and projection construction may not.
- Reuse existing verification/outcome facts for immediate progress and normal
  post-commit receipts for durability. Do not start a second monitor or replay
  the original act to decide `KEEP_AND_REREAD`.
- Surface shadow interpretations in diagnostics and make reducer failures
  visible to tests without affecting production.
- Reshape the shadow lifecycle:
  - useful landing: no theory;
  - program-owned continuation: no theory;
  - prior setup: shadow open/refine/advance;
  - same-scan missing shape: shadow open/refine/attempt;
  - unresolved: typed knowledge, no automatic active theory.
- Assert identical production Bearings, candidate order, events, checkpoints,
  worlds, and Compass knowledge with Stage 5 interpretation enabled or
  disabled.
- Assert zero intrascan retry executions for every Stage 5 case.
- Record countable diagnostics for ordinary steer executions, assertion
  projection builds, intrascan interpretations, `program_step` projections,
  post-commit monitoring scans, and same-scan retry executions.

### Stage 6A -- Control one same-scan missing-permissive slice

- Start with one neutral `RETRY_TOGETHER` fixture.
- Open one local controlling theory only after the exact ordinary pulse fails.
- Nominate only the exact missing steerable leaves for one observed consumer.
- Preserve conjunctive `AND` shape; nominate `OR` branches lazily.
- Execute one narrowed candidate once on the exact root or accepted provisional
  source.
- Pass that same live execution through the ordinary verification continuation.
- Adopt only after normal acceptance; otherwise discard it and retain detached
  attempt evidence.
- Validate or abandon the local theory, then return to ordinary fresh
  orientation.
- Do not activate chart-role generalization or first-edge theory filtering in
  this slice.

### Stage 6B -- Control prior-scan setup

- Add one neutral `SETUP_FIRST` fixture.
- Restore the exact source selected by the prior deadline.
- Let Compass establish one ordinary setup Bearing.
- Accept useful setup as the theory's provisional state and orient freshly.
- Never store the original pulse as a continuation.
- Treat its rediscovery at the new provisional state as a new semantic attempt.
- Abandon and restore the root when the local explanation is falsified or the
  bounded attempt set ends without new evidence.

### Stage 7 -- Transfer checkpoint-local failed-effect recovery

- Replace `_repair_one_active_requirement` and `_nested_guard_act` with local
  theory refinement, ordinary fresh Compass reads, and the Stage 6 retry seam.
- Make source requirements, receipt matching, schedules, and dedupe keys
  theory-aware so separate claims cannot mix.
- Convert bootstrap and historical program-guard prevention into ordinary
  positive/negative obligations.
- Preserve newest exact writer and nearest usable pre-writer checkpoint
  selection.
- Delete retained replay, retained-prefix execution, retained-Bearing
  composition, current-blocker replay, and remaining historical action-suffix
  ownership only after their evidence contracts pass through the common path.

### Stage 8 -- Transfer cross-scan phases and autonomous continuation

- Keep autonomous-continuation judgment with `program_step` and instruction
  owners; WorkingTheory records accepted phase facts but does not reproduce
  instruction algebra.
- Model one-shot rearm as an ordered false-scan phase followed by a fresh read,
  never as a release/assert overlay.
- Preserve exact one-shot hidden identity and distinguish a disposable armed
  source from a genuinely committed spent source.
- Introduce explicit requirement lifetime only when the controlling vertical
  slices demonstrate the need: `ACTIVE | DISCHARGED | INVALIDATED | AMBIGUOUS`.
- Keep local repair distinct from later occurrence discharge.
- Replace `_RecoveryContinuation` with accepted phase receipts.

### Stage 9 -- Subsume departure, regression, and investigation orchestration

- Make progress, departure, and investigation return typed facts or
  refinements instead of running competing commit/revert/retry loops.
- Route those facts through the theory lifecycle and drive-side resolver.
- Represent pending departure as accepted provisional state with bounded
  evidence lifetime.
- Turn regression of a validated local receipt into a linked successor theory.
- Delete special recovery loops only after event and behavioral parity is
  green.

### Stage 10 -- Harden lifetime, diagnostics, and pruning

- Retain every boundary referenced by an active requirement, unresolved
  incident, open theory, expectation receipt, theory receipt, or successor.
- Match identities by executable world, epoch, dynamic occurrence, selected
  writer/consumer, scope, and deadline; fail closed on ambiguity.
- Add explicit PilotRung supersession/revocation receipts.
- Harden masking versus neutralization, correction self-defeat, incompatible
  requirements, checkpoint loss, and budget exits.
- Emit readable lifecycle and temporal-evidence events.
- Remove superseded compatibility adapters and update the package guide to the
  final single-loop ownership model.

### Independent navigation follow-up

Chart discovery and first-edge scoping remain valuable but are not prerequisites
for validating temporal theory control:

- separate read-only `chart_roles` from opaque `pipeline_roles`;
- discover charts independently of current-target admission;
- admit relevant edges during fresh orientation;
- try an admitted chart edge as an ordinary candidate before temporal
  diagnosis;
- scope a failed first edge to exact theory/version/source only after its real
  trial;
- preserve structured-chart precedence without pre-executing every edge.

Schedule this work after the Stage 6 vertical slices unless a prerequisite is
demonstrated by a neutral temporal fixture.

## Test gates

### Stage 5 interpretation

- Overwrite, reset, displacement, false-consumer, and surviving-value
  interpretations reuse the projection from the already-run steer scan; the
  classifier performs no replay and builds no second projection for that
  execution owner.
- Useful persistent state or a newly exposed useful frontier yields
  `KEEP_AND_REREAD`, opens no theory, and executes no intrascan retry.
- An owner-declared moving timer, counter, or autonomous operation yields
  `COAST_TO_BOUNDARY` from `program_step` evidence; intrascan does not invent
  duration semantics.
- An exact prior occurrence or owner-bound hidden receipt yields `SETUP_FIRST`;
  a same-scan assignment is rejected as too late.
- A transient producer plus one false exact consumer guard yields
  `RETRY_TOGETHER` with only the missing steerable consumer shape.
- Missing or ambiguous producer, consumer, owner, projection, deadline, or
  supported expression yields `UNRESOLVED` with the missing evidence named.
- Conflicting specialist facts yield `UNRESOLVED` rather than arbitrary
  precedence.
- Stage 5 performs no new retry executions and changes no production decision,
  event, checkpoint, world, or Compass observation.

### Performance and evidence reuse

- Responsiveness is part of correctness. A `how()` result that is eventually
  right but withholds DAP progress for minutes is not shippable.
- Deep Tumbler drives run in a disposable worker while an outside orchestrator
  owns a total wall budget, a maximum inter-event silence budget, and a maximum
  DAP-visible silence budget, plus an explicit worker-tree memory cap. A
  timeout must retain the last visible fragment, recent structured
  event/scan/state receipts, current and peak memory, and a live Python stack
  dump before terminating the worker. A deadline inside the event loop is
  insufficient because it cannot fire while the next event is withheld.
- `make test-pilot` excludes the potentially OOM full Tumbler suite. Enter
  Tumbler through `make watch-pilot-burner` or
  `make watch-pilot-completed`, then run `make test-tumbler` only after the
  bounded drive remains responsive.
- One ordinary outer-loop turn executes only its selected Bearing. Stage 5
  adds zero PLC scans, zero action forks, and zero Compass orientations.
- One assertion execution has at most one owner-bound projection build.
  Effects, requirements, intrascan, outcome, and diagnostics share its indexed
  evidence rather than replaying the PLC.
- A source walk has a visited set and follows strictly earlier ordinals; one
  diagnosis cannot revisit occurrences or branch into an unbounded history
  search.
- Successful, useful, program-owned, and unrelated ordinary trials never enter
  same-scan retry enumeration.
- One qualifying failure may open one producer-local retry search. Trace,
  chart, Boolean-alternative, and retry budgets add; they never multiply into a
  Cartesian candidate-execution budget.
- `AND` leaves form one required shape. `OR` alternatives are nominated lazily
  and tried one at a time. No full steerable-input domain is enumerated unless
  a separately named complete finite proof explicitly requires it.
- An accepted Stage 6 retry execution continues verification and adoption on
  the same fork; it is never executed once for intrascan proof and again for
  ordinary gates.
- Count-based regression tests cover the algorithmic scaling contract on a
  `how(state)` fixture with multiple irrelevant trace choices, chart edges, and
  retry alternatives. Increasing two independent candidate dimensions must
  not multiply PLC executions, projection builds, or retry attempts. The
  outside-process wall and silence budgets separately gate user-visible
  end-to-end responsiveness without pretending that machine time explains the
  algorithmic cause.

### Same-scan execution

- Missing `AND` leaves remain conjunctive.
- `OR` siblings are nominated lazily without a Cartesian product.
- Repeated subroutine calls and branch occurrences retain dynamic identity.
- Earlier same-scan writes are followed only through strictly decreasing
  ordinals.
- One nominated composite executes exactly once.
- Normal verification judges that same fork; acceptance adopts without replay.
- Avoid, effect, safety, spin, or dead-end rejection discards the candidate and
  never advances the theory.
- Budget exhaustion creates no impossibility proof or global nogood.

### WorkingTheory lifecycle

- No theory opens for ordinary success, useful durable progress, or
  program-owned continuation.
- `SETUP_FIRST` and `RETRY_TOGETHER` open one local theory from exact evidence.
- New evidence creates a version; another experiment under unchanged evidence
  creates an attempt.
- Accepted setup advances one provisional state but queues no future action.
- A validated local claim closes while the larger `how()` drive may continue.
- Abandon restores the root and tombstones only the exact local version.
- A later regression creates a successor rather than mutating a closed theory.
- A theory-relative failure never creates a global Compass nogood.
- No live world, fork, Bearing, candidate cursor, route suffix, or callable
  enters the ledger.

### Acceptance

- Preserve existing bootstrap, alarm preset, delayed watchdog, zero-net
  deadline, successive hazard, direct chart, detour, and occurrence-identity
  fixtures as behavioral oracles.
- Add neutral end-to-end fixtures for useful later-scan state, owner-controlled
  duration, prior setup, same-scan missing permissive, overwrite/reset, and
  incomplete evidence.
- Preserve the external pristine-scan `how(HeelStep == 81)` case only as an
  end-to-end acceptance boundary; repository fixtures remain generic.
- Exhaustion never repeats an identical semantic attempt and never upgrades an
  empirical failure into impossibility.
- Tumbler golden comparisons gate every stage that changes orientation order or
  public events.

## Non-negotiable exactness contracts

- The concrete execution oracle is always a real PLC fork and exact ordered
  projection, never an endpoint prediction alone.
- Failure explanation never silently swaps the selected producer or consumer.
- Occurrence requirements are satisfied at their exact demanding occurrence,
  not because an endpoint happens to look right.
- Compressed histories are indexes only; owner-bound exact projections validate
  every selected occurrence and deadline.
- Historical prevention selects the newest exact harmful writer and nearest
  usable checkpoint before it.
- Instruction algebra remains owned by `Crossings`, `AdvanceProfile`, and the
  instruction implementation.
- Avoid constraints apply to every read, action, and executed scan in their
  declared scope.
- An empirical failure, unresolved read, or exhausted budget is not proof of
  impossibility.
- Every repeated activity consumes a finite budget or records knowledge that
  prevents identical repetition.
- No future Bearing, action suffix, or predicted world survives an observation.

## Glossary

| Term                   | Plain meaning                                                                     |
| ---------------------- | --------------------------------------------------------------------------------- |
| Bearing                | One next direction recomputed from the current world                              |
| WorkingTheory          | The active technician job card for one stubborn local claim                       |
| TheoryClaim            | “This exact producer can make this value effective at this consumer and boundary” |
| TheoryVersion          | The local claim plus exact facts learned so far                                   |
| Attempt                | One exact physical experiment under one evidence version and source               |
| Rollback root          | The safe state to restore if the local explanation fails                          |
| Provisional state/tip  | Useful accepted setup that has not yet validated the local claim                  |
| Requirement            | A condition that must hold at one named occurrence or deadline                    |
| Effect expectation     | The producer-to-consumer promise attached to an attempted action                  |
| Intrascan finding      | What exact ordered reads and writes explain about one assertion scan              |
| Theory-local tombstone | Do not repeat this identical local experiment                                     |
| Global act nogood      | Independent evidence that a physical act is invalid in one executable world       |
| PilotRung              | Scoped temporary PILOT logic used to hold or prevent a condition                  |
| Checkpoint             | A retained executable state that may be safely restored                           |

## Completion criteria

The migration is complete when:

- `_pilot_loop_events` has one ordinary READ/TRY/KEEP repetition path;
- Compass/orientation remains the sole producer of one fresh next
  `Bearing | NeedProbe | Stuck`;
- program-owned continuation remains a `program_step`/instruction-owner
  reading, not theory or intrascan policy;
- intrascan alone owns exact within-scan pulse forensics;
- every Stage 5 interpretation reuses the already-run steer execution,
  including its shared ordered projection, verification outcome, accepted
  landing, and normal progress receipts;
- Stage 5 adds no PLC scan, fork, reorientation, duplicate projection build,
  or duplicate progress monitor;
- same-scan retry execution is bounded, executes once, and continues normal
  verification on the same fork;
- WorkingTheory alone owns local hypothesis lifecycle decisions without
  retaining executable futures;
- a thin resolver alone performs theory-authorized restore/adopt/promote world
  mutations;
- ordinary useful progress and autonomous continuation bypass theory and retry
  search;
- prior setup becomes a separate accepted phase followed by fresh orientation;
- exact positive/negative occurrences and deadlines determine local
  validation;
- progress, departure, investigation, and recovery report facts rather than
  running competing outer loops;
- trace, chart, program-step, and retry work cannot multiply into a quadratic
  or Cartesian execution search as `how(state)` gains candidates;
- all lint, pilot, Tumbler, and neutral acceptance fixtures pass.
