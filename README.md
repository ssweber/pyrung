# pyrung

**Ladder logic in Python that reads like ladder, scans like a PLC, and deploys to real hardware.**

pyrung turns Python's `with` block into a ladder rung: condition on the rail, instructions in the body. The engine runs a real scan cycle with immutable state snapshots, and the DSL enforces the ladder paradigm. You can't write `for` loops, `if/else`, or direct assignment inside a program, because those don't exist in ladder logic, and pyrung is faithfully modeling ladder logic.

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

- Documentation: https://ssweber.github.io/pyrung/
- LLM docs index: https://ssweber.github.io/pyrung/llms.txt
- New to ladder logic? [Know Python? Learn Ladder Logic.](https://ssweber.github.io/pyrung/learn/)

## Why?

For 25 years, the industry has assumed a trade-off: if you want modern developer tooling (version control, unit testing, text-based editing, a real debugger), you switch from ladder to Structured Text. pyrung refuses that trade-off.

Ladder is still the dominant programming language in North American manufacturing, preferred for its visual clarity and the fact that an electrician can troubleshoot it on the floor. But ladder programmers have no git, no pytest, no CI. They draw logic in a vendor GUI, download to hardware, and hope. The [ladder-as-text problem](https://ssweber.github.io/blog/why-pyrung/) has been open since at least 2000. pyrung is a serious attempt at closing it.

The code reads like a ladder diagram. `with Rung(Start | Motor, ~Stop): out(Motor)` is a seal-in circuit, and it looks like one. The scan cycle is deterministic, timers accumulate the same way, rung order matters the same way, and the numeric behavior (clamping, wrapping, overflow) matches real Click PLC hardware. What you learn in pyrung transfers directly to the editor.

When it works, deploy it. `pyrung_to_ladder()` encodes your tested rungs for paste into Click via [ClickNick](https://github.com/ssweber/clicknick). Or generate a self-contained CircuitPython scan loop for a P1AM-200. Or run your logic over Modbus TCP and connect a real HMI. Same source, three deployment paths.

## Who's this for?

**Controls engineers** who want to test Click PLC logic without hardware or proprietary software. Write with semantic tag names, simulate with deterministic time, map to hardware addresses when you're ready. The validator tells you exactly what Click can and can't do before you find out at the panel.

**Python developers** entering industrial automation. You know Python, you know VS Code, you know pytest. pyrung meets you there and teaches you ladder logic in the language and tools you already have, with an engine that won't let you sidestep the paradigm. When you open a ladder editor for the first time, the contacts, coils, timers, and scan behavior all work the way you already learned.

**Makers and P1AM-200 users** who want a real scan loop, built-in ladder instructions, Modbus TCP, and SD card persistence without writing the plumbing. pyrung generates a complete CircuitPython program from the same logic you already tested.

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
