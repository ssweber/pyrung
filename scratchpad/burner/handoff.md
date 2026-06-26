# Burner PILOT handoff — driving to Execute, then y_BurnerLoop

Goal: `pilot_how(plc, y_BurnerLoop, choice=1)` reaches **Execute(6)** then
`y_BurnerLoop=True` on the real Click burner program.

- CLICK project: `C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project`
- Driver / event stream: `scratchpad/burner/sample_pilot_events.py`
- `choice=1` = ProductionMode.

## Where it stands — REACHED ✅ (cold ManualMode/Aborted → y_BurnerLoop)

`pilot_how(y_BurnerLoop, choice=1)` synthesizes the whole journey itself from a
cold start — `S_StateCurrent=9` (ABORTED), `S_UnitModeCurrent=3` (Manual), no
inputs pre-forced — driving trace distance 21 → 0 in 8 steps:

| # | action | effect |
|---|--------|--------|
| 1 | `C_Clear` + `C_UnitModeChgRequest` (bundled pulse) | Aborted→Stopped(2); **Manual→Production** (`S_UnitModeCurrent 3→1`) |
| 2 | `C_Reset` | Stopped→Idle(4) |
| 3 | `C_Start` | Idle→Starting(3) |
| 4 | coast (let-run) | timer-gated SFC step-counters advance |
| 5 | `C_Start` | re-assert |
| 6 | coast (let-run) | **→ Execute(6)** |
| 7 | terminal let-run | ejects on `Rotate_Error=2` watchdog (Execute→Aborting) |
| 8 | re-coast w/ `x_RotateSensor` liveness | **→ `o_BurnerLoop` → `y_BurnerLoop=True`** |

The headline: it did *not* know about the rotate watchdog up front. It drove
until the program threw it to Aborting, did a bounded post-mortem over the coast
span (Fix A), replay-confirmed that `x_RotateSensor` must oscillate (~100 scans),
and came back to finish. Verified in `scratchpad/burner/_fixA_final.log`
(`reached: True`, scan ~811 `trend_regression`, 1 of 32 hypotheses confirmed).
83 pilot tests pass.

## What landed (committed on `dev`)

- **`27372c1` feat(pilot): investigate terminal-letrun ejection over the
  coast-span window.** A terminal let-run that ejected was being checkpointed as
  *progress* (the Aborting branch has a misleadingly-low trace distance), so it
  was never investigated; and the watchdog that ejected fired mid-coast, so the
  post-eject window missed it. Now `_monitor_trend` routes a terminal-letrun
  `AMBIENT_DRIFT` ejection to `_investigate_and_revert` with the coast-span
  window (`trial.scan_before → fork end`) instead of checkpointing it. Shared
  investigate-and-revert body factored out so the regression path and the
  ejection path parameterize the incident window.
- **`dd80816` perf(cause): make historical-state replay slab reach folded tail
  scans.** `cause()` over the ~1300-scan folded ejection history was ~30s; this
  is what made the live investigation time out. Folding leaves checkpoints
  sparse/irregular, so the replay slab capped one interval past the anchor and a
  tail scan was never cached — every `at()` re-replayed the same interval and
  then replayed to the tail anyway. Fixes: slab end always covers the requested
  scan; one slab per anchor (LRU) across a backward walk; `_replay_capture_at`
  positions at `target-1` via compiled `replay_to` and interprets only the final
  view-capturing scan. **30.5s → 10.5s (~3×)**, chain output byte-identical.

## Earlier session (already on `dev` before this one)

- Gate fix (`outcome.py`/`verify.py`): governing-reached → CONFIRMED,
  governing-moved → AMBIENT_DRIFT (makes leg 1 stick).
- Generalized terminal let-run (`steer._try_terminal_letrun`,
  `_ops._coast_holding_state`): hold every pipeline-role governing tag, coast to
  the global target; `letrun_tried` guard prevents re-coast hangs.
- `LivenessHold` (`_ops`) + `investigate._liveness_hypotheses`: complement-reset
  watchdog detection → toggling hold; replay terminal-letrun mode.

## Open design-intent — cause()-driven liveness detection (kill the replay storm)

The investigation is correct and reaches the target, but the post-mortem is a
**shotgun**: `investigate_deviation` generated **32 hypotheses (1 confirmed, 31
rejected)** for the rotate ejection, and every rejected one costs a replay coast.
Now that `cause()` per call is ~10s (was 30s), that replay storm — not `cause()`
itself — is the dominant wall-clock cost of a run (~6–7 min).

Most of the 31 are noise from breadth-first heuristics: `_upstream_hypotheses`
sweeps the whole upstream cone of the departed register at **both polarities**,
and `_cause_hypotheses` recursively chases `chase_cause_roots`.
`_liveness_hypotheses` itself is structural (no `cause()`) and keys off the
`changed_tags` history window.

**Intent: drive liveness detection from `cause()` walked from the ejection
register**, not from the `changed_tags` window or the upstream shotgun.
`cause(Rotate_Error)` already returns the exact chain
`Rotate_Error ← Rotate_SensorOffWD_tmr_Done ← … ← x_RotateSensor` (verified
byte-identical pre/post the slab fix). Walking that one chain back to the
physical reset-input driver yields the single targeted hypothesis directly, so
the investigation proposes the liveness hold (and only it) without enumerating —
and replay-verifying — 31 dead ends.

**Keep the shotgun as a fallback tier, not a removal.** This is the PILOT
escalation rule ("read first, execute only when reading isn't enough") applied to
the post-mortem:

1. **Precise pass** — `cause()` from the ejection register → reset-input driver →
   one targeted liveness hypothesis → replay. Confirms → done, no shotgun.
2. **Fallback to the breadth-first heuristics** (`_upstream_hypotheses` /
   `_cause_hypotheses`) — only when the precise pass dead-walls: `cause()`
   returns `None`, the chain stops at an opaque/indirect writer `trace_back`
   can't follow back to a steerable input, or the targeted hypothesis is
   *rejected on replay* (real chain, wrong hold). Lazy escalation: pay for the
   replay storm only when the cheap precise read genuinely fails.

- This is the original handoff's "Fix B", now unblocked: the reason it was
  shelved (cause() over the ejection was too slow) is gone.
- Benefits from the recent `cause()` correctness work (indirect-copy,
  consumed-within-scan) that makes the watchdog→input chain reliable.
- Net: precision (right hypothesis first) **and** speed (no replay storm in the
  common case) — the single biggest remaining win for run wall-clock,
  independent of the harness work below. Robustness is unchanged: the shotgun
  still catches everything it does today, just lazily.

## Open design-intent — replace `LivenessHold` with a Harness oscillator

`LivenessHold` is a stopgap: a `(tag, value)` hold whose value is a
`LivenessHold(on_dwell, off_dwell)` sentinel that `_install_holds` skips and
`_coast_holding_state` animates by **manually single-stepping + `plc.force()`**
each scan. It works but it is bespoke pilot plumbing that duplicates what the
feedback Harness already does.

**Intent: make the oscillation a first-class extension of the bool feedback
Harness, gated by a program bit, so the coast just `run_until`s.**

- **`Physical` (`core/physical.py`)** — add `on_dwell` / `off_dwell` duration
  fields next to `on_delay` / `off_delay`. Semantics (user-specified): on the
  enable bit's rising edge, wait `on_delay`, turn the feedback ON, then
  oscillate (`on_dwell` ON / `off_dwell` OFF) for as long as the enable holds;
  on the enable's falling edge, wait `off_delay`, turn OFF. (Today's
  `on_delay`/`off_delay`-only coupling is the dwell-less special case.)
- **`Harness` (`core/harness.py`)** — `_BoolCoupling` carries the dwell; the
  existing per-scan `_on_pre_scan` hook (fires in `_prepare_scan` via
  `_pre_scan_callbacks`) drives the square wave while the enable is active. The
  enable is "whatever BIT you are using" — e.g. the rotate-active output or the
  watchdog timer's EN — so the sensor pulses exactly when rotation is expected.
- **Pilot** — `_liveness_hypotheses` registers such a coupling on the fork's
  harness (link `x_RotateSensor` → rotate-active bit, dwell = min watchdog
  preset / 2) instead of synthesizing a `LivenessHold`; `_coast_holding_state`
  drops its single-step liveness branch and just coasts. `_split_holds` /
  `_install_holds` / the replay terminal-letrun liveness path retire.

### The load-bearing subtlety: fold vs the scheduled toggle

`run_until(fold=True)` will **overshoot the toggle**. The fold folds to the
nearest crossing it knows about — the watchdog timer's done-bit crossing — and a
folded jump does not run `_prepare_scan`, so the harness never re-asserts the
toggle that resets the watchdog. It trips. This is the real reason the current
code single-steps (it is *not* that "folding freezes the input" — the input is
fine; the fold jumps **past** the scheduled toggle).

Two landing spots:

1. **Clean now:** harness drives the oscillation, coast uses
   `run_until(..., fold=False)`. Correct and first-class, removes the manual
   single-step/force branch. Same per-scan cost as today (no fold speedup — and
   that is fine; the live-investigation timeout was `cause()`, now fixed).
2. **Fast + deeper:** teach the fold to treat the next pending harness patch
   (`Harness._heap` head `target_scan`) as a fold ceiling, so it folds each
   constant dwell and steps only the toggles. Genuinely fast, but a fold-engine
   change in `core/fold.py`.

Recommend (1) first, (2) as a follow-up.

## Key files

- `pilot/pilot.py` — drive loop; terminal-letrun fallback + `letrun_tried` guard.
- `pilot/steer.py` — `_try_terminal_letrun`, `_letrun_zoom`, `_try_zoom`.
- `pilot/_ops.py` — `_coast_holding_state`, `_coast_to_value`, `LivenessHold`,
  `_split_holds`, `_install_holds`.
- `pilot/progress.py` — `_monitor_trend` + `_investigate_and_revert` (Fix A).
- `pilot/investigate.py` — `_liveness_hypotheses`, `build_replay_fn`.
- `pilot/physical.py` — `install_harness` (where the harness coupling registers).
- `core/harness.py` / `core/physical.py` — the Harness + Physical to extend.
- `core/runner.py` — `_replay_slab_fill`, `_state_at`, `_replay_capture_at`
  (the cause() perf fixes).
- Diagnostics: `diag_zoom_endstate2.py`, `diag_liveness_incident.py`,
  `prof_cause.py` / `instr_slab.py` (cause() perf), `repro_cause_slow.py`.

## Click state encoding
0 undefined, 1 CLEARING, 2 STOPPED, 3 STARTING, 4 IDLE, 5 SUSPENDED,
**6 EXECUTE**, 7 STOPPING, 8 ABORTING, 9 ABORTED, 10 HOLDING, 11 HELD, …
Startup: `9→(Clear)1→2→(Reset)15→4→(Start)3→(coast)6`.
