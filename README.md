# Pyrung

A Pythonic ladder logic framework — simulate, test, and debug PLC programs in pure Python.

## Why Pyrung?

**Offline PLC development** — Controls engineers working with Click PLCs can develop, test, and debug ladder logic without physical hardware.

**PLC logic as testable code** — Express PLC logic as pure Python so you can use git, pytest, and CI workflows.

| Feature | pyrung | Traditional simulation |
|---------|--------|------------------------|
| Logic syntax | Pure Python | Proprietary GUI / IEC text |
| State | Immutable snapshots | Mutable in-place |
| Time control | `FIXED_STEP` for exact determinism | Wall-clock only |
| Testing | Standard pytest | Custom tooling |
| Debugging | DAP + VS Code inline decorations | Separate runtime tool |

## Quick Example

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

## What's Included

**Core engine** — Immutable state machine with a context-manager DSL. All logic is pure `f(state) -> new_state`.

- Instructions: `out`, `latch`/`reset`, `copy`, `calc`, `run_function`/`run_enabled_function`
- Timers (`on_delay`, `off_delay`) and counters (`count_up`, `count_down`)
- Shift registers, search, bit/word packing, blockcopy, copy/fill
- Branching (`branch`, `any_of`, `all_of`), subroutines, for-loops
- Structured tags (`@udt`, `@named_array`) with auto-naming and field options
- Edge detection (`rise`, `fall`), one-shot support
- Program validation and scan-cycle introspection

**Click dialect** (`pyrung.click`) — Click PLC-specific layer on top of the core engine.

- Memory bank types and address ranges (`X`, `Y`, `C`, `DS`, `DD`, `DF`, etc.)
- `TagMap` for mapping tags to hardware addresses and nickname file I/O
- Modbus send/receive instructions
- Soft-PLC adapter via `ClickDataProvider`
- Profile-based capabilities and validation

**Debugging** (`pyrung.dap`) — VS Code Debug Adapter Protocol integration.

- Force tag values, set breakpoints on rungs, monitor expressions
- Scan history, time-travel playhead, diff, fork
- Conditional and hit-count breakpoints, logpoints, snapshot labels
- Full DAP adapter for step-through debugging in VS Code

## Documentation

- [Core Concepts](docs/getting-started/concepts.md) — Redux model, scan cycle, SystemState, tags, blocks
- [Quickstart](docs/getting-started/quickstart.md) — End-to-end example in 5 minutes
- [Ladder Logic Guide](docs/guides/ladder-logic.md) — Full DSL reference
- [Runner Guide](docs/guides/runner.md) — Execution, time modes, history, fork
- [Testing Guide](docs/guides/testing.md) — Unit testing with FIXED_STEP and forces
- [Forces & Debug](docs/guides/forces-debug.md) — Force vs patch, breakpoints, monitors, time travel
- [VS Code DAP](docs/guides/dap-vscode.md) — Debugging in VS Code
- [Click Dialect](docs/dialects/click.md) — Memory banks, TagMap, validation, soft-PLC
- [Click Reference](docs/click_reference/README.md) — Click PLC instruction reference (42 pages)

## Getting Started

```bash
# Install (requires Python 3.11+)
pip install -e .

# Run tests
make test
```

## Status

Core engine, Click dialect, and DAP debugger are implemented and tested (~19k lines, 1,100+ tests). Not yet published to PyPI. API may still change.
