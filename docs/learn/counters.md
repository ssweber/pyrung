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

But here's the catch: **a counter increments every scan while its rung is True**, not every edge. A sensor held True for 100 scans racks up 100 counts from a single box. Wrap the sensor with `rise()` — the edge detection from [Lesson 4](assignment.md) — to count edges instead. One increment per False→True transition. You'll do this almost every time you use a counter on a sensor input.

```python
from pyrung import Bool, Dint, Program, Rung, PLCRunner, count_up, rise

BinASensor = Bool("BinASensor")
BinBSensor = Bool("BinBSensor")
BinADone   = Bool("BinADone")
BinAAcc    = Dint("BinAAcc")
BinBDone   = Bool("BinBDone")
BinBAcc    = Dint("BinBAcc")
CountReset = Bool("CountReset")

with Program() as logic:
    with Rung(rise(BinASensor)):
        count_up(BinADone, BinAAcc, preset=10) \
            .reset(CountReset)
    with Rung(rise(BinBSensor)):
        count_up(BinBDone, BinBAcc, preset=10) \
            .reset(CountReset)
```

`rise(BinASensor)` fires for exactly one scan when the sensor goes from False to True. Without it, the counter would increment every scan while the sensor is active, racking up hundreds of counts per box.

The accumulators use `Dint` (32-bit) instead of `Int` because a 16-bit integer rolls over at 32,767 — on a fast line, that's a few hours of production. Production counters in real PLCs are almost always 32-bit for the same reason.

```
  rise(BinASensor)? --yes--> Acc += 1 --> Acc >= Preset? --yes--> Done = True
          |                                     |
          no                                    no
          v                                     v
      no change                           keep counting

  CountReset? --any time--> Acc = 0, Done = False
```

!!! tip "Key concept: chips, not function calls"

    Notice `.reset(CountReset)` on its own line. In Python, you'd pass all behavior into a single function call. In a ladder diagram, an instruction block is more like a **chip with multiple input pins**: the rung powers the count input, but the reset pin is a separate wire connected to its own condition. When `CountReset` goes true, the accumulator and done bit clear regardless of what the rung is doing.

    This mental model extends to every box instruction in real PLCs — timers, PID loops, message blocks, motion instructions. The `.reset()` chain is pyrung's way of drawing those extra wires.

If this looks familiar, it should — it's the same `.reset()` chain from the [retentive timer in Lesson 5](timers.md#retentive-on-delay). Counters and timers are structurally identical: both use two tags (done bit + accumulator), both chain `.reset()`, and `.reset()` is terminal for the [same reason](timers.md#retentive-on-delay).

Counters can also count in both directions. A `count_up` with a `.down()` chain becomes a bidirectional counter (CTUD) — boxes entering minus boxes leaving gives boxes currently in zone:

```python
count_up(ZoneDone, ZoneAcc, preset=50) \
    .down(BoxLeavesSensor) \
    .reset(ZoneReset)
```

Same chained-builder pattern, one more pin on the chip.

## Try it

```python
runner = PLCRunner(logic)
with runner.active():
    # Simulate 3 boxes into Bin A
    for _ in range(3):
        BinASensor.value = True
        runner.step()
        BinASensor.value = False
        runner.step()

    assert BinAAcc.value == 3
    assert BinADone.value is False

    # Simulate 7 more
    for _ in range(7):
        BinASensor.value = True
        runner.step()
        BinASensor.value = False
        runner.step()

    assert BinAAcc.value == 10
    assert BinADone.value is True   # Batch complete!
```

Notice the irony: the *test* uses `for` loops to simulate physical events, while the *logic* has no loops at all. Python where Python belongs (driving the simulation, asserting state), ladder where ladder belongs (the actual control). The boundary is the runner.

## Exercise

Add a total counter (`TotalAcc`) that counts every box regardless of which bin, triggered by an `EntrySensor`. Add a `TotalReset` button. Test that after 5 boxes (3 to Bin A, 2 to Bin B), the total is 5 and the individual counts are correct. Then reset and verify all three counters clear.

---

We have sensors, timers, counters, and a diverter. But nothing coordinates the sequence: detect a box, read its size, position the diverter, wait, count. That's a state machine.

!!! info "Also known as..."

    Counters are `CTU`/`CTD`/`CTUD`. Done bits and accumulators follow the same naming as timers. Reset is its own input pin. Edge-counting is always "one-shot feeding the counter" — never the counter itself.
