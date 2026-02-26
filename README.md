# pyrung

**Write ladder logic in Python. Simulate it. Test it. Deploy it.**

pyrung turns Python's `with` block into a ladder rung — condition on the rail, instructions in the body.

```python
from pyrung import Bool, PLCRunner, Program, Rung, out

Button = Bool("Button")
Light = Bool("Light")

with Program() as logic:
    with Rung(Button):
        out(Light)

runner = PLCRunner(logic)
with runner.active():
    Button.value = True
    runner.step()
    assert Light.value is True
```

> **Status:** Core engine, Click PLC dialect, CircuitPython dialect, and VS Code debugger are implemented and tested (~19k lines, 1,100+ tests). Not yet on PyPI. API may still change.

## Why?

AutomationDirect CLICK PLCs have no built-in simulator. You write logic, download it to hardware, and hope. pyrung lets you **test first** — same tag names, deterministic scans, real assertions. When it works, transpose it to Click.

Or don't transpose at all. Run your program as a **soft PLC** to test Modbus send/receive — it runs behind a Click-compatible Modbus interface, no hardware required. You can even spin up two pyrung programs and test them talking to each other. Or generate a CircuitPython scan loop for a ProductivityOpen P1AM-200 and run it on actual I/O.

## Quick start

```bash
# Requires Python 3.11+
pip install -e .
```

### A motor with start/stop logic

```python
from pyrung import Bool, Program, Rung, latch, reset

Start = Bool("Start")
Stop = Bool("Stop")
Running = Bool("Running")

with Program() as logic:
    with Rung(Start):
        latch(Running)
    with Rung(Stop):
        reset(Running)
```

### Test it

```python
from pyrung import PLCRunner

runner = PLCRunner(logic)
with runner.active():
    Start.value = True
    runner.step()
    assert Running.value is True

    # Release start — motor stays latched
    Start.value = False
    runner.step()
    assert Running.value is True

    Stop.value = True
    runner.step()
    assert Running.value is False
```

### Map to Click hardware when you're ready

```python
from pyrung.click import TagMap

tags = TagMap()
tags.map(Start, "X001")    # Physical input
tags.map(Stop, "X002")     # Physical input
tags.map(Running, "Y001")  # Physical output

tags.validate(logic)  # Checks against Click constraints
tags.export_nicknames("motor.csv")  # For Click programming software
```

## What's included

**Core engine** — Pure `f(state) → new_state` scan cycle with immutable snapshots. Coils, latches, timers, counters, branching, subroutines, structured tags, edge detection, and [more](docs/guides/ladder-logic.md). Built to match real Click behavior — no surprises when you move to hardware.

**Click PLC dialect** — Hardware address mapping, memory bank validation, Modbus instructions, and nickname file export. Run any program as a soft PLC behind a Click-compatible Modbus interface for integration testing. [Docs →](docs/dialects/click.md)

**CircuitPython dialect** — Generates a self-contained scan loop for P1AM-200 hardware from any pyrung program. [Docs →](docs/dialects/circuitpy.md)

**VS Code debugger** — Step through scans, set breakpoints on rungs, force tags, diff states, and time-travel through scan history. [Docs →](docs/guides/dap-vscode.md)

## Learn more

| | |
|---|---|
| [Core Concepts](docs/getting-started/concepts.md) | Scan cycle, SystemState, tags, blocks |
| [Ladder Logic Guide](docs/guides/ladder-logic.md) | Full DSL reference |
| [Runner Guide](docs/guides/runner.md) | Execution, time modes, history, fork |
| [Testing Guide](docs/guides/testing.md) | Unit testing with deterministic time |
| [Forces & Debug](docs/guides/forces-debug.md) | Force values, breakpoints, time travel |
| [Click Reference](docs/click_reference/README.md) | Click PLC instruction reference (42 pages) |
