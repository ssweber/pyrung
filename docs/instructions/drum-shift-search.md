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
- With `.clock(...).reset(...)`, `shift` is a box with pins and ends its rung.

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

A tag preset (`DS[1]`) needs CLICK v4 firmware. Earlier firmware takes literal durations only, and Click validation leaves a hint.

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

A step ends when its time is up **and** its event is on. `None` leaves out either half. Step 1 is time-only. Step 2 waits for `PartPresent` alone. Step 3 needs 2 s and `DrillHome`. Each step needs at least one of the two.

Events are levels, unlike `event_drum`'s rising edges. An event already on when the time comes up ends the step on that scan. One level held across several event-only steps ends one step per scan. The accumulator times from step entry and keeps counting while a timed step waits for its event. A step with no preset does not time.

`event_drum(...)`, `time_drum(...)` and `time_event_drum(...)` end their rung. `.reset(...)` is required and finalizes the instruction. `.jump(...)` and `.jog(...)` are optional.

### What a drum does each scan

All three drums run the same routine:

- **Reset** is a level, and it wins. While it is on, the drum sits on step 1 with its completion flag and **every output off**. A jump or jog that rises on a reset scan is dropped, not saved for later.
- **Jump and jog** act on their rising edges, even while the rung is false. A drum does one thing a scan. A jump beats a jog. A jump to a step the drum does not have does nothing, and the drum's jog, time and event do nothing that scan either. A jog on the last step completes the drum at once, rung true or not, and the drum stays on that step. A further jog there does nothing. A step a jump or jog moves to waits for the next scan to time or end.
- **A step register out of range** goes to step 1 the next scan the rung is true, keeping the accumulator. A paused drum leaves it alone. The correction writes no output by itself. If a jog, or a jump to a step the drum does not have, rises while the step is out of range, the drum does nothing that scan. The next scan corrects the step.
- **A step's own end** needs the rung true and the completion flag off. On those scans a step with a preset times. The step moves when its end is met, which is its time, its event, or both. A false rung pauses all of it. The outputs stay as they were.
- **On the scan the rung comes back, a reset ends or the program starts**, the drum writes the step's pattern row and does nothing else. The step does not time or end, and an event drum's rising edge on that scan is spent. A jump or jog on that scan still acts.
- **Outputs** are written when the step moves or the rung comes back, not on every scan, so a value another rung writes to one stands until then. A write to the step register is not a move. The outputs stay, and the next event, time or jog acts on the step the register holds.
- **The completion flag is the drum's state.** Completing sets it and turns every output off. While it is on, the drum's own time and events do nothing, the accumulator holds, and a rung coming back writes nothing. A jump moves the step and leaves the outputs alone. A jog forward writes the step's pattern row. A reset clears the flag, and so can any other rung. Set from outside, the flag stops a drum wherever it is. Cleared from outside, it frees the drum to go on from the step it is on.
- A time drum's accumulator is 0 on the scan of every step change: an advance, a jog, a jump and a reset. Completing and a correction keep it.
- **A step reads a tag preset once**, on its first scan of timing, and keeps it until the step changes. A tag preset changed mid-step applies from the next step. It isn't yet known when a CLICK reads a DS preset. A tag preset of 0 or less never ends its step. A constant 0 ends it on its first scan of timing.

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

The first argument is a comparison expression built from a `.select()` range. It uses the same operator syntax as tags elsewhere in the DSL.

- On success: `result = matched_address` (1-based), `found = True`
- On miss: `result = -1`, `found = False`
- `result` must be INT or DINT, and `found` must be BOOL
- For CLICK, `result` must be a `DS` and the range in `DS`, `DD`, `DH`, `DF`, `TD`, `CTD` or `TXT`. The export refuses anything else

### Continuous search (resume from last position)

```python
search(
    DS.select(1, 100) >= 100,
    result=FoundAddr, found=FoundFlag,
    continuous=True,
)
```

- `result == 0` → restart at first address
- `result == -1` → already exhausted, so return miss without rescanning
- otherwise → resume at first address after current result

### Text search

```python
search(
    Txt.select(1, 50) == "AB",     # Search for substring "AB"
    result=FoundAddr, found=FoundFlag,
)
```

Only `==` and `!=` are valid for CHAR ranges. The search matches substrings as long as the value string.
