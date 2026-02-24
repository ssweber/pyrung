# Core Concepts

## The Redux Mental Model

pyrung is architected like Redux: state is immutable, logic is a pure function, and execution is consumer-driven.

```
Logic(CurrentState) → NextState
```

Nothing is mutated in place. Every `step()` call takes the current `SystemState`, evaluates all rungs as pure functions, and produces a new `SystemState`. The old state is still accessible.

This makes pyrung programs:

- **Deterministic** — the same state + same inputs always produce the same next state
- **Testable** — no hidden mutable globals; every side-effect is captured in `SystemState`
- **Debuggable** — each historical state is a permanent immutable snapshot

## SystemState

```python
class SystemState(PRecord):
    scan_id   : int    # scan counter (resets to 0 on STOP→RUN/reboot)
    timestamp : float  # simulation clock (seconds)
    tags      : PMap   # tag values, keyed by name string
    memory    : PMap   # engine-internal state (edge detection, timer fractionals)
```

`tags` is everything user code touches. `memory` is internal engine bookkeeping — edge detection bits (`rise`/`fall`), timer fractional accumulators, etc.

`SystemState` is a [`PRecord`](https://pyrsistent.readthedocs.io/) from the pyrsistent library — a frozen, persistent data structure that shares structure between versions for memory efficiency.

## Scan Cycle

Every `step()` executes exactly one complete scan cycle:

```
Phase 0  SCAN START     Dialect resets (e.g., Click auto-clears SC40/SC43/SC44)
Phase 1  APPLY PATCH    One-shot inputs from patch() written to context
Phase 2  READ INPUTS    InputBlock values copied from external source
Phase 3  APPLY FORCES   Pre-logic force pass (debug overrides)
Phase 4  EXECUTE LOGIC  Rungs evaluated top-to-bottom
Phase 5  APPLY FORCES   Post-logic force pass (re-assert force values)
Phase 6  WRITE OUTPUTS  OutputBlock values pushed to external sink
Phase 7  ADVANCE CLOCK  scan_id += 1, timestamp updated per TimeMode
Phase 8  SNAPSHOT       New SystemState committed
```

All writes within a scan are batched in a `ScanContext` and committed atomically at phase 8. Rungs see each other's writes immediately — a write in rung 3 is visible to rung 4 in the same scan.

## Tags

A `Tag` is a named, typed reference. It holds no runtime state — all values live in `SystemState.tags`.

```python
Button = Bool("Button")   # TagType.BOOL, not retentive
Step   = Int("Step")      # TagType.INT, retentive by default
Temp   = Real("Temp")     # TagType.REAL, retentive by default
```

Tags are plain Python objects. You can pass them around, store them in lists, use them as dict keys. The engine looks up their values by name in the current `SystemState`.

### Tag types

| Constructor | IEC type | Size | Default retentive |
|-------------|----------|------|-------------------|
| `Bool(name)` | `BOOL` | 1 bit | False |
| `Int(name)` | `INT` | 16-bit signed | True |
| `Dint(name)` | `DINT` | 32-bit signed | True |
| `Real(name)` | `REAL` | 32-bit float | True |
| `Word(name)` | `WORD` | 16-bit unsigned | True |
| `Char(name)` | `CHAR` | 8-bit ASCII | True |

### Retentive vs non-retentive

A **retentive** tag preserves value across **STOP→RUN** transitions.
A **non-retentive** tag resets to its default on **STOP→RUN**.

For **power cycles** (`runner.reboot()`), battery state controls SRAM survival:

- Battery present (`sys.battery_present=True`): all known tags preserve.
- Battery absent (`sys.battery_present=False`): all known tags reset to defaults.

### UDT (User Defined Type)

A `@udt` groups mixed-type fields into a reusable structure. `@udt()` defaults to `count=1` for a single named instance; set `count` higher for multiple instances:

```python
from pyrung import Bool, Field, Int, Real, Rung, auto, latch, udt

@udt()
class Config:
    enable: Bool
    setpoint: Real

@udt(count=3)
class Alarm:
    id: Int = auto()           # per-instance sequence: 1, 2, 3
    active: Bool
    level: Real = Field(retentive=True)
```

Field names for `count=1` use compact `Struct_field` format; multi-count instances are numbered:

```python
Config.enable     # → LiveTag "Config_enable"

Alarm.id          # → Block (all 3 id tags, array mode)
Alarm[1].id       # → LiveTag "Alarm1_id"
Alarm[2].active   # → LiveTag "Alarm2_active"

# Unpack instances for convenience:
Alarm1, Alarm2, Alarm3 = Alarm[1], Alarm[2], Alarm[3]
Alarm1.id         # → LiveTag "Alarm1_id"
```

Pass `numbered=True` to force the numbered format (`Struct1_field`) even when `count=1`, which is useful when the struct may later be cloned under a different name or when you want naming to stay consistent with multi-count structs:

```python
@udt(numbered=True)
class Status:
    ready: Bool
    fault: Bool

Status[1].ready   # → LiveTag "Status1_ready"
```

Type annotations resolve Python primitives (`bool`, `int`, `float`, `str`) and IEC constructors (`Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`) to the corresponding `TagType`.

Use `auto()` for per-instance numeric sequences (INT/DINT/WORD only), `Field(retentive=True)` to mark fields as retentive, and `.clone(name, ...)` to create a copy with a different base name (optionally overriding `count`, and for `@named_array`, `stride`).

### named_array

A `@named_array` groups single-type fields with instance-interleaved memory layout:

```python
from pyrung import Block, Int, auto, named_array

@named_array(Int, count=4, stride=2)
class Sensor:
    reading = 0
    offset = auto()
```

Access patterns mirror UDT:

```python
Sensor.reading    # → Block (array mode)
Sensor[1].reading # → LiveTag "Sensor1_reading"
```

The `stride` parameter sets the per-instance memory footprint. With `stride=2` above, instance 1 occupies slots 0-1, instance 2 occupies slots 2-3, etc. Gaps between defined fields remain unmapped.

Use `.map_to(block_range)` to map a named array to hardware memory:

```python
DS = Block("DS", TagType.INT, 1, 100)
Sensor.map_to(DS.select(1, 8))  # 4 instances × stride 2 = 8 slots
```

## Blocks

A `Block` is a named, typed, 1-indexed array of tags — used for physical I/O and grouped memory.

```python
from pyrung import Block, InputBlock, OutputBlock, TagType

# Internal memory block (DS1..DS100)
DS = Block("DS", TagType.INT, 1, 100)

# Physical inputs (X001..X016) — elements are InputTag
X = InputBlock("X", TagType.BOOL, 1, 16)

# Physical outputs (Y001..Y016) — elements are OutputTag
Y = OutputBlock("Y", TagType.BOOL, 1, 16)
```

Indexing a block creates (and caches) a `Tag`:

```python
DS[1]   # → LiveTag("DS1", TagType.INT)
X[1]    # → LiveInputTag("X1", TagType.BOOL)   — has .immediate
Y[1]    # → LiveOutputTag("Y1", TagType.BOOL)  — has .immediate
```

### Per-slot runtime policy

Blocks support first-class per-slot policy for names, retention, and defaults:

```python
DS = Block("DS", TagType.INT, 1, 10, retentive=False, default_factory=lambda a: a)

DS.rename_slot(2, "Speed_Setpoint")
DS.configure_slot(2, retentive=True, default=500)
DS.configure_range(5, 8, default=42)

cfg = DS.slot_config(2)
cfg.name                 # "Speed_Setpoint"
cfg.retentive            # True
cfg.default              # 500
cfg.name_overridden      # True
cfg.retentive_overridden # True
cfg.default_overridden   # True
```

Precedence for effective slot policy:

- `name`: slot rename > generated block name
- `retentive`: slot override > block `retentive`
- `default`: slot override > `default_factory(addr)` > type default

Configuration must happen **before** the slot is materialized (`DS[n]`).
If a slot was already indexed, `rename_slot`, `clear_slot_name`, `configure_*`,
and `clear_*` for that slot raise `ValueError`.

!!! note "1-indexed"
    Block addresses start at 1, matching PLC conventions. `Block[0]` raises `IndexError`.

### Block ranges for bulk operations

Use `.select(start, end)` to get a range window for bulk operations like `blockcopy`, `fill`, `search`, `shift`, and `pack`:

```python
DS.select(1, 10)      # → BlockRange of DS1..DS10 (inclusive)
X.select(1, 16)       # → BlockRange of X1..X16
```

## Consumer-Driven Execution

The engine never runs unsolicited. You call:

- `runner.step()` — one complete scan cycle
- `runner.run(cycles)` — exactly N scan cycles
- `runner.run_for(seconds)` — run until simulation time advances by N seconds
- `runner.run_until(predicate)` — run until a condition is met
- `runner.scan_steps()` — rung-by-rung generator for DAP debugging
- `runner.stop()` — transition to STOP mode
- `runner.reboot()` — power-cycle simulation (battery-aware)

If the runner is in STOP mode, calling any execution method (`step`, `run`, `run_for`,
`run_until`, `scan_steps`) automatically performs STOP→RUN transition first.

This inversion of control is what makes pyrung suitable for testing, GUIs, and debuggers — any consumer can drive execution at whatever granularity it needs.

## Time Modes

```python
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)   # deterministic
runner.set_time_mode(TimeMode.REALTIME)               # wall-clock
```

| Mode | Use case | Behavior |
|------|----------|----------|
| `FIXED_STEP` | Unit tests, offline simulation | `timestamp += dt` each scan |
| `REALTIME` | Integration tests, live hardware | `timestamp` = actual elapsed time |

`FIXED_STEP` is the default and the right choice for most situations. Timer and counter instructions use `timestamp` to measure elapsed time, so `FIXED_STEP` gives perfectly reproducible results regardless of machine speed.
`REALTIME` is intentionally non-deterministic; scan timing depends on the host scheduler and wall-clock behavior.

## Inputs: patch vs force

**`patch()`** — one-shot input. The value is applied at the start of the next scan and then discarded. Use for momentary button presses, external reads, or test scenarios.

```python
runner.patch({"Button": True})
runner.step()   # Button is True during this scan
runner.step()   # Button is back to False
```

**`add_force()`** — persistent override. The value is re-applied every scan until removed. Use for stuck inputs, test fixtures, or override scenarios.

```python
runner.add_force("Button", True)
runner.step()   # Button is True
runner.step()   # Button is still True
runner.remove_force("Button")
```

See [Forces and Debug Overrides](../guides/forces-debug.md) for detailed force semantics.
