# Know Python? Learn Ladder Logic.

> A guided introduction to PLC programming for Python developers, using [pyrung](https://ssweber.github.io/pyrung/).

You know Python. You've never touched a PLC. This guide teaches you ladder logic, the dominant programming language of industrial automation, using tools you already have: Python, VS Code, and pytest. No hardware. No proprietary software. No Windows VM.

pyrung won't let you cheat: if you try to write a `for` loop where a scan cycle belongs, it'll tell you. That's the point. You're learning a different way of thinking about programs, and the guardrails are there to keep you honest.

---

## How this guide works

Each lesson introduces one ladder logic concept, shows you the Python you'd instinctively reach for, then shows you the ladder logic way and *why* it works that way in a machine that controls physical things. Every lesson ends with an exercise you can run and test.

**Prerequisites:** Python 3.11+, basic pytest knowledge, a text editor.

```bash
pip install pyrung
```

---

## Lesson 1: The Scan Cycle

### The Python instinct

```python
# You'd write this
if button_pressed:
    light = True
else:
    light = False
```

This runs once. A PLC doesn't run once. It runs in a **scan cycle**, an infinite loop that evaluates every line of logic, top to bottom, hundreds of times per second. Always. Forever. Even when nothing is happening.

### Why?

Because a PLC controls physical things. A conveyor belt doesn't stop needing instructions and a valve doesn't pause while you wait for user input. The machine is always running, so the logic is always running.

### The ladder logic way

```python
from pyrung import Bool, Program, Rung, PLCRunner, out

Button = Bool("Button")
Light = Bool("Light")

with Program() as logic:
    with Rung(Button):
        out(Light)
```

Read it aloud: "On this rung, if Button is true, energize Light." Every scan, this rung is evaluated, and `out` automatically makes Light follow the rung's power state. No `if/else` needed.

If you've seen ladder logic in a textbook or an editor, it looks something like this:

```
    |  Button       Light  |
    |--[ ]----------( )----|
```

The left rail is power. `[ ]` is a contact (condition). `( )` is a coil (output). If the contact closes, power flows through and the coil energizes. pyrung's `with Rung(Button): out(Light)` is the same thing expressed in Python.

### Try it

```python
runner = PLCRunner(logic)
with runner.active():
    Button.value = True
    runner.step()               # One scan
    assert Light.value is True

    Button.value = False
    runner.step()               # Next scan
    assert Light.value is False # Light follows Button, every scan
```

### Key concept: `out` is not assignment

`Light = True` in Python sets a value once. `out(Light)` means "Light follows this rung's power state, every single scan." If two rungs both `out` the same tag, the last one wins. This is how real PLCs work.

### Exercise 1

Create a program with two buttons and a light. The light should be on when *either* button is pressed. (Hint: check the [Conditions reference](https://ssweber.github.io/pyrung/instructions/conditions/) for ways to combine conditions.)

---

## Lesson 2: Tags

### The Python instinct

```python
motor_running = False  # Create it, set it, done
```

### The ladder logic way

```python
from pyrung import Bool, Int, Real

MotorRunning = Bool("MotorRunning")   # 1 bit
Speed        = Int("Speed")           # 16-bit signed integer
Temperature  = Real("Temperature")    # 32-bit float
```

Tags are typed and sized. You can't put a float in a Bool or store a negative number in an unsigned Word. This reflects real PLC hardware where each tag maps to a specific region of memory with a fixed width.

The important distinction is **retentive** vs **non-retentive**. When a PLC goes through a STOP→RUN cycle (like a reboot), retentive tags keep their values and non-retentive tags reset to defaults. Bool tags are non-retentive by default: your outputs start in a known safe state. Int, Real, and others are retentive: your production counter doesn't reset to zero every time someone power-cycles the machine.

### Setting values from outside the program

The program (your rungs) reads and writes tags through instructions. But you also need to set values from *outside* the program, the way an operator would type a setpoint into an HMI or a dataview window. In pyrung, that's the `runner.active()` block:

```python
from pyrung import Bool, Real, Program, Rung, PLCRunner, out

Alarm    = Bool("Alarm")
Setpoint = Real("Setpoint")

with Program() as logic:
    with Rung(Setpoint > 100.0):
        out(Alarm)

runner = PLCRunner(logic)
with runner.active():
    Setpoint.value = 50.0          # Like typing into a dataview
    runner.step()
    assert Alarm.value is False

    Setpoint.value = 150.0         # Change the setpoint
    runner.step()
    assert Alarm.value is True     # Program reacts on the next scan
```

`Setpoint.value = 150.0` happens outside the program, before the scan. The program sees the new value when it runs and reacts accordingly. This is the same relationship an operator has with a real PLC: they set inputs and parameters, the logic does the rest.

### Exercise 2

Create an Int tag called `Count` and a Bool called `Alarm`. Write a rung that energizes the Alarm when Count is greater than 10. From outside the program, set Count to 5, step, and verify the Alarm is off. Then set it to 15, step, and verify the Alarm is on.

---

## Lesson 3: Latch and Reset

### The Python instinct

```python
if start_pressed:
    motor_running = True
# But what turns it off?
# And what if start_pressed goes False?
```

### The problem

In the real world, you press a momentary "Start" button. Your finger comes off. The motor should keep running. `out` won't work here because it de-energizes the moment the rung goes false.

### The ladder logic way

```python
from pyrung import Bool, Program, Rung, latch, reset

Start   = Bool("Start")
Stop    = Bool("Stop")
Running = Bool("Running")

with Program() as logic:
    with Rung(Start):
        latch(Running)       # SET: Running = True, stays True
    with Rung(Stop):
        reset(Running)       # RESET: Running = False
```

`latch` is sticky. Once set, it stays set until explicitly `reset`. This is the bread and butter of motor control, alarm acknowledgment, and mode selection in every factory on earth.

### Try it

```python
runner = PLCRunner(logic)
with runner.active():
    Start.value = True
    runner.step()
    assert Running.value is True

    Start.value = False        # Finger off the button
    runner.step()
    assert Running.value is True   # Still running!

    Stop.value = True
    runner.step()
    assert Running.value is False  # Now it stops
```

### A subtlety: rung order matters

What if Start and Stop are both pressed at the same time? The answer: **the last rung to write wins.** Since `reset(Running)` is below `latch(Running)`, Stop wins. This is intentional. In industrial safety, stop always wins. Rung ordering is a design decision.

### Exercise 3

Build a "toggle" pattern: one button press turns a light on, the next press turns it off. (Hint: you'll need `rise()` for edge detection, see the [Conditions reference](https://ssweber.github.io/pyrung/instructions/conditions/). Think about why you can't just use `Button` as the condition.)

---

## Lesson 4: Assignment

### The Python instinct

```python
state = "green"
speed = speed + 10
total = price * quantity
```

Assignment is so fundamental in Python that it barely registers as a concept. You have `=` and you're done.

### The ladder logic way

In ladder logic, moving data is an explicit instruction that lives on the instruction side of a rung. It executes when the rung is true and does nothing when the rung is false.

```python
from pyrung import Bool, Int, Char, Program, Rung, copy, calc

State    = Char("State")
Speed    = Int("Speed")
Total    = Int("Total")
Price    = Int("Price")
Quantity = Int("Quantity")
GoFast   = Bool("GoFast")
NextStep = Bool("NextStep")

with Program() as logic:
    with Rung(NextStep):
        copy("y", State)              # State = "y"

    with Rung(GoFast):
        calc(Speed + 10, Speed)       # Speed = Speed + 10

    with Rung():
        calc(Price * Quantity, Total)  # Total = Price * Quantity (every scan)
```

`copy` moves a value into a tag. `calc` evaluates an expression and stores the result. Both are instructions that only execute when their rung has power. A `copy` inside a rung that's false simply doesn't happen, and the destination keeps whatever value it had.

### copy vs calc

These two handle overflow differently, and the difference matters. `copy` clamps: if you copy 50000 into a 16-bit signed Int, you get 32767 (the max). `calc` wraps: if an Int at 32767 has 1 added, it rolls to -32768. Clamping is safer for data movement; wrapping matches how real PLC arithmetic hardware behaves.

### Unconditional rungs

Notice `Rung()` with no condition. That rung is always true, so its instructions execute every scan. This is how you compute values that should always be current, like a running total or a scaled analog reading.

### Exercise 4

Create a step counter that starts at 0. Each time a button is pressed (use `rise()`), copy the current step into a `PreviousStep` tag, then `calc` the step plus 1 back into `Step`. Test that after 3 presses, `Step` is 3 and `PreviousStep` is 2.

---

## Lesson 5: Timers

### The Python instinct

```python
import time
time.sleep(3)  # Block for 3 seconds
motor_running = True
```

### Why that's wrong here

A PLC can't sleep. It has to keep scanning because sensors are still reading, safety interlocks are still being checked, and other rungs still need to execute. Blocking is not an option when you're controlling physical equipment.

### The ladder logic way

Timers **accumulate** across scans: every scan where the rung is true, the timer adds a little more time, and when the accumulator reaches the preset, it fires.

```python
from pyrung import Bool, Int, Program, Rung, Tms, on_delay, latch

Start     = Bool("Start")
Running   = Bool("Running")
DelayDone = Bool("DelayDone")
DelayAcc  = Int("DelayAcc")

with Program() as logic:
    with Rung(Start):
        on_delay(DelayDone, DelayAcc, preset=3000, unit=Tms)  # 3 seconds
    with Rung(DelayDone):
        latch(Running)
```

This reads: "While Start is pressed, accumulate time. After 3000 ms, set DelayDone. When DelayDone is true, latch Running." If you release Start before 3 seconds, the accumulator resets (that's `on_delay` / TON behavior).

### Test it deterministically

```python
from pyrung import PLCRunner, TimeMode

runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10 ms per scan

with runner.active():
    Start.value = True

runner.run(cycles=299)                        # 2.99 seconds
with runner.active():
    assert Running.value is False             # Not yet

runner.step()                                 # 3.00 seconds
with runner.active():
    assert Running.value is True              # Now!
```

`FIXED_STEP` mode advances the clock by exactly 10 ms each scan. No wall clock. Perfectly deterministic. This is why pyrung exists. Try writing this test against real hardware.

### Exercise 5

Build a "press and hold" button: the motor only starts if you hold the Start button for 2 full seconds. If you release early, nothing happens. Test both paths: the successful hold and the early release.

---

## Lesson 6: Counters

### The Python instinct

```python
count = 0
for item in items:
    count += 1
    if count >= 10:
        batch_complete = True
```

### The ladder logic way

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

### Exercise 6

Count 5 button presses, then turn on a light. Add a reset button that clears the count and turns the light off. Test the full sequence: 4 presses (light still off), 5th press (light on), reset (light off, count zero).

---

> If you're a visual person, this is a good time to set up the [VS Code debugger](https://ssweber.github.io/pyrung/guides/dap-vscode/). From here on, the logic gets complex enough that stepping through scans and watching tags update live can be more useful than reading assertions.

## Lesson 7: State Machines

### The Python instinct

```python
state = "green"
while True:
    if state == "green":
        time.sleep(3)
        state = "yellow"
    elif state == "yellow":
        # ...
```

### The ladder logic way

State machines in ladder logic use a tag for the current state, timers for durations, and `copy` for transitions. No `while`, no `sleep`, no blocking.

```python
from pyrung import Char, Bool, Int, Program, Rung, Tms, on_delay, copy

State      = Char("State")
GreenDone  = Bool("GreenDone")
GreenAcc   = Int("GreenAcc")
YellowDone = Bool("YellowDone")
YellowAcc  = Int("YellowAcc")
RedDone    = Bool("RedDone")
RedAcc     = Int("RedAcc")

with Program() as logic:
    with Rung(State == "g"):
        on_delay(GreenDone, GreenAcc, preset=3000, unit=Tms)
    with Rung(GreenDone):
        copy("y", State)

    with Rung(State == "y"):
        on_delay(YellowDone, YellowAcc, preset=1000, unit=Tms)
    with Rung(YellowDone):
        copy("r", State)

    with Rung(State == "r"):
        on_delay(RedDone, RedAcc, preset=3000, unit=Tms)
    with Rung(RedDone):
        copy("g", State)
```

Each state has two rungs: one to run its timer, one to handle the transition. Clean, readable, testable.

### Exercise 7

Extend the traffic light to include a "walk request" button. When pressed during the green phase, the light should complete its current green time, go through yellow, then hold red for 5 seconds (instead of the normal 3) before returning to green. Test the normal cycle and the walk-request cycle.

---

## Lesson 8: Branches and OR Logic

### The Python instinct

```python
if auto_mode and ready:
    start_pump()
elif manual_mode and button_pressed:
    start_pump()
```

### The ladder logic way

Ladder logic has two ways to combine conditions. For OR-ing two Bool tags together, use `|`:

```python
from pyrung import Bool, Int, Program, Rung, branch, out, latch, any_of, all_of

Auto       = Bool("Auto")
Manual     = Bool("Manual")
Estop      = Bool("Estop")
Ready      = Bool("Ready")
PumpButton = Bool("PumpButton")
Pump       = Bool("Pump")
Power      = Bool("Power")
Light      = Bool("Light")
Mode       = Int("Mode")

with Program() as logic:
    # | for OR-ing two Bool conditions
    with Rung(Auto | Manual):
        out(Light)                        # Light when either mode is active

    # any_of for OR-ing comparisons or more than two conditions
    with Rung(any_of(Mode == 1, Mode == 3, Mode == 5)):
        latch(Pump)
```

Use `|` when you're OR-ing two Bool tags. Use `any_of` when you're OR-ing comparisons or have more than two conditions.

### Branches

A `branch` creates a parallel path within a rung. Think of it as a second wire that ANDs its condition with the parent's.

```python
with Program() as logic:
    with Rung(Auto):
        out(Light)                        # Light when Auto
        with branch(Ready):
            out(Pump)                     # Pump when Auto AND Ready
```

These combine naturally. Here's a safety rung where power stays on when the E-stop isn't pressed, and the pump runs in Auto mode or when Manual mode and the pump button are both active:

```python
with Program() as logic:
    with Rung(~Estop):
        out(Power)
        with branch(Auto | all_of(Manual, PumpButton)):
            out(Pump)
```

Important: **all conditions evaluate before any instructions execute.** The branch doesn't "see" results of instructions above it in the same rung because each rung starts from a clean snapshot.

### Exercise 8

Build a three-mode system: Auto, Manual, and Off. In Auto mode, a pump runs when a level sensor is high. In Manual mode, the pump runs when a manual button is pressed. In Off mode, nothing runs. Test all three modes, and test that switching from Auto to Manual mid-run changes the control source.

---

## Lesson 9: Structured Tags and Blocks

### The Python instinct

```python
@dataclass
class Motor:
    running: bool = False
    speed: int = 0
    fault: bool = False

readings = [0] * 10
```

Python has dataclasses for structured records and lists for arrays. Ladder logic has both too, but they map to fixed regions of PLC memory.

### UDTs

```python
from pyrung import udt, Bool, Int, Real, Program, Rung, out, latch

@udt()
class Motor:
    running: Bool
    speed: Int
    fault: Bool

with Program() as logic:
    with Rung(Motor.running):
        out(StatusLight)
```

When you have multiple instances of the same kind of thing (three pumps, four valves), use `count`:

```python
@udt(count=3)
class Pump:
    running: Bool
    flow: Real
    fault: Bool

# Each instance accessed by index
with Rung(Pump[0].fault):
    latch(AlarmLight)
```

This maps directly to how real plants are organized: identical equipment, replicated logic, consistent naming. When all fields share the same type (like a group of Int fields for one sensor), pyrung also offers `named_array`, which maps to contiguous memory and supports bulk operations. See the [Tag Structures guide](https://ssweber.github.io/pyrung/guides/tag-structures/) for details.

### Blocks

When you need an array of same-typed tags rather than a structured record, a `Block` gives you a contiguous range you can index into and operate on in bulk. In Python you'd use a list; in ladder logic a block is a named region of PLC memory.

```python
from pyrung import Bool, Int, Block, TagType, Program, Rung, copy, blockcopy

readings    = Block("Readings", TagType.INT, 1, 10)    # Readings1..Readings10
NewReading  = Bool("NewReading")
SensorValue = Int("SensorValue")

with Program() as logic:
    with Rung(NewReading):
        blockcopy(readings.select(1, 9), readings.select(2, 10))    # shift everything down one slot
        copy(SensorValue, readings[1])                                # insert new value at the front
```

`readings.select(1, 9)` gives you Readings1 through Readings9 as a range, and `blockcopy` moves the whole thing in one instruction. The oldest value in Readings10 falls off the end. This is the ladder equivalent of `readings.insert(0, new_value)`: no loops, no index arithmetic.

### Exercise 9

Define a `Conveyor` UDT with fields for `running` (Bool), `speed` (Int), `jammed` (Bool), and `count` (Dint). Create 2 instances. Write logic where each conveyor runs only if it's not jammed, and a counter tracks items on each conveyor using edge-triggered counting. Test that jamming conveyor 0 stops it without affecting conveyor 1.

---

## Lesson 10: Testing Like You Mean It

This is where pyrung pays for itself. Everything you've built so far is testable with pytest.

```python
import pytest
from pyrung import PLCRunner, TimeMode

@pytest.fixture
def runner():
    r = PLCRunner(logic)
    r.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
    return r

def test_start_stop(runner):
    with runner.active():
        Start.value = True
    runner.step()
    with runner.active():
        assert Running.value is True

def test_stop_overrides_start(runner):
    """Safety: if both pressed, stop wins (last rung wins)."""
    with runner.active():
        Start.value = True
        Stop.value = True
    runner.step()
    with runner.active():
        assert Running.value is False
```

You can also use **forces** for persistent overrides across multiple scans:

```python
def test_sensor_stuck_high(runner):
    """Simulate a sensor failure, stuck on."""
    runner.add_force("Sensor", True)
    runner.run(cycles=1000)
    runner.remove_force("Sensor")
    # Assert the logic handled the stuck sensor correctly
```

And **history** to inspect past states:

```python
runner.step()
runner.step()
runner.step()
# Every scan is an immutable snapshot you can inspect, diff, or rewind
previous = runner.history[-2]    # two scans ago
```

### When tests aren't enough

Sometimes you need to watch logic execute step by step. pyrung includes a VS Code debugger that lets you set breakpoints on individual rungs, step through scans one at a time, watch tag values update live, and force overrides from the debug console. If you've ever debugged Python in VS Code, it works the same way, just with scans instead of lines. See the [DAP Debugger guide](https://ssweber.github.io/pyrung/guides/dap-vscode/) for setup.

### Exercise 10

Write a test suite for your traffic light from Lesson 7. Cover: normal full cycle, walk request during green, walk request during red (should have no effect), and timing precision (assert exact scan counts for each transition).

---

## Lesson 11: From Simulation to Hardware

The lessons are done. Everything from here is about taking what you've built and connecting it to the physical world.

### Option A: Connect via Modbus

Run your program behind a Modbus TCP interface. HMIs, SCADA systems, [ClickNick](https://github.com/ssweber/clicknick)'s Dataview window, or other PLCs can connect to your running pyrung program and read or write tags as if it were a Click PLC. Useful for testing your HMI layouts and integration logic without hardware.

### Option B: Map to a Click PLC

```python
from pyrung.click import x, y, ds, TagMap, pyrung_to_ladder

mapping = TagMap({
    Start:   x[1],       # Physical input terminal 1
    Stop:    x[2],       # Physical input terminal 2
    Running: y[1],       # Physical output terminal 1
})

mapping.validate(logic)                    # Check against Click constraints
pyrung_to_ladder(logic, mapping, "motor/") # Export ladder CSV + nicknames
```

The validator will tell you exactly what your program does that a Click PLC can't handle. For example, pyrung lets you write `Rung(Temp + Offset > 150.0)` with math directly in the condition, but Click requires you to `calc` that into a separate tag first. The validator catches this and tells you what to fix.

Once it's clean, `pyrung_to_ladder` generates the ladder CSV files and nickname mappings. From there, [ClickNick](https://github.com/ssweber/clicknick)'s Guided Paste walks you through importing the ladder into Click, file by file, with the confidence that it's already been tested.

For a full reference on memory banks, address mapping, and `named_array` patterns for Click, see the [Click Cheatsheet](https://ssweber.github.io/pyrung/guides/click-cheatsheet/).

### Option C: Generate CircuitPython for a P1AM-200

```python
from pyrung.circuitpy import P1AM, generate_circuitpy

hw = P1AM()
inputs  = hw.slot(1, "P1-08SIM")   # 8-ch discrete input
outputs = hw.slot(2, "P1-08TRS")   # 8-ch discrete output

source = generate_circuitpy(logic, hw, target_scan_ms=10.0)
```

This produces a self-contained CircuitPython file with a `while True` scan loop that runs the same logic directly on a P1AM-200 microcontroller with Productivity1000 I/O modules. Copy it to the board and it runs. Same logic, same behavior, real hardware.

---

## Where to go from here

You now understand the fundamentals: scans, tags, contacts, coils, latches, timers, counters, state machines, branches, structured tags, and testing. That covers the majority of real-world ladder logic programs.

For deeper topics, the pyrung docs cover:

- [Data movement](https://ssweber.github.io/pyrung/instructions/copy/): `copy`, `blockcopy`, `fill`, type conversion
- [Math](https://ssweber.github.io/pyrung/instructions/math/): `calc()`, overflow behavior, range sums
- [Tag structures](https://ssweber.github.io/pyrung/guides/tag-structures/): named arrays, cloning, field defaults, hardware mapping
- [Drum sequencers, shift registers, search](https://ssweber.github.io/pyrung/instructions/drum-shift-search/): advanced pattern instructions
- [Subroutines and program control](https://ssweber.github.io/pyrung/instructions/program-control/): `call`, `forloop`, multi-program structure
- [Communication](https://ssweber.github.io/pyrung/instructions/communication/): Modbus `send`/`receive`
- [VS Code debugger](https://ssweber.github.io/pyrung/guides/dap-vscode/): step through scans, set breakpoints on rungs, watch tags live
- [Click PLC dialect](https://ssweber.github.io/pyrung/dialects/click/): full hardware mapping and validation
- [CircuitPython deployment](https://ssweber.github.io/pyrung/dialects/circuitpy/): generate code for P1AM-200

---

*Built with [pyrung](https://github.com/ssweber/pyrung). Write ladder logic in Python, simulate it, test it, deploy it.*
