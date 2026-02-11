# Struct Design Exploration

## Problem

Setting up multi-field records (alarms, recipes, batch data) on a Click PLC means manually reserving parallel slices across different memory banks, maintaining naming conventions, and leaving room for future fields. This is the most tedious part of Click project setup.

Struct automates this. It works like a dataclass factory — you define fields with types and policies once, stamp out instances, and assign each attribute to a hardware range independently.

---

## Core API

```python
Alarm = Struct("Alarm", count=10,
    id   = Int(default=auto(), retentive=True),  # 1, 2, 3, ...
    val  = Int(default=0),
    Time = Int(),
    On   = Bool(retentive=True),
)
```

### Field factories

Struct fields are declared like dataclass fields: typed factory + policy metadata.

```python
Int(default=0, retentive=False)
Bool(default=False, retentive=True)
Int(default=auto(start=1, step=1))
```

`default` modes:
- Literal default (`default=0`) applies to all instances in the field.
- Enumerated default (`default=auto(...)`) is resolved per instance index.
  Example: `Alarm[i].id.default == i` for `auto(start=1, step=1)`.

### Instance access

`Alarm[i]` returns an instance object with field attributes:

```python
Alarm[1].id    # → Tag("Alarm1_id", INT, default=1, retentive=True)
Alarm[3].id    # → Tag("Alarm3_id", INT, default=3, retentive=True)
Alarm[1].val   # → Tag("Alarm1_val", INT, default=0)
Alarm[3].On    # → Tag("Alarm3_On", BOOL, retentive=True)
```

Name pattern: `{StructName}{Index}_{FieldName}` — e.g., `Alarm1_id`, `Alarm3_On`.

### Attribute access

Each attribute surfaces as a Block — all instances of that field:

```python
Alarm.id       # → Block of 10 Ints: Alarm1_id, Alarm2_id, ... Alarm10_id
Alarm.val      # → Block of 10 Ints: Alarm1_val, ...
Alarm.On       # → Block of 10 Bools: Alarm1_On, ...
```

These Blocks carry the policy (retentive, default) from the field declaration. Every tag in `Alarm.id` inherits `retentive=True`.

### Hardware assignment

In the field-grouped layout, each attribute is assigned independently, like any other Block:

```python
TagMap({
    Alarm.id:   ds.select(1001, 1010),
    Alarm.val:  ds.select(1011, 1020),
    Alarm.Time: ds.select(1021, 1030),
    Alarm.On:   c.select(1, 10),
})
```

CSV block tags stay per attribute block, not per Struct:
`<Alarm.id>...</Alarm.id>`, `<Alarm.val>...</Alarm.val>`, `<Alarm.Time>...</Alarm.Time>`,
`<Alarm.On>...</Alarm.On>`.

Export produces:
```
DS1001: Alarm1_id   (retentive)       C1:  Alarm1_On  (retentive)
DS1002: Alarm2_id   (retentive)       C2:  Alarm2_On  (retentive)
...                                   ...
DS1011: Alarm1_val  (default=0)
DS1012: Alarm2_val  (default=0)
...
DS1021: Alarm1_Time
DS1022: Alarm2_Time
...
```

---

## How It Works Internally

Struct is a tag factory that produces ordinary Blocks. By the time anything reaches TagMap, Struct has done its job.

```python
@dataclass(frozen=True)
class AutoDefault:
    start: int = 1
    step: int = 1

def auto(*, start: int = 1, step: int = 1) -> AutoDefault: ...

def resolve_default(spec, index: int):
    if isinstance(spec, AutoDefault):
        return spec.start + (index - 1) * spec.step
    return spec

class Struct:
    def __init__(self, name: str, count: int, **fields):
        self._name = name
        self._count = count
        self._fields = fields  # {"id": Int(retentive=True), ...}
        self._blocks = {}      # {"id": Block(...), "On": Block(...), ...}

        for field_name, field_def in fields.items():
            block = Block(
                f"{name}.{field_name}",
                field_def.type,
                start=1,
                end=count,
                retentive=field_def.retentive,
                address_formatter=lambda _prefix, i, fn=field_name: f"{name}{i}_{fn}",
            )
            # Tags are immutable; create per-slot defaults when slot tags are first materialized.
            # auto() defaults are index-based (1..count by default).
            self._blocks[field_name] = block
```

`Alarm.id` returns the Block. `Alarm[1]` returns a lightweight instance view that routes `.id` → `Alarm.id[1]`.
Struct never mutates `Tag` objects after creation.

### Layer separation

```
Struct (tag factory)
  → produces Blocks with patterned names and per-field policies
      → Blocks feed into TagMap as normal
          → TagMap exports to CSV (sparse, per .name)
```

TagMap never knows about Struct. It just sees Blocks.

---

## Two Layout Options

Both are supported. The choice is about how you assign hardware, not how Struct works.

### Field-grouped (default, simple)

Each attribute gets its own contiguous range. Recommended when you don't need pointer-level record copies:

```python
TagMap({
    Alarm.id:   ds.select(1001, 1010),
    Alarm.val:  ds.select(1011, 1020),
    Alarm.Time: ds.select(1021, 1030),
    Alarm.On:   c.select(1, 10),
})
```

Memory layout:
```
DS1001..1010: all Alarm ids
DS1011..1020: all Alarm vals
DS1021..1030: all Alarm Times
C1..10:       all Alarm On bits
```

### Instance-grouped (with stride, for pointer operations)

All fields for one instance are contiguous within each bank. Enables `blockcopy` of a full record. Requires a stride to leave room for future fields.

Design decision: use separate typed Struct groups per logical record family (for example `AlarmInts`, `AlarmBits`) rather than a mixed-type packed accessor like `Alarm.ints`.

```python
AlarmInts = Struct("Alarm", count=10, stride=5,
    id   = Int(default=auto(), retentive=True),
    val  = Int(default=0),
    Time = Int(),
)

AlarmBits = Struct("Alarm", count=10,
    On   = Bool(retentive=True),
)

TagMap({
    AlarmInts:      ds.select(1001, 1050),       # full stride span for INT fields
    AlarmBits.On:   c.select(1, 10),
})
```

`AlarmInts` expands internally to per-attribute mappings based on field order/offset. The selected range is validated before mapping:
- all attributes must fit in the declared stride
- total required slots (`count * stride`) must fit inside the selected range

Memory layout:
```
DS1001: Alarm1_id       DS1006: Alarm2_id       C1: Alarm1_On
DS1002: Alarm1_val      DS1007: Alarm2_val      C2: Alarm2_On
DS1003: Alarm1_Time     DS1008: Alarm2_Time     ...
DS1004: (spare)         DS1009: (spare)
DS1005: (spare)         DS1010: (spare)
```

Copying alarm 3 to history: `blockcopy(base + 2*stride, stride, dest)`.

Stride keeps a simple assignment model: select one full range for each typed Struct group, then validate it can contain every attribute slot.

---

## Per-Instance Override

Even with Struct defaults, per-instance export metadata is applied at the mapping layer:

```python
mapping = TagMap({
    Alarm.id:   ds.select(1001, 1010),
    Alarm.val:  ds.select(1011, 1020),
    Alarm.Time: ds.select(1021, 1030),
    Alarm.On:   c.select(1, 10),
})

mapping.override(Alarm[7].id, name="SpecialAlarm_id")  # custom nickname
mapping.override(Alarm[7].val, default=999)            # custom default
```

`Tag` objects remain immutable. `TagMap.override()` controls export/validation metadata for mapped slots.

---

## Relationship to Existing Concepts

| Concept | What it is | Who uses it |
|---------|-----------|-------------|
| Tag | Named value with type, retentive, default | Engine (logic), TagMap (export) |
| Block | Indexed collection of Tags | Engine (logic), TagMap (export) |
| Struct | Factory that produces Blocks with patterned names | User (setup), produces Blocks |
| TagMap | Maps Tags/Blocks to hardware addresses | Export (CSV), validation |

Struct adds no new concepts to the mapping layer. It's purely a convenience for the user.

---

## Open Questions

1. **Stride range assignment (decided)**: Map a full hardware span for each typed Struct group (for example `AlarmInts: ds.select(...)`) and validate that all attribute slots fit.

2. **Naming separator**: `Alarm1_id` uses underscore. Should this be configurable? Dot (`Alarm1.id`) would conflict with the attribute access syntax.

3. **Instance numbering (decided)**: Struct indices are 1-based.

4. **BlockTag markers in CSV (decided)**: Use per-attribute wrapping only:
   `<Alarm.id>...</Alarm.id>`, `<Alarm.val>...</Alarm.val>`, `<Alarm.Time>...</Alarm.Time>`,
   `<Alarm.On>...</Alarm.On>`. Do not wrap multiple attribute blocks under `<Alarm>...</Alarm>`.

5. **Typed group model (decided)**: Use separate Struct groups for each bank/type family (for example `AlarmInts`, `AlarmBits`). Do not add a mixed-type `Alarm.ints` accessor.
