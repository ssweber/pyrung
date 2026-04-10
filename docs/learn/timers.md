# Lesson 5: Timers

## The Python instinct

```python
import time
diverter_open = True
time.sleep(2)  # Block for 2 seconds while the box passes
diverter_open = False
```

## Why that's wrong here

A PLC can't sleep. It has to keep scanning because sensors are still reading, safety interlocks are still being checked, and other rungs still need to execute. Blocking is not an option when you're controlling physical equipment.

## The ladder logic way

Timers **accumulate** across scans: every scan where the rung is true, the timer adds a little more time, and when the accumulator reaches the preset, it fires.

```
  Each scan:
      Rung true? --yes--> Acc += elapsed --> Acc >= Preset? --yes--> Done = True
          |                                       |
          no                                      no
          v                                       v
      Acc resets to 0                       keep timing
```

The diverter gate needs to stay open for 2 seconds while a box passes through. Here's how:

```python
from pyrung import Bool, Timer, Program, Rung, PLC, on_delay, out

EntrySensor = Bool("EntrySensor")
DiverterCmd = Bool("DiverterCmd")
HoldTimer   = Timer.clone("HoldTimer")

with Program() as logic:
    with Rung(EntrySensor):
        on_delay(HoldTimer, preset=2000, unit="Tms")  # 2 seconds
    with Rung(EntrySensor, ~HoldTimer.Done):
        out(DiverterCmd)         # Hold diverter open while timing
```

This reads: "While the entry sensor sees a box, accumulate time. While the sensor is active and the timer hasn't finished, keep the diverter open." After 2 seconds, `HoldTimer.Done` goes true, `~HoldTimer.Done` goes false, and the diverter closes. If the sensor goes false early, the timer resets (that's `on_delay` / TON behavior).

`Timer` is a built-in structured type with two fields: `.Done` (Bool) fires when the accumulator reaches the preset, and `.Acc` (Int) tracks elapsed time. `Timer.clone("HoldTimer")` creates a named timer clone — in the PLC tag table, that expands to `HoldTimer_Done` and `HoldTimer_Acc`. You'll see the same two-field model again with counters in the next lesson.

!!! tip "Name your timers"

    For real programs deploying to hardware, always use `Timer.clone("Name")`. When `Timer1_Done` shows up in a fault log six months later, it tells you nothing. `HoldTimer_Done` tells you everything. `Timer[n]` (anonymous, auto-numbered) is fine for throwaway simulation tests — but named instances are the 95% case.

!!! note "Why `\"Tms\"` and not `\"Milliseconds\"`?"

    Time units in pyrung are 2–3 character strings: `"Tms"`, `"Ts"`, `"Tm"`, `"Th"`, `"Td"`. The `T` prefix mirrors IEC 61131-3 time literals, the short form fits PLC tag-name limits, and it sidesteps the `Min` ambiguity (minute vs minimum — plus shadowing Python's `min()`).

## Test it deterministically

```python
with PLC(logic, dt=0.010) as plc:
    EntrySensor.value = True

    plc.run(cycles=199)                        # 1.99 seconds
    assert DiverterCmd.value is True           # Diverter still held open

    plc.step()                                 # 2.00 seconds
    assert DiverterCmd.value is False          # Released -- box has passed
```

`dt=0.010` advances the clock by exactly 10 ms each scan. No wall clock. Perfectly deterministic. In pytest you'd reach for `freezegun` or monkeypatch `time.time` — pyrung bakes determinism in because PLC time *is* the scan clock. This is why pyrung exists. Try writing this test against real hardware.

## Retentive on-delay

The example above is a TON — it auto-resets when the rung goes false. What if you need the timer to *keep* its progress across rung-false cycles? That's a retentive on-delay (RTON). In pyrung, there's no separate instruction — chain `.reset()` and the behavior changes:

```python
# TON — auto-resets when rung goes False
on_delay(HoldTimer, preset=2000, unit="Tms")

# RTON — holds accumulator across rung-false;
# only the explicit reset clears it
on_delay(BatchTimer, preset=3600, unit="Ts") \
    .reset(BatchReset)
```

Without `.reset()`, the timer clears its accumulator the moment the rung drops — that's TON. With `.reset()`, the timer holds its accumulator and only clears when the reset condition fires — that's RTON. Same instruction, mode determined by the chain. This chained-builder pattern returns in [Lesson 6](counters.md) with counters.

!!! note "Why is `.reset()` terminal?"

    In most ladder editors, the reset input on a retentive timer is its own wire — you can power it from the rail with completely independent conditions. That flexibility makes rungs hard to read: reset logic *looks* tied to the main rung when it isn't. pyrung makes `.reset()` terminal so the syntax matches the semantics — conditions inside `.reset(...)` belong to the reset, not the rung. If you need more instructions after, write a separate rung. Counters use the same pattern.

## Exercise

Build a startup delay: after pressing Start, the conveyor waits 3 seconds before the motor turns on (safety: gives workers time to clear the area). Test both paths: the full 3-second wait, and releasing Start early (timer resets, motor never starts).

---

The diverter holds long enough for one box. But how many boxes have gone to each bin? We need to count sensor edges without looping.

!!! info "Also known as..."

    On-delay is `TON`; off-delay is `TOF`; retentive on-delay is `RTO`. The done bit is `.DN` or `.Q`; the accumulator is `.ACC` or `.ET`.
