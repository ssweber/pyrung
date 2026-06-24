"""How does CLEARING(1) behave on a pure wait? How many scans to STOPPED(2)?
Does anything re-trigger it? This decides whether the settle is just too
short (4 scans) or something holds/re-aborts the state."""

from __future__ import annotations

import os
import sys
from pathlib import Path

CLICK_PROJECT = Path(
    os.environ.get(
        "PYRUNG_CLICK_PROJECT",
        r"C:\Users\ssweb\AppData\Local\Temp\CLICK (00680950)\pyrung_project",
    )
)
sys.path.insert(0, str(CLICK_PROJECT))

from pyrung import PLC  # noqa: E402
from main import logic  # noqa: E402

WATCH = ["S_StateCurrent", "S_StateRequested", "S_UnitModeCurrent",
         "isStateEnbl_Yes", "C_CtrlCmd"]


def snap(plc):
    return {t: plc.state.tags.get(t) for t in WATCH}


def main() -> int:
    plc = PLC(logic)
    for n, v in {"x_DoorClosed": True, "x_LintDoorClosed": True, "x_BlowerFB": True,
                 "x_RotateFB": True, "x_RotateSensor": False, "x_SailRelay": True}.items():
        plc.force(n, v)
    plc.step()
    print(f"init: {snap(plc)}")

    # Mode change handshake
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step()
    for _ in range(3):
        plc.step()
    print(f"after mode: {snap(plc)}")

    # Clear -> should enter CLEARING(1)
    plc.patch({"C_Clear": True})
    plc.step()
    print(f"after C_Clear pulse: {snap(plc)}")

    # Pure wait: step forward with NO input, watch S_StateCurrent each scan.
    print("--- pure wait (no input), 60 scans ---")
    prev = None
    for i in range(1, 61):
        plc.step()
        sc = plc.state.tags.get("S_StateCurrent")
        if sc != prev:
            print(f"  scan +{i}: {snap(plc)}")
            prev = sc
    print(f"final after 60-scan wait: S_StateCurrent={plc.state.tags.get('S_StateCurrent')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
