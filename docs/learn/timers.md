# Lesson 5: Timers

## The Python instinct

```python
import time
time.sleep(3)  # Block for 3 seconds
motor_running = True
```

## Why that's wrong here

A PLC can't sleep. It has to keep scanning because sensors are still reading, safety interlocks are still being checked, and other rungs still need to execute. Blocking is not an option when you're controlling physical equipment.

## The ladder logic way

Timers **accumulate** across scans: every scan where the rung is true, the timer adds a little more time, and when the accumulator reaches the preset, it fires.

```python
from pyrung import Bool, Int, Program, Rung, Tms, on_delay, latch

Start     = Bool("Start")
Running   = Bool("Running")
DelayDone = Bool("DelayDone")
DelayAcc  = Int("DelayAcc")

with Program() as logic:
    with Rung(Start):
        on_delay(DelayDone, DelayAcc, preset=3000, unit=Tms)  # 3 seconds
    with Rung(DelayDone):
        latch(Running)
```

This reads: "While Start is pressed, accumulate time. After 3000 ms, set DelayDone. When DelayDone is true, latch Running." If you release Start before 3 seconds, the accumulator resets (that's `on_delay` / TON behavior).

## Test it deterministically

```python
from pyrung import PLCRunner, TimeMode

runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10 ms per scan

with runner.active():
    Start.value = True

runner.run(cycles=299)                        # 2.99 seconds
with runner.active():
    assert Running.value is False             # Not yet

runner.step()                                 # 3.00 seconds
with runner.active():
    assert Running.value is True              # Now!
```

`FIXED_STEP` mode advances the clock by exactly 10 ms each scan. No wall clock. Perfectly deterministic. This is why pyrung exists. Try writing this test against real hardware.

## Exercise

Build a "press and hold" button: the motor only starts if you hold the Start button for 2 full seconds. If you release early, nothing happens. Test both paths: the successful hold and the early release.
