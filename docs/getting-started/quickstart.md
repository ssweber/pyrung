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
from pyrung import Char, Bool, Int, Program, Rung, Tms, copy, on_delay

# State holds the current phase: "g", "y", or "r"
State = Char("State")

# Each phase gets a timer: a done bit and an accumulator
GreenDone  = Bool("GreenDone")
GreenAcc   = Int("GreenAcc")
YellowDone = Bool("YellowDone")
YellowAcc  = Int("YellowAcc")
RedDone    = Bool("RedDone")
RedAcc     = Int("RedAcc")

with Program() as logic:
    # Green for 3 seconds, then yellow
    with Rung(State == "g"):
        on_delay(GreenDone, GreenAcc, preset=3000, unit=Tms)
    with Rung(GreenDone):
        copy("y", State)

    # Yellow for 1 second, then red
    with Rung(State == "y"):
        on_delay(YellowDone, YellowAcc, preset=1000, unit=Tms)
    with Rung(YellowDone):
        copy("r", State)

    # Red for 3 seconds, then green
    with Rung(State == "r"):
        on_delay(RedDone, RedAcc, preset=3000, unit=Tms)
    with Rung(RedDone):
        copy("g", State)
```

Read it like a ladder diagram: `with Rung(State == "g")` is the condition on the left rail. If it's true, power flows into the body — `on_delay` starts accumulating. When the timer hits its preset, the done bit goes true, and the next rung copies a new state.

## Run it

```python
from pyrung import PLCRunner, TimeMode

runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10 ms per scan

# Initialize to green
with runner.active():
    State.value = "g"

# Run for 10 seconds (1,000 scans × 10 ms)
runner.run(cycles=1000)

with runner.active():
    print(f"State: {State.value}")  # Back to green — it's been through a full cycle
```

`FIXED_STEP` advances simulation time by exactly 10 ms per scan. No wall-clock dependency, perfectly repeatable.

## Test it

This is the point of pyrung. Put the same logic in a pytest file and make assertions:

```python
from pyrung import PLCRunner, TimeMode

def test_traffic_light_cycle():
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)

    with runner.active():
        State.value = "g"

    # Green phase lasts 3 seconds = 300 scans
    runner.run(cycles=299)
    with runner.active():
        assert State.value == "g"  # Still green

    runner.step()
    with runner.active():
        assert State.value == "y"  # Just turned yellow

    # Yellow lasts 1 second = 100 scans
    runner.run(cycles=100)
    with runner.active():
        assert State.value == "r"  # Now red

    # Red lasts 3 seconds = 300 scans
    runner.run(cycles=300)
    with runner.active():
        assert State.value == "g"  # Full cycle, back to green
```

Same logic, deterministic timing, real assertions. If this passes, the logic is correct — before it ever touches hardware.

## What's happening under the hood

Each call to `runner.step()` executes one complete scan cycle: evaluate every rung top to bottom, update all outputs, produce an immutable state snapshot. `runner.run(cycles=N)` just calls `step()` N times.

State snapshots are immutable — the runner keeps a history you can inspect, diff, or rewind to. Tags like `State.value` read from the runner's current state when inside a `runner.active()` block.

Timers accumulate across scans. `on_delay` with a 3000 ms preset and a 10 ms scan step needs 300 scans to fire. That's why the math is exact and the tests are deterministic.

## Next steps

The [full traffic light example](../../examples/traffic_light.py) builds on this with structured tags (`@udt`), car counting with edge detection, and a speed history log using `blockcopy`.

From here:

- [Core Concepts](concepts.md) — the scan cycle, SystemState, and how tags work
- [Ladder Logic Guide](../guides/ladder-logic.md) — full DSL reference: branching, counters, subroutines, structured tags
- [Testing Guide](../guides/testing.md) — patterns for unit testing with deterministic time
- [Runner Guide](../guides/runner.md) — time modes, scan history, forking
