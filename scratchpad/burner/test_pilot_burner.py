"""Test PILOT with Layer 6 on the real burner program."""

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
from pyrung.core.analysis.pilot import pilot_how  # noqa: E402
from main import logic  # noqa: E402


def main() -> int:
    print(f"CLICK_PROJECT={CLICK_PROJECT}")
    plc = PLC(logic)

    for name, value in {
        "x_DoorClosed": True,
        "x_LintDoorClosed": True,
        "x_BlowerFB": True,
        "x_RotateFB": True,
        "x_RotateSensor": False,
        "x_SailRelay": True,
    }.items():
        plc.force(name, value)

    plc.step()

    print(f"Target: y_BurnerLoop=True")
    print(f"Current: y_BurnerLoop={plc.state.tags.get('y_BurnerLoop')}")
    print(f"S_StateCurrent={plc.state.tags.get('S_StateCurrent')}")
    print()

    path = pilot_how(plc, plc._known_tags_by_name["y_BurnerLoop"], max_scans=3000, debug=True, choice=1)

    print(f"\nResult: reachable={path.reachable}")
    print(f"Steps: {len(path.steps)}")
    print(f"Total changes: {path.total_changes}")
    print(f"Total scans: {path.total_scans}")
    if hasattr(path, "reason") and path.reason:
        print(f"Reason: {path.reason}")
    if path.reachable:
        print("\nStep details:")
        for i, step in enumerate(path.steps):
            print(f"  [{i}] action={step.action}  scans={step.scans}")
    return 0 if path.reachable else 1


if __name__ == "__main__":
    raise SystemExit(main())
