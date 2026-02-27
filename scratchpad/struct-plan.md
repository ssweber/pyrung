# Struct / PackedStruct Design Plan (Refined)

## Problem

Click projects often need repeated records (alarms, recipes, batch data). Building these manually means:

- Reserving parallel slices in multiple banks
- Keeping naming/indexing consistent
- Leaving growth room without breaking existing address contracts

The goal is to introduce two factories:

- `Struct`: mixed-type records, field-grouped layout
- `PackedStruct`: single-type records, instance-grouped layout

Both must compile down to existing `Tag`, `Block`, and `MappingEntry` primitives.

---

## Hard Constraints from Current Code

1. `TagMap` currently accepts only:
   - `Tag -> Tag`
   - `Block -> BlockRange`
2. `MappingEntry` is `source: Tag | Block`, `target: Tag | BlockRange`.
3. `BlockRange` only models start/end windows; it does not natively model strided lists.
4. `TagMap.from_nickname_file()` reconstructs block spans from open/close block comments.
   Sparse block comments with large interior gaps will round-trip as dense spans.

Implication: `PackedStruct` interleaving must avoid depending on sparse `BlockRange` block comments unless TagMap import/export is also changed.

---

## API Surface

### Field descriptor

```python
from dataclasses import dataclass
from typing import Any
from pyrung.core import TagType


UNSET = object()


@dataclass(frozen=True)
class Field:
    # Required in Struct, ignored in PackedStruct
    type: TagType | None = None
    # UNSET means "use tag-type default"
    default: Any = UNSET
    retentive: bool = False
```

Default semantics:

- `default=UNSET`: use existing `Tag` type default behavior
- `default=<literal>`: fixed per instance
- `default=auto(...)`: per-instance enumerated default

### Auto defaults

```python
@dataclass(frozen=True)
class AutoDefault:
    start: int = 1
    step: int = 1


def auto(*, start: int = 1, step: int = 1) -> AutoDefault: ...
```

Resolution:

```python
def resolve_default(spec: object, index: int) -> object:
    if isinstance(spec, AutoDefault):
        return spec.start + (index - 1) * spec.step
    if spec is UNSET:
        return None  # let Tag.__post_init__ apply type default
    return spec
```

Validation rule: `AutoDefault` is allowed only for numeric tag types (`INT`, `DINT`, `WORD`).

---

## Struct (Mixed Type, Field Grouped)

### Declaration

```python
Alarm = Struct(
    "Alarm",
    count=10,
    id=Field(TagType.INT, default=auto(), retentive=True),
    val=Field(TagType.INT, default=0),
    Time=Field(TagType.INT),
    On=Field(TagType.BOOL, retentive=True),
)
```

### Access patterns

```python
Alarm.id        # Block of 10 INT tags
Alarm.On        # Block of 10 BOOL tags
Alarm[1].id     # Tag("Alarm1_id", ...)
Alarm[3].On     # Tag("Alarm3_On", ...)
```

Name pattern: `{StructName}{Index}_{FieldName}`.

### Mapping

Each field is a normal `Block`, mapped normally:

```python
TagMap({
    Alarm.id: ds.select(1001, 1010),
    Alarm.val: ds.select(1011, 1020),
    Alarm.Time: ds.select(1021, 1030),
    Alarm.On: c.select(1, 10),
})
```

CSV block markers stay per field (`<Alarm.id> ... </Alarm.id>`), because these are true block mappings.

---

## PackedStruct (Single Type, Instance Grouped)

### Declaration

```python
AlarmInts = PackedStruct(
    "Alarm",
    TagType.INT,
    count=10,
    pad=2,
    id=Field(default=auto(), retentive=True),
    val=Field(default=0),
    Time=Field(),
)
```

`pad=N` appends `empty1..emptyN` as real fields.

Effective record width:

`width = len(user_fields) + pad`

### Access patterns

```python
AlarmInts.id
AlarmInts.empty1
AlarmInts[1].id
```

### Mapping contract

`PackedStruct.map_to(target_range)` returns `list[MappingEntry]`.

Decision:

- If `width == 1`: emit one normal block mapping (`Block -> BlockRange`).
- If `width > 1`: emit per-slot mappings (`Tag -> Tag`) to represent interleaving without TagMap changes.

This keeps TagMap unchanged and avoids sparse block-comment round-trip distortion.

Example:

```python
mapping = TagMap([
    *AlarmInts.map_to(ds.select(1001, 1050)),  # 10 * (3 + 2) = 50
])
```

Interleaving algorithm for `width > 1`:

```python
hw_addrs = tuple(target.addresses)
expected = count * width
if len(hw_addrs) != expected:
    raise ValueError(...)

entries = []
for i in range(1, count + 1):
    base = (i - 1) * width
    for offset, field_name in enumerate(field_order):
        logical = blocks[field_name][i]               # Tag
        hardware = target.block[hw_addrs[base + offset]]  # Tag
        entries.append(logical.map_to(hardware))
return entries
```

Tradeoff for `width > 1`:

- `TagMap.resolve(AlarmInts.id, index)` is not available (field block is not mapped as a block entry).
- `TagMap.resolve(AlarmInts[index].id)` works.
- CSV export uses standalone rows (no field block markers) for those entries.

---

## Internals

### Block change: per-index default factory

Extend `Block`:

```python
default_factory: Callable[[int], Any] | None = None
```

Tag creation logic:

- For `addr`, compute `default = default_factory(addr)` when provided
- Pass `default` into `Tag(...)`
- Cache still keyed by address, so defaults remain deterministic per slot

Important: `InputBlock` and `OutputBlock` override `_get_tag`; they must also honor `default_factory` for consistency.

### New module

Add `src/pyrung/core/struct.py` with:

- `Field`
- `AutoDefault`, `auto()`, `resolve_default()`
- `Struct`
- `PackedStruct`
- `InstanceView`

### `Struct` validation

- `name` must be non-empty
- `count` must be `int >= 1`
- at least one field is required
- every field value must be a `Field`
- every field in `Struct` must have `type is not None`
- field names may not collide with reserved API names (`map_to`, `fields`, etc.)
- `auto()` only on numeric types

### `PackedStruct` validation

- `name` non-empty
- `count >= 1`
- `pad >= 0`
- at least one user field
- each field value must be `Field`
- `Field.type` is rejected (PackedStruct type is defined only at the class level)
- `pad` names (`empty1..`) must not collide with user fields
- `auto()` only on numeric `PackedStruct` base type

### `InstanceView`

```python
class InstanceView:
    def __init__(self, owner, index: int): ...
    def __getattr__(self, field_name: str) -> Tag: ...
```

- Index is validated on `__getitem__`
- Unknown field access raises `AttributeError` (not `KeyError`)

---

## TagMap Interaction

No TagMap code changes required.

`Struct` mappings are standard block mappings.

`PackedStruct` mappings:

- `width == 1`: standard block mapping
- `width > 1`: expanded `Tag -> Tag` mappings

Override behavior:

```python
mapping.override(Alarm[7].id, name="SpecialAlarm_id")
mapping.override(AlarmInts[7].id, default=999)
```

Works in both cases because overrides already support mapped standalone tags and block slots.

---

## Implementation Order

1. Add `default_factory` support in `Block` / `InputBlock` / `OutputBlock`.
2. Add `core/struct.py` with `Field`, `AutoDefault`, and helpers.
3. Implement `Struct` with block-per-field generation and `InstanceView`.
4. Implement `PackedStruct` with `pad`, accessors, and `map_to`.
5. Export new symbols from `src/pyrung/core/__init__.py`.
6. Add tests.

---

## Tests

### Core (`tests/core/test_struct.py`)

- `Struct` builds blocks with correct types
- `Struct` tags use `{Name}{Index}_{Field}` names
- `Struct` literal defaults and `auto()` defaults resolve correctly
- `Struct` retentive policy is inherited by generated tags
- invalid declarations raise (`count`, missing type, bad field value)
- `InstanceView` index bounds and missing attribute behavior

### Packed (`tests/core/test_packed_struct.py`)

- `pad` generates `empty1..emptyN`
- width calculation and range-length validation
- interleaved `map_to` output addresses are correct
- `width == 1` path emits block mapping
- `width > 1` path emits per-slot tag mappings
- `auto()` restrictions by type

### Click mapping integration (`tests/click/test_struct_mapping.py`)

- `TagMap.resolve(Alarm[2].id)` and `TagMap.resolve(Alarm.id, 2)` for `Struct`
- `TagMap.resolve(AlarmInts[2].id)` for `PackedStruct width > 1`
- CSV export contains expected slot names/defaults/retentive values

---

## Decided Points

1. Indexing is 1-based.
2. Name format is fixed: `{StructName}{Index}_{FieldName}`.
3. Keep `Struct` and `PackedStruct` as separate classes.
4. Keep TagMap unchanged.
5. `PackedStruct` interleaving uses `Tag -> Tag` expansion when width > 1.
6. `pad` fields are real named fields (`empty1..emptyN`).
7. Per-slot metadata remains a `TagMap.override(...)` concern.
