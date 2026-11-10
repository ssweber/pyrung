# Changelog

<!-- Style guide: follow the Writing section of the root AGENTS.md. Describe the
     user-visible effect, not the implementation. An entry that groups several changes
     is a one-sentence headline with one sub-bullet per change. Detail belongs in
     commit messages and PR descriptions, not here.

     Review and condense before release — entries accumulate during development and
     should be edited into shape before moving from Unreleased to a version heading. -->

## Unreleased

### Breaking Changes

- `calc()` is renamed `math()`, CLICK's name for the instruction, in `pyrung`, `pyrung.core` and generated projects:
  - `calc()` still works but warns `FutureWarning`, and will be removed in a later release
  - `from pyrung import *` or `from pyrung import math` now hides Python's `math` module. Import Python's under another name (`import math as pymath`)
  - finding codes `CLK_CALC_*` are now `CLK_MATH_*`, and `CLK_EXPR_ONLY_IN_CALC` is now `CLK_EXPR_ONLY_IN_MATH`
- `math()` follows CLICK's Math, in `PLC`, `CompiledPLC` and generated CircuitPython:
  - each intermediate result in a formula is a 64-bit float, so a whole number past 2**53 loses its last digits
  - `%` takes the sign of the dividend, so `-7 % 2` is now -1 (it was 1). Outside `math()`, `%` and `**` still work as in Python
  - `%` by 0 and `0 ** 0` give 0 with no flag (they set `fault.division_error`, and `0 ** 0` was 1)
  - 0 raised to a negative power is +Inf (it set `fault.division_error`). A `Real` stores +Inf, and an `Int`, `Dint` or `Word` stores 0. Both set `fault.out_of_range`
  - `abs()` of an `Int` holding -32768 or a `Dint` holding -2147483648 is 0 with `fault.out_of_range`
  - a hex shift that pushes a bit out of the Word sets no flag
  - a math error zeroes only the operation that failed and sets its flag. The rest of the formula still runs, so `X / 0 + 5` now stores 5 with `fault.division_error` (the whole formula was 0)
  - each whole-number constant is cut to 16 bits, whatever the destination. So `math(DD1 / 65536, DD2)` divides by 0 (0 with `fault.division_error`), and `math(2 * 40000, DD2)` writes -51072. Values read from tags keep their width
  - dividing by a constant that cuts to 0 is an error-level `MATH_DIV_ZERO` finding, and CLICK validation reports any constant past 16 bits as `CLK_MATH_WIDE_CONSTANT`. Copy the constant into a `DD` tag on a rung above and use the tag
- `run_for()` and `run_until()` take `jit=` in place of `fold=`, and `fold=` raises `TypeError`. `jit=False` runs every scan interpreted.
- `how()` no longer reports skipped scans, because its waits now run every scan:
  - plans no longer carry a "Jump ahead" line, and `PlanStep` loses `accelerators`. The step's "Wait ~Ns (N scans)" line stays
  - a wait's `CoastReceipt` reports `stretches` and `stretched_scans` in place of `macro_folds` and `skipped_scans`, and recordings log them as `coast_stretches` and `coast_stretched_scans`. `stretches` counts the compiled runs in the wait, and `stretched_scans` counts the scans in them
  - `advances` and `timer_quanta_replayed` are gone from `CoastReceipt`
- `Program` now raises `OwnedBitCoilError` (a `ValueError`) when it closes if a coil (`out()`, `latch()`, `reset()`, `toggle()` or another Out coil) writes a timer's or counter's Done bit, or a range that includes one such as `reset(C.select(1, 8))`. Clear the bit with the instruction's `.reset()` pin instead.
- Rungs evaluate as they do on a CLICK, in `PLC`, `CompiledPLC` and generated CircuitPython. Each instruction reads only the contacts on its own path from the rail:
  - the contacts at the start of the rung are read once, and everything below uses that result
  - each branch reads its own contacts when execution reaches it, so it sees what the instructions above it wrote
  - a `.continued()` rung reads its contacts when execution reaches it, like a separate rung
  - a box reads its enable and its pins (`.reset()`, `.down()`, `.clock()`, `.jump()`, `.jog()`, drum events) once, when execution reaches it, before it writes anything

  A branch, continued rung or box pin now sees the new value of a tag written earlier in the same rung (it saw the value from rung entry). `pyrung check` flags these rungs as `RUNG_ORDER_DEPENDENT`.
- CLICK import brings each wire of a CLICK rung back as a rung of its own, with `.continued()` on every wire after the first. Outputs share one `with rung(...)` or `with branch(...)` only where the CLICK rows branch them off one contact (it put them under one `with rung(...)`, which read the contact before an output above wrote it). The exporter no longer puts outputs under one shared contact when an output between them writes that contact's tag.
- A box with pins (a counter, a retentive timer's `.reset()`, a shift register, a drum) ends its rung, because CLICK won't compile it otherwise (error C000A). Building a program raises `RuntimeError` when another output or branch shares the box's rung, as in `with Rung(X1): out(Y1); on_delay(T1, ...).reset(X2)`. Move what comes before the box into a rung above it, and what follows into `with Rung(...).continued():`.
- The built-in `Timer` and `Counter` follow CLICK's bank defaults. A timer's `Acc` now resets on STOP->RUN (TD is non-retentive), and a counter's `Done` survives it (CT is retentive). To keep a timer's elapsed time across a restart, use `Timer.clone(..., retentive={"Acc": True})`. `clone(retentive=...)` overrides either field.
- CLICK import no longer makes up timer and counter nicknames. A T/TD (or CT/CTD) address with no nickname keeps its address as its name (it became `T1_Done`/`TD1_Acc`). A tag named after its own address exports with no nickname, so importing and exporting a project leaves its nickname file unchanged.
- Timer, counter and Out-coil presets (`on_delay`, `off_delay`, `count_up`, `count_down`, `debounce`, `flasher`, `timed`) must be an int or an `Int`, `Dint` or `Word` tag. Anything else raises `TypeError`. Copy a `Real` into an `Int` first.
- A `with Rung(...)` written inside `forloop()` is now a rung of the loop body, evaluated on every iteration (it became a separate rung before the loop).
- `forloop()` behaves like CLICK's For:
  - when the loop's rung is false, the body is skipped (it ran once with power off)
  - `out()` coils in a skipped body keep their values. To drop one when the loop stops, `reset()` it from a separate rung
  - timers and counters in a skipped body don't time, count or reset
  - one-shots in a skipped body don't re-arm until the loop runs with their rung false
  - a count of 0 runs no iterations (it ran one). An `Int` count is unsigned 16-bit, so -1 runs 65535 times. A `Dint` count is signed, so -1 runs none
  - an instruction in the body with a pin of its own (`count_up(...).reset(R)`) reads that pin after the body instructions above it run, so it sees what they wrote. `PLC` now matches compiled runs and generated CircuitPython here

  `always()`, `never()`, `reachable_states()` and `pyrung lock` answer `Intractable` when a loop can run more than 1,000,000 iterations in a scan, or when its count comes from an input without `choices=` or a `min=`/`max=` range of 1000 values or fewer.
- Counters count rising edges, as on a CLICK. `count_up()`, `.down()` and `count_down()` count once each time their input turns on, however long it stays on (they counted every scan it was on):
  - an input already on at the first scan counts
  - an input held through a reset does not count again
  - an up and a down edge on the same scan cancel
  - a down counter is done at `Acc <= preset` (it was `Acc <= -preset`), so with a positive preset it is done from its first scan
  - `how()` drives a counter to its preset one rising edge at a time. When the counter's input is a timer's `Done` that is already on, the plan stops instead of waiting

  A `rise()` on a counter's rung still works but is no longer needed. To count scans while a condition holds, use `math(N + 1, N)`.
- `rise()` and `fall()` work like CLICK's edge contacts. Each contact keeps its own copy of the bit from the last scan that evaluated it, powered or not (every edge on a tag compared with the tag's value at the end of the previous scan). The copy starts off at RUN and at STOP->RUN. So:
  - a bit set and cleared within every scan rises once, not every scan
  - a `rise()` in a subroutine called twice in a scan fires on one call only
  - an edge in a `forloop()` body compares with the previous iteration
  - an edge contact behind an open contact still sees the bit change, so it doesn't fire later when that contact closes
  - an edge in `run_until()` or `when()` starts from the bit's value when the condition is created

  `SystemState.memory` holds one `_edge:e{N}` entry per contact in place of `_prev:<tag>`. A saved state with `_prev:` entries still loads.
- Text and search follow CLICK:
  - a TXT holds a whole byte, so `copy(..., convert=to_binary)` of 200 stores 200, and `to_ascii` reads codes above 127
  - `pack_text()` into an `Int` or `Dint` clamps an out-of-range value and sets `fault.out_of_range`
  - `pack_text()` stops at a decimal point (`"12.5"` is 12), and rejects any other non-digit
  - `to_value` of a blank TXT writes 0
  - a continuous `search()` resumes after its last result if that result is inside the range. A result of 0 starts again from the first address, and any other value finds nothing and writes -1 (it resumed at the first address past the result, wherever the result was)
- Drums (`event_drum`, `time_drum`, `time_event_drum`) run CLICK's scan routine:
  - each scan takes one action, the first that applies of reset (a level), a jump edge, a jog edge, and the drum's own time or event
  - jump and jog act even with the rung false
  - a jog on the last step completes the drum at once
  - a jump to a step the drum does not have does nothing, and still uses up the scan
  - outputs are written only when the step changes or the rung turns back on, so a write from another rung stays until then
  - on completion the outputs turn off until a jog
  - on the scan the rung turns back on, a reset ends or the program starts, the drum only writes the step's outputs
  - after a jump or jog, the new step starts on the next scan
  - when the drum corrects a bad step, it keeps the accumulator
  - while the completion flag is on, whether the drum set it or something else did, the drum ignores its time and events and its accumulator holds
- A `time_drum` or `time_event_drum` step reads a tag preset on its first timed scan, and keeps that value until the step changes. A change mid-step takes effect from the next step. A tag preset of 0 or less now never ends its step (it ended it at once). A constant 0 still ends it at once.
- `reboot()` with `battery_present` True keeps only the retentive tags and resets the rest, as STOP->RUN does (it kept every tag). With `battery_present` False it still resets every tag.
- Copy, Block copy and Fill store these values as a CLICK does, in `PLC`, `CompiledPLC` and generated CircuitPython:
  - a `Dint` or `Real` into a `Word` stores 65535 above 65535 (it wrapped), and 32768 below -32768, with no flag. From -1 to -32768 it still wraps, as any constant or `Int` does
  - a `Word` into an `Int` keeps its bits with no flag, so 65535 is -1 (it clamped to 32767)
  - `blockcopy(..., convert=to_value)` skips a bad character and writes the rest (it wrote none)
  - a `copy()` from past the end of a CLICK `DS`, `DH`, `DF` or `TXT` leaves its destination unchanged (it wrote 0)
  - a plain `copy()` of a `Real` into a TXT writes its digits, like `to_text()`
  - a plain `copy()` of a TXT into a number parses it, like `to_value`
- A `Word` copied with `to_text()` and zero suppression is now hex (0x00AB is "AB"), as it already was without suppression. It was decimal.
- Comparing a TXT with a literal longer than one character (`Txt[1] == "AB"`) now compares the following TXTs too, as on a CLICK, in `PLC`, `CompiledPLC` and generated CircuitPython. It compared one TXT with the whole literal, so it was never true. The following TXTs are:
  - the next TXT addresses, for a raw `txt[n]` or a tag mapped to a TXT before the compare is built (including a `map_to` inside `with Program` above the rung)
  - the next elements of its block or `named_array`, for a tag not yet mapped. If those don't map to the next TXT addresses, CLICK export refuses the compare (`CLK_TEXT_WINDOW_NOT_CONTIGUOUS`)

  Imported CLICK code builds its `TagMap` before `with Program`, so these compares simulate and export as in the original CLICK project.
- The twin test harness is now the conformance suite. Import it from `pyrung.conformance` instead of `pyrung.twin`.
- `SystemState` is no longer a pyrsistent `PRecord` or `PMap`:
  - `isinstance` against either is False, and `thaw(state)` and `freeze(state)` return the state
  - a subclass can't add a `field()`
  - the map methods `remove`, `discard`, `transform`, `evolver`, `update_with`, `serialize`, `copy`, `iterkeys`, `itervalues`, `iteritems`, `+` and `|` are gone

  Its fields, `set()`, `update()`, `with_tags()`, reading it as a `Mapping` (`state["tags"]`, `dict(state)`), equality, hashing and pickles (old ones load) work as before.
- Generated CLICK projects keep pyrung's guidance in files of its own:
  - `AGENTS.pyrung.md` and `README.pyrung.md` hold the guidance, and are rewritten on every generation
  - `AGENTS.md` and `README.md` are yours, with a `<!-- pyrung:guidance -->` line pointing at the pyrung files
  - `refresh_workspace_lifecycle_guidance()` and the lifecycle markers are removed
- A `Real` holds CLICK's float32 range, -3.4028235E+38..3.4028235E+38:
  - a literal, default or constant outside it (or Inf or NaN) raises `ValueError`, and so does a patch or force of a finite value outside it
  - a `math()` or copy result outside it stores +/-Inf, and `math()` also sets `fault.out_of_range` (SC43)
  - `math()` of +/-Inf into an `Int` or `Dint` gives 0 with SC43, and `copy()` clamps it to the limit
- `generate_circuitpy()` and `write_circuitpy()` lose `mapped_tag_scope`. Drop the argument. The generated Modbus server now serves:
  - every register the TagMap maps, whether a rung uses it or not (what `"all_mapped"` did)
  - every raw CLICK register a rung uses (`ds[3901]`), which it refused with an illegal-address exception
- Harness bool feedback runs as the CLICK coil it behaves like, so a feedback edge can come a scan or more earlier or later:
  - `Physical(on_delay=, off_delay=)` runs as a `debounce` on its link:
    - a command dip shorter than `off_delay` keeps `Fb` on (it dropped `Fb`)
    - after the command drops, `Fb` turns off one scan later than it did
  - `Physical(flasher_on=, flasher_off=)` runs as a `flasher`:
    - its `Fb` goes high the scan after its enable turns on (it waited out the off time first)
    - its first high phase is one scan short
    - a `flasher_on` or `flasher_off` of 0 keeps `Fb` low
  - both delays of one `Physical` count in the same unit, and so do `flasher_on` and `flasher_off`
  - relinking a feedback that is on to a longer `on_delay` keeps it on (it turned off and counted the new delay)

  Update tests that count scans to a feedback edge.
- `system.sys.scan_clock_toggle` (SC3) is on when the scan counter (SD9) is even, starting on the first scan. It was a scan behind, off on the first scan. `rise()` of it now fires on scan 0 and `fall()` on scan 1.
- The verifier treats timers as black boxes. `always()`, `never()` and `reachable_states()` let an enabled on-delay, a disabled off-delay, a drum step or an Out coil's phase finish on any scan. So a `Proven` holds for any scan time and any preset. Every `Counterexample` is replayed on a `PLC` before it is reported, except one that only needs the clocks, the scan counter or the RTC to change, which carries a caveat instead. What changes:
  - a property that holds only because one timer finishes before another, or before enough pulses arrive, now comes back `Intractable` with `kind="timing"`. Check that timing with a `run_for()` test
  - `settled=True` checks only states where no timer is still timing
  - `depth_budget` is a plain search depth, so a count that needs more scans than that is not reached
  - a tag compared with an accumulator as a threshold (an HMI setpoint, a tag nothing writes) needs `choices=` or `min=`/`max=`

  Regenerate lock files with `pyrung lock`.
- `always()` and `never()` answer `Proven` only when the search covered every value and scan that matters:
  - an input the search can't try every value of now gives `Intractable`, with the input in `result.tags`. That happens when the input is used for more than contacts and comparisons with a literal, such as an index, a comparison with a computed tag, or a `math()` the search can't work backwards through. Give the input `choices=` or an integer `min=`/`max=` of at most 1000 values, or test it with `run_for()`
  - a search that `depth_budget` cuts short gives `Intractable` with no tags. Raise `depth_budget=`
  - a tag the property reads stays in the search unless an `out()` writes it every scan
  - a `Real` past 2**53 is tried on both sides of each literal it is compared with
- A block pointer must be an `Int` or `Dint` tag. `DS[Ptr]` with a `Real`, `Word`, `Bool` or `Char` `Ptr` now raises `TypeError` when the program is built. So does an index expression that could be a float (`DS[W + 1]` with a `Word` `W`, `DS[R + 1]`, `DS[I / 2]`), and a `select()` bound that breaks the same rule. Copy a `Real` or `Word` pointer into an `Int` or `Dint` first, and use `//` for division in an index.
- An expression outside `math()` (a rung condition, a `copy()` source, a `forloop()` count or an index) never stops the scan. An operation in it with a math error gives 0 and sets no fault flag, in `PLC`, `CompiledPLC` and generated CircuitPython. Inside `math()` the flags are still set.
- A pointer or index outside its block no longer stops the scan with `IndexError`, everywhere:
  - in a rung condition, it reads its type's zero with no flag
  - past the end, in a `copy()`, `math()`, `fill()`, `search()` or `forloop()` count, a read gives zero with no flag and a write is dropped
  - below the start, in those same instructions, it sets `fault.address_error` and writes nothing (a loop runs no iteration)
- `simplified()` and `pyrung live simplified` follow the new rung evaluation, so what they show changes for some programs:
  - when one rung has several `out()` writes of a tag, the last one decides it. `out(Y)` under branch `A` and then under branch `B` gives `Y = B` (it gave `Or(A, B)`). For an OR, write the contacts as `Or(A, B)`
  - contacts a branch shares with the start of the rung are read where the rung starts
- Fault flags report the most recent instruction, as on a CLICK, instead of staying on to the end of the scan:
  - each `math()` clears `fault.division_error` (SC40), then sets the flag if it divides by 0
  - each `math()`, `copy()`, `blockcopy()`, `fill()`, `pack_*()` and `unpack_*()` clears `fault.out_of_range` (SC43) and `fault.address_error` (SC44), then sets whichever one it trips
  - a `copy()` clears them before reading its operands, so `copy(fault.address_error, Bit)` reads off
  - all of them clear at the start of each scan
- A `copy()` that writes text across several elements (`to_text()`, a `Real` or a string literal into a TXT, `to_value`, `to_ascii`) fills what fits in its block, and drops the rest with no flag. Into a tag mapped with a TagMap or `Tag.map_to`, it keeps writing from the mapped address on. CLICK validation warns:
  - when the text always runs past the end of its bank (`CLK_TEXT_PAST_BANK`)
  - when the text runs past the end of a block mapped inside the bank, since a CLICK keeps writing past it (`CLK_TEXT_PAST_BLOCK`)
- `Physical` takes one flat keyword per value, and each keyword is also its nickname-comment key:
  - every argument after `name` is keyword-only, so `Physical("Fb", "2s")` raises `TypeError`
  - `Pulse`, `Ramp` and `Approach` are removed. Write `Physical(name, flasher_on="8ms", flasher_off="8ms")` for a `Pulse`, `Physical(name, ramp_on=0.8, ramp_off=-0.05)` for a `Ramp` and `Physical(name, approach_to=180, approach_rate=0.3)` for an `Approach`.
  - `Physical(profile=...)` raises `TypeError`. Give the keywords of one kind instead.
  - `ramp_off` left out is 0, which holds the value when the enable drops
  - a time given as a bare number raises. Write it with units, such as `"500ms"`
  - an `approach_to` string that looks like a number raises. Pass the number
  - keywords of two kinds in one `Physical` raise
  - comments read `flasher_on=8ms, flasher_off=8ms`, `ramp_on=0.8, ramp_off=-0.05` and `approach_to=180, approach_rate=0.3`. A `profile=` comment raises
  - the DAP `link` command takes the same keys. The `pyrungLink` request takes them in camelCase (`flasherOn`, `rampOn`, `approachTo`)
  - `pyrungListLinks` rows carry `kind`, `keys` and a field per key in place of `profile`
  - `TagMeta` has a field per key in place of `profile`. `parse_profile_spec()` and `profile_to_token()` are removed
  - `harness status` shows each flasher, timed, toggle, ramp or approach link with its kind and keys (it showed `profile=`)

  Run "Upgrade Project" in clicknick, or pass each comment to `pyrung.click.tag_map.upgrade_comment()`, to rewrite old comments.

### Features

- CLICK validation warns with `CLK_PTR_PAST_BANK` when a pointer can address memory past the end of its bank. Keep the pointer inside the bank, or give the tag `choices`, or `min` and `max`. A `max` alone counts as an upper bound.
- `pyrung.core` exports the table of physical kinds, the keys each `Physical(...)` kind takes. The names are `PHYSICAL_KINDS`, `PHYSICAL_KEYS`, `PhysicalKind`, `PhysicalKey`, `FeedbackType`, `kind_of_keys()` and `physical_kind()`.
- A comment in an old form raises `pyrung.click.tag_map.OldCommentFormError`:
  - it is a `ValueError`, so existing `except ValueError` clauses still catch it
  - its `code` is `"OLD_COMMENT_FORM"`
  - a nickname-file load keeps the class and names the row in the message

  Match on the class or the code to offer "Upgrade Project".
- `pyrung.click.tag_map` gains:
  - `comment_needs_upgrade()`, which is true when a comment holds an old token
  - `old_form_tokens()`, which lists those tokens
  - `pending_migrations()`, the rewrites "Upgrade Project" would make, in the order they run
- Harness feedback has two new kinds, which run as the CLICK coils of the same names:
  - `Physical(name, timed="200ms")` turns the feedback on for that time at each rise of its enable. With `maintained=True` it also stays on while the enable stays on
  - `Physical(name, toggle=True)` flips the feedback at each rise of its enable
  - a comment writes them as `timed=200ms`, a bare `maintained` and a bare `toggle`
  - `how()` plans through both
- `pyrung check` advises against `==` and `!=` on a `Real` the ladder computes with `math()` (`CMP_EQ_ON_COMPUTED_REAL`). Rounding can step over the exact value, so compare with a range or a tolerance. Analog inputs, setpoints, `== 0` and ordered comparisons are not flagged.
- CLICK validation refuses what CLICK's editor refuses, so ladder export stops where CLICK would. New refusals:
  - a `forloop()` count that isn't a constant or a `DS`, `DD`, `TD`, `CTD` or `SD` (`CLK_FORLOOP_COUNT_BANK`)
  - a `randint()` bound that is not an integer constant or an `Int` or `Dint` tag (`CLK_RAND_BOUND_TYPE`), and two equal constant bounds (`CLK_RAND_EQUAL_BOUNDS`)
  - an expression as a `randint()` bound or as an argument of `lsh`, `rsh`, `lro`, `rro`, `<<` or `>>` (`CLK_MATH_ARG_NOT_PLAIN`)
  - a `SUM` over a bank CLICK can't sum (`CLK_MATH_SUM_BANK_NOT_AVAILABLE`)
  - a bit in a `math()` formula (`math(C1 + 5, DS1)`, `CLK_MATH_BIT_OPERAND`). A `.sum()` over a bit range is still allowed
  - a `math()` result outside `DS`, `DD`, `DF`, `TD`, `CTD` or `DH` (`CLK_MATH_DEST_BANK`). Store into a register and drive the bit from a compare
  - a constant copied or filled into bits (`copy(2, C1)`, `CLK_COPY_CONSTANT_INTO_BIT`). Set bits with `out()`, `latch()` or `reset()`
  - a `search()` over a range outside `DS`, `DD`, `DH`, `DF`, `TD`, `CTD` or `TXT` (`CLK_SEARCH_RANGE_BANK`), or with its result outside `DS` (`CLK_SEARCH_RESULT_NOT_DS`)
  - a constant preset of 0 or less on a `flasher` or `timed` coil, or below 0 on a `debounce` or a drum step (`CLK_TIMER_PRESET_TOO_LOW`). A tag preset can hold any value
  - a timer's or counter's Done and Acc mapped to different numbers, such as `T5` with `TD7` (`CLK_TIMER_PAIR_MISMATCH`)
  - a nickname longer than 24 characters (`CLK_NICKNAME_TOO_LONG`)
  - an `immediate(...)` contact on anything but an X input (`CLK_IMMEDIATE_CONTACT_MUST_BE_X`)
  - a tag in a bank that can't play its role (`CLK_BANK_WRONG_ROLE`):
    - `out()`, `latch()` or `reset()` on anything but bit memory (`reset(DS1)`)
    - a `send()` or `receive()` status flag outside `C`
    - an `exception_response` outside `DS` or `DD`
    - a `receive()` `dest` in a bank that Receive can't write
  - a `call()` inside a subroutine, since CLICK has no nested calls (`CLK_NESTED_CALL`). It is an error in strict mode and a hint otherwise

  To let a refusal through, pass its code to `pyrung_to_ladder(allow=...)`.
- `pyrung check` warns with `RUNG_ORDER_DEPENDENT` (on by default) when a branch, a `.continued()` rung or a box pin reads a tag that an instruction above it in the same rung wrote. `pyrung.core.validation.reads_after_write()` finds these reads.
- The debugger's Memory pane names the contact or instruction each memory value belongs to (`_edge:e3` reads `True (rise(Start) line 12)`), and lists keys in number order.
- `pyrung.conformance` runs one set of test cases in-process, over Modbus against pyrung's soft PLC, on a real CLICK, or against generated P1AM-200 code:
  - `run_modbus()` runs cases over Modbus TCP (a real CLICK at `host[:port]`), and `build_program()` compiles the suite into a CLICK program with one subroutine per `Group`
  - each case resets itself while `Cmd = 0`, and gets a comment generated from its description
  - `run()` runs every case twice, and fails a case whose state after the reset differs from a fresh PLC (`keep=` exempts tags)
  - `case(expect_system={"SC11": 0})` checks system bits the ladder cannot record
  - `case(probe=...)` names the open hardware question a case asks
  - `Group(calls=2)` calls the group's subroutine twice a scan
  - the soft PLC (`SimServer`) advances its timers by at most 40 ms a scan (`max_scan_dt=`), so a slow host no longer fails timing cases
  - the slot layout changed (`LAYOUT_VERSION` 3), so export and load the whole project again
- The debugger can act as a simulated CLICK PLC:
  - a `modbus` launch argument serves the session over Modbus TCP, with X inputs read-only as on a real CLICK
  - `speed` paces Continue at that many times real time (0, the default, runs unpaced)
  - `startMode`, the `pyrungSetMode`, `pyrungSetSpeed` and `pyrungModbusInfo` requests, and the `mode` and `speed` console verbs control RUN/STOP and pacing
- Force, unforce and patch accept CLICK addresses such as `X001` or `DS10` in DAP requests and console commands. They reject a target that is neither a tag nor a mapped address, instead of creating an unused tag.
- A program with no rungs sends scan frames while it runs, so clients see its scan count advance.
- `pyrungScanFrame` is now sent after every state change, including `pyrung live` commands and Modbus writes. It reports the mode, whether the session is running, the set and measured speed, pending patches, and what caused the frame. `pyrung live` commands also send the debugger their stop and trace events.
- A `step` or `run` sent while Continue runs pauses it and then executes, instead of failing.
- `PLC.start()` enters RUN without executing a scan, `PLC.mode` reads `"run"` or `"stop"`, and `PLC.pending_patches` shows patches not yet committed. `CompiledPLC` has the same API.
- `PLC(keep_forces_on_run=True)`, the `keep_forces_on_run` property, or the debugger's `keepForcesOnRun` launch argument keeps forces across STOP->RUN. By default they are still cleared.
- `plc.debug.abandon_scan()` puts back the patches a partly stepped scan had taken. The debugger uses it, so a patch sent while paused mid-scan is never lost.
- `ClickDataProvider` accepts a runner getter, Click bank `blocks` for slots without a TagMap entry, and an `on_stop` hook, and can report the tag served at an address.
- The `link`, `unlink`, `restore` and `links` console verbs, and the `pyrungLink`, `pyrungUnlink`, `pyrungRestoreLink`, `pyrungSetLinks` and `pyrungListLinks` requests, add, replace or cut feedback links for the debug session without editing the program:
  - these session links survive `reload`
  - they can be set aside together (`links bench off`) or one at a time
  - session recordings capture them
  - they are refused when the program writes the feedback
- `Harness` gains:
  - `set_overrides()`, which replaces or cuts declared couplings, or adds new ones, by feedback tag
  - `declared()`, which lists the program's own couplings
  - `overrides`, the overrides in force, by feedback tag
  - `pending()`, which reports a bool feedback still counting toward a flip
  - `pending_ms()`, the milliseconds left before each bool feedback still counting flips

  `Harness.unlink()` is now an override that cuts the coupling. The module function `pyrung.core.harness.link_problem(plc, coupling)` explains why a coupling can't run.
- `link=` takes more forms:
  - `link="~Tag"` drives feedback while a Bool enable is off (normally closed), in Python, UDT fields, nickname comments and the `link` console verb. `~` on a non-Bool enable is refused
  - in a CLICK nickname comment, `link=` takes a CLICK address (`link=Y001`, read as the nickname at that address) and a value trigger (`link=State:SORTING`)
- `PHYS_LINK_UNKNOWN` warns when a `link=` names an enable the program never uses (the harness skipped it silently).
- `rng.max()`, `rng.min()`, `rng.median()` and `rng.mean()` on block ranges work inside `math()`, like `rng.sum()`:
  - they export to CLICK as `MAX`, `MIN`, `MDN` and `AVG`, and import back
  - they run in `PLC`, `CompiledPLC` and generated CircuitPython
  - `MDN` of an even count of integers is the mean of the middle two, truncated toward zero
  - CLICK validation checks range order, bank, bit ranges and `MDN` size (`CLK_MATH_RANGE_NOT_CONTIGUOUS`, `CLK_MATH_RANGE_MIXED_BANKS`, `CLK_MATH_BIT_RANGE_AGGREGATE`, `CLK_MATH_MEDIAN_RANGE_TOO_LARGE`)
- `toggle()`, `debounce()`, `flasher()` and `timed()` are CLICK v4's Out coil types. Each owns its `(timer, preset)` phases, and `debounce()` needs both sides, as in CLICK:
  - they run in `PLC`, `CompiledPLC` and generated CircuitPython, and `how()` can drive them
  - they export to CLICK and import back, and CLICK validation checks their timer banks and units
  - their timers' `Done` bits never turn on
  - the verifier models `toggle` exactly and the timed ones as black-box timers
- `time_event_drum()` is CLICK v4's Time & Event drum. A step ends when its time is up and its event is on, and either half may be `None`:
  - it runs in `PLC`, `CompiledPLC` and generated CircuitPython, and `how()` can drive it
  - it exports to CLICK and imports back
  - the verifier treats its timed steps as black-box timers
- `forloop()` bodies take more of CLICK v4's For/Next:
  - rungs with contacts inside the loop (`with Rung(...)`), evaluated on every iteration
  - `break_loop()` and `continue_loop()` (CLICK's Break and Continue), each in a body rung with a contact
  - nested loops up to three deep, each with its own `loop.idx`

  They run in `PLC`, `CompiledPLC`, generated CircuitPython and the debugger, and export to CLICK and import back. CLICK validation checks loop depth across `call()` (`CLK_FORLOOP_NESTING_DEPTH`), one-shots in an inner loop (`CLK_FORLOOP_INNER_ONESHOT`) and `return_early()` inside a loop (`CLK_FORLOOP_RETURN_INSIDE`). The verifier answers `Intractable` for a program with a rung inside a loop.
- `randint(lo, hi)` (from `pyrung.core`) is CLICK's `RAND`, a new random value from `lo` to `hi` inclusive each scan:
  - a scan draws the same value every time it runs, so replay, seek and `fork()` see the same values. A test can set it by patching or forcing its tag (`expr.tag`, or `expr.cut_tag` for a value cut into an `Int`)
  - it runs in `PLC`, `CompiledPLC` and generated CircuitPython (as `random.randint`), and exports to CLICK as `RAND ( lo , hi )` and imports back
  - a long `run_for()` or `run_until()` of a program that uses it runs its scans compiled, without the faster path for repeated scans, and the verifier explores every value in range
  - it is refused inside a `forloop()`
  - two equal bounds are a math error (0 with SC46), whether constants or tags holding one value
  - into an `Int`, each constant bound is cut to 16 bits (`randint(70000, 70002)` draws 4464..4466)
  - a bare `Real` tag as a bound reads as 0 with no flag
- A `time_drum` preset may be a register (CLICK v4), and exports to CLICK. `CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED` is removed.
- `pyrung.click.estimate_steps()` estimates an exported ladder's CLICK program steps, within about 1% on a 6,000-step project. Validation reports `CLK_STEP_LIMIT` as a program nears or passes CLICK's 8000, and `pyrung_to_ladder` warns with it but still exports. The new CLICK Internals page lists the costs.
- `run_for()` and `run_until()` no longer skip scans. After the first 1,000 scans of a run, every scan runs compiled, and scans that repeat the one before run faster still. An hour of a plain timer takes about half a second. A run steps every scan interpreted when:
  - it has a callable stop predicate, a monitor, a breakpoint or a `when()`
  - the PLC runs in realtime mode
  - a `Harness` has a bool feedback (each one watches its tag every scan)
  - the program has a `send()` or `receive()` to a `ModbusTcpTarget` or `ModbusRtuTarget`
  - the program can't be compiled
- History answers questions about the scans of a compiled `run_for()` or `run_until()` as if they had been stepped, by stepping them again from the nearest checkpoint:
  - `cause()`, `effect()`, `rung_firings()`, `history.previous_transition()`, `cold_rungs()` and `hot_rungs()` (`cause()` raised `ValueError` on any scan the run skipped)
  - `pyrung_coverage.collect(plc)` reads the whole run, however long it was
  - the VS Code History panel shows the same scans, one-scan pulses included, and **Load older** reads further back

  A question that would step more than 250,000 scans raises `UnrecordedScansError` naming the range, instead of running for minutes.
- `how()` plans for a program with `send()` or `receive()` without talking to a Modbus device (it sent real requests):
  - it sets a `receive()` destination, or the success or error bit of a `send()` or `receive()`, the way it sets an input
  - each step that sets one says "This value stands for the Modbus device's answer."
  - the other status bits keep their values
  - the plan's last line says that `how()` does not simulate Modbus
  - the forks `how()` returns, such as `plan.fork`, don't talk to the device either
  - `step()`, `run_for()` and the debugger still talk to the device
- Without `history=`, `rung_firings()` and `cause()` step again for a tag's later writes once one rung's writes to it have changed 1,000 times (they kept every write).
- Without `history=`, a stepped run stops recording rung firings once the records reach about 140 MB, as `how()` already did for the scans it runs (a stepped run's records had no limit):
  - `cause()`, `effect()` and `rung_firings()` step a later scan again from the nearest checkpoint
  - a question that would step more than 250,000 scans raises `UnrecordedScansError`
  - pass `history=` to keep every record in the window
- With `history=`, the runner keeps every write in the window. A value that changes on every scan takes about 250 bytes a scan, so `history="1h"` at 10 ms holds about 90 MB for each such tag. `history_budget` does not count these writes. Shorten `history=` for a program with many such tags.
- A compiled `run_for()` keeps the same scans as stepping when the runner is over `history_budget`. The runner weighs only the window's checkpoints against the budget (it also weighed the recent-state cache, the scan log and the recorded writes).
- `how()` has a time limit, `wall_clock_s=300.0` by default (`None` turns it off). When it runs out, the plan reads `Stopped before reaching ...` with `Reason: wall-clock budget exhausted (300 s).` and a `Waiting for:` line. A search that holds about 1 GB stops with `memory budget exhausted (about 1 GB held)`.
- `how(max_scans=)` charges a scan the same whether pyrung runs it compiled or not:
  - each scan `how()` steps counts one against `max_scans`, and so do the first two scans and the last scan of each wait
  - the other scans of a wait count a hundredth each, so a long timer fits a small budget
  - a wait pyrung cannot run compiled counts one for every scan. That happens when:
    - the program writes a read-only system tag
    - the program has a `run_function()` that keeps state between calls (a callable object, a closure, a global or a mutable default)
    - a tag is forced
- A reached `how()` plan uses temporary logic only where the plan needs it, so `Rung(En): on_delay(W, 3000)` plans "Set En=True, wait ~3s" (it latched `En` with temporary logic until `W.Done`):
  - `how()` reruns the plan with each piece of temporary logic left out, then with a Set of its value instead
  - it keeps the simpler plan only when that plan reaches the goal on the same scan and meets nothing in `avoid=`
  - logic that holds an input False is tried as a Set first, so a command the plan lets go of stays let go of
  - a Set isn't tried when another Set the plan prints comes less than a second before or after it. Sets on one scan count as one. A release whose Set comes that soon may be left out, so its command stays pressed
  - the plan's text and `plan.replay()` describe the same run
- A `how()` plan prints its temporary logic as the rows that ran, in pyrung source:
  - a tag the logic sets under one condition gets one row, an `out()` for a Bool (it printed a `latch()` or `reset()` per rule)
  - a condition that reads a tag the logic writes earlier goes on a bit instead
  - a tag the logic sets under two or more conditions gets one row when its conditions merge into contacts in series with no `Or` and they read no other tag the logic writes, such as `with rung(State == 6, ~RotateSensor): out(RotateSensor)`
  - otherwise the tag gets one bit per condition, such as `__on1__HoldSensor`, `__off1__HoldSensor` or `__set1__Speed`, and the bit rows run first
  - a non-Bool tag gets one `copy()` per bit, in order
  - the bits stay in `plan.fork`'s tags after the logic is removed
  - conditions leave out terms that can't change them
  - a step that rebuilds a tag's logic says which step it replaces
- When `how()` would stop, it tries what a technician would try next, each on a fork. A plan that needs one of these says so in a step of its own, before the plan's other steps:
  - releasing the session's forces ("Release the force on X.")
  - STOP->RUN ("Stop the program and go back to RUN."). When a restart alone falls short, the stopped plan names the retentive tags that keep it from the goal, and says which to write while stopped
  - one wait or one input change that nothing in the trace pointed to ("Nothing in the trace pointed to this.")
  - `Plan.released`, `Plan.restart` and `Plan.last_resort` hold what it tried
- A `how()` plan runs with the session's forces and pending patches, and doesn't list them as its steps (it planned without them):
  - `plan.changes` and `plan.ordered_steps` leave them out
  - a patch or force that no scan has applied yet gets a first step, "Let one scan run, so the session's own patch or force on X takes effect."
  - `Plan.session_inputs` and `Plan.waiting_overrides` hold them
- `why()` shows the forces in its answer:
  - a forced root is marked `forced`
  - a forced tag gets a line saying the force restores it before outputs are written
  - a force no scan has applied yet reads `X is forced V from the next scan`
  - a rung that a force accounts for is no longer marked blocked
  - `CausalChain.forced` and `CausalChain.force_pending` hold them
  - the line for a chain with every writer blocked reads "no writer is firing now" (it read "no writer has fired")
- `check_lock()` raises `LockIntractable` when the verifier can't finish (it raised a plain `RuntimeError`). `LockIntractable` is a `RuntimeError`, and its `intractable` holds the answer.
- `Timer.clone()` and `Counter.clone()` take `comments={"Done": ..., "Acc": ...}`. CLICK import keeps each timer and counter address's retentive setting and comment on the clone (`retentive=`, `comments=`).

### Fixes

- `dataview()` now reflects a tag setting changed after the first call (it kept the old answer).
- `how()`, `cause()` and the other analyses answer from a slot setting or name changed by a later `slot()` call or TagMap (they answered from the old one).
- After the debugger's `reload`, breakpoints match each rung to its lines in the edited file (they used the lines from the first load).
- Rungs after a `with Program()` block nested in another one join the outer program (they joined no program).
- Two threads can build programs at the same time (a rung could join the other thread's program).
- `history.at()`, `replay_to()`, `cause()` and `effect()` no longer send Modbus requests (a replayed `send()` could write to the device again). A replayed scan writes the answer the device gave on that scan.
- `replay_to()` and `plan.replay()` return a fork whose history starts at the scan replayed to (on some programs the fork also kept the scans from the checkpoint before).
- `rung_firings()`, `cause()` and `cold_rungs()` on a fork from `replay_to()` or `plan.replay()` now see the scan replayed to (they said no rung fired on it).
- `replay_to()` of an earlier scan returns the state that `history.at()` and `fork()` give for it. A battery change or `clear_forces()` made after that scan now takes effect on the next scan (`replay_to()` applied it to the scan replayed to). `replay_to()` of the last scan still shows a battery change, `clear_forces()` or `stop()` made after it.
- A `fork()` keeps the run mode and battery of the scan it forks (it always started in RUN with the battery in).
- The fork that `replay_to()` returns for a PLC in STOP restarts on its next scan, as the PLC does (it scanned on in STOP).
- `how()` on a PLC in STOP says `Stopped` unless the goal already holds, because its next scan would restart the PLC (it planned as if the PLC were running).
- `history.at()` and `replay_to()` keep what a harness uninstall or a debugger reload changes between two scans (they lost it once that scan left the cache, unless it fell on a checkpoint).
- `replay_trace_at()` of a scan that holds a checkpoint returns the rung traces of that scan (it returned those of the next scan).
- `cold_rungs()` and `hot_rungs()` on a `fork()` now read the scans the fork took from its parent (`hot_rungs()` found no rung, and `cold_rungs()` listed a rung that fired only before the fork).
- A `fork()` of a PLC with a live `send()` or `receive()` sends its own requests (it could collect the answer to its parent's request). A fork made while a request is in flight never gets that request's answer.
- `how()` now reaches these targets, or says `Stopped`, where stepping reaches them (it said `Cannot reach`):
  - a rung that needs both a harness flasher feedback (`flasher_on`) and that feedback's enable
  - a target with a second route through a pointer or a for loop
  - a two-tag target where a one-shot overwrites one tag only once
  - a force or patch already on the PLC
  - an enable the program changes mid-scan
- `how()` returns a plan in these cases (it raised a Python error):
  - a comparison with arithmetic in a rung (`T.Acc + 1 >= N`), or an internal error in its search, gives a `Stopped` plan with a reason
  - a callable `avoid=` that is true at the start of a wait gives a `Stopped` plan naming the avoid (it raised `ValueError`)
  - some targets behind a harness link get an answer (it raised `TheoryInvariantError`)
  - a target compared with a system tag before the rung that writes it has run gives a plan (it raised `TypeError`)
  - a target compared with a tag that only a `patch()` or `force()` by name gave a value gives a plan (it raised `TypeError`)

  `avoid=Bank[Idx] >= 9000` now reads the slot `Idx` points to (it counted as violated everywhere), and ends the plan on any scan where the condition becomes true.
- `how()` reaches more comparison goals against a number:
  - a goal on a tag that a rung adds a constant to with `math()`, such as `how(Count > 3)` on `math(Count + 1, Count)` (it said `Stopped`)
  - a `!=` goal, such as `how(Count != 0)` (it raised `ValueError`)
- `how(And(A, B))` is the same as `how(A, B)`, and `how(~Running)` is the same as `how(Running == False)` (it raised `ValueError`). `how(Or(...))`, `how(T.Acc >= Sp + 1)`, and `how(T.Acc >= Tag)` where no rung uses `Tag`, raise a `ValueError` saying what to pass.
- Timers, Out coils and time drums count time exactly in every unit. At a fixed `dt`, a timer is Done on scan `ceil(delay / dt)` whatever unit it counts in (a `60s` timer at `dt=0.1` or a `1min` at `dt=0.01` ended a scan late).
- A Real range `.sum()` adds plain left to right everywhere, so the stored result no longer changes in the last bit with the Python version, or between stepping, compiled runs and generated code.
- A `run_function()` or `run_enabled_function()` that scans, stops or reboots the PLC running it, or sets its battery, now raises `RuntimeError` (the scan published a wrong state). Set a tag through `outs=` and act on it from outside the scan, or scan another PLC or a `PLC.fork()`.
- CLICK import now reads programs it refused, or turned into code that failed to run:
  - Copy's `to_text` options as CLICK writes them (`termination_code=none`, `$XX`)
  - `raw(...)` instructions (Email, Home and other unsupported ones)
  - the contacts of a rung between For and Next, and nested For/Next blocks
  - OR branches and padding rows between an instruction's pin rows
  - a drum's jump pin
  - hex, Real and text constants such as `FFC0h`, which it could mistake for addresses
- Comparing a Char tag or slot with another Char (`txt[a] == txt[b]`, `>`, `<=` and the rest) builds a condition (it raised `TypeError`). `<`, `<=`, `>` and `>=` on a Char are a `CLK_TEXT_ORDER_COMPARE` finding, and CLICK export refuses them unless `allow=` names the code. Compare with `==` or `!=`, or compare the character codes.
- A `Real` that holds NaN and stays NaN no longer changes on every scan:
  - `cause()`, `effect()` and `history.previous_transition()` answer for it (they returned `None`). It last changed on the scan it became NaN, even when `math()` computes the NaN again.
  - `plc.diff()`, `monitor()` callbacks and the debugger's tag changes no longer report a change for it (they reported one on every scan).
- Over Modbus and in `unpack_to_words()` or `unpack_to_bits()`, a `Real` holding any NaN is 0xFFFF:0xFFFF as on a CLICK (it was its own bits, such as 0x7FC0:0x0000). This holds for the soft PLC, for generated CircuitPython and for a `send()` to a `ModbusAddress`. A `send()` of a NaN or Inf to a CLICK address now works too (the server refused it).
- pyrung needs pyclickplc 0.5.1, so a `ClickServer` (and the soft PLC, `SimServer`) answers raw Modbus requests as a CLICK does:
  - a read past the end of a bank answers exception 02 (it returned 0)
  - a request outside the Modbus limits answers exception 03
  - a write the server refuses leaves memory unchanged
  - a `DF` write accepts +Inf, -Inf and NaN, and every NaN reads back as 0xFFFF:0xFFFF
  - a `Text` write accepts any character code from 0 to 255
  - an odd `XD` or `YD` register above `XD0u` or `YD0u` holds its own value (it read and wrote the register before it), and an `XD` one reads 0 and refuses a write
  - function codes 22 and 23 answer exception 01
- A `send()` or `receive()` to a CLICK address sends a range longer than the Modbus limits as several requests (it set the error flag).
- `how(avoid=...)` no longer treats an avoid check that raises as clear, which let it plan through the avoided state. When the check raises on the start state, or on a scan of a command `how()` tries, `how()` raises `AvoidCheckError` with the check's exception as its cause.
- A long `how()` wait keeps its memory bounded. It keeps at most two compiled kernels and 512 checkpoints, and records a long run's scans only when a later `cause()` or a survey such as `cold_rungs()` reaches them.
- A kernel that fails to compile for a reason pyrung doesn't expect gives a `RuntimeWarning`, and the scans run interpreted, instead of raising out of `run_for()` or `how()`.
- `how()` no longer plans straight through a stop a rung causes. It ends where the program stopped itself, as stepping does, and says it doesn't plan across the restart.
- `how()` reads the settings of a block slot that the rungs reach only through a pointer or a range, on every PLC of the program (a PLC made before any other `how()` of the program ignored them):
  - it never sets a `readonly=True` slot (it could set the slot)
  - it returns the slot to its `default=` after a pulse (it returned the slot to `False`)
  - it tries the slot at a value other than its `default=` (it could try only the default, and say `Stopped`)
  - its restart note lists a retentive slot that keeps its value (it left the slot out)
- `how()` and `never()` try only values a tag can hold:
  - `==` and `!=` with a literal the tag can't hold (`"AB"` on a Char, `"x"` on an Int) no longer raise, or report a state no scan reaches
  - a fractional literal compared with an `Int` no longer misses the whole numbers on either side (`In > 2.5, In < 3.5` is met at 3)

  When a search finds no scan, it concludes nothing unless the values it tried are the only ones that reach the comparison. An input whose value passes through a multiply, a fractional literal, a wider tag or an overflow wrap on its way to the comparison now gets a named `Intractable` (it could wrongly come back `Proven`).
- `never()` no longer wrongly proves that a `Char` order comparison can't be met, and `how()` no longer gives up on one. `C > "A"` is met at `"B"`.
- `history.at()`, `fork_from()`, `rtc_at()` and a seek replay a scan as it ran:
  - under the harness plant and the clock it ran under
  - after a STOP->RUN restart, from its own run, not the previous run's patches or cached states (a restart now starts a new recording)
  - with each tag at its default until a rung, patch or force first writes it
  - past a stop a rung caused, without restarting
  - over a harness ramp or approach feedback (`ramp_on` or `approach_to`), without skipping scans
- `pyrung lock --check` on a program the verifier can't finish names the `Intractable` and exits 1, as `pyrung lock` does, instead of ending in a traceback.
- Compiled runs and generated CircuitPython run a `run_function()` as stepping does:
  - a compiled run calls the function itself, not a copy of its source (a seek raised `NameError`, and a `global` store landed in the kernel's module)
  - its `ins` and `outs` go in the order they are declared
  - a list or dict in `ins` lasts from scan to scan
  - its body is embedded exactly

  `generate_circuitpy()` refuses with a `ValueError`:
  - a constant in `ins` that can't be written back as source (an object, `inf`, a set, a list that appears twice)
  - a callable whose source is not what it runs (a closure, a decorator, a wrapper, a file edited since the import)
  - a function named like a builtin, `math` or a name in the generated module (`time`, `board`). Rename the function
  - a function that reads the `math` module under a builtin's name or a name starting with `_`. Import it under another name

  A function runs interpreted, scan by scan, when it could carry state from one call to the next (a closure, a read of its module's globals, a `global` store, a mutable default, an attribute store). `never()`, `always()`, `reachable_states()` and `pyrung lock` answer a named `Intractable` (`kind="function"`) for such a function, or for one given a list, dict or object in `ins`. A plain `def` that uses only its inputs avoids this.
- A tag holds a value of its own type:
  - it reads back what was last written, converted to its type and keeping the sign of zero. So a coil on an `Int` reads `False` from its first scan, and `math(0.0 * -1.0, x)` stores `-0.0`
  - `patch()` and `force()` store what `copy()` would. So `patch({"I1": 0.0})` on an `Int` holds `0`, and `patch({"Run": 1})` on a `Bool` holds `True` (a float left in an `Int` raised `struct.error` on the next Modbus read)
  - a string on a numeric tag and a number on a `Char` are refused. The debug console answers `cannot patch: ...` or `cannot force: ...`
  - a `Real` given an int default (`Real("R", default=5)`) holds and shows `5.0` everywhere
  - an Inf or NaN stored on a `Bool` is `False`, not `0`
- A coupling's tags are in `plc.state.tags` from `Harness.install()` on, at their defaults. So `plc.diff(0, n)` shows a feedback's first value as `(0, x)`, and a reset keeps them.
- `always()`, `never()`, `reachable_states()` and `pyrung lock` no longer report `Proven` for states their search skipped. Each of these could hide a violation:
  - an input compared with a value that changes, such as a timer or counter `Acc`, a tag a `math()` steps, an expression (`In + 5 > 100`), an indexed element (`DS[Ptr] == In`) or a `search()`
  - a `Real` input between two limits less than 1 apart (`Level > 5, Level < 6`)
  - one source written to a target two ways, or read by a `math()` or `copy()` the search can't work backwards through
  - an element a pointer or index picks, including an unbounded write into a large block such as a CLICK `DS` bank
  - a subroutine whose call has a condition or sits in a `forloop()` body, and a `return_early()` that skips the rest of one
  - an `out()` coil that a scan can skip, or that a rung above its writer reads
  - one-shot, drum, shift and toggle memory the first scan doesn't reach
  - an input that changes on the scan a timer finishes, or that starts one timer while others run
  - a tag the program rewrites every scan, or never touches
  - a system tag or a fault flag
  - a `run_function()` output through a pointer or `immediate(...)`

  When the search can't cover an input, it now answers `Intractable` and names the input. Give the input `choices=` or an integer `min=`/`max=` (1000 values at most), and write a callable property as a condition.
- `always()`, `never()` and `reachable_states()` answer `Intractable` for:
  - a `split_at=` tag that holds none of its choices (it raised `ValueError`)
  - a property that reads something the verifier can't evaluate (it could answer `Proven`)
- A block slot that only a `select()` or a pointer reaches behaves like any other slot, in `PLC`, `CompiledPLC` and generated CircuitPython:
  - a tag on the slot and a pointer to it no longer split into two registers in `replay_to()`, a seek, `CompiledPLC` or generated CircuitPython (the pointer read the slot's power-on value)
  - it keeps a retentive value through `stop()`/`start()`, and any value through a battery `reboot()` (it read 0)
  - its `max=`, `choices=` and `physical=`/`link=` apply
  - an interpreted `rng.sum()` reads its declared default (it read 0)
  - a harness plant on it uses the slot and its default (a stand-in tag ran from 0)
  - generated CircuitPython starts it at its `default=`, and resets a non-retentive one to it at STOP->RUN
- A PLC answers the same whichever block slots another PLC or an analysis read before it was built:
  - a PLC built later knows and holds the same tags as one built first (it also knew each slot a pointer scan, `how()`, a coverage report or the prover had read)
  - `slot(n).reset()` and a range's `reset()` work on a slot a scan or an analysis has read (they raised `ValueError`)
  - a `default_factory` is asked once for each slot, lowest address first (it was asked in the order scans read the slots)
  - generated CircuitPython starts each slot at its factory answer (a factory that counted its calls left every slot at 0)
  - the debugger's `prove` and `how` read a slot that only a range reaches, with its `choices=` labels (`how` said `unknown tag`, and `prove` could say `Proven`)
- A `copy(..., convert=to_text())` of an Inf or NaN `Real` writes it as lowercase text (`inf`, `-inf`, `nan`) with no flag (it left the text unchanged and set `out_of_range`).
- A `copy()`, `blockcopy()` or `fill()` that clamps at an `Int` limit sets `out_of_range`. A `Real` into a `Dint` clamps with no flag.
- A `copy()` of an integer or a `fill()` of a number into a `Char` no longer stops the scan in compiled runs or generated CircuitPython, and a `fill()` into a `Char` no longer stops it in `PLC`. `convert=to_binary` of Inf sets `out_of_range` and leaves the text unchanged (`PLC` raised `OverflowError`).
- `Tag.map_to()` and `Block.slot(name=)` now raise `ValueError` when they would put a system point, such as `system.rtc.second`, in a block slot.
- An `approach_to` that names a tag the program uses no longer makes `Harness.install()` raise `TypeError`. A setpoint tag that no rung uses and nothing has written gets a warning at install that it starts from 0.
- CLICK export writes what CLICK's editor writes, so an exported CSV matches a copy-back from CLICK v4 and imports back:
  - hex constants as `Ah` and `FFh`, in hex-mode Math, a copy or fill into DH/XD/YD, a compare against a DH/XD/YD tag, and a search over DH. They went out in decimal, which CLICK reads as hex, so `DH1 + 10` added 16 on the PLC
  - `Real` constants as `3.0E+38` and `1.0E-10`
  - math formulas spaced as `SQRT ( DS1 )` and `LSH ( DH1 , 2 )`, and the importer reads both forms
  - `abs()` as `ABS ( ... )`, flagged in hex-mode Math, where CLICK has no `ABS`
  - a `.sum()` over a Bool range as `SUM ( C1 : C8 )`
  - a one-register range operand as `TXT12..TXT12`, which ClickNick's Guided Paste needs
  - a reversed shift range as CLICK's descending `C1018..C1011`
  - a plain contact right after an `Or` merge on the merge cell (`T:X003`), which saves a column

  A bare number in hex-mode Math imports as hex, as CLICK reads it. Export refuses a constant outside 0..65535 compared with a DH/XD/YD tag or searched for over DH, and a fractional constant wherever CLICK reads hex.
- The debugger no longer lists the tags pyrung adds itself (`__on{n}__`, `__off{n}__`, `__set{n}__`, `__cpl_...__`, `__pls_...__`) in tag values, the Variables Tags scope, scan-frame changes or `pyrungTagChanges`.
- In the debugger, the trace shown during Continue keeps each branch's own state when one helper builds two branches (they shared one). It also decorates nested branches at any depth.
- In compiled runs and generated CircuitPython, a box placed directly in a `forloop()` body (not in a body rung) reads its pins (`.reset()`, `.jump()` and so on) before it writes, as `PLC` does. A drum's jump pin no longer reads the step the drum has just corrected.
- A drum reads its jump step when the scan reaches it, before it moves, everywhere. If the drum itself writes the step tag, the jump goes to the value the tag held when the scan reached the drum (it took the value the drum had just written).
- Ladder export no longer puts a vertical token (`T`, `|`, `T:`) in column A, the left power rail, which CLICK refuses ("Vertical-down tokens are not allowed in Column A").
- CLICK ladder export writes a `fill()` of one register as a Copy (it wrote a Fill, which CLICK refuses for one register). It refuses a text or REAL value that the Copy would convert, and a text of several characters. Write `copy()` for those.
- Analysis now sees inside `forloop()` bodies:
  - a `return_early()` in a loop makes the subroutine's later rungs conditional on it, for `cause()`, `how()` and the verifier
  - the stuck-bit and conflicting-output validators, and the call conditions `simplified()` shows, see calls in a body. A body rung's contacts count as part of the call's condition where no iteration can change them
- A `call()` in a `forloop()` body no longer raises "randint() is not supported inside a forloop body" because of a `randint()` somewhere else in the program. The subroutines the loop calls, and those they call, are still checked, and a `randint()` in one is still refused.
- Comparing a block element indexed by an expression with a tag (`Rung(Tbl[Q + 1] == S)`) is now true when the values are equal (`PLC` found them unequal). Comparing such an element with a number or an expression (`Tbl[Q + 1] == 370`, `S + 1 < Tbl[Q + 1]`) now builds a condition (it built a plain `bool` or raised). CLICK ladder export refuses these comparisons as `CLK_PTR_EXPR_NOT_ALLOWED`.
- A CLICK subroutine whose last rung is a conditional `return_early()` now exports with the unconditional `return()` CLICK requires after it (it exported without one, and CLICK refused the subroutine).
- Debugger hot reload moves one-shot, toggle, shift and drum memory to the matching instruction in the reloaded program, so inserting or deleting a rung no longer gives one rung's memory to another. Values with no clear match reset, and the reload summary lists them. Editing what an instruction reads (a preset, a copy's source) keeps its memory, so a held input doesn't fire it again. Editing its target or contacts resets the memory. A new or edited `rise()` or `fall()` contact starts from its tag's current value, so a bit that holds its value doesn't fire it.
- CLICK ladder export refuses two kinds of empty rung, instead of writing a NOP that CLICK rejects or deletes:
  - an empty rung with conditions (`with Rung(C1): pass`), since CLICK rejects a contact in front of NOP
  - an empty rung that shares a CLICK rung with a `.continued()` rung, since CLICK deletes a NOP row beside another row

  CLICK import refuses a CSV with a NOP row beside another row.
- CLICK ladder export writes the blank text constant (`""` or NUL, what a blank TXT holds) as `$00`, CLICK's code for NUL, and the importer reads `$00` (and any `$hh`) back as its character (copying the blank on a CLICK left the old character in place, and comparing with it matched any character). A longer text constant that holds a NUL is refused.
- Generated CircuitPython code runs as the other backends do:
  - a program with `math()`, split into `code.py` and `pyrung_rt.py`, now imports (it failed)
  - a text literal copied into a TXT (`copy("ABC", txt[1])`) fills consecutive registers (it stopped the scan with `ValueError`)
  - it drives the same system bits and registers as `PLC`:
    - SC40, SC43, SC44 and SC46 clear at the start of every scan
    - a pointer copy below its block sets SC44
    - SD9 counts scans
    - SC1, SC2, SC3 and the clocks SC4..SC9 move
    - SC10 reports the RUN/STOP switch, SD10 the scan time, and SD19..SD26 the real-time clock
  - when a rung writes SC50, the code goes to STOP with SC11 off, with or without a RUN/STOP switch. It stays there until the switch goes to STOP and back to RUN, or until a restart when there is no switch (a program without a switch kept scanning)
  - its Modbus server answers as a CLICK does:
    - a write to an SC or SD that CLICK's Data View shows as Read Only answers with exception code 2 and changes nothing (it acknowledged the write and stored a write to SC50 or SC51). Only a rung can stop the code.
    - an FC 15 or FC 16 write that reaches one refused address stores nothing (it stored the addresses before that one)
    - a coil write to X, T or CT answers with exception code 2 (it stored the write)
    - a read of an address in a CLICK bank that no tag or rung uses returns 0 (it answered with exception code 2). A write there still answers with exception code 2.
    - it serves every cell of a bank that a rung reaches by pointer or block instruction, as in `ds[ptr]` (a cell no tag named answered with exception code 2)
    - XD and YD serve CLICK's 17 registers each, built from the X and Y bits the TagMap maps. A write to YD sets those Y bits. A YD register with no Y bits keeps the word written to it.
    - a read of units 17..32 of an X or Y slot returns 0 (it answered with exception code 2)
  - timers take their time from `time.monotonic_ns()`, so they no longer drift after hours of uptime, and the first scan after STOP->RUN counts its time
- A TXT blanked by `fill("")` or `reset()` holds NUL, as an unwritten TXT and CLICK's text registers do:
  - its `to_ascii` copy writes 0 (it left the destination unchanged)
  - a blank inside a `pack_text` range is refused with SC43
  - generated CircuitPython starts an unwritten TXT as NUL, and keeps a TXT received over Modbus or loaded from retentive memory as NUL
- `cause()` explains a writer that feeds on its own output once, not once per scan (it showed thousands of lines, or raised `RecursionError` after about 500 scans). For an accumulator `math(T + 3, T)`, it shows the rung that added and the rung that seeded `T` (a `copy(Setpoint, T)` on reset). Two `math()` rungs feeding each other show one round.
- CLICK import reads Send and Receive remote addresses written in MODBUS hex (`50Eh` with its register type), which it refused. It reads MODBUS 984 coil addresses (`1`..`65536`) as coils, not holding registers. CLICK export keeps a remote address in the form it was written, so `ModbusAddress("50Eh")` exports as hex (it became `401295`), and a hex program keeps its addressing through import and export.
- `send()` and `receive()` status bits (`sending`/`receiving`, `success`, `error`, `exception_response`) are optional, as in CLICK. One left off runs on a hidden tag, and CLICK export leaves it off, so CLICK programs that leave a status bit off now import (they failed).
- `math(randint(...))` over `Word` tags runs and exports in hex mode (it ran in decimal mode, and CLICK export refused it).
- A `Program`, `subroutine` or `@program` built from code with no source file, such as `exec()` of a generated string or the REPL, quietly skips the strict control-flow check instead of warning. Source that exists but can't be parsed still warns.
- `how()` drives a timer or counter inside a `forloop()` (it got no enable condition from the loop's rung, and stopped waiting on it).
- `call()` of a decorated subroutine inside another subroutine no longer sends the caller's later rungs to the main program.
- An `immediate(...)` coil target used only there now runs compiled, instead of failing with a missing-symbol error.
- Compiled runs and generated CircuitPython now read and write the plain tag behind an `immediate(...)` operand (they failed with `SyntaxError` or `TypeError`). This covers an operand of `copy()`, `blockcopy()`, `fill()`, `pack_*()` or `unpack_*()`, a plain `math(immediate(Src), Dest)`, a `search()` value, and a `run_function()` input or output. CLICK export still refuses `immediate()` there (`CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED`).
- Generated CLICK projects import the bank behind a `SUM ( DS1 : DS10 )` range, and math functions such as `sqrt`, in the subroutine file that uses them, instead of failing with a `NameError` when run.
- Validation messages show range sums and math functions as source (`DS.select(1, 5).sum()`, `abs(X)`, `sqrt(X)`) instead of class names. CLICK nesting-depth checks count `SUM ( ... )` as a paren level.
- Generated CLICK workspace guidance:
  - asks for **Simulate** in ClickNick before `pyrung live` (Stop in ClickNick's main window ends the session)
  - points PLC data loading at the Simulator's **PLC Data...**
  - no longer names a fixed debugger extension version
- `pyrung_to_ladder(validate=True)` and `ladder_to_pyrung(validate=True)` now compare the generated code with the CLICK rows, row by row:
  - they check each contact of each output's condition, its pins and its wire, and raise when a contact is lost or moved
  - a lone `branch()` still passes as the rung's own contacts, because it draws the same (`with rung(A): with branch(B): out(Y)` reads back as `with rung(A, B):`)
  - a pin that branches off the box's input wire still passes. It keeps the shared contacts in its condition and exports again from the rail
- `math()` with `sqrt` of a negative, `log`/`log10` of 0 or a negative, or `asin`/`acos` outside -1..1 no longer raises out of the scan. The function gives 0 and sets `fault.math_operation_error` (SC46) for the rest of the scan, and the PLC stays in RUN, in `PLC`, `CompiledPLC` and generated CircuitPython.
- `rise()` and `fall()` on the timed clocks (`sys.clock_10ms` .. `sys.clock_1h`) count one edge per toggle in realtime mode (with 1 ms scans, `clock_100ms` counted about 80 rises a second).
- An `off_delay()` with preset 0 keeps timing when a scan adds less than one unit, such as a scan under 1 ms at `"Tms"` (its accumulator stayed at 0).
- A `select()` with tag bounds (`DS.select(Start, End)`) sets `fault.address_error` and writes nothing, everywhere, when a bound is outside its block at either end. A `search()` over one now compiles (it generated an `IndentationError`).
- A `select()` with tag bounds that spans a gap in a sparse block skips the gap everywhere, as a static `select()` does (compiled runs and generated CircuitPython set `fault.address_error`). So `always()` and `never()` no longer report `Proven` on what `PLC` didn't do. A `shift()` whose window lies wholly in a gap raises `ValueError` every scan, everywhere.
- A harness `on_delay`, `off_delay`, `flasher_on` or `flasher_off` longer than 32.767 s now elapses (the feedback never flipped, because its millisecond timer stopped at 32767). Such a delay counts in whole seconds, rounded up (whole minutes past about 9 hours, and so on). Both delays of one `Physical` then count in that unit.
- `how()` on a harness feedback partway through its delay plans the wait from the count so far (it answered `REACHED` with no scans).
- Findings show code you can write: `math(expr, dest)`, `event_drum(...)`, `time_drum(...)`, `time_event_drum(...)`, `unpack_to_bits(...)`, `unpack_to_words(...)`, `forloop(...)` and `run_function(...)` (they showed `math(dest, expr)`, `eventdrum(...)` and similar names, or a class name).
- The debugger sends a `Real` holding Inf or NaN as `inf`, `-inf` or `nan` (it sent `Infinity`, which DAP clients refused).
- CLICK ladder export refuses a rung whose drawing would move a contact's read past an output on that rung that writes the contact's tag. The error names the contact and the output. Move the output to a rung of its own, or give the rows their own contacts.
- CLICK import refuses a pin row whose pin doesn't belong to the instruction above it, or comes out of CLICK's order (`.down()` or `.clock()` before `.reset()`, and a drum's `.reset()`, `.jump()`, `.jog()` in that order).
- `math()` into a pointer destination (`math(X + 1, DS[Ptr])`) raises a `TypeError` when the program is built, and the error says what to do (it raised `AttributeError`). Store into a tag with `math()`, then `copy()` it to `DS[Ptr]`.
- A generated CLICK project's `run.py` checks one scan on a fork, so the PLC the debugger attaches to starts at scan 0 (it started at scan 1).

### Performance

- `run_for()`, `run_until()` and `how()` run compiled on a program with `nop()`, `raw()`, or a `send()` or `receive()` whose target is a name such as `"panel"` (every scan ran interpreted). `how()` also runs compiled when the target is a `ModbusTcpTarget` or `ModbusRtuTarget`.
- `how()` starts faster:
  - a second `how()` on an unchanged program skips several seconds of setup
  - the first `how()` takes about half as long to set up
- `effect()`, and `history.at()` reading one scan after another, run much faster on scans older than `cache=`. An `effect()` over 1,000 such scans runs about 40 times faster.

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
