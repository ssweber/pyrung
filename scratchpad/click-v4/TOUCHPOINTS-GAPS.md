# CLICK v4: gaps in TOUCHPOINTS.md

Read-only gap audit at `dev` HEAD `0a57b20` (2026-10-22). Companion to `TOUCHPOINTS.md` and `RESEARCH.md`.
It lists only sites that TOUCHPOINTS **misses or gets wrong**. Pilot internals are skipped (audited
separately). Where a non-pilot module is a pilot input, that is noted.

**Method.** The sweeps were mechanical, not sibling-following. A script extracted every `file:line` that
TOUCHPOINTS cites. Each sweep was a `git grep` over `src/` and `tests/` (plus docs, editors, Makefile,
pyproject, .github where relevant). Every hit was classified as cited (within 12 lines of a TOUCHPOINTS
reference), file-cited-but-other-line, or file never mentioned. Then the non-cited hits were read by hand.
The sweeps:
1. class-name `isinstance` / `type(x).__name__` / string tables / class-keyed registries;
2. duck-typed attribute readers (`getattr(instr, "target"|"done_bit"|"accumulator"|"preset"|"unit"|"oneshot"|"instructions"…)`
   and the `_reads/_writes/_conditions/_exclusive_fields/_status_fields` declarations);
3. rung-structure walkers (`.instructions`, `_instructions`, `_execution_items`, `_branches`, observer kinds);
4. Expression dispatchers;
5. determinism (caches, parity, replay, purity);
6. serialization and docs;
7. gating tests.

**Columns.** *Conf.*: **seen** means the code was read at that line; **inferred** means it was not traced
end to end. *Sev.*: **crash**, **silent wrong** (wrong answer or unsound with no error), **decline** (an
analysis/finding gives up or misses, honestly), **gate** (a test fails, by design), **cosmetic**.

Paths are relative to `src/pyrung/` unless they start with `tests/`, `docs/`, `editors/` or `.github/`.

---

## 0. Cross-cutting (every new instruction class inherits these)

### 0.1 The declaration-driven layer TOUCHPOINTS never names

Most modern analyses do not dispatch on class at all. They read the class-level field declarations, so a
new class that declares them wrong is silently mis-analysed, with no `isinstance` site to grep for.

| Site | What it does | Change for new classes | Conf. | Sev. |
|---|---|---|---|---|
| `core/analysis/write_sites.py:73-88` `instruction_write_targets` | **The** shared write enumeration: `_writes` + `_status_fields`. Consumers: `pdg._extract_instruction_writes` (pdg.py:1022-1048, so `writers_of` and every cone), `causal/recorded.py:548-556` (co-written tags = "internal state", skipped as causes), `activation.py:16,65`, `sp_values.py:220`, `prove/classify.py`, `prove/passes.py`, `validation/*`, `harness.py`, `runner.py`, plus pilot (`effect_observation`, `requirement_derivation`) | Out variants: `_writes` must include `target` **and** every owned Timer `Done`/`Acc` (and `_status_fields` if EN/TT are driven). T&E drum: mirror `TimeDrumInstruction`. If an Out variant omits its Timer fields, the PDG shows no writer for `Timer[n].Acc`, so `steerable` calls it external, the prover treats it as an ND input, and causal cannot attribute it | seen | silent wrong |
| `core/analysis/pdg.py:946-969` (and `_extract_instruction_event` 1004-1016, 1643) | read sets come from `_reads`; `condition_reads` from `_conditions`; **`exclusive_reads` from `_exclusive_fields`** | Out variants: presets (`int \| Tag`) in `_reads`; each owned accumulator in `_exclusive_fields`. T&E drum: `events` in `_conditions` (the time drum has none today); duration-register presets in `_reads` | seen | silent wrong |
| `_exclusive_fields` consumers: `prove/elision/slice.py:37` (accumulators enter the elision closure *via* `exclusive_reads`), `core/fold.py:365, 666, 727-732` | self-read of an accumulator keeps it stateful / foldable | an Out-owned accumulator not in `_exclusive_fields` drops out of the elision closure | seen | silent wrong (prover) |
| `core/instruction/base.py:77-90` `walker_fields()`; `core/validation/walker.py:120-130` | the generic operand walker behind Click portability/hardware operand checks sees declared fields only | undeclared preset/Timer fields skip bank and type checks | inferred | decline |

**Correction to TOUCHPOINTS 0.4:** `exclusive_resources()` has no callers, as stated, but `_exclusive_fields`
itself is **live** (row 3 above). Also dead: `Instruction.always_execute()` / `ALWAYS_EXECUTES` has no
callers anywhere (`base.py:92-98`; one docstring mention at `prove/events.py:718`). (seen, cosmetic)

### 0.2 Attribute naming is a semantic opt-in

Duck-typed sites treat any instruction with `done_bit` + `accumulator` (+ `preset`) as a TON/CTU. Naming a
variant's fields that way, most tempting for `timed`, which has one phase, opts it into TON semantics at:
`validation/readonly_write.py:81-86`; `prove/absorb.py:499-510` (`_is_matching_timer_preset_read`) and
`:999-1007` (`_is_matching_owner_preset_read`); `causal/projected.py:386-396` ("done_bit is temporal" candidate
acceptance); `circuitpy/validation.py:292` (`unit`). Decide the attribute names on purpose. (seen,
silent wrong if named by accident)

### 0.3 Compiled-backend safety nets are weaker than TOUCHPOINTS implies

| Site | Fact | Conf. | Sev. |
|---|---|---|---|
| `.github/workflows/ci.yml:66` vs `Makefile:19` | CI runs pytest **without** `--runner-backend=both`, and the default is `interpreted` (`tests/conftest.py:133`). The interpreted/compiled tag+memory parity gate (`tests/conftest.py:170-178`) runs only in local `make test`. Every new memory key (toggle prev, debounce phase, `_scan_id`, seed) is CI-unguarded | seen | gate gap |
| `core/runner.py:2546-2568` + `:348-362` `_looks_like_compiled_replay_gap` | when `compile_kernel` raises `NotImplementedError`/"Unsupported", the compiled **replay** kernel silently falls back to interpreted replay. A missing compile arm for a new class costs replay speed but fails nothing outside `runner_factory` tests | seen | silent (perf) |
| `src/pyrung/circuitpy/codegen.py.bak` | untracked, git-ignored 147 KB stale file with `OutInstruction`/`ForLoopInstruction`/`TimeDrumInstruction` references. Plain `grep -r` hits it; `git grep` does not. Delete it or brief agents to ignore it | seen | cosmetic |

### 0.4 Forcing functions TOUCHPOINTS 0.2 omits or overstates

| Site | Fact | Conf. | Sev. |
|---|---|---|---|
| `tests/core/analysis/test_crossings_concrete_oracle.py:342-346` | `_DIRECT_ORACLE_CLASSES \| frontiers == registered_classes()`. Fires for every new crossing (Out family, T&E drum) | seen | gate |
| `tests/core/analysis/test_crossings_fidelity.py:204-206` | `set(FIDELITY) == registered_classes()`. Same | seen | gate |
| `core/validation/registry.py:41-154` (`_SPECS`), `:168` (`VALIDATOR_ORDER`); `tests/validators/test_validate_report.py:382-391` | every new core finding code needs a `_SPECS` row; the report looks up `RULES[f.code]`, so an unregistered code raises `KeyError`. Each rule also needs a row in `docs/guides/ladder-lints.md`. Affects break/continue-needs-contact, loop depth, `return_early` in a loop, `randint` outside calc, any Out-family core rule | seen | crash |
| **Correction:** `tests/core/analysis/test_instruction_analysis_coverage.py:849-883` | `PUBLIC_RUNG_INSTRUCTIONS` is a hand list compared to the `GRAPH_CASES` set. It is **not** derived from `pyrung.__all__`, so a new verb passes silently unless it is added to both. It is a checklist, not a forcing function | seen | — |
| **Correction:** `docs/gen_reference.py` `_validate_manifest` | no test calls it. It runs from `devtools/stage_docs.py:76-77` (via `make docs-build/docs-check/verify`) and `.github/workflows/docs.yml:46`, which runs on main push/PR only. Not part of `make test` or dev CI | seen | — |
| Crossings MRO (answers the Addendum's open question) | `registered_classes()` returns exact keys; `crossing_for()` walks the MRO (`crossings/__init__.py:76-86`). An `OutInstruction` subclass placed in `EXEMPT` passes both coverage tests and still inherits `OutCrossing` at runtime. Nothing asserts `crossing_for(cls) is None` for `EXEMPT` members. Add that assertion | seen | silent wrong |

### 0.5 Fuzz suite beyond `strategies.py`

TOUCHPOINTS J sections name only `tests/fuzz/strategies.py`. Also:
- `tests/fuzz/reproducer.py:200-368`: a third per-kind dispatch; an unknown kind renders as
  `# unknown instruction: ...` (silent wrong reproducers). `:434, 493, 550, 599`: four hard-coded import header lists.
  `:383-397` `_rung_body_lines`: flat, single-depth forloop body.
- `strategies.py:955` `RungSpec.forloop: ForLoopSpec | None` (single depth, flat body); `:958` `_TERMINAL_KINDS`
  (needs `time_event_drum`, or generated programs place instructions after a terminal and fail to build);
  `:969-974` `_EXCLUSIVE_SPEC_EXTRACTORS` (needs the Out variants' Timer owners, or the fuzzer emits duplicate
  timer owners); `:1007` `_WRITE_DEST_KINDS` (aggregates join `range_sum_calc`).
- `tests/fuzz/patterns.py:335-373, 920`: the `range_sum` pattern, the template for aggregates.
- `tests/fuzz/minimize.py:217-242`: forloop unwrap assumes one level.
(seen)

### 0.6 Unit-test helpers build a bare context

`tests/conftest.py:361` (`execute`), `:381` (`evaluate_rung`), `:418` (`evaluate_program`) build a raw
`ScanContext` and set only `_dt`. A new instruction or expression that reads `_scan_id`, a seed, or any new
memory key without a default crashes every unit test that uses these helpers. (seen, crash)

### 0.7 Click import is an ordering dependency

- `click/codegen/emitter.py:1279` raises "Unrecognised AF instruction" for an unknown token. If laddercodec
  learns a v4 token before pyrung's importer does, imports crash instead of degrading to `raw(...)`. Land
  importer support no later than the laddercodec tokens. (seen, crash)
- `click/codegen/project_emitter.py:268` pins `pyrung>=0.15.2` in the generated `pyproject.toml`. A
  v4-imported project uses new verbs, so the pin must move with the release (it is already on the release
  checklist, but v4 makes it load-bearing). (inferred, crash)
- `project_emitter.py:906-907`: generated guidance "A few names differ: Set -> latch, Math -> calc" should
  gain MDN/AVG/RAND, Break/Continue and the Out types. (seen, cosmetic)

---

## 1. Math aggregates (`.max/.min/.median/.mean`, Bool `.sum()`, `abs`)

### Design constraints the generic walkers impose (apply to `RandIntExpr` too)

- **Subclass, never alias, `SumExpr`.** TOUCHPOINTS 1.A allows "subclass or alias". With an alias (or with
  the aggregates as `SumExpr` subclasses), every existing `isinstance(x, SumExpr)` arm reads max/min/median/mean
  as sum: `format_expr` (`core/expression.py:507`, cosmetic); the translator (`click/ladder/translator.py:449`,
  exports `SUM`, silent wrong); compiled `_primitives.py:190` (computes a sum, so the backends diverge); and
  `crossings/calc.py:133` (emits `Aggregate(operation="sum")`, which `core/crossing.py:121-128`
  `evaluate_forward` computes as a sum). Pilot reads that through `pilot/writer_selection.py:110-124` and
  `pilot/trace.py:1893`, so pilot would steer wrong. Make `RangeAggregateExpr` the base, with `SumExpr` as
  its `kind="sum"` subclass, and pass `operation=kind` into `Aggregate`.
  `core/crossing.py:70-77, 121` are not in TOUCHPOINTS. Non-sum operations already return `UNKNOWN` there,
  which is safe. (seen)
- **Public instance attributes, no `__slots__`.** `pdg.py:684-690, 789-794`, `circuitpy/codegen/context.py:357-362`,
  `compile/_core.py:245`, `core/instruction/calc.py:73-75`, `click/validation/portability.py:402-404, 518`
  walk `vars(expr)`; they skip `_`-prefixed names and raise `TypeError` on `__slots__`.
  `circuitpy/validation.py:163-171` catches that error and silently drops the reads. (seen)
- **Subclass `Expression` directly** (not `MathFuncExpr`/`ShiftFuncExpr`), or the existing arms treat the new
  node as a pure function of its args (`sp_values._expr_tag_names` 399-406, the translator, compiled). (seen)

### Missed sites

| Site | What it dispatches | New class | Conf. | Sev. |
|---|---|---|---|---|
| `core/fold.py:196-202` `_calc_self_referential.reads_tag` | self-feeding calc detection | ignores `SumExpr`, `MathFuncExpr`, `ShiftFuncExpr` today. `calc(DS[1:10].sum(), DS5)` is not seen as self-feeding, so it lands in the "frozen" path (see 2, fold) | seen | silent wrong |
| `core/analysis/prove/classify.py:871-955` `_interval_bounds` / `_domain_from_calc_expression` | finite domain of a calc dest | no arm, so no domain for `calc(rng.max(), M)` (flows into `value_domains` and the CMP rules). Exact arms are cheap: hull of member domains for max/min/median/mean | seen | decline |
| `core/analysis/prove/classify.py:553-563` `_expr_reads_tag` | TOUCHPOINTS names the `SumExpr` gap; it also misses `MathFuncExpr` and `ShiftFuncExpr`. Its one caller (`:693`) is the self-feeding "arithmetic" check | seen | silent wrong |
| `core/validation/cmp_conditions.py:207-229` `_operand_of_expr` | knows Tag/Literal/Binary/Unary only; everything else (Sum/MathFunc/Shift today) is opaque and renders as `?`. Add a generic `Expression` -> "computed" arm | seen | cosmetic / decline |
| `core/validation/math_conditions.py:54-61` `_division_nodes` | does not descend into MathFunc/Shift (or the new nodes). Division inside an aggregate operand is never flagged | seen | decline |
| `core/validation/render.py:125-137` | TOUCHPOINTS names `SumExpr` and abs. It also prints the class name for `MathFuncExpr` and `ShiftFuncExpr` (feeds `math_conditions.py:130`, `render.py:222`) | seen | cosmetic |
| `core/expression.py:482` `SumExpr.__repr__` | `sum(BlockRange(DS[1:10]))` is not DSL; it reaches DAP traces (`core/condition_trace.py:238-240, 341`) and the walker's `expr_dsl`. New kinds need DSL-shaped ASCII reprs | seen | cosmetic |
| **Correction:** `core/validation/walker.py:214-220` | TOUCHPOINTS 1.H says the generic `vars()` walkers pick up the `BlockRange`. True for pdg and codegen; **false** here: this walker treats an Expression as a leaf, so Click operand-bank checks never see the range members | seen | decline |
| Expressions outside calc: copy source (`core/instruction/resolvers.py:34-50`), fill value, compare RHS (`core/condition.py:132-163`), `ExprCompare`, indirect addresses (`core/memory_block.py:1295, 1435`) | aggregates are legal there in pyrung (`docs/instructions/math.md:56-61` advertises it). CLICK's calc-only rule is already enforced for any Expression by `CLK_EXPR_ONLY_IN_CALC` (`click/validation/portability.py:138-151`), so no new Click rule is needed. An aggregate *inside a pointer* is a separate case (next row) | seen | — |
| `core/analysis/pdg.py:660-691` `_indirect_expr_base_tag` | `DS[expr]` reads: takes the **first** Tag in the index expression and narrows the read set by that tag's min/max. For `DS[randint(Lo, Hi)]` or an aggregate index, the narrowing is wrong | seen | silent wrong |
| `circuitpy/codegen/compile/_primitives.py:792-815` `_compile_address_expr` | separate address-expression dispatcher, outside the 104-200 arm TOUCHPOINTS cites; raises on the new nodes | seen | crash |
| `click/codegen/_syntax.py:58-140` + `click/codegen/utils.py:226-243` | the importer's safe-syntax check rejects attribute calls, so `.max()` cannot be emitted directly; each new placeholder name (`_range_max`, ...) must join the `functions` set at 226-243, or import fails "Unsupported CSV operand expression" | seen | crash (import) |
| `core/validation/pointer_sites.py:95-103` | only affine calc writes give pointer values; an aggregate or randint dest used as a pointer is unknown | seen | decline |
| tests | `tests/core/analysis/test_crossings_concrete_oracle.py:798`, `tests/core/analysis/test_recorded_read_diff.py:35, 205, 241, 312, 346`, `test_prove_fuzz_reproducer_regressions.py:1136`; fuzz `strategies.py:321, 636-641, 922-924, 1012`, `reproducer.py:365-367`, `patterns.py:335-373, 920` | seen | gate |

Checked and safe (no change): `query.py:264-276`, `analysis/graph.py:129-133`, `crossings/copy.py:95-125, 229-255`,
`crossings/indirect_dest.py:102-107`, `prove/absorb.py:1029-1047, 1126, 1386`, `prove/classify.py:1530-1545`,
`runner.py:455-470`, `dap/expressions.py`; `affine.py:22-55`, `reverse_edges.py:64-128`, `sp_values.py:373-407`
return None as TOUCHPOINTS says; `init_constants.py:50-61` keys on literals.

### Serialization / docs / tests

| Site | Change | Conf. | Sev. |
|---|---|---|---|
| `docs/instructions/index.md:23` | the Math line says "range sums"; add the aggregates | seen | cosmetic |
| `docs/instructions/math.md:56-61` | says `SumExpr` "works anywhere expressions are accepted" and shows `copy(... .sum())` and `rung(... .sum() > 1000)`. Reconcile this with CLICK's calc-only rule for MAX/MIN/MDN/AVG, either as a portability finding or as a doc caveat | seen | cosmetic |
| `tests/fixtures/tumbler/csv/main.csv:199-200`, `main.py:476-479` | existing `SUM` uses: the regression case for the `_SUM_RE` generalisation and the `SumExpr` -> `RangeAggregateExpr` change, via `make test-tumbler` and the pilot goldens | seen | gate |
| `tests/click/test_codegen_input_safety.py:141-160` | parametrized list of supported CLICK expressions; add the aggregates, `ABS` and `RAND` | seen | cosmetic |

**Correction (K):** the two Click cheatsheets have **not** drifted. `docs/guides/click-cheatsheet.md` is a
single `--8<--` include of `src/pyrung/click/codegen/data/click-cheatsheet.md`. Edit only the bundled file.
This also retracts Surprise 10's third bullet. (seen)

---

## 2. `randint(lo, hi)`

### Fold skips draws (TOUCHPOINTS 2.H says the opposite)

TOUCHPOINTS: "the plateau guard refuses to fold a calc that changes every scan". **Wrong.**
- `core/fold.py:483-524` `_frozen_rung_writes` marks a rung's writes "frozen" when none of its reads are
  in the varying set. Frozen writes are excluded from the plateau guard (`:973-985`; exclude set at
  `:1638-1646`). `calc(randint(1, 6), Die)` with literal bounds reads nothing, so `Die` and everything
  downstream of it is frozen. The fold skips the draws, and a `latch`/`count_up` keyed on `Die` can be
  skipped. (seen, silent wrong)
- **This is a pre-existing bug, reproduced at HEAD.** `calc((X * 3) % 7, X)` plus
  `Rung(Tmr.Done, X == 5): out(Hit)`, then `run_until(Hit)`: folded lands at scan **501**, stepped at
  **503**, and `X` is in `frozen_writes`. Stubbing `_frozen_rung_writes` to empty gives 503. Probe:
  `fold_probe2.py` in the audit's scratchpad
  (`C:\Users\Sam\AppData\Local\Temp\claude\C--Users-Sam-documents-github-pyrung\4138f4d6-e9fe-4691-9a62-0ec484f688ec\scratchpad\fold_probe2.py`).
  Two fixes: a rung that reads its own write is never frozen; randint destinations are seeded as varying.
- The plateau check itself compares one probe pair of scans (`fold.py:1650-1700`, `_visible_items_match`
  1322). A draw repeats with probability 1/span, so "steady once means steady after" does not hold.
  `_scan_local_terminal_tags` (427-448) and `_unread_churn_tags` (404-424) exclude such tags too.
- Landing scan_id: `_do_fold` (`fold.py:1551-1587`) runs the one real scan at the *first* skipped scan_id,
  then sets `scan_id += skip - 1`; `CompiledPLC.step_replay` (`compiled_plc.py:528-580`) does the same. The
  landing draw would be H(first), not H(last).
- **Precedent for the fix:** `_SCAN_DERIVED_NAMES` already disables fold for `sys.scan_counter` and
  `scan_clock_toggle` (`fold.py:864-877, 1442-1446`). `_scan_rung_reads` cannot see a nameless expression,
  so treat every randint site the same way through a sentinel name.
- Callers affected: `run_for` (fold on by default, `runner.py:4552-4567`), `run_until` (4600-4636), compiled
  seek `_replay_seek` (2973), and the causal slab fill `_replay_slab_fill` (2357-2400), which `cause()` uses
  for evicted history. **Pilot depends:** `pilot/coast.py:184-206` repeats the `_SCAN_DERIVED_NAMES` gate,
  so a gate added only in `fold.py` does not reach it.
- `tests/fuzz/test_fold_soundness.py` states the frozen-rung assumption in its docstring; give it a randint
  shape (it would also have caught the pre-existing bug).

### Seed, scan_id and site plumbing (corrections to 2.B / 2.C)

| TOUCHPOINTS says | Actually | Conf. | Sev. |
|---|---|---|---|
| 2.C: inject `memory["_scan_id"]` beside `memory["_dt"]` | `_pass_discover_memory_keys` (`prove/passes.py:1748-1757`) turns every memory key not prefixed `_dt`/`_frac:`/effective-preset into a **state dimension**. A per-scan counter makes every BFS state distinct, so the search never converges. It would also have to be written by the interpreter, or `--runner-backend=both` memory parity fails. Better precedents: pass scan_id into the step, or follow `CompiledPLC._materialize_system_tags` (`compiled_plc.py:685-690`), which is how `sys.scan_counter` reaches compiled code | seen | crash / decline |
| 2.B Option 2: a memory key "written each scan like `_dt`" | `_dt` is written only when it changes (`runner.py:4068`, `compiled_plc.py:489`). `reboot()` and STOP->RUN wipe memory (`_reset_runtime_scope`, `runner.py:3652-3690`), so a seed key survives only if it joins `_apply_runtime_memory_flags` (3528-3537, the `_MODE_RUN_KEY` pattern). A constant seed key does not hurt the prover | seen | — |
| 2.B Option 1: propagate in fork / fork_from / replay_to / reboot / CompiledPLC | also needed: `Epoch` fields and `seal` (`runner.py:624-728`); `EpochQuery._runner` (745-790), which rebuilds a PLC from epoch fields only; `_replay_to_compiled` (2788-2797, builds `CompiledPLC` directly); `_fork_from_reconstructed_state` (2598); DAP hot reload `dap/reload_console.py:109-124` (copies dt/time_mode/rtc/forces only). Pattern to copy: `dt`/`time_mode`/`rtc_base` (Epoch field + `fork._set_time_mode` 2228 + CompiledPLC ctor arg). Mid-run reseed needs a new `LifecycleKind` (`scan_log.py:61-77`) and `_apply_lifecycle_to_replay` | seen | silent wrong |
| 2.B override "like `record_force_changes` / `rtc_base_changes`" | forces replace whole tag maps around a scan; a forced draw is per (site, iteration) and happens *inside* the scan. The real precedent is `io_drains_by_scan` (`scan_log.py:115-116, 161-165`), read via `ctx.is_replay_io` / `get_replay_io_drain` (`context.py:305, 731`; `send_receive/_core.py:251, 314`; `runner.py:2702-2705, 4043`). Caveat: compiled replay applies IO drains as tag writes **after** the step (`runner.py:2856-2861`), which is wrong for a draw used later in the scan. Override scans must take the `has_io_gaps` fallback (`runner.py:2570`) and join the fold boundary set (2805-2810) | seen | silent wrong |
| 2.A: stamp the site id in `_assign_state_keys` | that hook runs only in `Program.__exit__` (`_program.py:64-68`). `PLC(logic=[...])` (`runner.py:1126-1142`), synthesis plant/holds rungs and pilot rungs never get stamped and fall back to `str(id(obj))` (`instruction/base.py:104-108`; compiled `circuitpy/codegen/context.py:620-632`), which differs per process. The soft-exec program puts plant/holds ahead of user rungs (`runner.py:2531-2536`), so a site computed from rung position at compile time differs between the live interpreter and compiled replay. Stamp the site on the expression object at construction. `i{n}` counters shift on rung insertion, so a DAP reload changes draws | seen | silent wrong |
| 2.C: "`ReplayKernel` already has a `scan_id` slot" | true (`kernel.py:53, 99`). But the prover's `_step_compiled_kernel` (`prove/kernel.py:61-77`) never materialises scan-derived values, and BFS restores scan_id per snapshot (259, 289) while de-duplicating states without it. A hash-mode randint in the prover is path-dependent. The prove-mode switch already exists: `compile_kernel(..., proof_metadata=True)` at `prove/__init__.py:530, 895`, `prove/lockfile.py:221`, `prove/passes.py:990, 1170, 1524` (and pilot `drive_setup.py:315`, which must then supply the pseudo-input) | seen | silent wrong |
| "seek/replay/fork reproduce values" | true for fork (keeps scan_id) and CompiledPLC (`compiled_plc.py:642`). But `start()` (STOP->RUN) and `reboot()` reset scan_id to 0 (`runner.py:3425-3431, 3479-3505`), so every restart repeats the same draw sequence. A conformance "values vary" case that restarts would fail. Mix in a boot counter, or document it | seen | silent wrong |

### Other missed sites

| Site | Assumption | With randint | Conf. | Sev. |
|---|---|---|---|---|
| `prove/elision/slice.py:569-583` | write-before-read checked by one natural scan per enumerated combination at scan 0 | samples one draw, so it can certify a randint-conditional write as "always written". Sites must be enumerated vars, or bail | seen | silent wrong (unsound) |
| `prove/events.py` hidden-event acceleration (`TraceStep.scans > 1`) | inputs held fixed across an accelerated jump | a randint ND input held fixed covers only constant draw sequences. Same under-approximation TOUCHPOINTS notes for loops, but independent of them | inferred | silent wrong (unsound) |
| `prove/classify.py:2460-2500`, `prove/passes.py:534-578` | ND dims and snapshot scope come from PDG tags / `writers_of` | a pseudo-input that exists only in compiled code is never enumerated or snapshotted (constant draw). Making it a PDG tag exposes it to export, validation and TagMap. Design tension | inferred | silent wrong |
| prove ND domain size | — | `randint(1, 1000)` multiplies branching by 1000 per site per scan; needs a relevance cap (like `set_value_relevance`) or Intractable | inferred | decline |
| `prove/classify.py:871-955` `_interval_bounds` | — | `[lo, hi]` arm for a literal-bound randint gives the dest a finite domain | seen | decline |
| `prove/results.py:123-126` `TraceStep`; `tests/core/analysis/test_prove_matrix.py:50-56`, `test_prove_edge_demotion.py:83` `_replay_trace`; `dap/console.py:1084-1091` | a counterexample is patch-inputs-then-step-N | a pseudo-input name is not a patchable tag, and `scans > 1` cannot carry per-scan draws, so the rule that counterexamples replay on a concrete PLC (prove AGENTS.md:103) fails. Needs the override hook | seen | decline / gate |
| `causal/projected.py:330-356` `_simulate_scan`, `:363-397` `_rung_produces_value` | one simulated scan shows what a rung can write | goal `D == 3` is rejected whenever this scan's draw is not 3: "no producer". Wants the "random draw" root | seen | silent wrong / decline |
| `sp_values.py:673-713` (`_ProjectedView`, `_derive_one_hop`), `:367`; `causal/support.py:30-77`; `causal/projected.py:207, 305-321` (`_SnapshotView`); `core/condition_trace.py:72-76, 230-241` | duck-typed context views with no scan_id/seed re-evaluate expressions | `RandIntExpr.evaluate` must **raise** on these views, never default. `sp_values._expr_tag_names` (373-407) must keep returning None for randint; it is the "pure function of these tags" witness for `_derive_one_hop`, `_sole_calc_expression` (493), `classify.collect_produced_value_domains` (1418), and pilot (`program_facts.py:54`, `static_expressions.py:293`, `tide_tables.py:123, 514, 578`) | seen | silent wrong if defaulted |
| `causal/projected.py:343-356` | re-runs rungs with renumbered `capturing_rung(i)` and the state's own scan_id | an override hook that is *consumed* on read is consumed twice; site must not come from the executor's rung index | inferred | silent wrong |
| `core/analysis/pdg.py:660-691` | first tag in an index expression narrows reads | `DS[randint(Lo, Hi)]` narrows by `Lo`'s bounds (also listed under 1) | seen | silent wrong |
| core build-time check location | TOUCHPOINTS puts "randint only inside calc" at `calc.py:138` | expressions also enter through copy source (`resolvers.py:34-50`), fill, compare RHS (`condition.py:132-163`), `ExprCompare`, indirect addresses. The check belongs at the resolver / `_wrap` level | seen | silent wrong |
| `dap/condenser.py:212-243, 451-473, 494-517` | relevant tags = downstream slice of patched/forced tags | randint-only transitions look idle; spans get dropped or shrunk; later absolute scan_ids shift, so the condensed reproducer draws differently | seen | silent wrong |
| `dap/capture.py:142-156, 167-200` | transcripts depend only on relative scan counts | a recording that starts mid-session replays from the adapter's current scan with no seed. Record `# seed` / `# start_scan`, or warn | seen | silent wrong |
| `dap/miner.py:145-190`, `dap/spec.py:187, 237, 247` | mined correlations are causal | coincidental invariants from draws; generated `PLC(program, dt=...)` tests use the default seed. Exclude randint destinations | seen / inferred | cosmetic |
| `tests/conftest.py` `runner_factory` (allowed kwargs `{"dt", "initial_state", "compiled"}`), `_RunnerPair` | parity harness | a `seed=` kwarg makes the test skip under every backend. Needs a pass-through | seen | silent skip |
| naming: `runner._seed_defaults_for_fork` (2301), `_fork_seed_cache`, `compiled_plc.py:266`, `prove/kernel.py:209` `_seed_synthetic_presets` | "seed" already means default-value seeding | call the new knob `rand_seed` (or similar) | seen | cosmetic |
| `tests/conftest.py:361, 381, 418` | raw `ScanContext` with only `_dt` | `evaluate` must not require `_scan_id`/seed memory (see 0.6) | seen | crash |

Tests also missing from 2.J: `tests/core/test_fold.py`, `tests/fuzz/test_fold_soundness.py`,
`tests/fuzz/test_parity.py` (add randint to the grammar), `tests/core/test_compiled_replay.py`,
`test_record_and_replay.py`, `test_send_receive_replay.py` (override precedent), `test_runner_mode.py`
(reboot/start reset), `tests/dap/test_capture.py`, `test_miner.py`, `test_spec.py`,
`test_reload_console.py`, `tests/conftest.py`.

Checked and safe: `CalcCrossing.forward` returns UNKNOWN for non-affine expressions (`crossings/calc.py:130-165`);
`init_constants` folds literal copy/fill only (`:64-95`); fold self-calc/affine matchers and prove progress
absorption (`absorb.py:1108-1117`) need literal/TagExpr; no compile-time constant folding; interpreter and
compiled agree that `ctx.scan_id` is the pre-scan committed id (`context.py:527-530`, `compiled_plc.py:475-481`);
a disabled calc that skips its draw does not perturb later draws (stateless hash); `_recent_state_cache`,
`cause_memo`, `_fold_context_cache` stay correct if replay is deterministic.

---

## 3. `time_event_drum` + register presets

| Site | Change | Conf. | Sev. |
|---|---|---|---|
| `core/instruction/drums.py` (field declarations) | declare `events` in `_conditions` so the PDG gets `condition_reads` (pdg.py:968) and elision's `_collect_edge_tags` (slice.py:300-311) sees any edge inside an event. Declare register presets in `_reads`. `_exclusive_fields = ("current_step",)` is inherited from `_DrumBaseInstruction:204`; keep it | seen | silent wrong |
| `core/analysis/prove/elision/slice.py:260-280` | `drum_shift_writes` is an `isinstance` tuple of the two drum classes and shift: the "unmodelled cross-scan memory" bail. Add the new class (TOUCHPOINTS cites `:275` under H for the drum; listed here only because the same site matters more for feature 4) | seen | silent wrong |
| `core/validation/cmp_conditions.py:605-630` `_preset_sites` | `CMP_PRESET_STAYS_ZERO` covers timers and counters only. Drum duration registers (now legal for `time_drum` and T&E) that stay 0 are not flagged | seen | decline |
| `tests/fuzz/strategies.py:958` | `_TERMINAL_KINDS` (see 0.5) | seen | crash (fuzz) |
| `docs/instructions/index.md:27`; `docs/guides/ladder-lints.md:156` (`COIL_CONFLICTING_OUTPUT` writer-kind list) | text | seen | cosmetic |
| `tests/twin/AGENTS.md` checklist | "drum events are edge-based" needs a T&E-is-level counterpart | seen | cosmetic |

---

## 4. Out family: `toggle`, `debounce`, `flasher`, `timed`

| Site | Change | Conf. | Sev. |
|---|---|---|---|
| `core/analysis/prove/elision/slice.py:260-280` `_collect_memory_survey` | **soundness.** The bail set for hidden cross-scan memory lists only drums and shift. Toggle's previous-rung state, debounce/flasher phase, timed's latch and every `_frac:` remainder are the same kind of state. Without an entry, elision treats the variant's target as scan-local and may elide it | seen | silent wrong (unsound) |
| `core/analysis/prove/passes.py:1748-1757` `_pass_discover_memory_keys` | `excluded_prefixes = ("_dt", "_frac:", ...)`: `_frac:` keys are dropped from the prover state key because absorbed timers model them. Un-absorbed Out-owned accumulators using the `_frac:` naming would also lose their remainder from the state key. That is harmless at `unit="ms"` with integer-ms dt; it merges distinct states for `sec`/`min` | inferred | silent wrong (unsound) |
| 0.1 field declarations | `_writes` incl. Timer Done/Acc; presets in `_reads`; accumulators in `_exclusive_fields` | seen | silent wrong |
| `core/analysis/sp_values.py:184-208, 238-300` `writer_value_facts` | the fact source behind duplicate_out's one-hot mutual-exclusivity reasoning and pilot's source-alias recognition is `crossings.forward(instr)`. **Constraint:** every Out-variant crossing's `forward` must return `UNKNOWN`. Reusing `LatchCrossing` for toggle (tempting: "held") returns `Literal(True)` and turns toggle into a combinational fact | seen | silent wrong |
| `core/analysis/query.py:66-93` `_persistent_bits` -> `stranded_bits` (905-925) | only `latch()` targets count as persistent. A toggle target retains its value with the rung false, the same as a latch, so it belongs here (and maybe `timed(maintained=True)`) | seen | decline |
| `core/validation/cmp_conditions.py:605-630` `_preset_sites` | the `accumulating` tuple is TON/TOF/CTU/CTD; a variant phase preset Tag that stays 0 is not flagged | seen | decline |
| `core/analysis/simplified.py:420-440, 530-600` | a terminal's True form is its writer rungs' condition for any non-reset writer. A terminal driven by toggle/flasher/timed would render as "Y = rung condition". This approximation already exists for latch and timer done bits | seen | cosmetic |
| `core/runner.py:337-345` `_program_writes_read_only_system_tags`; `core/harness.py:171-185` `_program_writer` | both use `_collect_write_sites(target_extractor=readonly_write._any_write_targets)`. They inherit the early return after `target` that TOUCHPOINTS found, so an Out-owned Timer is invisible to the harness coupling-conflict check | seen | decline |
| `click/tag_map/_nickname_io.py:156` | T/TD nickname linking keys on the `_D` suffix pair. A project that nicknames only TD<n> for an Out coil produces no pair, which compounds Surprise 12 (rebuilding `Timer[n]` from TD alone) | inferred | decline |
| `editors/vscode/pyrung-debug/inlineValuesProvider.js:3-48` `IGNORED_INLINE_IDENTIFIERS` | hand list of verbs. `toggle`, `timed`, `flasher`, `debounce` (and `randint`, `break_loop`, `continue_loop`) would be looked up as tags. The list is already incomplete today | seen | cosmetic |
| docs | `docs/dialects/circuitpy.md:232` + `circuitpy/validation.py:435` (`CPY_TIMER_RESOLUTION` scope, if extended); `docs/guides/verification.md:82, 101` (the stateful list and the "three-valued Done abstraction" do not hold for Out-owned timers until they are absorbed); `docs/guides/ladder-lints.md:156`; `docs/instructions/index.md:17`; the cheatsheet's unit line "ms, sec, min, hour, day" is wrong for these verbs | seen | cosmetic |

---

## 5. For/Next: body rungs, `break_loop`/`continue_loop`, nesting

### Execution-journal identity (TOUCHPOINTS covers the executors, not the journal consumers)

| Site | Change | Conf. | Sev. |
|---|---|---|---|
| `core/executor.py:36` `ExecutionKind = Literal["rung","branch","subroutine","synthetic"]` (and `RungRun.kind` 154, builders 249) | a body rung's `RungRun` needs a kind. Every consumer below breaks one way or the other, depending on the choice | seen | — |
| `core/analysis/causal/_rung_writes.py:888-938` `_dynamic_branch_paths` | for `kind == "branch"` it looks the rung up in `parent_run.rung._branches` and **raises RuntimeError** if absent. Body rungs are not in `_branches`, so reusing "branch" crashes; any other kind gets path `()`, the loop rung's own address, which collides (misattribution). The address also has no iteration coordinate. `_dynamic_call_invocations` (835-885) also walks `LoopIterationRun` | seen | crash / silent wrong |
| `core/intrascan_counterfactual.py:25-60` `OccurrenceBoundary`; `:137-200` `begin_rung`; `:311-315` | the boundary has no loop-iteration or body-index field; `begin_rung` has the same `_branches` lookup that raises; `begin/end_loop_iteration` are no-ops. A patch at a body-rung boundary would match every iteration, so `applied_exactly_once` fails. **Pilot input** (`pilot/intrascan_research.py`, `navigation_contracts.py`, `theory_orientation.py`) | seen | crash / silent wrong |
| `core/debug_trace.py:89` (`kind: Literal["rung","branch","subroutine","instruction"]`), `core/debugger.py:26-27` (`RungKind`, `StepKind`), `dap/formatter.py:27-36, 310-318` | closed step-kind Literals and their DAP labels; a body-rung step needs a kind and a label. The trace carries one set of condition regions per rung per scan, so per-iteration body-rung conditions need a policy (last iteration? all?) | seen | cosmetic to silent wrong (decorations) |
| `core/executor.py:1448` | `forced_on` mode executes `ReturnInstruction` disabled so a forced run doesn't exit; Break/Continue need the same. `forced_on` has no external callers today | seen | cosmetic |

### Break/continue are a new "return shadow"; three return-shadow analyses lack the analogue

TOUCHPOINTS cites `return_guards.py` and elision. These sites encode "an earlier `return_early()` may skip
the rest" with their own `isinstance(..., ReturnInstruction)` scans:

| Site | Change | Conf. | Sev. |
|---|---|---|---|
| `core/analysis/pdg.py:1284-1316` `_augment_return_early_guards` (and `:356-380`, which strips `guard_reads`) | body items after a conditional break/continue need the break rung's condition reads as `guard_reads`/`condition_reads` | seen | silent wrong (cones, causal, fold, prove inputs) |
| `core/analysis/prove/passes.py:1300-1355` functional-dependency check ("no `return_early()` between X's last writer and Y's writer") | a conditional break/continue between two body writers voids the X->Y functional dependency | inferred | silent wrong (unsound) |
| `core/validation/stuck_bits.py:149-170` `_return_rungs` | an `out` after a break cannot be proven to execute | seen | silent wrong (finding) |

### Build-time and strict-DSL

| Site | Change | Conf. | Sev. |
|---|---|---|---|
| `core/program/validation.py:217-233, 262` | the strict-DSL AST check raises `ForbiddenControlFlowError` for `comment()` anywhere under a `with rung(...)` (`inside_rung` propagates through nested `with`). A comment on a body `with Rung(...)` inside `forloop` is rejected. Code the importer generates for commented CLICK body rungs would fail the check | seen | crash (import round-trip) |
| `core/program/validation.py:91, 104` (+ `_FORBIDDEN_NODE_RULES` 86-104) | Python `break`/`continue` get the generic statement hint; point them at `break_loop()`/`continue_loop()` | seen | cosmetic |
| `core/program/context/_program.py:125-147` `_iter_call_instructions` | walks loop `.instructions` only. A `call()` inside a body rung escapes `_validate_call_targets` (undefined subroutine not caught at build) unless `.instructions` stays a complete flat view | seen | decline |
| `core/program/context/_program.py:73-96` `_assign_state_keys` | `i{counter}` keys follow walk order; bare body instructions must keep their walk position, or the oneshot memory keys of every later instruction renumber (old scan-log/capture memory no longer lines up) | inferred | silent wrong (back-compat) |
| `core/analysis/pdg.py:89-96` `CallSite` | no loop depth at the call site; the C010D depth-3-across-`call()` rule needs it | inferred | — |

### Serialization

| Site | Change | Conf. | Sev. |
|---|---|---|---|
| `click/ladder/_exporter.py:415-427` `_collect_instruction_types` | would see `Rung` children and skip their instructions, so `break_loop`/`continue_loop` renames are missing from the export summary | seen | cosmetic |
| `click/ladder/types.py:59-73` `ExportSummary.added_next` | counts outer loops only (`_forloop_count`) | seen | cosmetic |
| `docs/instructions/index.md:28` | Program Control line | seen | cosmetic |

### Pre-existing blindness (loop bodies invisible today, unchanged by the feature, worth knowing)

`core/analysis/query.py:45, 83, 307-311, 353, 396, 411, 465, 667` (step registers, persistent bits, written
registers, unguaranteed names), `core/analysis/sp_values.py:211-230` `_writer_for_tag`,
`core/analysis/steerable.py:45, 109`, `core/validation/_common.py:104-125` `_build_tag_map`: all walk
`rung._instructions` and never descend into loops. (seen, decline)

---

## 6. Twin -> conformance

| Site | Change | Conf. | Sev. |
|---|---|---|---|
| `tests/twin/AGENTS.md` "Coverage checklist" | needs O*/D*/M*/F* lines; "forloop dynamic count == 0" overlaps F3 | seen | cosmetic |
| `pyproject.toml:188, 199` | `python_files=["*.py"]` and `testpaths` includes `src`. Anything named `test_*` inside a new `src/pyrung/conformance/` is collected, and its imports run at collection time | seen | cosmetic risk |
| leftovers | `scratchpad/deadcode-review.md:16`, `scratchpad/persistent_map_migration_note.md:88`; user memory (`MEMORY.md`, `project_click_v4_instructions.md`, about 4 others) mention twin | seen | cosmetic |

TOUCHPOINTS section 6's "no `twin` references in Makefile / pyproject / .github / docs / mkdocs" is
**correct** (re-verified). So is 5.I's "VS Code extension is generic over trace events": no `.js` file
switches on an instruction kind. The one exception is the inline-value verb list above.

---

## 7. Corrections to TOUCHPOINTS.md (consolidated)

| # | TOUCHPOINTS claim | Correction | Conf. |
|---|---|---|---|
| 1 | 0.4: `exclusive_resources()` has no callers, so exclusivity is dead | the method is dead, but `_exclusive_fields` is live (pdg `exclusive_reads` -> elision closure, fold). Also dead: `always_execute()`/`ALWAYS_EXECUTES` | seen |
| 2 | 0.2: `PUBLIC_RUNG_INSTRUCTIONS` forces a graph-matrix case for every public verb | it is a hand list compared to `GRAPH_CASES`, not derived from `__all__`. A new verb passes silently | seen |
| 3 | 0.2: the `gen_reference` manifest check is a forcing function | it runs only in `make docs-build/docs-check/verify` and `docs.yml` on main, not in `make test` or dev CI | seen |
| 4 | 0.1: `make test` asserts tag+memory parity between backends | true locally; **CI does not** (`ci.yml:66`, no `--runner-backend=both`) | seen |
| 5 | 0.3 list of class-name tables | add `click/ladder/instructions.py:353` (`OffDelayInstruction` token), `:416` (`EventDrumInstruction` token), `:536-544` (Call/Return/Nop/Raw tokens, where Break/Continue tokens go), `core/rung.py:76-109` (`is_terminal` gate; `TimeEventDrumInstruction` inherits `True` from `_DrumBaseInstruction:244`) | seen |
| 6 | 1.A: keep `SumExpr` as "subclass or alias" | alias is unsafe: every `isinstance(x, SumExpr)` arm computes or exports a sum | seen |
| 7 | 1.H: generic `vars()` walkers pick up `BlockRange` automatically | true for pdg/codegen; **false** for `validation/walker.py:214-220` (Expression is a leaf) | seen |
| 8 | 1.H: `_expr_reads_tag` ignores `SumExpr` | also ignores `MathFuncExpr` and `ShiftFuncExpr`; so does `fold._calc_self_referential.reads_tag` (196-202) | seen |
| 9 | 1.K / Surprise 10: the two cheatsheets have drifted | `docs/guides/click-cheatsheet.md` is a `--8<--` include of the bundled file; there is one file | seen |
| 10 | 2.A: site id via `_assign_state_keys` | not run for `PLC(logic=[...])`, synthesis or pilot rungs; the fallback is `id()`, which differs per process | seen |
| 11 | 2.B: "a memory key written each scan like `_dt`" | `_dt` is written only on change; reboot/STOP->RUN wipe memory unless the key joins `_apply_runtime_memory_flags` | seen |
| 12 | 2.B: override hook like `record_force_changes`/`rtc_base_changes` | the precedent is `io_drains_by_scan` (in-scan, per-site); compiled replay applies drains after the step, so override scans must fall back to interpreted | seen |
| 13 | 2.C: inject `memory["_scan_id"]` | the prover's memory-key discovery would make it a state dimension (BFS never converges) | seen |
| 14 | 2.H: fold's plateau guard refuses a calc that changes every scan | `_frozen_rung_writes` freezes read-nothing / self-feeding calcs; fold skips them. **Pre-existing bug reproduced** (501 vs 503) | seen (ran it) |
| 15 | 2.B seed propagation list | misses Epoch/`seal`, `EpochQuery._runner`, `_replay_to_compiled`, `_fork_from_reconstructed_state`, DAP hot reload | seen |
| 16 | 2.G: "core build-time check: randint only inside calc" at `calc.py:138` | expressions enter through resolvers, fill, compare RHS, `ExprCompare`, indirect addresses; the check belongs at `_wrap`/resolver level. Click's side is already covered by `CLK_EXPR_ONLY_IN_CALC` | seen |
| 17 | 4: "the real work is in three kinds of site" (writer enumerations, per-class arms, timer-owner sites) | a fourth and bigger kind: the field declarations (`_writes`, `_reads`, `_conditions`, `_exclusive_fields`, `_status_fields`) feeding `write_sites.instruction_write_targets` and pdg, with about 20 consumers and no `isinstance` to grep | seen |
| 18 | 4.H / 3.H: `slice.py:275` listed for the drum only | the same bail set must include all four Out variants (hidden memory); otherwise elision is unsound | seen |
| 19 | Addendum: "check whether `test_crossings_coverage.py` catches an inherited registration" | it does not: an `OutInstruction` subclass in `EXEMPT` passes and inherits `OutCrossing` | seen |
| 20 | Addendum: crossings matter for `reverse`/`propose` | `forward` matters too: `sp_values.writer_value_facts` (duplicate_out one-hot reasoning, pilot source alias) trusts `forward`; Out-variant crossings must return `UNKNOWN` | seen |
| 21 | 5: "about 28 files walk `.instructions`" and the executors | the walker list is essentially complete (re-swept: only `_program.py:125-147` `_iter_call_instructions` was missing). The bigger miss is the **execution journal**: `ExecutionKind`, `causal/_rung_writes._dynamic_branch_paths` and `intrascan_counterfactual.OccurrenceBoundary` key rung identity on `_branches` membership, which raises for body rungs | seen |
| 22 | 5.A: comment consumption by a body rung is a `_rung.py:58-62` matter | the strict-DSL AST check (`program/validation.py:228-233`) rejects `comment()` anywhere under `with rung(...)`, including before a body rung | seen |
| 23 | 5.I: "VS Code extension is generic (no change)" | true for trace events; `inlineValuesProvider.js:3-48` has a hand verb list, and the Python-side step kinds (`debug_trace.py:89`, `debugger.py:26-27`, `dap/formatter.py:27-36, 310-318`) are closed Literals | seen |
| 24 | 6: no `twin` in Makefile/pyproject/.github/docs/mkdocs | confirmed correct | seen |
