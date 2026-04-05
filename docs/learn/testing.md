# Lesson 10: Testing Like You Mean It

This is where pyrung pays for itself. Everything you've built -- the motor control, the sorting sequence, the bin counters, the mode switching -- is testable with pytest. No hardware, no manual verification, no "download and hope."

```python
import pytest
from pyrung import PLCRunner, TimeMode

@pytest.fixture
def runner():
    r = PLCRunner(logic)
    r.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
    with r.active():
        State.value = 0           # Start idle
        Auto.value = True         # Default to auto mode
    return r

def test_start_stop(runner):
    with runner.active():
        Start.value = True
    runner.step()
    with runner.active():
        assert Running.value is True
        assert ConveyorMotor.value is True

def test_estop_overrides_start(runner):
    """Safety: E-stop kills everything, even if Start is held."""
    with runner.active():
        Start.value = True
        Estop.value = True
    runner.step()
    with runner.active():
        assert Running.value is False
        assert ConveyorMotor.value is False
```

## Forces for persistent overrides

In the tests above, `.value` writes are one-shot -- consumed after one scan. **Forces** persist across multiple scans, which is what you need to simulate a sensor that stays on:

```python
def test_sorting_sequence(runner):
    """Full auto sort: box arrives, gets classified, exits to correct bin."""
    with runner.active():
        Start.value = True
    runner.step()

    runner.add_force(EntrySensor, True)
    runner.add_force(SizeReading, 150)       # Large box
    runner.add_force(SizeThreshold, 100)

    # Run until sorting state
    runner.run(cycles=55)                    # Past detection period
    with runner.active():
        assert DiverterCmd.value is True     # Extended for large box

    runner.remove_force(EntrySensor)
    runner.run(cycles=250)                   # Past hold period
    with runner.active():
        assert DiverterCmd.value is False    # Retracted after sort
        assert State.value == 0              # Back to idle
```

## History for inspection

```python
runner.step()
runner.step()
runner.step()
# Every scan is an immutable snapshot you can inspect, diff, or rewind
previous = runner.history[-2]    # Two scans ago
```

## Fork for parallel scenarios

Test two outcomes from the same starting point without resetting:

```python
def test_small_vs_large_box(runner):
    """Same setup, two outcomes."""
    with runner.active():
        Start.value = True
    runner.step()
    with runner.active():
        EntrySensor.value = True
        SizeThreshold.value = 100
    runner.step()

    # Fork: large box
    large = runner.fork()
    large.add_force(SizeReading, 150)
    large.run(cycles=300)
    with large.active():
        assert DiverterCmd.value is True

    # Fork: small box
    small = runner.fork()
    small.add_force(SizeReading, 50)
    small.run(cycles=300)
    with small.active():
        assert DiverterCmd.value is False
```

## When tests aren't enough

Sometimes you need to watch logic execute step by step. pyrung includes a VS Code debugger that lets you set breakpoints on individual rungs, step through scans one at a time, watch tag values update live, and force overrides from the debug console. If you've ever debugged Python in VS Code, it works the same way, just with scans instead of lines. See the [DAP Debugger guide](../guides/dap-vscode.md) for setup.

## Exercise

Write a test that covers the full conveyor lifecycle: start in auto mode, sort a large box (verify diverter extends and Bin B counter increments), sort a small box (verify diverter stays retracted and Bin A counter increments), then E-stop mid-sort and verify everything shuts down cleanly. Use `fork()` to test the large and small paths from a shared starting point.

---

The logic is tested, the tests pass. Now deploy it.
