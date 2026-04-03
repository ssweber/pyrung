"""Click PLC hello world — motor control with temperature zones.

Demonstrates the full pyrung → Click workflow:
  1. @named_array for multi-instance structured data
  2. TagMap linking logical tags to Click hardware addresses
  3. Start/stop motor control with run timer and start counter
  4. Simulation with PLCRunner in FIXED_STEP mode

This example is also used to generate the starter project release asset.
See ``devtools/build_release_assets.py``.
"""

import os

from pyrung import (
    Bool,
    Dint,
    Int,
    PLCRunner,
    Rung,
    TimeMode,
    Tms,
    any_of,
    auto,
    count_up,
    named_array,
    on_delay,
    out,
    program,
    rise,
)
from pyrung.click import TagMap, c, ct, ctd, ds, t, td, x, y

# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------
Start = Bool("Start")  # X001 — momentary start button
Stop = Bool("Stop")  # X002 — momentary stop button
Motor = Bool("Motor")  # Y001 — motor contactor
Alarm = Bool("Alarm")  # Y002 — high-temp indicator


@named_array(Int, count=4, stride=2)
class Zone:
    temp = auto()  # current reading (tenths °F)
    setpoint = auto()  # alarm threshold (tenths °F)


# Timer / counter use Click hardware blocks
RunDone = Bool("RunDone")  # T1  — run-time limit reached
RunAcc = Int("RunAcc")  # TD1 — run-time accumulator (ms)
StartsDone = Bool("StartsDone")  # CT1 — start-count limit reached
StartsAcc = Dint("StartsAcc")  # CTD1 — total motor starts
StartsReset = Bool("StartsReset")  # C1 — counter reset

# ---------------------------------------------------------------------------
# Click hardware mapping
# ---------------------------------------------------------------------------
mapping = TagMap(
    [
        Start.map_to(x[1]),
        Stop.map_to(x[2]),
        Motor.map_to(y[1]),
        Alarm.map_to(y[2]),
        *Zone.map_to(ds.select(1, 8)),  # 4 zones × 2 fields = DS1..DS8
        RunDone.map_to(t[1]),
        RunAcc.map_to(td[1]),
        StartsDone.map_to(ct[1]),
        StartsAcc.map_to(ctd[1]),
        StartsReset.map_to(c[1]),
    ],
    include_system=False,
)

# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------


@program
def logic():
    # Start/stop latch — motor stays on until Stop is pressed
    with Rung(Start | Motor, ~Stop):
        out(Motor)

    # Run timer — counts milliseconds while motor is on
    with Rung(Motor):
        on_delay(RunDone, RunAcc, preset=32767, unit=Tms)

    # Count motor start events
    with Rung(rise(Motor)):
        count_up(StartsDone, StartsAcc, preset=9999).reset(StartsReset)

    # High-temp alarm — flag if any zone exceeds its setpoint
    with Rung(
        any_of(
            Zone[1].temp > Zone[1].setpoint,
            Zone[2].temp > Zone[2].setpoint,
            Zone[3].temp > Zone[3].setpoint,
            Zone[4].temp > Zone[4].setpoint,
        )
    ):
        out(Alarm)


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10 ms per scan

if os.getenv("PYRUNG_DAP_ACTIVE") != "1":
    with runner.active():
        # Load zone setpoints
        for i in range(1, 5):
            Zone[i].setpoint.value = 750  # 75.0 °F

        # Press Start
        runner.patch({Start.name: True})
        runner.step()
        runner.patch({Start.name: False})

        # Simulate temperature readings (zone 3 over setpoint)
        Zone[1].temp.value = 720
        Zone[2].temp.value = 740
        Zone[3].temp.value = 760
        Zone[4].temp.value = 710

    runner.run(cycles=100)

    # Report
    with runner.active():
        print(f"Motor     : {'ON' if Motor.value else 'OFF'}")
        print(f"Alarm     : {'ON' if Alarm.value else 'OFF'}")
        print(f"Starts    : {StartsAcc.value}")
        print(f"Run time  : {RunAcc.value} ms")
        for i in range(1, 5):
            t_val = Zone[i].temp.value
            sp = Zone[i].setpoint.value
            flag = " ** HIGH **" if t_val > sp else ""
            print(f"  Zone {i}  : {t_val/10:.1f}°F  (SP {sp/10:.1f}°F){flag}")
