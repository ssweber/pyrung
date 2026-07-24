# Scout report: dead-code / refactor-residue hunt (2026-07-23, Opus, read-only)

Brief: find refactor residue, dead code, duplicated helpers, and over-general machinery
left behind by the ownership refactor series; estimate safely deletable LOC.

---

Scope: `src/pyrung/core/analysis/pilot/` (29 modules, ~26k LOC) plus callers in `tests/` and `src/pyrung/dap/`. Method: read `CLAUDE.md`, enumerated every top-level `def`/`class`, counted repo-wide references (src vs. tests separately), then hand-verified each candidate's callers and read the bodies. The Tumbler goldens lock behavior, so "provably dead" below means *no production reference exists* — deletion cannot change any decision sequence.

Headline: the ownership refactor was disciplined. It did **not** leave large orphaned subsystems; `accumulators.py` was deleted cleanly (no dangling imports anywhere). What remains is a scatter of **thin compatibility shims** and a few **test-only helpers** that production stopped calling. The safe, provable win is modest (~150 source LOC + ~200-300 test LOC), not thousands. I flag the larger-but-riskier tier at the end.

## Ranked findings (biggest safe win first)

### 1. `evidence.py:126` `expand_pipeline_need` — production-dead, test-only

- Zero production references. Only caller is `tests/core/analysis/test_pilot.py:2050,2115` (single test `test_expand_routes_indirect_jump_table_pipeline`, lines ~2046-2125).
- Source LOC: 32. Test LOC deletable: ~80 (the whole test).
- Risk: **provably dead in production**; deleting the test drops the only coverage of this path, so confirm the live pipeline-expansion entry point (`evidence.py` still owns route expansion via other functions) covers the same case — a golden run confirms.
- Coupling: none beyond its own test.

### 2. `advance.py` — dead `next_advance` + test-only `estimate_scans`/`measure_scans`

- `next_advance` (`advance.py:115-127`, 13 LOC): **fully dead**. Not imported by any module (`__init__` and all pilot modules import only `iter_advance_owners`, `build_advance_index`, `demand_holds`), not referenced by any test. The `test_next_advances_...` hit in `tests/dap/test_adapter.py` is an unrelated DAP symbol.
- `estimate_scans` (`advance.py:~149`, 9 LOC) + `measure_scans` (`advance.py:159-177`, 19 LOC): **production-dead**. Every `estimate_scans` reference in `src/` is `profile.linear.estimate_scans` (the `LinearProgress` dataclass method in `core/instruction/advance.py`), a different symbol. The pilot module-level `estimate_scans(owner, constraint, plc, …)` is called only by `tests/core/analysis/test_pilot_advance.py:112` and `tests/core/analysis/test_pilot_coupling_profile.py:52,90`. `measure_scans` is called only by `estimate_scans`, so the two die together.
- Source LOC: ~41 (of a 177-line module). Test LOC: a handful of asserts in two test files (not whole files).
- Risk: `next_advance` **provably dead**; `estimate_scans`/`measure_scans` **provably production-dead** (delete the fork-measuring path plus its two test cases). Coupling: `measure_scans` ↔ `estimate_scans` must go together.

### 3. `cyclefold.py:144-165` `_periods_to_crossing` — fully dead

- 22 LOC. No callers anywhere. Its helper `_monotone_read_surface` is **not** coupled-dead — it is still live at `cyclefold.py:441`, so only `_periods_to_crossing` is removable.
- Risk: **provably dead**. Coupling: none (do not delete `_monotone_read_surface`).

### 4. `corrections.py` compatibility shims — `_resolve_steerable_driver` + `_resolve_partial`

- `_resolve_steerable_driver` (`745-761`, 17 LOC): docstring literally "Compatibility projection for callers that genuinely need only a pair." Zero callers. Live siblings `_resolve_steerable_action` (used at 754, 894) remain.
- `_resolve_partial` (`911-920`, 10 LOC): docstring "Compatibility projection of structural actions to action pairs." Zero callers. Live sibling `_resolve_partial_actions` (used at 919, 964) remains.
- Source LOC: 27. Test LOC: none.
- Risk: **provably dead** shims left as adapters over the surviving `_resolve_*_action(s)` owners. Coupling: none.

### 5. `pilot.py:545-557` `_diagnose_stuck` — fully dead

- 13 LOC. No callers. The live stuck-diagnosis owner is `options.py:158 _diagnose_stuck_reason` (used at `options.py:1487`); this `pilot.py` copy is orphaned residue of the decision-ownership move to `options.py`.
- Risk: **provably dead**. Coupling: none.

### 6. `investigate.py:2195-2202` `_precise_cause` (singular) — test-only shim

- 8 LOC. Docstring "Compatibility helper returning the first exact causal frontier"; body is `_precise_causes(plc, incident, ctx)[0]`. Production uses the plural `_precise_causes` (`investigate.py:1448, 2201`). Singular is called only by tests (`test_pilot_investigate.py:1344,1403`).
- Risk: **provably production-dead**. Deletion requires rewiring 2 test call sites to `_precise_causes(...)[0]` (not full test deletion). Coupling: none.

### 7. `options.py:1575-1579` `_co_actions` — fully dead

- 5 LOC. `def _co_actions(candidate, applied): return tuple(pair for pair in applied if pair != candidate.pair)`. No callers (`route_co_actions`/`edge_co_actions` are unrelated field names). Superseded by the new owner-operation co-action handling.
- Risk: **provably dead**. Coupling: none.

### 8. `outcome.py:308-310` `classify_outcome` — test-only shim

- 3 LOC. "Compatibility projection for focused callers and external probes"; returns `assess_outcome(*args, **kwargs).legacy_outcome`. Only caller is the `_classify` helper in `tests/core/analysis/test_pilot_outcome.py:16`. Production classifies via `assess_outcome` directly (`verify.py:627`).
- Risk: **provably production-dead**. Rewire the one test helper to `assess_outcome(...).legacy_outcome`. Note `legacy_outcome` itself (`outcome.py:78`) is still live at `verify.py:627`, so it must **stay** (see non-findings).

### 9. `investigate.py:1258` dead `program` parameter on `build_deviation_incident`

- Docstring (`1275`): "*program* is retained for call compatibility. It no longer changes the evidence recorded in the incident." The parameter is unused in the body. Passed only at `progress.py:1244` (`program=ctx.program`); the in-module call at `investigate.py:761` already omits it.
- Deletable: 1 signature line + 1 doc paragraph + `progress.py:1244` + ~6 test call sites (`test_pilot_investigate.py`, `test_pilot_absence_root.py`, `test_pilot_dwell_liveness.py`). ~10 LOC net, spread out.
- Risk: **provably dead** parameter. Coupling: touch all call sites together.

## Bottom line (safe, provable tier)

- **Source LOC realistically deletable: ~150-170.** (32 + 41 + 22 + 27 + 13 + 8 + 5 + 3, plus the ~10-line `program`-param cleanup.)
- **Test LOC deletable/simplifiable: ~200-300**, dominated by the `expand_pipeline_need` test (~80) and the `estimate_scans`/`_precise_cause`/`classify_outcome` rewires.
- Every item in findings 2-9 (except the noted test-coverage caveat on #1) is **provably dead** by caller enumeration and does not need a golden run to justify removal — though running `make test-tumbler` after is still the cheap confirmation.

## Non-findings (checked, do NOT delete)

- **`provisional_*` event names** (`progress.py:566,672,772,792,823`): CLAUDE.md calls them "compatibility vocabulary only," but `tests/tumbler/golden/*.json` and `tests/tumbler/skeleton.py:270-298` and `src/pyrung/dap/console.py:656` all consume them. They are the locked public event contract — **not** deletable despite the "compatibility" label.
- **`legacy_outcome`** (`outcome.py:78`): still live at `verify.py:627`. Stays.
- **coast legacy property aliases** `kernel_scans`/`macro_folds`/`skipped_scans`/`logical_scans` (`coast.py:116-138`): thin aliases over `real_scans`/`folds`, but consumed at `coast.py:417-420` (a log/telemetry line) and by `test_pilot_coast.py:855-857`. Marginal; only worth collapsing if you also touch the log line and those asserts — low yield, skip.
- **Duplicated eval/walk helpers** (a hypothesis in the brief): largely *already consolidated* by this refactor. Only one guard-eval helper survives (`availability.py:84 _partial_eval_guard`); expression walking is not duplicated across trace/options/verify. Low yield here — the refactor already did this work.

## Larger but riskier tier (needs a golden run to size safely — not counted above)

These are structural, not single-symbol, so I did not include them in the provable total:

- **Dual outcome vocabulary**: the `Outcome` enum + `legacy_outcome` projection coexisting with the newer `assess_outcome`/`BearingEffect`/`Agency` result. Once `classify_outcome` (finding 8) is gone, `legacy_outcome` has a single remaining consumer (`verify.py:627`); migrating that one site to the structured result would let the entire `Outcome`-enum legacy layer be retired. Plausibly 30-60 LOC across `outcome.py`/`verify.py`, but it changes a classification surface — **needs golden run**.
- **`outcome.py:277`** stub branch ("Stub: for now we accept; full 'learn both' is future work") — over-general machinery: a hook for a case that is never distinguished. Removing the stub arm simplifies the function but is behavior-adjacent — **needs golden run**.
- **`compass.py:300-499`** carries an explicit "legacy pair observation" interpretation path alongside singleton-Pulse handling (`_observation_exercised_edge`, `_table_record/_no_change/_contradict`). CLAUDE.md frames these as one owner, but the "legacy action observations interpreted literally" comments (228, 312, 499) suggest a dual old/new evidence path. Worth a focused read against `test_pilot_nogood.py`/`test_pilot_recording.py` before touching — **needs golden run**.

If the maintainers want a second pass, the outcome/`Outcome`-enum collapse (first riskier bullet) is the most likely place to find a genuinely bigger delete, because it removes a whole vocabulary layer rather than one orphaned function.
