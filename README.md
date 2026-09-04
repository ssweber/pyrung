# pyrung

**Write ladder logic in Python. Test it. Deploy it to CLICK.**

pyrung turns a Python `with` block into a ladder rung: condition on the rail, instruction in the body. Same rungs, same scan order, same timers as the CLICK PLC, with git, pytest, and a debugger around them.

```python
from pyrung import Bool, PLC, Program, rung, out

Button = Bool()
Light = Bool()

with Program() as logic:
    with rung(Button):
        out(Light)

with PLC(logic) as plc:
    Button.value = True
    plc.step()
    assert Light.value is True
```

- Documentation: https://pyrung.com/pyrung/
- Know Python? [Learn Ladder Logic.](https://pyrung.com/pyrung/learn/)

## Why

CLICK PLCs ship with no simulator, no version control beyond copies of a `.ckp`, and no way to test a program short of downloading it to a real PLC. You draw the ladder in CLICK Programming Software, download it to hardware, and hope. The Structured Text crowd has options. The ladder crowd doesn't. pyrung is that option: write and test the logic in Python first, then move the same rungs into CLICK.

Same source, three paths: `pyrung_to_ladder()` encodes your rungs for [ClickNick](https://pyrung.com/clicknick/) to paste into CLICK Programming Software; the CircuitPython dialect generates a scan loop for a P1AM-200; or run it as an emulated CLICK over Modbus and test two programs talking to each other with no hardware.

## Who it's for

**Controls engineers** who want to test CLICK logic without hardware. Write with plain tag names, map them to X, Y, C, and DS addresses when you're ready, and let the validator tell you what CLICK's memory banks will and won't accept before you find out at the PLC.

**Python developers** entering industrial automation. pyrung teaches ladder logic in the language and tools you already have: Python, pytest, and VS Code. Start with [Know Python? Learn Ladder Logic.](https://pyrung.com/pyrung/learn/)

**Makers and P1AM-200 users** who want a real scan cycle without writing the plumbing. The same program you tested on your laptop generates a CircuitPython scan loop with timers, counters, Modbus TCP, and SD-backed retentive state.

## Quick start

```bash
# Requires Python 3.11+
uv add pyrung
```

Download the [starter project](https://github.com/ssweber/pyrung/releases) (`pyrung-starter-VERSION.zip`) from the GitHub releases page for ready-to-run examples with CLICK CSV round-trip. For the VS Code debugger, grab `pyrung-debug-VERSION.vsix` from the same page and install it:

```bash
code --install-extension pyrung-debug-VERSION.vsix
```

### A motor with start/stop logic

```python
from pyrung import Bool, Program, rung, latch, reset

Start = Bool()
Stop = Bool()
Running = Bool()

with Program() as logic:
    with rung(Start):
        latch(Running)
    with rung(Stop):
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

### Map to CLICK hardware when you're ready

```python
from pyrung.click import ClickBlocks, TagMap

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

mapping = TagMap({
    Start:   x[1],    # Physical input  → X001
    Stop:    x[2],    # Physical input  → X002
    Running: y[1],    # Physical output → Y001
})

mapping.validate(logic)                # Checks against CLICK constraints
mapping.to_nickname_file("motor.csv")  # For CLICK programming software
```

## What's included

### [Core engine](https://pyrung.com/pyrung/instructions/)

Coils, latches, timers, counters, branching, subroutines, structured tags, and edge detection. Every scan is a pure function, so you can fork, rewind, and diff any state in history.

### [CLICK dialect](https://pyrung.com/pyrung/dialects/click/)

Map tags to X, Y, C, DS, and the other CLICK banks. The validator checks the program against CLICK's memory rules and instruction set. Export the nickname CSV and the ladder for ClickNick to paste. Import an existing project the other way.

### [CircuitPython dialect](https://pyrung.com/pyrung/dialects/circuitpy/)

Generate a self-contained scan loop for the P1AM-200 with 35 supported I/O modules, SD-backed retentive storage, watchdog, and Modbus TCP.

### [Analysis](https://pyrung.com/pyrung/guides/analysis/)

`logic.simplified()["Motor"]` resolves a 14-rung interlock chain to the handful of inputs that actually decide it. `plc.cause(Running)` names why a tag changed. `plc.why(Alarm)` does it from a tag dump off a faulted machine. `plc.how(Running)` finds the input changes that reach a state, or says why none can. `how()` is experimental; the rest is stable.

### [VS Code debugger](https://pyrung.com/pyrung/guides/dap-vscode/)

Step through scans rung by rung, set breakpoints on rungs, force tags, and time-travel through history. Watch tags live, see the dependency graph, and turn a captured session into pytest invariants.

## Disclaimers

- **Simulation is best-effort.** pyrung models CLICK PLC behavior as closely as practical, but it is not a certified simulator. You are responsible for verifying your program on real hardware before production use.
- **Modbus is unauthenticated.** The emulated CLICK Modbus interface and CircuitPython Modbus TCP server listen on the network with no encryption or access control — standard for Modbus, but keep them off untrusted networks.
