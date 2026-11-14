# Changelog

<!-- Style guide: follow the Writing section of the root AGENTS.md. Write for a user
     deciding whether to upgrade. Each item is one line under a group heading.
     The sections run in this order.
     - "What's new" leads with what the user can now do.
     - "Behaviour changes" holds anything that breaks code. Each item says what
       changed on one line and what to do on the next.
     - "Fixes" and "Performance" follow.
     Say "as a CLICK does" where that is the change. Link the CLICK Conformance page
     instead of listing edge cases. Leave out fixes to anything no release shipped.
     Condense Unreleased into this shape before it gets a version heading. -->

## Unreleased

pyrung runs a program the way a CLICK PLC does. It is the engine under ClickNick's CLICK Simulator. A suite of test cases runs on pyrung and on a real CLICK, and the CLICK's answer is the expected one. The [CLICK Conformance](https://pyrung.com/pyrung/guides/conformance/) page gives the case counts by area and the two known differences. It also says what the suite doesn't reach and how to run it.

### What's new

#### Simulate a CLICK

- The debugger can act as a CLICK PLC. A `modbus` launch argument serves the session over Modbus TCP, with X inputs read-only as on a CLICK.
- The debugger has RUN and STOP modes and a paced Continue (`startMode`, `speed`, and the `mode` and `speed` console verbs).
- Force, unforce and patch take CLICK addresses such as `X001` or `DS10`.
- The `link`, `unlink`, `restore` and `links` console verbs add, replace or cut feedback links for one debug session without editing the program. These links survive `reload`.
- `PLC.start()` enters RUN without running a scan, and `PLC.mode` reads `"run"` or `"stop"`.
- `PLC(keep_forces_on_run=True)` keeps forces across STOP->RUN.
- For DAP client authors, `reload` keeps the RUN/STOP mode and lists each moved or dropped force and link in a `carried` field. The new `pyrungReload` request takes a plan that moves each force and link to a new tag name.

#### Check pyrung against a CLICK

- `pyrung.conformance` runs one set of test cases in each of these places:
  - in-process on a `PLC`
  - over Modbus against pyrung's soft PLC
  - on a real CLICK
  - against generated P1AM-200 code
- `build_program()` writes the suite as a CLICK program with one subroutine per group.

#### CLICK v4 instructions

Each of these runs in `PLC`, `CompiledPLC` and generated CircuitPython. Each exports to CLICK and imports back.

- `toggle()`, `debounce()`, `flasher()` and `timed()` are CLICK v4's Out coil types.
- `time_event_drum()` is CLICK's Time & Event drum. A step ends when its time is up and its event is on.
- `forloop()` bodies take rungs with contacts, `break_loop()`, `continue_loop()` and nested loops up to three deep.
- `randint(lo, hi)` is CLICK's `RAND`. A replayed scan draws the same value it drew the first time.
- `rng.max()`, `rng.min()`, `rng.median()` and `rng.mean()` work inside `math()` and export as `MAX`, `MIN`, `MDN` and `AVG`.
- A `time_drum` preset may be a register.

#### CLICK export and checks

- `pyrung.click.estimate_steps()` estimates a program's CLICK steps to within about 1%. Validation warns with `CLK_STEP_LIMIT` as a program nears CLICK's 8000.
- `CLK_PTR_PAST_BANK` warns when a pointer can reach past the end of its bank.
- `pyrung_to_ladder(validate=True)` and `ladder_to_pyrung(validate=True)` compare the generated code with the CLICK rows, row by row.
- These opt-in checks run only when you name them with `extend_select` or `--extend-select`:
  - `CMP_EQ_ON_COMPUTED_REAL` (new) flags `==` and `!=` on a `Real` that `math()` computes.
  - `CMP_PRESET_STAYS_ZERO` now also flags a drum step whose preset is a tag with no default that no rung writes.
  - `PHYS_LINK_UNKNOWN` (new) flags a `link=` that names an enable the program never uses.

#### Harness feedback

- `Physical(name, timed="200ms")` and `Physical(name, toggle=True)` are new feedback kinds. Each runs as the CLICK coil of the same name.
- `link="~Tag"` drives feedback while a Bool enable is off.
- In a CLICK nickname comment, `link=` also takes an address (`link=Y001`) or a value (`link=State:SORTING`).
- `Harness` gains `set_overrides()`, `declared()`, `overrides`, `pending()` and `pending_ms()`.
- `pyrung.click.tag_map` finds comments in an old form and rewrites them (`OldCommentFormError`, `comment_needs_upgrade()`, `upgrade_comment()`). ClickNick's "Upgrade Project" uses these.
- `pyrung.core` exports the table of `Physical` kinds and the keys each takes (`PHYSICAL_KINDS`, `PHYSICAL_KEYS`).

#### Long runs and history

- `run_for()` and `run_until()` run every scan (they skipped scans). After the first 1,000 scans they run compiled, so an hour of a plain timer takes about half a second.
- History answers questions about a compiled run's scans as if they had been stepped. `cause()`, `effect()`, `rung_firings()`, coverage and the VS Code History panel all see them.
- A question that would step again through more than 250,000 scans raises `UnrecordedScansError`.
- Without `history=`, a stepped run stops recording rung firings at about 140 MB and steps a later scan again when asked about it. With `history=`, the runner keeps every write in the window.

#### how() and why()

- `how()` plans for a program with `send()` or `receive()` without talking to the device (it sent real requests). It sets a received value or a status bit the way it sets an input.
- `how()` adds temporary logic only where the plan needs it, and prints that logic as rows of pyrung source.
- When `how()` would stop, it tries releasing a force, a STOP->RUN, or one more wait or input change. A plan that needs one says so in a step of its own.
- `how()` plans with the session's forces and pending patches in place (it planned without them).
- `how(max_scans=)` counts most scans of a long wait as a hundredth each, so a long timer fits a small budget.
- `how()` stops after 300 s by default (`wall_clock_s=`) or when its search holds about 1 GB.
- `why()` marks a forced tag and says when the force takes effect.
- `check_lock()` raises `LockIntractable`, a `RuntimeError`, when the verifier can't finish.
- `reachable_states()` returns a `ReachableStates`, whose `truncated` says whether `depth_budget` cut the search short. `pyrung lock` warns when it did.

#### Other

- The debugger's Memory pane names the contact or instruction each value belongs to.
- `Timer.clone()` and `Counter.clone()` take `comments=`. CLICK import keeps each timer's and counter's retentive setting and comment.
- `plc.debug.abandon_scan()` puts back the patches a partly stepped scan had taken.
- `ClickDataProvider` takes a runner getter, bank `blocks` and an `on_stop` hook.

### Behaviour changes

Most of these bring pyrung in line with what a CLICK does. Each says what changed, then what to do.

- `calc()` is now `math()`, CLICK's name for the instruction. Finding codes that said `CALC` now say `MATH`, such as `CLK_MATH_*` and `CLK_EXPR_ONLY_IN_MATH`.
  Rename your calls. `calc()` still works with a `FutureWarning`. `from pyrung import *` now hides Python's `math`, so import that as `import math as pymath`.
- `run_for()` and `run_until()` take `jit=` in place of `fold=`.
  Rename the argument. `fold=` raises `TypeError`.
- `how()` no longer skips scans while it waits, so a plan has no "Jump ahead" line. On a `CoastReceipt`, `stretches` and `stretched_scans` replace `macro_folds` and `skipped_scans`. `PlanStep.accelerators` is gone.
  Update code that reads those fields.
- Counters count rising edges, as a CLICK does (they counted every scan the input was on). A down counter is done at `Acc <= preset` (it was `Acc <= -preset`).
  A `rise()` on a counter's rung is no longer needed. To count scans, use `math(N + 1, N)`. To count down 5, give the down counter a preset of -5.
- Each instruction reads only the contacts on its own path from the rail, as a CLICK does. A branch or a box pin now sees a tag written earlier in the same rung (it saw the value from rung entry). A box with pins (a counter, a retentive timer's `.reset()`, a shift register or a drum) must be alone on its rung, because CLICK won't compile it otherwise. `simplified()` follows the new order, so the last `out()` of a tag in a rung decides it. Write an OR as `Or(A, B)`.
  Run `pyrung check`, which flags the changed rungs as `RUNG_ORDER_DEPENDENT`. Building a rung with anything else beside a box raises `RuntimeError`, so move the rest to rungs of their own.
- `math()` follows CLICK's Math. A math error zeroes only the operation that failed, so `X / 0 + 5` stores 5 with `fault.division_error` (it stored 0). `%` takes the sign of the dividend.
  Check formulas that relied on the old results.
- A timer, counter or Out-coil preset must be an int or an `Int`, `Dint` or `Word` tag. A block pointer must be an `Int` or `Dint` tag. Anything else raises `TypeError` when the program is built.
  Copy a `Real` preset into an `Int`, and a `Real` or `Word` pointer into an `Int` or `Dint`. Use `//` for division in an index.
- A coil that writes a timer's or counter's Done bit raises `OwnedBitCoilError` when the program closes.
  Clear the bit with the instruction's `.reset()` pin.
- `Physical` takes one flat keyword per value, and every argument after the name is keyword-only. `Pulse`, `Ramp` and `Approach` are gone.
  Write `flasher_on=` and `flasher_off=` for a `Pulse`, `ramp_on=` and `ramp_off=` for a `Ramp`, and `approach_to=` and `approach_rate=` for an `Approach`. Run "Upgrade Project" in ClickNick, or `pyrung.click.tag_map.upgrade_comment()`, to rewrite old comments.
- CLICK ladder export refuses what CLICK's editor refuses, so some programs that exported before now raise `LadderExportError`. Most refusals name a `CLK_` code.
  Fix the rung, or pass that code to `pyrung_to_ladder(allow=...)`.
- The verifier treats timers as black boxes, so a `Proven` holds for any scan time and preset. It answers `Intractable` when a property depends on which timer finishes first (`kind="timing"`), or when it can't try every value of an input.
  Regenerate lock files with `pyrung lock`. Give such an input `choices=` or a `min=`/`max=` of at most 1000 values, and test timing with `run_for()`.
- These now do what a CLICK does, so a test that counted on the old result may fail:
  - fault flags, which report the most recent instruction (they stayed on to the end of the scan)
  - a pointer or index outside its block, which no longer stops the scan with `IndexError`
  - a math error in an expression outside `math()`, which gives 0 with no flag
  - `rise()` and `fall()`, which compare with the bit from the last scan that evaluated each contact
  - the built-in `Timer`, whose `Acc` resets on STOP->RUN (keep it with `Timer.clone(..., retentive={"Acc": True})`)
  - the built-in `Counter`, whose `Done` survives STOP->RUN
  - drums
  - `forloop()`, whose body is skipped when its rung is false. A `with Rung(...)` inside it is a rung of the body, so move it out of the loop if it should run once a scan
  - Copy, Block copy and Fill into another type
  - text copies, text compares and `search()`
  - `reboot()` with the battery in, which keeps only the retentive tags
  - a `Real`, which holds CLICK's float32 range. A literal or default outside it, or Inf or NaN, raises `ValueError`. So does a patch or force of a finite value outside it
  - `scan_clock_toggle` (SC3), which is on from the first scan
  - harness feedback, which runs as the CLICK coil it behaves like, so an edge can move by a scan or more
  - CLICK import, which brings each wire of a CLICK rung back as a rung of its own

  Rerun your tests.
- Removed or renamed:
  - `pyrung.twin` is now `pyrung.conformance`
  - `SystemState` is no longer a pyrsistent `PRecord` or `PMap`. Its fields, `set()`, `update()`, `Mapping` reads, equality and pickles work as before. Replace calls to the other pyrsistent map methods
  - `SystemState.memory` holds `_edge:` keys in place of `_prev:` keys. A saved state still loads
  - `generate_circuitpy()` and `write_circuitpy()` lose `mapped_tag_scope`
  - `refresh_workspace_lifecycle_guidance()` is gone. Generated CLICK projects keep pyrung's guidance in `AGENTS.pyrung.md` and `README.pyrung.md`
  - CLICK import names a timer or counter address with no nickname by its address (it made up `T1_Done`)

  Change the imports and names, and drop the removed argument and function.

### Fixes

#### CLICK export and import

- CLICK export writes hex constants as `Ah` and `FFh` (it wrote them in decimal, which CLICK reads as hex, so `DH1 + 10` added 16 on the PLC).
- An exported CSV matches a copy-back from CLICK v4 for `Real` constants, math formulas and ranges.
- Ladder export draws each rung the way CLICK's syntax check redraws it, so the check leaves a pasted rung as it is.
- A rung with no contact exports one CLICK rung for each output (CLICK refused the shared rung).
- Ladder export writes a one-register `fill()` as a Copy, and ends a subroutine whose last rung is a conditional `return_early()` with the `return()` CLICK requires (CLICK refused both).
- Ladder export writes a blank text constant as `$00`, CLICK's code for NUL.
- Ladder export refuses these, which CLICK rejects or would run differently:
  - a pointer into a block mapped at a different bank address (`CLK_PTR_BLOCK_OFFSET`)
  - an empty rung with conditions
  - a rung whose drawing would move a contact's read past an output that writes the contact's tag
- CLICK import reads programs it refused, or turned into code that failed to run:
  - Copy's `to_text` options as CLICK writes them
  - `raw(...)` instructions such as Email
  - For/Next blocks and the contacts inside them
  - OR branches between an instruction's pin rows, and a drum's jump pin
  - hex, Real and text constants such as `FFC0h`
  - Send and Receive addresses in MODBUS hex, and a Send or Receive with a status bit left off
- CLICK import refuses a pin row out of CLICK's order, and a NOP row beside another row.

#### Instructions

- Timers and time drums count time exactly in every unit (a `60s` timer at `dt=0.1` ended a scan late).
- `off_delay()` with a preset of 0, or with its accumulator written to 0, times as a CLICK does.
- `math()` with `sqrt` of a negative, `log` of 0 or less, or `asin` outside -1..1 gives 0 and sets `fault.math_operation_error` (it raised out of the scan).
- A `Real` holding NaN reads 0xFFFF:0xFFFF over Modbus, as on a CLICK. A NaN that stays NaN no longer counts as a change on every scan.
- A TXT blanked by `fill("")` or `reset()` holds NUL, as an unwritten TXT does.
- `rise()` and `fall()` on the clock bits count one edge per toggle in realtime mode.
- A `select()` with tag bounds outside its block sets `fault.address_error` and writes nothing. A `select()` across a gap in a sparse block skips the gap everywhere.
- Comparing two `Char` tags builds a condition (it raised `TypeError`).
- Comparing a block element indexed by an expression with a tag is true when the values are equal (`PLC` found them unequal).
- A tag holds a value of its own type. `patch()` and `force()` store what `copy()` would, so `patch({"Run": 1})` on a `Bool` holds `True`.
- A block slot reached only through `select()` or a pointer keeps its default, its retentive value and its settings, as any other slot does.
- A PLC answers the same whichever block slots another PLC or analysis read first.
- A harness delay longer than 32.767 s now elapses (the feedback never flipped).
- A Real range `.sum()` adds left to right everywhere, so its result no longer differs in the last bit between stepping, compiled runs and generated code.

#### Soft PLC and generated CircuitPython

- pyrung needs pyclickplc 0.5.1, so the soft PLC answers raw Modbus requests as a CLICK does. A read past the end of a bank answers exception 02 (it returned 0).
- Generated CircuitPython drives the fault flags, scan counter, clocks and real-time clock as `PLC` does. A rung that writes SC50 stops the code.
- The generated Modbus server answers as a CLICK does. It refuses a write to a read-only system register (it accepted the write).
- Generated CircuitPython timers use `time.monotonic_ns()`, so they no longer drift after hours of uptime.
- A program with `math()` that is split into `code.py` and `pyrung_rt.py` now imports.
- A text literal copied into a TXT fills consecutive registers in generated CircuitPython (it stopped the scan).
- A `send()` or `receive()` longer than the Modbus limits goes as several requests (it set the error flag).

#### Verifier

- `always()`, `never()`, `reachable_states()` and `pyrung lock` no longer report `Proven` for states their search skipped. When the search can't cover an input, the answer is `Intractable` and names the input.
- `never()` and `how()` try only values a tag can hold, so a literal the tag can't hold no longer raises.
- `never()` no longer proves that a `Char` order comparison can't be met, and `how()` no longer gives up on one. `C > "A"` is met at `"B"`.
- `pyrung lock --check` on a program the verifier can't finish names the `Intractable` and exits 1 (it ended in a traceback).

#### how()

- `how()` reaches more goals, such as `how(Count > 3)` on a counting `math()` and `how(Count != 0)`.
- Some goals that made `how()` raise a Python error now give a `Stopped` plan with a reason.
- `how(And(A, B))` is the same as `how(A, B)`, and `how(~Running)` is the same as `how(Running == False)`.
- `how(avoid=...)` raises `AvoidCheckError` when the avoid check raises (it treated the check as clear and could plan through the avoided state).
- `how()` ends where the program stops itself (it planned straight through the stop). On a PLC in STOP it says `Stopped` unless the goal already holds.
- `how()` reads the settings of a block slot that rungs reach only through a pointer, so it never sets a `readonly=True` slot.
- `how()` drives a timer or counter inside a `forloop()`.

#### History and replay

- `history.at()`, `replay_to()`, `cause()` and `effect()` no longer send Modbus requests (a replayed `send()` could write to the device again).
- A `fork()` of a PLC with a live `send()` or `receive()` sends its own requests (it could collect its parent's answer).
- A `fork()` keeps the run mode and battery of the scan it forks (it always started in RUN with the battery in).
- `replay_to()` returns the state that `history.at()` and `fork()` give for the same scan.
- `history.at()` and a seek replay a scan under the harness and clock it ran under, and across a STOP->RUN restart.
- `cold_rungs()`, `hot_rungs()`, `rung_firings()` and `cause()` on a fork see the scans the fork took from its parent.
- `cause()` explains a writer that feeds on its own output once (it showed thousands of lines or raised `RecursionError`).

#### Programs

- Compiled runs and generated CircuitPython run a `run_function()` as stepping does. `generate_circuitpy()` refuses a function it can't copy exactly.
- A `run_function()` that could keep state between calls runs interpreted, and the verifier answers `Intractable` for it.
- A `run_function()` that scans, stops or reboots its own PLC raises `RuntimeError`.
- Rungs after a nested `with Program()` join the outer program.
- Two threads can build programs at the same time.
- `call()` of a decorated subroutine inside another subroutine keeps the caller's later rungs in the caller.
- `immediate(...)` operands work in compiled runs and generated CircuitPython (they failed with `SyntaxError`).
- Analysis sees inside `forloop()` bodies, including a `return_early()` in a loop.
- `math()` into a pointer destination raises a `TypeError` that says what to do (it raised `AttributeError`).
- A kernel that fails to compile warns and runs interpreted (it raised out of `run_for()`).
- Generated CLICK workspace guidance follows ClickNick's Simulator and its backups.
- Generated CLICK projects import what a `SUM` range or a math function needs in the file that uses it (they failed with `NameError`).

#### Debugger

- After `reload`, breakpoints match each rung to its lines in the edited file.
- Hot reload moves one-shot, shift and drum memory to the matching instruction, so inserting a rung no longer gives one rung's memory to another.
- A `reload`, `step` or `run` sent while Continue runs pauses it first (it failed).
- A `reload` removes a force on a tag the new program no longer has (the force stayed active).
- The debugger sends a `Real` holding Inf or NaN as `inf`, `-inf` or `nan` (DAP clients refused `Infinity`).
- The debugger no longer lists the tags pyrung adds itself.

### Performance

- `run_for()`, `run_until()` and `how()` run compiled on a program with `nop()`, `raw()`, or a `send()` or `receive()` to a named target (every scan ran interpreted).
- `how()` starts faster. A second `how()` on an unchanged program skips several seconds of setup.
- `effect()`, and `history.at()` reading one scan after another, run much faster on older scans.

## v0.15.2 (2026-10-11)

### Fixes

- Launching the debugger with `snapshotPath` now seeds PLC state for generated `src/plc` projects, including plain nicknamed addresses, instead of failing with "no TagMap found in project"; a CSV that is not a PLC data dump is rejected with a clear error.
- `TagMap.load_snapshot()` and `tags_from_plc_data()` accept `blocks` to seed Click bank slots that have no TagMap entry.

## v0.15.1 (2026-09-10)

### Fixes

- CLICK CSV imports reject unsupported executable expressions and preserve imported names and text as data in generated Python.

## v0.15.0 (2026-09-09)

### Breaking Changes

- Static checks now default to fourteen core rules, including always-true OR conditions, always-false comparisons, timer/counter equality hazards, and overwritten writes; use `select={"ALL"}` or `pyrung check --select ALL` to opt into the full registry.

### Features

- CLICK code generation accepts `analog_inputs` from hardware channel parameters and preserves external-input metadata for raw addresses, nicknames, blocks, and structures.
- `how()` can resolve stalled guidance with a bounded one-scan survey of single and paired input changes, confirming useful effects before steering from a fresh read.
- `[tool.pyrung.check]` supports `select`, `extend-select`, and `ignore` with rule prefixes and `ALL`, and reports expose `checked_rules` so integrations distinguish unselected checks from passed checks.
- Pointer checks account for ordered assignments and guards at each scalar indirect access, including branch and continued-rung condition snapshots, with optional `PTR_UNGUARDED_ACCESS` advice when valid bounds cannot be established.

### Fixes

- PILOT retains spent search work across discarded attempts and rollback, and scopes empirical action failures to their concrete source so abstractly similar states can still be tried.
- Comparison checks recognize Modbus receive payloads as external values even when the ladder also resets them, preserving uncertainty through sampled copies and calculations.
- Comparisons of writable numeric flags with `0` or `1` no longer produce constant-comparison findings from an unwritten zero default alone, and no-writer hints share a concise repair message.
- Multi-target `how()` steers toward all goals together, preserving relational bounds and allowing temporary detours instead of driving targets in a fixed order.
- Defensive range comparisons are no longer called redundant solely because of declared or inferred tag bounds, and no-writer advice appears once per operand per rung.

## v0.14.0 (2026-09-04)

### Breaking Changes

- `CMP_OPERAND_STAYS_ZERO` is now `CMP_OPERAND_NO_WRITER`, an advisory that reports the known modeling gap without claiming an HMI-, device-, test-, or retained value remains zero.

### Fixes

- `TAG_FINAL_MULTIPLE_WRITERS` is now an error, matching the explicit `final=True` single-writer contract.
- Ladder lint diagnostics now use declared `min`/`max`, valid pointer addresses, possible coil states, and feedback timing directly, and give clearer repairs for physical commands, recursive calls, waiting steps, readonly writes, tag choices, and redundant rung conditions.
- `CMP_ALWAYS_FALSE` is now a warning instead of an error. `CMP_ALWAYS_FALSE` / `CMP_ALWAYS_TRUE` findings now name the operand's closed values (`DelayedLaser is only 0..90`) and say where that domain comes from: declared `min`/`max` or `choices`, a read-only or never-written default, or the rungs that write it.
- `CMP_EQ_ON_MONOTONE` is now a warning instead of an error, calls out Timer/Counter behavior directly, and no longer suggests `>=` for `!=` comparisons.
- The DAP debugger guide now mirrors all registered console commands, documents current replay and autoharness behavior, and uses the renamed `autoreload` commands; `pyrung live -h` also includes invariant-review and spec commands.

## v0.13.0 (2026-09-02)

### Breaking Changes

- Generated CLICK project subroutine modules and functions now use private `_sub_<name>` identifiers to prevent collisions with tag nicknames; the `@subroutine` decorator and CLICK-visible name are unchanged.

### Fixes

- CLICK nickname imports now surface documented vendor system-name corrections as a structured repair error instead of failing later with a generic duplicate-name exception.
- `how()` documentation describes the result as a verified input-change sequence rather than a minimum-length path, and the Diagnosis guide states what `Reached`, `Stopped`, and the experimental label do and do not promise.

## v0.12.0 (2026-09-01)

### Breaking Changes

- **`pyrung check` now runs ladder lints.** Replace behavioral lock comparisons with `pyrung lock <module> --check`; use `pyrung check <module>` for static ladder findings, `pyrung live check` for the loaded program, and `Program.check()` in Python. `Program.validate()` without a dialect remains a compatibility alias, while dialect validation is unchanged.

### Features

- Simplified terminal forms expose inferred positive permissives for tag-first assertions, and `pyrung live simplified` displays them alongside each expression.
- Generated Click workspaces guide agents through the complete ClickNick proposal workflow, distinguish temporary from persistent storage, and let integrations refresh marked lifecycle guidance without replacing user-authored documentation.
- Timer and Counter clones accept independent `Done` and `Acc` nickname overrides, and Click reverse codegen preserves existing timer/counter nicknames while supplying address-based names for unnamed pairs.
- Choice-backed tags provide `tag.choice(label)` for explicit readable values, and Click reverse codegen reconstructs recognized comparison and `copy()` literals with that form.
- Click ladder bundles include `rung_sources.json`, which maps exported rungs back to their contributing Python source spans for downstream previews.
- The new Ladder Lints guide grounds static checks in ladder-logic practice and documents all 28 core rules, diagnostic output, rule selection, and command-line usage.
- Ladder checks now emit complete compiler-style diagnostics for constant comparisons, unsafe pointer domains, unreachable or recursive subroutines, definite zero divisors, redundant condition terms, overwritten writes, and literal comparisons better expressed as named read-only references or mapped Bool status tags.

### Fixes

- `pyrung live check` uses the loaded runner's configured scan period for timing-sensitive physical checks instead of assuming 10 ms.
- Generated Click projects preserve empty-project and edited-rung round trips, validate exports for address-identity conflicts, resolve digit-ending slot names to their actual hardware addresses, and use the preferred `PLC.state` property in smoke tests.
- Click validation recognizes named DS pointer slots without redundant `TagMap` entries and treats numeric `==`/`!= 0`/`1` conditions as Boolean conventions instead of reporting their operands as stuck at zero.
- `Program.check()` consistently ranks operand-order suggestions as advisories and labels the common uninitialized indirect-address problem as `Pointer Can Be 0`.
- PILOT plans and live progress describe exploratory input changes as set or try operations instead of incorrectly calling them pulses.

### Performance

- Core ladder checks share static-analysis facts, while Click validation reuses frozen tag-map slots and operand resolutions, checks each unique memory-bank pair once, and collects address identities without building a dependency graph.

## v0.11.0 (2026-08-24)

### Breaking Changes

- **PILOT's `via=` route selector was removed.** Use `avoid=` to exclude the reported deterministic route and select another current-world alternative; `RouteTaken.label` no longer includes a `via ` prefix, `RoutePivot.via_hint` is now `RoutePivot.avoid_hint`, and `RouteAlt.via_hint` was removed.
- **`avoid=` sequences are now a union of exclusions.** `avoid=(A, B)` excludes `A` and `B` independently; use `avoid=And(A, B)` to prohibit only the combined condition.
- **`@profile` and string physical-profile names were removed.** Replace `Physical(profile="generic_thermal")` with `Ramp(up=…, down=…)`, `Approach(toward=…, rate=…)`, or `Pulse(on_dwell=…, off_dwell=…)`.
- **Click singleton blocks were removed.** Create fresh, instance-scoped blocks with `x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()`.
- **Coverage APIs now return rung labels.** `query.cold_rungs()`/`hot_rungs()` and `CoverageReport.cold_rungs`/`hot_rungs` return strings such as `"3"` and `"MySub:3"`; existing integer whitelist entries are coerced.
- **Validation rule codes now use category prefixes.** Update `validate(select=…)`, `validate(ignore=…)`, and `finding.code` comparisons according to the table below; `select` and `ignore` also accept a category such as `"COIL"`.

  | old | new |
  |---|---|
  | `CORE_READONLY_WRITE` | `TAG_READONLY_WRITE` |
  | `CORE_CHOICES_VIOLATION` | `TAG_CHOICES_VIOLATION` |
  | `CORE_RANGE_VIOLATION` | `TAG_RANGE_VIOLATION` |
  | `CORE_FINAL_MULTIPLE_WRITERS` | `TAG_FINAL_MULTIPLE_WRITERS` |
  | `CORE_CONFLICTING_OUTPUT` | `COIL_CONFLICTING_OUTPUT` |
  | `CORE_STUCK_HIGH` | `COIL_STUCK_HIGH` |
  | `CORE_STUCK_LOW` | `COIL_STUCK_LOW` |
  | `CORE_POINTER_DEFAULT_BEFORE_BLOCK_START` | `PTR_DEFAULT_BEFORE_BLOCK_START` |
  | `CORE_MISSING_PROFILE` | `PHYS_MISSING_PROFILE` |
  | `CORE_ANTITOGGLE` | `PHYS_ANTITOGGLE` |

- **`run_until()` and `run_for()` now fold by default.** Pass `fold=False` for scan-by-scan execution.
- **`reset()` now writes the target type's OFF value.** Targets reset to `False`, numeric `0`, or empty text instead of their configured initialization default.
- **Conditions can no longer be used as Python booleans.** `Condition.__bool__` raises `TypeError`, exposing mistakes such as `assert value == SomeTag` that previously passed vacuously.

### Features

- **Experimental `how()` steering is more capable and observable.** The bounded, fork-backed controller adapts through feedback and multi-step machines, supports multi-target and `avoid=` requests, streams progress, and can be cancelled without changing the live session; a stopped search means only that it found no path within its current evidence and limits.
- **Recorded causal analysis is instruction-accurate.** `cause(deep=True)` follows the exact fired writer and its recursively established enablers across repeated writes and subroutine calls, while `cause(deep=False)` retains the shallow trigger-only view.
- **Validation findings have severities.** `ValidationReport` adds `errors()`, `warnings()`, `infos()`, `advisories()`, and `has_errors()`; use `assert not report.errors()` for the recommended CI gate.
- **New logic validators catch unsafe state-machine and comparison patterns.** `STEP_NO_ESCAPE`, `RUNG_CONTRADICTION`, `RUNG_TAUTOLOGY`, and the `CMP_*` family detect steps without autonomous escape, contradictory or tautological rungs, skipped monotone equality targets, inverted reset comparisons, unset operands or presets, and unreachable step values.
- **DAP command grammar is available as structured data.** `pyrung.dap.grammar.command_grammar()` describes completable tags, expressions, choices, flags, separators, and clause keywords for editor and REPL integrations.
- **Declarative physical models replace callable profiles.** `Ramp`, `Approach`, and `Pulse` use the new read-only `sys.dt` tag, fold with elapsed time, and survive forks with installed harness and dwell state; `Harness.unlink()` frees selected feedback for fault injection.
- **Timer and counter status bits are available in simulation.** `Tmr.EN`, `Tmr.TT`, `Ctr.CU`, and `Ctr.CD` are populated automatically and flagged as non-portable by Click validation.
- **Click codegen models hardware slots directly.** Generated projects use slot aliases, reconstruct declared blocks and structures even when referenced only indirectly, preserve per-instance defaults and unnamed configured rows, and verify that generated defaults reproduce the source project.
- `effect(from_=, to_value=)` supports explicit destination values for numeric what-if analysis, `why()` and `simplified()` understand subroutine call guards, `when(condition).do(callback)` adds a per-scan reactive hook, and `fork(history_budget=math.inf)` retains a fork's complete replay history.

### Fixes

- CircuitPython Modbus TCP servers now reclaim WIZnet client slots after peers disconnect, and release builds embed a stable `pyrung_rt.py` source name in deterministic `.mpy` bytecode.
- `cause()`, `why()`, `upstream_slice`, and `ProgramGraph` now resolve copy/calc writers, subroutine gates, timer/counter state, indirect writes, fault flags, range/status writers, affine counters, and one-hot pipelines without false-unreachable or over-broad results.
- Click project codegen now emits safe rung-comment literals and all required `Field`/`auto` imports, avoids duplicate structure declarations, preserves retentive and unnamed slot configuration, and reports topology errors with the program section and 1-based rung number.
- Block and structure mappings now share storage with their hardware bank, so indirect reads see configured values instead of zero; programs that indirectly address both aliases are rejected because they cannot share one compiled array.
- Bool feedback waits for sustained commands through real TON/TOF dwell behavior instead of fabricating feedback from short pulses, and `COIL_STUCK_HIGH` recognizes conditionally skipped `out()` instructions while exempting provably exhaustive state-machine writers.
- `always()`, `never()`, `prove()`, and `reachable_states()` preserve coupled inputs and states controlled by `return_early()` or hidden timer/counter events instead of returning incomplete or false proofs.
- `simplified()` preserves reset-dominated outputs, indirect comparisons resolve their operands, indirect-only block slots honor `default_factory`, coverage includes subroutine rungs, and strict mode rejects `comment()` inside a rung body.

### Performance

- Interpreted scans are about 20% faster, full causal-history scans improve by about 2.2x, compiled state-materializing scans improve by about 3x, and cached graph/replay facts substantially reduce causal, verifier, and block-heavy analysis time.

## v0.10.0 (2026-06-03)

### Features

- `plc.why(*tags)` — backward reachability from a frozen snapshot, no scan history required. Load a tag dump from a faulted machine, call `why(Alarm)`, and get the causal path through the program: which instructions wrote each tag, which contacts matter, and which external inputs are at the root. Handles both "why is this ON?" and "why isn't this running?", with latch/reset path analysis and multi-tag merging. Available from the DAP console (`why Tag1 Tag2`) and `pyrung live`.
- `plc.how(condition)` finds the minimum input-change sequence to reach a target state from the current snapshot, with `avoid=` and waypoint decomposition for multi-step targets. Heuristic domain seeding resolves programs with unbounded tag-to-tag comparisons (cross-correlated Reals, calc/copy chains). Path output shows semantic constraints (`Pressure > Setpoint`, `Temp=51 (> 50.0)`) with only changed inputs per step. DAP console syntax: `how State == RUNNING avoid State == FAULTED`.

- `ladder_to_pyrung_project()` now emits a complete agent workspace: `AGENTS.md` with program-specific metadata plus a `CLAUDE.md` compatibility import, `click-cheatsheet.md` (bundled as package data), `.claude/settings.json` (tool permissions), four `.claude/skills/` workflow definitions (diagnose, fix, review, failure), and a `tests/` scaffold with a smoke test and coverage plugin. New `machine_name` parameter sets the AGENTS.md header.
- `ladder_to_pyrung_project` preserves user-edited scaffolding files (pyproject.toml, README.md, .vscode/) on rebuild — only logic files are regenerated. Pass `overwrite=True` to force-write everything.
- `pyrung_to_ladder(..., index=True)` numbers rung markers sequentially (R1, R2, ...) instead of bare `R`; counter restarts per program scope. `ladder_to_pyrung` / `ladder_to_pyrung_project` now accept CSVs with numbered Rn markers.
- `ladder_to_pyrung_project(..., index=True)` annotates each emitted `with rung():` line with an inline `# R1`, `# R2`, ... comment showing the 1-indexed rung position; counter restarts per file. Continued rungs are not annotated.

- DAP console / `pyrung live`: new `get <tag> [tag2 ...]` command prints current tag values without the overhead of `why`.
- DAP `launch` accepts an optional `snapshotPath` argument — a path to a Click CSV data dump. When provided, the PLC is seeded with the snapshot values as its initial state, so the simulation starts from real plant data instead of defaults.
- `StuckBitReport.grouped()` collapses stuck-bit findings that share a write site into one `StuckBitGroup` — a range reset/fill that clears a whole block of coils now reads as a single entry (with `.common_prefix` and per-tag `.findings`) instead of one near-identical finding per tag.


### Breaking changes

- DAP `reload` now re-imports all `.py` files from the program directory instead of relying on Python's module cache. Previously, editing a subroutine file (e.g. `io.py`) and running `reload` would silently keep the old logic. The `watch`/`unwatch` console commands are renamed to `autoreload`/`autoreload off` to avoid confusion with DAP watch expressions. **Migration:** replace `watch` with `autoreload` and `unwatch` with `autoreload off` in any scripts or muscle memory.
- DAP `launch` accepts a new `autoReload` boolean argument. When `true`, the adapter monitors all `.py` files in the program directory and automatically reloads on changes (equivalent to typing `autoreload` in the console). The generated `launch.json` now includes `"autoReload": true` by default.
- `Char` tag default is now `"\x00"` (null character) instead of `""` (empty string), matching Click TXT register hardware default ($00). Empty strings in assignments and comparisons are normalized to `"\x00"` — existing code like `State == ""` continues to work.
- `prove()` is renamed to `always()` and a new `never()` complement is added. `always(logic, condition)` proves the condition holds in every reachable state; `never(logic, A, B)` proves `A and B` is never simultaneously true. **Migration:** replace `from pyrung.core.analysis import prove` with `from pyrung.core.analysis import always` (and/or `never`), then rename call sites. The `prove` module path (`pyrung.core.analysis.prove`) is unchanged. The DAP console command is now `prove always <expr>` / `prove never <expr>`.

### Changes

- Rung references in all user-facing output are now **1-indexed** to match Click/PLC convention and the 1-indexed `Block` pattern — the first rung is `Rung 1`. This covers `cause()`/`effect()`/`why()` chain output, validation findings (`rung N`), debugger stack-frame labels, and the values returned by `plc.query.cold_rungs()` / `hot_rungs()` and emitted in the coverage report JSON. **Migration:** if you maintain a coverage whitelist (`[cold_rungs] allow = [...]`), add 1 to each rung number — a previously whitelisted index `22` becomes `23`. Internal/structural data (`ChainStep.rung_index`, `CausalChain.rungs()`, DAP `rungId`/`rungIndex` trace fields, `to_dict()`/`to_config()`) remains 0-based.
- `ChainStep.proximate_causes` and `ChainStep.enabling_conditions` are renamed to `.triggers` and `.enablers` — the old names remain as read-only properties. Serialization (`to_dict()`) now emits `"triggers"` / `"enablers"` keys.
- `PLC.state` is now the preferred property for accessing the current state snapshot; `current_state` remains as an alias.
- `always()` / `never()` / `reachable_states()` now report all infeasible tags at once instead of stopping at the first pipeline pass that finds a blocker — unbounded-domain tags from classification and unclassified tags discovered during elision appear together in a single `Intractable`, with a state-space estimate from the surviving dimensions.

### Performance

- `always()` / `never()` / `reachable_states()` now factor independent free inputs into separate groups, evaluating each group independently and composing via delta merge instead of enumerating the full cross-product. Programs with 3+ independent input groups see ~3x speedup.
- `split_at=["AutoMode"]` on `always()` / `never()` / `reachable_states()` (also `__lock__["split_at"]`) promotes a stateful coupling tag to nondeterministic, enabling factoring across zones that would otherwise be inseparable. `Intractable` hints now suggest candidates automatically.
- `_grid_to_graph` (ladder codec) is ~4x faster — per-cell function calls replaced with flat arrays and precomputed connectivity bitflags.
- `pyrung_to_ladder(..., validate=False)` skips pre-export checks and round-trip verification for a ~8x speedup (22 ms → 3 ms on a realistic program). Default remains `validate=True`.
- Tag construction with an explicit name (`Bool("Pump")`) no longer runs AST inference, eliminating ~2 ms per tag of `executing` library overhead. Programs with hundreds of named tags see noticeably faster load times.
- `SystemState` scan commits no longer write `_prev:*` entries for non-edge tags, reducing per-scan overhead.

### Fixes

- `always()`/`never()`/`reachable_states()` now validate that kernel-produced values respect user-declared `min=`/`max=`/`choices=` bounds before BFS exploration. Programs where `calc()` or other instructions write values outside declared constraints raise `ValueError` immediately instead of silently using wrong domains.
- Unwritten tags are now auto-promoted to nondeterministic inputs — `external=True` is no longer required for tags the program never writes to. `external=True` remains meaningful for tags that are both written by the program and changed externally.
- Under-specified nondeterministic tags (no `min`/`max`/`choices`, no comparison-derived domain) now surface as `Intractable` instead of silently defaulting to `(0,)`. Add `readonly=True` for genuinely constant tags or declare bounds for HMI/operator inputs.
- `Char` tags now participate fully in domain inference — string-literal copies (`copy("g", State)`) and string comparisons (`State == "g"`) are recognized as domain values. Previously Char state machines were classified as `Intractable`.
- `how()` replay verification no longer fails on paths that require `rise()`/`fall()` transitions through edge-demoted tags — BFS traces now carry the prev values needed for edge detection during replay.
- Hidden-event jumps (timer/counter fast-forward) no longer produce unreplayable traces when the jump fires on an input step that transitioned into an already-visited state — jumps now fire only as a self-loop on the current plateau. Affects `how()`, `always()`, `never()`.
- Slice elision no longer incorrectly elides conditionally-written tags that have no readers — a latch or conditional copy whose entry value persists on the no-write path is now correctly kept as cross-scan state.
- `prove()` no longer returns a false `Proven` (missed violation) when a free input gates a `receive()` whose destination is itself a nondeterministic tag — free-input factoring now keeps the gating input and the written destination in the same group instead of evaluating them independently and dropping the states where the receive does not fire and the injected value survives.
- Pointer-default validator now suppresses findings when a `copy()` or `calc()` unconditionally writes the pointer before any dereference, including writes behind `return_early()` guards where both the write and all reads share the same guard.
- Stuck-bit validator now recognizes `copy()` to a Bool tag as a latch or reset — `copy(True, C)` counts as a latch, `copy(False, C)` as a reset, and `copy(tag, C)` as both, eliminating false stuck-high/stuck-low findings.
- Conflicting-output and stuck-bit validators now detect mutual exclusivity when subroutine callers compare a tag against another tag (e.g. `DS404 == ModeTag` vs `DS404 != ModeTag`) — previously only literal-value comparisons were recognized.
- Choices validator now correctly inspects `fill()` instructions — previously used stale attribute names, causing choice violations on fill targets to go unreported.
- `prove()` domain inference for Real tags with fractional `min`/`max` bounds no longer silently truncates to integers — fractional bounds now seed the partition path, producing a correct finite domain instead of an empty or integer-only one.
- Scoped kernel snapshots no longer drop Char tags written by text fan-out (`copy_convert` with `to_text`, or string-literal copies) — dynamically-created sequential keys are now captured and cleaned up on restore.
- `always()`/`never()` no longer returns a false `Proven` for oneshot `calc()`/`copy()` accumulators — threshold absorption incorrectly classified oneshot writes as constant-stride progress sources, but oneshot instructions only fire on rising edges, not every scan.
- Comparing a `choices`-typed tag against a label string (e.g. `StateCurrent == "IDLE"`) now resolves the label to its underlying key — previously the comparison used the literal string, producing a type mismatch in domain inference.

## v0.9.2 (2026-05-21)

### Changes

- The VS Code debugger extension now resolves the Python interpreter from the `ms-python.python` extension instead of defaulting to bare `python`, and shows a clear error if pyrung is not installed in the selected environment.
- Python 3.11 is supported again — the minimum version is now `>=3.11`.
- `ladder_to_pyrung` and `ladder_to_pyrung_project` now emit `default=` on tag declarations when the nickname CSV carries a non-zero `initial_value`, and inject standalone nickname tags that aren't directly referenced in any rung so they still appear in `tags.py` with their TagMap entry.

### Fixes

- `prove()` no longer returns false results (false `Proven` or false `Counterexample`) in programs with self-resetting counters, counter accumulator comparisons, absorbed condition-gating tags, or `return_early()`/pulse/reset patterns — the traced elision pass has been replaced by a sound-by-construction slice analysis.
- `reachable_states()` no longer misses states when an input drives both a timer enable condition and a downstream comparison through a copy/calc chain.
- `prove()` / `reachable_states()` no longer report Intractable for tags written by indirect-ref copies (e.g. `copy(block[pointer], target)`) — the domain classifier now resolves the pointer's finite domain to bound the target.

### Internal

- `prove()` soundness is now cross-checked by a subset-differential fuzzer that runs every optimization subset — not just all-on — against the unoptimized baseline, catching interaction bugs between optimizations that an all-optimizations-on check misses.
- Traced influence-graph elision replaced by slice elision (sound-by-construction write-before-read enumeration), eliminating a class of soundness bugs while recovering aggressiveness through two new projection passes (functional-dependency and init-constant).

## v0.9.1 (2026-05-19)

### Fixes

- `prove()` / `reachable_states()` could miss violations or reachable states in programs using copy/calc chains, edge-triggered conditions with transient outputs, subroutine-scoped writes, or `latch()` targets.
- Multiple timers expiring on the same scan now correctly reach all combined output states in `reachable_states()`.
- `reachable_states()` now retains locked tags that `prove()` elides, ensuring lock checking explores all reachable values.

## v0.9.0 (2026-05-18)

### New features

- Tag-name inference — `Bool()`, `Int()`, `Real()`, `Dint()`, `Word()`, `Char()` infer their name from the assignment target, so `Foo = Bool()` is equivalent to `Foo = Bool("Foo")`. Typed block constructors (`IntBlock`, `BoolBlock`, `DintBlock`, `RealBlock`, `WordBlock`, `CharBlock`) provide the same inference for memory blocks. Existing explicit names are unaffected.
- DINT truthy conditions — `Rung(dint_tag)` now works the same as `Rung(int_tag)` (nonzero = true); Click validation catches both with `CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED`.
- `rung` lowercase alias — `rung` is now the preferred spelling; `Rung` still works.
- `__lock__` `joint` / `exclusive` input group keys — `joint_inputs=` replaces `input_groups=`; new `exclusive_inputs=` prunes mutually exclusive input combinations from the state space.
- `prove(paced=True)` — forces a stutter scan after any input change, suppressing violations that require back-to-back input flips with no settling time. An automatic aggressive second pass attaches `aggressive_counterexample` to `Proven` results.
- `prove(journal=True)` — per-tag `Journal` showing classification, domain inference, elision, and absorption decisions with proof detail for diagnosability.

### Breaking changes

- Verifier `depth_budget` rename — `max_depth` / `--max-depth` renamed to `depth_budget` / `--depth-budget` on `prove()`, `reachable_states()`, `check_lock()`, and CLI commands.

### Performance

- Scan hot-path micro-optimizations — condition `evaluate()` methods resolve deferred imports, `isinstance` checks, `_contact_tag` resolution, and f-string allocation once at construction instead of every call. Branchless rungs skip branch-enable-map allocation.
- `prove()` significantly faster — optimizations across both state exploration (edge-source demotion, cached walks, identity short-circuits, reduced `isinstance` overhead) and the compiled kernel (per-type store helpers, codegen improvements).

### Fixes

- `prove()` / `reachable_states()` — substantially reworked soundness, backward propagation, and counterexample fidelity, backed by agreement oracles, known-answer tests, and fuzz coverage.
- `reachable_states()` now settles chained hidden events (e.g. counter Done firing a second counter via a transient boolean), fixing missed reachable states.
- `pyrung live` shows usage hint when invoked with no command.
- `prove()` now models time drum instructions as timer-like progress sources, enabling correct reachability proofs for drum-driven outputs and step advancement.
- Off-delay timer (`TOF`) initial Done state is now False when the enable has never been True, matching Click PLC hardware.
- Oneshot `out()` writes False after firing instead of retaining the entry value, matching Click spec.
- `blockcopy()` and `fill()` with indirect ranges set `fault.address_error` instead of crashing on out-of-range pointers.
- `calc()` treats expression overflow as an out-of-range math fault instead of crashing.
- `forloop()` rejects non-positive literal counts; tag-based counts resolving to zero or negative execute one iteration.
- Compiled kernel parity — copy converters (`to_text`/`to_value`/`to_ascii`) expand into sequential tag writes, address-fault classification preserved for indirect sources, and block tag materialization matches interpreted behavior.
- Interpreted runner parity — subroutine-only tags seeded at scan 0, `forloop(..., oneshot=True)` latch stored in scan memory, and instruction memory keys use stable sequential IDs instead of `id()` values.

### Internal

- `prove()` elision replaced with trace-based approach — instruments actual program execution to build a dependency graph, then backward-cone analysis from observers determines elidable tags, replacing the previous static analysis.
- Interpreted runner and prover share the same execution walker (`execute_program`).
- Fuzz reproducers are structurally minimized via delta-debugging before being written.
- Prove agreement oracle — `pytest --prove-agreement` re-runs every `Proven` result with optimizations disabled; opt out with `@no_agreement`.
- Known-answer reachability oracles — `pytest -m known_answer` for hand-enumerated `reachable_states()` ground-truth tests.
- Fuzz test duration configurable via `FUZZ_MAX_EXAMPLES` and `FUZZ_SCANS` environment variables.

## v0.8.0 (2026-05-04)

Major overhaul of `prove()` and `reachable_states()`. Single-flip BFS, pre-BFS elision via abstract interpretation, accumulator absorption (threshold vectors and comparison-only), and a blockless compiled kernel mode (~8× faster steps) together make `pyrung lock` practical on industrial-scale programs that previously hit `Intractable`.

### Breaking changes

- Python 3.12 minimum — bumped from 3.11.
- Lock file default projection is now `lock=True` tags — programs using `TagMap` get physical outputs automatically; others need explicit `lock=True` or `__lock__ = {"include": [...]}`.
- Lock file omits False values — each state now reads as "what's ON"; `check_lock` handles both formats transparently.

### New features

- `lock` tag flag and `TagMap` auto-stamping — new `lock` flag includes tags in the default `pyrung lock` projection; `TagMap` auto-stamps `lock=True` on output-mapped tags and `external=True` on input-mapped tags, and `InputBlock` tags are automatically treated as nondeterministic.
- `band` tag attribute — predicate-based value grouping (`band={"ZERO": 0, "POSITIVE": "> 0"}`) collapses numeric lock file values into categorical labels.
- `__lock__` `joint` key — declares multi-flip input groups for BFS exploration of inputs that must change in the same scan.
- Lock file improvements — progress reporting with queue trend arrows, choice labels instead of raw integers, and `--profile` flag for cProfile output.
- `Intractable.hints` — dimension diagnostics listing the largest state-space contributors when `prove()` or `reachable_states()` returns `Intractable`.
- Pointer-default core validator — `CORE_POINTER_DEFAULT_BEFORE_BLOCK_START` catches the common 1-based block + `default=0` mismatch before runtime.
- Click `[choices=Bool]` shorthand — nickname CSV comments accept `[choices=Bool]` for int-backed boolean dropdowns.
- `UnpackToBitsInstruction.dest` / `UnpackToWordsInstruction.dest` — property aliases matching the `dest` convention used by all other packing instructions.
- New examples — `fill_station.py` (Physical annotations, Harness, `prove()` fault coverage) and `packml_bench.py` (industrial-scale profiling benchmark).

### Fixes

- `call("missing")` now fails at build time instead of compiling cleanly and crashing at scan time.
- Mixed-type values in lock file state sorting no longer raise `TypeError` when choice labels mix with raw integers.

## v0.7.0 (2026-04-26)

### Breaking changes

- Lock file default projection is now terminals — existing lock files generated with the old public-first projection will need regeneration with `pyrung lock`.

### New features

- `__lock__` module-level projection override — `__lock__ = {"include": [...], "exclude": [...]}` customizes which tags the lock file tracks beyond the terminal default.
- Public `Coupling` API on `Harness` — `harness.couplings()` yields `Coupling` dataclasses for iterating all discovered enable→feedback pairings.
- `plc.tags` read-only tag mapping — `MappingProxyType[str, Tag]` of all known tags by name for introspection and test assertions.
- `prove()` settle-pending semantics — `prove()` now settles pending timer/counter Done bits before evaluating, eliminating false negatives for properties guarded by timing.
- `SumExpr` CircuitPython codegen — `BlockRange.sum()` expressions now compile to CircuitPython code.
- Fault coverage example — new `examples/fault_coverage.py` demonstrating `prove()`, `cause()`/`recovers()`, and the coverage plugin.
- `TraceStep` dataclass for counterexample traces — enables accurate replay of timer/counter fast-forward edges.

### Fixes

- `prove()` domain coverage — boundary partitions now emit lit-1/lit/lit+1, property expressions feed into domain analysis, and memory-backed state is included in the visited-state key.

### Internal

- `_AnalogCoupling` renamed to `_ProfileCoupling` for consistency with the `Physical` API terminology.

## v0.6.0

### Breaking changes

- `PLC(history_limit=...)` replaced by `history` / `cache` / `history_budget` — three knobs replace the single snapshot-count parameter: `history` (retention window, e.g. `"1h"`), `cache` (instant-lookup window), and `history_budget` (byte ceiling, default 100 MB).

### New features

#### Declare — tag metadata and physical annotations

- Tag flags: `readonly`, `external`, `final`, `public` — three semantic flags enforced by static validators plus one presentation flag for Data View visibility, with mutual exclusivity enforced at construction.
- `choices` tag metadata — tags carry a `choices` mapping (value→label) through DAP traces, Click CSV round-trip, and VS Code debugger dropdowns.
- `Physical` annotations and autoharness — `physical=`, `link=`, `min=`, `max=`, `uom=` on tags declare device feedback behavior (bool timing or profile functions); `Harness` reads these and auto-synthesizes feedback patches, replacing hand-written test toggles.
- Click nickname CSV physical metadata — tag flags and physical metadata (`min`/`max`/`uom`) survive the nickname CSV export/import cycle.

#### Analyze — static validators, causal chains, and test coverage

- `Program.validate()` with `select`/`ignore` filtering — unified validation entry point with dialect, mode, and finding-code filtering.
- Static validators — stuck-bit detection (`CORE_STUCK_HIGH`/`CORE_STUCK_LOW`), readonly write, choices violation, final multiple-writers, and physical realism checks (`CORE_RANGE_VIOLATION`, `CORE_MISSING_PROFILE`, `CORE_ANTITOGGLE`).
- Runtime bounds checking — tags with `min`/`max` or `choices` are checked per-scan; violations populate `plc.bounds_violations` without clamping values.
- Static program graph analysis — `build_program_graph()` produces rung summaries, `TagRole` classification, and SSA-style def-use chains.
- `plc.dataview` — chainable query API with role/physicality filters, abbreviation-aware name matching, and dependency slicing (`.upstream()`, `.downstream()`).
- `program.simplified()` — resolves each terminal's condition chain back to inputs, eliminating intermediate pivots while preserving series/parallel topology.
- `plc.cause()` / `plc.effect()` — causal chain analysis attributing proximate causes vs enabling conditions, with projected mode for reachability queries and what-if analysis.
- Mixed-fidelity causal chains — recent steps use full SP-tree attribution; older steps fall back to timeline-based approximation when state is out of cache.
- `assume={}` on `cause` / `effect` / `recovers` — scenario-pinning parameter that overrides tag values for projected walks without mutating state.
- `plc.recovers(tag)` — convenience predicate: `True` if the tag has a reachable clear path from the current state.
- `plc.query` namespace — `cold_rungs()`, `hot_rungs()`, `stranded_bits()` surveys with `report()` for mergeable `CoverageReport` objects.
- Pytest coverage plugin — `pyrung_coverage` fixture collects per-test reports, merges at session end, with CI gating via TOML whitelist (`--pyrung-whitelist`).
- Digital twin test harness (`pyrung.twin`) — plain-English `case("sentence", ladder=fn, expect={...})` test slots with `assert_all_passed(results)`.
- Exhaustive state-space verification (`prove()`) — BFS over reachable states using the compiled kernel; returns `Proven`, `Counterexample` (replayable trace), or `Intractable`.
- Lock file workflow (`pyrung.lock`) — `write_lock()` / `check_lock()` serialize reachable states to JSON; behavioral changes show up as diffs in PRs.
- Unified `pyrung` CLI — `pyrung lock`, `pyrung check`, `pyrung dap`, and `pyrung live` commands.

#### Commission — VS Code debugger and live tooling

- Hot-reload (`reload`, `watch`, `unwatch`) — re-execute the program file preserving PLC state; `watch` auto-reloads on save.
- VS Code Data View — panel for watching, forcing, and patching tags with live inline values, flag badges, and public-only filtering.
- VS Code Graph View — interactive Cytoscape.js tag dependency graph with role coloring, upstream/downstream slicing, and live value badges.
- VS Code Chain tab — interactive causal queries (`cause`/`effect`/`recovers`) in the History panel.
- Debug console command system — typed command dispatcher with verbs for stepping, forcing, analysis, monitoring, and annotation.
- `pyrung live` CLI — attach to a running debug session from another terminal with semicolon-chained commands and session discovery.
- Session capture pipeline — `record`/`replay` captures replayable transcripts; a condenser shrinks to causal-minimum reproducers and an invariant miner proposes candidates that generate pytest verification files.

#### Infrastructure and DX

- Byte-budgeted recent-state cache — `history.at()` serves cached scans directly; older scans reconstruct via replay from the nearest checkpoint.
- Timeline-routed transition finding — `cause()`/`effect()` consult per-rung firing timelines before touching state, eliminating per-contact `history.at()` reads.
- Modern Click timer/counter codegen syntax — `ladder_to_pyrung()` emits positional presets and friendly unit strings.
- Type stubs for IDE inference — `tag.pyi` gives IDEs accurate type information for tag imports and `Block` fields.

### Performance

- Sparse scan log + compiled replay kernel — history records only nondeterminism (idle scans contribute zero bytes) and reconstructs older states via a compiled kernel operating on plain dicts instead of immutable `SystemState` objects.
- Reduced per-scan memory overhead — system points are derived at read time instead of written into the PMap each scan.

### Bug fixes

- Modbus `send`/`receive` latching semantics — status flags now latch on completion and persist across disabled scans, matching Click PLC docs; `conflicting_outputs` validator now covers send/receive status tags.
- Snapshot-stable instruction helper conditions — `.reset(...)`, `.down(...)`, `.clock()` and drum inputs now evaluate against the rung's frozen `ConditionView` instead of live mid-rung writes.
- Click subroutine export filenames — `LadderBundle.write()` preserves original filenames instead of slugifying them.
- VS Code webview script regressions — fixed template-literal escaping bugs; `make lint` now syntax-checks embedded webview scripts.
- Derived edge detection on system clock tags — `rise()`/`fall()` on derived tags now uses a derived-edge registry instead of the broken `_prev:*` fallback.
- `scan_counter` wraps at 32768 to match the Click SD9 spec.
- Send/receive I/O replay — scan log now records I/O events for correct state reconstruction during history replay.
- Sparse block-element commit semantics — only elements actually written during a scan are committed to state.

### Migration

- Replace `PLC(logic, history_limit=N)` with `PLC(logic, history="1h")`, `PLC(logic, cache="5m")`, or `PLC(logic, history_budget=bytes)` — or drop the argument entirely to accept defaults.

## v0.5.2 — Friendlier timer/counter API

### New features

- Positional `preset` and `unit` — `on_delay`, `off_delay`, `count_up`, and `count_down` now accept positional arguments: `on_delay(MyTimer, 3000)`, `on_delay(MyTimer, 5, "sec")`. Keyword form still works.
- Human-friendly time units — `unit=` accepts `"ms"`, `"sec"`, `"min"`, `"hour"`, `"day"` (and plurals, abbreviations). Default is `"ms"`. Tag-name suffixes `Tms`/`Ts`/`Tm`/`Th`/`Td` still accepted — `FillTimeTm` stays short, and `Tm` sidesteps the minute-vs-minimum ambiguity of `Min`.
- `DoneAccUDT` protocol — Timer/counter functions now type as `timer: DoneAccUDT` instead of `InstanceView | _StructRuntime`. IDE hover shows the contract, not the implementation.
- `normalize_unit()` exported — Converts any unit alias to canonical form. Available from `pyrung.core`.
- `TimeUnitStr` Literal type — All valid unit strings in one type for IDE autocomplete.

### Migration

- No breaking changes. Existing `preset=` keyword and `unit="Tms"` code works unchanged.

## v0.5.0 — Timer/Counter cleanup

v0.4.0 introduced `Timer` and `Counter` as built-in UDTs with `.named()` for creating instances. That was one special case too many — `.named()` is gone, replaced by `.clone()` which matches how the rest of the tag system works.

### Breaking changes

- `Timer.named()` / `Counter.named()` replaced by `.clone()` — `Timer` and `Counter` are now `count=1` singletons. Use `Timer.clone("Name")` / `Counter.clone("Name")` for named instances. TagMap auto-resolve for timer/counter operands removed — all mappings are now explicit via `.map_to()`.

### New features

- Section comments in TagMap codegen — `TagMap` constructor output now emits `# --- Structures ---`, `# --- Timers & Counters ---`, `# --- Blocks ---`, and `# --- Tags ---` section headers when there are 2+ non-empty groups.

### Migration

- Replace `Timer.named(n, "Name")` with `Timer.clone("Name")`. Same for `Counter`.
- Add explicit `.map_to()` calls for any timer/counter tags that relied on TagMap auto-resolve.

## v0.4.0 — Cleaner surface, honest abstractions

### Breaking changes

- `all_of`/`any_of` renamed to `And`/`Or` — PascalCase combinators replace the old function names. `&` and `|` operators removed for conditions (kept for math/bitwise). Comma inside `Rung(...)` stays as implicit AND.
- Built-in `Timer` and `Counter` UDTs — `Timer` and `Counter` are now built-in structured types with `.Done` (Bool) and `.Acc` (Int/Dint) fields, exported from `pyrung`. Use `Timer.clone("Name")` for named instances. User-defined UDTs with the same shape still work.
- Single-argument timer/counter instructions — `on_delay(timer, preset=...)` replaces `on_delay(done, acc, preset=...)`. Same for `off_delay`, `count_up`, `count_down`. The two-tag form is removed entirely.
- `PLCRunner` renamed to `PLC` — `.active()` removed; `PLC` is now a context manager directly (`with PLC(logic) as plc:`). `dt=` (default `0.010`) and `realtime=True` kwargs replace `set_time_mode()`. `dt=` and `realtime=True` are mutually exclusive. `TimeMode` removed from public exports.
- `set_battery_present()` replaced by property — use `plc.battery_present = False`.
- `plc.debug.*` namespace — 11 debugger-internal methods moved off `PLC` into `plc.debug`: `scan_steps`, `scan_steps_debug`, `rung_trace` (was `inspect`), `last_event` (was `inspect_event`), `prepare_scan`, `commit_scan`, etc. `system_runtime` accessible via `plc.debug.system_runtime`.
- Force API renamed — `add_force()` → `force()`, `remove_force()` → `unforce()`, `with plc.force(...)` → `with plc.forced(...)`. DAP debug console commands updated to match (`remove_force` alias removed).
- `_fn` variants dropped — `run_until_fn` merged into `run_until`, `when_fn` merged into `when`. Both now accept Tag/Condition expressions or callable predicates directly.
- `Program` internals privatized — `add_rung`, `start_subroutine`, `end_subroutine`, `evaluate`, `current` → private. Legacy `call_subroutine` removed.
- `TagMap` internals privatized — `offset_for`, `block_entry_by_name`, `owner_of` → private.
- Time units as strings — `Tms`, `Ts`, `Tm`, `Th`, `Td` removed from public imports. Use `unit="Tms"` string form. `TimeUnit` enum stays internal.
- Validation entry points consolidated — `validate_click_program` and `validate_circuitpy_program` removed from public exports. Use `logic.validate(dialect=...)` or `mapping.validate(logic)` / `P1AM.validate(logic)`.
- `Tag.__rand__` precedence guard — `int & tag` and `BoolTag & tag` now raise `TypeError` with guidance to reorder operands, preventing the Python operator precedence trap where `2 & tag` silently evaluates wrong.

### New features

- Conflicting output target validation — detects multiple `INERT_WHEN_DISABLED=False` instructions writing the same tag from non-mutually-exclusive paths, with condition-complement detection on caller conditions.
- Click timer preset overflow validation — `CLK_TIMER_PRESET_OVERFLOW` warns when a preset exceeds the INT range for its time base.
- `P1AM.validate()` convenience method — mirrors `TagMap.validate()` for CircuitPython programs.

### Bug fixes

- Click bypassed imported contacts — codegen now warns on contacts that were bypassed during import.

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

- Tag defaults now seeded into initial state — tags are populated with their declared defaults at construction time, fixing a disagreement between `Tag.value` and rung condition evaluation for tags with non-False defaults.

### Docs

- Expanded and polished "Know Python? Learn Ladder Logic" tutorial — added ASCII diagrams, adversarial exercises, cross-lesson callbacks, NC naming conventions, and aligned all lesson examples with the Click conveyor reference.
- `pyrung.zen` — `import pyrung.zen` prints guiding principles for ladder logic in Python (à la `import this`).

### Examples

- Conveyor examples (`click_conveyor.py`, `circuitpy_conveyor.py`) updated to follow tutorial naming conventions and best practices.

## v0.3.0

### Breaking changes

- `system.storage.sd.save_cmd` removed — use `board.save_memory_cmd` (`from pyrung.circuitpy import board`).
- `generate_circuitpy(...)` now supports optional `runstop=RunStopConfig(...)` and board-only (zero-slot) codegen.
- `calc(...)` no longer accepts `mode=` — mode is inferred from referenced tag families.
- `send()`/`receive()` now use `ModbusTcpTarget` dataclass instead of inline `host`/`port`/`device_id` kwargs.
- Codegen API cleanup — `TagMap.to_ladder()` removed; use `pyrung_to_ladder(program, tag_map)`. `csv_to_pyrung()` renamed to `ladder_to_pyrung()`.
- Copy modifiers replaced by copy converters — `copy(as_value(source), target)` is now `copy(source, target, convert=to_value)`; all `as_*` functions, `CopyModifier`, and `pad=` removed.
- Search uses comparison expressions — `search(DS.select(1, 100) >= 100, ...)` replaces the old `condition=`/`value=`/`search_range=` form.
- Block slot API replaced — `rename_slot()` etc. removed; use `block.slot(addr)` which returns a `SlotView` with `.name`, `.retentive`, `.default`, `.comment` properties.

### Moved

- `send_receive` module moved from `pyrung.click` to `pyrung.core.instruction`; re-exported from `pyrung.click` unchanged.

### New features

- History time-travel slider — scrub across retained scan snapshots in the VS Code debug sidebar with live tag value updates.
- Raw Modbus TCP and RTU support — `send()`/`receive()` accept `ModbusAddress` for raw register access to any Modbus device, with new `ModbusRtuTarget`, `RegisterType`, and `WordOrder` types.
- `BlockRange.sum()` — `DS.select(1, 10).sum()` returns a `SumExpr` for use in `calc()`.
- Click ladder CSV export — `pyrung_to_ladder(program, tag_map)` generates deterministic CSV files importable into Click programming software.
- Click ladder semantic-loss guard — round-trip now fails loudly on mismatched objects or lossy conditions instead of silently dropping them.
- In-memory round-trip — `ladder_to_pyrung(bundle)` accepts a `LadderBundle` directly for program → ladder → Python source without disk I/O.
- Multi-file project codegen — `ladder_to_pyrung_project(source)` generates `tags.py`, `main.py`, and `subroutines/*.py` from Click ladder CSV, with nickname-based name substitution.
- `immediate()` wrapper — immediate I/O reads in contacts and coil targets, with Click validation for direct-only contacts and `Y` bank coils.
- `TagMap.tags_from_plc_data()` — converts a PLC data dump into logical tag values for initializing a runner.
- Click nickname CSV round-trip improvements — marker-only boundary rows and block-slot address comments now round-trip correctly.
- Empty rung preservation — empty and comment-only rungs survive Click ladder CSV round-trip via `NOP` emission.
- Bare text safeguard in codegen — raises `ValueError` on unrecognised bare AF tokens instead of emitting undefined names.
- Rung comments — `comment("...")` attaches comments to rungs, exported as `#,<text>` rows in Click CSV.
- Nested branches — `branch()` inside `branch()` is now supported, all depths evaluate against the rung-entry snapshot.
- `Rung.continued()` — reuses the prior rung's condition snapshot for multiple independent wires on the same visual rung.
- CircuitPython Modbus TCP codegen — `generate_circuitpy()` accepts `modbus_server=` and/or `modbus_client=` for P1AM-200 Modbus TCP via P1AM-ETH.
- `write_circuitpy()` — convenience function that generates and writes CircuitPython output files in one step.
- Split CircuitPython codegen output — produces `code.py` (program) and `pyrung_rt.py` (shared runtime); pre-compiled `.mpy` available from GitHub releases.
- Crash-safe retentive persistence — CircuitPython retentive tags auto-save to SD card every 30 seconds with atomic rename.
- `named_array` instance API — `instance(i)` and `instance_select(start, end)` for accessing single or ranged instances.
- New examples — `circuitpy_traffic_light_modbus.py` (intersection controller with Modbus) and `circuitpy_retentive_runstop.py` (retentive tags with RUN/STOP).
- Starter project release assets — ready-to-import Click ladder CSV and CircuitPython bundles with pre-compiled `.mpy`.
- "Know Python? Learn Ladder Logic" tutorial — multi-lesson series with a conveyor sorting station example.

### Bug fixes

- OR topology corrections — four fixes for ladder CSV export that could silently corrupt OR branch wiring.
- T junction propagation — bridge topology now propagates correctly through T junctions in codegen.
- Named array stride — corrected stride handling for `count=1` named arrays in the Click collector.
- Analyzer graph reduction — fixed edge cases in the Click rung analyzer.

### Performance

- Faster round-trip program construction in codegen.

### Internal

- Expression class hierarchy replaced with data-driven `BinaryExpr`/`UnaryExpr`/`ExprCompare`.
- Module splits: `tag_map`, `context`, `send_receive`, and `codegen` each split into packages.

### Migration

- Replace `out(system.storage.sd.save_cmd)` with `out(board.save_memory_cmd)`.
- Replace `calc(..., mode="hex"|"decimal")` — mode is now inferred. Split mixed WORD/non-WORD math into separate `calc()` steps.
- Replace `send(host=..., port=..., device_id=...)` with `send(target=ModbusTcpTarget("name", "host"))`. Same for `receive()`.
- Replace `copy(as_value(source), target)` with `copy(source, target, convert=to_value)`. Same for `as_ascii`→`to_ascii`, `as_text`→`to_text`, `as_binary`→`to_binary`. Remove `pad=` — use string literals instead.
- Replace `search(condition=..., value=..., search_range=...)` with `search(range >= value, ...)`.
- Replace `block.configure_slot(addr, ...)` with `block.slot(addr).name = ...` etc.

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
