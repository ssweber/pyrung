"""Fill station — the example from 'Gradual Typing for Ladder Logic.'

A tank, a fill valve, a flow sensor, and a level sensor.
Open the valve, watch the flow, stop at level. A watchdog
timer catches a dead flow sensor and an alarm fires if it trips.

Run under DAP to demo with ``pyrung live``.
"""

import os

from pyrung import (
    Bool,
    Harness,
    Int,
    Or,
    Physical,
    PLC,
    Rung,
    Timer,
    calc,
    latch,
    on_delay,
    out,
    program,
    reset,
    rise,
    fall,
)
from pyrung.core.analysis import Proven, prove

# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
StartBtn = Bool("StartBtn", public=True)
FillEnable = Bool("FillEnable", public=True)
FillValve = Bool("FillValve", public=True)

FlowSensor = Bool(
    "FlowSensor",
    external=True,
    physical=Physical("FlowSensor", on_delay="200ms", off_delay="100ms"),
    link="FillValve",
)
LevelSensor = Bool("LevelSensor", external=True)

FaultTimer = Timer.clone("FaultTimer")
FlowAlarm = Bool("FlowAlarm", public=True)
AlarmExtent = Int("AlarmExtent", public=True)

# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------


@program
def logic():
    # Start fill — blocked by level and alarm
    with Rung(StartBtn, ~LevelSensor, ~FlowAlarm):
        latch(FillEnable)
    with Rung(LevelSensor):
        reset(FillEnable)
    with Rung(FlowAlarm):
        reset(FillEnable)

    with Rung(FillEnable):
        out(FillValve)

    # Watchdog: valve open but no flow within 3 seconds
    with Rung(FillValve, ~FlowSensor):
        on_delay(FaultTimer, 3000)
    with Rung(FaultTimer.Done):
        latch(FlowAlarm)

    # Alarm extent — nonzero when any alarm active
    with Rung(rise(FlowAlarm)):
        calc(AlarmExtent + 1, AlarmExtent)
    with Rung(fall(FlowAlarm)):
        calc(AlarmExtent - 1, AlarmExtent)


# ---------------------------------------------------------------------------
# Verify — does every fault reach an alarm?
# ---------------------------------------------------------------------------
result = prove(logic, Or(~FillEnable, FlowSensor, AlarmExtent != 0))
status = "PROVEN" if isinstance(result, Proven) else f"FAIL: {result}"
print(f"Fault coverage: {status}")

# ---------------------------------------------------------------------------
# Simulate
# ---------------------------------------------------------------------------
runner = PLC(logic, dt=0.010)

if os.getenv("PYRUNG_DAP_ACTIVE") != "1":
    harness = Harness(runner)
    harness.install()

    def status_line(label: str) -> None:
        with runner:
            valve = "OPEN" if FillValve.value else "closed"
            flow = "active" if FlowSensor.value else "inactive"
            alarm = "ALARM" if FlowAlarm.value else "clear"
            level = "FULL" if LevelSensor.value else "filling"
        print(f"  {label:<20} valve={valve:<6} flow={flow:<8} alarm={alarm:<5} level={level}")

    # Normal fill cycle
    print("\n=== Normal fill ===")
    runner.patch({StartBtn: True})
    runner.run_for(0.5)
    status_line("start + 0.5s")

    runner.run_for(1.5)
    status_line("start + 2.0s")

    # Tank reaches level
    runner.patch({LevelSensor: True})
    runner.run_for(0.5)
    status_line("tank full")

    # Fault scenario: new fill, then the flow sensor dies
    print("\n=== Fault scenario ===")
    runner.patch({LevelSensor: False, StartBtn: True})
    runner.run_for(1.0)
    status_line("filling normally")

    runner.patch({FlowSensor: False})
    runner.run_for(2.0)
    status_line("flow lost + 2s")

    runner.run_for(1.5)
    status_line("flow lost + 3.5s")
