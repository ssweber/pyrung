"""Reproduce the pilot zoom coast (S_StateCurrent 3->6) under several hold
configurations, to see what the coast actually needs.  Mirrors _coast_to_value:
fork-less single PLC, pause-guard on ejection, run_until(==6, fold=True).

Configs:
  A) BlowerFB + RotateFB                      (what the trace currently surfaces)
  B) + DoorClosed + LintDoorClosed            (what investigation adds)
  C) B + animate x_RotateSensor               (rotate watchdog needs motion)
"""
from __future__ import annotations
import os, sys
from pathlib import Path

CLICK_PROJECT = Path(os.environ.get("PYRUNG_CLICK_PROJECT",
    r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project"))
sys.path.insert(0, str(CLICK_PROJECT))
from main import logic  # noqa: E402
from pyrung import PLC  # noqa: E402

WATCH = ["S_StateCurrent","S_Starting","S_Execute","S_Aborting","Rotate_Error",
         "Rotate_CurStep","Rotate__init","Blower_xCall","Blower_CurStep","Blower__init",
         "S_StateCompleteBool","Heat_xCall","Heat_CurStep","Heat_Error","o_BurnerLoop",
         "y_BurnerLoop"]

def dump(plc, label):
    t = plc.state.tags
    print(f"[{plc.state.scan_id:05d}] {label}")
    print("   " + "  ".join(f"{n}={t.get(n)!r}" for n in WATCH))

def pulse(plc, name, settle=4):
    plc.patch({name: True}); plc.step()
    for _ in range(settle): plc.step()

def drive_to_starting(holds):
    plc = PLC(logic); plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    pulse(plc, "C_Clear"); pulse(plc, "C_Reset")
    for k, v in holds.items():
        plc.force(k, v)
    pulse(plc, "C_Start")
    return plc

def coast(plc, animate_rotate=False):
    start_gov = plc.state.tags.get("S_StateCurrent")
    target = 6
    def reached(s): return s.tags.get("S_StateCurrent") == target
    def ejected(s):
        cur = s.tags.get("S_StateCurrent")
        return cur != start_gov and cur != target
    guard = plc.when(ejected).pause()
    try:
        if animate_rotate:
            # run_until with fold can't interleave an input animation; step manually.
            for i in range(10_000):
                plc.force("x_RotateSensor", (plc.state.scan_id // 50) % 2 == 0)
                plc.step()
                if reached(plc.state) or ejected(plc.state):
                    break
        else:
            plc.run_until(reached, max_cycles=10_000, fold=True)
    finally:
        guard.remove()
    return plc.state.tags.get("S_StateCurrent") == 6

def run_config(name, holds, animate_rotate=False):
    print("\n" + "="*70)
    print(f"CONFIG {name}: holds={holds} animate_rotate={animate_rotate}")
    print("="*70)
    plc = drive_to_starting(holds)
    dump(plc, "at Starting")
    ok = coast(plc, animate_rotate=animate_rotate)
    dump(plc, f"after coast (reached6={ok})")

BASE_HOLDS = {"x_BlowerFB": True, "x_RotateFB": True,
              "x_DoorClosed": True, "x_LintDoorClosed": True}

def coast_to_burner(plc, animate_rotate=False):
    """Leg 2: from Execute, coast toward y_BurnerLoop=True with ejection guard
    on S_StateCurrent leaving Execute(6)."""
    def reached(s): return s.tags.get("y_BurnerLoop") is True
    def ejected(s): return s.tags.get("S_StateCurrent") != 6
    guard = plc.when(ejected).pause()
    try:
        if animate_rotate:
            for _ in range(20_000):
                plc.force("x_RotateSensor", (plc.state.scan_id // 50) % 2 == 0)
                plc.step()
                if reached(plc.state) or ejected(plc.state):
                    break
        else:
            plc.run_until(reached, max_cycles=20_000, fold=True)
    finally:
        guard.remove()
    return plc.state.tags.get("y_BurnerLoop") is True

def run_leg2(name, extra_holds, animate_rotate=False):
    print("\n" + "="*70)
    print(f"LEG2 {name}: base + extra={extra_holds} animate_rotate={animate_rotate}")
    print("="*70)
    plc = drive_to_starting(BASE_HOLDS)
    # coast to Execute first (leg 1, proven to work with BASE_HOLDS)
    coast(plc, animate_rotate=False)
    for k, v in extra_holds.items():
        plc.force(k, v)
    dump(plc, "at Execute (entering leg 2)")
    ok = coast_to_burner(plc, animate_rotate=animate_rotate)
    dump(plc, f"after leg-2 coast (y_BurnerLoop={ok})")

def main():
    run_config("A", {"x_BlowerFB": True, "x_RotateFB": True})
    run_config("B", BASE_HOLDS)
    run_config("C", BASE_HOLDS, animate_rotate=True)
    # Leg 2: Execute -> y_BurnerLoop.  Which of {sail switch, rotate liveness} is load-bearing?
    run_leg2("D nothing-extra (static)", {})
    run_leg2("E sail-only (static)", {"x_SailRelay": True})
    run_leg2("F rotate-anim-only", {}, animate_rotate=True)
    run_leg2("G sail + rotate-anim", {"x_SailRelay": True}, animate_rotate=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
