# Lesson 6: Counters

## The Python instinct

```python
count = 0
for item in items:
    count += 1
    if count >= 10:
        batch_complete = True
```

## The ladder logic way

There's no `for` loop. There's no "list of items." There's a sensor at the end of each bin chute that goes True every time a box drops in — and a counter that counts it.

A counter counts **edges**, not scans: one increment each time its rung goes from False to True. A sensor held True for 100 scans while a box sits in the chute still counts that box once. The counter has the edge detection from [Lesson 4](assignment.md) built in — it remembers what its input was the last time it ran — so you don't wrap the sensor in `rise()`.

```python
from pyrung import Bool, Counter, Program, rung, PLC, count_up

BinASensor  = Bool()
BinBSensor  = Bool()
BinACounter = Counter.clone("BinACounter")
BinBCounter = Counter.clone("BinBCounter")
CountReset  = Bool()

with Program() as logic:
    with rung(BinASensor):
        count_up(BinACounter, preset=10) \
            .reset(CountReset)
    with rung(BinBSensor):
        count_up(BinBCounter, preset=10) \
            .reset(CountReset)
```

Holding `BinASensor` True does nothing more after the first scan; the next count comes when it goes False and then True again. The memory is the counter's own and is updated every scan it runs, even while `CountReset` holds it at 0, so a sensor still True when the reset releases does not count again.

`Counter` is a built-in structured type — the mirror of `Timer`. Its `.Acc` is a Dint (32-bit) because a 16-bit integer rolls over at 32,767 — on a fast line, that's a few hours of production. Production counters in real PLCs are almost always 32-bit for the same reason.

```
  BinASensor rose? --yes--> .Acc += 1 --> .Acc >= Preset? --yes--> .Done = True
          |                                     |
          no                                    no
          v                                     v
      no change                           keep counting

  CountReset? --any time--> .Acc = 0, .Done = False
```

!!! tip "Key concept: chips, not function calls"

    Notice `.reset(CountReset)` on its own line. In Python, you'd pass all behavior into a single function call. In a ladder diagram, an instruction block is more like a **chip with multiple input pins**: the rung powers the count input, but the reset pin is a separate wire connected to its own condition. When `CountReset` goes true, the accumulator and done bit clear regardless of what the rung is doing.

    This mental model extends to every box instruction in real PLCs — timers, PID loops, message blocks, motion instructions. The `.reset()` chain is pyrung's way of drawing those extra wires.

If this looks familiar, it should — it's the same `.reset()` chain from the [retentive timer in Lesson 5](timers.md#retentive-on-delay). Counters and timers are structurally identical: both are built-in types with `.Done` and `.Acc` fields, both chain `.reset()`, and `.reset()` is terminal for the [same reason](timers.md#retentive-on-delay).

Counters can also count in both directions. A `count_up` with a `.down()` chain becomes a bidirectional counter (CTUD) — boxes entering minus boxes leaving gives boxes currently in zone:

```python
ZoneCounter = Counter.clone("ZoneCounter")

count_up(ZoneCounter, preset=50) \
    .down(BoxLeavesSensor) \
    .reset(ZoneReset)
```

Same chained-builder pattern, one more pin on the chip.

## Try it

```python
with PLC(logic) as plc:
    # Simulate 3 boxes into Bin A
    for _ in range(3):
        BinASensor.value = True
        plc.step()
        BinASensor.value = False
        plc.step()

    assert BinACounter.Acc.value == 3
    assert BinACounter.Done.value is False

    # Simulate 7 more
    for _ in range(7):
        BinASensor.value = True
        plc.step()
        BinASensor.value = False
        plc.step()

    assert BinACounter.Acc.value == 10
    assert BinACounter.Done.value is True   # Batch complete!
```

Notice the irony: the *test* uses `for` loops to simulate physical events, while the *logic* has no loops at all. Python where Python belongs (driving the simulation, asserting state), ladder where ladder belongs (the actual control). The boundary is the runner.

## Exercise

Add a `TotalCounter` that counts every box regardless of which bin, triggered by an `EntrySensor`. Add a `TotalReset` button. Test that after 5 boxes (3 to Bin A, 2 to Bin B), the total is 5 and the individual counts are correct. Then reset and verify all three counters clear.

---

We have sensors, timers, counters, and a diverter. But nothing coordinates the sequence: detect a box, read its size, position the diverter, wait, count. That's a state machine.

!!! info "Also known as..."

    Counters are `CTU`/`CTD`/`CTUD`. Done bits and accumulators follow the same naming as timers. Reset is its own input pin. CLICK counters count rising edges of their inputs, as pyrung's do; on other platforms check whether the counter itself is edge-triggered before dropping the one-shot.
