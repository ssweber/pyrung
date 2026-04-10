# Testing

The whole point of pyrung is to test logic before it touches hardware. Every scan is deterministic, every state is a snapshot, and pytest works out of the box.

## Your first test

```python
from pyrung import Bool, PLC, Program, Rung, latch, reset

Start = Bool("Start")
Stop  = Bool("Stop")
Motor = Bool("Motor")

with Program() as logic:
    with Rung(Start):
        latch(Motor)
    with Rung(Stop):
        reset(Motor)

def test_start_latches_motor():
    with PLC(logic, dt=0.1) as plc:
        Start.value = True
        plc.step()
        assert Motor.value is True

        # Release start — motor stays latched
        Start.value = False
        plc.step()
        assert Motor.value is True

def test_stop_resets_motor():
    with PLC(logic, dt=0.1) as plc:
        Start.value = True
        plc.step()

        Start.value = False
        Stop.value = True
        plc.step()
        assert Motor.value is False
```

Tags are defined at module level (just like PLC addresses), and each test gets a fresh PLC. Inside the `with PLC(...) as plc:` block, `.value` reads and writes go through the runner's current state. Set a value, step, assert — that's the pattern.

## Testing timers

Timers accumulate time across scans. With a fixed `dt`, the math is exact:

```python
from pyrung import Bool, Timer, PLC, Program, Rung, on_delay

Enable   = Bool("Enable")
MyTimer  = Timer.clone("MyTimer")

with Program() as logic:
    with Rung(Enable):
        on_delay(MyTimer, preset=100, unit="Tms")

def test_timer_fires_at_preset():
    with PLC(logic, dt=0.001) as plc:
        Enable.value = True

        # 99 scans = 99 ms — not yet
        plc.run(cycles=99)
        assert MyTimer.Done.value is False
        assert MyTimer.Acc.value == 99

        # One more scan — 100 ms, timer fires
        plc.step()
        assert MyTimer.Done.value is True
```

A 100 ms preset with 1 ms scans takes exactly 100 steps. No timing jitter, no flaky tests.

## Testing time-of-day logic

Logic that depends on the real-time clock (shift changes, scheduled events, lighting) can be tested with `set_rtc`. With a fixed `dt`, the RTC advances with simulation time — no wall-clock dependency:

```python
from datetime import datetime

def test_shift_changeover():
    with PLC(logic, dt=0.1) as plc:
        plc.set_rtc(datetime(2026, 3, 5, 6, 59, 50))  # 10 seconds before 7 AM

        plc.run(cycles=100)  # 10 seconds at 0.1s/scan

        assert ShiftActive.value is True  # Logic triggered at 7:00:00
```

## Testing edge detection

`rise()` fires for exactly one scan on a false → true transition:

```python
from pyrung import Bool, PLC, Program, Rung, out, rise

Sensor = Bool("Sensor")
Pulse  = Bool("Pulse")

with Program() as logic:
    with Rung(rise(Sensor)):
        out(Pulse)

def test_rise_fires_once():
    with PLC(logic, dt=0.1) as plc:
        Sensor.value = True
        plc.step()
        assert Pulse.value is True   # Rising edge — fires

        plc.step()
        assert Pulse.value is False  # Still true, but no edge — doesn't fire
```

## Using forces as test fixtures

When you need an input held across many scans, forces are cleaner than setting `.value` before every step:

```python
def test_motor_runs_for_duration():
    with PLC(logic, dt=0.1) as plc:
        plc.force(Enable, True)
        plc.run(cycles=50)
        assert Motor.value is True
        plc.unforce(Enable)
```

The `forced()` context manager scopes forces to a block and cleans up automatically:

```python
def test_fault_during_operation():
    with PLC(logic, dt=0.1) as plc:
        with plc.forced({Enable: True}):
            plc.run(cycles=10)

            with plc.forced({Fault: True}):
                plc.step()
                assert Motor.value is False   # Fault killed the motor
            # Fault released, Enable still forced

        # All forces released
```

> Forces and patches also accept string keys (`plc.force("Enable", True)`) for cases where you're working with tag names directly.

## Running until a condition

For tests where you care about *what* happens, not *when*, `run_until` accepts the same condition expressions you use inside `Rung()`:

```python
def test_motor_eventually_stops():
    with PLC(logic, dt=0.1) as plc:
        Start.value = True
        plc.step()
        Stop.value = True

        plc.run_until(~Motor, max_cycles=100)
        assert Motor.value is False
```

Conditions compose the same way they do in rungs:

```python
runner.run_until(Motor & ~Fault)                  # Motor on, no fault
runner.run_until(Temp > 150.0)                    # Temperature exceeded
runner.run_until(Or(AlarmA, AlarmB, AlarmC))  # Any alarm triggered
```

`run_until` stops as soon as the condition is met, or after `max_cycles` — whichever comes first.

## Forking: test alternate outcomes

Get your process to a decision point once, then fork and test both paths independently:

```python
def test_fault_vs_normal():
    with PLC(logic, dt=0.01) as plc:
        Start.value = True
        plc.run(cycles=200)

        # What happens if a fault occurs?
        fault_path = plc.fork()
        with fault_path:
            Fault.value = True
            fault_path.run(cycles=50)
            assert Motor.value is False

        # What happens under normal operation?
        normal_path = plc.fork()
        with normal_path:
            normal_path.run(cycles=50)
            assert Motor.value is True
```

Each fork is an independent runner starting from the same snapshot. No need to duplicate a long warmup sequence in every test.

## Monitoring changes

`monitor` watches a tag and fires a callback whenever its value changes:

```python
def test_motor_transitions():
    transitions = []

    with PLC(logic, dt=0.1) as plc:
        plc.monitor(Motor, lambda curr, prev: transitions.append((prev, curr)))

        Start.value = True
        plc.step()
        Stop.value = True
        plc.step()

    assert transitions == [(False, True), (True, False)]
```

## Predicate breakpoints and snapshots

`when` uses the same condition expressions to pause execution or label a scan in history:

```python
def test_capture_fault_state():
    with PLC(logic, history_limit=1000, dt=0.1) as plc:
        plc.when(Fault).snapshot("fault_triggered")

        Start.value = True
        plc.run(cycles=500)

        snap = plc.history.find_labeled("fault_triggered")
        if snap is not None:
            assert snap.scan_id > 0
```

`when(condition).pause()` halts `run()` / `run_for()` / `run_until()` after committing the triggering scan — useful for debugging a long simulation without stepping through every scan.

## Comparing states

For debugging tests, `diff` shows exactly what changed between two scans:

```python
def test_inspect_changes():
    with PLC(logic, history_limit=100, dt=0.1) as plc:
        Start.value = True
        plc.step()   # scan 1

        Stop.value = True
        plc.step()   # scan 2

        changes = plc.diff(scan_a=1, scan_b=2)
        # {"Motor": (True, False), "Stop": (False, True), ...}
```

## Pytest fixtures

For a shared program across multiple tests:

```python
import pytest
from pyrung import Bool, PLC, Program, Rung, latch, reset

Start = Bool("Start")
Stop  = Bool("Stop")
Motor = Bool("Motor")

with Program() as logic:
    with Rung(Start):
        latch(Motor)
    with Rung(Stop):
        reset(Motor)

@pytest.fixture
def plc():
    return PLC(logic, dt=0.1)

def test_latch(plc):
    with plc:
        Start.value = True
        plc.step()
        assert Motor.value is True

def test_stop_after_start(plc):
    with plc:
        Start.value = True
        plc.step()
        Stop.value = True
        plc.step()
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
