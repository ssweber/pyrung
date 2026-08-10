# Audit 1 — Suffix legend (pattern 6) + identity vocabulary (pattern 7)

Opus agent audit, 2026-08-10. Grounds CHARTER.md patterns 6-7 in pilot/ code.
306 classes enumerated via AST; 55 carry a legend or near-miss suffix.

## Table 1 — Legend-suffixed types

| Type | file:line | Frozen | Compliant? | Fix |
|---|---|---|---|---|
| `CompassObservation` | `compass.py:137` | Y | Y — detached, only `Compass.apply` promotes (`compass.py:922`) | OK |
| `ActionNogoodObservation` | `compass.py:180` | Y | Y — consumed only at `compass.py:792` | OK |
| `ProbeExhaustedObservation` | `compass.py:188` | Y | Y — `compass.py:800` | OK |
| `CoastObservation` | `compass.py:195` | Y | Y — `compass.py:804` | OK |
| `StaticEdgeObservation` | `compass.py:203` | Y | Y — `compass.py:811` | OK |
| `DepartureObservation` | `departure.py:113` | Y | N — never reaches `Compass.apply`; consumed by `classify_departure` (`departure.py:616`), stored on `PendingDeparture.opening` (`progress.py:325`). Settled evidence, not promotable | RENAME → `DepartureEvidence` |
| `EffectObservation` | `effects.py:217` | Y | N twice — never promoted (consumed by `verify.py:96`, requirements, `intrascan.py:543`); retains a LIVE `execution_projection: ScanRungWriteProjection` (`effects.py:233`), so not detached. `EffectObservationSnapshot` (`effects.py:202`) is the detached twin | RENAME → `EffectReading` / keep `…Snapshot` as receipt |
| `IntrascanRequirementObservation` | `intrascan.py:242` | Y | N — detached but never promoted; per-scan disposition record | RENAME → `…RequirementReading` |
| `CoastReceipt` | `coast.py:95` | Y | Y — values-only, "safe to carry across reverts" | OK |
| `EarnedWorkReceipt` | `earned_work.py:110` | Y | Y | OK |
| `ExpectationReceipt` | `requirements.py:541` | Y | Y (soft) — settled + full identity (`:580`); retains live `local_bearing`/`source_checkpoint` as `compare=False` proof handles (`:553-556`) | OK (note live handles) |
| `FailedEffectReceipt` | `requirements.py:279` | Y | Y — past fact + `identity` (`:299`) | OK |
| `TheoryAttemptReceipt` | `working_theory.py:185` | Y | Y — detached identities only | OK |
| `TheoryReceipt` | `working_theory.py:256` | Y | Y | OK |
| `OperationReceipt` | `overlay.py:24` | Y | N — `until` is a FUTURE handoff boundary the overlay must still reach. A bill wearing a receipt name | RENAME → `OperationLifetime` (or `…Expectation`) |
| `_CorrectionReceipt` | `types.py:754` | Y | N (soft) — mutable lifecycle (`CorrectionStatus` PROBATIONARY→ACTIVE→REVOKED, `types.py:740`) rewritten via `replace` at `progress.py:1265`. A receipt does not get revoked | SEMANTIC — split settled `_ConfirmedCorrection` receipt from a `CorrectionInstallation` bill |
| `_RelationalRefinementReceipt` | `refinement.py:43` | N | N — plain `@dataclass` with mutating `admit()` and a `set` (`:51-56`). A budget counter, not a receipt | RENAME → `_RefinementBudget` |
| `EffectExpectation` | `effects.py:136` | Y | Y — bill; discharged by `observe_execution_window`, exempted via `ExpectationExemption` (`navigation_contracts.py:113`, enforced `ActPolicy.__post_init__:162`) | OK — the model bill |
| `ActiveRequirement` | `requirements.py:351` | Y | N — exactly ONE discharge site (`pilot.py:3709`, Stage-6A retry only), NO expiry path. `RequirementStatus.INVALIDATED`/`AMBIGUOUS` (`requirements.py:85-86`) never assigned anywhere; list at `types.py:848` append-only (`pilot.py:304`, `progress.py:281`), no remove/pop/clear | SEMANTIC — add expiry + invalidation |
| `RetryTogetherRequest` | `working_theory.py:243` | Y | Y — bill with proof (`pilot.py:3670`) and abandon (`AbandonTheory`) | OK |
| `NogoodProof` | `working_theory.py:278` | Y | N — NEVER constructed anywhere (0 hits in src/ and tests/). `TheoryLedger.nogood_proofs` (`:323`) permanently empty | SEMANTIC — wire a minter behind `tide_tables.guard_verdict`'s completeness gate, or delete |
| `_SpinVerdict` | `verify.py:85` | Enum | N — `EXCURSION` is explicitly "orchestration acts elsewhere", i.e. not terminal; no complete domain required | RENAME → `_SpinJudgment` |
| `CompassKnowledge` | `compass.py:495` | Y | Y — all-persistent, world-key scoped (`:498`) | OK |

Adjacent verdict-shaped values for the same sweep:
- `IntrascanClosureStatus.IMPOSSIBLE` (`intrascan.py:199`) — declared terminal, never returned (only ref is its own docstring `:341`). Same hole shape as `NogoodProof`.
- `tide_tables.GUARD_DEAD` (`tide_tables.py:332`) — the one genuinely complete-domain verdict, properly gated (`:410` returns `GUARD_PUNT if saw_unknown`). Bare `str` constant — deliberate compact contract.
- Charter reference example does not exist: pattern 1 cites `program_step (StepEvidence / classify_step)`. Neither symbol exists in the repo. Nearest real instance: `departure.py` (`DepartureObservation` + pure `classify_departure` → `DepartureResult`).

## Table 2 — Unsuffixed / near-miss types that should adopt a legend suffix

| Type | file:line | Why | Proposed | Fix |
|---|---|---|---|---|
| `NavigationEvidence` | `constrained_reachability.py:127` | Not a record — bare namespace of `@staticmethod` gates | `…Admission` / module functions | RENAME |
| `TransitionEvidence` | `evidence.py:719` | Not a record — stateful adapter over `ExploreContext` | `TransitionFacts` | RENAME |
| `PendingDeparture` | `progress.py:310` | Textbook bill: `expires_at_search_scan` + PROMOTE + EXPIRE | `…Expectation` | RENAME (see resists #5) |
| `RevisitCredential` | `types.py:100` | One-shot consumable bill | `…Expectation` | RENAME |
| `PilotOverlayExecution` | `overlay.py:87` | Docstring: "Effective-ownership receipt" | `…Receipt` | RENAME |
| `_RecoveryContinuation` | `types.py:806` | Docstring: "No future action … belongs to this receipt" | `…Receipt` | RENAME |
| `_ExecutionEvidence` | `types.py:660` | Frozen, PLC-free, past-only, MappingProxy-hardened (`:680`) | `…Receipt` | RENAME |
| `_CommittedAct` | `types.py:710` | Settled past act + steps | `…Receipt` | RENAME |
| `_HoldLogEntry` | `types.py:727` | "append-only, survives reverts" | `…Receipt` | RENAME |
| `CoastTriggerEvent` | `coast.py:85` | "One pen mark: a trigger firing at an exact scan" | `…Receipt` (or keep Event) | RENAME |
| `SkiffResult` | `skiff.py:39` | Probe reading; carries LIVE `work: PLC` (`:51`) | `SkiffReading` | RENAME |
| `ProgramStep` | `program_step.py:69` | "Current-world proof result"; 4-valued incl. UNCLEAR | `ProgramStepReading` | RENAME |
| `TrialAssessment` | `outcome.py:60` | honest already | — | OK |
| `CandidateRead`, `WaitRead`, `RouteRead`, `PrerequisiteRead`, `LearnedBatchRead`, `CrossingBatchRead`, `_RouteAndCompletionRead` | `options.py:307,144,246,255,263,271,326` | instruments contract is `-> Reading` | `…Reading` | RENAME (8 files) |
| `IntrascanResult` / `…DraftOverlayResult` / `…GuardOverlayResult` / `…ClosureResult` | `intrascan.py:181,297,305,340` | report-only findings | `…Reading`/`…Finding` | RENAME |
| `_DeadEndResult`, `_AttemptResult`, `_RequirementRepairResult`, `InvestigationResult`, `ExcursionResult`, `DepartureResult`, `CompositionResult` | `verify.py:79`, `types.py:1205`, `pilot.py:500`, `investigate.py:184`, `investigation_replay.py:1127`, `departure.py:126`, `recovery.py:173` | generic `*Result` carries no legend meaning | per-case `…Reading`/`…Judgment` | RENAME |
| `CorrectionHypothesis` | `corrections.py:71` | explicitly unsettled — correct | — | OK |
| `IntrascanRequirementDisposition` | `intrascan.py:202` | docstring says "verdict", name says disposition | align docstring | RENAME (doc) |

## Table 3 — Identity near-duplicates

`world_key.py` owns `_pilot_state_key`, `_rung_identity`, `_pilot_world_key`, `_semantic_key`, `wait_edge_nogood`. `act_identity`/`pulse_identity` live in `navigation_contracts.py:359-365` (one def, 26 call sites). NO owner exists for `occurrence`.

| # | Near-duplicate | Sites | Consolidate onto | Mechanical? |
|---|---|---|---|---|
| 1 | `_pilot_world_key(snap,cfg,rungs,reqs)` hand-assembled — 31 sites, 7 files | `orientation.py:67,378,436`; `pilot.py:1407,2011,2295,2503,2828,4497,4759,4923,4935,5318`; `progress.py:833,1283`; `steer.py:240,270,457,480,517,658,676,817,831,953,967`; `verify.py:208,782,838`; `departure.py:526` | new `world_key.world_key_for(state, snap)` | Yes |
| 2 | 4th arg has three spellings: `state.active_requirements`, `getattr(state,"active_requirements",())` (17 sites: steer ×11, verify ×5, orientation, pilot), literal `()` | as above | same helper (kills 17 pattern-3 getattr defaults too) | Yes |
| 3 | Literal `()` requirement-free key = second unnamed world-key scope | `orientation.py:71`, `pilot.py:4922` (`proof_world_key`), `pilot.py:1411,4927` | named `proof_world_key(...)` constructor | No — genuine scope (resists #1) |
| 4 | Thin per-module wrappers | `verify.py:203 _executed_source_world_key`; `progress.py:1271 _checkpoint_with_pilot_rungs`; `pilot.py:2825 _theory_live_boundary` | fold into #1 | Yes |
| 5 | Occurrence identity — 3 incompatible tuples | `requirements.py:198 _scheduled_occurrence_identity` = `(kind,tag,dynamic_address)`; `pilot.py:2790 _theory_occurrence_identity` = `(kind,tag,scan_id,dynamic_address,values,enabled)`; `working_theory.py:489 theory_occurrence_token` = same fields reordered+tagged | new `occurrence_identity(occ, *, scan_scoped)` in world_key.py (or occurrence.py) | Yes for #2↔#3; #1 is deliberate scan-free variant → named flag |
| 6 | `dynamic_address` re-derived 3×, real divergence | owner `effects.py:165` (8-tuple); `working_theory.py:470` rebuilds via getattr; `pilot.py:1909` ADDS `scan_id`, DROPS `depth` (can alias subroutine depths) | `effects.EffectOccurrenceSnapshot.dynamic_address` | Yes for working_theory; pilot.py:1909 needs behavior check |
| 7 | `("execution-owner", id(epoch), id(query))` inline at 5 sites + hard-coded `len==3` check | `pilot.py:2816,2841,2931,3085,3324`; `working_theory.py:359`; `intrascan.py:1183`; validated `working_theory.py:547` | `execution_owner_token(epoch, query)` | Yes |
| 8 | `("checkpoint-owner", …)` THREE arities under one tag | 4-tuple `pilot.py:2814` & `working_theory.py:366`; 2-tuple `pilot.py:2929`; 3-tuple `pilot.py:3752` | one constructor | No→Yes — live crack: fix shapes, then consolidate |
| 9 | `("pair", pair)` nogood identity inline-built, structurally parsed elsewhere | built `pilot.py:4367`, `progress.py:1969`; parsed `compass.py:522-527` | `pair_identity()` beside `pulse_identity` | Yes |
| 10 | `TheoryObjectiveSnapshot` constructed verbatim 3× | `pilot.py:2856-2866`; `working_theory.py:383-390,658-665` | one factory in working_theory.py | Yes |
| 11 | `TheoryObligationSnapshot` constructed 2× with field divergence — `pilot.py:2869-2889` OMITS `projected_consumer` (defaults False); `working_theory.py:392-412` sets it. Equal claims can compare unequal | — | working_theory.py | Yes, but a latent bug fix |
| 12 | 5 ad-hoc attempt-identity tuples, 3 sharing one dedupe set | `pilot.py:1234` bootstrap, `:1594` rebase, `:2395` failed-effect (all keyed into `state.requirement_repair_attempts`); `pilot.py:3412 _shadow_attempt_identity`; `intrascan.py:1104 _closure_attempt_identity` | `repair_attempt_identity(kind,…)` for the first 3 | Yes for the 3; other 2 separate ledgers |
| 13 | Obligation identity across layers | `intrascan.py:1090 _effect_obligation_identity` (9-tuple) vs `TheoryObligationSnapshot` vs `EffectObligation` field-equality | effects.py should own one | No — resists #3-adjacent |
| 14 | `CausalOccurrence` (`investigation_replay.py:156`) = `(rung,tag,value,scan_id,occurrence_ordinal)` | recorded-history vocabulary | — | No — genuine boundary |

## Resists the pattern (charter input)

1. Requirement-free "proof" world key (`orientation.py:67`, `pilot.py:4922`): exactly TWO world-key scopes — navigable (nogoods/probes/coasts/cycles) and proof (permanent rejections). Both belong in world_key.py as named constructors; the literal `()` is what goes, not the second scope.
2. Scan-free vs scan-scoped occurrence identity: retry executes at a different absolute scan; requirement schedule must survive that, theory attempt must not. One constructor, explicit `scan_scoped` flag.
3. `CausalOccurrence` vs `EffectOccurrenceSnapshot`: recorded-history identity ≠ relocatable-projection identity. Do not merge (matches CLAUDE.md invariant).
4. `DepartureResult` must NOT become `DepartureVerdict`: honest UNKNOWN arm. `*Verdict` requires complete finite domain. Same for `_SpinVerdict`, `ProgramStepStatus`.
5. `PendingDeparture`/`EffectExpectation` already read as bills: suffix is mandatory only when the name misleads about authority; optional when the noun already names a bill.
6. `tide_tables` GUARD_* stay bare strings — CLAUDE.md compact contract, exempt.
7. `_CorrectionReceipt` lifecycle cannot be renamed away — real work in progress.py install/revoke; not in the rename sweep.

## Sizing

Rename-only sweep (pilot_divergence should be byte-identical):
- Core mis-suffix set (DepartureObservation, EffectObservation, IntrascanRequirementObservation, OperationReceipt, _RelationalRefinementReceipt, NavigationEvidence, TransitionEvidence, SkiffResult): 26 files (14 src + 12 test), ~170 refs. EffectObservation alone 65 refs/5 files → own commit.
- `*Read`→`*Reading` family: 8 files, ~90 refs.
- Table-2 adoptions: ~20 files, ~200 refs; almost all private except ProgramStep (10 files), DepartureResult.
- Total: ~30 src files, 4-6 commits (one suffix family per commit).

Semantic fixes (own tracked changes, NOT in the sweep):
- ActiveRequirement expiry/invalidation — 6 src files + tests. Highest risk: `status` is inside `navigation_identity` (`requirements.py:396`) and therefore inside the world key; new status values re-scope nogoods.
- NogoodProof minter or deletion — 2 files.
- _CorrectionReceipt bill/receipt split — 4 files.
- IntrascanClosureStatus.IMPOSSIBLE — 1 file.

Identity consolidation: 24 files. #1/#2/#4 fully mechanical one commit; #7/#9/#10 mechanical one commit; #5/#6/#8/#11 behavior-affecting, gate individually (#8/#11 are latent bugs, will legitimately change goldens); #3/#12 mechanical.
