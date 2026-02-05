# Core Instructions — Handoff Brief

> **Status:** Handoff — decisions captured, needs full spec writeup.
> **Depends on:** `core/types.md` (Tag, Block, TagType), `core/dsl.md` (Rung context)
> **Referenced by:** `core/engine.md` (execution semantics), dialect specs (validation rules)

---

## Scope

Every instruction that can appear inside a `Rung`. This is the largest spec file — it covers basic I/O, copy/block operations, math, timers, counters, loops, search, and shift registers.

All instructions are hardware-agnostic. They operate on Tags and Blocks from core. Dialect validators check whether specific usages are compatible with their target hardware.

---

## Instruction Index

### Basic I/O

| Instruction | Signature | Description |
|-------------|-----------|-------------|
| `out` | `out(tag)` | Energize output. Follows rung power. |
| `latch` | `latch(tag)` | Set and hold (SET). |
| `reset` | `reset(tag)` | Clear latch (RESET). |

### Copy & Block Operations

| Instruction | Signature | Description |
|-------------|-----------|-------------|
| `copy` | `copy(source, dest, oneshot=False)` | Copy single value. Supports pointer addressing. |
| `blockcopy` | `blockcopy(source_block, dest_start, oneshot=False)` | Copy contiguous range. Dest length inferred from source. `source_block` is a `MemoryBlock` from `.select()`. |
| `fill` | `fill(value, dest_block, oneshot=False)` | Write constant to every element in range. `dest_block` is a `MemoryBlock` from `.select()`. |
| `pack_bits` | `pack_bits(bit_block, dest_word)` | Pack N bits into a Word or Dword. `bit_block` from `.select()`. |
| `pack_words` | `pack_words(hi, lo, dest_dword)` | Pack two Words into a Dword. |
| `unpack_to_bits` | `unpack_to_bits(source_word, bit_block)` | Unpack Word/Dword into individual bits. `bit_block` from `.select()`. |
| `unpack_to_words` | `unpack_to_words(source_dword, hi, lo)` | Unpack Dword into two Words. |

### Math

| Instruction | Signature | Description |
|-------------|-----------|-------------|
| `math` | `math(expression, dest, oneshot=False, mode="decimal")` | Evaluate expression, store result. |

Inline expressions in conditions and copy sources are Python-native. The `math()` instruction is the form that maps directly to Click hardware. Validators flag inline expressions with rewrite suggestions.

#### Math Overflow Behavior (Hardware-Verified)

**32-bit signed intermediates:** Expressions are evaluated in 32-bit signed registers with standard two's complement wrap. Truncation to destination width occurs on final store.

| Test | Result | Calculation |
|------|--------|-------------|
| 2,147,483,647 + 1 → DD | -2,147,483,648 | Max int + 1 wraps to min |
| 50000 * 50000 → DD | -1,794,967,296 | 2.5B wraps in 32-bit signed |

For simulation, pyrung uses Python arbitrary-precision integers with truncation on store. This differs from hardware only when intermediate overflow occurs before division — a pathological case indicating a program bug.

```python
# DS1 = 200, DS2 = 200, DS3 = 30000
math(DS1 * DS2 + DS3, Result)  # 200*200=40000, +30000=70000 in intermediate
                                # Stored to 16-bit DS: 70000 mod 65536 = 4464
```

**Truncation is modular (wrapping):** Low-order bits are preserved.

| Expression | Destination | Intermediate | Stored Result |
|------------|-------------|--------------|---------------|
| 1000 * 1000 | DD (32-bit) | 1,000,000 | 1,000,000 |
| 1000 * 1000 | DS (16-bit) | 1,000,000 | 16,960 (mod 65536) |
| 30000 + 30000 | DD (32-bit) | 60,000 | 60,000 |
| 30000 + 30000 | DS (16-bit) | 60,000 | -5,536 (signed wrap) |

**Division semantics:**

| Case | Behavior |
|------|----------|
| Division by zero | Result = 0, SC40 flag set |
| Integer division | Truncates toward zero (`-7 / 2 = -3`, not -4) |

**Mode parameter:**

| Mode | Operand treatment | Destination |
|------|-------------------|-------------|
| `"decimal"` | Signed arithmetic | Signed result |
| `"hex"` | Unsigned arithmetic | Unsigned 16-bit wrap (0x0000–0xFFFF) |

```python
# Hex mode example
# FFFFh + 1h = 0h (wraps at 16-bit unsigned boundary)
math(MaskA & MaskB, MaskResult, mode="hex")
```

### Type Conversion (via copy modifiers)

```python
copy(tag.as_value(), dest)        # Txt → numeric value ('5' → 5)
copy(tag.as_ascii(), dest)        # Txt → ASCII code ('5' → 53)
copy(tag.as_text(), dest)         # Numeric → Txt string
copy(tag.as_text(pad=5), dest)    # Numeric → zero-padded Txt
copy(tag.as_binary(), dest)       # Numeric → raw byte Txt
copy(tag.as_text(exponential=True), dest)  # Real → exponential Txt
```

### Timers

Two-bank model: a done bit (Bool) and an accumulator (Int).

| Instruction | Signature | Description |
|-------------|-----------|-------------|
| `on_delay` | `on_delay(done, acc=acc, setpoint=N, time_unit=Tms)` | TON (auto-reset) or RTON (with `.reset(tag)`). |
| `off_delay` | `off_delay(done, acc=acc, setpoint=N, time_unit=Tms)` | TOF — output stays ON until setpoint after rung goes false. |

Time units: `Tms` (milliseconds, default), `Ts` (seconds), `Tm` (minutes), `Th` (hours), `Td` (days). Accumulator always stores milliseconds internally.

### Counters

Two-bank model: a done bit (Bool) and an accumulator (Dint).

| Instruction | Signature | Description |
|-------------|-----------|-------------|
| `count_up` | `count_up(done, acc=acc, setpoint=N).reset(tag)` | Count up. `.reset()` required. |
| `count_down` | `count_down(done, acc=acc, setpoint=N).reset(tag)` | Count down. |
| Bidirectional | `count_up(...).down(condition).reset(tag)` | Up and down on separate conditions. |

### Loop

```python
with loop(count=N, oneshot=False):
    # Instructions execute N times per scan
    copy(Source[loop.idx], Dest[loop.idx])
```

- `loop.idx` is available inside the block, 0-based or 1-based (needs decision).
- Nested loops are NOT permitted.
- `count` can be a constant or a Tag (dynamic).

### Search

```python
search(
    condition=">" | "=" | "!=" | "<" | "<=" | ">=",
    value=100,
    start=Block[1],
    end=Block[100],
    result=ResultTag,
    found=FoundFlag,
    continuous=False,
    oneshot=False
)
```

### Shift Register

```python
shift_register(start=Block[1], end=Block[8]) \
    .clock(ClockBit) \
    .reset(ResetBit)
```

Direction determined by address order: `start < end` shifts right/up, `start > end` shifts left/down.

---

## Decisions Made

- **All instructions are hardware-agnostic.** They work on core Tags/Blocks. Dialect validators flag hardware-incompatible patterns.
- **Timers and counters use a two-bank model.** The done bit and accumulator are separate tags, potentially from separate blocks. This matches Click hardware and generalizes well.
- **`oneshot` parameter** on copy/math/block ops means "execute once on rising edge of rung power, not every scan."
- **`.immediate` on instructions:** `out(tag.immediate)`, `latch(tag.immediate)`, `reset(tag.immediate)` — the ImmediateRef flows through the instruction. Only valid on InputTag/OutputTag.
- **Inline expressions** are Python-native and run fine in simulation. Validators flag them as hardware hints when they can't be directly translated.

---

## Needs Specification

- **`out` vs `latch`/`reset` semantics:** `out` follows rung power (True when rung is True, False when False). `latch`/`reset` are sticky. Document the state machine clearly, especially for `out` on the same tag from multiple rungs ("last rung wins").
- **`oneshot` implementation:** Where is the edge-detection state stored? `SystemState.memory`? Keyed how?
- **`copy` pointer addressing:** Specify exactly what `Block[tag]` means as a source or dest in `copy`. What if the pointer is out of range?
- **Dynamic block bounds:** `blockcopy(Block.select(Start, Start+5), dest)` — `.select()` with Tag/Expression args returns `IndirectMemoryBlock`, resolved at scan time. Specify error semantics for out-of-range after resolution.
- **~~Math intermediate precision~~:** Resolved — 32-bit signed with standard two's complement wrap. pyrung uses Python arbitrary precision with truncation on store.
- **Math expression tree:** How are expressions like `PressureA + PressureB * 10` captured? Operator overloading on Tags returns Expression objects? What's the AST?
- **`math_obj.to_formula()`:** Conversion to Click Formula Pad format. Specify the output format.
- **Timer behavior details:** What happens when `on_delay` rung goes false (TON: reset acc, RTON: hold acc)? What's the accumulator value on the scan it fires? Clamping behavior at max.
- **Counter clamping:** At `INT_MAX` / `INT_MIN`, does the accumulator stop or wrap?
- **`loop.idx` base:** 0-based or 1-based? Since blocks are 1-indexed, 1-based `loop.idx` might be more natural. Or provide both (`loop.idx` 0-based, `loop.pos` 1-based)?
- **Search return semantics:** What value goes in `result` — the logical block index? The tag name? What if not found?
- **Shift register data input:** The rung condition is the data bit — confirm this. What type must start/end be (Bool only)?
- **Instruction return values:** Do instructions return anything? For chaining (`.reset()`, `.down()`)? For inspection?
