# pyrung

**Ladder logic in Python that reads like ladder, scans like a PLC, and deploys to real hardware.**

pyrung turns Python's `with` block into a ladder rung — condition on the rail, instructions in the body.

```python
from pyrung import Bool, PLC, Program, Rung, out

Button = Bool("Button")
Light = Bool("Light")

with Program() as logic:
    with Rung(Button):
        out(Light)

with PLC(logic) as plc:
    Button.value = True
    plc.step()
    assert Light.value is True
```

- Documentation: https://ssweber.github.io/pyrung/
- LLM docs index: https://ssweber.github.io/pyrung/llms.txt
- New to ladder logic? [Know Python? Learn Ladder Logic.](https://ssweber.github.io/pyrung/learn/)

## Why?

Ladder is still the dominant programming language in North American manufacturing, but ladder programmers have no git, no pytest, no CI. They draw logic in a vendor GUI, download to hardware, and hope. pyrung lets you write and test logic in Python first, then deploy it — same source, three paths:

- `pyrung_to_ladder()` encodes your rungs for [ClickNick](https://github.com/ssweber/clicknick), a companion editor for Click Programming Software
- Generate a self-contained CircuitPython scan loop for a P1AM-200
- Run as an emulated Click over Modbus — spin up two pyrung programs and test them talking to each other, no hardware required

The code reads like a ladder diagram. `with Rung(Start | Motor, ~Stop): out(Motor)` is a seal-in circuit, and it looks like one. There's no `if/else`. Power flows through the rung or it doesn't. The scan cycle is deterministic, timers accumulate the same way, rung order matters the same way. What you learn in pyrung transfers directly to a real ladder editor.

## Who's this for?

**Controls engineers** who want to test Click PLC logic without hardware. The [Click dialect](https://ssweber.github.io/pyrung/dialects/click/) handles address mapping, memory bank validation, and nickname and ladder file export. Have an existing project? [ClickNick](https://github.com/ssweber/clicknick) imports it.

**Python developers** entering industrial automation. pyrung teaches you ladder logic in the language and tools you already have. The [VS Code debugger](https://ssweber.github.io/pyrung/guides/dap-vscode/) lets you step through scans and watch power flow rung by rung. Start with the [learning guide](https://ssweber.github.io/pyrung/learn/).

**Makers and P1AM-200 users** who want a real scan loop without writing the plumbing. The [CircuitPython dialect](https://ssweber.github.io/pyrung/dialects/circuitpy/) generates a complete program from the same logic you write and test on your laptop.

## Quick start

```bash
# Requires Python 3.11+
uv add pyrung
```

Download the [starter project](https://github.com/ssweber/pyrung/releases) (`pyrung-starter-VERSION.zip`) from the GitHub releases page for ready-to-run examples with Click CSV round-trip. For the VS Code debugger, grab `pyrung-debug-VERSION.vsix` from the same page and install it:

```bash
code --install-extension pyrung-debug-VERSION.vsix
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
from pyrung import PLC

with PLC(logic) as plc:
    Start.value = True
    plc.step()
    assert Running.value is True

    # Release start — motor stays latched
    Start.value = False
    plc.step()
    assert Running.value is True

    Stop.value = True
    plc.step()
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

### [Core engine](https://ssweber.github.io/pyrung/instructions/)

Coils, latches, timers, counters, branching, subroutines, structured tags, and edge detection. Every scan is a pure function — same inputs, same outputs — so you can fork, rewind, and diff any state in history.

### [Click PLC dialect](https://ssweber.github.io/pyrung/dialects/click/)

Hardware address mapping, memory bank validation, Modbus instructions, and nickname and ladder file export.

### [CircuitPython dialect](https://ssweber.github.io/pyrung/dialects/circuitpy/)

Generate a self-contained scan loop for the P1AM-200 with 35 supported I/O modules, SD-backed retentive storage, watchdog, and Modbus TCP.

### [VS Code debugger](https://ssweber.github.io/pyrung/guides/dap-vscode/)

Step through scans rung by rung, set breakpoints, force tags, diff states, and time-travel through scan history.

## Disclaimers

- **Simulation is best-effort.** pyrung models Click PLC behavior as closely as practical, but it is not a certified simulator. You are responsible for verifying your program on real hardware before production use.
- **Modbus is unauthenticated.** The emulated Click Modbus interface and CircuitPython Modbus TCP server listen on the network with no encryption or access control — standard for Modbus, but keep them off untrusted networks.
