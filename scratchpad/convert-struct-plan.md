# Convert Struct/PackedStruct to @udt/@named_array Decorators

> Status (2026-02-14): Implemented. Finalized decision: use `@udt` and `@named_array` with `stride`-based gaps and no dummy "empty" fields.

## Context

The current `Struct` and `PackedStruct` builder APIs use an imperative style:
```python
alarms = Struct("Alarm", count=3, id=Field(TagType.INT), on=Field(TagType.BOOL))
```
This plan replaces them with dataclass-style decorator declarations:
```python
@udt(count=3)
class Alarm:
    id: Int = auto()
    on: Bool
```
This is more Pythonic, more readable, and makes the tag types (`Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`) proper classes usable as type annotations.

## Decisions

- **Class-based tag types**: Refactor all 6 tag constructors (`Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`) from functions to classes using `__new__`
- **No DT namespace**: Class names are the type markers
- **`Field()` for retentive**: `id: Int = Field(default=auto(), retentive=True)`
- **`int` maps to `INT`** (PLC convention)
- **Remove Struct/PackedStruct outright** (no deprecation period)
- **Runtime also accepts**: Python primitives (`bool`, `int`, `float`, `str`) and IEC strings
- **Naming**: `@udt` (User Defined Type) for mixed-type structures, `@named_array` for single-type (instance-interleaved) structures. Both use snake_case to match Python decorator convention (`@dataclass`, `@contextmanager`).
- **`stride` parameter**: The per-instance footprint in memory — distance from the start of one instance to the start of the next. Replaces the former `pad` concept. `@named_array(Int, count=10, stride=5)` means each instance occupies 5 slots (e.g., 1 defined field + 4 unmapped gaps).
- **No "Empty" fields**: The runtime uses `stride` strictly for calculating memory offsets. Gaps between defined fields remain unmapped and do not generate dummy Python attributes.
- **No annotations in `@named_array`**: Since the decorator's `base_type` argument already defines the type for all fields, annotations are not used. Fields are declared as bare names with optional defaults.

## Target API

```python
# Mixed-type (field-grouped) — "User Defined Type"
@udt(count=3)
class Alarm:
    id: Int = auto(start=10, step=5)
    on: Bool
    val: Int = 7

Alarm[1].id       # LiveTag "Alarm1_id"
Alarm.id          # Block
Alarm.field_names # ("id", "on", "val")

# Single-type (instance-interleaved) — "Named Array"
@named_array(Int, count=2, stride=2)
class Data:
    id = auto()
    val = 0

Data.map_to(ds.select(1, 6))  # packed mapping
```

---

## Step 1: Refactor tag type constructors to classes

**File**: `src/pyrung/core/tag.py`

Replace the 6 tag constructor functions (`Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`) and the `_tag_or_decl()` helper with classes using `__new__`:

```python
class _TagTypeBase:
    """Base for tag type marker classes."""
    _tag_type: ClassVar[TagType]
    _default_retentive: ClassVar[bool]

    def __new__(cls, name: str | None = None, retentive: bool | None = None):
        if retentive is None:
            retentive = cls._default_retentive
        if name is not None:
            return LiveTag(name, cls._tag_type, retentive)
        if not _is_class_declaration_context(stack_depth=2):
            raise TypeError(
                f"{cls.__name__}() without a name is only valid in a TagNamespace class body."
            )
        return _AutoTagDecl(cls._tag_type, retentive)

class Bool(_TagTypeBase):
    _tag_type = TagType.BOOL
    _default_retentive = False

class Int(_TagTypeBase):
    _tag_type = TagType.INT
    _default_retentive = True

class Dint(_TagTypeBase):
    _tag_type = TagType.DINT
    _default_retentive = True

class Real(_TagTypeBase):
    _tag_type = TagType.REAL
    _default_retentive = True

class Word(_TagTypeBase):
    _tag_type = TagType.WORD
    _default_retentive = False

class Char(_TagTypeBase):
    _tag_type = TagType.CHAR
    _default_retentive = True
```

Click aliases (`Bit = Bool`, `Int2 = Dint`, `Float = Real`, `Hex = Word`, `Txt = Char`) remain unchanged in `pyrung.click.__init__` — they just point to these classes now.

- Remove `_tag_or_decl()` function
- Remove all 6 function definitions (`Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`)
- Keep `_AutoTagDecl`, `_is_class_declaration_context` unchanged
- Adjust `stack_depth` in `_is_class_declaration_context` call if needed (test to verify)

**Verify**: `make test` — all existing TagNamespace and tag constructor tests pass.

## Step 2: Add type annotation resolver

**File**: `src/pyrung/core/struct.py`

Add helper to resolve annotations to `TagType` (used by `@udt` only; `@named_array` gets its type from the decorator's `base_type` argument):

```python
def _resolve_annotation(annotation, field_name: str) -> TagType:
    # _TagTypeBase subclass → annotation._tag_type
    # bool → BOOL, int → INT, float → REAL, str → CHAR
    # str annotation "Int" etc. → mapped TagType
    # else → TypeError
```

Also add internal `_FieldSpec` dataclass for parsed field metadata.

## Step 3: Implement @udt decorator

**File**: `src/pyrung/core/struct.py`

`udt(*, count=1)` → decorator that:
1. Reads `cls.__annotations__` for field types
2. Reads `cls.__dict__` for defaults (literal, `auto()`, `Field()`)
3. Validates field names, count, auto() compatibility
4. Creates `Block` per field (reuse `_make_formatter`, `_make_default_factory`)
5. Returns a `_StructRuntime` instance (not the class)

`_StructRuntime` mirrors current `Struct`:
- `.name`, `.count`, `.fields`, `.field_names`
- `[index]` → `InstanceView`
- `.field_name` → `Block` (via `__getattr__`)

Reuse from current code: `_make_formatter`, `_make_default_factory`, `resolve_default`, `_RESERVED_FIELD_NAMES`, `_NUMERIC_TYPES`, `InstanceView`.

## Step 4: Implement @named_array decorator

**File**: `src/pyrung/core/struct.py`

`named_array(base_type, *, count=1, stride=1)` → decorator that:
1. Resolves `base_type` (must be `_TagTypeBase` subclass)
2. Reads field names and defaults from `cls.__dict__` (no `__annotations__` — type comes from `base_type`)
3. Filters out dunder and private names to get the declared field list
4. Uses `stride` strictly for offset calculation: instance *n* starts at `n * stride`. Gaps between defined fields remain unmapped (no dummy attributes)
5. Returns `_NamedArrayRuntime` instance

`_NamedArrayRuntime` extends `_StructRuntime` behavior with:
- `.type`, `.stride`
- `.map_to(target)` — copy exact logic from current `PackedStruct.map_to()`

## Step 5: Remove Struct/PackedStruct

**File**: `src/pyrung/core/struct.py`
- Delete `Struct` class
- Delete `PackedStruct` class
- Keep: `Field`, `AutoDefault`, `auto`, `InstanceView`, `resolve_default`, internal helpers

## Step 6: Update exports

**File**: `src/pyrung/core/__init__.py`
- Remove `Struct`, `PackedStruct` from imports and `__all__`
- Add `udt`, `named_array` to imports and `__all__`

**File**: `src/pyrung/click/__init__.py`
- No changes needed (aliases `Bit = Bool` etc. still work since Bool is now a class)

## Step 7: Rewrite tests

**Delete and replace**:
- `tests/core/test_struct.py` → `tests/core/test_udt.py`
- `tests/core/test_packed_struct.py` → `tests/core/test_named_array.py`
- `tests/click/test_struct_mapping.py` → `tests/click/test_plc_mapping.py`

**Add new**:
- `tests/core/test_tag_type_classes.py` — test all 6 tag type classes (constructor, annotation, TagNamespace, errors) and primitive mappings (`bool`→`BOOL`, `int`→`INT`, `float`→`REAL`, `str`→`CHAR`)

**Update in place**:
- `tests/core/test_live_tag_proxy.py` — change Struct/PackedStruct usage to @udt/@named_array

Port all test cases 1:1, just change syntax from builder to decorator style.

## Step 8: Update scratchpad

- Update `scratchpad/convert-struct-plan.md` to reflect finalized decisions (mark pending decision as resolved)

---

## Files Modified (summary)

| File | Action |
|------|--------|
| `src/pyrung/core/tag.py` | Refactor tag type constructors from functions to classes |
| `src/pyrung/core/struct.py` | Add decorators, remove Struct/PackedStruct |
| `src/pyrung/core/__init__.py` | Update exports |
| `tests/core/test_struct.py` | Delete (replaced by test_udt.py) |
| `tests/core/test_packed_struct.py` | Delete (replaced by test_named_array.py) |
| `tests/core/test_udt.py` | New — port struct tests |
| `tests/core/test_named_array.py` | New — port packed struct tests |
| `tests/core/test_tag_type_classes.py` | New — tag type class + primitive mapping tests |
| `tests/core/test_live_tag_proxy.py` | Update struct usage |
| `tests/click/test_struct_mapping.py` | Delete (replaced) |
| `tests/click/test_plc_mapping.py` | New — port mapping tests |

## Verification

1. `make test` — all tests pass
2. `make lint` — no lint/type errors
3. Verify tag constructor behavior: `Int("name")` returns LiveTag
4. Verify TagNamespace: `x = Int()` auto-names in class body
5. Verify decorator: `@udt(count=3)` creates correct blocks/tags
6. Verify named array: `@named_array(Int, count=2, stride=2)` + `map_to()` works
7. Verify Click integration: TagMap resolves decorated struct fields
