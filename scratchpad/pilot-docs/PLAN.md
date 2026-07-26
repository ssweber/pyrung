# PILOT plan

One file. Supersedes `REVIEW.md`, `TRACE_REFACTOR.md`, `INNER_LOOP_NOTE.md`, and the
three `scout-*.md` reports, all deleted.

**House rules for this file**

- Identify work by **module + symbol**. Never by line number — five predecessor
  documents rotted because their line numbers moved out from under them.
- Every item carries: what, why, LOC delta, risk, and the test that gates it.
- Correctness outranks line count. Sections A and B land before anything in C-H.
- Delete items as they land. Do not archive them here.

Evidence base: five read-only scouts (2026-07-25) over the whole 27k-LOC package,
re-verifying the 2026-07-23 reports against the current tree. Where a scout
corrected an earlier claim, the correction is what is written below.

---

## Status

**Landed**

- `c9d6f5ce` — coast an owned crossing instead of its next requirement.
- `e1f64fa7` — `Condition.__repr__` delegates to `render_condition`; corrective-hold
  scope no longer renders duplicate disjuncts.
- `0e323620` — route-machinery removal (R1 Steps 1-3). `inferred_route_commitment`,
  `skipped_route_ids`, `skipped_root_routes`, `active_root_route`, `RouteExhausted`,
  and `RouteUnproductive` have **zero references anywhere** — src, tests, devtools,
  and goldens all confirmed clean.

- `32faa4f2` — admission unification. `_WaitPrescription` lost its `details` field,
  `_TraceAdmission` + `_admit_trace_details` give every reading one admission pass,
  `_read_wait` separates reading from admission, and `structural_nogoods` plus
  `Gauge.writer_path_erases_banked_work` are deleted. `pilot/CLAUDE.md` updated in
  the same commit.

One open review note on `32faa4f2`:

- `_wait_is_viable` demoting a prescribed wait whose inputs were not admitted is new
  policy, not a mechanical refactor. It is more honest than the unconditional accept
  it replaced, and it is what moved `how_completed_avoid_complete_progress_skeleton.json`.

## Open gates

- **Closed 2026-07-25:** `make test-tumbler` green against `32faa4f2`, goldens
  included. The predecessor note's unverified item is resolved — the regenerated
  goldens replay.
- **What that run does not establish.** A1 is a case the gate *cannot observe* — a
  passing avoid golden proves the endpoint was clean, which is all `wait_snaps`
  ever showed it.
  Treat "tumbler clean" as confidence in refactor mechanics, not as validation of
  the correctness section.
- Reproduction loop for deep-state work: do **not** drive `how()` from cold.
  `tests/tumbler/bench.py::Bench.force_done(acc_tag, preset)` parks a PLC at
  `Internal__Step == 102` in ~15s instead of 360k scans. `test_constructive_route.py`
  is the hand-driven ground truth for what PILOT should do after Step 102.
- Diagnosis tools: `devtools/pilot_divergence.py` finds the first changed golden
  decision without running the suite. `devtools/watch_pilot_decisions.py --stop-action
  TAG=VALUE` halts the moment an action enters candidate construction.

---

## A. Correctness

These are the reason this plan is not primarily a LOC exercise. Three of the four
are places where a stated soundness property is not actually enforced.

### A1. `avoid=` is blind during coasts, and worse under folding

`verify_gates` builds its entire avoid evidence base from `trial.wait_snaps`. It
never reads `trial.coast_receipt` or `trial.timeline`. What lands in `wait_snaps`
differs per path:

| Path | `wait_snaps` | Coverage |
| --- | --- | --- |
| `steer._apply_actions` (pulse) | `_settle_cone` per-scan trajectory | real |
| `steer._letrun_zoom`, channel-less | `_settle_cone` trajectory | real |
| `steer._letrun_zoom`, channel register | one final snapshot | **endpoint only** |
| `steer._try_terminal_letrun` | one final snapshot | **endpoint only** |
| `steer._try_terminal_dwell` | one final snapshot | **endpoint only** |

The load-bearing line is the last statement of `_letrun_zoom`: it returns a
single-element list. The unobserved span is `_ZOOM_BUDGET` (10,000 scans) and
`_letrun_zoom` raises it further from `estimate_scans`, so it has no fixed bound.

Folding makes it worse than unobserved: `coast.py` and `cyclefold.py` contain **zero
occurrences of "avoid"**. Folded scans are never executed, so no snapshot exists for
them at all, and `_fold_metadata` computes fold-protected tags only from armed `Bump`
reads — `avoid_pred` contributes nothing, so a fold may legally jump the range where
the avoided condition was true.

**Fix.** Arm a terminal one-shot avoid `Bump` in `coast.py` from `steer._try_zoom` /
`_try_terminal_letrun` / `_try_terminal_dwell`. `Bump` already takes `predicate` +
`condition` + `watched`, so passing the compiled avoid condition makes its reads
fold-protected automatically — one mechanism fixes both the sampling gap and the
fold blindness. Surface the firing on `CoastReceipt`; have `verify_gates` read it in
the same `if ctx.avoid_pred is not None:` block. `ctx` is already in scope at all
three call sites, so this is a parameter thread, not a plumbing rewrite.

LOC ~+30. Risk: medium — will move goldens; a currently-accepted coast may start
being rejected. Gate: `test_pilot_avoid_gates.py`, `test_pilot_coast.py`,
`test_pilot_cyclefold.py`, then `tests/tumbler/`.

Landing this also corrects four false claims — see B1.

### A2. `guard_verdict` can reject over an incomplete domain

`tide_tables.guard_verdict` returns `GUARD_DEAD` — a permanent rejection — over
domains resolved by soft fallbacks (`_index_domain` -> `_producible_int_domain` ->
`static_expressions.index_values`), which are plausible, not complete. It does not
check completeness itself; it relies on callers routing through
`trace._writer_guard_verdict` first. That is why `pilot/CLAUDE.md` has to say so in
prose. Any second caller of `guard_verdict` rejects unsoundly.

**Fix.** Give `guard_verdict` `require_complete_domains: bool = True`; return
`GUARD_PUNT` (never `GUARD_DEAD`) when any free tag fails `_is_complete_domain`.
Delete trace's duplicate `_complete_domain` closure and its pre-check.
`trace._writer_guard_verdict` keeps its real job: deriving `_transition_fire_pins`
and memoizing.

LOC −20. Risk: medium — changes which module can produce a rejection. Gate:
`test_pilot_rejection_arm.py` and `test_pilot_sandbox_gate.py` must both be green
before anything else moves.

### A3. Lock why VERIFY's post-trial trace omits the live harness

`orientation._read_target` builds the candidate/frame trace with
`TraceReadConstraints.from_context(ctx, state.work, ...)`, which carries the live
runner harness into the trace's advance index. `verify._gate_dead_end` retraces the
post-trial world with a manually constructed `TraceReadConstraints` that deliberately
leaves `harness=None`.

D5 briefly replaced that manual bundle with the context-derived bundle, causing
VERIFY to inherit the harness. We changed it back while diagnosing the Tumbler
Complete regression. That regression was ultimately the unrelated breadth-first
`program_step._first_advance` bug, so the harness choice itself was never adjudicated;
the pre-D5 omission is simply the behavior we preserved.

The asymmetry has a strong rationale: candidate tracing uses the harness as a
*planning model* to identify the driver of a self-advancing sensor. Verification
instead judges the executed post-trial world. `_gate_dead_end` separately calls
`_has_pending_effects(trial.fork)`, which covers both unsettled Boolean feedback and
an enabled analog harness coupling. If the harness is genuinely still advancing,
that live fact already prevents a dead-end rejection; if it is not, adding the
harness to the static retrace risks turning hypothetical future motion into evidence
that a no-op found a frontier.

**Investigate without changing behavior.** Add a focused harness-linked ramp test
that locks this proposal-versus-proof boundary: the harness may identify the action
and coast during orientation, while VERIFY accepts continued motion only from the
trial receipt or live pending-effects check. Change the omission only if that test
exposes a real rejected-progress counterexample.

LOC: test/documentation only unless a counterexample is found. Risk: low. Gate:
`test_pilot_verify.py`, `test_pilot_trace.py`.

---

## B. Documentation that is false

Independent of any refactor. These are claims the code contradicts.

### B1. The `avoid=` overclaim, in four places

All four assert the property A1 disproves:

- `pilot/CLAUDE.md` — "across every intermediate scan of a trial".
- `verify.py::verify_gates` — the "no two-scan wink" comment.
- `CHANGELOG.md` Unreleased — "the scan gate now vetoes transient exposure too (no
  two-scan wink where X blips true mid-coast and settles false)". **User-facing.**
- `docs/guides/analysis-diagnosis.md`, `### avoid` — same sentence. **User-facing.**

Correct all four when A1 lands. If A1 slips, weaken the two shipped docs now.

---

## D. Duplication collapse and single ownership

Ranked by value. Two items were found independently by two scouts with
non-overlapping scopes — noted, because that is unusually strong evidence.

| # | Item | LOC | Risk | Gate |
| --- | --- | --- | --- | --- |
| D14 | **One `edge_admissible` predicate.** *(Found independently by two scouts.)* The static-edge admission check is written **three** times — `options._compass_route_plan._edge_open`, `navigation_evidence.frontier_status.edge_allowed`, and an inline lambda in `detour.py` — same four checks in the same order, differing only in the nogood source and one extra term each. Home: `navigation_evidence.py`, already the declared owner of "reachability evidence shared with verification and recovery", with `detour.py` a recovery consumer. Callers compose `base(edge) and <extra>`. **The three copies may already have drifted — diff them before merging; one of them will change behavior.** | **−22** | med | `test_pilot_avoid_gates.py`, `test_pilot_nogood.py`, `test_pilot_detour_progress.py` |
| D17 | One `_ordered_unique_pairs` for three ordered-dedup spellings (**note:** the three differ on unhashable and `bool`-vs-`int` values; unifying on the `repr`-keyed form is a small behavior *fix*, verify against goldens). | −15 | low | various |

---

## G. Structural, deferred

Each needs its own gate; none should start before A and B land.

- **G1. `options.py::_build_candidates` phase extraction.** 544 lines, 14 sequential
  phases. Extract *within the module*: `_select_route` (phases 3-6), `_lower_prerequisites`
  (7-8), `_learned_fallback` (9), `_assemble_candidates` (11). LOC ≈ −20 — **the
  payoff is control flow, not line count; say so honestly.** High risk if rushed,
  medium if phased, and only **after** D6. Phases 3-6 share seven mutable locals
  rebound in phase 6; the extracted record must carry them.
- **G3. `UnsupportedConstruct` with caret rendering.** A missing tracer rule currently
  surfaces as the same unresolved/None as a genuinely opaque program, sending PILOT
  probing instead of the user filing an issue. Raise deep, catch at exactly one
  boundary, render in `recording.py`. Test mode propagates; drive mode degrades.
- **G4. Dispatch table for the recursion core.** `_trace_back` and `_trace_expression`
  are if/elif chains over construct kinds. One handler per kind in a dict; the
  unsupported case becomes a missing key raising G3's exception. Do immediately
  after G3 — that is where the raise sites become obvious.
- **G5. `TraceNode` collectors.** Six near-identical recursive collectors key on one
  "interior frontier" predicate whose definition `frontier_pairs`' docstring warns
  "must not drift". One `_interior_frontier` predicate + one `iter_nodes()`
  generator, with per-collector guards kept. −35. **This is the prerequisite for the
  larger TraceNode-split idea, and may make it unnecessary.** `_all_nodes` has nine
  call sites across the package, so this pays off across module boundaries.
- **G6. Repeated selection patterns in `trace.py`:** the avoid->via->pilotable->min-score
  filter written three times (−45; must preserve "retain best rejected branch when no
  pilotable alternative survives"); the writer-fallback stash/reset bookkeeping
  written three times in `_trace_back` (−30).
- **G7. `investigate.py`:** byte-duplicated replacement-fingerprint block in the raw
  and guarded replay arms -> `_advance_or_reject(...)` (−40, hottest correctness
  path); extract the 140-line channel-ejection arm from `progress._monitor_trend`
  (+4, but the single largest readability win in that file).
- **G8. Role-typed keys.** `NewType` wrappers for `ActionSourceKey` / `RollbackKey` /
  `SearchScan`; observation constructors take the owning object, not a loose key.
  The regression-nogood mis-scope was a class, not an instance. Mechanical once the
  files are smaller — do last.
---

## Explicitly not worth doing

Recorded so they are not re-proposed.

- **Splitting any file.** `orientation.py` is cohesive end to end with a matching
  ownership table. `compass.py` becomes cohesive the moment D1 lands. `pilot.py` is
  an orchestrator whose islands total ~200 LOC nobody else imports. `options.py`
  only *looks* splittable — its phases share one world read, one `key_nogoods`, one
  `detail_by_pair`. `progress.py`'s clusters B/C/E form a genuine call cycle; only
  the correction-receipt lifecycle separates cleanly, and that buys clarity, not
  lines. `charts.py`'s opaque-detection island is a move, not a shrink.
- **Splitting `_ops.py` into its six disjoint clusters.** It would read better, but
  saves zero lines, churns ~30 imports across ten modules, and every moved symbol is
  underscore-prefixed and would need renaming to be honest. Worst cost/benefit found.
- **Extracting the `compute_*` scanner island from `trace.py`** into a `static_facts.py`.
  A move, not a shrink; single-caller from `pilot._context_for`. (This supersedes the
  old R4a proposal.)
- **Converting `steer.execute`'s or `CompassKnowledge.apply`'s isinstance ladders to
  dispatch tables.** In both cases the per-arm bodies *are* the policy, and each
  `apply` arm folds a different persistent field with six running locals — a table
  makes it worse.
- **Restructuring `assess_outcome`'s nine terminal constructions.** An honest truth
  table; a table-driven rewrite would be shorter and much harder to audit.
- **Merging `currents`' live transition reading into the chart edge model.** ~135 LOC
  of duplicated *subject matter*, but the two use different evidence — live guard
  evaluation vs snapshot-free structure — and legitimately disagree. A merge is a
  behavior change dressed as a refactor. **Document why both exist instead.**
- **Merging `navigation_evidence._learned_reachable`'s BFS with
  `StaticTransitionGraph.find_path`.** The unifying abstraction costs more than
  either implementation.
- **Unifying the two "program constant" definitions** (`currents.is_program_constant`
  uses `not in steerable`; `trace.compute_reference_constants` uses `not external`).
  They are *intended* to be the same distinction and will diverge for any tag that is
  external but absent from `steerable`. They run at different phases with different
  inputs available. **Document now; fix on evidence of a bug.**
- **Deleting `outcome.classify_outcome`** (three lines, and the ergonomic entry seven
  tests use) or **`steer._settle_cone`** (22 lines, but it has a dedicated parity test
  and four call sites).
- **Hunting duplicated judgment between `detour.py` and `outcome.py`.** Checked
  specifically: `outcome` judges the trial, `detour` judges the settled landing. They
  share `Gauge` and `_values_match` and nothing else. The premise does not hold.
- **Moving `options._oscillating_rungs` / `_managed_boolean_rungs` to `_ops.py`.**
  Both docstrings already draw the "option lowering, not a correction hypothesis"
  line, and both need `ctx.target_*` and `state.rungs`. The boundary is stated and
  holding.

---

## Resolved conflicts

Predecessor documents disagreed; these are settled, do not re-litigate.

- **No `static_facts.py`.** The `compute_*` island stays in `trace.py`.
- **`_interior_frontier` + `iter_nodes()` (G5) comes first; the TraceNode split by
  kind may then be unnecessary.** They are the same lever at very different prices.
- **`structural_nogoods` was not "inner-loop transport."** It fired from the outer
  trace, so by the predecessor note's own criterion only the inner call site should
  have gone. Its removal is defensible on a different and better ground — a static
  pre-trial rejection contradicts "reject only with proof", and `verify.py` has the
  empirical gate.
- **`edge_admissible` home: `navigation_evidence.py`**, not `charts.py`. Two scouts
  proposed different homes; `navigation_evidence` is already the declared owner of
  evidence shared with verification and recovery, and `detour.py` is a recovery
  consumer. (D14.)

---

## Rough totals

| Section | LOC |
| --- | --- |
| A — correctness | ~+30 src, +40 test |
| D — remaining behavior-sensitive unification | ~−37 |
| G — structural, deferred | ~−170 |

The mechanical D/E/F pass is landed and removed from this file. The remaining D
work is small but behavior-sensitive; G's payoff is primarily control flow and
ownership rather than a large net line reduction.

Sequencing: **A -> B -> D (by rank) -> G.** Land the remaining
medium-risk semantic D items behind a green tumbler run.
