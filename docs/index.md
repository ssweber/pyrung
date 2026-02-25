# pyrung

**Write ladder logic in Python. Simulate it. Test it. Deploy it.**

```python
from pyrung import Bool, PLCRunner, Program, Rung, TimeMode, out

Button = Bool("Button")
Light  = Bool("Light")

with Program() as logic:
    with Rung(Button):
        out(Light)

runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
runner.patch({"Button": True})
runner.step()

print(runner.current_state.tags["Light"])  # True
```

## What it does

Ladder logic has always been a domain language for industrial control. pyrung asks a simple question: **what if that language lived in Python?**

**For controls engineers:** Write and simulate Click PLC logic without hardware or proprietary software. Use plain tag names from day one. Add hardware addresses when you're ready. A validator checks your program against Click constraints and tells you exactly what to fix.

**For developers:** VS Code becomes your PLC programming environment — step through scans, set breakpoints on rungs, watch tags update inline, and force overrides from the debug console. For the adventurous: the same program can generate a deployable CircuitPython `while True` scan loop for a P1AM-200 microcontroller.

## How it works

**Every scan is a snapshot.** Logic is a pure function — the same inputs always produce the same outputs, nothing is mutated in place. Every step produces a new immutable state, so history is always there when you want it.

**You drive execution.** The engine never runs on its own. Call `step()`, `run()`, or `run_until()` from tests, a GUI, or a debugger. Pause anywhere, inject inputs, inspect any historical state.

**Time is a variable.** `FIXED_STEP` mode advances the clock by a fixed amount each scan, making timers and counters perfectly deterministic in tests. Rewind and replay whenever you need to.

**Write first, validate later.** Start with semantic tag names and plain Python. Map to hardware addresses when you're ready, then run the validator. It tells you what Click can and can't do — before you find out at the PLC.

## Quick links

- [Installation](getting-started/installation.md) — `pip install pyrung`
- [Quickstart](getting-started/quickstart.md) — up and running in 5 minutes
- [Core Concepts](getting-started/concepts.md) — how the scan cycle and state model work
- [Writing Ladder Logic](guides/ladder-logic.md) — the full DSL reference
- [Click PLC Dialect](dialects/click.md) — memory banks, address mapping, validation
- [VS Code Debugger](guides/dap-vscode.md) — breakpoints, monitors, step-through debugging
- [CircuitPython Dialect](dialects/circuitpy.md) — P1AM hardware model and code generation
