"""Under clock-aware fold: hold ALL permissives (incl x_DoorClosed) BEFORE
C_Start so the alarm never latches -> does fold reach Execute(6)?"""
from __future__ import annotations
import os, sys
from pathlib import Path
CLICK_PROJECT = Path(os.environ.get("PYRUNG_CLICK_PROJECT",
    r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project"))
sys.path.insert(0, str(CLICK_PROJECT))
from main import logic
from pyrung import PLC
WATCH = ["S_StateCurrent","S_Execute","A_Alm14_DoorOpen_Trig","A_AlmExtent","Rotate_Error"]
PERMISSIVES = {"x_DoorClosed": True, "x_LintDoorClosed": True, "x_BlowerFB": True,
               "x_RotateFB": True, "x_SailRelay": True}
def dump(plc, label):
    t = plc.state.tags
    print(f"[{plc.state.scan_id:05d}] {label}: " + " ".join(f"{n}={t.get(n)!r}" for n in WATCH))
def pulse(plc, name, settle=4):
    plc.patch({name: True}); plc.step()
    for _ in range(settle): plc.step()
def main():
    plc = PLC(logic); plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True}); plc.step(); plc.step()
    pulse(plc, "C_Clear"); pulse(plc, "C_Reset")
    plc.patch(PERMISSIVES)        # hold door BEFORE C_Start
    pulse(plc, "C_Start")
    dump(plc, "at Starting (door held before start)")
    plc.run_until(lambda s: s.tags.get("S_StateCurrent") in (6, 8, 9),
                  max_cycles=10_000, fold=True)
    dump(plc, f"after fold (reached6={plc.state.tags.get('S_StateCurrent')==6})")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
