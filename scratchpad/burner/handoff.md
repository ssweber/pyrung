# Burner PILOT handoff — driving to Execute, then y_BurnerLoop

Goal: `pilot_how(plc, y_BurnerLoop, choice=1)` reaches **Execute(6)** then
`y_BurnerLoop=True` on the real Click burner program.

- CLICK project: `C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project`
- Driver / event stream: `scratchpad/burner/sample_pilot_events.py`
- `choice=1` = ProductionMode.

## Where it stands

**Reaches Execute(6).** Leg 1 (`S_StateCurrent 3→6`) is solved end to end:
trace surfaces `Blower__init`/`Rotate__init`, the let-run coasts them to
completion, the door holds are found by investigation (latch-exposure), and the
zoom is now CONFIRMED (see "gate fix" below).

**Leg 2 (Execute → y_BurnerLoop) is the open frontier.** After Execute the
master state machine is terminal — the remaining work is the Heat sub-SFC
completing *within* Execute (`HeatDelay_Tmr → Heat_xCall → Heat_CurStep→3 →
o_BurnerLoop`). The compass surfaces no governing route for it (only
`S_StateCurrent`/`S_StateRequested` are roles), so the **generalized terminal
let-run** (added this session) handles it: hold the macro-state, coast toward the
global target. That coast **ejects on `Rotate_Error=2`** — the rotate watchdog
(`rotate.py` R10/R11→R12) trips because `x_RotateSensor` sits static. The fix is
a **liveness (toggling) hold** on `x_RotateSensor`; the sail switch is a red
herring for this target (the coast stops on the first `y_BurnerLoop` edge, inside
R13's ~1 s grace window — `x_SailRelay` only matters for *staying* in Execute).

Empirically proven sufficient (`scratchpad/burner/diag_zoom_endstate2.py`):
holding door+feedback inputs reaches Execute; adding **rotate-sensor animation**
(toggle ~every 50–100 scans) reaches `y_BurnerLoop=True`. No sail switch needed.

## What landed this session

- **Gate fix (`outcome.py`, `verify.py`)** — a zoom/let-run whose *governing*
  register reached its target is CONFIRMED, and one whose governing register
  *ejected* is AMBIENT_DRIFT (handed to investigation) — instead of both being
  discarded by `_gate_dead_end` as "dead-end" when the global target's onward leg
  is another dwell trace can't surface. This is what makes leg 1 stick.
- **Terminal let-run (`steer._try_terminal_letrun`, `_ops._coast_holding_state`,
  wired in `pilot.py`)** — promotes the old unguarded `_settle_cone` fallback to:
  hold every pipeline-role governing tag at its current value (the ejection
  guard), coast toward the global target. Reached→CONFIRMED; macro-state
  leaves→AMBIENT_DRIFT→investigate; stall→`letrun_tried` guard prevents
  re-coasting (was hanging tests). Program-agnostic: no intermediate bearing.
- **Liveness hold (`_ops.LivenessHold`, `_split_holds`)** — a `(tag, value)`
  hold whose value is `LivenessHold(on_dwell, off_dwell)`; `_install_holds` skips
  forcing it, `_coast_holding_state` animates it (manual-step, no fold).
- **Liveness hypothesis (`investigate._liveness_hypotheses`)** — finds
  complement-reset `on_delay` watchdogs whose Done fired in the incident,
  resolves each reset input to its physical driver via `trace_back`, proposes a
  `LivenessHold` with dwell = `min(all watchdog presets on that input)/2` (clears
  the tightest watchdog regardless of edge). Replay (`build_replay_fn`, terminal-
  letrun mode) re-runs the coast with the hold and judges by the global target.

Detection is correct in isolation — `diag_watchdogs.py` and
`diag_liveness_incident.py` both show it proposing
`x_RotateSensor = LivenessHold(100,100)`.

## The one remaining bug (this is the next task)

The live loop never *proposes* the `x_RotateSensor` hold, because the
investigation it runs for the rotate ejection has the **wrong window**:

- The watchdog fires during the coast (Execute≈816 → eject≈1856), but every
  investigation window is the post-eject sliver `1856→1859`, so
  `Rotate_SensorOffWD_tmr_Done` is not in `changed_tags` and `fired` is empty.
- Root cause: ejecting to Aborting yields a *misleadingly-low* trace distance
  (trend 5 < Execute's 9 — Aborting's trace path has fewer open leaves), so the
  AMBIENT_DRIFT ejection is **checkpointed as progress** instead of investigated;
  the regression that *does* fire anchors at `cp_fork.scan_id` ≈ the eject point.

Two fixes (do A first — minimal; B is the durable version):

- **(A)** In `progress._monitor_trend`: for a terminal-letrun trial, investigate
  with the **coast-span window** (`trial.scan_before → fork end`), and don't let
  an ejection checkpoint as progress. Then the watchdog firing is in
  `changed_tags`, `x_RotateSensor` is proposed, replay confirms it, the hold is
  installed and the next coast animates it → `y_BurnerLoop`.
- **(B)** Drive `_liveness_hypotheses` off `cause()` from the ejection register
  (`S_StateCurrent→8` / `Rotate_Error→2`) back through the watchdog Done bit,
  instead of the history `changed_tags` window — robust to the transient Done and
  benefits from the recent indirect-copy / consumed-within-scan `cause()` work.

## Key files

- `pilot.py` — drive loop; terminal-letrun fallback + `letrun_tried` guard.
- `steer.py` — `_try_terminal_letrun`, `_letrun_zoom`, `_try_zoom`.
- `_ops.py` — `_coast_holding_state`, `_coast_to_value`, `LivenessHold`,
  `_split_holds`, `_install_holds`.
- `verify.py` / `outcome.py` — gate pipeline; governing-reached / governing-moved
  overrides; zoom→CONFIRMED/AMBIENT_DRIFT.
- `investigate.py` — `_liveness_hypotheses`, `build_replay_fn` (terminal-letrun
  replay mode), latch-exposure.
- `progress.py` — `_monitor_trend` (where fix A goes).
- Diagnostics: `diag_zoom_endstate2.py` (leg-1/leg-2 sufficiency configs A–G),
  `diag_watchdogs.py` (watchdog enumeration + input resolution),
  `diag_liveness_incident.py` (reproduce ejection, run detection directly).

## Click state encoding
0 undefined, 1 CLEARING, 2 STOPPED, 3 STARTING, 4 IDLE, 5 SUSPENDED,
**6 EXECUTE**, 7 STOPPING, 8 ABORTING, 9 ABORTED, 10 HOLDING, 11 HELD, …
Startup: `9→(Clear)1→2→(Reset)15→4→(Start)3→(coast)6`.
