# pyrung

**Write ladder logic in Python. Simulate it. Test it. Deploy to Click PLCs or P1AM-200 hardware.**

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

> **Status:** Core engine, Click PLC dialect, CircuitPython dialect, and VS Code debugger are implemented and tested (~30k lines, 1,700+ tests). API may still change.

- Documentation: https://ssweber.github.io/pyrung/
- LLM docs index: https://ssweber.github.io/pyrung/llms.txt

## Why?

AutomationDirect Click PLCs have no built-in simulator. You write logic, download it to hardware, and hope. pyrung lets you **test first** — same tag names, deterministic scans, real assertions. When it works, encode it with `pyrung_to_ladder()` and paste via [clicknick](https://ssweber.github.io/).

Or skip the Click editor entirely — generate a **CircuitPython scan loop** for a ProductivityOpen P1AM-200 and run your tested logic on open hardware.

Or run your logic as an **emulated Click over Modbus** to test send/receive, no hardware required. You can even spin up two pyrung programs and test them talking to each other.

## Quick start

```bash
# Requires Python 3.11+
uv add pyrung
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
from pyrung.click import TagMap, x, y

mapping = TagMap({
    Start:   x[1],    # Physical input  → X001
    Stop:    x[2],    # Physical input  → X002
    Running: y[1],    # Physical output → Y001
})

mapping.validate(logic)                # Checks against Click constraints
mapping.to_nickname_file("motor.csv")  # For Click programming software
```

## What's included

### [Core engine](docs/instructions/index.md)

Pure `f(state) → new_state` scan cycle with immutable snapshots. Coils, latches, timers, counters, branching, subroutines, structured tags, edge detection, and more. Built to match real Click behavior — no surprises when you move to hardware.

### [Click PLC dialect](docs/dialects/click.md)

Hardware address mapping, memory bank validation, Modbus instructions, and nickname file export. Run any program as an emulated Click over Modbus for integration testing.

### [CircuitPython dialect](docs/dialects/circuitpy.md)

Generate a self-contained CircuitPython scan loop from the same program you already tested. Targets the ProductivityOpen P1AM-200 with 35 supported I/O modules, SD-backed retentive storage, watchdog, Modbus TCP, and RUN/STOP control.

### [VS Code debugger](docs/guides/dap-vscode.md)

Step through scans rung by rung, set breakpoints, force tags, diff states, and time-travel through scan history.

## Learn more

| | |
|---|---|
| [Core Concepts](docs/getting-started/concepts.md) | Scan cycle, SystemState, tags, blocks |
| [Instruction Reference](docs/instructions/index.md) | Full DSL reference |
| [Tag Structures](docs/guides/tag-structures.md) | UDTs, named arrays, cloning, block config |
| [Runner Guide](docs/guides/runner.md) | Execution, time modes, history, fork |
| [Testing Guide](docs/guides/testing.md) | Unit testing with deterministic time |
| [Forces & Debug](docs/guides/forces-debug.md) | Force values, breakpoints, time travel |

## Disclaimers

- **Simulation is best-effort.** pyrung models Click PLC behavior as closely as practical, but it is not a certified simulator. You are responsible for verifying your program on real hardware before production use.
- **Modbus is unauthenticated.** The emulated Click Modbus interface and CircuitPython Modbus TCP server listen on the network with no encryption or access control — standard for Modbus, but keep them off untrusted networks.
