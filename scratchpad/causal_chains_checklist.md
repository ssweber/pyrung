# Causal Chains Implementation Checklist

## Section A: Prerequisite Infrastructure
- [x] **A1.** Add `rung_firings` to scan snapshots — `PMap[int, PMap[Tag, value]]` tracking which rungs fired and what they wrote, per scan
- [x] **A2.** Wire `rung_firings` into the engine's execute-logic phase so it's populated during simulation
- [x] **A3.** Tests: verify `rung_firings` is correct for simple programs, empty for non-firing rungs, sparse across scans

## Section B: SP Tree Exposure
- [ ] **B1.** Hand-written path: wrap `And`/`Or`/`~` expression AST behind a uniform SP-tree interface on rung objects
- [ ] **B2.** Equivalence corpus: paired Click→pyrung examples asserting identical SP tree shape, leaf sets, and attribution results
- [ ] **B3.** Four-rule attribution walk: implement the post-order SERIES/PARALLEL TRUE/FALSE walk on SP trees

## Section C: Retrospective `cause()`
- [ ] **C1.** `CausalChain`, `Transition`, `ChainStep`, `EnablingCondition` data model
- [ ] **C2.** Retrospective backward walk algorithm (find transition → firing log → SP attribution → proximate/enabling split → recurse)
- [ ] **C3.** `program.cause(tag)` and `program.cause(tag, scan=N)` — public API on `Program`
- [ ] **C4.** `CausalChain.to_dict()` and `CausalChain.to_config()` serialization
- [ ] **C5.** `CausalChain.tags()` and `CausalChain.rungs()` accessors
- [ ] **C6.** Confidence scoring (conjunctive vs ambiguous roots, scalar formula)
- [ ] **C7.** Tests: worked example from spec (Sensor_Pressure → Sts_FaultTripped chain), edge cases

## Section D: DAP Integration (cause)
- [ ] **D1.** DAP query handler for `cause:tag` and `cause:tag@scan`
- [ ] **D2.** Graph view highlighting — `causal-path` CSS class, sequenced numbering, proximate vs enabling visual weight
- [ ] **D3.** Sidebar timeline panel — chain as a story, click-to-jump via fork machinery

## Section E: Retrospective `effect()`
- [ ] **E1.** Counterfactual SP evaluation (flip leaf, re-evaluate tree, compare)
- [ ] **E2.** Forward walk algorithm with steady-state stopping rule (K=3 consecutive scans, max=1000)
- [ ] **E3.** `program.effect(tag, scan=N)` — public API
- [ ] **E4.** DAP query handler for `effect:tag@scan`
- [ ] **E5.** Tests: worked example (Sensor_Pressure forward chain), latch stopping behavior

## Section F: Prospective Mode
- [ ] **F1.** Prospective backward walk for `cause(tag, to=value)` — walk static PDG, ground in observed input behavior
- [ ] **F2.** Prospective forward walk for `effect(tag, from_=value)` — what-if analysis without mutating state
- [ ] **F3.** `CausalChain.mode` field (`'retrospective'` / `'prospective'`), hypothetical scan references
- [ ] **F4.** DAP query handlers: `cause:tag:value`, `effect:tag:value`
- [ ] **F5.** Tests: worked example (Sts_FaultTripped prospective clear path), stranded tag returns `None`

## Section G: `recovers()` and `program.query` Namespace
- [ ] **G1.** `program.recovers(tag)` — convenience predicate over prospective `cause()`
- [ ] **G2.** `program.query.cold_rungs()` and `program.query.hot_rungs()`
- [ ] **G3.** `program.query.stranded_bits()`
- [ ] **G4.** `program.query.coverage_gaps(tag)` / `program.query.unexercised_paths(tag)`
- [ ] **G5.** `program.query.full()` — comprehensive structured report
- [ ] **G6.** DAP query handler for `recovers:tag`
- [ ] **G7.** Tests: survey methods against known programs with expected cold/hot/stranded results

## Section H: Coverage Merge & Pytest Plugin
- [ ] **H1.** `CoverageReport` dataclass with `merge()` (intersection for negative, union for positive)
- [ ] **H2.** `program.query.report()` — emit per-test `CoverageReport`
- [ ] **H3.** Pytest plugin: fixture collects reports, `pytest_sessionfinish` merges and emits `pyrung_coverage.json`
- [ ] **H4.** Whitelist file format (TOML), CI-failure gating on whitelist diff
- [ ] **H5.** Tests: multi-test merge produces correct residuals, monotonic shrinkage property

## Section I: UI Polish
- [ ] **I1.** Conjunctive vs ambiguous root rendering in UI (joint causation vs "N candidate causes, expand to compare")
- [ ] **I2.** Prospective chain rendering in graph view — dashed lines, "if X transitions" labels, distinct visual treatment
