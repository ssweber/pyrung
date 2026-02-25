# Pyrung

**Write ladder logic in Python. Simulate it. Test it. Deploy it.**

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

## What it does

Ladder logic has always been a domain language for industrial control. pyrung asks a simple question: **what if that language lived in Python?** It turns out a `with` block is a rung — condition on the rail, instructions in the body.

**For controls engineers:** Write and simulate Click PLC logic without hardware or proprietary software. Use plain tag names from day one. Add hardware addresses when you're ready. A validator checks your program against Click constraints and tells you exactly what to fix.

**For developers:** VS Code becomes your PLC programming environment — step through scans, set breakpoints on rungs, watch tags update inline, and force overrides from the debug console. For the adventurous: the same program can generate a deployable CircuitPython `while True` scan loop for a P1AM-200 microcontroller.

## How it works

**Rungs look like rungs.** `with Rung(Button): out(Light)` maps directly to a ladder diagram — condition on the left rail, coil on the right. Python's `with` block carries the "power flows to the body" semantics naturally. A controls engineer reads it like a rung; a Python developer reads it as ordinary Python.

**Program collects the ladder, Runner steps through it.** `Program` accumulates your rungs into a logic graph. Pass it to `PLCRunner`, which executes one scan at a time — you call `step()` and decide when things happen.

**Write first, validate later.** Start with semantic tag names and plain Python. Map to hardware addresses when you're ready, then run the validator. It tells you what Click can and can't do — before you find out at the PLC.

**Test with perfect repeatability.** Every scan produces an immutable snapshot. The same inputs always give the same outputs. Timers and counters advance by a fixed step, so tests are deterministic — no flaky timing, no hidden state.

**Debug like code.** Step through scans, set breakpoints on rungs, rewind to any previous state. Force tag values, diff two scans, fork from history. VS Code sees it as a debuggable program because it is one.

## What's Included

**Core engine** — Rungs are `with` blocks; all logic is pure `f(state) → new_state`.

- Instructions: `out`, `latch`/`reset`, `copy`, `calc`, `run_function`/`run_enabled_function`
- Timers (`on_delay`, `off_delay`) and counters (`count_up`, `count_down`)
- Shift registers, search, bit/word packing, blockcopy, copy/fill
- Branching (`branch`), subroutines, for-loops
- Structured tags (`@udt`, `@named_array`) with auto-naming and field options
- Edge detection (`rise`, `fall`), one-shot support
- Program validation and scan-cycle introspection

**Click dialect** (`pyrung.click`) — Click PLC-specific layer on top of the core engine.

- Memory bank types and address ranges (`X`, `Y`, `C`, `DS`, `DD`, `DF`, etc.)
- `TagMap` for mapping tags to hardware addresses and nickname file I/O
- Modbus send/receive instructions
- Soft-PLC adapter via `ClickDataProvider`
- Profile-based capabilities and validation

**CircuitPython dialect** (`pyrung.circuitpy`) — P1AM-200 hardware model and code generation.

- 35-module hardware catalog with validation
- Generates a self-contained CircuitPython `while True` scan loop from any pyrung program
- Retentive tag persistence via SD card
- Syntax-checked output before returning

**Debugging** (`pyrung.dap`) — VS Code Debug Adapter Protocol integration.

- Force tag values, set breakpoints on rungs, monitor expressions
- Scan history, time-travel playhead, diff, fork
- Conditional and hit-count breakpoints, logpoints, snapshot labels
- Full DAP adapter for step-through debugging in VS Code

## Documentation

- [Core Concepts](docs/getting-started/concepts.md) — Redux model, scan cycle, SystemState, tags, blocks
- [Quickstart](docs/getting-started/quickstart.md) — Up and running in 5 minutes
- [Ladder Logic Guide](docs/guides/ladder-logic.md) — Full DSL reference
- [Runner Guide](docs/guides/runner.md) — Execution, time modes, history, fork
- [Testing Guide](docs/guides/testing.md) — Unit testing with FIXED_STEP and forces
- [Forces & Debug](docs/guides/forces-debug.md) — Force vs patch, breakpoints, monitors, time travel
- [VS Code Debugger](docs/guides/dap-vscode.md) — Breakpoints, monitors, step-through debugging *(extension marketplace publish pending)*
- [Click Dialect](docs/dialects/click.md) — Memory banks, TagMap, validation, soft-PLC
- [Click Reference](docs/click_reference/README.md) — Click PLC instruction reference (42 pages)
- [CircuitPython Dialect](docs/dialects/circuitpy.md) — P1AM hardware model and code generation

## Getting Started

```bash
# Install (requires Python 3.11+)
pip install -e .

# Run tests
make test
```

## Status

Core engine, Click dialect, and DAP debugger are implemented and tested (~19k lines, 1,100+ tests). Not yet published to PyPI. API may still change.
