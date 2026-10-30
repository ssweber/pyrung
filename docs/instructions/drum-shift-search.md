# Drum, Shift & Search

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## Shift register

```python
shift(C.select(1, 8)) \
    .clock(ClockBit) \
    .reset(ResetBit)
```

- **rung condition** is the data bit inserted at position 1
- **Clock** — shift occurs on the rising edge of the clock condition
- **Reset** — level-sensitive: clears all bits in range while True
- Terminal after `.clock(...).reset(...)`.

Direction is determined by the range order:
- `C.select(1, 8)` → shifts low-to-high (data enters at C1, exits at C8)
- `C.select(1, 8).reverse()` → shifts high-to-low

## Event drum

```python
with rung(Running):
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
with rung(Running):
    time_drum(
        outputs=[DrumOut1, DrumOut2, DrumOut3],
        presets=[50, DS[1], 75, DS[2]],
        unit="ms",
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

A tag preset (`DS[1]`) needs CLICK v4 firmware; earlier firmware takes literal durations only, and Click validation leaves a hint.

## Time & event drum

```python
with rung(Running):
    time_event_drum(
        outputs=[Clamp, Drill, Eject],
        presets=[500, None, 2000],
        events=[None, PartPresent, DrillHome],
        unit="ms",
        pattern=[
            [1, 0, 0],
            [1, 1, 0],
            [0, 0, 1],
        ],
        current_step=DrumStep,
        accumulator=DrumAcc,
        completion_flag=DrumDone,
    ).reset(ShiftReset)
```

A step ends when its time is up **and** its event is on. `None` leaves out either half: step 1 is time-only, step 2 waits for `PartPresent` alone, step 3 needs 2 s and `DrillHome`. Each step needs at least one.

Events are levels, unlike `event_drum`'s rising edges: an event already on when the time comes up ends the step on that scan, and one level held across several event-only steps ends one step per scan. The accumulator times from step entry and keeps counting while a timed step waits for its event.

`event_drum(...)`, `time_drum(...)` and `time_event_drum(...)` are terminal builders. `.reset(...)` is required and finalizes the instruction. `.jump(...)` and `.jog(...)` are optional.

### What a drum does each scan

All three drums follow a CLICK, measured on hardware:

- **Reset** is a level, and it wins: while it is on the drum sits on step 1 with its completion flag and **every output off**, and a jump or jog that rises on a reset scan is dropped, not saved for later.
- **Jump and jog** act on their rising edges, even while the rung is false. The jog runs first and the jump last, so a jump and a jog on one scan land on the jump's step. A jog on the last step does nothing.
- **A false rung** pauses the drum: no timing, no event steps, and its outputs stay as they were. They show the step a paused jump or jog took once the rung is true again.
- **Completion** sets the flag and turns every output off. They stay off until the drum steps forward again, by a jog or by its own event or time. A jump moves the step but leaves them off. Only a reset clears the flag.
- **A step register out of range** goes to step 1 the next scan the rung is true; a paused drum leaves it alone.
- A time drum's accumulator is 0 on the scan of every step change: an advance, a jog, a jump and a reset.

CLICK has not been asked yet whether an event or time step after a jump ends a completion; pyrung takes it that one does, as a jog does.

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
