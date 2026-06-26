"""What does the pilot's zoom actually do? Hold ONLY the trace-surfaced
feedbacks (x_BlowerFB, x_RotateFB) — NOT x_DoorClosed — reach Starting, then
fold toward S_StateCurrent==6 with the same ejection guard the zoom uses.
Report where the governing tag actually lands."""
from __future__ import annotations
import os, sys
from pathlib import Path
CLICK_PROJECT = Path(os.environ.get("PYRUNG_CLICK_PROJECT",
    r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project"))
sys.path.insert(0, str(CLICK_PROJECT))
from main import logic  # noqa: E402
from pyrung import PLC  # noqa: E402

WATCH = ["S_StateCurrent","S_Starting","S_Execute","S_Aborting","S_Aborted",
         "S_Held","A_Alm14_DoorOpen_Trig","Rotate_Error","Blower__init","Rotate__init"]
HOLDS = {"x_BlowerFB": True, "x_RotateFB": True}  # what the pilot currently holds

def dump(plc, label):
    t = plc.state.tags
    print(f"[{plc.state.scan_id:05d}] {label}")
    print("   " + "  ".join(f"{n}={t.get(n)!r}" for n in WATCH))

def pulse(plc, name, settle=4):
    plc.patch({name: True}); plc.step()
    for _ in range(settle): plc.step()

def main():
    plc = PLC(logic); plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    pulse(plc, "C_Clear"); pulse(plc, "C_Reset")
    plc.patch(HOLDS)          # hold feedbacks BEFORE C_Start, like a prereq hold
    pulse(plc, "C_Start")
    dump(plc, "at Starting (only BlowerFB/RotateFB held)")
    start_gov = plc.state.tags.get("S_StateCurrent")
    target = 6
    def reached(s): return s.tags.get("S_StateCurrent") == target
    def ejected(s):
        cur = s.tags.get("S_StateCurrent")
        return cur != start_gov and cur != target
    guard = plc.when(ejected).pause()
    try:
        plc.run_until(reached, max_cycles=10_000, fold=True)
    finally:
        guard.remove()
    dump(plc, f"after zoom-fold (reached6={plc.state.tags.get('S_StateCurrent')==6})")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
