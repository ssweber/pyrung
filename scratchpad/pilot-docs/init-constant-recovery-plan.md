# Pilot failed-effect recovery plan

## Status

Grounded design proposal. This replaces the earlier idea of a separate startup
audit plus a separate initialization-constant planner with one failed-effect
loop that fits the current `pilot/` ownership boundaries.

The intended merge order is summarized separately in
[`phases.md`](phases.md); this document remains the detailed design authority.

The central rule is:

```text
read the current world
-> choose one steer
-> execute it on one fork
-> did the expected value appear?
     no  -> explain the selected writer: guard false / spent / not executable
     yes -> did that value reach its obliged consumer or terminal boundary?
              no  -> identify the exact overwriter and the values it read
              yes -> for a consumer obligation, did it observe the required shape?
                       no, effect not consumed -> identify the missed/late read
                       no, another read shifted -> identify the displaced read
                       yes -> ordinary verification and progress
                     for a terminal obligation -> ordinary boundary verification
-> turn the explanation into the next requirement
-> if its deadline is behind the current source, recover its causal checkpoint
   and re-execute only the repaired local transaction
   otherwise compose it into the next steer
-> repeat only when the attempt or knowledge changed
```

Passing the immediate boundary is only local success. A steer which appeared
to work may have established the cause of a departure several committed
decisions later. Its expectation and satisfying occurrence therefore remain
addressable in retained history. Later investigation may add a requirement to
that earlier steer, recover its source checkpoint, re-execute the repaired
local transaction, adopt that corrected landing as the new live tip, and then
return to ordinary current-world orientation. The invalidated future remains
evidence; it is never retained as an action prefix to execute.

The first program scan is the same shape. It is a program-owned, single-scan
coast from the retained pre-scan boundary. Pilot observes its landing and asks
whether target-relevant work appeared before that landing. It does not need a
separate startup-analysis engine.

An unwritten timer preset is therefore not special because it was classified
up front as an "initialization constant." It becomes a checkpoint-persistent
assignment when an exact failed-effect explanation proves that a writer read
the old preset before an already-passed deadline.

## Current behavior and reusable machinery

The current implementation already owns most of the required evidence and
checkpoint/correction mechanics:

| Concern | Current owner | What can be reused | Missing seam |
| --- | --- | --- | --- |
| Current-world read | `orientation.py`, `trace.py`, `options.py` | One recomputed `Bearing`; `TraceAction.writer_path`, requirement tree, and `operation_boundary` | The selected writer/effect, obliged consumer, and required read-shape are not carried as an executable expectation |
| Trial execution | `steer.py` | Fork-only `Pulse`, `BatchPulse`, coast, and exact scan window | Judgment is primarily endpoint/key based |
| Ordered scan truth | `causal/_rung_writes.py`, `PLC._replay_rung_write_projection_at()` | Exact reads and writes, occurrence ordinals, dynamic rung identity, same-scan definition links | No Pilot-level whole-shape effect receipt |
| Trial judgment | `verify.py`, `outcome.py` | Avoid gates, target-relative assessment, accepted-trial contract | A same-scan overwrite, missed consumer, or shifted consumer read-shape can collapse to `spin` or false success |
| Durable departure | `progress.py`, `departure.py` | Checkpoints, pending departure, investigation, rollback | Startup is stepped before any checkpoint or trial receipt exists |
| Causal correction | `investigate.py`, `investigation_replay.py`, `corrections.py` | Exact incidents, nested locally re-executed corrections, bounded composition | Entry is a committed excursion/departure, not any failed expected effect |
| Causal checkpoint recovery | `_RecoveryOrigin`, checkpoints, `retained.py` | Epoch-correct source worlds, rollback/adoption, and exact occurrence identity | Current `RetainedReplay`/prefix semantics conflate restoring an anchor with retaining future actions |
| Executable correction | `overlay.py::PilotRung` | Ordered guarded overlay, stable world-key identity, and persistence across recalculated scans | Generated scope can accidentally require the destination already equal its proposed value |
| Bounded composition | `recovery.py::compose_corrections` | Identity-safe nested correction transaction | Candidate identity does not yet include an expected writer and deadline |
| Instruction timing | `instruction/advance.py::AdvanceProfile` | Timer/counter completion relation and scan estimates | No inverse "keep this boundary false until occurrence X" synthesis |
| Search knowledge | `compass.py` | World-scoped knowledge and stable act identity | Failed attempts and proved nogoods are currently conflated |

`src/pyrung/core/analysis/init_constants.py` is a prover state-projection
optimization for program-written first-scan constants. It is not the right
home for externally supplied timer/counter values and should remain unchanged.

## Design decision: one investigation path

Expectation recovery is not a second
correction system beside the current unexpected-channel investigation.
Unexpected channel motion, an absent expected write, a same-scan overwrite,
and a delayed consequence traced to an earlier steer are different evidence
shapes for the same question:

> Why did this steer's expectation fail, and what additional requirement makes
> its obliged consumer observe the intended shape?

The intended common flow is:

```text
observe the steer expectation
    fulfilled -> ordinary progress
    ambiguous -> existing pending-departure evidence gathering
    violated  -> one investigation path
                   -> derive one nested requirement/correction
                   -> compose it into the implicated steer
                   -> recover its causal checkpoint when necessary
                   -> re-execute the repaired local transaction
                   -> judge the original expectation and continue from its landing
```

This common path must be validated against the current verification-time
excursion, post-commit departure, and checkpoint-recovery paths.
Implementation should not add a fourth orchestration loop and hope the
corrections later converge.

### Existing machinery which should grow a designation

These owners appear structurally suitable. The expected change is primarily a
broader input/output designation, not a duplicate mechanism:

| Existing machinery | Broader designation to validate |
| --- | --- |
| `BearingObjective` / `ActPolicy.heading` plus a selected-effect receipt | The global target, immediate boundary, and exact consumer-relative reason for this act |
| `_PulseState` / `_ExecutionEvidence` | The exact execution window in which an expectation is observed |
| `verify.py` gates | Classify a factual expectation observation before applying ordinary spin/dead-end judgment |
| `DepartureObservation` / `PendingDeparture` | The ambiguous arm when an observed channel movement may still be useful program motion |
| `DeviationIncident` | The common bounded evidence for any proved expectation violation, not only a committed channel regression |
| `investigate_deviation` / `investigate_excursion` | One expectation-investigation engine entered through evidence adapters |
| `CorrectionHypothesis` | A proposed additional requirement on the implicated steer |
| `compose_corrections` | The bounded transaction which nests requirements into that steer and re-executes the repaired local composite |
| `PilotRung` / correction receipts | Executable requirement lifetime and separately retained causal justification |
| `_RecoveryOrigin`, checkpoints, and `RetainedOccurrence` | The exact causal checkpoint before the occurrence which needed the missing requirement |
| Existing checkpoint rollback/adoption | Restore that source, re-execute only the repaired local transaction, and make its landing the new live tip; `RetainedReplay` should not remain a future-action navigation act |
| `CompassKnowledge` | Durable observations and attempt identity which survive rollback |
| `progress.py` | Decide whether a committed landing is progress, ambiguous departure, or a violated expectation; do not derive a second kind of correction |

The names do not all need to change. The important part is that their contracts
say which stage they own. For example, `departure.py` may remain specialized
channel classification while its regression result becomes one adapter into a
general expectation investigation.

### Expected disposition of `retained.py`

`retained.py` is an implementation namespace, not a design boundary. Much of
it implements the future-action behavior this plan now rejects:

- `replay_retained_prefix()` re-executes a historical suffix;
- `read_retained_replay()` independently searches current blockers and mints a
  `RetainedReplay` navigation act;
- `_RetainedCompositionCandidate`, `_merge_retained_bearings()`,
  `compose_retained_bearing()`, and `execute_retained_replay()` compose and
  execute those special acts; and
- `_occurrence_repeated()` judges the reproduced historical occurrence rather
  than the selected whole-shape obligation.

Those paths should be removed as causal checkpoint recovery lands. Useful
pieces—exact projection lookup, dynamic write addressing, writer-occurrence
resolution, and selection of the preceding retained checkpoint—should move to
their causal/checkpoint owners or remain in a much smaller evidence-only
module. Occurrence-scoped correction derivation should enter the common
investigation path instead of staying a retained-only candidate engine.

If all surviving helpers acquire clearer owners, `retained.py` may disappear
entirely. The implementation should not preserve the module by inventing a new
responsibility for it.

### Machinery which appears genuinely new

The current system does not yet have these contracts:

1. **A steer-owned expectation.** A typed statement of the effect, selected
   writer or producer, survival boundary, obliged consumer, and ordered
   read-shape that execution is meant to test. The obligation and shape come
   from the requirement tree used to select the bearing; they must not be
   reconstructed after execution. Composed steers may require a terminal
   obligation plus ordered intermediate obligations as one conjunctive act;
   alternatives remain separate Bearings.
2. **An expectation observation.** An occurrence-aware result distinguishing
   absent, appeared-then-overwritten, stranded, displaced, survived, and
   ambiguous. It must work for bootstrap, pulses, batches, coasts, and causal
   checkpoint recovery.
3. **An occurrence-targeted causal entry point.** Public recorded `cause()`
   currently explains the committed boundary transition for a tag/scan. Pilot
   also needs to ask about a selected transient write or overwriter by exact
   occurrence, and about the reason a selected writer occurrence was absent.
4. **A requirement with an occurrence deadline.** The logical condition,
   selected writer, source occurrence, `(scan, ordinal)` deadline, and temporal
   phase needed to decide compose-now versus causal-checkpoint recovery.
5. **A committed expectation receipt.** The link from a later causal chain back
   to the earlier steer, source world, and satisfying occurrence it implicated.
6. **Exact one-shot rearm evidence.** A receipt that a selected instruction's
   hidden `_oneshot` memory key was cleared by a false scan, even when the
   public world key did not move.
7. **Attempt evidence distinct from proof.** `AttemptReceipt` prevents an
   identical semantic repair; `NogoodProof` requires complete-domain evidence.
8. **A bootstrap-steer adapter.** Boundary `0 -> 1` must produce the same
   designation/execution/incident contracts without pretending to be one of
   the existing multi-scan coast modes.
9. **Instruction-boundary inversion.** Timer/counter ownership can state its
   completion relation today; recovery additionally needs to solve the
   constraint which keeps that relation false or true through an exact
   deadline.

These should be introduced as the narrow seams consumed by existing
investigation and checkpoint/correction machinery. They are not authorization
to create a parallel expectation planner.

### Settled design decisions

1. **The atomic unit is a producer-to-consumer obligation.** It records one
   selected effect, the consumer which makes that effect useful, and the
   consumer-relative read-shape. An act owns the obligation as a whole even
   when several inputs are applied together. Co-actions and holds are
   requirements, not separate expectations. A genuinely multi-handoff act may
   carry an ordered conjunctive tuple; alternatives remain separate Bearings.
2. **Bootstrap has designations, not promises.** Boundary 0 watches the target
   and conservative concrete program-written operation/channel handoffs on
   valid target traces. Each designation keeps its path and consumer identity.
   Steerable inputs, pure data/guard reads, and heuristic proposals are not
   initial watchlist members. Only a designated effect which actually appeared
   can become a bootstrap failed-effect incident. Keep this selection behind an
   adjustable `bootstrap_designations(trace)` policy.
3. **The consumer read is the normal survival boundary.** The producer must
   precede that read without an intervening displacement, and the selected
   shape must hold at its exact reads. A consumer may legitimately advance the
   value afterward. A terminal obligation with no consumer instead uses its
   declared act/scan/channel boundary.
4. **Selection and explanation are separate.** Trace/Orientation selects and
   passes the exact writer. Absence recovery explains that writer as
   guard-false, spent, not executable, or unknown; it never silently switches
   producers. A false guard becomes a narrower requirement for ordinary trace
   resolution. Another producer may be selected only by a later ordinary
   Orientation read.
5. **Required-shape policy is isolated from recorded facts.** A pure selected-
   path policy returns local enabling reads plus value-producing instruction
   reads. A projection query returns the exact observed reads. Call/caller
   identity belongs to the dynamic consumer occurrence rather than being
   flattened into every shape. Repeated reads remain occurrence-addressed, not
   a tag dictionary.
6. **Only exact obligation evidence proves a violation.** `ABSENT`,
   `OVERWRITTEN`, `STRANDED`, and `DISPLACED` require a selected/designated
   obligation and matching exact occurrences. Other unexpected movement stays
   ambiguous and follows `PendingDeparture`; incomplete identity is `UNKNOWN`,
   never an inferred regression.
7. **A deadline is the exact demanding read.** The requirement must already be
   observable at that occurrence. An earlier same-scan writer is timely; a
   later writer is not. Causal inversion may move the actionable deadline
   upstream—for example from an alarm's `Done=True` read to the timer operand
   reads which produced `Done=True`. Crossings may own generic relational
   inversion while `AdvanceProfile` owns instruction-specific temporal
   completion semantics; their exact interface can be settled in that phase.
8. **Requirements retain separate provenance but compile into one schedule.**
   Same-phase assignments compose simultaneously, release/assert phases stay
   ordered, compatible same-tag constraints intersect, and a stronger
   constraint may supersede a weaker executable value without deleting either
   receipt. An incompatible composition rejects only that exact repair.
9. **Compass navigates under active requirements.** Recovery derives them;
   adjustable scope policy reports `ACTIVE`, `DISCHARGED`, `INVALIDATED`, or
   `AMBIGUOUS`; Pilot state retains them; Compass/Orientation answers “given
   this world and these active requirements, what can Pilot do next?”; overlay
   applies them; verification proves they were preserved. Compass never
   weakens or silently retires them. Active requirement identities participate
   in world and attempt identity.
10. **Every scan is reread.** Requirements and their causal justification may
    persist, but predicted PLC worlds, ordinals, and future action suffixes do
    not. After every scan Pilot rebuilds the projection/world and recalculates.
    A changed shape ends the bounded attempt and produces new evidence.
11. **Past-deadline repair is causal checkpoint recovery, not retained future
    execution.** Match the later cause to an exact expectation receipt, recover
    its source checkpoint, re-execute only the repaired local transaction,
    adopt the corrected landing, and orient forward normally. Old operations
    remain evidence and are never reapplied. Matching uses epoch, occurrence,
    dynamic address, act/consumer identity, and source world and fails closed
    on ambiguity.
12. **Repair success has two stages.** `LOCALLY_REPAIRED` proves the original
    whole-shape obligation still worked and the new requirement was installed
    early enough. `DISCHARGED` is recorded later when its demanding occurrence
    actually observes the requirement. Same-scan cases may satisfy both at
    once. A future failure re-enters recovery with stronger evidence.
13. **Checkpoint retention follows obligations.** Always retain boundary 0,
    every committed expectation-bearing source boundary, and each scan
    boundary in an active requirement/coast corridor. Prune only when no active
    requirement, unresolved incident, or expectation receipt can refer back.
    Exact ordinals provide within-scan precision; no within-scan checkpoint is
    needed.
14. **Learning records the exact repair attempt.** Attempt identity includes
    checkpoint/world, selected obligation/writer, ordered phases, scopes,
    deadlines, and correction identities. A changed world, checkpoint,
    requirement, phase, or producer is a different attempt. Only a recorded
    complete-domain proof creates a nogood.

## What the two fixtures prove today

The fixtures in `tests/fixtures/pilot_alarm_presets/` already exercise the
right execution truth.

### Destructive startup scan

The simulator calls the initial state `scan_id == 0`; executing the first
program scan commits `scan_id == 1`. The exact access projection for that
`0 -> 1` scan contains, in order:

```text
write FirstScanProcessStep 0 -> 10
read  FirstScanProcessStep = 10
write FirstScanProcessStep 10 -> 40
read  FirstScanProcessStep = 40
write FirstScanProcessStep 40 -> 80
read  FirstScanWatchdog_Acc = 0
read  FirstScanWatchdogPresetMs = 0
write FirstScanWatchdog_Done False -> True
write FirstScanWatchdog_Acc 0 -> 10
read  FirstScanWatchdog_Done = True
write FirstScanProcessStep 80 -> 90
```

So no new recorder is needed. The target value appeared, its exact overwriter
is known, and the overwriter's `Done=True` read links to the timer occurrence
which read `Preset=0`.

Current Pilot does retain the pre-scan state in public history and
`retained.py` finds the relevant occurrences. It even proposes
`FirstScanWatchdogPresetMs=1`. That re-execution is correctly rejected because the
timer accumulates 10 ms during the scan, so `1` is still already expired and
the same occurrence repeats. The missing deduction is:

```text
FirstScanWatchdogPresetMs > FirstScanWatchdog_Acc at the overwrite deadline
FirstScanWatchdogPresetMs > 10 ms in this observed scan
```

### Alarmed steer

With `Reset=True` and `AtTarget=True`, the exact action scan contains:

```text
write ProcessStep 91 -> 40
write ProcessStep 40 -> 80
read  WatchdogPresetMs = 0
write Watchdog_Done False -> True
read  Watchdog_Done = True
write ProcessStep 80 -> 91
```

Current Pilot reduces the endpoint back at `91` to a rejected spin and learns
ordinary action nogoods. The failed fork's exact evidence is then not available
as a next requirement.

The failed fork is disposable, so its Reset one-shot spentness does not by
itself require a release/reassert sequence: a corrected retry can start from
the pre-steer source checkpoint where that writer is still armed. Release/reassert is
required only when the selected one-shot is already spent in the source
checkpoint world, or when recovery deliberately continues from the
failed landing.
The acceptance tests must cover those as separate cases.

## Unified observation contract

### Expected obligation

Every executable bearing should carry the exact reason its act was selected.
The atomic shape is:

```text
EffectObligation
    tag, value
    selected static writer path, when known
    immediate operation/channel boundary
    obliged consumer identity and read locus
    required read-shape = ordered (tag, value, consumer read locus) entries
    source world key and source scan

EffectExpectation
    ordered obligations               # usually exactly one
```

The expectation belongs to the act as a whole. A `BatchPulse` applying Reset,
AtTarget, and a preset correction may still have one obligation: make the
selected writer produce `Step = COMPLETE` for its consumer. Co-actions and
holds remain requirements. When a selected program path genuinely promises
several observable handoffs, the expectation carries them as an ordered
conjunction; alternatives are separate Bearings.

For an ordinary trace action, this is minted from the selected `TraceAction`
together with its exact requirement-tree path, not by reconstructing provenance
strings in `verify.py`. `ActPolicy.heading` continues to describe the channel
boundary; the expectation describes the specific value/writer whose execution
is being tested. The consumer and its required read-shape come from the
requirement tree which made that effect worth producing. Extend `TraceAction`
or add a companion selection receipt so that information is retained when the
bearing is selected, rather than inferred from the landing after the fact.

### The concrete handoff gap today

This rationale exists during the current read, but is not passed as the
selected act's contract:

1. `trace.py::TraceNode` owns the parent/child requirement chain which explains
   why a leaf action is needed. `TraceAction` retains only parts of that path,
   including `writer_path` and `operation_boundary`.
2. `options.py::_candidate_for()` looks up the rich `TraceAction`, then lowers
   it into `_Candidate`. That type keeps action, provenance, ranking, and an
   optional channel boundary, but no selected consumer obligation or required
   read-shape.
3. `orientation.py::_pulse_policy()` lowers `_Candidate` again into
   `ActPolicy`. The resulting executable policy therefore cannot state why its
   candidate was needed.
4. `_bearing()` carries `BearingObjective(target, frontier)`. That is useful
   global target-relative context, but it does not identify the selected path
   from this action to its particular consumer.

`Bearing.orientation` still retains the broad `OrientationRead` for diagnostics,
but making execution search that whole reading after the fact would be
ambiguous when the same action appears on several paths. The expectation must
be minted at candidate selection, while the selected trace/route/learned edge
is known, and carried explicitly through `ActPolicy` or `Bearing`. In other
words, Compass/Orientation should pass along not only “try `Step = 2`,” but
“try `Step = 2` because this exact consumer must observe this exact shape.”

Trace candidates obtain that obligation from the selected requirement-tree
path. Route-, program-, awaited-, and learned candidates need an equivalent
typed obligation from their own selected edge/read receipt; a rationale string
is not executable evidence.

The obligation is ordinal-relative. For example, producing `Step = 2` is not
enough when the reason for producing it is that rung 22 must observe
`Step == 2` while `Latch == True`. The expectation therefore records rung 22's
exact read as the deadline and records the latch read as part of the shape.

For the startup coast there is no selected act and therefore no promise that
any particular value must appear. A bootstrap designation watchlist contains
the target plus conservative concrete program-written operation/channel
handoffs on valid target traces. Each designation retains its path and
consumer. Only a designated effect which actually appears can be observed as
failed; absent watchlist members are not `ABSENT` expectations.

When an expectation survives and the steer commits, retain an expectation
receipt with the committed operation:

```text
ExpectationReceipt
    source checkpoint and world key
    physical act and active PilotRungs
    obligations and selected writers
    exact producer and consumer occurrences which satisfied them
    local-repair/discharge status and boundary
```

This makes the historical decision causally addressable if a later departure
traces back through it. Exact epoch, dynamic address, act/consumer identity,
occurrence, and source world must select one receipt and fail closed on
ambiguity. The receipt points to a checkpoint; it never promises to execute a
future action suffix.

### Effect observation

After execution, before generic spin/dead-end rejection, inspect the exact
projection for the execution window and return one structured result:

```text
EffectObservation
    ABSENT
        selected writer did not produce the expected value
        explanation = GUARD_FALSE | SPENT | NOT_EXECUTABLE | UNKNOWN

    OVERWRITTEN
        exact expected write occurrence
        exact displacement before the obliged consumer or terminal boundary
        values read by the overwriter before its write

    STRANDED
        exact expected write occurred
        obliged consumer did not fire, or did not observe the expected value
        exact consumer reads/occurrences observed instead

    DISPLACED
        exact expected write occurred and reached its consumer
        obliged consumer fired and observed the expected effect
        a required shape read observed a different value
        exact read and the write occurrence which displaced it

    SURVIVED
        exact expected write occurrence, if one was needed
        no displacement before the obliged consumer
        obliged consumer observed it by its read deadline
        every required shape read observed its required value
        terminal/no-consumer value present at its declared boundary
```

This observation is factual. It does not select a correction, commit a world,
or create a nogood.

`SURVIVED` is deliberately a consumer-relative whole-shape verdict, not an
endpoint-value verdict. A value which arrived after its consumer is
`STRANDED`. A consumer which saw the expected value after another required
input had shifted is `DISPLACED`. After correct consumption, the consumer may
legitimately advance the value again; only a terminal obligation requires the
value to remain at its declared landing boundary.

The ordered query should live next to
`ScanRungWriteProjection` in `causal/_rung_writes.py` because that object
already owns:

- all exact writes ordered by occurrence ordinal;
- all exact reads ordered by occurrence ordinal;
- `reads_observed_by_write()`;
- `transition_observed_by_read()`; and
- dynamic rung and instruction identity.

Pilot should consume a small query API from that owner instead of repeating
ordinal scans in `steer.py`, `verify.py`, and checkpoint recovery.

Overwrite and consumer-shape checks are the same ordered-projection query in
opposite directions:

- overwrite: after the expected write and before its boundary, find another
  write to the effect tag;
- consumption/nullification: before or at the obliged consumer read, prove the
  expected effect and every required shape value were the values observed.

Both queries must identify exact occurrences and fail closed when dynamic
identity is ambiguous.

The adjustable definition of “whole shape” is isolated from factual capture:

```text
required_shape(selected path)
    -> ordered local enabling reads plus value-producing instruction reads

observed_shape(exact consumer occurrence)
    -> exact occurrence-addressed reads from the projection
```

The policy does not include every read in the rung or scan. Call/caller
availability belongs to the dynamic consumer identity and explains a missing
consumer. The same tag may appear several times in the shape; reads must not be
flattened into a tag dictionary.

Only a selected or bootstrap-designated obligation plus matching exact
occurrences can produce `ABSENT`, `OVERWRITTEN`, `STRANDED`, or `DISPLACED`.
Other unexpected movement remains ambiguous and follows `PendingDeparture`.
Incomplete identity is `UNKNOWN`, not a guessed regression.

### Explaining absence

Absence is relative to the selected writer occurrence, not to the destination
tag globally. Trace/Orientation already chose that writer; the ordered
projection supplies execution facts; a thin failed-effect explainer combines
the two receipts. It answers “why did this selected producer not write?” and
never silently changes the producer. A different producer can appear only in a
later ordinary Orientation read after the explanation changed knowledge.

1. **Guard false.** The selected dynamic rung/instruction did not reach its
   write because an observed guard term was false. That term becomes the next
   narrower requirement. The ordinary trace reader may recursively turn that
   guard requirement—not the original destination search—into one steer or a
   coordinated steer.
2. **Spent.** The selected rung was powered, the instruction is one-shot, and
   its exact memory key was already true at entry. The next requirement is a
   false scan of the owning rung followed by a later assertion. The memory key
   is `instruction.memory_key("_oneshot")`; spentness must be read from the
   source checkpoint state for that exact instruction, never inferred from the
   destination tag.
3. **Not executable.** No matching dynamic occurrence can execute from this
   checkpoint/world because its caller, return path, first-scan condition, or
   instruction-owned boundary is unavailable. This produces an earlier-
   checkpoint requirement or an unresolved frontier, not an empirical nogood.
4. **Unknown.** Incomplete capture or ambiguous dynamic identity remains a
   named frontier and may be probed. It is not silently upgraded to
   impossibility.

## Requirements and deadlines

An explanation feeds the next read as a requirement. A requirement needs both
logical content and an occurrence deadline:

```text
Requirement
    condition or exact value
    source occurrence which demanded it
    deadline = (scan, occurrence ordinal)
    selected writer identity
    phase = steady | release | assert
```

The occurrence ordinal matters. In both fixtures, the desired state and its
overwriter are in the same scan; a scan number alone cannot say whether a
correction was early enough.

For `STRANDED` and `DISPLACED`, the deadline is the obliged consumer's exact
read occurrence. If a required latch or other shape value can be established
at a lower ordinal, it may be composed into this scan. If its writer would run
after the consumer read, it is too late. This expresses “latched before getting
there, or at the same time” without inventing a separate temporal planner: the
value must already be observable at the exact read.

For an overwrite, the overwriter occurrence first demands the condition which
would prevent it. Following a same-scan definition may move the actionable
deadline further upstream. For example, `Done == False` at an alarm read
becomes `Acc < Preset` at the timer operand reads which produced `Done=True`.
Changing the preset after that evaluation cannot claim to have prevented it.

Resolution is deliberately small:

```text
the executable source is still before the requirement deadline
    -> add it to the next single or composed steer

the live tip is past the deadline and no current source remains before it
    -> match the exact expectation receipt
    -> select its latest retained checkpoint before all required occurrences
    -> install the correction there
    -> re-execute only the repaired local transaction
    -> adopt that landing as the new live tip
    -> run ordinary Orientation from the newly read PLC world
```

Requirements keep separate causal provenance even when compiled into one
execution schedule. Same-phase assignments compose simultaneously;
release/assert requirements remain ordered phases; compatible same-tag
constraints intersect; and a stronger constraint may supersede a weaker
executable value without deleting either receipt. An incompatible schedule
rejects only that exact repair. A `BatchPulse` acts as one artifact: several
applied values do not imply several expectations.

`recovery.py::compose_corrections` should remain the transaction owner. Extend
its candidate identity to include:

- source world key and causal checkpoint;
- obligations and selected writers/consumers;
- ordered release/assert phases;
- simultaneous assignments in each phase;
- active `PilotRung` identities; and
- requirement deadlines and scopes.

An identical attempt from the same checkpoint/world is recorded once and never
executed twice.

### Active requirement ownership and lifetime

Requirement lifetime is an adjustable policy seam:

```text
scope_status(requirement, current world, observed occurrences)
    -> ACTIVE | DISCHARGED | INVALIDATED | AMBIGUOUS
```

A requirement normally remains active until its exact obligation is
discharged, its owning operation definitively closes, or the final target is
reached. Removal produces a receipt; a different-looking scan does not silently
retire it.

Recovery derives the condition and deadline. Pilot state/checkpoint recovery
retains it. Compass/Orientation owns the question:

> Given this world and these active requirements, what can Pilot do next?

Overlay applies executable assignments and verification proves each newly
selected Bearing preserved all active requirements. Compass does not invent,
weaken, retire, or physically apply them. A requirement conflict makes the
current repair `INVALIDATED` or ambiguous; it is not silently discarded to
admit another candidate.

Every executed scan produces a fresh projection and current-world read.
Requirements may persist, but predicted PLC worlds, future ordinals, and action
suffixes do not. A multi-phase local transaction proves one phase, rereads the
world, and proceeds only if the next phase is still valid. A changed causal
shape ends that attempt and returns evidence to ordinary calculation.

Repair acceptance has two stages:

```text
LOCALLY_REPAIRED
    original whole-shape obligation still succeeded
    new requirement was installed early enough
    no avoid or correction self-defeat occurred

DISCHARGED
    the exact future demanding occurrence arrived
    and observed the required condition/shape
```

Same-scan repairs may establish both at once. A delayed requirement remains
active after local repair while Pilot orients forward from the corrected
landing; a later failure re-enters the same loop with stronger evidence.

## Overwrite recovery

When the value appeared but did not survive:

1. Find the first later write to the same destination after the expected
   occurrence and before the obliged consumer or terminal boundary.
2. Read only the conditions and data values actually observed by that exact
   overwriter.
3. Follow same-scan definitions with
   `transition_observed_by_read()` before falling back to an earlier boundary.
4. Ask existing correction derivation for a minimal way to falsify or delay the
   overwriter. Do not flatten every read into a hold.
5. Attach the overwriter occurrence as the requirement deadline.
6. Re-execute the repaired local transaction and judge the original obligation
   at its consumer or terminal boundary.

This is the common entry into `corrections.py` and `investigate.py`. Committed
channel departures still use `progress.py`; failed trial effects and startup
landings should build the same `DeviationIncident`/local re-execution evidence
without first pretending the endpoint was useful progress.

## Stranded and displaced recovery

When the expected value survived but its purpose did not, run the same causal
path relative to the obliged consumer:

1. If the consumer did not fire or read the old effect value, report
   `STRANDED` with the exact reads it made, or the exact guard which prevented
   its occurrence.
2. If the consumer observed the effect but another required read no longer
   matched the required shape, report `DISPLACED` with that read and the exact
   write which moved it.
3. Turn the missing guard/value into a requirement whose deadline is the
   consumer read occurrence.
4. Compose it now when its selected writer can occur before that deadline;
   otherwise recover the exact source checkpoint before the earliest required
   occurrence and re-execute the repaired local transaction.
5. Judge the original whole-shape expectation again. Matching the endpoint
   alone cannot verify the correction.

This is not a second investigation engine. It is the overwrite query run from
the consumer side of the same ordered access projection, producing the same
condition-plus-deadline input to `corrections.py` and `investigate.py`.

## Delayed consequences across committed steers

A locally successful steer can create a delayed hazard:

```text
steer 3: Start=True -> Running appears and survives
...
steer 8: coast -> Alarm
cause(Alarm) -> Watchdog.Done -> timer enabled during steer 3
```

`Start=True` worked and must not become a nogood. The later causal chain says
that steer 3 was incomplete: it also needed the timer disabled, a safe preset,
or another condition established before its deadline.

Recovery origin is therefore selected by the exact causal occurrence, not by
the most recent steer or checkpoint:

1. `progress.py` detects the later departure or loss of target-relative work.
2. Recorded `cause()` crosses committed operation boundaries and identifies
   the earlier implicated occurrence.
3. The matching expectation receipt identifies the decision and source world
   which established it.
4. `investigate.py` derives one additional condition/deadline requirement.
5. Checkpoint recovery restores the latest retained checkpoint before that
   occurrence and re-executes only the repaired implicated steer/transaction.
6. If the original whole-shape obligation still succeeds and the new
   requirement is installed early enough, the result is `LOCALLY_REPAIRED`.
7. Pilot adopts that landing, discards the invalidated future as executable
   work, and orients normally from the newly read PLC world.
8. The requirement remains active until its real demanding occurrence safely
   discharges it, or adjustable scope policy closes it.

The correction may change an old steer without denying its original useful
effect. Local repair must preserve the target-relative work for which the steer
was accepted; a future deadline is not declared neutralized until it is
`DISCHARGED`. Existing investigation distinctions such as neutralization
versus masking, continuation evidence, and correction self-defeat remain
authoritative. Old physical operations are historical evidence only and are
never reapplied as a retained prefix.

## Timer and counter deadline synthesis

Timer/counter parameters should be solved only when an exact failed-effect
chain reaches them.

For the watchdog fixtures:

```text
required at overwriter read: Watchdog.Done == False
owner completion relation:   Watchdog.Acc >= WatchdogPresetMs
observed Acc at deadline:     10 ms
derived requirement:          WatchdogPresetMs > 10 ms
```

Use the instruction owner's `AdvanceProfile.completion_boundary` and exact
observed execution, not a generic `0 -> 1` numeric widening. Crossings may own
generic relational inversion while `AdvanceProfile` supplies the
instruction-specific temporal/completion relation; settle that narrow interface
in this phase. Choose the smallest type-valid value satisfying the strict
relation, or a conservative larger value when unit quantization requires it.
Re-execution and later discharge are the oracle.

If a later scan reaches the deadline with a larger accumulator, strengthen the
same constraint monotonically and create a new exact repair attempt from the
causal checkpoint. If several consumers constrain one tag, intersect the
constraints. If the intersection is empty only a complete bounded domain may
prove that fact.

A program-unwritten parameter (whether explicitly external or merely inferred
as steerable) is installed as a checkpoint-persistent `PilotRung` for the active
requirement corridor. Its guard must be an outer unresolved-target or
incident-corridor condition. It must not contain a self-demand such as
`Preset == proposed_value`, because that prevents the overlay from
establishing the proposed value on its first scan.

The assignment remains unchanged for that exact repair attempt and is included
in every nested correction derived from it. A stronger constraint creates a
new attempt identity; it is not a mid-execution mutation.

## One-shot recovery

One-shot readiness is another requirement source, not a separate recovery
planner.

For the exact selected instruction:

```text
entry memory[_oneshot:<instruction state key>] is false
    -> writer is armed

entry memory[...] is true and rung guard is true
    -> writer is spent
    -> require one scan with the owning guard false
    -> then require the original assertion
```

The false scan is observable progress for this recovery attempt even if no
public tag or world key changes. Verification therefore needs a rearm receipt
for the exact memory key; it must not reject the release as `spin`.

If the failed write occurred only on a disposable trial fork, prefer retrying
from that trial's source where the instruction was armed. Do not manufacture a
release step. If the source world itself contains the spent memory state, a
bounded composed steer may perform:

```text
phase 1: Reset = false             # one scan; prove exact writer rearmed
phase 2: Reset = true
         AtTarget = true           # simultaneous assertion requirements
         WatchdogPresetMs > 10     # persistent active requirement
observe: obliged consumer sees the COMPLETE shape
```

After phase 1 Pilot rereads the PLC and proceeds only if phase 2 remains valid.
The corrected landing returns to ordinary Orientation; no future action suffix
is retained.

## Startup as a one-scan coast

`pilot.py::_pilot_loop_events` currently calls `state.work.step()` at
`scan_id == 0` before emitting `started`, before creating a checkpoint, and
without a trial or progress receipt. Replace that hidden settle with an
observable bootstrap transaction:

1. Capture the pre-scan `_World` and target objective at boundary 0.
   Retain boundary 0 as a causal checkpoint.
2. Execute exactly one normal program scan with no operator action.
3. Build the exact access projection for boundary `0 -> 1`.
4. Build the conservative designation watchlist: the target plus concrete
   program-written operation/channel handoffs on valid target traces, each with
   its path and consumer identity.
5. Observe only designated effects which actually appeared using the same
   consumer-relative whole-shape contract. Missing watchlist members are not
   `ABSENT` promises.
6. Keep the committed landing as the ordinary current world when nothing
   useful appeared.
7. If the target remains unresolved and a designated effect was overwritten
   before consumption, stranded, or displaced, build an incident anchored at
   boundary 0 and run the ordinary bounded correction/checkpoint path.

This is semantically a one-scan coast, but it should not call the current
multi-scan bearing/terminal coast implementations: those own seek/settle
budgets and different stop conditions. Construct the same execution and
incident receipts directly, then hand them to the shared observation and
recovery owners.

An exact startup obligation violation is already known regression and can be
investigated immediately. A designated value which was correctly consumed and
then legitimately advanced is not regression. `PendingDeparture` remains
useful when a committed landing's target-relative meaning is genuinely
unsettled; its search-scan expiry should not be overloaded as the occurrence
deadline above.

## Attempts are not nogoods

Current behavior records `ActionNogoodObservation` for ordinary rejected
trials, and `_transition_once` also records an act-identity nogood whenever
`attempt.trial is None`. That is too strong for this loop.

Split the concepts:

```text
AttemptReceipt
    this exact checkpoint/world, obligation/writer, ordered phases,
    requirement scopes/deadlines, and corrections need not repeat

NogoodProof
    every member of a complete relevant domain has been excluded
```

A failed steer adds an `EffectObservation`, a requirement, a stronger
constraint, an earlier causal checkpoint, or an `AttemptReceipt`. It does not
by itself add a nogood.

The same physical action remains eligible when its world, checkpoint,
requirement strength, phase schedule, or selected producer/consumer changes;
that is a different semantic repair attempt.

A true nogood may be created only when completeness is owned and recorded, for
example:

- Bool domain exhausted;
- declared `choices=` exhausted;
- prover `nd_domains` is complete for every varied input;
- `tide_tables.guard_verdict` returns `GUARD_DEAD` from a complete finite
  domain; or
- a composed Cartesian domain is explicitly finite and every member has a
  proof-bearing rejection.

User `avoid=` remains an explicit constraint, not an empirical nogood.
Budget/probe exhaustion may return `Stuck`/`STOPPED` with the remaining
frontier. It must not report that the route is impossible.

## Grounded implementation sequence

### Phase 1: Make scan 0 observable and pin execution truth

- Preserve boundary 0 as a causal checkpoint before the hidden settle step.
- Execute exactly one program-owned scan and retain its execution receipt and
  ordered projection without changing the current landing behavior.
- Promote the two alarm-preset fixtures from script assertions into focused
  tests; pin the exact `0 -> 1` projection and alarmed action scan.
- Add the committed-spent and conditional-negative fixtures as separate
  evidence cases.

### Phase 2: Designate target-relevant bootstrap work

- Read conservative target/bootstrap designations from the pre-scan trace:
  target plus concrete program-written operation/channel handoffs, with their
  path and consumer identity.
- Intersect only effects which appeared with the scan projection; do not treat
  absent designations as failed promises.
- Report exact overwritten-before-consumption, stranded, and displaced
  bootstrap effects as known violations; leave unrelated or ambiguous motion
  with ordinary progress/`PendingDeparture`.

### Phase 3: Carry selected obligations through ordinary steers

- Add `EffectObligation` and act-owned `EffectExpectation`; carry the selected
  writer, consumer, shape, and boundary unchanged through `_Candidate`,
  `ActPolicy`/`Bearing`, execution, and recording.
- Keep batches whole: applied co-actions become requirements, not independent
  expectations, unless the selected path has genuinely ordered handoffs.
- Add isolated `required_shape()` policy and factual `observed_shape()` plus
  ordered appeared/consumer-survival queries beside
  `ScanRungWriteProjection`.
- Have `steer.py` observe `ABSENT`, `OVERWRITTEN`, `STRANDED`, `DISPLACED`, or
  whole-shape `SURVIVED` before generic spin/dead-end verification.
- Prove with focused tests that no lowering seam loses or silently replaces the
  selected producer-to-consumer obligation.

### Phase 4: Explain failures and establish active requirements

- Explain only the selected absent writer; route a false guard back as a
  narrower ordinary trace requirement without selecting another producer.
- Represent conditions with exact demanding occurrences, deadlines, phases,
  and adjustable lifetime scope.
- Store expectation receipts and match later causes to their exact source
  checkpoints, failing closed on ambiguity.
- Retain obligation-driven checkpoints and expose active requirements to
  Compass/Orientation as admissibility constraints.
- Emit exact violation, requirement, deadline, scope, checkpoint, and
  `LOCALLY_REPAIRED`/`DISCHARGED` events.

### Phase 5: Compose and recover from causal checkpoints

- Compile separately justified requirements into simultaneous and ordered
  phases using the existing bounded `compose_corrections` owner.
- When a deadline is past, restore the exact causal checkpoint, execute only
  the repaired local transaction, adopt its landing, and return immediately to
  ordinary Orientation. Do not create or execute a retained action prefix.
- Retire `RetainedReplay`, `replay_retained_prefix()`, retained-Bearing
  composition/execution, and current-blocker-driven `read_retained_replay()`;
  move only exact occurrence/checkpoint evidence into its natural owners.
- Recalculate projection, world, scope, and admissible next work after every
  scan/phase.
- Invert timer/counter completion through Crossings/`AdvanceProfile`, install
  a self-demand-free persistent requirement, and strengthen it only as a new
  exact attempt from observed evidence.
- Reach both alarm-preset targets through the common local-repair and later-
  discharge contract.

### Phase 6: Complete and harden recovery semantics

- Carry exact one-shot identity, prove false-scan rearm, and preserve ordered
  release/assert phases with a reread between scans.
- Keep delayed requirements active while Compass calculates forward from the
  corrected landing; discharge them only at their real demanding occurrences.
- Split exact `AttemptReceipt` identity from complete-domain `NogoodProof` and
  remove unconditional empirical nogood creation.
- Harden scope invalidation, ambiguity, masking/self-defeat, incompatible
  requirements, checkpoint pruning, budget exits, and reporting.

## Acceptance scenarios

### Destructive startup scan

- Pilot observes `AT_TARGET` at its exact within-scan write.
- It identifies the later `ABORTED` writer and its `Done=True` read.
- It follows that read to the timer occurrence and derives
  `PresetMs > 10 ms`, not merely `PresetMs != 0`.
- It restores checkpoint 0, installs one stable preset requirement, and
  re-executes only the corrected scan 1 transaction.
- The designated `AT_TARGET` obligation is consumed with its required shape;
  the corrected landing becomes the new live tip.

### Disposable failed alarm recovery

- Pilot tries the selected Reset/AtTarget effect on a fork.
- It observes `COMPLETE` appearing and being overwritten by the watchdog.
- It retries from the pre-steer source checkpoint with the preset requirement
  composed into the same whole Reset/AtTarget act.
- It does not add an unnecessary Reset release, because that source checkpoint's
  one-shot remains armed.
- The obliged consumer observes the `COMPLETE` shape and the plan is reachable.

### Committed spent one-shot

Given a PLC handed to `how()` after the failed Reset scan has actually
committed:

- Pilot reads the exact Reset copy instruction as guard-true and spent.
- It executes and proves one false rearm scan.
- It then asserts Reset with AtTarget while retaining the preset correction.
- The release and assertion remain ordered and are not flattened into one
  unordered set.

### Conditional negative observation

Given `~Timer.Done` contributing to a write while `Done` has never been true:

- the exact `Done=False` read is consequential because a writer observed it;
- changing the preset after that occurrence does not claim to repair the
  retained consequence;
- a prevention attempt restores the causal checkpoint before that read and
  re-executes only the repaired local transaction; and
- a current-state recovery separately clears any committed consequence.

### Delayed consequence from an earlier successful steer

Given a steer which reaches and preserves its immediate boundary but enables a
timer or other delayed producer that causes a later departure:

- the original steer remains locally successful and is not made a nogood;
- the later departure's recorded cause crosses back to the exact earlier
  occurrence and expectation receipt;
- Pilot restores the receipt's source checkpoint and adds the newly discovered
  requirement to that act;
- local re-execution preserves the original useful whole-shape effect and
  installs the requirement early enough;
- the old future remains evidence and is never executed as a prefix;
- ordinary orientation resumes from the corrected landing; and
- the requirement becomes `DISCHARGED` only when its real later consumer safely
  observes it.

### Surviving value with a nullified consumer shape

Given a selected writer which produces `Step = 2`, while a downstream consumer
requires both `Step == 2` and `Latch == True` at its exact occurrence:

- the expectation retains that consumer and both required reads from the
  requirement tree;
- `Step = 2` at the scan landing is neither necessary nor sufficient: it must
  reach the consumer, which may then legitimately advance it;
- if the consumer misses `Step = 2`, Pilot reports `STRANDED` with the actual
  consumer read;
- if the consumer observes `Step = 2` but observes `Latch == False`, Pilot
  reports `DISPLACED` and the exact write which moved the latch;
- the derived `Latch == True` requirement is due by the consumer's read
  ordinal; and
- local repair succeeds only when the full consumer-relative shape is restored.

### Exhausted search

- no exact semantic repair repeats from the same checkpoint/world;
- failed attempts add explanation or named unresolved evidence, not empirical
  nogoods;
- only a complete-domain proof emits a nogood; and
- finite budget exhaustion terminates with an actionable `Stuck` reason and
  does not claim global impossibility.

## Non-goals

- A second planner dedicated to initialization.
- Reusing the prover's init-constant projection as a steering model.
- Treating every external numeric tag as a configuration constant.
- Treating every timer read as consequential; an exact consumer/deadline is
  required.
- Recovering from program startup when the latest pre-deadline checkpoint is
  enough.
- Retaining a future route suffix across observations.
- Reapplying historical actions after causal checkpoint recovery.
- Inferring one-shot spentness from a tag rather than an exact instruction
  memory key.
- Turning a failed empirical trial into an impossibility claim.

## Completion criteria

This work is complete when both alarm-preset fixtures reach their targets
through public Pilot behavior, the committed-spent variant performs the
ordered rearm sequence, the event stream names each expected writer,
overwriter, obliged consumer, displaced read, read requirement, deadline, and
causal checkpoint, `SURVIVED` proves the full consumer-relative shape,
`LOCALLY_REPAIRED` and `DISCHARGED` remain distinct when the deadline is later,
and no rejected attempt becomes a nogood without recorded complete-domain
evidence.
