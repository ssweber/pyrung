# pyrung

Python DSL for ladder logic. `with Rung()` maps to a ladder rung — condition on the rail, instructions in the body. Targets Click PLCs and P1AM-200. No built-in Click simulator — test in Python first, then transpose.

## Build & Development

- `make install` — install deps (or `uv sync --all-extras --dev`)
- `make` — install + lint + test
- `make lint` — codespell, ruff (check + format), ty
- `make test` — pytest, excludes slow markers (ALWAYS use make, not `uv run pytest`)
- `make test-prove` — prover subsystem only (~23 test files)
- `make test-hypothesis` — property-based tests
- `make test-soundness` — prove agreement checks (`--prove-agreement`)
- `make test-fuzz` — fuzzer suite
- `make test-parity` — Click parity tests
- `make test-integration` — integration tests (needs hardware/network)
- Conventional Commits (`feat(core):`, `fix(ladder):`, etc.)

## Module Map

```
src/pyrung/
├── __init__.py          # Public re-exports
├── cli.py               # Unified CLI (lock, check, dap, live)
├── pytest_plugin.py     # Coverage plugin (pyrung_coverage fixture)
├── core/
│   ├── tag.py           # Tag types (Bool, Int, Dint, Real, Word, Char), UDT, named_array
│   ├── state.py         # SystemState (immutable PRecord), scan state
│   ├── rung.py          # Rung DSL, condition evaluation
│   ├── runner.py        # Consumer-driven engine (step, run, run_for, seek, fork)
│   ├── kernel.py        # Compiled execution kernel
│   ├── condition.py     # Condition combinators (And, Or, rise, fall, comparisons)
│   ├── expression.py    # Expression tree for conditions/calc
│   ├── history.py       # Time travel, scan log access
│   ├── scan_log.py      # Sparse ScanLog with compiled replay
│   ├── memory_block.py  # Block, InputBlock, OutputBlock
│   ├── context.py       # Global context for DSL execution
│   ├── harness.py       # Physical harness, feedback synthesis
│   ├── physical.py      # Physical annotations (physical, link, uom)
│   ├── bounds.py        # Runtime min/max/choices checking
│   ├── debugger.py      # Breakpoints, monitors, debug hooks
│   ├── instruction/     # All instructions (coils, timers, counters, copy, calc, drums, control, packing, send/receive)
│   ├── program/         # Program builder, decorators, validation, context managers
│   ├── validation/      # Static validators (duplicate_out, readonly, stuck_bits, etc.)
│   └── analysis/
│       ├── dataview.py  # Chainable query API (.inputs, .upstream, .downstream)
│       ├── query.py     # Whole-program surveys (cold_rungs, stranded_bits, coverage)
│       ├── causal/      # cause()/effect() over scan history, projected paths
│       ├── simplified.py # Resolved Boolean form per terminal
│       └── prove/       # Exhaustive state-space verifier (has its own CLAUDE.md)
├── click/
│   ├── __init__.py      # Pre-built blocks (x, y, c, ds, dd, etc.), type aliases
│   ├── tag_map/         # TagMap: semantic tags ↔ hardware addresses, nickname CSV
│   ├── ladder/          # Ladder export (translator, layout, exporter)
│   ├── codegen/         # Click project file generation (parser, emitter, analyzer)
│   ├── validation/      # Click-specific validation (hardware, portability, findings)
│   ├── data_provider.py # Soft-PLC via Modbus (ClickDataProvider)
│   └── profile.py       # PLC model profiles
├── circuitpy/
│   ├── hardware.py      # P1AM hardware model
│   ├── catalog.py       # Module catalog
│   ├── validation.py    # CircuitPython-specific validation
│   ├── codegen/         # CircuitPython code generation (render, compile, kernel)
│   └── p1am/            # P1AM board abstraction
├── dap/
│   ├── adapter.py       # DAP server main loop
│   ├── session.py       # Debug session state
│   ├── handlers/        # DAP request handlers (lifecycle, breakpoints, history, causal, etc.)
│   ├── capture.py       # Session recording
│   ├── condenser.py     # Transcript condensing to causal-minimum
│   ├── miner.py         # Invariant miner
│   └── live.py          # pyrung live (TCP attach)
└── twin/                # Twin harness: same test against soft PLC or real PLC
```

## Sub-CLAUDE.md Files

- `src/pyrung/core/analysis/prove/CLAUDE.md` — Prover internals, optimization glossary, module map, invariants
- `editors/vscode/pyrung-debug/CLAUDE.md` — VS Code extension event architecture, key files
- `docs/CLAUDE.md` — Documentation tone/style, API design decisions, technical details
- `tests/twin/CLAUDE.md` — Twin harness protocol, slot layout, coverage checklist
- `tests/fuzz/reproducers/CLAUDE.md` — Fuzz reproducer workflow, diagnose script usage

## Docs Index

- `CHANGELOG.md` — User-visible changes, grouped by release
- `docs/getting-started/` — Installation, core concepts, quickstart
- `docs/instructions/` — Full DSL reference by instruction group
- `docs/guides/` — Runner, testing, commissioning, analysis, verification, architecture, DAP
- `docs/dialects/` — Click and CircuitPython dialect details
- `docs/internal/debug-spec.md` — Debug architecture spec

## VS Code Extension (`editors/vscode/pyrung-debug/`)

- Package: `cd editors/vscode/pyrung-debug && npx @vscode/vsce package`
- Install: `code --install-extension /absolute/path/to/pyrung-debug-0.6.0.vsix`
- No `npm install` needed — plain JS, no dependencies

## Gotchas

- `copy()` clamps out-of-range; `calc()` wraps (modular arithmetic)
- Counters count every scan while condition is True — use `rise()` for edge-triggered
- Division by zero → result = 0, fault flag set
- Comma inside `Rung(...)` is implicit AND; use `Or()` for OR logic
