# Lesson 3: Latch and Reset

## The Python instinct

```python
if start_pressed:
    motor_running = True
# But what turns it off?
# And what if start_pressed goes False?
```

## The problem

In the real world, you press a momentary "Start" button. Your finger comes off. The motor should keep running. `out` won't work here because it de-energizes the moment the rung goes false.

## The ladder logic way

```python
from pyrung import Bool, Program, Rung, latch, reset

Start   = Bool("Start")
Stop    = Bool("Stop")
Running = Bool("Running")

with Program() as logic:
    with Rung(Start):
        latch(Running)       # SET: Running = True, stays True
    with Rung(Stop):
        reset(Running)       # RESET: Running = False
```

`latch` is sticky. Once set, it stays set until explicitly `reset`. This is the bread and butter of motor control, alarm acknowledgment, and mode selection in every factory on earth.

## Try it

```python
runner = PLCRunner(logic)
with runner.active():
    Start.value = True
    runner.step()
    assert Running.value is True

    Start.value = False        # Finger off the button
    runner.step()
    assert Running.value is True   # Still running!

    Stop.value = True
    runner.step()
    assert Running.value is False  # Now it stops
```

## A subtlety: rung order matters

What if Start and Stop are both pressed at the same time? The answer: **the last rung to write wins.** Since `reset(Running)` is below `latch(Running)`, Stop wins. This is intentional. In industrial safety, stop always wins. Rung ordering is a design decision.

## Exercise

Build a "toggle" pattern: one button press turns a light on, the next press turns it off. (Hint: you'll need `rise()` for edge detection, see the [Conditions reference](../instructions/conditions.md). Think about why you can't just use `Button` as the condition.)
