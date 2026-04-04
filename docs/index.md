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

- LLM docs index: https://ssweber.github.io/pyrung/llms.txt
- New to ladder logic? [Know Python? Learn Ladder Logic.](learn/index.md)

## Why?

For 25 years, the industry has assumed a trade-off: if you want modern developer tooling (version control, unit testing, text-based editing, a real debugger), you switch from ladder to Structured Text. pyrung refuses that trade-off.

Ladder is still the dominant programming language in North American manufacturing, preferred for its visual clarity and the fact that an electrician can troubleshoot it on the floor. But ladder programmers have no git, no pytest, no CI. They draw logic in a vendor GUI, download to hardware, and hope. The [ladder-as-text problem](https://ssweber.github.io/blog/why-pyrung/) has been open since at least 2000. pyrung is a serious attempt at closing it.

The code reads like a ladder diagram. `with Rung(Start | Motor, ~Stop): out(Motor)` is a seal-in circuit, and it looks like one. The scan cycle is deterministic, timers accumulate the same way, rung order matters the same way, and the numeric behavior (clamping, wrapping, overflow) matches real Click PLC hardware. What you learn in pyrung transfers directly to the editor.

When it works, deploy it. `pyrung_to_ladder()` encodes your tested rungs for paste into Click via [ClickNick](https://github.com/ssweber/clicknick). Or generate a self-contained CircuitPython scan loop for a P1AM-200. Or run your logic over Modbus TCP and connect a real HMI. Same source, three deployment paths.

## Who's this for?

**Controls engineers** who want to test Click PLC logic without hardware or proprietary software. Write with semantic tag names, simulate with deterministic time, map to hardware addresses when you're ready. The validator tells you exactly what Click can and can't do before you find out at the panel.

**Python developers** entering industrial automation. You know Python, you know VS Code, you know pytest. pyrung meets you there and teaches you ladder logic in the language and tools you already have, with an engine that won't let you sidestep the paradigm. When you open a ladder editor for the first time, the contacts, coils, timers, and scan behavior all work the way you already learned.

**Makers and P1AM-200 users** who want a real scan loop, built-in ladder instructions, Modbus TCP, and SD card persistence without writing the plumbing. pyrung generates a complete CircuitPython program from the same logic you already tested.

## How it works

**Every scan is a snapshot.** Logic is a pure function — the same inputs always produce the same outputs, nothing is mutated in place. Every step produces a new immutable state, so history is always there when you want it.

**You drive execution.** The engine never runs on its own. Call `step()`, `run()`, or `run_until()` from tests, a GUI, or a debugger. Pause anywhere, inject inputs, inspect any historical state.

**Time is a variable.** `FIXED_STEP` mode advances the clock by a fixed amount each scan, making timers and counters perfectly deterministic in tests. Rewind and replay whenever you need to.

**Write first, validate later.** Start with semantic tag names and plain Python. Map to hardware addresses when you're ready, then run the validator. It tells you what Click can and can't do — before you find out at the PLC.

## Quick links

- [Installation](getting-started/installation.md) — `pip install pyrung`
- [Quickstart](getting-started/quickstart.md) — up and running in 5 minutes
- [Core Concepts](getting-started/concepts.md) — how the scan cycle and state model work
- [Instruction Reference](instructions/index.md) — the full DSL reference
- [Click PLC Dialect](dialects/click.md) — memory banks, address mapping, validation
- [VS Code Debugger](guides/dap-vscode.md) — breakpoints, monitors, step-through debugging
- [CircuitPython Dialect](dialects/circuitpy.md) — P1AM hardware model and code generation
- [CircuitPython Modbus TCP](dialects/circuitpy-modbus.md) — Modbus server and client for P1AM-200
