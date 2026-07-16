"""Ground truth for the how(y_BurnerLoop) drive on the tumbler fixture.

Hand-steps the machine and prints the intersection table — at what scan each
bump fires — for three trajectories:

  A. happy path: physical permissives held, rotate sensor animated (50-scan
     half period) — the trajectory a correct pilot drive should reproduce
     with an OSCILLATE correction instead of hand animation.
  B. sensor held steady False (the world a naive steady-hold coast sees):
     SensorOffWD (10 s) faults rotate once Rotate_CurStep >= 3.
  C. sensor held steady True: SensorOnWD (2 s) faults it faster.

Run:  uv run python scratchpad/burner/ground_truth_y_burnerloop.py
"""

from __future__ import annotations

import importlib

from tests.fixtures.tumbler import enter_production
from tests.tumbler.bench import Bench

WATCH = (
    "Sts_UnitModeCurrent",
    "Sts_StateCurrent",
    "Internal__Step",
    "Rotate_CurStep",
    "Rotate__init",
    "Blower_CurStep",
    "Blower__init",
    "Rotate_SensorOnWD_tmr_Done",
    "Rotate_SensorOffWD_tmr_Done",
    "Rotate_Error",
    "A_Alm11_Rotate_Trig",
    "A_Alm12_Blower_Trig",
    "HeatDelay_Tmr_Done",
    "Heat_xCall",
    "Heat_CurStep",
    "o_BurnerLoop",
    "y_BurnerLoop",
)


def run(name: str, animate: bool, sensor_steady: bool | None, budget: int) -> None:
    logic = importlib.import_module("tests.fixtures.tumbler").logic
    b = Bench(logic)
    b.force_physical()
    if not animate:
        # Defeat the bench's animation: force the steady value every scan.
        b._oscillate = lambda: b.force("x_RotateSensor", sensor_steady)  # type: ignore[method-assign]
    b.step()
    enter_production(b.plc)
    b.scan = b.plc.state.scan_id
    b.pulse("Cmd_State_Clear")
    b.pulse("Cmd_State_Reset")
    b.pulse("Cmd_State_Start")

    print(f"\n=== {name} (post-Start scan {b.scan}) ===")
    prev = {t: b.get(t) for t in WATCH}
    hit = False
    for _ in range(budget):
        b.step()
        for t in WATCH:
            cur = b.get(t)
            if cur != prev[t]:
                print(f"  scan {b.scan:6d}  {t}: {prev[t]!r} -> {cur!r}")
                prev[t] = cur
        if b.get("y_BurnerLoop") is True:
            print(f"  scan {b.scan:6d}  *** y_BurnerLoop TRUE ***")
            hit = True
            break
        if b.get("Sts_StateCurrent") == 9 and b.scan > 200:
            print(f"  scan {b.scan:6d}  *** ABORTED(9) — trajectory dead ***")
            break
    if not hit and b.get("Sts_StateCurrent") != 9:
        print(f"  budget {budget} exhausted: {b.snapshot(WATCH)}")


if __name__ == "__main__":
    run("A: animated sensor (happy path)", animate=True, sensor_steady=None, budget=4000)
    run("B: sensor steady False (OffWD 10s)", animate=False, sensor_steady=False, budget=4000)
    run("C: sensor steady True (OnWD 2s)", animate=False, sensor_steady=True, budget=4000)
