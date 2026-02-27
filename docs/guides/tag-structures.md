# Tag Structures

Advanced patterns for UDTs, named arrays, and block configuration. For basic tag and UDT usage, see [Core Concepts](../getting-started/concepts.md).

## UDT naming

A singleton UDT (`count=1`) generates compact tag names — no instance number:

```python
@udt()
class Motor:
    running: Bool
    speed: Int

# Tags: Motor_running, Motor_speed
```

With `count > 1`, tags are numbered by instance:

```python
@udt(count=3)
class Pump:
    running: Bool
    flow: Real

# Tags: Pump1_running, Pump1_flow, Pump2_running, ...
```

If you want numbered names even for a singleton, use `numbered=True`:

```python
@udt(count=1, numbered=True)
class Heater:
    on: Bool
    temp: Real

# Tags: Heater1_on, Heater1_temp (not Heater_on)
```

This is useful when your naming convention requires consistency across singletons and counted structures.

## Field options

Plain annotations give you the type default (0 for `Int`, `False` for `Bool`, etc.). `Field` lets you override:

```python
@udt(count=3)
class Alarm:
    id: Int = Field(default=100)
    active: Bool
    message: Char = Field(retentive=True)
```

`retentive=True` means the field survives a STOP→RUN transition. By default, UDT fields inherit the type's retentive policy.

You can also assign a plain literal as a default:

```python
@udt()
class Config:
    mode: Int = 2
    threshold: Real = 75.0
```

### Per-instance sequences with auto()

`auto()` generates a different default for each instance — useful for IDs and addresses:

```python
@udt(count=3)
class Alarm:
    id: Int = auto(start=10, step=5)
    active: Bool

# Alarm[1].id defaults to 10
# Alarm[2].id defaults to 15
# Alarm[3].id defaults to 20
```

`auto()` only works on numeric types: `Int`, `Dint`, `Word`.

## Named arrays

A `@named_array` is a single-type structure where all fields share the same `TagType`. Fields are declared as class attributes (not annotations):

```python
from pyrung import named_array

@named_array(Int, count=4)
class Sensor:
    reading = 0
    setpoint = 100
```

This creates 4 instances, each with an `Int`-typed `reading` and `setpoint`. Access works the same as UDTs:

```python
Sensor[1].reading   # first sensor's reading
Sensor[3].setpoint  # third sensor's setpoint
```

### Stride

`stride` controls how many hardware slots each instance spans. When stride exceeds the field count, the extra slots are gaps:

```python
@named_array(Int, count=2, stride=4)
class DataPack:
    id = auto()
    value = 0
```

Instance 1 occupies slots 1–4, instance 2 occupies slots 5–8. Only slots 1–2 and 5–6 hold named fields; slots 3–4 and 7–8 are gaps. This matters when mapping to hardware with fixed slot widths.

## Cloning

`.clone()` creates an independent copy of a structure with a new name. Same field layout, fresh tags:

```python
@udt(count=2)
class Motor:
    running: Bool
    speed: Int

Pump = Motor.clone("Pump")           # Same layout, count=2
Fan = Motor.clone("Fan", count=4)    # Same layout, 4 instances
```

For named arrays, you can also override stride:

```python
@named_array(Int, count=2, stride=3)
class Slot:
    id = auto()
    value = 0

WideSlot = Slot.clone("WideSlot", stride=5)
```

Use case: define a template structure once, clone it for each subsystem.

## Mapping to hardware

Named arrays can map their interleaved layout onto a hardware block range with `.map_to()`:

```python
from pyrung.click import ds

@named_array(Int, count=3, stride=2)
class Channel:
    id = auto()
    value = 0

entries = Channel.map_to(ds.select(101, 106))
```

This maps:

- Channel[1].id → DS101, Channel[1].value → DS102
- Channel[2].id → DS103, Channel[2].value → DS104
- Channel[3].id → DS105, Channel[3].value → DS106

The target range must have exactly `count * stride` addresses. Each instance claims `stride` consecutive slots, with fields filling from the front and gaps (if any) at the end.

For UDTs, use `TagMap` to map individual field blocks — see the [Click Dialect](../dialects/click.md) guide.

## Block configuration

Blocks support per-slot overrides for name, retentive policy, and default value. All configuration must happen **before** you index the slot (before tag materialization).

### rename_slot

Give a slot a human-readable name:

```python
ds = Block("DS", TagType.INT, 1, 100)
ds.rename_slot(1, "SpeedCommand")
ds.rename_slot(2, "SpeedFeedback")

ds[1].name   # "SpeedCommand"
ds[2].name   # "SpeedFeedback"
ds[3].name   # "DS3" (default)
```

### configure_slot and configure_range

Override retentive policy or default value for individual slots or ranges:

```python
ds.configure_slot(10, retentive=True, default=999)
ds.configure_range(20, 30, retentive=True)
```

`configure_range` applies to all valid addresses in the inclusive window. For sparse blocks, it only affects addresses within the block's valid ranges.

### default_factory

Set a function that computes defaults by address when creating a block:

```python
ds = Block("DS", TagType.INT, 1, 10, default_factory=lambda addr: addr * 10)

ds[1].default   # 10
ds[5].default   # 50
```

Per-slot overrides from `configure_slot` take precedence over `default_factory`.

### Clearing overrides

```python
ds.clear_slot_name(1)            # Restore generated name
ds.clear_slot_config(10)         # Clear retentive + default overrides
ds.clear_range_config(20, 30)    # Clear overrides for a range
```

## SlotConfig

`slot_config()` inspects the effective policy for a slot **without materializing** the tag:

```python
config = ds.slot_config(10)

config.name                 # Effective tag name
config.retentive            # Effective retentive policy
config.default              # Effective default value
config.name_overridden      # True if rename_slot was called
config.retentive_overridden # True if retentive was overridden
config.default_overridden   # True if default was overridden
```

The `*_overridden` flags tell you whether a value comes from an explicit override or from the block's inherited defaults. Useful for validation and diagnostic tooling.
