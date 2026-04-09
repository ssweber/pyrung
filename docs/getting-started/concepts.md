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
    on_delay(GreenTimer, preset=3000, unit="Tms")
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

on_delay(MyTimer, preset=3000, unit="Tms")   # Timer: accumulate while rung is true
count_up(MyCounter, preset=100)              # Counter: increment each scan
```

`out` vs `latch`: `out` follows the rung — true when the rung is true, false when it's false. `latch` is sticky — once set, it stays set until explicitly `reset`.

The full instruction set (branching, subroutines, shift registers, edge detection, and more) is in the [Instruction Reference](../instructions/index.md).

## Timers and counters

`Timer` and `Counter` are built-in structured types. Each has a `.Done` bit and an `.Acc` accumulator. Use `Timer.named(n, "Name")` for named instances:

```python
from pyrung import Timer, Counter

GreenTimer = Timer.named(1, "GreenTimer")

with Rung(State == "g"):
    on_delay(GreenTimer, preset=3000, unit="Tms")  # 3000 ms
```

The accumulator tracks progress in the unit you specify (`"Tms"` for milliseconds). If the rung goes false before the preset, the accumulator resets (that's `on_delay` — use `off_delay` for the inverse behavior).

Counters increment once per scan while enabled. Use `rise()` on the rung condition if you want one increment per leading edge:

```python
PartCounter = Counter.named(1, "PartCounter")

with Rung(rise(Sensor)):
    count_up(PartCounter, preset=9999).reset(CountReset)
```

## Instruction pins

Some instructions have extra condition inputs beyond the rung — like the `.reset()` on the counter example above. These are pins on the instruction block.

```
                    ┌─────────────────┐
 Sensor ───────────▶│   count_up       │
                    │                  │──▶ .Done
 Reverse ──.down()─▶│  preset: 100     │──▶ .Acc
 Home, Auto .reset()▶│                  │
                    └─────────────────┘
```

The rung condition powers the instruction (top wire). Other pins are wired with dot-methods: `.down()`, `.reset()`, `.clock()`. Multiple conditions on one pin AND together — `Home, Auto` on `.reset()` means both must be True.

```
                       ┌─────────────────┐
 State == "g" ────────▶│   on_delay       │
                       │                  │──▶ .Done
                       │  preset: 3000    │──▶ .Acc
 StopBtn, Fault .reset()▶│  unit: "Tms"     │
                       └─────────────────┘
```

Each pin gets its own line with `\` continuation:

```python
with Rung(rise(Sensor)):
    count_up(PartCounter, preset=100) \
        .down(Reverse) \
        .reset(Home, Auto)
```

Reads directly off the diagram — the pin name in the ASCII maps to the dot-method in the Python.

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

Both forms produce the same thing — a `Program` you pass to `PLC`.

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

ds = Block("DS", TagType.INT, 1, 100)         # Internal memory DS1..DS100
x  = InputBlock("X", TagType.BOOL, 1, 16)     # Physical inputs X1..X16
y  = OutputBlock("Y", TagType.BOOL, 1, 16)    # Physical outputs Y1..Y16
```

Index into a block to get a tag:

```python
ds[1]   # Tag "DS1", INT
x[1]    # Input tag "X1", BOOL
y[1]    # Output tag "Y1", BOOL
```

Use `.select()` for bulk operations:

```python
blockcopy(ds.select(1, 4), ds.select(2, 5))  # Shift DS1..DS4 into DS2..DS5
```

## Reading and writing values

Inside a `with PLC(...) as plc:` block (or `with runner:` when you have a runner from a fixture), you can read and write tag values directly:

```python
with PLC(logic) as plc:
    State.value = "g"           # Write (one-shot, consumed after one scan)
    print(State.value)          # Read
    plc.step()                  # Step with current values
```

For persistent overrides that hold across multiple scans, use forces:

```python
plc.force("Button", True)
plc.step()   # True
plc.step()   # Still True
plc.unforce("Button")
```

## System points

The PLC exposes built-in status and control through the `system` namespace. Import it with `from pyrung import system`.

**`system.sys`** — scan-level status: `always_on`, `first_scan`, clock toggles (`clock_10ms` through `clock_1h`), `mode_run`, `scan_counter`. Use `first_scan` for one-time initialization:

```python
with Rung(system.sys.first_scan):
    copy("g", State)
```

**`system.fault`** — math and runtime fault flags: `division_error`, `out_of_range`, `math_operation_error`, `address_error`, `plc_error`, and `code` (the most recent fault code as an integer). Fault flags are auto-cleared at the start of each scan.

```python
with Rung(system.fault.division_error):
    latch(MathFaultSeen)
```

**`system.rtc`** — real-time clock: `year4`, `month`, `day`, `hour`, `minute`, `second` (read-only). Writable counterparts (`new_hour`, etc.) with `apply_date`/`apply_time` triggers. Use for time-of-day logic like shift changes.

The [Click cheatsheet](../guides/click-cheatsheet.md#system-points) has the full point-to-address mapping.

## Next steps

- [Quickstart](quickstart.md) — build and test a traffic light
- [Instruction Reference](../instructions/index.md) — full instruction reference
- [Testing Guide](../guides/testing.md) — patterns for deterministic testing
- [Architecture](../guides/architecture.md) — engine internals, scan phases, SystemState
