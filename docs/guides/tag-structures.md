# Tag Structures

Advanced patterns for UDTs, named arrays, and block configuration. For basic tag and UDT usage, see [Core Concepts](../getting-started/concepts.md).

## UDT naming

A singleton UDT (`count=1`) generates compact tag names — no instance number:

```python
@udt()
class Motor:
    Running: Bool
    Speed: Int

# Tags: Motor_Running, Motor_Speed
```

With `count > 1`, tags are numbered by instance:

```python
@udt(count=3)
class Pump:
    Running: Bool
    Flow: Real

# Tags: Pump1_Running, Pump1_Flow, Pump2_Running, ...
```

If you want numbered names even for a singleton, use `always_number=True`:

```python
@udt(count=1, always_number=True)
class Heater:
    On: Bool
    Temp: Real

# Tags: Heater1_On, Heater1_Temp (not Heater_On)
```

This is useful when your naming convention requires consistency across singletons and counted structures.

## Field options

Plain annotations give you the type default (0 for `Int`, `False` for `Bool`, etc.). `Field` lets you override:

```python
@udt(count=3)
class Alarm:
    Id: Int = Field(default=100)
    Active: Bool
    Message: Char = Field(retentive=True)
```

`retentive=True` means the field survives a STOP→RUN transition. By default, UDT fields inherit the type's retentive policy.

You can also assign a plain literal as a default:

```python
@udt()
class Config:
    Mode: Int = 2
    Threshold: Real = 75.0
```

### Per-instance sequences with auto()

`auto()` generates a different default for each instance — useful for IDs and addresses:

```python
@udt(count=3)
class Alarm:
    Id: Int = auto(start=10, step=5)
    Active: Bool

# Alarm[1].Id defaults to 10
# Alarm[2].Id defaults to 15
# Alarm[3].Id defaults to 20
```

`auto()` only works on numeric types: `Int`, `Dint`, `Word`.

## Named arrays

A `@named_array` is a single-type structure where all fields share the same `TagType`. Fields are declared as class attributes (not annotations):

```python
from pyrung import named_array

@named_array(Int, count=4)
class Sensor:
    Reading = 0
    Setpoint = 100
```

This creates 4 instances, each with an `Int`-typed `Reading` and `Setpoint`. Access works the same as UDTs:

```python
Sensor[1].Reading   # first sensor's reading
Sensor[3].Setpoint  # third sensor's setpoint
```

### Selecting whole instances

You can select one or more complete instances as a contiguous `BlockRange`. This works with both dense and sparse layouts — stride is known, so instance boundaries are always well-defined:

```python
@named_array(Int, count=3)
class RecipeProfile:
    MixSeconds = 0
    HoldSeconds = 0
    TargetTemp = 0

RecipeProfile.instance(2)              # one complete profile
RecipeProfile.instance_select(1, 2)    # the first two profiles
```

This is useful with range-based instructions such as `blockcopy()` and `fill()`:

```python
blockcopy(RecipeProfile.instance(2), ds.select(201, 203))
fill(0, RecipeProfile.instance_select(1, 2))
```

For sparse layouts the returned `BlockRange` spans the full stride (including gap slots), while the tag list contains only the named fields.

### Stride

`stride` controls how many hardware slots each instance spans. When stride exceeds the field count, the extra slots are gaps:

```python
@named_array(Int, count=2, stride=4)
class DataPack:
    Id = auto()
    Value = 0
```

Instance 1 occupies slots 1–4, instance 2 occupies slots 5–8. Only slots 1–2 and 5–6 hold named fields; slots 3–4 and 7–8 are gaps. This matters when mapping to hardware with fixed slot widths.

## Cloning

`.clone()` creates an independent copy of a structure with a new name. Same field layout, fresh tags:

```python
@udt(count=2)
class Motor:
    Running: Bool
    Speed: Int

Pump = Motor.clone("Pump")           # Same layout, count=2
Fan = Motor.clone("Fan", count=4)    # Same layout, 4 instances
```

For named arrays, you can also override stride:

```python
@named_array(Int, count=2, stride=3)
class Slot:
    Id = auto()
    Value = 0

WideSlot = Slot.clone("WideSlot", stride=5)
```

### Flag overrides

`clone()` accepts optional flag overrides. `None` (the default) inherits from the parent:

```python
BinACounter = Counter.clone("BinACounter", public=True)
```

Available flags: `readonly`, `external`, `final`, `public`. See [Tag flags](#tag-flags) for details.

Use case: define a template structure once, clone it for each subsystem.

## Mapping to hardware

Named arrays can map their interleaved layout onto a hardware block range with `.map_to()`:

```python
from pyrung.click import ds

@named_array(Int, count=3, stride=2)
class Channel:
    Id = auto()
    Value = 0

entries = Channel.map_to(ds.select(101, 106))
```

This maps:

- Channel[1].Id → DS101, Channel[1].Value → DS102
- Channel[2].Id → DS103, Channel[2].Value → DS104
- Channel[3].Id → DS105, Channel[3].Value → DS106

The target range must have exactly `count * stride` addresses. Each instance claims `stride` consecutive slots, with fields filling from the front and gaps (if any) at the end.

For UDTs, use `TagMap` to map individual field blocks — see the [Click Dialect](../dialects/click.md) guide.

## Block configuration

Blocks support per-slot overrides for name, retentive policy, default value, and comment. All configuration must happen **before** you index the slot (before tag materialization). The unified `slot()` method handles inspection, configuration, and reset.

### Configuring slots

```python
ds = Block("DS", TagType.INT, 1, 100)

# Name a slot
ds.slot(1, name="SpeedCommand")
ds.slot(2, name="SpeedFeedback")

ds[1].name   # "SpeedCommand"
ds[2].name   # "SpeedFeedback"
ds[3].name   # "DS3" (default)

# Override retentive policy and default
ds.slot(10, retentive=True, default=999)

# Configure a range (retentive and default only)
ds.slot(20, 30, retentive=True)
```

Range configuration applies to all valid addresses in the inclusive window. For sparse blocks, it only affects addresses within the block's valid ranges.

### default_factory

Set a function that computes defaults by address when creating a block:

```python
ds = Block("DS", TagType.INT, 1, 10, default_factory=lambda addr: addr * 10)

ds[1].default   # 10
ds[5].default   # 50
```

Per-slot overrides from `slot()` take precedence over `default_factory`.

### Inspecting slots

`slot()` without configuration kwargs returns a live `SlotView` — no tag materialization:

```python
sv = ds.slot(10)

sv.name                 # Effective tag name
sv.retentive            # Effective retentive policy
sv.default              # Effective default value
sv.name_overridden      # True if name was overridden
sv.retentive_overridden # True if retentive was overridden
sv.default_overridden   # True if default was overridden
```

The `*_overridden` flags tell you whether a value comes from an explicit override or from the block's inherited defaults.

### Resetting overrides

```python
ds.slot(10).reset()        # Clear all overrides for slot 10
ds.slot(20, 30).reset()    # Clear all overrides for range 20–30
```

## Tag flags

Tags carry metadata flags that control validation and presentation. Three semantic flags are enforced by static validators; one presentation flag controls Data View visibility.

### Semantic flags

```python
SizeThreshold = Int("SizeThreshold", readonly=True)   # zero writers after startup
HmiSetpoint   = Int("HmiSetpoint", external=True)     # written outside the ladder
FilteredVal   = Int("FilteredVal", final=True)         # exactly one writer
```

**`readonly`** — the tag is initialized from its declared default and never written again. The `CORE_READONLY_WRITE` validator flags any write site. The stuck-bits validator skips readonly tags.

**`external`** — something outside the ladder (HMI, SCADA, comms) is the writer. The stuck-bits validator treats the external source as satisfying the missing latch or reset side. `plc.recovers()` returns `'external'` instead of `False`.

**`final`** — exactly one instruction in the ladder may write this tag. The `CORE_FINAL_MULTIPLE_WRITERS` validator flags any tag with more than one write site, regardless of mutual exclusivity.

Mutual exclusivity: `readonly` + `final` and `readonly` + `external` raise `ValueError` at construction. `external` + `final` is allowed (one ladder writer plus external writers).

### Presentation flag

```python
Running = Bool("Running", public=True)         # operator-facing status
State   = Int("State", choices=SortState, public=True)
```

**`public`** — part of the intended API surface. Setpoints, mode commands, alarms, key status bits. The VS Code Data View shows a **P** badge and provides a **Public** filter checkbox to hide plumbing tags. No validator consequence.

The absence of `public` means plumbing — not hidden, not forbidden, just not the featured interface. Same convention as Python's `foo` vs `_foo`.

### Flags on structures

Flags set on a `@udt()` or `@named_array()` decorator apply to all fields. Individual fields can override with `Field()`:

```python
@udt(external=True, public=True)
class Cmd:
    Speed: Int                              # inherits external=True, public=True
    Mode: Int = Field(external=False)       # overrides: ladder writes this one

@named_array(Int, stride=4, readonly=True)
class SortState:
    IDLE = 0
    DETECTING = 1
```

`clone()` inherits flags from the parent but accepts overrides:

```python
BinACounter = Counter.clone("BinACounter", public=True)
```

### Click comment convention

All flags round-trip through the Click nickname CSV comment parser using bracket syntax:

```
[readonly]
[external]
[final]
[public]
[readonly, choices=Off:0|On:1]
```
