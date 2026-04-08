# Lesson 3: Latch and Reset

## The Python instinct

```python
if start_pressed:
    conveyor_running = True
# But what turns it off?
# And what if start_pressed goes False?
```

## The problem

In the real world, you press a momentary "Start" button. Your finger comes off. The conveyor should keep running. `out` won't work here because it de-energizes the moment the rung goes false.

## The ladder logic way

```python
from pyrung import Bool, Program, Rung, PLCRunner, latch, reset

StartBtn = Bool("StartBtn")    # NO momentary contact
StopBtn  = Bool("StopBtn")     # NC contact: conductive at rest
Running  = Bool("Running")

with Program() as logic:
    with Rung(StartBtn):
        latch(Running)       # SET: Running = True, stays True
    with Rung(~StopBtn):
        reset(Running)       # RESET when stop pressed or wire broken
```

`latch` is sticky. Once set, it stays set until explicitly `reset`. This is the bread and butter of motor control, alarm acknowledgment, and mode selection in every factory on earth.

`StopBtn` is wired **normally-closed** — the circuit is conductive at rest, so the PLC input reads True when healthy. Writing `~StopBtn` means "this contact fires when the stop circuit opens" — button pressed, wire cut, or power lost. The reset rung is last because stop should always win (remember "last rung wins" from [Lesson 1](scan-cycle.md)).

!!! note "Why two rungs instead of one?"

    Your Python instinct says "the rung went false, the output should drop." That's how `out` works — it de-energizes the moment the rung goes false. But `latch` isn't `out`. `latch` sets the bit and *leaves it set*. The latch rung only fires *once*, when Start is pressed. After that, `Running` stays true on its own. To clear it, you need a *separate* rung with `reset()`. That's why this lesson has two rungs: one to start, one to stop. If you only had the first rung, the motor would stop the instant you released Start — exactly the bug [Lesson 1](scan-cycle.md) ended on.

!!! note "What `~` actually means"

    Your Python instinct reads `~StopBtn` as "not StopBtn" — a Boolean inversion. That's not what it is. In ladder logic, `~` declares the **contact type**: normally-closed (NC), conductive in its resting state. In a real ladder editor, `~` is drawn as `|/|` (NC), versus `| |` for normally-open (NO). Two different symbols, two different physical contact types — not "X" and "not X."

    Why does this matter? Because it composes naturally with how real devices are wired. Stop buttons, door interlocks, motor overload contacts, and level sensors are *typically wired NC* so that a wire break reads as "stop" instead of silently leaving the machine running. Every NC device on a real machine reads with a `~` in the rung — not because it's "alarmed" but because it's *physically wired* as normally-closed. Once you read `~` as "NC contact" instead of "not," ladder rungs start reading like wiring diagrams. Which is what they are.

```
              latch(Running)                reset(Running)
  Off -----------------------------> On ---------------------------> Off
                                      |                               |
                                      +-- StartBtn released? Still On.+
```

## Try it

```python
runner = PLCRunner(logic)
with runner.active():
    StopBtn.value = True             # NC input: True = healthy wiring

    StartBtn.value = True
    runner.step()
    assert Running.value is True

    StartBtn.value = False           # Finger off the button
    runner.step()
    assert Running.value is True     # Still running!

    StopBtn.value = False            # Stop pressed (NC opens)
    runner.step()
    assert Running.value is False
```

## A subtlety: rung order matters

What if Start and Stop are both pressed at the same time? The answer: **the last rung to write wins.** Since `reset(Running)` is below `latch(Running)`, Stop wins. This is intentional — stop always wins.

## Labeling your rungs

As programs grow, each rung benefits from a label. `comment()` attaches one to the next rung:

```python
from pyrung import comment

with Program() as logic:
    comment("Start the conveyor")
    with Rung(StartBtn):
        latch(Running)
    comment("Stop — NC contact resets when pressed or wire broken")
    with Rung(~StopBtn):
        reset(Running)
```

This isn't a Python `#` comment — it's rung metadata that travels with the program. When you export to a Click PLC, these appear above each rung in the ladder editor. From here on, we'll use `comment()` to label rungs as the logic gets more complex.

!!! info "Also known as..."

    `latch` is called `SET`, `OTL`, or `S`; `reset` is `RST`, `OTU`, or `R`. Seal-in rungs look the same in every ladder editor — Start OR-branched with Running, ANDed with the stop contact. You'll see that pattern in [Lesson 8](branches.md) as a *seal-in rung* — same behavior, single rung, self-holding via a feedback branch.

By [Lesson 11](hardware.md) you'll meet `EstopOK` — same NC wiring, different governance story. The wiring direction you're learning here is the easy part; the hard part is who *owns* the stop.

## Exercise

Build a stop-blocks-start test: start the conveyor, then press stop. Verify it stops. Then verify that pressing Start while Stop is still held does NOT restart the conveyor. (Hint: you need `~StopBtn` to block the start, not just reset after it. Think about adding `~StopBtn` as a condition on the latch rung too.)

---

The conveyor runs and stops, but there's no tracking. When a box arrives, the system needs to record its size and keep a tally. That needs data movement -- `copy` and `calc`.
