# TagMap Implementation Plan

## Context

TagMap is the Click dialect's mapping layer — it connects user-defined semantic tags/blocks to Click hardware addresses. The engine runs with logical names ("Valve", "Alarms5"); TagMap is metadata used for export, import (nickname CSV), validation, and soft PLC bridging. This follows the "Write First, Validate Later" philosophy from `spec/overview.md`.

---

## File Changes

| File | Action |
|------|--------|
| `src/pyrung/core/memory_block.py` | Modify: add `__hash__`, `map_to()` |
| `src/pyrung/core/tag.py` | Modify: add `MappingEntry`, `map_to()` |
| `src/pyrung/click/tag_map.py` | **New**: TagMap class |
| `src/pyrung/click/__init__.py` | Modify: export TagMap, MappingEntry |
| `tests/click/test_tag_map.py` | **New**: full test coverage |

---

## 1. Core Changes (`memory_block.py`, `tag.py`)

### 1a. Block gets `__hash__`

Block is a mutable dataclass (has `_tag_cache`), can't use field-based hashing. Add identity-based hash so Block can be used as a dict key in `TagMap({Alarms: c.select(...)})`:

```python
def __hash__(self) -> int:
    return id(self)
```

### 1b. `map_to()` on Tag and Block

Both return a `MappingEntry` — a lightweight frozen dataclass:

```python
# In tag.py
@dataclass(frozen=True)
class MappingEntry:
    source: Tag | Block        # the logical tag/block
    target: Tag | BlockRange   # the hardware destination

class Tag:
    ...
    def map_to(self, target: Tag | BlockRange) -> MappingEntry:
        return MappingEntry(self, target)

# In memory_block.py
class Block:
    ...
    def map_to(self, target: Tag | BlockRange) -> MappingEntry:
        return MappingEntry(self, target)
```

Usage:
```python
entries = [Valve.map_to(c[1]), Alarms.map_to(c.select(101, 200))]
mapping = TagMap(entries)
```

---

## 2. TagMap Class (`src/pyrung/click/tag_map.py`)

### Constructor

```python
class TagMap:
    def __init__(self, mappings: dict[Tag | Block, Tag | BlockRange]
                              | Iterable[MappingEntry]):
```

Accepts either a dict (spec's primary API) or a list of MappingEntry (from `map_to()`). Internally normalizes to structured entries, builds lookup dicts, runs validation.

### Internal Data Model

```python
@dataclass(frozen=True)
class _TagEntry:
    """Single tag → hardware tag mapping."""
    logical: Tag
    hardware: Tag

@dataclass(frozen=True)
class _BlockEntry:
    """Block → hardware range mapping."""
    logical: Block
    hardware_range: BlockRange
    offset: int                  # hardware_start - logical_start

class TagMap:
    _tag_entries: tuple[_TagEntry, ...]
    _block_entries: tuple[_BlockEntry, ...]
    _forward: dict[str, str]     # logical_name → hardware_name
    _reverse: dict[str, str]     # hardware_name → logical_name
```

Construction logic:
1. Classify each mapping as Tag→Tag or Block→BlockRange
2. For Block→BlockRange: compute offset, expand all address pairs into `_forward`/`_reverse`
3. For Tag→Tag: add single entry to `_forward`/`_reverse`
4. Run type validation (see section 3)
5. Run validation (see section 4)

### Public API

```python
def resolve(self, source: Tag | str) -> Tag:
    """Logical tag → hardware Tag object.

    Accepts a Tag object or tag name string.
    For block tags like "Alarms5", looks up the block mapping.
    Returns the hardware Tag (e.g., c[105] → Tag("C105", BOOL)).
    Raises KeyError if not mapped.
    """

def resolve_address(self, source: Tag | str) -> str:
    """Logical tag → hardware address string.

    Convenience: equivalent to resolve(source).name
    Returns e.g., "C105", "Y001", "DS1".
    """

def offset_for(self, block: Block) -> int:
    """Return the address offset for a block mapping.

    E.g., Alarms[1..99] mapped to C[101..199] → offset = 100.
    Raises KeyError if block not in this TagMap.
    """

def logical_tags(self) -> list[Tag]:
    """All mapped logical tags (standalone + block-expanded)."""

def hardware_tags(self) -> list[Tag]:
    """All mapped hardware tags."""

@property
def entries(self) -> tuple[_TagEntry | _BlockEntry, ...]:
    """All mapping entries for introspection."""

def __contains__(self, item: Tag | Block | str) -> bool:
    """Check if a tag/block/name is mapped."""

def __len__(self) -> int:
    """Number of individual tag mappings."""

def __repr__(self) -> str:
    """TagMap(5 tags, 2 blocks)"""
```

---

## 3. Type Validation

Runs during TagMap construction. `Block.type` is always required — TagMap **validates** compatibility rather than inferring types.

### Tag → Tag mapping:
- Logical tag type must match hardware tag type (both come from their respective blocks or constructors)
- E.g., `Bool("Valve")` (BOOL) → `c[1]` (BOOL from C bank) — OK
- E.g., `Int("Counter")` (INT) → `c[1]` (BOOL) — TypeError

### Block → BlockRange mapping:
- Logical block type must match hardware block type
- E.g., `Block("Alarms", TagType.BOOL, 1, 100)` → `c.select(101, 200)` (BOOL) — OK
- Logical block size must be <= hardware range size

---

## 4. Validation

Validation runs at TagMap construction time. Errors raise `ValueError` with clear messages. Warnings are collected and accessible.

### Checks performed:

**Errors (raise immediately):**
1. **Type mismatch**: logical tag type != hardware tag type
2. **Size mismatch**: logical block range > hardware BlockRange length
3. **Address conflict**: two logical tags map to the same hardware address
4. **Reverse conflict**: same logical name appears twice

**Warnings (collected, accessible via `.warnings`):**
1. **Nickname too long**: logical tag name > 24 chars (pyclickplc `NICKNAME_MAX_LENGTH`)
2. **Forbidden chars**: logical tag name contains chars from pyclickplc `FORBIDDEN_CHARS`
3. **Reserved name**: logical tag name in pyclickplc `RESERVED_NICKNAMES`

Note: retentive/default differences between logical and hardware tags are **intentional** — each tag carries its own settings. No warning needed.

```python
@property
def warnings(self) -> list[str]:
    """Validation warnings from construction."""
```

---

## 5. Nickname File Round-Trip

### `TagMap.from_nickname_file(path: str | Path) -> TagMap`

1. `records = pyclickplc.read_csv(path)` → `dict[int, AddressRecord]`
2. Group records that have nicknames by memory_type
3. Use `pyclickplc.compute_all_block_ranges(records)` to find `<Name>`/`</Name>` grouped rows
   - Returns list of `pyclickplc.BlockRange(start_idx, end_idx, name, ...)`
   - `parse_block_tag(comment)` → `BlockTag` for individual row parsing
4. For each block range group:
   - Create `Block(group.name, type_from_bank, start, end)` as logical
   - Create hardware BlockRange from the pre-built Click block
   - Add as Block→BlockRange entry
5. For standalone nicknamed addresses:
   - Create logical `Tag(nickname, type_from_bank, retentive=record.retentive, default=record.initial_value)`
   - Look up the hardware Tag from the pre-built block: e.g., `c[addr]` — ensures same Tag object identity as dict-constructor path
   - Add as Tag→Tag entry

### `TagMap.to_nickname_file(path: str | Path) -> int`

1. Build `dict[int, AddressRecord]` from all mappings:
   - For each Tag→Tag entry: create AddressRecord with nickname=logical.name, address from hardware, retentive=logical.retentive, initial_value from logical.default
   - For each Block→BlockRange entry: emit `format_block_tag(name, "open")` / `format_block_tag(name, "close")` markers in comments + individual records (per-tag retentive/default)
2. `count = pyclickplc.write_csv(path, records)`
3. Return count

Key pyclickplc functions used:
- `read_csv(path)` / `write_csv(path, records)` — CSV I/O
- `compute_all_block_ranges(rows)` — find `<Name>`/`</Name>` block groups
- `parse_block_tag(comment)` / `format_block_tag(name, type)` — block tag parsing/formatting
- `get_addr_key(memory_type, address)` — for building the records dict
- `parse_address(str)` — for parsing hardware tag names back to (type, addr)
- `format_address_display(type, addr)` — for generating display names
- `AddressRecord(...)` — for constructing export records
- `CLICK_TO_IEC` (our mapping) — for DataType → TagType conversion

---

## 6. Implementation Order

### Step 1: Core changes
- `Block.__hash__` via `id()`
- `MappingEntry` dataclass in `tag.py`
- `Tag.map_to()` and `Block.map_to()` methods

### Step 2: TagMap core
- `_TagEntry`, `_BlockEntry` internal types
- `TagMap.__init__` with dict and MappingEntry support
- `_forward` / `_reverse` dict construction
- `resolve()`, `resolve_address()`, `offset_for()`
- `__contains__`, `__len__`, `__repr__`

### Step 3: Validation
- Error checks (type mismatch, size, conflicts)
- Warning collection (nickname rules via pyclickplc)

### Step 4: Nickname file I/O
- `from_nickname_file()` using pyclickplc.read_csv
- `to_nickname_file()` using pyclickplc.write_csv

### Step 5: Integration + export
- Export TagMap, MappingEntry from `pyrung.click.__init__`
- Update `__all__`

---

## 7. Testing Strategy (`tests/click/test_tag_map.py`)

```
test_tag_to_tag_mapping          — Valve: c[1], resolve, reverse lookup
test_block_to_range_mapping      — Alarms: c.select(101,200), offset, expansion
test_map_to_syntax               — Tag.map_to(), Block.map_to() → MappingEntry → TagMap
test_dict_constructor            — TagMap({...}) primary API
test_type_mismatch_error         — BOOL tag mapped to INT bank → ValueError
test_size_mismatch_error         — block too large for range → ValueError
test_address_conflict_error      — two tags → same hardware addr → ValueError
test_per_tag_retentive_preserved — tag retentive/default not overwritten by mapping
test_nickname_warnings           — long names, forbidden chars
test_resolve_standalone_tag      — resolve("Valve") → "C1"
test_resolve_block_tag           — resolve("Alarms5") → "C105"
test_resolve_unknown_raises      — resolve("Unknown") → KeyError
test_offset_for                  — offset_for(Alarms) → 100
test_contains                    — "Valve" in mapping, Alarms in mapping
test_from_nickname_file          — round-trip: write → read → verify
test_to_nickname_file            — export mappings to CSV
test_empty_tagmap                — TagMap({}) works
test_input_output_blocks         — x/y blocks map correctly (InputTag/OutputTag preserved)
```

Run with: `make test` (which calls pytest)

---

## 8. Verification

```bash
make                    # full workflow: install + lint + test
make lint               # codespell, ruff (check + format), ty
make test               # pytest
```

All existing tests must continue to pass (core changes are additive). New tests in `tests/click/test_tag_map.py` cover all TagMap functionality.

---

## 9. Deferred / Out of Scope

These are mentioned in the spec's "Needs Specification" but are **not** part of this plan:

- **ClickDataProvider** (soft PLC adapter) — uses TagMap but is a separate feature
- **Click validation rules** (pointer restrictions, expression restrictions) — separate validator
- **ValidationReport integration** — the validator feature, not TagMap itself
- **XD/YD addressing** — pseudo-addresses, needs spec work first
- **SC/SD system blocks** — read-only rules
