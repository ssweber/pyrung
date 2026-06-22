# PILOT signals — what can we know, infer, or learn?

## What we are building

PILOT (Probe, Input, Let-run, Observe, Trace) is an automated
commissioning engine for PLC programs. Given a target tag value, it
backward-traces the program's rungs to find needed inputs, simulates
forward, and re-traces from any regression. It replaces a 15-module
POCL planner with a single reactive loop that reads the program
directly and learns from observation.

This document catalogs the signals available to PILOT — from program
structure, runtime observation, or both — to measure progress, detect
regression, select candidates, and converge faster. None of these are
rules. They're guides. The simulation is always the final judge.

---

## Progress — "how close are we?"

### Trace tree depth weighting (static + dynamic)

`unsatisfied_count()` treats all conditions equally. A missing `CmdStart`
(one pulse away) weighs the same as a missing `StateCurrent == EXECUTE`
(five transitions away).

Better: weight by depth in the trace tree. A node near the root
flipping from unsatisfied to satisfied is major progress — a whole
sub-tree of prerequisites resolved. A leaf flipping is minor. Measure
progress by the deepest satisfied node, not the count.

### Timer accumulator proximity (dynamic)

A timer at 1500/2000 is 75% done. The trace currently says "unsatisfied"
for both 0/2000 and 1900/2000. Read the accumulator value and the preset
from the live snapshot — that's a continuous progress metric. Also useful
for estimating time remaining.

### Latch vs copy durability (static)

A latch that flipped is durable — it won't revert unless explicitly
reset. A copy result is transient — needs its condition sustained every
scan. The trace can annotate: "this sub-goal was satisfied by a latch
(durable)" vs "satisfied by a copy (fragile)." Durable progress counts
more.

Detectable statically: is the writer an `out()` (latching) or a
`copy()` (non-retentive)?

### Trace tree shape stability (dynamic)

Re-trace each iteration. Compare the tree structure:
- Same shape, fewer unsatisfied nodes → real progress.
- Different shape (different writer path chosen) → lateral movement.
  Not progress or regression — PILOT found a different route.
- Same shape, same unsatisfied count → stasis. Keep going, something
  internal may be advancing (timer, SFC step).

### State on the observed execution flow (hybrid)

If the influence map records a flow A→C→D→Z, and D just moved to a
value that's on-path toward Z, that's progress — even if the trace
tree's unsatisfied count didn't change. The flow tells you things the
trace tree can't see through opaque instructions.

---

## Regression — "did we lose ground?"

### The current rule: all goals regressed (dynamic)

Only re-trace when every tracked goal is worse off than before the last
action. Progress, stasis, or one-up-one-down are acceptable. This is
coarse but safe — overreacting to single-goal regression is what killed
the old walker.

### Depth-weighted tree diff (static + dynamic)

A previously satisfied deep node becoming unsatisfied is worse than a
shallow node flipping. `StateCurrent` going from EXECUTE back to ABORTED
is a deep regression — the root prerequisite collapsed. A timer resetting
is a shallow regression — one leaf lost.

Weight regressions by depth. A deep regression outweighs shallow
progress. A shallow regression alongside deep progress is acceptable.

### Prerequisite ordering (static)

If goal B depends on goal A (A is in B's trace sub-tree), and A
regresses while B advances: ignore B. It's built on a broken foundation
and will collapse next scan. Track A.

The trace tree already encodes this ordering. Don't need a separate
dependency analysis.

### Distinguishing program behavior from regression (dynamic)

PLC programs change tags every scan. Most changes are normal operation.
Regression is specifically: a tag that PILOT drove to a specific value
has reverted, and the reversion undoes progress toward the target.

Signals that it's normal operation (not regression):
- Timer accumulator incrementing (expected).
- State machine transitioning through a transient state (STARTING →
  EXECUTE is expected, not regression from STARTING).
- Subroutine internal state advancing (SFC steps).

Signals that it's regression:
- A tag that PILOT forced or patched reverting to its resting value
  despite the force (something overrode it).
- The trace tree gaining new unsatisfied nodes that weren't there
  before.
- A tag on the observed influence flow moving opposite to the
  target direction.

---

## Blast radius — "how much does this input touch?" (static)

Not all inputs are equal. Some write one tag. Some write dozens.
Prefer narrow inputs over broad ones.

`CmdClear` writes `CtrlCmd` and triggers one state transition — narrow,
predictable. A hypothetical `ResetAll` writes 0 to every register —
broad, destructive. PILOT should prefer `CmdClear` even if both can
satisfy the immediate sub-goal.

Measure blast radius statically: for a given input, how many tags are
in its downstream write cone (via PDG)? How many of those tags are
currently satisfying other sub-goals?

Score: penalize inputs whose downstream cone overlaps with currently
satisfied sub-goals. An input that satisfies one condition but breaks
three others is worse than one that satisfies one and touches nothing
else.

This feeds into candidate selection alongside controllability, stability,
and non-conflict:
- **Controllable**: is it an external input?
- **Stable**: does the writer latch?
- **Non-conflicting**: does it break other sub-goals?
- **Narrow**: does it touch only what we want?

The narrowness check is cheap — count the downstream writes in the PDG
and intersect with the current satisfied set.

---

## Influence mapping — learned correlations (hybrid)

See: `pilot_influence_mapping.md`

Short version: when PILOT observes that input A changes tag Z through
opaque instructions, it records the connection. Static analysis extends
it: if B writes the same intermediate as A, B probably also affects Z.
Confidence decays with structural distance and pipeline coverage. Used
to prioritize candidates when the backward trace hits UNKNOWN.

The execution flow (which rungs fired, in what order, writing what) is
captured after each action — not just the diff, but the ordered chain
of rung executions. This gives PILOT the actual pipeline, not inferred
structure.

---

## The survivor — transient excursions (dynamic)

A tag leaves its resting value and returns within the settle window.
By the time PILOT checks endpoints, nothing appears to have changed.
But the excursion may have triggered side effects — alarms, latches,
resets — that persist even though the triggering tag reverted.

`cause()` walks back from a transition, so matched endpoints (same
value before and after) give it nothing to anchor on. The excursion
is invisible to endpoint comparison.

**Fix:** use the full scan history to detect there-and-back excursions
on monitored tags. For each tracked tag, check: did it leave its
resting value and return during the settle window? If yes, find the
peak scan (where it was at its extreme / non-resting value). Point
`effect()` at that scan index to see what it triggered. Point `cause()`
at that scan to see what drove the excursion.

This composes from existing primitives:
- `monitor()` or scan-by-scan snapshot comparison for excursion detection
- `plc.effect(tag, scan=peak_scan)` for downstream consequences
- `plc.cause(tag, scan=peak_scan)` for upstream cause

The survivor matters because it explains "nothing changed but something
broke." The tag that caused the alarm already reverted — but the alarm
latched. Without excursion detection, PILOT sees the alarm and has no
idea what triggered it.

---

## Writer ranking — prefer resolvable writers (static)

The backward trace picks writers in `sorted(writers)` order and takes
the first one that *could* produce the target value. This is wrong when
a tag has both resolvable writers (with `sp_tree()` conditions) and
opaque writers (indirect memory, indexed arrays).

**Concrete example (burner state machine):**

`S_StateRequested` has multiple writers:
- `sm_CtrlCmd2StateRequest`: `with rung(C_CtrlCmd == 2, S_Idle): copy(sm__STATESTARTINGREF, S_StateRequested)` — resolvable condition, clear steerable path
- `sm_CopyOrJumpState` R11: `copy(sm__where2jump, S_StateRequested)` — opaque, source is `ds[computed_index]`

The trace picks R11 (the jump-table fallback) because it sorts earlier.
This dead-ends at `sm__where2jump`, which has no `sp_tree()` because
it's an indirect memory read. The command-driven writer would have given
the trace a workable path: need `C_CtrlCmd==2` and `S_Idle`.

**Fix:** rank writers by resolvability:
1. Writers with `sp_tree() is not None` AND can produce the value
2. Writers with data-flow bindings (copy source resolvable)
3. Writers with opaque sources (indirect memory, computed indices)

This is purely static — no runtime observation needed. Unblocks the
trace for indexed-lookup patterns (`ds[]`, `dh[]`) common in Click PLCs.

---

## Indirect memory — the concrete "opaque instruction" (static)

The generic "UNKNOWN" / "opaque instruction" pattern in the influence
mapping doc has a specific face in Click programs: **indexed array reads**.

```python
copy(ds[sm__jump_target_ds_idx], sm__where2jump)  # rung 408
copy(dh[isCmdValid__dh_base], isCmdValid__cmd)    # sm_IsCmdValid
```

`sp_tree()` returns `None` for these because the source is computed at
runtime. The trace marks them as dead-ends with no steerable actions.

These are lookup tables — the PLC engineer's equivalent of a switch
statement. They encode configuration (which states are enabled, which
commands are valid, which state to jump to). The program's behavior
depends on them, but the trace can't see through them.

**What influence mapping buys here:** after observing `C_Clear` change
`S_StateCurrent` through this pipeline, the influence map records the
correlation. Next time the trace hits `sm__where2jump` as a dead-end,
the map says "try `C_Clear`" instead of blind-probing 590 inputs.

---

## Sequential sub-goal decomposition (the gap)

The documents above discuss progress metrics and candidate selection —
making PILOT better at picking which button to press. But the burner
probe revealed a structural problem: PILOT doesn't decompose multi-step
sequences into sub-goals.

**The PackML example (without knowing PackML):**

An engineer staring at the HMI sees `State = ABORTED`. They want
`State = EXECUTE`. They don't know PackML. What do they do?

1. Try `C_Start` — nothing happens (not valid from Aborted).
2. Try `C_Clear` — state changes to Clearing, then Stopped. Progress!
3. Try `C_Start` again — nothing (not valid from Stopped either).
4. Try `C_Reset` — state changes to Resetting, then Idle. More progress!
5. Try `C_Start` — state changes to Starting, then Execute. Done.

They didn't plan. They tried things, observed which buttons moved the
state, and kept pressing buttons that moved it forward. Each transition
was a sub-goal they discovered by observation.

**PILOT's failure mode:** It tries `C_Clear`, observes `S_StateCurrent`
change from 9→2. But the distance metric says 11→11 (NEUTRAL) because
the trace tree for `y_BurnerLoop` didn't improve — it still dead-ends
at `sm__where2jump`. PILOT discards the action.

The problem is that `S_StateCurrent=2` IS progress toward
`S_StateCurrent=6`, but PILOT can't see it because:
- The trace tree structure doesn't change (same dead-ends).
- `unsatisfied_count()` doesn't distinguish 9→2 from 9→9.
- The state is "closer" only in the sense of the transition graph,
  which PILOT doesn't have.

**What would fix this:**

1. **Influence mapping** — after observing `C_Clear` change
   `S_StateCurrent`, PILOT records it. Next iteration, when the trace
   hits `S_StateCurrent` as unsatisfied, the map suggests "try the
   commands that previously moved this tag."

2. **Watch-tag value tracking** — if a watch tag (`S_StateCurrent`)
   changes value (even without distance improvement), treat it as
   lateral movement worth exploring further. Don't discard NEUTRAL
   actions that move watched intermediate tags.

3. **Observation-driven sub-goals** — when an action changes a gate tag
   but doesn't reach the target, PILOT could re-trace from the NEW
   state and check if different actions are now available. `C_Reset`
   does nothing from Aborted, but works from Stopped. The re-trace
   after `C_Clear` would show `C_Reset` as a new candidate.

Option 3 is closest to what the engineer does. It doesn't require
planning — just "try again from the new state." The influence map
(option 1) makes it faster. Watch-tag tracking (option 2) prevents
discarding useful actions.

---

## Summary: the signal catalog

| Signal | Source | Used for |
|---|---|---|
| Trace tree unsatisfied count | dynamic | Basic progress metric |
| Trace tree depth weighting | static+dynamic | Better progress metric |
| Timer accumulator value | dynamic | Continuous progress for timers |
| Latch vs copy (writer type) | static | Durability of progress |
| Trace tree shape diff | dynamic | Progress vs lateral movement |
| Prerequisite ordering | static | Ignoring dependent regression |
| Influence flow direction | hybrid | Progress through opaque paths |
| Blast radius (write cone) | static | Candidate selection — prefer narrow |
| Influence map correlations | hybrid | Candidate selection — through UNKNOWN |
| Execution flow capture | dynamic | Building influence map |
| Transient excursion (survivor) | dynamic | Explaining invisible regressions |
| Writer resolvability ranking | static | Prefer writers with sp_tree over opaque |
| Indirect memory detection | static | Identify ds[]/dh[] dead-end pattern |
| Watch-tag value tracking | dynamic | Accept NEUTRAL actions that move gates |
| Observation-driven re-trace | dynamic | Discover new candidates from new state |

### The principle

Every signal either reads the program (static), watches it run
(dynamic), or combines both (hybrid). None of them are rules. They're
guides that help PILOT make better first guesses. The simulation is
always the final judge.
