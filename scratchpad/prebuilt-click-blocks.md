# Plan: Pre-built Click Blocks

## Context

The Click dialect (`pyrung.click`) currently only exports type aliases. The next step is pre-built block instances — hardware memory bank objects constructed from `pyclickplc.BANKS`. Click blocks should only expose real hardware addresses (sparse X/Y validation).

## Design

### 1. Core changes: sparse validation + canonical tag formatting

Add two optional fields to core `Block`:
- `valid_ranges`: sparse address model (for X/Y-like banks)
- `address_formatter`: tag-name formatter hook (for dialect-specific canonical names)

**Changes to `src/pyrung/core/memory_block.py`:**

- Add field: `valid_ranges: tuple[tuple[int, int], ...] | None = None`
- Add field: `address_formatter: Callable[[str, int], str] | None = None`
- Add helper: `_format_tag_name(addr)` with default `f"{name}{addr}"` when no formatter is set
- `__post_init__`: validate sparse segments fall within `[start, end]` and each segment has `lo <= hi`
- `__getitem__(int)`: check address against valid_ranges when present
- `select(int, int)`:
  - enforce `start <= end`
  - for sparse blocks, treat selection as an inclusive address window and include only valid addresses in that window
  - do not require start/end to be in the same sparse segment
- Ensure tag creation paths (`Block._get_tag`, `InputBlock._get_tag`, `OutputBlock._get_tag`) use `_format_tag_name(addr)` instead of manual string concatenation
- `BlockRange` updates:
  - `addresses` must reflect sparse-filtered addresses from block rules, not raw `range(start, end + 1)`
  - `__len__` must match filtered address count
  - `tags()` and iteration must walk filtered addresses in ascending order
- `IndirectBlockRange.resolve_ctx(ctx)` must call `block.select(start, end)` so runtime-resolved bounds use the same validation/filtering rules
- Export from `__init__.py` — no new exports needed, `valid_ranges` is just a Block parameter

**InputBlock/OutputBlock:** Pass `valid_ranges` and `address_formatter` through to parent. Requires updating their `__init__` signatures.

### 2. Type mapping: `pyclickplc.DataType` → pyrung `TagType`

```python
CLICK_TO_IEC = {
    DataType.BIT:   TagType.BOOL,
    DataType.INT:   TagType.INT,
    DataType.INT2:  TagType.DINT,
    DataType.FLOAT: TagType.REAL,
    DataType.HEX:   TagType.WORD,
    DataType.TXT:   TagType.CHAR,
}
```

### 3. Factory function (private)

`_block_from_bank_config(config) → Block | InputBlock | OutputBlock`

- X → InputBlock, Y → OutputBlock, everything else → Block
- Type from CLICK_TO_IEC
- Retentive from DEFAULT_RETENTIVE (the bank default — per-address overrides are a TagMap concern)
- `valid_ranges` passed through from BankConfig (X/Y get sparse ranges, others get None)
- `address_formatter` set to `pyclickplc.format_address_display` so tag names use Click canonical display form (`X001`, `Y001`, `C1`, `DS1`, etc.)
- Block identity names remain uppercase (`"X"`, `"DS"`), while exported variables stay lowercase (`x`, `ds`)
- Build from `pyclickplc.BANKS` entries in one place to avoid duplicated ranges/defaults

### 4. Pre-built block instances (14 variables, lowercase)

| Variable | Block Name | Class | Type | Range | Sparse | Retentive |
|----------|------------|-------|------|-------|--------|-----------|
| x | X | InputBlock | BOOL | 1-816 | 10 slots of 16 | - |
| y | Y | OutputBlock | BOOL | 1-816 | 10 slots of 16 | - |
| c | C | Block | BOOL | 1-2000 | No | False |
| t | T | Block | BOOL | 1-500 | No | False |
| ct | CT | Block | BOOL | 1-250 | No | True |
| sc | SC | Block | BOOL | 1-1000 | No | False |
| ds | DS | Block | INT | 1-4500 | No | True |
| dd | DD | Block | DINT | 1-1000 | No | True |
| dh | DH | Block | WORD | 1-500 | No | True |
| df | DF | Block | REAL | 1-500 | No | True |
| td | TD | Block | INT | 1-500 | No | False |
| ctd | CTD | Block | DINT | 1-250 | No | True |
| sd | SD | Block | INT | 1-1000 | No | False |
| txt | TXT | Block | CHAR | 1-1000 | No | True |

### 5. Retentive: defaults only, overrides via TagMap

Pre-built blocks use `DEFAULT_RETENTIVE` values. These are bank defaults, not per-address truth. Tags are lazy references (name + type + retentive), not value containers — values live in SystemState.

Per-address retentive overrides flow through TagMap:
- `TagMap.from_nickname_file()` reads per-address retentive from CSV
- User-defined logical tags carry their own retentive
- Pre-built block defaults don't pin anything down

### 6. XD/YD excluded

Not included as pre-built blocks:
- Core Block enforces `start >= 1`; XD/YD start at 0
- XD0/XD0u/XD1 addressing breaks the `block[int]` pattern
- They're Modbus views of X/Y discrete I/O packed as hex words
- The spec's import list already excludes them
- Will be addressed when ClickDataProvider is built

## Files to modify

1. **`src/pyrung/core/memory_block.py`** — Add `valid_ranges` to Block, InputBlock, OutputBlock; update validation logic
2. **`src/pyrung/click/__init__.py`** — Add type mapping, factory, 14 block instances, update `__all__`
3. **`tests/core/test_memory_bank.py`** — Test sparse range validation/selection and formatter-based naming
4. **`tests/click/test_aliases.py`** — Extend with block tests

## Test checklist

### Core (`tests/core/test_memory_bank.py`)

- `Block(..., valid_ranges=...)` allows valid sparse addresses and rejects gaps:
  - valid: `1`, `16`, `21`
  - invalid: `17`
- `select()` on sparse block is inclusive-window filtered:
  - `select(1, 21)` includes `1..16` and `21`
  - `len()` reflects filtered count
  - iteration and `tags()` preserve ascending valid order
- `select(start, end)` with `start > end` raises `ValueError`
- `IndirectBlockRange.resolve_ctx()` applies the same rules (including `start > end` rejection)
- `address_formatter` behavior:
  - default formatter gives `DS1` style names
  - custom formatter gives custom names
  - InputBlock/OutputBlock use formatter too

### Click (`tests/click/test_aliases.py`)

- Prebuilt blocks are exported (`x`, `y`, `c`, `t`, `ct`, `sc`, `ds`, `dd`, `dh`, `df`, `td`, `ctd`, `sd`, `txt`)
- `x` is `InputBlock`, `y` is `OutputBlock`, others are `Block`
- Block identity names are uppercase (`x.name == "X"`, `ds.name == "DS"`)
- Canonical tag naming:
  - `x[1].name == "X001"`
  - `y[1].name == "Y001"`
  - `c[1].name == "C1"`
  - `ds[1].name == "DS1"`
- Sparse select behavior:
  - `x.select(1, 21)` returns expected valid addresses/tags
  - `x[17]` raises `IndexError`
- Bank metadata correctness:
  - expected `TagType` per bank
  - expected default `retentive` per bank

## Verification

- `make test` — all passes
- `make lint` — clean
- `x[1]` works and tag name is `X001`; `y[1]` is `Y001`; `c[1]` is `C1`; `ds[1]` is `DS1`
- `x[17]` raises IndexError (sparse gap)
- `x.select(1, 21)` works and returns all valid addresses in that inclusive window (`1-16` and `21`)
- `x.select(21, 1)` raises ValueError (`start` must be `<= end`)
- `ds[1]` works normally (no valid_ranges, uses min/max)
- `x[1]` returns InputTag, `y[1]` returns OutputTag, `ds[1]` returns Tag
- All blocks have correct type, retentive defaults
