# Audit 3 — Evidence/verdict split (pattern 1) + proposer/single gate (pattern 2)

Opus agent audit, 2026-08-10.

## The reference pattern (corrected)

1. The charter's named reference does not exist: no `StepEvidence`, no `classify_step` anywhere. `program_step.py::read_program_step` (`:310-587`) has the SHAPE: forking front-loaded (`:326-423`), decision cascade (`:458-587`) reads only locals, every exit through one constructor `_step` (`:425-456`).
2. Real completed instance: `attempt_interpretation.py::interpret_attempt` (`:269-302`) — pure classifier over three frozen records (`_AcceptedTrial`, `ProgramStep`, `IntrascanResult`) + scalar → frozen verdict, never touches a PLC.
3. Second: `earned_work.py` — `EarnedWork.receipt()` (`:167-179`) freezes observation; `EarnedWorkReceipt.movement` (`:124`) + `earned_work_is_useful_motion` (`:139`) are the verdicts.
4. Third: `progress.py:1022 _assess_pending_departure` → `:1088 _apply_departure_decision` — decide-then-apply over frozen `DepartureDecision` (`:301`).
5. Statement of pattern: impure reader returns one frozen record whose fields are all the classifier needs; a module-level pure function maps record → verdict + reason + supporting identities; a third function applies the verdict. Cite attempt_interpretation and earned_work, not program_step.

## Table 1 — verify.py gate order

Gates: `_verify_gates` (`:848-1176`) then `_verify_after_spin` (`:518-703`); excursion replay re-enters at `_verify_after_spin` via `verify_excursion_replay` (`:706-845`).

| # | Gate (line) | Evidence | Judgment | Splittable? |
|---|---|---|---|---|
| 0 | pre-gather `:871-892` | channel_motion (`_owned_channel_motion:154`), earned_work_receipt | none | Y — already unconditional |
| 1 | AVOID `:933-966` | avoid_pred over coast receipt `:935`, settled snap `:948`, action/wait/post-pulse snaps `:957` | first violation rejects | Partial — snapshots retained on trial, but eager gather calls an OPAQUE USER CALLABLE N extra times; keep lazy inside the evidence builder or inline |
| 2 | BANKED-WORK `:974-992` | applied_actions, policy.motion, receipt | pure | Y |
| 3 | EXPECTATION-VIOLATED `:999-1012` | `_proved_effect_violations(attempt)` (`:96`) | pure | Y |
| 4 | REQUIREMENT-VIOLATED `:1018-1040` | `active_requirement_violations(reqs, frame.snap, trial.snap)` | pure over snaps | Y |
| 5 | TARGET `:1046-1052` | `target_reached(trial.snap,…)` | pure | Y |
| 6 | EXPECTATION-SURVIVED `:1061-1129` | fulfilled observations, obligation boundaries | pure BUT rebinds `channel_motion` at `:1089` and builds a TrialAssessment inline `:1095-1105` | Y with caveat: carry channel_motion as derived verdict field, not rebound local |
| 7 | SPIN `:1131-1164` | `_gate_spin:268` reads `_has_pending_effects(trial.fork)` `:277` — live fork query | 3-way `_SpinVerdict` | Y — one cheap fork read, hoistable |
| 8 | TARGET again `:548-570` | duplicate of #5 | — | Y (exists only because excursion replay re-enters here) |
| 9 | DEAD-END `:572-586` → `_gate_dead_end:369-510` | full `trace_back` over trial.snap `:401`, `NavigationEvidence.frontier_status` w/ compass knowledge `:427`, `_has_pending_effects` `:444` | frozen `_DeadEndResult` (`:78`) | N up-front — most expensive evidence, reached only after 8 cheaper gates; eager gathering violates the plan's perf gate |
| 10 | outcome `:588-598` → `assess_outcome` | `_motion_agency` (`outcome.py:95-144`) runs `action_caused_change(trial.fork,…)` per opaque-loop tag (`outcome.py:134`) | 4-axis assessment | N up-front, Y locally — hoist `agency` into evidence, assess_outcome becomes fully pure |
| 11 | not-accepted `:600-624` | assessment | pure | Y |
| 12 | credentials `:626-653` | source_world_key, act_identity, _semantic_key | pure | Y |
| 13 | REVISIT `:654-664` → `_gate_revisit:302` | seen_keys, consumed_revisits | pure | Y |

VERDICT: a single up-front VerifyEvidence is NOT achievable (gates 9-10). Achievable, gate-order-preserving split at the file's existing seam:
- `TrialEvidence` (cheap, unconditional) + pure `classify_trial_gates` → gates 0-8.
- `LandingEvidence` (post-spin: new_tree, trend, has_new_frontier, pending, agency) + pure `classify_landing` → gates 9-13.
Mechanical requirements for decision-identity: `gate_events` and `collected_nogoods` are shared mutable lists appended in-order across both halves (`:884-885,:293,:348,:449,:479`) — pure classifiers must return ordered tuples appended by the caller in the same order. `_gate_dead_end` already returns a frozen record, so half of gate 9 is done.

## Table 2 — departure.py split status + leaks

`classify_departure` (`:616-674`) reads only DepartureObservation fields; `logger.debug` (`:664`) its only effect. Four leaks:

| Leak | Where | What |
|---|---|---|
| Judgment duplicated in observer | `observe_departure:498-506` vs `classify_departure:623-628` | `stop_reason != "quiescent"` and `movement is BACKWARD` evaluated twice with same literal reason strings |
| Verdict smuggled via string prefix | `_not_inspected` builds `Unknown(f"continuation not inspected because {reason}")` `:495-496`; classify does `startswith(...)` + `removeprefix` `:637-641` | Observer decision encoded in prose, string-parsed back — control channel, not evidence. Needs typed `NotInspected(reason)` or a bool on ContinuationEvidence |
| Policy changes record contents | `explain=` `:464-475`, set from `movement is not FORWARD` at `:559,:604`, hard-False `:612` | Whether `_shared_cause` runs — hence whether `disposition` can be OWNED/REACTIVE — decided inside observe; `classify:657-660` then demotes CLEAN_CONTINUATION on REACTIVE. Classifier output depends on hidden laziness policy |
| Second classifier in observer | `_departure_reading:368-426` | Pure but IS classification (disposition+reason `:408-418`) inside the impure half |

Also: `_is_hold_landing` (`:328-365`) hard-codes `{"holding","held"}` PackML labels + graph-action name scan — domain knowledge in a classifier (charter "no domain annotation").

## Table 3 — other gather+judge sites

| Site | Status | Verdict |
|---|---|---|
| `outcome.py:152 assess_outcome` | pure except `_motion_agency` (`:95-144`, fork query `:134`) | SPLIT — hoist agency only; the injected `causal_probe` hook (`:100,:123`) exists because impurity is misplaced; deleting the hook is the payoff. S |
| `effects.py:1571 observe_execution_window` | already split (window impure, `observe_expectation` pure over frozen projections) | mostly inline; one leak `:1687-1714` rewrites SURVIVED→UNKNOWN on corridor completeness — carry `corridor_complete` as evidence field. S |
| `earned_work.py` | fully split | leave — charter reference |
| `investigation_replay.py:612 _regression_ownership` | frozen verdict but 2 impure inputs inside (`plc.state.tags:627`, `cause_replayed:633`) | SPLIT as TESTABILITY EXEMPTION (fails two-consumer on reuse; Stage 10 needs the 8 booleans `:642-668` testable). S |
| `intrascan.py:541 derive_recorded_observations` | judgment loop with lazy impure re-gather (`project(scan):588-589`, advance_index_factory `:593-596`) | INLINE — laziness IS the contract ("one projection build per execution owner"). Charter boundary case |
| `intrascan.py:513 inspect_assertion_scan` | question-record → result-record | leave |
| `progress.py:453 _monitor_trend` | 7-branch retention cascade with mutation+yield interleaved (`:493,:517,:530,:550,:586,:603`) | SPLIT — inputs already materialized; RetentionDecision + pure classifier + apply mirrors _assess_pending_departure in the SAME file (second consumer of the pattern); Stage 9 mandates it. M |
| `correction_candidates.py:175 _rank_hypotheses` | sort with IMPURE key (`chase_chain_tags(plc,…):195`, `_last_transition_scan:207` inside key()) | split evidence, keep sort — precompute primal frozenset + proximity map. S |
| `investigate.py:197 _resolve_replay_attempt` | pure over ReplayOutcome | leave |

## Table 4 — precedence sites

| Site | Today | Class | Feasibility |
|---|---|---|---|
| `orientation.py:621-847 _orient_read` | STATEMENT-ORDER, 7 tiers, no tuple, no generators (tiers at `:642,:646-691,:693-716,:718-749,:751-770,:776-806,:808-814,:816-845,:847`); docstring `:626-632` describes precedence only statement order enforces | proposer-tuple candidate — the charter's own reference, does not exist yet | HIGH. Admission already ONE expression copy-pasted at 6 sites (`:682,:707,:736,:754,:797,:836`): `_act_preserves_requirements(world,act) and not act_is_nogood(world_key, act_identity(act))`. Each tier already yields (act, rationale). The `:754` `continue` wrinkle disappears correctly under a single gate. M |
| `orientation.py:88-189 _theory_retry_bearing` | tier 0; RAISES ValueError on every mismatch (13 sites); runs own admission `:175-182` | EXACTNESS CONTRACT — exempt. Proposer may be fail-loud when re-resolving a retained identity | do not fold into tuple |
| `orientation.py:850-887 _read_group` + `orient:937-970` | second orthogonal precedence: open-work worlds before fresh; `_is_maintenance` demotes terminal coast/dwell for fresh only | proposer-precedence, OUTER level | separate ordered tuple over worlds; do not merge axes. S |
| `orientation.py:574-575 _bearing` expectation check | raises if expectation and exemption both None, after admission | implicit second gate | fold into the single admission gate. S |
| `options.py:1973-2038 _assemble_candidate_read` | statement-order proposer: route `:1973`, trace `:1977`, learned `:1987`, broad-reach `:1994`, awaited `:2005`; orientation `:751` consumes in exactly this order | proposer-tuple candidate | HIGH but blocked on double-gating (below). M/L |
| `options.py:1714-1769 _select_wait` | explicit 3-source precedence, pure, documented sole chooser | ALREADY TARGET SHAPE | leave; second reference example |
| `options.py:1414-1421` route-plan suppression | `route_plan = None if current_trace_actions or banked_trace_work or pending…` | proposer silently filtering — evidence source deleted before any gate | make it an admission reason, not erasure. S |
| `options.py:1278-1364 _admit_trace_details` | single admission policy; docstring "Nothing enters candidate ranking by being appended after this pass" | correct single gate — but `:2005-2038` awaited action IS appended after the pass | keep fn; fix bypass. S |
| `trace.py:4521-4645 _rank_writers` | tuple sort `:4634/:4637`; three `continue`s `:4571,:4574,:4579` are proofs | sort key — fine | none |
| `trace.py:3593-3678 rank_trace_choices` | avoid filter + sorted | sort key — fine | none |
| `trace.py:2297-2342 _select_trace_alternative` | filter→min→conditional replacement w/ documented carve-out `:2309-2312` | EXACTNESS CONTRACT | composition stops here |
| `correction_candidates.py:175-238` | 6-tuple key sort | sort key (fix impure key per Table 3) | none |
| `correction_candidates.py:241-299 _compose_hypotheses` | union with contradiction detection, None on conflict | EXACTNESS CONTRACT | do not pipeline |
| `investigate.py:649-705 _initial_hypotheses` | ALREADY a lazy generator, 3 ordered tiers (`:658,:675,:691`), documented laziness `:650-657`, one gate loop `:771-` | closest existing instance | promote to named tuple of 3 generators; move dedupe (`local_ids:667,:686-688,:704`) into consumer loop (already keeps observed_hypotheses `:772-776`). S |

### Double-gating / admission-in-proposer

- `earned_work_is_useful_motion` consulted 6× per accepted trial: `verify.py:292,:478,:322,:651` + `outcome.py:178,:232` — clearest candidate for one `useful_motion: bool` evidence field.
- TARGET gated twice (`verify.py:1046` and `:548`) — an artifact of the excursion re-entry point.
- Action admission applied in proposer AND gate with two vocabularies: `options.py:127 _action_allowed` + key_nogoods at `:1329,:1844,:1974,:1989,:2012` (pair-level) vs orientation `:754` act-identity-level. An act filtered by options never reaches the gate → no gate event, no rationale — REJECTION IS INVISIBLE.
- Admission implicit in order: `options.py:1414` (route erased), `:2005-2007` (awaited only if no TRACE candidate), `:1346-1354` (establish_pending truncates candidate set).
- `verify.py:1089` channel_motion rebind: gates 7-13 judge different evidence than gates 0-5.

## Resists the pattern (charter input)

1. Single up-front VerifyEvidence impossible. Boundary: evidence may be gathered in STAGES when a later stage's cost is conditional on earlier gates passing; each stage = one frozen record + one pure classifier.
2. `intrascan.derive_recorded_observations` stays lazy — "one projection build per execution owner" outranks eagerness.
3. `_theory_retry_bearing` is fail-loud by design — a proposer re-resolving a retained identity may raise; only current-world proposers must decline.
4. `_select_trace_alternative`, `_compose_hypotheses`, `_rank_writers`' continues = exactness contracts.
5. `departure._is_hold_landing` PackML domain knowledge — record exemption or fix under "no domain annotation".
6. options.py proposer consumes forks (`_prescribe_wait` → `read_program_step` at `options.py:1123`) — proposer tuple over options inherits an undeclared fork budget; collides with instruments/probes boundary (kernel slice problem, but constrains this slice).

## Sizing + order

| # | Transform | Size | Files |
|---|---|---|---|
| A1 | verify two-stage evidence/verdict; events/nogoods as ordered tuples | L | verify.py, types.py |
| A2 | departure leak fixes (typed NotInspected, kill string protocol, explain as evidence field, merge _departure_reading into classify) | S | departure.py |
| A3 | make the reference real: StepEvidence + classify_step (cascade order do-not-touch; `_input_split:547` precomputed into record) | M | program_step.py |
| A4 | hoist agency out of assess_outcome; delete causal_probe hook | S | outcome.py, verify.py |
| A5 | _monitor_trend → RetentionDecision + pure classifier + apply | M | progress.py |
| A6 | _regression_ownership evidence record (testability exemption) | S | investigation_replay.py |
| A7 | _rank_hypotheses pure sort | S | correction_candidates.py |
| B1 | orientation PRECEDENCE tuple + one admission gate absorbing 6 copies + _bearing check | M | orientation.py, navigation_contracts.py |
| B2 | options append stanzas → ordered generator tuple; move _action_allowed/key_nogoods to the single gate (rejections become visible) | M/L | options.py, orientation.py |
| B3 | _initial_hypotheses → named tuple of 3 generators; dedupe to consumer | S | investigate.py |
| B4 | route-plan suppression + awaited-action bypass become admission reasons | S | options.py |

Recommended order: A2 → A4 → A6 → A7 → B3 → B1 → A3 → A5 → B2 → A1 (B1 before A3 so the reference is rewritten only after the pattern is load-bearing; A1 last — only L, pilot_divergence matters most there).
