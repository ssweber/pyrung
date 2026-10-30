# Changelog

<!-- Style guide: one sentence per entry. Describe the user-visible effect, not the
     implementation. Group related fixes/features into a single entry when they share
     a theme. Breaking changes and migration steps can be longer — users need the
     specifics. Detail belongs in commit messages and PR descriptions, not here.

     Review and condense before release — entries accumulate during development and
     should be edited into shape before moving from Unreleased to a version heading. -->

## Unreleased

### Breaking Changes

- The built-in `Timer` and `Counter` follow CLICK's bank defaults: a timer's `Acc` now resets on STOP->RUN (TD is non-retentive) and a counter's `Done` survives it (CT is retentive). Pass `Timer.clone(..., retentive={"Acc": True})` to keep a timer's elapsed time across a restart; `clone(retentive=...)` overrides either field.
- CLICK import no longer invents timer and counter nicknames: a T/TD (or CT/CTD) address without a nickname is named after itself (`Timer.clone("T1", nicknames={'Done': 'T1', 'Acc': 'TD1'})`) instead of `T1_Done`/`TD1_Acc` or `OvenTimer_Acc`, the clone takes the retentive setting and comments of its addresses (`clone(retentive=..., comments=...)`), and a tag named after its own address exports with no nickname, comment or not, so a project imported and exported again keeps its nickname file.
- Timer, counter and Out-coil presets (`on_delay`, `off_delay`, `count_up`, `count_down`, `debounce`, `flasher`, `timed`) must be an int or an `Int`, `Dint` or `Word` tag, and raise `TypeError` otherwise; a `Real` preset compared as `acc >= int(2.5)` compiled and `acc >= 2.5` interpreted, so the two backends finished a count apart, and CLICK refuses one. Copy the value into an `Int` first.
- A `with Rung(...)` written inside `forloop()` is now a body rung of the loop, evaluated on every iteration; it used to become a separate top-level rung placed before the loop.
- `forloop()` behaves like CLICK's For, measured on hardware: a false loop rung skips the body instead of running it once with power off, so body `out()` coils keep their values, body timers and counters neither time, count nor reset, and body one-shots do not re-arm until the loop runs with their rung false; a tag count is read as unsigned 16-bit, so 0 runs no iteration (it ran one) and -1 runs 65535 (it ran one). `reset()` a body coil from a rung of its own if it must drop when the loop stops.
- Counters count rising edges, as a CLICK does (measured on hardware): `count_up()`, `.down()` and `count_down()` add or take one count each time their input goes True, however long it then stays True, instead of one count every scan it is True. Each counter remembers each input as it last saw it, on every scan it runs, reset scans included, so an input held through a reset does not count again, an input already on at the first scan counts, an up and a down edge on the same scan cancel, and a counter in a `forloop()` counts the edges between iterations. A down counter is done at `Acc <= preset` (it was `Acc <= -preset`), so with a positive preset it is done from its first scan. `rise()` around a counter's rung still works and is now redundant; to count scans while a condition holds, use `calc(N + 1, N)`. `run_until()` and `how()` no longer fast-forward a counter under a held input, and generated CircuitPython code counts edges too. `how()` reaches a counter's preset (or an `Acc` threshold) one rising edge at a time: it pulses a steerable input, alternates a compared value (`Sel == 3`, a `Char`) with another one the tag may hold, and cycles an input carried by a coil, latch or seal-in through the inputs that drive it, releasing it first when it is already on (`Stop`, then `Start`, once per count), after releasing a held reset pin and calling the counter's subroutine. When the press that moves the input would also turn the reset on, it clears the reset through another input first; when every cycle of the input would be reset or cancelled by its down input (the same button latches the input and drives the reset, `.reset(Y)` or `.down(Y)` on input `Y`), it stops at once instead of cycling the input until its search budget runs out. It does not yet cycle a timer's `Done` that is already on (that needs the timer dropped and run again); such a drive stops instead of waiting.
- Text and search follow CLICK, measured on hardware: a TXT is a whole byte (`copy(..., convert=to_binary)` of 200 stores 200, `to_ascii` reads codes above 127), `pack_text()` into an `INT` or `DINT` clamps a value past the range (123456 gives 32767) and raises the out-of-range fault, stops at a decimal point (`"12.5"` is 12) and refuses any other non-digit (`"12x"`, with the fault), `to_value` of a blank TXT writes 0, `MDN` of an even count of integers is an integer (the mean of the middle two, truncated toward zero), and a continuous `search()` resumes after an old result inside its range but misses (`-1`) from any other value, including one below the range.
- Drums (`event_drum`, `time_drum`, `time_event_drum`) behave like CLICK's, measured on hardware: reset is a level that wins, holding step 1 with every output off (it used to write step 1's outputs) and dropping a jump or jog rising on its scan; jump and jog act on their edges while the rung is false (they were ignored), the jump after the jog, so it wins (the jog used to run last); a paused drum leaves its outputs alone; and completion turns every output off until a jog, where a jump moves the step but leaves them off. pyrung also brings them back when the drum steps forward by its own event or time after such a jump, which is not yet probed on a CLICK.
- The twin test harness is now the conformance suite: import it from `pyrung.conformance` instead of `pyrung.twin`.
- Generated CLICK projects put pyrung's guidance in `AGENTS.pyrung.md` and `README.pyrung.md`, rewritten on every generation, while `AGENTS.md` and `README.md` become the user's files with a `<!-- pyrung:guidance -->` line pointing at them; `refresh_workspace_lifecycle_guidance()` and the lifecycle markers are removed.
- A `Real` holds only CLICK's range, -3.4028235E+38..3.4028235E+38: a literal, default, `copy`/`fill` source, `calc` constant, patch or force past it raises `ValueError`, and a `calc` or copy result past it saturates at the limit and sets `fault.out_of_range` (SC43), on both backends and in CircuitPython. pyrung still computes a `Real` at 64-bit precision, where CLICK uses 32.
- `generate_circuitpy()` and `write_circuitpy()` lose `mapped_tag_scope`: the generated Modbus server serves every register the TagMap maps, whether a rung uses it or not (what `"all_mapped"` did), and every raw CLICK register a rung uses (`ds[3901]`), which it used to refuse with an illegal-address exception. Drop the argument.
- Out coils follow CLICK on hardware: `debounce`, `flasher` and `timed` never set their Timers' `Done` (CLICK's `T<n>`), so a contact on it is always off, and read their phases from the `Acc` values (TD) alone; a `flasher` whose rung goes false keeps both `Acc` values frozen instead of clearing them, then restarts with a full on phase from 0.

### Features

- `pyrung.conformance.run_modbus()` runs conformance cases over Modbus TCP, observing only DS registers, against pyrung's soft PLC by default or a real CLICK at `host[:port]`; `build_program()` compiles the suite into one CLICK program with one subroutine per case `Group`, on fixed DS blocks from a checked-in manifest, so editing a group means replacing one subroutine, and a group whose subroutine was not repushed is reported stale while the others run; `make test-conformance` runs the suite (`PLC=host[:port]` for hardware, `GROUP=name` for some groups), `make conformance-export` writes the project, `ConformanceProgram.export_workspace()` (`make conformance-workspace`) regenerates a ClickNick workspace's `src/plc/` so later updates reach CLICK as Preview Changes rung diffs; a run against a real CLICK records each case that passed, with a hash of its exported rows, in a checked-in `confirmed.toml` (`record_confirmations()`), and the report counts a case as confirmed only while that hash matches, so an edited case needs another hardware pass.
- Conformance cases reset themselves and document themselves: each case's first rung carries a comment generated from its id, sentence and expectation, and is true while `Cmd = 0`, zeroing the slot and running the case's `init=` (resets through the instructions' own semantics); the harness writes `Cmd = 1` only after the slot's new `Ready` register comes up (`ready=` conditions for resets that take time); `run()` runs every case twice on one PLC and fails a case whose state after the reset differs from a fresh PLC's (`keep=` exempts tags on purpose); cases that share memory are refused at build time; and `make test-conformance` runs the suite twice without restarting the PLC, then each group alone, A -> B -> ... -> B -> A. The slot layout changed (`LAYOUT_VERSION` 3), so export and load the whole conformance project again.
- The conformance suite loads on a CLICK as three projects, since the whole suite is more than CLICK's 8000 steps: `make conformance-export PART=A` (or `B`, `C`) writes one, each main writes its own layout hash to `DS1`, and `make test-conformance PLC=...` reads it to run that part's groups. `pyrung.conformance.read_program_hash()` reads the hash of the program loaded at an endpoint, and `wait_for_scan_counter_wrap()` waits until a CLICK's scan counter SD9 has passed 32767.
- The debugger can act as a simulated CLICK PLC: a `modbus` launch argument serves the session over Modbus TCP (X inputs stay read-only, as on a real CLICK), `speed` paces Continue to real time, and `startMode` plus the `pyrungSetMode`, `pyrungSetSpeed`, and `pyrungModbusInfo` requests (and the `mode` and `speed` console verbs) control RUN/STOP and pacing.
- Force, unforce, and patch accept CLICK addresses such as `X001` or `DS10` in DAP requests and console commands, and reject targets that are neither a tag nor a mapped address instead of creating an unused tag.
- A program with no rungs sends scan frames while it runs, so clients see its scan count advance.
- `pyrungScanFrame` is now sent after every state change, including `pyrung live` commands and Modbus writes, and reports mode, running, speed, measured speed, pending patches, and what caused it; `pyrung live` commands also notify the debugger with their stop and trace events.
- A `step` or `run` sent while Continue runs pauses it and then executes instead of failing.
- `PLC.start()` enters RUN without executing a scan, `PLC.mode` reads `"run"` or `"stop"`, and `PLC.pending_patches` shows patches not yet committed; the same API is on `CompiledPLC`.
- `PLC(keep_forces_on_run=True)` (or the `keep_forces_on_run` property, or the debugger's `keepForcesOnRun` launch argument) keeps forces across STOP -> RUN; by default they are still cleared.
- `plc.debug.abandon_scan()` re-queues the patches a partially stepped scan had taken, and the debugger uses it so a patch sent while paused mid-scan is never lost.
- `ClickDataProvider` accepts a runner getter, Click bank `blocks` for slots without a TagMap entry, and an `on_stop` hook, and can report the tag served at an address.
- Bench links: the `link`, `unlink`, `restore`, and `links` console verbs and the `pyrungLink`, `pyrungUnlink`, `pyrungRestoreLink`, `pyrungSetLinks`, and `pyrungListLinks` requests add, replace, or cut feedback links for the debug session without editing the program. They survive `reload`, can be set aside together (`links bench off`) or one at a time, are recorded by captures, and are refused when the program writes the feedback. Frames carry `linksRevision`, and `linksPending` gives the simulated milliseconds left on each bool feedback's running delay (`Harness.pending_ms()`).
- `Harness.set_overrides()` replaces or cuts declared couplings, or adds new ones, by feedback tag; `Harness.declared()` lists the program's own couplings, `Harness.pending()` reports a bool feedback still counting toward a flip, and `link_problem()` explains why a coupling cannot run. `Harness.unlink()` is now a cut override.
- In a CLICK nickname comment, `link=` accepts a CLICK address (`link=Y001`, read as the nickname at that address) and a value trigger (`link=State:SORTING`).
- `link="~Tag"` drives feedback while a Bool enable is off (normally closed), in Python, UDT fields, nickname comments, and the `link` console verb; bench-link text shows it as `~Tag`. `~` on a non-Bool enable is refused.
- `PHYS_LINK_UNKNOWN` warns when a `link=` names an enable the program never uses, which the harness skipped silently.
- `rng.max()`, `rng.min()`, `rng.median()`, and `rng.mean()` on block ranges join `rng.sum()` inside `calc()`, export to CLICK as `MAX`, `MIN`, `MDN`, and `AVG`, import back, and run on both backends and in CircuitPython; CLICK validation checks range order, bank, bit-range, and `MDN` size rules (`CLK_CALC_RANGE_NOT_CONTIGUOUS`, `CLK_CALC_RANGE_MIXED_BANKS`, `CLK_CALC_BIT_RANGE_AGGREGATE`, `CLK_CALC_MEDIAN_RANGE_TOO_LARGE`).
- `toggle()`, `debounce()` (both sides required, as in CLICK), `flasher()` and `timed()` are CLICK v4's Out coil types, each owning its `(timer, preset)` phases; they run on both backends and in CircuitPython, fold in `run_for()`/`run_until()`, steer in `how()`, export to CLICK and import back, and CLICK validation checks their timer banks and units; the prover models `toggle` exactly and reports the timed ones as Intractable for now.
- `time_event_drum()` is CLICK v4's Time & Event drum: a step ends when its time is up and its level event is on, either half may be `None`, and it runs on both backends and in CircuitPython, folds in `run_for()`/`run_until()`, steers in `how()`, and exports to CLICK and imports back; the prover reports a program using it as Intractable for now.
- `forloop()` bodies take conditioned body rungs (`with Rung(...)` inside the loop, contacts evaluated on every iteration), `break_loop()` and `continue_loop()` (CLICK v4 Break and Continue, each in a body rung with a contact), and nested loops up to three deep, each with its own `loop.idx`; they run on both backends, in CircuitPython and in the debugger, export to CLICK (a body rung keeps its contacts, `break()`/`continue()`) and import back, CLICK validation checks loop depth across `call()`, inner one-shots and `return_early()` inside a loop (`CLK_FORLOOP_NESTING_DEPTH`, `CLK_FORLOOP_INNER_ONESHOT`, `CLK_FORLOOP_RETURN_INSIDE`), and the prover reports a program with a conditioned body rung as Intractable for now.
- `randint(lo, hi)` (from `pyrung.core`) is CLICK's `RAND`: a draw from `lo` to `hi` inclusive on every scan, deterministic per scan so replay, seek and `fork()` see the same values, overridden in tests by patching or forcing the site's tag (`expr.tag`); it runs on both backends and in CircuitPython (as `random.randint`), keeps `run_for()`/`run_until()` stepping scan by scan, exports to CLICK as `RAND ( lo , hi )` and imports back, the verifier explores every value in range, and it is refused inside a `forloop()`.
- A `time_drum` preset may be a register (CLICK v4) and exports to CLICK; `CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED` is removed, since pyrung leaves CLICK version checks to the tool that pastes into CLICK.
- Conformance cases can check CLICK system bits the ladder cannot record (`case(expect_system={"SC11": 0})`), read by the harness in-process and over Modbus; the wire run refuses a PLC already in STOP.
- Conformance cases expect what a real CLICK does: `case(pending="S2")` marks one pyrung does not meet yet, which pyrung's own targets run as a strict xfail (reported `XFAIL`, failing once pyrung matches) while a run against a CLICK judges it as usual; `case(probe=...)` names the open hardware question a case asks; and `Group(calls=2)` has main call a group's subroutine twice a scan.
- `make test-conformance TARGET=circuitpy` (`--conformance-target`, or `run_modbus(..., sim=CircuitPySim)`) runs the conformance suite against the program's generated P1AM-200 code instead of pyrung's soft PLC: `pyrung/circuitpy/host.py` runs a generated `code.py` unchanged on a desktop Python, with stand-ins for the board's hardware and its own Modbus server on a local port. Each conformance slot's spare register is now mapped, under its own address so the exported CLICK project is unchanged, and every target serves a slot's whole block.
- `pyrung.click.estimate_steps()` estimates an exported ladder's CLICK program steps from per-element costs measured on CLICK Programming Software (within about 1% on a 6,000-step project), and validation reports `CLK_STEP_LIMIT` as a program nears or passes CLICK's 8000 (`pyrung_to_ladder` warns with it and still exports); the costs are in the new CLICK Internals page.

### Fixes

- The verifier no longer reports `Proven` for a property that an input changing on the very scan a timer finishes (or the scan before) breaks through a site that sees the edge: a one-shot (`copy(1, F, oneshot=True)` under `Rung(Btn)`), `rise(X)` of a coil `X` an input drives, or a counter, whose result a rung reading `rise(T.Done)` sees on that scan or the next; it jumped over the timer's run and tried input changes only before and after the scan it finished on.
- A CLICK subroutine whose last rung is a conditional `return_early()` now exports with the unconditional `return()` CLICK requires after it; it used to export without one, and CLICK refused the subroutine.
- `always()` and `never()` could report `Proven` for a violation reachable only through a one-shot (or a drum's jump, jog or event memory, a shift clock, a `toggle`) that the first scan does not reach, such as one in a subroutine not called on scan 0; that memory is now part of every explored state.
- `always()` and `never()` could report `Proven` for a state a `time_drum` reaches by timing a step while its completion flag is on: after a jump back (the flag stays on through it), from a flag that starts on (`default=True`), or after another rung writes its step register or flag. Such a program is now Intractable for every query, like one with a `time_event_drum`, and the hint names the drum's accumulator and the reason, until the prover models that drum's time exactly.
- Debugger hot reload carries one-shot, toggle, shift and drum memory to the matching instruction of the reloaded program, so inserting or deleting a rung no longer hands one rung's memory to another, and it resets and lists in the reload summary the values with no unambiguous match.
- CLICK ladder export refuses an empty rung with conditions (`with Rung(C1): pass`) and an empty rung that shares a CLICK rung with a `.continued()` rung, instead of writing a NOP that CLICK rejects (a contact in front of NOP) or deletes (a NOP row beside another row); CLICK import refuses a CSV with a NOP row beside another row.
- CLICK ladder export refuses a box with pins (counter, retentive timer, shift register, drum) that shares its rung's input wire with another output, which CLICK does not compile (C000A: all inputs of a box must start from the same branch); give the box a rung of its own.
- CLICK ladder export writes a NUL text constant (the blank a TXT holds) in a `copy()`, `fill()`, compare or `search()` as CLICK's blank `""`; it wrote a raw NUL inside the quotes, which ends a CLICK text constant early. A longer text constant with a NUL in it is refused. Validation (and so ladder export) refuses `out()`, `latch()` and `reset()` on anything but bit memory (`reset(DS1)`, `reset(TXT1)`) as `CLK_BANK_WRONG_ROLE`: CLICK's Out, Set and Reset coils take X, Y, C, T, CT and SC only.
- CLICK ladder export writes a one-register `fill`, `blockcopy`, `pack_*` or other range operand as `TXT12..TXT12`; it wrote a bare `TXT12`, which ClickNick's Guided Paste refused.
- Generated CircuitPython code no longer fails to import when a program with a `calc()` is split into `code.py` and `pyrung_rt.py`, and a text literal copied into a TXT (`copy("ABC", txt[1])`) fills consecutive registers, as on the other backends, instead of stopping the scan with `ValueError`.
- Generated CircuitPython code drives the system points the other backends do: SC40, SC43, SC44 and SC46 clear at the start of every scan (and an out-of-range pointer copy sets SC44), SD9 counts scans, SC1, SC2, SC3 and the clock bits SC4..SC9 move, SC10 reports the RUN/STOP switch, SD10 the scan time, and SD19..SD26 the real-time clock.
- Generated CircuitPython code stops as the other backends do: SC50 written by a rung or over Modbus puts the code in STOP with SC11 off, with or without a RUN/STOP switch, and STOP holds until the switch goes to STOP and back to RUN. Before, a program without a switch kept scanning, and a Modbus stop lasted two scans.
- Generated CircuitPython timers take their time from `time.monotonic_ns()`, so they no longer drift after hours of uptime on the board, where `time.monotonic()` loses millisecond resolution; the first scan after STOP->RUN now counts its time.
- A TXT blanked by `fill("")` or `reset()` holds NUL, as an unwritten TXT does and as CLICK's text registers do, so its `to_ascii` copy writes 0 instead of leaving the destination unchanged, and a blank inside a `pack_text` range is refused with SC43; generated CircuitPython code also starts an unwritten TXT as NUL and keeps a TXT received over Modbus or loaded from retentive memory as NUL.
- `cause()` explains a writer that feeds on its own output once instead of once per scan: an accumulator `calc(T + 3, T)` shows the rung that added and the rung that seeded `T` (a `copy(Setpoint, T)` on reset) instead of thousands of lines, or a `RecursionError` after about 500 scans, and two calcs feeding each other show one round.
- CLICK import reads Send and Receive remote addresses written in MODBUS hex (`50Eh` with its register type), which it refused, and reads MODBUS 984 coil addresses (`1`..`65536`) as coils instead of holding registers. CLICK export keeps a remote address in the form it was written: `ModbusAddress("50Eh")` exports as hex instead of turning into `401295`, so a hex program imported and exported again keeps its addressing.
- `send()` and `receive()` status bits (`sending`/`receiving`, `success`, `error`, `exception_response`) are optional, as in CLICK: one left off runs on a hidden tag and CLICK export leaves it off, so CLICK programs that turn a status bit off import instead of failing.
- CLICK import of a program with `raw(...)` instructions (Email, Home and other unsupported ones) imports `raw` in the generated code, which failed with a NameError.
- CLICK import reads Copy's `to_text` options as CLICK writes them, including `termination_code=none` and `$XX`, which failed to import or run.
- CLICK import writes a drum's jump pin as `.jump(cond, step=...)`; the step was passed positionally and the generated code failed to run.
- `calc(randint(...))` over WORD tags runs and exports in hex mode; the draw's hidden tag made it count as mixing hex and decimal, so it ran in decimal mode and CLICK export refused it.
- A `Program`, `subroutine` or `@program` built from code with no source file, such as `exec()` of a generated string or the REPL, skips the strict control-flow check quietly instead of warning; source that exists but cannot be parsed still warns.
- CLICK import keeps the contacts of a rung between For and Next, which it dropped, and reads nested For/Next blocks.
- `how()` drives a timer or counter inside a `forloop()`, which got no enable condition from the loop's rung and stopped waiting on it.
- `call()` of a decorated subroutine inside another subroutine no longer sends the caller's later rungs to the main program.
- A `return_early()` inside a `forloop()` now guards the subroutine's later rungs in the program graph behind `cause()`, PILOT, folding and the verifier, which treated them as unguarded.
- The verifier no longer reports `Proven` when a property depends on which element an index picks: a pointer the program never writes (`copy(DS[Ptr], N)` with `Ptr` in 3..4) was held at its default as if it were constant storage, an offset index (`DS[Ptr + 2]`, `DS[6 - Ptr]`) was taken to read the elements in `Ptr`'s own range, an index that is not an offset of one tag (`DS[Ptr * 2]`) to read only those too, and an index with no `min`/`max`/`choices` (`DS[Ptr]`, or a pointer computed every scan) to read nothing, so an element only it read was dropped from the search; the program graph behind `cause()`, PILOT and folding now maps each index through its offset, the verifier has an unbounded index read the block elements that can change (written, `external`, or in an `InputBlock`) at the addresses its pointer's writers can produce, and a hint for an unbounded offset pointer suggests the pointer values that land in the block.
- The verifier no longer reports `Proven` when a property depends on an element written through an unbounded index (`copy(7, DS[Ptr])`, `Ptr` with no `min`/`max`/`choices`) into a block of more than 1,000 elements, such as a CLICK `DS` bank: the write counted as writing nothing there, so a read of that element through another index saw only its power-on value. It now writes the elements its pointer's writers can produce, which also keeps a counter in such an element (`calc(DS[7] + 1, DS[7])`) from being tracked only by the limits it has passed when the write can jump it.
- The verifier no longer reports `Proven` when a counter kept in a block element (`calc(DS[7] + 1, DS[7])`, reset at 5) is read back through an unbounded index (`copy(DS[Ptr], N)`): the check that lets it track only which limits the counter has passed missed the indexed read.
- `blockcopy` into an indexed window (`DS.select(Ptr, Ptr + 1)`) no longer makes the verifier report every element of the block as having no domain (`Intractable`): the elements the window can reach take the source elements' values, and the rest of the block keeps its own.
- The verifier no longer drops a tag from its search when an index reads it before the rung that writes it (`copy(DS[P], N)` above `copy(K, DS[2])`): the check that a tag is always written before it is read held the pointer at its default, where the index read some other element, and never tried the pointer value that reaches the tag.
- The verifier no longer reports `Proven` when a race between timers depends on when an input starts one of them (`B` latching a 50 ms `T2` partway through a 100 ms `T1` and a 120 ms `T3` it must finish between); it tried the input only when the running timers started and when the first of them finished, and a revisit with the timers at a different phase was taken for one reached by waiting.
- `always()` and `never()` no longer report `Proven` for a property that reads a tag the program never touches (`always(logic, Or(~M2, M0))` with `M0` in no rung): such a tag is now an input the property must hold for, or a constant if it is `readonly`, and a condition the verifier cannot evaluate makes the result `Intractable` naming it, as does a callable property that reads such a tag (`lambda s: s["M0"]`), which raised `KeyError`. An input compared only with `==`/`!=` in the property's cone no longer crashes the search when another rung compares it with `>`, and its counterexample values are ones the PLC can hold.
- `run_for()`, `run_until()`, `how()` coasts, and history seeks now end on the same state and scan count as stepping scan by scan when rungs read RTC fields (`rtc.second == 30`, copies of the clock), `sys.scan_counter`, or tags written further down the scan, when a rung counts or pulses under a system clock (`Rung(sys.clock_1s): count_up(...)`), when timers run at a `dt` whose multiples are inexact in floating point (ms timers at `dt=0.3` ended a unit low, seconds timers at `dt=0.1` a scan early), when a calc feeds its own destination (`calc(Tot + K, Tot)`), when a tag is forced, when a seconds-unit timer runs through a repeating cycle, and when a rung above a timer, counter or self-calc compares its value or tests a slow timer with `==`, when a one-scan pulse resets a counter, when a system clock gates a `time_drum`, when a rung, an instruction's reset, a `run_until()` stop or a callable predicate reads a timer, counter or self-calc other than by a plain comparison (`(Tmr.Acc // 5000) == 1`, an index, a stored copy, a moving preset or threshold), when a callable predicate reads `runner.state` instead of its argument, when a `run_until()` condition reads through an index (`DS[Idx] == 3`), and when something else also writes a timer's `Acc` in the scan (a `copy(0, Tmr.Acc)` rung, a second `on_delay` on the same `Timer`, a forced `Acc`, a second call into the timer's subroutine), which could reset the timer and time it again every scan, and when a `how()` coast skips whole cycles past a timer's 32767 limit; skipping steady scans used to land a few scans off, lose counts, or miss an RTC tick, and a replay or `history.at()` of a run that skipped counting scans showed the skipped counts early, and `history.range()` / `latest()` returned only the scans a fold landed on (recorded `cause()` / `effect()` now raise `ValueError` for a scan a fold skipped, on a fork of the run too); a folded run no longer leaves `sys.scan_time_max_ms` at the length of the skipped stretch, and a rung reading `sys.dt` or `sys.scan_time_current_ms` keeps the run unfolded.
- A rung with a coil before a drum, timer, counter or shift register that has `.reset()`, `.jump()`, `.jog()` or `.clock()` now exports to CLICK; the instruction's pin rows were dropped and the export failed its round-trip check.
- An `immediate(...)` coil target used only there now runs on the compiled backend instead of failing with a missing-symbol error.
- CLICK validation reports `CLK_TIMER_PAIR_MISMATCH` when a timer's or counter's Done and Acc map to different numbers (`T5` with `TD7`), which CLICK cannot store and the export used to write silently.
- CLICK validation reports `CLK_NICKNAME_TOO_LONG` for a nickname longer than CLICK's 24 characters, mapped or set on a bank slot, so ladder export refuses it; export used to write it silently.
- `abs()` exports to CLICK as `ABS ( ... )` instead of lowercase `abs(...)`, imports back as Python's `abs`, and is flagged in hex-mode math, where CLICK has no `ABS`.
- CLICK math formulas export spaced the way CLICK's formula editor writes them (`SQRT ( DS1 )`, `LSH ( DH1 , 2 )`, `DS1 * ( DS2 + 1 )`), so an exported CSV matches a copy-back from CLICK v4, which reports `ABS( x )` as a Math instruction error; the importer reads both the spaced and unspaced forms.
- `.sum()` over a Bool range (C, X, Y, ...) exports and imports as plain `SUM ( C1 : C8 )`.
- Generated CLICK projects import the bank behind a `SUM ( DS1 : DS10 )` range, and math functions such as `sqrt` in the subroutine file that uses them, instead of failing with a `NameError` when run.
- The verifier no longer reports `Proven` for a property that an input can break through a tag the program rewrites every scan (`copy(Ext, X)` then `Rung(X == 7)`, or `copy(Button, B)` then `Rung(rise(B))`), or through a tag a rung reads after an earlier rung in the same scan wrote it (`Rung(Arm): latch(P)`, `Rung(And(P, Fire))`, `Rung(): reset(P)`); it skipped enumerating that input. It also held every system tag at its declared default, so `sys.always_on` read False, `sys.first_scan` never fired, clocks never toggled and the runtime's scan-start reset of fault and command bits never ran: `sys.first_scan` is now True on scan 0 only, constant system tags read their runtime values, and clocks, `sys.scan_counter`, `sys.scan_clock_toggle` and the RTC fields (within their calendar ranges) are explored as free inputs, with a caveat on any counterexample that uses them, whether a rung or only the property reads them. A rung or property that reads a fault flag (`calc(100 / Divisor, Q)` then `Rung(fault.division_error)`) now depends on the operands of every `calc()`, `copy()`, `blockcopy()`, `fill()` and `pack_text()` that can set it, in the verifier and in the program graph behind `cause()`, PILOT and the lints. `never()`, a callable property and a `reachable_states()` projection now explore an input that reaches what they read through data flow alone (`calc(100 / Divisor, Q)` with `Q` or `fault.division_error` observed), which they held at one value, so `never(logic, fault.division_error)` was `Proven` and lock files could miss states; a callable property's `scope=` is its read set, and without one every input stays in play. When the free inputs read at one state have more than 10,000 joint values (`rtc.minute`, `rtc.second` and `rtc.hour` compared together), the verifier still searches an evenly spaced sample of them for counterexamples but reports `Intractable` naming them instead of `Proven`, and `reachable_states()` returns `Intractable` instead of the sample's states. A timer, counter or integer counter that advances once per input pulse or change (`Rung(rise(X)): calc(N + 1, N)`, `count_up()` under `rise()`, `Rung(M != P): calc(N + 1, N); copy(M, P)`) or beside a bit that toggles every scan now reaches its preset in the verifier, which only accumulated progress while every input was held, so `always(logic, N < 3)` was `Proven`; the counterexample repeats the pulse sequence. A wide tag compared only against a tag the program rewrites every scan (the last minute seen, against `rtc.minute` copied this scan) now keeps its value in the search instead of only the comparison's last outcome. A timer in a unit coarser than the scan (`on_delay(T, 1, "sec")`, `"Ts"` or `"min"` at a 10 ms scan, on, off or retentive) now reaches its preset in the verifier, which saw it advance only its fractional carry and took it for idle, so `always(logic, ~T.Done)` was `Proven`; hidden-event jumps and cycle pumping now advance every timer on the runner's per-scan carry, so its counterexample lands on the scan the runner does. The PackML example's one-second state and task timers now run in the search, which explores about twice as many states. A timer that resets itself on its Done (`Rung(~T.Done): on_delay(T, 50)`), the usual pulse generator, now drives a count of its laps (`calc(N + 1, N)`, `count_up()` or `rise(T.Done)` under `T.Done`) to its threshold in the verifier; a lap spans the timer's jump to its Done, which no cycle could cross, so `always(logic, N < 3)` was `Proven`. A state reached again with a counter ahead of or behind the first visit there (its pulses made on another branch: `Rung(A): latch(L)`, `Rung(L): on_delay(T, 1000)`, `Rung(rise(Y), L): calc(N + 1, N)`, `Rung(N >= 3, ~T.Done)`, where the step that latches `L` can also count), or with a running timer behind it (started later, after a detour), is now explored in its own right; the verifier kept only the first visit, so a race won by the head start or the late start was `Proven`. An input whose `rise()` or `fall()` only an instruction's own condition reads (`count_up(C, 4).down(rise(Y))`, `.reset(rise(R))`) now keeps its previous value in the search; the verifier took it for a free input and its edge for dead, merging the scans before and after the rise, so an up/down counter that reaches its preset was `Proven` unreachable. A callable property (`always(logic, lambda s: s["N"] != 50)`, with or without `scope=`) and a `reachable_states()` projection now read the exact value of a counter or timer accumulator, which the verifier hid behind its thresholds and showed them one representative value of, so the property was `Proven` and the projection listed two of the counter's values; a counter they read that adds to itself but is compared nowhere in their cone now makes the result `Intractable` instead of being left out of the search. A timer toward a tag preset (`on_delay(T, P)`, retentive or not) that advances beside a bit that toggles every scan now reaches its other thresholds (`T.Acc >= 40`) once its preset is crossed; the verifier never followed such a timer around a multi-scan cycle.
- Validation messages show range sums and math functions as source (`DS.select(1, 5).sum()`, `abs(X)`, `sqrt(X)`) instead of class names, and CLICK nesting-depth checks count `SUM ( ... )` as a paren level.
- Generated CLICK workspace guidance asks for **Simulate** in ClickNick before `pyrung live` (Stop in ClickNick's main window ends the session), points PLC data loading at the Simulator's **PLC Data...**, and no longer names a fixed debugger extension version.
- `pyrung_to_ladder(validate=True)` and `ladder_to_pyrung(validate=True)` now compare each output's condition, pins and snapshot with the generated code and raise on a lost contact, while a change of shape (continued rows coming back as branches) still passes.
- CLICK import reads OR branches and padding rows between an instruction's pin rows (`event_drum(...).reset(Or(C6, C7)).jog(C8)`, and the same on timer, counter and shift pins), which it refused with "Pin row must immediately follow its owning instruction row"; pins must still come in the instruction's order.
- CLICK export writes hex constants the way CLICK does (`Ah`, `FFh`) in hex-mode Math, in a copy or fill into DH/XD/YD, in a compare against one and in a search over DH; they went out in decimal, which CLICK reads as hex, so `DH1 + 10` added 16 on the PLC. A bare number in hex-mode Math imports as hex, as CLICK reads it, and a compare constant outside 0..65535 is refused. `Real` constants export as CLICK writes them (`3.0E+38`, `1.0E-10`).
- A reversed shift range (`shift(C.select(1011, 1018).reverse())`) exports as CLICK's descending range `C1018..C1011` and imports back; it went out as a bracket list that the generated code could not run.
- `calc()` with `sqrt` of a negative, `log`/`log10` of 0 or a negative, or `asin`/`acos` outside -1..1 no longer raises out of the scan: it stores 0 and sets `fault.math_operation_error` (SC46) for the rest of the scan, and the PLC stays in RUN, as a CLICK does (its help says SC50 turns on and the PLC goes to STOP; the hardware does neither), on both backends and in CircuitPython.
- `rise()` and `fall()` on the timed clocks (`sys.clock_10ms` .. `sys.clock_1h`) count one edge per toggle in realtime mode; 1 ms scans counted about 80 rises of `clock_100ms` a second.
- An `off_delay()` with preset 0 keeps timing when a scan adds less than one unit (scans under 1 ms at `"Tms"`); its accumulator stayed at 0 for good.
- `always()`, `never()` and `reachable_states()` no longer report `Proven` (or miss states) when an `out()` coil is read by a rung above the one that writes it, so the read sees the coil's value from the previous scan (`Rung(Y2): out(Y3)` above `Rung(Y1): out(Y2)`, in the main program, a subroutine, or through an `Or` or a branch): the verifier left such a coil out of its state, merging scans that differ on it.

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
