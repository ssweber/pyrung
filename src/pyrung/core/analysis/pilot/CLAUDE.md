# pilot/ — a harbor pilot for PLC programs

The user is the **captain** — decides the destination. The PLC program is the **ship** —
it has its own mass, inertia, timing, and logic. **PILOT** comes aboard, reads the
charts, learns the handling, and navigates the dangerous passage.

### Why this is hard (first principles)

- **You share the helm.** Same register file, same scan cycle, no locks, no transactions.
- **Your actions ripple everywhere.** One input hits rungs you haven't traced.
- **The scan is atomic and you're on one side.** You set inputs, the PLC runs all its
  logic, you see the result. No mid-scan intervention.
- **The same input means different things in different states.** Reset from STOPPED ≠
  Reset from EXECUTE.
- **You can't tell who wrote what.** After a scan, a register changed. Was it you or
  the program?
- **Some of the logic is unreadable.** Computed indices, runtime masks, indirect
  addressing. You can see the rung but can't resolve it without running it.
- **Time works against you.** Timers tick, watchdogs count, whether you're making
  progress or not.
- **The program doesn't know you exist.** Not adversarial, not cooperative. It just runs
  its logic every scan.

## The compass is a bearing, not a route

A compass does not plan a path. It gives the pilot a persistent *bearing* toward the
target and keeps re-pointing as the state changes. The pilot is free to "fly around the
mountain" — lateral moves, excursions, detours through the acceptance layers — but the
compass always knows which way the target is. When the loop is wandering (oscillating,
stuck on a distance plateau), the fix is almost always **consult the compass**, not add
another acceptance heuristic.

```
compass = trace + let-run + sandbox
```

All three instruments answer one question — *"I need `(tag = value)`; what must I do?"* —
and differ only in how much of the causal path is readable.

## The loop

```
Compass     — gives the bearing. Trace + let-run + sandbox, merged into one
              persistent direction.
Act         — steer toward it (command pulse or zoom through timer dwell).
Verify      — who moved what?

  1. I moved it where I wanted.        → Confirmed edge.
  2. The PLC moved it where I wanted.  → Auto-edge. Record correctly.
  3. I moved it wrong.                 → Bad edge. Correct the compass.
  4. The PLC moved it wrong.           → My command was a no-op; the program
                                         has its own agenda. Learn both.
  5. Nothing happened / new frontier.  → Unmet prerequisites. Trace back why —
                                         that's the real frontier.

Investigate — on regression (trend worsened after verify), build a bounded
              incident, propose hypotheses, replay-test each, apply confirmed
              holds, revert to checkpoint.

Fix what's fixable, accept what isn't.
Revert on sustained decline — checkpoint, try a different branch.
```

Outcome classification lives in `outcome.py`.
Investigation lives in `investigate.py`.

## The three instruments

### 1. `trace` — read the charts  (`trace.py`)

Reads the map; runs nothing. Two capabilities under one roof:

- **Transparent backward resolution** — walk writer conditions / copy / calc backward to
  steerable inputs. Output: a prerequisite tree (`TraceNode`).
- **Opaque-but-constant value navigation** — when a writer is an indirect/computed jump
  the backward walk can't follow (`ds[computed_idx]`), but the table is *declared
  constants + affine index*, invert it statically and BFS multi-hop over one register's
  value space (`CompassGraph`, `CompassPlan`, `expand_routes`).

Owns: transparent completion chains, and constant commanded value-jumps.

Hard limit: the static read is valid **only while the jump/enable tables are constants
that are never rewritten**. The moment enablement depends on a live word (e.g.
`mask & A_CurDisabledStates_HEX`), trace must return UNKNOWN — never fabricate an edge.

### 2. `let-run` — read the current  (`_try_zoom` / `_letrun_zoom` in `steer.py`)

When the bearing points at a **self-advancing frontier** — a timer or step-counter that
completes on its own under the currently-held state (`Blower__init`→1 while `S_Starting`
drives the calls) — hold heading and let scans pass. Everything live, no isolation.

The primary mechanism is **zoom**: fork, install prerequisite holds, `run_until` the
governing register hits its target value (with an ejection guard that stops immediately
if the register goes somewhere unexpected). Zoom results flow through the same
`verify_gates` pipeline as command pulses — SPIN if nothing moved, CONFIRMED if the
governing register transitioned, AMBIENT_DRIFT if the program ejected.

A bare cone-settle fallback exists at the bottom of the loop as last resort when
neither zoom nor command candidates apply.

Owns: completion *dwell*. This is what closes automatic/completion transitions
(Starting→Execute).

### 3. `sandbox` — send out a skiff  (`sandbox.py`)

When the map is genuinely **unreadable** — a runtime-computed table trace must report
UNKNOWN for — run an isolated experiment: fork, pin every non-participating mutable tag
to its pre-scan value, step, and observe the isolated edge. Feed the learned edge back
into trace's map.

Owns: runtime-gated transitions. **No consumer in the burner today** — it is the
documented escalation for when trace can't read the edge. Kept as a named instrument,
not yet wired into the drive loop.

## Escalation rule

Read first; execute only when reading isn't enough.

1. `trace` (transparent) — cheapest, no execution.
2. `trace` (opaque-but-constant value graph) — still static.
3. `let-run` — when the surfaced frontier self-advances under the held state.
4. `sandbox` — only when trace returns UNKNOWN for a runtime-computed edge.

## Cross-cutting invariant: state-consistent writer selection

A multi-writer pipeline tag (`S_StateCompleteBool`, `isStateEnbl_Yes`) must be traced
through the writer whose guard is **already consistent with the held state**, not the
writer with the fewest open leaves. Minimizing open leaves picks counterfactual branches
(`S_Clearing` needing `S_StateCurrent=1`, or the runtime-mask rung) over the live one
(`S_Starting ∧ Blower__init ∧ Rotate__init`). This is the single change that makes the
real prerequisites surface, and it appears in two places (`isStateEnbl_Yes`,
`S_StateCompleteBool`).

## Boundary gate (the acceptance test)

The burner **Starting→Execute** transition, end to end: trace surfaces
`Blower__init==1` / `Rotate__init==1` as the frontier (via state-consistent writer
selection), let-run coasts them to completion, and `sample_pilot_events.py` drives
distance → 0 (`y_BurnerLoop=True`). Sandbox is *not* needed for this case — if a change
makes it look needed, the bug is in trace's writer selection.

## Module map

- `pilot.py` — the drive loop: iteration prep, candidate selection, commit/revert,
  entry points (`pilot_events`, `pilot_how`, `pilot_drive`).  The conductor.
- `steer.py` — Act instrument: cone settlement, pulse execution, zoom through timer
  plateaus, try-verify wrappers, candidate value proposals.
- `verify.py` — gate pipeline for trial acceptance (SPIN, CYCLE, DEAD-END, outcome).
- `progress.py` — trend monitoring, checkpoint lifecycle, regression recovery.
- `candidates.py` — compass bearing → ranked candidate list, prerequisite/command split,
  zoom prescription.
- `outcome.py` — five-outcome classifier (who moved what).
- `investigate.py` — bounded incident investigation: deviation capture, hypothesis
  generation, replay-confirmed holds.
- `causal.py` — cause-chain walker (`chase_cause_roots`), shared by gate pipeline,
  outcome classifier, and investigation.
- `types.py` — shared cross-boundary types (`_PilotContext`, `_PilotState`,
  `_IterationFrame`, `_PulseState`, `_TrialResult`, `_AttemptResult`, events, aliases).
- `_ops.py` — low-level PLC manipulation primitives (state-key projection, hold
  installation, pulse application, delayed-effect settlement).
- `trace.py` — backward trace engine (transparent static reader), `_all_nodes` utility.
- `compass.py` — opaque-but-constant value graph, influence map.
- `evidence.py` — static route/role expansion that trace reads.
- `sandbox.py` — isolated fork-and-observe experiments.
- `physical.py` — harness/feedback install on forks.

### Phase map

```
Compass     →  candidates.py, trace.py, compass.py
Act         →  steer.py (pulse + zoom + try-verify wrappers)
Verify      →  verify.py (gate pipeline → outcome.py classifies)
Investigate →  investigate.py (regression → hypotheses → replay)
Progress    →  progress.py (trend + checkpoints + regression recovery)
Shared      →  types.py, _ops.py, causal.py
```

