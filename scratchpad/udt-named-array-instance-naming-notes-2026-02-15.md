# UDT / Named Array Instance Naming Notes

Date: 2026-02-15

## Question
How to reuse a `@udt` / `@named_array` declaration under different top-level names, and whether per-instance renaming (for example `PumpAlarm = Alarm[1]; PumpAlarm.name = "PumpAlarm"`) is supported.

## Findings
- There is no public rename API for individual `@udt` or `@named_array` instances.
- Struct runtime names are derived from the decorated class name (`cls.__name__`).
- Generated slot/tag names use the pattern `<TopName><index>_<field>` (for example `Alarm1_id`).
- `Alarm[1]` returns an `InstanceView`, not a mutable tag container.
- Assigning `Alarm[1].name = "PumpAlarm"` only adds an attribute on the `InstanceView`; it does not rename underlying tags.
- Tag identity and runtime state access are keyed by `tag.name`, so post-hoc renaming is not a cosmetic-only change in current architecture.

## CSV Export (TagMap) Outcome
- For nickname customization in Click CSV export, use `TagMap.override(slot, name=...)`.
- This is the correct way to emit `PumpAlarm_*` style nicknames without changing logical tag identity.
- Pattern:
  - Alias instance for readability: `PumpAlarm = alarms[1]`
  - Iterate fields and override each slot nickname before `to_nickname_file(...)`.

## Caveat
- CSV block comments (for example `<Alarm.id>`) come from logical block names, not per-slot override nicknames.
- If block comments must reflect aliases (`PumpAlarm`), that requires a feature change.

## Practical Recommendation (Current)
1. Keep logical names stable (`Alarm1_id`, etc.).
2. Apply export-layer naming via `TagMap.override(...)` for CSV output.
3. If reusable top-level logical names are required in core objects, generate separate runtimes from a factory using different class names.

## Factory Pattern (Concrete)
Use dynamic class creation so the decorator sees a different `__name__` each time.

```python
from typing import Any

from pyrung.core import Bool, Int, auto, named_array, udt


def make_alarm_udt(name: str, *, count: int = 3):
    cls = type(name, (), {"__annotations__": {"id": Int, "on": Bool}})
    return udt(count=count)(cls)


def make_alarm_named_array(name: str, *, count: int = 2, stride: int = 3):
    cls = type(name, (), {"id": auto(), "val": 0})
    return named_array(Int, count=count, stride=stride)(cls)


PumpAlarm = make_alarm_udt("PumpAlarm")
MotorAlarm = make_alarm_udt("MotorAlarm")

PumpPacked = make_alarm_named_array("PumpPacked")
MotorPacked = make_alarm_named_array("MotorPacked")

assert PumpAlarm[1].id.name == "PumpAlarm1_id"
assert MotorAlarm[1].id.name == "MotorAlarm1_id"
assert PumpPacked[1].id.name == "PumpPacked1_id"
```

## Potential Feature Directions
- Add a display alias API for `InstanceView` (non-semantic, export/UI only).
- Add a struct clone/factory API for explicit top-level renaming at creation time.
- Extend CSV block comment generation to optionally use overrides/aliases.
