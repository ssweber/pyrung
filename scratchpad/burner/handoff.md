# Burner PILOT handoff — rotate-sensor liveness via round-by-round holds

Goal: `pilot_how(plc, y_BurnerLoop, choice=1)` reaches **Execute(6)** then
`y_BurnerLoop=True` on the real Click burner. The last mile is **liveness**: once
in Execute the rotate sensor (`x_RotateSensor`) must *oscillate* or a watchdog
faults the SFC. The round-by-round investigation that drives that oscillation is
implemented; the live blocker is now **upstream in the compass**, not in
investigation (see "The current blocker").

- CLICK project: `C:\Users\ssweb\AppData\Local\Temp\CLICK (00010A00)\pyrung_project`
  (set `PYRUNG_CLICK_PROJECT` to this — some diag scripts still default to a stale path).
- `choice=1` = ProductionMode.
- **Focused drivers (no 7-min cold start), run with `$env:PYRUNG_CLICK_PROJECT` set:**
  - `scratchpad/burner/pilot_rotate_liveness.py` — drive to Execute(6), rotate parked
    False (~815 scans), then `pilot_events` toward `y_BurnerLoop`.
  - `scratchpad/burner/diag_liveness_rounds.py` — same drive, prints each
    `trend_regression`'s liveness hypotheses with CONFIRMED/REJECTED verdicts and
    how `forced_holds[x_RotateSensor]` accumulates polarities across rounds.
  - `scratchpad/burner/diag_letrun_classification.py` — reads the let-run
    ejection straight from the event stream (no monkeypatch).
  - `scratchpad/burner/diag_polarity_resolution.py` — proves the trace_back
    short-circuit (one incident resolves only the currently-wrong polarity).

## Current state (read this first)

The pivot is largely landed across two workstreams that share the tree:

- **Round-by-round liveness (this workstream):**
  - `b856574` `ConditionalHold` carrier (dwell-free, carried as a `forced_holds`
    value, animated per-scan during the coast).
  - `4ed73e4` `when().do()` reactive breakpoint — runner-native, fold-safe
    carrier for reactive holds. Uses `patch` (one-shot), not `force` (a force
    would pin the input so the program could never drift it). Removable via the
    returned `_RunnerHandle`. **Not yet used as the liveness carrier** (see the
    deferred swap below).
  - `f43646b` **observability** — `letrun_ejection` event + enriched
    `zoom_accepted` (`observe_label` / `ejected` / governing tag+value), so the
    ejection is legible without monkeypatching.
  - `e398bed` **entry checkpoint seed** — the first ejection from a
    pre-positioned Execute now has a checkpoint to revert to, so the rotate
    ejection reaches `_investigate_and_revert` (step 5 routing: **done**).
  - **Step 2 — compose/supersede** (`_ops.py::_merge_hold`, `_install_holds`;
    `investigate.py::build_replay_fn`): two `ConditionalHold`s compose **by
    guard** — a new guard *accumulates a polarity*, a same guard *supersedes*
    (latest evidence wins, no dead shadowed rule). `_install_holds` merges rather
    than skipping an already-held tag; replay merges base+hypothesis rule-wise.
  - **Step 3 — new-cause acceptance** (`investigate.py::incident_eject_dones`,
    `build_replay_fn` `eject_cause_dones`; `progress.py` wiring): a governed /
    let-run replay that *silences the incident's watchdog Done but trips a
    different one* is scored as **progress**, not rejected. This lets the
    one-sided round-1 hold survive so round 2 can add the complement.
  - Investigation payload now carries `confirmed_detail` / `rejected_detail`.

- **Accumulator generalization + compass cleanup (other workstream, committed):**
  - `f83155f` generalize accumulator-completion handling beyond timers;
    `accumulators.py` (`iter_profiles` / `resolve_profile` / `scans_to_eject`).
    `_liveness_hypotheses` rewritten into Sub-case A (complement-reset
    oscillation — still emits the single-polarity `ConditionalHold`, the contract
    steps 2+3 rely on), Sub-case B (held-advance → Done "cannot-hold"), Sub-case C
    (`Acc > Target` threshold).
  - `ae391ee` **delete BFS candidate search, make stuck terminal** — removes the
    "wild search masquerading as compassing". The compass now refuses to fabricate
    an edge it cannot read and finishes `stuck: trace_opaque` instead.
  - `559c750` / `71854c7` trace inequality + multi-tag pointer fixes.

## The current blocker (the new frontier)

From a pre-positioned Execute, `pilot_events` now finishes immediately:

```
[scan 815] finished reached=False reason=stuck: trace_opaque
```

The compass bails **before any investigation runs** — so the round-by-round
liveness path (steps 2+3) is correct but **unreachable** in the live burner. The
BFS-search deletion was necessary and good; it exposed that **trace / let-run
cannot surface the rotate Execute→y_BurnerLoop frontier on their own**. The
instrument gap, not the investigation, is what now stops the burner. Closing it
(trace reading the opaque edge, or the terminal let-run being prescribed where
BFS used to paper over) is the prerequisite for validating steps 2+3 end-to-end.

`make test-pilot` currently has ~6 reachability failures (`test_return_early`,
`test_candidate_generation_*`, `test_fill_shape_solves`,
`test_layer2_excursion_recovery`) — all `reachable=False`, fallout of the
`trace_opaque` stuck-terminal change, not of steps 2/3.

## The round-by-round model (implemented)

```
park False → coast → SensorOffWD trips → demand True  (resolvable: currently False)
   replay still ejects on SensorOnWD → NEW cause → accepted as progress (step 3)
drive True → coast → SensorOnWD trips  → demand False (now resolvable: currently True)
   replay merges {True}+{False} by guard → oscillates → no eject → accepted (step 2)
both rules present → sensor oscillates → RunDelay completes → y_BurnerLoop
```

- **Carrier kept as `ConditionalHold`** (not swapped to `.when().do()`). The
  original plan called for retiring the ConditionalHold coast-forcing path for
  the runner-native `.when().do()` reactive hold. That swap was **deferred** —
  fixing composition + acceptance on the existing carrier was the surgical path
  and avoided colliding with the live `_liveness_hypotheses` rewrite. The
  `_coast_holding_state` conditional-coast cannot fold (it steps scan-by-scan
  while a rule fires), so the `.when().do()` swap remains a worthwhile perf/
  cleanliness follow-up.

## Remaining steps

1. **Close the `trace_opaque` instrument gap** (BLOCKING, now top priority).
   Make trace/let-run surface the Execute→`y_BurnerLoop` frontier the deleted BFS
   search used to reach, so the loop enters the terminal let-run → ejection →
   investigation path instead of finishing `stuck`. Until this lands, steps 2+3
   cannot be exercised on the live burner.
2. **Validate steps 2+3 end-to-end** once (1) lands: confirm via
   `diag_liveness_rounds.py` that round 1's one-sided `{True}` hold is CONFIRMED
   (new-cause), round 2 adds `{False}`, `forced_holds[x_RotateSensor]` becomes a
   two-rule oscillation, and the next coast reaches `y_BurnerLoop`.
3. **Rewrite the structural-synthesis tests** to assert round-by-round behavior:
   `tests/core/analysis/test_pilot_investigate.py` (`TestLivenessHypotheses`,
   `TestShaftRotateLiveness`), `tests/core/analysis/test_pilot_ops.py`
   (`TestConditionalHold` + new `_merge_hold` compose/supersede cases). Add a
   `build_replay_fn` test that a new-cause ejection is accepted.
4. **(Deferred) `.when().do()` carrier swap** — retire the conditional coast-
   forcing path for the fold-safe runner-native reactive hold (uses `patch`).
5. **Trim investigation noise** — the rotate regression confirmed ~23 holds,
   mostly `heuristic-upstream` config-tag holds + Sub-case B/C `done-boundary`
   cannot-holds. Audit whether these should install at all; they muddy
   `forced_holds` and may interact with the compass cleanup.

## Rotate watchdog structure (reference — `subroutines/rotate.py` R10–R12)

- **R10 `SensorOnWD`** 2 s, enable `Rotate_CurStep>=3 AND i_RotateSensor`,
  `reset(~i_RotateSensor)` → counts while sensor **True** → demands **False**.
- **R11 `SensorOffWD`** 10 s, enable `Rotate_CurStep>=3`, `reset(i_RotateSensor)`
  → counts while sensor **False** → demands **True**.
- **R12** either Done → `Rotate_Error=2` → ejects Execute→Aborting(8).
- `i_RotateSensor` is the input image of steerable `x_RotateSensor` (identity bridge).
- Both watchdogs expose an `accumulating_profile()`; `resolve_profile(done_name)`
  maps the Done bit back to the owning instruction.

## Key files

- `core/runner.py` — `when().do()`: `_BreakpointBuilder.do`, `_register_breakpoint`
  (action `"do"` + `callback`), `_evaluate_breakpoints`; `_RunnerHandle.remove()`.
- `pilot/accumulators.py` — `iter_profiles`, `resolve_profile`, `scans_to_eject`,
  `AccumulatorMatch` (watchdog/counter/timer profile resolution).
- `pilot/investigate.py` — `_liveness_hypotheses` (Sub-cases A/B/C),
  `incident_eject_dones`, `build_replay_fn` (new-cause acceptance + rule-merge),
  `_resetting_polarity`, `_SnapView`.
- `pilot/_ops.py` — `ConditionalHold` / `_HoldRule` / `_merge_hold` (compose/
  supersede) / `_split_holds` / `_install_holds` / `_coast_holding_state`.
- `pilot/progress.py` — `_monitor_trend` (LETRUN-EJECTION branch, entry-checkpoint
  revert), `_investigate_and_revert`, payload `confirmed_detail`/`rejected_detail`.
- `pilot/candidates.py` — `_STUCK_TRACE_OPAQUE`; the stuck-terminal behavior to
  unblock in step 1.
- `pilot/steer.py` — `_try_terminal_letrun`.
- `pilot/trace.py` — `trace_back`; the opaque-edge reading that step 1 needs.
- `core/fold.py` — fold-safety for reactive holds.

## Diagnostics & tests (this work)

- `pilot_rotate_liveness.py` — focused driver: Execute → pilot → y_BurnerLoop.
- `diag_liveness_rounds.py` — round-by-round CONFIRMED/REJECTED + hold accumulation.
- `diag_letrun_classification.py` — let-run ejection from the event stream.
- `diag_polarity_resolution.py` — proves the trace_back short-circuit.
- `tests/core/test_breakpoints_labels.py` — `.do()` tests incl. fold-correctness.
- `tests/core/analysis/test_pilot_ops.py` — `TestConditionalHold` (+ `_merge_hold`).
- `tests/core/analysis/test_pilot_investigate.py` — `TestLivenessHypotheses`,
  `TestShaftRotateLiveness`. **Rewrite for round-by-round (step 3 above).**

## Click state encoding
0 undefined, 1 CLEARING, 2 STOPPED, 3 STARTING, 4 IDLE, 5 SUSPENDED,
**6 EXECUTE**, 7 STOPPING, 8 ABORTING, 9 ABORTED, 10 HOLDING, 11 HELD, …
Startup: `9→(Clear)1→2→(Reset)15→4→(Start)3→(coast)6`.

---

# Frozen-rung write exclusion (time folding optimization)

## What was built

**`_frozen_rung_writes()`** in `fold.py` (section 6) — fixed-point reachability
analysis that identifies tags written exclusively by rungs whose inputs can't
vary during a plateau. Added `frozen_writes` field to `_FoldContext`, wired into
the `exclude` set in both `fold_run_until` and `fold_run_for`.

Files changed:
- `src/pyrung/core/fold.py` — new field + function + context wiring + exclude set
- `tests/core/test_fold.py` — 6 unit tests in `TestFrozenRungWrites`
- `tests/fuzz/test_fold_soundness.py` — new fuzzer `test_frozen_write_exclusion_preserves_non_frozen_tags`

All existing tests pass (4549). Frozen-write fuzzer passes at 500 examples.

### Algorithm

Seed `varying` with base tags that change during plateaus (acc_names,
modwrap_names, mirror_names, profile_fb_names, churn_excluded). Propagate
through PDG rung read→write edges until stable. Tags written only by rungs
outside the reachable cone are frozen. System clocks / resolved-on-read names
are stripped from rung reads (they don't appear in state.tags).

## Pre-existing sat_clock + OTE bug found by fuzzer

The **existing** `test_saturated_heartbeat_fold_is_bit_equal` fuzzer found a
disagreement. `frozen_writes` is empty for this program — the bug is unrelated.

Reproducer:
```python
spec = {"src": "counter", "preset": 1676, "threshold": 1,
        "form": "gt", "clock": "clock_500ms", "body": "coil",
        "dt": 0.01, "a": 0, "b": 0}
# Beat = True (fold) vs False (no-fold)
```

### What's already in place (and still broken)

Two mechanisms already try to keep `_prev:clock` correct across fold jumps:

1. **`_scans_to_clock_edge`** (`fold.py:1218`) — `ceil(raw - eps) - 1` to land
   strictly before the edge. Comment says "using floor lands ON the edge which
   misses pulse outputs and leaves stale _prev."

2. **`_capture_previous_states`** (`runner.py:2598`) — resolves _prev:clock at
   `prior_clock_ts = ctx.timestamp + dt - self._dt` (synthetic "one normal scan
   before landing"). Uses `(int(ts / hp) % 2) == 1`, same formula as runtime
   `resolve()` in `system_points.py:394`.

Despite both, the OTE coil (`out(Beat)`) ends up in a different final state.

### Where to look next

The bug lives in the sat_clock soft-promotion → inertness confirmation path.
Once `Ctr_Acc > 1` saturates (scan 2), `_runtime_soft_clocks` promotes
`clock_500ms` to soft. Somehow inertness gets confirmed even though rising
edges should clear `inert_run` (Beat toggles True→False→True).

- Instrument `_mark_inert_soft` and the post-fold visibility check
  (`fold_run_until` ~line 1466) to trace when `inert_run` increments vs
  clears for `sys.clock_500ms`.
- Check whether clock phase formula `(int(ts/hp) % 2) == 1` agrees between
  `_capture_previous_states` (line 2629) and `resolve()` (line 396) at the
  exact timestamps for this spec.
- The "keep 2 scans" idea: `_scans_to_clock_edge` already subtracts 1, and
  `_capture_previous_states` already synthesizes _prev. If both work, the bug
  is elsewhere — possibly in how `_mark_inert_soft` counts toggles when the
  probe itself straddles an edge.

## Next: macro-skip in pilot (proposal 2)

With frozen-rung detection in place, the pilot can macro-skip entire
timer/counter dwells proven solvable. If all intervening rungs are frozen given
current holds, skip the entire `_coast_to_value` and jump directly to the
accumulator crossing. Lives in `steer.py` (`_letrun_zoom` / `_try_zoom`),
keyed on `(state_key_at_entry, governing_tag, target_value)`.
