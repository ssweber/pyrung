# CLICK v4: touchpoints per feature

Read-only survey at `dev` HEAD `0a57b20` (2026-10-22). Companion to `RESEARCH.md`. For each planned
feature: every file in code, tests and docs that has to change, so that build agents can be briefed.

**Confidence column**: **seen** means I read the code at that line. **inferred** means I followed the
pattern (a sibling class, a generic walker) and did not trace it all the way. Line numbers are at this HEAD.

**Method**: I traced each feature from its nearest sibling (`SumExpr`/`UnaryExpr`, `TimeDrumInstruction`,
`OutInstruction`/`OnDelayInstruction`, `ForLoopInstruction`/`SubroutineReturnSignal`). I grepped for
`isinstance` and for class-name strings (`type(x).__name__ == "..."`, `"OutInstruction"` keys). I also read
the duck-typed attribute walkers (`getattr(instr, "target")`). The drum commit `42efd0e` was too old to use
as a checklist; the code has grown prove, pilot, crossings and fold since. So the checklist is the current
grep surface of the sibling class.

---

## 0. Cross-cutting facts every feature inherits

### 0.1 Six execution paths (all must agree)

| # | Path | File / lines | Notes |
|---|---|---|---|
| 1 | `Instruction.execute` via `Rung.execute` | `core/rung.py:230-260`, `core/instruction/*.py` | also used by `causal/projected.py:376` (single-rung simulation) |
| 2 | Executor, natural (fast) | `core/executor.py:1086-1243` | `_execute_for_loop_natural` 1204, `_execute_call_natural` 1175 |
| 3 | Executor, observed | `core/executor.py:1293-1585` | `_execute_for_loop_instruction` 1522; observers `begin/end_loop_iteration` |
| 4 | Debugger stepping | `core/debug_handlers.py` (ForLoop handler 169-200), `core/debugger.py:133-200, 385-410` | re-implements loop iteration |
| 5 | Compiled kernel | `circuitpy/codegen/compile/_core.py:545-605` dispatch → `render_kernel.compile_kernel` (106) | used by `core/compiled_plc.py` (replay), `core/scan_log.py` replay, the prover (`prove/kernel.py`) |
| 6 | CircuitPython | same compile package + `circuitpy/codegen/render*.py` | |

`make test` runs `--runner-backend=both` (`tests/conftest.py:127-140, 170-178`). It asserts that **tags and
memory dicts are identical** between the interpreted and compiled backends. Every new memory key must
therefore be spelled the same in the compiled code.

### 0.2 Forcing functions (a new class or export fails here first)

| What | Where |
|---|---|
| Every concrete `Instruction` subclass must be in `EXPECTED_WITH_CROSSINGS` or `EXEMPT` | `tests/core/analysis/test_crossings_coverage.py:63, 92, 137` |
| Every public rung verb must have a graph-matrix case | `tests/core/analysis/test_instruction_analysis_coverage.py:849-878` (`PUBLIC_RUNG_INSTRUCTIONS`), test at 882 |
| Every `pyrung.__all__` / `pyrung.click.__all__` symbol must be on a reference page | `docs/gen_reference.py:11, 217-255` (`_validate_manifest`); runs in `make verify` → `docs-check` |
| CSV token grammar is a contract shared with laddercodec | `laddercodec/src/laddercodec/csv/{contract,token_parser,writer}.py` |

### 0.3 Class-name string tables (grep for `isinstance` misses these)

`causal/why.py:221-247` (`_INSTRUCTION_LABELS`), `core/validation/render.py:48-71` (`_VERB`) and `:210+`,
`click/validation/hardware.py:43-88` (`_WRITE_TARGET_FIELDS`, `_ROLE_ASSIGNMENT_FIELDS`), `:384`, `:462`,
`:501`, `click/validation/portability.py:375`, `click/ladder/instructions.py:57, 117, 147, 173, 185, 333, 441`,
`click/ladder/_exporter.py:58-62` (`_RENAME_TABLE`), `:305`, `click/ladder/layout.py:244`,
`click/ladder/validator.py:90, 97, 218`, `circuitpy/validation.py:289`, `core/validation/pointer_sites.py:69, 175`,
`core/validation/walker.py:493`, `core/analysis/init_constants.py:141`, `prove/classify.py:108-153`.

### 0.4 Dead tables (do not rely on them)

- `prove/classify.py:108-153`: `_OTE_INSTRUCTION`, `_STATEFUL_INSTRUCTIONS` and `_TIMER_COUNTER_INSTRUCTIONS` are
  defined and **never read**. Update them for consistency, or delete them.
- `Instruction.exclusive_resources()` (`core/instruction/base.py:63-74`) has **no callers**. Duplicate-timer
  detection (CLICK C0112) actually comes from `duplicate_out` via `_instruction_write_targets`.

---

## 1. Math aggregates (`.max/.min/.median/.mean`, Bool `.sum()`, `abs` fix)

Sibling: `SumExpr` (`core/expression.py:473-488`) and `UnaryExpr(symbol="abs")` (`:294-308`, created by
`Expression.__abs__` at `:137`). RESEARCH says `SumExpr` is in five src files. I confirmed that:
`expression.py`, `memory_block.py`, `click/ladder/translator.py`, `circuitpy/.../_primitives.py`,
`crossings/calc.py`, plus two test files. Aggregates also need arms in files that ignore `SumExpr` today:
portability depth, `render_expr`, `format_expr`.

### A. DSL surface
| File | Change | Conf. |
|---|---|---|
| `core/expression.py:473-488` | `SumExpr` → `RangeAggregateExpr(block_range, kind)` with kinds sum/max/min/median/mean. Keep `SumExpr` as a name (subclass or alias): tests import it (`tests/click/test_ladder_export.py`, `tests/core/test_expression.py`) | seen |
| `core/expression.py:491-533` `format_expr` | arm at 506-508 prints `sum(...)` → per-kind | seen |
| `core/memory_block.py:1180-1184` | add `.max() .min() .median() .mean()` next to `.sum()`. Only `BlockRange` has `.sum()`; `IndirectBlockRange` has none (keep it that way, since CLICK needs a fixed range) | seen |
| `__init__` re-exports | none; these are methods. `SumExpr` is not exported (inferred from the grep of `core/__init__.py`) | inferred |
| type stubs | none; `core/tag.pyi` and `structure.pyi` do not cover `BlockRange` | seen |

### B. Interpreted runtime
| File | Change | Conf. |
|---|---|---|
| `core/expression.py` | `evaluate` per kind. Bool ranges already work for sum (`sum()` counts True as 1). Mean and median return float, so the calc truncation in `core/instruction/calc.py:152-176` decides rounding (probes M1/M2) | seen |
| `core/instruction/calc.py:41-101` `infer_calc_mode` | walks generically; Bool counts as the decimal family. Probably no change | inferred |

### C. Compiled
| File | Change | Conf. |
|---|---|---|
| `circuitpy/codegen/compile/_primitives.py:190-200` | `SumExpr` arm → per-kind emission (`max(...)`, `min(...)`, inline median and mean) | seen |
| `core/kernel.py`, `core/scan_log.py` | none (they consume compiled code) | inferred |

### D. CircuitPython
| File | Change | Conf. |
|---|---|---|
| `_primitives.py` (same arm) | CircuitPython has no `statistics` module, so median and mean must be inline (`sorted`, `len`) | inferred |
| `circuitpy/validation.py` | none | inferred |

### E. Ladder export
| File | Change | Conf. |
|---|---|---|
| `click/ladder/translator.py:449-454` | `SUM ( a : b )` → `MAX/MIN/MDN/AVG ( a : b )`; Bool banks pass as-is | seen |
| `click/ladder/translator.py:425-431` | **abs bug**: `self._fn("abs", inner)` renders lowercase → render `ABS` | seen |

### F. Ladder import
| File | Change | Conf. |
|---|---|---|
| `click/codegen/utils.py:64` `_SUM_RE` | generalise to `(SUM\|MAX\|MIN\|MDN\|AVG)`. **Not** via `_CLICK_FUNC_TO_PYTHON`: these take a colon range, not call args | seen |
| `click/codegen/utils.py:215-242` `_validate_operand` | `_range_sum` placeholder and allowed-function set → per-kind placeholders | seen |
| `click/codegen/utils.py:329-336` (`SUM` branch in `_sub_operand`), `:379-386` (`_sub_sum`) | per-kind `.max()` etc. | seen |
| `click/codegen/utils.py:34-50` | add `ABS: abs`. `abs` is a builtin, so exclude it from `_EXPR_FUNC_IMPORT_NAMES` (73) and `_PYTHON_EXPR_FUNC_RE` (76-78); otherwise the emitter writes `from pyrung.core.expression import abs` (`emitter.py:400-403`, `project_emitter.py:614, 830`) | seen |
| `click/codegen/emitter.py:80-90` | `used_expr_funcs` collection follows the table | seen |

### G. Validation
| File | Change | Conf. |
|---|---|---|
| `click/validation/portability.py:419-458` `_expr_paren_depth` | returns 0 for `SumExpr` today; aggregates (and `abs`) should add one level | seen |
| `click/validation/portability.py:463-520` | "abs into the decimal-only set" does not fit the current shape: `_DECIMAL_ONLY_MATH_FUNCS` is only checked for `MathFuncExpr`, and `abs` is a `UnaryExpr`. Add a `UnaryExpr(symbol="abs")` arm. Update the comment table at 463-465 (aggregates allowed in hex) | seen |
| `click/validation/findings.py` (+ `click/validation/__init__.py` `__all__`) | new codes: MDN range > 1024 bytes (OBJ-1013); MAX/MIN/MDN/AVG on a Bool range (only SUM is legal on bits) | inferred |
| `core/validation/render.py:125-137` `render_expr` | no arm for `SumExpr`/aggregates (prints the class name); renders abs as `absX`. Both are pre-existing bugs | seen |
| core validators (`math_conditions.py`, `cmp_conditions.py`) | none (inferred: generic) | inferred |

### H. Analysis
| File | Change | Conf. |
|---|---|---|
| `core/analysis/crossings/calc.py:26, 65, 133-134, 227, 268` | `Aggregate` sign-oracle seam. Max, min, median and mean are monotone per member but not additive, so any exact-solve arm must branch on kind | seen |
| `prove/classify.py:555-565` `_expr_reads_tag` | does not descend into `SumExpr` (pre-existing gap); aggregates have the same gap | seen |
| `pdg.py:679-690, 789-815`, `validation/walker.py:214-220` | generic `vars()` walkers pick up the `BlockRange` attribute automatically | inferred |
| `affine.py`, `reverse_edges.py:64-128`, `sp_values.py` | unknown expression → `None` (safe) | inferred |

### I. Debug/DAP
None. DAP text uses the generic formatters; `format_expr` covers it.

### J. Tests
`tests/core/test_expression.py`, `tests/core/test_expression_format.py`, `tests/core/test_calc.py` (parity via
`runner_factory`), `tests/click/test_ladder_export.py` (SUM and abs rendering), `tests/click/test_codegen.py`
(import of SUM/MAX/ABS), `tests/click/test_validation.py` (portability depth and mode),
`tests/circuitpy/test_codegen.py`, `tests/core/analysis/test_crossings_calc*.py` (inferred name). Capture
goldens come from laddercodec (section L).

### K. Docs
`docs/instructions/math.md` (the `.sum()` section around line 48; add the four methods, Bool sum and `abs`),
`docs/dialects/click.md:102-116` (naming table: `mean`→AVG, `median`→MDN), both cheatsheets
(`docs/guides/click-cheatsheet.md`, `src/pyrung/click/codegen/data/click-cheatsheet.md:139`; **they already
differ**), `CHANGELOG.md` Unreleased.

### L. Outside
laddercodec: `src/laddercodec/instructions/math.py:108-130` `RESERVED_NAMES` add MAX, MIN, MDN, AVG, RAND, ABS,
plus the formula token encoding (tokens 0x1e-0x23 per BINARIES-PC) and goldens. clicknick: capture only.

**Size: S-M.** One expression class and about 8 src files, plus an importer regex and laddercodec tokens. The
semantic unknowns (M1/M2/M4) are probes, not code.

---

## 2. `randint(lo, hi)`

Siblings: `ShiftFuncExpr` and `lsh()` for the two-argument expression path (`core/expression.py:416-441`).
Forces and the RTC (`runner.py:3509`, `scan_log.py:133-145`) for "runner state that replay must reproduce".

### A. DSL surface
| File | Change | Conf. |
|---|---|---|
| `core/expression.py` | new `RandIntExpr(lo, hi, site)` + `randint()` beside `lsh` (434). `format_expr` arm | seen (pattern) |
| `core/__init__.py:27-32, 263-274` | export next to `sqrt`/`lsh`. The math functions are **not** top-level `pyrung` exports today. RESEARCH writes `from pyrung import randint`; if it goes top-level, also edit `pyrung/__init__.py` and `docs/gen_reference.py` (instruction-set page, 135-180) | seen |
| `core/program/context/_program.py:73-96` `_assign_state_keys` | natural hook to stamp a stable **site id** on each `randint` (walk the expression trees of calc instructions, including inside loop bodies and subroutines) | inferred |
| `core/instruction/calc.py:138` | reject `randint` outside `calc()` (in `ExprCompare` conditions or a copy source) at build time. CLICK only allows RAND in math | inferred |

### B. Interpreted runtime
| File | Change | Conf. |
|---|---|---|
| `RandIntExpr.evaluate(ctx)` | needs seed, `scan_id`, site and iteration. `ConditionView.scan_id` (`context.py:176`, class at 64) and `ScanContext.scan_id` (`context.py:528`) both exist, as does `get_memory` on both | seen |
| seed storage | Option 1: runner field, propagated in `PLC.fork` (`runner.py:2186`), `fork_from` (2250), `replay_to` (2911), `reboot` (3479) and `CompiledPLC` (`core/compiled_plc.py:202+`). Option 2 (simpler): a `SystemState.memory` key written each scan like `_dt`. That travels through fork, seek and replay automatically, but must be excluded in prove `_pass_discover_memory_keys` (`prove/passes.py:1749-1757`) | inferred |
| iteration | needs a loop-iteration stack on the context; nesting comes from feature 5 | inferred |
| override hook | runner API (`runner.py`, `compiled_plc.py`), recorded in `ScanLog` like `record_force_changes` (`scan_log.py:133`) and `rtc_base_changes` (`:143`) so replay reproduces forced draws | inferred |

### C. Compiled
| File | Change | Conf. |
|---|---|---|
| `circuitpy/codegen/render_kernel.py:643, 659-662` | the step function `_kernel_step(tags, blocks, memory, prev, dt)` has **no scan_id**. Inject it (for example `memory["_scan_id"]` beside `memory["_dt"]`); `ReplayKernel` already has a `scan_id` slot (`core/kernel.py:53, 68, 99`) | seen |
| `render_kernel.py:288+` `_render_helpers` | add the splitmix (or a 32-bit) hash helper | inferred |
| `compile/_primitives.py:104-200` | new arm, with the site id as a compile-time constant | seen (pattern) |
| `core/compiled_plc.py` | pass scan_id and seed into the step | inferred |
| `prove/kernel.py` | prove-mode compile: the site reads an injected per-scan value instead of hashing | inferred |

### D. CircuitPython
| File | Change | Conf. |
|---|---|---|
| `circuitpy/codegen/render_runtime.py` / `render.py` | no scan counter exists in generated code (grep for `scan_id` finds nothing); add a counter and a seed | seen (absence) |
| hash | 64-bit splitmix needs long ints; consider a 32-bit mix for the SAMD51 | inferred |

### E. Ladder export
| File | Change | Conf. |
|---|---|---|
| `click/ladder/translator.py:406-459` | new arm → `RAND(lo,hi)` (spacing from the capture) | seen (pattern) |

### F. Ladder import
| File | Change | Conf. |
|---|---|---|
| `click/codegen/utils.py:34-50` | `RAND: randint`; the import line follows through `_EXPR_FUNC_IMPORT_NAMES` (`emitter.py:400-403`) | seen |

### G. Validation
| File | Change | Conf. |
|---|---|---|
| `click/validation/portability.py:419-520` | depth +1; allowed in both modes | seen |
| core build-time check | randint only inside calc; `lo > hi` literal (M3) | inferred |

### H. Analysis
| File | Change | Conf. |
|---|---|---|
| prove: `prove/classify.py` (domain stack in the docstring at 1-30), `prove/inputs.py`, `prove/passes.py`, `prove/lockfile.py` | each site becomes a synthetic **nondeterministic input** with domain `[lo, hi]`. A tag-valued lo/hi needs that tag's domain, else Intractable. Pseudo-inputs must be hidden from lock projections | inferred |
| prove soundness | one ND input per site **under-approximates** a site inside a `forloop` with count > 1 (each iteration draws). It needs a per-(site, iteration) input, or Intractable | inferred |
| `core/fold.py:190-270` self-calc matchers | must not treat `calc(X + randint(..))` as affine. Unknown expressions already fail to match, and the plateau guard refuses to fold a calc that changes every scan | inferred |
| `pilot/cyclefold.py` | limit-cycle detection fails on random values (safe, no fold) | inferred |
| causal (`causal/recorded.py`, `causal/projected.py`) | a calc dest driven by randint wants a "random draw" root, not an input chase | inferred |
| static evaluators (`sp_values.py`, `value_domains.py`, `init_constants.py`) | must not fold `randint(1,6)` to a constant. They key on `LiteralExpr`, so this is probably safe | inferred |

### I. Debug/DAP
None required. Optionally, an inline value for the draw.

### J. Tests
New `tests/core/test_randint.py`: determinism across seek, fork and `replay_to`; seed; override;
`runner_factory` parity. New `tests/core/analysis/test_prove_randint.py` (ND treatment, loop case).
Extend `tests/core/test_expression*.py`, `tests/click/test_ladder_export.py`, `tests/click/test_codegen.py`,
`tests/circuitpy/test_codegen.py`, `tests/core/test_scan_log.py` (override recording).

### K. Docs
`docs/instructions/math.md`, `docs/guides/runner.md` (seed and override), `docs/guides/verification.md` (ND
treatment), `docs/guides/testing.md`, `docs/dialects/click.md` naming table, cheatsheets, `CHANGELOG.md`.

### L. Outside
laddercodec `RESERVED_NAMES` + RAND token. Conformance checks only range and variation.

**Size: M-L.** The expression is small. The determinism plumbing spans runner, fork, replay, the compiled
kernel signature, CircuitPython, the prover input model and the scan_log override. It also couples to
feature 5 (iteration index).

---

## 3. Drum: `time_event_drum(...)` + register presets for `time_drum`

Siblings: `TimeDrumInstruction` and `EventDrumInstruction` (`core/instruction/drums.py:450-657, 284-449`),
both children of `_DrumBaseInstruction` (201). They are **siblings**; the comment at
`validation/_common.py:162` ("TimeDrum is subclass") is stale.

**Make it its own class, not a `TimeDrumInstruction` subclass.** The prover absorbs a time drum assuming every
step has a preset (`prove/absorb.py:128-140`, `__drum_preset:{done}`), and fold registers the time-drum
accumulator as a timed "up" source (`core/fold.py:144-148, 169`). A subclass would silently inherit both.

### A. DSL surface
| File | Change | Conf. |
|---|---|---|
| `core/instruction/drums.py` | new `TimeEventDrumInstruction(_DrumBaseInstruction)`: optional preset, optional **level** event per step, and the "at least one per step" check (OBJ-1019) as a `ValueError`. Declare `_reads/_writes/_conditions/_structural_fields` | seen (pattern) |
| `core/program/builders.py:271-543` | new `TimeEventDrumBuilder` (copy `TimeDrumBuilder` 418-497). Widen the `_DrumBuilderBase._instruction` type (284). Add `time_event_drum()` beside `time_drum()` (519) | seen |
| `core/instruction/__init__.py`, `core/program/__init__.py:24-26, 125-127`, `core/__init__.py:101, 242`, `pyrung/__init__.py:84, 162` | export | seen |
| `docs/gen_reference.py:156-161` | add `pyrung.time_event_drum` | seen |

### B. Interpreted runtime
`execute` and `advance_profile` on the new class. Model `execute` on `TimeDrumInstruction.execute`
(509-585), but gate the advance on `(preset None or acc >= preset) and (event None or event level)`. Keep the
`_frac:` key naming (the prover's `events.py:288-460` relies on it) and the memory parity with compiled code.
Probes D1 (acc keeps counting?) and D3 decide the details. Conf: seen (pattern).

### C. Compiled
| File | Change | Conf. |
|---|---|---|
| `compile/_core.py:577-580` | dispatch arm | seen |
| `compile/_core.py:98-150` `_collect_helper_conditions` | events snapshot (the `events[i]` keys) with `None` slots | seen |
| `compile/_instructions_block.py:374, 544` | new `_compile_time_event_drum_instruction` built from both siblings | seen |

### D. CircuitPython
Same codegen. `circuitpy/validation.py:283-290` timer-resolution finding: consider adding the drum's Tms unit
(inferred).

### E. Ladder export
| File | Change | Conf. |
|---|---|---|
| `click/ladder/instructions.py:147-166` `_pin_specs` | add the type to the drum set | seen |
| `click/ladder/instructions.py:441-470` `_instruction_token` | new token (CSV name to agree with laddercodec). `None` entries render blank; `_render_sequence` → `_render_operand(None)` would fail today | seen / inferred |
| `click/ladder/_exporter.py:58-62` | `_RENAME_TABLE` only if the CSV name differs | seen |
| `click/ladder/validator.py` | round-trip expected outputs; pins already flow through `_expected_pin_infos` | inferred |

### F. Ladder import
| File | Change | Conf. |
|---|---|---|
| `click/codegen/constants.py:59-87, 90-98` | `_INSTRUCTION_NAMES`, `_PIN_INSTRUCTIONS` | seen |
| `click/codegen/emitter.py:341-358`, `project_emitter.py:778-795` | instruction→import maps (two copies) | seen |
| `click/codegen/utils.py:478-512` | `_RESERVED_IMPORT_NAMES` | seen |
| emitter list args | blank cells → `None` in `presets=` and `events=` | inferred |

### G. Validation
| File | Change | Conf. |
|---|---|---|
| `click/validation/hardware.py:43-66, 68-88` | `_WRITE_TARGET_FIELDS` / `_ROLE_ASSIGNMENT_FIELDS` rows for the new class | seen |
| `click/validation/hardware.py:384, 448-458` | `_evaluate_drums` type set; event role check | seen |
| `click/validation/hardware.py:462-480` | **remove `CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED`**. The constant is public: `findings.py:41, 264`, `click/validation/__init__.py:25, 242`. Without CPU gating, v3 targets lose the guard (see Surprises) | seen |
| new Click rules | duration register bank must be DS (D2); units | inferred |
| `core/validation/_common.py:154-170` | `_instruction_write_targets` arm (outputs, step, flag, acc) | seen |
| `core/validation/render.py:48-71` + the render function | `_VERB` row | seen |
| `core/validation/readonly_write.py:87-100` | duck-typed on outputs/current_step, so it works as-is | seen |
| `core/validation/duplicate_out.py:1-12` | docstring list | seen |

### H. Analysis
| File | Change | Conf. |
|---|---|---|
| `prove/absorb.py:105-140` (done kinds), `:245-260` (`_all_write_targets`) | new arm. Either a new done-kind with event gating, or leave it un-absorbed (generic stateful, which may end Intractable) | seen |
| `prove/classify.py:330-342` (condition walker incl. events), `:1076` (drum domain) | add type | seen |
| `prove/expr.py:326-330`, `prove/elision/slice.py:275` | add type | seen |
| `crossings/drums.py:37-38` | `register(TimeEventDrumInstruction, _DRUM)` (or a new crossing) | seen |
| `core/fold.py:133-150, 153-188` | registry entry, or leave it out (no fold, still correct) | seen |
| `dap/miner.py:156-176` | `_OWNED_TYPES` | seen |
| `causal/why.py:231-232` | label | seen |
| pilot | via `advance_profile()` (`pilot/advance.py:61-72`); the new class needs its own profile (time and/or event demands) | seen |
| `core/validation/cmp_conditions.py:440` | consumes `advance_profile` | seen |

### I. Debug/DAP
Reuse `_DrumBuilderBase._build_debug_substeps` (Auto/Reset/Jump/Jog). Nothing else.

### J. Tests
`tests/core/test_drums.py`;
`tests/core/analysis/test_instruction_analysis_coverage.py` (PUBLIC list 849-878, hidden-memory test 1165);
`test_crossings_coverage.py` (+`EXPECTED_WITH_CROSSINGS`);
`test_crossings_concrete_oracle.py`, `test_crossings_fidelity.py`, `test_crossings_sequential.py`;
`tests/click/test_validation_stage3.py:356, 445` (the preset-literal assertions flip);
`tests/click/test_ladder_export.py`, `tests/click/test_codegen.py`, `tests/circuitpy/test_codegen.py`;
`tests/core/test_source_location.py`; fuzz grammar `tests/fuzz/strategies.py:336-338, 682-707, 896-920`.

### K. Docs
`docs/instructions/drum-shift-search.md` (new section after "Time drum" at line 43; level vs edge note),
`docs/dialects/click.md` (finding code if dropped), cheatsheets (line 23 of the bundled one),
`CHANGELOG.md`, `gen_reference`.

### L. Outside
laddercodec `instructions/drum.py`: `drum_kind: Literal["event","time"]` (116) → add T&E. Add func codes 10505..10538
(TEDRUM) **and** register-duration codes 10249.. (`DRUM.DS*`). `_compute_func_codes` (162-200) derives codes from
the unit only, so today a `time_drum` with DS presets would encode literal-duration codes (inferred). Add
goldens. clicknick: capture.

**Size: M.** A template exists for every touchpoint (about 30 drum sites). Removing the preset rule is S on
its own.

---

## 4. Out family: `toggle`, `debounce`, `flasher`, `timed`

Siblings: `OutInstruction` (`core/instruction/coils.py:49-95`) and `OnDelayInstruction` (`timers.py:23-172`).

**The 25-file claim is accurate** for `OutInstruction` in `src/`. Most of those sites mean "OTE =
combinational (bit equals rung)", and the new classes must **not** match there, so don't subclass
`OutInstruction`. The real work is in three other kinds of site:
- writer enumerations that must add the new classes and their Timer Done/Acc writes;
- per-class implementation arms;
- the timer-owner sites, because each variant owns `Timer` UDT(s).

### Classification of every `OutInstruction` site (src)

| Site | Kind | Action for the new classes | Conf. |
|---|---|---|---|
| `causal/recorded.py:1730` | OTE value match | leave; add its own arm if causal attribution is wanted | seen |
| `analysis/pdg.py:960` (`ote_writes`) | OTE | leave (feeds `simplified._ote_resolvable` 624) | seen |
| `pilot/corrections.py:1469-1472` | non-inert + OTE | leave (`INERT_WHEN_DISABLED=False` makes it skip correctly) | seen |
| `pilot/effect_observation.py:529` | OTE producer | leave | seen |
| `prove/classify.py:221, 2044` | OTE oneshot / all-OTE | leave | seen |
| `analysis/query.py:466` | OTE external closure | leave | seen |
| `analysis/simplified.py:624` | OTE resolvable | leave | seen |
| `analysis/sp_values.py:295` | OTE literal True | leave | seen |
| `validation/dead_write.py:121` | guaranteed write | leave (a variant is not a guaranteed write) | seen |
| `validation/physical_realism.py:250` | opposing OUT pairs | decide: include them or not | seen |
| `validation/_common.py:155` | **writer enumeration** | add target + Timer Done/Acc (feeds duplicate_out, which gives CLICK C0112 vs `on_delay`) | seen |
| `prove/absorb.py:225` | **writer enumeration** | add | seen |
| `validation/stuck_bits.py:124` | **writer enumeration** (both set and reset kinds) | add; variants drive both values | seen |
| `validation/duplicate_out.py:1-12` | docstring | update | seen |
| `instruction/coils.py`, `instruction/__init__.py:11, 71` | class | new classes (or a new `out_variants.py`) | seen |
| `program/instructions.py:132-147` | verb | new `toggle/debounce/flasher/timed` (plain calls, keyword-only phases) | seen |
| `compile/_core.py:551`, `_instructions_basic.py:93` | compile | new compile functions | seen |
| `click/ladder/instructions.py:173` | token | new tokens; render `Timer[n].Acc` as TD only | seen |
| `click/validation/hardware.py:45` | write target | add rows; target-bit role and TD `timer_accumulator` role (`click/capabilities.py:16-21, 111-116`) | seen |
| `click/validation/portability.py:375` | immediate coil | add `toggle` (Y immediate); the others reject immediate | seen |
| `causal/why.py:222` | label | add | seen |
| `crossings/boolean.py:145` | crossing | register or EXEMPT (forced by the coverage test) | seen |
| `validation/render.py:49, 210` | render | add | seen |

**Duck-typed walkers** (missed by `isinstance` grep): `validation/readonly_write.py:71-85` `_any_write_targets`
**returns after `target`**. A variant that has `target` and also owns timers would lose its Done/Acc writes,
which breaks `final_writers.py` and readonly checks. Give it an explicit arm. `analysis/steerable.py:46`,
`analysis/simplified.py:433` and `analysis/query.py:46` also read `target` generically; unknown classes fall
through safely (inferred).

**Timer-owner sites** (from `OnDelayInstruction`), all needing a decision per class:
`core/fold.py:133-150` registry; `dap/miner.py:156-176`; `crossings/accumulating.py:110-113`;
`prove/absorb.py:120, 337, 1209, 1248` (accumulator ownership and done kinds); `prove/classify.py:2126`;
`circuitpy/validation.py:289`; `click/validation/hardware.py:69-71, 501` (role, preset overflow);
`core/validation/cmp_conditions.py:440` (advance_profile); `causal/projected.py:385-396` (accepts temporal
`done_bit` writers; a variant's `target` is temporal too).

### A-L summary
| Cat. | Files | Conf. |
|---|---|---|
| A | `core/instruction/coils.py` (or a new module); `program/instructions.py`; exports in `instruction/__init__`, `program/__init__`, `core/__init__`, `pyrung/__init__`; `docs/gen_reference.py` instruction-set page; Timer extraction via `builders._extract_done_acc` (39-66) and `_extract_status_tags` (68-80). EN/TT are `final=True` (`structure.py:1038-1047`) | seen |
| B | new `execute`: `INERT_WHEN_DISABLED=False`, reads `_dt`, one `_frac:{acc}` per phase. Toggle needs a prev-rung memory key that is **written every scan** (see Surprises: the prover discovers memory keys from one scan). `advance_profile()` per class for pilot | seen / inferred |
| C | `compile/_core.py:545-605`, `_instructions_basic.py` (+ memory-key parity) | seen |
| D | same; `circuitpy/validation.py:283-290` timer resolution | seen |
| E | `click/ladder/instructions.py` tokens, TD-only operand; `translator.py` immediate (`_render_operand(..., immediate_context="coil")`) for toggle | seen |
| F | `click/codegen/constants.py:59-87`; `emitter.py:341-358, 1283` (`_CSV_TO_DSL`), `:1310-1321` (Timer merge); `project_emitter.py:757-795`; `collector.py:927-943, 1222-1235`. **TD-only operand**: `_register_timer_counter_clone` expects a (done, acc) pair, so `Timer[n]` must be rebuilt from TD n. Timer import gate at `emitter.py:314` and `project_emitter.py:584, 757`. `utils.py` reserved names | seen |
| G | core: `_common`, `stuck_bits`, `readonly_write`, `duplicate_out`, `render`, `physical_realism` (above). Click: units (debounce/flasher ms/sec, timed ms/sec/min) and `CLK_OUT_TIMER_DONE_UNSET` (a read of an Out-owned `Timer.Done`) as new codes in `findings.py` + `click/validation/__init__.py` | seen / inferred |
| H | prove absorb/classify/events (the new accumulators are not absorbed, so expect Intractable until they are); crossings; fold; miner; why; projected; pilot through `advance_profile` (`pilot/advance.py:61-90`, `harness.py:399`) | seen / inferred |
| I | `GenericInstructionDebugHandler` covers it; optional `debug_substeps` | inferred |
| J | `tests/core/test_instruction.py`, `test_timers.py`, new `tests/core/test_out_variants.py`; `test_instruction_analysis_coverage.py` PUBLIC list; `test_crossings_coverage.py`, `test_crossings_coils.py`, `test_crossings_accumulating.py`; `tests/validators/test_conflicting_output_validation.py`, `test_stuck_bits.py`, `test_physical_realism.py`; `tests/click/test_ladder_export.py`, `test_codegen.py`, `test_validation_stage3.py`; `tests/circuitpy/test_codegen.py`; prove `test_prove_matrix.py` / `test_prove_timer_abstraction.py`; fuzz `strategies.py:313, 452-463, 776-810`; `test_source_location.py` | seen |
| K | `docs/instructions/coils.md` (new sections after `out`, line 5-18), `timers-counters.md` (a Timer claimed by an Out coil), `docs/dialects/click.md` naming table and codes, cheatsheets (bundled 126-130), `CHANGELOG.md` | seen |
| L | laddercodec `instructions/coil.py:23-45` func-code table (8224-8235), `csv_names` (222), CSV contract, per-variant payload fields (preset/TD/unit/maintained), goldens. clicknick: capture | seen |

**On "desugar in core" and reusing `core/synthesis.py`:** see Surprises 1. In short, reuse is limited to
borrowing patterns; no lowering layer exists.

**Size: L.** Four classes × about 35 sites, plus compiled-memory parity, new prover accumulators (or accepting
Intractable), crossings, pilot profiles, TD-only Timer reconstruction on import, and laddercodec func codes.

---

## 5. For/Next: conditioned body rungs, `break_loop`/`continue_loop`, nesting

Siblings: `ForLoopInstruction` (`instruction/control.py:126-182`), `ForLoop`/`forloop`
(`program/context/_control_flow.py:126-200`), `_state._forloop_active` (`_state.py:13`), `return_early`
(`program/instructions.py:461-473`), `ReturnInstruction` and `SubroutineReturnSignal` (`base.py:122`).

The data model is the crux. `ForLoopInstruction.instructions` is a flat `list[Instruction]`, and **about 28
files walk it as such** (list below). A body that holds `Rung` objects changes every one of them. Keeping
`.instructions` as a flat compatibility view (all body instructions) plus a new ordered `body` (items that
are an `Instruction` or a `Rung`) keeps most walkers working. Condition-sensitive walkers would then still
see body-rung conditions as absent: `_collect_write_sites`, pdg `condition_reads`, prove classify and
elision, `choices_violation`, `physical_realism`.

### A. DSL surface
| File | Change | Conf. |
|---|---|---|
| `_control_flow.py:128-200` | depth counter instead of the bool; per-depth idx tag (`Tag("_forloop_idx")` fixed at ~137); body capture keeps order (bare items vs `Rung`); oneshot only on the outer loop | seen |
| `_control_flow.py:233-234` | `branch()` forbidden anywhere in a loop; decide whether it is allowed inside a body `Rung` | seen |
| `_state.py:13` | `_forloop_active: bool` → depth/stack | seen |
| `_rung.py:132-151` | `Rung.__exit__` always calls `prog._add_rung`; inside a loop it must attach to the loop body instead. **Footgun today**: verified that `with Rung(B)` inside `forloop` silently becomes a separate top-level rung placed *before* the loop rung | seen (ran it) |
| `_rung.py:58-62` | comment consumption by a body rung | seen |
| `program/instructions.py:461-473` | add `break_loop()` / `continue_loop()`; they must sit in a conditioned body rung inside a loop | seen |
| `instruction/base.py:17-21, 122` | new `_EXECUTOR_*` kinds and `LoopBreakSignal`/`LoopContinueSignal` | seen |
| `instruction/control.py` | `BreakInstruction`, `ContinueInstruction`; `ForLoopInstruction` body model | seen |
| exports | `instruction/__init__`, `program/__init__` (44, 86), `core/__init__` (83, 216), `pyrung/__init__` (61, 136), `gen_reference.py:128` (program-structure page) | seen |

### B. Interpreted runtime (each path re-implements the loop)
| File | Change | Conf. |
|---|---|---|
| `instruction/control.py:140-182` `ForLoopInstruction.execute` (path 1, reached by `rung.py:250-260` and `causal/projected.py:376-382`) | body rungs, signals, idx per depth | seen |
| `executor.py:1204-1243` natural; `:1522-1585` observed; `LoopIterationRun` / builder `:123-147, 271-279, 947-998` | the same, plus the observer contract for body rungs | seen |
| `debug_handlers.py:169-200`, `debugger.py:133-200` (re-raises `SubroutineReturnSignal`), `:385-410` (line collection) | stepping into body rungs | seen |
| condition snapshot | a body rung evaluated per iteration needs a fresh view (`executor._resolve_condition_view` 1277); probe F13 | inferred |
| `_program.py:73-96` `_assign_state_keys` | walk body rungs (oneshot memory keys; probe F14) | seen |
| `causal/projected.py:372-382` | catch loop signals as well as `SubroutineReturnSignal` | seen |

### C. Compiled
| File | Change | Conf. |
|---|---|---|
| `compile/_core.py:897-931` | body rungs compiled like branches (`_compile_rung_items` 673-722); `break`/`continue` map to Python inline. They survive the `try/except IndexError` wrapper (`_primitives.py:406-446`). Nested loops reuse `_iterations`/`_for_i`, so give them unique names per depth | seen |
| `compile/_core.py:98-150` | `_collect_helper_conditions` does not descend into loops today | seen |
| `circuitpy/codegen/context.py:270-295` | tag collection walks `instructions` only when it is a list of instructions; body-rung conditions are missed | seen |

### D. CircuitPython
Same codegen. `circuitpy/validation.py:314-365` `_iter_instruction_sites` walks `.instructions` (seen).

### E. Ladder export
| File | Change | Conf. |
|---|---|---|
| `click/ladder/instructions.py:50-95` | body rows are emitted unconditioned (`_expand_conditions([])`); add body-rung contacts, nested for/next recursion, break/continue tokens | seen |
| `click/ladder/_exporter.py:58-62` | `_RENAME_TABLE`: `break_loop`→`break`, `continue_loop`→`continue` | seen |
| `click/ladder/_exporter.py:69, 304-325, 407, 413-427` | forloop-only rung rule, `_forloop_count`, type collection (one level today) | seen |
| `click/ladder/layout.py:241-252`, `validator.py:90-105, 216-258` | branch rule; round-trip expected outputs, which currently set `condition_tree=None` for body items | seen |
| `loop.idx` | exports as the unmapped `_forloop_idx` (open in RESEARCH) | inferred |

### F. Ladder import
| File | Change | Conf. |
|---|---|---|
| `click/codegen/analyzer.py:794-815` | groups `for(` up to the **first** `next()`, so nesting breaks | seen |
| `click/codegen/emitter.py:745-775, 795-848` | `_emit_forloop` emits `body_rung.instructions` only (842-846); **confirmed it drops contacts**. Emit `with rung(...)` for contacted body rungs; recurse for nesting | seen |
| `emitter.py:1283` `_CSV_TO_DSL`, `:341-370` imports; `project_emitter.py:778-805` | break/continue names | seen |
| `constants.py:82-83` | add `break`, `continue` | seen |
| `models.py:270, 311` (`RungRole`, `has_forloop`), `collector.py:169, 1112`, `utils.py:484` | as needed | seen |

### G. Validation
| File | Change | Conf. |
|---|---|---|
| walkers of `.instructions` | `validation/_common.py:240-256, 300-320`; `choices_violation.py:133`; `physical_realism.py:126`; `math_conditions.py:151`; `walker.py:492-494`; `pointer_sites.py:175`; `click/validation/__init__.py:106`; `circuitpy/validation.py:359` | seen |
| new rules | depth ≤ 3 across `call()` (`core/validation/call_graph.py` exists); oneshot only on the outer loop; `return_early` inside a loop is an error; break/continue need a contact | inferred |

### H. Analysis
| File | Change | Conf. |
|---|---|---|
| `analysis/return_guards.py:40-60` | `return_early` gating exists; break/continue need the same treatment. Body instructions after a conditional break may not run, and analyses that assume they always do include `prove/elision/slice.py:225-240, 312` (write-before-read: **a soundness risk**), `prove/classify.py:343`, `prove/expr.py:332`, `prove/inputs.py:91`, `prove/absorb.py:295` | seen / inferred |
| `pdg.py:971, 1165, 1663` | body walks; body-rung `condition_reads` | seen |
| `causal/why.py:266`, `activation.py:46`, `init_constants.py` (`_execution_items`), `pilot/effect_observation.py:515` | body walks | seen |

### I. Debug/DAP
`debug_handlers.py`, `debugger.py` (above); `dap/breakpoints.py:195, 235` (nested lines). The VS Code extension is
generic over trace events (inferred, no change).

### J. Tests
`tests/core/test_forloop.py` (the "Nested forloop is not permitted" test at 101 and the branch test at 110 both
flip); `test_debugger_refactor.py`; `test_source_location.py`; `test_replay_capture.py` (`_forloop_idx`);
`tests/core/analysis/test_instruction_analysis_coverage.py` (1088, PUBLIC list);
`test_crossings_coverage.py` (Break/Continue → EXEMPT); `test_prove_matrix.py`,
`test_prove_simultaneous_edge_coverage_tests.py`, `test_prove_fuzz_reproducer_regressions.py`;
`tests/click/test_ladder_export.py`, `test_codegen.py`, `test_ladder_realistic.py` +
`tests/click/ladder_realistic_output/main.csv` (golden); `test_validation_stage3.py`;
`tests/circuitpy/test_codegen.py`; fuzz `strategies.py:119` and the forloop production.

### K. Docs
`docs/instructions/program-control.md:50-77`; `docs/dialects/click.md:116` (token names) and the naming table;
cheatsheets (bundled ~230); `docs/internal/debug-spec.md`; `CHANGELOG.md`; `gen_reference`.

### L. Outside
laddercodec `instructions/forloop.py` (break/continue tokens with func codes 9232/9233; nested parsing);
goldens `tests/fixtures/coverage/golden/forloop__*.csv|bin`; capture "v3 loop with a contact on a body rung".

**Size: L.** It changes the loop's data model across 6 execution paths and about 28 walkers. Export and
import both need nesting. It needs a new static guard analysis to keep elision sound, and the runtime
semantics are pending probes (F2/F6/F7/F14).

---

## 6. Twin → conformance (+ run over pyrung's Modbus sim)

### References to `twin`
| Location | What |
|---|---|
| `src/pyrung/twin/{__init__,_case,_runner,_slot}.py` | move to `src/pyrung/conformance/`; internal imports `pyrung.twin._case/_slot` (`_case.py:9`, `_runner.py:6-7`, `__init__.py:3-4`) |
| `tests/twin/{AGENTS.md, CLAUDE.md, __init__.py, test_rung_semantics.py}` | move to `tests/conformance/`; test imports `pyrung.twin` (line 6) |
| `AGENTS.md:87, 96` | module map + sub-AGENTS list |
| `CHANGELOG.md:388` | historical entry (leave it); add an Unreleased rename entry (breaking: `pyrung.twin` import path) |
| `.hypothesis/constants/*`, `.pytest_cache` | caches; ignore |
| Makefile, `pyproject.toml` markers, `.github/workflows/ci.yml`, `docs/`, `mkdocs.yml` | **no `twin` references**. `tests/twin` has no marker, so it runs in `make test` and in CI today |
| Unrelated "twin" prose | `crossings/copy.py:295`, `crossings/indirect_dest.py:12`, `pilot/availability.py:63`, `prove/expr.py:146`, `validation/sat.py:8, 53`, several tests. Do not rename these |

### Modbus runner touchpoints
| File | Change | Conf. |
|---|---|---|
| `twin/_runner.py:10-42` | today it reads `plc.current_state.tags` in-process. Replace with a `ClickDataProvider` (`click/data_provider.py:43-115`, which **requires a TagMap**) behind `pyclickplc.server.ClickServer`, and a `pyclickplc.client.ClickClient` (async). The PLC must be free-running (thread) or stepped between requests; `dap/modbus.py` shows the threaded-server pattern | seen |
| `twin/_slot.py:13-31` | slots are a `named_array` with no TagMap; they need fixed DS addresses (`map_to`), with DS1 reserved for the build hash | seen |
| `pyproject.toml:191-198` | add a `conformance` marker. A localhost socket run is networked; decide whether it joins `integration` (which excludes it from `make test` and CI, `Makefile:18-19`, `ci.yml` last step) | seen |
| `Makefile` | optional `test-conformance` target | inferred |

RESEARCH's "avoid `parity`, already a test marker name" is not quite right. No `parity` marker is registered
in `pyproject.toml`. "parity" is `tests/fuzz/test_parity.py` and the `--runner-backend=both` option. Avoiding
the word is still sensible.

**Size: S** for the rename (about 6 files + AGENTS). **M** for the Modbus runner (TagMap for slots, the
server/client lifecycle, wall-clock pacing and tolerances).

---

## 7. Shared hot spots (merge-conflict risk)

| File | Features | What each does there |
|---|---|---|
| `core/expression.py` | 1, 2 | 1: `RangeAggregateExpr`, `format_expr`. 2: `RandIntExpr`, `randint()`, `format_expr` |
| `circuitpy/codegen/compile/_primitives.py` | 1, 2 (+4 via helpers) | expression arms; guarded-instruction wrapper |
| `circuitpy/codegen/compile/_core.py` | 3, 4, 5 | dispatch arms (3, 4); helper-condition walker (3, 5); loop compile (5) |
| `circuitpy/codegen/compile/_instructions_basic.py` / `_instructions_block.py` | 4 / 3 | new compile functions |
| `circuitpy/codegen/render_kernel.py` | 2 (+5) | scan_id injection, hash helper; loop-name hygiene |
| `click/ladder/translator.py` | 1, 2, 4 | aggregates and ABS; RAND; immediate for toggle |
| `click/ladder/instructions.py` | 3, 4, 5 | tokens and pin specs; Out tokens; loop rendering |
| `click/ladder/_exporter.py` | 3?, 4?, 5 | `_RENAME_TABLE`, loop-only rung rule |
| `click/codegen/utils.py` | 1, 2, 3, 4, 5 | func table and SUM regex (1, 2); reserved names (3, 4, 5) |
| `click/codegen/constants.py` | 3, 4, 5 | `_INSTRUCTION_NAMES`, `_PIN_INSTRUCTIONS` |
| `click/codegen/emitter.py` + `project_emitter.py` | 1, 2, 3, 4, 5 | import maps (duplicated in both), `_CSV_TO_DSL`, `_emit_forloop`, Timer merge |
| `click/codegen/collector.py` | 4, 5 | TD-only Timer clone; `has_forloop` |
| `click/validation/hardware.py` | 3, 4 | drum rows and preset rule; Out rows and roles |
| `click/validation/portability.py` | 1, 2, 4 | depth and mode; immediate coil for toggle |
| `click/validation/findings.py` + `click/validation/__init__.py` | 1, 3, 4 | new codes; removed drum code |
| `core/validation/_common.py` | 3, 4, 5 | write targets; loop walkers |
| `core/validation/render.py` | 1, 3, 4 | `render_expr`; `_VERB` and render arms |
| `core/analysis/prove/absorb.py`, `classify.py` | 2, 3, 4, 5 | ND inputs; drum and timer kinds; loop walkers |
| `core/analysis/causal/why.py` | 3, 4, 5 | labels; loop walk |
| `core/analysis/crossings/*` + `test_crossings_coverage.py` | 1, 3, 4, 5 | calc aggregate; drum, Out registration; EXEMPT Break/Continue |
| `core/fold.py` | 2, 3, 4 | selfcalc safety; acc registry |
| `core/instruction/control.py`, `core/executor.py`, `core/debug_handlers.py` | 5 (+2 iteration stack) | loop body model and signals |
| `core/program/builders.py` | 3 (4 if Out verbs land there) | drum builder; Timer extraction helpers |
| `core/program/instructions.py` | 4, 5 | Out verbs; `break_loop`/`continue_loop` |
| `core/program/context/_program.py` | 2, 5 | site-id stamping; state keys over body rungs |
| `core/__init__.py`, `pyrung/__init__.py`, `core/program/__init__.py`, `instruction/__init__.py`, `docs/gen_reference.py` | 2, 3, 4, 5 | exports and manifest |
| `tests/core/analysis/test_instruction_analysis_coverage.py` | 3, 4, 5 | PUBLIC list and cases |
| `tests/fuzz/strategies.py` | 3, 4, 5 | grammar |
| `docs/dialects/click.md`, both cheatsheets, `CHANGELOG.md` | all | naming table, codes, examples |

Suggested landing order to limit conflicts: 6 (rename) → 1 → 3 → 2 → 4 → 5. Feature 2's iteration index
depends on 5's loop-context stack, so either land a minimal iteration hook first or put RAND-in-loop behind
5.

---

## 8. Surprises (contradictions with RESEARCH.md, or bigger than it looks)

1. **No lowering layer exists**, and `core/synthesis.py` cannot be reused for the Out family as is.
   - **History:** a drum-desugar design (authored vs lowered program, an origin map) lived in
     `scratchpad/drum-desugar-plan.md` (commit `2324bd4`) and was deleted in `79fc61b`. Nothing in `src/`
     implements lowering.
   - **What synthesis is:** its factories build *rung lists* for the soft-exec overlay. They are never part of
     a `Program`, never seen by export or the prover (docstring at 1-21), and cannot sit inside a rung,
     branch or loop the way an Out coil does.
   - **Semantics don't match:** `pulse_feedback_rungs` (174-226) is a flasher that rests low and starts with
     the off dwell, with one scan of slop; CLICK's flasher starts ON. `bool_feedback_rungs` is TON→TOF, which
     RESEARCH "Feels off #7" already rules out for debounce.
   - **Realistic path:** native instruction classes that implement the semantics by composing timer
     arithmetic internally, with an `advance_profile` and a crossing each. "Desugar in core" then means
     implementation reuse, not a lowered program the analyses consume. Building real lowering is its own L
     project and would also serve the queued drum desugar.
2. **The loop body model is the biggest hidden cost.** There are six execution paths (section 0.1) and about
   28 files that walk `ForLoopInstruction.instructions` as a flat list. Today `with Rung()` inside `forloop` is
   accepted and silently hoisted to a top-level rung ahead of the loop (verified by running it).
3. **RESEARCH's importer note is wrong in mechanism.** MAX/MIN/MDN/AVG don't belong in
   `_CLICK_FUNC_TO_PYTHON`: they take colon ranges and need the `_SUM_RE` path, with the `_range_sum`
   placeholder in `_validate_operand`. `ABS→abs` must be kept out of the `pyrung.core.expression` import set.
   "abs into the decimal-only set" needs a `UnaryExpr` arm because `abs` is not a `MathFuncExpr`.
4. **The 25-file `OutInstruction` claim is accurate, but its meaning is inverted.** Most sites encode "OTE is
   combinational" and must exclude the variants. The sites that need additions are writer enumerations,
   string tables, duck-typed walkers (`readonly_write._any_write_targets` drops timer writes) and the
   Timer-owner sites, about 12 more files.
5. **Dead tables:** `prove/classify.py` instruction sets are unused, and `Instruction.exclusive_resources()`
   has no callers. CLICK C0112 (timer number reuse) comes only from `duplicate_out` write targets.
6. **The prover finds memory keys by stepping one scan** (`prove/passes.py:1749-1757`). Any new instruction
   that creates a memory key lazily (toggle prev state, a debounce phase flag) is missing from the state key,
   which silently merges states. Write such keys unconditionally every scan, or keep the state in tags.
7. **RAND plumbing gaps.** The compiled step function has no `scan_id`. CircuitPython output has no scan
   counter. One ND input per site under-approximates RAND inside a multi-iteration loop.
8. **Dropping `CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED` changes behaviour for v3 targets.** With CPU and version
   gating out of scope, a v3 export with tag presets loses its guard. The constant is also public API
   (`pyrung.click.validation.__all__`). laddercodec's drum encoder derives func codes from the unit only, so
   register durations probably encode wrong today (inferred). Consider downgrading the rule to a hint
   ("needs CLICK v4") rather than deleting it.
9. **`time_event_drum` must not subclass `TimeDrumInstruction`** (prover drum absorption, fold registry).
   The `_common.py:162` comment claiming a subclass relation is stale.
10. **Pre-existing bugs found on the way:**
    - `core/validation/render.py:125-137` renders abs as `absX` and `SumExpr` as its class name.
    - `prove/classify.py:555-565` `_expr_reads_tag` ignores `SumExpr`.
    - The two click cheatsheets have drifted apart.
    - `.github/workflows/ci.yml` does not exclude the `tumbler` marker, though `make test` does (unrelated).
11. **There is no `parity` pytest marker** (see section 6). The `tests/twin` case runs in `make test` and CI
    today. Moving it onto real sockets forces a marker decision.
12. **Out-variant import needs Timer reconstruction from TD alone.** The CLICK coil carries only TD<n>, but
    pyrung's verbs take `Timer[n]` (T<n> + TD<n>). The importer's Timer clone path keys on a (done, acc)
    operand pair.

---

## 9. Size summary

| Feature | Size | Why |
|---|---|---|
| 1. Math aggregates + abs fix | **S-M** | one expression class across ~8 src files + importer regex + laddercodec tokens |
| 2. randint | **M-L** | tiny expression; determinism plumbing through runner/fork/replay/compiled/CircuitPython/prover/scan_log, and coupled to loop iteration |
| 3. time_event_drum (+ drop preset rule) | **M** | a sibling template exists for ~30 drum sites; the rule drop is S but has a v3 policy question |
| 4. Out family | **L** | 4 classes × ~35 sites, compiled parity, prover accumulators, crossings, pilot profiles, TD-only import, laddercodec func codes |
| 5. For/Next | **L** | body data model through 6 execution paths and ~28 walkers, nested export/import, new break/continue static guards for prover soundness |
| 6. Conformance | **S** rename / **M** Modbus runner | ~6 files to move; TagMap slots + server/client lifecycle + wall-clock pacing |

---

## Addendum (orchestrator, 2026-10-22): crossings are a pilot/causal input, not just a coverage test

Consumers of `analysis/crossings` (`reverse` / `forward` / `propose`): `pilot/trace.py:338-345`,
`pilot/writer_selection.py:65`, `pilot/trace_constraints.py:88`, `causal/projected.py`, `causal/recorded.py`,
`analysis/pdg.py`, `analysis/sp_values.py`. An unregistered class gives FALLTHROUGH / UNKNOWN / no proposal,
so pilot can't steer through it (honest, but `how()` declines).

**MRO trap:** `crossing_for()` (`crossings/__init__.py:76`) walks the MRO. Any new class that subclasses
`OutInstruction` silently inherits `OutCrossing` ("Y follows the rung"), which is wrong for toggle (retains),
timed (drops while the rung is still true), debounce and flasher. Same for `time_event_drum` if it subclassed
`TimeDrumInstruction`. Either don't subclass, or register an explicit crossing. Check whether
`test_crossings_coverage.py` catches an inherited registration; if it doesn't, tighten it.

Per feature:
- Math: `CalcCrossing` needs an aggregate arm; max/min/median/mean aren't additive, so reverse is UNKNOWN or a
  kind-specific bound. `randint`: forward UNKNOWN, no proposal (pilot can't steer a random value).
- Drum T&E: `_DRUM` registration likely fits; the event is level, not edge, so check `propose`.
- Out family: four real crossings. Toggle reverses through a rung rising edge while Y has the other value;
  debounce/timed/flasher follow `TimerDoneCrossing` (time demand) plus the phase. Without them `how(Y)` declines.
- For/Next: Break/Continue EXEMPT; body instructions under a `with Rung()` need the body rung's condition as
  their rung context in `reverse`/`propose`.
