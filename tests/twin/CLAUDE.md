# Twin Harness

Plain-English tests for PLC programs. Same test, same ladder, same check — run against the soft PLC or the real PLC.

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

Setup copies fire on the first scan the rung is true, then stop. Observation copies keep overwriting Results every scan — the last read wins by the time the harness checks.

## Slot layout

Each test owns a slot — a `named_array` instance with 8 Int fields:

| Field     | Purpose                  |
|-----------|--------------------------|
| Cmd       | Trigger (harness writes) |
| Scratch   | Temporary storage        |
| Result1-4 | Outputs to check         |
| Fired     | One-shot gate            |
| ErrorCode | Error reporting          |

Tests cannot share memory. Test #1 owns one contiguous block, test #2 the next.

## 8-step protocol

1. Zero every slot.
2. Stage inputs with `Cmd = 0` so nothing fires.
3. Read back to catch dropped writes.
4. Write `Cmd = 1` everywhere.
5. Run until the slowest test has had its scans.
6. Read every slot.
7. Compare to `expect`. Verify `Fired` went up.
8. Report.

## Running against the real PLC

`run(cases, on=real_plc)` — same 8 steps. The harness does not push ladder; you load it with the Click software. The canonical program writes a build hash to `DS1` on first scan. The harness reads `DS1` first and bails if it doesn't match.

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
