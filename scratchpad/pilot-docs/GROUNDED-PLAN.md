# PILOT Charter — Grounded Plan (now → vision)

2026-08-10. Synthesis of five code audits (`audits/01-05`, file:line evidence there).
Companion to `CHARTER.md` and `working-theory-plan.md`. Everything below is grounded
in current HEAD; nothing is aspirational description of code that doesn't exist.

## Sequencing vs working-theory-plan.md

The two documents braid; drive from this one only until Stage 6B, then hand off:

1. **Phases 0-2 first** (mechanical, ~a week, tree green throughout). This is the
   charter's "kernel extraction before 6B" mandate; 6B done first would land new
   logic inside the region Commit 1 extracts and Stage 7 later deletes.
2. **Stage 6B next** — the product roadmap resumes as soon as Phase 2 lands.
   Phases 3-5 are filler: mechanical, mostly disjoint files, run them in gaps.
   One rule: Phase 3's theory-identity commits (C4-C5) land before 6B starts or
   after it lands, not during.
3. **Phases 6-7 before Stage 7** — the one deliberate interruption of the
   working-theory sequence. Stage 7 reroutes recovery through verify/orientation;
   reshape those first (evidence/verdict split, PRECEDENCE tuple) so recovery is
   rewired once, not twice.
4. **Stages 8-10 absorb Phases 8-9.** Those phases are the same items the
   working-theory plan already owns (ActiveRequirement lifecycle = Stage 8,
   progress-returns-facts = Stage 9, PilotRung receipts = Stage 10). At that
   point this document dissolves into the working-theory checklists; drive from
   one document only.

Exception to all sequencing: the `avoid.py` fail-closed fix (Phase 0) is a live
soundness bug and goes first regardless.

## What the audits established

1. **The charter is directionally right but three of its named references don't
   exist in code**: `StepEvidence`/`classify_step`, orientation `PRECEDENCE`, and
   `ForkBudget` are all zero-grep. They are *targets to build*. The real, completed
   exemplars of the charter's patterns are: `attempt_interpretation.py` and
   `earned_work.py` (evidence/verdict), `options._select_wait` and
   `investigate._initial_hypotheses` (nearest proposer/gate),
   `IntrascanClosureQuestion.budget` and `recovery.CompositionBudget` (declared
   budgets), `EffectExpectation` (the model bill), skiff probe rounds (the model
   enforced budget).
2. **The code is closer to the charter than expected in the load-bearing places**:
   `Compass.apply` verified as the sole knowledge write path (6 sites, all in the
   two mutation owners); `Bearing(` minted at exactly 2 sites; `CandidateRead`/
   `ActPolicy`/`navigation_contracts.py` already fully typed with zero optional
   probes; there is NO missing dependency behind any `getattr(ctx,…)` — every
   probed field is declared on the receiving type.
3. **And further in a few specific places**: `avoid.py` is fail-open (a raising
   avoid predicate reports "no violation") — the package's most soundness-critical
   primitive; `ActiveRequirement` has no expiry path at all; `PendingDeparture`
   expiry is unreachable on the reject arm; orientation is a 7-tier statement-order
   cascade with one admission expression copy-pasted 6×; `progress.py` is a
   full kernel member (2nd-largest mutator) the charter's kernel list omits.
4. **The pilot.py cut is cheap**: one branch, six mechanical commits, ~4,500 lines
   move, pilot.py → ~1,650-2,000 kernel lines. Nothing must land before it.

## Phase sequence

Charter process rules apply throughout: one branch = one pattern; per file
transform → `pilot_divergence` → `make test-pilot` → commit; a divergence on a
structural sweep means the transform leaked semantics — revert that file.
Decision-affecting commits additionally ride golden regeneration +
`make watch-pilot-burner` / `watch-pilot-completed` before `make test-tumbler`.

### Phase 0 — True up the charter; bank the free wins  (1 branch, ~4 commits, S)

1. **Amend CHARTER.md** (list below).
2. **Delete provably dead records** (audit 5, cat A — no readers anywhere):
   `NogoodProof` + `ledger.nogood_proofs`, `_PilotState.last_wait_log`,
   `DeviationIncident.occurrence_writer`, `_HoldLogEntry.tags`,
   `CoastReceipt.budget`, `EarnedWorkReceipt.source_mark`,
   `_RequirementRepairResult.declined`, `TheoryView.claim/.source/.attempts`,
   `IntrascanClosureStatus.IMPOSSIBLE`. ~120 lines removed. Gate: test-pilot.
3. **Evict the `close_intrascan` laboratory** (~400 test-only lines) from
   `intrascan.py` to tests/ or delete; the plan already calls its WITNESS contract
   "not the production target". Takes `IntrascanRequirementObservation`+kin with it.
4. **Fix `avoid.py` fail-open** (`avoid.py:18-25`): exceptions in avoid evaluation
   must fail closed (treat as violation / raise), not `return ()`. Own commit,
   decision-affecting in principle, gate with goldens + watchers.

### Phase 1 — Suffix sweep  (1 branch, 4-6 commits, rename-only, M)

Per audit 1: one suffix family per commit, `pilot_divergence` byte-identical.
- Mis-suffixed observations/receipts: `DepartureObservation`→`DepartureEvidence`,
  `EffectObservation`→`EffectReading` (own commit, 65 refs),
  `IntrascanRequirementObservation`→`…Reading`, `OperationReceipt`→`OperationLifetime`,
  `_RelationalRefinementReceipt`→`_RefinementBudget`, `_SpinVerdict`→`_SpinJudgment`,
  `NavigationEvidence`→admission functions, `TransitionEvidence`→`TransitionFacts`,
  `SkiffResult`→`SkiffReading`, `ProgramStep`→`ProgramStepReading`.
- `*Read`→`*Reading` family in options.py (8 files, ~90 refs).
- Receipt adoptions: `PilotOverlayExecution`→`…Receipt`, `_RecoveryContinuation`,
  `_ExecutionEvidence`, `_CommittedAct`, `_HoldLogEntry`, `CoastTriggerEvent`,
  and the `*Result` de-genericization.
~30 src files total. Semantic suffix violations (`_CorrectionReceipt` split,
`ActiveRequirement` lifecycle) are NOT in this sweep — they are Phase 9.

### Phase 2 — The pilot.py kernel cut  (1 branch, 6 commits, moves only, M)

Audit 4's cut, verbatim. **This is the gate for Stage 6B** — do it early.
- C0: `_DriveSetup`/`_DriveOutcome`/`_IterationTransition`/`_ProverContext` → types.py.
- C1: `requirement_repair.py` ← pilot.py:231-2700 (~2,470 lines; 15-name export
  list in audit 4; function-local `_transition_once` import breaks the one cycle).
  This region is exactly what Stage 7 later deletes — extraction makes that a
  one-file diff.
- C2: `theory_drive.py` ← pilot.py:2790-3965 (~1,180 lines).
- C3: `context.py` ← context/prover build + route prep (~410 lines).
- C4: `probes/target_prefix.py` ← pilot.py:4631-4774 (~145 lines) — first module
  written to the probe contract, carries the first `ForkBudget(forks=2, scans=1)`.
- C5: render evictions → recording.py (~200 lines).
Test logistics: re-export shims in pilot.py for the 13 test files importing
privates; retarget the one `_record_controlling_theory_fact` monkeypatch.
After this branch: **Stage 6B may proceed in parallel with Phases 3-8.**

### Phase 3 — Identity consolidation  (1 branch, ~5 commits, M)

Per audit 1 Table 3. Mechanical first, bug-fixes gated individually:
- C1 (mechanical): `world_key.world_key_for(state, snap)` replaces 31 hand-built
  call sites + kills the 17 `getattr(state,"active_requirements",())` spellings +
  folds the 3 thin wrappers. Add named `proof_world_key(...)` for the deliberate
  requirement-free scope (kills the literal-`()` idiom).
- C2 (mechanical): `execution_owner_token()`, `pair_identity()` (beside
  `pulse_identity`), single `TheoryObjectiveSnapshot` factory,
  `repair_attempt_identity()` for the 3 tuples sharing one dedupe set.
- C3 (mechanical): `occurrence_identity(occ, *, scan_scoped)` — one constructor,
  merges the theory/working_theory twins; requirements' scan-free variant becomes
  the named flag. Move `pipeline_graph.{_applied_key,_canonical_applied,
  _context_value_key}` → world_key.py.
- C4-C5 (behavior-affecting, gate each): checkpoint-owner token arity unification
  (3 arities under one tag — live crack); `TheoryObligationSnapshot`
  `projected_consumer` divergence (equal claims compare unequal);
  `pilot.py:1909` dynamic_address dropping `depth` (can alias subroutine depths).
  These are latent bug fixes and may legitimately change goldens.

### Phase 4 — Declared needs  (1 branch, ~4 commits, M; tests are the bulk)

Per audit 2. There is no needs *mechanism* to build for contexts — it's type
tightening plus fail-loud:
- C1: annotate the 83 `ctx: Any`/`state: Any` signatures with
  `_PilotContext`/`_PilotState` (TYPE_CHECKING where needed); delete getattr at the
  sites whose annotations are ALREADY correct (departure, investigation_replay,
  pilot, recording).
- C2: kill the `prior`/`domain_prior` dual (rename + delete the dead fallback at
  `program_step.py:183`); plain attribute access for `_harness` (20 sites) and
  TraceNode fields (27 sites).
- C3: corrections.py fail-loud — delete the 7 silent `return []` degradations;
  replace the investigation_replay mini-ctx `SimpleNamespace` with a narrow frozen
  record (it is a deliberate capability restriction — formalize it).
- C4: rework the 19 test files building `SimpleNamespace` ctx stand-ins onto a
  shared typed fixture. (Do this FIRST within the branch if churn is a concern.)
- WalkContext Protocol (`types.py:120`) is the reference needs mechanism — extend,
  don't invent.

### Phase 5 — ForkBudget, counting first  (1 branch, 2 commits, S/M)

Per audit 2 B.4:
- C1: thread a two-counter `ForkBudget` (forks, scans) through
  `fork_with_pilot_rungs` as counting/reporting only — decision-identical by
  construction; gives `_projection_replay_count` its missing src consumer.
- C2: restate EXISTING bounds as declared budgets (intrascan closure, composition,
  skiff single-pass, steer, coast) — mechanical; fix the skiff pass-3
  never-decrements inconsistency (`skiff.py:379`).
- Enforcement over the currently-unbounded readers (program_step per-input forks,
  the 18-site projection-replay family, skiff/hypothesis outer loops) is
  DEFERRED to after Phase 8 — any new binding limit changes decisions and its
  exhaustion must be attributed by the kernel ledger.

### Phase 6 — Evidence/verdict sweep  (1 branch, ~8 commits, M/L)

Audit 3's order: A2 departure leak fixes (typed NotInspected, kill the
string-prefix verdict channel, `explain` becomes evidence, merge
`_departure_reading` into classify) → A4 hoist `agency` out of `assess_outcome`
(delete the `causal_probe` hook) → A6 `_regression_ownership` evidence record →
A7 `_rank_hypotheses` pure sort → A3 make the charter's reference real
(`StepEvidence`/`classify_step` in program_step; cascade order is do-not-touch) →
A5 `_monitor_trend` → RetentionDecision + pure classifier + apply → A1 the verify
two-stage split (`TrialEvidence`+`classify_trial_gates` for gates 0-8,
`LandingEvidence`+`classify_landing` for 9-13; gate_events/nogoods become ordered
tuples appended by the caller). A single up-front VerifyEvidence is impossible —
the two-stage seam preserves gate order exactly. Fix the `verify.py:1089`
channel_motion mid-cascade rebind as part of A1.

### Phase 7 — Proposer + single gate  (1 branch, ~4 commits, M/L)

Audit 3's order: B3 `_initial_hypotheses` → named 3-generator tuple, dedupe to the
consumer loop → B1 orientation `PRECEDENCE` ordered tuple + ONE admission gate
absorbing the 6 copied expressions and `_bearing`'s expectation check
(`_theory_retry_bearing` stays fail-loud outside the tuple — exactness contract) →
B4 route-plan suppression + awaited-action bypass become admission reasons →
B2 options candidate stanzas → ordered generator tuple, `_action_allowed`/
key_nogoods move to the single gate so proposer-filtered rejections become
visible gate events. B2 is the risky one; it changes which rejections are
*recorded* (not which occur) — expect golden event additions.

### Phase 8 — Kernel boundary: soundness relocation + directory materialization
(1 branch per arc, L, highest risk — audit 5 Table 4)

- avoid gates fold into kernel (`verify.py` or kernel `constraints.py`).
- `requirement_recovery.py` gates → verify.py (acceptance) + orientation.py
  (admission); delete the re-export facade.
- `recovery.py` invariant asserts → kernel; the `_ACTIVE_RECOVERY` ContextVar
  capability token becomes a passed capability, not ambient.
- `effects.py` terminal_target displacement veto → verify.py.
- `world_key._pilot_world_key` getattr chain → typed protocol, fail loud.
- **progress.py decision**: declare it kernel (honest, cheap) or split
  pure `classify_retention` (instrument) + kernel applier (Stage 9 direction).
  Recommend: declare kernel now, split rides Stage 9.
- Then materialize the target tree (`kernel/`, `instruments/`, `probes/`,
  `render/`) as pure `git mv` per audit 4 Table 1, and land the registry
  (one entry per reader FUNCTION; schema in audit 4).
- Inline-backs ride along: physical.py, pulse.py, availability's 5 trace-privates,
  `_holds_defeat_needed`, `_channel_*`; promote the ~20 private cross-module APIs
  (progress/recording/trace/tide_tables/steer) to public names.

### Phase 9 — Bill lifecycle semantics  (owned by the working-theory plan)

These are NOT structural sweeps — they change drive decisions and belong to
Stages 7-10, informed by audit 5 Table 1:
- `ActiveRequirement` expiry + INVALIDATED/AMBIGUOUS writers (Stage 8 already
  owes `ACTIVE|DISCHARGED|INVALIDATED|AMBIGUOUS`). Risk: `status` is inside
  `navigation_identity` and hence the world key.
- `PendingDeparture`: expiry reachable on the reject arm; the 6 silent `= None`
  drops get events; structural link between `checkpoints.clear()` and
  `pending_departure=None` (currently convention — 4th unpaired clear crashes).
- `ExpectationReceipt`: the 7 silent early-returns mint a typed negative receipt.
- `EffectExpectation` discharge on the ACCEPT arm (`pilot.py:369` early return).
- PilotRung prerequisite-hold supersession/revocation receipts (Stage 10 debt);
  fix `steer.py:113` install-before-verification as part of it.
- `_CorrectionReceipt` → settled receipt + installation bill split.
- `proof_rejected_acts` doc/code mismatch: decide (re-admit on changed
  requirements, or fix the comment).
- Open-theory closure on budget-exit/target-reached paths.
- `_rebound_bearing` deletion (Stage 7: orientation re-resolves from the receipt).
- Give readers to: `ledger.unattributed` (→ `_knowledge_payload`),
  `ledger.tombstones` (→ theory_view suppression — otherwise "do not repeat this
  exact experiment" is unenforced), `PilotGateEvent.evidence` (→ skeleton, or
  delete the field + 11 write sites), `stall_pending` (→ CoastObservation or delete).

## CHARTER.md amendments (Phase 0, commit 1)

1. Pattern 1 reference: cite `attempt_interpretation.interpret_attempt` and
   `earned_work` (receipt/verdict); mark program_step's `StepEvidence`/
   `classify_step` and orientation's `PRECEDENCE` as *to be built* (Phases 6-7).
2. Add the staged-evidence boundary: evidence may be gathered in stages when a
   later stage's cost is conditional on earlier gates passing; each stage is one
   frozen record with one pure classifier. (Single up-front VerifyEvidence is
   impossible without violating the plan's own performance gates.)
3. Budgets are per-caller-site: the reader declares the budget *shape*, the
   caller declares the *amount* (resolves the skiff 16-vs-8 duplicate).
4. Identity: exactly two world-key scopes (navigable, proof), both named in
   world_key.py; one occurrence constructor with an explicit `scan_scoped` flag;
   recorded-history identity ≠ relocatable-projection identity (never merge
   `CausalOccurrence` with `EffectOccurrenceSnapshot`).
5. Suffix legend additions: `*Reading` (instrument output, may be UNCLEAR);
   `*Judgment` (classification with an honest UNKNOWN arm — NOT `*Verdict`, which
   keeps its complete-domain requirement); `*Entry` (knowledge-table row,
   revisable by evidence; immutability belongs to the causing observation);
   `*Incident` (transient evidence window, no identity outside its
   investigation). Name the buffer→receipt transition (`_PulseState` freezes into
   `_ExecutionEvidence` at accept). Suffix mandatory only where the plain noun
   would mislead about authority.
6. Declared needs: `needs` is the right mechanism where a reader must be DENIED
   fields (capability restriction, e.g. excursion diagnosis); fail-closed inbound
   validation of foreign payloads is exempt; a proposer re-resolving a retained
   identity may be fail-loud — only current-world proposers must decline.
   Decide: are narrow test-only contexts legitimate consumers of public readers?
   (Recommend no — Phase 4 C4 rewrites the 19 fixtures.)
7. Registry: one entry per reader *function*, not per file; add an interpreter
   shape `read(evidence) -> Reading` for receipt-readers like
   attempt_interpretation; narrow "only skiff may propose" to NAVIGATION acts
   (correction modules propose installable PilotRungs — different authority);
   kernel gains explicit `decide` (orientation) and `execute`
   (coast/pulse/cyclefold/overlay) slots; add a `render/` bucket.
8. Kernel set correction: progress.py IS kernel (the charter list omitted the
   second-largest mutator); avoid/coast-avoid-enforcement/recovery-asserts/
   effects-terminal-veto are soundness currently outside the kernel — Phase 8
   relocates them.
9. "Two consumers or stay inline" boundary: a module earns existence by reuse,
   an exactness contract, or a declared ownership seam.
10. Inert laboratories are not exemptions: promoted or deleted.

## Bug register (found by audit; fix at the phase noted)

| Bug | Where | Phase |
|---|---|---|
| avoid evaluation fail-open | `avoid.py:18-25` | 0 |
| skiff pass-3 budget never decrements | `skiff.py:379` | 5 |
| channel_motion rebound mid-cascade | `verify.py:1089` | 6 (A1) |
| departure verdict smuggled via string prefix | `departure.py:495,637` | 6 (A2) |
| checkpoint-owner token: 3 arities, one tag | `pilot.py:2814,2929,3752` | 3 |
| TheoryObligationSnapshot field divergence | `pilot.py:2869` vs `working_theory.py:392` | 3 |
| dynamic_address drops `depth` | `pilot.py:1909` | 3 |
| proof_rejected_acts doc/code mismatch | `types.py:862` vs `pilot.py:4922` | 9 |
| prerequisite holds installed pre-verification | `steer.py:113-114` | 9 |
| PendingDeparture expiry unreachable on reject arm | `progress.py:470` | 9 |
| ActiveRequirement: no expiry, statuses never assigned | `requirements.py:85`, `types.py:848` | 9 |
| EffectExpectation undischarged on accept arm | `pilot.py:369` | 9 |
| ExpectationReceipt: 7 silent early-returns | `pilot.py:433-478` | 9 |
| checkpoints.clear()/pending_departure coupling by convention | `pilot.py:1268` etc. | 9 |
| open theory survives loop termination | `pilot.py:5582` paths | 9 |

## Dependencies and parallelism

- Phase 0 → Phase 1 → Phase 2 in order (renames before moves keeps diffs in
  original files; the cut unblocks Stage 6B earliest).
- After Phase 2: Stage 6B (product roadmap) proceeds in parallel; Phases 3-7 are
  independent of it except theory_drive.py (coordinate Phase 3's theory-identity
  commits with any active 6B work).
- Phases 3, 4, 5 are mutually independent; 6 and 7 after 4 (they rely on
  tightened types); 8 after 6-7 (verify/orientation reshaped first); 9 rides the
  working-theory stages.
- Every phase leaves the tree green and shippable; there is no big-bang moment.
  The directory tree materializes only in Phase 8, when the files' contracts
  already match their destinations.
