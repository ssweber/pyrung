# pilot/ — the compass

PILOT drives a PLC program from its current state to a target by pulsing steerable
inputs and letting scans run. The organizing idea of this directory is the **compass**.

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

## The three instruments

### 1. `trace` — the static reader  (`trace.py`)

Reads the map; runs nothing. Two capabilities under one roof (the old `compass.py`
value-graph is folded in here):

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

### 2. `let-run` — coast  (the WAIT action, in `pilot.py`)

When the bearing points at a **self-advancing frontier** — a timer or step-counter that
completes on its own under the currently-held state (`Blower__init`→1 while `S_Starting`
drives the calls) — hold heading and let scans pass. Everything live, no isolation.

Owns: completion *dwell*. This is what closes automatic/completion transitions
(Starting→Execute).

### 3. `sandbox` — scout  (`sandbox.py`, was `probe.py`)

When the map is genuinely **unreadable** — a runtime-computed table trace must report
UNKNOWN for — run an isolated experiment: fork, pin every non-participating mutable tag
to its pre-scan value, step, and observe the isolated edge. Feed the learned edge back
into trace's map.

Owns: runtime-gated transitions. **No consumer in the burner today** (its targets are all
on the hardcoded fast-path) — it is the documented escalation for when that stops being
true. Kept as a named instrument, not yet wired into the drive loop.

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

## Supporting substrate (not instruments)

- `evidence.py` — `expand_routes`, `PipelineRoles`, `infer_pipeline_roles`: the static
  route/role expansion that trace reads.
- `influence.py` — `InfluenceMap` (learned transitions, `find_path`, WAIT prescription via
  `WaitCause`), `detect_opaque_loop`. Note: `InfluenceMap.probed_actions` is the
  *influence-learning* probe, unrelated to `sandbox`.
- `steers.py` — candidate value generation (`upstream_candidates`).
- `physical.py` — harness/feedback install on forks.
- `pilot.py` — the drive loop and acceptance layers that consult the compass.

## Naming history

- `compass.py` (value graph) folded into `trace.py`; the name `compass` was promoted to
  this whole layer.
- `probe.py` → `sandbox.py` (the word "probe" collided with `InfluenceMap.probed_actions`).
- If `trace.py` grows unwieldy, split into a `trace/` package (`back.py` + `graph.py`).
  Deferred until the functional work lands.
