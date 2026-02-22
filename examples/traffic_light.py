"""Traffic light example using AutoTag for auto naming.

Demonstrates:
  1. Auto naming with class Devices(AutoTag) + flat aliases
  2. Timer-driven state machine (green -> yellow -> red -> green)
  3. Edge-triggered car counter with rise()
  4. Speed history log using blockcopy to shift a window
  5. Running a simulation with PLCRunner and FIXED_STEP timing
"""

import os

from pyrung.core import *


# ---------------------------------------------------------------------------
# 1. Tag declarations
# ---------------------------------------------------------------------------
class Devices(AutoTag):
    # Traffic light state: "g"reen, "y"ellow, "r"ed
    State = Char()

    # One timer per transition (done bit + accumulator)
    GreenDone = Bool()
    GreenAcc = Int()

    YellowDone = Bool()
    YellowAcc = Int()

    RedDone = Bool()
    RedAcc = Int()

    # Car counter: rising-edge sensor
    CarSensor = Bool()
    CarCountDone = Bool()
    CarCountAcc = Dint()
    CountReset = Bool()

    # Speed history log (5 slots, newest at DS1)
    SpeedIn = Int()
    LogEnable = Bool()

# Memory blocks are declared outside AutoTag classes.
DS = Block("DS", TagType.INT, 1, 5)


# Flatten names into module scope (no namespace-style usage required).
Devices.export(globals())


# ---------------------------------------------------------------------------
# 2. Traffic light state machine
# ---------------------------------------------------------------------------
@program
def logic():

    # Green phase: 3 000 ms then transition to yellow
    with Rung(State == "g"):
        on_delay(GreenDone, GreenAcc, preset=3000, unit=Tms)

    with Rung(GreenDone):
        copy("y", State)

    # Yellow phase: 1 000 ms then transition to red
    with Rung(State == "y"):
        on_delay(YellowDone, YellowAcc, preset=1000, unit=Tms)

    with Rung(YellowDone):
        copy("r", State)

    # Red phase: 3 000 ms then transition to green
    with Rung(State == "r"):
        on_delay(RedDone, RedAcc, preset=3000, unit=Tms)

    with Rung(RedDone):
        copy("g", State)

    # ------------------------------------------------------------------
    # 3. Car counter: count rising edges of CarSensor
    # ------------------------------------------------------------------
    with Rung(rise(CarSensor)):
        count_up(CarCountDone, CarCountAcc, preset=9999).reset(CountReset)

    # ------------------------------------------------------------------
    # 4. Speed history: shift DS2..DS4 -> DS3..DS5 then write new value
    # ------------------------------------------------------------------
    with Rung(rise(LogEnable)):
        blockcopy(DS.select(1, 4), DS.select(2, 5))  # shift up
        copy(SpeedIn, DS[1])                         # newest into slot 1


# ---------------------------------------------------------------------------
# 5. Run the simulation
# ---------------------------------------------------------------------------
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)  # 100 ms per scan

# Initialize state to green
with runner.active():
    State.value = "g"

if os.getenv("PYRUNG_DAP_ACTIVE") != "1":
    runner.step()

    # Simulate a few car detections and speed readings
    for speed in (45, 52, 38):
        with runner.active():
            CarSensor.value = True
            LogEnable.value = True
            SpeedIn.value = speed
        runner.step()
        with runner.active():
            CarSensor.value = False
            LogEnable.value = False
        runner.step()

    # Let the light cycle run for 10 seconds (1 000 scans x 10 ms)
    runner.run(cycles=100)

    # -----------------------------------------------------------------------
    # Print results
    # -----------------------------------------------------------------------
    with runner.active():
        print(f"Light state : {State.value}")
        print(f"Sim time    : {runner.simulation_time:.1f} s")
        print(f"Cars counted: {CarCountAcc.value}")
        print(f"Speed log   : {[DS[i].value for i in range(1, 6)]}")
