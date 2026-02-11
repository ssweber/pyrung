# TagMap Implementation Plan

## Context

`TagMap` is the Click dialect's mapping layer. It maps user-defined logical `Tag`/`Block` objects to Click hardware addresses for:

- Address resolution (`resolve`)
- Nickname CSV import/export
- Mapping validation
- Soft PLC bridge features that need logical-to-hardware lookup

This follows the architecture's "Write First, Validate Later" flow in `spec/overview.md`.

### Key Mental Model

`TagMap` maps two shapes:

- **Standalone tags** (`Valve`, `PumpDone`) map to one hardware address each.
- **Blocks** (`Alarm[1..100]`) map to hardware ranges and resolve by slot lookup.

Hardware addresses are locations only. Logical metadata (name, default, retentive) comes from the logical source side.

Block mappings must support sparse banks (for example `X`/`Y`) with no special user behavior.
Resolution is always by aligned logical/hardware slot lists, never by assuming contiguous offset math.

For the Tag/Block boundary, we support per-slot metadata overrides at the mapping layer:

```python
mapping = TagMap({Alarm: c.select(101, 200)})
mapping.override(Alarm[1], default=1, retentive=True)
mapping.override(Alarm[1], name="Alarm1_id")
```

This gives per-index control without making `Tag` mutable.

---

## File Changes

| File | Action |
|------|--------|
| `src/pyrung/core/memory_block.py` | Modify: identity semantics for `Block`, add `map_to()` |
| `src/pyrung/core/tag.py` | Modify: add `MappingEntry`, add `map_to()` |
| `src/pyrung/click/tag_map.py` | **New**: `TagMap` implementation |
| `src/pyrung/click/__init__.py` | Modify: export `TagMap`, `MappingEntry` |
| `tests/click/test_tag_map.py` | **New**: full coverage for mapping + overrides + CSV round-trip |

---

## 1. Core Changes (`memory_block.py`, `tag.py`)

### 1a. Block identity semantics (hash/eq contract safe)

`Block` must be usable as a dict key for `TagMap({...})` and lookups by identity.

Use dataclass identity semantics directly:

```python
@dataclass(eq=False)
class Block:
    ...
```

Also set `eq=False` on `InputBlock` and `OutputBlock`.

Result:
- `__eq__` is identity-based (object default)
- `__hash__` is identity-based (object default)
- No hash/eq contract break

### 1b. `map_to()` on `Tag` and `Block`

Add a frozen `MappingEntry` declaration in `tag.py`:

```python
@dataclass(frozen=True)
class MappingEntry:
    source: Tag | Block
    target: Tag | BlockRange
```

Add helpers:

```python
class Tag:
    def map_to(self, target: Tag) -> MappingEntry: ...

class Block:
    def map_to(self, target: BlockRange) -> MappingEntry: ...
```

### 1c. Keep `Tag` immutable

Do not add `Tag.name`/`Tag.default`/`Tag.retentive` setters.

`Tag` stays frozen (`src/pyrung/core/tag.py`) and existing frozen-behavior tests remain valid.

Per-slot adjustments are handled in `TagMap` override metadata (section 2c).

---

## 2. TagMap Class (`src/pyrung/click/tag_map.py`)

### 2a. Constructor

```python
class TagMap:
    def __init__(
        self,
        mappings: dict[Tag | Block, Tag | BlockRange] | Iterable[MappingEntry] | None = None,
    ):
```

Supports:
- Dict constructor (primary API)
- `MappingEntry` iterable (`map_to()` syntax)
- Empty map (`None` or `{}`)

### 2b. Internal model

```python
@dataclass(frozen=True)
class _TagEntry:
    logical: Tag
    hardware: Tag

@dataclass(frozen=True)
class _BlockEntry:
    logical: Block
    hardware: BlockRange
    logical_addresses: tuple[int, ...]
    hardware_addresses: tuple[int, ...]
    logical_to_hardware: dict[int, int]  # logical slot address -> hardware slot address
```

Lookups:

```python
_tag_entries: tuple[_TagEntry, ...]
_block_entries: tuple[_BlockEntry, ...]
_tag_forward: dict[str, _TagEntry]     # logical standalone name -> entry
_block_lookup: dict[int, _BlockEntry]  # id(logical block) -> entry
_slot_ids: set[int]                    # id(slot Tag) for mapped block slots
_standalone_names: set[str]            # logical standalone tag names
```

All internal keying avoids raw `Tag` equality semantics. Use stable primitive keys
(`tag.name`, `id(block)`, and per-slot object identity where required).

### 2c. Per-slot override bridge API

Add mapping-layer override state:

```python
UNSET = object()

@dataclass(frozen=True)
class SlotOverride:
    name: str | None = None
    retentive: bool | None = None
    default: object = UNSET
```

Public API:

```python
def override(
    self,
    slot: Tag,
    *,
    name: str | None = None,
    retentive: bool | None = None,
    default: object = UNSET,
) -> None:
    """Attach export/mapping metadata override to a mapped slot."""

def clear_override(self, slot: Tag) -> None: ...
def get_override(self, slot: Tag) -> SlotOverride | None: ...
```

Rules:
- `slot` must belong to this `TagMap` (standalone mapped tag or tag returned by a mapped logical block).
- Overrides do **not** mutate runtime `Tag` objects.
- Effective metadata precedence at export/validation:
  1. Slot override
  2. Logical slot tag metadata (`slot.name`, `slot.default`, `slot.retentive`)

Membership checks are key-based:
- standalone slots by `slot.name`
- mapped block slots by `id(slot)` (from mapped logical blocks)

### 2d. Public API

```python
def resolve(self, source: Tag | Block | str, index: int | None = None) -> str
def offset_for(self, block: Block) -> int
def tags(self) -> tuple[_TagEntry, ...]
def blocks(self) -> tuple[_BlockEntry, ...]
@property
def entries(self) -> tuple[_TagEntry | _BlockEntry, ...]
def __contains__(self, item: Tag | Block | str) -> bool
def __len__(self) -> int
def __repr__(self) -> str
```

`resolve` behavior:
- `resolve("Valve")` / `resolve(Valve)` for standalone tags
- `resolve(Alarm, 5)` for block slot
- `TypeError` for wrong call shape
- `KeyError` for missing mapping
- `IndexError` for block index out of logical range

`offset_for` behavior:
- returns `int` only for affine mappings where every slot satisfies `hardware = logical + k`
- raises `ValueError` for non-affine mappings (for example sparse `X`/`Y` windows)

---

## 3. Validation

Validation runs during `TagMap` construction. Failures raise `ValueError`.

### Errors

1. Unsupported mapping pair (`Tag->BlockRange`, `Block->Tag`, etc.)
2. Type mismatch (logical vs hardware bank type)
3. Block size mismatch (logical window larger than hardware range)
4. Hardware address conflicts:
   - standalone tag vs standalone tag
   - standalone tag inside mapped block range
   - overlapping mapped block ranges
5. Standalone logical name conflicts
6. Effective nickname collisions across exported rows (standalone + block slots, after overrides)

### Warnings (`.warnings`)

Nickname rules from `pyclickplc.validation.validate_nickname` are checked for all effective exported names.

This includes:
- standalone logical tag names (or overrides)
- block slot names (auto-generated or overridden)

---

## 4. Nickname File Round-Trip

### 4a. `TagMap.from_nickname_file(path: str | Path) -> TagMap`

Implementation flow (aligned with current `pyclickplc` APIs):

1. `records = pyclickplc.read_csv(path)` returns `dict[int, AddressRecord]`
2. Build ordered rows:
   - `rows = sorted(records.values(), key=lambda r: (MEMORY_TYPE_BASES[r.memory_type], r.address))`
3. `ranges = pyclickplc.compute_all_block_ranges(rows)` (uses row indices)
4. For each block range:
   - use `start_idx`/`end_idx` to identify boundary rows even if middle rows are missing from CSV
   - derive block name from parsed opening tag comment
   - determine `memory_type`, `start_addr`, and `end_addr` from boundary rows
   - build full hardware slot list from dialect bank range:
     - `full_hw_addrs = tuple(click_block.select(start_addr, end_addr).addresses)`
   - create logical block as `Block(block_name, type_from_bank, 1, len(full_hw_addrs), retentive=bank_default)`
   - map logical block to `click_block.select(start_addr, end_addr)`
   - apply per-row slot overrides by matching each present CSV row to its logical slot via hardware address lookup
5. For rows not covered by block ranges:
   - if row has nickname, create standalone logical `Tag(...)`
   - map to corresponding hardware address tag
6. Return `TagMap` with mappings + applied overrides

Notes:
- Do not assume `len(block_rows)` equals logical block size. CSV files can omit interior rows.
- Must support arbitrary existing Click CSV files, not just dense files exported by `TagMap`.

### 4b. `TagMap.to_nickname_file(path: str | Path) -> int`

Sparse export: only mapped entries produce rows.

1. Build `dict[int, AddressRecord]` for mapped standalone tags and mapped block slots
2. For each block mapping:
   - emit `<Name>` and `</Name>` using `format_block_tag`
   - emit each slot row with effective metadata (override-aware name/default/retentive)
3. `count = pyclickplc.write_csv(path, records)`
4. Return `count`

Unmapped hardware addresses are omitted so Click factory defaults are not overwritten.

---

## 5. Type Handling and Conversion

Type compatibility checks use:
- hardware bank data type (`pyclickplc.BANKS[...]`)
- `CLICK_TO_IEC` mapping in `pyrung.click`

CSV value conversion helpers:
- parse CSV `initial_value` string into Python value by `TagType` on import
- format Python default into CSV-compatible string on export

No type inference from hardware to logical `Block` in map-time constructor. Logical `Block.type` remains required.

---

## 6. Implementation Order

### Step 1: Core
- `Block`/`InputBlock`/`OutputBlock` use `eq=False`
- Add `MappingEntry`
- Add `Tag.map_to()` / `Block.map_to()`

### Step 2: TagMap core
- Implement `_TagEntry`, `_BlockEntry`
- Constructor normalization (dict + iterable support)
- Sparse-safe block slot alignment (`logical_addresses`, `hardware_addresses`, `logical_to_hardware`)
- Lookup structures keyed by stable primitives (no raw `Tag` key dependence)
- `resolve`, `offset_for`, container/introspection dunders

### Step 3: Override bridge
- `SlotOverride`, override storage, public override APIs
- Effective metadata resolver used by validation/export

### Step 4: Validation
- Error checks + warning collection
- Override-aware nickname checks and uniqueness checks

### Step 5: CSV I/O
- `from_nickname_file()` with row-index block detection + boundary-address span reconstruction
- `to_nickname_file()` sparse export with block tags

### Step 6: Integration + spec sync
- Export `TagMap` + `MappingEntry` from `pyrung.click.__init__`
- Update `__all__`
- Update spec docs to use `TagMap` terminology consistently

---

## 7. Testing Strategy (`tests/click/test_tag_map.py`)

```
test_resolve_standalone_tag
test_resolve_block_slot
test_resolve_block_slot_sparse_bank
test_offset_for_block
test_offset_for_sparse_block_raises
test_map_to_syntax
test_dict_constructor
test_empty_map
test_contains_tag_name_and_object
test_contains_block
test_len_counts_entries_not_slots

test_type_mismatch_tag_raises
test_type_mismatch_block_raises
test_block_size_mismatch_raises
test_address_conflict_tag_tag_raises
test_address_conflict_tag_block_raises
test_address_conflict_block_block_raises
test_name_conflict_standalone_tags_raises

test_override_block_slot_name
test_override_block_slot_default
test_override_block_slot_retentive
test_override_requires_mapped_slot
test_override_standalone_by_name_key
test_clear_override

test_nickname_validation_warnings_standalone
test_nickname_validation_warnings_block_slots
test_nickname_validation_warns_leading_underscore
test_effective_nickname_collision_raises

test_to_nickname_file_sparse_only_mapped_rows
test_to_nickname_file_uses_override_metadata
test_from_nickname_file_round_trip
test_from_nickname_file_sparse_block_rows_preserve_full_span
```

Also update core tests as needed for `Block` identity semantics.

---

## 8. Verification

```bash
make lint
make test
```

All existing tests must pass. New tests cover `TagMap`, override bridge behavior, validation, and CSV round-trip.

---

## 9. Out of Scope

- `ClickDataProvider` runtime bridge details
- Click instruction compatibility validator details (pointer/expression restrictions)
- XD/YD addressing model decisions
- SC/SD read-only policy decisions
