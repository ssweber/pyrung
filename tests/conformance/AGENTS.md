# Conformance Suite

Plain-English cases for PLC semantics. Same case, same ladder, same check -- run in-process, over Modbus against pyrung's soft PLC, or over Modbus against a real CLICK.

A conformance suite in the Modbus sense: the harness observes the implementation **only through registers**. The ladder computes its own answer and writes it into its slot; the harness writes `Cmd`, waits, and reads the slot back. Everything else -- the reset between runs, the handshake that says the reset is done, the documentation -- lives in the ladder too, so any target that runs the ladder and serves its DS registers over Modbus runs the suite unchanged: pyrung's soft PLC, a real CLICK, or the program's generated P1AM-200 code (run on this machine today; on the board later).

The whole suite is **one CLICK program**: a main program that calls one subroutine per group (`Conf_Rungs`, later `Conf_Math`, `Conf_Drum`, ...). Load the full project once; after that, editing a group means pasting its changed rungs (through the ClickNick workspace, see "Running against the real PLC") and rerunning that group.

| Run | Where | How | Pacing |
|---|---|---|---|
| In-process | `make test` (unmarked) | `run(group)` on a fixed-step `PLC` | exact scan counts |
| Modbus, local sim | `make test-conformance` (marker `conformance`) | `run_modbus(suite.build())` | wall clock |
| Modbus, real CLICK | `make test-conformance PLC=host[:port]` (or `--conformance-plc`, or env `PYRUNG_CONFORMANCE_PLC`) | `run_modbus(suite.build(), "host:port")` | wall clock |
| Modbus, P1AM-200 code | `make test-conformance TARGET=circuitpy` (or `--conformance-target`, or env `PYRUNG_CONFORMANCE_TARGET`) | `run_modbus(suite.build(), sim=CircuitPySim)` | wall clock |

Add `GROUP=name[,name...]` (or `--conformance-group`, or env `PYRUNG_CONFORMANCE_GROUP`) to run some groups only. The Modbus runs share every line of client code; only the endpoint differs. `-rP` prints the report: one line per case (`PASS`/`XFAIL`/`FAIL`/`SKIP`/`STALE`, `[confirmed]`/`[unconfirmed]`, the pending step and the probe question; a `FAIL` or `XFAIL` line is followed by what the case read) and totals.

## What a test is

1. A sentence that says what should be true.
2. A pyrung ladder that proves it by writing the answer into a known slot.
3. A check that reads the slot and compares to what we expected.
4. A reset that puts everything the case touches back the way a fresh PLC has it (see "Reset").

The ladder writes its own answer because Modbus only reads addresses — we can't peek at variables from Python.

```python
group = Group("rungs", [
    case("Memory writes are visible to the next instruction on the same scan",
         ladder=prove_memory_is_immediate, expect={"Result1": 42}),
])

def test_rungs():
    assert_all_passed(run(group))
```

- **Group**: a named set of cases, one per case module (`group = Group(...)`). The name is snake_case; the subroutine is `Conf_<Name>` (`rung_order` -> `Conf_RungOrder`) and slot nicknames start with `<Name>`. CLICK nicknames are at most 24 characters and `<Name><slot>_ErrorCode` must fit, so keep `<Name>` to 12 characters (build fails otherwise). `suite.py` lists every group in `ALL_GROUPS`; a new case module adds its `group` there.
- **Case id**: the stable key of the case's slot in the manifest. It defaults to the ladder function's name without a leading `_` or `prove_` (`prove_source_order` -> `source_order`); pass `id=` to pin it. Renaming the ladder function renames the id, and the build then asks for a manifest entry -- pin `id=` if you rename. To change a case, rewrite it in place (same id, same slot) while its id still names the question it asks; the group goes stale until it is pushed again, as for any edit. When the id no longer fits, or the case asks nothing (no model reads it differently), retire it: drop the case and leave its manifest line, commented `# retired: why`, so the slot is never reused; a replacement takes a new id at the next index.
- **Confirmed**: nobody flips a flag. A run against a real CLICK (`make test-conformance PLC=...`) writes every case that passed to `confirmed.toml` (`group/id`, the hash of the case's own exported rows -- its `Case` comment through its last rung, not the subroutine's closing `return()` rung, so appending a case leaves its hash alone -- the date) and drops every case that failed (`record_confirmations()`). The report marks a case `[confirmed]` while its recorded hash matches the case as it is now, so rewriting a case (sentence, expectation, ladder, slot) leaves it unconfirmed until it passes on a CLICK again, and counts the unconfirmed ones. Commit `confirmed.toml` with the run. An in-process-only case confirmed some other way is entered by hand with `hash = ""`.
- **The expectation is CLICK's.** A case expects what a real CLICK reads, not what pyrung does today. Where pyrung differs, the case says so with `pending=` (next section); where nobody knows yet, it is a probe (`probe=`) expecting the answer pyrung would adopt.
- **Comment**: generated, never written by hand. Each case's first rung carries `Case <group>/<id>`, the sentence, and `Expect: Result1 = 5, Result2 in 1..6` (`case_comment()` in `src/pyrung/conformance/_ladder.py`), so the ladder in CLICK documents itself and Python stays the single source. The comment is part of the exported ladder, so editing a sentence or an expectation changes the group hash: the group reports stale until its rungs are pushed again, and Preview Changes shows the comment as a diff.

## Pending and probes

**`pending="S2"`** marks a case whose expectation (CLICK's) pyrung does not meet yet. The tag names the fix step that will close the gap (`PENDING_STEPS` in `src/pyrung/conformance/_case.py`): `S2` counters count edges, `S3` per-contact edge memory, `S4` live row-by-row rung solve, `DRUM` drum ordering, `INF` REAL overflow stores +/-Inf, `TEXT` text/pack/NUL/search-cursor/MDN fixes, `SYS` the fault-flag model (SC40 follows the last Math, SC43/SC44 the last instruction), `MATH` Math results CLICK stores differently (RAND bounds past an Int).

- On pyrung's own targets (in-process `run()`, the local sim, `TARGET=circuitpy`) a pending case is a **strict xfail**: it passes, reported `XFAIL` with what pyrung read, only if no run meets its expectation and its reset is clean (a leak still fails it). Once pyrung starts meeting the expectation, the case fails with "remove pending=": the fixer of that step deletes the marker in the same commit, so `grep 'pending="S2"'` lists exactly what a step must flip.
- Against a real CLICK (`PLC=`) the expectation is judged as for any case. `run_modbus(pending=None)` decides by endpoint: an endpoint means a CLICK; pass `pending=True` for a local target served on an endpoint of its own (`test_suite_repeats_without_restarting`).
- `pending` is not part of the exported ladder or the case comment, so removing it changes no group hash.

**`probe="..."`** marks a case that asks an open hardware question, in a few words for the report. Its expectation is the answer pyrung would adopt (and, where pyrung reads something else, the case is also `pending`). Its module docstring lists every candidate answer and what each reads, so a probe that fails on a CLICK is an answer, not a bug: change the expectation to what the CLICK read and say which answer it was; the next CLICK run confirms it.

## Tee-drawn twins

A CLICK pin can run from the rail with its contacts, or tee off the box's input wire after contacts it shares with the enable (CLICK refuses a box whose pins start from different places, error C000A "All inputs of the box instruction must originate from the same branch": all pins from the rail, or all teed off one node, never a mix; hardware-confirmed on the counter twin). pyrung's exporter always writes the first. A case with `tee=TeeSpec(box, pins, after)` is the tee-drawn twin of a rail-drawn case: it runs the same pyrung rungs on every target (its pins hold the shared contacts too, as `And(shared..., own)`), and the CLICK export (`ConformanceProgram.export()`, so the part export and the workspace) redraws the named pins, which must be the first pin rows under the box row, as tees after the first `after` contacts of the box row; the pins named must be every pin of the box (`draw_tees` raises naming C000A when one stays on the rail), so a pin's own condition must start with the shared contacts (the counter twin's reset adds a never-true contact for that). `export(drawn=False)` writes the rail drawing. The group and case hashes are taken from the drawn rows. `test_tee_twins.py` pins what the redraw touches and that the importer reads the drawing back as the rail drawing, so what the twin runs in-process is what the drawing imports to. In ClickNick, Preview Changes lists a twin's rungs as changed against the rail source, since pyrung source cannot draw a tee.

## One-shot pattern

Every test ladder gates on `slot.Cmd != 0, slot.Fired == 0` and sets `copy(1, slot.Fired)` in its last rung. This ensures instructions fire exactly once:

- Scans with `Cmd = 0`: the case's reset rung runs (see "Reset"), nothing fires.
- Scan 1 (after Cmd patched to 1): `Cmd != 0` and `Fired == 0`, logic fires, `Fired` set to 1.
- Scan 2+: `Fired != 0`, logic skipped.

For multi-rung tests, put `copy(1, slot.Fired)` in the last rung. Earlier rungs still see `Fired == 0` on the firing scan because rung N+1 sees rung N's writes.

The runner verifies `Fired` went up — a test that never fires is a failure even if values match defaults.

## Multi-scan tests

For tests that observe behavior over time (timers, counters), use `oneshot=True` on setup instructions and regular `copy` on observation instructions. `Fired` stays a boolean.

```python
def prove_ton_resets(slot):
    with Rung(slot.Cmd != 0):
        copy(1, slot.Scratch, oneshot=True)   # setup fires once
        on_delay(timer, preset=100)
        copy(timer.Acc, slot.Result1)         # always updates
        copy(timer.Done, slot.Result2)        # always updates
        copy(1, slot.Fired, oneshot=True)
```

Setup copies fire on the first scan the rung is true, then stop. Observation copies keep overwriting Results every scan — the last read wins by the time the harness checks. Over Modbus the harness reads twice, so a Result that is still moving (`timer.Acc` above) needs a `range` expectation; see Expectations.

## Scan-paced tests

A case about how a bit moves from scan to scan (coils, edges, one-shots) counts its own scans, with the helpers in `tests/conformance/scans.py`. `count_scans(slot)` makes `Scratch` the number of the scan in progress; the rungs under test key on it (`slot.Scratch >= 3, slot.Scratch <= 5`); `trace(slot, bit)` records how many scans a bit was on and its first and last scan, `edges(rise(bit), slot, count, at)` how often an edge contact passed power and when; `done(slot)` raises `Fired` once the window is recorded, not on the first scan. Nothing moves after the harness sees `Fired`, so every expectation is an exact int, in-process and on the wire, however late the scans come. `test_coils.py` is the example.

An edge contact goes first on its rung (`edges()` puts it there), on the rail, so the case does not also ask whether an edge contact behind an open contact tracks its bit. That is a question of its own.

## Checking a case checks something

Before a case lands, change each of its expected values by one and check that the case fails, and remove its `init` (if it has one) and check that the leak check complains. A case that still passes asserts nothing. For a `pending` case, do it against what pyrung reads: pin the expectation to pyrung's reading (it must pass), then move each value by one (each must fail); and check that CLICK's expectation differs from pyrung's reading. A case whose answer could differ on a CLICK should be able to tell the two apart: say in the module docstring which case shows which model, and what the other model would read.

## Reset

`Cmd = 0` resets the case; `Cmd != 0` runs it. Every case's first rung is emitted by the framework (`emit_case()`), is true every scan while `Cmd == 0`, and does three things in order:

1. `fill(0, <the whole slot>)` -- one instruction, `fill(0, DS111..DS119)` for `rungs` slot 1. This also zeroes `Cmd` (already 0) and `Ready`.
2. The case's `init(slot)`, if it has one: instructions that put the case's own state back.
3. `copy(1, slot.Ready)`. A case with `ready=` raises `Ready` in a second rung instead, `Rung(slot.Cmd == 0, *ready(slot))`, so `Ready` is 1 at the end of a scan only once those conditions hold.

The harness writes `Cmd = 1` only after it reads `Ready = 1` with `Cmd = 0` and `Fired = 0`. Modbus is served between scans, so that read means a whole scan ran with `Cmd = 0` and the reset was complete.

Reset with the instructions' own semantics, not by poking registers an instruction owns:

- **Timers** (TON, Out coils): the case's rungs are gated on `Cmd != 0`, so the rung goes false and the instruction resets itself. A debounce coil turns off only once its off side times out: `ready=lambda slot: ~c[1811]`.
- **Counters, RTON, drums, shift registers**: tie the reset input to the reset, `count_up(ct, 100).reset(slot.Cmd == 0)`, `time_event_drum(...).reset(slot.Cmd == 0)`.
- **Latched or toggled bits, and bits driven from inside a loop**: `reset(c[1801])` in `init`.
- **Staged registers** (a case's raw DS/DD/DF/DH): `fill(0, ds.select(4401, 4402))` or `copy(0, df[1])` in `init`.
- **Edge history and one-shots** need nothing: they rearm on the scans the rung is false. Not inside a loop whose For rung is false: CLICK skips that body, so nothing in it rearms (`loop/body_oneshot_holds_while_for_false`).

**A reset must not depend on an open probe.** If what CLICK does is the question a case asks, the reset cannot rely on pyrung's answer. F2 (a false For rung skips its body, answered on a CLICK) is why the `loop` edge, timer, counter and one-shot cases keep their For rung true and gate a body rung instead, so their resets are scanned.

**`keep=`** names tags the reset leaves alone on purpose. Two kinds:

- An accumulator that keeps running while the case is idle, so it never matches a fresh PLC. A debounce coil's off-side TD counts from power-up while the input stays off: `keep=[t[311], td[311]]`.
- A retentive value the experiment carries across the reset. The case must still pass on every repeat. Cases that need a power cycle or STOP->RUN in between ("retentive survives STOP->RUN") also need a harness that can switch modes; that does not exist yet.

Two checks keep resets honest:

- **Leak check (in-process).** `run()` runs every case twice on one PLC. After the second reset it compares the whole state -- tags and instruction memory (timer fractions, edge history, one-shot memory) -- with the state after the first reset on a fresh PLC. Anything that differs, other than system tags, pyrung's internal `_` tags and `keep` tags, fails the case that owns it: `state left after the reset: CTD201 = 5 (fresh 0)`. A case that passes its first run and fails its second also says `run 1 of 2: actual ...`.
- **Disjoint memory (build time).** `run()` and `build_program()` refuse cases where one writes a tag another reads or writes (`g/a and h/b share DS4000`), across groups too. Shared memory is shared state: a case's result would depend on its neighbours.

The leak check reads pyrung's state, so it only runs in-process. On the wire, `test_suite_repeats_without_restarting` is the check: against whatever endpoint the run targets, it runs the whole suite twice on one PLC without restarting it, then each group alone in the order A -> B -> ... -> B -> A.

## Program and register layout

```
main:           copy(<layout hash>, DS1)         every scan
                call("Conf_Rungs")               every scan, one rung per group
                                                 (Group(calls=2): two rungs, twice a scan)
                ...
Conf_Rungs:     copy(<group hash>, DS101)        every scan, first rung
                per case: # Case rungs/<id>      generated comment
                          reset rung             while Cmd == 0: fill slot, init, Ready
                          <case rungs, gated by its slot's Cmd/Fired>
                return()
```

| Registers | Holds |
|---|---|
| `DS1` | Layout hash (1..32767): the group table (subroutine names and bases) and `LAYOUT_VERSION`. Changes only when a group is added, removed, renamed, or moved. |
| `DS2`..`DS10` | Spare program header. |
| `DS<base>` | Group hash (1..32767): SHA-256 of the CLICK export of the group's case rungs, folded into a register. Changes whenever that subroutine's exported ladder would. |
| `DS<base+1>`..`DS<base+9>` | Spare group header. |
| `DS<base+10N>`..`DS<base+10N+8>` | Slot N of the group; the last register of each 10 is spare. Each spare is mapped as a tag named after its address (`DS120`), which exports with no nickname, so a target that serves only mapped registers (the P1AM code) serves the whole slot while the CLICK project stays as it was. |

Each slot is a `named_array` instance with 9 Int fields:

| Field     | Offset | `rungs` slot 1 | Purpose                  |
|-----------|--------|--------|--------------------------|
| Cmd       | +0     | DS111  | Trigger (harness writes) |
| Scratch   | +1     | DS112  | Temporary storage        |
| Result1-4 | +2..+5 | DS113-116 | Outputs to check      |
| Fired     | +6     | DS117  | One-shot gate            |
| ErrorCode | +7     | DS118  | Error reporting          |
| Ready     | +8     | DS119  | Reset done (reset rung writes) |

Cases cannot share memory, and the build checks it (see "Reset"). Anything a case touches outside its slot is raw CLICK memory its group owns (see "Raw memory"). `build_program(groups, manifest)` returns the program, its TagMap, the layout hash, and one `GroupBuild` per group (subroutine, block, slots, group hash). The build exports each group's subroutine through the validating CLICK exporter, so a case the exporter rejects fails at build time -- mark it in-process-only. Without a manifest, `build_program` packs the groups in order from DS11 (`Manifest.packed`): fine for harness tests and the local sim, never for a real CLICK, because adding a case moves registers.

CLICK does not support nested subroutine calls (confirmed in the CLICK editor), so a case cannot `call()` a subroutine of its own: its rungs already run inside `Conf_<Group>`. A group whose subroutine main calls more than once a scan (`Group(..., calls=2)`, the `call_twice` group) is how a case asks what a subroutine called twice does; every rung of such a group runs twice a scan, so its cases count calls, not scans. A group built with `in_main=True` (`loop_call`, part E) is the subroutine home: its case rungs run in main, so a case can `call()` a helper subroutine it defines itself (`with subroutine("Conf_<Group>_<Name>")` in the case ladder), even from a `forloop()` body. It has no `Conf_<Group>` subroutine: its group hash covers main's case rungs and the helpers, so a changed case is re-pasted into main (Preview Changes) plus its helper subroutines, and it takes a part of its own.

Unconditional rungs (an output wired straight to the rail, as in main's `copy`/`call` rungs and each group's hash rung) are valid in CLICK (confirmed).

## Raw memory

A slot is DS and comes from the manifest. Everything else a case touches -- C bits, timers, counters, staged DS/DD/DF/DH (a contiguous source range for `AVG`, a search table), TXT -- is a raw CLICK address, and nothing allocates it: a case picks addresses inside its group's ranges in `OWNED` (`tests/conformance/memory.py`). Import the banks (`c`, `t`, `td`, `ct`, `ctd`, `ds`, `dd`, `df`, `dh`, `txt`) and the `ClickTimer(n)` / `ClickCounter(n)` handles (`T<n>`/`TD<n>`, `CT<n>`/`CTD<n>` as `.Done`/`.Acc`) from there too.

A timer number claims both `T<n>` and `TD<n>`; a counter number both `CT<n>` and `CTD<n>`. Raw DS lives in the staging area DS3901..DS4500 (`STAGING_DS`), which no manifest block may reach. System points (SC/SD) are not raw memory: read them as `system.*` tags (`system.fault.out_of_range` is SC43, `system.sys.mode_run` SC11), which the runtime drives and the exporter maps to the CLICK address. A raw `sc[43]` is a plain tag nothing writes (it reads 0 in-process and on the sim, and the exporter refuses it beside the system point), so `memory.py` does not export `sc`/`sd` and the build check reports a case that uses one. `OWNED` is the only record of who owns what; a new group takes its ranges there before its first case, and a group that outgrows a bank's range takes a second one (a tuple of ranges, as `drum` and `text` do). Within a group, each case takes its own run of 10 (the `out` cases use C1801.., C1811.., C1821..), so a case can grow without moving its neighbours.

`test_build.py` checks it: every tag a case touches (its `footprint`, outside its slot) lies in its group's ranges (`coils/x: T5 is outside the group's memory (C1501..C1600; ...)`), the ranges are disjoint and inside CLICK's banks, and no manifest block reaches the staging area. A case that strays fails in its own module's run, not when another group lands.

## Manifest

`tests/conformance/manifest.toml` is the checked-in layout. It gives each group a fixed DS block and each case a fixed slot, keyed by case id:

```toml
[groups.rungs]
base = 101 # Conf_Rungs: DS101..DS300
slots = 19

[groups.rungs.cases]
memory_is_immediate = 1
source_order = 2
global_mutation = 3
```

A block is `10 * (slots + 1)` registers starting at `base` (header, then the slots). Blocks may not overlap, start in the program header, or run past DS4500, and the checked-in ones end below the staging area at DS3901. A group header with no cases reserves its block (`timers`, `counters`, `coils`, `numeric`, `text`, `block`, `shift`, `search`, `system` are reserved at DS1101..DS2900); the build ignores it until the suite has that group. Nothing ever moves: a new case takes the next index in its group, a removed case leaves its line (or its gap) behind, a new group takes the next free block. So editing one group never changes another group's hash or any other case's registers.

Entries are added by hand, guided by the build. When a wire case or group has no entry, `build_program` (and so `make test-conformance`) fails with `ManifestError` and prints the exact lines to paste, with the next free index (`b = 4`) or the next free block (`[groups.math]`, `base = 301`, `slots = 19`, and every case). A group that outgrows its slots says so: raise `slots` if the next block allows, or split the group. In-process-only cases need no entry.

## Expectations

`expect` maps a slot field to an int or a `range` (`range(90, 111)` reads as `90..110` in failures). Exact ints are for values that do not depend on how long the harness waited. Over the wire, anything wall-clock dependent (timer `Acc`, scan counts) either stops moving before the harness reads -- observe `Done`, or capture the value once with `oneshot=True` when a condition is met -- or uses a bracket wide enough for scan jitter that both reads fall in. Keep timer presets well above the scan period (hundreds of ms, not tens) and set `scans` so the settle time covers the preset.

A case that cannot run over Modbus passes `in_process_only="reason"`. The Modbus run leaves it out of the program and reports it as skipped with that reason.

`expect_system` maps a CLICK system bit (`"SC46"`) to an expected 0/1 (or a `range`) that the harness reads itself, after the case has run, in-process from the system runtime and over the wire with a Modbus read of the bit. It is for what the ladder cannot record: a PLC in STOP runs no ladder, so whether `SC11` (`_PLC_Mode`) is off has to be read from outside. The case comment lists these with the slot expectations (`Expect: Result1 = 0, SC46 = 1, SC11 = 0`).

## Math errors do not stop the PLC

A CLICK does not stop on a Math error (hardware round 1, finding 14): it writes 0, sets `SC46` for the rest of the scan, and stays in RUN; `SC46` clears at the next scan start, like the other fault flags. pyrung does the same, so `math_error` (`test_math_error.py`, the seven domain errors, one every other scan) is an ordinary group in `suite.ALL_GROUPS` (part A), last so that no other group's rung reads `SC46` on a scan it raised it. The harness still copes with a PLC in STOP (a case that writes `SC50`, the STOP command): in-process, `run()` stops stepping a PLC that is in STOP (`step()` would put it back in RUN) and calls `PLC.start()` before the next reset; the `SimServer` does not scan a PLC in STOP and keeps serving Modbus. The seven retired `stop_*` groups keep their manifest blocks (DS2901..DS3040), never reused.

## 8-step protocol

In-process (`run(group)`; a plain case list also works), twice on one fixed-step `PLC`:

1. Patch every slot to zero. A group's cases run inside its `Conf_<Group>` subroutine, called unconditionally, as on the wire.
2. -- (no case stages inputs; the reset rung initializes it).
3. Put a PLC that a case left in STOP back in RUN (`PLC.start()`), then scan until every slot reads `Ready = 1`, up to 10 s of scans. A case that never does is judged a failure and not fired. After the first reset, snapshot the state; after the second, compare with it (the leak check).
4. Patch `Cmd = 1` on every ready slot.
5. Run `max(case.scans)` scans, or fewer if a case put the PLC in STOP (a PLC in STOP does not scan; the next reset puts it back in RUN).
6. Read every slot from `current_state`.
7. Compare to `expect`. Verify `Fired` went up. Both runs must pass and the reset must leave no state behind (a `pending` case: no run may meet `expect`; see "Pending and probes").
8. Report.

Over Modbus (`run_modbus(build, endpoint, groups=None)`), every step is a register read or write, and only the selected groups' registers are touched:

0. Read `DS1` until it is nonzero (timeout -> `ConformanceError`, "program is not running"). Read `SC11`; if the PLC is in STOP, raise `ConformanceError` ("put the PLC in RUN"). If `DS1` is not this build's layout hash, raise `BuildMismatchError` ("Export the project again and load it") before writing anything. Then read each selected group's hash register. A group whose hash does not match is **stale**: its cases come back with `stale=True` and the note `Conf_Math is stale: repush (DS301 reads ..., this build writes ...)`, its registers are not written, and the other groups carry on.
1. Zero the used slots of every live group (one block write per group, chunked at 100 registers). `Cmd` is the first register of each slot, so it is never left at 1 after its `Fired` is cleared.
2. -- (no case stages inputs; each case's reset rung initializes it while `Cmd = 0`).
3. Poll until every slot reads `Cmd = 0`, `Fired = 0` and `Ready = 1`, or until the timeout. A slot that never does is judged a failure with the note `reset did not complete within 10 s: {'Cmd': .., 'Fired': .., 'Ready': ..}` and is not fired; the other cases carry on. A dropped zeroing write shows up here too (`Cmd` still 1).
4. Write `Cmd = 1` to every ready slot, one single-register write per slot. A block write would rewrite a slot's other registers after its case might already have fired (the sim applies each register as a patch on the next scan, so one request can span scans).
5. Poll every 20 ms until every `Fired != 0`, or until the timeout (default 10 s). A case that never fires is judged a failure, not raised. Then sleep the settle time, `max(case.scans) * 10 ms`.
6. Read every slot (and every system bit a case's `expect_system` names), wait `max(100 ms, settle)`, read them again.
7. A case passes only if it fired and **both** reads meet `expect`. If one read passes and the other does not, the result's `note` says "unstable" with both reads. On a local target a `pending` case passes as `XFAIL` only if neither read meets `expect`.
8. Report. `assert_all_passed` lists stale groups (`STALE: ...`, once per group) and failures; in-process-only skips do not fail. `format_report` gives the per-case lines.

## Pacing over the wire

Over Modbus nothing counts scans, so waits are wall clock: "Fired, then settle, then two reads". `case.scans` becomes settle time at the nominal 10 ms scan. The double read is the stability check: a value still moving when the harness reads fails loudly instead of passing by luck.

The local sim (`SimServer`) is `ClickDataProvider` behind `pyclickplc.server.ClickServer` on a free localhost port, serving a `PLC(realtime=True)` that free-runs on its own thread at one scan per 10 ms of wall clock (late scans are not burst to catch up). Timers therefore see wall-clock time, as on a CLICK, with one exception: a scan advances them by at most `MAX_SCAN_DT_S` (40 ms; `SimServer(max_scan_dt=None)` lifts it). With the whole suite loaded the sim scans every 30-40 ms, and on a busy machine the scan thread stalls 70-190 ms now and then; let through, a stall becomes a scan no CLICK has (they take milliseconds), steps over a case's 80-100 ms windows, and fails cases that are right (the O4 flasher did, about one run in five). Capped, PLC time lags wall time by what the stall took beyond 40 ms, which the harness never sees: it waits on `Fired`, not on the clock. The P1AM-200 target has no cap: its generated code times itself. Modbus reads and writes take the same lock as the scan, and each write becomes a `patch()` applied at the start of the next scan. A scan exception stops the sim and fails the run.

The default 10 s timeout is far above what the sim needs (a debounce coil's 1 s off side is the longest reset, and no case runs past 1 s); it is sized for a real CLICK on a busy network.

## The P1AM-200 target

`CircuitPySim` (`src/pyrung/conformance/_circuitpy.py`) stands where `SimServer` does, but serves the program's generated P1AM-200 code instead of pyrung's engine: `generate(build)` runs `generate_circuitpy()` on the suite with a Modbus server, and `src/pyrung/circuitpy/host.py` runs the resulting `code.py` unchanged in a child Python (`python -I host.py DIR --port N`), with stand-ins for the board: `board`, `digitalio`, `busio`, `P1AM` (every module reads 0), no SD card (retentive memory starts fresh), the RUN/STOP switch in RUN, and the WIZnet socket pool played by ordinary TCP sockets. The generated scan loop, wall-clock timers and Modbus server all run as they would on the board; the harness talks to it exactly as to a CLICK. A child that dies (an exception in a scan) fails the run with its output.

The generated server serves only what the TagMap maps and what rungs use, so every register the harness reads or zeroes must be mapped; that is why slot spares are. Each `run_modbus()` without an endpoint starts a fresh child, as the sim starts a fresh PLC.

What this target cannot see is what only the board does: REAL math in single precision, the CircuitPython language subset, scan timing and memory. The whole suite also makes a 2 MB `code.py`, far too big for the board, so loading it on a real P1AM will take one build per group.

## Running against the real PLC

The harness does not push ladder. CLICK takes it two ways: a whole program through ClickNick's Guided Paste (first load), and changed rungs through ClickNick's Preview Changes (every update after). `tests/conformance/workspace/` is the ClickNick workspace for the conformance project: its committed `.clicknick.toml` links it to the CLICK PLC named `PYRUNG_CONFORMANCE`, and everything ClickNick generates or seeds in it is gitignored (see its `README.md`). pytest, ruff and ty skip the folder (`pyproject.toml`), since ClickNick seeds a smoke test and a `pyproject.toml` there.

First load:

1. `make conformance-export PART=A` (and `PART=B` to `PART=E`, `PART=G` and `PART=H`; `OUT=dir`, default `build/conformance-A`) writes one part as its own project: `main.csv`, `subroutines/Conf_<Group>.csv` per group, and `nicknames.csv`, and prints the layout hash and each group's hash and block. The whole suite (no `PART=`) is more than CLICK's 8000 steps, so a CLICK loads it as seven projects (`suite.PARTS`, split by topic: A and B the instructions, C how a rung is solved -- `rungs`, `solve_order`, `call_twice`, `loop`, `coils`; D and E probes CLICK may refuse at paste or transfer, `ret_branch` and `loop_call`, each kept apart so a refusal costs only that project; G the fault-flag store probes, `faults`, reloaded on their own; H `for_count`, a FOR over a non-finite Real count that may run until the watchdog stops the PLC). `test_build.py` keeps each part's `estimate_steps()` under `suite.PART_STEP_BUDGET` (7500); a part that outgrows it moves a group to another part. The registers are the manifest's either way, so a group's subroutine is the same in its part as in the whole project; only main differs, writing its own part's layout hash to `DS1`.
2. In a new CLICK project, ClickNick **Ladder > Open in Guided Paste...** on `dir`: it imports `nicknames.csv`, then walks through main and each subroutine.
3. In ClickNick's Workspace controls, find an existing workspace and pick `tests/conformance/workspace/`. ClickNick asks a project without a PLC name for one: enter `PYRUNG_CONFORMANCE`. It refuses the folder if the project already has another name; rename the PLC in CLICK.
4. Save in CLICK (ClickNick fills `src/plc/` and `csv/`), write to the PLC, put it in RUN.
5. `make test-conformance PLC=192.168.0.10` (port defaults to 502). The run reads `DS1` and takes the groups of whichever part is loaded (`suite.build_for`); `GROUP=` may name only groups of that part. When the `system` group runs, the run first waits (at most one lap of SD9, 5.5 minutes at 10 ms, and says how long) for the scan counter SD9 to pass 32767, so `system/scan_counter_wraps_to_zero` can tell its answers apart (`wait_for_scan_counter_wrap()`).

A change to the header protocol (`LAYOUT_VERSION`, now 3 for the `Ready` field) changes the layout hash in `DS1`; every run then bails with `BuildMismatchError` until each part is exported and loaded again.

Then, per edit:

1. Edit a group's cases (add a manifest line if `make test-conformance` asks for one).
2. `make conformance-workspace` (or `WORKSPACE=dir` for another folder, `PART=A` to `H` for a part's project) rewrites the workspace's `src/plc/` from the suite: `pyrung_to_ladder` to temporary CSVs, then `ladder_to_pyrung_project` with ClickNick's arguments (`index=True`, the suite's nicknames), swapped in whole. It never touches `csv/`, which is CLICK's accepted snapshot and ClickNick's diff baseline.
3. ClickNick **Preview Changes** shows only the changed rungs: the group's case rungs and its hash rung (R1 of `Conf_<Group>`). **Copy to Click**, paste in CLICK, save, write to the PLC.
4. `make test-conformance PLC=... GROUP=<group>`.

`test_workspace.py` pins what step 3 depends on: the suite exported, regenerated as workspace source, and exported again gives byte-identical CSVs, so an unchanged rung never shows as a diff. New slot nicknames land in the regenerated `src/plc/tags.py`; `clicknick-cli tag apply` stages them in ClickNick's Address Editor (**Changed**, then **Sync**). They only make the ladder readable, so they can wait.

If you forget step 3, that group reports `Conf_<Group> is stale: repush` and its cases do not run; every other group still runs. Adding, removing, or moving a group changes the main program and the layout hash in `DS1`, so Preview Changes shows main's rungs too. A new group's subroutine does not show there at all (ClickNick only diffs subroutines CLICK already has): paste its `subroutines/Conf_<Group>.csv` from `make conformance-export` with Guided Paste, then take main from Preview Changes. The harness self-checks in `test_modbus.py` always use their own local sim. When a real-PLC answer differs from pyrung's, that is a finding: either pyrung's semantics change, or the difference becomes a documented CLICK deviation. A case that passes on the real CLICK is recorded in `confirmed.toml`.

## Coverage checklist

Each line becomes a sentence, a ladder, and a row in the report. `[x]` lines name the cases that cover them (`group/id`); `[ ]` lines are still open, most of them waiting on a harness that does not exist yet (mode switching for STOP->RUN, a subroutine home called from main, send/receive).

### Scan
- [ ] patch values consumed after one scan
- [ ] patch applied before logic, on the same scan
- [ ] forces persist across scans
- [x] fault flags (SC40/SC43/SC44): which later instruction clears them (answered on hardware round 2: SC40 the next Math, SC43/SC44 the next instruction; pending SYS; `system/division_flag_clears_each_scan`, `system/out_of_range_flag_clears_each_scan`, `system/address_flag_clears_each_scan`)
- [x] fault flags after a coil, an open rung, a timer, a counter (SC43/SC44), and after a hex-mode Math (SC40) (answered on hardware 2026-10-31 for SC44: none of the four clears it, `system/address_flag_vs_other_instructions`; a Dint Math wrap raises no SC43 on a CLICK, pending SYS, `system/out_of_range_flag_vs_other_instructions`, so the probe `system/out_of_range_int_vs_other_instructions` asks again with an Int overflow; `system/division_flag_vs_hex_math`)
- [x] which stores raise SC43 (probes, part G): a Math whose result does not fit a narrower destination, not a 32-bit wrap (`faults/math_store_out_of_range`, pending SYS); a Copy that clamps (`faults/copy_clamp_out_of_range`, pending SYS); RAND past an Int and with Real bounds (`faults/rand_wide_bounds_flags`, `faults/rand_real_bounds`, pending MATH)
- [x] a Copy through a pointer past either end of its bank: SC44, destination untouched (probe; `faults/pointer_past_bank`)
- [x] Inf and NaN in a Real (probes, part G): ^ overflow (`faults/power_overflow_is_inf`), float32 intermediates (`faults/real_intermediate_range`), Inf/NaN arithmetic and NaN compares (`faults/inf_and_nan_arithmetic`), Math of Inf/NaN into an Int or a Dint (`faults/non_finite_math_into_int`), Copy of Inf/NaN into an Int (`faults/copy_non_finite_to_int`, pending SYS) and into text (`faults/copy_inf_to_text`), SIN(Inf) and SQRT(-Inf) (`faults/non_finite_functions`, pending MATH)
- [ ] a Modbus Send/Receive Slave ID out of range (SC43): needs a second Modbus device; a DF holding Inf or NaN read over Modbus or on the display: the harness reads slot DS only
- [x] first scan after STOP->RUN: SD9 = 0 with SC3 on (`system/first_scan_counter_and_clock`, answered on hardware round 2); SD9's wrap (answered on hardware; `system/scan_counter_wraps_to_zero`)
- [x] edge and counter memory at STOP->RUN (answered on hardware; `coils/edge_on_first_scan`, `counters/first_scan_input_already_on`)
- [ ] first_scan true once, on scan 0
- [ ] first_scan re-triggers on STOP->RUN
- [x] scan clock on when SD9 is even (`system/scan_clock_follows_counter`)
- [x] scan_counter steps by one every scan (`system/scan_counter_steps_by_one`)
- [ ] scan_counter resets on STOP->RUN
- [x] clock toggles fire at their nominal rate (`system/clock_bits_toggle_at_their_rate`)
- [x] mode_run readable from ladder (`system/mode_bits_on_in_run`), still on after a math error (read by the harness: `math_error/math_errors_do_not_stop`)
- [x] RTC advances while the ladder runs (`system/rtc_seconds_advance`)
- [ ] RTC set from the ladder (SC53/SC55) applies
- [ ] STOP->RUN clears scan_id and scan_counter

### Rung semantics
- [x] rows solve live, top to bottom: a branch reads its contacts when the solve reaches it (`rungs/branch_sees_rung_entry`, pending S4)
- [x] CLICK refuses a box below another output on one wire (C000A: all inputs (pins) of a box must start from the same branch), so a box's legs never read a row above it in the same wire (`rungs/box_leg_reads_rows_above` and the `solve_order` box-leg cases retired)
- [x] source order within a rung (`rungs/memory_is_immediate`)
- [x] branches AND with parent (`rungs/branch_and_with_parent`)
- [x] nested branches read live too (`rungs/nested_branches_see_rung_entry`, pending S4)
- [x] last rung wins on shared coil (`rungs/last_rung_wins_shared_coil`)
- [x] a continued rung is one more row, read live (`rungs/continued_shares_snapshot`, `rungs/continued_chain_breaks`, pending S4)
- [x] solve order: every topology where an earlier part of a rung writes what a later part reads, each annotated order-dependent or not (`ORDER_DEPENDENT` in `test_solve_order.py`, which also maps the four `rungs` live-read cases; the order-dependent ones are pending S4)
- [x] rows in series read the rows above (`solve_order/row_series_reads_rows_above`, pending S4); a row does not see a row below, and the rung is solved once (`solve_order/row_reads_row_below`)
- [x] sibling branches inside a branch, and a later branch reading an inner branch's write (`solve_order/sibling_branch_reads_sibling_above`, `solve_order/outer_row_reads_inner_branch_write`, pending S4)
- [x] an OR on the rail is solved once, before the outputs (`solve_order/input_or_legs_read_before_outputs`); an OR on a branch reads live (`solve_order/branch_or_reads_row_above`, pending S4)
- [x] output bus: an operand sees the coil above it (`solve_order/bus_operand_sees_coil`); the last coil write wins (`solve_order/bus_coils_last_write_wins`)
- [x] a wire does not see a later continued wire's write (`solve_order/continued_reads_later_wire`)
- [x] a box leg does not see a continued wire below the box (answered on hardware; `solve_order/box_leg_reads_row_below`)
- [x] For body: a body rung sees the body rung above it in the same pass (`solve_order/for_body_rung_reads_body_rung_above`)
- [ ] solve order around a `call()` between the rows of one rung (the home exists, `in_main=True`, but no such case yet); a branch row in a For body rung (pyrung refuses `branch()` inside `forloop()`)
- [ ] empty rung (NOP): nothing to observe. CLICK refuses a contact before NOP and deletes a NOP row that shares its rung (so `rungs/nop_rung_takes_snapshot` is retired); the exporter refuses both, and a bare NOP round-trips (`tests/click/test_codegen.py`)
- [x] subroutines: same rules (every case runs inside its group's subroutine)
- [x] a subroutine called twice a scan: an edge contact fires once per edge (answered on hardware; `call_twice/rise_once_per_scan`, pending S3)
- [x] call into a subroutine from a For body (probe; `loop_call/call_inside_loop`; a call from a group's own subroutine is nested, which CLICK refuses and `CLK_NESTED_CALL` flags)
- [ ] subroutine memory writes visible to the caller after the call
- [x] forloop runs N times in one scan (`loop/loop_runs_n_times`)
- [x] forloop body sees its own prior-iteration writes (`loop/loop_runs_n_times`)
- [ ] forloop loop.idx indirect addressing
- [x] forloop over a Real count: 3.0, +Inf, NaN (probe, part H; `for_count/non_finite_count`)
- [x] forloop dynamic count == 0: no iteration (answered on hardware; 65536 was the other reading) (`loop/zero_count`); -1 runs 65535 (unsigned 16-bit) (`loop/negative_count_reads_unsigned`, in-process only)
- [x] forloop (`loop` group, F1-F14, F17-F19): a false For rung skips its body (F2); count read as unsigned 16-bit (F3); count read once (F4); body-rung contacts per iteration (F5); edges inside a loop (F6, answered, pending S3); timers accumulate per iteration (F7); counters count edges across iterations (F8, answered); Break leaves the innermost loop (F9) after the earlier body rungs (F10); Continue skips the rest of one iteration (F11, F12); a Break contact sees same-iteration writes (F13); a one-shot re-arms on an iteration its rung is false (F14); a body one-shot holds while its For rung is false (F17); an edge contact's memory across iterations of one scan (F18, answered, pending S3); a timer and a counter in a skipped body hold, reset unscanned (F19, answered)
- [ ] forloop inner one-shot: does CLICK reject it (C0111) or ignore it (F15); a loop in a called subroutine counts toward depth 3 (F16, probe at depth exactly 3, `loop_call/loop_in_called_subroutine`; depth 4 is refused by the exporter, so CLICK's own C010D is not probed); body-rung contacts per iteration around a call (`loop_call/body_contact_after_call`)

### Memory
- [x] rung N+1 sees rung N's writes, same scan (`rungs/global_mutation`)
- [x] Bool default False (`rungs/branch_and_with_parent`)
- [ ] Int default 0
- [ ] per-slot default override
- [ ] default_factory by address
- [ ] Bool non-retentive by default
- [ ] Int / Dint / Real retentive by default
- [ ] retentive survives STOP->RUN
- [ ] non-retentive resets on STOP->RUN
- [ ] both reset on cold (no battery) reboot

### Numerical handling
- [x] Int wrap at 32767 (`numeric/int_calc_wraps`)
- [x] Dint wrap at 2^31 (`numeric/dint_calc_wraps`)
- [x] Word wrap at 65535 (`numeric/word_calc_wraps`)
- [x] Real precision: pyrung keeps 64 bits, CLICK 32 (`numeric/real_is_64_bit_in_pyrung`, expected to fail on a CLICK: a documented deviation)
- [x] Real range: an overflow stores +/-Inf with SC43 (`numeric/real_overflow_stays_finite`)
- [x] a Dint quotient into an Int: a Math constant into an Int is cut to 16 bits (`numeric/dint_quotient_into_int`)
- [x] an Int Math constant in 32768..65535 or below -32768, a float constant, and RAND bounds past 16 bits (probes; `numeric/int_math_wide_constant`, `numeric/int_math_rand_wide_bounds` (answered on hardware 2026-10-31: 0, pending MATH), `numeric/int_math_real_operand_constant`)
- [x] division by zero -> 0 + fault (`numeric/div_by_zero`)
- [x] a REAL division by zero (1/0, -1/0, 0/0) -> 0 + SC40, not +/-Inf or NaN (`numeric/real_div_by_zero`)
- [x] math domain error (sqrt, ln/log, asin/acos) -> 0 + SC46 for the rest of the scan, and the PLC stays in RUN (`math_error/math_errors_do_not_stop`)
- [x] copy clamps, calc wraps (`numeric/copy_clamps_calc_wraps`)
- [x] copy argument order is source -> dest (`numeric/copy_source_to_dest`)
- [x] calc mode inference (hex vs decimal) (`numeric/word_math_unsigned`)
- [x] calc WORD-only infers hex mode (`numeric/word_math_unsigned`, `numeric/hex_literal_is_decimal`)
- [ ] calc mixing WORD and non-WORD: validator finding CLK_CALC_MODE_MIXED (a static finding, not a ladder case)
- [x] Math intermediates are wider than 32 bits, on a CLICK too (`numeric/intermediate_exceeds_32_bits`, `numeric/intermediate_fits_32_bits`)

### Per instruction
- [x] out follows rung (`coils/out_follows_rung`)
- [x] out oneshot fires for one scan only (`coils/out_oneshot_fires_one_scan`)
- [x] out oneshot re-fires after rung goes False then True again (`coils/out_oneshot_refires`)
- [x] rise / fall fire one scan (`coils/rise_fall_fire_one_scan`)
- [x] each edge contact keeps its own memory, not the bit's end-of-scan value (`coils/edge_sees_previous_scan_end`, `coils/edge_before_its_writer`; in a loop and across calls: `loop/edge_contact_inside_loop`, `loop/edge_memory_across_iterations`, `call_twice/rise_once_per_scan`)
- [x] edge memory behind an open contact and in a false rung's branch (answered on hardware; `coils/edge_behind_open_contact`, `coils/edge_in_false_rung_branch`)
- [x] latch / reset stickiness (`coils/latch_reset_sticky`)
- [x] toggle flips once per OFF->ON and holds (O1) (`out/toggle_flips_once_per_edge`)
- [x] debounce TD keeps counting past its preset while steady (O2); a bounce restarts the timing side (O3) (`out/debounce_td_keeps_counting`, `out/debounce_bounce_restarts_on_side`)
- [x] flasher with its rung false: TDs kept and frozen, and the restart is a fresh on phase (O4, answered on hardware round 2; `out/flasher_restarts_with_on_phase`)
- [x] timed (not maintained) needs a fresh edge after timing out (O5) (`out/timed_needs_fresh_edge`)
- [x] Out coil variants never set the phase timer's T bit (O6) (`out/out_coil_sets_timer_done`)
- [x] TON auto-reset, drops Acc and Done together when rung goes false (`timers/ton_auto_reset`)
- [x] TON enable: the enabling scan's dt counts and Done is judged on that scan, so a 0 ms preset is done on the scan its rung goes true (hardware-verified per `tests/core/test_timers.py`; `timers/ton_done_on_enabling_scan`)
- [x] RTON holds Acc and Done across rung-false (`timers/rton_holds_across_rung_false`)
- [x] RTON reset sets Acc=0, Done=False, frac=0 and does not judge Done again on the reset scan (`timers/rton_reset_gap`)
- [x] TOF never-enabled: Done stays False and Acc 0 while the rung has never been True, unlike a TOF whose rung fell (`timers/tof_never_enabled`)
- [x] TOF: Done True and Acc 0 while rung True; Acc counts only when rung false (`timers/tof_times_only_when_false`)
- [x] TOF enable sets Done=True, Acc=0, frac=0 in a single atomic write -- no transition scan where Done is stale (`timers/tof_enable_atomic`)
- [x] TOF Done flips False when Acc reaches preset (`timers/tof_times_only_when_false`)
- [x] timer Acc clamp at 32767 (`timers/acc_clamps_at_32767`, TD seeded at 32000)
- [x] timer Acc continues past preset up to 32767 clamp (`timers/acc_clamps_at_32767`, `timers/tof_times_only_when_false`)
- [x] timer fractional ms accumulation across scans (`timers/fractional_accumulation`)
- [x] timer resumes if preset raised mid-count (`timers/preset_raised_resumes`)
- [x] timer fires immediately if preset lowered below Acc (`timers/preset_lowered_fires_at_once`)
- [x] timer preset resolved dynamically each tick (the two preset cases above)
- [x] counter counts rising edges of its inputs, not scans; a held input is not counted again after a reset (`counters/level_vs_edge`, `counters/held_input_not_recounted`, pending S2)
- [x] counter edge on a reset scan; up and down edges on one scan (answered on hardware; `counters/edge_on_reset_scan`, `counters/up_down_same_scan`)
- [x] counter resumes if preset raised mid-count (`counters/preset_raised_resumes`)
- [x] counter survives rung going false (no auto-reset) (`counters/survives_rung_false`)
- [x] counter Acc clamp at Dint range (`counters/acc_clamps_at_dint_range`)
- [x] counter reset sets Done=False, Acc=0 and returns; Done is recomputed against the preset on the very next scan, counting or not, as CLICK's help says of a down counter ("CT1 is turned on immediately after [Reset] is turned off", `clickplc_reference/counters.md`) (`counters/reset_zero_preset`)
- [x] counter preset lowered while idle: Done turns on without a count (undocumented by CLICK; `counters/preset_lowered_while_idle` is the probe)
- [x] counter reset with preset<=0: Done stays False for one scan even though Acc>=preset (`counters/reset_zero_preset`)
- [x] counter preset resolved dynamically each scan (`counters/preset_raised_resumes`, `counters/preset_lowered_while_idle`)
- [x] count_down starts at 0, counts negative, done at Acc <= preset (`counters/count_down_negative`, answered: `counters/count_down_done_at_preset`)
- [x] copy converters: to_value, to_ascii, to_text, to_binary (`text/value_vs_ascii`, `text/to_text_suppress`, `text/to_binary`)
- [x] to_value vs to_ascii on the same CHAR ('5' -> 5 vs 53) (`text/value_vs_ascii`)
- [x] to_ascii of default/null TXT ($00) writes 0 to destination (not a no-op) (`text/null_ascii`); to_value of NUL writes 0 (`text/to_value_rejects`, pending TEXT)
- [x] to_text suppress_zero default vs fixed-width (`text/to_text_suppress`, `text/to_text_fixed`, `text/to_text_fixed_negative`)
- [x] to_text with termination_code appends one char after the digits (`text/to_text_termination`)
- [x] copy literal string fans out across consecutive txt slots (`text/literal_fanout`)
- [ ] blockcopy only supports to_value / to_ascii converters
- [x] blockcopy length match (`block/blockcopy_range`)
- [x] fill writes constant (`block/fill_constant`)
- [x] pack_bits / unpack_to_bits (`block/pack_bits_int`, `block/unpack_bits`)
- [x] pack_bits into REAL stores the raw IEEE-754 bit pattern (`block/pack_bits_real`)
- [x] pack_words / unpack_to_words (`block/pack_words`, `block/unpack_words`)
- [x] pack_words first source is the low word (`block/pack_words`)
- [x] pack_text into WORD parses hex; into INT/DINT parses signed decimal (`text/pack_text_word_hex`, `text/pack_text_int`, `text/pack_text_dint`)
- [x] pack_text on numeric with leading whitespace faults unless allow_whitespace (`text/pack_text_whitespace`)
- [x] pack_text into INT/DINT clamps with the out-of-range fault, stops at a point and refuses any other non-digit with the fault (`text/pack_text_int`, `text/pack_text_dint`, `text/pack_text_int_fraction`, answered on hardware round 2); TXT is 8-bit (`text/to_binary`)
- [x] copy/fill executes every scan while rung true (not edge-triggered) (`block/every_scan`)
- [x] shift register direction by range order (`shift/forward_range_shifts_up`; `shift/reversed_range_shifts_down` in-process only)
- [x] shift register: reset takes precedence over clock if both true same scan (`shift/reset_beats_clock`, `shift/reset_swallows_the_edge`)
- [x] shift register: rung power is the shift-in value (`shift/data_is_the_rung_at_the_edge`)
- [x] drum events are edge-based (`drum/events_are_edges`)
- [x] time & event drum events are levels: one already on ends the step when its time is up (D3) (`drum/te_level_event_already_on`)
- [x] time & event step keeps timing past its duration while it waits for the event (D1) (`drum/te_acc_counts_while_waiting`)
- [x] drum reset wins, jump beats jog; a paused drum takes jog, jump and reset and leaves its outputs (`drum/reset_jump_jog_order`, `drum/paused_drum_jogs_and_resets`, answered: `drum/paused_drum_jumps`)
- [x] drum reset is a level that turns every output off (`drum/reset_no_stale_outputs`); a jog rising on a reset scan is spent (answered on hardware; `drum/reset_swallows_held_jog`)
- [x] drum completion turns its outputs off; a jog brings them back, a jump does not (`drum/completion_turns_outputs_off`)
- [x] drum jump/jog do NOT clear completion flag -- only reset does (`drum/jump_jog_keep_completion`)
- [x] drum jump/jog are edge-triggered (no double-fire on sustained condition) (`drum/jump_jog_edge_triggered`)
- [x] drum time accumulator resets on step transition (jump, jog, advance, reset) (`drum/time_acc_clears_on_every_transition`)
- [x] drum event_ready re-evaluates on step change (`drum/events_are_edges`, `drum/te_level_event_already_on`)
- [x] drum step auto-corrects to 1 if current_step tag holds an invalid value (`drum/invalid_step_corrects_to_1`)
- [x] a jog on an invalid step: the drum completes, outputs off, at step 1 (answered on hardware 2026-10-31, pending DRUM; `drum/pins_read_before_step_fix`, its tee twin); the correction alone, a jump to the drum's own step register, a jog on the last step (probes; `drum/correction_without_jog`, `drum/jump_to_own_step_register`, `drum/jog_on_last_step`, the last pending DRUM)
- [x] search hit / miss / continuous (`search/hit_returns_address`, `search/miss_writes_minus_one`, `search/continuous_walks`)
- [x] search returns the register number of the match, -1 on miss (`search/hit_returns_address`, `search/miss_writes_minus_one`)
- [x] search continuous resumes after an old result inside the range; result=0 restarts; anything else misses (`search/continuous_walks`, `search/continuous_zero_restarts`, `search/continuous_cursor_is_address`, answered: `search/continuous_cursor_outside_range`)
- [ ] search over a reversed range: the exporter writes it as an address list, which CLICK's Search does not take
- [x] search rung false preserves result and found (`search/false_rung_preserves`)
- [ ] send / receive status tags
- [ ] send/receive success and error pulse for one scan
- [ ] send/receive status tags reset to defaults when rung goes false
- [ ] send/receive values applied atomically
- [ ] send sparse addresses split into multiple writes
- [ ] exception_response holds last code until next transaction
