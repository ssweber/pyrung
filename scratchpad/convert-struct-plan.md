# Convert Struct/PackedStruct to @plc_block/@plc_pack Decorators

## Context

The current `Struct` and `PackedStruct` builder APIs use an imperative style:
```python
alarms = Struct("Alarm", count=3, id=Field(TagType.INT), on=Field(TagType.BOOL))
```
This plan replaces them with dataclass-style decorator declarations:
```python
@plc_block(count=3)
class Alarm:
    id: Int = auto()
    on: Bool
```
This is more Pythonic, more readable, and makes `Int`/`Bool` proper classes (valid type annotations).

## Decisions

- **Class-based `Int`/`Bool`**: Refactor from functions to classes using `__new__`
- **No DT namespace**: Class names are the type markers
- **`Field()` for retentive**: `id: Int = Field(default=auto(), retentive=True)`
- **`int` maps to `INT`** (PLC convention)
- **Remove Struct/PackedStruct outright** (no deprecation period)
- **Runtime also accepts**: Python primitives (`bool`, `int`, `float`, `str`) and IEC strings

## Target API

```python
# Mixed-type (field-grouped)
@plc_block(count=3)
class Alarm:
    id: Int = auto(start=10, step=5)
    on: Bool
    val: Int = 7

Alarm[1].id       # LiveTag "Alarm1_id"
Alarm.id          # Block
Alarm.field_names # ("id", "on", "val")

# Single-type (instance-interleaved)
@plc_pack(Int, count=2, pad=1)
class Data:
    id: Int = auto()
    val: Int = 0

Data.map_to(ds.select(1, 6))  # packed mapping
```

---

## Step 1: Refactor Int/Bool/etc. to classes

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

Add helper to resolve annotations to `TagType`:

```python
def _resolve_annotation(annotation, field_name: str) -> TagType:
    # _TagTypeBase subclass → annotation._tag_type
    # bool → BOOL, int → INT, float → REAL, str → CHAR
    # str annotation "Int" etc. → mapped TagType
    # else → TypeError
```

Also add internal `_FieldSpec` dataclass for parsed field metadata.

## Step 3: Implement @plc_block decorator

**File**: `src/pyrung/core/struct.py`

`plc_block(*, count=1)` → decorator that:
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

## Step 4: Implement @plc_pack decorator

**File**: `src/pyrung/core/struct.py`

`plc_pack(base_type, *, count=1, pad=0)` → decorator that:
1. Resolves `base_type` (must be `_TagTypeBase` subclass)
2. Same annotation/default parsing as plc_block
3. Validates all fields match base type (annotations present but type comes from decorator arg)
4. Adds padding fields (`empty1..emptyN`) with collision check
5. Returns `_PackedStructRuntime` instance

`_PackedStructRuntime` extends `_StructRuntime` behavior with:
- `.type`, `.pad`, `.width`
- `.map_to(target)` — copy exact logic from current `PackedStruct.map_to()`

## Step 5: Remove Struct/PackedStruct

**File**: `src/pyrung/core/struct.py`
- Delete `Struct` class
- Delete `PackedStruct` class
- Keep: `Field`, `AutoDefault`, `auto`, `InstanceView`, `resolve_default`, internal helpers

## Step 6: Update exports

**File**: `src/pyrung/core/__init__.py`
- Remove `Struct`, `PackedStruct` from imports and `__all__`
- Add `plc_block`, `plc_pack` to imports and `__all__`

**File**: `src/pyrung/click/__init__.py`
- No changes needed (aliases `Bit = Bool` etc. still work since Bool is now a class)

## Step 7: Rewrite tests

**Delete and replace**:
- `tests/core/test_struct.py` → `tests/core/test_plc_block.py`
- `tests/core/test_packed_struct.py` → `tests/core/test_plc_pack.py`
- `tests/click/test_struct_mapping.py` → `tests/click/test_plc_mapping.py`

**Add new**:
- `tests/core/test_tag_type_classes.py` — test Int/Bool as classes (constructor, annotation, TagNamespace, errors)

**Update in place**:
- `tests/core/test_live_tag_proxy.py` — change Struct/PackedStruct usage to @plc_block/@plc_pack

Port all test cases 1:1, just change syntax from builder to decorator style.

## Step 8: Update scratchpad

- Update `scratchpad/convert-struct-plan.md` to reflect finalized decisions (mark pending decision as resolved)

---

## Files Modified (summary)

| File | Action |
|------|--------|
| `src/pyrung/core/tag.py` | Refactor Int/Bool/etc. from functions to classes |
| `src/pyrung/core/struct.py` | Add decorators, remove Struct/PackedStruct |
| `src/pyrung/core/__init__.py` | Update exports |
| `tests/core/test_struct.py` | Delete (replaced by test_plc_block.py) |
| `tests/core/test_packed_struct.py` | Delete (replaced by test_plc_pack.py) |
| `tests/core/test_plc_block.py` | New — port struct tests |
| `tests/core/test_plc_pack.py` | New — port packed struct tests |
| `tests/core/test_tag_type_classes.py` | New — Int/Bool class tests |
| `tests/core/test_live_tag_proxy.py` | Update struct usage |
| `tests/click/test_struct_mapping.py` | Delete (replaced) |
| `tests/click/test_plc_mapping.py` | New — port mapping tests |

## Verification

1. `make test` — all tests pass
2. `make lint` — no lint/type errors
3. Verify tag constructor behavior: `Int("name")` returns LiveTag
4. Verify TagNamespace: `x = Int()` auto-names in class body
5. Verify decorator: `@plc_block(count=3)` creates correct blocks/tags
6. Verify packed mapping: `@plc_pack(Int, count=2, pad=1)` + `map_to()` works
7. Verify Click integration: TagMap resolves decorated struct fields
