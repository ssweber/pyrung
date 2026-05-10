# Plan: `Explanation` for `prove()`

## Context

When `prove()` returns `Proven`, it gives back `states_explored` and `caveats` — no visibility into what the prover decided about each tag. This makes debugging soundness issues hard: you can't see where a tag was misclassified, scoped out, or absorbed. The Explanation is a per-tag accounting of every decision the prover made during the pass pipeline, attached to the result when `explain=True`.

Phase A adds the Explanation. Phase B cleans up the channels it makes redundant.

**Status: Phase A implemented.** All types, builder, pipeline instrumentation, and public exports are in place. 19 tests cover the plan items (see test mapping below).

## Data types — `results.py`

Add three frozen dataclasses:

```python
Decision(pass_name, kind, outcome, reason, detail={})
TagEntry(name, classification, domain, domain_source, cone, outcome, decisions)
Explanation(tags: MappingProxyType[str, TagEntry])  # with __iter__, __getitem__, __len__, __str__
```

Add `explanation: Explanation | None = None` field to `Proven`, `Counterexample`, `Intractable`.

## Phase A: Add Explanation

### 1. Types (`results.py`)
- Add `Decision`, `TagEntry`, `Explanation` dataclasses
- Add `explanation: Explanation | None = None` to all three result types
- `Explanation.__str__` renders multi-line per-tag format

### 2. Builder (`passes.py`)
- Add `_ExplanationBuilder` class near `_DiagnosticAccumulator` (~line 144)
  - `record(tag_name, decision)` — appends to per-tag list
  - `freeze(graph_tags, stateful_dims, nondeterministic_dims, combinational_tags, absorptions, threshold_absorptions, elided_tags)` — builds `Explanation`
- Freeze logic: iterate all tags in `graph.tags`, synthesize `TagEntry` from classification dicts + recorded decisions + cone derivation (diff graph tags vs classified tags)

### 3. Thread through pipeline
- Add `explanation_builder: _ExplanationBuilder | None = None` to `_PassContext` (after `obligations`, ~line 187)
- Add `_combinational_tags: frozenset[str] | None = None` to `_PassContext` (new field — currently `_comb` is discarded at passes.py:379)
- Add `_elided_tags: dict[str, str] | None = None` to `_PassContext` (captures which tags were elided and by what method — `"provenance"` or `"concrete"`)
- Add `explanation: Explanation | None = None` to `_ExploreContext` (__init__.py:37)
- In `_PassContext.freeze()` (~line 213): if `explanation_builder is not None`, call `freeze()` and pass result to `_ExploreContext`
- Add `explain: bool = False` param to `_build_explore_context()` (__init__.py:106); create builder conditionally
- Add `explain: bool = False` param to `prove()` (__init__.py:372); pass through to `_build_explore_context()`

### 4. Save combinational tags
- In `_pass_classify_dimensions` (passes.py:379): save `ctx._combinational_tags = _comb`
- In `_pass_classify_dimensions_no_absorb` (passes.py:~682): same

### 5. Return elision method from `_elide_scan_local_stateful_dims`

The `_ElisionContext.elided` dict already maps tag name → method (`"provenance"` or `"concrete"`) but is discarded at elision/__init__.py:124. Change the return type:

```python
def _elide_scan_local_stateful_dims(...) -> tuple[dict[str, tuple[Any, ...]], dict[str, str]]:
    ...
    return ctx.stateful_dims, ctx.elided
```

Update the single caller in `_pass_elide_scan_local_state` (passes.py:532) to unpack both values.

### 6. Instrument passes (all guarded by `if ctx.explanation_builder is not None:`)

**classify_dimensions** (passes.py:366) — two paths:

*Success path* (after unpacking at line 384):
- For each tag in `sd`: record classification=stateful + domain decision
- For each tag in `nd`: record classification=nondeterministic + domain decision
- For each tag in `_comb`: record classification=combinational

*Intractable path* (after `isinstance(result, Intractable)` at line 376):
- For each tag in `result.tags`: record classification=infeasible with reason from `result.reason`
- For each `_ThresholdBlocker` in the Intractable context: record absorption_blocked with per-reason detail
- This ensures the explanation shows WHY classification failed, not just that it did

**pilot_sweep** (passes.py:387) — after domain discovery:
- For newly discovered tags: record domain decision with source="pilot"

**elide_scan_local_state** (passes.py:532) — using returned elided dict:
- Unpack `(reduced_dims, elided_dict)` from the modified function
- For each entry in `elided_dict`: record elision decision with method (`"provenance"` or `"concrete"`)
- Save `ctx._elided_tags = elided_dict`

**collect_done_acc_pairs** (passes.py:~560):
- For each done/acc pair: record pairing decision

**find_redundant_absorptions** (passes.py:~564):
- For absorbed acc_names: record absorption=three_valued
- For absorbed preset_tags: record absorption=synthetic_preset
- For *rejected* candidates: record absorption_skipped with reason. Currently `_find_redundant_acc_absorptions` (absorb.py:382) has four silent `continue` points — modify to return a `rejected: dict[str, str]` alongside absorbed, mapping acc_name → reason:
  - `"not consumed by Done/Acc pair"` (line ~395)
  - `"accumulator comparisons not redundant"` (line ~404)
  - `"preset atoms not fully absorbed"` (line ~408)
  - `"preset tag has non-timer data reads"` (line ~410)
- Add `rejected` field to `_RedundantAccAbsorptions` dataclass

**find_threshold_absorptions** (passes.py:~583):
- For progress_names: record absorption=threshold_vector
- For threshold_tags: record absorption=threshold_tag
- For comparison_tags: record absorption=comparison_only
- For each `_ThresholdBlocker` in `ctx.threshold_absorptions.blockers`: record absorption_blocked with each reason as a separate Decision entry

**freeze()** — three decision types:

*Cone exclusion:*
- For tags in `graph.tags` but not in classified∪absorbed∪elided: record cone=excluded

*Input partition:*
- For each ND tag in `edge_bearing` set: record input_partition=edge_bearing ("previous-scan value affects behavior, included in state key")
- For each ND tag in `free` set: record input_partition=free ("current value doesn't constrain future behavior, excluded from state key")

*Exclusive input groups:*
- For each member in each `_ExclusiveInputGroup`: record exclusive_group with group target and members

### 6. Attach to results (`bfs.py`)

All result construction sites pass `explanation=context.explanation`:
- `Proven(...)` at lines 458, 462
- `Counterexample(...)` at lines 149, 153 — via closure capture
- `Intractable(...)` at lines 380, 425

For `Intractable` from the pipeline (early exit in `_run_pre_bfs_pipeline`, passes.py:849): freeze partial explanation and attach via `dataclasses.replace`.

For batch results in `prove()` (line 477, fallback `Proven(states_explored=0)`): attach `None` since no context was built.

### 7. Public exports
- `prove/__init__.py`: re-export `Decision`, `TagEntry`, `Explanation`
- `analysis/__init__.py`: add to imports and `__all__`

### 8. Tests (`test_prove_passes.py`)

New `TestExplanation` class:
1. `test_explain_false_returns_none` — default path, `explanation is None`
2. `test_explain_classifications` — stateful/ND/combinational tags classified correctly
3. `test_explain_domain_sources` — Bool, choices, min/max identified
4. `test_explain_elision_provenance` — tag elided by abstract provenance, `outcome="elided:provenance"`
5. `test_explain_elision_concrete` — tag elided by concrete kernel proof, `outcome="elided:concrete"`
6. `test_explain_redundant_absorption` — timer acc absorbed, decisions recorded
7. `test_explain_threshold_absorption` — threshold vector decisions
8. `test_explain_threshold_absorption_blocked` — tag where threshold absorption failed, `Decision(kind="absorption_blocked")` with specific reason from `_ThresholdBlocker`
8b. `test_explain_redundant_absorption_skipped` — Done/Acc pair where redundant absorption was rejected, `Decision(kind="absorption_skipped")` with reason (e.g. "accumulator comparisons not redundant")
9. `test_explain_cone_exclusion` — explicit scope excludes tags, `cone="excluded"`
10. `test_explain_input_partition` — ND tags marked as `edge_bearing` vs `free`
11. `test_explain_exclusive_group` — exclusive input group members recorded
12. `test_explain_intractable_infeasible` — Intractable result carries per-tag infeasibility decisions
13. `test_explain_counterexample` — explanation attached to Counterexample
14. `test_explain_batch_shared` — batch results share same Explanation instance
15. `test_explanation_str` — `__str__` renders readable per-tag format
16. `test_explanation_getitem_iter` — `[]` and `iter()` work

## Phase B: Clean up redundant channels

Once Phase A lands and tests pass, remove the infrastructure the Explanation supersedes.

### B1. Retire `_ProofObligation` and `_discharge_obligations` (`passes.py`)
- Delete `_ProofObligation` dataclass (line 165)
- Delete `_DISCHARGE_HANDLERS` dict (line 728) and `_DOWNSTREAM_PASS_NAMES` (line 730)
- Delete `_discharge_obligations()` function (line 735)
- Remove `obligations: list[_ProofObligation]` field from `_PassContext` (line 187)
- Remove obligation check in `_run_pre_bfs_pipeline` (lines 855-859)
- The obligation concept is now handled by assumption-type `Decision` entries with discharged/reverted status
- Update `test_prove_passes.py` — remove any tests that reference `_ProofObligation`

### B2. Replace `_DiagnosticAccumulator` (`passes.py`)
- Route `info`-level messages (never-written tags at line 507, missing external at line 523) through `progress_info` callback directly instead of accumulator
- Route `warning`-level messages through explanation entries (when explain=True) or keep as caveats (when explain=False)
- If explain=True: warnings become `Decision` entries with severity, caveats derived from explanation
- If explain=False: build caveats directly in `freeze()` without accumulator
- Delete `_DiagnosticAccumulator` and `_DiagnosticEntry` classes (lines 137-161)
- Remove `diagnostics` field from `_PassContext` (line 186)
- Update `_PassContext.freeze()` caveat assembly (lines 264-271)
- Update `TestDiagnosticAccumulator` in `test_prove_passes.py` — replace with tests asserting equivalent behavior through explanation or caveats

### B3. Derive `Intractable.hints` from Explanation (`classify.py`, `bfs.py`, `results.py`)
- When explain=True: `Intractable.hints` becomes a derived view — filter explanation entries by `outcome ∈ {infeasible, blocked}` and render
- Delete `_build_infeasible_hints()` (classify.py:1158-1192) — its logic moves into the explanation builder's classification recording
- Delete `_build_dimension_hints()` (classify.py:1195-1217) — its logic moves into the explanation builder's freeze, computing per-tag domain size
- Remove `_ThresholdBlocker.reasons` rendering from hints — blocker reasons are now structured `Decision` entries from the absorption pass
- Keep `Intractable.hints` field for backward compat but populate it from explanation when available
- Update `TestIntractableHints` in `test_prove.py` — assert hints still present, verify they now derive from explanation entries

### B4. Fold `_detect_edge_caveats` into explanation (`passes.py`)
- Each ND input already has a classification entry (from Phase A step 5)
- Add edge-bearing/free partition info to ND input entries: `detail={"edge_bearing": True/False, "covered_by_joint": True/False}`
- Derive uncovered-edge caveats from explanation entries instead of calling `_detect_edge_caveats`
- Delete `_detect_edge_caveats()` (passes.py:51-75)
- Update `freeze()` caveat assembly to query explanation for uncovered edges

### B5. Fold `_ThresholdBlocker` into explanation (`absorb.py`, `classify.py`)
- Currently `_ThresholdBlocker(acc_name, kind, reasons)` is created in absorb.py and rendered into hints in classify.py
- With explanation: each blocked absorption attempt becomes a `Decision` entry on the tag with `kind="absorption_blocked"`, `reason=` the blocker reason
- Delete `_ThresholdBlocker` dataclass (absorb.py:1338-1342)
- Remove `blockers` field from `_ThresholdAbsorptions` (absorb.py:1351)
- Absorption-blocked tags get their blocker reasons as structured decisions instead of nested hint strings
- The explanation's `__str__` rendering shows these naturally

### B6. Tests for Phase B
- Verify all existing tests still pass (same observable behavior, different internal path)
- Verify `Intractable.hints` still populated (backward compat)
- Verify caveats still appear on `Proven`/`Counterexample`
- Add tests that the explanation entries now carry the information previously in hints/diagnostics/blockers

## Critical files

- `src/pyrung/core/analysis/prove/results.py` — types
- `src/pyrung/core/analysis/prove/passes.py` — builder, instrumentation, threading
- `src/pyrung/core/analysis/prove/__init__.py` — `_ExploreContext`, `_build_explore_context`, `prove()`, exports
- `src/pyrung/core/analysis/prove/bfs.py` — attach to results
- `src/pyrung/core/analysis/prove/absorb.py` — add `rejected` to `_RedundantAccAbsorptions`, capture rejection reasons
- `src/pyrung/core/analysis/prove/elision/__init__.py` — return elided dict from `_elide_scan_local_stateful_dims`
- `src/pyrung/core/analysis/__init__.py` — public exports
- `tests/core/analysis/test_prove_passes.py` — tests

## Test coverage mapping (Phase A)

| Plan item | Test(s) |
|---|---|
| 1. Types (`Decision`, `TagEntry`, `Explanation`) | `test_explain_getitem_iter_contains_len`, `test_explain_str_readable` |
| 2. Builder (`_ExplanationBuilder`) | `test_explain_false_returns_none`, `test_explain_classifications` |
| 3. Thread through pipeline (`explain=` param) | `test_explain_false_no_overhead`, `test_explain_true_returns_explanation` |
| 4. Save combinational tags | `test_explain_classifications` (asserts combinational outcome) |
| 5. Return elision method | `test_explain_elision` |
| 6a. classify_dimensions instrumentation | `test_explain_classifications`, `test_explain_domain_sources_bool`, `test_explain_domain_sources_choices`, `test_explain_exclusion_readonly` |
| 6b. elide_scan_local_state instrumentation | `test_explain_elision` |
| 6c. collect_done_acc_pairs instrumentation | `test_explain_redundant_absorption` |
| 6d. find_redundant_absorptions instrumentation | `test_explain_redundant_absorption` |
| 6e. find_threshold_absorptions instrumentation | `test_explain_with_threshold_absorption`, `test_explain_threshold_absorption_blocked` |
| 6f. freeze (input partition) | `test_explain_input_partition` |
| 6g. Attach to results (BFS) | `test_explain_counterexample`, `test_explain_max_states_intractable` |
| 7. Public exports | `test_explain_true_returns_explanation` (imports from public API) |
| 8a. Batch results | `test_explain_batch_partition_sharing` |
| 8b. skip_optimizations notes | `test_explain_skip_optimizations`, `test_explain_skip_optimizations_pass_disabled` |
| 8c. Caveats coexistence | `test_explain_caveats_coexist` |
| 8d. Depth truncation note | `test_explain_notes_depth_truncation` |

## Verification

1. `make test` passes at every step (all new fields are `None`-defaulted)
2. `make lint` passes (ruff + ty)
3. `prove(logic, cond)` returns `explanation=None` (no overhead)
4. `prove(logic, cond, explain=True)` returns populated Explanation
5. `--prove-agreement` still works (new field is transparent)
6. `str(result.explanation)` produces readable multi-line output
