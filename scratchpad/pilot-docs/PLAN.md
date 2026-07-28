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
- `navigation.ActSource` replaces four stored provenance booleans on the private
  `_Candidate`, then travels inside the selected act's immutable `ActPolicy`.
- `trace.TraceReadConstraints` owns the common trace-read bundle.
- `types._ExecutionEvidence` is VERIFY's frozen, PLC-free receipt for the final
  accepted snapshots, channel landing, coast receipt, and exact timeline.
  `_AcceptedTrial` and the committed `_StepContext` share that object; the
  original `ActPolicy` remains a separate declaration, while frontier tags and
  executable rungs remain commit-owned.
- `_HoldLogEntry.tags` and `_StepContext.steady_holds` are derived from executable
  rung evidence rather than stored in parallel.
- `navigation_evidence.StaticEdgeAdmission` owns the current-world answer to
  whether one static chart edge may participate in a path search. Options,
  frontier evidence, and detour consume its Boolean projection; detour composes
  the recovery-only `ContinuationSafety` decision around it.
- `trace._TraceSelection` owns the common precedence among unlocked local trace
  alternatives while each caller supplies its exact rank. Its alternatives
  retain literal avoid, dead-end, and exact-rejection facts; alternative-specific
  root locks and complete-route ranking remain separate. Subroutine callers are
  distinct program contexts: they share avoid and coherence selection but
  do not redirect on exact action rejection.

Those are the pattern to continue: construct evidence once, keep it typed, and
let later modules consume the object.

### Target object flow

The cleanup is aiming for this chain:

```text
options          orientation        steer             verify
CandidateRead -> Bearing          -> ExecutedAttempt -> AcceptedTrial
                 Act + Policy        declaration +      attempt + frozen
                 Objective           physical pulse     execution evidence
                                                        + GaugeReceipt
                                                            |
                                                            v
pilot / progress   CommittedOperation -> DepartureResult      -> PendingDeparture
                   policy + shared      observation + typed     opening observation
                   execution evidence   classification          + rollback owners
                   + commit-owned rungs
```

Each arrow should carry the object on its left intact. A downstream object may
compose new evidence around it, but should not copy selected fields and recreate
its meaning.

## C. Give repeated decisions one owner

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

## G. Apply the grounded naming backlog

The naming work is maintained separately in
[`RENAME_PLAN.md`](../../src/pyrung/core/analysis/pilot/RENAME_PLAN.md). Apply
its approved mechanical tranche only after the ownership work above, then
re-ground the deferred concept names against the owners that actually landed.

---

## Sequence

1. **Compiled residual replay:** E1, E2, then E3.
2. **Diagnostics and type hardening:** C3, C4, D5.
3. **Naming:** the approved tranche in G, then re-audit the deferred names.

After each step, remove the landed item and update `pilot/CLAUDE.md` so its
ownership table names the object now carrying the decision.

The expected LOC reduction is deliberately not totaled. The acceptance
criterion is a shorter reasoning path: one owner, one receipt, and consumers
that apply rather than reconstruct.

## Working pipeline

The safe parallel pattern is a pipeline, not parallel edits to the same
subsystem:

1. One agent implements the current item.
2. Another agent audits the next item against committed `HEAD`.
3. The primary agent reviews and gates the current diff.
4. Only then does the next implementation begin.

This overlaps design grounding with the long test gate while preserving a
single attributable implementation change and a meaningful first deviation.
