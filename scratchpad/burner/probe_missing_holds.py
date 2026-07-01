"""Pin down which dropped holds are load-bearing in the how() plan.

State 9 = ABORTED (sm__STATEABORTEDREF); the manual reconstitute reaches
State 6 = EXECUTE and lights y_BurnerLoop.  The how() plan, replayed alone,
sticks in ABORTED because path.holds is None — the sustained prerequisite
holds PILOT kept during its drive were never recorded.

Hypothesis: adding back the missing background holds (x_SailRelay + the
physical permissives, held from scan 0) makes the same how() plan reach EXECUTE.

Each variant forces the how() plan's steering actions (sustained), oscillates
x_RotateSensor every scan through the coasts, and differs only in which
permissives are held from the start.
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

from pyrung import PLC  # noqa: E402
from main import logic  # noqa: E402

# how() plan steering actions, applied (and then sustained via force) per step.
PLAN: list[tuple[dict[str, object], int]] = [
    ({"C_Clear": True, "C_ProductionMode": True}, 3),
    ({"C_Reset": True}, 3),
    ({"C_Start": True}, 3),
    ({}, 800),
    (
        {
            "C_UnitModeChgRequest": True,
            "C_P1_OperatingTempF": 120,
            "S_DryerTemp_F": -1,
            "x_BlowerFB": True,
            "x_DoorClosed": True,
            "x_LintDoorClosed": True,
            "x_RotateFB": True,
        },
        1199,
    ),
]

ALL_PERMISSIVES = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_SailRelay": True,
}

WATCH = ("S_UnitModeCurrent", "S_StateCurrent", "Heat_CurStep", "o_BurnerLoop", "y_BurnerLoop")
STATE_NAMES = {2: "STOPPED", 3: "STARTING", 4: "IDLE", 6: "EXECUTE", 8: "ABORTING", 9: "ABORTED", 15: "RESETTING"}


def run(label: str, prehold: dict[str, object]) -> None:
    print("\n" + "=" * 78)
    print(label)
    print("=" * 78)
    plc = PLC(logic)
    # Background holds from scan 0 (what path.holds SHOULD have captured).
    for name, value in prehold.items():
        plc.force(name, value)
    plc.step()

    y_first = None
    for i, (action, scans) in enumerate(PLAN, 1):
        for name, value in action.items():
            plc.force(name, value)
        for _ in range(scans):
            # oscillate the rotate sensor every scan (the recorded reactive hold)
            plc.force("x_RotateSensor", not bool(plc.state.tags.get("x_RotateSensor")))
            plc.step()
            if plc.state.tags.get("y_BurnerLoop") is True and y_first is None:
                y_first = plc.state.scan_id

    t = plc.state.tags
    st = t.get("S_StateCurrent")
    print(f"  y_BurnerLoop first True @ scan : {y_first}")
    print(
        f"  final: Mode={t.get('S_UnitModeCurrent')}  "
        f"State={st} ({STATE_NAMES.get(st, '?')})  "
        f"Heat_CurStep={t.get('Heat_CurStep')}  "
        f"o_BurnerLoop={t.get('o_BurnerLoop')}  y_BurnerLoop={t.get('y_BurnerLoop')}"
    )


run("A) how() plan alone (no background holds) — the bug", prehold={})
run("B) how() plan + x_SailRelay held from scan 0", prehold={"x_SailRelay": True})
run("C) how() plan + ALL permissives (incl x_SailRelay) held from scan 0", prehold=ALL_PERMISSIVES)
