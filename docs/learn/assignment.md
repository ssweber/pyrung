# Lesson 4: Assignment

## The Python instinct

```python
last_size = current_size
total_boxes = total_boxes + 1
```

Assignment is so fundamental in Python that it barely registers as a concept. You have `=` and you're done.

## The ladder logic way

In ladder logic, moving data is an explicit instruction that lives on the instruction side of a rung. It executes when the rung is true and does nothing when the rung is false.

```python
from pyrung import Bool, Int, Program, Rung, PLCRunner, copy, calc, rise

EntrySensor = Bool("EntrySensor")
BoxSize     = Int("BoxSize")          # Raw sensor reading
CurrentSize = Int("CurrentSize")     # Snapshot of this box's reading
SortCount   = Int("SortCount")       # Total boxes sorted
CycleCount  = Int("CycleCount")      # Scans since startup

with Program() as logic:
    with Rung(rise(EntrySensor)):
        copy(BoxSize, CurrentSize)           # Snapshot the size reading
        calc(SortCount + 1, SortCount)    # Increment total

    with Rung():
        calc(CycleCount + 1, CycleCount)  # Always counting (every scan)
```

`copy` moves a value into a tag. `calc` evaluates an expression and stores the result. Both are instructions that only execute when their rung has power. A `copy` inside a rung that's false simply doesn't happen, and the destination keeps whatever value it had.

```
  rise(EntrySensor) -- fires one scan:
      BoxSize --copy--> CurrentSize
      SortCount + 1 --calc--> SortCount

  Every scan:
      CycleCount + 1 --calc--> CycleCount
```

## Edge detection: `rise()` and `fall()`

`rise(EntrySensor)` fires for exactly one scan when the sensor transitions from False to True. Without it, the copy and calc above would execute *every scan* while the sensor stays active. If a box sits on the sensor for 100 scans, you'd get 100 copies and 100 increments instead of one.

This is the biggest conceptual jump from Python. In Python, `if sensor:` is about the *current value*. In ladder, `rise()` is about the *transition* — it detects the leading edge and fires once. `fall()` does the same for the trailing edge (True → False). You'll use `rise()` constantly from here on: edge-triggered counting in [Lesson 6](counters.md), state transitions in [Lesson 7](state-machines.md), and anywhere you need "do this once when the condition changes."

## Try it

```python
runner = PLCRunner(logic)
with runner.active():
    BoxSize.value = 150
    EntrySensor.value = True
    runner.step()
    assert CurrentSize.value == 150
    assert SortCount.value == 1

    EntrySensor.value = False
    runner.step()
    assert SortCount.value == 1       # rise() only fires once
    assert CycleCount.value == 2      # Unconditional rung runs every scan
```

## copy vs calc

These two handle overflow differently, and the difference matters. `copy` clamps: if you copy 50000 into a 16-bit signed Int, you get 32767 (the max). `calc` wraps: if an Int at 32767 has 1 added, it rolls to -32768. Reach for `copy` when moving data (don't silently roll over a sensor reading) and `calc` for arithmetic (wrapping matches how real PLC counters and accumulators behave).

Note the argument order: `copy(source, dest)` reads like an assignment left-to-right. Some vendors (Rockwell's MOV) use the same direction; Click's editor displays it destination-first. pyrung always uses source-first.

## Unconditional rungs

Notice `Rung()` with no condition. That rung is always true, so its instructions execute every scan. This is how you compute values that should always be current, like a cycle counter or a scaled analog reading.

!!! info "Also known as..."

    `copy` is `MOV`, `COP`, or `MOVE`. `calc` is `MATH` or `CPT` (or an expression in Structured Text). `rise()` and `fall()` are one-shots (`ONS`/`OSR`), positive/negative edge triggers (`R_TRIG`/`F_TRIG`), or "leading-edge" / "trailing-edge" contacts. An unconditional rung is "always on" — some PLCs expose a special bit (`SP1`, `S:1/15`), others just wire straight from the rail.

## Exercise

Remember "order has meaning" from [Lesson 1](scan-cycle.md)? It applies within a rung too: instructions execute top-to-bottom, in the order you write them. That matters here.

Create a `PreviousSize` tag. Each time a new box arrives (`rise(EntrySensor)`), copy `CurrentSize` to `PreviousSize` before copying the new `BoxSize` into `CurrentSize`. Test that after two boxes (sizes 100 and 200), `CurrentSize` is 200 and `PreviousSize` is 100. Then swap the two copies and run the test again -- verify that `PreviousSize` gets the *wrong* value. Understand why before you swap them back.

---

The conveyor needs to wait -- hold the diverter gate open long enough for the box to pass through. Python would `sleep`. A PLC can't sleep. That's where timers come in.
