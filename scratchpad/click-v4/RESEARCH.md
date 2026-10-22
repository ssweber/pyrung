# CLICK v4.00 instructions — research + pyrung design draft

Source: clicknick#91. CLICK Ver4.00 (installed at `C:\Program Files (x86)\AutomationDirect\CLICK Ver4.00`),
help pages diffed against Ver3.92 (`Help/Content/*.htm`), plus `Readme.pdf` and the v4 error-code tables
(189 Programming, 187 Object). Overlay work is already done in clicknick (0352421).

Order of work: design in pyrung -> conformance cases on pyrung's Modbus sim -> capture in CLICK v4 ->
laddercodec typed blobs -> pyrung export/import -> same conformance cases on a real PLC.
CPU gating (C0-0x lacks all of this) is **out of scope for now**; pyrung doesn't gate anything yet.

## Decisions so far (2026-10-22)

- **Builder rule.** `verb(...).method(...)` chaining is reserved for instructions that take *extra rung
  inputs*: power-rail legs beyond the rung itself (`.reset()`, `.down()`, `.clock()`, `.jog()`, `.jump()`).
  An instruction with one input (the rung) is a plain call with keywords. The Out variants all have one
  input, so none of them is a builder. The drums keep theirs.
- **SUM over bits** -> no new DSL. `.sum()` on a Bool range. CLICK's editor spells it plain `SUM` too
  (checked 2026-10-22; "SUMBIT" is only the internal/error-table name), so export/import only need to
  accept Bool banks.
- **Out variants** are separate verbs, not `out(..., kind=)` and not chained off `out()`. Each one owns its
  instruction class, because 25 files dispatch on `OutInstruction` assuming OUT semantics.
- **Timers inside Out variants are `Timer` UDTs** (`Timer[1]`), the same thing `on_delay()` takes. A phase is
  written `(timer, preset)`, in `on_delay(timer, preset)` order. See "Feels off" #1 and #2.
- **One unit per instruction**, `unit="ms"` keyword, because CLICK has a single Unit dropdown per coil.
- **Native classes, no desugar** (decided 2026-10-22). toggle/debounce/flasher/timed are their own
  instruction classes that do their own timing (like the timers), each with its own `advance_profile` and
  crossing. There is no lowering layer to build on (the drum-desugar plan was deleted in `79fc61b`, and
  `core/synthesis.py` only feeds the soft-PLC overlay). They must not subclass `OutInstruction`:
  `crossing_for()` walks the MRO and would hand them `OutCrossing`.
- **Drum register durations**: `CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED` stays, downgraded to a hint
  ("register drum durations need CLICK v4"). That keeps the v3 guard without adding version gating.
- **Existing bugs found by the survey are fixed inside the feature that touches them** (see
  TOUCHPOINTS.md "Surprises" #12): `render_expr` abs/SumExpr, prover `_expr_reads_tag` ignoring SumExpr, the
  drifted click cheatsheets, and CI not excluding the `tumbler` marker.
- **RAND**: seeded deterministic stream at runtime, nondeterministic input in the prover, test override hook.
- **For/Next body model**: bare instructions inside `forloop` are implicit always-true body rungs (today's
  meaning). A body rung with contacts is written as `with Rung(...)` inside the loop. Export and import go
  both ways on that rule. `break_loop()` / `continue_loop()` live in such a rung.
- **Twin -> conformance.** The twin harness becomes a Modbus conformance suite, built against pyrung's own
  Modbus sim first (section 6). The fast in-process run stays in `make test`; the Modbus run goes behind a
  `conformance` marker with a `make test-conformance` target (decided 2026-10-22).

---

## New syntax, all of it

Out coil variants (section 1):

- `toggle(Light)` — single tag, no range, no oneshot; immediate via `Light.immediate` as with `out()`.
- `debounce(Valve, on=(Timer[1], 100), off=(Timer[2], 100), unit="ms")` — either side may be left out,
  not both (error at call time).
- `flasher(Beacon, on=(Timer[3], 500), off=(Timer[4], 500), unit="ms")` — both sides required.
- `timed(Horn, Timer[5], 1000, unit="ms", maintained=False)` — CLICK "Timed".

Presets anywhere above are `int | Tag` (CLICK allows DS or a constant).

Drum (section 2):

- `time_event_drum(outputs=, presets=[...], events=[...], unit=, pattern=, current_step=, accumulator=,
  completion_flag=)` with `.reset()` / `.jump()` / `.jog()` as on `time_drum`. `None` is allowed per step in
  `presets` or `events`, never both in the same step.
- `time_drum(presets=[Tag, ...])` — already legal in core; the Click validation that rejects it goes away for v4.

Math (section 3), all inside `calc()` expressions:

- `rng.max()`, `rng.min()`, `rng.median()`, `rng.mean()` — siblings of the existing `rng.sum()`.
- `rng.sum()` on a Bool range — exists; CLICK spells it `SUM` as well.
- `randint(lo, hi)` — inclusive, args const or tag.
- `abs(x)` — exists; export/import fix only.

For/Next (section 4):

- `with Rung(...)` inside `with forloop(...)` — a conditioned body rung.
- `break_loop()`, `continue_loop()` — only inside a conditioned body rung (CLICK requires a contact).
- Nested `forloop` up to 3 deep, each with its own `loop.idx`.

---

## Feels off — discussion

### 1. `(timer, preset)` tuples

No other pyrung verb takes a tuple keyword. The alternatives:

```python
debounce(Valve, on=(Timer[1], 100), off=(Timer[2], 100), unit="ms")                   # A: tuples
debounce(Valve, on=Timer[1], on_preset=100, off=Timer[2], off_preset=100, unit="ms")  # B: flat kwargs
debounce(Valve, on=Phase(Timer[1], 100), off=Phase(Timer[2], 100), unit="ms")         # C: value type
```

**Decided: A** (2026-10-22). It reads as "on for 100, timed by Timer[1]", and its order is `on_delay(Timer[1], 100)`'s
order, so nothing new has to be learned. B doubles the keyword count and lets `on_preset` be given without
`on`. C adds a public type that exists only to hold two fields. With A, a half-written phase (`on=Timer[1]`)
fails at call time with a clear error.

`timed` has one phase, so it takes `(target, timer, preset)` positionally, like `on_delay`. That's a small
inconsistency with debounce/flasher, but `timed(Horn, on=(Timer[5], 1000))` would be a keyword for nothing.

### 2. Does CLICK set the T done bit?

CLICK's Out dialog only asks for a TD (the accumulator), but C0112 ("refers to a Timer Address which is
already in use") says the coil claims the whole timer number. So T<n> is reserved either way; the question is
whether CLICK ever *writes* it. Probe O6 answers it.

Plan either way: the verbs take a `Timer` UDT and pyrung sets `.Done` with a defined meaning per phase.

| Verb | Phase | `.Done` in pyrung |
|---|---|---|
| debounce | on | input has been steadily on >= preset (on-side has fired) |
| debounce | off | input has been steadily off >= preset (off-side has fired) |
| flasher | on / off | that phase's time is up; drops when the phase restarts |
| timed | — | the on time has elapsed |

If CLICK doesn't write T<n>, pyrung still sets it. Click validation then reports any *read* of that done bit
(`CLK_OUT_TIMER_DONE_UNSET`: "CLICK's Out coil does not set T<n>; this contact reads False on the PLC").
A program that never reads it loses nothing. If CLICK does write T<n>, the table above gets corrected from the
probe and there's no finding.

### 3. `timed` as a name

`min_on` (the earlier draft) is wrong for the default mode. Not maintained, the output is on for *exactly* T
and then goes off even though the rung is still true. That's a fixed pulse (IEC TP), not a minimum. Only
`maintained=True` is "at least T". One verb has to cover both modes, and `timed` is CLICK's own word, so
it's easy to find in both directions. `pulse` is out (the `Pulse` physical spec). Keeping `timed`.

### 4. `time_event_drum` vs `time_drum(events=...)`

CLICK's dropdown says "Time & Event". pyrung already has one constructor per base (`event_drum`,
`time_drum`), so a third, `time_event_drum`, follows the pattern. It also keeps `time_drum`'s signature
honest: in T&E a step may have no preset, which `time_drum` must never allow. Recommendation
**`time_event_drum`**. The builder is justified here: reset/jump/jog are real rung inputs.

### 5. `.mean()` vs `.avg()`

CLICK spells it AVG and MDN. pyrung spells it the way Python does (`statistics.mean`, `statistics.median`),
and the exporter maps it, the same way `log10` maps to `LOG`. `.max()` / `.min()` match the built-ins.

This follows the existing rule in `_CLICK_FUNC_TO_PYTHON` (`click/codegen/utils.py:34`): where Python has a
name, pyrung uses it (`RAD`->`radians`, `DEG`->`degrees`, `LOG`->`log10`, `LN`->`log`); where it doesn't,
pyrung lowercases CLICK's name (`lsh`, `rsh`, `lro`, `rro`). `.avg()` would be the outlier.
Recommendation **`.mean()` / `.median()`**.

### 6. `randint`

`rand(lo, hi)` would mirror CLICK. `randint` mirrors Python and signals the inclusive upper bound.
`from pyrung import randint` shadows `random.randint` in a user's file. That's acceptable: nobody calls
`random` inside ladder. Recommendation **`randint`**.

### 7. Debounce with both sides is not two timers

Kept from the earlier draft because it constrains the desugar. With both sides set, Debounce is not
`off_delay(on_delay(in))`. The help says a brief off-bounce while on leaves the output on. With composed
timers, the on-delay restarts on that bounce, so the output can drop when T_on > T_off. Debounce is really
"output follows input once they've disagreed for T". The desugar is that state machine, and probe O3 checks
whether CLICK resets the on-side TD on a bounce.

---

## 0. Things the issue didn't list (found in error tables / dialogs)

| Finding | Evidence |
|---|---|
| **Drum durations can be registers**, in Time base too (not only Time & Event) | v4 Time-base dialog has `...` browse buttons on every Duration cell; v3.92's doesn't. Error C0227 "does not support Drum: Time&Event, Duration Register". |
| **SUM over a bit range** ("SUMBIT" / "SUM(BIT)") | C0229 / OBJ-1016 list "SUMBIT" next to MAX..ABS. Not on the keypad image; rendering unknown. |
| Range rules for the aggregates | OBJ-1014 range must be ordered Low:High; OBJ-1015 same address block; OBJ-1013 MDN range <= 1024 bytes (512 DS / 256 DD,DF). |
| Functions compose inside formulas | C0114 "missing an operation +-*/ following a function()". |
| Hex Math gets MAX/MIN/MDN/AVG/RAND but **not ABS** | Math (Hex) page diff. |
| Out/Drum TD registers collide with Timer TD use | C0112 (OUT), C0113 (DRUM) "refers to a Timer Address which is already in use". |
| For-loop rules | C010D nesting max 3 counting called subroutines; C0111 For one-shot only on outer loop; C0115 RETURN/END not allowed inside For/Next; C010F/C0110 Break/Continue only inside a loop. |
| Embedded Discrete Message now on C0-1x | Readme "Improvements 2". Send instruction, not in scope here. |

---

## 1. Out coil expansion

The v4 Out dialog has four Output Types: **Out/Toggle, Debounce, Flasher, Timed**. Only Out/Toggle has
range / one-shot / immediate. Debounce, Flasher and Timed show only `Bit Memory Address1`: single bit, no immediate.

### Semantics (from help)

- **Toggle** (checkbox on plain Out): inverts the bit on each OFF->ON rung transition; state retained when
  rung false. Single address only; one-shot and range disabled; Immediate still available for Y.
- **Debounce**: On Debounce Time (DS or const) + On TD; Off Debounce Time + Off TD; either side optional,
  one shared unit (ms/sec). On-side: output turns on after the input has been steadily ON >= on time, and off
  immediately (when off-debounce isn't used). Off-side: the mirror image. One TD per configured side.
- **Flasher**: On Flash Time + TD, Off Flash Time + TD (both required), unit ms/sec. Starts in the ON
  phase as soon as the rung goes true; rung false -> output off immediately, at any point in the cycle.
- **Timed**: Minimum On Time (DS or const) + TD, unit ms/sec/min (editor check 2026-10-22), `Output Maintained` checkbox.
  Not maintained: rung true for any time -> ON for exactly the on time, then OFF *even if the rung is still true*.
  Maintained: ON for at least the on time, then follows the rung.

### pyrung design

```python
toggle(Light)
debounce(Valve, on=(Timer[1], 100), off=(Timer[2], 100), unit="ms")
flasher(Beacon, on=(Timer[3], 500), off=(Timer[4], 500), unit="ms")
timed(Horn, Timer[5], 1000, unit="ms", maintained=False)
```

- These are OUT-family writers: they drive the bit when the rung is false, so `INERT_WHEN_DISABLED = False`,
  and they belong in duplicate-out validation. Each timer also counts as a writer of its `Timer` UDT, so
  `on_delay(Timer[1], ...)` elsewhere is a duplicate-timer finding (CLICK's C0112).
- Units (checked in the v4 editor): debounce and flasher take ms/sec; timed takes ms/sec/min. Anything else
  (`min` on debounce/flasher, `hour`/`day` anywhere) is a Click validation error, not a core one.
- Native classes (decided): each does its own timing with `_frac:` per phase, has its own `advance_profile`
  and crossing, and writes all of its internal state every scan (the prover discovers memory keys from one
  scan). Not subclasses of `OutInstruction`. Most existing `OutInstruction` dispatch sites must *exclude* them.

### Probes (conformance cases, section 6)

| # | Question |
|---|---|
| O1 | Toggle on first scan with the rung already true: does it toggle? |
| O2 | Debounce: TD value after it fires (held at preset? reset to 0?). Preset 0? |
| O3 | Debounce both sides: does the on-side TD reset when the input bounces back mid-count? |
| O4 | Flasher: are both TDs zeroed when the rung goes false? Does a restart begin with a full ON phase? |
| O5 | Timed, not maintained: after timing out with the rung still true, does it need a fresh OFF->ON to fire again? Does a new rising edge during the on time restart the timer? |
| O6 | **Does any Out variant write T<n>?** Read T<n> every scan through a full cycle of each variant. |
| O7 | **Settled**: debounce/flasher ms/sec; timed ms/sec/min (editor check). Capture one `timed` in min to confirm func code 8232. |

---

## 2. Drum: Time & Event base (+ duration registers)

- The step advances when **duration elapsed AND event ON (level)**. Per step, either part may be blank:
  event-only, time-only, or both (OBJ-1019: each step needs at least one).
- The timing chart shows elapsed time counting from step start while the event is already on
  (step 4: X007 on mid-step, the step ends at 30 ms).
- Units ms/sec/min/hour/day as in Time base. Current step DS, elapsed TD, completion C, jog,
  load-new-step: all unchanged.

```python
time_event_drum(
    outputs=[Y1, Y2],
    presets=[1000, None, 500],          # None: event-only step
    events=[None, PartPresent, Clamp],  # None: time-only step
    unit="ms",
    pattern=[[1, 0], [0, 1], [1, 1]],
    current_step=Step,
    accumulator=StepAcc,
    completion_flag=Done,
).reset(ResetBtn)
```

Note the event is level, while `event_drum` events are edge-based (twin checklist: "drum events are edge-based").
That difference goes in the docstring and gets a probe.

- D1: does elapsed TD keep counting past the duration while it waits for the event (and is it clamped)?
- D2: which banks are eligible for a duration register (presumably DS)?
- D3: T&E event is level: confirm an event already on at step entry advances as soon as the time is up.

`time_drum(presets=[Tag|int])` already accepts tag presets. Click validation rejects them
(`CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED`, `click/validation/hardware.py:462`). Decided: keep the code, downgrade
to a hint ("register drum durations need CLICK v4"). `time_event_drum` is its own class, not a
`TimeDrumInstruction` subclass (the prover assumes every time-drum step has a preset).

---

## 3. Math expansion

| CLICK | Dec | Hex | pyrung | Notes |
|---|---|---|---|---|
| `SUM(a:b)` | yes | yes | `rng.sum()` (exists) | already rendered `SUM ( a : b )` |
| SUM over bits | yes | ? | `rng.sum()` on a Bool range | `SumExpr` already counts True as 1; CLICK renders plain `SUM ( C1 : C10 )` |
| `MAX(a:b)` | yes | yes | `rng.max()` | sibling of `.sum()` |
| `MIN(a:b)` | yes | yes | `rng.min()` | |
| `MDN(a:b)` | yes | yes | `rng.median()` | <= 1024 bytes; even-count rule unknown |
| `AVG(a:b)` | yes | yes | `rng.mean()` | integer/rounding rule unknown |
| `RAND(lo,hi)` | yes | yes | `randint(lo, hi)` | inclusive like Python `randint`; args const or register |
| `ABS(x)` | yes | **no** | `abs(x)` (exists, `UnaryExpr`) | **bug today**: translator renders lowercase `abs(...)`; importer has no `ABS` mapping |

Implementation shape: make `SumExpr` one case of a `RangeAggregateExpr(kind)`. It is referenced in only five
files: `core/expression.py`, `core/memory_block.py`, `click/ladder/translator.py`, `circuitpy/.../_primitives.py`,
`core/analysis/crossings/calc.py`. Importer (corrected by the survey): MAX/MIN/MDN/AVG take colon ranges, so they
go through a generalised `_SUM_RE` path, not `_CLICK_FUNC_TO_PYTHON`; RAND goes in the function table; `ABS`
maps to the Python builtin and stays out of the `pyrung.core.expression` import set (`click/codegen/utils.py`).
Portability: `abs` into the decimal-only set needs a `UnaryExpr` check, since `abs` is not a `MathFuncExpr`.

**RAND vs deterministic replay.** pyrung's core promise is deterministic replay, seek and fork,
and the prover is exhaustive.
- Runtime: a **stateless counter-based hash**, `value = lo + H(seed, scan_id, site, iteration) mod (hi-lo+1)`.
  - `seed`: runner-level, default fixed (so two runs of the same test agree), settable.
  - `scan_id`: already in state, so seek/replay/fork reproduce values with no RNG state to store or restore.
  - `site`: a stable id for the `randint` call (rung index + position in the expression), so two RANDs in
    one scan differ.
  - `iteration`: the for-loop iteration (0 outside loops), so a RAND in a loop body differs per iteration.
  - `H`: something cheap and portable, e.g. splitmix64, so CircuitPython can compute the same numbers.
  A fork gets the same values as its parent for the same scan. That's deliberate (what-if forks differ only
  by what you changed); a fork may reseed if it wants different draws.
- Prover: treat each `randint` as a nondeterministic input over `[lo, hi]`.
- Tests: a runner-level seed/override hook so a test can force a value.
- Conformance: RAND cases check only the range and that values vary, never specific values.

Probes: M1 AVG/MDN result for integer ranges (truncate vs round); M2 MDN with even count (mean of the middle
two, or the lower one?); M3 RAND with lo > hi; M4 MAX/MIN on DH, signed or unsigned?

---

## 4. For/Next: Break, Continue, nesting

- **Break**: exits the innermost loop immediately and continues after its Next. **Continue**: skips to the
  next iteration. Both are their own rung, inside the loop, and **require an enabling contact** (C000B/C000C).
- **Next** stays alone and unconditional. The **For** rung may have contacts (help: `X001 -> For 10`).
- Nesting up to 3, counting loops inside called subroutines. One-shot only on the outermost For.
  RETURN/END not allowed inside a loop.

### Today's model

`forloop()` today is *one rung*. Its body is a flat list of instructions that all run unconditionally,
with `branch()` forbidden inside it. Export emits each body instruction as a wire-only rung
(`click/ladder/instructions.py:50`). Break/Continue need a *conditioned rung inside the loop*, so the flat
body can't express them.

Related, **unverified**: if CLICK allows contacts on ordinary body rungs (I expect it does; v4 Break proves
conditioned rungs can live inside a loop), the importer drops them. `_emit_forloop`
(`click/codegen/emitter.py:838`) emits `body_rung.instructions` and never that rung's conditions. None of the
existing fixtures (`forloop__basic`, `forloop__oneshot`, `ladder_realistic_output/main.csv`) has a body rung
with a contact, so nothing has exercised this. **Capture one to settle it.**

### Design (decided, back-compatible)

A bare instruction in the loop body is an implicit always-true rung. A body rung with contacts is written
as `with Rung(...)` inside the loop.

```python
with Rung(Enable):
    with forloop(10) as loop:
        copy(Src[loop.idx + 1], Dst[loop.idx + 1])   # bare: wire-only rung, as today
        with Rung(Dst[loop.idx + 1] == 0):
            continue_loop()
        with Rung(Fault):
            break_loop()
```

- Export: bare -> wire-only body rung (unchanged); `with Rung(...)` -> body rung with those contacts.
- Import: a wire-only body rung -> bare instruction; a body rung with contacts -> `with Rung(...)`. That
  fixes the dropped-contact importer bug as a side effect.
- `break_loop()` / `continue_loop()` outside a conditioned body rung is an error (CLICK requires a contact).
- Names follow `return_early()` (Python reserves `break`/`continue`).
- Nesting: `_state._forloop_active` becomes a depth counter. `ForLoop.idx` is the fixed `Tag("_forloop_idx")`,
  so nested loops need distinct index tags.
- Runtime: signals like `SubroutineReturnSignal`, caught by the innermost `ForLoopInstruction.execute`.
- Validation (later, with gating): depth <= 3 across `call()`, one-shot only on the outer loop,
  `return_early` inside a loop is an error.

### Probe matrix: what CLICK actually does in a loop

Run before fixing the body model's runtime semantics. Each one is a conformance case. Using
`For(oneshot=1)` gated on `slot.Cmd` makes each loop run exactly once, so the counts are readable.
F2, F6, F7 and F14 are where pyrung's current model is most likely wrong.

| # | Question | Ladder sketch | pyrung today |
|---|---|---|---|
| F1 | Runs N times | body: `DS += 1`, For 5 | 5 |
| F2 | **For rung false: are body OUTs skipped (retain) or driven OFF?** | pre-set C via earlier rung, For contact false, body `out(C)` | children run with enabled=False, so **OUT goes OFF** |
| F3 | Count 0 / negative / > max | For DS with 0, -1 | runs once (`max(1, n)`) |
| F4 | Count register re-read each iteration? | body writes the count register | read once |
| F5 | Body rung with a contact (also a capture) | contact toggles per iteration, body `out(C)` | not expressible |
| F6 | **Edge contact inside a loop: per iteration or per scan?** | body flips C each iteration; rung `rise(C)` -> `DS += 1` | n/a |
| F7 | **Timer inside a loop: accumulates once per scan or once per iteration?** | TON in body, For 10, read TD after 1 scan | n/a |
| F8 | Counter inside a loop counts per iteration | CTU in body, For 5 | n/a |
| F9 | Break exits the innermost loop only | 2-deep, Break in inner at k=2 | n/a |
| F10 | Rungs before Break run in that iteration; rungs after don't | `DS_a += 1`, Break, `DS_b += 1` | n/a |
| F11 | Continue on iteration k skips only the rest of that iteration | `DS_a += 1`, Continue when k=2, `DS_b += 1` | n/a |
| F12 | Continue on the last iteration | same as F11, k = N | n/a |
| F13 | Break/Continue contact driven by a body write in the same iteration | body sets C, Break on C | n/a |
| F14 | **One-shot coil/copy inside a loop: once per scan or once per iteration?** | `copy(..., oneshot)` in body | per-scan memory key |
| F15 | Nested loops: does the inner one-shot error (C0111) or ignore? | 2-deep, inner `oneshot=1` | n/a |
| F16 | Loop inside a called subroutine counts toward depth 3 | 2-deep main + 2-deep in sub | n/a |

Observation: each case writes iteration counts into `Result1..4`. There's no loop index in CLICK; count with
a DS register, or with a pointer incremented in the body. Separately, check how pyrung's `loop.idx` exports
today: it's the unmapped tag `_forloop_idx`.

---

## 5. Capture list for CLICK v4 -> laddercodec

One rung each unless noted. Use `clicknick-rung save <name>` per `laddercodec/add-features-workflow.md`.

**Regression first**: paste an existing v3 golden into v4 and copy it back. That confirms the clipboard
format is unchanged and plain `out()` blobs are identical (func codes 8193..8220).

1. Out: toggle; toggle + immediate(Y); debounce on-only / off-only / both; debounce sec unit; debounce
   preset as DS; flasher const and DS; timed not-maintained / maintained; timed sec.
2. Drum: Time & Event with mixed steps (event-only, time-only, both); Time base with a DS duration; T&E with
   a DS duration; one non-ms unit.
3. Math dec: MAX/MIN/MDN/AVG over DS, DD and DF ranges; `RAND(1,100)`; `RAND(DD1,DD2)`; `ABS(DF1)`;
   SUM over a C range (plain `SUM`; capture confirms the blob); composed `MAX(DS1:DS10) + ABS(DS11)`.
   Math hex: `MAX(DH1:DH4)`, `RAND(...)`.
4. For/Next: **a v3-style loop with a contact on a body rung** (settles the importer question);
   Break with contact; Continue with contact; 3-deep nest; outer one-shot.
5. Scr side: save a v4 project with all of the above and run `decode_program()` on it.
6. Right after a Syntax Check, copy `%TEMP%\CLICK (xxxx)\Compile\` (the `Mnemonic.MN` listing and related
   files). Check every captured func code against the Compile.dll table (section 7).

---

## 7. Binaries stake (2026-10-22): not a semantics oracle

Both surveys were static only. Details: `BINARIES-PC.md`, `BINARIES-FIRMWARE.md`.

- **Firmware**: `Click_fw0400.clk` is a ZIP with every entry AES-256 encrypted. Unreadable; stopped there.
- **Compile.dll lowers nothing.** Each v4 instruction is its own opcode pointing at a firmware routine, so
  its semantics sit behind the encryption. O1-O6, D1-D3, M1-M4 and F1-F16 stay conformance questions.
- **Useful by-products** (predicted func codes, to confirm during capture):

  | Instruction | func codes | mnemonic |
  |---|---|---|
  | Toggle | 8224, 8225 immediate | `OTOG`, `OTOG.Y` |
  | Debounce | 8226 ms, 8227 sec | `ODEB` |
  | Flasher | 8228 ms, 8229 sec | `OFLA` |
  | Timed | 8230-8232 | `OTIME` |
  | Timed, maintained | 8233-8235 | `OTIMEOM` |
  | Continue / Break | 9232 / 9233 | `CONTINUE` / `BREAK` |
  | Drum, register durations | 10249.. | `DRUM.DS*` |
  | Drum, Time & Event | 10505..10538 | `TEDRUM.DS*` (register form only) |

- O7 settled: debounce/flasher accept ms/sec; timed accepts ms/sec/min. The compiler's third Timed unit
  (`OTIME.m`, func code 8232 by the table) is minutes, and the editor offers it.
- Break/Continue are marked as needing exactly one rung input, which agrees with "requires a contact".
- The aggregates (MAX..ABS) are formula tokens 0x1e-0x23. The rounding rules (M1/M2) are in firmware.

---

## 6. Conformance suite (was: twin harness)

### What it is

A conformance suite in the Modbus sense: a fixed set of cases, each with a stated expectation, run against
an implementation that is observed **only through Modbus registers**. The ladder computes its own answer and
writes it into a slot; the harness writes `Cmd`, waits, and reads the slot back. Nothing else is visible,
on a real PLC or on pyrung.

"Twin" undersells that, and it collides with "digital twin" (plant simulation), which in pyrung is the
harness/physical layer.

### Name

Recommendation: **`conformance`**. `src/pyrung/twin/` -> `src/pyrung/conformance/`, `tests/twin/` ->
`tests/conformance/`. The in-process run stays unmarked in `make test`; the Modbus run gets a new registered
`conformance` marker and `make test-conformance` (decided). No `parity` marker exists (an earlier draft was wrong).

### Build order

1. **pyrung over Modbus first.** Start pyrung's Modbus sim (`ClickDataProvider` behind pyclickplc's
   server) with the case program loaded, and run the 8-step protocol through a Modbus client. The
   expectations in each case are what pyrung's sim answers over the wire. That makes the Modbus path the
   contract and shakes out mapping, transport and pacing before any hardware is involved.
2. **Real PLC second.** Same cases, same client, pointed at a CLICK with the exported program. A mismatch
   is a finding: either pyrung's semantics change, or the difference becomes a documented Click deviation
   (as with `CLK_OUT_TIMER_DONE_UNSET`).

### Gaps in today's harness

- `_runner.run()` reads `plc.current_state.tags` in-process; nothing goes over Modbus. The `on=real_plc`
  in `tests/twin/AGENTS.md` isn't implemented.
- Slots are a `named_array` with no TagMap. Over Modbus they need fixed DS addresses (the build hash already
  claims `DS1`).
- Step 5 ("run until the slowest test has had its scans") counts scans in-process. Over the wire, against a
  free-running sim or PLC, it's wall-clock: timer cases need presets well above scan jitter, and expectations
  need tolerances (or a bracket like "Acc between 90 and 110").
- Case programs must export to CLICK, so the cases are limited to what the exporter supports. The v4
  probes (O*, D*, M*, F*) wait for the v4 export.

### Cases from this doc

O1..O7 (Out), D1..D3 (drum), M1..M4 (math), F1..F16 (for/next). Each goes in as a sentence, a ladder and an
expectation. Write pyrung's current answer as the expectation, mark it unconfirmed, and replace it with the
PLC's answer when the run comes back.
