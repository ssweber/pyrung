# Lesson 2: Tags

## The Python instinct

```python
conveyor_speed: int = 0
```

Python's type hint tells you it's an integer. It doesn't tell you it's 16-bit signed, non-retentive, or mapped to a specific region of physical memory.

## The ladder logic way

```python
from pyrung import Bool, Int, Real

ConveyorSpeed = Int("ConveyorSpeed")     # 16-bit signed integer, in mm/s
SpeedLimit    = Int("SpeedLimit")        # Alarm threshold
Temperature   = Real("Temperature")     # 32-bit float
```

Tags are typed and sized. You can't put a float in a Bool or store a negative number in an unsigned Word. This reflects real PLC hardware where each tag maps to a specific region of memory with a fixed width.

The important distinction is **retentive** vs **non-retentive**. When a PLC goes through a STOP->RUN cycle (like a reboot), retentive tags keep their values and non-retentive tags reset to defaults. Bool tags are non-retentive by default: your outputs start in a known safe state. Int, Real, and others are retentive: your production counter doesn't reset to zero every time someone power-cycles the machine.

## Setting values from outside the program

The program (your rungs) reads and writes tags through instructions. But you also need to set values from *outside* the program, the way an operator would type a setpoint into an HMI or a dataview window. In pyrung, that's the `runner.active()` block:

```python
from pyrung import Bool, Int, Program, Rung, PLCRunner, out

ConveyorSpeed = Int("ConveyorSpeed")
SpeedLimit    = Int("SpeedLimit")
OverSpeed     = Bool("OverSpeed")

with Program() as logic:
    with Rung(ConveyorSpeed > SpeedLimit):
        out(OverSpeed)

runner = PLCRunner(logic)
with runner.active():
    SpeedLimit.value = 500             # Like typing into a dataview
    ConveyorSpeed.value = 300
    runner.step()
    assert OverSpeed.value is False

    ConveyorSpeed.value = 600          # Speed exceeds limit
    runner.step()
    assert OverSpeed.value is True     # Program reacts on the next scan
```

`ConveyorSpeed.value = 600` happens outside the program, before the scan. The program sees the new value when it runs and reacts accordingly. This is the same relationship an operator has with a real PLC: they set inputs and parameters, the logic does the rest.

## Exercise

Add a `BoxWeight` (Real) tag and a `WeightLimit` (Real). Write a rung that energizes a `HeavyBox` alarm when weight exceeds the limit. Test with values below and above the threshold.

---

The motor turns on and off with the button, but in a real factory you press Start and walk away. The motor needs to stay running after you release the button. That's latch and reset.
