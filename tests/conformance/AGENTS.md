# Conformance Suite

Plain-English cases for PLC semantics. Same case, same ladder, same check -- run in-process, over Modbus against pyrung's soft PLC, or over Modbus against a real CLICK.

A conformance suite in the Modbus sense: the harness observes the implementation **only through registers**. The ladder computes its own answer and writes it into its slot; the harness writes `Cmd`, waits, and reads the slot back.

| Run | Where | How | Pacing |
|---|---|---|---|
| In-process | `make test` (unmarked) | `run(cases)` on a fixed-step `PLC` | exact scan counts |
| Modbus, local sim | `make test-conformance` (marker `conformance`) | `run_modbus(cases)` | wall clock |
| Modbus, real CLICK | `make test-conformance PLC=host[:port]` (or `--conformance-plc`, or env `PYRUNG_CONFORMANCE_PLC`) | `run_modbus(cases, "host:port")` | wall clock |

The two Modbus runs share every line of client code; only the endpoint differs.

## What a test is

1. A sentence that says what should be true.
2. A pyrung ladder that proves it by writing the answer into a known slot.
3. A check that reads the slot and compares to what we expected.

The ladder writes its own answer because Modbus only reads addresses — we can't peek at variables from Python.

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

## Slot layout

Each case owns a slot -- a `named_array` instance with 8 Int fields, mapped to fixed DS registers so it is reachable over Modbus. Slot N lives at `DS(10*N + 1)`..`DS(10*N + 8)`; the last two registers of each block of 10 are spare.

| Field     | Offset | Slot 1 | Purpose                  |
|-----------|--------|--------|--------------------------|
| Cmd       | +0     | DS11   | Trigger (harness writes) |
| Scratch   | +1     | DS12   | Temporary storage        |
| Result1-4 | +2..+5 | DS13-16 | Outputs to check        |
| Fired     | +6     | DS17   | One-shot gate            |
| ErrorCode | +7     | DS18   | Error reporting          |

DS1..DS10 are the header. The program's first rung writes, on the first scan (`sys.first_scan`, SC2):

- `DS1` = build hash (1..32767): a SHA-256 of the CLICK ladder export of the case rungs, folded into a DS register. It changes whenever the exported ladder would.
- `DS2` = slot count.

Cases cannot share memory. Slot order is case order, with in-process-only cases left out. `build_program(cases)` returns the program, its TagMap, and the hash; `ConformanceProgram.export(dir)` writes `main.csv` and `nicknames.csv` for the CLICK software. The build export is validated, so a case the CLICK exporter rejects fails at build time -- mark it in-process-only.

Case modules expose `cases`; `test_modbus.py` lists every module in `ALL_CASES`. A new case module adds itself there.

## Expectations

`expect` maps a slot field to an int or a `range` (`range(90, 111)` reads as `90..110` in failures). Exact ints are for values that do not depend on how long the harness waited. Over the wire, anything wall-clock dependent (timer `Acc`, scan counts) either stops moving before the harness reads -- observe `Done`, or capture the value once with `oneshot=True` when a condition is met -- or uses a bracket wide enough for scan jitter that both reads fall in. Keep timer presets well above the scan period (hundreds of ms, not tens) and set `scans` so the settle time covers the preset.

A case that cannot run over Modbus passes `in_process_only="reason"`. The Modbus run leaves it out of the program and reports it as skipped with that reason.

## 8-step protocol

In-process (`run`):

1. Zero every slot (fresh `PLC`).
2. Stage inputs with `Cmd = 0` so nothing fires (scan 0).
3. -- (nothing can be dropped in-process).
4. Patch `Cmd = 1` everywhere.
5. Run `max(case.scans)` scans.
6. Read every slot from `current_state`.
7. Compare to `expect`. Verify `Fired` went up.
8. Report.

Over Modbus (`run_modbus`), every step is a register read or write:

0. Read `DS1..DS2` until `DS1 != 0` (timeout -> `ConformanceError`, "program is not running"). If `(DS1, DS2)` is not this build's `(hash, slot count)`, raise `BuildMismatchError` before writing anything.
1. Zero every slot register (one block write, chunked at 100 registers). `Cmd` is the first register of each slot, so it is never left at 1 after its `Fired` is cleared.
2. Stage inputs with `Cmd = 0`. No case stages inputs yet; this is the zeroing.
3. Read the slots back until they are all zero (timeout -> `ConformanceError`, "writes were dropped").
4. Write `Cmd = 1`, one single-register write per slot. A block write would rewrite a slot's other registers after its case might already have fired (the sim applies each register as a patch on the next scan, so one request can span scans).
5. Poll every 20 ms until every `Fired != 0`, or until the timeout (default 10 s). A case that never fires is judged a failure, not raised. Then sleep the settle time, `max(case.scans) * 10 ms`.
6. Read every slot, wait `max(100 ms, settle)`, read every slot again.
7. A case passes only if it fired and **both** reads meet `expect`. If one read passes and the other does not, the result's `note` says "unstable" with both reads.
8. Report. `assert_all_passed` lists failures; skipped cases do not fail.

## Pacing over the wire

Over Modbus nothing counts scans, so waits are wall clock: "Fired, then settle, then two reads". `case.scans` becomes settle time at the nominal 10 ms scan. The double read is the stability check: a value still moving when the harness reads fails loudly instead of passing by luck.

The local sim (`SimServer`) is `ClickDataProvider` behind `pyclickplc.server.ClickServer` on a free localhost port, serving a `PLC(realtime=True)` that free-runs on its own thread at one scan per 10 ms of wall clock (late scans are not burst to catch up). Timers therefore see wall-clock time, as on a CLICK. Modbus reads and writes take the same lock as the scan, and each write becomes a `patch()` applied at the start of the next scan. A scan exception stops the sim and fails the run.

The default 10 s timeout is far above what the sim needs (a whole run is about 0.3 s); it is sized for a real CLICK on a busy network.

## Running against the real PLC

`make test-conformance PLC=192.168.0.10` (port defaults to 502). The harness does not push ladder: export the program with `build_program(ALL_CASES).export(dir)`, load `main.csv` and `nicknames.csv` with the CLICK software, and put the PLC in RUN. The harness reads `DS1` first and bails if the build does not match. The harness self-checks in `test_modbus.py` always use their own local sim. When a real-PLC answer differs from pyrung's, that is a finding: either pyrung's semantics change, or the difference becomes a documented CLICK deviation.

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
- call into subroutine
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
