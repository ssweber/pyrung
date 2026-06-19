# Burner walk handoff

Goal: `PLC(logic).how(y_BurnerLoop)` should reach `y_BurnerLoop`. The CLICK
project under test: `C:\Users\Sam\AppData\Local\Temp\CLICK (00010A66)\pyrung_project`.
Ground truth: `uv run python scratchpad/burner/reconstitute_y_burnerloop_steps.py`
(permissives + commands + ~2000-scan wait, `y_BurnerLoop` at scan ~2016).

## Current state (2026-06-18, session 3)

- `make test-walk`: green (294 passed, 1 xfailed).
- Burner probe: `reachable=False`, budget exhausted (1104 forks, 1408 scans, 90s).
  Down from ~2015 forks at session start.
- The cause chain now traces the 6→9 regression correctly (29 steps, through
  subroutine boundaries, names `C_Abort=True` as the trigger).
- **Current blocker**: the oracle decomposes `HeatDelay_Tmr_Done` incorrectly —
  it asks for `S_Execute=False`, which sabotages EXECUTE.

## Commits on dev (newest first, this session)

- `31dce00` fix(causal): cross adjacent-scan consume and fix subroutine cycle guard
- `0617bc9` feat(walk): stabilisation sweep for non-self-sustaining regressions
- `53a59d3` feat(walk): instrument explore/fold/steer pipeline with debug events
- `80bb7dc` fix(walk): mark unprotectable regressions to stop empty hold-mining thrash

## What each fix did

**Unprotectable thrash guard** (`80bb7dc`): when `mine_regression_holds` and the
counterfactual sweep both return `[]`, the regression is marked unprotectable and
skipped on subsequent frames. Eliminated ~1934 empty re-mining attempts.

**Debug instrumentation** (`53a59d3`): added events to explore.py, fold.py,
steer.py (explore-corridor/exit/steer/divest, fold-done/bail/react, steer-apply,
governing-selected). Revealed that the walker reaches EXECUTE via command pulses
(1 scan each), not via time-folding through Starting.

**Stabilisation sweep** (`0617bc9`): when the counterfactual sweep's baseline
settle already breaks the goal (anchor not self-sustaining), the inverse mode
tries holding each candidate at its alternative value. Currently finds
`C_Hold=True, C_Suspend=True` (freeze commands — correct but over-protective).
Fires as a fallback on second/third regressions.

**Adjacent-scan consume + cycle guard** (`31dce00`): two bugs in recorded cause:
1. The read-diff window `[end-of-(N-1), fire-time-at-N]` missed operands written
   at N-1 and consumed at N (both endpoints had the same value). Now widens to
   N-2 when the operand is consumed (fire-time ≠ end-of-scan).
2. The backward walk's visited set was keyed on tag name only, so
   `S_StateRequested` at scan 9 blocked re-entry at scan 8. Now keyed on
   `(tag_name, scan_id)`.

Together: `cause(S_StateCurrent)` at the regression scan now traces 29 steps
through `sm_CopyOrJumpState` → `sm_CtrlCmd2StateRequest` → `C_Abort=True`.

## How the walker works on the Burner

**Steer alphabet**: S_StateCurrent has 23 Bool inputs in its upstream cone.
Command tags (`C_Clear`, `C_Reset`, etc.) have `external=False` but no PDG
writers, so the walker classifies them as inputs (`TagRole.INPUT`). Physical
permissives (`x_DoorClosed`, etc.) have `external=True`.

**Corridor**: the BFS reaches EXECUTE (state 6) by pulsing commands:
`0→9→2→4→6`, each transition in 1 scan. No fold through Starting.

**Hold extraction**: `_commit_holds` extracts `C_Clear=True, C_Reset=True,
C_Start=True` from the corridor path. Permissives don't participate.

**First regression** (41s): oracle decomposes `HeatDelay_Tmr_Done` and mines
`S_Execute=False` as a sub-goal. Walker achieves it by holding `C_Abort=True`.
C_Abort kills EXECUTE (6→9). `mine_regression_holds` now finds `C_Abort` as
the transitioned root (cause-chain fix). Regression is handled, not thrashed.

**Second/third regressions** (57s, 77s): after the walker avoids C_Abort, the
stabilisation sweep fires and finds `C_Hold=True, C_Suspend=True`. These freeze
the state machine — goal preserved but downstream progress blocked.

**Final state**: `HeatDelay_Tmr_Done` fails, budget exhausted.

## The next blocker: oracle decomposition

The oracle (`_recovery_goals` / `projected_cause`) says `HeatDelay_Tmr_Done`
needs `S_Execute=False`. This is wrong — Heat runs WHILE in EXECUTE. The timer
is gated by `S_Execute=True`, not False. The oracle is likely confusing a
condition-level contact (`S_Execute` read in the timer's condition) with a
prerequisite direction.

**EXECUTE is stable without permissives** for 200+ scans (verified by replaying
the walker's path and stepping). The alarms fire (`A_AlmExtent=3` at scan ~68)
but don't immediately trigger an abort from EXECUTE. The regression is entirely
self-inflicted by the walker following the oracle's bad `S_Execute=False` advice.

To confirm: `cause(HeatDelay_Tmr_Done)` or `why(HeatDelay_Tmr_Done)` from
EXECUTE should show what the oracle sees and why it concludes `S_Execute=False`.

## Reconstitute vs walker gap

| Step | Reconstitute | Walker |
|------|-------------|--------|
| Permissives | Force all True | Not needed for 200 scans; sweep finds C_Hold instead |
| Production mode | `C_ProductionMode=True` | Discovered ✓ |
| Commands | Pulse Clear→Reset→Start | Discovered ✓ (corridor BFS) |
| SFC init wait | ~2000 scans | Never — commands bypass Starting |
| Rotate sensor | Toggled every 50 scans | Never discovered |
| Heat timer | Runs in EXECUTE | **Oracle says S_Execute=False — wrong** |

## Key files

- `src/pyrung/core/analysis/causal/crossings_recorded.py` — `recorded_read_changes`
  (adjacent-scan consume widening)
- `src/pyrung/core/analysis/causal/recorded.py` — `_walk_backward` (cycle guard
  keyed on `(tag, scan)`)
- `src/pyrung/core/analysis/walk/explore.py` — `_counterfactual_hold_sweep`
  (stabilisation sweep)
- `src/pyrung/core/analysis/walk/agenda.py` — `_check_progress_regression`,
  `_counterfactual_fallback_holds`, `unprotectable_commits`
- `src/pyrung/core/analysis/walk/fold.py` — `_advance_time` (instrumented)
- `src/pyrung/core/analysis/walk/steer.py` — `_apply_steer_fold` (instrumented)

## Probes

- `scratchpad/burner/probe_regression_holds.py` — wraps `mine_regression_holds`
- `scratchpad/burner/probe_execute_abort_diff.py` — real-time startup probe
- `scratchpad/burner/reconstitute_y_burnerloop_steps.py` — ground truth
