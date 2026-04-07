# Lesson 1: The Scan Cycle

## The Python instinct

```python
# You'd write this
if run_button:
    conveyor_motor = True
else:
    conveyor_motor = False
```

This runs once. A PLC doesn't run once. It runs in a **scan cycle**, an infinite loop that evaluates every line of logic, top to bottom, hundreds of times per second. Always. Forever. Even when nothing is happening.

## Why?

Because a PLC controls physical things. A conveyor belt doesn't stop needing instructions and a valve doesn't pause while you wait for user input. The machine is always running, so the logic is always running.

## The ladder logic way

```python
from pyrung import Bool, Program, Rung, PLCRunner, out

RunButton     = Bool("RunButton")
ConveyorMotor = Bool("ConveyorMotor")

with Program() as logic:
    with Rung(RunButton):
        out(ConveyorMotor)
```

Read it aloud: "On this rung, if RunButton is true, energize ConveyorMotor." Every scan, this rung is evaluated, and `out` automatically makes the motor follow the rung's power state. No `if/else` needed.

If you've seen ladder logic in a textbook or an editor, it looks something like this:

```
    |  RunButton    ConveyorMotor  |
    |--[ ]---------( )-------------|
```

The left rail is power. `[ ]` is a contact (condition). `( )` is a coil (output). If the contact closes, power flows through and the coil energizes. pyrung's `with Rung(RunButton): out(ConveyorMotor)` is the same thing expressed in Python.

## Try it

```python
runner = PLCRunner(logic)
with runner.active():
    RunButton.value = True
    runner.step()               # One scan
    assert ConveyorMotor.value is True

    RunButton.value = False
    runner.step()               # Next scan
    assert ConveyorMotor.value is False  # Motor follows button, every scan
```

## Key concept: `out` is not assignment

`ConveyorMotor = True` in Python sets a value once. `out(ConveyorMotor)` means "the motor follows this rung's power state, every single scan." Take your finger off the button, the conveyor stops. That's why `out` works this way -- in a factory, releasing the button *should* stop the machine.

If two rungs both `out` the same tag, the last one wins. This is how real PLCs work.

## Exercise

Add an `EntrySensor` (Bool) and a `SensorLight` (Bool). Write a second rung where the sensor light comes on when the entry sensor detects a box. Test both rungs independently: the motor should follow the button, and the light should follow the sensor.

---

The motor and sensor work, but they're just on or off. What if we need to track a speed setpoint or trigger an alarm at a threshold? That requires typed tags.
