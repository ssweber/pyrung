# Lesson 9: Structured Tags and Blocks

## The Python instinct

```python
@dataclass
class Bin:
    sensor: bool = False
    count: int = 0
    full: bool = False

bins = [Bin(), Bin()]
size_log = [0] * 5
```

Python has dataclasses for structured records and lists for arrays. Ladder logic has both too, but they map to fixed regions of PLC memory.

## UDTs

Up to now, each bin had its own separate tags: `BinASensor`, `BinAAcc`, `BinBSensor`, `BinBAcc`. That's fine for two bins, but it doesn't scale -- and it doesn't match how real plants are organized. Identical equipment should use identical structures.

Remember the doubled name from [Lesson 2](tags.md) — `ConveyorSpeed = Int("ConveyorSpeed")`? It's gone. pyrung generates the flat identity from the structure: `Bin[1].Sensor` is the Python access path to a tag whose real identity is `Bin1_Sensor`. On Click that's a flat nickname; on Rockwell it's a real UDT member. Your Python stays the same either way.

```python
from pyrung import udt, Bool, Int, Dint, Program, Rung, PLCRunner, out, rise, count_up

@udt(count=2)
class Bin:
    Sensor: Bool
    Done: Bool
    Acc: Dint
    Full: Bool

CountReset = Bool("CountReset")

with Program() as logic:
    with Rung(rise(Bin[1].Sensor)):
        count_up(Bin[1].Done, Bin[1].Acc, preset=10) \
            .reset(CountReset)
    with Rung(rise(Bin[2].Sensor)):
        count_up(Bin[2].Done, Bin[2].Acc, preset=10) \
            .reset(CountReset)

    with Rung(Bin[1].Done):
        out(Bin[1].Full)
    with Rung(Bin[2].Done):
        out(Bin[2].Full)
```

`@udt(count=2)` creates two instances, accessed by index. `Bin[1].Sensor` and `Bin[2].Sensor` are distinct tags, but they share the same structure. This maps directly to how real plants are organized: identical equipment, replicated logic, consistent naming.

Yes, the `Bin[1]` and `Bin[2]` rungs look nearly identical. Your Python instinct says "loop." Resist it. Each rung is independently editable, grep-able, and visible in the ladder editor. When Bin 2 needs a different preset or an extra condition, you edit one rung — you don't fight a loop. Duplication in ladder logic is a feature, not a smell.

That said, Python `for` loops work fine at build time — `for i in (1, 2): with Rung(rise(Bin[i].Sensor)): ...` emits two distinct rungs into the program. pyrung doesn't forbid it; it's just normal Python running during program construction. But explicit rungs are usually more readable, especially once the bins diverge.

A singleton UDT (`count` omitted or `count=1`) generates compact names with no instance number: `Motor_Running`, `Motor_Speed`. With `count > 1` you get numbered names: `Pump1_Running`, `Pump2_Running`. If your naming convention wants `Motor1_Running` even for a singleton (so future expansion doesn't rename everything), pass `always_number=True`.

```
  Bin (UDT, count=2)          SortLog (Block, Int, 1-5)
  +-- .Sensor : Bool          +-- [1] : Int
  +-- .Done   : Bool          +-- [2] : Int
  +-- .Acc    : Dint          +-- [3] : Int
  +-- .Full   : Bool          +-- [4] : Int
                              +-- [5] : Int
```

!!! warning "PLC arrays start at 1"

    `Bin[1]`, not `Bin[0]`. Every PLC vendor in the world is 1-indexed and pyrung honors that because the tag table you generate has to match the PLC's. Your Python instinct will betray you here exactly once. If you specifically need 0-based addressing (matching a 0-based hardware range or porting code), Blocks accept a 0-based start — but the default is 1.

When all fields share the same type (like a group of Int fields for one sensor), pyrung also offers `named_array`, which maps to contiguous memory and supports bulk operations. See the [Tag Structures guide](../guides/tag-structures.md) for details.

## Blocks

When you need an array of same-typed tags rather than a structured record, a `Block` gives you a contiguous range you can index into and operate on in bulk. Here's a sort log that records the last 5 box sizes:

```python
from pyrung import Block, TagType, copy, blockcopy

SortLog  = Block("SortLog", TagType.INT, 1, 5)    # SortLog1..SortLog5
BoxSize  = Int("BoxSize")
NewBox   = Bool("NewBox")

with Program() as logic:
    # (bin counting rungs from above...)

    # Log box sizes: shift register pattern
    with Rung(rise(NewBox)):
        blockcopy(SortLog.select(1, 4), SortLog.select(2, 5))  # Shift down
        copy(BoxSize, SortLog[1])                                # Insert at front
```

`SortLog.select(1, 4)` gives you SortLog1 through SortLog4 as a range, and `blockcopy` moves the whole thing in one instruction. The oldest value in SortLog5 falls off the end. This is a **shift register** — the canonical FIFO pattern in ladder logic, with dedicated instructions on every platform (`BSL`/`BSR` on Rockwell, `SHIFT` on Click and Do-More). pyrung uses `blockcopy` over `select` for the same effect: no loops, no index arithmetic.

Why `.select(1, 4)` instead of `[1:4]`? Python's `list[1:4]` is `[1, 2, 3]` — exclusive end. PLC ranges like `DS1..DS4` are inclusive on both ends — `[1, 2, 3, 4]`. Reusing slice syntax would silently do the wrong thing exactly half the time. `.select(start, end)` is visibly different because the semantics are different. Both bounds are inclusive, every time.

## Try it

```python
runner = PLCRunner(logic)
with runner.active():
    # 3 boxes into Bin 1
    for _ in range(3):
        Bin[1].Sensor.value = True
        runner.step()
        Bin[1].Sensor.value = False
        runner.step()

    assert Bin[1].Acc.value == 3
    assert Bin[2].Acc.value == 0    # Bin 2 untouched
    assert Bin[1].Full.value is False

    # Log 3 box sizes
    for size in [150, 80, 200]:
        BoxSize.value = size
        NewBox.value = True
        runner.step()
        NewBox.value = False
        runner.step()

    # Newest first
    assert SortLog[1].value == 200
    assert SortLog[2].value == 80
    assert SortLog[3].value == 150
```

!!! info "Also known as..."

    Structured tags are UDTs or `STRUCT`s. Flat-namespace PLCs fake it with underscore prefixes — exactly what pyrung generates as the flat identity. Block-copy, shift-register, and fill all have dedicated instructions on every platform.

## Going deeper

The [Tag Structures guide](../guides/tag-structures.md) covers the full API. Two features worth knowing early:

- **`Field()`** — override defaults or retentive policy per field: `id: Int = Field(default=100, retentive=True)`
- **`@named_array`** — like `@udt` but all fields share one type. Use UDT for mixed types, named_array for same-typed records

The guide also covers cloning, stride, hardware mapping, and per-instance sequences.

## Exercise

Add a singleton `Conveyor` UDT with fields for `Running` (Bool), `Speed` (Int), and `MotorFault` (Bool). Write logic where the conveyor stops when `MotorFault` is true, regardless of the running state. Use `fill` to add a "clear log" function: when a `ClearLog` button is pressed, fill the SortLog with zeros. (Hint: see the [Data Movement reference](../instructions/copy.md) for `fill`.)

---

The logic is complete. Now prove it works -- write a test suite that covers the normal cycle, the fault path, the mode switch, and the edge cases.
