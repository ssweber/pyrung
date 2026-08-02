# Pilot initialization and recovery plan

## Status

Proposed. This document describes the cohesive behavior needed for Pilot to
prevent and recover from immediate timer-driven departures. It does not propose
changes to the user program or simulated PLC semantics.

## Problem

Pilot can encounter a program where:

- an externally configured timer preset begins at zero;
- the timer completes immediately during the first relevant scan;
- the route briefly reaches a useful state and is overwritten by an alarm or
  abort later in the same scan;
- an apparently suitable reset action has a one-shot writer which may already
  be spent; and
- the successful recovery requires a persistent initialization value,
  simultaneous holds, and ordered release/reassert actions.

Today these facts are handled by separate mechanisms, or are not represented
at all. Pilot may omit the preset from its candidates, treat a failed steer as
an undifferentiated nogood, miss transient within-scan progress, and retry an
equivalent rejected composition until its wall-clock behavior resembles a
hang.

## Desired behavior

The Pilot loop should be:

```text
run scan 0
-> inspect ordered target-relevant history
-> recognize and solve initialization constants
-> attempt a steer
-> explain a failed steer using exact writer identity
-> discover overwrites, missing prerequisites, or spent one-shots
-> compose initialization, holds, and ordered recovery actions
-> replay from the earliest necessary anchor
-> verify progress or terminate when no new knowledge is possible
```

The behavior has two modes that share the same learned facts:

- **Prevention:** install initialization assignments before their first
  consequential observation and replay from that anchor.
- **Recovery:** keep those assignments installed, then clear consequences and
  rearm/reassert any required one-shot writers from the current state.

## Core concepts

### 1. Initialization constants

An external tag should be considered an initialization constant when it has no
program writer and is consumed as a configuration parameter, initially timer
and counter presets.

An initialization assignment is not an ordinary steer. It is:

- chosen once for a replay attempt;
- installed at the replay anchor;
- persistent for the entire replay;
- refined by stronger constraints rather than mutated opportunistically in
  the middle of execution; and
- included in every later composition derived from that replay.

The exact type names remain an implementation choice, but the model should
separate at least:

```text
InitializationAssignment  persistent configuration
OperationalHold           simultaneous scan-level input
RecoveryAction             ordered release/assert/clear operation
```

### 2. Initialization value synthesis

Classification alone is insufficient. A timer preset whose observed domain is
only `(0,)` still needs a useful nonzero candidate.

Pilot should derive a constraint from the timer and the route, for example:

```text
PresetMs > elapsed time during the guarded interval
```

It may then choose a conservative satisfying value. If several consumers
constrain the same initialization tag, Pilot must solve the intersection. If a
route contains several independent timer presets, each receives its own
assignment.

### 3. Consequential observations and replay anchors

"The timer has never completed" is not a sufficient test for whether a preset
can safely be installed at the current point. A false timer output can already
have affected execution:

```python
with Starting, ~CycleTimer.Done:
    latch(Alarm)
```

The relevant boundary is the timer's first **consequential observation**: a
read of `Done`, `~Done`, or another derived timer value that selected, blocked,
or otherwise influenced a route-relevant writer.

Use the cheapest sufficient rule first:

```text
timer never enabled
    -> install the new initialization assignment at the current anchor

timer enabled, but no derived output had a consequential consumer
    -> installation at the current anchor is still valid

derived output had a consequential consumer
    -> replay from the activation episode or first consequential observation
```

This anchor is not automatically program startup. Replay only the relevant
activation episode unless the causal history requires going farther back.

### 4. Exact within-scan history

The startup audit must not rely only on committed boundary values. A single
scan may contain a route such as:

```text
initial -> ready -> running -> at target -> alarmed
```

The audit needs ordered writer occurrences and their causal reads so it can
answer:

- Did a target-relevant leaf change during scan 0?
- Was a desired route node satisfied transiently?
- Which writer overwrote it?
- Did a zero-preset timer complete immediately?
- Which timer output did the overwriting writer observe?

Committed scan boundaries remain the authority for state. Exact replay remains
the authority for occurrence identity.

### 5. Failed-steer explanations

A steer that does not produce its expected effect should trigger a focused
causal query rather than only becoming a generic action nogood.

The initial explanation taxonomy is:

1. The selected writer's guard never became true.
2. Its guard became true, but the selected one-shot writer was spent.
3. The intended write occurred and was overwritten later in the same scan.
4. The write committed and was reverted in a later scan.
5. The writer selected by trace was not executable from this anchor.

Each explanation must identify the selected writer occurrence. It should
produce a prerequisite, rearm requirement, earlier replay anchor, or explicit
nogood that the composer can use.

### 6. One-shot readiness

Spentness belongs to a particular one-shot writer occurrence, not to the tag
being steered globally.

Trace/recovery needs to represent:

- whether the selected writer is armed or spent at the candidate anchor;
- the condition whose false scan rearms it;
- whether a separate scan is required for that rearm; and
- the later assertion required to execute it again.

A generic suggestion to release a held input is not enough. Pilot should be
able to explain that the release is required to rearm a specific writer and
preserve the ordering in the final recovery sequence.

### 7. Composition

The recovery composer must combine three different forms of correction rather
than flattening them into an unordered set of tag changes:

```text
init:    WatchdogPresetMs = safe value

step 1:  Reset = false             # rearm the selected one-shot writer

step 2:  Reset = true
         AtTarget = true           # simultaneous operational holds

then:    coast and verify target
```

Alarm clearing, where structurally required, is another ordered recovery
action. Fixing the initialization cause does not imply that already committed
consequences have been repaired.

Every composed plan must state its anchor. Prevention plans normally anchor
before the consequential timer observation; recovery plans may anchor in the
current alarmed state.

### 8. Productive-search and termination rule

Every rejected attempt must add at least one new fact:

- a causal explanation;
- a prerequisite;
- a stronger initialization constraint;
- a new ordered composition;
- a replay anchor; or
- a nogood that excludes the attempted action from the same anchor.

Attempt identity must include the anchor, initialization assignments, ordered
actions, simultaneous holds, and relevant writer identity. An equivalent
attempt from the same anchor must not execute twice.

If no unexplored explanation or composition remains, Pilot emits a concrete
`stuck` result. Repeating retained probes without changing knowledge is not
progress.

## Implementation sequence

### Phase 1: Pin the observable contracts

- Use the test-only fixtures in `tests/fixtures/pilot_alarm_presets/` as the
  primary acceptance programs.
- Add a focused conditional-negative-observation fixture in the same package
  so that `~Timer.Done` causing a write before `Done` has ever been true is
  covered explicitly.
- Capture expected ordered scan-0 writes, selected timer writer identity,
  one-shot runtime state, and current Pilot event sequence.
- Keep the fixtures generalized and independent of any application template.

### Phase 2: Add the scan-0 route audit

- Retain and query the physical first scan after it executes.
- Compare the target route against ordered occurrences, not only endpoint
  state.
- Emit findings for transient satisfaction, immediate departure, and the
  writer/read edge responsible for the departure.
- Run this audit before selecting the first ordinary steer.

### Phase 3: Classify and solve initialization constants

- Detect external, unwritten preset tags structurally.
- Derive timer preset constraints even when the observed tag domain contains
  only zero.
- Store the chosen values as persistent initialization assignments.
- Find the cheapest valid replay anchor using timer activation and
  consequential-consumer history.
- Prohibit treating these assignments as opportunistic mid-route steers.

### Phase 4: Explain failed steers

- Route ordinary rejected/no-effect steers through the focused causal
  explanation taxonomy.
- Return structured findings instead of only logging prose.
- Feed the findings into the same knowledge/nogood scope used by composition.
- Distinguish same-scan overwrite from later committed reversion.

### Phase 5: Model one-shot writer readiness

- Expose armed/spent state for the exact writer selected by trace.
- Derive its rearm condition and whether a release scan is required.
- Convert the result into ordered recovery actions.
- Verify that unrelated one-shot writers for the same destination tag are not
  conflated.

### Phase 6: Compose and replay

- Compose persistent initialization assignments, simultaneous holds, and
  ordered recovery actions without losing their different lifetimes.
- Choose prevention or recovery anchoring from causal history.
- Preserve learned initialization across sibling correction attempts.
- Verify both reaching the target and avoiding the diagnosed departure.

### Phase 7: Enforce termination

- Give attempts a stable semantic identity.
- Suppress identical rejected attempts from the same anchor.
- Require a knowledge delta before retrying retained replay/composition.
- Emit `stuck` with the exhausted explanations and unmet prerequisites when no
  productive branch remains.

## Acceptance scenarios

### Alarmed at start

Given the alarmed-at-start fixture:

- Pilot recognizes the zero-valued watchdog preset as an initialization
  constant despite its observed domain containing no useful value.
- A preset-only retry is explained as insufficient when the reset one-shot is
  spent.
- Pilot derives the release scan, reset reassertion, and simultaneous target
  hold.
- The composed recovery reaches the target state.
- The initialization assignment remains constant throughout the replay.

### Destructive first scan

Given the first-scan-abort fixture:

- Pilot observes the ordered route through the target before the final abort.
- It attributes the departure to the immediate timer completion.
- It derives and installs the initialization assignment before the
  consequential observation.
- It replays from the retained pre-scan-0 anchor and reaches the target without
  aborting.

### Conditional negative timer observation

Given a timer which has never produced `Done=True`, but whose `Done=False`
condition contributed to an alarm write:

- Pilot does not claim that installing the preset in the current state repairs
  the existing alarm.
- It identifies the negative timer observation as consequential.
- Prevention replays from before that observation.
- Current-state recovery includes both the persistent initialization and the
  required alarm-clear/rearm sequence.

### Exhausted search

Given a case where all explanations and compositions are rejected:

- no semantic attempt is repeated from the same anchor;
- the event stream continues to expose each newly learned fact; and
- Pilot terminates with `stuck` and an actionable reason within its search
  budget.

## Non-goals

- Adding configuration bits or `preset_set` tags to user programs.
- Mutating timer presets repeatedly during a route.
- Replaying from program startup when a later activation anchor is sufficient.
- Treating every read of a timer output as consequential.
- Replacing committed history with occurrence summaries.
- Solving all external numeric parameter inference in the first implementation;
  timer and counter presets are the initial bounded domain.

## Completion criteria

This work is complete when the acceptance scenarios pass through public Pilot
behavior, the event stream explains the selected anchors and recovery steps,
and the original repeated-probe behavior terminates without relying solely on
a wall-clock timeout.
