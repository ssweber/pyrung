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

Use `nicknames=` when the two field tags must keep an existing naming scheme:

```python
HeatTimer = Timer.clone(
    "HeatTimer",
    nicknames={"Done": "HeatingComplete", "Acc": "HeatingElapsedMs"},
)
```

Unspecified fields retain the normal `{name}_Done` or `{name}_Acc` name. Nickname overrides are supported on single-instance Timer and Counter clones.

A timer's `Done` and `Acc` reset on STOP->RUN; a counter's survive, as on a CLICK (its T and TD banks are non-retentive, CT and CTD retentive). Override either field with `retentive=`, for example a retentive timer whose elapsed time must survive a restart:

```python
CureTimer = Timer.clone("CureTimer", retentive={"Acc": True})
```

`comments=` gives either field a comment, exported to its address's row in the CLICK nickname file (single-instance clones only):

```python
CureTimer = Timer.clone("CureTimer", comments={"Acc": "Elapsed cure time"})
```

### Status bits

Timers and counters also have status bits that reflect instruction state each scan. These are available in the runner API (`.when()`, `.monitor()`, `.value`) but cannot be used in `with rung()` — CLICK PLCs manage these bits internally.

| Type | Bit | Meaning |
|------|-----|---------|
| Timer | `.EN` | Rung is energizing the timer |
| Timer | `.TT` | Timer is actively accumulating (TON: enabled and not done; TOF: disabled and done) |
| Counter | `.CU` | Count-up condition is active |
| Counter | `.CD` | Count-down condition is active |

```python
# Observation — not ladder logic
runner.when(OvenTimer.TT).snapshot("timing")
runner.monitor(OvenTimer.EN, lambda c, p: print(f"EN: {p} → {c}"))
runner.run_until(OvenTimer.TT == False)  # wait for stall or done
```

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

### Presets

A preset is an int or an `Int`, `Dint` or `Word` tag. A `Real` preset raises `TypeError` — copy it into an `Int` first, as CLICK requires.

## Timers

### On-delay timer (TON / RTON)

```python
# TON: auto-reset when rung goes False
on_delay(OvenTimer, preset=100)

# RTON: hold accumulator when rung goes False (manual reset required)
on_delay(OvenTimer, preset=100) \
    .reset(ResetButton)
```

**TON behavior:**
- rung True → accumulator counts up; done = True when acc ≥ preset
- rung False → immediately resets acc and done

**RTON behavior:**
- Same as TON while rung is True
- rung False → holds acc and done (does not reset)
- `.reset(tag)` → resets acc and done regardless of rung state

`on_delay(...).reset(...)` (RTON) is terminal — no later instruction or branch can follow in the same flow.

### Off-delay timer (TOF)

```python
off_delay(CoolDown, preset=100)
```

**TOF behavior:**
- rung True → done = True, acc = 0
- rung False → accumulator counts up; done = False when acc ≥ preset

TOF is non-terminal — instructions can follow it in the same rung.

### Time units

| Argument | Aliases | Unit |
|----------|---------|------|
| `"ms"` | `"milliseconds"`, `"msec"` | Milliseconds (default) |
| `"sec"` | `"s"`, `"seconds"` | Seconds |
| `"min"` | `"m"`, `"minutes"` | Minutes |
| `"hour"` | `"h"`, `"hr"`, `"hours"` | Hours |
| `"day"` | `"d"`, `"days"` | Days |

Also accepted: `Tms`/`Ts`/`Tm`/`Th`/`Td` — great for tag names (`FillTimeTm` stays short, and `Tm` sidesteps the minute-vs-minimum ambiguity of `Min`).

The accumulator stores integer ticks in the selected unit. The time unit controls how `dt` is converted to accumulator ticks.

### Example: traffic light

```python
GreenTimer  = Timer.clone("GreenTimer")
YellowTimer = Timer.clone("YellowTimer")
RedTimer    = Timer.clone("RedTimer")

with rung(State == "g"):
    on_delay(GreenTimer, preset=3000)
with rung(State == "y"):
    on_delay(YellowTimer, preset=1000)
```

See [Structured Tags](../learn/structured-tags.md) for the full UDT pattern.

## Counters

Counters use a `.Done` bit (Bool) and a `.Acc` accumulator (Dint).

Counters count **rising edges**, as on a CLICK: one count each time the rung goes from False to True, however long it then stays True. Each counter keeps its own memory of each count input (the input as that counter last saw it), updated on every scan the counter runs, reset scans included. So:

- an input held True through a reset does not count again once the reset releases;
- an input already True on the first scan counts once;
- a counter inside a `for` loop sees each iteration's input, so it counts the edges between iterations.

### Count up (CTU)

```python
count_up(PartCounter, preset=100) \
    .reset(ResetButton)
```

- rung goes True → accumulator increments by one; done = True when acc ≥ preset
- `.reset(tag)` → resets acc and done while that tag is True (a level: it wins over a count on the same scan)

`count_up(...).reset(...)` is terminal.

### Count down (CTD)

```python
count_down(Dispense, preset=100) \
    .reset(ResetButton)
```

- Accumulator starts at 0 and goes down by one on each rising edge
- done = True when acc ≤ preset, so with a positive preset the counter is done from its first scan (stage the accumulator above the preset with `copy()` to count down to it, as on a CLICK)

`count_down(...).reset(...)` is terminal.

### Bidirectional counter

```python
count_up(ZoneCounter, preset=100) \
    .down(DownCondition) \
    .reset(ResetButton)
```

Each input counts its own rising edges; an up edge and a down edge on the same scan cancel.

### Counting a held condition

A counter never counts a held condition more than once. To count scans or time while something is on, use a timer, or `calc()` (`calc(N + 1, N)` adds one every scan its rung is True). `rise()` on the rung is allowed but redundant: the counter already counts edges.

For chained builders (counters, shift registers, drums), complete the full chain (`.down(...)`, `.clock(...)`, `.reset(...)`) before any later DSL statement.
