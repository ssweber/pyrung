# Lesson 10: Testing

If you know pytest, you already know how to test pyrung. No `plc-test` framework to learn, no proprietary test runner, no XML config. Standard pytest fixtures and asserts.

This is where pyrung pays for itself. Everything you've built -- the motor control, the sorting sequence, the bin counters, the mode switching -- is testable with standard pytest. No hardware, no manual verification, no "download and hope."

```python
import pytest
from pyrung import PLC

@pytest.fixture
def plc():
    r = PLC(logic, dt=0.010)
    r.force(StopBtn, True)            # NC inputs: healthy wiring
    r.force(EstopOK, True)
    r.force(Auto, True)               # Default to auto mode
    return r
```

Remember the `dt=0.010` determinism from [Lesson 5](timers.md)? This is what it was for. Every test in this lesson runs in deterministic, reproducible scan time -- no flaky tests, no timing race conditions, no "works on my machine." One scan, one tick, every time.

Each test gets its own `PLC` because pytest's default scope is `function` -- no state accumulates between tests. All tag I/O and stepping happens inside the context manager:

```python
def test_start_stop(plc):
    with plc:
        StartBtn.value = True
        plc.step()
        StartBtn.value = False
        plc.step()
        assert Running.value is True
        assert ConveyorMotor.value is True

def test_estop_overrides_start(plc):
    """Safety: E-stop kills everything, even if Start is held."""
    plc.unforce(EstopOK)
    with plc:
        EstopOK.value = False
        StartBtn.value = True
        plc.step()
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
def test_small_vs_large_box(plc):
    """Same setup, two outcomes."""
    with plc:
        SizeThreshold.value = 100
        StartBtn.value = True
        plc.step()
        EntrySensor.value = True
        plc.step()

    # Fork: large box — run past detection, check mid-sorting
    large = plc.fork()
    large.force(SizeReading, 150)
    with large:
        large.run(cycles=50)
        assert State.value == 2              # SORTING
        assert DiverterCmd.value is True

    # Fork: small box
    small = plc.fork()
    small.force(SizeReading, 50)
    with small:
        small.run(cycles=50)
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
def test_box_classification(plc, box_size, expected_diverter):
    with plc:
        SizeThreshold.value = 100
        StartBtn.value = True
        plc.step()

        plc.force(EntrySensor, True)
        plc.force(SizeReading, box_size)
        plc.run(cycles=55)                    # Past detection, mid-sorting
        assert DiverterCmd.value is expected_diverter
```

The kind of boundary testing that's agonizing on real hardware -- load 5 specific test boxes, push them through by hand -- and trivial in pyrung.

## History for post-mortem debugging

!!! tip "Also impossible on real hardware"

    Real PLC software has trends and trace buffers, but they're sampled and lossy. pyrung's history is **every scan, every tag, immutable, indexable**. This is post-mortem debugging -- the alarm fired, you have the complete record, and you can walk backwards until you find the cause.

```python
plc.run(cycles=100)
assert JamAlarm.value is True

# Why did the jam fire? Walk back through scans:
for i in range(-1, -10, -1):
    snapshot = plc.history[i]
    print(f"scan {i}: State={snapshot[State]} EntrySensor={snapshot[EntrySensor]}")
```

## Driving signals: values, forces, and patches

Three ways to set a tag's value, each with different persistence:

| Mechanism | Persistence | Use case |
|---|---|---|
| `tag.value = X` (inside `with plc:`) | one scan | Setting an initial value, simulating a one-shot input |
| `plc.force(tag, X)` | persistent until removed | Holding a sensor on across many scans |
| `plc.unforce(tag)` | releases the force | Letting the logic see the computed value again |

Forces are how the sorting test above keeps `EntrySensor` on across 55+ scans without re-setting it every cycle:

```python
def test_sorting_sequence(plc):
    """Full auto sort: box arrives, gets classified, exits to correct bin."""
    with plc:
        SizeThreshold.value = 100
        StartBtn.value = True
        plc.step()

        plc.force(EntrySensor, True)
        plc.force(SizeReading, 150)       # Large box

        # Run past detection period into sorting
        plc.run(cycles=55)
        assert DiverterCmd.value is True     # Extended for large box

        plc.unforce(EntrySensor)
        plc.run(cycles=250)                   # Past hold period
        assert DiverterCmd.value is False    # Retracted after sort
        assert State.value == 0              # Back to idle
```

!!! warning "Forces deserve respect"

    Forcing is a real debugging feature on every PLC platform. On real hardware, forces override the program's control of physical outputs and bypass safety interlocks — that's why real PLCs gate force mode behind confirmation dialogs. When you `force()`, you are telling the engine "ignore whatever the logic computes for this tag." Use forces for testing. Treat them with the same caution you'd give the real thing.

## When tests aren't enough

Sometimes you need to watch logic execute step by step. pyrung includes a VS Code debugger that lets you set breakpoints on individual rungs, pause *between* rungs within a single scan, watch tag values update live, and force overrides from the debug console. Real PLC editors show you live rung state, but they can't stop the scan partway through -- the whole program executes as one atomic pass. pyrung can. See the [DAP Debugger guide](../guides/dap-vscode.md) for setup.

## Exercise

Write a test that covers the full conveyor lifecycle: start in auto mode, sort a large box (verify diverter extends and Bin B counter increments), sort a small box (verify diverter stays retracted and Bin A counter increments), then E-stop mid-sort and verify everything shuts down cleanly. Use `fork()` to test the large and small paths from a shared starting point.

---

The logic is tested, the tests pass. Now deploy it.

!!! info "Also known as..."

    `plc.step()` is "single scan" — some PLC simulators expose it, many don't. `force`/`unforce` mirror the universal Force On/Off feature. `history[-N]` is like a trend or data log, except trends are sampled and lossy. `fork()`, deterministic `dt` time, and full-scan history have **no equivalent on real PLCs**.
