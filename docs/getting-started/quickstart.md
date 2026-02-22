# Quickstart

This guide walks through a complete pyrung program in five steps: define tags, write logic, create a runner, inject inputs, and read state.

## 1. Define tags

Tags are named, typed references to values. They hold no runtime state — values live in `SystemState`.

```python
from pyrung import Bool, Int, PLCRunner, Program, Rung, TimeMode, latch, math, reset

# Boolean (1 bit, not retentive by default)
StartButton = Bool("StartButton")
MotorRunning = Bool("MotorRunning")

# 16-bit signed integer (retentive by default)
Step = Int("Step")
```

## 2. Write logic

Logic is written with context managers. `Program` collects rungs. `Rung` evaluates conditions and executes instructions.

```python
with Program() as logic:
    # Rung 1: Start motor on button press
    with Rung(StartButton):
        latch(MotorRunning)

    # Rung 2: Stop motor when Step reaches 10
    with Rung(Step >= 10):
        reset(MotorRunning)

    # Rung 3: Increment step while motor is running
    with Rung(MotorRunning):
        math(Step + 1, Step)
```

## 3. Create a runner

`PLCRunner` takes a `Program` and executes it scan-by-scan.

```python
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)  # 100ms per scan
```

`FIXED_STEP` advances simulation time by a fixed `dt` each scan, giving deterministic timing regardless of wall-clock speed. Use it for tests and offline simulation.

## 4. Inject inputs and step

```python
# Queue a one-shot input (consumed after one scan)
runner.patch({"StartButton": True})

# Execute one complete scan cycle
state = runner.step()
```

After this step:

- `StartButton` was True → Rung 1 fired → `MotorRunning` is now latched True
- Rung 2 did not fire (Step is still 0)
- Rung 3 fired → Step incremented to 1

## 5. Read state

```python
print(state.tags["MotorRunning"])  # True
print(state.tags["Step"])          # 1
print(state.scan_id)               # 1
print(state.timestamp)             # 0.1
```

`SystemState` is immutable. Every scan produces a new snapshot. The runner's `current_state` always points to the latest committed state.

## Running multiple scans

```python
# Run until MotorRunning turns False (or 100 scans max)
state = runner.run_until(
    lambda s: not s.tags.get("MotorRunning", True),
    max_cycles=100,
)
print(f"Motor stopped at scan {state.scan_id}, time {state.timestamp:.1f}s")
```

## Reading state with `.value`

Tags support live value access inside a `runner.active()` scope:

```python
with runner.active():
    print(MotorRunning.value)  # reads from runner's pending state
    Step.value = 5             # queues a patch (one-shot)
```

## Complete example

```python
from pyrung import Bool, Int, PLCRunner, Program, Rung, TimeMode, latch, math, reset

StartButton  = Bool("StartButton")
MotorRunning = Bool("MotorRunning")
Step         = Int("Step")

with Program() as logic:
    with Rung(StartButton):
        latch(MotorRunning)
    with Rung(Step >= 10):
        reset(MotorRunning)
    with Rung(MotorRunning):
        math(Step + 1, Step)

runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

runner.patch({"StartButton": True})
for _ in range(15):
    runner.step()

state = runner.current_state
print(state.tags["MotorRunning"])  # False — stopped after Step hit 10
print(state.tags["Step"])          # 10
```

## Next steps

- [Core Concepts](concepts.md) — understand the Redux mental model and scan cycle
- [Writing Ladder Logic](../guides/ladder-logic.md) — full DSL reference: conditions, branches, timers, counters
- [Running and Stepping](../guides/runner.md) — `scan_steps()`, `run_until()`, time modes
