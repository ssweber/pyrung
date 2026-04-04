# Lesson 8: Branches and OR Logic

## The Python instinct

```python
if auto_mode and ready:
    start_pump()
elif manual_mode and button_pressed:
    start_pump()
```

## The ladder logic way

Ladder logic has two ways to combine conditions. For OR-ing two Bool tags together, use `|`:

```python
from pyrung import Bool, Int, Program, Rung, branch, out, latch, any_of, all_of

Auto       = Bool("Auto")
Manual     = Bool("Manual")
Estop      = Bool("Estop")
Ready      = Bool("Ready")
PumpButton = Bool("PumpButton")
Pump       = Bool("Pump")
Power      = Bool("Power")
Light      = Bool("Light")
Mode       = Int("Mode")

with Program() as logic:
    # | for OR-ing two Bool conditions
    with Rung(Auto | Manual):
        out(Light)                        # Light when either mode is active

    # any_of for OR-ing comparisons or more than two conditions
    with Rung(any_of(Mode == 1, Mode == 3, Mode == 5)):
        latch(Pump)
```

Use `|` when you're OR-ing two Bool tags. Use `any_of` when you're OR-ing comparisons or have more than two conditions.

## Branches

A `branch` creates a parallel path within a rung. Think of it as a second wire that ANDs its condition with the parent's.

```python
with Program() as logic:
    with Rung(Auto):
        out(Light)                        # Light when Auto
        with branch(Ready):
            out(Pump)                     # Pump when Auto AND Ready
```

These combine naturally. Here's a safety rung where power stays on when the E-stop isn't pressed, and the pump runs in Auto mode or when Manual mode and the pump button are both active:

```python
with Program() as logic:
    with Rung(~Estop):
        out(Power)
        with branch(Auto | all_of(Manual, PumpButton)):
            out(Pump)
```

Important: **all conditions evaluate before any instructions execute.** The branch doesn't "see" results of instructions above it in the same rung because each rung starts from a clean snapshot.

## Exercise

Build a three-mode system: Auto, Manual, and Off. In Auto mode, a pump runs when a level sensor is high. In Manual mode, the pump runs when a manual button is pressed. In Off mode, nothing runs. Test all three modes, and test that switching from Auto to Manual mid-run changes the control source.
