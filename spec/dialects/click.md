# Click Dialect — Handoff Brief

> **Status:** Handoff — decisions captured, needs full spec writeup.
> **Depends on:** All core specs. Also depends on `pyclickplc` (external package).
> **Implementation milestones:** 9 (pyclickplc extraction), 10 (Tag Mapping)

---

## Scope

Everything Click-hardware-specific: pre-built blocks, Click type aliases, `TagMap`, nickname file I/O, Click validation rules, and the bridge to `pyclickplc`.

---

## Decisions Made

### Pre-built Blocks

Constructed from `clickplc.banks.ADDRESS_RANGES`. These are convenience instances — users can also create their own blocks and map them.

```python
from pyrung.click import x, y, c, ds, dd, dh, df, t, td, ct, ctd, sc, sd, txt

# x and y are InputBlock/OutputBlock, everything else is Block
x   = InputBlock("X",   Bool, range(1, 817))
y   = OutputBlock("Y",  Bool, range(1, 817))
c   = Block("C",        Bool, range(1, 2001))
ds  = Block("DS",       Int,  range(1, 4501))
dd  = Block("DD",       Dint, range(1, 1001))
dh  = Block("DH",       Word, range(1, 501))
df  = Block("DF",       Real, range(1, 501))
t   = Block("T",        Bool, range(1, 501))
td  = Block("TD",       Int,  range(1, 501))
ct  = Block("CT",       Bool, range(1, 251))
ctd = Block("CTD",      Dint, range(1, 251))
sc  = Block("SC",       Bool, range(1, ...))   # System control bits
sd  = Block("SD",       Int,  range(1, ...))   # System data
txt = Block("TXT",      Char, range(1, 1001))
```

Exact ranges come from `pyclickplc`. Retentive defaults also come from `pyclickplc` (`DEFAULT_RETENTIVE`).

### Click Type Aliases

Re-exported from `pyrung.click` for Click-familiar users:

```python
Bit   = Bool
Int2  = Dint
Float = Real
Hex   = Word
Txt   = Char
```

These are just aliases — they produce standard `Tag` objects with IEC `TagType`.

### TagMap

Click-specific. Maps semantic tags/blocks to Click hardware addresses.

```python
from pyrung.click import TagMap

# Dict constructor
mapping = TagMap({
    Valve:      c[1],
    Motor:      y[1],
    Alarms:     c.select(101, 200),  # Block → hardware range
    PumpDone:   t[1],
    PumpAcc:    td[1],
})

# Method-call syntax
Valve.map_to(c[1])
Alarms.map_to(c.select(101, 200))

# From nickname file
mapping = TagMap.from_nickname_file("project.csv")

# Export
mapping.to_nickname_file("project.csv")
```

### Type Inference at Map Time

When a user-defined Block omits the type, it's inferred from the hardware bank it maps to:

```python
Alarms = Block("Alarms", range(1, 100))        # No type specified
Alarms.map_to(c.select(101, 200))                       # → Bool inferred from C bank
```

Explicit type always overrides. Retentive default can also be inferred but explicit overrides.

### `from_meta()` Bridge

This is a Click-dialect factory, **not** a method on core `Block`:

```python
# In pyrung.click
from clickplc.blocks import MemoryBankMeta

def block_from_meta(meta: MemoryBankMeta) -> Block | InputBlock | OutputBlock:
    """Construct a Block from pyclickplc metadata.
    
    Dispatches to InputBlock/OutputBlock/Block based on 
    memory_type (X → InputBlock, Y → OutputBlock, else Block).
    """
```

Core `Block` has no knowledge of `MemoryBankMeta`.

### Nickname File Round-Trip

Loading:
```python
mapping = TagMap.from_nickname_file("project.csv")
# Uses clickplc.nicknames.load_nickname_file()
# Reconstructs Blocks from MemoryBankMeta (block tags)
# Creates Tags for standalone nicknames
# Timer/counter _D suffix pairs linked automatically
```

Exporting:
```python
mapping.to_nickname_file("project.csv")
# Blocks emit <Name> / </Name> block tag pairs
# Standalone tags emit individual rows
# Unmapped tags omitted
```

### Click Validation Rules

The validator walks every rung and checks against Click hardware restrictions. Produces a `ValidationReport` (core structure, Click rules).

**Pointer restrictions:**

| Context | pyrung allows | Click requires |
|---------|--------------|----------------|
| Pointer in copy | Any block, arithmetic | DS only, no arithmetic |
| Pointer in blockcopy | Not allowed | Not allowed |
| Pointer in comparison | Any block, arithmetic | Not allowed |

**Expression restrictions:**

| Context | pyrung allows | Click requires |
|---------|--------------|----------------|
| Inline in condition | `(A + B) > 100` | Must use `math()` first |
| Inline in copy source | `copy(A * 2, dest)` | Must use `math()` first |

**The validator produces hints, not errors,** for hardware incompatibilities. The program runs fine in simulation. Hints include concrete rewrite suggestions using the actual mapped addresses and offsets.

### System Control Flags (Hardware-Verified)

Click uses SC (System Control) bits to signal math/copy errors. These flags are **auto-reset at scan start** — they remain ON for the remainder of the scan in which they triggered, then clear automatically.

| SC Bit | Name | Trigger | Result Behavior |
|--------|------|---------|-----------------|
| SC40 | `_Division_Error` | Division by zero | Result forced to 0 |
| SC43 | `_Out_of_Range` | Value exceeds destination range | COPY: clamped, MATH: wrapped |
| SC44 | `_Address_Error` | Pointer out of range | Operation aborted |
| SC46 | `_Math_Operation_Error` | Invalid register values | **Fatal:** sets SC50, stops PLC |

**Scan-level behavior:**

```python
# At scan start (phase 0):
state = state.set_bit("SC40", False)
state = state.set_bit("SC43", False)
state = state.set_bit("SC44", False)
# SC46 is fatal and latches — not auto-cleared

# During execution:
# - If math(100 / 0, Result) executes: SC40 = True, Result = 0
# - Flag stays True for rest of scan
# - Next scan: SC40 auto-clears to False
```

**Contrast with Allen-Bradley:** Click flags auto-reset and never halt the processor (except SC46). Allen-Bradley SLC-500 latches overflow flags and can fault the processor if not explicitly cleared.

---

## Needs Specification

- **TagMap.resolve():** Given a logical tag reference, return the hardware address. Specify the return type.
- **TagMap.offset_for():** Given a Block, return the offset between logical indices and hardware addresses. E.g., `Alarms` at logical 1–99 mapped to C101–199, offset = 100.
- **Validation report sections:** Mapping status, mapping errors, hardware hints, summary. Port from original SPEC.md with updated terminology (Block not MemoryBank).
- **Bank compatibility matrix:** Which Click banks can be compared, used together in blockcopy, etc. This comes from pyclickplc.
- **XD/YD addressing:** Click's XD/YD pseudo-addresses for reading discrete inputs/outputs as words. How does this surface?
- **Interleaved pairs:** Click's DD/DH/DF interleaving. Document how this affects mapping validation.
- **Tag name validation at map time:** Names validated against `clickplc.validation` rules (24-char max, forbidden chars, reserved words). Errors vs warnings.
- **SC/SD (system) blocks:** Are these read-only? Can you map user tags to them? Probably no — they're system-provided.
- **What `pyclickplc` provides vs what `pyrung.click` owns:** Clear boundary. pyclickplc = address model, types, file I/O. pyrung.click = live blocks, TagMap, validation, DSL integration.
