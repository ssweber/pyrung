# pyrung

A Pythonic ladder logic framework with an immutable, pure-functional architecture.

## User Stories

1. **Offline PLC development** — Controls engineers simulate Click PLC ladder logic in Python to develop, test, and debug without hardware.
2. **PLC logic as testable code** — Developers express PLC logic as pure Python to use git, pytest, and CI workflows.

## Key Folders / Files

- `src/pyrung/core/` — Engine, DSL, instructions, tags, validation
- `src/pyrung/click/` — Click PLC dialect (memory banks, TagMap, Modbus, profiles)
- `src/pyrung/dap/` — VS Code Debug Adapter Protocol integration
- `examples/` — Example programs
- `tests/` — Core, Click, DAP, and example tests

### Documentation (`docs/`)

- `docs/index.md` — Project entry point, philosophy, feature overview
- `docs/getting-started/` — Installation, core concepts (Redux model, SystemState, scan cycle), quickstart tutorial
- `docs/guides/ladder-logic.md` — Full DSL reference (conditions, instructions, timers, counters, branching, subroutines)
- `docs/guides/runner.md` — Execution engine (time modes, history, seek/rewind, fork, rung inspection)
- `docs/guides/testing.md` — Unit testing patterns with FIXED_STEP, forces as fixtures, pytest usage
- `docs/guides/forces-debug.md` — Force vs patch semantics, breakpoints, monitors, history/diff/fork
- `docs/guides/dap-vscode.md` — VS Code DAP integration (breakpoints, logpoints, monitors, trace decorations)
- `docs/dialects/click.md` — Click dialect (pre-built blocks, TagMap, nickname files, validation, soft-PLC via ClickDataProvider)
- `docs/dialects/circuitpy.md` — CircuitPython dialect (planned, not implemented)
- `docs/internal/debug-spec.md` — Debug architecture specification
- `docs/internal/circuitpy-spec.md` — CircuitPython architecture decisions and open questions
- `docs/click_reference/` — Click PLC instruction reference (42 files: contacts, coils, timers, counters, copy, math, shift, search, memory banks, system memory, data types)

## Build & Development Commands

```bash
# Install dependencies
make install                    # or: uv sync --all-extras --dev

# Default workflow (install + lint + test)
make

# Individual commands
make lint                       # Run codespell, ruff (check + format), ty
make test                       # Run pytest (ALWAYS use this, not uv run pytest)
```

## Architecture

- **Immutable state**: `SystemState` (frozen `PRecord` via `pyrsistent`) with `scan_id`, `timestamp`, `tags` (`PMap`), `memory` (`PMap`). Logic is pure `f(state) -> new_state`.
- **Scan cycle** (8 phases): start → apply patch → read inputs → pre-force → execute logic → post-force → write outputs → clock/snapshot
- **Consumer-driven**: Engine never auto-runs. Consumer calls `step()`, `run()`, `run_for()`, `run_until()`, `scan_steps()`
- **DSL**: Context managers for readable logic (`with Rung(Button): out(Light)`)
- **Tags**: Named typed references (`Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`). No runtime state in tags — all values live in `SystemState.tags`
- **Structured tags**: `@udt()` for mixed-type structs, `@named_array()` for single-type interleaved arrays. Both support singleton and counted modes
- **Blocks**: Named, typed, 1-indexed arrays for I/O and grouped memory (`Block`, `InputBlock`, `OutputBlock`)
- **Hardware-agnostic core** with dialect modules layered on top (Click implemented, CircuitPython planned)

### Key Patterns

- **FIXED_STEP time mode** for deterministic testing (default, recommended). `REALTIME` for wall-clock.
- **patch()** — one-shot input, consumed after one scan
- **add_force()** — persistent override, re-applied pre- and post-logic every scan until removed
- **`with runner.force({...}):`** — scoped force context manager (restores on exit)
- **copy()** clamps out-of-range; **math()** wraps (modular arithmetic)
- **Timers/counters** use two-tag model: done-bit + accumulator
- **Counters** count every scan while condition True — use `rise()` for edge-triggered counting
- **Division by zero** → result = 0, fault flag set

### Debug System

- **History**: `runner.history.at(scan_id)`, `.range()`, `.latest()`, configurable `history_limit`
- **Breakpoints**: `runner.when(predicate).pause()` / `.snapshot("label")`
- **Monitors**: `runner.monitor(tag, callback)` fires on committed value changes
- **Time travel**: `runner.seek(scan_id)`, `runner.rewind(seconds)`, `runner.playhead`
- **Inspection**: `runner.inspect(rung_id)` → `RungTrace`, `runner.diff(scan_a, scan_b)`
- **Fork**: `runner.fork_from(scan_id)` — independent runner from historical snapshot

### Click Dialect

- Pre-built blocks: `x`, `y`, `c`, `ds`, `dd`, `dh`, `df`, `t`, `td`, `ct`, `ctd`, `sc`, `sd`, `txt`, `xd`, `yd`
- `TagMap` for mapping semantic tags to hardware addresses (dict, `.map_to()`, or nickname CSV)
- Validation: `mapping.validate(logic, mode="warn"|"strict")`
- `ClickDataProvider` for soft-PLC via Modbus (implements `pyclickplc` DataProvider protocol)
- Type aliases: `Bit`→`Bool`, `Int2`→`Dint`, `Float`→`Real`, `Hex`→`Word`, `Txt`→`Char`

## Current Status

Core engine, Click dialect, and DAP debugger are all implemented and tested. The codebase has ~19k lines of source and 1,100+ tests covering core, click, dap, and examples.

**Not yet done:** CircuitPy dialect (architecture decided, implementation deferred), PyPI publishing, stable public API guarantee.
