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

There's no `for` loop. There's no "list of items." There's a sensor at the end of each bin chute that goes True every time a box drops in. You count the edges.

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

Notice `.reset(CountReset)` on its own line below the counter. In Python, you'd pass all behavior into a single function call or handle reset in separate logic. In a ladder diagram, an instruction block like a counter is more like a chip with multiple input pins: the rung powers the count input, but the reset pin is a separate wire connected to its own condition. When `CountReset` goes true, the counter's accumulator and done bit clear regardless of what the rung is doing. Timers have the same pattern.

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

## Exercise

Add a total counter (`TotalAcc`) that counts every box regardless of which bin, triggered by an `EntrySensor`. Add a `TotalReset` button. Test that after 5 boxes (3 to Bin A, 2 to Bin B), the total is 5 and the individual counts are correct. Then reset and verify all three counters clear.

---

We have sensors, timers, counters, and a diverter. But nothing coordinates the sequence: detect a box, read its size, position the diverter, wait, count. That's a state machine.
