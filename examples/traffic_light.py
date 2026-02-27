"""Traffic light example using @udt for structured tags.

Demonstrates:
  1. Structured tags with @udt() and class-qualified usage
  2. Timer-driven state machine (green -> yellow -> red -> green)
  3. Edge-triggered car counter with rise()
  4. Speed history log using blockcopy to shift a window
  5. Running a simulation with PLCRunner and FIXED_STEP timing
"""

import os

from pyrung import (
    udt,
    Block,
    Bool,
    Char,
    Dint,
    Int,
    PLCRunner,
    Rung,
    TagType,
    TimeMode,
    Tms,
    blockcopy,
    copy,
    count_up,
    on_delay,
    program,
    rise,
)


# ---------------------------------------------------------------------------
# 1. Tag declarations
# ---------------------------------------------------------------------------
# Traffic light state: "g"reen, "y"ellow, "r"ed
State = Char("State")

@udt()
class Tmr:
    # One timer per transition (done bit + accumulator)
    GreenDone: Bool
    GreenAcc: Int

    YellowDone: Bool
    YellowAcc: Int

    RedDone: Bool
    RedAcc: Int

@udt()
class Car:
    # Car counter: rising-edge sensor
    Sensor: Bool
    CountDone: Bool
    CountAcc: Dint
    CountReset: Bool

    # Speed history log (5 slots, newest at DS1)
    SpeedIn: Int
    LogEnable: Bool

# Memory blocks for speed history log.
DS = Block("DS", TagType.INT, 1, 5)


# ---------------------------------------------------------------------------
# 2. Traffic light state machine
# ---------------------------------------------------------------------------
@program
def logic():

    # Green phase: 3 000 ms then transition to yellow
    with Rung(State == "g"):
        on_delay(Tmr.GreenDone, Tmr.GreenAcc, preset=3000, unit=Tms)

    with Rung(Tmr.GreenDone):
        copy("y", State)

    # Yellow phase: 1 000 ms then transition to red
    with Rung(State == "y"):
        on_delay(Tmr.YellowDone, Tmr.YellowAcc, preset=1000, unit=Tms)

    with Rung(Tmr.YellowDone):
        copy("r", State)

    # Red phase: 3 000 ms then transition to green
    with Rung(State == "r"):
        on_delay(Tmr.RedDone, Tmr.RedAcc, preset=3000, unit=Tms)

    with Rung(Tmr.RedDone):
        copy("g", State)

    # ------------------------------------------------------------------
    # 3. Car counter: count rising edges of CarSensor
    # ------------------------------------------------------------------
    with Rung(rise(Car.Sensor)):
        count_up(Car.CountDone, Car.CountAcc, preset=9999).reset(Car.CountReset)

    # ------------------------------------------------------------------
    # 4. Speed history: shift DS2..DS4 -> DS3..DS5 then write new value
    # ------------------------------------------------------------------
    with Rung(rise(Car.LogEnable)):
        blockcopy(DS.select(1, 4), DS.select(2, 5))  # shift up
        copy(Car.SpeedIn, DS[1])                 # newest into slot 1


# ---------------------------------------------------------------------------
# 5. Run the simulation
# ---------------------------------------------------------------------------
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10 ms per scan

# Initialize state to green
with runner.active():
    State.value = "g"

if os.getenv("PYRUNG_DAP_ACTIVE") != "1":
    runner.step()

    # Simulate a few car detections and speed readings
    for speed in (45, 52, 38):
        with runner.active():
            Car.Sensor.value = True
            Car.LogEnable.value = True
            Car.SpeedIn.value = speed
            runner.step()
            Car.Sensor.value = False
            Car.LogEnable.value = False
            runner.step()

    # Let the light cycle run for 10 seconds (1 000 scans x 10 ms)
    runner.run(cycles=1000)

    # -----------------------------------------------------------------------
    # Print results
    # -----------------------------------------------------------------------
    with runner.active():
        print(f"Light state : {State.value}")
        print(f"Sim time    : {runner.simulation_time:.1f} s")
        print(f"Cars counted: {Car.CountAcc.value}")
        print(f"Speed log   : {[DS[i].value for i in range(1, 6)]}")
