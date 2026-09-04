# pyrung

Write ladder logic in Python, test it with pytest, and deploy it to a CLICK PLC or P1AM-200.

```python
from pyrung import Bool, PLC, Program, rung, out

Button = Bool()
Light  = Bool()

with Program() as logic:
    with rung(Button):
        out(Light)

with PLC(logic) as plc:
    Button.value = True
    plc.step()
    assert Light.value is True
```

**Know ladder?** Start with the [Quickstart](getting-started/quickstart.md): a traffic light, a test, and a hardware map in ten minutes.

**Know Python?** Start with [Know Python? Learn Ladder Logic.](learn/index.md): eleven lessons that build a conveyor sorting station.

```bash
pip install pyrung
```

## How it works

**Every scan is a snapshot.** Logic is a pure function: same inputs, same outputs, nothing mutated in place. Each `plc.step()` produces a new immutable state, and history keeps all of them.

**You drive execution.** The engine never runs on its own. `plc.run_until(Motor, ~Fault)` from a test, a GUI, or a debugger; pause anywhere, inject inputs, inspect any past state.

**Time is a variable.** `PLC(logic, dt=0.010)` advances the clock exactly 10 ms per scan, so a 3-second timer fires on scan 300 every time. No wall clock, no flaky tests.

## What you can ask

**Why a tag changed.** `plc.cause(Running)` walks backward through scan history and names the triggers.

**Why, from a dead machine.** `plc.why(Alarm)` does the same from a tag dump alone, no recorded history needed.

**How to get somewhere.** `plc.how(Running)` finds a sequence of input changes that reaches the state, or tells you why it can't. Experimental.

## Where to go

1. [Install](getting-started/installation.md), then the [Quickstart](getting-started/quickstart.md).
2. [Instruction reference](instructions/index.md): every coil, timer, counter, copy, and math instruction.
3. Deploy: the [CLICK quickstart](guides/click-quickstart.md) maps tags to addresses and exports for ClickNick; the [CircuitPython quickstart](guides/circuitpy-quickstart.md) generates a P1AM-200 program.
4. [Analysis](guides/analysis.md): cause, effect, why, and how.

!!! tip "Rather keep drawing ladder in CLICK?"

    **[ClickNick](https://pyrung.com/clicknick/)** adds nickname autocomplete, program checks, offline runs, and your ladder (as Python) after every save. Built on pyrung.
