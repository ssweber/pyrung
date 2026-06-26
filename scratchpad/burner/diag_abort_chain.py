"""Does the door-alarm abort chain (R1) fire? Drive to Starting holding ONLY
Blower/Rotate FB (no door), then PLAIN-step (no fold) and watch the abort chain:
A_Alm14 -> A_AlmExtent -> C_CtrlCmd=Abort -> S_StateCurrent->Aborting."""
from __future__ import annotations
import os, sys
from pathlib import Path
CLICK_PROJECT = Path(os.environ.get("PYRUNG_CLICK_PROJECT",
    r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project"))
sys.path.insert(0, str(CLICK_PROJECT))
from main import logic  # noqa: E402
from pyrung import PLC  # noqa: E402

WATCH = ["S_StateCurrent","A_Alm14_DoorOpen_Trig","A_AlmExtent","C_CtrlCmd",
         "C_CmdChgRequestBool","Blower__init","Rotate__init","Blower_CurStep","Rotate_CurStep"]
HOLDS = {"x_BlowerFB": True, "x_RotateFB": True}

def dump(plc, label):
    t = plc.state.tags
    print(f"[{plc.state.scan_id:05d}] {label}: " + " ".join(f"{n}={t.get(n)!r}" for n in WATCH))

def pulse(plc, name, settle=4):
    plc.patch({name: True}); plc.step()
    for _ in range(settle): plc.step()

def main():
    plc = PLC(logic); plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    pulse(plc, "C_Clear"); pulse(plc, "C_Reset")
    plc.patch(HOLDS)
    pulse(plc, "C_Start")
    dump(plc, "at Starting")
    print("--- plain stepping (no fold) ---")
    for i in range(40):
        plc.step()
        dump(plc, f"step{i}")
        if plc.state.tags.get("S_StateCurrent") in (6, 8, 9):
            print(f">>> landed at {plc.state.tags.get('S_StateCurrent')}")
            break
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
