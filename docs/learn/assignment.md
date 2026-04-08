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
LastSize    = Int("LastSize")         # Saved reading for this box
SortCount   = Int("SortCount")       # Total boxes sorted
CycleCount  = Int("CycleCount")      # Scans since startup

with Program() as logic:
    with Rung(rise(EntrySensor)):
        copy(BoxSize, LastSize)           # Snapshot the size reading
        calc(SortCount + 1, SortCount)    # Increment total

    with Rung():
        calc(CycleCount + 1, CycleCount)  # Always counting (every scan)
```

`copy` moves a value into a tag. `calc` evaluates an expression and stores the result. Both are instructions that only execute when their rung has power. A `copy` inside a rung that's false simply doesn't happen, and the destination keeps whatever value it had.

`rise(EntrySensor)` fires for exactly one scan when the sensor goes from False to True. Without it, the copy and calc would execute every scan while the sensor is active.

```
  rise(EntrySensor) -- fires one scan:
      BoxSize --copy--> LastSize
      SortCount + 1 --calc--> SortCount

  Every scan:
      CycleCount + 1 --calc--> CycleCount
```

## Try it

```python
runner = PLCRunner(logic)
with runner.active():
    BoxSize.value = 150
    EntrySensor.value = True
    runner.step()
    assert LastSize.value == 150
    assert SortCount.value == 1

    EntrySensor.value = False
    runner.step()
    assert SortCount.value == 1       # rise() only fires once
    assert CycleCount.value == 2      # Unconditional rung runs every scan
```

## copy vs calc

These two handle overflow differently, and the difference matters. `copy` clamps: if you copy 50000 into a 16-bit signed Int, you get 32767 (the max). `calc` wraps: if an Int at 32767 has 1 added, it rolls to -32768. Clamping is safer for data movement; wrapping matches how real PLC arithmetic hardware behaves.

## Unconditional rungs

Notice `Rung()` with no condition. That rung is always true, so its instructions execute every scan. This is how you compute values that should always be current, like a cycle counter or a scaled analog reading.

!!! info "Also known as..."

    `copy` is `MOV`, `COP`, or `MOVE`. `calc` is `MATH` or `CPT` (or an expression in Structured Text). `rise()` and `fall()` are one-shots (`ONS`/`OSR`), positive/negative edge triggers (`R_TRIG`/`F_TRIG`), or "leading-edge" / "trailing-edge" contacts. An unconditional rung is "always on" — some PLCs expose a special bit (`SP1`, `S:1/15`), others just wire straight from the rail.

## Exercise

Create a `PreviousSize` tag. Each time a new box arrives (`rise(EntrySensor)`), copy the current `LastSize` to `PreviousSize` before copying the new `BoxSize` into `LastSize`. Test that after two boxes (sizes 100 and 200), `LastSize` is 200 and `PreviousSize` is 100.

---

The conveyor needs to wait -- hold the diverter gate open long enough for the box to pass through. Python would `sleep`. A PLC can't sleep. That's where timers come in.
