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

- **Narrowing `make test-fold` is shelved.** Branch bob/fold-narrow (ef7059b, local only) runs only the 121 test
  files whose runs go through the jit (`run_for`/`run_until`), from a generated list. Narrowed, `make test-fold` took
  676 s on a quiet machine. The full run took 960 s. Both compared the same 2282 tests. The branch adds about 440
  lines, and `make test` fails when a new test file goes through the jit but is missing from the list. Sam shelved it
  on 2026-11-17. `make test-fold` runs only after a jit change, so the saving does not pay for the lines and the new
  `make test` failure.
- **Fuzz runs in no gate.** The `pytestmark` in tests/fuzz/conftest.py has no effect, and the fuzz tests put `@given`
  on an inner function. What is left checks `PLC` against `CompiledPLC` and the fold check. Decide whether a gate
  runs it. [Sam]

- **Comments still say `calc`.** Many docstrings and comments still say `calc` where the code now says `math`.
  The comment rewrite (#85) kept the old word, because the rename changes a fact. Rename them in one pass.

## 1. The deletions release

Plan items 3 (public surface) and 6 (search removal) ship in 0.17.0, on Sam's tag. All [nothing]. The record machinery (plan item
1.1) is gone. The public surface and the `Rung` -> `rung` rename landed in #94, #95 and #96, and `Rung` is no longer
a public name. The static facts below are what is left of this step.
ClickNick reads `command_grammar()`, so tell its session that `log`, `note`, `candidates`, `accept`, `deny`,
`suppress` and `spec` left it. ClickNick's test of its named imports is ClickNick's work.

### 1b. The static facts

tests/facts checks the facts against real runs, over the idiom corpus and the conformance programs.

- **Facts the runs refute.** The check in tests/facts holds each one as a strict xfail, `test_known_failure_is_fixed[...]` in the file named.
  - Two kinds of `Real` keep an input domain with no NaN or Inf in it, so how() never sets one there. No run refutes it, because the runs never write one. A `Real` the program writes gets no domain when it may hold NaN or Inf (`prove/non_finite.py`).
    - an input `Real` the outside writes and no rung writes
    - a `receive()` payload
  - A value that reaches a compared tag through a copy or a `math()` keeps split points the comparison does not split. A count copied to a display tag that a rung compares with an HMI count keeps its domain, which ends at the preset. `test_facts_idioms.py`, `[shown_count_compared_with_an_hmi_count-stateful domain-Boxes_Acc]`. An HMI `Real` copied into the table entry a Real input is compared with keeps whole numbers only, `[real_reading_compared_with_a_table_entry-input domain-HmiTemp]`. A drop of the compared tag would have to reach back along the copy to the tags it reads.
  - A tag copied into a tag compared with an HMI tag keeps a short domain. With `copy(Raw, F)` on every scan and `F == C`, Raw keeps the split points of its own comparisons, and a run refutes it at 37. Widening reaches only the writers of a tag that keeps a domain, and F has none. This goes to the bindings transport.
  - The facts see no comparison inside a counter's or timer's `.reset()`, or inside a `search()`. So `.reset(Lvl == 30)` gives `Lvl` no split point at 30, and `search()` gives none at its value. The reviewer's probes refute both.
  - Two `Char` tags a rung compares with each other share no values, since `_close_tag_comparisons` in classify.py shares only numbers. So `A == B` may have no pair of domain values that answers True. No run refutes it yet.
  - A functional dependency is claimed before the rung first writes the tag. `test_facts_idioms.py`, `[funcdep_follows_its_source-functional dep-PosOffset]`.
  - A functional dependency ignores a rung that writes the tag before its source moves. `test_facts_idioms.py`, `[funcdep_written_before_its_source_moves-functional dep-Lag]`.
  - A `to_text()` copy of an expression into a tag in no block declares only that tag. The runtime counts on by name (`Msg1`, `Msg2`, ...), and an expression's text has no bound, so the analyses miss the rest.
  - Slice elision takes a write that an instruction can skip for a write on every scan. `ProgramGraph.unconditional_write_before_read` counts each write an unconditional rung declares. These instructions can skip theirs. `test_facts_idioms.py`, `[text_copy_skipped_on_a_bad_digit-scan-local-KeyLevel]`.
    - `send()` and `receive()` on a scan with no reply, for the status tags and the payload. The runs set those tags as a peer would, so the check cannot refute this case.
    - a `copy()` through a pointer out of range, past the end of its source, or from text that is not a number
    - a `pack_text()` of text that does not parse
    - a pack or unpack through a bad pointer
    - a `search()` through a range or value pointer out of range
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
  - `causal/projected.py` (`cause(tag, to=)` and `effect(tag, to=)`) still reads `writers_of` alone. So it calls an
    element that only a write through a pointer reaches unreachable, and it takes such an element in a contact for an
    input.
  - `writable_slots` (crossings/indirect_dest.py) takes an `external` root's default as its only value. It also leaves
    out a literal-only root's default. Its region can miss addresses the pointer reaches. Today it only removes levers.
  - `_ValueSets._indexed_values` takes an element's default when the element is missing from the tag table, even
    when a write through a pointer reaches it. The pointer-range lint uses it.
  - A `forloop()` index makes `cause()` slow. With `copy(Src, Big[idx + 1])` and `copy(Big[idx + 1], Dst[idx +
    3001])` over 2000 elements, `cause("Dst4500")` takes 35 s and 2004 steps (2.2 s with `deep=False`). The step for
    the index takes every element the loop reads as a held enabler. An index occurrence's footprint should not make
    every element read an enabler.
  - why() with no scan history loses a write through a pointer that a patch or a force once aimed at the tag,
    after the pointer moved on. It reads where the pointer aims in the state, and otherwise only where the
    program's own values can aim it. `PLC.why()` reads the scan history and sees the write.
  - why() repeats a tag in `conjunctive_roots` when two pointer writers that the scan history confirms both reach
    it. Two listed writers already did this.
  - why() shows no step for a writer in an unconditional `Rung()`. `_walk_backward` skips a rung with no condition
    tree (`if sp_tree is None: continue`), so its write and the tags it reads drop out of the chain.
  - No test pins the absence fallback of PILOT on a never-written element of a block of more than 1000 elements.
    The guard cuts (`_precise_causes`) also offer the lever in every how() program tried. A unit test like
    `test_pilot_absence_root.py`, where only the absence fallback offers the lever, would pin it.
  - The VS Code extension does not show uncertain steps. `renderStep` (historyPanel.js) ignores `step.uncertain`, so
    a `why()` step marked `?` looks certain there.
- **A slot with a mapped tag and a name override has two names.** `_effective_slot_name` (memory_block.py)
  gives the override. The runtime stores under the mapped tag's name (`_get_tag`). So a caller that looks a
  store up by `_effective_slot_name` may miss the mapped tag. These callers do that.
  - classify.py:745 and :953
  - pilot/evidence.py:454 and pilot/tide_tables.py:607
  - causal/recorded.py:1327 and crossings/indirect_dest.py:192
  - input_overrides.py:251 and :393
  The fix is in memory_block.py. Either one name wins everywhere, or the second assignment is refused. [nothing]
- **System tags pyrung never writes.** On a CLICK, `fault.plc_error` (SC19) and `fault.code` (SD1) change, and the firmware version (SD5-SD8) is not 0. pyrung leaves them at 0, so the facts hold them at 0. A run on pyrung refutes nothing. Decide whether pyrung sets them, and then add them to `RUNTIME_VALUES`.
- **Declared `min`/`max`/`choices` on a tag the ladder writes.** Decide what they mean there. Sam deferred the decision. [Sam]
- **Instruction relations recorded by the codegen** (the bindings). One codegen writes both the compiled kernel and
  the CircuitPython code. Each of its helpers records what its piece of code reads, writes and compares. prove/ and
  pilot/ then read those records in place of their hand-kept lists for each instruction. The design is
  `pyrung-wt/buildbot/jobs/bindings/EMIT-DESIGN.md`.
  - Steps 0 and 1a are in main (#79). Step 1a moved the raw code strings into shared helpers, with byte-identical
    output. The timer records followed (#84).
  - Step 2 onward waits for Sam's nod on the size. The design estimates about 1,600 to 2,100 lines of package and
    codegen code, plus tests. [Sam]
  - Two gaps the records close (`bindings/CONSUMERS.md` 1.2 and 1.3, migration step 2).
    - CTU and CTD declare no `completion_controls` (counters.py:152 and :266), so pilot's `_completion_branch_error`
      (requirement_derivation.py:211) refuses them. The records would give "reset False", and for CTU the down input.
    - The TOF profile ignores the armed memory (timers.py:301). The kernel times a TOF only while it is armed or done
      or has a nonzero Acc. So PILOT may wait on a TOF that was never armed, with Done False and Acc 0, and it never moves.
- **Two write questions left by bob/validator-writes.** [nothing]
  - COIL_CONFLICTING_OUTPUT keeps its own list, `_common._instruction_write_targets`. `write_sites` also reports
    `receive()`'s destination and `run_enabled_function()`'s outputs on a scan with the rung off, because both write
    then. If COIL_CONFLICTING_OUTPUT switched to `write_sites`, this error-level check would report those writes too. Run each one first to confirm what it writes with the rung off.
  - The choices, readonly and final checks skip a write through a pointer that may reach a flagged slot. Decide
    what severity a write that only might land gets.
- **Other validators read tag flags from `_build_tag_map`, which lists only contacts, `.target` and `.source`.**
  readonly-writes moved the readonly and final checks to the tags write_sites reports. A follow-up branch does the
  same for the rest. [nothing]
  - Confirmed: choices_violation.py:101 and :160. `fill(9, Modes.select(1, 2))` gives no TAG_CHOICES_VIOLATION.
  - Unprobed: stuck_bits.py:591 and :644, physical_realism.py:427 and simplified.py:1249.
  - None of them reports a write through a pointer to a flagged slot, readonly and final included.
    `static_write_target_tags` returns nothing for a pointer target.
  - Other hand-kept write lists: `_common._instruction_write_targets`, `_common._resolve_tag_names` and
    `_resolve_tag_objects`, `_written_names` in cmp_conditions.py:459-467, and `_instruction_write_targets` in
    prove/elision/slice.py:362.

#### One owner per question

The design review of prove/ and pilot/ (`pyrung-wt/buildbot/jobs/design-review/REPORT.md`) found questions that
several modules each answer their own way. A fix then lands at one caller and misses the others. Each refactor below
gives one question one owner. All [nothing].

1. **What an instruction writes.** The readonly and final checks read write_sites (readonly-writes). The other validators follow (above).
2. **What a condition reads.** Each atom of a resolved condition carries its read set and its source condition.
   Four read sets then take the union of the atoms' reads. They are `_referenced_tags`, read_trace.py:24-102,
   query.py:332 and static_expressions.py:56. `prove/expr._eval_atom` keeps literal compares and evaluates the rest
   through the source condition, so 5 leaf evaluators become one. runner.py:850-933 gets simpler, and `ArithAtom`
   goes. This refactor lands after bob/compared-tags, which it conflicts with.
3. **What values a declaration allows.** `value_domains.declared_value_domain` (:16) is the owner. It gains
   `with_default=` and `max_values=`, and gives None for a Real. It replaces `classify._declared_domain` (:834, 12
   callers), `validation/_common._tag_domain_values` (:925), `pilot/skiff._declared_domain` (:489) and
   `_ValueSets.declared` (value_sets.py:393). They disagree today on empty choices and on caps. classify's copy has no Real check.
   Its callers at classify.py:3187, :3348, :3352, :3497 and :3510 may give a Real whole numbers. This refactor lands after bob/compared-tags and
   bob/pdg-leftovers.
4. **Whether a write runs on every scan.** `pdg.unconditional_write_before_read` (:562) splits into
   `scan_local(tag)` and `power_on_value_unread(tag)`. `_extract_rung_node` (:1351) leaves oneshot writes out of
   `sure_writes`. `runs_every_scan(node)` reads `_always_run_subroutines` (:1937). The patches at slice.py:594 and
   :826 and why.py:134-139 go. This refactor lands last, after the codegen records each instruction's reads of its own state.
5. **Whether a tag is an input.** `ProgramGraph.source_of(tag)` is the owner. It answers CONSTANT, OUTSIDE, LADDER
   or LADDER_UNLISTED. `classify_tags` (pdg.py:1766) derives `TagRole.INPUT` from it. The readers are
   trace.py:1346 and :2154, program_facts.py:231, awaited_actions.py:405 and dataview.py:252. Today an input is any
   tag that is read and not in `writers_of`. So `.inputs()` lists an internal `Mode` with default 2 that nothing
   writes. No order set.
6. **What values a pointer takes.** One builder makes a pointer's value set. pointer_range.py:50 reads no rung
   guards, so it may report a false CLK_PTR_PAST_BANK. No order set.
7. **Where the program walkers live.** They move to `core/program/walk.py`. That breaks the import cycle between
   `analysis/` and `validation/` (49 and 29 import lines). No order set.
8. **What an id in pilot is.** NewType ids replace `tuple[Any, ...]`. pilot has 220 such fields, and 131 of them
   are ids. No order set.
9. **Whether two values match, and which rung writes a tag.** `sp_values` becomes public, because 58 modules
   import `_values_match`. `_writer_for_tag` (sp_values.py:209) returns the first writer in a rung, but the scan
   keeps the last. It also caches on `Rung` with setattr. No order set.
10. **What each stage of a search does.** `_search_events` (pilot.py:580-1775) does all of them in 1,196 lines,
    nested 8 deep. Split it into its stages. No order set.

## 2. CLICK-match fixes

The 2026-11-10 readings shipped in 0.16.0. The conformance page lists two known differences: 64-bit Reals (step 5)
and a copy past the end of the DD bank (`pending="PASTBANK"`, no fix planned).

- **No watchdog stop for now** (Sam, 2026-11-17). A cheap CLICK validation finding could estimate a For loop's work
  (iterations x body) and warn past a threshold. [nothing]
- **The hardware round at the end of this step.** [PLC] It goes through clicknick's PLC queue (Sam,
  2026-11-17). bob/main-runner exports conformance groups as main-only projects for it. Only items that need
  wiring, input override, a power cycle or a person stay for Sam. clicknick has answered the stores of NaN and
  +/-Inf, the pack_text of text past the Real range, and the TD write (`jobs/plc-answers/NOTES.md`). bob/plc-runtime
  matched pyrung to them and added part follow4_reads (groups `plc_stores` and `td_count_on`).
  - Run part follow4_reads on a CLICK to confirm its cases.
  - Run part modbus_raw once on a CLICK, to record its probes of X, T, CT, the coils between slots and the registers
    after XD8 and YD8 in `readings.toml`. clicknick read the same requests by hand (`jobs/plc-answers/NOTES.md`), and
    bob/plc-modbus made both Modbus servers match them.
  - `flag_boxes/ton_ctu_by_pointer` (part follow_flags) can't tell whether a TON or a CTU clears an SC44 raised
    through pointer -1. It needs another way to raise and hold SC44. clicknick has nothing on file, and its runner
    cannot read a runtime flag (clicknick, 2026-11-18). So a pyrung session writes a new conformance case first
    [nothing]. The case's ladder keeps SC44 raised on every scan, for example with a pointer copy that faults on
    each scan, and puts a TON or a CTU box alone after it. The case then runs on a CLICK on Sam's go.
  - Re-run parts drum1, drum2, follow_timing, follow_extra and text_flasher to confirm their rewritten readings.
  - Re-paste and re-run the `to_text()` cases in parts copy, math, system and follow_extra. Their export now
    writes only the flag the source has, which changed their hashes.
  - Re-paste part control from the current build, which clears the false "Changed" results.
  - Re-run `word_store/word_literals`. Its 70000 halves were removed, since the export refuses them now, so it needs
    one CLICK run to confirm it again.
  - Unread for a TD write. A CLICK was read dropping the whole ms that a timer had counted before the write.
    - Whether a CLICK keeps the fraction of a ms that the timer had counted.
    - What a ladder write to the next phase's TD does on the scan that a phase ends.
  - A Copy of a positive quiet NaN (7FC00000) from a DF into a DD. A CLICK was read storing -2147483648 for the
    NaN FFFFFFFF only. pyrung stores -2147483648 for every NaN.
- **`math()` of a Real into a Word.** pyrung runs it, but a CLICK refuses it. The CLICK check reports
  `CLK_MATH_MODE_MIXED`. Decide whether pyrung runs it or refuses it. [nothing]
- **CLICK v4 send/receive forms** (laddercodec's asks, ruled for after 0.16.0). [CLICK]
  - The form questions go into laddercodec's Send and Receive grids, not into a list here. clicknick's runner
    drives Receive with `try`, and Send is being added (clicknick, 2026-11-18). clicknick runs the forms on Sam's go.
  - What a CLICK does when the DS register of a `device_id` holds an ID out of range, at request time, needs a probe
    [PLC]. pyrung passes the value on as it is.

## 3. The recording core

Plan item 1.2. The design is `session-recording/design.md`. Sam's rulings of 2026-11-15 are in the plan.
All [nothing].

The fixed `replay`, `assert`, `run until <condition> within N`, and `record` with pytest collection are in main
(#79, #84). These are left:
- **The rename function.** It lives here, because it parses expressions. ClickNick supplies the old-to-new map at the
  build that first carries the new names. A name that still exists is left alone, so a swap rewrites nothing. A name
  that is gone becomes whatever now names its old address. Recordings hold names: the nickname, or the display
  address where there is none.
- Declining ClickNick's setup prompt means no recording.
- **A reload drops the scan-1 stop event.** `src/pyrung/dap/reload_console.py:773-774` calls `reloaded.stop()` before
  `_set_time_mode`, which starts a new scan log, so history never records that stop. Found by bob/record-save's review. [nothing]

## 4. Console commands for every change

Plan item 1.3. Every action that changes state is a console command, so a recording holds everything a user did.
The old structured requests for those actions are deleted, not wrapped. Reads stay as requests. pyrung, ClickNick and
the VS Code extension release together. [nothing, with a joint release]

- **Requests that become console commands.**
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

- **Parse operands once, in the builder.** Four places each decide which operands are valid: each instruction's
  `execute()`, the compiled kernel's code generator, laddercodec's CLICK rules, and the operand declarations
  (`_operands`) that bob/build-refusals adds. Sam wants no fourth copy (2026-11-19, "parse, don't validate sounds like a
  winner"). [nothing]
  - Each builder turns its arguments into checked operand objects, once. `execute()`, the kernel, CircuitPython codegen
    and the CLICK export accept only those objects, so their defensive code goes.
  - The split, in Sam's words to the planner: "get the 80% via 20% and then let laddercodec get the long tail". pyrung's
    parse checks the shape of each operand: its kind, its type, a range against a tag, and a count. laddercodec keeps
    every CLICK rule (which banks, which addresses, which constants), and the export passes it the checked objects.
  - The operand declarations become pyrung's one grammar.
  - Why: build-refusals took three review rounds and kernel-agree two. Every finding was one copy disagreeing with
    another.
  - Start after both branches land.

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

The list to sort. Each entry names the mechanism behind the gap. Group them by cause before fixing any, and
look for the one change that closes a group, as the codegen records closed the hand-kept timer and counter lists.
- The last resort can't see inputs through plant links.
- Wider input domains cost the last resort and the skiff. Tags a rung compares with each other share their values, so some domains grew (tumbler `Ref_Cmd_Stop` from 6 values to 19).
  - The last resort tries the first 8 values of an input's domain in sorted order (last_resort.py:72). It no longer tries 7, 8 and 9 for `Ref_Cmd_Stop`.
  - The skiff probes a word only when its domain has at most 8 values (skiff.py:564).
  - A tag compared with a pointer element, with arithmetic or with an input that has no bounds now gets no domain. Neither of them tries a value for it.
- A stateful tag compared with another tag now holds that tag's values too, so `_satisfying_value` (sp_values.py:392) can pick a value its writers never store. A lever aimed at that value may find no writer for it. No case shows it yet.
- A comparison with a pointer element stops, as in `Rung(Lvl == Tb[P], Lvl > 5)`. how() has no lever that sets Lvl to the value `Tb[P]` reads. `test_pilot_answers_not_errors.py`, `test_a_tag_compared_with_a_pointer_element` is a strict xfail.
- A comparison with arithmetic stops, as in `A.Acc == B.Acc + 2` and `A.Acc - B.Acc == 2`. So does `A.Acc > B.Acc` on two counters. The trace has no lever for them.
- The planner's guesses read `writers_of` alone, so they miss a write through a pointer that lists no targets. These are tide_tables.py:518 and :645, static_expressions.py:302, trace_constraints.py:235, options.py:150 and :155, and guidance.py:75. A run checks each plan, so none of them makes a false claim. [nothing]
- An off-delay that was never switched on still stops `how()`. The off-delay's profile now plans "switch it on
  until Done, then off" (bob/bindings-timers). The trace does not follow a timer's enable through the
  accumulator, so PILOT never starts the plan. An on-delay there is reached only through the last resort. [nothing]
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
- **No how_compare program reaches a timer's or counter's completion branch.** `_completion_branch_error`
  (requirement_derivation.py:211) has 0 calls across the set, so a change to `completion_controls` shows in unit
  tests only. The counter-held-in-reset row reaches through the last resort and never asks it (bob/bindings-timers r3).

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

- **laddercodec as a dependency, in three steps.** They follow the plan's order (Sam, 2026-11-16). Before them, a
  dev-only diff on bob/lc-diff lists where pyrung's export and rules disagree with laddercodec's. [nothing]
  1. Validation on laddercodec (plan item 2, step 2). laddercodec becomes a runtime dependency here. No laddercodec
     release comes first. The work lives on one branch. It builds against laddercodec's `main` and stays open until
     the joint release (plan, "The joint release", Sam 2026-11-17). It does not merge early. Meanwhile pyrung's
     `main` stays releasable on its own.
     - The joint release waits for two things. This branch must pass pyrung's existing validation tests against
       laddercodec's `main`. Sam must also review, from a user's view, the kebab-case `.code` that laddercodec gives
       each refusal. Nothing else is waited for.
     - The plan said the public surface lands before this branch opens. It and the `Rung` rename landed in #94, #95
       and #96, so the branch can open.
     - The branch needs a git source for laddercodec in `pyproject.toml`. Check that `make install`, mergeq and CI
       accept it.
     - Each case where pyrung becomes more permissive is logged in `pyrung-wt/buildbot/jobs/lc-diff/PERMISSIVE.md`.
       laddercodec's CLICK checks test each one against CLICK before the PR merges (Sam, 2026-11-17).
  2. Export through laddercodec (plan step 5). laddercodec's main has `csv_rows()`, a public `CSV_HEADER`, and
     `write_bundle()` and `read_bundle()` for `main.csv` plus `subroutines/<name>.csv`. When pyrung moves to
     `write_bundle()`, delete the `_2` suffix code in click/ladder/types.py. It never fires, because
     `Program.subroutines` is a dict keyed by name.
  3. Import reads laddercodec's objects (plan step 6).
  Where pyrung and laddercodec disagree, the case is logged and the fix goes into laddercodec (Sam,
  2026-11-17). This replaces "the refusal wins". The pyrung sections of
  laddercodec's `csv-format.md` move here.
  - Disagreements for laddercodec to fix (logged 2026-11-17): `to_text(exponential=1)` from a Real constant
    (CLICK accepts it, case22) and from a DF pointer such as `DF[DS1]` (CLICK accepts it, clicknick
    copy-pointer-text). laddercodec's `Copy._is_df` matches only a literal `DF<n>`.
- **The undecoded marker.** laddercodec replaces an undecodable rung with a marker rung. Its one instruction,
  `undecoded("rung N, row R: ...")`, runs here as an unconditional End (Return in a subroutine) and reports a
  finding. laddercodec has the marker on its `main`, unreleased. pyrung runs it after the joint release (plan, "In
  laddercodec"). [nothing]

- **A TXT initial value with edge spaces.** clicknick imported nickname files into CLICK and read the result
  (`clicknick-wt/buildbot/jobs/csv-import/RESULTS.md:102-106`). CLICK keeps a quoted value as written, so `" a "`
  stays ` a `. CLICK trims the whitespace (space, tab, VT and FF) at both ends of a bare value, so ` a` becomes `a`,
  and a bare space is stored as no value. [nothing]
  - The pyrung work is for `TagMap.from_nickname_file()` to keep a quoted value and trim a bare one, as CLICK does.
  - pyclickplc 0.9.1's `read_csv` keeps a Text initial value as written, and its result does not say whether the
    cell was quoted. So the reader must get the quoting from pyclickplc, or read the raw cell itself.

## Small fixes

All [nothing] unless marked.

- **Some accepted forms never write anything.** Under Sam's fault-only ruling (2026-11-19), a form that raises or
  only sets a fault flag on every data is refused at build. bob/build-refusals left these forms accepted, and they
  still write nothing. Each one needs a run over several data before a refusal.
  - `forloop()` with a `randint()` count whose bound is a Real.
  - `+ [list]` into a Bool or a Char.
  - `calc(sqrt(...))` over text or a range.
  - A copy with a converter (`convert=`) whose every data only faults.
- **Odd forms that write something, for Sam to rule on.** They run, so refusing one is Sam's call.
  - `math(range, Bool)` always writes True.
  - `copy(Timer[1], txt)` stores the Timer object in the TXT.
- **More checks show the caret under the first match only.** #103 marks the refused argument in each copy-family
  finding. These checks still mark the first place the text matches, which can be the wrong argument:
  `core/validation/_common.py:1536`, `choices_violation.py:165`, `cmp_conditions.py:723`, `physical_realism.py:286`
  and `math_conditions.py:150`.
- **The kernel declines an `IntFlag` constant.** Stepping stores its number (`Fl.A | Fl.B` gives 3), so the kernel
  could compile it as it compiles an `IntEnum`. The decline is safe and only costs speed.
- **An aggregate outside `math()` gets one finding only.** A `.sum()` in a `copy()` source or a compare is reported as
  `CLK_EXPR_ONLY_IN_MATH`, and the range check does not run on it. The export still refuses a scattered range there.
- **A rung over CLICK's 32 rows can export.** pyrung's Click export counts one row for each CSV row. In CLICK many
  boxes are taller than one row. On 2026-11-18 the export of `plc_stores` wrote a rung of 17 CSV rows, a Fill and
  16 Copies. laddercodec counted 34 rows, and its `encode()` refused the rung. The bob/hw-split branch split that
  rung in the case.
  - `_LadderExporter._assert_rung_height` in `src/pyrung/click/ladder/_exporter.py` refuses a rung of more than 32
    CSV rows. 17 Copies into DS pass that check and make a rung of 34 rows.
  - laddercodec stores each box's height in its instruction's `cell_params()["visual_rows"]`:
    - 2 rows for every Copy-family box (copy, block copy, fill, pack and unpack), whatever the destination. Search
      and a timed coil are also 2 rows.
    - 3 rows for Send, Receive, Email, Home, Position and Velocity, and for a debounce or flasher coil.
    - One row for each pin, plus 1, for a Timer, Counter, Shift or Drum. A Timer with no reset pin is 2 rows, and
      pyrung writes it as 1 CSV row.
  - The height check should take each box's height from that list, or ask laddercodec for it.
  - `estimate_steps` in `src/pyrung/click/ladder/steps.py` estimates program steps and counts no rows.
  - laddercodec's main refuses a rung of more than 32 rows in `read_csv`, `write_csv`, `read_bundle`, `csv_rows`
    and `write_bundle`, and its `check()` reports `layout-too-tall` (laddercodec 12a42d9, #120). pyrung's export
    does not go through laddercodec yet. Until it does, `_assert_rung_height` is pyrung's only guard.
- **The graph panel's slice ignores the call-site rule.** Sam's coordinating session ruled on 2026-11-18, under Sam's
  rule that the views should not surprise a user, that the panel should match the console.
  - The panel computes its slice in JS (`computeSliceLocal`, graphPanel.js) over the graph of the active file. So it
    does not stop at a call rung's condition as the console does, and it can't see a call rung in another file.
  - The fix is for `performSlice` to ask the adapter for `pyrungSlice` through the extension host. That restores the
    `sliceResult` relay.
  - `on_pyrung_slice`'s edge filter (graph_slice.py:247-254) needs a second fix. It drops the edge from a call
    condition to its call rung, because the call rung writes nothing. The condition would show as a node with no
    edge. Keep that edge, and add the rung to `slice_rungs`, when the rung has `calls`.

- **Two subroutine names that differ only in case.** pyrung accepts "Pump" and "pump" in one program. The export
  writes `subroutines/Pump.csv` and then `subroutines/pump.csv`, and on Windows the second file overwrites the first.
  laddercodec's `write_bundle()` refuses the pair. pyrung should report the pair as a validation finding before the
  export. CLICK refuses the pair too. ClickNick's runner checked on its CLICK 4.00 on 2026-11-18, in two runs.
  After "Pump" existed, adding "pump" brought up the same box as adding "Pump" again: "GUI-036 The Program Name
  already exists. Please choose another name."
- **CLICK import of a Break, Continue or For/Next that CLICK refuses.** CLICK refuses a Break or Continue with no
  contact (C000B, C000C) or outside a For loop (C010F, C0110). It also refuses For/Next nested more than three deep
  (C010D). laddercodec's `check()` will report each of these per program, for Main and for each subroutine.
  - pyrung's DSL refuses all three when the program is built. `break_loop()` and `continue_loop()` raise
    `RuntimeError` outside a `forloop()` and in a body rung with no contact (`_loop_exit_context` in
    core/program/instructions.py). A fourth nested `forloop()` raises `RuntimeError` (`ForLoop.__enter__` in
    core/program/context/_control_flow.py).
  - CLICK import has no check for them, even with `validate=True`. On 2026-11-18 a release-prep session edited an
    exported CSV and ran `ladder_to_pyrung()` on it. A Break with no contact, and a Break outside the loop, both
    imported. The generated code raised when it ran, and both times the message said "break_loop() works only
    inside a forloop", with no CLICK code.
  - A pyrung session makes CLICK import report each case with CLICK's code before it writes the code. Once
    validation runs on laddercodec, import can read the findings of laddercodec's `check()`.

- **`CompiledPLC` leaves out a block slot that no rung uses.** When the program text writes `DS[1]` and no rung
  reads or writes it, `PLC`'s state holds `DS1: 0` and `CompiledPLC`'s state has no `DS1`. The two engines must
  hold the same tags, so `CompiledPLC` should carry the slot. The fuzz suite found it
  (`tests/fuzz/reproducers/parity_20261117_indexed_unused_slot.py`, strict xfail
  `test_unused_indexed_slot_parity`).
- **Breakpoint conditions share no rule with the Watch.** `compile_condition_for_breakpoint` resolves no choice labels
  or `Pump.State` aliases, and a condition on an unknown tag never fires. Its users are breakpoint conditions,
  logpoint conditions and data-breakpoint conditions (monitor_data_breakpoints.py:169). They should share the Watch's
  rule, with labels looked up at evaluation time because breakpoints outlive a reload. `to_conditions` (how() and
  causal) resolves labels but raises KeyError on a `Pump.State` alias.
- **Fill a number into TXT.** `fill(5, txt.select(1, 3))` stores the int 5 in each TXT and exports
  `fill(5,TXT1..TXT3)`. CLICK's editor saves that Fill. Nobody has read what a CLICK stores in each TXT. [PLC]
- **Export a fraction constant into DH or YD.** CLICK accepts `copy(1.5, DH1)` (copy_single answers). What it stores
  there is unread. [PLC]
- **Spell a Real of 1e7 and up as CLICK saves it.** CLICK saves 123456789.0 as `1.234568E+08`, 12345678.0 as
  `1.234568E+07` and 1e10 as `1E+10` (clicknick `lc-diff-click/RESULTS9-10.md`). CLICK writes it in E form with up to
  7 significant digits (`%.7G`). `click_real_literal` writes `123456800.0`, `12345680.0` and `10000000000.0`, and
  `3.0E+38` where CLICK would save `3E+38`. The TXT1000 check already counts CLICK's form. Changing the export moves
  the hashes of the conformance cases with such constants:
  - `copy(1.0e10, df[196])` in `copy/test_copy_more.py`, and the 1.0e10 in `copy/test_real_text.py:103`.
  - `copy(16777216.0, df[63])` and the `3.0e38` constants in `copy/test_real_bits.py`.
  - The `3.0e38` constants in `copy/test_word_floor.py` and `math/test_agg_more.py`.
  - The +-1.0e10 in `math/test_math_more.py:118-119` and the +-3.0e38 compares in `math/test_math_terms.py:148-149`.
    [nothing]
- **Copy Binary (`convert=to_binary`) on the type-7 pairs:** CTD to and from DF, a Real into DD or CTD, 32768 or 65536
  into DF. CLICK runs them (RESULTS6). pyrung faults and writes nothing, and its validation passes the constant-source
  forms. [nothing]
- **Text stores.**
  - A text into a logical block mapped mid-bank should count on into the bank, as CLICK does. Today it stops at the
    block end with CLK_TEXT_PAST_BLOCK, while a windowed compare on the same tag reads on through the bank.
  - A text store in a structure field past the structure.
  - An import round trip of a compare across a UDT boundary or onto padding.
- **A For body rung with branches imports to code that fails.** The import writes `branch()` inside `forloop()`, and the DSL refuses it ("branch() is not permitted inside forloop()"). A NOP leg beside a coil hits it, and so do two coils on branches.
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
