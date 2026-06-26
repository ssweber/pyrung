"""Why does Blower stall at CurStep=1 under a plain settle (no pilot)?

Drives Production -> Clear/Reset/Start, then settles WITHOUT animating the
rotate sensor (mirroring what the pilot does), dumping the blower/rotate SFC
internals so we can tell a trace gap (sim would advance, pilot can't see it)
from a genuine sim block (timer reset / xInit loop).
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
    "S_StateCurrent",
    "Rotate_CurStep", "Rotate__init", "Rotate_xInit", "Rotate_xCall", "Rotate_tmr_Acc", "Rotate_Trans",
    "Blower_CurStep", "Blower__init", "Blower_xInit", "Blower_xCall", "Blower_tmr_Acc", "Blower_Trans",
    "Blower__x", "Blower__valstepisodd", "i_BlowerFB",
]


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
    for name, value in {
        "x_DoorClosed": True, "x_LintDoorClosed": True, "x_BlowerFB": True,
        "x_RotateFB": True, "x_RotateSensor": False, "x_SailRelay": True,
    }.items():
        plc.force(name, value)
    plc.step()

    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step(); plc.step()
    pulse(plc, "C_Clear")
    pulse(plc, "C_Reset")
    pulse(plc, "C_Start")
    dump(plc, "after Start")

    for i in range(1200):
        plc.step()
        if plc.state.tags.get("Blower__init") == 1:
            dump(plc, "Blower__init reached 1")
            break
        if i % 100 == 0:
            dump(plc, f"settle {i}")
    else:
        dump(plc, "FINAL (Blower__init never reached 1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
