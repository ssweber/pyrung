"""Measure the Execute-phase oscillation cycle to size a pilot-authorized fold.

Drives to Execute(6) with the rotate sensor parked, then oscillates x_RotateSensor
period-2 (mimicking the pilot's ConditionalHold) and coasts toward y_BurnerLoop.

Reports:
  - total scans the coast actually takes (the "waiting" cost),
  - which non-fold-excluded visible tags change each scan (defeats the fold),
  - whether the pilot STATE KEY is stable across the churn (the coarser guard),
  - the monotone progress coordinate(s) and per-cycle delta.
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
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_SailRelay": True,
}


def drive_to_execute(plc: PLC, budget: int = 4000) -> int:
    for name, value in {**PERMISSIVES, "x_RotateSensor": False}.items():
        plc.force(name, value)
    plc.step()
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    for name in ("C_Clear", "C_Reset", "C_Start"):
        plc.patch({name: True})
        plc.step()
        for _ in range(4):
            plc.step()
    for _ in range(budget):
        plc.force("x_RotateSensor", False)
        plc.step()
        if plc.state.tags.get("S_StateCurrent") == 6:
            break
    return plc.state.tags.get("S_StateCurrent")


def main() -> int:
    plc = PLC(logic)
    st = drive_to_execute(plc)
    print(f"reached S_StateCurrent={st} at scan {plc.state.scan_id}")
    if st != 6:
        print("!! did not reach Execute")
        return 1

    ctx = plc._ensure_fold_context()
    exclude = (
        ctx.acc_names | ctx.profile_fb_names | ctx.churn_excluded
        | ctx.modwrap_names | ctx.mirror_names | ctx.frozen_writes
    )
    print(f"fold exclude set size: {len(exclude)}")

    # Release the parked force; oscillate period-2 instead (mimic ConditionalHold).
    plc.force("x_RotateSensor", False)  # keep forced, we'll flip the forced value

    start_scan = plc.state.scan_id
    watch = ["Heat_CurStep", "Rotate_CurStep", "Rotate_Error", "y_BurnerLoop",
             "S_StateCurrent"]

    def snap_watch():
        t = plc.state.tags
        return {k: t.get(k) for k in watch}

    print(f"\nstart: {snap_watch()}")

    # Per-scan churn census over the first 40 oscillation scans.
    churn_counter: dict[str, int] = {}
    heat_changes: list[tuple[int, object]] = []
    prev_heat = plc.state.tags.get("Heat_CurStep")

    MAX = 6000
    sensor = False
    reached = False
    for i in range(MAX):
        sensor = not sensor
        plc.force("x_RotateSensor", sensor)
        before = _visible_items(plc._state, exclude)
        plc.step()
        after = _visible_items(plc._state, exclude)
        if i < 40:
            for k in set(before) | set(after):
                if before.get(k) != after.get(k):
                    churn_counter[k] = churn_counter.get(k, 0) + 1
        h = plc.state.tags.get("Heat_CurStep")
        if h != prev_heat:
            heat_changes.append((plc.state.scan_id, h))
            prev_heat = h
        if plc.state.tags.get("y_BurnerLoop") is True:
            reached = True
            break
        if plc.state.tags.get("Rotate_Error", 0) != 0:
            print(f"!! Rotate_Error at scan {plc.state.scan_id} (watchdog fired)")
            break

    total = plc.state.scan_id - start_scan
    print(f"\nreached y_BurnerLoop={reached} after {total} oscillation scans")
    print(f"end: {snap_watch()}")

    print(f"\nnon-excluded tags changing during first 40 scans ({len(churn_counter)}):")
    for k, n in sorted(churn_counter.items(), key=lambda kv: -kv[1]):
        print(f"   {k}: changed in {n}/40 scans")

    print(f"\nHeat_CurStep advances ({len(heat_changes)} transitions):")
    for scan, val in heat_changes[:20]:
        print(f"   scan {scan}: -> {val}")
    if len(heat_changes) >= 2:
        gaps = [heat_changes[i][0] - heat_changes[i - 1][0]
                for i in range(1, len(heat_changes))]
        print(f"   gaps between advances (scans): {gaps[:20]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
