# pyrung API cleanup

No engine semantics change. Naming, ergonomics, doc rewrites.

## 1. `PLCRunner` → `PLC`, drop `.active()`, fold `dt=` into constructor

The class *is* the simulated PLC. `.active()` exists only to make `Tag.value =` route to a runner — that's the context manager's whole job, so put it on the object directly. `FIXED_STEP` is the default; `dt=` belongs in `__init__`.

```python
with PLC(logic, dt=0.010) as plc:
    Button.value = True
    plc.step()
    assert Light.value is True
```

**Constructor `dt` default is `0.010` (10 ms)**, not the current API default of `0.1` (100 ms). Every example in the docs uses 10 ms; the 100 ms default is too coarse for the timer examples to even fire. Match the docs.

`Program` stays. Build phase and run phase get one `with` block each — the boundary should be visible.

## 2. Stay in the context manager

The current docs show enter/exit-per-operation. The context has one job, so there's no reason to ever leave it during a test. Hoist once at the top:

```python
with PLC(logic, dt=0.010) as plc:
    State.value = "g"
    plc.run(cycles=299)
    assert State.value == "g"
    plc.step()
    assert State.value == "y"
```

Pure doc rewrite. Document the ambient-runner semantics once in the Runner guide:

> Entering a `PLC` context makes it the active runner for tag reads and writes. Contexts nest; the innermost wins.

`patch()` and `.value =` coexist after the cleanup. They serve slightly different mental models — `patch({Tag: value})` reads as "injecting stimulus into the simulation," `Tag.value = value` reads as "operating the HMI." Both are fine; let users pick.

## 3. Move *all* debug methods under `plc.debug.*`

The full list from the Runtime API reference is **eleven** debugger-internal methods currently sitting on the public `PLCRunner` class:

```python
plc.debug.scan_steps()
plc.debug.scan_steps_debug()
plc.debug.rung_trace(rung_id)              # was inspect()
plc.debug.last_event()                      # was inspect_event()
plc.debug.prepare_scan()
plc.debug.commit_scan(ctx, dt)
plc.debug.iter_top_level_rungs()
plc.debug.evaluate_condition_value(cond, ctx)
plc.debug.condition_term_text(cond, details)
plc.debug.condition_annotation(status, expression, summary)
plc.debug.condition_expression(cond)
```

DAP adapter still gets everything; `PLC` autocomplete stops being a wall of debugger internals.

The `system_runtime: SystemPointRuntime` attribute is also internal-looking — make it `_system_runtime` or move under `plc.debug.system_runtime`.

## 4. Time units as IEC-mirror strings

Drop `Tms` / `Ts` / `Tm` / `Th` / `Td` as importable symbols. Replace with strings of the same name:

```python
on_delay(Done, Acc, preset=3000)              # default "Tms", no kwarg
on_delay(Done, Acc, preset=30, unit="Ts")
on_delay(Done, Acc, preset=5, unit="Tm")
on_delay(Done, Acc, preset=2, unit="Th")
on_delay(Done, Acc, preset=1, unit="Td")
```

- Accepted: `"Tms"`, `"Ts"`, `"Tm"`, `"Th"`, `"Td"`. Default `"Tms"`.
- Mirrors IEC 61131-3 time literals (`T#1ms`, `T#1s`, `T#1m`, `T#1h`). `Td` is a pyrung extension; IEC stops at hours.
- The `m`/`ms` collision is harmless here: inside `unit="..."` the kwarg name is the disambiguating context, and `Tms` (two letters) vs `Tm` (one letter) is itself the visual disambiguator.
- Type hint: `Literal["Tms", "Ts", "Tm", "Th", "Td"]` so IDEs autocomplete and typos fail at type-check time.
- Validation error lists the valid set: `ValueError: unknown unit 'Tsec'; expected one of 'Tms', 'Ts', 'Tm', 'Th', 'Td'`.

**Codec round-trip is free.** `pyrung_to_ladder` keeps emitting `Tms` as a bare CSV token; `ladder_to_pyrung` wraps it in quotes when generating Python. Two tiny shims, no schema change, byte-identical CSVs.

The pedagogy that motivated `Tms`/etc. as constants — teach `OvenTm` over `OvenMin` for tag names — moves to a *Naming time-domain tags* section on the Tags page. Convention: longer suffix = smaller unit (`MotorTms` ms, `MotorTm` minutes).

## 5. Drop `_fn` variants

Both `run_until_fn` AND `when_fn` exist. Type-check the argument instead:

```python
plc.run_until(~Motor)                       # condition expression
plc.run_until(lambda s: s.scan_id >= 100)   # callable

plc.when(~Motor).pause()                    # condition expression
plc.when(lambda s: s.fault.plc_error).snapshot("fault")  # callable
```

Drop both `_fn` methods.

## 6. Drop `TimeMode` from public surface

Two values isn't worth an enum + setter + import line.

```python
PLC(logic, dt=0.010)        # fixed-step (default)
PLC(logic, realtime=True)   # wall-clock
```

`TimeMode` can stay internal.

## 7. Force API: one verb stem

```python
plc.force(Button, True)            # set
plc.unforce(Button)                # clear one
plc.clear_forces()                 # clear all
with plc.forced({Button: True}):   # scoped
plc.forces                         # inspect
```

After the `.active()` removal, scoped force blocks nest cleanly inside `with plc:`.

## 8. `set_battery_present` → property

```python
plc.battery_present = False
plc.reboot()
```

Battery presence is configuration state, not an action. The property form reads correctly: the battery isn't being *set* during reboot, it's being persistently absent.

## 9. Submodule import split

```python
from pyrung import PLC, Program, Rung           # runtime + structure
from pyrung.tags import Bool, Int, Real         # tag types
from pyrung.dsl import latch, reset, out, on_delay, rise   # instructions
```

Three short categorical lines. Keep flat re-exports so `from pyrung import *` still works.

## 10. udt-based timers in generic docs

`@udt` already gives you what a `Timer` factory would. The fix is documentation, not code:

```python
@udt()
class Timer:
    done: Bool
    acc: Int

Green = Timer()

with Rung(State == "g"):
    on_delay(Green.done, Green.acc, preset=3000)
with Rung(Green.done):
    copy("y", State)
```

- Generic docs (Quickstart, Concepts, learn/timers): rewrite around `@udt`.
- **Click dialect: unchanged.** Click ships timers as `T[i]` / `TD[i]` parallel blocks because that's the hardware convention.
- Optionally ship `pyrung.patterns` with pre-baked `Timer`/`Counter` udts.

## 11. Document numeric behavior loudly (and de-duplicate)

`copy` clamps; `calc` wraps; division by zero returns 0 and sets fault flags. The numeric behavior table is currently duplicated in `guides/runner` AND `instructions/math` — they will drift.

- **`instructions/math`** is the authoritative location. Already covers `calc` wrap behavior prominently. Keep.
- **`instructions/copy`** already has the clamp callout. Good.
- **`guides/runner`** — drop the duplicated table. Link to the instruction pages instead.
- **Reference fault flags by name**, not as "the fault flag." They live in the `system.fault` namespace:
  - `system.fault.division_error`
  - `system.fault.out_of_range`
  - `system.fault.math_operation_error`
  - `system.fault.address_error`
  - `system.fault.plc_error`
  - `system.fault.code` (the most recent fault code)

## 12. Click dialect: timer preset INT cap callout

Timer accumulators are 16-bit on Click hardware (32,767 max). The unit kwarg is therefore *load-bearing* for range — a 1-minute timer with `unit="Tms"` and `preset=60000` silently clamps to 32.7 seconds.

- **Click dialect docs:** add a loud preset-range table:
  | Unit | Max preset | Max duration |
  |------|-----------|--------------|
  | `Tms` | 32,767 | 32.7 seconds |
  | `Ts` | 32,767 | 9.1 hours |
  | `Tm` | 32,767 | 22.7 days |
  | `Th` | 32,767 | 3.7 years |
  | `Td` | 32,767 | 89 years |
- **Validate at construction time when targeting Click:** raise `ValueError: preset 60000 exceeds INT range; use unit="Ts" with preset=60` instead of clamping silently.
- **Generic Timers page:** one sentence pointing to dialect-specific limits. Core pyrung doesn't impose this cap.

## 13. Conditions: lead with comma form, document the precedence trap

Three ways to AND (comma, `&`, `all_of`) and two to OR (`|`, `any_of`) is fine — `all_of` and `any_of` are load-bearing for nesting (`Rung(Start, all_of(AutoMode, Ready), RemoteStart)` can't be expressed with comma alone). Keep all of them. But:

- **Lead with comma form everywhere in docs.** Most readable for the common N-AND case and sidesteps Python operator precedence.
- **Document the `&` / comparison precedence trap once and prominently.** `Fault & MotorTemp > 100` parses wrong without parens. Make it a callout, not a sentence.
- Use `&` / `|` operators sparingly in examples; reserve for cases where they genuinely read better.

## 14. Document the `system` namespace as a user-facing thing

The `system` namespace is a clean design — `system.sys.first_scan`, `system.rtc.hour`, `system.fault.division_error`, `system.firmware.main_ver_high`, `system.storage.sd.ready` — but right now it's only documented as a giant code block in the Runtime API reference, with no prose explaining how to *use* it.

Add a brief section to the Concepts or Runner page: *"System points are accessed via the `system` namespace: `system.sys.first_scan` for the first-scan bit, `system.rtc.hour` for time-of-day logic, `system.fault.division_error` to check for math faults, etc. See the Runtime API reference for the full tree."*

## 15. Small cleanups during the doc pass

- **Counter accumulator is positional** — the reference page shows it as `accumulator=` keyword while the Quickstart uses positional. Update the reference page to match.
- **`ScanContext` mention** in the Architecture page references an internal type on a user-facing doc. Rewrite: *"Inside a `with PLC(...)` context, tag reads and writes are routed through the runner's pending scan state and committed atomically when the scan completes."*
- **Slot configuration error message** — "Cannot configure slot N: tag has already been materialized. Configure slots before first access." (Verify the current error is at least this helpful.)

## Decisions deliberately *not* changing

- **Tag names stay explicit.** `Bool("Button")` is verbose but honest. The duplication is intentional pedagogy — Lesson 2 forwards-references Lesson 9 (udts) as the structural answer. The pain motivates learning the better pattern.
- **Timer/counter instruction names stay.** `on_delay`/`off_delay` is the canonical PLC name (TON/TOF); the most important word goes first.
- **`copy` clamps, `calc` wraps stays** — matches Click. Just document it loudly.
- **Three forms of AND, two of OR stays** — `all_of`/`any_of` are load-bearing for nested expressions, not redundant.
- **`oneshot=True` stays as a cross-cutting modifier** — every instruction supports it consistently. Withdrawing the earlier "flag toggle smell" complaint.
- **`patch()` and `.value =` both stay** — different mental models, both useful.

## Files to touch

- `getting-started/quickstart` — biggest payoff. Rename, hoist context, switch to udt timers, drop `Tms` import, use string unit form.
- `getting-started/concepts` — *Reading and writing values*, *Timers and counters*, add `system` namespace intro.
- `guides/runner` — class intro, ambient-runner sentence, drop `_fn`/`TimeMode`, move debug section to `plc.debug.*`, drop duplicated numeric table, `dt=0.010` default, `battery_present` property.
- `guides/testing` — every example: hoist `with plc:`, switch to `forced()` ctx mgr.
- `guides/forces-debug` — verb stem consistency.
- `guides/architecture` — rewrite `ScanContext` paragraph without the internal type name.
- `guides/click-quickstart` — same enter/exit fix as the generic Quickstart.
- `learn/tags` — add *Naming time-domain tags* section with `Tms`/`Tm` convention.
- `learn/timers` — rewrite "Why Tms?" callout for the string form, switch to udt pattern, ambient runner update.
- `learn/counters` — same shape as timers.
- `learn/structured-tags` — heavy `PLCRunner` / `runner.active()` usage, full rewrite.
- `instructions/conditions` — comma-first examples, precedence trap callout.
- `instructions/math` — keep as authoritative numeric behavior page; reference fault flags by name.
- `instructions/copy` — reference fault flags by name.
- `instructions/timers-counters` — counter accumulator positional fix, unit string update.
- `dialects/click` — preset INT cap table, validation error spec.
- `reference/api/runtime` — full debug-method relocation, drop `Tms`/etc. symbol entries, drop `TimeMode` enum entry, `set_time_mode` removal.

## Codec changes (laddercodec)

- `pyrung_to_ladder`: no change — `Tms` already emitted as bare CSV token.
- `ladder_to_pyrung`: wrap unit token in quotes when generating Python (`unit=Tms` → `unit="Tms"`).
- Two-line shim. No schema change, no CSV migration.

## Migration

Nothing's in the wild. Search-and-replace and ship.

---

# Round 4 — additions from Rungs / Communication / Click dialect / CircuitPython

After reading the remaining instruction pages, the Click dialect spec, and the CircuitPython dialect, only a handful of new findings. Most of what's left is well-designed and confirms patterns already established.

## 16. Validation API divergence between dialects

The Click dialect and CircuitPython dialect have meaningfully different shapes for the same conceptual operation — *validate this program against a target's constraints*.

**Click:**
```python
report = mapping.validate(logic, mode="warn")
report.findings           # flat list
finding.level             # severity field
```

**CircuitPython:**
```python
report = validate_circuitpy_program(program, hw, mode="warn")
report.errors             # categorized
report.warnings
report.hints
finding.severity          # different field name
```

Two divergences:

1. **Shape:** Click attaches validation to the mapping object (`mapping.validate(...)`); CircuitPython exposes a top-level function. Click's shape is better — validation rules belong to the target (mapping/hw), not floating in the namespace. CircuitPython should match: `hw.validate(logic, mode="warn")`.
2. **Finding access:** Click is a flat list with a `level` field; CircuitPython is categorized into `errors`/`warnings`/`hints` with a `severity` field. Pick one. Flat list with a `severity` field is more flexible (callers can filter/group however they want), and "severity" is the more standard name.

After convergence:
```python
# Click
report = mapping.validate(logic, mode="warn")

# CircuitPython
report = hw.validate(logic, mode="warn")

# Both:
for f in report.findings:
    print(f"{f.severity}: {f.code} — {f.message}")
```

Same shape across dialects. Finding `code` field already exists on both — that's good.

## 17. CircuitPython dialect is real, not vestigial

You wondered whether CircuitPython is leftover cruft. It isn't. The page documents:

- `P1AM` hardware model with 35 modules across six categories
- `hw.slot()` API returning typed `InputBlock` / `OutputBlock` / `tuple` for combo modules
- `generate_circuitpy()` produces a complete self-contained `.py` file with imports, hardware bootstrap, tag declarations, retentive SD persistence, schema-hash invalidation, NVM dirty flag for crash recovery, watchdog support, and a paced `while True` scan loop
- `validate_circuitpy_program()` with finding codes (`CPY_FUNCTION_CALL_VERIFY`, `CPY_IO_BLOCK_UNTRACKED`, `CPY_TIMER_RESOLUTION`)
- SD command bits wired to `system.storage.sd.*` so ladder logic can trigger persistence
- Excluded modules documented (`P1-04PWM`, `P1-02HSC` deferred to v2)

The "Aspirational PoC" framing in the doc undersells it. This is real, working codegen for a real PLC-class controller, and it's the *only* place where the "write once, simulate and deploy" thesis is demonstrated end-to-end. Keep it. If anything, the doc should be a touch more confident about what works today vs what's deferred.

(There's no overlap with this cleanup unless you want to address the validation divergence above.)

## 18. `hw.slot()` combo-module return is a bare tuple

```python
inputs, outputs = hw.slot(1, "P1-16CDR")   # combo discrete in/out module
```

Bare tuple unpacking is fine but easy to get wrong (which is which?). A `NamedTuple` would be clearer:

```python
io = hw.slot(1, "P1-16CDR")
io.inputs[1]   # explicit
io.outputs[1]
```

`hw.slot()` already returns three different shapes depending on module type (`InputBlock`, `OutputBlock`, or tuple). Making the third shape a `NamedTuple` (or a tiny dataclass) eliminates positional ambiguity without breaking the existing tuple-unpacking sites.

Minor — only matters for combo modules, which are 3 of 35. Do during the doc pass if at all.

## 19. `target_scan_ms` vs `dt` unit inconsistency

```python
plc = PLC(logic, dt=0.010)                              # seconds
source = generate_circuitpy(logic, hw, target_scan_ms=10.0)  # milliseconds
```

Same concept (scan period), different units, different namespaces. This is mild — they're in different dialects and the suffix tells you the unit — but if you're tightening things up, `target_scan_dt=0.010` would match. Probably not worth the breakage given CircuitPython is dialect-isolated.

## 20. `system_runtime` and other internal attributes — privatize

You agreed: rename `system_runtime` → `_system_runtime` (or move under `plc.debug.system_runtime` if the DAP adapter needs reflective access). Same treatment for any other "_runtime" / "_state" / engine-internal attribute that currently lives on the public class without a user-facing purpose. Audit during the debug-namespace move.

## 21. The Click dialect's "DSL naming philosophy" section is gold — reference it

`dialects/click` has an explicit, well-argued section explaining *why* certain Click instruction names were renamed in pyrung:

- `SET` → `latch` (shadows Python builtin `set`)
- `MATH` → `calc` (shadows Python stdlib `math`)
- `RET` → `return_early` (normal return is implicit; only early exit needs a call)

This is exactly the kind of "why we did this" documentation that prevents future contributors from second-guessing the naming. **Reference this section from the Concepts page or the Architecture page** so newcomers find it without having to dig into a dialect spec. It's also the perfect place to add: *"Time units use string form (`unit='Tms'`) rather than imported constants — see Lesson 5 for the convention."* once the time-unit cleanup lands.

## 22. Communication instructions (deferred)

You said `CommStatus` is a later cleanup, so skipping the prescription. For the record, the alignment is:

```python
@udt()
class CommStatus:
    sending: Bool      # or receiving
    success: Bool
    error: Bool
    exception_response: Int

# Becomes:
send(target=peer, remote_start="DS1", source=DS.select(1, 10), status=CommStatus())
```

Same pattern as the udt-for-timers approach, same justification (four tags that always travel together), same Click compatibility story (the dialect can still expose them as parallel blocks if hardware needs it). Park it for later.

## Things deliberately *not* changing — additions to the list

- **`Rung.continued()` stays.** Documented as load-bearing for codegen round-tripping; the visual rung structure in Click ladder editors needs this to survive import/export. Do not touch.
- **`Int2`, `Bit`, `Float`, `Hex`, `Txt` Click aliases stay.** These are the dialect's job — Click users expect Click names.
- **The drum/shift/search builder chains stay.** Backslash multi-line chains are consistent across all the terminal builders (timer/counter/drum/shift). The pattern is the pattern.
- **Comment system, nested branches, continued rungs, drum builders** — all the things that exist "for codegen round-tripping" are load-bearing for the laddercodec roundtrip story. None of them should be touched. (Note: `comment()` function stays; `r.comment` attribute removed — see #28.)

## Updated files-to-touch additions

- `dialects/circuitpy` — converge validation API to `hw.validate(logic, mode=...)` matching Click; rename `severity` → consistent with Click after picking a winner.
- `dialects/click` — converge `finding.level` → `finding.severity` (or vice versa); brief mention of CircuitPython as the second working dialect in any "what dialects exist" overview.
- `instructions/drum-shift-search` — apply the unit string change to `time_drum(unit="Tms")`.

## Stuff I read that has zero findings

Listing these so you know they were checked:

- `instructions/rungs` — comments, branches, `continued()` — all clean.
- `instructions/communication` — clean (modulo the CommStatus udt opportunity you're already planning).
- `instructions/drum-shift-search` — clean except for the unit string update. Good builder consistency.
- `guides/tag-structures` — clean throughout. Best-designed part of the API.
- `instructions/copy` — clean. Clamp callout already prominent.
- `dialects/click` (full read) — clean. The DSL naming philosophy section is exemplary.
- `dialects/circuitpy` — clean except for the validation API divergence in #16.

---

# Round 5 — additions from formal API references and codegen

After reading the Program Structure API, Click Dialect API, and Click codegen pages, several findings — most about internal methods leaking onto user-facing classes.

## 23. Validation has THREE entry points — should be at most two

The formal API references reveal there are three documented ways to validate a Click program:

```python
# Form 1: Method on TagMap (the one in dialects/click guide)
report = mapping.validate(logic, mode="warn")

# Form 2: Top-level function
report = validate_click_program(logic, mapping, mode="warn")

# Form 3: Method on Program with dialect parameter
report = logic.validate(dialect="click", mode="warn", tag_map=mapping)
```

Form 3 is actually the universal entry point — `Program.validate(dialect, **kwargs)` dispatches to whichever dialect validator was registered via `Program.register_dialect()`. This is **really nice architecture** that I want to call out specifically: it means new dialects can be added by external packages without modifying core, and there's a single canonical entry point regardless of dialect.

But then forms 1 and 2 exist as parallel shortcuts, which:
- Forces the docs to choose one (currently `mapping.validate()`).
- Hides the universal pattern from users who only learn the shortcut.
- Means CircuitPython invented its own form (`validate_circuitpy_program()`) instead of registering with `Program.validate(dialect="circuitpy", hw=hw)`.

**Recommendation:** keep Form 3 as canonical, keep the dialect-specific shortcut method (Form 1), drop the standalone function (Form 2).

```python
# Universal (always works):
report = logic.validate(dialect="click", tag_map=mapping)
report = logic.validate(dialect="circuitpy", hw=hw)

# Dialect convenience (delegates to universal):
report = mapping.validate(logic)        # Click
report = hw.validate(logic)             # CircuitPython
```

Drop `validate_click_program()` and `validate_circuitpy_program()` as standalone functions. They're redundant with both Form 3 (canonical) and Form 1 (convenience).

The CircuitPython convergence in #16 then becomes: register the CircuitPython validator with `Program.register_dialect("circuitpy", ...)` and add a `hw.validate(logic)` shortcut method. CircuitPython gets the same shape as Click *and* slots into the universal entry point.

Document `Program.register_dialect()` prominently in the Architecture or dialects overview page — it's the pluggability story and it's currently buried in the formal API reference.

## 24. `Program` has internal methods leaked onto its public surface

The Program Structure API documents these as user-callable methods on `Program`:

```python
program.add_rung(rung)              # called by `with Rung():`
program.start_subroutine(name)      # called by `with subroutine():`
program.end_subroutine()            # called by `with subroutine():`
program.call_subroutine(name, state)        # "legacy state-based API"
program.call_subroutine_ctx(name, ctx)      # current API
program.evaluate(ctx)               # called by the engine
program.current()                   # called by `with Rung():` to find parent
```

None of these should be on the user-facing surface. Users interact with Program through `with Program() as logic:`, `with Rung():`, `with subroutine():`, and `call(...)`. The methods above are the *implementation* of those context managers and should be private (`_add_rung`, `_start_subroutine`, `_evaluate`, `_current`, etc.).

`call_subroutine` is explicitly labeled "legacy state-based API." If nothing uses it anymore, **delete it.** Legacy methods on a zero-stars project are pure noise. If something internal still uses it during a transition, mark it `_legacy_call_subroutine` and put it on the deletion list.

## 25. `TagMap` has the same problem — many undocumented public methods

From the Click Dialect API reference, `TagMap` has these methods:

**Documented for users:**
- `from_nickname_file()`, `to_nickname_file()` — file I/O
- `validate()` — validation
- `map_to()` (via Tag) — mapping construction

**Not documented in user-facing pages, but on the public class:**
- `resolve(source, index)`
- `offset_for(block)`
- `block_entry_by_name(name)`
- `mapped_slots()`
- `tags_from_plc_data(data)`
- `owner_of(display_address)`

These look like internal helpers used by `ClickDataProvider`, the codec, and the validator. They shouldn't be public API. Either privatize (`_resolve`, `_offset_for`, etc.) or move under a `mapping.internals.*` namespace if other parts of `pyrung.click` need to call them across module boundaries.

Same logic as the `plc.debug.*` move — keep autocomplete on the user-facing class focused on what users actually need.

## 26. `run_function` / `run_enabled_function` naming is confusing

```python
run_function(fn, ins=..., outs=..., oneshot=False)
run_enabled_function(fn, ins=..., outs=...)
```

Both run a Python function inline as an instruction (escape hatch for things the DSL doesn't model directly — used by CircuitPython codegen). The difference:

- `run_function` — only runs when rung is true
- `run_enabled_function` — runs every scan, receives the rung enabled state as context

The names don't communicate the difference. Both are "running an enabled function" in some sense. Consider:

```python
run_function(fn, ...)                       # runs when rung enabled (the common case)
run_function(fn, always=True, ...)          # runs every scan, gets enable state
```

Or two distinct verbs:

```python
call_python(fn, ...)            # only when rung true
tick_python(fn, ...)            # every scan, with enable state
```

The current pair will trip people up because `enabled_function` reads as "the function that's enabled" rather than "the function that always runs and is told whether it's enabled." Minor priority — these are escape hatches, not core API.

## 27. Builder base classes leaking into API docs

The Program Structure API reference shows:

```
class CountUpBuilder(_BuilderBase): ...
class OnDelayBuilder(_AutoFinalizeBuilderBase): ...
```

Users will never instantiate or subclass these. The `_BuilderBase` / `_AutoFinalizeBuilderBase` underscore-prefixed parents are internal — but they show up in autogenerated API documentation as the "Bases:" line. Either:

- Hide them from the doc generator (mkdocs / sphinx config), or
- Rename to remove from the public API (already underscore-prefixed, just don't render).

Cosmetic — fix during the autogen-doc cleanup pass.

## 28. `comment()` is canonical — remove `r.comment` attribute

The Rungs instruction page shows:
```python
with Rung(Button) as r:
    r.comment = "Initialize the light system."
    out(Light)
```

The Program Structure API and the formal `comment()` docs show:
```python
comment("UnitMode Change")
with Rung(C_UnitModeChgRequest):
    copy(1, C_UnitModeChgRequestBool, oneshot=True)
```

These aren't equivalent — they imply different mental models for *where* the comment lives.

In every ladder editor (Click included), comments render **above** the rung as a header line, separated from the instruction body. The top-level `comment("text")` form matches that exactly: declared before the rung, lives above it visually, reads top-to-bottom in source order. The `r.comment = "..."` form puts the comment assignment lexically *inside* the rung body, mixed in with `out(Light)` and other instructions, even though the comment is supposed to live outside and above. The source code lies about the structure.

`r.comment` is one of three things:

1. **Dead code** — the attribute exists but nothing reads it during codegen. Users write it, lose their comments silently on export.
2. **Works but wrong placement** — emits inline with instructions or somewhere visually wrong.
3. **Works correctly** — pure redundancy that duplicates `comment()`.

In all three cases the answer is the same: **delete `r.comment` and the Rungs page section that documents it.** Only `comment("text")` before `with Rung(...):` should remain.

Verification before deletion: write a tiny test that uses `r.comment`, run `pyrung_to_ladder`, inspect the CSV for where the comment lands. Determines which of the three cases it is — but doesn't change the cleanup, only the urgency. (Case 1 is "users have silently broken comments today" which is a real bug; cases 2 and 3 are "works but should be removed for clarity.")

After removal, the Rungs page example becomes:

```python
comment("Initialize the light system")
with Rung(Button):
    out(Light)
```

Multi-line variant (using the existing dedent + 1400-char limit):

```python
comment("""
    This rung controls the main light.
    It activates when Button is pressed.
""")
with Rung(Button):
    out(Light)
```

No `as r:` ceremony, comment lives lexically and visually above the rung.

## 29. The Click codec round-trip story is genuinely impressive — and round-trip-tested

`ladder_to_pyrung()` and `ladder_to_pyrung_project()` are deeper than I thought:

- Imports from CSV, directory, or in-memory `LadderBundle` (no disk I/O for round-trip tests).
- Reconstructs `@named_array` and `@udt` from `:block`, `:udt`, `:named_array(...)` markers in the nickname CSV.
- Infers `always_number=True` from numbered singleton names.
- Detects whole-instance windows and emits `RecipeProfile.instance(2)` instead of flat ranges.
- Per-file imports in project mode — only imports what each file uses.
- Subroutines become decorated functions in their own files; main calls them by reference, not string name.
- Round-trip is tested: `exec(ladder_to_pyrung(bundle))` then `pyrung_to_ladder(logic, mapping)` reproduces the original CSV.

The only thing the cleanup affects here is the unit-string change — `ladder_to_pyrung` will need the two-line shim from #4 to wrap `Tms` in quotes when emitting Python. You already noted this.

**One observation:** the codec does a lot of inference work that *depends on the nickname CSV being present.* Without it, structures come back flat. The docs are clear about this, but it's worth noting that `Tms` quoting is the *only* thing the unit-string change adds to this story — everything else is unaffected because units are already tokens in the CSV format, not Python identifiers.

## Updated Round 5 additions to files-to-touch

- `reference/api/program-structure` — privatize `add_rung`, `start_subroutine`, `end_subroutine`, `call_subroutine` (delete legacy), `call_subroutine_ctx`, `evaluate`, `current`. Hide `_BuilderBase` / `_AutoFinalizeBuilderBase` from autogen.
- `reference/api/click-dialect` — privatize `resolve`, `offset_for`, `block_entry_by_name`, `mapped_slots`, `tags_from_plc_data`, `owner_of`. Drop `validate_click_program` standalone function.
- `reference/api/circuitpy-dialect` — drop `validate_circuitpy_program` standalone; register with `Program.register_dialect("circuitpy", ...)`; add `hw.validate(logic)` shortcut.
- `instructions/rungs` — add the `comment()` top-level form alongside `r.comment`.
- `dialects/click-codegen` — verify the unit-string shim works on import side (`Tms` → `"Tms"`). No structural changes.
- New page or section: **Architecture / Dialects overview** — document `Program.register_dialect()` and the universal `program.validate(dialect, **kwargs)` entry point. This is the cleanest architectural story in the codebase and it's currently invisible.

## Stuff that's actually clean in Round 5

- The `Program.register_dialect()` plugin pattern — really nice, just needs surfacing.
- `LadderBundle` round-trip without disk — clean abstraction.
- Per-file imports in project codegen — careful work.
- Named-array whole-instance window detection — clever.
- The dual `comment()` / `r.comment` ergonomics — both have reasonable use cases.
- `forloop(count, oneshot=False)` — `oneshot` here means "execute the entire loop once per rung enable" which is consistent with the rest of the DSL.
- The instruction set as a whole — all instructions accept `oneshot=` consistently, all builders use the same chaining pattern, all variadic conditions accept tuple/list/positional uniformly.

The DSL is consistently designed across instructions. The leaks are all at the **engine boundary** — methods and helpers that exist for the runtime to call but accidentally got left on public classes.

## Final ranking with Round 5 items

1. PLC rename + stay-in-context (foundational)
2. Universal validation entry point (#23) — architectural cleanup, big readability win
3. Privatize internal methods on `Program` and `TagMap` (#24, #25)
4. Move debug methods under `plc.debug.*` (#3, expanded list)
5. udt-based timers in generic docs
6. Time unit strings + codec shim
7. Submodule import split
8. Force stem consistency
9. Numeric behavior docs (de-duplicate, name fault flags)
10. Click preset INT cap callout + validation
11. CircuitPython validation convergence (#16, now subsumed by #23)
12. Conditions: comma-first
13. `system` namespace documentation
14. Drop `_fn`, drop `TimeMode`, `battery_present` property
15. Small cleanups: `comment()` doc parity, `_BuilderBase` autogen hide, `run_function` rename, counter accumulator positional
16. Decisions deliberately *not* changing: tag names, `oneshot=`, `Rung.continued()`, `r.comment` / `comment()`, `Int2`/`Bit`/etc. aliases, drum builder chains
