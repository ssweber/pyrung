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

The first tests are the start. Things to add sentences for as we go.

**Execution and memory model**
- rung snapshot on entry
- conditions before instructions
- source order within a rung
- branches AND with parent
- nested branches all see the same rung-entry snapshot
- last rung wins on shared coil
- rung N+1 sees rung N's writes, same scan
- subroutines: same rules
- continued rungs share snapshot
- continued chain breaks on a normal rung
- empty rung (NOP) executes and survives round-trip
- forloop runs N times in one scan
- forloop body sees its own prior-iteration writes
- NOT CLICK forloop loop.idx for indirect addressing
- one-shot fires once
- rise / fall fire one scan
- rise/fall see the value carried from end of previous scan
- patch values consumed after one scan
- patch applied before logic, on the same scan
- forces persist across scans
- fault flags (SC40/SC43/SC44) auto-clear at scan start

**Numerical limits**
- Int wrap at 32767
- Dint wrap at 2^31
- Word wrap at 65535
- Real precision (32-bit)
- timer Acc clamp at 32767
- counter Acc clamp at Dint range
- division by zero → 0 + fault

**Defaults and initial values**
- Bool default False
- Int default 0
- per-slot default override
- default_factory by address
- first_scan true once
- first_scan true on scan 0, false on scan 1

**Retentive**
- Bool non-retentive by default
- Int / Dint / Real retentive by default
- retentive survives STOP→RUN
- non-retentive resets on STOP→RUN
- both reset on cold (no battery) reboot
- STOP→RUN clears scan_id and scan_counter

**calc vs copy**
- copy clamps
- calc wraps
- copy argument order is source → dest
- calc mode inference (hex vs decimal)
- calc WORD-only infers hex mode
- calc mixing WORD and non-WORD: validator finding `CLK_CALC_MODE_MIXED`
- copy converters: to_value, to_ascii, to_text, to_binary
- to_value vs to_ascii on the same CHAR ('5' → 5 vs 53)
- to_text suppress_zero default vs fixed-width
- to_text with termination_code appends one char after the digits
- blockcopy only supports to_value / to_ascii converters
- blockcopy length match
- fill writes constant

**calc intermediates**
- pyrung uses Python-precision intermediates
- Click uses 32-bit intermediates
- agree when intermediates fit in 32 bits
- disagree when intermediates exceed 32 bits — measured by harness, not assumed
- new validator finding: `CLK_CALC_INTERMEDIATE_OVERFLOW` (hint by default, error in strict)
- the validator's bounds are guesses until the harness measures Click — then they're facts

**Per instruction**
- out follows rung
- out oneshot fires once
- latch / reset stickiness
- on_delay (TON) auto-reset
- TON drops Acc and Done together when rung goes false
- on_delay with .reset() (RTON) holds
- RTON holds Acc and Done across rung-false
- off_delay (TOF) inverse
- TOF: Done True and Acc 0 while rung True; Acc counts only when rung false
- off_delay keeps counting after .Done flips, down to Int min
- timer resumes if preset raised mid-count
- timer fires immediately if preset lowered below Acc
- counter increments every scan the rung is true (not per edge)
- counter resumes if preset raised mid-count
- counter survives rung going false (no auto-reset)
- count_up edge vs level
- count_down starts at 0, counts negative
- shift register direction by range order
- drum events are edge-based
- search hit / miss / continuous
- copy literal string fans out across consecutive txt slots
- copy literal string starting at last txt slot — what happens?
- pack_bits / unpack_to_bits
- pack_bits into REAL stores the raw IEEE-754 bit pattern
- pack_words / unpack_to_words
- pack_words first source is the low word
- pack_text into WORD parses hex; into INT/DINT parses signed decimal
- pack_text on numeric with leading whitespace faults unless allow_whitespace
- send / receive status tags
- send/receive success and error pulse for one scan
- send/receive status tags reset to defaults when rung goes false
- exception_response holds last code until next transaction
- call into subroutine
- forloop loop.idx indirect addressing

**System points**
- scan_counter resets on STOP→RUN
- clock toggles fire at their nominal rate under fixed dt
- mode_run readable from ladder, false in STOP
- RTC advances with sim time after set_rtc

Each line becomes a sentence, then a ladder, then a row in the report.