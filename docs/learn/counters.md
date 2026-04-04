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

There's no `for` loop. There's no "list of items." There's a sensor that goes True every time a box passes by. You count the edges.

```python
from pyrung import Bool, Dint, Program, Rung, count_up, rise

Sensor    = Bool("Sensor")
BatchDone = Bool("BatchDone")
BatchAcc  = Dint("BatchAcc")
BatchRst  = Bool("BatchReset")

with Program() as logic:
    with Rung(rise(Sensor)):
        count_up(BatchDone, BatchAcc, preset=10) \
            .reset(BatchRst)
```

`rise(Sensor)` fires for exactly one scan when Sensor goes from False to True. Without it, the counter would increment every scan while the sensor is active, racking up hundreds of counts per box.

Notice `.reset(BatchRst)` on its own line below the counter. In Python, you'd pass all behavior into a single function call or handle reset in separate logic. In a ladder diagram, an instruction block like a counter is more like a chip with multiple input pins: the rung powers the count input, but the reset pin is a separate wire connected to its own condition. When `BatchRst` goes true, the counter's accumulator and done bit clear regardless of what the rung is doing. Timers have the same pattern, and you'll see `.reset()` on retentive timers and bidirectional counters too.

## Exercise

Count 5 button presses, then turn on a light. Add a reset button that clears the count and turns the light off. Test the full sequence: 4 presses (light still off), 5th press (light on), reset (light off, count zero).
