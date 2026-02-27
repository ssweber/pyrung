# Testing

The whole point of pyrung is to test logic before it touches hardware. Every scan is deterministic, every state is a snapshot, and pytest works out of the box.

## Your first test

```python
from pyrung import Bool, PLCRunner, Program, Rung, TimeMode, latch, reset

Start = Bool("Start")
Stop  = Bool("Stop")
Motor = Bool("Motor")

with Program() as logic:
    with Rung(Start):
        latch(Motor)
    with Rung(Stop):
        reset(Motor)

def test_start_latches_motor():
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    with runner.active():
        Start.value = True
        runner.step()
        assert Motor.value is True

        # Release start — motor stays latched
        Start.value = False
        runner.step()
        assert Motor.value is True

def test_stop_resets_motor():
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    with runner.active():
        Start.value = True
        runner.step()

        Start.value = False
        Stop.value = True
        runner.step()
        assert Motor.value is False
```

Tags are defined at module level (just like PLC addresses), and each test gets a fresh runner. Inside `runner.active()`, `.value` reads and writes go through the runner's current state. Set a value, step, assert — that's the pattern.

## Testing timers

Timers accumulate time across scans. With `FIXED_STEP`, the math is exact:

```python
from pyrung import Bool, Int, PLCRunner, Program, Rung, TimeMode, Tms, on_delay

Enable    = Bool("Enable")
TimerDone = Bool("TimerDone")
TimerAcc  = Int("TimerAcc")

with Program() as logic:
    with Rung(Enable):
        on_delay(TimerDone, accumulator=TimerAcc, preset=100, unit=Tms)

def test_timer_fires_at_preset():
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.001)  # 1 ms per scan

    with runner.active():
        Enable.value = True

        # 99 scans = 99 ms — not yet
        runner.run(cycles=99)
        assert TimerDone.value is False
        assert TimerAcc.value == 99

        # One more scan — 100 ms, timer fires
        runner.step()
        assert TimerDone.value is True
```

A 100 ms preset with 1 ms scans takes exactly 100 steps. No timing jitter, no flaky tests.

## Testing time-of-day logic

Logic that depends on the real-time clock (shift changes, scheduled events, lighting) can be tested with `set_rtc`. In `FIXED_STEP` mode, the RTC advances with simulation time — no wall-clock dependency:

```python
from datetime import datetime

def test_shift_changeover():
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
    runner.set_rtc(datetime(2026, 3, 5, 6, 59, 50))  # 10 seconds before 7 AM

    runner.run(cycles=100)  # 10 seconds at 0.1s/scan

    with runner.active():
        assert ShiftActive.value is True  # Logic triggered at 7:00:00
```

## Testing edge detection

`rise()` fires for exactly one scan on a false → true transition:

```python
from pyrung import Bool, PLCRunner, Program, Rung, TimeMode, out, rise

Sensor = Bool("Sensor")
Pulse  = Bool("Pulse")

with Program() as logic:
    with Rung(rise(Sensor)):
        out(Pulse)

def test_rise_fires_once():
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    with runner.active():
        Sensor.value = True
        runner.step()
        assert Pulse.value is True   # Rising edge — fires

        runner.step()
        assert Pulse.value is False  # Still true, but no edge — doesn't fire
```

## Using forces as test fixtures

When you need an input held across many scans, forces are cleaner than setting `.value` before every step:

```python
def test_motor_runs_for_duration():
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    runner.add_force(Enable, True)
    runner.run(cycles=50)

    with runner.active():
        assert Motor.value is True

    runner.remove_force(Enable)
```

The `force()` context manager scopes forces to a block and cleans up automatically:

```python
def test_fault_during_operation():
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    with runner.force({Enable: True}):
        runner.run(cycles=10)

        with runner.force({Fault: True}):
            runner.step()
            with runner.active():
                assert Motor.value is False   # Fault killed the motor
        # Fault released, Enable still forced

    # All forces released
```

> Forces and patches also accept string keys (`runner.add_force("Enable", True)`) for cases where you're working with tag names directly.

## Running until a condition

For tests where you care about *what* happens, not *when*, `run_until` accepts the same condition expressions you use inside `Rung()`:

```python
def test_motor_eventually_stops():
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    with runner.active():
        Start.value = True
        runner.step()
        Stop.value = True

    runner.run_until(~Motor, max_cycles=100)

    with runner.active():
        assert Motor.value is False
```

Conditions compose the same way they do in rungs:

```python
runner.run_until(Motor & ~Fault)                  # Motor on, no fault
runner.run_until(Temp > 150.0)                    # Temperature exceeded
runner.run_until(any_of(AlarmA, AlarmB, AlarmC))  # Any alarm triggered
```

`run_until` stops as soon as the condition is met, or after `max_cycles` — whichever comes first.

## Forking: test alternate outcomes

Get your process to a decision point once, then fork and test both paths independently:

```python
def test_fault_vs_normal():
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.01)

    # Run shared setup
    with runner.active():
        Start.value = True
    runner.run(cycles=200)

    # What happens if a fault occurs?
    fault_path = runner.fork()
    with fault_path.active():
        Fault.value = True
        fault_path.run(cycles=50)
        assert Motor.value is False

    # What happens under normal operation?
    normal_path = runner.fork()
    normal_path.run(cycles=50)
    with normal_path.active():
        assert Motor.value is True
```

Each fork is an independent runner starting from the same snapshot. No need to duplicate a long warmup sequence in every test.

## Monitoring changes

`monitor` watches a tag and fires a callback whenever its value changes:

```python
def test_motor_transitions():
    transitions = []
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    runner.monitor(Motor, lambda curr, prev: transitions.append((prev, curr)))

    with runner.active():
        Start.value = True
        runner.step()
        Stop.value = True
        runner.step()

    assert transitions == [(False, True), (True, False)]
```

## Predicate breakpoints and snapshots

`when` uses the same condition expressions to pause execution or label a scan in history:

```python
def test_capture_fault_state():
    runner = PLCRunner(logic, history_limit=1000)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    runner.when(Fault).snapshot("fault_triggered")

    with runner.active():
        Start.value = True
    runner.run(cycles=500)

    snap = runner.history.find_labeled("fault_triggered")
    if snap is not None:
        assert snap.scan_id > 0
```

`when(condition).pause()` halts `run()` / `run_for()` / `run_until()` after committing the triggering scan — useful for debugging a long simulation without stepping through every scan.

## Comparing states

For debugging tests, `diff` shows exactly what changed between two scans:

```python
def test_inspect_changes():
    runner = PLCRunner(logic, history_limit=100)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    with runner.active():
        Start.value = True
        runner.step()   # scan 1

        Stop.value = True
        runner.step()   # scan 2

    changes = runner.diff(scan_a=1, scan_b=2)
    # {"Motor": (True, False), "Stop": (False, True), ...}
```

## Pytest fixtures

For a shared program across multiple tests:

```python
import pytest
from pyrung import Bool, PLCRunner, Program, Rung, TimeMode, latch, reset

Start = Bool("Start")
Stop  = Bool("Stop")
Motor = Bool("Motor")

with Program() as logic:
    with Rung(Start):
        latch(Motor)
    with Rung(Stop):
        reset(Motor)

@pytest.fixture
def runner():
    r = PLCRunner(logic)
    r.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
    return r

def test_latch(runner):
    with runner.active():
        Start.value = True
        runner.step()
        assert Motor.value is True

def test_stop_after_start(runner):
    with runner.active():
        Start.value = True
        runner.step()
        Stop.value = True
        runner.step()
        assert Motor.value is False
```

## Running tests

```bash
make test       # recommended
pytest tests/   # or directly
```

## Next steps

- [Forces & Debug](forces-debug.md) — force semantics, history, time travel
- [Runner Guide](runner.md) — time modes, execution methods, numeric behavior
- [Quickstart](../getting-started/quickstart.md) — the traffic light example
