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
  the same commit. **Sections A5 and A6 are obligations this commit created and are
  still outstanding — A5 verified unfixed against the committed `steer.py`.**

Two open review notes on `32faa4f2`:

- `_program_input_details` attaches a handoff boundary to each input independently,
  while `_prescribe_wait` still requires one uniform heading for the prescription.
  Those two disagree about what "handed off" means. E3 settles it.
- `_wait_is_viable` demoting a prescribed wait whose inputs were not admitted is new
  policy, not a mechanical refactor. It is more honest than the unconditional accept
  it replaced, and it is what moved `how_completed_avoid_complete_progress_skeleton.json`.

## Open gates

- **Closed 2026-07-25:** `make test-tumbler` green against `32faa4f2`, goldens
  included. The predecessor note's unverified item is resolved — the regenerated
  goldens replay.
- **What that run does not establish.** A green tumbler is structurally incapable of
  catching A1, A3, or A5, and this is why they survived:
  - **No golden contains a `banked-work` event at all**, so nothing in the suite
    exercises the veto A5 weakened. A6 exists to fix exactly this.
  - A1 is a case the gate *cannot observe* — a passing avoid golden proves the
    endpoint was clean, which is all `wait_snaps` ever showed it.
  - A3 makes the dead-end gate more accepting; goldens pin current behavior, so they
    agree with the defect.
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

### A3. `_gate_dead_end` re-derives channel reach from the snapshot

`verify_gates` computes the owned `bearing_stop_reason` via
`_owned_bearing_stop_reason`, then calls `_gate_dead_end` **without passing it**.
`_gate_dead_end` re-derives `channel_reached` by snapshot equality.
`_owned_bearing_stop_reason`'s own docstring names the failing case: relational inner
boundaries keep a `reached` receipt when their scalar heading was *crossed* rather
than equalled. So a relational coast that crossed its boundary has
`stop_reason == "reached"` but `channel_reached == False`, and can be rejected as
dead-end or lateral.

**Fix.** Pass `bearing_stop_reason` down; take the **union**, not the replacement —
the receipt is absent for settle-path trials.

LOC −4. Risk: medium (makes the gate more accepting for relational coasts; goldens
move). Gate: `test_pilot_verify.py`, `test_pilot_relational.py`,
`test_pilot_moving_boundary.py`, then tumbler.

Related, same fix family: `progress._bearing_satisfied`'s `assessment is None`
snapshot fallback is **not** defensive — `verify_gates`' target-reached returns build
a trial without an assessment, so it is the only path for target-reached coast
trials. Same shape in `_monitor_trend::channel_ejection`. Fix by having the two
target-acceptance paths attach a `TrialAssessment` (they know everything needed),
then delete both fallback arms. LOC −8 `progress.py` / +6 `verify.py`. Risk: medium.

### A4. Seven sites decide "did the coast reach its bearing"

The 2026-07-23 report said two. Current count of the loose scalar set
(`zoom_channel_tag | zoom_target_value | zoom_stop_reason | zoom_progressed |
bearing_stop_reason | channel_target`): **162 occurrences across 13 src and 8 test
files**. A1, A3, and the `progress` fallback above are three of the seven; the
`ChannelMotion` receipt in section E is the structural cure.

### A5. The banked-work veto lost its durability guarantee — outstanding since `32faa4f2`

Verified against the committed `steer.execute`: the `Pulse` arm sets
`nogood_pair=act.action`, so a rejected destructive pulse becomes a world-keyed
nogood. **The `BatchPulse` arm sets `regression_nogoods` and no `nogood_pair`** —
and the banked-work path does not consult `regression_nogoods`. A destructive
`BatchPulse` is now executed on a fork, rejected, learns nothing, and is eligible
again at the same world key. Nothing structurally prevents the repeat. That is the
"bound loops and name failures" guarantee, lost with the static predicate.

**Fix.** Give the `BatchPulse` intent a `nogood_pair`, or have the banked-work gate
fall back to `intent.regression_nogoods`. One line.

Two undocumented consequences of the same deletion, to record rather than fix:

- **The destructive fork still teaches the compass.** `_try_action_batch` computes
  `_compass_observations(..., contradict_no_change=True)` *before* `verify_gates` and
  returns them even when `result.trial is None`; `_record_attempt` applies
  observations for rejected attempts by design. A reset writer's edges now enter the
  transition graph, where previously those actions never became candidates and were
  never observed. Not wrong — they are true facts — but new.
- **Cost:** ~18 forked scans per destructive candidate, worst case ~2,018 when a
  harness has pending delayed effects. Bounded, but real work where there was none.

### A6. The banked-work gate has no end-to-end test

It is now the *only* protection, and it is the least-tested thing in the path. The
sole test is a pure unit test: the "pulse" is a `SimpleNamespace` with four dict
fields, no PLC, no fork, hand-built gauge. It proves the arithmetic and nothing about
the gate's position in a live drive. No drive-level test asserts a `banked-work`
event; no tumbler golden contains one; the deleted
`Gauge.writer_path_erases_banked_work` assertions were not replaced.

**Add** one drive-level test: a program with a proved reset writer, PILOT offered a
reset lever and a productive lever, driven through `pilot_events(...)`, asserting
(a) a `banked-work` gate event appears, (b) the reset action becomes a world-keyed
nogood, (c) the run still reaches the target. Land it with A5.

### A7. Transcript defects (low severity, small)

- `progress._channel_transitions` builds its channel list solely from
  `ctx.target_tag`, so a zoom's navigated channel is missing — this is why the
  Boolean Tumbler golden has empty `channel_transitions` for real `Sts_StateCurrent`
  ejections. `trial.zoom_channel_tag` is already in scope in the same function.
  Recording-only; nothing consumes it for a decision.
- `pilot.py`'s `watch_tags` is seeded once (`if not state.watch_tags:`) and frozen
  for the run. Blast radius is wider than previously recorded: it feeds
  `steer._pen_tags` (the pen universe for **every** coast) and
  `progress._deviation_bearing`, so a pivot discovered by a later route gets no pen
  and yields an empty deviation bearing. One-line union-extend, but it grows the pen
  set monotonically — land with a cyclefold-performance check.

**Struck:** the former claim that plan recording reconstructs control without
evaluating the guard is **false against the current tree**. `recording._controlled_at`
consumes `_ops._rung_execution_receipt(...).owner(tag)`, which evaluates every
expanded branch's guard against the act's frozen snapshot and honors revocations.

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

### B2. Four `src/` docstrings describe deleted route machinery

- `pilot.py::_prepare_route` — "An inferred default becomes one revocable session
  commitment instead; only exact exhaustion releases it." Contradicted by its own
  body eight lines later, which returns `None, frozenset(), route_taken` whenever
  `via_pred is None`.
- `pilot.py::_report_selected_route` — "If that inferred commitment is later
  exhausted and replaced…". The function is fine; the justification is stale.
- `types.py::_PilotContext.route` comment — "Inferred commitment lifecycle lives in
  `_PilotState`". It does not.
- `trace.py::trace_choice_identity` — "identity for one root route commitment".
  (This symbol is also dead — see C.)

### B3. Rung 4 does not use an unchanged fork

`pilot/CLAUDE.md` rung 4 and `program_step.py`'s module docstring both say the proof
happens "in an unchanged fork". But `_input_reaches_exact_producer` and
`_input_handoffs` each `fork.patch({action.tag: action.value})` and step — one
counterfactual probe per required input. The *contract* still holds (nothing there
returns an action), but the description is false. Say "an otherwise-unchanged fork,
plus one counterfactual input patch per required input."

### B4. The escalation ladder has four rungs, not five

Rung 3 (`AdvanceProfile`) is not a stage. The class lives in
`core/instruction/advance.py`, outside the pilot package; inside pilot the only
rung-3 code is `advance.demand_holds` plus scattered direct reads of
`owner.profile.linear`. The doc itself already assigns pilot's `advance.py` to rung 1.

Restate as: **trace/advance -> static readers -> exact-producer proof -> skiff**,
with `AdvanceProfile` described as the contract every rung consumes. Also state that
the rung-4/rung-5 line is drawn by **who may return an action**, not who may probe.

---

## C. Provable deletes

No golden gate needed — caller enumeration proves these cannot change a decision.
Totals corrected from the 2026-07-23 report, which under-counted source and
over-counted tests by ~4x.

**Source: ~215-230 LOC.**

| Module | Symbols | LOC |
| --- | --- | --- |
| `evidence.py` | `expand_pipeline_need`, `roles_for_needed_tag`, `PipelineNeedExpansion`, `_route_satisfies_need` (contiguous block; production asks the same question through `charts.StaticTransitionGraph.target_values_for_need`) | 73 |
| `advance.py` | `next_advance`, `estimate_scans`, `measure_scans` + orphaned imports, `_DEFAULT_DT`, `_MEASURE_BUDGET`, the whole `TYPE_CHECKING` block | ~60 |
| `corrections.py` | `_resolve_steerable_driver`, `_resolve_partial` (self-described shims, zero callers) | 27 |
| `cyclefold.py` | `_periods_to_crossing` (**keep `_monotone_read_surface`** — still live) | 22 |
| `pilot.py` | `_diagnose_stuck` (orphaned; `options._diagnose_stuck_reason` is the owner) | 13 |
| `investigate.py` | `_precise_cause` singular; the inert `program` param on `build_deviation_incident` + its `progress.py` argument | 12 |
| `options.py` | `_co_actions`, `_STUCK_COMPASS_NO_ROUTE`, `_STUCK_ZOOM_REJECTED` | 7 |
| `trace.py` | `trace_choice_identity` (zero refs repo-wide) | 4 |
| `outcome.py` | `classify_outcome` — **see caveat below** | 3 |
| `coast.py` | unused fields `CoastLimits.zoom_budget`, `CoastLimits.delayed_effects_budget` | 2 |
| `navigation.py` | `EvidenceResult` type alias | 1 |

**Tests: ~45-60 deleted, ~25 rewired.** Not 200-300.

**Traps — read before deleting:**

1. `test_pilot_coupling_profile.py::test_nonlinear_profile_falls_back_to_empirical`
   is the **only** assertion anywhere that a first-order `Approach` profile yields no
   analytic scan estimate, and `owner.profile.linear.estimate_scans` **is live**
   (`corrections.py`, `steer.py`). Rewrite it against `owner.profile.linear`; do not
   delete it. Its sibling `test_analog_coupling_boundary_estimate_is_analytic` is
   separately covered by `tests/core/test_advance_profile.py` and is safe to drop.
2. `_precise_cause` and `classify_outcome` tests: **rewire, do not delete**.
   `_precise_causes` and `assess_outcome` are the live owners and those tests are
   their real coverage — seven outcome tests hang off the `classify_outcome` helper.
3. The `expand_pipeline_need` test is **not** an 80-line whole-test delete. Its first
   ~68 lines assert `expand_routes` and `infer_pipeline_roles`, both live. Only the
   ~9-line tail goes.
4. Deleting `navigation.EvidenceResult` makes `Impossible` dead too. Re-run a type
   check before deleting the second one; a string annotation could not be ruled out.
5. `orientation._read_world` is a devtools-only survivor — sole caller is
   `devtools/pilot_wip_dark_run.py`, which is itself imported by
   `test_pilot_wip_dark_run_tool.py`, so it is transitively exercised. Deleting it
   means a one-line rewire to `_read_worlds(...)[0]`, not a free delete.

**Not dead, do not touch:** `TraceChoice`, `_RouteDraft`, `_RouteConflict`,
`_RouteConflictPin` (all heavily live — section G); `root_route` /
`recorded_root_route` (live reporting receipts); `legacy_outcome` (live in
`verify.py`); coast alias properties; `provisional_*` event names (consumed by
`dap/console.py` — locked contract); `corrections._best_forcing_holds` (labelled a
shim, has two real callers).

**`tests/tumbler/skeleton.py` address machinery — vacuous, not dead.**
`_OBJECT_ADDRESS_RE`, `_canonicalize_object_addresses`, `_address_neutral_sort_key`
are still called, but all four goldens now contain zero `ADDR` tokens, zero
`object at 0x`, and zero `0x` — `e1f64fa7` emptied them. **Recommendation: keep.**
If any repr regresses to a default `<... at 0x...>`, goldens become
process-dependent and this is the guard that catches it.

**`dap/` has zero imports from the pilot package.** The coupling is entirely through
event-name string literals. Nothing in `dap/` constrains a pilot symbol rename.

---

## D. Duplication collapse and single ownership

Ranked by value. Two items were found independently by two scouts with
non-overlapping scopes — noted, because that is unusually strong evidence.

| # | Item | LOC | Risk | Gate |
| --- | --- | --- | --- | --- |
| D1 | **`compass.py`: delete the six pass-through query methods.** `has_transitions`, `find_path`, `unprobed_actions`, `probed_actions`, `transition_dest`, `off_path_actions` are one-line delegations to `CompassKnowledge`; three have zero src callers. Every other consumer already reaches `.knowledge.` directly — the codebase has voted. Keep `orient`, `apply`, `graphs`, `action_tags`. | **−130** | low-med | `test_pilot.py`, `test_pilot_candidate_wait.py`, `test_pilot_sandbox_gate.py`, `test_pilot_compass_bridge.py` |
| D2 | **`tide_tables.py`: merge `solve_table_predicate` and `solve_calc_preimage`.** Same four-phase pipeline (model operands -> free tags -> domains -> bounded product + evaluate) written twice; differences are exactly `op`, `allow_free_sources`, `require_complete_domains`, and the projection. Keep "empty means unsat" vs "empty means no-pin" in the projections, not the core. | **−100** | med-high | `test_table_oracle.py`, `test_pilot_sandbox_gate.py`, `test_pilot_delayed_tide.py` |
| D3 | **`pilot.py` drive loop:** three act-kind isinstance ladders (try / rejected / accepted) that `navigation.act_identity` already discriminates; two entry-point tails (~37 similar lines each); two terminal-stop sequences; three failure `Plan` shapes in `_pilot_how_multi`. Also moves the inline event payloads to `recording.py`, their declared owner. | **−145** (net −115) | med | `test_pilot_golden_skeleton.py`, `test_pilot_recording.py` |
| D4 | **`program_step.py`:** collapse the eight terminal `ProgramStep(...)` arms' repeated kwargs behind one `_step(...)` closure (`boundary.tag if ... else producer.command_tag` alone appears six times); fold `_first_boundary` into `_first_advance`; hoist the `owner.profile.linear.distance` guard triad into one `_distance` closure; import `trace._all_nodes` instead of the local `_nodes` copy. Per-arm `required_inputs` presence is semantically meaningful — set it deliberately, do not default it on. | **−83** | low | `test_pilot_program_step.py`, `test_pilot_candidate_wait.py` |
| D5 | **`orientation.py` + `pilot.py` + `options.py`: one trace-read constraint bundle.** The nine-kwarg block (`clear_only`, `opaque_loop`, `pipeline_internal_tags`, `route`, `prior`, `avoid_pred`, `via_pred`, `rejected_actions`, `harness`) is hand-spelled at **eight** call sites. Some deliberately omit constraints — a bundle makes those omissions visible instead of invisible. **Rule-to-structure:** avoid enforcement currently depends on eight sites remembering `avoid_pred=`; see A1 for the other half of that fragility. | **−65** | med | `test_pilot_trace.py`, `test_pilot_avoid_gates.py`, goldens |
| D6 | **`options.py` cheap deletes:** keep one `_TraceAdmission` variable instead of unpacking seven locals twice; carry `preflight_admission` out of the zoom loop instead of recomputing it byte-identically in phase 6 (it calls `_managed_boolean_rungs` -> `_rung_execution_receipt`, real compile work, on the hot path); delete `continuation_evidence`; delete `deferred_commands`; fold the wait scalars back into `_WaitPrescription`. **Re-anchor against the in-flight work first.** | **−61** | low | `test_pilot_candidate_wait.py`, `test_pilot_orientation_contract.py` |
| D7 | **`verify.py`:** five byte-similar rejection returns + two target-acceptance returns behind `_reject()` / `_accept_target()` closures (the two early avoid returns legitimately omit `collected_nogoods` — the closure needs a default); `gauge.ordinal_advanced` computed four times per pass with identical arguments, reducible to two (the `_gate_spin` call must stay separate, it precedes a possible excursion-retry fork replacement). | **−48** | low | `test_pilot_verify.py`, `test_pilot_avoid_gates.py` |
| D8 | **`progress.py`:** five sites build the same trial `_Checkpoint`; four build the same `provisional_*` payload. Keep the `_refresh_checkpoint`-on-key-match vs always-append distinction explicit. | **−45** | very low | `test_pilot_progress.py`, goldens (payload key order must not change) |
| D9 | **`tide_tables.py`: delete `guard_satisfiable`** — a 48-line docstring around `guard_verdict(...) != GUARD_DEAD`; only caller outside the module is its own test. Move the two informative docstring paragraphs into `guard_verdict`. | **−45** | low | `test_guard_satisfiable.py` |
| D10 | **`steer.py`:** `_try_zoom` takes seven loose params that are exactly the fields of the `Coast` act it was given, then reassembles the precedence rule `Coast`'s docstring already states — pass the act. Optionally collapse the three coast `_try_*` fork/session/observe/PulseState skeletons (−45 more, medium risk: `_try_terminal_letrun` alone computes `start_roles` and the stall early-return; `_try_zoom` alone rebases `verify_channel` on `departed_route`). | −18 (−63) | very low (med) | `test_pilot_steer.py`, `test_pilot_coast.py` |
| D11 | **`orientation.py`:** state the `Coast` heading precedence once (`route_plan is not None` appears six times, the program-or-advance disjunction three); pass the already-computed `rejected_actions` set into `_route_rejected_actions` instead of rebuilding the frozenset per pair inside an `all(...)` — a hidden quadratic. | **−42** | low-med | `test_pilot_orientation_contract.py`, `test_pilot_coast.py` |
| D12 | **`_ops.py`: merge the two atom-lowering functions.** `_until_unresolved_condition` and `_atom_condition` are the same lowering with an inverted operator table. Keep the crossing-atom prefix and the `Bool` special case behind flags; do **not** merge the third operator table for crossing `Cmp.op` with the trace-`form` table. | **−28** | med | `test_pilot_ops.py`, `test_pilot_holds.py` |
| D13 | **`availability.py`:** `_expr_availability`'s three fold ladders are `max()` (And), `min()` (Or), `min()` (alias) over the enum. The class docstring already *asserts* the total order; collapsing makes it true by construction. Preserve empty-`terms` behavior via explicit `default=`. | **−25** | low | `test_pilot_needed_vocabulary.py` |
| D14 | **One `edge_admissible` predicate.** *(Found independently by two scouts.)* The static-edge admission check is written **three** times — `options._compass_route_plan._edge_open`, `navigation_evidence.frontier_status.edge_allowed`, and an inline lambda in `detour.py` — same four checks in the same order, differing only in the nogood source and one extra term each. Home: `navigation_evidence.py`, already the declared owner of "reachability evidence shared with verification and recovery", with `detour.py` a recovery consumer. Callers compose `base(edge) and <extra>`. **The three copies may already have drifted — diff them before merging; one of them will change behavior.** | **−22** | med | `test_pilot_avoid_gates.py`, `test_pilot_nogood.py`, `test_pilot_detour_progress.py` |
| D15 | **One "unique legal current reading" owner.** *(Found independently by two scouts.)* `options._current_bearing` and `detour.py`'s channel arm both build a `WorldView`, call `current_readings`, filter by `route_allowed` + `not _avoid_forces`, and require `len(legal) == 1`. `detour` silently omits `_awaits_operator`. `CLAUDE.md` says Compass owns filtering and ambiguity policy — neither copy is in Compass. Make `_awaits_operator` a stated parameter; **default it off so `detour` keeps today's behavior exactly.** | −20 | med | `test_pilot_detour_progress.py`, `test_pilot_table_detour.py`, `test_pilot_currents_capability.py` |
| D16 | **`_ops.py::_pilot_world_key` re-defines `_rung_identity` inline** — byte-identical, eleven lines apart in the same file. That identity is what `_append_rungs` dedupes on, `_install_confirmed_correction` validates against, and `_revoke_corrections` removes by. Cheapest true win in the package. | **−8** | **none** | `test_pilot_ops.py`, `test_pilot_nogood.py` |
| D17 | Small: `tide_tables.bounded_product` for the `_MAX_FREE_INDICES`/product-cap guardrail written 4x (kills `corrections.py`'s cross-module private import of `_MAX_*`); `evidence._call_site_nodes` for the `main_by_rung` loop written twice; `_ops._route_allowed` inlined into its single same-file caller; one `_ordered_unique_pairs` for three ordered-dedup spellings (**note:** the three differ on unhashable and `bool`-vs-`int` values; unifying on the `repr`-keyed form is a small behavior *fix*, verify against goldens). | −60 | low | various |
| D18 | **`verify.py::_gate_spin` hand-rebuilds `_PulseState` field-by-field and drops `coast_receipt`**, so a retry fork loses its receipt and `pilot.py`'s accelerator payload loses `trial.coast_receipt.advances`. **Latent, not live** — the excursion arm requires `post_pulse_key != frame.key` and every coast producer sets them equal, so only receipt-less pulses reach it. Use `dataclasses.replace`. Removes a bug class. | **−11** | very low | `test_pilot_verify.py` |

---

## E. Receipt structures

Facts that already have an owning artifact but travel as loose scalars.

- **E1. `ChannelMotion` receipt.** The cure for A4: one receipt (channel_tag,
  target_value, boundary, owned stop_reason) on `_AttemptIntent` / `_TrialResult`,
  replacing 162 occurrences of six scalars across 13 src modules.
  `assess_outcome` consumes `motion.reached`; the snapshot fallback goes.
  −40 to −60. Risk: medium (wide but mechanical).
- **E2. `TargetSpec` on `_PilotContext`.** 32 reads of `ctx.target_tag` in-package
  and two full `TargetSpec(...)` rebuilds — `pilot._pilot_loop_events` and
  `verify._gate_dead_end`. The latter is the doctrinal case: the bearing's own
  `attempt.intent.bearing_objective.target` is in scope and ignored. −15 to −25.
- **E3. `ProgramStep` should carry its own derived facts.** `handoff_by_action` is
  built twice from `input_handoffs`; `required_pairs` is rebuilt twice more in
  `options`. Give the receipt `handoff_by_action`, `uniform_handoff_boundary`
  (`None` when inputs are not all handed off under one boundary — this also settles
  the in-flight disagreement noted in Status), `required_pairs`, and
  `inputs_with_lifetime`. `_program_input_details` then disappears. −13.
- **E4. `_Candidate.source`.** Four booleans (`route_prescribed`,
  `influence_prescribed`, `current_prescribed`, `program_prescribed`) are mutually
  exclusive *by construction*, but proving it requires reading 400 lines of
  `_build_candidates`. Three downstream sites re-derive the kind from them. One
  `source` literal set once in `_candidate_for`, with the four booleans kept as
  `@property` shims so the golden payload stays byte-identical. −35.
  **Rule-to-structure:** the ordering commentary spread across `_Candidate`'s field
  comments collapses to one enum ordering.

---

## F. Ownership relocations

Net-small on LOC; each removes a seam the doc currently polices in prose.

- **F1. Finish `static_expressions.py`.** It is 70 lines and is a half-finished
  extraction, not an owner: `trace.py` still re-exports its two functions under
  `_`-prefixed aliases, and `evidence.py` imports the *alias from trace* rather than
  the function from its owner. Move in: the trace inequality-resolver sub-cluster
  (`_resolve_inequality_target`, `_heuristic_inequality_target`,
  `_strict_inequality_step`, `_domain_granularity`, `_declared_float_bounds`,
  `_atom_text` — keep `_inequality_levers` and `_rewrite_internal_compare` in trace
  as thin consumers), `availability._simplified_expr_tags`, and
  `evidence._channel_constraint` + `_channel_from_values`. Drop the re-export
  aliases. **Takes six cross-module `from X import _private` reaches to zero.**
  ~−35 net; the point is the reach count. Risk: low-med (all pure functions).
- **F2. `evidence.py` consumes `tide_tables`' operand model.** `_indirect_pipeline_source`
  + `_canonical_index_source` + `_IndirectPipelineSource` + `_destination_from_indirect`
  reimplement `table_from_indirect_src` + `_TableOperand` + `_read_table`; the
  instruction-finding loop is written a *third* time in `trace._invert_indirect`.
  The two versions differ in `IndirectExprRef` name filtering (one requires exactly
  one name, the other exactly one *mutable* name) and in snapshot vs empty-env
  address evaluation — **preserve both as explicit parameters, do not smooth them.**
  −35. Risk: medium.
- **F3. `trace._invert_indirect` -> `tide_tables.py`**, beside `_model_table_operand`,
  factoring the shared src-finding loop. Its own docstring says tide_tables
  generalizes it. −30.
- **F4. `investigate.hold_defeats_needed` + `_hold_values` -> `options.py`**, its sole
  caller. A static write-vs-need predicate about option ranking, not incident replay.
  Net small; tightens the "still needed has separate meanings" boundary.
- **F5. Out of `progress.py`:** `_investigation_started_event` and
  `_channel_transitions` -> `recording.py` (pure payload rendering; `_channel_transitions`
  also carries dead generality — a `channels` list that can only hold one element —
  collapsing to ~8 lines); `_replay_step` and `_deviation_bearing` -> `investigate.py`
  (pure mappers into its own types). ~95 LOC leaves; 0 net. Makes two `CLAUDE.md`
  claims true by construction.
- **F6. `compass._observation_exercised_edge` -> `charts.py`** as
  `StaticTransitionEdge.exercised_by(...)`. It reads five edge fields plus three more
  in its caller loop; compass should ask the edge, not know its anatomy. 0 net.
  Do it alongside D1 while compass is being tidied.
- **F7. Small ownership:** `currents.WorldView` -> `types.py` beside the `WalkContext`
  protocol it satisfies (removes charts' and program_step's structural dependency on
  `currents.py`); `steer._letrun_zoom`'s reaches into `work._dt` / `work._harness`
  -> `_ops.py` or `advance.py` (every other runner-private reach lives in `_ops.py`
  by design); publish `TraceNode.unsatisfied_conditions()` so `verify.py` stops
  reaching `_collect_unsatisfied`.

---

## G. Structural, deferred

Each needs its own gate; none should start before A and B land.

- **G1. `options.py::_build_candidates` phase extraction.** 544 lines, 14 sequential
  phases. Extract *within the module*: `_select_route` (phases 3-6), `_lower_prerequisites`
  (7-8), `_learned_fallback` (9), `_assemble_candidates` (11). LOC ≈ −20 — **the
  payoff is control flow, not line count; say so honestly.** High risk if rushed,
  medium if phased, and only **after** D6. Phases 3-6 share seven mutable locals
  rebound in phase 6; the extracted record must carry them.
- **G2. R1 Step 4 — prove and delete the structural route machinery.**
  `TraceChoice` (carries `via=`), `_RouteDraft`, `_RouteConflict`, `_RouteConflictPin`
  are all still live. Before deleting, dark-compare the complete admissible
  immediate-act universe and constraint dispositions with and without them.
  Coverage showing the commitment fields are dead is **not** sufficient.
  `root_route` / `recorded_root_route` survive deliberately as reporting.
  `wip_dark_completed.jsonl` beside this file is the saved dark-run report from R1
  Step 1 — kept for this step. `devtools/pilot_wip_dark_run.py` produces it, and
  `orientation._read_world` exists only to serve that devtool (see C, trap 5).
  Delete the report and the scaffolding together, in a separate commit, once G2 is
  decided.
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
- **G9. `outcome.assess_outcome`'s snapshot fallback.** Tracing every `steer.py`
  producer, the `else:` arm looks unreachable — but **all seven tests in
  `test_pilot_outcome.py` exercise only that branch** (none passes `zoom_stop_reason`).
  So the focused truth-table suite may be testing a path production never takes.
  Two steps: assert-and-log that `zoom_channel_tag is not None` implies
  `zoom_stop_reason is not None`, run tumbler, confirm; only then delete the fallback
  **and rewrite the test file to pass receipts**. Do not delete it while the tests
  still pin it — you would delete their only path. Subsumed by E1 if that lands first.

---

## H. `CLAUDE.md` shrink

Beyond the corrections in section B, these paragraphs are already true by
construction and can reduce to one line naming the owner. **No code change required
for the first group.**

- Hold-log tag summaries derived from exact rungs (`_HoldLogEntry.tags` and
  `_StepContext.steady_holds` are `@property`; no parallel field can desync).
- Coast channel-owner set (`_ops.coast_departure_tags` has exactly two consumers,
  both verbatim).
- Correction artifact consumed whole (the installer takes the artifact, validates
  identity, stores without recompiling).
- `_rung_execution_receipt` "produced from the same expanded branches `_set_rungs`
  installs" — both literally call `_expand_pilot_rules`.
- Compass returns exactly one `Bearing | NeedProbe | Stuck` — enforced by the
  `OrientationResult` union alias and the return annotations. **A fourth result kind
  is not expressible.** This is the strongest guarantee the route removal bought and
  the doc does not yet claim it.
- `max_scans` and pending-departure lifetimes share one coordinate —
  `_PilotState.search_scan` is a derived `@property`; there is no second coordinate.
- Empirical evidence never creates a lever — `CompassKnowledge` has no lever field;
  `action_tags` delegates to the static catalog. The type cannot express it.
- Availability orders, never rejects — in `_rank_writers`, availability is bound only
  into the sort tuple; the only two `continue`s are the documented qualifiers.
- **New, from the in-flight work:** `_WaitPrescription` has no `details` field, so a
  wait receipt is structurally incapable of carrying actions; and
  `_admit_trace_details` makes single-admission true by construction. Both should be
  claimed as structural, not merely stated.

Each of A2, D14, D15, E4, F5 licenses a further paragraph reduction once landed —
noted inline above.

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
  tests use), **`steer._settle_cone`** (22 lines, but it has a dedicated parity test
  and four call sites), or **`orientation._read_world`** (a devtools seam — soften the
  docstring instead).
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

- **Inequality resolvers go to `static_expressions.py`, not a new `inequalities.py`.**
  The module already exists and is named for exactly this; `investigate.py` and
  `recording.py` already reach into trace for these helpers. (F1.)
- **No `static_facts.py`.** The `compute_*` island stays in `trace.py`.
- **`_interior_frontier` + `iter_nodes()` (G5) comes first; the TraceNode split by
  kind may then be unnecessary.** They are the same lever at very different prices.
- **`structural_nogoods` was not "inner-loop transport."** It fired from the outer
  trace, so by the predecessor note's own criterion only the inner call site should
  have gone. Its removal is defensible on a different and better ground — a static
  pre-trial rejection contradicts "reject only with proof", and `verify.py` has the
  empirical gate — but that argument carries the obligations in A5 and A6.
- **`hold_defeats_needed -> options.py` appeared in two documents.** One item. (F4.)
- **`edge_admissible` home: `navigation_evidence.py`**, not `charts.py`. Two scouts
  proposed different homes; `navigation_evidence` is already the declared owner of
  evidence shared with verification and recovery, and `detour.py` is a recovery
  consumer. (D14.)

---

## Rough totals

| Section | LOC |
| --- | --- |
| A — correctness | ~+30 src, +40 test |
| C — provable deletes | −215 to −230 src, −45 to −60 test |
| D — duplication collapse | ~−850 |
| E — receipt structures | ~−125 |
| F — ownership relocations | ~−100 net |
| G — structural, deferred | ~−170 |

Realistic near-term: **−1,000 to −1,200 LOC** from C + D + E, most at low risk, out
of ~27,000. `trace.py` (4,247), `investigate.py` (2,697), and `pilot.py` (1,981)
remain the three largest files; only `pilot.py` shrinks materially in that pass.

Sequencing: **A -> B -> C -> D (by rank) -> E -> F -> G.** Within D, land D16 first
(zero risk, proves the loop), then D1 and D7/D8 (large, low risk), then the
medium-risk semantic ones behind a green tumbler run.
