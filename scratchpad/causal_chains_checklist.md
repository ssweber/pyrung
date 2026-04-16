# Causal Chains Implementation Checklist (v1.3)

## Section A: Prerequisite Infrastructure
- [x] **A1.** Add `rung_firings` to scan snapshots — `PMap[int, PMap[Tag, value]]` tracking which rungs fired and what they wrote, per scan
- [x] **A2.** Wire `rung_firings` into the engine's execute-logic phase so it's populated during simulation
- [x] **A3.** Tests: verify `rung_firings` is correct for simple programs, empty for non-firing rungs, sparse across scans

## Section B: SP Tree Exposure
- [x] **B1.** Hand-written path: wrap `And`/`Or`/`~` expression AST behind a uniform SP-tree interface on rung objects
- [x] **B2.** Equivalence corpus: paired Click→pyrung examples asserting identical SP tree shape, leaf sets, and attribution results
- [x] **B3.** Four-rule attribution walk: implement the post-order SERIES/PARALLEL TRUE/FALSE walk on SP trees

## Section C: Recorded `cause()`
- [x] **C1.** `CausalChain`, `Transition`, `ChainStep`, `EnablingCondition` data model; `BlockingCondition`, `BlockerReason` for unreachable; `blockers` field on `CausalChain`; mode literal `'recorded'` / `'projected'` / `'unreachable'`
- [x] **C2.** Recorded backward walk algorithm (find transition → firing log → SP attribution → proximate/enabling split → recurse)
- [x] **C3.** `plc.cause(tag)` and `plc.cause(tag, scan=N)` — public API on `PLC`
- [x] **C4.** `CausalChain.to_dict()` and `CausalChain.to_config()` serialization
- [x] **C5.** `CausalChain.tags()` and `CausalChain.rungs()` accessors
- [x] **C6.** Confidence scoring (conjunctive vs ambiguous roots, scalar formula)
- [x] **C7.** Tests: worked example from spec (Sensor_Pressure → Sts_FaultTripped chain), edge cases

## Section D: DAP Integration (cause)
- [ ] **D1.** DAP query handler for `cause:tag` and `cause:tag@scan`
- [ ] **D2.** Graph view highlighting — `causal-path` CSS class, sequenced numbering, proximate vs enabling visual weight
- [ ] **D3.** Sidebar timeline panel — chain as a story, click-to-jump via fork machinery

## Section E: Recorded `effect()`
- [x] **E1.** Counterfactual SP evaluation (flip leaf, re-evaluate tree, compare)
- [x] **E2.** Forward walk algorithm with steady-state stopping rule (K=3 consecutive scans, max=1000)
- [x] **E3.** `plc.effect(tag, scan=N)` — public API on `PLC`
- [ ] **E4.** DAP query handler for `effect:tag@scan`
- [x] **E5.** Tests: worked example (Sensor_Pressure forward chain), latch stopping behavior

## Section F: Projected Mode
- [x] **F1.** Projected backward walk for `cause(tag, to=value)` — walk static PDG, ground in observed input behavior, return `mode='projected'` or `mode='unreachable'` with `blockers`
- [x] **F2.** Projected forward walk for `effect(tag, from_=value)` — what-if analysis without mutating state, dead-end vs unreachable trigger distinction
- [x] **F3.** `CausalChain.mode` field (`'recorded'` / `'projected'` / `'unreachable'`), `CausalChain.__str__`, hypothetical scan references
- [ ] **F4.** DAP query handlers: `cause:tag:value`, `effect:tag:value`
- [x] **F5.** Tests: worked example (Sts_FaultTripped projected clear path), stranded tag returns `mode='unreachable'`

## Section G: `recovers()` and `program.query` Namespace
- [x] **G1.** `program.recovers(tag)` — convenience predicate: `cause(tag, to=resting).mode != 'unreachable'`
- [x] **G2.** `program.query.cold_rungs()` and `program.query.hot_rungs()`
- [x] **G3.** `program.query.stranded_bits()` — returns `list[CausalChain]` (unreachable chains, one per stranded bit)
- [ ] **G4.** `program.query.coverage_gaps(tag)` / `program.query.unexercised_paths(tag)`
- [ ] **G5.** `program.query.full()` — comprehensive structured report
- [ ] **G6.** DAP query handler for `recovers:tag`
- [x] **G7.** Tests: survey methods against known programs with expected cold/hot/stranded results

## Section H: Coverage Merge & Pytest Plugin
- [x] **H1.** `CoverageReport` dataclass with `merge()` (intersection for negative, union for positive); stranded bits merge by chain identity (effect tag + blocker fingerprint)
- [x] **H2.** `program.query.report()` — emit per-test `CoverageReport`
- [x] **H3.** Pytest plugin: fixture collects reports, `pytest_sessionfinish` merges and emits `pyrung_coverage.json`
- [x] **H4.** Whitelist file format (TOML), CI-failure gating on whitelist diff
- [x] **H5.** Tests: multi-test merge produces correct residuals, monotonic shrinkage property

## Section I: UI Polish
- [ ] **I1.** Conjunctive vs ambiguous root rendering in UI (joint causation vs "N candidate causes, expand to compare")
- [ ] **I2.** Projected and unreachable chain rendering in graph view — dashed lines for projected, distinct red for unreachable, "if X transitions" labels
