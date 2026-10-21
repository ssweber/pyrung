# CLICK v4.00 instructions — research + pyrung design draft

Source: clicknick#91. CLICK Ver4.00 (installed at `C:\Program Files (x86)\AutomationDirect\CLICK Ver4.00`),
help pages diffed against Ver3.92 (`Help/Content/*.htm`), plus `Readme.pdf` and the v4 error-code tables
(189 Programming, 187 Object). Overlay work is already done in clicknick (0352421).

Order of work: design in pyrung -> capture in CLICK v4 -> laddercodec typed blobs -> pyrung export/import.
CPU gating (C0-0x lacks all of this) is **out of scope for now**; pyrung doesn't gate anything yet.

## Decisions so far (2026-10-21)

- **SUMBIT** -> no new DSL. `.sum()` on a Bool range; the exporter picks CLICK's spelling from the bank type
  (capture tells us the spelling), and the importer maps it back to `.sum()`.
- **Out variants** use CLICK's names:
  - `toggle(Light)`: its own verb and its own instruction class. Toggle keeps its state when the rung is
    false, and 25 files dispatch on `OutInstruction` assuming OUT semantics. It takes a single tag only
    (no range, no oneshot); immediate goes through `Light.immediate` as with `out()`. It exports as an Out
    coil with CLICK's Toggle box checked.
  - `debounce(Valve).on_delay(100, acc=A).off_delay(100, acc=B)`: a builder. It is not chained off `out()`,
    because `out()`'s oneshot/toggle/range don't apply. At least one side is required (enforced the way
    drum `.reset()` is). There's one unit for both sides, `debounce(Valve, unit="ms")`, because CLICK has a
    single Unit dropdown.
  - `flasher(Beacon, on=500, on_acc=A, off=500, off_acc=B, unit="ms")`
  - `timed(Horn, 1000, acc=T, maintained=False)`
- **Desugar** debounce/flasher/timed in core; Click export emits the native coil.
- **RAND** approach accepted: seeded deterministic stream at runtime, nondeterministic input in the prover,
  test override hook.
- **`break_loop()` / `continue_loop()`** accepted. Before designing the body model, run the probe matrix in
  section 4 on a real PLC and pin down *exactly* what CLICK does in a loop.

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
- **Timed**: Minimum On Time (DS or const) + TD, unit, `Output Maintained` checkbox.
  Not maintained: rung true for any time -> ON for exactly the on time, then OFF *even if the rung is still true*.
  Maintained: ON for at least the on time, then follows the rung.

### Open questions (need PLC/twin runs, not just captures)

1. Toggle on first scan with the rung already true: does it toggle?
2. Debounce: TD value after it fires (held at preset? reset to 0?). Does the TD reset when the input bounces
   back mid-count? Preset 0?
3. Flasher: are both TDs zeroed when the rung goes false? Does a restart begin with a full ON phase?
4. Timed, not maintained: after timing out with the rung still true, does it need a fresh OFF->ON to fire
   again? Does a new rising edge during the on time restart the timer?
5. Units: only ms/sec (dialog shows ms, sec). Confirm there's no min/hour/day.

### pyrung design (proposal)

These are OUT-family writers: they drive the bit when the rung is false, so `INERT_WHEN_DISABLED = False`,
and they belong in duplicate-out validation. Each TD accumulator must be an explicit tag, because CLICK needs
the TD numbers. pyrung tags are semantic and TagMap maps them to TD.

```python
toggle(Light)                     # Out + Toggle; immediate via Light.immediate as today
debounce(Valve, on=100, on_acc=DbOn, off=100, off_acc=DbOff, unit="ms")   # either side optional
flasher(Beacon, on=500, on_acc=FlOn, off=500, off_acc=FlOff, unit="ms")
min_on(Horn, 1000, acc=HornT, unit="ms", maintained=False)                # CLICK "Timed"
```

**Chain idea (`out(Y).off_delay(...)`):** the delay names fit **Debounce**, not Timed.
- On-debounce = output on only after the input is steadily on for T, then off at once. That's on-delay.
- Off-debounce = output stays on until the input is steadily off for T. That's off-delay.
- So `out(Y).on_delay(100, acc=A).off_delay(100, acc=B)` could export as one Debounce coil.

Caveat: Debounce with both sides is **not** off_delay(on_delay(in)). The help says a brief off-bounce while
on leaves the output on. With composed timers, the on-delay restarts on that bounce, so the output can drop
when T_on > T_off. Debounce is really "output follows input once they've disagreed for T". The desugar
must be that state machine, and the probe must check whether CLICK resets the on-side TD on a bounce.

Timed doesn't match off_delay either. Off-delay holds for the rung's on time **+ T**. Timed holds for
**max(rung on time, T)** when maintained, and exactly T from the rising edge when not (IEC "TP"). If it's
chained, `out(Y).min_on(T, acc=, maintained=)` names it honestly.

Decisions for you:
- **Verbs vs `out(..., kind=)`**: separate verbs (decided). Open: `debounce(...)` or the `out().on_delay().off_delay()` chain.
- **Name for "Timed"**: `min_on` describes the behaviour. `timed_out` copies CLICK's name, but it reads like
  "timed out". Avoid `pulse`, which is already the `Pulse` physical spec.
- **Native vs desugared**: prove/pilot/causal dispatch on instruction class. A drum desugar is already queued
  for pilot. Debounce/flasher/min_on could share that route (core lowers them to timer primitives plus
  internal state, while Click export emits the native coil), or get native classes taught to each analyzer.

---

## 2. Drum: Time & Event base (+ duration registers)

- The step advances when **duration elapsed AND event ON (level)**. Per step, either part may be blank:
  event-only, time-only, or both (OBJ-1019: each step needs at least one).
- The timing chart shows elapsed time counting from step start while the event is already on
  (step 4: X007 on mid-step, the step ends at 30 ms).
- Units ms/sec/min/hour/day as in Time base. Current step DS, elapsed TD, completion C, jog,
  load-new-step: all unchanged.

Open: does elapsed TD keep counting past the duration while it waits for the event (and is it clamped)?
Which banks are eligible for a duration register (presumably DS)?

pyrung today: `time_drum(presets=[Tag|int])` already accepts tag presets. Click validation rejects them
(`CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED`, `click/validation/hardware.py:462`). That check goes away for v4.
Proposal: `time_drum(..., events=[cond | None, ...])`, with `None` allowed in `presets` too. When
`events` is present, it exports as Time & Event base. That's one builder, not a third drum constructor.

---

## 3. Math expansion

| CLICK | Dec | Hex | pyrung proposal | Notes |
|---|---|---|---|---|
| `SUM(a:b)` | yes | yes | `rng.sum()` (exists) | already rendered `SUM ( a : b )` |
| SUM over bits | ? | ? | `rng.sum()` on a Bool range | `SumExpr` already counts True as 1; need capture for CLICK's rendering |
| `MAX(a:b)` | yes | yes | `rng.max()` | sibling of `.sum()` |
| `MIN(a:b)` | yes | yes | `rng.min()` | |
| `MDN(a:b)` | yes | yes | `rng.median()` | <= 1024 bytes; even-count rule unknown |
| `AVG(a:b)` | yes | yes | `rng.avg()` (or `.mean()`) | integer/rounding rule unknown |
| `RAND(lo,hi)` | yes | yes | `randint(lo, hi)` | inclusive like Python `randint`; args const or register |
| `ABS(x)` | yes | **no** | `abs(x)` (exists, `UnaryExpr`) | **bug today**: translator renders lowercase `abs(...)`; importer has no `ABS` mapping |

Implementation shape: make `SumExpr` one case of a `RangeAggregateExpr(kind)`. It is referenced in only five
files: `core/expression.py`, `core/memory_block.py`, `click/ladder/translator.py`, `circuitpy/.../_primitives.py`,
`core/analysis/crossings/calc.py`. Importer: add MAX/MIN/MDN/AVG/RAND/ABS to `_CLICK_FUNC_TO_PYTHON` and
generalise `_SUM_RE` (`click/codegen/utils.py`). Portability: `abs` goes into the decimal-only set.

**RAND is the real design problem.** pyrung's core promise is deterministic replay, seek and fork,
and the prover is exhaustive.
- Runtime: draw from a deterministic stream, e.g. seeded per (scan id, instruction id) from a runner seed,
  so history/fork replays reproduce the same values.
- Prover: treat each `randint` as a nondeterministic input over `[lo, hi]`.
- Tests: a runner-level seed/override hook so a test can force a value.

Open (hardware): AVG/MDN result for integer ranges (truncate vs round; MDN with even count = mean of
middle two, or the lower one?); RAND with lo > hi; MAX/MIN on DH treated unsigned?

---

## 4. For/Next: Break, Continue, nesting

- **Break**: exits the innermost loop immediately and continues after its Next. **Continue**: skips to the
  next iteration. Both are their own rung, inside the loop, and **require an enabling contact** (C000B/C000C).
- **Next** stays alone and unconditional. The **For** rung may have contacts (help: `X001 -> For 10`).
- Nesting up to 3, counting loops inside called subroutines. One-shot only on the outermost For.
  RETURN/END not allowed inside a loop.

### Conflict with pyrung's current model

`forloop()` today is *one rung*. Its body is a flat list of instructions that all run unconditionally,
with `branch()` forbidden inside it. Export emits each body instruction as a wire-only rung
(`click/ladder/instructions.py:50`). Break/Continue need a *conditioned rung inside the loop*, so the flat
body can't express them.

Related, **unverified**: if CLICK allows contacts on ordinary body rungs (I expect it does; v4 Break proves
conditioned rungs can live inside a loop), the importer drops them. `_emit_forloop`
(`click/codegen/emitter.py:838`) emits `body_rung.instructions` and never that rung's conditions. None of the
existing fixtures (`forloop__basic`, `forloop__oneshot`, `ladder_realistic_output/main.csv`) has a body rung
with a contact, so nothing has exercised this. **Capture one to settle it.**

### Proposal (back-compatible)

Allow `with Rung(...)` blocks inside `forloop`. Each block becomes a body rung with its own conditions.
Bare instructions keep their current meaning (unconditional rung).

```python
with Rung(Enable):
    with forloop(10) as loop:
        copy(Src[loop.idx + 1], Dst[loop.idx + 1])   # bare: unconditional, as today
        with Rung(Dst[loop.idx + 1] == 0):
            continue_loop()
        with Rung(Fault):
            break_loop()
```

- Names follow `return_early()` (Python reserves `break`/`continue`).
- Nesting: `_state._forloop_active` becomes a depth counter. `ForLoop.idx` is the fixed `Tag("_forloop_idx")`,
  so nested loops need distinct index tags.
- Runtime: signals like `SubroutineReturnSignal`, caught by the innermost `ForLoopInstruction.execute`.
- Validation (later, with gating): depth <= 3 across `call()`, one-shot only on the outer loop,
  `return_early` inside a loop is an error.

### Probe matrix: what CLICK actually does in a loop

Run these on a real C0-1x/C2 PLC before fixing the body model. Each one is a twin case (`tests/twin`
one-shot pattern). Using `For(oneshot=1)` gated on `slot.Cmd` makes each loop run exactly once, so the
counts are readable. F2, F6, F7 and F14 are where pyrung's current model is most likely wrong.

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
   SUM over a C range (see how CLICK spells SUMBIT); composed `MAX(DS1:DS10) + ABS(DS11)`.
   Math hex: `MAX(DH1:DH4)`, `RAND(...)`.
4. For/Next: **a v3-style loop with a contact on a body rung** (settles the importer question);
   Break with contact; Continue with contact; 3-deep nest; outer one-shot.
5. Scr side: save a v4 project with all of the above and run `decode_program()` on it.

## 6. Behaviour checks (twin / real PLC, since CLICK has no simulator)

Open questions are in sections 1, 2 and 3 above. The twin harness (`tests/twin/AGENTS.md`) is the vehicle:
same test against pyrung and a real C0-1x/C2 PLC.
