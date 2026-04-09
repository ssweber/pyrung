# Your First Click PLC Program

Write a motor start/stop circuit, test it, map it to Click hardware addresses, export ladder CSV, and load it into Click via [ClickNick](https://github.com/ssweber/clicknick).

## The program

A sealed motor circuit: press Start to latch the motor on, press Stop to reset it off. A speed input copies through only while the motor runs.

```python
from pyrung import Bool, Real, PLC, Program, Rung, copy, latch, reset, rise
from pyrung.click import x, y, ds, df, TagMap

# Semantic tags — no hardware addresses yet
StartButton  = Bool("StartButton")
StopButton   = Bool("StopButton")
MotorRunning = Bool("MotorRunning")
Speed        = Real("Speed")
DisplaySpeed = Real("DisplaySpeed")

with Program() as logic:
    with Rung(rise(StartButton)):
        latch(MotorRunning)

    with Rung(rise(StopButton)):
        reset(MotorRunning)

    with Rung(MotorRunning):
        copy(Speed, DisplaySpeed)
```

`rise()` triggers on the rising edge — one scan pulse when the button transitions from off to on. Without it, holding Start would re-latch every scan (harmless here, but wrong for counting or toggling).

## Test it

```python
def test_motor_start_stop():
    runner = PLC(logic, dt=0.1)

    # Start the motor
    runner.patch({StartButton: True})
    runner.step()
    with runner:
        assert MotorRunning.value is True

    # Release button — motor stays latched
    runner.run(cycles=5)
    with runner:
        assert MotorRunning.value is True

    # Stop the motor
    runner.patch({StopButton: True})
    runner.step()
    with runner:
        assert MotorRunning.value is False

    # Speed only copies while running
    runner.patch({StartButton: True, Speed: 75.0})
    runner.step()
    with runner:
        assert DisplaySpeed.value == 75.0

    runner.patch({StopButton: True})
    runner.step()
    runner.patch({Speed: 99.0})
    runner.step()
    with runner:
        assert DisplaySpeed.value == 75.0  # Didn't update — motor is off
```

Same logic, deterministic timing, real assertions. Run with `pytest`.

## Map to Click hardware

Once the logic is correct, map semantic tags to Click memory addresses:

```python
mapping = TagMap({
    StartButton:  x[1],       # X001 — discrete input
    StopButton:   x[2],       # X002
    MotorRunning: y[1],       # Y001 — discrete output
    Speed:        df[1],      # DF1  — float register (analog input)
    DisplaySpeed: df[11],     # DF11
})
```

Validate that the program fits Click's constraints:

```python
report = mapping.validate(logic, mode="warn")
print(report.summary())
```

The validator checks type compatibility, instruction support, and addressing limits. Fix any findings, then tighten to `mode="strict"` when the program is clean.

## Export ladder CSV

```python
from pyrung.click import pyrung_to_ladder

bundle = pyrung_to_ladder(logic, mapping)
bundle.write("./output")  # writes main.csv + subroutines/*.csv
```

This produces deterministic Click ladder CSV files ready for import.

## Load into Click via ClickNick

[ClickNick](https://github.com/ssweber/clicknick) loads the exported CSV into Click Programming Software via the clipboard. In the GUI, use **Ladder → Open in Guided Paste...** and point it at the output folder. Or from the command line:

```
clicknick-rung program load ./output
```

Either way, ClickNick walks you through pasting each rung and subroutine into Click. The addresses, nicknames, and logic are all wired up.

## Next steps

- [Click PLC Dialect](../dialects/click.md) — pre-built blocks, TagMap details, validation findings, nickname CSV I/O
- [Click Python Codegen](../dialects/click-codegen.md) — round-trip: import Click ladder CSV back into pyrung
- [Testing Guide](testing.md) — forces, time travel, forking, pytest patterns
- [ClickNick](https://github.com/ssweber/clicknick) — the companion tool for Click clipboard I/O and nickname management
