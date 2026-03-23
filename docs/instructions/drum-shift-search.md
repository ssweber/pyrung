# Drum, Shift & Search

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## Shift register

```python
shift(C.select(1, 8)) \
    .clock(ClockBit) \
    .reset(ResetBit)
```

- **Rung condition** is the data bit inserted at position 1
- **Clock** — shift occurs on the rising edge of the clock condition
- **Reset** — level-sensitive: clears all bits in range while True
- Terminal after `.clock(...).reset(...)`.

Direction is determined by the range order:
- `C.select(1, 8)` → shifts low-to-high (data enters at C1, exits at C8)
- `C.select(1, 8).reverse()` → shifts high-to-low

## Event drum

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
    ) \
        .reset(ShiftReset) \
        .jump((AutoMode, Found), step=DrumJumpStep) \
        .jog(Clock, Found)
```

## Time drum

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
    ) \
        .reset(ShiftReset) \
        .jump(Found, step=2) \
        .jog(Start)
```

`event_drum(...)` and `time_drum(...)` are terminal builders. `.reset(...)` is required and finalizes the instruction. `.jump(...)` and `.jog(...)` are optional.

### Variadic condition chaining

Builder condition arguments (`.down(...)`, `.clock(...)`, `.reset(...)`, `.jump(...)`, `.jog(...)`) all accept single conditions, multiple positional conditions, or tuple/list groups. All forms normalize to one AND expression:

```python
event_drum(...).reset(ResetA, ResetB).jog(JogA, JogB)
event_drum(...).jump((AutoMode, Found), step=2)
```

## Search

Find the first element in a range matching a condition:

```python
search(
    DS.select(1, 100) >= 100,
    result=FoundAddr,
    found=FoundFlag,
)
```

The first argument is a comparison expression built from a `.select()` range — the same operator syntax tags use elsewhere in the DSL.

- On success: `result = matched_address` (1-based), `found = True`
- On miss: `result = -1`, `found = False`
- `result` must be INT or DINT; `found` must be BOOL

### Continuous search (resume from last position)

```python
search(
    DS.select(1, 100) >= 100,
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
    Txt.select(1, 50) == "AB",     # Search for substring "AB"
    result=FoundAddr, found=FoundFlag,
)
```

Only `==` and `!=` are valid for CHAR ranges. Matches windowed substrings of length equal to the value string.
