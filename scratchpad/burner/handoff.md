# Burner PILOT handoff

Goal: `pilot_how(plc, y_BurnerLoop, choice=1)` should reach `y_BurnerLoop=True`
on the real Click burner program. This is the **PILOT** planner
(`core/analysis/pilot/`), not the older `walk/` corridor walker.

- CLICK project under test:
  `C:\Users\ssweb\AppData\Local\Temp\CLICK (00680950)\pyrung_project`
  (set `PYRUNG_CLICK_PROJECT` to override).
- Harness: `scratchpad/burner/test_pilot_burner.py` (forces door/blower/rotate
  permissives, one `step()`, then `pilot_how(... choice=1, max_scans=3000)`).
- `y_BurnerLoop` has 3 output routes (Production / Maintenance / Manual);
  `choice=1` = ProductionMode. Without a choice, `pilot_how` returns an
  ambiguous `Path` (`path.ambiguous`, `path.choices`).

## Current state (2026-06-23)

- `make test` pilot suite: **28 passed** (`tests/core/analysis/test_pilot.py`).
- Burner: `reachable=False`, budget exhausted at scan 3000.
- **Stall progression this session:** 47 (pre-fix, stuck before UnitMode) →
  24 (L6 cone + trace guard, `choice=1`) → 19 (Layer 3 frontier fix) →
  **16 (trend dedup)**. Distances after the dedup are distinct-pivot counts.
- Stuck cycling the state machine **backward**: `S_StateCurrent` oscillates
  `9↔1↔2` (ABORTED↔CLEARING↔STOPPED), never reaching **6 (EXECUTE)**. Still
  need: `S_StateCurrent=6`, `S_StateRequested=6`, `isStateEnbl_Yes=1`,
  `S_Execute=True`, then the Heat chain.

**Click state encoding** (`S_StateCurrent`): 0 undefined, 1 CLEARING,
2 STOPPED, 3 STARTING, 4 IDLE, 5 SUSPENDED, **6 EXECUTE**, 7 STOPPING,
8 ABORTING, 9 ABORTED, 10 HOLDING, 11 HELD, 12 UNHOLDING, 13 SUSPENDING,
14 UNSUSPENDING, 15 RESETTING, 16 COMPLETING, 17 COMPLETED. Real startup path:
`9 → (Clear) 1 → (wait) 2 → (Reset) 15 → (wait) 4 → (Start) 3 → (wait) 6`.
The `-ING` states are transient (auto-advance to the next stable state on a
wait); the others are stable.

## What landed this session

**Commit `6a1d1a4`** `feat(pilot): selectable output routes, L6 command cones,
trace loop guard`:

1. **L6 per-target command cone** (`pilot.py` `_cmd_inputs`, `influence.py`).
   L6 was probing the opaque-pipeline `free_args` *union* — polluted with
   alarms, IO faults, limits, and a literal `True`, while **missing
   `C_UnitModeChgRequest`**. Now each dead-end register is probed with its own
   `upstream(tag) & steerable & Bool` cone. This got PILOT past UnitMode.

2. **Opaque-loop trace guard** (`trace.py` feedback-loop guard +
   `influence.py` `detect_opaque_loop`). `trace_back` was inverting the
   jump-table state machine (`S_StateCurrent → isStateEnbl_Yes →
   S_StateRequested=2 → S_Stopping → S_StateCurrent=7 → …`), walking the whole
   state graph. Now registers in the opaque feedback loop (downstream ∩
   upstream of an indirect-copy target) are cut to dead-end leaves so Layer 6
   owns them. Burner trace **depth 33→12, unsatisfied count 97→44**. Gated on
   the opaque-loop cluster, so simple direct-copy state machines are untouched.

3. **Route choices for ambiguous Bool outputs** (`TraceChoice`,
   `enumerate_trace_choices`, `Path.choices`/`.ambiguous`, `choice=` on
   `how()`/`pilot_how`/`pilot_drive`). Root-only locks: a choice pins the
   output writer + its OR-arm; `trace_back` re-traces below. Enumeration reuses
   `trace_back`'s lock mechanism (the parallel `_enumerate_target_routes` walk
   was removed in the consolidation pass).

**Commit `fb5b097`** `fix(pilot): Layer 3 counts opaque-loop registers as
frontier`. Layer 3 ("Don't Dead-End") counted only the *trace* frontier, so
once the opaque-loop guard handed the state registers to L6, every probe into
the state machine read as an empty frontier and was rejected. Now a dead-end
leaf whose tag is in `opaque_loop` counts as frontier. **Scoped to
`opaque_loop`** — broadening it to "any dead-end leaf with a command cone" made
DEAD-END never fire and broke `test_l6_probe_with_trace_context` (terminal
`Mode` output is not a feedback register). Burner 24→19.

**Commit `f95a2c4`** `fix(pilot): dedup Layer 4 trend count by (tag, value)`.
`unsatisfied_count` counted every tree node (~2x inflation on the cyclic state
machine). Now counts distinct `(tag,value)` pivots; `still need` deduped too.
Burner 19→16 and the stall became legible (one checkpoint, oscillation)
instead of tree-size noise.

## Next blocker: backward state cycling (Layer 6 topology / goal distance)

NOT a settle bug and NOT a wandering bug. The deduped trend made it legible:
the state machine **cycles backward**. Evidence from `run_dedup.log`:

- accepted transitions: `9→1`, `9→2`, `2→9`, `1→9`.
- accepted backward commands: `C_ResetToFactoryDefaults ×18`, `C_Abort ×1`,
  `C_ForceClear ×2`. PILOT reaches CLEARING/STOPPED, then presses a button that
  throws it back to ABORTED(9). Loops `9→1→2→9`.

**The settle is fine.** `diag_clearing_settle.py` proves CLEARING(1)→STOPPED(2)
is a **1-scan** auto-advance and STOPPED is stable for 60+ scans. "Let-run" (the
4-scan settle in `_apply_pulse`) already waits enough. So "wait on everything"
works mechanically; the problem is what PILOT does *after* the wait.

**Why nothing stops the backward moves:**
- **Trend is blind to state-machine proximity.** ABORTED(9), CLEARING(1),
  STOPPED(2) all have the *same* deduped trace-distance (~16) — each still needs
  the whole `…→15→4→3→6` chain. So "press Start-ward" and "press Abort" look
  identical to Layer 4. STOPPED is 3 hops from EXECUTE, ABORTED is 5, but the
  trace count can't see that.
- **Novelty is blind too.** Going back to ABORTED *should* trip L1 (Don't
  Cycle), but the full state key carries extra flags, so ABORTED-with-different-
  flags reads as novel and slips through.
- **L6 chases a phantom edge.** At the stall L6 probes `S_StateCurrent (1->3)` —
  a single command for CLEARING→STARTING, which is not a real transition. The
  real edge `1→2` is a *wait* (auto) edge; L6 only models command edges.

**The fix = the Layer 6 topology accelerator** (the "goal distance via slices"
half of the causal-momentum design):

1. **L6 learns the real state graph from observed transitions, INCLUDING
   wait/auto edges** (`1→2` happens on a step with no command — record it, not
   just command-induced changes). Then BFS chains wait + command edges
   (`9→1→2→15→4→3→6`) and stops chasing the phantom `1→3`.
2. **Gate direction by graph distance.** A move that increases
   `S_StateCurrent`'s hop-distance-to-EXECUTE in the learned graph is a
   regression — reject `Abort`/`ResetToFactoryDefaults` from STOPPED even though
   trace-distance is flat. Prefer the forward command, and prefer *waiting* when
   the graph says the current state auto-advances toward the goal.

**Open design questions (for next session):**
- Learn the graph purely from observation, or also seed it statically from the
  `sm_CtrlCmd2StateRequest` rungs (command→requested-state)? Static seed
  jump-starts it but flirts with "build a graph first."
- Distance as a new `InfluenceMap.graph_distance(tag, from, to)` feeding a
  per-candidate regression check, vs. folding into existing `harmful_inputs`
  masking (already prunes off-BFS-path inputs once a path is known).

The earlier "demote Layer 3 / reorg" plan still stands but is secondary — the
backward-cycling is the thing pinning the burner now.

**Recommended next steps:** (1) dedup `unsatisfied_count` / `still need` by
`(tag,value)` and re-run; (2) then the Layer 3 demotion + ordering reshuffle.

## Architecture (PILOT layered acceptance)

`_pilot_loop` in `pilot.py`. Per candidate action (fork → pulse → settle):
- **L0 Don't Spin** — state key must change (key = prover projection).
- **L1 Don't Cycle** — new key must be novel (`seen_keys`).
- **L2 Don't Hallucinate** — excursion detection + hold derivation.
- **L3 Don't Dead-End** — frontier non-empty (trace actions ∪ **L6 frontier**
  ∪ pending). *This is the layer just patched.*
- Commit; then **L4 Don't Wander** (checkpoint on trend) / **L5 Don't Repeat**
  (cause-chain holds + checkpoint revert).
- **L6 Don't Rediscover** — `InfluenceMap` transition tables, BFS, harmful
  masking; owns the opaque-loop registers.

## Key files

- `src/pyrung/core/analysis/pilot/pilot.py` — `_pilot_loop`, `_cmd_inputs`,
  `_has_l6_frontier`, `_prepare_trace_choice`, entry points.
- `src/pyrung/core/analysis/pilot/trace.py` — `trace_back` (opaque-loop guard
  at `_SAME_TAG_VALUE_BUDGET`), `enumerate_trace_choices`, `TraceChoice`.
- `src/pyrung/core/analysis/pilot/influence.py` — `InfluenceMap`,
  `detect_opaque_loop`, `detect_opaque_pipelines`.
- `src/pyrung/core/analysis/graph.py` — `Path.choices` / `Path.ambiguous`.

## Diagnostics (scratchpad/burner/, all untracked)

- `test_pilot_burner.py` — the burner harness (`choice=1`, `debug=True`).
- `diag_l6_freeargs.py` / `diag_l6_leaf_starvation.py` — free_args pollution,
  L6 leaf eligibility, count inflation.
- `diag_opaque_cluster.py` — the opaque feedback-loop cluster computation.
- `diag_leaf_depth.py` / `diag_trace_paths.py` — trace depth + the
  state-machine explosion path.
- `diag_per_target_cone.py` / `diag_steerable_junk.py` — per-register Bool cone,
  steerable junk root-cause (`True`, alarms, IO faults).
- `diag_state_rungs.py` — the `sm_CopyOrJumpState` writer rungs.
- `diag_clearing_settle.py` — proves CLEARING(1)→STOPPED(2) is a 1-scan
  auto-advance and STOPPED is stable (settle is not the blocker).
- `run_*.log` — captured debug runs (`run_dedup.log` = current stall:
  `9↔1↔2` backward cycling).
