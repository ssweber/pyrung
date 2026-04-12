"""Traffic light example using @udt for structured tags.

Demonstrates:
  1. Structured tags with @udt() and class-qualified usage
  2. Timer-driven state machine (green -> yellow -> red -> green)
  3. Edge-triggered car counter with rise()
  4. Speed history log using blockcopy to shift a window
  5. Running a simulation with PLC and FIXED_STEP timing
"""

import os

from pyrung import (
    PLC,
    Block,
    Bool,
    Char,
    Counter,
    Int,
    Rung,
    TagType,
    Timer,
    blockcopy,
    copy,
    count_up,
    on_delay,
    program,
    rise,
    udt,
)

# ---------------------------------------------------------------------------
# 1. Tag declarations
# ---------------------------------------------------------------------------
# Traffic light state: "g"reen, "y"ellow, "r"ed
State = Char("State")

GreenTimer = Timer.clone("GreenTimer")
YellowTimer = Timer.clone("YellowTimer")
RedTimer = Timer.clone("RedTimer")


@udt()
class Car:
    # Car counter: rising-edge sensor
    Sensor: Bool
    CountReset: Bool

    # Speed history log (5 slots, newest at DS1)
    SpeedIn: Int
    LogEnable: Bool


CarCounter = Counter.clone("CarCounter")

# Memory blocks for speed history log.
DS = Block("DS", TagType.INT, 1, 5)


# ---------------------------------------------------------------------------
# 2. Traffic light state machine
# ---------------------------------------------------------------------------
@program
def logic():

    # Green phase: 3 000 ms then transition to yellow
    with Rung(State == "g"):
        on_delay(GreenTimer, 3000)

    with Rung(GreenTimer.Done):
        copy("y", State)

    # Yellow phase: 1 000 ms then transition to red
    with Rung(State == "y"):
        on_delay(YellowTimer, 1000)

    with Rung(YellowTimer.Done):
        copy("r", State)

    # Red phase: 3 000 ms then transition to green
    with Rung(State == "r"):
        on_delay(RedTimer, 3000)

    with Rung(RedTimer.Done):
        copy("g", State)

    # ------------------------------------------------------------------
    # 3. Car counter: count rising edges of CarSensor
    # ------------------------------------------------------------------
    with Rung(rise(Car.Sensor)):
        count_up(CarCounter, preset=9999).reset(Car.CountReset)

    # ------------------------------------------------------------------
    # 4. Speed history: shift DS2..DS4 -> DS3..DS5 then write new value
    # ------------------------------------------------------------------
    with Rung(rise(Car.LogEnable)):
        blockcopy(DS.select(1, 4), DS.select(2, 5))  # shift up
        copy(Car.SpeedIn, DS[1])  # newest into slot 1


# ---------------------------------------------------------------------------
# 5. Run the simulation
# ---------------------------------------------------------------------------
runner = PLC(logic, dt=0.010)

# Initialize state to green
with runner:
    State.value = "g"

if os.getenv("PYRUNG_DAP_ACTIVE") != "1":
    runner.step()

    # Simulate a few car detections and speed readings
    for speed in (45, 52, 38):
        with runner:
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
    with runner:
        print(f"Light state : {State.value}")
        print(f"Sim time    : {runner.simulation_time:.1f} s")
        print(f"Cars counted: {CarCounter.Acc.value}")
        print(f"Speed log   : {[DS[i].value for i in range(1, 6)]}")
