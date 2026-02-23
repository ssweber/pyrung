# Click PLC Engine: Architecture & API Specification

## Core Philosophy

1. **Redux Architecture:** Logic is a pure function. `Logic(Current_State) -> Next_State`. State is never mutated in place.

2. **Generator Driver:** The engine yields control after every atomic step, allowing the consumer (GUI, CLI, test runner) to inject inputs, inspect history, or pause execution.

3. **Time is a Variable:** Execution produces a stream of immutable snapshots. The consumer can pause, rewind, and inspect any historical state.

4. **Write First, Validate Later:** pyrung is a Pythonic PLC engine, whose first module emulates a CLICK PLC. Write logic with semantic tags and native Python expressions. Map tags to hardware addresses when you're ready. Run the validator and iterate until it's clean.

---

## Architecture Principles

| Principle | Implementation |
|-----------|----------------|
| Inversion of Control | Engine is a generator; consumer drives via `step()` |
| Immutable State | `pyrsistent.PRecord` + `PMap` |
| Pure Function Logic | `Rung.evaluate(state) -> new_state` |
| Live Object Graph | Rungs/Contacts/Coils are persistent objects with stable IDs |
| Visualization Protocol | Logic objects implement `render(state)` for historical playback |
| History Buffer | List of snapshots; `seek`/`rewind` for inspection, `fork_from` for branching |
| Predicate Breakpoints | `when(lambda).pause()` for conditional halts |
| Shared Hardware Model | `pyclickplc` provides address model, types, and file I/O |
| Tag Mapping | Semantic tags bind to hardware addresses; validation checks compatibility |

---

## pyclickplc Dependency

pyrung depends on `pyclickplc` for all CLICK hardware knowledge. This package is the shared source of truth between pyrung (simulation), ClickNick (GUI editor), and any future CLICK tooling.

**What pyclickplc provides:**

| Module | Contents |
|--------|----------|
| `clickplc.banks` | `ADDRESS_RANGES`, `MEMORY_TYPE_BASES`, `DataType` enum, `DEFAULT_RETENTIVE`, interleaved pair definitions |
| `clickplc.addresses` | `AddressRecord` dataclass, `get_addr_key`, `format_address_display`, `parse_address_display`, XD/YD helpers |
| `clickplc.blocks` | `BlockTag`, `BlockRange`, `MemoryBankMeta`, block tag parsing, `extract_bank_metas()` |
| `clickplc.nicknames` | CSV read/write, `load_nickname_file()` → `NicknameProject` |
| `clickplc.validation` | `NICKNAME_MAX_LENGTH`, `FORBIDDEN_CHARS`, `RESERVED_NICKNAMES`, `validate_nickname()` |

**What pyrung owns:**

- `MemoryBank` — live, indexable, simulatable tag groups (constructed from `MemoryBankMeta` or from scratch)
- `Tag` — simulation-side tag objects with type metadata
- `TagMap` — mapping + validation logic
- `PLCRunner` — engine, history, debug
- All DSL syntax (`Rung`, `Program`, instructions)

pyrung re-exports pyclickplc types where convenient but does not redefine the hardware model.

---

## The Data Structure

```python
from pyrsistent import PRecord, field, pmap, PMap

class SystemState(PRecord):
    scan_id = field(type=int, initial=0)
    timestamp = field(type=float, initial=0.0)  # Simulation clock
    tags = field(type=PMap, initial=pmap())     # Tag values (bool, int, float)
    memory = field(type=PMap, initial=pmap())   # Internal state for timers/counters
```

---

## Time Modes

```python
class TimeMode(Enum):
    REALTIME = "realtime"      # Simulation clock = wall clock
    FIXED_STEP = "fixed_step"  # Each scan = fixed dt
```

| Mode | Use Case | Behavior |
|------|----------|----------|
| `REALTIME` | Integration tests, hardware-in-loop, live GUI | `timestamp` advances with actual elapsed time |
| `FIXED_STEP` | Unit tests, deterministic timing, fast execution | `timestamp += dt` each scan, regardless of wall clock |

---

## API Reference

### Configuration & Execution

```python
runner = PLCRunner(logic_graph, initial_state)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
runner.simulation_time                   # Property: current timestamp (float)

runner.step()                            # Advance one scan
runner.run(cycles=N)                     # Run N scans
runner.run_for(seconds=N)               # Run until simulation clock advances N seconds
runner.run_until(predicate)              # Run until predicate(state) returns True
runner.current_state                     # Property: snapshot at tip of history
```

### Tag Manipulation

Since state is immutable, mutations are queued for the next cycle.

```python
runner.patch(tags={...})             # One-shot: applied to next scan, then released
runner.force(tags={...})             # Context manager: overrides logic outputs
runner.add_force(tag, value)         # Manual force toggle (for UI)
runner.remove_force(tag)             # Release manual force
```

**Example:**
```python
# Momentary button press
runner.patch({'Start_Button': True})
runner.step()
runner.patch({'Start_Button': False})

# Forcing a sensor for test block (also used for watchdog suppression)
with runner.force({'Safety_Guard': True, 'Watchdog_OK': True}):
    runner.run(cycles=100)
```

### Debug (Breakpoints & Observers)

```python
runner.when(predicate).pause()              # Halt when condition met
runner.when(predicate).snapshot(label)      # Bookmark history when condition met
runner.monitor(tag, callback)               # Fire callback(curr, prev) on change
runner.inspect(rung_id)                     # Return live object data for a rung
```

**Example:**
```python
runner.when(lambda s: s.tags['Motor_Temp'] > 100).pause()
runner.monitor('Valve_1', lambda c, p: print(f'Valve: {p} -> {c}'))
```

### Time Travel (History Buffer)

```python
runner.history.at(scan_id)           # Retrieve specific snapshot
runner.history.range(start, end)     # Retrieve slice of snapshots
runner.history.latest(n)             # Last N snapshots
runner.seek(scan_id)                 # Move playhead for inspection (read-only)
runner.rewind(seconds=N)             # Jump back N seconds of simulation time
runner.diff(scan_a, scan_b)          # Returns dict of changed tags with (old, new) values
runner.fork_from(scan_id)            # Branch execution - returns new runner instance
```

**Note:** `seek` and `rewind` are for inspection only. Calling `step()` always appends to the end of history.

**Example:**
```python
# Debugging a fault
runner.rewind(seconds=5)
state = runner.history.at(runner.playhead)
print(runner.diff(runner.playhead, runner.playhead + 10))

# Branching to test "what if"
alt_runner = runner.fork_from(scan_id=50)
alt_runner.patch({'X': True})
alt_runner.run(cycles=10)
```

---

## DSL Syntax

### Tag Types (IEC 61131-3)

Tags carry type metadata but no state. Values live only in `SystemState.tags`.

pyrung uses IEC 61131-3 standard type names as the canonical `TagType` enum. Click-familiar aliases are available for convenience:

| IEC (Primary) | Click (Alias) | `TagType` | Size | Description |
|---------------|---------------|-----------|------|-------------|
| `Bool` | `Bit` | `BOOL` | 1 bit | Boolean |
| `Int` | — | `INT` | 16-bit signed | -32768 to 32767 |
| `Dint` | `Int2` | `DINT` | 32-bit signed | -2,147,483,648 to 2,147,483,647 |
| `Real` | `Float` | `REAL` | 32-bit | IEEE 754 |
| `Word` | `Hex` | `WORD` | 16-bit unsigned | 0x0000 to 0xFFFF |
| `Char` | `Txt` | `CHAR` | 8-bit | ASCII character |

```python
from pyrung.click import Bool, Int, Dint, Real, Word, Char
# Click aliases also available: Bit, Int, Int2, Float, Hex, Txt

Button = Bool("Button")
Light = Bool("Light")
Step = Int("Step")
Temperature = Real("Temperature")
AutoMode = Bool("AutoMode", retentive=True)
```

Tag names are validated against `clickplc.validation` rules at map time: max 24 characters, no forbidden characters (`%"<>!#$&'()*+-./:;=?@[\]^{|}~`), not a reserved word (`log`, `sin`, `mod`, `and`, `or`, etc.).

### Memory Banks

MemoryBank provides typed, range-validated tag factories for groups of related tags.

When a type is specified, it is used directly. When omitted, the type (and retentive default) is inferred from the hardware bank at map time. Explicit values always override inference.

```python
from pyrung.click import MemoryBank
from clickplc.banks import DataType

# Explicit type (always works, required when not mapping to hardware)
Setpoints = MemoryBank("Setpoints", DataType.INT, range(1, 51), retentive=True)
Accumulators = MemoryBank("Accumulators", DataType.INT2, range(1, 101))

# Type inferred from hardware bank at map time
Alarms = MemoryBank("Alarms", range(1, 100))
Alarms.map_to(c[101:200])       # → DataType.BIT, retentive=False (from c bank)

# Explicit override of inferred defaults
Setpoints = MemoryBank("Setpoints", range(1, 51), retentive=True)
Setpoints.map_to(ds[201:250])   # → DataType.INT inferred, retentive=True kept

# Single tag access
MaxTemp = Setpoints[1]            # Tag("MaxTemp", INT, retentive=True)

# Named attribute access
Setpoints.Max_Temp                # Equivalent to Setpoints["Max Temp"]
Setpoints["Name with Spaces"]    # For names with spaces

# Block access for block operations
block = Setpoints[10:20]          # MemoryBlock for blockcopy, fill

# Pointer/indirect addressing
Index = Int("Index")
value = Accumulators[Index]       # IndirectTag - resolved at runtime
```

**Constructing from pyclickplc metadata:**

When loading from a nickname file, `MemoryBankMeta` provides the metadata to construct a bank:

```python
from clickplc.blocks import MemoryBankMeta

meta = MemoryBankMeta(
    name="Alarms",
    memory_type="C",
    start_address=101,
    end_address=200,
    data_type=DataType.BIT,
    retentive=False,
    bg_color="Red",
    paired_bank=None,
)

bank = MemoryBank.from_meta(meta)
# Equivalent to:
# Alarms = MemoryBank("Alarms", DataType.BIT, range(1, 101), retentive=False)
# Alarms.map_to(c[101:200])
```

**Pre-built Click banks** are available for convenience. These are constructed from `clickplc.banks.ADDRESS_RANGES` and match Click hardware address ranges:

```python
from pyrung.click import x, y, c, ds, dd, dh, df, t, td, ct, ctd, sc, sd, txt

# These are equivalent to:
# x  = MemoryBank("X",  TagType.BOOL, range(1, 817),  retentive=False)
# ds = MemoryBank("DS", TagType.INT,  range(1, 4501), retentive=True)
# c  = MemoryBank("C",  TagType.BOOL, range(1, 2001), retentive=False)
# df = MemoryBank("DF", TagType.REAL, range(1, 501),  retentive=True)
# etc. — types translated from clickplc.banks DataType to TagType
```

You can use pre-built banks directly, or define your own semantic banks and map them to hardware later (see Tag Mapping & Validation).

### Program Structure

```python
from pyrung.click import Program, Rung, branch, subroutine, call
from pyrung.click import out, latch, reset, copy, nc, rise, fall

with Program() as logic:
    with Rung(Button):
        out(Light)

    with Rung(Step == 0):
        out(Light)
        with branch(AutoMode):
            copy(1, Step, oneshot=True)
        call("my_sub")

    with subroutine("my_sub"):
        with Rung(Step == 1):
            out(SubLight)

runner = PLCRunner(logic)
```

### Conditions

```python
# Basic conditions
with Rung(Button):                    # Normally open
with Rung(nc(Button)):                # Normally closed
with Rung(rise(Button)):              # Rising edge
with Rung(fall(Button)):              # Falling edge

# Multiple conditions (AND)
with Rung(Button, nc(Fault)):         # All must be true

# AnyCondition (OR) - two syntaxes
with Rung(AutoMode, any_of(Start, oCmdStart)):   # any_of() function
with Rung(Button | OtherButton):                  # Pipe operator

# Comparisons
with Rung(Step == 0):
with Rung(Temperature >= 100.0):

# Inline expressions (Python-native, validator flags for hardware export)
with Rung((PressureA + PressureB) > 100):
with Rung((Temperature * 1.8 + 32) > 212):
```

### Instructions

```python
out(tag)                              # Energize output
latch(tag)                            # Set and hold
reset(tag)                            # Clear latch
copy(source, dest, oneshot=False)     # Copy value
```

**Immediate I/O (mid-scan physical read/write):**

Physical I/O (x/y banks) is normally read at scan start and written at scan end. Use `.immediate` to access physical pins mid-scan.

```python
# Input - read physical pin NOW instead of scan snapshot
Estop = x[1]
with Rung(Estop.immediate):
with Rung(nc(Estop.immediate)):

# Output - write physical pin NOW instead of waiting for scan end
Pulse = y[1]
out(Pulse.immediate)
latch(Pulse.immediate)
reset(Pulse.immediate)
```

The validator checks that `.immediate` is only used on x/y banks.

---

## Timers & Counters

Timers and counters use a two-bank model: a done bit and a separate accumulator. You can use standalone tags, user-defined banks, or the pre-built Click banks — the instructions are identical regardless.

### Counters

A counter uses a done bit (Bit) and an accumulator (Int2). The done bit energizes when the accumulator reaches the setpoint.

**Tag declaration — any of these work:**

```python
# Standalone tags
PartsDone = Bool("PartsDone")
PartsAcc = Dint("PartsAcc")

# User-defined banks
MyCT = MemoryBank("MyCT", DataType.BIT, range(1, 251))
MyCTD = MemoryBank("MyCTD", DataType.INT2, range(1, 251))
PartsDone = MyCT[1]
PartsAcc = MyCTD[1]

# Pre-built Click banks (same thing, pre-configured)
from pyrung.click import ct, ctd
PartsDone = ct[1]
PartsAcc = ctd[1]
```

**Instructions:**

```python
# Count up with required reset
with Rung(rise(PartSensor)):
    count_up(PartsDone, acc=PartsAcc, setpoint=100).reset(ResetButton)

# Bidirectional counting
with Rung(rise(PartEnter)):
    count_up(ZoneDone, acc=ZoneAcc, setpoint=50) \
        .down(rise(PartExit)) \
        .reset(ResetZone)

# Count down
with Rung(rise(DispenseSignal)):
    count_down(RemainingDone, acc=RemainingAcc, setpoint=25).reset(ReloadCmd)

# Use done bit as condition
with Rung(PartsDone):
    out(BatchComplete)

# Compare accumulator
with Rung(PartsAcc >= 50):
    out(HalfwayThere)
```

### Timers

A timer uses a done bit (Bit) and an accumulator (Int). The done bit energizes when the accumulator reaches the setpoint.

**Tag declaration — same options as counters:**

```python
PumpDone = Bool("PumpDone")
PumpAcc = Int("PumpAcc")

# Or with pre-built banks
from pyrung.click import t, td
PumpDone = t[1]
PumpAcc = td[1]
```

**Instructions:**

```python
from pyrung.click import TimeUnit

# TON - auto-resets when rung goes false
with Rung(MotorRunning):
    on_delay(PumpDone, acc=PumpAcc, setpoint=5, time_unit=TimeUnit.Ts)

# RTON - retentive, manual reset required
with Rung(MotorRunning):
    on_delay(PumpDone, acc=PumpAcc, setpoint=90, time_unit=TimeUnit.Ts) \
        .reset(ResetPump)

# TOF - output stays ON until setpoint after rung goes false
with Rung(MotorCommand):
    off_delay(CoastDone, acc=CoastAcc, setpoint=10, time_unit=TimeUnit.Ts)

# Use done bit as condition
with Rung(PumpDone):
    out(PumpReady)

# Compare accumulator (always stored as ms internally)
with Rung(PumpAcc >= 4500):
    out(AlmostDone)
```

**Time units:** `Tms` (milliseconds, default), `Ts` (seconds), `Tm` (minutes), `Th` (hours), `Td` (days). The `time_unit` controls how the setpoint value is interpreted. Internally the accumulator always counts in milliseconds.

---

## Extended Instructions

### Math

Click overloads standard Python operators for Tag objects, so you can write expressions directly in conditions or as instruction arguments:

```python
# Inline expressions in conditions
with Rung((PressureA + PressureB) > 100):
with Rung((Temperature * 1.8 + 32) > 212):
with Rung((BatchCount % 10) == 0):

# Inline expressions in copy source
copy(Speed * 2 + Offset, Result)
```

The `calc()` instruction stores a result in a destination register. This is also the form that Click hardware supports natively — the validator will flag inline expressions and suggest `calc()` rewrites.

```python
calc(PressureA + PressureB * 10, TotalPressure, oneshot=False)

# Click hardware also distinguishes decimal vs hex mode
calc(PressureA + PressureB * 10, TotalPressure, mode="decimal")
calc(MaskA & MaskB, MaskResult, mode="hex")
```

`math_obj.to_formula()` converts the expression to Click Formula Pad format.

**Validator hint for inline expressions:**
```
Rung 12: with Rung((PressureA + PressureB) > 100):
  → Inline expression in condition. Click hardware requires
    arithmetic as a separate instruction step.
  Hint:
    calc(PressureA + PressureB, temp)
    with Rung(temp > 100):
```

### Copy with Type Conversion (Txt ↔ Numeric)

```python
# Txt → Numeric
copy(InputChar.as_value(), ResultInt)      # '5' → 5 (numeric value)
copy(InputChar.as_ascii(), ResultInt)      # '5' → 53 (ASCII code)

# Numeric → Txt
copy(SourceInt.as_text(), DestChar)            # 123 → "123" (suppress zero)
copy(SourceInt.as_text(pad=5), DestChar)       # 123 → "00123" (padded)
copy(SourceInt.as_binary(), DestChar)          # 123 → "{" (raw byte)
copy(SourceReal.as_text(exponential=True), DestChar)  # 10000 → "1.0E+04"
```

### Copy & Block Operations

**Single Copy:**

```python
copy(source, dest, oneshot=False)
```

Most flexible — supports pointer addressing. The validator checks that pointers use ds bank and appear only in `copy()`.

**Block Copy:**

```python
blockcopy(source_block, dest_start, oneshot=False)
```

Copies a contiguous range of same-type registers. The destination length is inferred from the source block. No pointer addressing — source and destination must be fixed slices.

```python
# Load first recipe into active buffer
Recipes = MemoryBank("Recipes", DataType.INT, range(1, 51))
ActiveParams = MemoryBank("ActiveParams", DataType.INT, range(1, 6))
blockcopy(Recipes[1:6], ActiveParams[1])
```

**Fill:**

```python
fill(value, dest_block, oneshot=False)
```

Writes a constant value to every register in a range.

```python
fill(0, ActiveParams[1:6])          # Clear active parameters
fill(0, Setpoints[1:50])            # Zero out all setpoints
```

**Dynamic block bounds:**

pyrung allows computed slice bounds at runtime — the engine resolves them each scan. The validator flags these since Click hardware can't do dynamic block addressing, and suggests a loop-based alternative.

```python
# Load recipe N (5 params starting at (N-1)*5 + 1)
calc((RecipeNumber - 1) * 5 + 1, StartIdx)
blockcopy(Recipes[StartIdx : StartIdx + 5], ActiveParams[1])
```

```
Validator hint:
  Rung 5: blockcopy(Recipes[StartIdx : StartIdx + 5], ActiveParams[1])
    → Dynamic block bounds not allowed on Click hardware.
    Recipes maps to ds[301:350], offset +300.
    Hint: Use a loop with single copy:
      with loop(count=5):
          calc(StartIdx + 300 + loop.idx, ptr)
          copy(ds[ptr], ActiveParams[loop.idx + 1])
```

**Pack (many → one):**

```python
# Bits → Word/Dword (width inferred from dest type)
StatusBits = MemoryBank("StatusBits", DataType.BIT, range(1, 17))
StatusWord = Int("StatusWord")
AlarmBits = MemoryBank("AlarmBits", DataType.BIT, range(1, 33))
AlarmDword = Dint("AlarmDword")

pack_bits(StatusBits[1:17], StatusWord)     # 16 Bit → Int
pack_bits(AlarmBits[1:33], AlarmDword)      # 32 Bit → Int2

# Words → Dword
PositionHi = Int("PositionHi")
PositionLo = Int("PositionLo")
Position = Dint("Position")
pack_words(PositionHi, PositionLo, Position)   # 2 Int → Int2

# Txt → Numeric (use copy with .as_value()/.as_ascii() instead)
```

**Unpack (one → many):**

```python
# Word/Dword → Bits (width inferred from source type)
unpack_to_bits(StatusWord, StatusBits[1:17])     # Int → 16 Bit
unpack_to_bits(AlarmDword, AlarmBits[1:33])      # Int2 → 32 Bit

# Dword → Words
unpack_to_words(Position, PositionHi, PositionLo)   # Int2 → 2 Int
```

The validator checks that mapped hardware types and sizes are compatible:

```python
# After mapping:
# StatusBits.map_to(c[201:217])
# StatusWord.map_to(ds[50])
# Validator: ✓ 16 Bit → Int, sizes match
```

### For Loop

Execute instructions multiple times within a single scan.

```python
# For-Next loop with count (constant or tag)
with Rung(EnableLoop):
    with loop(count=10, oneshot=False):
        # Instructions execute 10 times per scan
        copy(SourceTable[loop.idx], DestTable[loop.idx])

# Using a tag for dynamic count
with Rung(EnableLoop):
    with loop(count=LoopCount, oneshot=True):  # Oneshot: all loops once per rising edge
        ...
```

**Note:** Nested For-Next loops are NOT permitted.

### Search

Search for a value within a range of registers.

```python
Readings = MemoryBank("Readings", DataType.INT, range(1, 101))
SearchResult = Int("SearchResult")
SearchFound = Bool("SearchFound")

# Search with condition, range, result, and flag
with Rung(EnableSearch):
    search(
        condition=">",           # "=", "!=", "<", "<=", ">", ">="
        value=100,               # Value to compare against (constant or tag)
        start=Readings[1],       # Starting address
        end=Readings[100],       # Ending address
        result=SearchResult,     # Result address (-1 if not found)
        found=SearchFound,       # Flag bit (ON if found)
        continuous=False,        # Continue from last found position
        oneshot=False
    )

# Text search
with Rung(EnableSearch):
    search(
        condition="=",
        value="A",             # Text to find
        start=Messages[1],
        end=Messages[100],
        result=ResultIdx,
        found=FoundFlag
    )
```

### Shift Register

Shift a range of control bits with each clock pulse.

```python
ConveyorStages = MemoryBank("ConveyorStages", DataType.BIT, range(1, 9))

# Shift register - shifts on clock rising edge
with Rung(DataBit):                                 # Data input
    shift_register(start=ConveyorStages[1], end=ConveyorStages[8]) \
        .clock(ClockBit) \                           # Shift on rising edge
        .reset(ResetBit)                             # Clears all bits
```

Direction is determined by address order:
- `start < end`: Shifts from start toward end (right/up)
- `start > end`: Shifts from end toward start (left/down)

---

## Execution Behavior

### Memory & Timing

| Behavior | Description |
|----------|-------------|
| Instruction visibility | All memory updates are immediately visible to subsequent instructions in the same scan |
| Input reading | Physical inputs (x) read at scan start (unless `.immediate`) |
| Output writing | Physical outputs (y) written at scan end (unless `.immediate`) |
| Timer accumulator | Updates immediately when instruction executes. First enable with 2ms scan → acc = 2ms for next instruction |

### Numeric Handling

| Context | Behavior |
|---------|----------|
| Timer accumulator | Clamps at max value (no overflow) |
| Counter accumulator | Clamps at min/max value (no overflow) |
| Copy operations | Preserves sign, range limit the value to the datatype of the Destination |
| Math operations | Wraps arithemic, otherwise clamps (verify) |

---

## Tag Mapping & Validation

### Overview

pyrung lets you write logic with semantic tag names and full Python expressiveness. When you're ready to target Click hardware, you map tags to hardware addresses and run the validator. The validator checks compatibility and produces actionable hints. You iterate until the report is clean.

1. **Write and test** — Semantic tags, inline expressions, pointer arithmetic anywhere. Everything runs in simulation.
2. **Map to hardware** — Bind tags to Click addresses. Type, retentive, and range checks run.
3. **Validate** — The validator walks every rung and flags anything Click hardware can't do, with concrete rewrite suggestions that use your actual mapped addresses.
4. **Iterate** — Fix what the report flags. Run again. Repeat until `report.exportable == True`.

### Mapping Tags to Hardware

```python
from pyrung.click import TagMap, x, y, c, ds, dd, df, t, td, ct, ctd

mapping = TagMap({
    # Standalone tags → specific addresses
    Valve:       c[1],
    MotorRun:    c[2],
    BatchCount:  ds[100],
    Temperature: df[1],

    # MemoryBank → hardware slice (type inferred from bank)
    Alarms:      c[101:200],
    Setpoints:   ds[201:250],

    # Timer/counter pairs
    PumpDone:    t[1],
    PumpAcc:     td[1],
    PartsDone:   ct[1],
    PartsAcc:    ctd[1],
})
```

**Alternative: method-call syntax for incremental binding:**

```python
Valve.map_to(c[1])
Alarms.map_to(c[101:200])
PumpDone.map_to(t[1])
PumpAcc.map_to(td[1])
```

### Loading from Nickname Files

Nickname CSV files (exported from CLICK software or authored in ClickNick) can reconstruct a full project with tag mappings and memory banks. pyrung uses `pyclickplc` to parse the file, extract block tags, and discover bank structure.

**Block tag convention:** ClickNick uses XML-style tags in the comment field to define memory banks:

```csv
Address,Data Type,Nickname,Initial Value,Retentive,Address Comment
C101,BIT,"Alm1",0,No,"<Alarms bg=""Red"">"
C102,BIT,"Alm2",0,No,""
...
C200,BIT,"Alm100",0,No,"</Alarms>"
```

The `<Alarms>` / `</Alarms>` block tag pair defines a memory bank named "Alarms" spanning C101–C200.

**Timer/counter pair convention:** Timer and counter banks use a `_D` suffix on the accumulator bank. When ClickNick creates a block `<PumpTimers>` on T rows, it auto-creates `<PumpTimers_D>` on the corresponding TD rows. pyrung recognizes this suffix convention to associate done-bit and accumulator banks:

```csv
T1,BIT,"PumpDone",0,No,"<PumpTimers>"
T2,BIT,"FanDone",0,No,""
T3,BIT,"",0,No,"</PumpTimers>"
TD1,INT,"PumpAcc",0,No,"<PumpTimers_D>"
TD2,INT,"FanAcc",0,No,""
TD3,INT,"",0,No,"</PumpTimers_D>"
```

Block names must be unique across the entire file.

**Loading:**

```python
from clickplc.nicknames import load_nickname_file

project = load_nickname_file("my_project.csv")
project.records      # dict[int, AddressRecord] — all rows by addr_key
project.banks        # dict[str, MemoryBankMeta] — discovered from block tags
project.tags         # dict[str, AddressRecord] — nicknamed rows not in any block
```

**Building a TagMap from a nickname file:**

```python
mapping = TagMap.from_nickname_file("my_project.csv")
```

This reconstructs MemoryBanks from `MemoryBankMeta`, creates Tags for standalone nicknames, and wires up all hardware address mappings. Timer/counter `_D` pairs are linked automatically.

Equivalent to:

```python
project = load_nickname_file("my_project.csv")

# Banks from block tags
Alarms = MemoryBank.from_meta(project.banks["Alarms"])
# → MemoryBank("Alarms", DataType.BIT, range(1, 100), retentive=False)
# → auto-mapped to c[101:200]

PumpTimers = MemoryBank.from_meta(project.banks["PumpTimers"])
PumpTimers_D = MemoryBank.from_meta(project.banks["PumpTimers_D"])
# → paired: PumpTimers.paired_bank == PumpTimers_D

# Standalone tags (nicknamed addresses not in any block)
Valve = Bool("Valve")  # from project.tags["Valve"]
# → auto-mapped to its CSV address
```

**Nickname validation:** Tag names loaded from CSV are validated against `clickplc.validation` rules. Invalid nicknames are reported as warnings (the project still loads, but `report.exportable` will be `False` until fixed).

### Exporting to Nickname Files

Round-trip support: generate a nickname CSV from a TagMap + Program for import into ClickNick or CLICK software.

```python
mapping.to_nickname_file("exported_project.csv")
```

MemoryBanks emit block tags (`<n>` / `</n>` pairs). Standalone mapped tags emit individual rows. Unmapped tags are omitted.

### Running the Validator

```python
report = logic.validate(mapping)
print(report)
```

The report has four sections:

**1. Mapping Status**

```
=== Validation Report ===

Mapped (12 of 15):
  ✓ Valve → C[1]                   (Bit, retentive: ok)
  ✓ Alarms[1:99] → C[101:199]     (Bit, offset: +100)
  ✓ BatchCount → DS[100]           (Int, retentive: ok)
  ✓ PumpDone → T[1], PumpAcc → TD[1]   (Timer pair)

Unmapped (3):
  ⚠ TempReading (Float) — needs address assignment
  ⚠ DebugFlag (Bit) — needs address assignment
  ⚠ StepNumber (Int) — needs address assignment
```

**2. Mapping Errors & Warnings**

```
Errors:
  ✗ BatchCount declared as Int but mapped to DD[5] (Int2)
    → Type mismatch. Change tag to Dint("BatchCount") or map to a DS address.

Warnings:
  ⚠ Temperature declared retentive=True but DF[1] is non-retentive
    Hint: DF[1:500] are non-retentive. Use DF[501:1000] for retentive,
    or remove retentive=True from the tag declaration.
```

**3. Hardware Compatibility Hints**

Generated by walking every rung and checking each instruction against Click hardware restrictions, using the now-known addresses and offsets:

```
Hardware Hints:

  Rung 7, line 34: copy(Alarms[idx + 1], dest)
    → Pointer arithmetic not allowed in copy() on Click hardware.
    Your idx (logical 1–99) maps to C[101:199], offset +100.
    Hint: calc(idx + 101, ptr)
          copy(C[ptr], dest)

  Rung 12, line 58: with Rung((PressureA + PressureB) > 100):
    → Inline expression in condition. Click hardware requires
      arithmetic as a separate instruction step.
    Hint: calc(PressureA + PressureB, temp)
          with Rung(temp > 100):

  Rung 15, line 72: with Rung(BigValue[ptr] > 100):
    → Pointer not supported in comparisons on Click hardware.
    BigValue maps to DD[1:100].
    Hint: copy(BigValue[ptr], temp)
          with Rung(temp > 100):
```

**4. Summary**

```
Summary:
  3 unmapped tags (program will run but cannot export)
  1 type error (must fix)
  1 retentive warning (logic will work, but data may be lost on power cycle)
  3 hardware hints (refactor for Click hardware compatibility)
```

### Hardware Restriction Reference

The validator checks these restrictions. Your program runs fine without satisfying them — they only matter for hardware export.

**Pointer restrictions:**

| Context | Allowed | Hardware restriction |
|---------|---------|---------------------|
| Pointer in copy | Any bank, arithmetic ok | ds only, no arithmetic |
| Pointer in blockcopy | Not allowed | Not allowed |
| Pointer in comparison | Any bank, arithmetic ok | Not allowed |

**Expression restrictions:**

| Context | Allowed | Hardware restriction |
|---------|---------|---------------------|
| Inline in condition | `(A + B) > 100` | Must use `calc()` first |
| Inline in copy source | `copy(A * 2, dest)` | Must use `calc()` first |
| calc() with dest | `calc(A + B, dest)` | `calc(A + B, dest)` (same) |

**Bank compatibility:** The validator checks which memory banks can be used together — both in comparisons (which banks can be compared to each other) and in instruction arguments (valid source/dest combinations for `blockcopy`, `pack`, `fill`, etc.).

### TagMap API

```python
class TagMap:
    def __init__(self, mapping: dict = None):
        """Create from dict of {tag_or_bank: hardware_address_or_slice}"""

    @classmethod
    def from_nickname_file(cls, path: str) -> "TagMap":
        """Load from Click nickname CSV.

        Uses clickplc.nicknames.load_nickname_file() to parse CSV,
        extract block tags into MemoryBankMeta, and reconstruct
        MemoryBanks, Tags, and hardware mappings.

        Timer/counter _D suffix pairs are linked automatically.
        """

    @classmethod
    def from_dict(cls, d: dict) -> "TagMap":
        """Same as __init__, explicit alternative"""

    def to_nickname_file(self, path: str) -> None:
        """Export to Click nickname CSV.

        MemoryBanks emit block tag pairs. Standalone tags emit rows.
        Unmapped tags are omitted.
        """

    def resolve(self, logical_ref) -> HardwareAddress:
        """Resolve a logical tag/index to its hardware address"""

    def offset_for(self, bank: MemoryBank) -> int:
        """Return the offset between logical and hardware addressing"""

    def unmapped(self) -> list:
        """Return list of tags/banks not yet mapped"""

    def validate(self) -> ValidationReport:
        """Run type, retentive, range, and hardware restriction checks."""
```

```python
class ValidationReport:
    mapped: list        # Successfully mapped tags with details
    unmapped: list      # Tags still needing addresses
    errors: list        # Type mismatches, range overflows (must fix)
    warnings: list      # Retentive mismatches, bank compatibility (should fix)
    hints: list         # Hardware compatibility suggestions (refactor for export)

    @property
    def exportable(self) -> bool:
        """True if no errors and no unmapped tags"""

    def __str__(self) -> str:
        """Pretty-print the full report"""

    def summary(self) -> str:
        """One-line summary: '3 unmapped, 1 error, 2 warnings, 5 hints'"""
```

---

## Example Test Scenarios

**Unit test with deterministic timing:**
```python
runner = PLCRunner(logic, initial_state)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

runner.patch({'Start_Button': True})
runner.run(cycles=50)  # 5 seconds of simulated time

assert runner.current_state.tags['Motor_Running'] == True
```

**Testing a fault condition:**
```python
runner.when(lambda s: s.tags['Fault']).pause()
runner.run(cycles=1000)

if runner.current_state.tags['Fault']:
    runner.rewind(seconds=2)
    for _ in range(20):
        print(runner.diff(runner.playhead, runner.playhead + 1))
        runner.seek(runner.playhead + 1)
```

**Branching to test "what if":**
```python
alt_runner = runner.fork_from(scan_id=50)
alt_runner.patch({'X': True})
alt_runner.run(cycles=10)
```

**Load from nickname file, simulate, validate:**
```python
# Load a ClickNick project
mapping = TagMap.from_nickname_file("conveyor_project.csv")

# Banks and tags are reconstructed from the CSV
Alarms = mapping.banks["Alarms"]       # MemoryBank from block tags
PumpDone = mapping.tags["PumpDone"]    # Tag from standalone nickname

# Write logic using the loaded tags
with Program() as logic:
    with Rung(PumpDone):
        latch(Alarms[1])

# Simulate
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
runner.patch({'PumpDone': True})
runner.run(cycles=10)
assert runner.current_state.tags['Alm1'] == True

# Validate for hardware export
report = logic.validate(mapping)
print(report)
# Already mapped from CSV — report shows hardware compatibility hints only
```

**Write from scratch, map, validate, export:**
```python
# Write with semantic tags
Valve = Bool("Valve")
Motor = Bool("Motor")
Alarms = MemoryBank("Alarms", range(1, 100))

with Program() as logic:
    with Rung(Valve):
        out(Motor)
    with Rung(Alarms[idx + 1]):
        latch(Alarms[99])

# Test it
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
runner.patch({'Valve': True})
runner.run(cycles=10)
assert runner.current_state.tags['Motor'] == True

# Map to hardware (Alarms type inferred from c bank)
mapping = TagMap({
    Valve:  c[1],
    Motor:  y[1],
    Alarms: c[101:200],
})

# Validate
report = logic.validate(mapping)
print(report)
# Mapped (3 of 3): ✓
# Errors: 0
# Warnings: 0
# Hints:
#   Rung 2: Alarms[idx + 1] → pointer arithmetic
#   Alarms maps to C[101:199], offset +100
#   Hint: calc(idx + 101, ptr); copy(C[ptr], ...)

# Fix the hint, validate again, done.

# Export for ClickNick / CLICK software
mapping.to_nickname_file("my_project.csv")
# Alarms bank emits <Alarms> / </Alarms> block tags
# Valve, Motor emit standalone rows
```

---

## Implementation Status

| Milestone | Status | Features |
|-----------|--------|----------|
| 1: Core Engine | ✅ | SystemState, PLCRunner, TimeMode, step/run/patch |
| 2: Basic Logic | ✅ | Tags, Conditions, Instructions, Rung, Program |
| 3: Program Structure | ✅ | branch(), subroutine(), call() |
| 4: Counters | ✅ | count_up, count_down, bidirectional (two-bank model) |
| 5: Timers | ✅ | on_delay (TON/RTON), off_delay (TOF), TimeUnit (two-bank model) |
| 6: MemoryBank | ✅ | Typed banks, pointer addressing, blocks |
| 7: Copy & Math | 🔲 | copy, blockcopy (static + dynamic bounds), fill, pack_bits, pack_words, unpack_to_bits, unpack_to_words, calc, .immediate |
| 8: Loop, Search, Shift | 🔲 | loop (for-next), search, shift_register |
| 9: pyclickplc | 🔲 | Extract shared hardware model from ClickNick (see transition plan) |
| 10: Tag Mapping | 🔲 | TagMap, map_to(), from_nickname_file(), to_nickname_file(), ValidationReport |
| 11: Advanced Features | 🔲 | force(), history, debug breakpoints, fork_from() |

---

## Future Ideas (Not for Current Scope)

- `delta()` instruction for any-value-change detection
- Array types (FIFO, LIFO, circular buffer, shift register)
- Auto-mapping suggestions (assign unmapped tags to available hardware ranges)

