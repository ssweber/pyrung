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

```
  IDLE --rise(Entry)--> DETECTING --0.5s--> SORTING --2s--> RESETTING --cleanup--> IDLE
```

```python
from pyrung import Bool, Int, Program, Rung, PLCRunner, TimeMode, Tms
from pyrung import comment, on_delay, copy, latch, reset, out, rise

# State tag — 0=idle, 1=detecting, 2=sorting, 3=resetting
State = Int("State")

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
    comment("IDLE to DETECTING: box arrives")
    with Rung(State == 0, rise(EntrySensor)):
        copy(1, State)

    comment("DETECTING: read size for 0.5 seconds")
    with Rung(State == 1):
        on_delay(DetDone, DetAcc, preset=500, unit=Tms)
    with Rung(State == 1, SizeReading > SizeThreshold):
        latch(IsLarge)
    with Rung(DetDone):
        copy(2, State)

    comment("SORTING: hold diverter open for 2 seconds")
    with Rung(State == 2):
        on_delay(HoldDone, HoldAcc, preset=2000, unit=Tms)
    with Rung(State == 2, IsLarge):
        out(DiverterCmd)         # Extend diverter for large boxes
    with Rung(HoldDone):
        copy(3, State)

    comment("RESETTING: clean up for next box")
    with Rung(State == 3):
        reset(IsLarge)
        copy(0, State)
```

Each state has a small group of rungs: one to run its timer or check its condition, one to handle the transition. Clean, readable, testable.

## Try it

```python
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)

with runner.active():
    State.value = 0
    SizeThreshold.value = 100

    # Box arrives -- large box
    EntrySensor.value = True
    SizeReading.value = 150
runner.step()
with runner.active():
    assert State.value == 1             # Detecting

# Wait for detection period (0.5s = 50 scans)
runner.run(cycles=50)
with runner.active():
    assert State.value == 2             # Sorting
    assert DiverterCmd.value is True    # Diverter extended for large box

# Wait for hold (2s = 200 scans)
runner.run(cycles=200)
with runner.active():
    assert State.value == 3             # Resetting

runner.step()
with runner.active():
    assert State.value == 0             # Back to idle
    assert DiverterCmd.value is False   # Diverter retracted
```

!!! info "Also known as..."

    State machines in ladder are almost always hand-rolled using an Int tag plus comparison contacts, or built on a dedicated sequencer instruction (`SQO`/`SQI`/`SQL`, `DRUM`). IEC 61131-3 has Sequential Function Chart (SFC) as a first-class language for this. For standardized state models, search for **PackML** — it defines ~17 states that any operator from any vendor recognizes.

## Exercise

Add an error state (`4`). If the entry sensor stays active for more than 5 seconds during the detecting phase (the box is jammed), transition to state `4` (error) and turn on a `JamAlarm`. The jam clears only when the sensor goes false AND an operator presses an `AckButton`. Test both the jam path and the normal path.

---

> If you're a visual person, this is a good time to set up the [VS Code debugger](../guides/dap-vscode.md). From here on, the logic gets complex enough that stepping through scans and watching tags update live can be more useful than reading assertions.

The sorting sequence works in one mode. But a real conveyor has auto mode (runs the sequence) and manual mode (operator controls the diverter directly). That's OR logic and branches.
