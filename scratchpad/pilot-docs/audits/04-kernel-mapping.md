# Audit 4 — Kernel/instruments/probes mapping + the pilot.py cut

Opus agent audit, 2026-08-10.

## Charter facts to correct

- `StepEvidence`/`classify_step` (program_step) and `PRECEDENCE` (orientation) and `ForkBudget` do not exist in code (grep = 0 hits). Only existing evidence/verdict split: `departure.py:113 DepartureObservation` / `departure.py:616 classify_departure`. program_step and orientation are TARGETS, not exemplars.
- `orientation.py:621-849 _orient_read` is exactly the statement-order cascade the charter calls a bug: 8 return sites over 230 lines, no precedence tuple.

## Table 1 — All 48 files (Mut = mutates _PilotState/_World/CompassKnowledge; Fork; Chooses = mints NavigationAct/Bearing or selects installable artifact)

| File | LOC | Class | Mut | Fork | Chooses | Destination |
|---|---|---|---|---|---|---|
| pilot.py | 6516 | SPLIT | Y | Y | Y | kernel/drive + 4 evictions |
| trace.py | 4758 | instrument | N | N | N | instruments/trace |
| requirements.py | 2521 | instrument | N | N | N | instruments/requirements |
| options.py | 2188 | SPLIT | N | Y (via probe `:1123`) | wait-source | instruments/options + declared probe call |
| corrections.py | 2146 | instrument (proposer) | N | N | hypotheses | instruments/corrections |
| progress.py | 2019 | KERNEL | Y (21 sites) | Y `:1280` | Y (installs/reverts) | kernel/commit + evict departure/correction policy |
| effects.py | 1878 | instrument | N | N | N | instruments/effects |
| intrascan.py | 1413 | SPLIT | N | Y `:1137` | N | instruments/intrascan (report) + probes/intrascan_closure |
| investigation_replay.py | 1391 | probe | N | Y `:763,:1240` | N | probes/replay |
| working_theory.py | 1343 | kernel (ledger) | value-only | N | N | kernel/ledger |
| types.py | 1259 | SPLIT | Y (setters) | Y `:972,:1010,:1024` | N | kernel/world + shared contracts |
| investigate.py | 1208 | probe (orchestrator) | N | indirect | correction disposition | probes/investigate |
| verify.py | 1195 | KERNEL | N | N | verdict | kernel/verify |
| steer.py | 1096 | KERNEL | Y `:113-114` | Y ×4 | N | kernel/execute |
| coast.py | 1026 | kernel (exec primitive) | fork only | N | N | kernel/execute |
| compass.py | 983 | SPLIT | N (persistent) | N | N | kernel/knowledge + facade |
| orientation.py | 970 | MISFIT → kernel | N | N | Y (`:585`,`:604+`) | kernel/decide |
| recording.py | 922 | render | N | N | N | render/ (no charter bucket) |
| pipeline_graph.py | 793 | instrument | N | N | N | instruments/ |
| evidence.py | 788 | instrument | N | N | N | instruments/ |
| tide_tables.py | 759 | instrument | N | N | permanent reject | instruments/ (soundness-bearing) |
| cyclefold.py | 675 | kernel (exec) | fork only `:547` | N | N | kernel/execute |
| departure.py | 674 | SPLIT | N | Y `:144` | N | probes/observe_departure + instruments/classify |
| correction_candidates.py | 671 | instrument (proposer) | N | N | ranks | instruments/ |
| earned_work.py | 598 | instrument | N | N | N | instruments/ |
| causal.py | 596 | instrument | N | N | N | instruments/ |
| program_step.py | 587 | PROBE (licensed) | N | Y `:262,:290,:327` | N | probes/program_step |
| skiff.py | 568 | PROBE (licensed) | N | Y `:131` | proposes | probes/skiff |
| overlay.py | 553 | kernel (fork factory) | PLC only | Y (IS the factory) | N | kernel/fork |
| awaited_actions.py | 409 | instrument | N | N | N | instruments/ |
| navigation_contracts.py | 390 | vocabulary | N | N | N | kernel/contracts |
| availability.py | 379 | instrument helper | N | N | N | instruments/ (sort key only) |
| bootstrap.py | 335 | instrument | N | N | designation | instruments/ |
| static_expressions.py | 327 | instrument helper | N | N | N | instruments/ |
| attempt_interpretation.py | 302 | instrument | N | N | N | instruments/ (misfit signature) |
| outcome.py | 298 | instrument | N | N | N | instruments/ |
| constrained_reachability.py | 297 | instrument | N | N | N | instruments/ |
| recovery.py | 292 | kernel (guards) | N | N | N | kernel/guards |
| intrascan_schedule.py | 287 | instrument (pure) | N | N | N | instruments/ |
| multitarget.py | 178 | instrument (pre-pass) | N | N | order | instruments/ |
| world_key.py | 173 | kernel (identity) | N | N | N | kernel/identity |
| refinement.py | 162 | probe | N | indirect | nominations | probes/ |
| advance.py | 128 | instrument | N | N | N | instruments/ |
| requirement_recovery.py | 122 | facade (misfit) | N | N | N | delete or fold |
| avoid.py | 64 | instrument helper | N | N | N | kernel/verify support |
| pulse.py | 48 | kernel (exec) | fork only | N | N | kernel/execute |
| physical.py | 42 | kernel (setup) | PLC only | N | N | kernel/setup |
| __init__.py | 14 | exports | N | N | N | root |

Totals: kernel 15, instruments 22, probes 6, SPLIT 6, render 1.

## Table 2 — pilot.py dissection (AST-verified regions)

| # | Responsibility | Lines | Class | First cut? |
|---|---|---|---|---|
| 1 | imports (28 intra-package) | 1-230 | — | stays |
| 2a | Requirement/receipt retention+derivation (`_retain_active_requirement`, `_derive_bootstrap_requirements`, `_derive_attempt_requirements:358`, `_retain_expectation_receipt:425`) | 231-497 | kernel(appends)+instrument | YES → requirement_repair.py |
| 2b | Disposable repair state, knowledge copy, exact sources | 499-745 | kernel | YES |
| 2c | `_nested_guard_act:745`, `_mandatory_guard_blocker:949`, `_rebound_bearing:1021` | 745-1053 | CHOOSES (2nd Bearing minter) | YES |
| 2d | Schedule compile / correction record / bootstrap repair | 1054-1279 | kernel | YES |
| 2e | Program-guard rebase from history (forks `:1399`) | 1279-1685 | probe | YES |
| 2f | `_repaired_program_continuation` (forks `:1753,:1888`) | 1685-2317 | probe+kernel | YES |
| 2g | `_repair_failed_requirement:2318` / `_repair_one_active_requirement:2567` — RE-ENTERS `_transition_once` at `:1606`,`:2412` | 2318-2612 | SECOND DRIVE LOOP | YES |
| 2h | `_execute_bootstrap_scan` | 2613-2700 | kernel | YES |
| 3a | `_DriveSetup`/`_DriveOutcome`/`_IterationTransition` | 2703-2789 | contracts | YES → types.py (prereq) |
| 3b | Optional/controlling theory transitions (30 fns; mutation only `:3356,:3364`) | 2790-3965 | ledger driver | YES → theory_drive.py |
| 3c | `_ProverContext` | 3968-3974 | contract | YES → context.py |
| 4 | `_commit_step` | 3982-4025 | kernel-core | stays |
| 5 | Context build (`_make_pilot_context`, `_prepare_drive`, `_prepare_target_context`, `_infer_pipeline_roles_for_context`, `_build_static_transition_graphs_for_context`) | 4027-4244 | setup | YES → context.py |
| 6 | `_with_avoid_reason`, `_stopped_reason`, `_avoid_route_names` | 4245-4347 | render | YES → recording.py |
| 7 | `_record_attempt` (sole knowledge-commit `:4369`) | 4348-4383 | kernel-core | stays |
| 8 | `_resolve_excursion` | 4384-4426 | kernel-core | stays |
| 9 | `_step_context` | 4427-4467 | extractable | no |
| 10 | `_adopt_trial`, `_monitor_committed_trial` | 4468-4534 | kernel-core | stays |
| 11 | `_commit_trial` (world commit `:4576`) | 4535-4604 | kernel-core | stays |
| 12 | `_prepare_oriented_result` | 4605-4630 | kernel-core | stays |
| 13 | `_certify_current_target_prefix` — forks `:4673,:4725`, `.step()`, calls read_program_step | 4631-4774 | PROBE | YES → probes/target_prefix.py |
| 14 | `_transition_once` | 4775-4995 | kernel-core | stays |
| 15 | `_adopt_deferred_transition` | 4996-5024 | kernel-core | stays |
| 16 | `_selected_terminal_target_expectation`, `_promote_transient_target_failure` | 5025-5189 | instrument+kernel | no |
| 17 | `_finished_event`, `_stuck_event`, `_stopped_events` | 5190-5279 | render | YES → recording.py |
| 18 | `_pilot_loop_events` (drive loop) | 5280-5810 | kernel-core | stays |
| 19 | `_pilot_loop` | 5811-5850 | kernel-core | stays |
| 20 | Failure diagnostics + route prep (`_prepare_route:6018`, `_build_route_taken`, `_report_selected_route`) | 5857-6069 | instrument+render | YES → context.py/recording |
| 21 | `_build_prover_context` | 6075-6166 | setup | YES → context.py |
| 22 | Public API (target parsing, `pilot_events:6305`, `pilot_how:6331`, multi-target) | 6167-6516 | API | optional → api.py |

## Table 3 — Mutation and fork discipline violations

| # | Site | Violation | Fix |
|---|---|---|---|
| 1 | `steer.py:113-114` `_install_prerequisites` | executor writes `_World.pilot_rungs`+`hold_log` on LIVE state before verification; rejected trial leaves overlay installed | return prerequisites on `_AttemptResult`; kernel commits at `_commit_trial` |
| 2 | `pilot.py:1606,:2412` | repair re-enters `_transition_once` — second self-policed drive loop | Stage 7 target; extract first, delete second |
| 3 | `pilot.py:1044 _rebound_bearing` | second Bearing-minting site (other: `orientation.py:585`); binds a RETAINED act to a fresh read — breaks "no future Bearing survives an observation" | orientation re-resolves from theory receipt (Stage-6A pattern) |
| 4 | `departure.py:144 _settle_departure` | nominal reader forks + bounded coast, no declared budget | probe (`ForkBudget(forks=1, scans=landing_cap)`); classify stays instrument |
| 5 | `options.py:1096-1123` | instrument calls read_program_step → ≤3 forks per orientation, self-policed | kernel supplies the reading, or options declares aggregate budget |
| 6 | `intrascan.py:1124-1140` | report-only module forks+steps (production-inert, contract crossed) | closure → probes/; inspect/derive stay instrument |
| 7 | `investigation_replay.py:763,:1240` | replay forks via progress, self-policed | probes/ with declared budget |
| 8 | `pilot.py:4631-4774` | probe embedded in commit path | probes/target_prefix |
| 9 | `pilot.py:1399,:1753,:1888` | repair-block forks | follows #2 |
| 10 | `progress.py:1213,:1441,:1280` | pilot_rungs written directly + fork during correction install/revoke | legitimate IF progress is kernel — kernel-owns-mutation today = pilot.py + progress.py + one line of steer.py |
| 11 | progress.py 19 PilotEvent constructions + yields | rendering fused into mutation owner | event shapes → recording.py; progress returns facts |
| 12 | `orientation.py:621-849` | statement-order precedence | PRECEDENCE tuple + one gate |
| 13 | 61/51/37 getattr in corrections/requirements/pilot | pattern 3 at scale | typed fields |

Compliant, verified: `Compass.apply` IS the sole knowledge write path — exactly 6 sites (`pilot.py:4369,4916,4944,5559,5560`, `progress.py:1974`), all in the two mutation-owning modules. `CompassKnowledge` persistent (`compass.py:802` `.set`).
Do readers choose? For navigation acts CLAUDE.md holds: `Bearing(` at exactly 2 sites (`orientation.py:585`, `pilot.py:1044`), `ActPolicy(` only orientation (7) + one default. Skiff returns CompassObservation only. But four OTHER decisions live outside orientation: `options._select_wait` (wait source), `verify.verify_gates` (accept/reject), `investigate._resolve_replay_attempt` (correction disposition), `progress._apply_departure_decision`/`_investigate_and_revert` (retain/revert/install). "Readers contribute facts, none chooses" is true only of the act.

## The minimal behavior-identical first kernel cut

Pure file moves, no logic edits. One branch, six commits:

- Commit 0 (prereq, ~40 lines): `_DriveSetup`/`_DriveOutcome`/`_IterationTransition` (`pilot.py:2704-2760`) + `_ProverContext` (`:3968-3974`) → types.py.
- Commit 1 — requirement_repair.py ← pilot.py:231-2700 (~2470 lines). Exports 15 names (xref-proven): `_configured_input_names, _release_attempt_projections, _derive_attempt_requirements, _retain_expectation_receipt, _exact_failed_source, _repaired_program_continuation, _promoted_target_suffix_observation, _adjacent_continuation_source, _exact_local_repair_window, _program_step_from_bearing, _preempt_recovery_action_with_program_coast, _execution_epoch_owner, _advance_recovery_continuation, _repair_one_active_requirement, _execute_bootstrap_scan`. One non-mechanical detail: calls `_transition_once` (`:1606,:2412`) which stays in pilot.py → function-local import inside those two functions (idiom already used 20+ times in the package).
- Commit 2 — theory_drive.py ← pilot.py:2790-3965 (~1180 lines). 16-name interface. Mutation only at `_record_optional_theory_fact:3356` / `_record_controlling_theory_fact:3364` (`state.theory_state = reduce_theory(...)`).
- Commit 3 — context.py ← pilot.py:4027-4244 + :6075-6166 + :5914-6069 (~410 lines). No mutation, no forks.
- Commit 4 — probes/target_prefix.py ← pilot.py:4631-4774 (~145 lines). First module written to the probe contract; first `ForkBudget(forks=2, scans=1)`.
- Commit 5 — recording ← pilot.py:4245-4347 + :5190-5279 (~200 lines) into recording.py (already owns 14 of 43 PilotEvent sites).

Stays: kernel ~2000 lines (`_commit_step, _record_attempt, _resolve_excursion, _step_context, _adopt_trial, _monitor_committed_trial, _commit_trial, _prepare_oriented_result, _transition_once, _adopt_deferred_transition, _pilot_loop_events, _pilot_loop`, public entry). Optional api.py split → ~1650-line kernel.

Alignment: Stage 7 "replace `_repair_one_active_requirement` and `_nested_guard_act`" — both entirely in Commit 1's region. Stage 9 competing-loop deletion becomes a one-file diff. `_transition_once` target contract satisfied structurally by Commits 1+2.

Logistics before it: (1) Commit 0 first; (2) 13 test files import private names from pilot.pilot (`_build_prover_context` ×5, `_transition_once`, `_record_attempt`, `_resolve_excursion`, `_commit_trial`, `_derive_attempt_requirements`, `_disposable_requirement_state`, `_copy_repair_knowledge`, `_mandatory_guard_blocker`, `_infer_pipeline_roles_for_context`, `_bootstrap_local_designation_survived`) — re-export shims keep the cut invisible; ONE monkeypatch (`pilot_module, "_record_controlling_theory_fact"`) MUST be retargeted to theory_drive (a shim will not intercept it); (3) the function-local import in Commit 1.
Must NOT land before it: Stage 6B (would double the extraction diff and add logic to the block Stage 7 deletes).
Gate per commit: `devtools/pilot_divergence.py --target y_BurnerLoop --golden tests/tumbler/golden/how_y_burnerloop_skeleton.json` then `make test-pilot`.

## Registry sketch

```
name:        program_step
question:    "Can this exact producer keep running under unchanged controls?"
inputs:      plc, pilot_rungs, producer: TraceNode, resting, projection_scans   # typed
reading:     ProgramStep(status: KEEP_RUNNING|NEEDS_INPUT|INTERRUPTED|UNCLEAR, ...)
budget:      ForkBudget(forks=3, scans=projection_scans + len(required_inputs))
owner:       probes/program_step.py
consumers:   options._read_route_and_wait, attempt_interpretation, recording
expiry:      one orientation
may_reject:  no        # permanent rejection needs a complete finite domain
```
`budget: NONE` marks an instrument; non-NONE forces probes/ and kernel enforcement.

Maps cleanly (22 instruments + 5 probes). Misfits (11):
1. orientation — mints the act; needs a kernel/decide seat in the charter.
2. options — composes readings, calls a probe, chooses wait source.
3. recording — pure render; target tree has no bucket.
4. types/navigation_contracts/world_key — vocabulary, not readers (pattern 7 governs).
5. attempt_interpretation — reads an `_AttemptResult`, not `(world,target,board)`; widen contract to `read(evidence) -> Reading` or add interpreters/.
6. departure/intrascan — one file, two contracts; registry needs one entry per FUNCTION (contradicts "a file plus one registry entry").
7. corrections/correction_candidates/investigate/refinement propose installable PilotRungs — "only skiff may propose" must be narrowed to NAVIGATION acts (recommended) or add proposers/.
8. requirement_recovery — facade; delete, not an entry.
9. availability/static_expressions/avoid — sub-instrument helpers, no standalone question.
10. recovery — invariant guard, kernel policy.
11. coast/pulse/cyclefold/overlay/physical — execution primitives; charter kernel list omits an execution slot; add kernel/execute.
