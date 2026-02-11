# pyrung: Architecture Overview

## What pyrung Is

pyrung is a Pythonic PLC simulation engine. You write ladder logic as Python, simulate it with full time-travel debugging, and optionally target real hardware through dialect modules.

The core is hardware-agnostic. Hardware knowledge lives in dialects.

---

## Core Philosophy

1. **Redux Architecture:** Logic is a pure function. `Logic(Current_State) → Next_State`. State is never mutated in place.

2. **Generator Driver:** The engine yields control after every atomic step. The consumer (GUI, CLI, test runner) drives execution via `step()`.

3. **Time is a Variable:** Execution produces a stream of immutable snapshots. The consumer can pause, rewind, inspect any historical state, or fork a branch to explore "what if."

4. **Write First, Validate Later:** Write logic with semantic tags and native Python expressions. Map tags to hardware addresses when you're ready. Run the dialect's validator and iterate until it's clean.

---

## Layer Architecture

```
┌────────────────────────────────────────────────────────┐
│                   User Program                         │
│  from pyrung import *                                  │
│  from pyrung.click import x, y, c, ds, TagMap          │
│  # or: from pyrung.circuitpy import P1AM, ...          │
├────────────────────────────────────────────────────────┤
│                   pyrung (core)                        │
│                                                        │
│  DSL         Program, Rung, branch, subroutine,        │
│              conditions (nc, rise, fall, any_of,        │
│              all_of, |, &)                              │
│                                                        │
│  Types       Tag, InputTag, OutputTag                  │
│              Block, InputBlock, OutputBlock            │
│              Bool, Int, Dint, Real, Word, Char         │
│              TagType (IEC 61131-3 enum)                │
│                                                        │
│  Instructions  out, latch, reset, copy, blockcopy,     │
│                fill, math, pack/unpack,                │
│                on_delay, off_delay, count_up/down,     │
│                search, shift_register, loop            │
│                                                        │
│  Engine      SystemState, PLCRunner, TimeMode          │
│              step, run, patch                          │
│                                                        │
│  Debug       force, when().pause(), monitor(),         │
│              history, seek, rewind, diff, fork_from    │
│                                                        │
│  Validation  ValidationReport (base structure)         │
│              Validator (base class / protocol)         │
├────────────────────────────────────────────────────────┤
│              Dialects (hardware-specific)              │
│                                                        │
│  pyrung.click                pyrung.circuitpy          │
│  ─────────────               ──────────────            │
│  Pre-built blocks:           P1AM hardware model       │
│    x, y, c, ds, df,         Slot/channel addressing    │
│    t, td, ct, ctd, etc.     Module catalog             │
│  Click aliases:              Code generation:          │
│    Bit, Float, Hex, etc.      generate_arduino()       │
│  TagMap (mapping)              generate_micropython()  │
│  Nickname CSV I/O      CircuitPython validation rules  │
│  Click validation rules                                │
│  Depends on clickplc-config                            │
└────────────────────────────────────────────────────────┘
```

---

## Import Conventions

The recommended pattern is `from pyrung import *` for the core DSL, plus explicit imports from your target dialect.

`pyrung.__init__` exports a curated `__all__` containing: all DSL constructs, all IEC type constructors, all instructions, all conditions, the engine classes, and the debug API. This is intentional — pyrung is a DSL, and `import *` is the idiomatic way to use a DSL.

Dialects export only their hardware-specific symbols. They do **not** re-export core.

### Click program

```python
from pyrung import *
from pyrung.click import x, y, c, ds, df, t, td, ct, ctd, TagMap

Button = Bool("Button")
Light = Bool("Light")

with Program() as logic:
    with Rung(Button):
        out(Light)

runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
runner.patch({"Button": True})
runner.step()
```

### CircuitPython program (same core DSL, different hardware)

```python
from pyrung import *
from pyrung.circuitpy import P1AM, generate_arduino

hw = P1AM()
inputs  = hw.slot(1, "P1-08SIM")    # → InputBlock("Slot1", Bool, range(1, 9))
outputs = hw.slot(2, "P1-08TRS")    # → OutputBlock("Slot2", Bool, range(1, 9))

Button = inputs[1]                    # InputTag
Light  = outputs[1]                   # OutputTag

with Program() as logic:
    with Rung(Button):
        out(Light)

# Simulate — identical API
runner = PLCRunner(logic)
runner.patch({Button.name: True})
runner.step()

# Generate deployable code
generate...
```

### Explicit imports (for those who prefer them)

```python
from pyrung import Program, Rung, Bool, Int, out, latch, nc, rise
from pyrung import PLCRunner, TimeMode
from pyrung.click import x, y, c, ds, TagMap
```

Both patterns work. `__all__` is the convenience layer.

### Porting between dialects

Only the dialect import line and hardware setup change. All DSL, instructions, conditions, engine, and debug code stays identical.

---

## Type Hierarchy

### Tags (single values)

```
Tag                  ← Bool("X"), Int("X"), Block[n]
├── InputTag         ← InputBlock[n]
└── OutputTag        ← OutputBlock[n]
```

- **Tag** — Single named, typed value. Always internal memory. Created by IEC constructors (`Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`) or by indexing a `Block`.
- **InputTag** — Single physical input. Created only by indexing an `InputBlock`. Supports `.immediate`.
- **OutputTag** — Single physical output. Created only by indexing an `OutputBlock`. Supports `.immediate`.

Standalone tags (`Bool("Valve")`) are always plain `Tag`. Physical I/O always comes from a block.

### Blocks (1-indexed arrays of tags)

```
Block                ← internal memory array
├── InputBlock       ← physical input array
└── OutputBlock      ← physical output array
```

- **Block** — Named, typed, 1-indexed array of tags. Internal memory. `Block[0]` is always an `IndexError`.
- **InputBlock** — Physical inputs. Values read from external source at scan start. Elements are `InputTag`.
- **OutputBlock** — Physical outputs. Values written to external sink at scan end. Elements are `OutputTag`.

### IEC 61131-3 Types

pyrung uses IEC 61131-3 standard type names as the canonical `TagType` enum. Click-familiar aliases are available in the Click dialect.

| IEC (Primary) | Click (Alias) | `TagType` | Size | Description |
|---------------|---------------|-----------|------|-------------|
| `Bool` | `Bit` | `BOOL` | 1 bit | Boolean |
| `Int` | — | `INT` | 16-bit signed | -32768 to 32767 |
| `Dint` | `Int2` | `DINT` | 32-bit signed | ±2 billion |
| `Real` | `Float` | `REAL` | 32-bit | IEEE 754 |
| `Word` | `Hex` | `WORD` | 16-bit unsigned | 0x0000–0xFFFF |
| `Char` | `Txt` | `CHAR` | 8-bit | ASCII character |

---

## Scan Cycle Model

Every PLC scan follows this sequence:

```
1. READ INPUTS     InputBlock values copied from external source
2. EXECUTE LOGIC   Rungs evaluated top-to-bottom, left-to-right
3. WRITE OUTPUTS   OutputBlock values pushed to external sink
```

### In-scan visibility

All in-memory writes — including to OutputBlock tags — are visible to subsequent rungs **immediately within the same scan**. This is standard PLC behavior. "Output" refers to the direction toward physical hardware, not to read/write access within logic.

### .immediate

`.immediate` is an annotation on InputTag/OutputTag that means "interact with the physical layer right now, don't wait for the normal scan phase." It is only valid on tags from InputBlock or OutputBlock. Attempting `.immediate` on a plain Tag (from a Block) is a Python-time error — the attribute doesn't exist.

| Context | Behavior |
|---------|----------|
| Simulation (pure) | Validation-time check only. No runtime behavior change. |
| Click dialect | Transcription hint for Click software export. |
| CircuitPython dialect | Generates different code |
| Hardware-in-the-loop | Re-reads/writes the hardware adapter mid-scan. |

---

## Validation Framework

The validation framework lives in core. Validation **rules** live in dialects.

```python
class ValidationReport:
    mapped: list        # Successfully mapped tags with details
    unmapped: list      # Tags still needing addresses
    errors: list        # Type mismatches, range overflows (must fix)
    warnings: list      # Retentive mismatches (should fix)
    hints: list         # Hardware compatibility suggestions (refactor for export)

    @property
    def exportable(self) -> bool:
        """True if no errors and no unmapped tags."""

    def summary(self) -> str:
        """One-line: '3 unmapped, 1 error, 2 warnings, 5 hints'"""
```

Each dialect implements its own validator that produces a `ValidationReport`. The Click dialect checks Click-specific restrictions (pointer banks, expression limits, bank compatibility). The CircuitPython dialect checks slot/channel validity and codegen constraints.

The report structure is universal. The rules that populate it are dialect-specific.

---

## What Lives Where

| Symbol | Package | Rationale |
|--------|---------|-----------|
| `Program`, `Rung`, `branch`, `subroutine`, `call` | `pyrung` | Universal DSL structure |
| `out`, `latch`, `reset`, `copy`, `math`, `on_delay`, `count_up`, ... | `pyrung` | Universal instructions |
| `nc`, `rise`, `fall`, `any_of`, `all_of` | `pyrung` | Universal conditions |
| `Bool`, `Int`, `Dint`, `Real`, `Word`, `Char` | `pyrung` | IEC 61131-3 types |
| `Tag`, `InputTag`, `OutputTag` | `pyrung` | Core tag types |
| `Block`, `InputBlock`, `OutputBlock` | `pyrung` | Core block types |
| `TagType` | `pyrung` | IEC type enum |
| `PLCRunner`, `SystemState`, `TimeMode` | `pyrung` | Universal engine |
| `ValidationReport` | `pyrung` | Universal report structure |
| `x`, `y`, `c`, `ds`, `df`, `t`, `td`, `ct`, `ctd`, ... | `pyrung.click` | Click-specific pre-built blocks |
| `Bit`, `Float`, `Hex`, `Txt`, `Int2` | `pyrung.click` | Click convenience aliases |
| `TagMap` | `pyrung.click` | Click mapping + validation |
| Nickname CSV I/O | `pyrung.click` | Click ecosystem |
| Click validation rules | `pyrung.click` | Click hardware restrictions |
| `P1AM`, `Slot`, module catalog | `pyrung.circuitpy` | P1AM hardware model |
| `generate_circuitpython` | CircuitPython code generation |
| CircuitPython validation rules | `pyrung.circuitpy` | CircuitPython hardware restrictions |

---

## Dependency Graph

```
pyrung (core)
  └── pyrsistent          (immutable state)

pyrung.click (dialect)
  ├── pyrung              (core)
  └── clickplc-config          (shared Click hardware contants, Nickname (csv) and Dataview (.cdv) file handling.)

pyrung.circuitpy (dialect)
  └── pyrung              (core)

clickplc-config (standalone)
  └── (no pyrung dependency — shared by pyrung.click, ClickNick, etc.)
```

`clickplc-config` is the shared source of truth for Click hardware knowledge. It provides address ranges, data types, nickname file I/O, block tag parsing, and validation rules. pyrung.click depends on it; pyrung core does not.

---

## Spec File Index

| File | Contents | Status |
|------|----------|--------|
| `overview.md` | This file. Architecture, layers, conventions. | ✅ |
| `core/types.md` | Tag, InputTag, OutputTag, Block, InputBlock, OutputBlock, TagType, IEC constructors. | Handoff |
| `core/dsl.md` | Program, Rung, conditions, branch, subroutine, call. | Handoff |
| `core/instructions.md` | All instructions: out, latch, copy, math, timers, counters, etc. | Handoff |
| `core/engine.md` | SystemState, PLCRunner, TimeMode, step/run/patch, scan cycle. | Handoff |
| `core/debug.md` | force, when().pause(), monitor, history, seek, rewind, diff, fork_from. | Handoff |
| `dialects/click.md` | Pre-built blocks, TagMap, validation, nickname I/O, clickplc-config bridge. | Handoff |
| `dialects/circuit.md` | P1AM model, slot/channel, codegen, circuitpython validation. | Handoff |
