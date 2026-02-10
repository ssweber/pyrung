# Click Dialect — Handoff Brief

> **Status:** Handoff — decisions captured, needs full spec writeup.
> **Depends on:** All core specs. Also depends on `pyclickplc` (external package).
> **Implementation milestones:** 9 (pyclickplc extraction), 10 (Tag Mapping)
> **See also:** `spec/HANDOFF.md` (rich value types & soft PLC architecture)

---

## Scope

Everything Click-hardware-specific: pre-built blocks, Click type aliases, `TagMap`,
nickname file I/O, Click validation rules, the bridge to `pyclickplc`, and the
soft PLC adapter (`ClickDataProvider`).

---

## Decisions Made

### Pre-built Blocks

Constructed from `pyclickplc.BANKS[NAME]`. These are convenience instances — users can also create their own blocks and map them.

```python
from pyrung.click import x, y, c, ds, dd, dh, df, t, td, ct, ctd, sc, sd, txt

# x and y are InputBlock/OutputBlock, everything else is Block

# exported variables are lowercase, but block identities remain uppercase
assert x.name == "X"
assert ds.name == "DS"

# canonical Click tag display names
assert x[1].name == "X001"
assert y[1].name == "Y001"
assert c[1].name == "C1"
assert ds[1].name == "DS1"
```

Exact ranges come from `pyclickplc`. Retentive defaults also come from `pyclickplc` (`DEFAULT_RETENTIVE`).

Sparse selection behavior:

```python
# X/Y are sparse banks. select() uses an inclusive window and returns only valid addresses.
window = x.select(1, 21)
# Includes X001..X016 and X021 (17 tags total)
```

### Click Type Aliases

Re-exported from `pyrung.click` for Click-familiar users. These are the **only**
home for Click aliases — pyrung core uses IEC names exclusively.

```python
# Tag constructors
Bit   = Bool
Int2  = Dint
Float = Real
Hex   = Word
Txt   = Char

# Rich value types (re-exported from pyclickplc)
from pyclickplc.values import PlcBit, PlcInt2, PlcFloat, PlcHex, PlcTxt
```

These are just aliases — they produce standard `Tag` objects with IEC `TagType`.
Rich value types are the same classes as their IEC counterparts (e.g. `PlcHex`
is `PlcWord`).

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


Uses from pyclickplc import read_csv, write_csv # see readme
```

### Type Inference at Map Time

When a user-defined Block omits the type, it's inferred from the hardware bank it maps to:

```python
Alarms = Block("Alarms", range(1, 100))        # No type specified
Alarms.map_to(c.select(101, 200))                       # → Bool inferred from C bank
```

Explicit type always overrides. Retentive default can also be inferred but explicit overrides.

### `_block_from_bank_config()` Bridge

This is a Click-dialect factory, **not** a method on core `Block`:

```python
# In pyrung.click
from pyclickplc import BankConfig

def _block_from_bank_config(config: BankConfig) -> Block | InputBlock | OutputBlock:
    """Construct a Block from pyclickplc bank metadata.
    
    Dispatches to InputBlock/OutputBlock/Block based on 
    memory_type (X → InputBlock, Y → OutputBlock, else Block).
    """
```

Core `Block` has no knowledge of Click metadata types.

### Nickname File Round-Trip

Loading:
```python
mapping = TagMap.from_nickname_file("project.csv")
# Uses pyclickplc.nicknames.read_csv()/load helper
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

### Soft PLC Adapter (`ClickDataProvider`)

Implements pyclickplc's `DataProvider` protocol, bridging pyrung's `SystemState`
to the Modbus server. This enables pyrung to act as a soft PLC accessible via
standard Modbus TCP.

```python
from pyclickplc.server import ClickServer, DataProvider

class ClickDataProvider:
    """Bridges pyrung SystemState to pyclickplc DataProvider protocol."""

    def read(self, address: str) -> PlcValue:
        tag_name = self._address_to_tag(address)
        return self._state.tags.get(tag_name, default)

    def write(self, address: str, value: PlcValue) -> None:
        self._runner.patch({tag_name: value})

# Usage:
provider = ClickDataProvider(runner)
server = ClickServer(provider, port=502)
```

Values flow as raw primitives through the adapter — no rich type wrapping needed
on the server path. See `spec/HANDOFF.md` for the full data flow diagram.

---

## Needs Specification

- **ClickDataProvider details:** Thread safety (Modbus server is async, scan engine is sync). How writes are queued for the next scan. How reads get a consistent snapshot.
- **TagMap.resolve():** Given a logical tag reference, return the hardware address. Specify the return type.
- **TagMap.offset_for():** Given a Block, return the offset between logical indices and hardware addresses. E.g., `Alarms` at logical 1–99 mapped to C101–199, offset = 100.
- **Validation report sections:** Mapping status, mapping errors, hardware hints, summary. Port from original SPEC.md with updated terminology (Block not MemoryBank).
- **Bank compatibility matrix:** Which Click banks can be compared, used together in blockcopy, etc. This comes from pyclickplc.
- **XD/YD addressing:** Click's XD/YD pseudo-addresses for reading discrete inputs/outputs as words. How does this surface?
- **Interleaved pairs:** Click's DD/DH/DF interleaving. Document how this affects mapping validation.
- **Tag name validation at map time:** Names validated against `pyclickplc.validation` rules (24-char max, forbidden chars, reserved words). Errors vs warnings.
- **SC/SD (system) blocks:** Are these read-only? Can you map user tags to them? Probably no — they're system-provided.
- **What `pyclickplc` provides vs what `pyrung.click` owns:** Clear boundary. pyclickplc = address model, types, file I/O, rich value types. pyrung.click = live blocks, TagMap, validation, DSL integration, soft PLC adapter.
