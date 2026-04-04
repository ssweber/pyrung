# Lesson 9: Structured Tags and Blocks

## The Python instinct

```python
@dataclass
class Motor:
    running: bool = False
    speed: int = 0
    fault: bool = False

readings = [0] * 10
```

Python has dataclasses for structured records and lists for arrays. Ladder logic has both too, but they map to fixed regions of PLC memory.

## UDTs

```python
from pyrung import udt, Bool, Int, Real, Program, Rung, out, latch

@udt()
class Motor:
    running: Bool
    speed: Int
    fault: Bool

with Program() as logic:
    with Rung(Motor.running):
        out(StatusLight)
```

When you have multiple instances of the same kind of thing (three pumps, four valves), use `count`:

```python
@udt(count=3)
class Pump:
    running: Bool
    flow: Real
    fault: Bool

# Each instance accessed by index
with Rung(Pump[0].fault):
    latch(AlarmLight)
```

This maps directly to how real plants are organized: identical equipment, replicated logic, consistent naming. When all fields share the same type (like a group of Int fields for one sensor), pyrung also offers `named_array`, which maps to contiguous memory and supports bulk operations. See the [Tag Structures guide](../guides/tag-structures.md) for details.

## Blocks

When you need an array of same-typed tags rather than a structured record, a `Block` gives you a contiguous range you can index into and operate on in bulk. In Python you'd use a list; in ladder logic a block is a named region of PLC memory.

```python
from pyrung import Bool, Int, Block, TagType, Program, Rung, copy, blockcopy

readings    = Block("Readings", TagType.INT, 1, 10)    # Readings1..Readings10
NewReading  = Bool("NewReading")
SensorValue = Int("SensorValue")

with Program() as logic:
    with Rung(NewReading):
        blockcopy(readings.select(1, 9), readings.select(2, 10))    # shift everything down one slot
        copy(SensorValue, readings[1])                                # insert new value at the front
```

`readings.select(1, 9)` gives you Readings1 through Readings9 as a range, and `blockcopy` moves the whole thing in one instruction. The oldest value in Readings10 falls off the end. This is the ladder equivalent of `readings.insert(0, new_value)`: no loops, no index arithmetic.

## Exercise

Define a `Conveyor` UDT with fields for `running` (Bool), `speed` (Int), `jammed` (Bool), and `count` (Dint). Create 2 instances. Write logic where each conveyor runs only if it's not jammed, and a counter tracks items on each conveyor using edge-triggered counting. Test that jamming conveyor 0 stops it without affecting conveyor 1.
