# Scout report: trace.py / investigate.py decomposition map (2026-07-23, Opus, read-only)

Brief: cohesion maps of the two largest pilot files, compression opportunities,
split-vs-shrink recommendations, and rule-to-structure conversion notes.

Read first: `pilot/CLAUDE.md` ownership table (lines 95–129) and invariant list (209–281). All line numbers below are current as of the read.

---

## FILE 1 — trace.py (3,985 LOC)

Docstring scope (lines 1–11): "resolves a target through writers, guards, copies, calculations, and accumulating instructions… enumerates trace routes, determines steerable inputs, and ranks writers." Matches CLAUDE.md's four-responsibility claim, but the file has grown three additional clusters the doc does not name.

### Table of contents (line range → cluster)

| Lines | Cluster | Owner per CLAUDE.md |
|---|---|---|
| 70–188 | Env/config: `DomainPrior`, `_TraceEnv` (WalkContext seam), `_env_for` | trace (core) |
| 190–309 | Route value types: `TraceChoice`, `TraceAction`, `_RouteDraft`, `_expr_route_key` | trace (core) |
| 310–639 | **`TraceNode` + 6 recursive collectors** (`leaves`, `_collect_chains`, `_collect_ordered`, `_collect_pivots`, `_collect_unsatisfied`, `_collect_dead_end_parents`) | trace (core) |
| 641–701 | `frontier_pairs` (Notion #1 of "still needed") | trace (core) |
| 702–1152 | **Inequality/relational lever cluster** — `_resolve_inequality_target`, `_heuristic_inequality_target`, `_strict_inequality_step`, `_domain_granularity`, `_declared_float_bounds`, `_atom_text`, `_rewrite_internal_compare`, `_inequality_levers`, `_Lever` | **static_expressions.py** (static-expr work) |
| 1154–1531 | Trace demand orchestration: `_trace_demand`, `_advance_frontier`, `trace_relational`, `_reconcile_relational`, `_owner_call_gate_nodes` | trace (core) |
| 1538–1752 | Route scoring/conflict: `_route_pilotable`, `_route_actions_rejected`, `_route_forces`, `_route_conflicts` | trace (core) |
| 1756–1986 | **`_trace_expression`** (And/Or/Atom walk) | trace (core) |
| 1989–2497 | **`trace_back` / `_trace_back`** (writer-selection recursion) | trace (core) |
| 2499–2657 | `_preserve_children`, `_arm_fully_steerable`, `_or_ambiguity_over_inputs` | trace (core) |
| 2658–3072 | Route enumeration + ranking: `enumerate_trace_choices`, `rank_trace_choices`, `_writer_route_drafts`, `_enumerate_expr_routes`, labels/hints | trace (core) |
| 3074–3252 | **Structural one-time scanners**: `compute_reference_constants`, `compute_edge_tags`, `compute_resting_values` | islands (drive-layer setup) |
| 3259–3410 | Preimage/decompose: `_decompose_sum`, `_atom_comparison`, `_condition_required_values`, `_flag_gate_comparisons`, `_transition_fire_pins` | mixed |
| 3412–3476 | `_writer_guard_verdict` (domain-completeness gate) | trace — **correctly owned** (CLAUDE.md line 69) |
| 3479–3646 | **Table logic**: `_table_enablement_prereqs`, `_invert_indirect` | **tide_tables.py** (table logic) |
| 3648–3733 | Small predicates: `_visit_key`, `_can_produce`, `_concrete_written_value`, `_writer_clobbers_codemand`, `_is_self_gated` | trace (core) |
| 3735–3985 | Writer ranking: `_WriterRank`, `_rank_writers`, `_scan_transient_rest` | trace — `_rank_writers` **correctly owned** (line 103); `_scan_transient_rest` is a scanner island |

### Cohesion assessment

**Tight core (well-cohered, leave alone):** lines 1154–3072 form one connected component. `_trace_back` → `_trace_expression` → `_rank_writers`/`_preserve_children`/`_table_enablement_prereqs`/`_decompose_sum`, all threading one `_TraceEnv`. `enumerate_trace_choices` reuses `_rank_writers` and the same lock mechanism rather than re-walking. This is the module's reason to exist and is genuinely single-owner.

**Islands (share nothing with the recursion):**
- `compute_reference_constants` / `compute_edge_tags` / `compute_resting_values` / `_scan_transient_rest` (3074–3252, 3871–3985, ~370 LOC). Pure structural program scans, called once each from `pilot.py::_context_for` (lines 318–321). They take `(pdg, program)`, never a `_TraceEnv`, never a snapshot-driven walk. `steerable.py:242` and `causal.py:47` reach in for `compute_reference_constants` cross-module.

### Findings — trace.py

**T1 — Inequality/relational lever cluster is a static-expression copy trace grew locally.**
`trace.py:702–1152` (~450 LOC). `_resolve_inequality_target`, `_heuristic_inequality_target`, `_atom_text`, `_strict_inequality_step`, `_domain_granularity`, `_declared_float_bounds`, `_rewrite_internal_compare`. These resolve "inequality atom → nearest satisfying value" over domains/snapshot — pure static-expression arithmetic. **Cross-module proof of misplacement:** `investigate.py:2248–2251` imports `_atom_text`, `_heuristic_inequality_target`, `_resolve_inequality_target` from trace for `_analog_boundary_hold`; `recording.py:34` imports `_atom_text`. Two non-trace modules reach past the trace surface into these `_`-helpers. Per ownership table ("static expression work to static_expressions.py"), this belongs in `static_expressions.py` (currently only 70 LOC).
- Proposed action: extract the resolver sub-cluster (`_resolve_inequality_target`, `_heuristic_inequality_target`, `_strict_inequality_step`, `_domain_granularity`, `_declared_float_bounds`, `_atom_text`) to `static_expressions.py`; keep `_inequality_levers` (which builds `TraceNode` children) and `_rewrite_internal_compare` in trace as thin consumers. This is (b) extract-to-clarify-ownership: it removes the "trace owns a copy of static-expr math that two modules import" smell.
- LOC delta: ≈ −300 from trace.py, +260 to static_expressions.py (net −40 after dedup of duplicated import blocks). Not a pure delete, but it converts three cross-module `from ...trace import _private` reaches into one honest owner.
- Risk: Low-medium. Pure functions, no `_TraceEnv` dependency. Golden skeletons cover the behavior; the import sites are explicit and few.

**T2 — Repeated "select best alternative" pattern (avoid→via→pilotable→min-by-score).** This 4-step filter appears three times with small variations:
- `_trace_expression` Or-arm selection: `trace.py:1829–1886`
- `_table_enablement_prereqs` arm selection: `trace.py:3568–3588`
- `_trace_back` subroutine caller selection: `trace.py:2279–2297`

Each builds candidate `list[TraceNode]`, drops arms where `_route_forces(..., avoid_pred)`, prefers arms where `_route_forces(..., via_pred)`, filters to `_route_pilotable`, then `min(..., key=_trace_score)`.
- Proposed action: a single `_select_alternative(candidates, env, *, score)` helper embodying the avoid/via/pilotable/score precedence. The Or-arm case additionally tracks `rejected` (exact-leaf rejection) for the fallback branch — pass that as an optional post-filter.
- LOC delta: ≈ −45.
- Risk: Medium. The Or-arm variant has the subtle "retain best rejected branch when no pilotable alternative survives" rule (1876–1886) that CLAUDE.md line 184 pins ("Trace uses those exact leaf rejections only to order unlocked alternatives"). Consolidation must preserve that; the golden `test_pilot_trace` + `test_pilot_rejection_arm` gates cover it.
- **Rule-to-structure bonus:** collapsing to one helper is exactly how CLAUDE.md's prose invariant "exact leaf rejections only order unlocked alternatives" becomes structurally enforced — there'd be one code path that can express the ordering, so a future edit can't accidentally let a rejection *remove* an arm.

**T3 — Writer-fallback bookkeeping repeated 3× in `_trace_back`.** `trace.py:2416–2433` (avoid_shadowed), `2439–2448` (unpilotable_alternative), `2455–2472` (empirically_rejected). Each does the identical sequence: build/keep a 5-tuple fallback (`children, writer_rung, availability, live_guard, visited`), then `node.children.clear(); node.writer_rung=None; node.writer_availability=AVAILABLE_NOW; node.live_guard=False; writer_skips.append(...)` and roll back `_visited`. The 5-tuple type is even spelled twice at 2157–2162.
- Proposed action: a local `_stash_and_reset(node, reason)` closure returning the fallback tuple and doing the reset+rollback; a `_FallbackAttempt` frozen dataclass for the tuple.
- LOC delta: ≈ −30.
- Risk: Low. Mechanical; the three call sites differ only in stash-condition and skip slug.

**T4 — Six near-identical `TraceNode` recursive collectors over one predicate.** `_collect_pivots` (555), `_collect_unsatisfied` (581), `_collect_dead_end_parents` (619), and `frontier_pairs`'s `_all_nodes` loop (659) all key on the same "interior frontier node" predicate: `not satisfied and not is_steerable and not pipeline_internal and children`. `frontier_pairs` docstring (648–649) explicitly warns this definition "must not drift" from `hold_defeats_needed`'s consumption.
- Proposed action: one `def _interior_frontier(n) -> bool` predicate + one `iter_nodes()` generator; rewrite the collectors as filters over it.
- LOC delta: ≈ −35.
- Risk: Low-medium. `_collect_unsatisfied` has an extra relational-node early-return (582–592) and `_collect_pivots` adds `not relational`; keep those as per-collector guards.
- **Rule-to-structure:** this is the strongest doc-shrink lever in trace. CLAUDE.md spends lines 213–216 distinguishing three "still needed" notions and points a whole test (`test_pilot_needed_vocabulary.py`) at keeping them from drifting. Making the interior-frontier predicate a single named function means the "#1 whole-tree residual" notion has exactly one definition — the prose rule can shrink to "see `_interior_frontier`."

**T5 — `_invert_indirect` is a table-logic copy that belongs in tide_tables.py.** `trace.py:3594–3645`. Its own docstring (3611–3614) says it's "the single-table / identity-predicate slice… generalized by `tide_tables.solve_table_predicate`," and `tide_tables.py:477` comments "mirrors trace._invert_indirect." Both already share `table_from_indirect_src` and `_read_table`; the duplicated part is the CopyInstruction-finding loop (trace 3621–3631 ≡ tide_tables `_model_table_operand` 516–524).
- Proposed action: move `_invert_indirect` into `tide_tables.py` beside `_model_table_operand`, factoring the shared "find indirect-copy src writing `tag`" loop into one helper there; trace calls it. Aligns with "table logic to tide_tables.py."
- LOC delta: ≈ −45 from trace, +15 to tide_tables (net −30 via dedup of the src-finding loop).
- Risk: Low. One call site (`_trace_back:2374`).

**T6 — `compute_*` scanner island.** `trace.py:3074–3252` + `_scan_transient_rest:3871–3985` (~370 LOC). Not part of the trace recursion; called once from `pilot.py` context setup. This is a candidate extraction (b) but a *move*, not a *shrink* — lower priority given the LOC-reduction goal. Flagged for completeness; recommend leaving unless a `program_scan.py` is created for other reasons.

### Split-vs-shrink verdict — trace.py: **(a) shrink in place**, plus one targeted extraction (T1).

The recursion core (1154–3072) is cohesive and single-owner; do not split it. The wins are deletion of duplication (T2/T3/T4/T5, ≈ −155 LOC) and the one ownership-clarifying extraction of the static-expr inequality resolvers (T1). Do **not** extract the `compute_*` island for its own sake.

---

## FILE 2 — investigate.py (2,496 LOC)

Docstring scope (1–11): "constructs incident windows and replay functions, derives candidate holds…, ranks those hypotheses, closes the first explanation…, returns the first composite that survives; also provides the shorter excursion investigation. Confirms but does not install." Accurate.

### Table of contents

| Lines | Cluster | Boundary note |
|---|---|---|
| 80–265 | Value types: `ReplayStep`, `CausalOccurrence`, `RegressionWitness`, `ReplacementEvidence`, `ReplayIncident`, `InvestigationHypothesis`, `ReplayOutcome`, `InvestigationResult`, identity helpers | own |
| 267–340 | `_scoped_correction_rungs` | own |
| 341–626 | **Regression-ownership engine**: `incident_regression_witness`, `_regression_cause_replayed`, `_RegressionOwnership`, `_replacement_departure_scan`, `_same_occurrence`, `_same_bounded_channel_outcome`, `_shared_causal_suffix`, `_regression_ownership` | own (neutralization proof) |
| 627–954 | **`build_replay_fn`** (the replay closure `_replay`, ~264 LOC) | own |
| 962–1231 | **`investigate_excursion`** + `_implicated_writers`, `_skiff_suppression_nominations` | own |
| 1232–1358 | Incident construction: `_first_timeline_departure`, `build_deviation_incident`, `_hold_is_noop` | own |
| 1359–1490 | **Hypothesis ranking**: `_rank_hypotheses`, `_generate_deviation_hypotheses`, `_compose_hypotheses` | own |
| 1491–1791 | **`investigate_deviation`** (main loop, ~300 LOC) | own |
| 1792–1902 | **"defeat needed" trio**: `_hold_values`, `hold_defeats_needed`, `_active_rungs_defeat_needed`, `_holds_defeat_needed` | boundary (see I2) |
| 1904–2323 | **Precise-cause walk**: `_precise_causes`, `_precise_cause`, `_ordered_truth`, `_analog_boundary_hold` | boundary with causal.py + corrections.py |
| 2324–2497 | Absence roots + utils: `_absence_root_correctives`, `_last_transition_scan`, `_dedupe_pairs`, `_dedupe_hypotheses` | own |

### Boundary assessment (corrections.py / progress.py / causal.py)

The **derivation vs replay** boundary with `corrections.py` is clean and correctly wired: `_precise_causes` and `_absence_root_correctives` call *into* corrections for hold synthesis (`guard_correction_holds`, `break_guard_holds`, `correct_enablers` at 2075, 2152, 1451) rather than reimplementing it. `investigate_deviation` never installs — it returns a `_ConfirmedCorrection`, matching CLAUDE.md lines 120–122. No derivation logic leaked from corrections into investigate.

The **progress.py** boundary is respected: investigate builds/returns; `progress.py` owns install/revoke. `investigate_deviation` consumes `excluded_corrections` and `installed_rungs` as inputs, does not mutate lifecycle.

### Findings — investigate.py

**I1 — `investigate_deviation` main loop mixes three concerns in one 300-line function.** `investigate.py:1491–1784`. It does (a) pre-filter screening (revoked / already-installed-active / vacuous no-op / self-defeat, lines 1588–1641 + 1687–1711), (b) the nested-replacement closure loop (1644–1773), and (c) result assembly. The raw-replay and guarded-replay arms (1644–1677 vs 1712–1741) contain a **byte-duplicated** replacement-fingerprint-and-extend block (fingerprint tuple → `seen_replacements` cycle check → `_extend_from_replacement` → reject-or-continue).
- Proposed action: extract `_advance_or_reject(current, outcome, seen_replacements)` returning `(next_current | None, rejection | None)`; call it from both replay arms. Optionally pull the four pre-filters into a `_prescreen(hypothesis) -> str | None` returning a rejection slug.
- LOC delta: ≈ −40.
- Risk: Medium. This is the hottest correctness path (`test_pilot_investigate.py`, `test_pilot_detour_progress.py`); the nested-cause budget/cycle semantics (CLAUDE.md 251–255) must be preserved exactly. The two arms are already near-identical, so consolidation reduces the chance they drift.

**I2 — "defeat needed" trio is thinly spread and one member is misplaced.** `hold_defeats_needed` (1802), `_active_rungs_defeat_needed` (1832), `_holds_defeat_needed` (1852). `hold_defeats_needed` is **only** consumed by `options.py:1198,1215` (not by investigate itself), yet lives in investigate; `trace.py:649` names it as the must-not-drift consumer of `frontier_pairs`. It is a static write-vs-need predicate about *option ranking*, not incident replay.
- Proposed action: move `hold_defeats_needed` + `_hold_values` to `options.py` (its sole caller) or to a shared static predicate home; keep `_holds_defeat_needed`/`_active_rungs_defeat_needed` in investigate (they serve the self-defeat gate at 1691). This tightens the CLAUDE.md 213–216 "still needed has separate meanings" boundary — the option-ranking notion physically leaves the replay module.
- LOC delta: ≈ −55 from investigate, +50 to options (net small); the value is ownership clarity, not LOC.
- Risk: Low. Single external caller; behavior covered by `test_pilot_needed_vocabulary.py`.

**I3 — `_precise_causes` hand-rolls causal-chain traversals that overlap causal.py's charter.** `investigate.py:1904–2192` (~290 LOC). Inside it are three bespoke recursive walkers over `chain.steps`: `_origins` (1984–2003), `_mark_trigger_spine` (2013–2034), and the `moved_tags`/`common` spine reconstruction (2047–2070). CLAUDE.md line 345 gives causal.py "recorded cause-chain queries." These trigger-spine/origin walks are cause-chain topology queries that could live beside `chase_cause_roots`/`chase_chain_tags` in causal.py, leaving `_precise_causes` to do only hypothesis *shaping*.
- Proposed action: move `_mark_trigger_spine` and `_origins` into causal.py as `trigger_spine(chain, effect)` and `origin_tags(chain)`; `_precise_causes` consumes them. This is (b) extract-to-clarify-ownership; it shrinks the single largest generator function.
- LOC delta: ≈ −60 from investigate, +55 to causal (net small); clarifies that investigate ranks/replays and causal *walks*.
- Risk: Medium-high. `_precise_causes` is intricate (trigger-spine vs enabler-origin distinction at 2008–2010 is load-bearing). Requires care and full `test_pilot_investigate.py` + `test_pilot_detour_*` gates. Recommend only if the ownership boundary is being firmed up deliberately; otherwise defer.

**I4 — `build_replay_fn._replay` three-shape judgment is a long implicit dispatch.** `investigate.py:822–952`. Sequential `if zoom_channel_tag / if terminal_letrun_role_tags / if departure_bearing / else trace-back-trend` — the four incident shapes (CLAUDE.md 642–657). Each arm assembles a `ReplayOutcome` with overlapping fields. This is the AdvanceProfile-style opportunity: the shape is already discriminated by `ReplayIncident` fields, so a small dispatch keyed on incident kind would replace the if-ladder.
- Proposed action: give `ReplayIncident` a `kind` (channel / letrun-target / command / trend) computed once at construction; dispatch `_judge_<kind>(snap, session, receipts)`. The channel arm (822–906, ~84 lines) is by far the heaviest and would become its own named judge.
- LOC delta: ≈ −20 (mostly clarity, not deletion).
- Risk: Medium. The channel arm's accept/reject reasoning (neutralized/masked/progress-erased) is subtle; keep it intact inside the extracted judge.

**I5 — `_precise_cause` (singular) is a one-line compatibility shim.** `investigate.py:2195–2202` returns `_precise_causes(...)[0]`. Grep shows **no caller** inside `src/pyrung/core`.
- Proposed action: verify against tests, then delete (or confirm a test-only consumer and leave a comment).
- LOC delta: −8.
- Risk: Low (pending test-usage check).

### Split-vs-shrink verdict — investigate.py: **(a) shrink in place.**

The file is already a coherent single owner (construct → derive → rank → replay → confirm). Do **not** split it. Wins are internal deduplication (I1, I4, I5) and two ownership-tightening moves (I2 to options, I3 to causal) that firm the "still needed" and "cause-chain query" boundaries CLAUDE.md already legislates in prose.

---

## Ranked shortlist — 5 highest-value moves across both files

1. **T4 — Unify the `TraceNode` interior-frontier collectors behind one predicate + generator** (trace.py 544–701). Best rule-to-structure payoff: it makes the "must not drift" warning (trace.py:649) and the three-notion "still needed" vocabulary (CLAUDE.md 213–216, guarded by a dedicated test) enforceable by a single definition. ≈ −35 LOC, low-medium risk.

2. **T2 — Collapse the avoid→via→pilotable→score alternative-selection pattern into one helper** (trace.py 1829/2279/3568). Deletes real duplication (≈ −45) *and* structurally pins CLAUDE.md's "exact leaf rejections only order alternatives, never remove them" invariant (line 184) into one code path. Medium risk, well-gated.

3. **I2 — Relocate `hold_defeats_needed` to its sole caller (options.py)** (investigate.py 1792–1830). Low risk, clarifies the option-ranking-vs-replay ownership line and removes a cross-module reach. Modest LOC but high doctrine value.

4. **I1 — Deduplicate the raw/guarded replay arms in `investigate_deviation`** (investigate.py 1644–1741). ≈ −40 LOC on the hottest correctness path; the two arms are already near-identical, so unifying them prevents future drift in the nested-cause lifecycle (CLAUDE.md 251–255). Medium risk, strong gate coverage.

5. **T1 — Extract the inequality-resolution helpers to static_expressions.py** (trace.py 702–1152). Largest ownership correction: three modules (trace, investigate, recording) currently share a static-expr copy that lives in trace. Aligns with the ownership table's "static expression work to static_expressions.py" and turns cross-module `_private` imports into one honest owner. ≈ −40 net after dedup, low-medium risk.

Honorable mention (defer unless firming boundaries deliberately): **T5** (`_invert_indirect` → tide_tables, clean −30) and **I3** (`_precise_causes` walkers → causal.py) both correct real ownership leaks but are moves, not deletes; T5 is the safer of the two.

Note on the LOC-reduction goal: the pure-deletion wins (T2+T3+T4+T5 in trace ≈ −155; I1+I5 in investigate ≈ −48) total roughly **−200 LOC** without moving ownership. The extractions (T1, I2, I3) are net-small on LOC but are where the *doc* shrinks, because each removes a duplicated-decision seam the CLAUDE.md ownership table is currently forced to police in prose.
