# pyrung

Python DSL for ladder logic. `with Rung()` maps to a ladder rung — condition on the rail, instructions in the body. Targets Click PLCs and P1AM-200. No built-in Click simulator — test in Python first, then transpose.

## Build & Development

- `make install` — install deps (or `uv sync --all-extras --dev`)
- `make` — install + lint + test
- `make lint` — codespell, ruff (check + format), ty
- `make test` — pytest, excludes slow markers (ALWAYS use make, not `uv run pytest`). It, `make test-pilot` and `make test-fold` run on `PYTEST_WORKERS` xdist workers (default 6; `PYTEST_WORKERS=0` for one process, a file stays on one worker)
- `make test-prove` — prover (verifier) subsystem only
- `make test-pilot` — pilot (`how()` planner) tests
- `make test-tumbler` — generated-program PILOT fixtures; run after `make test-pilot`
- `make test-hypothesis` — property-based tests
- `make test-soundness` — prove agreement checks (`--prove-agreement`)
- `make test-fuzz` — fuzzer suite
- `make test-fold` — the suite with every run in the guarded loop checked against a stepped twin, scan by scan (`--fold-check`, `tests/fold_check.py`); run after loop, timer, counter or clock changes
- `make test-fold-new` — only the test files this branch adds, under `--fold-check` (mergeq's `fold-new` stage; judges mismatches, not vacuity)
- `make test-conformance` — conformance cases over Modbus TCP against the local soft PLC (`PLC=host[:port]` for a real CLICK, `TARGET=circuitpy` for the generated P1AM-200 code run locally, `GROUP=name` for some groups); `make conformance-export OUT=dir` writes the CLICK project
- `make test-integration` — integration tests (needs hardware/network)
- Conventional Commits (`feat(core):`, `fix(ladder):`, etc.)

## Release Checklist

- Bump `pyrung>=` version in `src/pyrung/click/codegen/project_emitter.py` (generated pyproject.toml template)
- Update `CHANGELOG.md` — move Unreleased to versioned heading with today's date
- Bump `version` in `editors/vscode/pyrung-debug/package.json` to match (the release script also writes it into the built VSIX)

## User-Facing Output

- Text authored by pyrung for terminals, logs, exceptions, and `str()` representations must use ASCII only. Use `->`, `<->`, `...`, and `1..100` instead of typographic arrows, ellipses, or dashes.
- Preserve user-authored Unicode in tag names, choice labels, comments, values, and source excerpts. The ASCII rule applies to pyrung's surrounding text, not user data.
- Documentation, comments, and generated documentation may use typographic Unicode when their output channel is explicitly UTF-8.

## Writing

These rules cover the docs, `CHANGELOG.md`, comments, docstrings and commit messages. Each must pass one test. A PLC programmer who knows Python reads the sentence once and knows what it means. If they would ask "what does this mean?", rewrite the sentence or cut it.

- **Use the ordinary word.** A word pyrung coined (coast, fold, stretch, free system tag, body row, site) gets the plain word instead, or a definition on that page before its first use. Never use a name only the code uses.
- **Say who does what to what.** Write "each `copy()` clears the flag", not "the flag follows the last copy".
- **One idea per sentence, with a subject and a verb.** End it with a full stop. Don't chain clauses with a colon or a semicolon.
- **A comma joins words, not facts.** When commas start holding a list of facts, write a list.
- **Keep the subject short.** Don't make the reader hold five items before the verb. Say what happens, then list what causes it.
- **"It" means the noun just before it.** Otherwise repeat the noun.
- **New behaviour first, old behaviour in brackets:** "stores 5 (it stored 0)". Don't write "where it stored 0".
- **When two versions pass the test, keep the shorter.**

Each channel has its own shape:

- **Changelog:** the headline is one sentence saying what changed for the user. Each sub-bullet is one change. What the user should do about it comes last ("Copy it into an `Int` first."). No rationale and no evidence.
- **Docs:** code first, then what it does, then the gotcha. If the code is clear, don't restate it in prose.
- **Comments and docstrings:** say why, in one or two sentences, with the purpose first and then the trap. Cut a comment that narrates what the code plainly does.

To review prose, give a fresh reviewer these rules and the diff, not the writer's reasoning. A quick grep first finds most of the problems:
- a `;` in a sentence
- a colon in the middle of a sentence
- three or more commas in one sentence
- "where it"
- the coined words above

The reviewer then reads every flagged line, and every new noun, against the test.

## Module Map

```
src/pyrung/
├── __init__.py          # Public re-exports
├── cli.py               # Unified CLI (lock, check, dap, live)
├── pytest_plugin.py     # Coverage plugin (pyrung_coverage fixture)
├── core/
│   ├── tag.py           # Tag types (Bool, Int, Dint, Real, Word, Char), UDT, named_array
│   ├── state.py         # SystemState (immutable, slotted; tags and memory are PMaps), scan state
│   ├── rung.py          # Rung DSL, condition evaluation
│   ├── runner.py        # Engine plus first-class causal Epoch lineage (step, seek, fork)
│   ├── kernel.py        # Compiled execution kernel
│   ├── compiled_run.py  # run_for/run_until on the kernel: interpreted prefix, checkpoints, stops
│   ├── scan_trace.py    # Guarded scan loop traced from the kernel source (soundness argument in its docstring)
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
│       ├── prove/       # Exhaustive state-space verifier (has its own AGENTS.md)
│       └── pilot/       # PILOT — how() steering, backward-trace + forward-simulate (has its own AGENTS.md)
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
│   ├── host.py          # Run a generated code.py on a desktop Python (board stand-ins, TCP Modbus)
│   └── p1am/            # P1AM board abstraction
├── dap/
│   ├── adapter.py       # DAP server main loop
│   ├── session.py       # Debug session state
│   ├── handlers/        # DAP request handlers (lifecycle, breakpoints, history, causal, etc.)
│   ├── capture.py       # Session recording
│   ├── condenser.py     # Transcript condensing to causal-minimum
│   ├── miner.py         # Invariant miner
│   └── live.py          # pyrung live (TCP attach)
└── conformance/         # Conformance suite: same cases in-process, over Modbus, or on a real PLC
```

## Sub-AGENTS.md Files

- `src/pyrung/core/analysis/prove/AGENTS.md` — Prover internals, optimization glossary, module map, invariants
- `src/pyrung/core/analysis/pilot/AGENTS.md` — PILOT steering invariants, actual control flow, module map
- `editors/vscode/pyrung-debug/AGENTS.md` — VS Code extension event architecture, key files
- `docs/AGENTS.md` — Documentation tone and style, API design decisions, technical details
- `tests/conformance/AGENTS.md` — Conformance suite protocol, groups and manifest, register layout, wire pacing, coverage checklist
- `tests/fuzz/reproducers/AGENTS.md` — Fuzz reproducer workflow, diagnose script usage

## Docs Index

- `CHANGELOG.md` — User-visible changes, grouped by release
- `docs/getting-started/` — Installation, core concepts, quickstart
- `docs/instructions/` — Full DSL reference by instruction group
- `docs/guides/` — Runner, testing, commissioning, analysis, verification, architecture, DAP
- `docs/dialects/` — Click and CircuitPython dialect details
- `docs/internal/debug-spec.md` — Debug architecture spec

## VS Code Extension (`editors/vscode/pyrung-debug/`)

- Package: `cd editors/vscode/pyrung-debug && npx @vscode/vsce package`
- Install: `code --install-extension /absolute/path/to/pyrung-debug-0.12.0.vsix`
- No `npm install` needed — plain JS, no dependencies

## Gotchas

- `copy()` clamps out-of-range, except a `Word` into an `Int` keeps its bits and a negative into a `Word` wraps; `math()` wraps (modular arithmetic)
- Counters count rising edges (CLICK semantics, per-counter input memory `_cu:i{N}`/`_cd:i{N}`); a held input counts once. Down counter Done is `Acc <= preset` from Acc 0 (a negative preset counts down to it; a positive one is done at once unless Acc is staged above it)
- Division by zero → that division = 0 (the rest of the formula runs), fault flag set
- Comma inside `Rung(...)` is implicit AND; use `Or()` for OR logic
