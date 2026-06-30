# Burner PILOT handoff — rotate-sensor liveness (SOLVED 2026-06-29)

## Where we are

`pilot_how(plc, y_BurnerLoop, choice=1)` reaches Execute(6) then `y_BurnerLoop=True`
on the regenerated Click burner — **end to end, both cold and pre-positioned**. The
last mile is liveness: in Execute the rotate sensor (`x_RotateSensor`) must oscillate
or a watchdog faults the SFC. The `trace_opaque` blocker is gone; the round-by-round
oscillation investigation drives the SFC all the way to the target.

This doc is a **regression anchor** for the pilot refactor/cleanup: the two acceptance
runs below are how you'll see whether a change broke the burner.

## Acceptance runs — re-run after any pilot change

- CLICK project: `C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project`
  (regenerated 2026-06-29). `choice=1` = ProductionMode. All three burner drivers
  (`reconstitute_y_burnerloop_steps.py`, `pilot_rotate_liveness.py`,
  `sample_pilot_events.py`) default to this path now; override with
  `$env:PYRUNG_CLICK_PROJECT` when the project is regenerated to a new temp folder.

| Driver | Start | Expected (currently passing) |
|---|---|---|
| `pilot_rotate_liveness.py` | pre-positioned Execute(6), rotate parked False (~scan 815) | `finished reached=True`, **3 steps**, ~scan 2016 |
| `sample_pilot_events.py` | cold (scan 1) | `finished reached=True`, **9 steps**, ~scan 2011 |

**The shape that must survive a refactor** (both drivers, identical at the rotate frontier):

1. Reach Execute. Cold path: `Clear → Reset → Start` route (S_StateCurrent 9→2→4→3) with
   a `latch-exposure` detour clearing the door/lint alarm latches
   (`x_DoorClosed`/`x_LintDoorClosed`), then a let-run zoom coasting Starting→Execute (3→6).
2. Terminal let-run coasts and **ejects on the rotate watchdog** (Execute→Aborting(8)).
3. **Round 1** — `trend_regression`, liveness hold `x_RotateSensor` oscillate→**True**
   CONFIRMED (ejects `SensorOffWD`). The single-polarity hold survives via new-cause
   acceptance. `hyps=1 confirmed=1`.
4. **Round 2** — `trend_regression`, both a precise-cause `x_RotateSensor=False` and a
   liveness oscillate→**False** CONFIRMED (ejects `SensorOnWD`). The two `ConditionalHold`
   rules **compose by guard** into a two-polarity oscillation. `hyps=2 confirmed=2`.
5. Sensor oscillates → `Heat_CurStep` advances → terminal let-run coasts to
   `y_BurnerLoop=True` (S_StateCurrent stays 6).

If a run finishes `reached=False` (especially `stuck: trace_opaque`), or the step count /
round count / hypothesis verdicts differ from the above, **that's the regression** — diff
from the first event whose shape changed. Deeper views when a run regresses:
`diag_liveness_rounds.py` (round-by-round verdicts + `forced_holds` accumulation),
`diag_letrun_classification.py` (let-run ejection), `diag_polarity_resolution.py`
(trace_back short-circuit).

## What the solve leans on — touch these → re-run acceptance

- **Trace reads opaque edges** (inequality + multi-tag pointers) via the Tier-1
  `nd_domains` / functional-dep `DomainPrior` in `trace_back`. Without it the rotate
  frontier bails `trace_opaque`.  → `pilot/trace.py`
- **State-consistent writer selection** (`isStateEnbl_Yes`, `S_StateCompleteBool`): trace
  through the writer whose guard matches the held state, not the fewest-leaves one.
- **Round-by-round liveness**: single-polarity `ConditionalHold` + compose-by-guard
  (`_merge_hold`) + new-cause acceptance (`build_replay_fn`).  → `pilot/investigate.py`,
  `pilot/_ops.py`
- **Terminal let-run → ejection → investigation handoff.**  → `pilot/steer.py`
  (`_try_terminal_letrun`), `pilot/progress.py` (`_monitor_trend` LETRUN-EJECTION branch)
- **Accumulator profile resolution** (watchdog `Done` → owning instruction).
  → `pilot/accumulators.py`
- **Carrier is `.when().do()` (patch, not force)** — the reactive-hold swap landed
  (2026-06-30). `_coast_holding_state` installs one `when(rule active).do(patch)` per
  conditional hold and folds (`fold=True`): an oscillating hold patches a *visible* flip
  every scan, so there is no plateau to skip, while a dormant single-polarity hold emits
  no change and folds its dwell soundly. The winning terminal let-run `_Step` records its
  `reactive_holds`, so the recorded path replays the coast with pyrung primitives alone.

## Key files

- `pilot/trace.py` — `trace_back`; opaque-edge reader (inequality + multi-tag pointers).
- `pilot/candidates.py` — `_STUCK_TRACE_OPAQUE`; honest terminal for genuinely opaque
  edges (no longer fires on the burner).
- `pilot/steer.py` — `_try_terminal_letrun`; terminal coast that ejects on the watchdog.
- `pilot/progress.py` — `_monitor_trend` (LETRUN-EJECTION branch, entry-checkpoint revert),
  `_investigate_and_revert`.
- `pilot/investigate.py` — `_liveness_hypotheses` (Sub-cases A/B/C), `incident_eject_dones`,
  `build_replay_fn` (new-cause acceptance + rule-merge).
- `pilot/_ops.py` — `ConditionalHold` / `_HoldRule` / `_merge_hold` / `_install_holds` /
  `_install_reactive_holds` / `_coast_holding_state`.
- `pilot/accumulators.py` — `iter_profiles` / `resolve_profile` / `scans_to_eject`.
- `pilot/corrections.py` — `correct_enablers` (the two enabler-correction passes).
- `core/runner.py` — `when().do()` reactive breakpoint + `patch` (the reactive-hold carrier).
- `core/fold.py` — fold-safety for reactive holds.

## Rotate watchdog structure (reference — `subroutines/rotate.py` R10–R12)

- **R10 `SensorOnWD`** 2 s, enable `Rotate_CurStep>=3 AND i_RotateSensor`,
  `reset(~i_RotateSensor)` → counts while sensor **True** → demands **False**.
- **R11 `SensorOffWD`** 10 s, enable `Rotate_CurStep>=3`, `reset(i_RotateSensor)`
  → counts while sensor **False** → demands **True**.
- **R12** either Done → `Rotate_Error=2` → ejects Execute→Aborting(8).
- `i_RotateSensor` is the input image of steerable `x_RotateSensor` (identity bridge).
- Both watchdogs expose `accumulating_profile()`; `resolve_profile(done_name)` maps the
  Done bit back to the owning instruction.

## Open follow-ups (not regressions)

1. **Rewrite the structural-synthesis tests** to assert round-by-round behavior:
   `test_pilot_investigate.py` (`TestLivenessHypotheses`, `TestShaftRotateLiveness`),
   `test_pilot_ops.py` (`TestConditionalHold` + `_merge_hold` compose/supersede). Add a
   `build_replay_fn` test that a new-cause ejection is accepted.
2. **DONE 2026-06-30 — `.when().do()` carrier swap.** Retired the conditional coast-forcing
   path for the fold-safe runner-native reactive hold (`patch`, not `force`); the winning
   terminal let-run `_Step` now records `reactive_holds`, so the path is self-describing
   (`verify_path_recording.py` replays the coast from the step alone → `y_BurnerLoop=True`).
   *Still open:* `state.steps` is an **attempt log**, not a clean sequential path — reverted
   let-run attempts linger (pre-positioned records 3 steps; only the last is the real path,
   the first two are reverted rounds with overlapping ~815 spans). Truncating reverted steps
   at the three checkpoint-revert sites (`progress.py:276`, `pilot.py:837`, `pilot.py:1024`,
   via one `_revert_to_checkpoint` helper using the existing `replay_steps` filter
   `scan_before >= cp_fork.scan_id`) would make the list sequentially replayable and shift
   the anchors to 1 / 7. Deferred pending a check that no consumer (DAP, `how()` Path) reads
   dead-ends out of `steps`.
3. **Trim investigation noise** — the rotate regression confirmed ~23 holds, mostly
   `heuristic-upstream` config-tag holds + `done-boundary` cannot-holds. Audit whether they
   should install at all.
4. **Diag-script drift** — `sample_pilot_events.py`'s printer was fixed this pass (the
   `candidates_built` payload dropped `upstream_candidate_count` / `influence_candidates`).
   Other diag scripts that print those keys will crash the same way.
5. **Macro-skip in pilot** — see the proposal at the end of this doc.

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

## Pre-existing sat_clock + OTE bug found by fuzzer — RESOLVED 2026-06-29

The **existing** `test_saturated_heartbeat_fold_is_bit_equal` fuzzer found a
disagreement. `frozen_writes` is empty for this program — the bug was unrelated
to the frozen-write work.

Reproducer (now a permanent `@example` on the heartbeat fuzzer):
```python
spec = {"src": "counter", "preset": 1676, "threshold": 1,
        "form": "gt", "clock": "clock_500ms", "body": "coil",
        "dt": 0.01, "a": 0, "b": 0}
# was: Beat = True (fold) vs False (no-fold)
```

### Root cause — floating-point clock-boundary straddle (NOT the inert path)

The sat_clock/inert machinery was a red herring. The counter's `Done` (1676
counts) is evaluated at `t = 16.75 s`, which is **exactly a `clock_500ms` rise
edge** (`16.75 = 0.25 + 0.5×33`). System clocks are step functions
`int(t / half_period) % 2` of a float timestamp that is meant to sit on the dt
grid:

- **nofold** accumulates `+dt` ×1675 → `t = 16.74999999999982` →
  `int(66.9999…) = 66` → clock **low** → no rise → `Beat = False`.
- **fold** reconstructs `t` via big `skip×dt` jumps → `t = 16.75` exactly →
  `int(67.0) = 67` → clock **high** → rise → `Beat = True`.

Two arithmetic paths landing on opposite sides of the same boundary. (`Beat` is
an OTE, so only the final scan matters — dropped intermediate rises are
irrelevant.)

### Fix — one grid-snapped clock-phase primitive, used at all four sites

`system_points.clock_phase(t, hp) = floor(t / hp + _CLOCK_SNAP_EPS)` (and
`clock_high`), `_CLOCK_SNAP_EPS = 1e-7`. The epsilon snaps a timestamp on (or a
rounding step below) an exact `k·hp` boundary to phase `k`, far above realistic
drift (~1e-13) yet far below the smallest per-scan ratio step `dt/hp`. Replaced
the duplicated ad-hoc `int(t/hp)` at all four sites so the fold's edge math
agrees with the resolved clock by construction:

- `system_points.resolve()` (clock value)
- `runner._capture_previous_states()` (`_prev:clock` synthesis)
- `fold._scans_to_clock_edge()` (next-edge gap)
- `fold._mark_inert_soft()` (toggle counting)

Validation: reproducer now agrees; full suite 4555 passed; soundness fuzzer
green; ruff/ty clean.

Related-but-separate (left untouched): `history.at_or_before_timestamp` does the
inverse `int(timestamp/dt)` map with the same drift fragility, but it's a
user-facing seek, not part of the fold contract.

## Next: macro-skip in pilot (proposal 2)

With frozen-rung detection in place, the pilot can macro-skip entire
timer/counter dwells proven solvable. If all intervening rungs are frozen given
current holds, skip the entire `_coast_to_value` and jump directly to the
accumulator crossing. Lives in `steer.py` (`_letrun_zoom` / `_try_zoom`),
keyed on `(state_key_at_entry, governing_tag, target_value)`.
