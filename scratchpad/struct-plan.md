# Struct Design Exploration

## Problem

Setting up multi-field records (alarms, recipes, batch data) on a Click PLC means manually reserving parallel slices across different memory banks, maintaining naming conventions, and leaving room for future fields. This is the most tedious part of Click project setup.

Struct automates this. It works like a dataclass factory — you define fields with types and policies once, stamp out instances, and assign each attribute to a hardware range independently.

---

## Core API

```python
Alarm = Struct("Alarm", count=10,
    id   = Int(retentive=True),
    val  = Int(default=0),
    Time = Int(),
    On   = Bool(retentive=True),
)
```

### Instance access

`Alarm[i]` returns an instance object with field attributes:

```python
Alarm[1].id    # → Tag("Alarm1_id", INT, retentive=True)
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

Each attribute is assigned independently, like any other Block:

```python
HardwareAssignments({
    Alarm.id:   ds.select(1001, 1010),
    Alarm.val:  ds.select(1011, 1020),
    Alarm.Time: ds.select(1021, 1030),
    Alarm.On:   c.select(1, 10),
})
```

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

Struct is a tag factory that produces ordinary Blocks. By the time anything reaches HardwareAssignments, Struct has done its job.

```python
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
                default=field_def.default,
            )
            # Set per-tag names: Alarm1_id, Alarm2_id, ...
            for i in range(1, count + 1):
                block[i].name = f"{name}{i}_{field_name}"
            self._blocks[field_name] = block
```

`Alarm.id` returns the Block. `Alarm[1]` returns a lightweight instance view that routes `.id` → `Alarm.id[1]`.

### Layer separation

```
Struct (tag factory)
  → produces Blocks with patterned names and per-field policies
      → Blocks feed into HardwareAssignments as normal
          → HardwareAssignments exports to CSV (sparse, per .name)
```

HardwareAssignments never knows about Struct. It just sees Blocks.

---

## Two Layout Options

Both are supported. The choice is about how you assign hardware, not how Struct works.

### Field-grouped (default, simple)

Each attribute gets its own contiguous range. Recommended when you don't need pointer-level record copies:

```python
HardwareAssignments({
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

All fields for one instance are contiguous within each bank. Enables `blockcopy` of a full record. Requires a stride to leave room for future fields:

```python
Alarm = Struct("Alarm", count=10, stride=5,
    id   = Int(retentive=True),
    val  = Int(default=0),
    Time = Int(),
    On   = Bool(retentive=True),
)

HardwareAssignments({
    Alarm.ints:  ds.start_at(1001),   # DS1001..1050, stride=5
    Alarm.bools: c.select(1, 10),
})
```

Memory layout:
```
DS1001: Alarm1_id       DS1006: Alarm2_id       C1: Alarm1_On
DS1002: Alarm1_val      DS1007: Alarm2_val      C2: Alarm2_On
DS1003: Alarm1_Time     DS1008: Alarm2_Time     ...
DS1004: (spare)         DS1009: (spare)
DS1005: (spare)         DS1010: (spare)
```

Copying alarm 3 to history: `blockcopy(base + 2*stride, stride, dest)`.

The stride version needs more design work — how `Alarm.ints` collapses int fields into one interleaved block, how spare slots are handled at export (omitted, matching sparse export rule), and whether stride is per-bank or global.

---

## Per-Instance Override

Even with Struct defaults, individual tags can be overridden:

```python
Alarm[7].id.name = "SpecialAlarm_id"    # custom name
Alarm[7].val.default = 999              # custom default for this instance
```

This works because Struct produces real Tag objects. Overrides stick on the Tag and flow through to export.

---

## Relationship to Existing Concepts

| Concept | What it is | Who uses it |
|---------|-----------|-------------|
| Tag | Named value with type, retentive, default | Engine (logic), HardwareAssignments (export) |
| Block | Indexed collection of Tags | Engine (logic), HardwareAssignments (export) |
| Struct | Factory that produces Blocks with patterned names | User (setup), produces Blocks |
| HardwareAssignments | Maps Tags/Blocks to hardware addresses | Export (CSV), validation |

Struct adds no new concepts to the mapping layer. It's purely a convenience for the user.

---

## Open Questions

1. **Stride scope**: Per-bank (int stride vs bool stride) or single global stride? Per-bank is more flexible; single is simpler to declare.

2. **Naming separator**: `Alarm1_id` uses underscore. Should this be configurable? Dot (`Alarm1.id`) would conflict with the attribute access syntax.

3. **Instance numbering**: Start at 0 or 1? PLC convention is typically 1-based.

4. **BlockTag markers in CSV**: Should the `<Alarm>...</Alarm>` markers in the nickname file wrap all attributes of a Struct together, or wrap each attribute Block separately? Wrapping all together communicates "these are one concept" but spans multiple memory types.

5. **Deferred: `Alarm.ints` accessor**: For the stride layout, how to expose "all int fields interleaved" as a single Block for assignment. This is the main design challenge of the stride variant.
