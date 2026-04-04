# Lesson 10: Testing Like You Mean It

This is where pyrung pays for itself. Everything you've built so far is testable with pytest.

```python
import pytest
from pyrung import PLCRunner, TimeMode

@pytest.fixture
def runner():
    r = PLCRunner(logic)
    r.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
    return r

def test_start_stop(runner):
    with runner.active():
        Start.value = True
    runner.step()
    with runner.active():
        assert Running.value is True

def test_stop_overrides_start(runner):
    """Safety: if both pressed, stop wins (last rung wins)."""
    with runner.active():
        Start.value = True
        Stop.value = True
    runner.step()
    with runner.active():
        assert Running.value is False
```

You can also use **forces** for persistent overrides across multiple scans:

```python
def test_sensor_stuck_high(runner):
    """Simulate a sensor failure, stuck on."""
    runner.add_force("Sensor", True)
    runner.run(cycles=1000)
    runner.remove_force("Sensor")
    # Assert the logic handled the stuck sensor correctly
```

And **history** to inspect past states:

```python
runner.step()
runner.step()
runner.step()
# Every scan is an immutable snapshot you can inspect, diff, or rewind
previous = runner.history[-2]    # two scans ago
```

## When tests aren't enough

Sometimes you need to watch logic execute step by step. pyrung includes a VS Code debugger that lets you set breakpoints on individual rungs, step through scans one at a time, watch tag values update live, and force overrides from the debug console. If you've ever debugged Python in VS Code, it works the same way, just with scans instead of lines. See the [DAP Debugger guide](../guides/dap-vscode.md) for setup.

## Exercise

Write a test suite for your traffic light from [Lesson 7](state-machines.md). Cover: normal full cycle, walk request during green, walk request during red (should have no effect), and timing precision (assert exact scan counts for each transition).
