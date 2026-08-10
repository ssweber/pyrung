# Audit 5 — Bills & receipts (pattern 5) + rules of extraction

Opus agent audit, 2026-08-10.

## Table 1 — Bill lifecycles

| Bill | Create | Discharge | Expire/revoke | Silent-drop | Flag |
|---|---|---|---|---|---|
| `ActiveRequirement` | `pilot.py:304` (+`:348` bootstrap, `:421` attempt, `progress.py:281` delayed-regression) | `pilot.py:3708-3712` — ONLY site, only via Stage-6A `_prove_controlled_retry` | NONE | never removed from `state.active_requirements` (list, `types.py:848`); refused repair stays ACTIVE forever; `pilot.py:2590-2605` rescans every turn; `requirement_repair_attempts` only suppresses the retry | MISSING EXPIRY. `RequirementStatus.INVALIDATED`/`AMBIGUOUS` never assigned (Stage-8 debt) |
| (proof blocking) | — | — | — | `pilot.py:3892-3914` withholds theory proof while any requirement ACTIVE — shadow-record only | undischargeable requirement permanently blocks theory proof |
| `ExpectationReceipt` minting | `pilot.py:480-496` | consumed `progress.py:138` / `requirements.py:622` | never removed | 7 SILENT EARLY-RETURNS before minting: `pilot.py:433,436,442,446,450,463,478` — no event, no negative receipt; the `EffectExpectation` bill vanishes traceless | MISSING RECEIPT-PATH ON FAILURE |
| `EffectExpectation` | `options.py` (once, on ActPolicy), `navigation_contracts.py:160` | `verify.py:119-133` → effect_observations; `:1062-1129` survived gate | — | ACCEPTED trial with violated obligation: `pilot.py:369` returns `if attempt.trial is not None` → no FailedEffectReceipt, no ActiveRequirement; only later regression (`progress.py:260-282`) may recover | MISSING DISCHARGE on the accept arm |
| `FailedEffectReceipt` | `pilot.py:405-420`; `progress.py:260-275` | `pilot.py:581-595`; `attempt_interpretation.py:180-226` | never | append-only knowledge list (`types.py:850`), dedup by identity | acceptable (invocation-scoped) but unbounded |
| `PendingDeparture` | `progress.py:897-905`; deadline = min(max_scans, search+`_PENDING_DEPARTURE_SCAN_BUDGET`) | PROMOTE `progress.py:1108-1136`; REGRESS `:1137-1156` | EXPIRE `:1158-1174` | 6 silent `= None` sites, no event: `pilot.py:1269,1645,2548` (after `checkpoints.clear()` in repair), `progress.py:1718,1775,1981` | EXPIRY UNREACHABLE ON REJECT ARM — `_assess_pending_departure` runs only from `progress.py:470` inside `_assess_and_retain(trial=…)`; a drive rejecting every candidate never re-checks the deadline; departed world persists until max_scans |
| `_Checkpoint`/`_CheckpointOwner` | `progress.py:357-373`; appended `:557,570,1113` | revert `types.py:1012` | `del checkpoints[i+1:]` `progress.py:1109,1159,1985`; `clear()` `pilot.py:1268,1644,2547`, `progress.py:1717` | `clear()` orphans owners referenced by a live PendingDeparture; `_checkpoint_index` (`progress.py:332-337`) RAISES. Each clear() site currently pairs with `pending_departure=None` — latent coupling | INVARIANT BY CONVENTION — 4th clear() without the paired None crashes the drive |
| PilotRung prerequisite holds | `steer.py:107-119` (+hold_log) | NONE | world revert (`types.py:1024`) or correction revocation (`progress.py:1438-1441`) only | hold installed for one bearing survives every later bearing in the lineage; no guard-expiry, no supersession receipt | MISSING DISCHARGE + REVOCATION RECEIPT (plan Stage 10 owes this, working-theory-plan:572) |
| PilotRung correction rungs | `progress.py:1213` | promote `:1253-1268` | revoke `:1411-1457` (nogoods + hold_log "revocation") | none | OK — the only fully-receipted bill |
| `_CorrectionReceipt` | `progress.py:1221-1232` | ACTIVE `:1263` | REVOKED `:1423` | none | OK |
| WorkingTheory version/request | OpenTheory `working_theory.py:1004-1027`; RetryTogetherRequest `:761-771` | ProveTheory `:1281-1312` | AbandonTheory→tombstone `:1326-1339`; drive-side `pilot.py:5638-5649` | theory left OPEN at loop exit never abandoned unless Stuck fires `:5582`; budget-exit and target-reached paths do not close it | PARTIAL EXPIRY — OPEN can survive termination |
| Probe budget (skiff) | mark `pilot.py:5560`, folded `compass.py:800-802` | — | enforced `orientation.py:526,536` (`_PROBE_BUDGET=2`) | none | OK — cleanest declared budget |
| `CompositionBudget` | `recovery.py:31-48`; `pilot.py:1252,1628,2524` (limit 1), `investigate.py:1129` (9) | consume `:42-48` | `CompositionTermination.BUDGET` `recovery.py:254` | none | OK |
| Skiff caps | `skiff.py:196-197` | — | loop exit | none | OK; but duplicate name `_SKIFF_MAX_PROBES=8` at `refinement.py:33` re-exported `investigate.py:86` |
| `RevisitCredential` | `verify.py:319-362` | consumed `pilot.py:4584` | never | spent even if a deferred-adoption trial (`pilot.py:4954`) is later discarded; documented intentional (`types.py:866-868`) | OK by design |
| `recovery_continuation` | `pilot.py:2301-2313,:2513` | tip validation `:2006-2029` | `= None` `:1646,2271,2276,2291` | cleared without event at all 4 sites | minor — no receipt of why it ended |
| `proof_rejected_acts` | `pilot.py:4932` | read `orientation.py:76` | never cleared | keyed with `active_requirements=()` — changed requirement set does NOT re-admit, contradicting `types.py:862-864` comment | DOC/CODE MISMATCH (`pilot.py:4922-4928` passes `()` deliberately) |

## Table 2 — Receipt consumers (UNREAD/TEST-ONLY/DEAD highlights)

PROD-consumed and healthy (keep): CoastReceipt (+trajectory), EarnedWorkReceipt, ExpectationReceipt+snapshot, FailedEffectReceipt+snapshot, ActiveRequirementSnapshot, RequirementSourceWalk/OccurrenceSourceLink, _BootstrapExecution, OperationReceipt, PilotOverlayExecution (5 modules), _CorrectionReceipt, _HoldLogEntry, _StepContext.frontier_tags/steady_holds, _ExecutionEvidence.effect_observations/accelerators, _PilotState.{recorded_root_route,lever_notes,avoid_names,journey,correction_nogoods,watch_tags}, DeviationIncident.occurrence_conditions, CompassEntry/Provenance, coast_receipts, static_overlays, TheoryAttemptReceipt, AttemptInterpretation, _RelationalRefinementReceipt.

| Item | Status | Recommendation |
|---|---|---|
| `CoastReceipt.macro_folds` (`coast.py:116`), `.timer_quanta_replayed` (`:122`) | aggregated + debug log only | fold into `_knowledge_payload` perf receipts or delete |
| `CoastReceipt.budget` (`coast.py:114`) | DEAD | delete |
| `EarnedWorkReceipt.source_mark` (`earned_work.py:116`) | DEAD (progress uses locally-built landing_mark) | delete |
| `_HoldLogEntry.tags` property (`types.py:734-737`) | DEAD (recording iterates pilot_rungs directly) | delete |
| `PilotGateEvent.evidence` (`types.py:587-595`) | written 11× in verify; read ONLY by test_pilot_verify (5 asserts); tumbler skeleton keeps only ("event","detail") (`tests/tumbler/skeleton.py:146`) | docstring claim ("durable machine-readable ground used by decision skeletons") is FALSE — wire into skeleton/rejection payloads or delete field + 11 sites |
| `_AttemptResult.stall_pending` (`types.py:1232`) | read nowhere in src (1 test) | docstring says loop reads it; it does not — wire into CoastObservation at `pilot.py:4916` or delete |
| `_PilotState.last_wait_log` (`types.py:873`) | only write is `= None` (`pilot.py:5771`); no read anywhere | delete |
| `DeviationIncident.occurrence_writer` (`types.py:556`) | never assigned, never read | delete |
| `PilotRungExecutionState` non-EFFECTIVE states (`overlay.py:61-68`) | prod reads only .EFFECTIVE (`overlay.py:97`); 4 states asserted only in test_pilot_plc_primitives:477-481 | render "shadowed hold" in recording, or collapse to `effective: bool` |
| `TheoryReceipt` | written `:1306`, read `:1030` + tests | keep (proof artifact); no rendering reader |
| `TheoryTombstone`/`ledger.tombstones` | written `:1326-1339`; read only tests | give reader — natural: theory_view/options suppression of tombstoned experiment (plan:280); otherwise "do not repeat" is UNENFORCED |
| `TheorySuccessor`/`ledger.successors` | written `:1033-1036`; read only tests | recording regression prose, or delete until Stage 9 |
| `NogoodProof`/`ledger.nogood_proofs` | never written, never read, not even tests | DELETE (plan:283 reserves the concept; empty PMap is a placeholder) |
| `UnattributedTheoryEvidence`/`ledger.unattributed` | 5 production writers (`working_theory.py:1096-1104`; `pilot.py:3465,3757,3863,3893…`), ZERO readers | biggest unread record — natural reader `recording.py::_knowledge_payload` (`pilot.py:5207`) so "proof withheld: N active requirements" reaches `how` prose |
| `TheoryView.attempts` | not read in src | delete or wire to first-edge suppression |
| `TheoryView.first_edge_exclusions` + `excludes_first_edge()` | no production caller (plan:513 says deliberately off in 6A) | explicit charter exemption or delete until it lands |
| `TheoryView.claim`/`.source` | unread | delete from view (stay on ledger) |
| `close_intrascan` subsystem (`IntrascanClosureResult`/`Question`/`WITNESS`, `intrascan.py:313,340,193,796`) | no production caller — ~400 lines test-only | plan:189-193: "production-inert laboratory", WITNESS contract "not the production target" — delete or move to tests/; biggest single block of unread machinery |
| `IntrascanRequirementObservation`/Evidence/Disposition | consumed only inside closure path | dies with close_intrascan |
| `_RequirementRepairResult.declined` (`pilot.py:506`) | no reader | delete field |

## Table 3 — Single-consumer cross-module extractions

Whole modules with one consumer:
| Module | Verdict |
|---|---|
| attempt_interpretation.py (302) | keep — Stage-5 split exemplar |
| cyclefold.py (675) | keep — exactness contract |
| departure.py (674) | keep — the evidence/verdict split for channel motion |
| orientation.py (970) | keep — facade seam |
| physical.py (42) | INLINE — one function, one caller |
| pulse.py (48) | INLINE — one private function, sole consumer is investigation_replay (steer does NOT import it); CLAUDE.md:527 listing it as a peer of overlay/coast is wrong |
| requirement_recovery.py (122) | keep logic; DELETE the 4-symbol `__all__` pass-through re-export block (`:31-36`) — pilot.py:125 imports through it instead of from intrascan_schedule |
| recording.py / multitarget.py / avoid.py | keep |

Private symbols crossing module lines (92 total). Key ones:
- `availability.{_GUARD_CONTRADICTION,_equality_gated_coil,_reduce_guard_by_pin,_reduce_guard_by_fire_pins,_writer_availability}` → sole consumer trace.py → INLINE BACK into trace.py (only `_WriterAvailability` is genuinely shared with options)
- `progress.{_anchor_bearing_receipt,_anchor_frame_receipt,_install_confirmed_correction,_monitor_trend,_promote_probationary_corrections,_record_pending_landing}` → all pilot.py — the de-facto public progress API; rename public and declare, or pilot/progress are one module in two files
- `recording.{_act_event,_build_plan_journal,_candidates_built_payload,_frontier_clause,_iteration_payload,_knowledge_payload}` → all pilot.py — make public
- `steer.{_coast_to_bearing,_terminal_target_trigger}` → investigation_replay — LAYERING INVERSION; move shared coast entry into coast.py
- `trace._can_produce` → investigation_replay; `trace.{_constraint_atom,_inequality_levers}` → corrections; `trace._route_forced_names` → pilot; `trace._route_has_no_dead_end` → correction_candidates — promote/inline
- `tide_tables.{_guard_operand_domain,_read_table}` → promote
- `compass._action_sort_key` → skiff (move there)
- `overlay._constraint_condition` → promote
- `static_expressions.{_channel_constraint,_channel_from_values}` → inline
- `options._holds_defeat_needed` → correction_candidates — inline
- `pipeline_graph.{_applied_key,_canonical_applied,_context_value_key}` → all compass.py — IDENTITY functions living in a graph module → world_key.py (pattern 7)

Identity/budget vocabulary duplicates: `_SKIFF_MAX_PROBES` 16 vs 8; applied-artifact canonicalization (pipeline_graph vs `compass.py:172-176`); act identity split across navigation_contracts vs world_key; FOUR identities for ActiveRequirement (`identity:427`, `navigation_identity:381`, `_scheduled_condition_identity:178`, `TheoryRequirementSnapshot.semantic_identity`).

## Table 4 — Soundness leaked outside the kernel set

| Module | Leak | Why soundness | Fix |
|---|---|---|---|
| avoid.py (64) | `_avoid_violations`/`_avoid_forces`/`_avoid_snap_names`/`_hold_allowed` — the primitive behind EVERY avoid gate; every function getattr-duck-typed and SWALLOWS EVERY EXCEPTION (`avoid.py:20,24` → `return ()` = FAIL-OPEN) | delete → steer/verify/orientation/options/departure/intrascan all silently pass; how() can enter a forbidden state. A raising avoid predicate currently reports "no violation" — worst leak in the package | kernel (fold into verify or kernel constraints.py); fail closed |
| coast.py (1026) | `CoastSession.seek` sole enforcer of avoid across LOGICAL scans of a pre-commit trial; `_COAST_BUDGET` only bound on bearing coast | delete → trial coast folds through avoided crossings unobserved | kernel (or avoid evaluation moves to kernel gate) |
| requirement_recovery.py (122) | `active_requirement_violations` called from `verify.py:1019` (acceptance gate); `actions_preserve_active_requirements` from `orientation.py:81` (admission gate) | delete → verify accepts landings that destroy a proved requirement | gates → verify.py + orientation.py |
| progress.py (2019) | owns load_world, pilot_rungs=, checkpoints mutation, correction install/revoke, pending-departure lifecycle | charter kernel list omits the second-largest mutator — single biggest charter/reality gap | declare progress kernel, or split pure classify_retention (instrument) + kernel applier |
| recovery.py (292) | `assert_recovery_disposable_state`/`assert_recovery_inactive`/`register_disposable_state` — ContextVar capability token (`recovery.py:87`) | delete asserts → commit (`pilot.py:4541`), transition (`:4800`), correction install (`progress.py:1195`), skiff (`:93,227`) reachable from inside a disposable transaction = world corruption | invariant-assertion half → kernel; token passed, not ambient |
| effects.py | `terminal_target` displacement veto (`effects.py:118-122`) prevents accepting a displaced target | deleting effects removes a soundness veto along with observation | move veto → verify.py |
| world_key.py | `_pilot_world_key` (`:163-171`) getattr chain `navigation_identity`→`identity`→object | requirement type without either attr silently keys on the object → identity collision → nogoods leak between worlds | typed protocol, fail loud |
| earned_work / tide_tables / multitarget / cyclefold | NOT leaks (degrade to fewer accepts/rejections = sound; tide_tables completeness check is do-not-touch) | — | stay |

## Resists the pattern (charter input)

1. `_PulseState` (`types.py:1042-1138`) — mutable execution-lifetime evidence buffer, frozen into `_ExecutionEvidence` at accept. THIRD CATEGORY: name the buffer→receipt transition explicitly.
2. `CompassEntry`+`Provenance` — entries are DEMOTED (OBSERVED→CONTRADICTED), not superseded. `*Knowledge` records are revisable-by-evidence; immutability belongs to the causing observation. Legend needs a `*Entry` row.
3. `recovery.py` `_ACTIVE_RECOVERY` ContextVar — capability token: kernel concept, must be passed, not ambient.
4. `_AvoidPredicate`/`_AvoidMember` (`types.py:192-239`) — user constraints: never expire, never discharge, but MUST evaluate fail-closed (avoid.py violates).
5. `DeviationIncident` — transient evidence window, no identity outside its investigation. Legend: `*Incident`.
6. intrascan closure half — "an inert laboratory is not a charter exemption; it is either promoted or deleted."
7. "Two consumers or stay inline" boundary: a module earns existence by (a) reuse, (b) an exactness contract, or (c) a declared ownership seam. physical.py and pulse.py have none; multitarget.py has (b).

## Sizing + order

| Cat | Items | Diff | Risk | Gate |
|---|---|---|---|---|
| A. Delete dead records/fields (NogoodProof+map; last_wait_log; occurrence_writer; _HoldLogEntry.tags; CoastReceipt.budget; source_mark; declined; TheoryView.claim/source/attempts) | 8 | ~120 lines | none | test-pilot |
| B. TEST-ONLY: reader or delete (PilotGateEvent.evidence; stall_pending; PilotRungExecutionState; tombstones; successors; first_edge_exclusions) | 6 | ~150 | low | divergence + golden if wired |
| C. ledger.unattributed gets a reader | 1 | ~40 in recording | low (additive) | golden regen |
| D. Extract close_intrascan lab | 1 | ~400 moved | low (inert) | test-pilot |
| E. Missing-expiry bills (ActiveRequirement INVALIDATED/AMBIGUOUS writers + deadline; PendingDeparture reject-arm expiry; 6 silent =None get events) | 3 arcs | ~250 | HIGH — changes decisions/event order | full suites + goldens |
| F. PilotRung supersession/revocation receipts | 1 arc | ~200 | HIGH — overlay world identity | goldens + divergence |
| G. Inline single-consumer (physical, pulse, requirement_recovery re-exports, availability's 5, _holds_defeat_needed, _channel_*) | 6 | ~350 moved, net −60 | low | divergence per file |
| H. Promote private cross-module APIs (~20 renames) | 20 | ~200 edits | none | lint + test-pilot |
| I. Identity moves (pipeline_graph identity fns → world_key; act_identity location; _SKIFF rename; world_key getattr fix) | 4 | ~120 | low-med | test-pilot |
| J. Soundness relocation (avoid→kernel fail-closed; requirement_recovery gates→verify/orientation; recovery asserts→kernel; effects veto→verify; progress declare-or-split) | 5 arcs | ~600 | HIGHEST — the kernel boundary itself | full suite + goldens + watchers |

Order: A → B/C → G → H → I → D → E → F → J. A–I decision-identical (divergence gates); E/F/J ride goldens.
Single most urgent: avoid.py fail-open handlers (`avoid.py:18-25`).
