"""Concrete patch/force/step sequence that reaches y_BurnerLoop.

This intentionally does not use how().  It drives the generated CLICK project
like a test bench:

1. Hold physical permissives/feedback true.
2. Select Production mode.
3. Pulse Clear, Reset, Start.
4. Keep the rotate sensor moving while the SFCs initialize.
5. Wait until Heat reaches step 3 and turns on the burner output.
"""

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


MONITOR_TAGS = (
    "S_UnitModeCurrent",
    "S_StateCurrent",
    "S_StateRequested",
    "S_StateCompleteBool",
    "Internal__Step",
    "S_CurrStep_Dry",
    "Rotate_xCall",
    "Rotate__x",
    "Rotate_CurStep",
    "Rotate__init",
    "Rotate_Error",
    "Blower_xCall",
    "Blower__x",
    "Blower_CurStep",
    "Blower__init",
    "Blower_Error",
    "HeatDelay_Tmr_Acc",
    "HeatDelay_Tmr_Done",
    "Heat_xCall",
    "Heat__x",
    "Heat_CurStep",
    "Heat__init",
    "Heat_Error",
    "S_DryerTemp_F",
    "S_P1_OperatingTemp_F",
    "Heat_TargetTemp_F",
    "o_BurnerLoop",
    "y_BurnerLoop",
)


scan = 0


def get(plc: PLC, name: str) -> object:
    return plc.state.tags.get(name, "<missing>")


def dump(plc: PLC, label: str) -> None:
    fields = ", ".join(
        f"{name}={get(plc, name)!r}" for name in MONITOR_TAGS if name in plc.state.tags
    )
    print(f"\n[{scan:04d}] {label}")
    print(f"  {fields}")


def step(plc: PLC, count: int = 1, *, animate_rotate_sensor: bool = False) -> bool:
    """Step the PLC; return True as soon as y_BurnerLoop is true."""
    global scan
    for _ in range(count):
        if animate_rotate_sensor:
            # Rotate watchdogs require a changing sensor after Rotate_CurStep >= 3.
            plc.force("x_RotateSensor", (scan // 50) % 2 == 0)
        plc.step()
        scan += 1
        if get(plc, "y_BurnerLoop") is True:
            dump(plc, "HIT y_BurnerLoop")
            return True
    return False


def pulse(plc: PLC, name: str, settle_scans: int = 4) -> None:
    plc.patch({name: True})
    step(plc)
    dump(plc, f"after {name} pulse")
    step(plc, settle_scans)
    dump(plc, f"after {name} settle")


def main() -> int:
    print(f"CLICK_PROJECT={CLICK_PROJECT}")
    plc = PLC(logic)

    # Physical permissives and feedback.  These are external inputs, not
    # internal shortcuts; read_inputs maps them into the i_* tags.
    for name, value in {
        "x_DoorClosed": True,
        "x_LintDoorClosed": True,
        "x_BlowerFB": True,
        "x_RotateFB": True,
        "x_RotateSensor": False,
        "x_SailRelay": True,
    }.items():
        plc.force(name, value)

    step(plc)
    dump(plc, "after first scan + physical inputs")

    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    step(plc, 2)
    dump(plc, "after Production mode request")

    pulse(plc, "C_Clear")
    pulse(plc, "C_Reset")
    pulse(plc, "C_Start")

    # Normal scan-time wait.  At dt=0.010:
    # - Rotate initializes around 4s.
    # - Blower initializes around 7s.
    # - Execute then starts HeatDelay_Tmr; Heat_xCall follows around 10s later.
    # - Heat reaches CurStep 3 roughly 2s after it is called.
    for block in range(1, 100):
        if step(plc, 50, animate_rotate_sensor=True):
            return 0
        if block % 4 == 0:
            dump(plc, f"after wait {block * 50} scans")

    dump(plc, "FAILED to reach y_BurnerLoop")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
