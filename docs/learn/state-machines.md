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
from pyrung import comment, on_delay, copy, latch, reset, rise

# State values as tag-constants — initialized once, never written
IDLE      = Int("IDLE",      default=0)
DETECTING = Int("DETECTING", default=1)
SORTING   = Int("SORTING",   default=2)
RESETTING = Int("RESETTING", default=3)

State = Int("State")

# Inputs
EntrySensor   = Bool("EntrySensor")
SizeReading   = Int("SizeReading")
SizeThreshold = Int("SizeThreshold")

# Internal
IsLarge  = Bool("IsLarge")
DetDone  = Bool("DetDone")
DetAcc   = Int("DetAcc")
HoldDone = Bool("HoldDone")
HoldAcc  = Int("HoldAcc")

with Program() as logic:
    comment("IDLE to DETECTING: box arrives")
    with Rung(State == IDLE, rise(EntrySensor)):
        copy(DETECTING, State)

    comment("DETECTING: read size for 0.5 seconds")
    with Rung(State == DETECTING):
        on_delay(DetDone, DetAcc, preset=500, unit=Tms)
    with Rung(State == DETECTING, SizeReading > SizeThreshold):
        latch(IsLarge)
    with Rung(DetDone):
        copy(SORTING, State)

    comment("SORTING: hold diverter for 2 seconds")
    with Rung(State == SORTING):
        on_delay(HoldDone, HoldAcc, preset=2000, unit=Tms)
    with Rung(HoldDone):
        copy(RESETTING, State)

    comment("RESETTING: clean up and return to idle")
    with Rung(State == RESETTING):
        reset(IsLarge)
        copy(IDLE, State)
```

Each state has a small group of rungs: one to run its timer or check its condition, one to handle the transition. Clean, readable, testable.

The state values are **tag-constants** — `Int` tags initialized once and never written. Your Python instinct says `Enum`; the ladder answer is "constants are tags." They live in the PLC's tag table, visible to anyone who opens the project — better documentation than a Python comment because they travel with the project file.

A few things to notice in the code:

- **`rise(EntrySensor)`** — remember [Lesson 4](assignment.md)? Without it, the IDLE→DETECTING transition fires every scan the sensor sees a box, not just the first.
- **`State == DETECTING` repeats across three rungs.** In Python you'd write one `if` and nest. In ladder, each rung stands alone — independently editable, grep-able, and deletable. The maintenance tech at 3am searching for `DETECTING` finds every rung that participates.
- **We never reset `DetDone`.** Once `State` leaves DETECTING, the `on_delay` rung goes false and the TON auto-resets — `DetDone` clears on its own. That's the [TON behavior from Lesson 5](timers.md).
- **`IsLarge` crosses states.** It's latched in DETECTING and reset in RESETTING. [Lesson 8](branches.md) reads it in the diverter output rung. Latches outlive rungs — they're how a state machine carries data between states without globals or context objects.

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
    assert State.value == 1             # DETECTING

    # Wait for detection period (0.5s = 50 scans)
    runner.run(cycles=50)
    assert State.value == 2             # SORTING
    assert IsLarge.value is True        # Classified as large

    # Wait for hold period + pass through RESETTING (2s = 200 scans)
    runner.run(cycles=200)
    assert State.value == 0             # Back to IDLE
    assert IsLarge.value is False       # Cleaned up in RESETTING
```

RESETTING is a **pass-through state** — it transitions to IDLE in the same scan. That's fine; its job is to clean up (`reset(IsLarge)`, `copy(IDLE, State)`), and cleanup doesn't need to wait. If you want to observe it, use `runner.monitor(State, callback)` — it fires on every committed change, including mid-cycle transitions.

!!! info "Also known as..."

    State machines in ladder are almost always hand-rolled using an Int tag plus comparison contacts, or built on a dedicated sequencer instruction (`SQO`/`SQI`/`SQL`, `DRUM`). IEC 61131-3 has Sequential Function Chart (SFC) as a first-class language for this. For standardized state models, search for **PackML** — it defines ~17 states that any operator from any vendor recognizes.

## Exercise

Add an error state (`4`). If the entry sensor stays active for more than 5 seconds during the detecting phase (the box is jammed), transition to state `4` (error) and turn on a `JamAlarm`. The jam clears only when the sensor goes false AND an operator presses an `AckButton`. Test both the jam path and the normal path.

---

> If you're a visual person, this is a good time to set up the [VS Code debugger](../guides/dap-vscode.md). From here on, the logic gets complex enough that stepping through scans and watching tags update live can be more useful than reading assertions.

The sorting sequence works in one mode. But a real conveyor has auto mode (runs the sequence) and manual mode (operator controls the diverter directly). That's OR logic and branches.
