"""Constructive ground truth: hand-drive the burner from cold to COMPLETE(17).

Companion to ``reconstitute_y_burnerloop_steps.py``.  That script proves the
known-good prefix (mode change -> Reset -> Start -> Rotate/Blower init ->
HeatDelay -> the burner loop) reaches ``y_BurnerLoop`` around scan ~2016.  This
one picks up the *new territory*: what happens AFTER the burner loop, all the
way through ProductionExecuteSteps to S_Shining_tmr and on into COMPLETE(17).

It never presses C_Complete.  The Complete command is issued *internally* by
ProductionExecuteSteps R23 (``rise(S_Shining_tmr.Done)`` ->
``copy(CmdCompleteRef, C_CtrlCmd)``).  The whole point is to prove the internal
route exists.

PackML state map (S_StateCurrent):
    1 CLEARING  2 STOPPED  3 STARTING  4 IDLE  5 SUSPENDED  6 EXECUTE
    7 STOPPING  8 ABORTING  9 ABORTED  10 HOLDING  11 HELD  12 UNHOLDING
    13 SUSPENDING 14 UNSUSPENDING 15 RESETTING  16 COMPLETING  17 COMPLETED

Route (the captain's chart, verbatim, made concrete):
    y_BurnerLoop (Internal__Step 101 = Dry, Heat SFC step 3, burner firing)
      -> Dry done  (S_HeatAtTemp_tmr, gated temp > HoldBack) -> Step 103 Cool
      -> Cool done (S_CoolCycle_tmr)                         -> Step 105 Hold
      -> HoldForShine issues Hold; door-open advances        -> Step 107 Shine+
      -> Unhold back to Execute; step advances               -> Step 109 Shine
      -> S_Shining_tmr done -> R23 copy(CmdCompleteRef,C_CtrlCmd)
      -> COMPLETING(16) -> SFCs stop -> S_StateComplete       -> COMPLETED(17)

Timer dwells are minute-scale (Dry 60 min, Cool 15 min, Shine 30 min at
dt=0.010).  A bench fast-forwards a self-advancing dwell by writing its
accumulator straight to preset -- exactly what PILOT's let-run/zoom does
conceptually.  Everything else is the program's own transitions.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (00010A00)\pyrung_project",
    )
)
if not CLICK_PROJECT.is_dir():
    raise SystemExit(f"burner export does not exist: {CLICK_PROJECT}")
print(f"[EXPORT] {CLICK_PROJECT}", flush=True)
sys.path.insert(0, str(CLICK_PROJECT))

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402

# ---------------------------------------------------------------------------
# Alarm status words: A_AlmExtent = ds[201..300].sum() (main R67).  Any nonzero
# member drives ProductionErrors R1 -> Abort.  A_Alm100_Status is ds[300], the
# free word PILOT's decline named.  We track the whole band to prove the
# completion gate isStateEnbl_Yes=1 is satisfied *without* touching any of them.
# ---------------------------------------------------------------------------
ALARM_STATUS_TAGS = [f"A_Alm{i}_Status" for i in range(1, 101)]  # ds201..ds300
ALARM_TRIG_BITS = (
    "A_Alm11_Rotate_Trig",
    "A_Alm12_Blower_Trig",
    "A_Alm13_Heat_Trig",
    "A_Alm14_DoorOpen_Trig",
    "A_Alm15_LintOpen_Trig",
    "A_Alm16_Sail_Trig",
    "A_Alm17_HiHeat_Trig",
)

MONITOR = (
    "S_UnitModeCurrent",
    "S_StateCurrent",
    "S_StateRequested",
    "S_StateCompleteBool",
    "Internal__Step",
    "Internal__TransBool",
    "S_CurrStep_Dry",
    "S_CurrStep_Cool",
    "S_CurrStep_HoldForShine",
    "S_CurrStep_ShineAdded",
    "S_CurrSteP_Shine",
    "C_CtrlCmd",
    "C_CmdChgRequestBool",
    "Rotate__x",
    "Blower__x",
    "Heat__x",
    "Heat_CurStep",
    "Heat_Error",
    "S_DryerTemp_F",
    "S_HeatAtTemp_tmr_Acc",
    "S_HeatAtTemp_tmr_Done",
    "S_CoolCycle_tmr_Acc",
    "S_CoolCycle_tmr_Done",
    "S_Shining_tmr_Acc",
    "S_Shining_tmr_Done",
    "o_BurnerLoop",
    "y_BurnerLoop",
    "A_AlmExtent",
    "A_Alm100_Status",
    "i_DoorClosed",
)

PHYSICAL = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_RotateSensor": False,
    "x_SailRelay": True,
}


class Bench:
    def __init__(self) -> None:
        self.plc = PLC(logic, dt=0.010)
        self.scan = 0
        self.burner_hit_scan: int | None = None
        # Baseline alarm snapshot (captured after cold boot below).
        self.alarm_baseline: dict[str, object] = {}
        self.alarm_violations: list[str] = []
        self.almextent_max = 0

    # -- primitives ---------------------------------------------------------
    def get(self, name: str):
        return self.plc.state.tags.get(name, "<missing>")

    def force(self, name: str, value) -> None:
        self.plc.force(name, value)

    def patch(self, mapping: dict) -> None:
        self.plc.patch(mapping)

    def _oscillate(self) -> None:
        # Rotate watchdogs (rotate.py R10/R11) fault a stalled sensor once
        # Rotate_CurStep >= 3: SensorOn WD 2 s (reset ~sensor), SensorOff WD
        # 10 s (reset sensor).  A 50-scan (0.5 s) half period keeps both fed.
        self.force("x_RotateSensor", (self.scan // 50) % 2 == 0)

    def step(self, count: int = 1) -> None:
        for _ in range(count):
            self._oscillate()
            self.plc.step()
            self.scan += 1
            if self.burner_hit_scan is None and self.get("y_BurnerLoop") is True:
                self.burner_hit_scan = self.scan
            self._check_alarms()

    def step_until(self, pred, limit: int, label: str) -> bool:
        for _ in range(limit):
            self.step()
            if pred():
                return True
        return False

    # -- alarm red-herring tracking ----------------------------------------
    def snapshot_alarms(self) -> None:
        self.alarm_baseline = {t: self.get(t) for t in ALARM_STATUS_TAGS}
        self.alarm_baseline.update({t: self.get(t) for t in ALARM_TRIG_BITS})

    def _check_alarms(self) -> None:
        if not self.alarm_baseline:
            return
        extent = self.get("A_AlmExtent")
        if isinstance(extent, int) and extent > self.almextent_max:
            self.almextent_max = extent
        for tag, base in self.alarm_baseline.items():
            cur = self.get(tag)
            if cur != base:
                note = f"scan {self.scan}: {tag} {base!r} -> {cur!r}"
                # only record the first departure per tag
                if not any(v.split(':')[1].strip().startswith(tag + " ")
                           for v in self.alarm_violations):
                    self.alarm_violations.append(note)

    # -- reporting ----------------------------------------------------------
    def dump(self, label: str) -> None:
        fields = ", ".join(
            f"{n}={self.get(n)!r}" for n in MONITOR if n in self.plc.state.tags
        )
        print(f"\n[{self.scan:05d}] {label}\n  {fields}", flush=True)


def force_done(bench: Bench, acc_tag: str, preset: int) -> None:
    """Fast-forward a self-advancing timer by writing its accumulator to preset.

    Mirrors PILOT let-run/zoom: once the dwell's guard holds, jump the governing
    register to completion rather than stepping the (minute-scale) real dwell.
    """
    bench.patch({acc_tag: preset})


def main() -> int:  # noqa: C901 - a linear bench, read top to bottom
    print(f"CLICK_PROJECT={CLICK_PROJECT}", flush=True)
    b = Bench()

    ledger: list[tuple[str, str]] = []

    def land(stage: str, ok: bool, detail: str) -> None:
        mark = "OK " if ok else "!! "
        ledger.append((stage, f"{mark}{detail}"))
        print(f"  --> {mark}{stage}: {detail}", flush=True)
        if not ok:
            b.dump(f"STALLED at {stage}")

    # -- Stage 0: cold boot + physical permissives -------------------------
    for name, value in PHYSICAL.items():
        b.force(name, value)
    b.step()
    b.snapshot_alarms()
    b.dump("Stage 0: cold boot + physical inputs")
    land("0 cold boot", b.get("S_StateCurrent") == 9,
         f"S_StateCurrent={b.get('S_StateCurrent')} (expect 9 ABORTED)")

    # -- Stage 1: Production mode ------------------------------------------
    b.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    b.step(3)
    b.dump("Stage 1: Production mode requested")
    land("1 production mode", b.get("S_UnitModeCurrent") == 1,
         f"S_UnitModeCurrent={b.get('S_UnitModeCurrent')} (expect 1)")

    # -- Stage 2: Clear / Reset / Start -----------------------------------
    for cmd in ("C_Clear", "C_Reset", "C_Start"):
        b.patch({cmd: True})
        b.step(5)
    b.dump("Stage 2: Clear/Reset/Start pulsed")
    # After Start -> Starting(3); after Rotate/Blower init -> Execute(6).
    reached = b.step_until(lambda: b.get("S_StateCurrent") == 6, 2000, "execute")
    b.dump("Stage 2b: reached EXECUTE(6)")
    land("2 execute", reached and b.get("S_StateCurrent") == 6,
         f"S_StateCurrent={b.get('S_StateCurrent')} at scan {b.scan}")

    # -- Stage 3: burner loop (Internal__Step 101 = Dry) ------------------
    reached = b.step_until(lambda: b.get("y_BurnerLoop") is True, 2000, "burner")
    b.dump("Stage 3: burner loop achieved")
    land("3 burner loop", reached and b.get("y_BurnerLoop") is True,
         f"y_BurnerLoop at scan {b.burner_hit_scan}, Internal__Step="
         f"{b.get('Internal__Step')}")

    # -- Stage 4: complete Dry (Step 101 -> 103 Cool) ---------------------
    # Raise dryer temp above HoldBack (P1-P5 = 135-5 = 130) so Production
    # ExecuteSteps R11 enables S_HeatAtTemp_tmr, then fast-forward that dwell.
    b.force("S_DryerTemp_F", 131.0)
    b.step(3)
    force_done(b, "S_HeatAtTemp_tmr_Acc", 60)
    reached = b.step_until(lambda: b.get("Internal__Step") == 103, 200, "cool step")
    b.dump("Stage 4: Dry complete -> Cool")
    land("4 dry->cool", reached and b.get("Internal__Step") == 103,
         f"Internal__Step={b.get('Internal__Step')} (expect 103 Cool)")

    # -- Stage 5: complete Cool (Step 103 -> 105 HoldForShine) ------------
    b.step(3)
    force_done(b, "S_CoolCycle_tmr_Acc", 15)
    reached = b.step_until(lambda: b.get("Internal__Step") == 105, 200, "hold step")
    b.dump("Stage 5: Cool complete -> HoldForShine")
    land("5 cool->hold", reached and b.get("Internal__Step") == 105,
         f"Internal__Step={b.get('Internal__Step')} (expect 105 HoldForShine)")

    # -- Stage 6: HoldForShine settles to HELD(11) ------------------------
    # Step 105 issues CmdHoldRef (ProductionExecuteSteps R17): 6 -> 10 -> 11.
    reached = b.step_until(lambda: b.get("S_StateCurrent") == 11, 400, "held")
    b.dump("Stage 6: HoldForShine -> HELD(11)")
    land("6 held", reached and b.get("S_StateCurrent") == 11,
         f"S_StateCurrent={b.get('S_StateCurrent')} (expect 11 HELD)")

    # -- Stage 7: open door -> Step 105 -> 107 ShineAdded -----------------
    # ProductionExecuteSteps R18: HoldForShine & ~door -> TransBool -> Step 107.
    b.force("x_DoorClosed", False)
    reached = b.step_until(lambda: b.get("Internal__Step") == 107, 200, "shineadded")
    b.force("x_DoorClosed", True)  # re-close before we unhold (else door alarm)
    b.step(3)
    b.dump("Stage 7: door cycle -> Step 107 ShineAdded")
    land("7 hold->shineadded", reached and b.get("Internal__Step") == 107,
         f"Internal__Step={b.get('Internal__Step')} (expect 107 ShineAdded)")

    # -- Stage 8: Unhold back to EXECUTE(6), step advances to 109 Shine ---
    b.patch({"C_Unhold": True})
    reached = b.step_until(lambda: b.get("S_StateCurrent") == 6, 2000, "unhold-exec")
    b.dump("Stage 8a: Unhold -> EXECUTE(6)")
    # Back in Execute at Step 107; R20 advances 107 -> 109 Shine.
    reached2 = b.step_until(lambda: b.get("Internal__Step") == 109, 200, "shine step")
    b.dump("Stage 8b: Step 109 Shine")
    land("8 unhold->shine", reached and reached2
         and b.get("S_StateCurrent") == 6 and b.get("Internal__Step") == 109,
         f"S_StateCurrent={b.get('S_StateCurrent')} Internal__Step="
         f"{b.get('Internal__Step')} (expect 6 / 109)")

    # -- Stage 9: S_Shining_tmr done -> internal Complete cmd -> COMPLETING(16)
    ctrl_before = b.get("C_CtrlCmd")
    b.step(3)  # let R22 arm S_Shining_tmr under Shine+Execute
    force_done(b, "S_Shining_tmr_Acc", 30)
    reached = b.step_until(lambda: b.get("S_StateCurrent") == 16, 200, "completing")
    ctrl_after = b.get("C_CtrlCmd")
    b.dump("Stage 9: Shine timer done -> COMPLETING(16)")
    land("9 shine->completing", reached and b.get("S_StateCurrent") == 16,
         f"C_CtrlCmd {ctrl_before!r}->{ctrl_after!r} (CmdCompleteRef=10), "
         f"S_StateCurrent={b.get('S_StateCurrent')} (expect 16 COMPLETING)")

    # -- Stage 10: COMPLETING(16) -> COMPLETED(17) ------------------------
    reached = b.step_until(lambda: b.get("S_StateCurrent") == 17, 400, "completed")
    b.dump("Stage 10: COMPLETED(17)")
    land("10 completing->completed", reached and b.get("S_StateCurrent") == 17,
         f"S_StateCurrent={b.get('S_StateCurrent')} at scan {b.scan}")

    # -- Red-herring proof --------------------------------------------------
    print("\n" + "=" * 70, flush=True)
    print("ALARM RED-HERRING PROOF", flush=True)
    print("=" * 70, flush=True)
    a100_final = b.get("A_Alm100_Status")
    extent_final = b.get("A_AlmExtent")
    print(f"A_Alm100_Status final = {a100_final!r} (cold baseline "
          f"{b.alarm_baseline.get('A_Alm100_Status')!r})", flush=True)
    print(f"A_AlmExtent   final   = {extent_final!r}  (max seen over whole run "
          f"= {b.almextent_max})", flush=True)
    print("isStateEnbl gate: with A_AlmExtent resting at 0 and every "
          "A_Alm*_Status at cold value, ProductionErrors R1 never fired an "
          "Abort, so the 6->16 and 16->17 enables passed naturally.",
          flush=True)
    if b.alarm_violations:
        print("Alarm-word departures observed during the run:", flush=True)
        for v in b.alarm_violations:
            print(f"    {v}", flush=True)
    else:
        print("No alarm status word (ds201..300) or trigger bit ever left its "
              "cold value across the entire run.", flush=True)

    # -- Ledger -------------------------------------------------------------
    print("\n" + "=" * 70, flush=True)
    print("STAGE LEDGER", flush=True)
    print("=" * 70, flush=True)
    for stage, detail in ledger:
        print(f"  {stage:28s} {detail}", flush=True)

    # -- Gate results -------------------------------------------------------
    reached_17 = b.get("S_StateCurrent") == 17
    all_stages_ok = all(detail.startswith("OK ") for _, detail in ledger)
    alarms_at_rest = not b.alarm_violations and b.almextent_max == 0
    print("\n" + "=" * 70, flush=True)
    print("GATE RESULTS", flush=True)
    print("=" * 70, flush=True)
    print(f"  reached COMPLETED(17)              : {reached_17}", flush=True)
    print(f"  every stage landmark asserted      : {all_stages_ok}", flush=True)
    print(f"  alarm words at cold value throughout: {alarms_at_rest}", flush=True)

    if reached_17 and all_stages_ok and alarms_at_rest:
        print(f"\nSUCCESS: reached COMPLETED(17) at scan {b.scan}", flush=True)
        return 0
    print(f"\nFAILED: reached_17={reached_17} all_stages_ok={all_stages_ok} "
          f"alarms_at_rest={alarms_at_rest}; last state "
          f"{b.get('S_StateCurrent')} at scan {b.scan}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
