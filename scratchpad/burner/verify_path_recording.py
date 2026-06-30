"""Verify the recorded path is self-describing after the when().do() swap.

Runs the real pilot solve (pre-positioned Execute(6) -> y_BurnerLoop), then:

  1. Asserts the winning terminal let-run _Step carries reactive_holds (the
     two-polarity oscillation on x_RotateSensor).
  2. Replays the path from a fresh Execute(6) fork using ONLY the step's recorded
     reactive_holds (register when().do() from the step) — never touching the
     pilot result's forced_holds — and confirms y_BurnerLoop=True.

If (2) reaches the target, the path replays with pyrung primitives alone.

Run:  uv run python scratchpad/burner/verify_path_recording.py
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
from pyrung.core.analysis.pilot._ops import ConditionalHold, _install_reactive_holds  # noqa: E402
from pyrung.core.analysis.sp_values import _values_match  # noqa: E402

SENSOR = "x_RotateSensor"
TARGET_TAG = "y_BurnerLoop"
PERMISSIVES = {
    "x_DoorClosed": True,
    "x_LintDoorClosed": True,
    "x_BlowerFB": True,
    "x_RotateFB": True,
    "x_SailRelay": True,
}


def _drive_to_execute(plc: PLC, budget: int = 4000) -> int:
    for name, value in {**PERMISSIVES, SENSOR: False}.items():
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
        plc.force(SENSOR, False)
        plc.step()
        if plc.state.tags.get("S_StateCurrent") == 6:
            break
    return plc.state.tags.get("S_StateCurrent")


def main() -> int:
    plc = PLC(logic)
    if _drive_to_execute(plc) != 6:
        print("!! did not reach Execute; aborting")
        return 1
    base = plc.fork()  # clean Execute(6) snapshot

    # --- 1. solve, capture recorded steps ---
    target = plc._known_tags_by_name[TARGET_TAG]
    steps = None
    journey = ()
    for event in pilot_events(plc, target, choice=1, max_scans=100000):
        if event.kind == "finished":
            steps = event.data["steps"]
            journey = event.data.get("journey", ())
            print(
                f"solve finished reached={event.data['reached']} "
                f"steps={len(steps)} (clean path)  journey={len(journey)} (attempts incl. reverted)"
            )
            break

    if not steps:
        print("!! no steps")
        return 1

    # The clean path drops the reverted rounds; the journey keeps them. The
    # accumulating holds tell the round-by-round story: {} -> {True} -> {True,False}.
    print("\njourney (attempt log):")
    for i, s in enumerate(journey):
        rh = sorted(s.reactive_holds) if s.reactive_holds else "-"
        print(f"  attempt {i}: inputs={s.inputs} scans={s.scans} holds={rh}")

    letrun_steps = [s for s in steps if s.reactive_holds]
    print(f"\nsteps with reactive_holds: {len(letrun_steps)} of {len(steps)}")
    for i, s in enumerate(steps):
        rh = {t: v for t, v in s.reactive_holds.items()}
        tag = "  <-- reactive" if rh else ""
        print(f"  step {i}: inputs={s.inputs} scans={s.scans} reactive_holds={rh}{tag}")

    if not letrun_steps:
        print("\n!! FAIL: no step carries reactive_holds — path is NOT self-describing")
        return 1

    win = letrun_steps[-1]
    ch = win.reactive_holds.get(SENSOR)
    two_polarity = isinstance(ch, ConditionalHold) and len({r.guard_value for r in ch.rules}) == 2
    print(f"\nwinning step reactive_holds[{SENSOR}] two-polarity oscillation: {two_polarity}")

    # --- 2. primitive replay using ONLY the recorded step's reactive_holds ---
    fork = base.fork()
    for name, value in PERMISSIVES.items():
        fork.force(name, value)

    handles = _install_reactive_holds(fork, win.reactive_holds)
    try:
        fork.run_until(
            lambda s: _values_match(s.tags.get(TARGET_TAG), True),
            max_cycles=6000,
            fold=True,
        )
    finally:
        for h in handles:
            h.remove()

    reached = _values_match(fork.state.tags.get(TARGET_TAG), True)
    print(
        f"\nprimitive replay (step.reactive_holds only): reached={reached} "
        f"scan={fork.state.scan_id} {TARGET_TAG}={fork.state.tags.get(TARGET_TAG)} "
        f"S_StateCurrent={fork.state.tags.get('S_StateCurrent')}"
    )

    ok = bool(letrun_steps) and two_polarity and reached
    print(f"\n========== ALL GREEN: {ok} ==========")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
