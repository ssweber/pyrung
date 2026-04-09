# Quickstart

Build a traffic light that cycles green → yellow → red, then test it.

## Install

```bash
# Requires Python 3.11+
pip install -e .
```

## Your first program

A traffic light is a timer-driven state machine. Each phase runs for a set duration, then transitions to the next.

```python
from pyrung import Char, Timer, Program, Rung, copy, on_delay

# State holds the current phase: "g", "y", or "r"
State = Char("State")

# Each phase gets a named timer
GreenTimer  = Timer.named(1, "GreenTimer")
YellowTimer = Timer.named(2, "YellowTimer")
RedTimer    = Timer.named(3, "RedTimer")

with Program() as logic:
    # Green for 3 seconds, then yellow
    with Rung(State == "g"):
        on_delay(GreenTimer, preset=3000, unit="Tms")
    with Rung(GreenTimer.Done):
        copy("y", State)

    # Yellow for 1 second, then red
    with Rung(State == "y"):
        on_delay(YellowTimer, preset=1000, unit="Tms")
    with Rung(YellowTimer.Done):
        copy("r", State)

    # Red for 3 seconds, then green
    with Rung(State == "r"):
        on_delay(RedTimer, preset=3000, unit="Tms")
    with Rung(RedTimer.Done):
        copy("g", State)
```

Read it like a ladder diagram: `with Rung(State == "g")` is the condition on the left rail. If it's true, power flows into the body — `on_delay` starts accumulating. When the timer hits its preset, `GreenTimer.Done` goes true, and the next rung copies a new state.

## Run it

```python
from pyrung import PLC

with PLC(logic, dt=0.010) as plc:
    State.value = "g"

    # Run for 10 seconds (1,000 scans × 10 ms)
    plc.run(cycles=1000)

    assert State.value == "g"  # Back to green — it's been through a full cycle
```

`dt=0.010` advances simulation time by exactly 10 ms per scan. No wall-clock dependency, perfectly repeatable.

## Test it

This is the point of pyrung. Put the same logic in a pytest file and make assertions:

```python
from pyrung import PLC

def test_traffic_light_cycle():
    with PLC(logic, dt=0.010) as plc:
        State.value = "g"

        # Green phase lasts 3 seconds = 300 scans
        plc.run(cycles=299)
        assert State.value == "g"  # Still green

        plc.step()
        assert State.value == "y"  # Just turned yellow

        # Yellow lasts 1 second = 100 scans
        plc.run(cycles=100)
        assert State.value == "r"  # Now red

        # Red lasts 3 seconds = 300 scans
        plc.run(cycles=300)
        assert State.value == "g"  # Full cycle, back to green
```

Same logic, deterministic timing, real assertions. If this passes, the logic is correct — before it ever touches hardware.

## What's happening under the hood

Each call to `plc.step()` executes one complete scan cycle: evaluate every rung top to bottom, update all outputs, produce an immutable state snapshot. `plc.run(cycles=N)` just calls `step()` N times.

State snapshots are immutable — the runner keeps a history you can inspect, diff, or rewind to. Tags like `State.value` read from the runner's current state when inside the `with PLC(...) as plc:` block.

Timers accumulate across scans. `on_delay` with a 3000 ms preset and a 10 ms scan step needs 300 scans to fire. That's why the math is exact and the tests are deterministic.

## Next steps

The [full traffic light example](https://github.com/ssweber/pyrung/blob/main/examples/traffic_light.py) builds on this with structured tags (`@udt`), car counting with edge detection, and a speed history log using `blockcopy`.

From here:

- [Core Concepts](concepts.md) — the scan cycle, SystemState, and how tags work
- [Instruction Reference](../instructions/index.md) — full DSL reference: branching, counters, subroutines, structured tags
- [Testing Guide](../guides/testing.md) — patterns for unit testing with deterministic time
- [Runner Guide](../guides/runner.md) — time modes, scan history, forking
