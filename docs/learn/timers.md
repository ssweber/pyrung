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
from pyrung import Bool, Int, Program, Rung, PLCRunner, TimeMode, Tms, on_delay, out

EntrySensor = Bool("EntrySensor")
DiverterCmd = Bool("DiverterCmd")
HoldDone    = Bool("HoldDone")
HoldAcc     = Int("HoldAcc")

with Program() as logic:
    with Rung(EntrySensor):
        on_delay(HoldDone, HoldAcc, preset=2000, unit=Tms)  # 2 seconds
    with Rung(EntrySensor, ~HoldDone):
        out(DiverterCmd)         # Hold diverter open while timing
```

This reads: "While the entry sensor sees a box, accumulate time. While the sensor is active and the timer hasn't finished, keep the diverter open." After 2 seconds, `HoldDone` goes true, `~HoldDone` goes false, and the diverter closes. If the sensor goes false early, the timer resets (that's `on_delay` / TON behavior).

Two tags, not one — `HoldDone` and `HoldAcc` are separate because that's how timers work in PLCs. The accumulator tracks elapsed time; the done bit fires when it reaches the preset. Real PLCs bundle these into a structured type (`TIMER` in Rockwell) or paired addresses (`T1` for the done bit, `TD1` for the accumulator in Click — pyrung borrows that convention). Either way, pyrung makes them explicit tags you can inspect, assert on, and force independently. You'll see this two-tag model again with counters in the next lesson, and it collapses back into structure members in [Lesson 9](structured-tags.md).

## Test it deterministically

```python
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10 ms per scan

with runner.active():
    EntrySensor.value = True

runner.run(cycles=199)                        # 1.99 seconds
with runner.active():
    assert DiverterCmd.value is True          # Diverter still held open

runner.step()                                 # 2.00 seconds
with runner.active():
    assert DiverterCmd.value is False         # Released -- box has passed
```

`FIXED_STEP` mode advances the clock by exactly 10 ms each scan. No wall clock. Perfectly deterministic. In pytest you'd reach for `freezegun` or monkeypatch `time.time` — pyrung bakes determinism in because PLC time *is* the scan clock. This is why pyrung exists. Try writing this test against real hardware.

## Retentive on-delay

The example above is a TON — it auto-resets when the rung goes false. What if you need the timer to *keep* its progress across rung-false cycles? That's a retentive on-delay (RTON). In pyrung, there's no separate instruction — chain `.reset()` and the behavior changes:

```python
# TON — auto-resets when rung goes False
on_delay(HoldDone, HoldAcc, preset=2000, unit=Tms)

# RTON — holds accumulator across rung-false;
# only the explicit reset clears it
on_delay(BatchDone, BatchAcc, preset=3600, unit=Ts) \
    .reset(BatchReset)
```

Without `.reset()`, the timer clears its accumulator the moment the rung drops — that's TON. With `.reset()`, the timer holds its accumulator and only clears when the reset condition fires — that's RTON. Same instruction, mode determined by the chain. This chained-builder pattern returns in [Lesson 6](counters.md) with counters.

!!! note "Why is `.reset()` terminal?"

    In Click and most ladder editors, the reset input on a retentive timer is its own wire — you can power it from the rail with completely independent conditions. That flexibility makes rungs hard to read: reset logic *looks* tied to the main rung when it isn't. pyrung makes `.reset()` terminal so the syntax matches the semantics — conditions inside `.reset(...)` belong to the reset, not the rung. If you need more instructions after, write a separate rung. Counters use the same pattern.

!!! info "Also known as..."

    On-delay is `TON` or `TMR`; off-delay is `TOF`; retentive on-delay is `RTO` or `TMRA`. The done bit is `.DN`, `.Done`, or `.Q`; the accumulator is `.ACC`, `.Acc`, or `.ET`. pyrung makes these explicit tags so you can inspect and test them.

!!! note "Why `Tms` and not `Milliseconds`?"

    Time units in pyrung are 2–3 characters: `Tms`, `Ts`, `Tm`, `Th`, `Td`. The `T` prefix mirrors IEC 61131-3 time literals (`T#2s500ms`), the short form fits Do-More's 16-character tag budget, and it sidesteps the `Min` ambiguity (minute vs minimum — plus shadowing Python's `min()`). The same convention works as a tag-name suffix: `HeatTs`, `MotorTms`, `IdleTm`.

    One naming collision worth knowing: Click uses `TD` as the timer-data (accumulator) prefix (`TD1`, `TD2`); pyrung uses `Td` as the day time-base unit. Case differs and contexts differ — no conflict in practice, but if you see `TD` in Click docs, it means accumulator, not days.

## Exercise

Build a startup delay: after pressing Start, the conveyor waits 3 seconds before the motor turns on (safety: gives workers time to clear the area). Test both paths: the full 3-second wait, and releasing Start early (timer resets, motor never starts).

---

The diverter holds long enough for one box. But how many boxes have gone to each bin? We need to count sensor edges without looping.
