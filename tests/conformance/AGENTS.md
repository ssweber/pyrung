# Conformance Suite

Plain-English cases for PLC semantics. Same case, same ladder, same check -- run in-process, over Modbus against pyrung's soft PLC, or over Modbus against a real CLICK.

A conformance suite in the Modbus sense: the harness observes the implementation **only through registers**. The ladder computes its own answer and writes it into its slot; the harness writes `Cmd`, waits, and reads the slot back.

The whole suite is **one CLICK program**: a main program that calls one subroutine per group (`Conf_Rungs`, later `Conf_Math`, `Conf_Drum`, ...). Load the full project once; after that, editing a group means pasting its changed rungs (through the ClickNick workspace, see "Running against the real PLC") and rerunning that group.

| Run | Where | How | Pacing |
|---|---|---|---|
| In-process | `make test` (unmarked) | `run(group)` on a fixed-step `PLC` | exact scan counts |
| Modbus, local sim | `make test-conformance` (marker `conformance`) | `run_modbus(suite.build())` | wall clock |
| Modbus, real CLICK | `make test-conformance PLC=host[:port]` (or `--conformance-plc`, or env `PYRUNG_CONFORMANCE_PLC`) | `run_modbus(suite.build(), "host:port")` | wall clock |

Add `GROUP=name[,name...]` (or `--conformance-group`, or env `PYRUNG_CONFORMANCE_GROUP`) to run some groups only. The two Modbus runs share every line of client code; only the endpoint differs. `-rP` prints the report: one line per case (`PASS`/`FAIL`/`SKIP`/`STALE`, `[confirmed]`/`[unconfirmed]`) and totals.

## What a test is

1. A sentence that says what should be true.
2. A pyrung ladder that proves it by writing the answer into a known slot.
3. A check that reads the slot and compares to what we expected.

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
- **Case id**: the stable key of the case's slot in the manifest. It defaults to the ladder function's name without a leading `_` or `prove_` (`prove_source_order` -> `source_order`); pass `id=` to pin it. Renaming the ladder function renames the id, and the build then asks for a manifest entry -- pin `id=` if you rename.
- **confirmed**: `case(..., confirmed=False)` by default. Flip it to `True` once the case has passed against a real CLICK. The report counts unconfirmed cases.

## One-shot pattern

Every test ladder gates on `slot.Cmd != 0, slot.Fired == 0` and sets `copy(1, slot.Fired)` in its last rung. This ensures instructions fire exactly once:

- Scan 0: `Cmd = 0`, nothing fires.
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

## Program and register layout

```
main:           copy(<layout hash>, DS1)         every scan
                call("Conf_Rungs")               every scan, one rung per group
                ...
Conf_Rungs:     copy(<group hash>, DS101)        every scan, first rung
                <case rungs, each gated by its slot's Cmd/Fired>
                return()
```

| Registers | Holds |
|---|---|
| `DS1` | Layout hash (1..32767): the group table (subroutine names and bases) and `LAYOUT_VERSION`. Changes only when a group is added, removed, renamed, or moved. |
| `DS2`..`DS10` | Spare program header. |
| `DS<base>` | Group hash (1..32767): SHA-256 of the CLICK export of the group's case rungs, folded into a register. Changes whenever that subroutine's exported ladder would. |
| `DS<base+1>`..`DS<base+9>` | Spare group header. |
| `DS<base+10N>`..`DS<base+10N+7>` | Slot N of the group; the last two registers of each 10 are spare. |

Each slot is a `named_array` instance with 8 Int fields:

| Field     | Offset | `rungs` slot 1 | Purpose                  |
|-----------|--------|--------|--------------------------|
| Cmd       | +0     | DS111  | Trigger (harness writes) |
| Scratch   | +1     | DS112  | Temporary storage        |
| Result1-4 | +2..+5 | DS113-116 | Outputs to check      |
| Fired     | +6     | DS117  | One-shot gate            |
| ErrorCode | +7     | DS118  | Error reporting          |

Cases cannot share memory. A case that needs a contiguous source range (the `math` group's `AVG`/`MDN`/`MAX`/`MIN`) stages it in raw registers it owns, outside every group block: the `math` group uses DS4401..DS4500 (its test checks that no manifest block reaches them) and DD, DF and DH, which no other group uses. `build_program(groups, manifest)` returns the program, its TagMap, the layout hash, and one `GroupBuild` per group (subroutine, block, slots, group hash). The build exports each group's subroutine through the validating CLICK exporter, so a case the exporter rejects fails at build time -- mark it in-process-only. Without a manifest, `build_program` packs the groups in order from DS11 (`Manifest.packed`): fine for harness tests and the local sim, never for a real CLICK, because adding a case moves registers.

CLICK does not support nested subroutine calls (confirmed in the CLICK editor), so a case cannot `call()` a subroutine of its own: its rungs already run inside `Conf_<Group>`. Subroutine tests must be called from main instead; that home is not built yet.

Unconditional rungs (an output wired straight to the rail, as in main's `copy`/`call` rungs and each group's hash rung) are valid in CLICK (confirmed).

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

A block is `10 * (slots + 1)` registers starting at `base` (header, then the slots). Blocks may not overlap, start in the program header, or run past DS4500. Nothing ever moves: a new case takes the next index in its group, a removed case leaves its line (or its gap) behind, a new group takes the next free block. So editing one group never changes another group's hash or any other case's registers.

Entries are added by hand, guided by the build. When a wire case or group has no entry, `build_program` (and so `make test-conformance`) fails with `ManifestError` and prints the exact lines to paste, with the next free index (`b = 4`) or the next free block (`[groups.math]`, `base = 301`, `slots = 19`, and every case). A group that outgrows its slots says so: raise `slots` if the next block allows, or split the group. In-process-only cases need no entry.

## Expectations

`expect` maps a slot field to an int or a `range` (`range(90, 111)` reads as `90..110` in failures). Exact ints are for values that do not depend on how long the harness waited. Over the wire, anything wall-clock dependent (timer `Acc`, scan counts) either stops moving before the harness reads -- observe `Done`, or capture the value once with `oneshot=True` when a condition is met -- or uses a bracket wide enough for scan jitter that both reads fall in. Keep timer presets well above the scan period (hundreds of ms, not tens) and set `scans` so the settle time covers the preset.

A case that cannot run over Modbus passes `in_process_only="reason"`. The Modbus run leaves it out of the program and reports it as skipped with that reason.

## 8-step protocol

In-process (`run(group)`; a plain case list also works):

1. Zero every slot (fresh `PLC`). A group's cases run inside its `Conf_<Group>` subroutine, called unconditionally, as on the wire.
2. Stage inputs with `Cmd = 0` so nothing fires (scan 0).
3. -- (nothing can be dropped in-process).
4. Patch `Cmd = 1` everywhere.
5. Run `max(case.scans)` scans.
6. Read every slot from `current_state`.
7. Compare to `expect`. Verify `Fired` went up.
8. Report.

Over Modbus (`run_modbus(build, endpoint, groups=None)`), every step is a register read or write, and only the selected groups' registers are touched:

0. Read `DS1` until it is nonzero (timeout -> `ConformanceError`, "program is not running"). If it is not this build's layout hash, raise `BuildMismatchError` ("Export the whole project again and load it") before writing anything. Then read each selected group's hash register. A group whose hash does not match is **stale**: its cases come back with `stale=True` and the note `Conf_Math is stale: repush (DS301 reads ..., this build writes ...)`, its registers are not written, and the other groups carry on.
1. Zero the used slots of every live group (one block write per group, chunked at 100 registers). `Cmd` is the first register of each slot, so it is never left at 1 after its `Fired` is cleared.
2. Stage inputs with `Cmd = 0`. No case stages inputs yet; this is the zeroing.
3. Read the slots back until they are all zero (timeout -> `ConformanceError`, "writes were dropped").
4. Write `Cmd = 1`, one single-register write per slot. A block write would rewrite a slot's other registers after its case might already have fired (the sim applies each register as a patch on the next scan, so one request can span scans).
5. Poll every 20 ms until every `Fired != 0`, or until the timeout (default 10 s). A case that never fires is judged a failure, not raised. Then sleep the settle time, `max(case.scans) * 10 ms`.
6. Read every slot, wait `max(100 ms, settle)`, read every slot again.
7. A case passes only if it fired and **both** reads meet `expect`. If one read passes and the other does not, the result's `note` says "unstable" with both reads.
8. Report. `assert_all_passed` lists stale groups (`STALE: ...`, once per group) and failures; in-process-only skips do not fail. `format_report` gives the per-case lines.

## Pacing over the wire

Over Modbus nothing counts scans, so waits are wall clock: "Fired, then settle, then two reads". `case.scans` becomes settle time at the nominal 10 ms scan. The double read is the stability check: a value still moving when the harness reads fails loudly instead of passing by luck.

The local sim (`SimServer`) is `ClickDataProvider` behind `pyclickplc.server.ClickServer` on a free localhost port, serving a `PLC(realtime=True)` that free-runs on its own thread at one scan per 10 ms of wall clock (late scans are not burst to catch up). Timers therefore see wall-clock time, as on a CLICK. Modbus reads and writes take the same lock as the scan, and each write becomes a `patch()` applied at the start of the next scan. A scan exception stops the sim and fails the run.

The default 10 s timeout is far above what the sim needs (a whole run is about 0.3 s); it is sized for a real CLICK on a busy network.

## Running against the real PLC

The harness does not push ladder. CLICK takes it two ways: a whole program through ClickNick's Guided Paste (first load), and changed rungs through ClickNick's Preview Changes (every update after). `tests/conformance/workspace/` is the ClickNick workspace for the conformance project: its committed `.clicknick.toml` links it to the CLICK PLC named `PYRUNG_CONFORMANCE`, and everything ClickNick generates or seeds in it is gitignored (see its `README.md`). pytest, ruff and ty skip the folder (`pyproject.toml`), since ClickNick seeds a smoke test and a `pyproject.toml` there.

First load:

1. `make conformance-export OUT=dir` writes the whole project: `main.csv`, `subroutines/Conf_<Group>.csv` per group, and `nicknames.csv`, and prints the layout hash and each group's hash and block.
2. In a new CLICK project, ClickNick **Ladder > Open in Guided Paste...** on `dir`: it imports `nicknames.csv`, then walks through main and each subroutine.
3. In ClickNick's Workspace controls, find an existing workspace and pick `tests/conformance/workspace/`. ClickNick asks a project without a PLC name for one: enter `PYRUNG_CONFORMANCE`. It refuses the folder if the project already has another name; rename the PLC in CLICK.
4. Save in CLICK (ClickNick fills `src/plc/` and `csv/`), write to the PLC, put it in RUN.
5. `make test-conformance PLC=192.168.0.10` (port defaults to 502).

Then, per edit:

1. Edit a group's cases (add a manifest line if `make test-conformance` asks for one).
2. `make conformance-workspace` (or `WORKSPACE=dir` for another folder) rewrites the workspace's `src/plc/` from the suite: `pyrung_to_ladder` to temporary CSVs, then `ladder_to_pyrung_project` with ClickNick's arguments (`index=True`, the suite's nicknames), swapped in whole. It never touches `csv/`, which is CLICK's accepted snapshot and ClickNick's diff baseline.
3. ClickNick **Preview Changes** shows only the changed rungs: the group's case rungs and its hash rung (R1 of `Conf_<Group>`). **Copy to Click**, paste in CLICK, save, write to the PLC.
4. `make test-conformance PLC=... GROUP=<group>`.

`test_workspace.py` pins what step 3 depends on: the suite exported, regenerated as workspace source, and exported again gives byte-identical CSVs, so an unchanged rung never shows as a diff. New slot nicknames land in the regenerated `src/plc/tags.py`; `clicknick-cli tag apply` stages them in ClickNick's Address Editor (**Changed**, then **Sync**). They only make the ladder readable, so they can wait.

If you forget step 3, that group reports `Conf_<Group> is stale: repush` and its cases do not run; every other group still runs. Adding, removing, or moving a group changes the main program and the layout hash in `DS1`, so Preview Changes shows main's rungs too. A new group's subroutine does not show there at all (ClickNick only diffs subroutines CLICK already has): paste its `subroutines/Conf_<Group>.csv` from `make conformance-export` with Guided Paste, then take main from Preview Changes. The harness self-checks in `test_modbus.py` always use their own local sim. When a real-PLC answer differs from pyrung's, that is a finding: either pyrung's semantics change, or the difference becomes a documented CLICK deviation. When a case passes on the real CLICK, set `confirmed=True`.

## Coverage checklist

Each line becomes a sentence, a ladder, and a row in the report.

### Scan
- patch values consumed after one scan
- patch applied before logic, on the same scan
- forces persist across scans
- fault flags (SC40/SC43/SC44) auto-clear at scan start
- fault flags auto-clear only if NOT retriggered same scan
- first_scan true once, on scan 0
- first_scan re-triggers on STOP->RUN
- scan clock initial state is False on first scan
- scan_counter resets on STOP->RUN
- clock toggles fire at their nominal rate under fixed dt
- mode_run readable from ladder, false in STOP
- RTC advances with sim time after set_rtc
- STOP->RUN clears scan_id and scan_counter

### Rung semantics
- rung snapshot on entry
- conditions before instructions
- source order within a rung
- branches AND with parent
- nested branches all see the same rung-entry snapshot
- last rung wins on shared coil
- continued rungs share snapshot
- continued chain breaks on a normal rung
- empty rung (NOP) executes and survives round-trip
- subroutines: same rules
- call into subroutine (not inside a group: CLICK has no nested calls)
- subroutine memory writes visible immediately
- forloop runs N times in one scan
- forloop body sees its own prior-iteration writes
- forloop loop.idx indirect addressing
- forloop dynamic count == 0: pin down whether Click executes once, skips, or faults

### Memory
- rung N+1 sees rung N's writes, same scan
- Bool default False
- Int default 0
- per-slot default override
- default_factory by address
- Bool non-retentive by default
- Int / Dint / Real retentive by default
- retentive survives STOP->RUN
- non-retentive resets on STOP->RUN
- both reset on cold (no battery) reboot

### Numerical handling
- Int wrap at 32767
- Dint wrap at 2^31
- Word wrap at 65535
- Real precision (32-bit)
- division by zero -> 0 + fault
- non-finite float (NaN, +/-Inf) -> 0 + fault
- copy clamps, calc wraps
- copy argument order is source -> dest
- calc mode inference (hex vs decimal)
- calc WORD-only infers hex mode
- calc mixing WORD and non-WORD: validator finding CLK_CALC_MODE_MIXED
- pyrung uses Python-precision intermediates, Click uses 32-bit
- agree when intermediates fit in 32 bits
- disagree when intermediates exceed 32 bits — measured by harness, not assumed

### Per instruction
- out follows rung
- out oneshot fires for one scan only (True on rising edge, False while rung stays True)
- out oneshot re-fires after rung goes False then True again
- rise / fall fire one scan
- rise/fall see the value carried from end of previous scan
- latch / reset stickiness
- TON auto-reset, drops Acc and Done together when rung goes false
- TON enable: no immediate tick on the scan that enables — Done stays whatever it was before until the next tick
- RTON holds Acc and Done across rung-false
- RTON reset sets Acc=0, Done=False, frac=0 — same one-scan gap as counter reset
- TOF never-enabled: Done stays False when rung has never been True
- TOF: Done True and Acc 0 while rung True; Acc counts only when rung false
- TOF enable sets Done=True, Acc=0, frac=0 in a single atomic write — no transition scan where Done is stale
- TOF Done flips False when Acc reaches preset
- timer Acc clamp at 32767
- timer Acc continues past preset up to 32767 clamp
- timer fractional ms accumulation across scans
- timer resumes if preset raised mid-count
- timer fires immediately if preset lowered below Acc
- timer preset resolved dynamically each tick (same as counter — no stale window)
- counter increments every scan the rung is true (not per edge)
- counter resumes if preset raised mid-count
- counter survives rung going false (no auto-reset)
- counter Acc clamp at Dint range
- counter reset sets Done=False, Acc=0 and returns — Done is NOT recomputed against preset until the next counting scan
- counter reset with preset<=0: Done stays False for one scan even though Acc>=preset
- counter preset resolved dynamically each scan (no stale window — preset change takes effect on the very next scan that hits the counting branch)
- count_up edge vs level
- count_down starts at 0, counts negative
- copy converters: to_value, to_ascii, to_text, to_binary
- to_value vs to_ascii on the same CHAR ('5' -> 5 vs 53)
- to_ascii of default/null TXT ($00) writes 0 to destination (not a no-op)
- to_text suppress_zero default vs fixed-width
- to_text with termination_code appends one char after the digits
- copy literal string fans out across consecutive txt slots
- blockcopy only supports to_value / to_ascii converters
- blockcopy length match
- fill writes constant
- pack_bits / unpack_to_bits
- pack_bits into REAL stores the raw IEEE-754 bit pattern
- pack_words / unpack_to_words
- pack_words first source is the low word
- pack_text into WORD parses hex; into INT/DINT parses signed decimal
- pack_text on numeric with leading whitespace faults unless allow_whitespace
- copy/fill executes every scan while rung true (not edge-triggered)
- shift register direction by range order
- shift register: reset takes precedence over clock if both true same scan
- shift register: rung power is the shift-in value
- drum events are edge-based
- drum pause/reset/jump precedence order
- drum reset sets step=1, completion=False, then applies outputs for step 1 — no stale output scan
- drum jump/jog do NOT clear completion flag — only reset does
- drum jump/jog are edge-triggered (no double-fire on sustained condition)
- drum time accumulator resets on step transition (jump, jog, advance, reset)
- drum event_ready re-evaluates on step change (new step's event may already be True)
- drum step auto-corrects to 1 if current_step tag holds an invalid value
- search hit / miss / continuous
- search returns 1-based address, -1 on miss
- search continuous resumes from last position; result=0 restarts
- search rung false preserves result and found
- send / receive status tags
- send/receive success and error pulse for one scan
- send/receive status tags reset to defaults when rung goes false
- send/receive values applied atomically
- send sparse addresses split into multiple writes
- exception_response holds last code until next transaction
