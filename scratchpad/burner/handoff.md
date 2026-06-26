# Burner PILOT handoff — investigation finds the door

Goal: `pilot_how(plc, y_BurnerLoop, choice=1)` reaches **Execute(6)** on the real
Click burner program.  Acceptance bar: Starting(3) → Execute(6).

- CLICK project: `C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project`
- Diagnostic: `scratchpad/burner/sample_pilot_events.py`
- `choice=1` = ProductionMode.

## What now works (this session)

The pilot reaches Starting via C_Start, zooms toward Execute, and the zoom
ejects to **Aborting(8)** because the door/lint alarms latch on entering
Starting with the doors open.  **Investigation now correctly identifies the
fix**: hold `x_DoorClosed=True` AND `x_LintDoorClosed=True` together.

`trend_regression` event:
```
investigation:
  hypotheses: 28
  confirmed: 1
  [latch-exposure] x_DoorClosed=True, x_LintDoorClosed=True
    clear 2 active latches: A_Alm14_DoorOpen_Trig, A_Alm15_LintOpen_Trig
```
The two single-input holds are correctly *rejected* (neither alone reaches the
corridor — both alarms feed `A_AlmExtent`); only the conjunction is confirmed.

## The core reframing

PILOT's investigation was built entirely for **regression** ("a tag that *was*
good went bad → revert it to its from_value").  The door is a different failure
mode: an **unmet precondition** ("a guard was never satisfied; a latent latch
finally bit").  There is no transition to revert — the fix is to *establish*
the precondition (flip a steady input to its corrective value).  The three
investigation tiers were all regression-shaped and blind to this; the fixes
below make the unmet-precondition mode first-class.

## What changed (landed)

### investigate.py
- **heuristic-upstream now flips.** The old `if/else` was dead code (both
  branches proposed `before_val`, the steady-broken value).  For boolean cone
  inputs it now proposes *both* polarities; the corrective flip is what restores
  a never-satisfied precondition.
- **latch-exposure rewritten** as the targeted mechanism: find latches that are
  *active* (True after the regression) and *gated by a state we were already in*
  (True in `before_snap`) — i.e. they latched because we entered that state.
  Resolve each non-state guard tag to its steerable driver via `trace_back`
  (bridges the `i_DoorClosed` PIVOT → physical `x_DoorClosed`), and propose the
  **conjunction** of all active-latch guard holds (plus per-latch singles).
  - Condition tags come from `pdg.rung_nodes[ri].condition_reads` (subroutine-
    aware), NOT `ro.sp_tree().tag_names` — SPNode has no `tag_names` accessor,
    so the old code silently read an empty set and the tier *never fired*.
    `sp_tree()` itself is fine.
- **build_replay_fn re-zooms.** For a zoom incident it replays the recorded
  command steps then re-zooms under the proposed holds, accepting iff the
  governing register reaches its corridor target (vs ejecting) — the right
  question, instead of comparing a Starting-corridor trend against the
  pre-corridor checkpoint.

### _ops.py + steer.py
- Extracted **`_coast_to_value`** (run_until target, pause-guard on ejection,
  fold) into `_ops.py`.  The live zoom (`steer._letrun_zoom`) and the replay now
  coast identically, so a replay faithfully reproduces the live zoom.
  `_ZOOM_BUDGET` moved to `_ops.py`.

### progress.py
- Passes `trial.zoom_governing_tag` / `zoom_target_value` into `build_replay_fn`.

### candidates.py
- Replaced an ineffective `# type: ignore[union-attr]` (mypy-style; ty ignores
  it) with `assert route_plan is not None` in the zoom branch.

## Program fix by user (not pilot)
`C_ForceClear` used to reach Execute spuriously (force-clearing alarms); the
user guarded it `with rung(C_ForceClear, S_Clearing):` so it's no longer a
degenerate "fix" the investigation could latch onto.

## What's next (the new frontier)

With the doors held, C_Start reaches Starting(3) at trend 23, but the next zoom
`S_StateCurrent 3→6` is **`zoom_rejected: dead-end (empty frontier, no pending
effects)`**.  This is the *downstream* completion frontier — `still_need` shows
`Blower__init=1`, `Rotate__init=1`, `Blower_CurStep`, etc.  Starting→Execute is
the ~1400-scan SFC step-counter completion; the zoom isn't finding the
self-advancing frontier to coast.  That is the next thing to chase, separate
from the door investigation.

## Known pre-existing debt (NOT touched)
`make lint` is red independent of this work:
- ty self-type false-positives ("Self/PLC not assignable to
  `pyrung.core.runner.PLC`") across ~20 call sites codebase-wide (runner,
  history, fold, query, validate, simplified_forms…).  Suppressing with
  `# ty: ignore` is unstable — ty's unused-ignore detection differs between
  single-file and whole-project analysis.  Left alone.
- Deprecated walk tests pass removed `how()` kwargs (`walk_seconds`,
  `max_steps`, `unlink`).
The pilot side is ty-clean (candidates.py fixed).

## Key files
- `investigate.py` — hypotheses (recorded-cause / latch-exposure / heuristic-
  upstream), replay harness.
- `_ops.py` — `_coast_to_value`, holds, pulse, state-key, settle.
- `steer.py` — zoom (`_letrun_zoom` → `_coast_to_value`), pulse, candidates.
- `progress.py` — trend monitor, checkpoints, regression → investigation.
- `trace.py` — backward trace; `trace_back(...).steerable_leaves()` is the
  PIVOT→physical bridge latch-exposure uses.
- Diagnostics: `sample_pilot_events.py`, `diag_cause_instrument.py` (recursive
  cause dump — shows the chain severs at `A_Alm14_Status`, an indirect
  `copy(..., ds[idx])` cause() can't attribute), `diag_incident_source.py`
  (spies the incident + replay verdicts).

## Click state encoding
0 undefined, 1 CLEARING, 2 STOPPED, 3 STARTING, 4 IDLE, 5 SUSPENDED,
**6 EXECUTE**, 7 STOPPING, 8 ABORTING, 9 ABORTED, 10 HOLDING, 11 HELD,
12 UNHOLDING, 13 SUSPENDING, 14 UNSUSPENDING, 15 RESETTING, 16 COMPLETING,
17 COMPLETED.  Startup: `9→(Clear)1→2→(Reset)15→4→(Start)3→(wait)6`.
