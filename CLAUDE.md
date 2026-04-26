# pyrung

pyrung is a Python DSL for writing ladder logic. `with Rung()` maps to a ladder rung — condition on the rail, instructions in the body. It targets AutomationDirect CLICK PLCs and ProductivityOpen P1AM-200 controllers.

## User Stories

Click PLCs have no built-in simulator. pyrung lets you test first — write logic in Python, unit test with pytest, then transpose to Click. Or run as a soft PLC over Modbus to test send/receive instructions (two pyrung programs can talk to each other). Or generate a CircuitPython scan loop for P1AM-200.

## Key Folders / Files

- `src/pyrung/core/` — Engine, DSL, instructions, tags, validation
- `src/pyrung/click/` — Click PLC dialect (memory banks, TagMap, Modbus, profiles)
- `src/pyrung/dap/` — VS Code Debug Adapter Protocol integration
- `examples/` — Example programs
- `tests/` — Core, Click, DAP, and example tests

### Documentation (`docs/`)

- `docs/index.md` — Project entry point, philosophy, feature overview
- `docs/getting-started/` — Installation, core concepts (Redux model, SystemState, scan cycle), quickstart tutorial
- `docs/instructions/` — Full DSL reference split by instruction group (rungs, conditions, coils, timers, counters, copy, math, drums, program control, communication)
- `docs/guides/runner.md` — Execution engine (time modes, history, seek/rewind, fork, rung inspection)
- `docs/guides/testing.md` — Unit testing patterns with FIXED_STEP, forces as fixtures, pytest usage
- `docs/guides/forces-debug.md` — Force vs patch semantics, breakpoints, monitors, history/diff/fork
- `docs/guides/commissioning.md` — Declare/Analyze/Commission workflow, coverage plugin, CI gating
- `docs/guides/physical-harness.md` — Physical annotations, autoharness, feedback synthesis
- `docs/guides/analysis.md` — DataView queries, causal chains, coverage reports
- `docs/guides/architecture.md` — Engine internals, compiled replay kernel, sparse scan log
- `docs/guides/dap-vscode.md` — VS Code DAP integration (breakpoints, logpoints, monitors, trace decorations, Data View, Graph View, Chain tab)
- `docs/guides/click-quickstart.md` — Click-specific getting started
- `docs/guides/circuitpy-quickstart.md` — CircuitPython-specific getting started
- `docs/dialects/click.md` — Click dialect (pre-built blocks, TagMap, nickname files, validation, soft-PLC via ClickDataProvider)
- `docs/dialects/circuitpy.md` — CircuitPython dialect (P1AM hardware model, module catalog, validation, code generation)
- `docs/internal/debug-spec.md` — Debug architecture specification

## Build & Development

- `make install` — install deps (or `uv sync --all-extras --dev`)
- `make` — install + lint + test
- `make lint` — codespell, ruff (check + format), ty
- `make test` — pytest (ALWAYS use this, not `uv run pytest`)
- Conventional Commits (`feat(core):`, `fix(ladder):`, etc.)

### VS Code Extension (`editors/vscode/pyrung-debug/`)

- Requires Node.js LTS (`winget install OpenJS.NodeJS.LTS`)
- Package: `cd editors/vscode/pyrung-debug && npx @vscode/vsce package`
- Install: `code --install-extension /absolute/path/to/pyrung-debug-0.6.0.vsix`
- No `npm install` needed — plain JS, no dependencies

## Architecture

- **Immutable state**: `SystemState` (frozen `PRecord` via `pyrsistent`) with `scan_id`, `timestamp`, `tags` (`PMap`), `memory` (`PMap`). Logic is pure `f(state) -> new_state`.
- **Scan cycle** (8 phases): start → apply patch → read inputs → pre-force → execute logic → post-force → write outputs → clock/snapshot
- **Consumer-driven**: Engine never auto-runs. Consumer calls `step()`, `run()`, `run_for()`, `run_until()`, `scan_steps()`
- **DSL**: Context managers for readable logic (`with Rung(Button): out(Light)`)
- **Tags**: Named typed references (`Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`). No runtime state in tags — all values live in `SystemState.tags`
- **Structured tags**: `@udt()` for mixed-type structs, `@named_array()` for single-type interleaved arrays. Both support singleton and counted modes
- **Blocks**: Named, typed, (typically) 1-indexed arrays for I/O and grouped memory (`Block`, `InputBlock`, `OutputBlock`)
- **Hardware-agnostic core** with dialect modules layered on top (Click and CircuitPython implemented)

### Key Patterns

- **`dt=` time mode** for deterministic testing (default, recommended). `realtime=True` for wall-clock.
- **patch()** — one-shot input, consumed after one scan
- **force()** — persistent override, re-applied pre- and post-logic every scan until removed
- **`with plc.forced({...}):`** — scoped force context manager (restores on exit)
- **`plc.tags`** — read-only `MappingProxyType[str, Tag]` of all known tags by name. Useful for introspection and iterating couplings
- **copy()** clamps out-of-range; **calc()** wraps (modular arithmetic)
- **Built-in `Timer` and `Counter` UDTs** (count=1) — `.Done` (Bool) + `.Acc` (Int/Dint). `Timer.clone("Name")` for named instances, single-arg `on_delay(timer, preset=...)`. Any UDT with `Done: Bool` and `Acc: Int|Dint` fields works with timer/counter instructions (structural contract).
- **`And()` / `Or()`** — condition combinators. Comma inside `Rung(...)` is implicit AND.
- **Counters** count every scan while condition True — use `rise()` for edge-triggered counting
- **Division by zero** → result = 0, fault flag set

### Analysis and Verification

- **`prove(logic, condition)`** — exhaustively checks a property over all reachable states, with counterexample traces when it fails. Same condition syntax as `Rung()`. Returns `Proven`, `Counterexample` (replayable trace), or `Intractable`. Settles pending timers before evaluating, so timer-gated alarm paths don't produce false negatives
- **Lock file workflow** — `reachable_states()` projects to `public` tags, `write_lock()` / `check_lock()` serialize to JSON. Behavioral diffs show up in PRs
- **`plc.dataview`** — chainable query API: `.inputs()`, `.pivots()`, `.terminals()`, `.upstream(tag)`, `.downstream(tag)`, `.physical_inputs()`, `.contains("cmd")`. Also available as `program.dataview()` for static use
- **`plc.cause(tag)` / `plc.effect(tag)`** — causal chain analysis over scan history. Projected mode (`cause(tag, to=value)`) finds reachable paths or reports blockers. `plc.recovers(tag)` tests reachable clear paths
- **`plc.query`** — whole-program surveys: `cold_rungs()`, `hot_rungs()`, `stranded_bits()`, `report()` for mergeable `CoverageReport`
- **Pytest coverage plugin** — `pyrung_coverage` fixture collects per-test reports, merges at session end. CI gating via `--pyrung-whitelist`
- **`program.simplified()`** — resolved Boolean form per terminal, eliminating intermediate pivots
- **Physical annotations** — `physical=`, `link=`, `min=`, `max=`, `uom=` on tags for device behavior. `Harness` auto-synthesizes feedback. `link="Tag:value"` for value-triggered feedback. `Harness.couplings()` yields `Coupling` dataclasses for iterating all discovered enable/feedback pairings
- **Tag flags** — `readonly`, `external`, `final`, `public` metadata flags with static validator enforcement
- **Runtime bounds checking** — `min`/`max`/`choices` checked per scan, populates `plc.bounds_violations`

### Debug System

- **History**: sparse `ScanLog` with compiled replay kernel. `runner.history.at(scan_id)`, `.range()`, `.latest()`, configurable `history` (time-based retention, e.g. `"1h"`), `cache` (instant-lookup window), `history_budget` (byte ceiling, default 100 MB). States outside cache reconstruct via replay from nearest checkpoint
- **Breakpoints**: `runner.when(condition).pause()` / `.snapshot("label")`
- **Monitors**: `runner.monitor(tag, callback)` fires on committed value changes
- **Time travel**: `runner.seek(scan_id)`, `runner.rewind(seconds)`, `runner.playhead`
- **Inspection**: `runner.inspect(rung_id)` → `RungTrace`, `runner.diff(scan_a, scan_b)`
- **Fork**: `runner.fork(scan_id=None)` (primary) / `runner.fork_from(scan_id)` (alias) — independent runner from historical snapshot
- **Hot-reload**: `reload` re-executes the program file preserving PLC state. `watch`/`unwatch` for auto-reload on save
- **Session capture**: `record`/`record stop` captures replayable transcripts. Condenser shrinks to causal-minimum. Invariant miner proposes candidates from edge correlations and steady implications. Accepted invariants generate pytest files with structural verification
- **`pyrung live`**: attach to a running debug session from another terminal via TCP. Semicolon-chained commands, session discovery

### Unified CLI

- `pyrung lock` — compute reachable states, write `pyrung.lock`
- `pyrung check` — recompute and diff against lock file, exit 1 on change
- `pyrung dap` — run the DAP debug adapter
- `pyrung live` — attach to a running DAP session

### Click Dialect

- Pre-built blocks: `x`, `y`, `c`, `ds`, `dd`, `dh`, `df`, `t`, `td`, `ct`, `ctd`, `sc`, `sd`, `txt`, `xd`, `yd`
- `TagMap` for mapping semantic tags to hardware addresses (dict, `.map_to()`, or nickname CSV)
- Validation: `mapping.validate(logic, mode="warn"|"strict")`
- `ClickDataProvider` for soft-PLC via Modbus (implements `pyclickplc` DataProvider protocol)
- Type aliases: `Bit`→`Bool`, `Int2`→`Dint`, `Float`→`Real`, `Hex`→`Word`, `Txt`→`Char`

## Current Status

Core engine, Click dialect, and DAP debugger are all implemented and tested. The codebase has ~59k lines of source and 3,000+ tests covering core, click, dap, and examples.

**API status:** Beta. All public exports are explicit (`__all__` on every module). The core DSL, Click dialect, and runner APIs are settled. The analysis layer (`prove()`, `cause()`/`effect()`, `dataview`, coverage) is newer and may evolve. No formal deprecation machinery yet — breaks are clean cuts with migration guides in CHANGELOG.md.

