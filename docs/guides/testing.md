# Testing with FIXED_STEP

pyrung's immutable state and consumer-driven execution make it ideal for deterministic unit testing with standard pytest. No mocks, no timing hacks, no external dependencies.

## Basic pattern

```python
# tests/test_motor.py
from pyrung import Bool, PLCRunner, Program, Rung, TimeMode, latch, reset, rise

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

`FIXED_STEP` controls simulation-time behavior (`state.timestamp`, timers/counters, and scan clocks).
RTC system points (`rtc.year4`, `rtc.hour`, etc.) are derived from wall clock (`datetime.now()`) plus internal RTC offset, so they are not deterministic unless you freeze or monkeypatch time.

## Testing RTC deterministically

Use a frozen clock for tests that read or write `rtc.*` values:

```python
from freezegun import freeze_time
from pyrung import PLCRunner, system

def test_rtc_fields_with_frozen_wall_clock():
    with freeze_time("2026-03-05 06:07:08"):
        runner = PLCRunner(logic=[])
        runner.step()

        found, year = runner.system_runtime.resolve(system.rtc.year4.name, runner.current_state)
        found2, second = runner.system_runtime.resolve(system.rtc.second.name, runner.current_state)

        assert found is True and year == 2026
        assert found2 is True and second == 8
```

If you need "what RTC was at each scan" in retained history, copy the `rtc.*` value into a normal tag during the scan. That tag is then stored in `state.tags` and can be inspected later from history snapshots.

## Testing timers

```python
def test_on_delay_fires_after_preset():
    TimerDone = Bool("TimerDone")
    TimerAcc  = Int("TimerAcc")
    Enable    = Bool("Enable")

    with Program() as logic:
        with Rung(Enable):
            on_delay(TimerDone, accumulator=TimerAcc, preset=5, unit=Tms)

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

## Testing with count-one udt

For larger programs, count-one `@udt()` keeps class-qualified names without string duplication:

```python
from pyrung import Bool, Int, PLCRunner, Program, Rung, TimeMode, latch, udt

@udt()
class Tags:
    Start: Bool
    Stop: Bool
    MotorRunning: Bool
    Step: Int

with Program() as logic:
    with Rung(Tags.Start):
        latch(Tags.MotorRunning)

runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

with runner.active():
    Tags.Start.value = True
    runner.step()
assert runner.current_state.tags["Tags_MotorRunning"] is True
```

## run_until() in tests

```python
def test_counter_reaches_target():
    CountDone = Bool("CountDone")
    CountAcc  = Dint("CountAcc")
    Pulse     = Bool("Pulse")

    with Program() as logic:
        with Rung(rise(Pulse)):
            count_up(CountDone, accumulator=CountAcc, preset=10).reset(Bool("_never"))

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
from pyrung import Bool, PLCRunner, Program, Rung, TimeMode, latch, reset

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
