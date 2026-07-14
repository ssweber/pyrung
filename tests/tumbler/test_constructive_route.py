"""Constructive ground truth: hand-drive the tumbler from cold to COMPLETED(17).

Port of ``scratchpad/burner/reconstitute_completed_steps.py``.  The companion
``test_burnerloop_prefix`` proves the known-good prefix (mode change -> Reset
-> Start -> Rotate/Blower init -> HeatDelay -> the burner loop).  This one
picks up the territory AFTER the burner loop, all the way through
ProductionExecuteSteps to S_Sheeting_tmr and on into COMPLETED(17).

It never presses the Complete command.  The Complete command is issued
*internally* by ProductionExecuteSteps R23 (``rise(S_Sheeting_tmr.Done)`` ->
``copy(Ref_Cmd_Complete, Cmd_CtrlCmd)``).  The whole point is to prove the
internal route exists.

Route:
    y_BurnerLoop (Internal__Step 101 = Dry, Heat SFC step 3, burner firing)
      -> Dry done  (S_HeatAtTemp_tmr, gated temp > HoldBack) -> Step 103 Cool
      -> Cool done (S_CoolCycle_tmr)                         -> Step 105 Hold
      -> HoldForSheet issues Hold; door-open advances        -> Step 107 Sheet+
      -> Unhold back to Execute; step advances               -> Step 109 Sheet
      -> S_Sheeting_tmr done -> R23 copy(Ref_Cmd_Complete, Cmd_CtrlCmd)
      -> COMPLETING(16) -> SFCs stop -> StateComplete        -> COMPLETED(17)

Timer dwells are minute-scale (Dry 60 min, Cool 15 min, Sheet 30 min at
dt=0.010).  The bench fast-forwards a self-advancing dwell by writing its
accumulator straight to preset; everything else is the program's own
transitions.  Scan-count landmarks from the pre-rename export are recorded
but not asserted.
"""

from __future__ import annotations

import pytest

from tests.fixtures.tumbler import enter_production
from tests.tumbler.bench import Bench

pytestmark = pytest.mark.tumbler

# Budgets: the old script's step_until limits, padded ~2x.
EXECUTE_BUDGET = 4000
BURNER_BUDGET = 4000
COOL_STEP_BUDGET = 400
HOLD_STEP_BUDGET = 400
HELD_BUDGET = 800
SHEETADDED_BUDGET = 400
UNHOLD_EXEC_BUDGET = 4000
SHEET_STEP_BUDGET = 400
COMPLETING_BUDGET = 400
COMPLETED_BUDGET = 800

DIAG = (
    "Sts_UnitModeCurrent",
    "Sts_StateCurrent",
    "Sts_StateRequested",
    "Sts_StateCompleteBool",
    "Internal__Step",
    "Internal__TransBool",
    "S_CurrStep_Dry",
    "S_CurrStep_Cool",
    "S_CurrStep_HoldForSheet",
    "S_CurrStep_SheetAdded",
    "S_CurrSteP_Sheet",
    "Cmd_CtrlCmd",
    "Cmd_CmdChgRequestBool",
    "Heat_CurStep",
    "Heat_Error",
    "S_DryerTemp_F",
    "S_HeatAtTemp_tmr_Acc",
    "S_HeatAtTemp_tmr_Done",
    "S_CoolCycle_tmr_Acc",
    "S_CoolCycle_tmr_Done",
    "S_Sheeting_tmr_Acc",
    "S_Sheeting_tmr_Done",
    "o_BurnerLoop",
    "y_BurnerLoop",
    "A_AlmExtent",
    "i_DoorClosed",
)


def test_constructive_route_to_completed(tumbler_logic) -> None:
    b = Bench(tumbler_logic)
    landmarks: list[tuple[str, int]] = []

    def land(stage: str) -> None:
        landmarks.append((stage, b.scan))

    # -- Stage 0: cold boot + physical permissives -------------------------
    b.force_physical()
    b.step()
    b.snapshot_alarms()
    assert b.get("Sts_StateCurrent") == 9, (
        f"cold boot should settle in ABORTED(9): {b.snapshot(DIAG)}"
    )
    land("0 cold boot ABORTED(9)")

    # -- Stage 1: Production mode ------------------------------------------
    enter_production(b.plc)
    b.scan = b.plc.state.scan_id
    assert b.get("Sts_UnitModeCurrent") == 1, b.snapshot(DIAG)
    land("1 production mode")

    # -- Stage 2: Clear / Reset / Start -> EXECUTE(6) -----------------------
    for cmd in ("Cmd_State_Clear", "Cmd_State_Reset", "Cmd_State_Start"):
        b.patch({cmd: True})
        b.step(5)
    # After Start -> STARTING(3); after Rotate/Blower init -> EXECUTE(6).
    reached = b.step_until(lambda: b.get("Sts_StateCurrent") == 6, EXECUTE_BUDGET)
    assert reached, f"never reached EXECUTE(6): {b.snapshot(DIAG)}"
    land("2 EXECUTE(6)")

    # -- Stage 3: burner loop (Internal__Step 101 = Dry) --------------------
    reached = b.step_until(lambda: b.get("y_BurnerLoop") is True, BURNER_BUDGET)
    assert reached and b.burner_hit_scan is not None, (
        f"never reached y_BurnerLoop: {b.snapshot(DIAG)}"
    )
    assert b.get("Internal__Step") == 101, b.snapshot(DIAG)
    land("3 y_BurnerLoop (Dry)")

    # -- Stage 4: complete Dry (Step 101 -> 103 Cool) ------------------------
    # Raise dryer temp above HoldBack (P1 - P5 = 135 - 5 = 130) so
    # ProductionExecuteSteps R11 enables S_HeatAtTemp_tmr, then fast-forward
    # that dwell (Dry dwell is Sts_P2_Dry_Tm minutes).
    b.force("S_DryerTemp_F", 131.0)
    b.step(3)
    b.force_done("S_HeatAtTemp_tmr_Acc", 60)
    reached = b.step_until(lambda: b.get("Internal__Step") == 103, COOL_STEP_BUDGET)
    assert reached, f"Dry never completed into Cool(103): {b.snapshot(DIAG)}"
    b.step()  # step coil is out()'d on the scan after Internal__Step advances
    assert b.get("S_CurrStep_Cool") is True, b.snapshot(DIAG)
    land("4 Dry -> Cool(103)")

    # -- Stage 5: complete Cool (Step 103 -> 105 HoldForSheet) ---------------
    b.step(3)
    b.force_done("S_CoolCycle_tmr_Acc", 15)
    reached = b.step_until(lambda: b.get("Internal__Step") == 105, HOLD_STEP_BUDGET)
    assert reached, f"Cool never completed into HoldForSheet(105): {b.snapshot(DIAG)}"
    b.step()  # step coil is out()'d on the scan after Internal__Step advances
    assert b.get("S_CurrStep_HoldForSheet") is True, b.snapshot(DIAG)
    land("5 Cool -> HoldForSheet(105)")

    # -- Stage 6: HoldForSheet settles to HELD(11) ---------------------------
    # Step 105 issues Ref_Cmd_Hold (ProductionExecuteSteps R17): 6 -> 10 -> 11.
    reached = b.step_until(lambda: b.get("Sts_StateCurrent") == 11, HELD_BUDGET)
    assert reached, f"program-owned Hold never reached HELD(11): {b.snapshot(DIAG)}"
    land("6 HELD(11)")

    # -- Stage 7: open door -> Step 105 -> 107 SheetAdded --------------------
    # ProductionExecuteSteps R18: HoldForSheet & ~door -> TransBool -> Step 107.
    b.force("x_DoorClosed", False)
    reached = b.step_until(lambda: b.get("Internal__Step") == 107, SHEETADDED_BUDGET)
    b.force("x_DoorClosed", True)  # re-close before we unhold (else door alarm)
    b.step(3)
    assert reached, f"door cycle never advanced to SheetAdded(107): {b.snapshot(DIAG)}"
    assert b.get("S_CurrStep_SheetAdded") is True, b.snapshot(DIAG)
    land("7 door cycle -> SheetAdded(107)")

    # -- Stage 8: Unhold back to EXECUTE(6), step advances to 109 Sheet ------
    b.patch({"Cmd_State_Unhold": True})
    reached = b.step_until(lambda: b.get("Sts_StateCurrent") == 6, UNHOLD_EXEC_BUDGET)
    assert reached, f"Unhold never returned to EXECUTE(6): {b.snapshot(DIAG)}"
    # Back in Execute at Step 107; R20 advances 107 -> 109 Sheet.
    reached = b.step_until(lambda: b.get("Internal__Step") == 109, SHEET_STEP_BUDGET)
    assert reached, f"step never advanced to Sheet(109): {b.snapshot(DIAG)}"
    b.step()  # step coil is out()'d on the scan after Internal__Step advances
    assert b.get("S_CurrSteP_Sheet") is True, b.snapshot(DIAG)
    land("8 Unhold -> Sheet(109)")

    # -- Stage 9: S_Sheeting_tmr done -> internal Complete -> COMPLETING(16) --
    # The test NEVER writes Cmd_State_Complete or Cmd_CtrlCmd; the Complete
    # command must be issued internally by ProductionExecuteSteps R23.
    assert b.get("Cmd_State_Complete") is False, b.snapshot(DIAG)
    ctrl_before = b.get("Cmd_CtrlCmd")
    ref_complete = b.get("Ref_Cmd_Complete")
    b.step(3)  # let R22 arm S_Sheeting_tmr under Sheet+Execute
    b.force_done("S_Sheeting_tmr_Acc", 30)
    reached = b.step_until(lambda: b.get("Sts_StateCurrent") == 16, COMPLETING_BUDGET)
    ctrl_after = b.get("Cmd_CtrlCmd")
    assert reached, (
        f"Sheeting timer done never drove COMPLETING(16): "
        f"Cmd_CtrlCmd {ctrl_before!r}->{ctrl_after!r}, {b.snapshot(DIAG)}"
    )
    # Internal route proof: the program itself copied Ref_Cmd_Complete into
    # the control command; the operator-facing Complete button stayed off.
    assert ctrl_after == ref_complete, (
        f"Cmd_CtrlCmd {ctrl_before!r}->{ctrl_after!r}, expected internal "
        f"Ref_Cmd_Complete={ref_complete!r}"
    )
    assert b.get("Cmd_State_Complete") is False, b.snapshot(DIAG)
    land("9 internal Complete -> COMPLETING(16)")

    # -- Stage 10: COMPLETING(16) -> COMPLETED(17) ---------------------------
    reached = b.step_until(lambda: b.get("Sts_StateCurrent") == 17, COMPLETED_BUDGET)
    assert reached, f"never reached COMPLETED(17): {b.snapshot(DIAG)}"
    land("10 COMPLETED(17)")

    # -- Alarm red-herring proof ---------------------------------------------
    # A_AlmExtent = sum of ds[201..300] (main R67); any nonzero member drives
    # ProductionErrors R1 -> Abort.  The whole run must keep the band at its
    # cold value: the 6->16 and 16->17 enables passed naturally.
    assert b.almextent_max == 0, (
        f"A_AlmExtent left zero during the run (max {b.almextent_max}); "
        f"violations: {b.alarm_violations}"
    )
    assert not b.alarm_violations, (
        f"alarm status words / trigger bits departed their cold values: {b.alarm_violations}"
    )

    # -- Landmarks (recorded, not asserted; pre-rename export for reference:
    #    burner ~2016, COMPLETED(17) ~2817) ----------------------------------
    print("\nStage landmarks (scan counts):")
    for stage, scan in landmarks:
        print(f"  {stage:32s} scan {scan}")
    print(f"  y_BurnerLoop first hit           scan {b.burner_hit_scan}")
