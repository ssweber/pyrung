# Core Concepts

This page covers the vocabulary you need to write and understand a pyrung program. For engine internals and architecture, see [Architecture](../guides/architecture.md).

## Scans

A PLC doesn't run code line by line — it runs in **scans**. Each scan works through every rung in order, first to last. On each rung, conditions are checked, then instructions execute. Once every rung has been evaluated, that's one scan.

```python
runner.step()           # Execute one scan
runner.run(cycles=100)  # Execute 100 scans
```

## Tags

A tag is a named, typed value. Think of it as a PLC address with a human-readable name.

```python
from pyrung import Bool, Int, Real, Char

Button  = Bool("Button")    # 1 bit, resets on STOP→RUN
Step    = Int("Step")       # 16-bit signed, retentive
Temp    = Real("Temp")      # 32-bit float, retentive
State   = Char("State")     # 8-bit ASCII, retentive
```

Tags don't hold values themselves — they're references. Values live in the system state and update each scan.

| Constructor | Size | Retentive by default |
|-------------|------|---------------------|
| `Bool` | 1 bit | No |
| `Int` | 16-bit signed | Yes |
| `Dint` | 32-bit signed | Yes |
| `Real` | 32-bit float | Yes |
| `Word` | 16-bit unsigned | Yes |
| `Char` | 8-bit ASCII | Yes |

**Retentive** means the value survives a STOP→RUN transition. Non-retentive tags reset to their defaults.

## Rungs

A rung is a `with` block. The condition goes on the `Rung`, the instructions go in the body. If the condition is true, the instructions execute. If false, instructions that depend on the live power rail are turned off.

```python
with Rung(Button):
    latch(MotorRunning)
```

This reads like a ladder diagram: `Button` is the contact on the left rail, `latch(MotorRunning)` is the coil on the right. If `Button` is true, `MotorRunning` gets latched.

Conditions can be combined and compared:

```python
with Rung(Button & ~EStop):       # AND + NOT
    latch(MotorRunning)

with Rung(Temp > 150.0):          # Comparison
    out(OverTempAlarm)

with Rung(State == "g"):          # Equality
    on_delay(GreenDone, GreenAcc, preset=3000, unit=Tms)
```

### Branches

A `branch` creates a parallel condition within a rung — like a parallel path on a ladder diagram:

```python
with Rung(First):          # ① Evaluate: First
    out(Third)             # ③ Execute
    with branch(Second):   # ② Evaluate: First AND Second
        out(Fourth)        # ④ Execute
    out(Fifth)             # ⑤ Execute
```

Three rules:

- **Conditions evaluate before instructions.** ① and ② are resolved before ③ ④ ⑤ run. A branch ANDs its own condition with the parent rung's.
- **Instructions execute in source order.** ③ → ④ → ⑤, as written — not "all rung, then all branch."
- **Each rung starts fresh.** The next rung sees the state as it was left after the previous rung's instructions.

## Instructions

Instructions are what go inside a rung. Here are the ones you'll use most often:

```python
out(Light)                    # Energize while rung is true, de-energize when false
latch(Motor)                  # Set and hold — stays true even if rung goes false
reset(Motor)                  # Clear a latched tag

copy("g", State)              # Copy a value into a tag
calc(Step + 1, Step)          # Evaluate an expression, store the result

on_delay(Done, Acc, preset=3000, unit=Tms)   # Timer: accumulate while rung is true
count_up(Done, Acc, preset=100)              # Counter: increment on rising edge
```

`out` vs `latch`: `out` follows the rung — true when the rung is true, false when it's false. `latch` is sticky — once set, it stays set until explicitly `reset`.

The full instruction set (branching, subroutines, shift registers, edge detection, and more) is in the [Ladder Logic Guide](../guides/ladder-logic.md).

## Timers and counters

Timers accumulate time while their rung is true. When the accumulator reaches the preset, the done bit fires.

```python
GreenDone = Bool("GreenDone")
GreenAcc  = Int("GreenAcc")

with Rung(State == "g"):
    on_delay(GreenDone, GreenAcc, preset=3000, unit=Tms)  # 3000 ms
```

The accumulator tracks progress in the unit you specify (`Tms` for milliseconds). If the rung goes false before the preset, the accumulator resets (that's `on_delay` — use `off_delay` for the inverse behavior).

Counters work on edges — each rising edge of the rung increments the accumulator:

```python
with Rung(rise(Sensor)):
    count_up(CountDone, CountAcc, preset=9999).reset(CountReset)
```

`rise()` detects the transition from false to true, so holding the sensor high only counts once.

## Programs

`Program` collects your rungs into a unit of logic that a runner can execute.

```python
with Program() as logic:
    with Rung(Start):
        latch(Running)
    with Rung(Stop):
        reset(Running)
```

For larger programs, use the `@program` decorator to define logic as a function:

```python
@program
def logic():
    with Rung(Start):
        latch(Running)
    with Rung(Stop):
        reset(Running)
```

Both forms produce the same thing — a `Program` you pass to `PLCRunner`.

## Structured tags (UDTs)

When you have a group of related tags, a `@udt` keeps them organized:

```python
from pyrung import udt

@udt()
class Motor:
    running: Bool
    speed: Int
    fault: Bool
```

Access fields with dot notation:

```python
with Rung(Motor.running):
    out(StatusLight)
```

For multiple instances of the same structure, set `count`:

```python
@udt(count=3)
class Pump:
    running: Bool
    flow: Real

# Access by instance
with Rung(Pump[1].running):
    out(Pump1Light)
```

## Blocks

A block is a contiguous array of tags — used for grouped memory and physical I/O. Addresses typically start at 1 to match PLC conventions, but any start index is supported.

```python
from pyrung import Block, InputBlock, OutputBlock, TagType

DS = Block("DS", TagType.INT, 1, 100)         # Internal memory DS1..DS100
X  = InputBlock("X", TagType.BOOL, 1, 16)     # Physical inputs X1..X16
Y  = OutputBlock("Y", TagType.BOOL, 1, 16)    # Physical outputs Y1..Y16
```

Index into a block to get a tag:

```python
DS[1]   # Tag "DS1", INT
X[1]    # Input tag "X1", BOOL
Y[1]    # Output tag "Y1", BOOL
```

Use `.select()` for bulk operations:

```python
blockcopy(DS.select(1, 4), DS.select(2, 5))  # Shift DS1..DS4 into DS2..DS5
```

## Reading and writing values

Inside a `runner.active()` block, you can read and write tag values directly:

```python
with runner.active():
    State.value = "g"           # Write (one-shot, consumed after one scan)
    print(State.value)          # Read
    runner.step()               # Step with current values
```

For persistent overrides that hold across multiple scans, use forces:

```python
runner.add_force("Button", True)
runner.step()   # True
runner.step()   # Still True
runner.remove_force("Button")
```

## Next steps

- [Quickstart](quickstart.md) — build and test a traffic light
- [Ladder Logic Guide](../guides/ladder-logic.md) — full instruction reference
- [Testing Guide](../guides/testing.md) — patterns for deterministic testing
- [Architecture](../guides/architecture.md) — engine internals, scan phases, SystemState
