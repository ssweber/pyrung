"""Lesson 7: State Machines — docs/learn/state-machines.md"""

# --- The ladder logic way ---

from pyrung import PLC, Bool, Int, Program, Rung, Timer, comment, copy, latch, on_delay, reset, rise

# State values as tag-constants — initialized once, never written
IDLE = Int("IDLE", default=0)
DETECTING = Int("DETECTING", default=1)
SORTING = Int("SORTING", default=2)
RESETTING = Int("RESETTING", default=3)

State = Int("State")

# Inputs
EntrySensor = Bool("EntrySensor")
SizeReading = Int("SizeReading")
SizeThreshold = Int("SizeThreshold")

# Internal
IsLarge = Bool("IsLarge")
DetTimer = Timer.clone("DetTimer")
HoldTimer = Timer.clone("HoldTimer")

with Program() as logic:
    comment("IDLE to DETECTING: box arrives")
    with Rung(State == IDLE, rise(EntrySensor)):
        copy(DETECTING, State)

    comment("DETECTING: read size for 0.5 seconds")
    with Rung(State == DETECTING):
        on_delay(DetTimer, 500)
    with Rung(State == DETECTING, SizeReading > SizeThreshold):
        latch(IsLarge)
    with Rung(DetTimer.Done):
        copy(SORTING, State)

    comment("SORTING: hold diverter for 2 seconds")
    with Rung(State == SORTING):
        on_delay(HoldTimer, 2000)
    with Rung(HoldTimer.Done):
        copy(RESETTING, State)

    comment("RESETTING: clean up and return to idle")
    with Rung(State == RESETTING):
        reset(IsLarge)
        copy(IDLE, State)

# --- Try it ---

with PLC(logic, dt=0.010) as plc:
    State.value = 0
    SizeThreshold.value = 100

    # Box arrives -- large box
    EntrySensor.value = True
    SizeReading.value = 150

    plc.step()
    assert State.value == 1  # DETECTING

    # Wait for detection period (0.5s = 50 scans)
    plc.run(cycles=50)
    assert State.value == 2  # SORTING
    assert IsLarge.value is True  # Classified as large

    # Wait for hold period + pass through RESETTING (2s = 200 scans)
    plc.run(cycles=200)
    assert State.value == 0  # Back to IDLE
    assert IsLarge.value is False  # Cleaned up in RESETTING
