# Lesson 10: Testing

If you know pytest, you already know how to test pyrung. No `plc-test` framework to learn, no proprietary test runner, no XML config. Standard pytest fixtures and asserts.

This is where pyrung pays for itself. Everything you've built -- the motor control, the sorting sequence, the bin counters, the mode switching -- is testable with standard pytest. No hardware, no manual verification, no "download and hope."

```python
import pytest
from pyrung import PLCRunner, TimeMode

@pytest.fixture
def runner():
    r = PLCRunner(logic)
    r.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
    r.add_force(StopBtn, True)        # NC inputs: healthy wiring
    r.add_force(EstopOK, True)
    r.add_force(Auto, True)           # Default to auto mode
    return r
```

Remember the `FIXED_STEP` determinism from [Lesson 5](timers.md)? This is what it was for. Every test in this lesson runs in deterministic, reproducible scan time -- no flaky tests, no timing race conditions, no "works on my machine." One scan, one tick, every time.

Each test gets its own `PLCRunner` because pytest's default scope is `function` -- no state accumulates between tests. All tag I/O and stepping happens inside `with runner.active()`:

```python
def test_start_stop(runner):
    with runner.active():
        StartBtn.value = True
        runner.step()
        StartBtn.value = False
        runner.step()
        assert Running.value is True
        assert ConveyorMotor.value is True

def test_estop_overrides_start(runner):
    """Safety: E-stop kills everything, even if Start is held."""
    runner.remove_force(EstopOK)
    with runner.active():
        EstopOK.value = False
        StartBtn.value = True
        runner.step()
        assert Running.value is False
        assert ConveyorMotor.value is False
```

## Fork for parallel scenarios

!!! tip "Impossible on real hardware"

    You can't fork a real conveyor. You can't pause a real PLC, copy its state, and run two futures in parallel from the same instant. With pyrung you can. This is how you test "what if the part is large vs small," "what if the operator hits stop now vs in 100 ms," "what if the network packet arrives before vs after the sensor edge." Two assertions from one starting point, no setup duplication, no flakiness.

Test two outcomes from the same starting point without resetting:

```
  Setup: start conveyor, box arrives at sensor
                      |
                runner.fork()
                  +---+---+
                  v       v
          SizeReading  SizeReading
            = 150        = 50
              |            |
              v            v
          DiverterCmd  DiverterCmd
           = True       = False
```

```python
def test_small_vs_large_box(runner):
    """Same setup, two outcomes."""
    with runner.active():
        SizeThreshold.value = 100
        StartBtn.value = True
        runner.step()
        EntrySensor.value = True
        runner.step()

    # Fork: large box — run past detection, check mid-sorting
    large = runner.fork()
    large.add_force(SizeReading, 150)
    large.run(cycles=50)
    with large.active():
        assert State.value == 2              # SORTING
        assert DiverterCmd.value is True

    # Fork: small box
    small = runner.fork()
    small.add_force(SizeReading, 50)
    small.run(cycles=50)
    with small.active():
        assert State.value == 2
        assert DiverterCmd.value is False
```

`fork()` branches state *mid-test* from a shared dynamic starting point. For testing the *whole* test with different starting conditions, use `pytest.mark.parametrize`:

```python
@pytest.mark.parametrize("box_size,expected_diverter", [
    (50,  False),   # small
    (150, True),    # large
    (99,  False),   # boundary, just under
    (100, False),   # boundary, exactly at threshold
    (101, True),    # boundary, just over
])
def test_box_classification(runner, box_size, expected_diverter):
    with runner.active():
        SizeThreshold.value = 100
        StartBtn.value = True
        runner.step()

    runner.add_force(EntrySensor, True)
    runner.add_force(SizeReading, box_size)
    runner.run(cycles=55)                    # Past detection, mid-sorting
    with runner.active():
        assert DiverterCmd.value is expected_diverter
```

The kind of boundary testing that's agonizing on real hardware -- load 5 specific test boxes, push them through by hand -- and trivial in pyrung.

## History for post-mortem debugging

!!! tip "Also impossible on real hardware"

    Real PLC software has trends and trace buffers, but they're sampled and lossy. pyrung's history is **every scan, every tag, immutable, indexable**. This is post-mortem debugging -- the alarm fired, you have the complete record, and you can walk backwards until you find the cause.

```python
runner.run(cycles=100)
with runner.active():
    assert JamAlarm.value is True

# Why did the jam fire? Walk back through scans:
for i in range(-1, -10, -1):
    snapshot = runner.history[i]
    print(f"scan {i}: State={snapshot[State]} EntrySensor={snapshot[EntrySensor]}")
```

## Driving signals: values, forces, and patches

Three ways to set a tag's value, each with different persistence:

| Mechanism | Persistence | Use case |
|---|---|---|
| `tag.value = X` (inside `with runner.active()`) | one scan | Setting an initial value, simulating a one-shot input |
| `runner.add_force(tag, X)` | persistent until removed | Holding a sensor on across many scans |
| `runner.remove_force(tag)` | releases the force | Letting the logic see the computed value again |

Forces are how the sorting test above keeps `EntrySensor` on across 55+ scans without re-setting it every cycle:

```python
def test_sorting_sequence(runner):
    """Full auto sort: box arrives, gets classified, exits to correct bin."""
    with runner.active():
        SizeThreshold.value = 100
        StartBtn.value = True
        runner.step()

    runner.add_force(EntrySensor, True)
    runner.add_force(SizeReading, 150)       # Large box

    # Run past detection period into sorting
    runner.run(cycles=55)
    with runner.active():
        assert DiverterCmd.value is True     # Extended for large box

    runner.remove_force(EntrySensor)
    runner.run(cycles=250)                   # Past hold period
    with runner.active():
        assert DiverterCmd.value is False    # Retracted after sort
        assert State.value == 0              # Back to idle
```

!!! warning "Forces deserve respect"

    Forcing is a *real* debugging feature on every PLC platform -- Click, Rockwell, Do-More, Codesys. On real hardware, forces override the program's control of physical outputs and bypass safety interlocks. That's why real PLCs gate force mode behind confirmation dialogs -- Rockwell warns of *injury or death*, Codesys requires an explicit force-enable step. pyrung mirrors the API because the concept matters: when you `add_force`, you are telling the engine "ignore whatever the logic computes for this tag." Use forces for testing. Treat them with the same caution you'd give the real thing.

## When tests aren't enough

Sometimes you need to watch logic execute step by step. pyrung includes a VS Code debugger that lets you set breakpoints on individual rungs, pause *between* rungs within a single scan, watch tag values update live, and force overrides from the debug console. Real PLC editors show you live rung state, but they can't stop the scan partway through -- the whole program executes as one atomic pass. pyrung can. See the [DAP Debugger guide](../guides/dap-vscode.md) for setup.

!!! info "Also known as..."

    `runner.step()` advances exactly one scan -- some PLCs expose this as "single scan" or "test single scan" in their simulator, but many don't. `add_force`/`remove_force` mirror the universal Force On/Off features -- forcing is a real debugging tool everywhere, not a pyrung invention. `history[-N]` is sort of like a trend or data log, except trends are sampled and lossy. And then: `fork()`, `FIXED_STEP` deterministic scan time, and full-scan history have **no equivalent on real PLCs**.

## Exercise

Write a test that covers the full conveyor lifecycle: start in auto mode, sort a large box (verify diverter extends and Bin B counter increments), sort a small box (verify diverter stays retracted and Bin A counter increments), then E-stop mid-sort and verify everything shuts down cleanly. Use `fork()` to test the large and small paths from a shared starting point.

---

The logic is tested, the tests pass. Now deploy it.
