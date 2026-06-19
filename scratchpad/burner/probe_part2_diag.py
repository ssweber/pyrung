"""Diagnose the C_Abort fire-view at the leaving scan."""

from __future__ import annotations

import os
import sys
from pathlib import Path

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (00010A66)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from pyrung import PLC  # noqa: E402
from main import logic  # noqa: E402
from pyrung.core.analysis.walk.base import _values_match  # noqa: E402
from pyrung.core.context import RungId  # noqa: E402

PHYS = {
    "x_DoorClosed": True, "x_LintDoorClosed": True, "x_BlowerFB": True,
    "x_RotateFB": True, "x_RotateSensor": False, "x_SailRelay": True,
}


def drive_to_starting(plc):
    for k, v in PHYS.items():
        plc.force(k, v)
    plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    for cmd in ("C_Clear", "C_Reset", "C_Start"):
        plc.patch({cmd: True})
        for _ in range(5):
            plc.step()


def main() -> int:
    plc = PLC(logic)
    drive_to_starting(plc)
    plc.patch({"C_Abort": True})
    for _ in range(6):
        plc.step()

    h = plc.history
    print("scan : C_Abort C_CtrlCmd S_StateCurrent S_StateRequested")
    for s in range(14, h.newest_scan_id + 1):
        st = h.at(s).tags
        print(f"  {s:>2} :  {st.get('C_Abort')!s:>5}  {st.get('C_CtrlCmd')!s:>4}   "
              f"{st.get('S_StateCurrent')!s:>4}    {st.get('S_StateRequested')!s:>4}")

    # leaving scan
    states = h.range(h.oldest_scan_id, h.newest_scan_id + 1)
    leaving = None
    for i in range(len(states) - 1, 0, -1):
        if _values_match(states[i - 1].tags.get("S_StateCurrent"), 3) and not _values_match(
            states[i].tags.get("S_StateCurrent"), 3
        ):
            leaving = states[i].scan_id
    print(f"\nleaving scan = {leaving}")

    # node firings at leaving: who wrote C_CtrlCmd, to what?
    nf = plc._node_firings_at(leaving)
    print(f"\nnode firings writing C_CtrlCmd at scan {leaving}:")
    for rid in sorted(nf, key=lambda r: (r.subroutine or "", r.rung_index)):
        w = nf[rid]
        if "C_CtrlCmd" in w:
            print(f"  {rid.subroutine}[r{rid.rung_index}] -> C_CtrlCmd={w['C_CtrlCmd']!r}")

    # fire-views at leaving: C_Abort as each sm_MapCmd2Val rung saw it
    views = plc._replay_node_views_at(leaving)
    print(f"\nfire-view count={len(views)}; sm_MapCmd2Val rungs' C_Abort at entry:")
    for rid in sorted(views, key=lambda r: (r.subroutine or "", r.rung_index)):
        if rid.subroutine == "sm_MapCmd2Val":
            v = views[rid]
            print(f"  {rid.subroutine}[r{rid.rung_index}] C_Abort={v.get_tag('C_Abort')!r} "
                  f"C_CtrlCmd={v.get_tag('C_CtrlCmd')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
