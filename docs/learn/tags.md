# Lesson 2: Tags

## The Python instinct

```python
conveyor_speed: int = 0
```

Python's type hint tells you it's an integer. It doesn't tell you it's 16-bit signed, non-retentive, or mapped to a specific region of physical memory.

## The ladder logic way

```python
from pyrung import Bool, Int, Real

ConveyorSpeed = Int("ConveyorSpeed")     # 16-bit signed integer, in mm/s
SpeedLimit    = Int("SpeedLimit")        # Alarm threshold
Temperature   = Real("Temperature")     # 32-bit float
```

The name appears twice: the Python variable is how *you* reference the tag in code; the string is the tag's identity in PLC memory — it's what HMIs and tag exports see. They're allowed to differ, but matching them avoids confusion. This duplication goes away in [Lesson 9](structured-tags.md), where UDT member names *are* the tag strings.

Tags are typed and sized. You can't put a float in a Bool or store a negative number in an unsigned Word. This reflects real PLC hardware where each tag maps to a specific region of memory with a fixed width.

!!! note "A note on naming"

    Tag names in this guide use `TitleCase` (e.g. `ConveyorRunning`), not Python's `snake_case`. Two reasons:

    1. **It matches PLC convention** — what you'll see in every vendor's projects.
    2. **Characters are a budget.** Most PLCs cap tag names at 16–40 characters. `EStopPressed` fits everywhere; `e_stop_pressed` might not.

    On flat-namespace PLCs like Click, underscores group related tags into a pseudo-namespace (`Bin1_Count`, `Bin1_Full`) that becomes a real UDT member (`Bin1.Count`) on platforms with structures. More on that in [Structured Tags and Blocks](structured-tags.md).

```
  Tag Types
  +-- Bool  -- 1-bit on/off
  +-- Int   -- 16-bit signed
  +-- Dint  -- 32-bit signed
  +-- Real  -- 32-bit float
  +-- Word  -- 16-bit unsigned
  +-- Char  -- text string
```

## Retentive vs non-retentive

When a PLC goes through a STOP→RUN cycle (like a reboot), **retentive** tags keep their values and **non-retentive** tags reset to defaults. There's no Python analog — every Python variable is "retentive" until the process exits.

Bool tags are non-retentive by default: your outputs start in a known safe state. Int, Real, and others are retentive: your production counter doesn't reset to zero every time someone power-cycles the machine. This matters because a control engineer's first question about any tag is "what happens on power-up?"

## Setting values from outside the program

The program (your rungs) reads and writes tags through instructions. But you also need to set values from *outside* the program, the way an operator would type a setpoint into an HMI. In pyrung, that's `with runner:` — inside it, you read and write tag values and call `runner.step()` or `runner.run()` to execute scans.

```python
from pyrung import Bool, Int, Program, Rung, PLC, out

ConveyorSpeed = Int("ConveyorSpeed")
SpeedLimit    = Int("SpeedLimit")
OverSpeed     = Bool("OverSpeed")

with Program() as logic:
    with Rung(ConveyorSpeed > SpeedLimit):
        out(OverSpeed)

runner = PLC(logic)
with runner:
    SpeedLimit.value = 500             # Like typing into a dataview
    ConveyorSpeed.value = 300
    runner.step()
    assert OverSpeed.value is False

    ConveyorSpeed.value = 600          # Speed exceeds limit
    runner.step()
    assert OverSpeed.value is True     # Program reacts on the next scan
```

`ConveyorSpeed.value = 600` happens outside the program, before the scan. The program sees the new value when it runs and reacts accordingly. This is the same relationship an operator has with a real PLC: they set inputs and parameters, the logic does the rest.

## Exercise

Add a `BoxWeight` (Real) tag and a `WeightLimit` (Real). Write a rung that energizes a `HeavyBox` alarm when weight exceeds the limit. Test with values below and above the threshold. Then test the boundary: what happens when `BoxWeight` exactly equals `WeightLimit`? Does your rung use `>` or `>=`? Make sure it does what you intend -- in a real plant, that boundary is the difference between a nuisance alarm and a missed overweight.

---

The motor turns on and off with the button, but in a real factory you press Start and walk away. The motor needs to stay running after you release the button. That's latch and reset.

!!! info "Also known as..."

    `Bool` tags are called control relays, `C` bits, or `X`/`Y` for I/O. `Int` is a 16-bit signed type almost everywhere. `Real` is a 32-bit float. "Retentive" is universal — it's a tag's ability to survive a power cycle or STOP→RUN transition.
