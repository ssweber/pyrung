# Testing with FIXED_STEP

pyrung's immutable state and consumer-driven execution make it ideal for deterministic unit testing with standard pytest. No mocks, no timing hacks, no external dependencies.

## Basic pattern

```python
# tests/test_motor.py
from pyrung.core import *

def make_runner():
    StartButton  = Bool("StartButton")
    StopButton   = Bool("StopButton")
    MotorRunning = Bool("MotorRunning")

    with Program() as logic:
        with Rung(rise(StartButton)):
            latch(MotorRunning)
        with Rung(rise(StopButton)):
            reset(MotorRunning)

    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
    return runner, StartButton, StopButton, MotorRunning


def test_start_latches_motor():
    runner, StartButton, _, MotorRunning = make_runner()

    runner.patch({"StartButton": True})
    runner.step()

    assert runner.current_state.tags["MotorRunning"] is True


def test_stop_resets_motor():
    runner, StartButton, StopButton, MotorRunning = make_runner()

    # Start first
    runner.patch({"StartButton": True})
    runner.step()

    # Then stop
    runner.patch({"StopButton": True})
    runner.step()

    assert runner.current_state.tags["MotorRunning"] is False
```

## Why FIXED_STEP?

`TimeMode.FIXED_STEP` advances the simulation clock by exactly `dt` per scan. This means:

- Timer and counter behavior is **reproducible** regardless of machine speed.
- Tests don't depend on wall-clock timing.
- You can choose any `dt` that makes sense for your test (e.g. `dt=0.001` for 1ms steps).

## Testing timers

```python
def test_on_delay_fires_after_setpoint():
    TimerDone = Bool("TimerDone")
    TimerAcc  = Int("TimerAcc")
    Enable    = Bool("Enable")

    with Program() as logic:
        with Rung(Enable):
            on_delay(TimerDone, accumulator=TimerAcc, setpoint=5, time_unit=Tms)

    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.001)  # 1ms per scan

    runner.add_force("Enable", True)

    # Run 4 scans (4ms) — not yet done
    runner.run(4)
    assert runner.current_state.tags["TimerDone"] is False
    assert runner.current_state.tags["TimerAcc"] == 4

    # Run 1 more scan (5ms total) — should fire
    runner.step()
    assert runner.current_state.tags["TimerDone"] is True
```

## Testing edge detection

`rise()` and `fall()` fire for exactly **one scan** on a transition:

```python
def test_rise_fires_for_one_scan():
    Signal = Bool("Signal")
    Pulse  = Bool("Pulse")

    with Program() as logic:
        with Rung(rise(Signal)):
            out(Pulse)

    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    # Scan 1: Signal goes True — rise fires
    runner.patch({"Signal": True})
    runner.step()
    assert runner.current_state.tags["Pulse"] is True

    # Scan 2: Signal stays True — rise does NOT fire again
    runner.step()
    assert runner.current_state.tags["Pulse"] is False
```

## Using forces for persistent test conditions

`add_force()` persists across scans. Use it to hold inputs True/False without re-patching every scan:

```python
runner.add_force("AutoMode", True)
runner.run(10)                # AutoMode stays True for all 10 scans
runner.remove_force("AutoMode")
```

The `force()` context manager is useful for scoped test fixtures:

```python
with runner.force({"AutoMode": True, "Fault": False}):
    runner.run(5)
# Forces released here; AutoMode and Fault return to logic-computed values
```

## Checking multiple tags

```python
state = runner.current_state
assert state.tags["MotorRunning"] is True
assert state.tags["Step"] == 3
assert state.tags.get("Fault", False) is False  # tag absent → use default
```

## Testing with AutoTag

For larger programs, `AutoTag` reduces boilerplate:

```python
from pyrung.core import *
from pyrung.core import AutoTag

class Tags(AutoTag):
    Start        = Bool()
    Stop         = Bool()
    MotorRunning = Bool()
    Step         = Int()

Start, Stop, MotorRunning, Step = Tags.Start, Tags.Stop, Tags.MotorRunning, Tags.Step

with Program() as logic:
    with Rung(Start):
        latch(MotorRunning)

runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

runner.patch({"Start": True})
runner.step()
assert runner.current_state.tags["MotorRunning"] is True
```

## run_until() in tests

```python
def test_counter_reaches_target():
    CountDone = Bool("CountDone")
    CountAcc  = Dint("CountAcc")
    Pulse     = Bool("Pulse")

    with Program() as logic:
        with Rung(rise(Pulse)):
            count_up(CountDone, accumulator=CountAcc, setpoint=10).reset(Bool("_never"))

    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    # Toggle Pulse 10 times
    for _ in range(10):
        runner.patch({"Pulse": True})
        runner.step()
        runner.patch({"Pulse": False})
        runner.step()

    assert runner.current_state.tags["CountDone"] is True
    assert runner.current_state.tags["CountAcc"] >= 10
```

## Pytest fixtures

For a shared runner across multiple tests in a module:

```python
import pytest
from pyrung.core import *

@pytest.fixture
def motor_runner():
    Start  = Bool("Start")
    Stop   = Bool("Stop")
    Motor  = Bool("Motor")

    with Program() as logic:
        with Rung(Start):
            latch(Motor)
        with Rung(Stop):
            reset(Motor)

    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
    return runner


def test_latch(motor_runner):
    motor_runner.patch({"Start": True})
    motor_runner.step()
    assert motor_runner.current_state.tags["Motor"] is True
```

## Running the test suite

```bash
make test       # recommended: uses pytest with correct configuration
```

Or directly:

```bash
pytest tests/
```
