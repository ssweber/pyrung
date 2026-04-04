# Lesson 2: Tags

## The Python instinct

```python
motor_running = False  # Create it, set it, done
```

## The ladder logic way

```python
from pyrung import Bool, Int, Real

MotorRunning = Bool("MotorRunning")   # 1 bit
Speed        = Int("Speed")           # 16-bit signed integer
Temperature  = Real("Temperature")    # 32-bit float
```

Tags are typed and sized. You can't put a float in a Bool or store a negative number in an unsigned Word. This reflects real PLC hardware where each tag maps to a specific region of memory with a fixed width.

The important distinction is **retentive** vs **non-retentive**. When a PLC goes through a STOP->RUN cycle (like a reboot), retentive tags keep their values and non-retentive tags reset to defaults. Bool tags are non-retentive by default: your outputs start in a known safe state. Int, Real, and others are retentive: your production counter doesn't reset to zero every time someone power-cycles the machine.

## Setting values from outside the program

The program (your rungs) reads and writes tags through instructions. But you also need to set values from *outside* the program, the way an operator would type a setpoint into an HMI or a dataview window. In pyrung, that's the `runner.active()` block:

```python
from pyrung import Bool, Real, Program, Rung, PLCRunner, out

Alarm    = Bool("Alarm")
Setpoint = Real("Setpoint")

with Program() as logic:
    with Rung(Setpoint > 100.0):
        out(Alarm)

runner = PLCRunner(logic)
with runner.active():
    Setpoint.value = 50.0          # Like typing into a dataview
    runner.step()
    assert Alarm.value is False

    Setpoint.value = 150.0         # Change the setpoint
    runner.step()
    assert Alarm.value is True     # Program reacts on the next scan
```

`Setpoint.value = 150.0` happens outside the program, before the scan. The program sees the new value when it runs and reacts accordingly. This is the same relationship an operator has with a real PLC: they set inputs and parameters, the logic does the rest.

## Exercise

Create an Int tag called `Count` and a Bool called `Alarm`. Write a rung that energizes the Alarm when Count is greater than 10. From outside the program, set Count to 5, step, and verify the Alarm is off. Then set it to 15, step, and verify the Alarm is on.
