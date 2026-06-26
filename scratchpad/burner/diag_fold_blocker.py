"""Why doesn't fold engage during the Starting dwell?

At Starting with permissives held, find the *visible* (non-excluded) tags that
change every scan -- those are what break fold's plateau guard.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from main import logic  # noqa: E402

from pyrung import PLC  # noqa: E402
from pyrung.core.fold import _visible_items  # noqa: E402

PERMISSIVES = {
    "x_DoorClosed": True, "x_LintDoorClosed": True, "x_BlowerFB": True,
    "x_RotateFB": True, "x_SailRelay": True,
}


def pulse(plc: PLC, name: str, settle: int = 4) -> None:
    plc.patch({name: True})
    plc.step()
    for _ in range(settle):
        plc.step()


def main() -> int:
    plc = PLC(logic)
    plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    pulse(plc, "C_Clear"); pulse(plc, "C_Reset"); pulse(plc, "C_Start")
    plc.patch(PERMISSIVES)
    for _ in range(6):
        plc.step()

    ctx = plc._ensure_fold_context()
    exclude = (
        ctx.acc_names | ctx.profile_fb_names | ctx.churn_excluded
        | ctx.modwrap_names | ctx.mirror_names
    )
    print(f"excluded tag count: {len(exclude)}")
    print(f"  churn_excluded ({len(ctx.churn_excluded)}): {sorted(ctx.churn_excluded)[:30]}")

    # Step several times, report visible tags that change each scan.
    for i in range(8):
        before = _visible_items(plc._state, exclude)
        plc.step()
        after = _visible_items(plc._state, exclude)
        diff = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        print(f"\nscan {plc.state.scan_id}: {len(diff)} visible changes")
        for k in sorted(diff):
            print(f"   {k}: {before.get(k)!r} -> {after.get(k)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
