# Lesson 1: The Scan Cycle

## The Python instinct

```python
# You'd write this
if button_pressed:
    light = True
else:
    light = False
```

This runs once. A PLC doesn't run once. It runs in a **scan cycle**, an infinite loop that evaluates every line of logic, top to bottom, hundreds of times per second. Always. Forever. Even when nothing is happening.

## Why?

Because a PLC controls physical things. A conveyor belt doesn't stop needing instructions and a valve doesn't pause while you wait for user input. The machine is always running, so the logic is always running.

## The ladder logic way

```python
from pyrung import Bool, Program, Rung, PLCRunner, out

Button = Bool("Button")
Light = Bool("Light")

with Program() as logic:
    with Rung(Button):
        out(Light)
```

Read it aloud: "On this rung, if Button is true, energize Light." Every scan, this rung is evaluated, and `out` automatically makes Light follow the rung's power state. No `if/else` needed.

If you've seen ladder logic in a textbook or an editor, it looks something like this:

```
    |  Button       Light  |
    |--[ ]----------( )----|
```

The left rail is power. `[ ]` is a contact (condition). `( )` is a coil (output). If the contact closes, power flows through and the coil energizes. pyrung's `with Rung(Button): out(Light)` is the same thing expressed in Python.

## Try it

```python
runner = PLCRunner(logic)
with runner.active():
    Button.value = True
    runner.step()               # One scan
    assert Light.value is True

    Button.value = False
    runner.step()               # Next scan
    assert Light.value is False # Light follows Button, every scan
```

## Key concept: `out` is not assignment

`Light = True` in Python sets a value once. `out(Light)` means "Light follows this rung's power state, every single scan." If two rungs both `out` the same tag, the last one wins. This is how real PLCs work.

## Exercise

Create a program with two buttons and a light. The light should be on when *either* button is pressed. (Hint: check the [Conditions reference](../instructions/conditions.md) for ways to combine conditions.)
