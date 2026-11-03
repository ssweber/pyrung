"""how() and a program that stops itself.

A rung that sets ``sys.cmd_mode_stop`` ends the recording: the next scan is a restart
with scan ids from 0.  A plan is the scans of one recording, so how() says it stopped
rather than step over it, or plan a route stepping would never run.
"""

from __future__ import annotations

import pytest

from pyrung import PLC, Bool, Program, Rung, Timer, on_delay, out
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.system_points import system

StopTimer = Timer.clone("StopTimer")
LampTimer = Timer.clone("LampTimer")
Lamp = Bool("Lamp")


@pytest.mark.parametrize(
    "stop_at, lamp_at",
    [(100, 300), (20_000, 30_000)],
    ids=["interpreted", "at the end of a stretch"],
)
def test_a_stop_before_the_target_is_the_answer(stop_at: int, lamp_at: int) -> None:
    with Program() as logic:
        with Rung():
            on_delay(StopTimer, preset=stop_at)
        with Rung(StopTimer.Done):
            out(system.sys.cmd_mode_stop)
        with Rung():
            on_delay(LampTimer, preset=lamp_at)
        with Rung(LampTimer.Done):
            out(Lamp)
    plc = PLC(logic, dt=0.001)

    # Stepped: the program stops one scan after the timer is done, and starts again from
    # nothing, so the lamp never comes on.
    stepped = plc.fork()
    while stepped._running:
        stepped.step()
    assert stepped.state.scan_id == stop_at + 1
    for _ in range(lamp_at // 100):
        stepped.step()
    assert stepped.state.scan_id == lamp_at // 100

    plan = plc.how(Lamp, max_scans=4000)
    assert plan.status is PlanStatus.STOPPED
    assert plan.reason == (
        f"the program stopped itself at scan {stop_at + 1} and its next scan would "
        "restart it; how() does not plan across a restart"
    )
