"""Focused PILOT run for the rotate-sensor liveness case.

Drive the burner to ProductionMode + Execute(6) with the rotate sensor parked
(NOT cycling), then hand y_BurnerLoop to PILOT from that state.  The only thing
left is the terminal let-run toward y_BurnerLoop, which ejects on the rotate
watchdog (Execute -> Aborting); the investigation must now synthesize a
ConditionalHold that oscillates x_RotateSensor and re-coast to y_BurnerLoop.

This isolates the conditional-hold path we just landed, without paying the full
cold-start journey.
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
from pyrung.core.analysis.pilot import pilot_events  # noqa: E402


def _drive_to_execute(plc: PLC, budget: int = 4000) -> int:
    """Force physical permissives, select Production, pulse Clear/Reset/Start,
    then step (rotate parked False) until S_StateCurrent == 6 (Execute)."""
    for name, value in {
        "x_DoorClosed": True,
        "x_LintDoorClosed": True,
        "x_BlowerFB": True,
        "x_RotateFB": True,
        "x_RotateSensor": False,  # parked — the liveness case is that it must move
        "x_SailRelay": True,
    }.items():
        plc.force(name, value)
    plc.step()

    plc.patch({"C_ProductionMode": True, "C_UnitModeChgRequest": True})
    plc.step()
    plc.step()

    for name in ("C_Clear", "C_Reset", "C_Start"):
        plc.patch({name: True})
        plc.step()
        for _ in range(4):
            plc.step()

    for _ in range(budget):
        plc.force("x_RotateSensor", False)  # keep it parked
        plc.step()
        if plc.state.tags.get("S_StateCurrent") == 6:
            break
    return plc.state.tags.get("S_StateCurrent")


def main() -> int:
    plc = PLC(logic)
    state = _drive_to_execute(plc)
    print(f"reached S_StateCurrent={state} at scan {plc.state.scan_id}")
    print(
        "  Rotate_CurStep="
        f"{plc.state.tags.get('Rotate_CurStep')}"
        f"  Rotate_Error={plc.state.tags.get('Rotate_Error')}"
        f"  y_BurnerLoop={plc.state.tags.get('y_BurnerLoop')}"
        f"  x_RotateSensor={plc.state.tags.get('x_RotateSensor')}"
    )
    if state != 6:
        print("!! did not reach Execute; aborting pilot run")
        return 1

    target = plc._known_tags_by_name["y_BurnerLoop"]
    max_scans = int(os.environ.get("PILOT_MAX_SCANS", "100000"))
    print("\n--- PILOT from Execute toward y_BurnerLoop ---")
    for event in pilot_events(plc, target, max_scans=max_scans):
        if event.kind == "trend_regression":
            inv = event.data.get("investigation", {})
            print(f"[scan {event.scan}] trend_regression  "
                  f"hyps={inv.get('hypotheses', 0)} confirmed={inv.get('confirmed', 0)} "
                  f"rejected={inv.get('rejected', 0)}")
            for h in inv.get("hypothesis_detail", ()):
                holds = ", ".join(f"{t}={v!r}" for t, v in h["holds"])
                marker = "  <-- liveness" if h["kind"] == "liveness" else ""
                print(f"    [{h['kind']}] {holds}{marker}")
                if h.get("detail"):
                    print(f"      {h['detail']}")
        elif event.kind in {"zoom", "zoom_accepted"}:
            print(f"[scan {event.scan}] {event.kind}: {event.data.get('reason', '')}")
        elif event.kind == "finished":
            print(f"[scan {event.scan}] finished  reached={event.data['reached']}  "
                  f"reason={event.data['reason']}  steps={len(event.data['steps'])}")
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
