# PILOT understanding and ownership plan

One working plan. Supersedes the earlier LOC-first version of this file and the
deleted predecessor reports.

## Cold-start protocol

Before taking an item:

- Read this file and `src/pyrung/core/analysis/pilot/CLAUDE.md`.
- Inspect the current HEAD and worktree. Baseline SHAs, counts, and descriptions
  below are historical grounding, not substitutes for reading today's code.
- Re-ground every claim at the named module and symbol. Treat each "required
  shape" as a proposed route to the objective, not as a specification to
  implement despite contrary evidence.
- If a supposedly broken path is unreachable under current invariants, prove and
  delete it rather than adding machinery to support an artificial case. Correct
  this plan when its premise is inaccurate.
- Preserve unrelated concurrent work. Keep one ownership decision in one
  conventional commit.
- Use focused tests while iterating, then run `make test-pilot` as the final gate.
- Remove the landed item and update `pilot/CLAUDE.md` so the next conversation
  starts from the new ownership boundary.

## Purpose

Make PILOT easier to understand before asking it to do more.

The primary measure is not file size. It is whether a reader can answer, without
reconstructing a chain across modules:

1. Who made this decision?
2. What exact evidence did that owner use?
3. What immutable object carries that decision and evidence forward?
4. Which consumers merely apply it, and which are still deciding it again?

LOC reduction matters when it removes duplicate derivation, parallel state, or
control-flow scaffolding. Moving code, adding a dataclass around the same loose
fields, or hiding policy behind a generic callback does not count as simplification.

### House rules

- Identify work by **module + symbol**, never by line number.
- Describe each cross-module change as **owner -> receipt -> consumers**.
- Prefer carrying the owned object intact over dehydrating it to scalars and
  rehydrating a lookalike in the next module.
- A dataclass should make an invalid state unrepresentable, preserve evidence, or
  give one derivation a single home. Do not introduce records that merely shorten
  an argument list.
- Derived views should be properties of their evidence owner. Do not store a
  second field that can disagree with the first.
- Separate declaration, observation, classification, and policy. They are
  different decisions even when one function currently performs two of them.
- Every item records why, rough LOC effect, risk, and its gate.
- Correctness work lands before structural cleanup that would obscure its diff.
- Delete items as they land. Do not archive completed work here.

---

## Grounded baseline (2026-07-26)

The audit behind this plan was recorded at `af3060a4`, when the package was 33
Python files and about 26.3k lines. The mechanical ownership pass had landed.

The current tree already has several of the receipt structures the predecessor
plan proposed:

- `navigation.TargetSpec` is constructed once in `pilot._make_pilot_context`.
- `navigation.BearingObjective` carries the target plus the complete orientation
  frontier into verification and recovery.
- `types.ChannelMotion` carries one requested channel boundary and VERIFY's owned
  landing interpretation.
- `program_step.ProgramStep` derives `handoff_by_action`,
  `uniform_handoff_boundary`, `required_pairs`, and `inputs_with_lifetime` once in
  `__post_init__`.
- `options._CandidateSource` replaces four stored provenance booleans on
  `_Candidate`; the compatibility booleans are derived properties.
- `trace.TraceReadConstraints` owns the common trace-read bundle.
- `_HoldLogEntry.tags` and `_StepContext.steady_holds` are derived from executable
  rung evidence rather than stored in parallel.

Those are the pattern to continue: construct evidence once, keep it typed, and
let later modules consume the object.

### Target object flow

The cleanup is aiming for this chain:

```text
options          orientation        steer             verify
CandidateRead -> Bearing          -> ExecutedAttempt -> VerifiedTrial
                 Act + Policy        declaration +      attempt + acceptance
                 Objective           execution evidence + GaugeReceipt
                                                            |
                                                            v
pilot / progress   CommittedOperation -> DepartureObservation -> PendingDeparture
                   verified evidence    observed landing       recovery policy
                   + commit-owned rungs + classification       + rollback owners
```

Each arrow should carry the object on its left intact. A downstream object may
compose new evidence around it, but should not copy selected fields and recreate
its meaning.

### Inaccuracies corrected from the previous plan

- A1 cannot be fixed merely by "passing the compiled avoid condition." The
  compiled condition is currently discarded by `runner._compile_avoid`;
  `_AvoidMember` retains only a predicate, display name, and tag names.
- An opaque callable `avoid=` has no condition metadata at all. A fold cannot
  soundly infer which skipped scan would satisfy an arbitrary callable. Its
  correctness/performance policy must be explicit.
- A terminal settle already executes scan by scan and produces a
  `CoastReceipt.trajectory`; it does not need the same mechanism as a folded seek.
  The current problem is that terminal dwell discards that receipt/trajectory.
- G1's former "after D6" prerequisite is satisfied: D6 landed in `af3060a4`.
- `trace._all_nodes` now has 15 package call sites, not nine.
- The three static-edge filters have drifted by policy, not just by one extra
  term. Detour intentionally omits several current-world nogood checks that
  options and frontier evidence apply.
- "Never split a file" is too absolute. A file move is not a first move, but an
  ownership pass may expose a real boundary worth splitting later. Reassess after
  the objects and decision owners are clear.

### Test posture

`make test-tumbler` was green after the admission/mechanical pass. That establishes
refactor mechanics, not the correctness of evidence the current receipts never
record. In particular, no passing endpoint golden can prove that a folded coast
never crossed `avoid=`.

For deep Tumbler work, use `tests/tumbler/bench.py::Bench.force_done(acc_tag,
preset)` to park at `Internal__Step == 102`; do not drive `how()` from cold.
Use `devtools/pilot_divergence.py` for the first changed decision and
`devtools/watch_pilot_decisions.py --stop-action TAG=VALUE` for candidate entry.

---

## A. Correctness preconditions

These are active soundness or contract issues. Keep their diffs separate from the
ownership refactors below.

### A2. Put complete-domain enforcement inside `tide_tables.guard_verdict`

**Current ownership failure**

`guard_verdict` can return permanent `GUARD_DEAD` using soft, plausible operand
domains. `trace._writer_guard_verdict` separately protects its call with a
duplicate completeness check. The public decision owner is therefore unsafe
without its one current caller's private precondition.

**Required shape**

- Add `require_complete_domains: bool = True` to `guard_verdict`.
- When a free operand lacks a complete domain, return `GUARD_PUNT`, never
  `GUARD_DEAD`.
- Reuse `tide_tables._is_complete_domain`.
- Delete trace's `_complete_domain` closure and pre-check; trace retains pin
  derivation and memoization.

**Why**

The object that returns the rejection must own the proof requirement. A future
caller should not be able to fabricate a permanent rejection by forgetting
prose in `pilot/CLAUDE.md`.

**LOC:** about -15 to -25.

**Risk:** medium but contained.

**Gate:** `test_pilot_rejection_arm.py`, `test_pilot_sandbox_gate.py`, and direct
`guard_verdict` tests for complete and incomplete free domains.

### A3. Lock VERIFY's deliberate omission of the live harness

`orientation._trace_for_route` / `_read_route_trees` use
`TraceReadConstraints.from_context`, including the live harness, as a planning
model. `verify._gate_dead_end` deliberately constructs a post-trial read without
that harness and separately checks live
`_has_pending_effects(trial.fork)`.

The rationale is coherent: proposal may model hypothetical harness motion; proof
must use the executed receipt or a currently pending live effect. Preserve the
asymmetry unless a counterexample disproves it.

**Work:** add a focused harness-linked ramp test. The harness may identify the
driver during orientation, but VERIFY accepts continued motion only from executed
or live-pending evidence.

**LOC:** tests/documentation only unless the test finds a real rejected-progress
case.

**Risk:** low.

**Gate:** `test_pilot_verify.py`, `test_pilot_trace.py`.

## B. Preserve objects across module seams

This is the main cleanup program. Items are ordered by leverage.

### B1. Carry navigation policy intact from option materialization into verification

**Current dehydration chain**

`options._Candidate` -> `navigation.Pulse.option: Any` ->
`steer.execute` interprets the private `_CandidateSource` ->
`types._AttemptIntent` stores parallel booleans and scalars ->
`verify` consumes those reconstructed fields.

`steer.py` currently imports `_CandidateSource` from `options.py` and decides:

- whether a Pulse is influence-prescribed;
- whether route/current provenance deserves prescribed-bearing policy;
- which channel/value forms `ChannelMotion`;
- which nogoods and causal-chase policy apply.

Batch source literals are decoded a second way in the same dispatcher.
Execution is therefore re-deciding navigation policy instead of executing a
declared act.

**Required shape**

- Give each `NavigationAct` a typed, immutable verification policy constructed by
  the navigation owner. Exact names are secondary; the object must carry source
  category, action artifact, observation labels, nogood scope, causal policy, and
  optional channel heading together.
- `Bearing` remains the declaration. `steer.execute` validates freshness, installs
  prerequisites, and executes the act; it does not interpret candidate provenance.
- `_ExecutedAttempt` carries the original bearing/act policy plus physical
  execution evidence. Remove `_AttemptIntent` fields that duplicate the act.
- Recording consumes the same candidate/policy object; it must not require a
  parallel diagnostic copy.

Prefer eliminating the `_Candidate` crossing entirely or moving its cross-module
contract to `navigation.py`; do not leave `Pulse.option: Any`.

**Why**

One navigation decision should survive execution unchanged. This removes the
highest-value dehydrate/rehydrate seam and makes source-specific behavior visible
on the declared act.

**LOC:** about -30 to -60.

**Risk:** medium; wide but intended to be behavior-preserving.

**Gate:** `test_pilot_orientation_contract.py`, `test_pilot_candidate_wait.py`,
`test_pilot_steer.py`, `test_pilot_verify.py`, recording tests, then Tumbler.

### B2. Make `_CandidateList` a composition of owned readings, not a flattened state bag

**Current dehydration chain**

`options._admit_wait_read` now keeps `WaitRead` and `_TraceAdmission` together as
`_AdmittedWait`, but `_build_candidates` still unpacks that admitted reading into
locals and returns `_CandidateList` with parallel optional fields:

- `wait_prescribed` + `wait_reason`;
- `advance_boundary` + `advance_condition`;
- `completion_frontier` + `program_step`;
- `prescribed_batch`;
- route plan, route candidates, co-actions, and prerequisite rungs.

`orientation._orient_read` then reconstructs a coast heading by walking
`ProgramStep.projected_changes`, interpreting `preserve_channels`, preferring a
route channel, and reassembling the route edge context.

**Required shape**

- No false-valued `_WaitPrescription`. Use `None | WaitPrescription`; a present
  prescription is valid by construction.
- `ProgramStep` owns the exact observable program motion it proved. Orientation
  should not reverse-walk `projected_changes` to infer a heading.
- Introduce a typed heading receipt containing the immediate channel/value,
  original boundary, and optional outer route-edge context. `Coast` consumes that
  object whole; `route_prescribed` becomes derived from the route context.
- `_CandidateList` (rename after its final role is clear) composes
  `_TraceAdmission`, optional wait/heading, candidate options, prerequisite
  artifact, and diagnosis. Do not copy their fields into the outer record.
- `OrientationTrace.candidates` and `Bearing.prerequisites` stop using `Any`.

**Why**

Candidate construction is hard to follow because each phase mutates a set of
loose variables whose combinations encode hidden variants. Make the variants
explicit first; phase extraction then becomes mechanical.

**LOC:** neutral to -40. The payoff is fewer states and less reconstruction, not
the raw delta.

**Risk:** medium-high; `_build_candidates` is the action-selection hot path.

**Gate:** `test_pilot_candidate_wait.py`, `test_pilot_orientation_contract.py`,
`test_pilot_coast.py`, `test_pilot_program_step.py`, decision goldens.

### B3. Carry one gauge comparison receipt through verify, commit, and progress

**Current dehydration chain**

`verify` computes `ordinal_advanced`; `outcome.assess_outcome` turns that Boolean
plus trace distance into `ProgressEffect`; `pilot._commit_trial` calls
`Gauge.ordinal_advanced` again to award dwell credit; pending progress later
rebuilds comparisons for new source/landing pairs.

`GaugeReceipt` already exists but the main trial path does not carry it.
Its current single `effect` field is not a replacement for
`ordinal_advanced`: one gauge component can advance while another moves behind,
so "any earned coordinate" and the worst-wins overall effect are independent
facts.

**Required shape**

- `Gauge.receipt` records the component evidence needed to derive both the
  worst-wins effect and whether any coordinate advanced. Do not collapse those
  axes to one enum.
- `verify` creates the post-retry `GaugeReceipt` once for the accepted trial.
  `_gate_spin` may compute an earlier receipt for its pre-retry decision, but a
  replaced fork necessarily gets a new owned receipt.
- Add typed `GaugeEffect` values and derived `GaugeReceipt.any_advanced` /
  `.behind` properties; stop comparing string literals throughout the package.
- `TrialAssessment` consumes the receipt, not a `channel_progressed` Boolean.
- The accepted trial carries the receipt. Commit dwell accounting and progress
  policy consume it. A later pending transition gets a new receipt for its new
  source/landing pair, constructed once by the pending-policy owner.

**Why**

The source/landing marks are the auditable evidence. Passing only a Boolean loses
precision and invites later modules to decide the comparison again.

**LOC:** about -10 to -30.

**Risk:** medium; affects spin, outcome, dwell accounting, and pending progress.

**Gate:** `test_pilot_gauge.py`, `test_pilot_verify.py`,
`test_pilot_progress.py`, Tumbler.

### B4. Replace `_TrialResult`'s flattened optional fields with verification variants

**Current dehydration chain**

`_PulseState` + `_AttemptIntent` become `_ExecutedAttempt`; `verify._trial_result`
copies their fields into `_TrialResult`; ordinary acceptance then `replace()`s
`new_key`, `trend`, `outcome`, and `assessment`.

Target acceptance intentionally lacks those four fields, while ordinary accepted
motion requires them. One dataclass expresses both states with optionals, and
`progress.py` repeatedly checks for `None`. `outcome` is also stored beside the
`TrialAssessment` from which it is derived.

**Required shape**

- Preserve the executed-attempt object inside the accepted receipt instead of
  copying its objective, action artifact, labels, motion, coast receipt, timeline,
  and causal policy field by field.
- Represent target acceptance and assessed-motion acceptance as explicit typed
  variants, or give both a required verification receipt with distinct variants.
- Make legacy `Outcome` a property of `TrialAssessment`; never store both.
- Type `CoastReceipt` and `BumpEvent` at the trial seam instead of `Any`.
- `progress`, `recording`, and commit code consume the verification object and
  properties. They do not reconstruct its interpretation from snapshots.

**Why**

An accepted trial is the central package receipt. Making its variants explicit
removes a large class of "field happens to be None on this path" reasoning.

**LOC:** roughly -40 to -90.

**Risk:** high because the receipt crosses verify, pilot, recording, progress, and
investigation. Land after B1 and B3 so it composes stable objects.

**Gate:** all verify/outcome/progress/recording tests, departure tests, then full
pilot and Tumbler.

### B5. Let committed operation context embed execution evidence instead of rebuilding it

`pilot._step_context` currently reconstructs a durable operation record from
`_TrialResult`, `_IterationFrame`, and `_PilotState`: frontier tags, control rungs,
channel heading, before/after snapshots, timeline, and coast accelerators.

Some facts are commit-owned (`control_rungs`); others already belong to the
execution/verification receipt (`motion`, snapshots, channel, timeline,
`CoastReceipt.advances`). Preserve that boundary.

**Required shape**

- Define one immutable, PLC-free execution window/operation evidence object.
- The accepted trial and `_StepContext` reference that object rather than copying
  its fields.
- Keep commit-owned rung ownership and checkpoint context on `_StepContext`.
- Make `accelerators` a derived view of the typed coast receipt.
- Preserve the existing good pattern: `steady_holds` remains derived from exact
  rungs.

**Why**

Incident construction and replay should read the same operation evidence VERIFY
accepted, not a commit-time reconstruction that merely looks equivalent.

**LOC:** about -20 to -50.

**Risk:** medium-high; affects replay and correction evidence.

**Gate:** recording, holds, progress, investigate, and cyclefold tests.

### B6. Separate departure observation from departure policy without flattening either

`detour.classify_departure` currently returns `DepartureVerdict`, which mixes:

- the settled PLC fork with selected fields dehydrated from the local
  `CoastReceipt` (`settled_value`, `settle_scans`);
- causal `DepartureReading`;
- `GaugeReceipt`;
- route-continuation evidence;
- the policy string `"continue" | "unknown" | "regression"`.

`progress` consumes the object initially, then rehydrates a `PendingDeparture`
from selected fields.

**Required shape**

- Detour owns a typed immutable `DepartureObservation` containing the actual
  landing `CoastReceipt`, causal reading, gauge receipt, and constrained
  continuation evidence.
- A typed classification is derived once from that observation.
- Progress owns `PendingDeparture` policy and embeds the durable opening
  observation/receipt plus rollback owners; it should not copy a partial set of
  source marks and classification strings.
- The mutable settled fork remains an adoption handle, not durable evidence.

**Why**

This makes "what happened" independently inspectable from "what recovery will do,"
and gives pending policy the exact opening evidence it is waiting to resolve.

**LOC:** likely neutral to -30.

**Risk:** high; do after the central trial receipt is stable.

**Gate:** `test_pilot_detour_progress.py`,
`test_pilot_detour_hold_release.py`, `test_pilot_progress.py`,
`test_pilot_investigate.py`.

---

## C. Give repeated decisions one owner

### C1. Replace the three static-edge lambdas with one typed base decision

The previous plan described the filters as nearly identical. They are not.

- `options._compass_route_plan._edge_open` applies excluded identities,
  static-edge evidence, wait/action nogoods, exact Pulse nogoods,
  `route_allowed`, and avoid.
- `navigation_evidence.frontier_status.edge_allowed` applies blocked actions,
  wait/action nogoods, exact Pulse nogoods, and avoid.
- `detour.classify_departure` applies static-edge evidence, reset-blocked
  destinations, discharged-action resurrection, `route_allowed`, and avoid, but
  omits current-world learned nogoods.

**Required shape**

- First write a policy matrix and decide whether detour's omissions are intended.
- `navigation_evidence.py` owns a base `EdgeDecision` with allowed/excluded and
  machine-readable reasons for the shared static status, action artifact, wait
  artifact, route constraint, and avoid checks.
- Options and frontier evidence consume that decision.
- Detour composes explicit recovery-only exclusions (reset and resurrection) and
  either explicitly opts out of world nogoods or consumes them. No anonymous
  callback hides that choice.
- Recording/debug output may surface the reason object; path search still consumes
  the Boolean projection.

**Why**

The win is one auditable answer to "why was this edge excluded?", not 22 fewer
lines.

**LOC:** neutral to -25.

**Risk:** medium-high because one current behavior will likely be adjudicated.

**Gate:** avoid, nogood, candidate-wait, and detour-progress tests.

### C2. Give trace alternative selection one result object

`trace.py` repeats variants of:

1. apply avoid;
2. prefer via;
3. retain pilotable alternatives;
4. score;
5. preserve the best rejected branch when no untried pilotable branch survives.

The policy appears in expression OR selection, caller-route selection, table-arm
selection, and writer fallback bookkeeping. The variants are legitimate, but
their differences are encoded as local list operations.

**Required shape**

- Introduce a local `TraceAlternative` / `TraceSelection` receipt carrying score,
  avoid/via disposition, pilotability, empirical rejection, and fallback status.
- One selector owns the common precedence.
- Callers supply only genuinely local eligibility evidence.
- Preserve the honest rejected fallback explicitly in the result instead of
  stashing/restoring mutable node fields in multiple arms where possible.

**Why**

Writer/arm selection is a core "why" decision. A named result makes rejected,
avoided, unpilotable, and selected alternatives visible to recording and tests.

**LOC:** about -40 to -80.

**Risk:** high; trace choice changes cascade widely.

**Gate:** trace, route, avoid, needed-vocabulary, and Tumbler tests.

### C3. Introduce `UnsupportedConstruct` before converting trace dispatch

A missing tracer rule currently looks like a genuinely opaque program and sends
PILOT toward probing. That destroys the most important diagnostic fact: the reader
did not understand the construct.

**Required shape**

- `trace.py` raises a typed `UnsupportedConstruct` containing construct kind,
  source/rung context, and the unsupported object.
- Catch it at exactly one drive boundary.
- `recording.py` renders the caret/source diagnostic.
- Test mode propagates; drive mode degrades to a named terminal result.
- Only after that contract is tested, replace `_trace_back` /
  `_trace_expression` kind ladders with explicit handler dispatch if it makes the
  ownership clearer.

**LOC:** initially positive; dispatch may recover it.

**Risk:** medium.

**Gate:** trace, recording, and public `how()` diagnostics.

### C4. Unify frontier-pair identity only after defining its semantics

The same frontier is ordered-deduplicated three ways:

- `trace.frontier_pairs` uses `(tag, repr(value))`;
- `orientation._frontier` uses list membership;
- `orientation._combined_nonbearing` uses `dict.fromkeys`.

They disagree for unhashable values and `bool` versus `int`. A `repr` key supports
unhashable values and keeps `True` distinct from `1`, but it can collide for
unrelated values with the same representation.

Define the package's action/value identity contract first, preferably as a small
owned key function or value object. Then use one ordered-unique helper.

**LOC:** about -15.

**Risk:** low-medium; this is a behavior decision, not mechanical cleanup.

**Gate:** focused identity tests plus goldens.

---

## D. Extract control flow after the receipts are stable

### D1. Extract `options._build_candidates` by owned phase

The function remains about 540 lines and has sequential phases sharing mutable
locals. Do not extract the old scalar phases unchanged.

After B2:

- `_read_route_and_wait` returns the typed route/wait/admission receipt;
- `_lower_prerequisites` consumes admission and returns executable prerequisites
  plus remaining actions;
- `_read_learned_fallback` returns an optional learned act;
- `_assemble_candidates` returns the final option set and diagnosis.

The extracted phase record must carry every rebound value explicitly. No closure
over seven mutable locals.

**LOC:** approximately neutral to -30.

**Risk:** medium after B2, high before it.

**Gate:** candidate-wait, orientation-contract, coast, nogood, and Tumbler tests.

### D2. Give `TraceNode` one traversal and one interior-frontier predicate

`TraceNode` has six recursive collectors (`leaves`, same-tag chains, ordered
actions, pivots, unsatisfied conditions, dead-end parents) plus `_all_nodes`, which
has 15 package call sites.

Add a stable `iter_nodes(order=...)` generator and one
`is_interior_frontier`/`_interior_frontier` predicate. Keep per-collector stopping
rules—especially relational frontiers—explicit.

This is the prerequisite for any TraceNode split by kind and may make that split
unnecessary.

**LOC:** about -25 to -45.

**Risk:** medium; traversal order is behavior.

**Gate:** trace, needed-vocabulary, program-step, options, and skiff tests.

### D3. Collapse repeated trace writer-fallback bookkeeping

`trace._trace_back` repeats save/reset/restore blocks for attempted writers and
their rejected/avoid fallbacks. After C2 supplies a typed selection receipt,
factor the mutation transaction once.

**LOC:** about -25 to -40.

**Risk:** high; hottest static correctness path.

**Gate:** rejection-arm, trace, avoid, route, and Tumbler tests.

### D4. Extract replay advancement in `investigate.py`

The raw and guarded replay arms repeat replacement-fingerprint and
advance-or-reject logic. Extract an `_advance_or_reject` result object, not a
Boolean/tuple. Then extract the large channel-ejection arm from
`progress._monitor_trend` once B4/B6 provide stable receipts.

**LOC:** investigate roughly -30 to -45; progress extraction may be net-neutral.

**Risk:** medium-high.

**Gate:** investigate, progress, detour, correction, and golden tests.

### D5. Add role-typed keys last

Use `NewType`/small value objects for action-source keys, rollback owners, and
search scans only after the containing receipts settle. Observation constructors
should accept the owning object rather than loose keys.

The purpose is preventing scope mixups, not annotating every tuple.

**LOC:** slightly positive.

**Risk:** low mechanically, wide in reach.

**Gate:** type check plus nogood, progress, and regression-scope tests.

---

## E. Compile residual causal replay after folding

The remaining performance project is not “replace the interpreter with the
compiled runner.” Keep the existing semantic owners and compose them:

1. the shared coast/fold machinery decides which scans are provably skippable,
   including ordinary timer folding and cycle folding;
2. the compiled runner executes only the residual scans between observation
   boundaries;
3. causal replay lands at `witness_scan - 1`, then executes one interpreted scan
   to capture exact reads, runs, edges, and attempted writes for the witness.

That division preserves the reason causal replay exists while removing most of
its per-scan Python cost. It also gives candidate replay the same advancement
primitive instead of introducing a second replay log or a causal-only fold
implementation.

### E1. Instrument the residual work before changing its executor

For the exact avoided-Complete Tumbler route, report:

- ordinary-folded, cycle-folded, compiled-residual, and interpreted-witness scan
  counts;
- cold compile, warm execution, observation handoff, and total replay time;
- slab refill and candidate-replay counts, including repeated replay of an
  identical interval.

Measure cold and warm runs separately. A roughly half-second cold compilation
can erase a small replay win even though the warm kernel is substantially faster.
Keep the current interpreter run as the semantic and timing baseline.

Known evidence is encouraging but not yet an end-to-end acceptance result:
compiled state transitions have matched interpreted transitions in the measured
route, and a warm compiled kernel ran a 331-scan residual interval in roughly
0.10--0.13 seconds versus roughly 1.0--1.5 seconds interpreted. Re-measure these
figures under the final observation contract rather than copying them into a
performance promise.

**Risk:** low; instrumentation must not retain another scan-by-scan log.

**Gate:** the focused causal/investigate tests plus the exact avoided-Complete
route.

### E2. Define one replay-advancement API

Extract a narrow advancement operation shared by causal slab refill and
candidate replay. Its inputs must identify the executable `World`, target scan
or observation boundary, avoid predicate, and active coast/fold proof. Its
receipt must distinguish scans skipped by ordinary folding, scans skipped by
cycle folding, residual scans executed by the backend, and the exact endpoint.

Do not make the compiled backend decide whether a jump is sound. The coast/fold
owner proves the jump; the backend applies state transitions. Do not add a
universal receipt type pre-emptively: define the result beside the advancement
owner, and let D4 reuse it if that makes `_advance_or_reject` smaller.

The operation must work with every current repeating-history encoding through
its public transition/jump surface. It must not branch on a private concrete RHE
representation or silently expand compressed history.

**Owner -> receipt -> consumers:**

`shared replay advancement -> exact advancement receipt -> causal slab refill
and candidate replay`

**Risk:** medium-high; endpoint ownership and avoid observation are semantic.

**Gate:** RHE transition, fold, cyclefold, causal capture, and investigate
tests.

### E3. Add the compiled residual executor

Cache compiled kernels by the executable world shape that affects semantics:
program, synthesis plant, active `PilotRung`s, and runner options. A cache hit is
valid only for the same executable world; equal tag values in a different world
are not parity. Preserve patches, forces, scan lifecycle, timer fractional
state, edge memory, avoid checks, and exact target-scan landing.

Use compiled execution only between required observation boundaries. At an
evidence boundary, materialize the state at the preceding scan and let the
interpreted observer execute the witness scan. The resulting causal chain,
read/run views, attempted writes, and replay result must be identical to the
all-interpreted baseline.

This should compose with folding rather than compete with it: long proven waits
mostly disappear through ordinary/cycle folds; the kernel accelerates the
unavoidable remainder. If a route cannot prove a fold, compiled residual replay
must still remain a correct optimization rather than changing the evidence.

**Acceptance:**

- exact causal and candidate results match the interpreted baseline;
- the avoided-Complete Tumbler investigation retains its golden explanation;
- warm end-to-end investigation is at least 2x faster on the measured route;
- cold end-to-end timing is reported separately and does not regress
  pathologically;
- memory remains bounded by the existing slab/cache policy, with no second
  per-scan history.

**Risk:** high.

**Gate:** compiled/interpreted parity, fold and cyclefold, causal chain/replay
capture, pilot investigate/progress, exact avoided-Complete Tumbler golden, and
`make test-pilot`.

Profile again after this lands. Smaller iteration or zoom costs may remain, but
they should be justified by the new profile rather than folded into this
executor redesign.

---

## F. Boundaries to preserve

These are not current cleanup targets.

- Do not create a generic `receipts.py` or grow `types.py` into a universal dumping
  ground. Define a receipt beside its owner when dependencies allow; use
  `navigation.py`/`types.py` only for genuinely neutral cross-module contracts.
- Do not split files before the object flow exposes a cohesive owner. Reassess
  `options.py`, `trace.py`, and `progress.py` after B/D, not before.
- Keep live transition reading (`currents.py`) distinct from snapshot-free static
  charts. They answer related questions from different evidence and may disagree.
- Keep `_learned_reachable` distinct from `StaticTransitionGraph.find_path`; their
  evidence and scoping differ.
- Keep the two program-constant definitions phase-local until a concrete bug shows
  that their available inputs can support one owner.
- Keep `outcome.classify_outcome` as the small ergonomic compatibility projection;
  remove stored duplicate outcomes from trial receipts instead.
- Keep `steer._settle_cone`; it is the thin execution adapter around
  `CoastSession.settle` and has parity coverage.
- Do not turn policy ladders into dispatch tables merely to remove `isinstance`.
  Dispatch is useful only when handlers become independently named owners, as in
  C3.
- Do not move private helpers solely to reduce file size. A move must reduce a
  decision seam or dependency reach.

---

## Sequence

1. **Correctness:** A2/A3.
2. **Navigation continuity:** B1, then B2.
3. **Trial evidence continuity:** B3, B4, then B5.
4. **Shared decisions:** C1 and C2.
5. **Control flow:** D1/D2/D3.
6. **Recovery continuity:** B6, then D4.
7. **Compiled residual replay:** E1, E2, then E3.
8. **Diagnostics and type hardening:** C3, C4, D5.

After each step, remove the landed item and update `pilot/CLAUDE.md` so its
ownership table names the object now carrying the decision.

The expected LOC reduction is deliberately not totaled. B1-B5 and D1-D4 should
remove meaningful code, but the acceptance criterion is a shorter reasoning path:
one owner, one receipt, and consumers that apply rather than reconstruct.
