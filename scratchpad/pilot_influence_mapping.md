# Influence mapping — learned correlation for PILOT

## The insight

When PILOT observes that input A changes tag Z (through opaque
instructions it can't analyze statically), it learns: A influences Z.

But it can learn more. If static analysis shows that B writes the same
intermediate tag C that A also writes — and A's path to Z goes through
C — then B probably also influences Z. The closer B is to the observed
path, the more likely the correlation holds.

This is what an engineer does: "I changed A and Z moved. B does the
same thing as A in this part of the circuit. They're probably connected."

Correlation, extended by structural proximity.

## Confidence by distance — two dimensions

Confidence depends on two things:

1. **Structural proximity** — how many hops from B to a shared point
   on the observed path. Fewer hops = higher confidence.
2. **Pipeline coverage** — where in the chain is the shared point?
   Earlier = more of the downstream pipeline is shared = higher
   confidence. Later = less shared = lower confidence.

Given an observed path A→C→D→E→Z:

- **B also writes C** (same entry point as A): high confidence. B
  shares the entire pipeline C→D→E→Z.
- **B writes D** (one step later in the chain): moderate confidence.
  B shares D→E→Z but not the C→D link. Less pipeline in common.
- **B writes something that writes D** (one hop away AND enters late):
  lower confidence on both dimensions.
- **B writes E** (near the end of the chain): low confidence. Only
  shares E→Z. Most of the pipeline isn't shared.

Confidence = proximity to observed path × how much downstream pipeline
is shared. Both decay, and they multiply.

## How to use it

### Building the map

As PILOT runs, after each action:
1. Capture the **execution flow**, not just the diff. Which rungs fired,
   in what order, writing which tags? This is the actual pipeline:
   `CmdClear → rung 12 → CtrlCmd → rung 25 → StateRequested → rung 47
   → StateEnableYes → rung 52 → StateCurrent`. An ordered chain of
   (rung, tag written, value) triples.
2. `cause()` already traces this backward from a changed tag. Use it to
   reconstruct the forward flow. Or record which rungs produced new
   values during the pulse scans (compare tag snapshots per-rung, not
   just per-scan).
3. The observed flow gives you the concrete path A→C→D→...→Z with real
   rung indices. No inference needed for this — it's directly observed.
4. Then extend with static structure: which other inputs write the same
   intermediate tags in this path? Those are inferred influences, scored
   by proximity × pipeline coverage.

Store as: `{target_tag: [(input, confidence, source), ...]}` where
source is "observed" or "inferred via <intermediate>".

### Using the map for candidate selection

When the backward trace hits UNKNOWN (can't statically determine what
affects a tag), check the influence map:
- Any observed influences? Try those first.
- Any high-confidence inferred influences? Try those next.
- Nothing? Fall back to upstream-cone probing (Phase 2).

This replaces blind probing with informed guessing. Not guaranteed
correct — but a much better guess than alphabetical order.

### Growing the map over time

Each PILOT run adds observations. Across multiple `how()` or `pilot()`
calls on the same program, the influence map accumulates. First run
discovers A→Z. Second run (maybe from a different starting state)
discovers B→Z. The map gets richer with use.

Could persist across sessions if attached to the program (like a cache).
Invalidated when the program changes.

## The principle: learned guides, not rules

The influence map is an optimization for PILOT's convergence speed.
It helps PILOT make better first guesses so it needs fewer attempts.
It is NOT a rule system. The map suggests candidates — the simulation
validates them. A wrong suggestion costs one fork. A right suggestion
saves twenty.

Nothing from the map overrides the simulation. If the map says "try B"
and B doesn't work, PILOT moves on. The map is an engineer's intuition
built from experience: "last time I was here, this worked." It makes
PILOT faster, not different.

## Relationship to PILOT layers

- **Layer 1**: builds the map from observations during forward execution.
  Uses it for candidate ordering on retries.
- **Layer 2**: uses the map to prioritize fork probes when the backward
  trace returns UNKNOWN. Fewer forks, faster convergence.
- **Not a planning mechanism**: the map is a cache of observations, not a
  model of the program. It suggests candidates. The simulation validates
  them. Wrong suggestions cost one fork. Right suggestions save twenty.

## API surface

Eventually:

```python
plc.influences(tag)
# Returns observed and inferred inputs that affect this tag:
# [("CmdClear", 1.0, "observed"),
#  ("CmdReset", 0.8, "inferred via CtrlCmd"),
#  ("CmdAbort", 0.6, "inferred via CtrlCmd → StateRequested")]
```

Same principle as everything else: build it for PILOT, expose it for
everyone. An engineer calls `plc.influences(AlarmCoil)` and sees which
inputs affect that alarm. Useful for debugging, not just for the walker.

## Concrete example: burner state machine (2026-06-22)

The burner program has a PackML state machine implemented as a pipeline
of `sm_*` subroutines. PILOT was asked to reach `y_BurnerLoop=True`.

### What PILOT sees (and can't get past)

After an initial patch (distance 16 to 11), PILOT gets stuck. The trace
tree bottoms out at:

```
S_StateCurrent: GATE  want=6  have=9
  S_StateRequested: GATE  want=6  have=0  [copy]  (rung 409)
    sm__where2jump: DEAD-END  want=6  have=0  [copy]  (rung 408)
```

Rung 408 is `copy(ds[sm__jump_target_ds_idx], sm__where2jump)` — an
indexed array read. `sp_tree()` returns None. Dead end. Every steerable
input is NEUTRAL at distance 11 because nothing in the trace tree
changes.

### What an engineer sees (without knowing PackML)

The HMI shows `State = ABORTED`. The engineer tries buttons:

1. `C_Clear` — state changes: Aborted(9) -> Clearing(1) -> Stopped(2)
2. `C_Reset` — state changes: Stopped(2) -> Resetting(15) -> Idle(4)
3. `C_Start` — state changes: Idle(4) -> Starting(3) -> Execute(6)

Each button worked from a specific state. The engineer learned by
observation which button to press when.

### What the influence map would capture

After step 1 (`C_Clear`), the execution flow is:

```
C_Clear=True
  -> sm_MapCmd2Val rung R10: copy(9, C_CtrlCmd)
  -> sm_IsCmdValid: dh[S_StateCurrent] lookup validates command
  -> sm_CtrlCmd2StateRequest R8: copy(sm__STATECLEARINGREF, S_StateRequested)
  -> sm_CopyOrJumpState R3: isStateEnbl_Yes=1 (state 2 is always-enabled)
  -> sm_CopyOrJumpState R8: copy(S_StateRequested, S_StateCurrent)
  -> sm_MapVal2State: S_Clearing=True
  -> sm_StateComplete2Request R10: copy(sm__STATESTOPPEDREF, S_StateRequested)
  -> (next scan) S_StateCurrent=2, S_Stopped=True
```

The map records: `C_Clear` influences `S_StateCurrent` (observed, confidence=1.0).
Static extension: `C_Reset`, `C_Start`, `C_Stop`, `C_Abort` all write
through the same pipeline (`sm_MapCmd2Val` -> `C_CtrlCmd`), so they
also influence `S_StateCurrent` (inferred, high confidence — same entry
point, full pipeline shared).

On the next iteration, when the trace hits `S_StateCurrent` as
unsatisfied, the influence map says "try the command buttons" instead
of blind-probing 590 inputs. The re-trace from `S_StateCurrent=2`
discovers `C_Reset` is now valid (needs `S_Stopped`, which is True).
Repeat until Execute.

### Why this works without knowing PackML

The map doesn't encode "Aborted -> Clear -> Stopped -> Reset -> Idle."
It encodes "command buttons affect state." Each iteration discovers
which command works from the current state by observation. The sequence
emerges from repeated trace + observe, not from a model of PackML.

This is exactly what the engineer does. They don't memorize the state
diagram. They learn "Clear worked last time the state was stuck" and
try it. If it doesn't work, they try the next button.

## Implementation plan (from 2026-06-22 session)

### What's built

Infrastructure that the influence map needs:

- **Writer ranking** (`trace.py:_rank_writers`): prefers Literal/satisfied-source
  writers, so the trace follows command-driven paths instead of dead-ending
  at jump tables.
- **Lookup table inversion** (`trace.py:_invert_indirect`): reads `block[ptr]`
  from the snapshot, enumerates plausible index values, finds which produce the
  target.  Hops through calc-defined scratch via `_single_calc_source`.
- **Reference constant detection** (`trace.py:compute_reference_constants`):
  never-written copy sources that feed into pointer chains (via functional dep
  collapse).  Excluded from steerable set.
- **Blast-radius filtering** (`pilot.py`): `pdg.downstream_slice` per action,
  excludes high-blast-radius inputs from batches and deprioritizes in candidates.
- **Gate-movement acceptance**: accepts NEUTRAL actions that move watch tags
  (budgeted, max 3).
- **Chain-progress acceptance**: accepts regressions that move a watch tag to
  a same-tag-chain prerequisite value.
- **Damage/nogood management**: damage_history persists (won't re-accept same
  damage); nogoods clear on progress (state-dependent).

### What's NOT working (the wall)

The backward trace picks paths through states unreachable from the current
state.  The distance metric (unsatisfied_count) lies for state machines.
`C_Reset` from Stopped genuinely transitions to Idle, but the trace-tree
distance goes UP because the tree reshuffles.  Every incremental fix reveals
the next symptom.

### Next step: influence-map probing

Replace blind candidate probing with targeted state-machine exploration:

1. **Trigger**: when the trace says "I need tag T = V" and T is a watch tag
   (a gate in the trace tree), switch to influence-map mode for T.

2. **Structural extension**: once one input (e.g. `C_Clear`) is observed to
   move T, find other inputs that write through the same pipeline entry point.
   Static check: do they share a downstream path through the same intermediate
   tag (e.g. `C_CtrlCmd`)?  If yes, they're command-group siblings.

3. **Group probe**: fork-and-probe each command-group sibling from the current
   state.  Record `{(current_T_value, input): new_T_value}`.  One fork per
   sibling -- cheap.

4. **Build transition table**: after probing the group, you have:
   ```
   From 9: C_Clear -> 2
   From 9: C_Reset -> 9  (no change)
   From 9: C_Start -> 9  (no change)
   From 2: C_Reset -> 4
   From 2: C_Start -> 2  (no change)
   From 4: C_Start -> 6  (target!)
   ```

5. **Pathfinding**: BFS over the transition table.  Shortest path from current
   value to target value.  Execute the path step by step.

6. **Integration**: the path is a sequence of `(input, expected_T_value)` pairs.
   Apply each, verify T moved, continue.  If it doesn't move as expected,
   re-probe from the new state.

This replaces the backward trace + distance metric for state-machine tags.
The trace is still useful for non-state-machine parts (latch chains, timers,
Boolean conditions).

## What this is NOT

Not a dependency graph. Not a data flow analysis. Not a planner input.
It's correlation — observed, then extended by structural proximity.
An engineer's intuition, formalized as a dict.
