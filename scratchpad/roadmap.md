# pyrung roadmap (2026-11-15)

This is pyrung's list of work still to do, in the order Sam agreed on 2026-11-15. Work that crosses repos is written
once in the cross-repo plan (`ssweber.github.io/plan/README.md`, "the plan" below). This file links to it and does not
repeat it. When an item lands, delete its line.

Each item says what it needs:
- **[nothing]** is a refactor or a fix a session can do alone.
- **[CLICK]** needs the CLICK programming software.
- **[PLC]** needs a run on a real CLICK.
- **[Sam]** needs Sam's judgment.

Work that needs nothing is never queued behind work that needs Sam.

Specs that later work needs are kept beside this file:
- `session-recording/design.md` is the recording design.
- `specs/history-api.md` is the history API proposal.
- `specs/per-plc-definitions.md` sizes the per-PLC definitions table.
- `specs/jit-catalogue.md` lists the jit ideas already measured, so none is tried twice.
- `specs/conformance-tidy.md` is the recommendation for tidying the conformance suite.
- `specs/reconnect-findings.md` holds the notes from a reconnect fallback for how() that was tried and dropped.

## 0. Gate fixes, and the range bound

Every later step pays the gate, so it comes first. All [nothing]. The range bound, the soundness and conformance trims
and the cleanups landed in #52. After them `train-jit` took 61 min (it took 78.9). Per-test times from that run are in
`pyrung-wt/buildbot/logs/gate-trim/`.

- **Narrow `fold`** to the test files whose long runs use the loop (`run_for`/`run_until`). Today it reruns all of
  `test`'s selection, less the 16 slowest cases of `test_pilot_long_wait.py`. It took 964 s on 2026-11-15 (1,873 s
  with those cases).
- **Fuzz runs in no gate.** The `pytestmark` in tests/fuzz/conftest.py has no effect, and the fuzz tests put `@given`
  on an inner function. What is left checks `PLC` against `CompiledPLC` and the fold check. Decide whether a gate
  runs it. [Sam]

## 1. The deletions release

Plan items 3 (public surface) and 6 (search removal). All [nothing]. The record machinery (plan item 1.1) is gone.
ClickNick reads `command_grammar()`, so tell its session that `log`, `note`, `candidates`, `accept`, `deny`,
`suppress` and `spec` left it.

### 1b. The static facts

tests/facts checks the facts against real runs, over the idiom corpus and the conformance programs.

- **Facts the runs refute.** The check in tests/facts holds each one as a strict xfail, `test_known_failure_is_fixed[...]` in the file named.
  - Two kinds of `Real` keep an input domain with no NaN or Inf in it, so how() never sets one there. No run refutes it, because the runs never write one. A `Real` the program writes gets no domain when it may hold NaN or Inf (`prove/non_finite.py`).
    - an input `Real` the outside writes and no rung writes
    - a `receive()` payload
  - A tag the rungs compare with a tag that has no domain keeps a domain that may have no value equal to the other's. No run refutes it yet. Tags compared with each other that both have domains share their values (`_close_tag_comparisons` in classify.py). The other tag may have been absorbed, or have no claim, as `Loop_Result3` beside `TD211` in `loop/skipped_body_holds_timer_and_counter`. A sound fix gives the first tag no claim, unless its domain lists every value it can hold.
  - Slice elision drops a tag that a time drum reads as a step preset. `test_facts_conformance.py`, `[drum_ds/ds_preset_ends-scan-local-DS4016]`.
  - A functional dependency is claimed before the rung first writes the tag. `test_facts_idioms.py`, `[funcdep_follows_its_source-functional dep-PosOffset]`.
  - A functional dependency ignores a rung that writes the tag before its source moves. `test_facts_idioms.py`, `[funcdep_written_before_its_source_moves-functional dep-Lag]`.
  - A `to_text()` copy of an expression into a tag in no block declares only that tag. The runtime counts on by name (`Msg1`, `Msg2`, ...), and an expression's text has no bound, so the analyses miss the rest.
  - A copy of an input takes only the values the ladder writes or the input's split points, not values the outside sets. `test_facts_idioms.py`, `[hmi_value_copied_after_ladder_default-stateful domain-WorkingSetpoint]` and `[compared_input_copied_and_compared-stateful domain-HeldLevel]`. The fixpoint adds an input's split points after it computes the copy, so the fix is not a one-liner.
- **A pointer's input domain leaves out the addresses past its block.** An `external` pointer with no declared
  bounds and a default of 5 gets (1..100) for a block of 100 elements. A run sets it to 0, and the copy through it sets
  `fault.address_error`. No value of the domain does that. Found while fixing the writes through a pointer that list
  no targets (over-cap-writes). [nothing]
- **A pointer's declared bounds stand for what the ladder writes into it.** `_indirect_ref_tags` (pdg.py) lists only
  the addresses a pointer's `choices=` or `min=`/`max=` allow. A CLICK stores a ladder write past a declared bound, and
  pyrung only reports it. So a `math()` that sets a pointer with `max=10` to 50 writes element 50, and no writer list
  names it. The narrowing of writes that list no targets trusts no bound the ladder can pass, except here. A write
  through such a pointer reaches only the listed elements there too (`_declared_reach` in pdg.py). Without that, the
  tumbler's `AlmHistorian__*_idx` writes reach all of DS, and the tumbler loses 1100 levers. [nothing]
- **Unchecked leads from over-cap-writes.** [nothing]
  - `causal/why.py` and the `cause()`/`effect()` walks read `writers_of` and never ask
    `ProgramGraph.unlisted_writers_of`. So they miss a write through a pointer that lists no targets.
  - `causal/recorded.py:1370` (`_resolve_indirect_writers`) matches a tag to a write through a pointer by
    `_pyrung_block_addr` alone. That address moves when a run resolves a mapped slot. `Logi5` mapped onto bank 1005
    gives 5 before and 1005 after. So the walk can miss the write before the slot is resolved.
  - `writable_slots` (crossings/indirect_dest.py) takes an `external` root's default as its only value. It also leaves
    out a literal-only root's default. Its region can miss addresses the pointer reaches. Today it only removes levers.
  - `_ValueSets._indexed_values` takes an element's default when the element is missing from the tag table, even
    when a write through a pointer reaches it. The pointer-range lint and `_UnboundedReads` use it.
- **System tags pyrung never writes.** On a CLICK, `fault.plc_error` (SC19) and `fault.code` (SD1) change, and the firmware version (SD5-SD8) is not 0. pyrung leaves them at 0, so the facts hold them at 0. A run on pyrung refutes nothing. Decide whether pyrung sets them, and then add them to `RUNTIME_VALUES`.
- **Left over from the search deletion.**
  - `build_program_graph(unbounded_index_reads=, observed_system=, observed_tags=)` and `_UnboundedReads` in pdg.py
    were the search's. Only tests pass them now. The `indirect_reach` maps they fill are then always empty. [nothing]

### 1c. The public surface

Plan item 3. `pyrung`, `pyrung.click` and `pyrung.circuitpy` are public. The few names users need move to the top
level, and validation gets one public module. ClickNick's test of its named imports is ClickNick's work.
- Decide `expr_requires` and `reset_dominance` (analysis/simplified.py). Nothing in src calls them since the record
  machinery went, but test files that `spec test` wrote import them.

### 1d. Then the `Rung` -> `rung` rename in tests

- 9,536 lines in 399 test files, plus 70 lines in 19 src files. `rung` is already exported (_rung.py:204), and the
  docs use only `rung`.
- It gets its own squash commit straight after the deletions, before new branches start, because it conflicts with
  every open branch.
- Add `.git-blame-ignore-revs` with that commit, and a check in `devtools/semantic_lint.py` that refuses a new
  `with Rung(` in tests.

## 2. CLICK-match fixes and the watchdog

The 2026-11-10 readings shipped in 0.16.0. The conformance page lists two known differences: 64-bit Reals (step 5)
and a copy past the end of the DD bank (`pending="PASTBANK"`, no fix planned).

- **A watchdog stop.** Past a generous iteration cap, a scan stops with a watchdog error in the interpreter, the
  kernel and the CircuitPython code instead of hanging. CLICK's default watchdog is 200 ms. [nothing]
- **The hardware round at the end of this step.** [PLC]
  - modbus_raw probes, with no ladder change. FC15 across the Y gap (Y115..Y202) skips reserved coils and stores named
    ones. FC05 to a reserved coil (Y117) answers 02. A read past XD8/YD8 answers 02. A read in an X gap (units 17..32)
    reads 0. FC05/FC15 to X001, T1 and CT1 answer 02. How Data View shows TXT byte 200, by eye.
  - A Modbus write of one word of a DF that holds NaN. pyrung keeps 0xFFFF in the other word. Also writing 2.5 as two
    FC6 writes, low word first, over a NaN (pyrung leaves 2.50002).
  - `flag_boxes/ton_ctu_by_pointer` (part follow_flags) can't tell whether a TON or a CTU clears an SC44 raised
    through pointer -1. It needs another way to raise and hold SC44.
  - Re-run parts drum1, drum2, follow_timing, follow_extra and text_flasher to confirm their rewritten readings.
  - Re-paste part control from the current build, which clears the false "Changed" results.
  - Unread: whether a TD write keeps any part of a ms left over, and a ladder write to the next phase's TD on the scan
    a phase ends.
  - Read the stores that pyrung models but no CLICK has recorded. These are a Copy of NaN or +/-Inf into an Int or a
    Dint, a Math of NaN into an Int, and a Math of +/-Inf into an Int or a Dint. Also read what a CLICK stores for a
    pack_text of text just past the Real range, such as "3.40282352E38" (pyrung stores +Inf).
- **`math()` of a Real into a Word.** pyrung runs it, but a CLICK refuses it. The CLICK check reports
  `CLK_MATH_MODE_MIXED`. Decide whether pyrung runs it or refuses it. [nothing]
- **CLICK v4 send/receive forms** (laddercodec's asks, ruled for after 0.16.0). [CLICK]
  - `com_port="wlan"` on ModbusTcpTarget (a missing com_port means Ethernet), and `"cpu3"` for RTU.
  - `device_id` as an int or a DS address. CLICK refuses DD, DH and C there. What an out-of-range ID does at request
    time needs a probe [PLC].
  - `to_text` writes only the flag that applies to its source. Export refuses `exponential=True` on an integer
    source.

## 3. The recording core

Plan item 1.2. The design is `session-recording/design.md`. Sam's rulings of 2026-11-15 are in the plan.
All [nothing].

- **Fix `replay`.** It stops after the first `step`, `run` or `reload`, because those return `"stopped"` and
  capture.py:182-196 treats that as a breakpoint. The tests miss it because every replay transcript ends on the step.
- **Add** `assert` (a tolerance on Reals, an error on an unknown tag) and `run until <condition> within N`. Today
  `run` takes only a count or a duration. dap/expressions.py has a parser to reuse.
- `record` saves the raw transcript, and a recording carries its scan time. The pytest plugin collects recordings.
- **The rename function.** It lives here, because it parses expressions. ClickNick supplies the old-to-new map at the
  build that first carries the new names. A name that still exists is left alone, so a swap rewrites nothing. A name
  that is gone becomes whatever now names its old address. Recordings hold names: the nickname, or the display
  address where there is none.
- Declining ClickNick's setup prompt means no recording.

## 4. One door

Plan item 1.3. Every action that changes state is a console command, so a recording holds everything a user did.
The old structured requests for those actions are deleted, not wrapped. Reads stay as requests. pyrung, ClickNick and
the VS Code extension release together. [nothing, with a joint release]

- **Through the door.**
  - From the VS Code extension: pyrungForce, pyrungUnforce and pyrungPatch. The panel sends a dict of tags, and
    `patch` takes one tag today.
  - From ClickNick (dap_service.py:611-758): SetMode, SetSpeed, Force, Patch, Unforce, ClearForces, the five Link
    requests, Reload and StepScan.
- **Stay as requests (reads).** Query, Graph, FindLabel, ListMonitors, Seek, ForkAt, TagChanges, Causal, ModbusInfo
  and ListForces. Monitors change the debugger's view, not the PLC, so AddMonitor and RemoveMonitor stay too.
- Console verbs can already return structured data (`ConsoleResult.body`, as `reload` does).
- Today panel force and patch never reach `capture_hook`, so recordings miss them. That is the gap this step closes.

## 5. Larger features

- **float32 Real stores.** Match CLICK: denormals kept, and 0.1 + 0.2 == 0.3. The cases `real_bits/underflow` and
  `real_bits/float32_equality` carry `pending="F32"`, and the `real_text` 123456.7 case waits on it too. Sam called it
  "worthwhile, but it has sorta big blast radius". [nothing]
- **The per-PLC definitions table.** Each PLC owns its slot and Tag definitions, so a later `slot()` edit or TagMap
  can't reach a PLC already built. The sizing is `specs/per-plc-definitions.md`. [nothing]
  - The "middle" attempt (bob/freeze-middle, dropped) kept missing an edit path. `Block.reset()` clears history a
    built PLC reads, and a TagMap `external` stamp on a pointer-only slot gets past the analysis refusal.
  - Still open: `slot(n).reset()` on a slot a scan made a tag of works in place, so a running PLC picks up the new
    name and loses a retentive value.
- **The history API.** It provides `changes()`, `values()`, `state_at()` and `states()`. Sam wants it. The spec is
  `specs/history-api.md`, items 4.2-4.6. Each records reader gives exactly what a replay gives, and it falls back to a
  replay across an edit made between scans. [nothing]
- **calc() into a pointer destination,** across the interpreter, kernel, codegen and the pilot readers. The CLICK
  export refuses it unless the hand check allows it. Remove the unreachable indirect-dest code in
  `CalcInstruction.execute` and `_dest_type`. [nothing]

## how(): after the recording core

`how()` stays and grows. The last resort stays small. A goal reached only through it is a missed connection in the
planner, which is fixed in the planner. The last resort is not widened into a search.

1. **Classify every stop first.** Sam names two kinds. Either pilot cannot trace back, or it does not know how to
   continue forward. Record the kind and the place for every stop. [nothing]
   - Today `Plan.reason` is prose, and `__str__` parses it back (graph.py:578-586). `Stuck(reason_code, frontier)`
     exists inside pilot (navigation_contracts.py:789) but only reaches an event.
   - There are 8 stop exits in pilot.py (561, 1029, 1282, 1338, 1499, 1527, 1548, 1752) and about 3 in api.py.
   - Add a stop kind and a place to `Plan`, and thread them through `_DriveOutcome` and `_plan_from_outcome`. The
     estimate is 150-250 lines plus tests.
2. **The idiom corpus** (plan item 5). Each result is one of: reached directly, reached through the last resort, or
   stopped. A `how()` test goes through `how()` on a corpus program, not through pilot's internals. [nothing]
3. **Sort the list below by stop kind** before fixing anything in it.
4. **A plan becomes a recording** run by the one replayer. That replaces the planned verdict kernel.
5. **Soften the docs** that say "Cannot reach" means proved until the writer list has one source and the facts check
   exists: analysis-diagnosis.md:159 and 174, and graph.py:419-421. [nothing]
6. **Per-call work.** The facts and the proof kernel are cached per program already.
   - `prepare_drive`'s six computations (drive_setup.py:174-221) run on every call and can be cached per program.
   - The replay kernel is compiled per search on purpose (pilot.py:519). A shared cache saved about 2 s of compiling
     and nothing on the trio (`specs/jit-catalogue.md`).
   - The three pilot `lru_cache`s ignore `_invalidate_graph_cache`. [nothing]
7. **Speculative narrowing for steering.** Sam asked for it on 2026-11-16. `how()` may guess a tighter range for a tag than the facts allow, and use the guess to order its candidates and routes. The guess reads declared `min`/`max`/`choices`, even on a tag the ladder also writes. Each guess is checked by running the plan. A failed run drops the guess and falls back to the sound facts. A guess never feeds `static_proofs`, a stop reason or any other claim. So a "can't reach" or a "never changes" rests only on the sound facts. [nothing]

The list to sort:
- The last resort can't see inputs through plant links.
- The planner's guesses read `writers_of` alone, so they miss a write through a pointer that lists no targets. These are tide_tables.py:518 and :645, static_expressions.py:302, trace_constraints.py:235, options.py:150 and :155, and guidance.py:75. A run checks each plan, so none of them makes a false claim. [nothing]
- Chains past the trace depth of 15 reach only through the last resort.
- An En misread. It reads the scan end where it should read at the rung.
- Fold the frontier projection into `read_program_step`.
- Drop the holds a replay shows unneeded.
- A reconnect fallback. The dropped bob/how-gaps tried one; the notes are `specs/reconnect-findings.md`.
- The drum profile reads a tag preset live, where the drum latches it at step entry.
- Static proofs N1 (a block write onto a plain tag named like the slot) and N3 (an AttributeError at
  build).
- A plain watchdog and a latched valve say STOPPED.
- Goals on system tags say STOPPED, for example `how(sys.always_on)`, `how(sys.scan_counter > 5)`, and
  `rtc.new_second == 0` before any write. `target_reached` reads a missing tag as False, while stepping reads its
  value. The fix puts system tags in pilot's snapshots.
- An avoid operand that only a patch or force gave isn't registered the way goal operands are.
- `Val[Ptr] > 5` stops. `copy(cmd[ptr], mid)` stops. A plan's text prints `IndirectRef(Sp[Ptr])`.
- Seeding speed. how() on a fresh PLC went from 28 s to 35 s on tumbler. Seed only the reached slots some rung reads,
  plus the goal's tags.
- A restart plan doesn't say it dropped a by-name operand. With Lim set only by `patch({"Lim": 5})`, a goal
  `Count > Lim` needs a restart. The plan is right, because Lim reads 0 after the restart, but its text doesn't say
  so. On the reached state, `how(Count > Lim)` raises "no rung uses that tag".
- Pilot's change checks count a new NaN object as a change. Pens, quiescence, departures and recording transitions
  must change together: coast.py near 266, 343, 481 and 674, recording.py near 147, causal.py:56,
  corrections.py:1286, regression_requirements.py:281 and 387, and drive_setup.py:280.
- A plan on a counter runs one scan long. `how(Count > 3)` ends at Count 5, where stepping meets the goal at 4
  (trim.py and the pulse path).
- An `==` goal on a counter reaches only through the last resort. The first pulse moves Count 0 -> 2 against a
  requested 3, so the drive reads a regression and gives up on Go. A latch gated by a comparison on a count does the
  same.
- A rung gated by `Count > 3` with `how(Lit)` reaches only through the last resort.
  `math(Count + K + 1, Count)` with a tag K stops. `math(Count + 1, Count, oneshot=True)` stops.
- A `!=` gate on a register its own writer produces gets no lever, so it reaches only through the last resort.
- A counter behind a `Mode != 0` gate stops (stepping sets Arm, then Bump). A `!=` goal through a gated copy stops,
  and the last resort seems not to take `!=` goals.
- `how(Y, avoid=Alarm)` stops on a For-called subroutine where one scan reaches Y without Alarm.
- A how() `wall_clock_s` overrun on a For over an open external Dint count.
- In STOP, `pilot_events`' `started` data has placeholder `steerable_count`, `route` and `pipeline_roles`.

## Tests

Sam agreed to these on 2026-11-15. The gate fixes are step 0 above.

- **One authority for instruction behaviour.** Conformance cases already run on the compiled kernel through
  `tests/conformance/test_loop_corpus*.py`, but never through `CompiledPLC` (`--runner-backend=both`). Run them
  through the engine pair, then delete the core files that restate them. The candidates (6,555 lines) are test_timers,
  test_drums, test_counters, test_out_variants, test_real_range, test_edge_detection, test_click_copy_stores and
  test_shift. Check each fact against a case before deleting it. [nothing]
- **Rules for new tests, from now on.**
  - A fix adds a row to an existing table or case group, not a new file.
  - What an instruction does is a conformance case. Engine agreement comes from the harness, never by hand.
  - A `how()` test goes through `how()` on a corpus program.
- **Case tables and shared builders** only in files that change anyway.
- **Pilot's internal tests** retire gradually as the idiom corpus grows (1,121 tests never call `how()`).
- **Tidy the conformance suite, don't rebuild it** (Sam, 2026-11-10). The checklist in `tests/conformance/AGENTS.md`
  becomes data the report reads, each case names its subject, and a few new cases cover untested rules. The
  recommendation is `specs/conformance-tidy.md`. [nothing]

## laddercodec and pyclickplc

Plan item 2. The order follows laddercodec's and pyclickplc's releases.

- **laddercodec as a dependency, in three steps.**
  1. Dev-only. Pass pyrung's CSV export through laddercodec's `read_csv` and list the disagreements.
  2. Runtime, when the first duplicated rule table is deleted.
  3. pyrung's CLICK codegen reads laddercodec's objects.
  Where the two disagree, laddercodec's rule wins by default. The pyrung sections of laddercodec's `csv-format.md`
  move here. [nothing]
- **The undecoded marker.** laddercodec replaces an undecodable rung with a marker rung. Its one instruction,
  `undecoded("rung N, row R: ...")`, runs here as an unconditional End (Return in a subroutine) and reports a
  finding. It waits on laddercodec's release. [nothing]
- **pyclickplc's `read_csv` changes, and the tag map reads through it.** It raises on a bad row with its line
  number, keeps nickname and comment as written, accepts true and 1 for Retentive, doesn't fail on a repeated
  nickname, and reports which columns were present. Check the tag map's tests against it when pyclickplc releases.
  [nothing]

## Small fixes

All [nothing] unless marked.

- **Text stores.**
  - A text into a logical block mapped mid-bank should count on into the bank, as CLICK does. Today it stops at the
    block end with CLK_TEXT_PAST_BLOCK, while a windowed compare on the same tag reads on through the bank.
  - A text store in a structure field past the structure.
  - An import round trip of a compare across a UDT boundary or onto padding.
- **A PDG cached before a later TagMap goes stale** (`program._cached_graph`, pdg.py ~1819). `writers_of` keeps only
  the mapped tag while every engine writes on.
- **The weak `_pyrung_hw_slot`** frees with the bank when the TagMap and ClickBlocks are dropped, and a store then
  faults.
- **CircuitPython codegen.**
  - A Char whose name holds a newline breaks it ("unterminated string literal").
  - It never writes `_rand.N`, so a rung reading `expr.tag` sees only its default.
  - Its validation has no immediate checks (a contact on an InputBlock element, a coil on an OutputBlock element).
  - A tag mapped straight onto `yd` (`TagMap({OutWord: yd[1]})`, as docs/dialects/click.md:240 shows) is hidden from
    Modbus. Fold the slot into the Y bit image, or refuse the mapping.
  - Two `ClickBlocks()` calls give one bank two arrays. After `copy(22, ds2[ptr])`, `copy(ds[ptr], Seen)` gives 22 in
    the core and 0 in the generated code.
- **A randint whose expression is garbage-collected** while a rung reads its `.tag` is drawn by neither engine.
- **Loop coverage.** The guarded loop declines a Copy to text through a DS pointer, and a read through `dd[ZERO]`.
- **reload NB3.** A leg-only edit fires at most once on the reload scan. Put legs into the `_match_sites` identity.
- **Immediate leftovers.** CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y's message isn't in pyrung terms.
  `calc(expr, immediate(D))` and `search(result=immediate(R))` raise AttributeError at build instead of a clear
  refusal.
- **CompiledPLC raises OverflowError** on a huge Dint product outside calc, where the PLC gives 0.0
  (`_primitives.py:564`).
- **A stuck-bit false positive** on a For over a constant-fed count.
- **Leftovers from the timing work.** `pilot_events(And(...))`. Block `_tag_cache`. "X False via OTE". A raising
  opaque avoid escaping lockstep. `run_for(1.0)` at dt 0.1 takes 11 scans. `_set_time_mode` mid-run replay. Realtime
  dt as a Fraction. DAP Memory shows `_frac` in ns.
- **The loop corpus's bookkeeping.** Its ~70k checked scans can prop up `fold_check.vacuous()`.
  `production_constants` is re-run under `--fold-check` (fold_check.py:1459). `PARTS_THAT_DECLINE` records only the
  first reason. `ledger stop not judged` is missing from `_SKIPS_LISTED_BY_TEST`.
- **DAP reload.** A Block in a module outside the program folder survives a reload, so re-running
  `slot(addr, default=new)` raises the conflict error.
- **`ClickDataProvider._resolve_xy_slot`** (data_provider.py:269) raises for the XD/YD registers between the
  documented ones, where the P1AM code maps them onto XD1/YD1. Check against the 2026-11-10 modbus_raw readings
  first.
- **`cause(tag, to=nan)` says unreachable** on a NaN the program reaches. For `math(Source * 1.0, Mid)`, the crossings
  reverse turns `Eq(Mid, {nan})` into an empty set (core/analysis/crossings/, which pilot shares).
- **`repr(plc.tags)` depends on which slots were asked for,** so `<not read yet>` shows for different slots on two
  PLCs. Display only.
- **effect() raises TypeError after a slot rename between scans.** `effect("Src", scan=2)` gives "'>' not supported
  between 'NoneType' and 'int'".
- **`history.at(root)` may replay inside a parent epoch** when the records reader checks a fork root that isn't
  cached. Rare.
- **send_receive wrappers rebind `backends.ClickClient` on every call** (send_receive/_core.py). A test that patches
  `_core.ClickClient` leaks the fake into `backends`.
- **Comments left from the records work** in coast_run.py (689-690, 798-801, 856-857) and rung_firings.py (492-494).

## Jit: fault flags as hook keys

The fault flags are hook keys (`_HOOK_KEYS` at scan_trace.py:1819, `_HOOK_CELLS` at coast_run.py:953), so a trace in
which a flag moves is declined. The scan-start hook never touches them once present. They could leave both lists
while `scan_start_is_quiet` still requires them present, which also drops its per-scan `all()`. It needs the fold
stage. [nothing]

## Ideas, not scheduled

- **One condition representation.** There are two: `simplified.py` (1,323 lines) and the Constraint family with
  crossings/ (2,409). `GuardRequirement` wraps a Constraint with evidence, and the console parser is a front end that
  stays. Merging them is about 70 src files, 300 sites and 1.5-3k changed lines, plus 56 test files.
- **The plant pane release** (ClickNick). Printed temporary logic as CLICK coils (the old hold-text A3, prototype
  first). Suggesting `link=` and `physical=` (C): how() tries each bool kind against the outputs, a replay confirms
  it, and how() suggests the annotation without adding it. It suggests only when the logic is faster than a person
  can act. Printing a plant as its coil (B7) needs real T addresses, picked by the pane's snippet export. Still to
  name: a non-Bool rule's bit kind (`__set1__` proposed). See memory `project_clicknick_plant_pane`.
- **qev**, labqoat's small local decision model. Exploration only.
- **Task programs** (CLICK's interrupts as named blocks with a `.when()` trigger) and **ASCII send/receive**. Both
  were designed in April 2026 and never built. The notes are in git history (`scratchpad/interrupt-plan.md` and
  `scratchpad/send_receive_ascii.md`, removed 2026-11-15). [Sam]
- **A C-backed persistent map** in place of pyrsistent. It was parked in June 2026: about 0.6-0.7 ms off a 2.0 ms
  interpreted scan for a large refactor. A trial with `immutables` was 3x worse in its pure-Python fallback.
- The how() compare could count last-resort steps. [Sam]
