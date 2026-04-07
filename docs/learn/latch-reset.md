# Lesson 3: Latch and Reset

## The Python instinct

```python
if start_pressed:
    conveyor_running = True
# But what turns it off?
# And what if start_pressed goes False?
```

## The problem

In the real world, you press a momentary "Start" button. Your finger comes off. The conveyor should keep running. `out` won't work here because it de-energizes the moment the rung goes false.

## The ladder logic way

```python
from pyrung import Bool, Program, Rung, PLCRunner, latch, reset

Start   = Bool("Start")
Stop    = Bool("Stop")
Estop   = Bool("Estop")
Running = Bool("Running")

with Program() as logic:
    with Rung(Start):
        latch(Running)       # SET: Running = True, stays True
    with Rung(Stop):
        reset(Running)       # RESET: Running = False
    with Rung(Estop):
        reset(Running)       # E-stop also resets
```

`latch` is sticky. Once set, it stays set until explicitly `reset`. This is the bread and butter of motor control, alarm acknowledgment, and mode selection in every factory on earth.

## Try it

```python
runner = PLCRunner(logic)
with runner.active():
    Start.value = True
    runner.step()
    assert Running.value is True

    Start.value = False          # Finger off the button
    runner.step()
    assert Running.value is True  # Still running!

    Stop.value = True
    runner.step()
    assert Running.value is False
```

## A subtlety: rung order matters

What if Start and Stop are both pressed at the same time? The answer: **the last rung to write wins.** Since `reset(Running)` is below `latch(Running)`, Stop wins. This is intentional. In industrial safety, stop always wins. The E-stop rung is last for the same reason.

## Labeling your rungs

As programs grow, each rung benefits from a label. `comment()` attaches one to the next rung:

```python
from pyrung import comment

with Program() as logic:
    comment("Start the conveyor")
    with Rung(Start):
        latch(Running)
    comment("Normal stop")
    with Rung(Stop):
        reset(Running)
    comment("Emergency stop")
    with Rung(Estop):
        reset(Running)
```

This isn't a Python `#` comment — it's rung metadata that travels with the program. When you export to a Click PLC, these appear above each rung in the ladder editor. From here on, we'll use `comment()` to label rungs as the logic gets more complex.

## Exercise

Build an E-stop test: start the conveyor, then press E-stop. Verify it stops. Then verify that pressing Start while E-stop is still active does NOT restart the conveyor. (Hint: you need E-stop to block the start, not just reset after it. Think about adding `~Estop` as a condition on the latch rung.)

---

The conveyor runs and stops, but there's no tracking. When a box arrives, the system needs to record its size and keep a tally. That needs data movement -- `copy` and `calc`.
