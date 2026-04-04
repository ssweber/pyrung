# Lesson 4: Assignment

## The Python instinct

```python
state = "green"
speed = speed + 10
total = price * quantity
```

Assignment is so fundamental in Python that it barely registers as a concept. You have `=` and you're done.

## The ladder logic way

In ladder logic, moving data is an explicit instruction that lives on the instruction side of a rung. It executes when the rung is true and does nothing when the rung is false.

```python
from pyrung import Bool, Int, Char, Program, Rung, copy, calc

State    = Char("State")
Speed    = Int("Speed")
Total    = Int("Total")
Price    = Int("Price")
Quantity = Int("Quantity")
GoFast   = Bool("GoFast")
NextStep = Bool("NextStep")

with Program() as logic:
    with Rung(NextStep):
        copy("y", State)              # State = "y"

    with Rung(GoFast):
        calc(Speed + 10, Speed)       # Speed = Speed + 10

    with Rung():
        calc(Price * Quantity, Total)  # Total = Price * Quantity (every scan)
```

`copy` moves a value into a tag. `calc` evaluates an expression and stores the result. Both are instructions that only execute when their rung has power. A `copy` inside a rung that's false simply doesn't happen, and the destination keeps whatever value it had.

## copy vs calc

These two handle overflow differently, and the difference matters. `copy` clamps: if you copy 50000 into a 16-bit signed Int, you get 32767 (the max). `calc` wraps: if an Int at 32767 has 1 added, it rolls to -32768. Clamping is safer for data movement; wrapping matches how real PLC arithmetic hardware behaves.

## Unconditional rungs

Notice `Rung()` with no condition. That rung is always true, so its instructions execute every scan. This is how you compute values that should always be current, like a running total or a scaled analog reading.

## Exercise

Create a step counter that starts at 0. Each time a button is pressed (use `rise()`), copy the current step into a `PreviousStep` tag, then `calc` the step plus 1 back into `Step`. Test that after 3 presses, `Step` is 3 and `PreviousStep` is 2.
