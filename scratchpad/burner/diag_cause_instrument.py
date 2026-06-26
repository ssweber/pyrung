"""Instrument cause() at the door-alarm abort, recursively, the way walk's
recursive_cause_evidence chases triggers -> enablers -> roots.

Drives to Starting holding ONLY Blower/Rotate FB (door open), plain-steps until
S_StateCurrent->Aborting(8), then for each link in the abort chain dumps:
  - cause() chain.__str__()  (recorded mode)
  - every step's triggers (transitioned) and enablers (held steady)
The point: the enabler that held the abort path open is ~i_DoorClosed -- the very
condition the pilot failed to establish.  An enabler whose held value == the thing
we never set is the smoking gun.
"""
from __future__ import annotations
import os, sys
from pathlib import Path
CLICK_PROJECT = Path(os.environ.get("PYRUNG_CLICK_PROJECT",
    r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project"))
sys.path.insert(0, str(CLICK_PROJECT))
from main import logic  # noqa: E402
from pyrung import PLC  # noqa: E402

HOLDS = {"x_BlowerFB": True, "x_RotateFB": True}
ROOTS = ["S_StateCurrent", "C_CtrlCmd", "A_AlmExtent", "A_Alm14_DoorOpen_Trig"]
_MAX_DEPTH = 12


def pulse(plc, name, settle=4):
    plc.patch({name: True}); plc.step()
    for _ in range(settle):
        plc.step()


def drive_to_abort(plc):
    plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    pulse(plc, "C_Clear"); pulse(plc, "C_Reset")
    plc.patch(HOLDS)
    pulse(plc, "C_Start")
    for _ in range(60):
        plc.step()
        if plc.state.tags.get("S_StateCurrent") in (6, 8, 9):
            return plc.state.scan_id
    return None


def _cause(plc, tag, scan):
    try:
        return plc.cause(tag, scan=scan) if scan is not None else plc.cause(tag)
    except Exception as exc:  # noqa: BLE001
        print(f"    cause({tag}, scan={scan}) raised: {exc!r}")
        return None


def walk(plc, tag, scan, depth, seen):
    pad = "  " * depth
    key = (tag, scan)
    if key in seen or depth > _MAX_DEPTH:
        return
    seen.add(key)
    chain = _cause(plc, tag, scan)
    if chain is None:
        print(f"{pad}{tag}@{scan}: <no chain>")
        return
    print(f"{pad}>>> cause({tag}, scan={scan})  mode={chain.mode}")
    for line in str(chain).splitlines():
        print(f"{pad}    {line}")
    # recurse into triggers and enablers, the way walk._walk_chain does
    for step in chain.steps:
        for trig in step.triggers:
            walk(plc, trig.tag_name, trig.scan_id, depth + 1, seen)
        for en in step.enablers:
            print(f"{pad}  [enabler] {en.tag_name} = {en.value!r} "
                  f"(held_since={en.held_since_scan})")
            walk(plc, en.tag_name, en.held_since_scan, depth + 1, seen)
    for r in chain.conjunctive_roots:
        walk(plc, r.tag_name, r.scan_id, depth + 1, seen)
    for r in chain.ambiguous_roots:
        walk(plc, r.tag_name, r.scan_id, depth + 1, seen)


def main():
    plc = PLC(logic)
    abort_scan = drive_to_abort(plc)
    print(f"=== landed at S_StateCurrent={plc.state.tags.get('S_StateCurrent')} "
          f"scan={abort_scan} ===\n")
    for tag in ROOTS:
        print("=" * 72)
        walk(plc, tag, abort_scan, 0, set())
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
