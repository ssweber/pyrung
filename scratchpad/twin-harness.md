# Harness v2.2

## What this is

A way to write plain-English tests for PLC programs.

A test is three things:

1. A sentence that says what should be true.
2. A small pyrung ladder that proves it. The ladder does the thing, then writes the answer into a known spot.
3. A check that reads the spot over Modbus and compares it to what we expected.

The harness runs step 3. Same test, same ladder, same check, against the soft PLC or the real PLC.

The ladder writes its own answer because Modbus only reads addresses. We can't peek at variables from Python.

Nobody has written these claims down as testable sentences. Academics went to temporal logic. Textbooks went to scan diagrams. The middle is empty.

## What a test looks like

```python
case(
    "Memory writes are visible to the next instruction on the same scan",
    ladder = prove_memory_is_immediate,
    expect = {"Result1": 42},
)
```

```python
def prove_memory_is_immediate(slot):
    with Rung(slot.Cmd != 0):
        copy(42, slot.Scratch)
        copy(slot.Scratch, slot.Result1)
```

```python
run(cases, on=soft_plc)
run(cases, on=real_plc)
```

## Each test gets its own spot

Tests can't share memory. Each test owns a slot — a named array instance.

```python
@named_array(Int, count=8)
class Case:
    Cmd       = 0
    Scratch   = 0
    Result1   = 0
    Result2   = 0
    Result3   = 0
    Result4   = 0
    Fired     = 0
    ErrorCode = 0

Case.map_to(ds.select(101, 356))
```

Test #1 owns `DS101..DS108`. Test #2 owns `DS109..DS116`. The author uses `slot.Scratch`, `slot.Result1`. The harness handles which instance.

## How a run works

1. Zero every slot.
2. Stage inputs with `Cmd = 0` so nothing fires yet.
3. Read back to catch dropped writes.
4. Write `Cmd = 1` everywhere.
5. Wait on `system.sys.scan_counter` until the slowest test has had its scans.
6. Read every slot.
7. Compare to `expect`. Check `Fired` went up.
8. Report.

We never peek at internals. Every answer is something the ladder wrote itself.

## Running against the real PLC

```python
run(cases, on=real_plc)
```

Same eight steps. The harness does not push ladder. You load it with the Click software yourself.

The canonical program writes a build hash to `DS1` on first scan. The harness reads `DS1` first and bails if it doesn't match. Skipped on the soft PLC.

## Not in v1

- No pushing ladder to the real PLC. Build hash catches mismatches.
- No fancy soft-vs-real analysis. Two reports, side by side, simple diff.
- No slot fields beyond what the first tests need.
- No subroutines unless a test is about subroutines.
- No round-tripping ladder through export/import as a reference.
- No config files, env vars, or CLI.
- No parallel execution.

## First tests

Three sentences. They apply to the main program and to subroutines.

1. **Rungs take a snapshot of memory when entered.**
2. **Rungs evaluate all conditions first, then execute instructions in source order.**
3. **Instructions mutate global memory.**

## Coverage checklist

Each line becomes a sentence, then a ladder, then a row in the report.

**Scan**
- patch values consumed after one scan
- patch applied before logic, on the same scan
- forces persist across scans
- fault flags (SC40/SC43/SC44) auto-clear at scan start
- fault flags auto-clear only if NOT retriggered same scan
- first_scan true once
- first_scan true on scan 0, false on scan 1
- first_scan re-triggers on STOP→RUN (not just once at power-on)
- scan clock initial state is False on first scan
- scan_counter resets on STOP→RUN
- clock toggles fire at their nominal rate under fixed dt
- mode_run readable from ladder, false in STOP
- RTC advances with sim time after set_rtc
- STOP→RUN clears scan_id and scan_counter

**Rung semantics**
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
- subroutine memory writes visible immediately (calling rung evaluated, but memory sees writes)
- forloop runs N times in one scan
- forloop body sees its own prior-iteration writes
- forloop loop.idx indirect addressing

**Memory**
- rung N+1 sees rung N's writes, same scan
- Bool default False
- Int default 0
- per-slot default override
- default_factory by address
- Bool non-retentive by default
- Int / Dint / Real retentive by default
- retentive survives STOP→RUN
- non-retentive resets on STOP→RUN
- both reset on cold (no battery) reboot

**Numerical handling**
- Int wrap at 32767
- Dint wrap at 2^31
- Word wrap at 65535
- Real precision (32-bit)
- division by zero → 0 + fault
- non-finite float (NaN, ±Inf) → 0 + fault
- copy clamps
- calc wraps
- copy argument order is source → dest
- calc mode inference (hex vs decimal)
- calc WORD-only infers hex mode
- calc mixing WORD and non-WORD: validator finding `CLK_CALC_MODE_MIXED`
- pyrung uses Python-precision intermediates
- Click uses 32-bit intermediates
- agree when intermediates fit in 32 bits
- disagree when intermediates exceed 32 bits — measured by harness, not assumed
- new validator finding: `CLK_CALC_INTERMEDIATE_OVERFLOW` (hint by default, error in strict)
- the validator's bounds are guesses until the harness measures Click — then they're facts

**Per instruction**
- out follows rung
- out oneshot fires once
- one-shot fires once
- rise / fall fire one scan
- rise/fall see the value carried from end of previous scan
- latch / reset stickiness
- on_delay (TON) auto-reset
- TON drops Acc and Done together when rung goes false
- on_delay with .reset() (RTON) holds
- RTON holds Acc and Done across rung-false
- off_delay (TOF) inverse
- TOF: Done True and Acc 0 while rung True; Acc counts only when rung false
- off_delay keeps counting after .Done flips, down to Int min
- timer Acc clamp at 32767
- timer Acc continues past preset up to 32767 clamp (not just stops at preset)
- timer fractional ms accumulation across scans — does Click track remainders?
- timer resumes if preset raised mid-count
- timer fires immediately if preset lowered below Acc
- TOF Done flips False when Acc reaches preset (the actual transition)
- counter increments every scan the rung is true (not per edge)
- counter resumes if preset raised mid-count
- counter survives rung going false (no auto-reset)
- counter Acc clamp at Dint range
- count_up edge vs level
- count_down starts at 0, counts negative
- copy converters: to_value, to_ascii, to_text, to_binary
- to_value vs to_ascii on the same CHAR ('5' → 5 vs 53)
- to_text suppress_zero default vs fixed-width
- to_text with termination_code appends one char after the digits
- copy literal string fans out across consecutive txt slots
- copy literal string starting at last txt slot — what happens?
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
- shift register: rung power is the shift-in value (not a separate input)
- drum events are edge-based
- drum pause/reset/jump precedence order
- drum accumulator resets on step transition
- search hit / miss / continuous
- search returns 1-based address (not 0-based), -1 on miss
- search continuous resumes from last position; result=0 restarts
- search rung false preserves result and found (no reset)
- send / receive status tags
- send/receive success and error pulse for one scan
- send/receive status tags reset to defaults when rung goes false
- send/receive values applied atomically (all or none on error)
- send sparse addresses split into multiple writes
- exception_response holds last code until next transaction