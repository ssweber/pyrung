"""Validate the 'establish held state + fold' approach for Starting->Execute.

Drives the burner to Starting WITHOUT forcing permissives (mirroring the pilot),
then *steers* (patches + holds) the physical feedbacks and folds toward Execute.
Answers: does holding x_BlowerFB/x_RotateFB/... + fold actually reach
S_StateCurrent==6 within a small *logical* scan budget?
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

WATCH = [
    "S_StateCurrent", "S_Starting", "S_Execute", "S_StateCompleteBool",
    "Blower_CurStep", "Blower__init", "Blower_Error",
    "Rotate_CurStep", "Rotate__init", "Rotate_Error",
    "Heat_CurStep", "y_BurnerLoop",
]

PERMISSIVES = {
    "x_DoorClosed": True, "x_LintDoorClosed": True, "x_BlowerFB": True,
    "x_RotateFB": True, "x_SailRelay": True,
}


def dump(plc: PLC, label: str) -> None:
    t = plc.state.tags
    fields = "  ".join(f"{n}={t.get(n)!r}" for n in WATCH)
    print(f"[{plc.state.scan_id:04d}] {label}\n   {fields}")


def pulse(plc: PLC, name: str, settle: int = 4) -> None:
    plc.patch({name: True})
    plc.step()
    for _ in range(settle):
        plc.step()


def main() -> int:
    plc = PLC(logic)
    plc.step()

    # Drive to Starting WITHOUT permissives forced (as the pilot sees it).
    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    pulse(plc, "C_Clear")
    pulse(plc, "C_Reset")
    pulse(plc, "C_Start")
    dump(plc, "at Starting (no permissives)")

    # Now ESTABLISH the held state: steer the physical feedbacks True and hold.
    plc.patch(PERMISSIVES)
    start = plc.state.scan_id

    # Fold toward Execute. External inputs persist, but the rotate sensor
    # watchdog (CurStep>=3) may need toggling. First try a plain fold.
    plc.run_until(
        lambda s: s.tags.get("S_StateCurrent") == 6 or s.tags.get("Rotate_Error") != 0,
        max_cycles=4000,
        fold=True,
    )
    hit = plc.state.tags.get("S_StateCurrent") == 6
    dump(plc, f"after fold (S_StateCurrent==6? {hit}, dscans={plc.state.scan_id - start})")

    # Also report whether y_BurnerLoop comes True with a bit more folding.
    plc.run_until(
        lambda s: s.tags.get("y_BurnerLoop") is True or s.tags.get("Rotate_Error") != 0,
        max_cycles=4000,
        fold=True,
    )
    print(f"  y_BurnerLoop now: {plc.state.tags.get('y_BurnerLoop')!r}")
    dump(plc, "after fold to y_BurnerLoop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
