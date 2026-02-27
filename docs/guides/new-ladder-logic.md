# Ladder Logic Reference

Full reference for conditions, instructions, and program structure. For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## Conditions

Everything that goes inside `Rung(...)`. All forms can be mixed freely.

```
Fault                          tag is truthy
~Fault                         tag is falsy
MotorTemp > 100                comparison  (==  !=  <  <=  >  >=)
Fault, Pump                    comma = implicit AND
Fault, MotorTemp > 100         implicit AND with comparison
Fault & Pump                   & works for truthy tags
Running | ~Estop               | and ~ work for truthy tags
Fault & (MotorTemp > 100)      & with comparison needs parens
Running | (Mode == 1)          | with comparison needs parens
Running | ~Estop, Mode == 1    mix commas and operators freely
all_of(Fault, Pump, Valve)     explicit AND (same as commas)
any_of(Low, High, Emergency)   explicit OR
```

### Normally open (examine-on)

```python
with Rung(Button):          # True when Button is True
    out(Light)
```

### Normally closed (examine-off)

```python
with Rung(~Button):      # True when Button is False
    out(FaultLight)
```

### Rising and falling edge

```python
with Rung(rise(Button)):    # True for ONE scan on False→True transition
    latch(Motor)

with Rung(fall(Button)):    # True for ONE scan on True→False transition
    reset(Motor)
```

### Multiple conditions (AND)

```python
# Comma syntax — all must be True
with Rung(Button, ~Fault, AutoMode):
    out(Motor)

# all_of() — explicit AND
with Rung(all_of(Button, ~Fault, AutoMode)):
    out(Motor)
```

### OR conditions

```python
# any_of() — at least one must be True
with Rung(any_of(Start, RemoteStart)):
    latch(Motor)

# Pipe operator — same as any_of
with Rung(Start | RemoteStart):
    latch(Motor)
```

### Nested AND/OR

```python
with Rung(any_of(Start, all_of(AutoMode, Ready), RemoteStart)):
    latch(Motor)
```

### Comparisons

```python
with Rung(Step == 0):
    out(InitDone)

with Rung(Temperature >= 100.0):
    latch(OverTempFault)

with Rung(Counter != 5):
    out(NotAtTarget)
```

### INT truthiness

INT tags are True when non-zero:

```python
with Rung(Step):                    # True if Step != 0
    out(StepActive)

with Rung(any_of(Step, AlarmCode)):
    out(AnyActive)
```

### Inline expressions

```python
with Rung((PressureA + PressureB) > 100):
    latch(HighPressureFault)
```

Inline expressions work in simulation. The Click dialect validator will flag them if targeting Click hardware — rewrite as `calc()` instructions instead.

---

## Basic I/O instructions

### `out` — energize output

```python
with Rung(Button):
    out(Light)      # Light = True while rung is True; False when rung is False
```

`out` follows rung power: True when rung is True, False when False. Last rung to write a tag wins within a scan.

### `latch` — set and hold (SET)

```python
with Rung(Start):
    latch(Motor)    # Motor becomes True and stays True until reset
```

### `reset` — clear latch (RESET)

```python
with Rung(Stop):
    reset(Motor)    # Motor becomes False
```

### Immediate I/O

For `InputTag` / `OutputTag` elements (from `InputBlock` / `OutputBlock`), `.immediate` bypasses the scan-cycle image table:

```python
with Rung(SensorA.immediate):
    out(ValveB.immediate)
```

---

## Copy and block operations

### `copy` — copy single value

```python
copy(Setpoint, DS[1])               # Copy tag to tag
copy(42, DS[1])                     # Copy literal to tag
copy(DS[1], DS[DS[0]])              # Indirect addressing: DS[pointer]
copy(DS[1], DS[1], oneshot=True)    # Execute only on rung rising edge
```

Out-of-range values are **clamped** to the destination type's min/max. This is different from `calc()`, which wraps.

### `blockcopy` — copy a range

```python
blockcopy(DS.select(1, 10), DS.select(11, 20))   # Copy DS1..DS10 → DS11..DS20
```

Source and destination ranges must have the same length.

### `fill` — write constant to range

```python
fill(0, DS.select(1, 100))          # Zero out DS1..DS100
fill(Setpoint, Alarms.select(1, 8)) # Copy tag value to all 8 elements
```

### Type conversion (copy modifiers)

```python
copy(ModeChar.as_value(), DS[1])    # CHAR '5' → numeric 5
copy(ModeChar.as_ascii(), DS[1])    # CHAR '5' → ASCII code 53
copy(DS[1].as_text(), ModeChar)     # Numeric → CHAR string
copy(DS[1].as_text(pad=5), Txt[1])  # Numeric → zero-padded CHAR
copy(DS[1].as_binary(), ModeChar)   # Numeric → raw byte CHAR
```

### Pack / unpack

```python
pack_bits(C.select(1, 16), DS[1])          # Pack 16 BOOLs into one WORD
unpack_to_bits(DS[1], C.select(1, 16))     # Unpack WORD into 16 BOOLs

pack_words(DS.select(1, 2), DD[1])         # Pack two INTs into DINT (low-word first)
unpack_to_words(DD[1], DS.select(1, 2))    # Unpack DINT into two INTs
```

---

## Math

```python
calc(DS[1] + DS[2], DS[3])              # DS3 = DS1 + DS2 (wraps to INT range)
calc(DS[1] * 2, DS[3], oneshot=True)    # One-shot: execute once per rung rising edge
calc(DS[1] | DS[2], DS[3], mode="hex")  # Unsigned 16-bit bitwise OR
```

**Math wraps** — overflow truncates to the destination type's bit width (modular arithmetic). This differs from `copy()` which clamps.

### Overflow behavior

| Expression | Destination | Result |
|------------|-------------|--------|
| `DS1 + 1` (DS1=32767) | INT (16-bit signed) | −32768 (wraps) |
| `50000 * 50000` | DINT (32-bit signed) | −1,794,967,296 (wraps) |
| `40000` → `copy()` | INT | 32767 (clamped) |

### Division

- Division by zero produces result = 0 and sets the system fault flag.
- Integer division truncates toward zero: `−7 / 2 = −3`.

### Math modes

| Mode | Operand treatment |
|------|-------------------|
| `"decimal"` (default) | Signed arithmetic |
| `"hex"` | Unsigned 16-bit arithmetic (0x0000–0xFFFF wrap) |

---

## Timers

Timers use a **two-tag model**: a done-bit (`BOOL`) and an accumulator (`INT`).

### On-delay timer (TON / RTON)

```python
# TON: auto-reset when rung goes False
on_delay(TimerDone, accumulator=TimerAcc, preset=100, unit=Tms)

# RTON: hold accumulator when rung goes False (manual reset required)
on_delay(TimerDone, accumulator=TimerAcc, preset=100).reset(ResetButton)
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
off_delay(TimerDone, accumulator=TimerAcc, preset=100, unit=Tms)
```

**TOF behavior:**
- Rung True → done = True, acc = 0
- Rung False → accumulator counts up; done = False when acc ≥ preset

TOF is non-terminal — instructions can follow it in the same rung.

### Time units

| Symbol | Unit |
|--------|------|
| `Tms` | Milliseconds (default) |
| `Ts` | Seconds |
| `Tm` | Minutes |
| `Th` | Hours |
| `Td` | Days |

The accumulator stores integer ticks in the selected unit. The time unit controls how `dt` is converted to accumulator ticks.

---

## Counters

Counters use a **two-tag model**: a done-bit (`BOOL`) and an accumulator (`DINT`).

Counters count **every scan** while the condition is True — they are not edge-triggered. Use `rise()` on the rung condition if you want one increment per leading edge.

### Count up (CTU)

```python
count_up(CountDone, accumulator=CountAcc, preset=100).reset(ResetButton)
```

- Rung True → accumulator increments each scan; done = True when acc ≥ preset
- `.reset(tag)` → resets acc and done when that tag is True

`count_up(...).reset(...)` is terminal.

### Count down (CTD)

```python
count_down(CountDone, accumulator=CountAcc, preset=100).reset(ResetButton)
```

- Accumulator starts at 0 and goes negative each scan
- done = True when acc ≤ −preset

`count_down(...).reset(...)` is terminal.

### Bidirectional counter

```python
count_up(CountDone, accumulator=CountAcc, preset=100) \
    .down(DownCondition) \
    .reset(ResetButton)
```

Both up and down conditions are evaluated every scan; the net delta is applied once.

### Oneshot counting

To count edges instead of scans, use `oneshot=True`:

```python
with Rung(Sensor):
    count_up(CountDone, CountAcc, preset=9999, oneshot=True).reset(CountReset)
```

For chained builders (counters, shift registers, drums), complete the full chain (`.down(...)`, `.clock(...)`, `.reset(...)`) before any later DSL statement.

---

## Search

Find the first element in a range matching a condition:

```python
search(
    condition=">=",
    value=100,
    search_range=DS.select(1, 100),
    result=FoundAddr,
    found=FoundFlag,
)
```

- On success: `result = matched_address` (1-based), `found = True`
- On miss: `result = -1`, `found = False`
- `result` must be INT or DINT; `found` must be BOOL

### Continuous search (resume from last position)

```python
search(
    condition=">=", value=100,
    search_range=DS.select(1, 100),
    result=FoundAddr, found=FoundFlag,
    continuous=True,
)
```

- `result == 0` → restart at first address
- `result == -1` → already exhausted; return miss without rescanning
- otherwise → resume at first address after current result

### Text search

```python
search(
    condition="==",
    value="AB",                     # Search for substring "AB"
    search_range=Txt.select(1, 50),
    result=FoundAddr, found=FoundFlag,
)
```

Only `==` and `!=` are valid for CHAR ranges. Matches windowed substrings of length equal to the value string.

---

## Shift register

```python
shift(C.select(1, 8)).clock(ClockBit).reset(ResetBit)
```

- **Rung condition** is the data bit inserted at position 1
- **Clock** — shift occurs on the rising edge of the clock condition
- **Reset** — level-sensitive: clears all bits in range while True
- Terminal after `.clock(...).reset(...)`.

Direction is determined by the range order:
- `C.select(1, 8)` → shifts low-to-high (data enters at C1, exits at C8)
- `C.select(1, 8).reverse()` → shifts high-to-low

---

## Drum sequencers

`event_drum(...)` and `time_drum(...)` are terminal builders. `.reset(...)` is required and finalizes the instruction. `.jump(...)` and `.jog(...)` are optional.

### Event drum

```python
with Rung(Running):
    event_drum(
        outputs=[DrumOut1, DrumOut2, DrumOut3],
        events=[DrumEvt1, DrumEvt2, DrumEvt3, DrumEvt4],
        pattern=[
            [1, 0, 0],
            [0, 1, 0],
            [0, 0, 1],
            [1, 1, 0],
        ],
        current_step=DrumStep,
        completion_flag=DrumDone,
    ).reset(ShiftReset).jump((AutoMode, Found), step=DrumJumpStep).jog(Clock, Found)
```

### Time drum

```python
with Rung(Running):
    time_drum(
        outputs=[DrumOut1, DrumOut2, DrumOut3],
        presets=[50, DS[1], 75, DS[2]],
        unit=Tms,
        pattern=[
            [1, 0, 0],
            [0, 1, 0],
            [0, 0, 1],
            [1, 1, 0],
        ],
        current_step=DrumStep,
        accumulator=DrumAcc,
        completion_flag=DrumDone,
    ).reset(ShiftReset).jump(Found, step=2).jog(Start)
```

### Variadic condition chaining

Builder condition arguments (`.down(...)`, `.clock(...)`, `.reset(...)`, `.jump(...)`, `.jog(...)`) all accept single conditions, multiple positional conditions, or tuple/list groups. All forms normalize to one AND expression:

```python
event_drum(...).reset(ResetA, ResetB).jog(JogA, JogB)
event_drum(...).jump((AutoMode, Found), step=2)
```

---

## Branching

`branch()` creates a parallel path within a rung. The branch condition is ANDed with the parent rung's condition.

```python
with Rung(First):          # ① Evaluate: First
    out(Third)             # ③ Execute
    with branch(Second):   # ② Evaluate: First AND Second
        out(Fourth)        # ④ Execute
    out(Fifth)             # ⑤ Execute
```

Three rules:

1. **Conditions evaluate before instructions.** ① and ② are resolved before ③ ④ ⑤ run. A branch ANDs its own condition with the parent rung's.
2. **Instructions execute in source order.** ③ → ④ → ⑤, as written — not "all rung, then all branch."
3. **Each rung starts fresh.** The next rung sees the state as it was left after the previous rung's instructions.

---

## Subroutines

### Context-manager style

```python
with Program() as logic:
    with subroutine("startup"):
        with Rung(Step == 0):
            out(InitLight)

    with Rung(AutoMode):
        call("startup")
```

### Decorator style

```python
@subroutine("init")
def init_sequence():
    with Rung():
        out(InitLight)

with Program() as logic:
    with Rung(Button):
        call(init_sequence)     # auto-registers and calls
```

---

## Programs

Two equivalent ways to define a program:

```python
# Context manager
with Program() as logic:
    with Rung(Start):
        latch(Running)

# Decorator
@program
def logic():
    with Rung(Start):
        latch(Running)
```

Both produce a `Program` you pass to `PLCRunner`. See [Core Concepts — Programs](../getting-started/concepts.md#programs) for details.
