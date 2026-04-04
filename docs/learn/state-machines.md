# Lesson 7: State Machines

## The Python instinct

```python
state = "idle"
while True:
    if state == "idle":
        if entry_sensor:
            state = "detecting"
    elif state == "detecting":
        read_size()
        time.sleep(0.5)
        state = "sorting"
    # ...
```

## The ladder logic way

State machines in ladder logic use a tag for the current state, timers for durations, and `copy` for transitions. No `while`, no `sleep`, no blocking.

Here's the full sorting sequence: a box arrives, the system reads its size, positions the diverter, holds it open, then returns to idle.

```python
from pyrung import Bool, Int, Char, Program, Rung, PLCRunner, TimeMode, Tms
from pyrung import on_delay, copy, latch, reset, out, rise

# State tag
State = Char("State")  # "i"dle, "d"etecting, "s"orting, "c"ounting

# Inputs
EntrySensor   = Bool("EntrySensor")
SizeReading   = Int("SizeReading")
SizeThreshold = Int("SizeThreshold")

# Outputs
DiverterCmd = Bool("DiverterCmd")

# Internal
IsLarge  = Bool("IsLarge")
DetDone  = Bool("DetDone")
DetAcc   = Int("DetAcc")
HoldDone = Bool("HoldDone")
HoldAcc  = Int("HoldAcc")

with Program() as logic:
    # IDLE -> DETECTING: box arrives
    with Rung(State == "i", rise(EntrySensor)):
        copy("d", State)

    # DETECTING: read size for 0.5 seconds
    with Rung(State == "d"):
        on_delay(DetDone, DetAcc, preset=500, unit=Tms)
    with Rung(State == "d", SizeReading > SizeThreshold):
        latch(IsLarge)
    with Rung(DetDone):
        copy("s", State)

    # SORTING: hold diverter open for 2 seconds
    with Rung(State == "s"):
        on_delay(HoldDone, HoldAcc, preset=2000, unit=Tms)
    with Rung(State == "s", IsLarge):
        out(DiverterCmd)         # Extend diverter for large boxes
    with Rung(HoldDone):
        copy("c", State)

    # COUNTING: done, reset for next box
    with Rung(State == "c"):
        reset(IsLarge)
        copy("i", State)
```

Each state has a small group of rungs: one to run its timer or check its condition, one to handle the transition. Clean, readable, testable.

## Try it

```python
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)

with runner.active():
    State.value = "i"
    SizeThreshold.value = 100

    # Box arrives -- large box
    EntrySensor.value = True
    SizeReading.value = 150
runner.step()
with runner.active():
    assert State.value == "d"           # Detecting

# Wait for detection period (0.5s = 50 scans)
runner.run(cycles=50)
with runner.active():
    assert State.value == "s"           # Sorting
    assert DiverterCmd.value is True    # Diverter extended for large box

# Wait for hold (2s = 200 scans)
runner.run(cycles=200)
with runner.active():
    assert State.value == "c"           # Counting

runner.step()
with runner.active():
    assert State.value == "i"           # Back to idle
    assert DiverterCmd.value is False   # Diverter retracted
```

## Exercise

Add an error state. If the entry sensor stays active for more than 5 seconds during the detecting phase (the box is jammed), transition to state `"e"` (error) and turn on a `JamAlarm`. The jam clears only when the sensor goes false AND an operator presses an `AckButton`. Test both the jam path and the normal path.

---

> If you're a visual person, this is a good time to set up the [VS Code debugger](../guides/dap-vscode.md). From here on, the logic gets complex enough that stepping through scans and watching tags update live can be more useful than reading assertions.

The sorting sequence works in one mode. But a real conveyor has auto mode (runs the sequence) and manual mode (operator controls the diverter directly). That's OR logic and branches.
