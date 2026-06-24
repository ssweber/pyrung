"""Sample the structured PILOT event stream on the burner project."""

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
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402


def _interesting(event_kind: str) -> bool:
    return event_kind in {
        "started",
        "iteration",
        "candidates_built",
        "candidate_try",
        "candidate_rejected",
        "candidate_accepted",
        "trial_committed",
        "trend_checkpoint",
        "trend_regression",
        "wait",
        "finished",
    }


def main() -> int:
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

    target = plc._known_tags_by_name["y_BurnerLoop"]
    kept = 0
    for event in pilot_events(plc, target, choice=1, max_scans=3000):
        if not _interesting(event.kind):
            continue
        data = event.data
        if event.kind == "iteration":
            print(
                f"{event.kind} scan={event.scan} distance={data['distance']} "
                f"need={list(data['still_need'][:4])}"
            )
        elif event.kind == "candidates_built":
            print(
                f"{event.kind} scan={event.scan} count={len(data['candidates'])} "
                f"first={list(data['candidates'][:5])}"
            )
        elif event.kind in {"candidate_try", "candidate_rejected", "candidate_accepted"}:
            gates = data.get("gates", ())
            print(
                f"{event.kind} scan={event.scan} candidate={data['candidate']['pair']} "
                f"gates={[gate.event for gate in gates]} trend={data.get('trend')}"
            )
        elif event.kind == "trend_regression":
            print(
                f"{event.kind} scan={event.scan} "
                f"nogoods={sorted(data['regression_nogoods'])}"
            )
        else:
            compact = {
                key: value
                for key, value in data.items()
                if key not in {"snapshot", "tree", "work", "steps"}
            }
            print(f"{event.kind} scan={event.scan} data={compact}")
        kept += 1
        if kept >= 45:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
