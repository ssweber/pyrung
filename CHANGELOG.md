# Changelog

## Unreleased

### Bug fixes

- **Snapshot-stable instruction helper conditions** — embedded helper conditions such as `.reset(...)`, counter `.down(...)`, shift `.clock()` / `.reset()`, and drum event/jump/jog/reset inputs now evaluate against the rung's frozen `ConditionView` instead of live mid-rung writes. `.continued()` snapshot reuse is now explicitly fenced to the same execution scope, so snapshots cannot leak across subroutine boundaries.

## v0.5.2 — Friendlier timer/counter API

### New features

- **Positional `preset` and `unit`** — `on_delay`, `off_delay`, `count_up`, and `count_down` now accept positional arguments: `on_delay(MyTimer, 3000)`, `on_delay(MyTimer, 5, "sec")`. Keyword form still works.
- **Human-friendly time units** — `unit=` accepts `"ms"`, `"sec"`, `"min"`, `"hour"`, `"day"` (and plurals, abbreviations). Default is `"ms"`. Tag-name suffixes `Tms`/`Ts`/`Tm`/`Th`/`Td` still accepted — `FillTimeTm` stays short, and `Tm` sidesteps the minute-vs-minimum ambiguity of `Min`.
- **`DoneAccUDT` protocol** — Timer/counter functions now type as `timer: DoneAccUDT` instead of `InstanceView | _StructRuntime`. IDE hover shows the contract, not the implementation.
- **`normalize_unit()` exported** — Converts any unit alias to canonical form. Available from `pyrung.core`.
- **`TimeUnitStr` Literal type** — All valid unit strings in one type for IDE autocomplete.

### Migration

- No breaking changes. Existing `preset=` keyword and `unit="Tms"` code works unchanged.

## v0.5.0 — Timer/Counter cleanup

v0.4.0 introduced `Timer` and `Counter` as built-in UDTs with `.named()` for creating instances. That was one special case too many — `.named()` is gone, replaced by `.clone()` which matches how the rest of the tag system works.

### Breaking changes

- **`Timer.named()` / `Counter.named()` replaced by `.clone()`** — `Timer` and `Counter` are now `count=1` singletons. Use `Timer.clone("Name")` / `Counter.clone("Name")` for named instances. TagMap auto-resolve for timer/counter operands removed — all mappings are now explicit via `.map_to()`.

### New features

- **Section comments in TagMap codegen** — `TagMap` constructor output now emits `# --- Structures ---`, `# --- Timers & Counters ---`, `# --- Blocks ---`, and `# --- Tags ---` section headers when there are 2+ non-empty groups.

### Migration

- Replace `Timer.named(n, "Name")` with `Timer.clone("Name")`. Same for `Counter`.
- Add explicit `.map_to()` calls for any timer/counter tags that relied on TagMap auto-resolve.

## v0.4.0 — Cleaner surface, honest abstractions

### Breaking changes

- **`all_of`/`any_of` renamed to `And`/`Or`** — PascalCase combinators replace the old function names. `&` and `|` operators removed for conditions (kept for math/bitwise). Comma inside `Rung(...)` stays as implicit AND.
- **Built-in `Timer` and `Counter` UDTs** — `Timer` and `Counter` are now built-in structured types with `.Done` (Bool) and `.Acc` (Int/Dint) fields, exported from `pyrung`. Use `Timer.clone("Name")` for named instances. User-defined UDTs with the same shape still work.
- **Single-argument timer/counter instructions** — `on_delay(timer, preset=...)` replaces `on_delay(done, acc, preset=...)`. Same for `off_delay`, `count_up`, `count_down`. The two-tag form is removed entirely.
- **`PLCRunner` renamed to `PLC`** — `.active()` removed; `PLC` is now a context manager directly (`with PLC(logic) as plc:`). `dt=` (default `0.010`) and `realtime=True` kwargs replace `set_time_mode()`. `dt=` and `realtime=True` are mutually exclusive. `TimeMode` removed from public exports.
- **`set_battery_present()` replaced by property** — use `plc.battery_present = False`.
- **`plc.debug.*` namespace** — 11 debugger-internal methods moved off `PLC` into `plc.debug`: `scan_steps`, `scan_steps_debug`, `rung_trace` (was `inspect`), `last_event` (was `inspect_event`), `prepare_scan`, `commit_scan`, etc. `system_runtime` accessible via `plc.debug.system_runtime`.
- **Force API renamed** — `add_force()` → `force()`, `remove_force()` → `unforce()`, `with plc.force(...)` → `with plc.forced(...)`. DAP debug console commands updated to match (`remove_force` alias removed).
- **`_fn` variants dropped** — `run_until_fn` merged into `run_until`, `when_fn` merged into `when`. Both now accept Tag/Condition expressions or callable predicates directly.
- **`Program` internals privatized** — `add_rung`, `start_subroutine`, `end_subroutine`, `evaluate`, `current` → private. Legacy `call_subroutine` removed.
- **`TagMap` internals privatized** — `offset_for`, `block_entry_by_name`, `owner_of` → private.
- **Time units as strings** — `Tms`, `Ts`, `Tm`, `Th`, `Td` removed from public imports. Use `unit="Tms"` string form. `TimeUnit` enum stays internal.
- **Validation entry points consolidated** — `validate_click_program` and `validate_circuitpy_program` removed from public exports. Use `logic.validate(dialect=...)` or `mapping.validate(logic)` / `P1AM.validate(logic)`.
- **`Tag.__rand__` precedence guard** — `int & tag` and `BoolTag & tag` now raise `TypeError` with guidance to reorder operands, preventing the Python operator precedence trap where `2 & tag` silently evaluates wrong.

### New features

- **Conflicting output target validation** — `validate_conflicting_outputs(program)` detects when multiple `INERT_WHEN_DISABLED=False` instructions (`out`, timers, counters, drums, shift registers) write to the same tag from non-mutually-exclusive execution paths. The only safe pattern is different subroutines whose callers have provably exclusive conditions (e.g., `State == 1` vs `State == 2`); same-scope duplicates are always flagged because the disabled instruction actively resets the target every scan. Supports `CompareEq` different-constant, `BitCondition`/`NormallyClosedCondition` complement, `CompareEq`/`CompareNe` complement, and range-complement (`Lt`/`Ge`, `Le`/`Gt`) detection on caller conditions.
- **Click timer preset overflow validation** — `CLK_TIMER_PRESET_OVERFLOW` finding warns when a timer preset exceeds the INT range for its time base (e.g., >32767 for `Tms`). Click silently clamps overflows.
- **`P1AM.validate()` convenience method** — mirrors `TagMap.validate()` for CircuitPython programs.

### Bug fixes

- **Click bypassed imported contacts** — codegen now warns on contacts that were bypassed during import.

### Docs

- System namespace section added to concepts (`system.sys.*`, `system.fault.*`, `system.rtc.*`).
- Operator precedence trap callout added to conditions reference.
- Structured timer (`@udt`) note added to timers/counters reference.
- Click timer preset INT cap table added to Click dialect docs.
- Counter/timer accumulator switched to positional form in reference.
- Fault flags named explicitly in math reference.
- `ScanContext` section rewritten without internal type name.
- System points cross-referenced from runner guide.

### Migration

- Replace `all_of(...)` with `And(...)`, `any_of(...)` with `Or(...)`. Remove `&` / `|` between conditions — use `And()` / `Or()` or commas.
- Replace `on_delay(done, acc, preset=...)` with `on_delay(timer, preset=...)` using a `Timer` instance. Same for `off_delay`, `count_up`, `count_down` with `Counter` instances.
- Replace standalone `Bool`/`Int`/`Dint` timer and counter tags with `Timer.clone("Name")` / `Counter.clone("Name")`. Access `.Done` and `.Acc` fields on the instance.
- Replace `PLCRunner` with `PLC` everywhere. Replace `runner = PLCRunner(logic); ctx = runner.active()` with `with PLC(logic) as plc:`.
- Replace `runner.set_time_mode(TimeMode.REALTIME)` with `PLC(logic, realtime=True)`.
- Replace `plc.set_battery_present(False)` with `plc.battery_present = False`.
- Replace `plc.inspect(rung_id)` with `plc.debug.rung_trace(rung_id)`.
- Replace `plc.add_force(...)` with `plc.force(...)`, `plc.remove_force(...)` with `plc.unforce(...)`, `with plc.force(...)` with `with plc.forced(...)`.
- Replace `plc.run_until_fn(fn)` with `plc.run_until(fn)`, `plc.when_fn(fn)` with `plc.when(fn)`.
- Replace `on_delay(..., unit=Tms)` with `on_delay(..., unit="Tms")`. Same for `Ts`, `Tm`, `Th`, `Td`.
- Replace `validate_click_program(logic)` with `logic.validate(dialect="click")` or `mapping.validate(logic)`.
- Replace `validate_circuitpy_program(logic, hw)` with `logic.validate(dialect="circuitpy")` or `hw.validate(logic)`.

## v0.3.1

### Bug fixes

- **Tag defaults now seeded into initial state** — `PLCRunner` now populates `SystemState.tags` with every program tag's declared default at construction time. Previously, tags were absent from state until first written, causing `Tag.value` (which falls back to `tag.default`) to disagree with the engine's condition evaluation (which fell back to hardcoded `False`). A `Bool("X", default=True)` would report `True` via `.value` but evaluate as `False` in rung conditions.

### Docs

- Expanded and polished "Know Python? Learn Ladder Logic" tutorial — added ASCII diagrams, adversarial exercises, cross-lesson callbacks, NC naming conventions, and aligned all lesson examples with the Click conveyor reference.
- `pyrung.zen` — `import pyrung.zen` prints guiding principles for ladder logic in Python (à la `import this`).

### Examples

- Conveyor examples (`click_conveyor.py`, `circuitpy_conveyor.py`) updated to follow tutorial naming conventions and best practices.

## v0.3.0

### Breaking changes

- Core system point `system.storage.sd.save_cmd` was removed.
- CircuitPython save trigger moved to onboard board model tag `board.save_memory_cmd` (`from pyrung.circuitpy import board`).
- `generate_circuitpy(...)` now supports optional RUN/STOP board-switch mapping via `runstop=RunStopConfig(...)` and supports board-only (zero-slot) codegen when board tags are referenced.
- `calc(...)` and `CalcInstruction(...)` no longer accept a public `mode=` argument; mode is inferred from referenced tag families.
- `send()`/`receive()` now use `ModbusTcpTarget` dataclass instead of inline `host`/`port`/`device_id` keyword arguments.
- **Codegen API cleanup** — `TagMap.to_ladder(program)` removed; use free function `pyrung_to_ladder(program, tag_map)`. `csv_to_pyrung()` renamed to `ladder_to_pyrung()`. Both exported from `pyrung.click`.
- **Copy modifiers replaced by copy converters** — `copy(as_value(source), target)` is now `copy(source, target, convert=to_value)`. The `as_value`, `as_ascii`, `as_text`, `as_binary` functions, `CopyModifier` class, Tag helper methods (`.as_value()`, `.as_ascii()`, etc.), and BlockRange helper methods are all removed. The `pad=` parameter is removed entirely — use string literals for leading zeros (`copy("00026", Txt[1])`). `fill()` no longer supports text conversion. `blockcopy()` `convert=` is limited to `to_value`/`to_ascii`.
- **Search uses comparison expressions** — `search(condition=">=", value=100, search_range=DS.select(1, 100), ...)` is now `search(DS.select(1, 100) >= 100, ...)`. The `condition=`, `value=`, and `search_range=` parameters are removed. The first positional argument is a `RangeComparison` built by applying a comparison operator to a `.select()` range. Click ladder CSV format updated to match: `search(DS001..DS100 >= 100,result=...,found=...)`.
- **Block slot API replaced** — `rename_slot()`, `clear_slot_name()`, `configure_slot()`, `configure_range()`, `clear_slot_config()`, `clear_range_config()`, and `slot_config()` are all removed. Use `block.slot(addr)` which returns a live `SlotView` (or `RangeSlotView` for ranges) with `.name`, `.retentive`, `.default`, `.comment` properties and `.reset()` for clearing overrides.

### Moved

- `send_receive` module (`send`, `receive`, `ModbusTcpTarget`, `ModbusSendInstruction`, `ModbusReceiveInstruction`) moved from `pyrung.click.send_receive` to `pyrung.core.instruction.send_receive`. Re-exported from `pyrung.click` unchanged.

### New features

- **History time-travel slider** — new "History" panel in the VS Code debug sidebar. Scrub a range slider across retained scan snapshots; tag values update live. Backed by two new DAP custom requests (`pyrungHistoryInfo`, `pyrungSeek`).

- **Raw Modbus TCP and RTU support** — `send()`/`receive()` now accept `ModbusAddress` for `remote_start` (instead of Click address strings) to talk to any Modbus device, not just Click PLCs. New types: `ModbusAddress` (register address + type + word order), `ModbusRtuTarget` (serial connection details), `RegisterType` (holding/input/coil/discrete input), `WordOrder` (high-low/low-high for 32-bit values). Uses `pymodbus` for raw I/O (lazy-imported). `word_order` lives on `ModbusAddress`, not on the target — Click handles word swap natively, so it only matters for raw register addressing. All exported from `pyrung.core` and `pyrung.click`.

- **`BlockRange.sum()`** — `DS.select(1, 10).sum()` returns a `SumExpr` for use in `calc()`. Click ladder export renders as `SUM ( DS1 : DS10 )`.
- **Click ladder CSV export** — `pyrung_to_ladder(program, tag_map)` generates deterministic 33-column CSV files importable into Click programming software. Supports AND/OR expansion, branch continuation rows, forloop lowering, subroutine splitting, and `LadderBundle.write()` for multi-file output.
- **Click ladder semantic-loss guard** — Click ladder CSV round-trip now fails loudly instead of silently dropping incomplete objects. `pyrung_to_ladder()` validates emitted rows by reparsing them through the existing in-memory Click CSV analyzer and raises on mismatched output objects, pin objects, condition trees, or comments. `ladder_to_pyrung()` now also raises on partial decoded rungs and lossy pin conditions instead of truncating them.
- **In-memory round-trip** — `ladder_to_pyrung(bundle)` accepts a `LadderBundle` directly, enabling program → ladder → Python source round-trip without writing CSV to disk. Also accepts file paths (replaces `csv_to_pyrung`).
- **Multi-file project codegen** — `ladder_to_pyrung_project(source)` generates a multi-file Python project from Click ladder CSV: `tags.py` (declarations + TagMap), `main.py` (program logic with `call(func)` references), and `subroutines/*.py` (one `@subroutine`-decorated function per file). Supports `nickname_csv=` for variable name substitution and structured type inference, and `output_dir=` for writing to disk. Designed for ClickNick's "Export to pyrung" workflow.
- **`immediate()` wrapper** — new `immediate(tag)` helper (exported from `pyrung`) for immediate I/O reads in contacts and coil targets. Click validation enforces that immediate contacts are direct only (no `rise`/`fall`), immediate coils resolve to `Y` bank, and wrapped ranges are contiguous.
- **`TagMap.tags_from_plc_data()`** — returns logical tag values from a PLC data dump (e.g. from `pyclickplc.read_plc_data`), so a PLC snapshot can initialize a runner in three lines.
- **Click nickname CSV round-trip improvements** — `TagMap.from_nickname_file()` now treats marker-only boundary rows (blank nickname + default slot state + comment containing only the block tag) as boundary metadata instead of invalid slot overrides. Standalone and block-slot address comments now round-trip through `TagMap` and are re-emitted alongside block tags in the CSV comment field.
- **Empty rung preservation** — empty rungs (`with Rung(): pass`) and comment-only rungs now survive Click ladder CSV round-trip. The exporter emits `NOP` in the AF column; the codegen imports them as `pass`. An explicit `nop()` instruction is also available in `pyrung.click` for Click programs that want to be explicit.
- **Bare text safeguard in codegen** — the ladder-to-pyrung codegen now raises `ValueError` on unrecognised bare AF tokens instead of emitting undefined Python names.
- **Rung comments** — `comment("...")` before a `with Rung()` attaches a comment to the rung. Comments export as `#,<text>` rows in Click ladder CSV. Multi-line supported, trimmed to 1400 characters.
- **Nested branches** — `branch()` inside `branch()` is now supported. All conditions at every nesting depth evaluate against the same rung-entry snapshot (ConditionView). Primarily for codegen to faithfully represent imported ladder topologies.
- **`Rung.continued()`** — `with Rung(B).continued():` reuses the prior rung's condition snapshot instead of freezing a fresh one. Models multiple independent wires on the same visual rung in Click's ladder editor. Static error if used on the first rung in a program or subroutine. Continued rungs cannot have their own comment.
- CircuitPython Modbus TCP codegen — `generate_circuitpy()` accepts `modbus_server=ModbusServerConfig(...)` and/or `modbus_client=ModbusClientConfig(...)` to generate a Modbus TCP server, client, or both for the P1AM-200 via P1AM-ETH. Register layout matches a real Click PLC. Client send/receive generates a non-blocking state machine (one step per scan).
- **`write_circuitpy()`** — new convenience function that calls `generate_circuitpy()` and writes the output files to disk in one step. `generate_circuitpy()` still available for in-memory use.
- **Split CircuitPython codegen output** — `generate_circuitpy()` now produces two files: `code.py` (your program, regenerate when logic changes) and `pyrung_rt.py` (shared runtime library, same for every project). A pre-compiled `pyrung_rt.mpy` is available from [GitHub releases](https://github.com/ssweber/pyrung/releases) for faster boot and lower memory use.
- **Crash-safe retentive persistence** — CircuitPython retentive tags now auto-save to SD card every 30 seconds when values change and on RUN→STOP transitions. Writes use atomic rename for crash safety.
- **`named_array` instance API** — `instance(i)` returns tags for a single instance, `instance_select(start, end)` selects a range. Supports sparse layouts with explicit instance numbering.
- Improved CircuitPython codegen readability — section separators, bank-name comments on Modbus reverse-mapping tables, and descriptive comments on client `build_request` functions.
- New example: `circuitpy_traffic_light_modbus.py` — P1AM-200 intersection controller with relay outputs, Modbus TCP server for SCADA/HMI, and client reading a walk request from a remote pedestrian panel.
- New example: `circuitpy_retentive_runstop.py` — demonstrates retentive tags, RUN/STOP switch, and SD persistence on P1AM-200.
- **Starter project release assets** — `pyrung_to_ladder` and `generate_circuitpy` can produce ready-to-import starter bundles: Click ladder CSV project files and CircuitPython output with pre-compiled `.mpy` via mpy-cross.
- **"Know Python? Learn Ladder Logic" tutorial** — new multi-lesson tutorial series in `docs/` walking through core concepts with a conveyor sorting station example.

### Bug fixes

- **OR topology corrections** — four fixes for ladder CSV export that could silently corrupt OR branch wiring: branch-local OR preservation, wire fill merging in series ORs, removal of `_compact_any_triplet` (which silently changed OR to AND), and correct `T:` prefix on multi-contact OR branches.
- **T junction propagation** — bridge topology and pin-row wiring now propagate correctly through T junctions into sibling output conditions in codegen.
- **Named array stride** — corrected stride handling for `count=1` named arrays in the Click collector.
- **Analyzer graph reduction** — simplified graph reduction to fix edge cases in the Click rung analyzer.

### Performance

- Faster round-trip program construction in codegen.

### Internal

- Expression class hierarchy replaced with data-driven `BinaryExpr`/`UnaryExpr`/`ExprCompare`.
- Module splits for maintainability: `tag_map`, `context`, `send_receive`, and `codegen` each split into packages.

### Migration

- Replace `out(system.storage.sd.save_cmd)` with `out(board.save_memory_cmd)` in CircuitPython programs.
- Replace `calc(..., mode="hex")` with WORD-only calc expressions (hex will be inferred).
- Replace `calc(..., mode="decimal")` with `calc(...)` (decimal is inferred whenever any non-WORD tag is involved).
- For Click portability, split mixed WORD/non-WORD math into separate `calc()` steps to avoid `CLK_CALC_MODE_MIXED`.
- Replace `send(host="...", port=502, device_id=1, remote_start=..., source=...)` with `send(target=ModbusTcpTarget("name", "host"), remote_start=..., source=...)`.
- Replace `copy(as_value(source), target)` with `copy(source, target, convert=to_value)`. Same for `as_ascii`→`to_ascii`, `as_text(...)`→`to_text(...)`, `as_binary`→`to_binary`. Replace `tag.as_value()`/`range.as_value()` helper calls the same way. Remove `pad=` from `to_text()` calls — use string literals for leading zeros instead. Remove `fill(as_*(...), dest)` calls — `fill()` no longer supports converters.
- Replace `receive(host="...", port=502, device_id=1, remote_start=..., dest=...)` with `receive(target=ModbusTcpTarget("name", "host"), remote_start=..., dest=...)`.
- Replace `search(condition=">=", value=100, search_range=DS.select(1, 100), result=R, found=F)` with `search(DS.select(1, 100) >= 100, result=R, found=F)`. Same for all operators (`==`, `!=`, `<`, `<=`, `>`, `>=`).
- Replace `block.configure_slot(addr, ...)` / `block.rename_slot(addr, name)` with `block.slot(addr).name = name`, `.retentive = True`, etc. Replace `block.clear_slot_config(addr)` with `block.slot(addr).reset()`.

## v0.1.0

Initial public release.

### Core engine

- Pure-function scan cycle with immutable `SystemState` snapshots (via `pyrsistent`)
- DSL: `with Rung()` context managers for readable ladder logic
- Instructions: `out`, `latch`/`reset`, `copy`, `calc`, `on_delay`/`off_delay`, `count_up`/`count_down`, `shift`, `search`, `fill`, `blockcopy`, `event_drum`/`time_drum`, `pack_bits`/`unpack_to_bits`, `pack_words`/`unpack_to_words`, `pack_text`
- Tag types: `Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`
- Structured tags: `@udt()` for mixed-type structs, `@named_array()` for single-type interleaved arrays
- Blocks: `Block`, `InputBlock`, `OutputBlock` with configurable indexing
- Control flow: `branch`, `subroutine`/`call`, `forloop`, `return_early`
- Conditions: `rise()`, `fall()`, `all_of()`, `any_of()`, comparison operators
- Time modes: `FIXED_STEP` (deterministic, default) and `REALTIME` (wall-clock)
- Runner: `step()`, `run()`, `run_for()`, `run_until()`, `scan_steps()`
- Forces: `add_force()`, `remove_force()`, `with runner.force()` scoped context manager
- Patch: one-shot inputs via `patch()` or `.value` writes
- History: `runner.history.at()`, `.range()`, `.latest()`, configurable `history_limit`
- Time travel: `runner.seek()`, `runner.rewind()`, `runner.playhead`
- Fork: `runner.fork()` / `runner.fork_from()` for independent runners from snapshots
- Breakpoints: `runner.when(condition).pause()` / `.snapshot()`
- Monitors: `runner.monitor(tag, callback)` on committed value changes
- Inspection: `runner.inspect(rung_id)` for `RungTrace`, `runner.diff(scan_a, scan_b)`

### Click PLC dialect

- Pre-built memory blocks: `x`, `y`, `c`, `ds`, `dd`, `dh`, `df`, `t`, `td`, `ct`, `ctd`, `sc`, `sd`, `txt`, `xd`, `yd`
- `TagMap` for mapping semantic tags to Click hardware addresses
- Nickname CSV import/export for Click programming software
- Validation against Click memory bank constraints
- `ClickDataProvider` for running programs as a soft PLC over Modbus
- Type aliases: `Bit`, `Int2`, `Float`, `Hex`, `Txt`

### CircuitPython dialect

- P1AM-200 hardware model with module catalog
- Slot configuration and I/O validation
- CircuitPython scan loop code generation from pyrung programs

### VS Code debugger (DAP)

- Step through scans, set breakpoints on rungs
- Force tags from the debug console
- Diff states between scans
- Time-travel through scan history
- Logpoints and trace decorations
