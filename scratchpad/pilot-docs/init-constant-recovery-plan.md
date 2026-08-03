# Pilot failed-effect recovery plan

## Status

Grounded design proposal. This replaces the earlier idea of a separate startup
audit plus a separate initialization-constant planner with one failed-effect
loop that fits the current `pilot/` ownership boundaries.

The central rule is:

```text
read the current world
-> choose one steer
-> execute it on one fork
-> did the expected value appear?
     no  -> explain the selected writer: guard false / spent / not executable
     yes -> did that value survive to the observed boundary?
              no  -> identify the exact overwriter and the values it read
              yes -> ordinary verification and progress
-> turn the explanation into the next requirement
-> if its deadline is behind the current anchor, re-anchor and replay
   otherwise compose it into the next steer
-> repeat only when the attempt or knowledge changed
```

The first program scan is the same shape. It is a program-owned, single-scan
coast from the retained pre-scan boundary. Pilot observes its landing and asks
whether target-relevant work appeared before that landing. It does not need a
separate startup-analysis engine.

An unwritten timer preset is therefore not special because it was classified
up front as an "initialization constant." It becomes an anchor-persistent
assignment when an exact failed-effect explanation proves that a writer read
the old preset before an already-passed deadline.

## Current behavior and reusable machinery

The current implementation already owns most of the required evidence and
replay mechanics:

| Concern | Current owner | What can be reused | Missing seam |
| --- | --- | --- | --- |
| Current-world read | `orientation.py`, `trace.py`, `options.py` | One recomputed `Bearing`; `TraceAction.writer_path` and `operation_boundary` | The selected writer/effect is not carried as an executable expectation |
| Trial execution | `steer.py` | Fork-only `Pulse`, `BatchPulse`, coast, and exact scan window | Judgment is primarily endpoint/key based |
| Ordered scan truth | `causal/_rung_writes.py`, `PLC._replay_rung_write_projection_at()` | Exact reads and writes, occurrence ordinals, dynamic rung identity, same-scan definition links | No Pilot-level "appeared / overwritten / absent" receipt |
| Trial judgment | `verify.py`, `outcome.py` | Avoid gates, target-relative assessment, accepted-trial contract | A same-scan write followed by overwrite can collapse to `spin` |
| Durable departure | `progress.py`, `departure.py` | Checkpoints, pending departure, investigation, rollback | Startup is stepped before any checkpoint or trial receipt exists |
| Causal correction | `investigate.py`, `investigation_replay.py`, `corrections.py` | Exact incidents, nested replay-tested corrections, bounded composition | Entry is a committed excursion/departure, not any failed expected effect |
| Historical replay | `retained.py` | Epoch-correct prefix replay and exact occurrence identity | It independently searches current blockers instead of consuming a failed-effect deadline |
| Executable correction | `overlay.py::PilotRung` | Ordered guarded overlay, stable world-key identity, replay persistence | Generated scope can accidentally require the destination already equal its proposed value |
| Bounded composition | `recovery.py::compose_corrections` | Identity-safe nested correction transaction | Candidate identity does not yet include an expected writer and deadline |
| Instruction timing | `instruction/advance.py::AdvanceProfile` | Timer/counter completion relation and scan estimates | No inverse "keep this boundary false until occurrence X" synthesis |
| Search knowledge | `compass.py` | World-scoped knowledge and stable act identity | Failed attempts and proved nogoods are currently conflated |

`src/pyrung/core/analysis/init_constants.py` is a prover state-projection
optimization for program-written first-scan constants. It is not the right
home for externally supplied timer/counter values and should remain unchanged.

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
`FirstScanWatchdogPresetMs=1`. That replay is correctly rejected because the
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
itself require a release/reassert sequence: a corrected retry can replay from
the pre-steer anchor where that writer is still armed. Release/reassert is
required only when the selected one-shot is already spent in the retained
source world, or when recovery deliberately continues from the failed landing.
The acceptance tests must cover those as separate cases.

## Unified observation contract

### Expected effect

Every executable bearing should carry one explicit expected effect. A useful
minimal shape is:

```text
EffectExpectation
    tag, value
    selected static writer path, when known
    immediate operation/channel boundary
    source world key and source scan
```

For an ordinary trace action, this comes from the selected `TraceAction`, not
from reconstructing provenance strings in `verify.py`. `ActPolicy.heading`
continues to describe the channel boundary; the expectation describes the
specific value/writer whose execution is being tested.

For the startup coast there is no operator action. Its expectation is the
target and the target-relevant route nodes found in the one executed scan.

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
        exact later overwriter occurrence
        values read by the overwriter before its write

    SURVIVED
        exact expected write occurrence, if one was needed
        expected value present at the declared boundary
```

This observation is factual. It does not select a correction, commit a world,
or create a nogood.

The ordered query should live next to
`ScanRungWriteProjection` in `causal/_rung_writes.py` because that object
already owns:

- all exact writes ordered by occurrence ordinal;
- all exact reads ordered by occurrence ordinal;
- `reads_observed_by_write()`;
- `transition_observed_by_read()`; and
- dynamic rung and instruction identity.

Pilot should consume a small query API from that owner instead of repeating
ordinal scans in `steer.py`, `verify.py`, and `retained.py`.

### Explaining absence

Absence is relative to the selected writer occurrence, not to the destination
tag globally.

1. **Guard false.** The selected dynamic rung/instruction did not reach its
   write because an observed guard term was false. That term becomes the next
   requirement. The ordinary trace reader may recursively turn it into one
   steer or a coordinated steer.
2. **Spent.** The selected rung was powered, the instruction is one-shot, and
   its exact memory key was already true at entry. The next requirement is a
   false scan of the owning rung followed by a later assertion. The memory key
   is `instruction.memory_key("_oneshot")`; spentness must be read from the
   retained source state for that exact instruction, never inferred from the
   destination tag.
3. **Not executable.** No matching dynamic occurrence can execute from this
   anchor because its caller, return path, first-scan condition, or
   instruction-owned boundary is unavailable. This produces an earlier-anchor
   requirement or an unresolved frontier, not an empirical nogood.
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

Resolution is deliberately small:

```text
the executable source is still before the requirement deadline
    -> add it to the next single or composed steer

the live tip is past the deadline and no current source remains before it
    -> select the latest retained boundary before all required occurrences
    -> install the correction there
    -> replay the suffix through the observation boundary
```

Several requirements may compose when they must hold at the same occurrence.
Release/assert requirements remain ordered phases. This is not a stored route
suffix: it is one bounded attempt to make one selected writer/effect succeed,
analogous to how `investigate.py` nests a correction inside one replay.

`recovery.py::compose_corrections` should remain the transaction owner. Extend
its candidate identity to include:

- source world key and replay anchor;
- expected effect and selected writer;
- ordered release/assert phases;
- simultaneous assignments in each phase;
- active `PilotRung` identities; and
- requirement deadlines.

An identical attempt from the same anchor is recorded once and never executed
twice.

## Overwrite recovery

When the value appeared but did not survive:

1. Find the first later write to the same destination after the expected
   occurrence and before the declared boundary.
2. Read only the conditions and data values actually observed by that exact
   overwriter.
3. Follow same-scan definitions with
   `transition_observed_by_read()` before falling back to an earlier boundary.
4. Ask existing correction derivation for a minimal way to falsify or delay the
   overwriter. Do not flatten every read into a hold.
5. Attach the overwriter occurrence as the requirement deadline.
6. Replay-test the correction through the original expected boundary.

This is the common entry into `corrections.py` and `investigate.py`. Committed
channel departures still use `progress.py`; failed trial effects and startup
landings should build the same `DeviationIncident`/replay evidence without
first pretending the endpoint was useful progress.

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

Use the instruction owner's `AdvanceProfile.completion_boundary` and the
observed execution/replay, not a generic `0 -> 1` numeric widening. Choose the
smallest type-valid value satisfying the strict relation, or a conservative
larger value when unit quantization requires it. Replay is the oracle.

If replay reaches a later deadline with a larger accumulator, strengthen the
same constraint monotonically and retry from the same anchor. If several
consumers constrain one tag, intersect the constraints. If the intersection is
empty only a complete bounded domain may prove that fact.

A program-unwritten parameter (whether explicitly external or merely inferred
as steerable) is installed as an anchor-persistent `PilotRung` for the replay
corridor. Its guard must be an outer unresolved-target or incident-corridor
condition. It must not contain a self-demand such as
`Preset == proposed_value`, because that prevents the overlay from
establishing the proposed value on its first scan.

The assignment remains unchanged for that replay attempt and is included in
every nested correction derived from it. A stronger constraint creates a new
attempt identity; it is not a mid-execution mutation.

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

If the failed write occurred only on a disposable trial fork, prefer replay
from that trial's source where the instruction was armed. Do not manufacture a
release step. If the source world itself contains the spent memory state, a
bounded composed steer may perform:

```text
phase 1: Reset = false             # one scan; prove exact writer rearmed
phase 2: Reset = true
         AtTarget = true           # simultaneous assertion requirements
         WatchdogPresetMs > 10     # persistent replay requirement
observe: ProcessStep = COMPLETE survives the boundary
```

The outer loop still receives one recomputed result after the transaction; no
future action suffix is retained.

## Startup as a one-scan coast

`pilot.py::_pilot_loop_events` currently calls `state.work.step()` at
`scan_id == 0` before emitting `started`, before creating a checkpoint, and
without a trial or progress receipt. Replace that hidden settle with an
observable bootstrap transaction:

1. Capture the pre-scan `_World` and target objective at boundary 0.
2. Execute exactly one normal program scan with no operator action.
3. Build the exact access projection for boundary `0 -> 1`.
4. Observe target-relevant effects using the same appeared/survived contract.
5. Keep the committed landing as the ordinary current world when nothing
   useful appeared.
6. If target-relevant work appeared and was overwritten, build an incident
   anchored at boundary 0 and run the ordinary bounded correction/replay path.

This is semantically a one-scan coast, but it should not call the current
multi-scan bearing/terminal coast implementations: those own seek/settle
budgets and different stop conditions. Construct the same execution and
incident receipts directly, then hand them to the shared observation and
recovery owners.

An exact startup overwrite is already known regression and can be investigated
immediately. `PendingDeparture` remains useful when a committed landing's
target-relative meaning is genuinely unsettled; its search-scan expiry should
not be overloaded as the occurrence deadline above.

## Attempts are not nogoods

Current behavior records `ActionNogoodObservation` for ordinary rejected
trials, and `_transition_once` also records an act-identity nogood whenever
`attempt.trial is None`. That is too strong for this loop.

Split the concepts:

```text
AttemptReceipt
    this exact writer/anchor/composition/deadline was tried and need not repeat

NogoodProof
    every member of a complete relevant domain has been excluded
```

A failed steer adds an `EffectObservation`, a requirement, a stronger
constraint, an earlier anchor, or an `AttemptReceipt`. It does not by itself
add a nogood.

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

### Phase 1: Pin current execution evidence

- Promote the two alarm-preset fixtures from script assertions into focused
  tests.
- Pin the ordered `0 -> 1` writes and exact preset/Done reads listed above.
- Pin the alarmed action scan's `91 -> 40 -> 80 -> 91` occurrences.
- Add a retained-source case whose Reset one-shot is genuinely spent before
  `how()` begins; keep it separate from the disposable failed-trial case.
- Add the conditional-negative timer fixture: `~Done` contributes to a write
  before `Done` has ever been true.

### Phase 2: Add the expected-effect observation seam

- Carry the selected `TraceAction` writer/effect identity into `Bearing` or
  `ActPolicy` as a typed expectation.
- Add ordered appeared/survived queries beside
  `ScanRungWriteProjection`.
- Have `steer.py` return an `EffectObservation` for every executed attempt.
- Run this observation before `verify.py` turns an endpoint no-op into spin.
- Emit events for `effect_absent`, `effect_overwritten`, and
  `effect_survived`, including exact writer identities and reads.

### Phase 3: Feed explanations back as requirements

- Represent value/guard requirements with occurrence deadlines.
- Route false guards back through the ordinary trace reader.
- Build overwrite incidents from the exact expected and overwriting
  occurrences.
- Let `investigate.py` nest one correction into the selected steer using the
  existing bounded `compose_corrections` transaction.
- Extend semantic attempt identity before enabling retries.

### Phase 4: Make startup observable

- Preserve the pre-first-scan `_World` before the current settle step.
- Execute the startup scan as the single-scan program-owned transaction above.
- Feed an overwritten startup effect into the same requirement/deadline path.
- Reuse `retained.py::replay_retained_prefix` for the corrected `0 -> 1`
  suffix; do not reinterpret scan 1 under a later overlay.

### Phase 5: Solve instruction-owned deadlines

- Invert timer/counter `AdvanceProfile` completion boundaries into constraints
  that keep an observed result on the required side through a deadline.
- Use exact observed accumulator/time values to select the first candidate.
- Refine monotonically from replay evidence.
- Install past-deadline assignments as corridor-scoped persistent
  `PilotRung`s without self-demanding guards.

### Phase 6: Model exact one-shot readiness

- Carry the selected instruction identity far enough to resolve its
  `_oneshot` memory key.
- Distinguish guard-false from guard-true-and-spent.
- Accept an exact false-scan rearm receipt as recovery progress.
- Compose release, assertion, simultaneous holds, and persistent deadline
  assignments in their real order.

### Phase 7: Separate attempts from proofs

- Remove unconditional empirical nogood creation from failed-trial handling.
- Keep stable attempt receipts so identical work cannot repeat.
- Require complete-domain evidence on every `NogoodProof` construction path.
- Update terminal recording to distinguish `proved impossible`, `attempts
  exhausted`, `budget exhausted`, and `unresolved frontier`.

## Acceptance scenarios

### Destructive startup scan

- Pilot observes `AT_TARGET` at its exact within-scan write.
- It identifies the later `ABORTED` writer and its `Done=True` read.
- It follows that read to the timer occurrence and derives
  `PresetMs > 10 ms`, not merely `PresetMs != 0`.
- It anchors at boundary 0, installs one stable preset assignment, and replays
  scan 1.
- `AT_TARGET` survives the boundary and the plan is reachable.

### Disposable failed alarm recovery

- Pilot tries the selected Reset/AtTarget effect on a fork.
- It observes `COMPLETE` appearing and being overwritten by the watchdog.
- It replays from the pre-steer anchor with the preset requirement composed
  into the same Reset/AtTarget steer.
- It does not add an unnecessary Reset release, because that source anchor's
  one-shot remains armed.
- `COMPLETE` survives and the plan is reachable.

### Retained spent one-shot

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
- a prevention attempt replays from before that read; and
- a current-state recovery separately clears any committed consequence.

### Exhausted search

- no semantic attempt repeats from the same anchor;
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
- Replaying from program startup when the latest pre-deadline anchor is enough.
- Retaining a future route suffix across observations.
- Inferring one-shot spentness from a tag rather than an exact instruction
  memory key.
- Turning a failed empirical trial into an impossibility claim.

## Completion criteria

This work is complete when both alarm-preset fixtures reach their targets
through public Pilot behavior, the committed-spent variant performs the
ordered rearm sequence, the event stream names each expected writer,
overwriter, read requirement, deadline, and replay anchor, and no rejected
attempt becomes a nogood without recorded complete-domain evidence.
