# Timers & Counters

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## Timer and Counter types

`Timer` and `Counter` are built-in structured types. Each has a `.Done` bit (Bool) and an `.Acc` accumulator (Int for timers, Dint for counters).

```python
from pyrung import Timer, Counter

# Named instances — the 95% case for real programs
OvenTimer   = Timer.clone("OvenTimer")
CycleTimer  = Timer.clone("CycleTimer")
PartCounter = Counter.clone("PartCounter")

# Anonymous instances — fine for throwaway simulation tests
t = Timer[1]
c = Counter[1]
```

Use `Timer.clone("Name")` for production code — `OvenTimer_Done` in a fault log tells you everything; `Timer1_Done` tells you nothing.

### Custom types

Timer and counter instructions use a structural contract: any `@udt()` with a `Done: Bool` field and an `Acc: Int` or `Acc: Dint` field works with `on_delay`, `off_delay`, `count_up`, and `count_down`.

```python
from pyrung import udt, Bool, Dint

@udt()
class MyCounter:
    Done: Bool
    Acc: Dint
    Faults: Dint  # extra fields are fine
```

## Timers

### On-delay timer (TON / RTON)

```python
# TON: auto-reset when rung goes False
on_delay(OvenTimer, preset=100, unit="Tms")

# RTON: hold accumulator when rung goes False (manual reset required)
on_delay(OvenTimer, preset=100) \
    .reset(ResetButton)
```

**TON behavior:**
- Rung True → accumulator counts up; done = True when acc ≥ preset
- Rung False → immediately resets acc and done

**RTON behavior:**
- Same as TON while rung is True
- Rung False → holds acc and done (does not reset)
- `.reset(tag)` → resets acc and done regardless of rung state

`on_delay(...).reset(...)` (RTON) is terminal — no later instruction or branch can follow in the same flow.

### Off-delay timer (TOF)

```python
off_delay(CoolDown, preset=100, unit="Tms")
```

**TOF behavior:**
- Rung True → done = True, acc = 0
- Rung False → accumulator counts up; done = False when acc ≥ preset

TOF is non-terminal — instructions can follow it in the same rung.

### Time units

| Symbol | Unit |
|--------|------|
| `"Tms"` | Milliseconds (default) |
| `"Ts"` | Seconds |
| `"Tm"` | Minutes |
| `"Th"` | Hours |
| `"Td"` | Days |

The accumulator stores integer ticks in the selected unit. The time unit controls how `dt` is converted to accumulator ticks.

### Example: traffic light

```python
GreenTimer  = Timer.clone("GreenTimer")
YellowTimer = Timer.clone("YellowTimer")
RedTimer    = Timer.clone("RedTimer")

with Rung(State == "g"):
    on_delay(GreenTimer, preset=3000, unit="Tms")
with Rung(State == "y"):
    on_delay(YellowTimer, preset=1000, unit="Tms")
```

See [Structured Tags](../learn/structured-tags.md) for the full UDT pattern.

## Counters

Counters use a `.Done` bit (Bool) and a `.Acc` accumulator (Dint).

Counters count **every scan** while the condition is True — they are not edge-triggered. Use `rise()` on the rung condition if you want one increment per leading edge.

### Count up (CTU)

```python
count_up(PartCounter, preset=100) \
    .reset(ResetButton)
```

- Rung True → accumulator increments each scan; done = True when acc ≥ preset
- `.reset(tag)` → resets acc and done when that tag is True

`count_up(...).reset(...)` is terminal.

### Count down (CTD)

```python
count_down(Dispense, preset=100) \
    .reset(ResetButton)
```

- Accumulator starts at 0 and goes negative each scan
- done = True when acc ≤ −preset

`count_down(...).reset(...)` is terminal.

### Bidirectional counter

```python
count_up(ZoneCounter, preset=100) \
    .down(DownCondition) \
    .reset(ResetButton)
```

Both up and down conditions are evaluated every scan; the net delta is applied once.

### Edge-triggered counting

To count edges instead of scans, wrap the condition with `rise()`:

```python
with Rung(rise(Sensor)):
    count_up(PartCounter, preset=9999) \
        .reset(CountReset)
```

For chained builders (counters, shift registers, drums), complete the full chain (`.down(...)`, `.clock(...)`, `.reset(...)`) before any later DSL statement.
