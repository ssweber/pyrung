# PILOT Refactoring Charter

The product is `how(State)`: an evidenced explanation of how to reach a state,
from reset or a tag dump, with no domain annotation. Every structural decision
serves that. Receipts are the product's internals, not overhead.

## Target architecture

- **kernel/** — world, fork, identity, verify, commit, the drive loop, the
  ledger, and progress. The soundness-critical set. Small, trusted, owns all
  mutation. The kernel exposes explicit `decide` (orientation) and `execute`
  (coast, pulse, cyclefold, overlay) slots. Until Phase 8 relocates the current
  leaks, avoid and coast-time avoid enforcement, requirement-recovery gates,
  recovery assertions, and the effects terminal veto are also kernel
  responsibilities regardless of their present file locations.
- **instruments/** — pure readers. `read(world, target, board) -> Reading`.
  Return facts and unresolved frontiers; never choose actions, never mutate,
  never fork.
- **probes/** — fork-licensed readers (program_step, skiff) with a declared
  budget shape. The caller declares the amount and the kernel enforces it.
  Report readings; only skiff may propose navigation acts. Correction readers
  may propose installable `PilotRung` values under their separate authority.
- **render/** — prose, event, plan, and diagnostic rendering. Rendering owns no
  drive decision.
- The instrument/probe registry replaces the one-owner prose. A new reader
  function has one registry entry describing one of these shapes:
  `read(world, target, board) -> Reading`, `read(evidence) -> Reading`, or a
  declared fork-licensed probe shape. Registry entries are per function, not
  per file.

## Patterns (with the reference example)

1. **Evidence / verdict split** — impure gathering returns one frozen
   evidence record; a pure function classifies it. See:
   `attempt_interpretation.interpret_attempt` and `earned_work`'s receipt and
   verdict split. `program_step.StepEvidence` / `classify_step` is a Phase 6
   target, not a current reference. Applies to: verify, departure. Evidence may
   be gathered in stages when a later stage's cost is conditional on earlier
   gates passing; each stage still has one frozen record and one pure
   classifier, followed by a separate mutation step. Verify specifically
   gathers `TrialEvidence` for `classify_trial_gates`, then `LandingEvidence`
   for `classify_landing`; one eager record would violate its cost and gate-order
   constraints.
2. **Proposer + single gate** — precedence is an ordered tuple of small
   generators yielding `(act, rationale)`; admission is checked once.
   Orientation's `PRECEDENCE` is a Phase 7 target, not a current reference.
   Current partial exemplars are `options._select_wait` and
   `investigate._initial_hypotheses`. Statement-order precedence is a bug in
   policy cascades, not in exactness algorithms, proof continuations, or pure
   ranking. `_theory_retry_bearing` remains a fail-loud exactness contract
   outside the tuple.
3. **Declared needs** — no `getattr(ctx, x, default)`. Dependencies are typed
   fields or declared `needs`; absence fails loud at construction. Use `needs`
   where a reader must be denied fields as a capability restriction. Fail-closed
   validation of foreign payloads is exempt. A proposer re-resolving a retained
   identity may fail loud; a current-world proposer must decline. Narrow
   test-only contexts are not consumers of public readers.
4. **Declared budgets** — every fork-consuming reader states its fork/scan
   budget shape; the caller states the amount and the kernel enforces it.
   Fork and scan counters are independent. Counting and reporting may precede
   enforcement; only existing bounds can be enforced decision-identically.
   Exhaustion is typed exhaustion, never impossibility. `ForkBudget` is a
   Phase 5 target. Self-policed loops are a smell.
5. **Bills and receipts** — a bill (expectation, requirement, hold) binds the
   future and must be discharged or explicitly expired; a receipt records the
   past and is immutable. Every bill has a receipt path; every receipt traces
   to a bill or a named observation.
6. **Suffix legend** — `*Expectation`/requirement = bill. `*Receipt` =
   settled immutable past fact with identities. `*Observation` = detached, no
   authority; only `Compass.apply` promotes. `*Reading` = instrument output and
   may be `UNCLEAR`. `*Verdict`/`*Proof` = terminal and requires a complete
   domain. `*Judgment` = classification with an honest `UNKNOWN` arm. `*Entry`
   = revisable knowledge-table row; immutability belongs to the causing
   observation. `*Incident` = transient evidence window with no identity
   outside its investigation. `*Knowledge` = accumulated, scoped, survives
   revert. The suffix is mandatory where the plain noun would mislead about
   authority. A type violating its suffix is a boundary crack. Mutable
   execution buffers such as `_PulseState` become immutable receipts such as
   `_ExecutionEvidence` only at the named buffer-to-receipt transition.
7. **One identity vocabulary** — occurrence, act identity, and world key are
   defined once and consumed everywhere. There are exactly two named world-key
   scopes in `world_key.py`: navigable and proof. Occurrence identity has one
   constructor with an explicit `scan_scoped` flag. Recorded-history identity
   is not relocatable-projection identity; never merge `CausalOccurrence` with
   `EffectOccurrenceSnapshot`. No local near-duplicates.

## Rules of extraction

- **Two consumers or stay inline.** A primitive earns existence by reuse, an
  exactness contract, or a declared ownership seam.
- **Every receipt has a consumer** — verify, progress, the oracle, or the
  rendered `how` prose. Unread records get a reader or get deleted.
- **Composition stops at exactness contracts.** Algorithms with proofs are
  not pipelines.
- Deletion test: removing a module may cost reach or speed, never soundness.
  Soundness lives only in the kernel.
- Inert laboratories are not exemptions: promote them into the product path or
  delete them.
- Avoid enforcement, coast-time avoid checks, recovery invariants, and terminal
  displacement vetoes are kernel soundness responsibilities even where the
  current files have not yet moved. `progress.py` is already a kernel member by
  authority and mutation ownership.

## Do not touch

- verify's gate order; the strictly-decreasing ordinal walk; program_step's
  classification cascade order; occurrence/epoch identity rules; the
  complete-finite-domain requirement for permanent rejection; "budget
  exhaustion is never impossibility."

## Process

- **Sweep by pattern, not by file.** One homogeneous commit or commit series =
  one pattern applied everywhere it fits. All phases land sequentially on
  `dev`; task branches are not part of this effort.
- **Gate mechanically.** Per file: transform → `pilot_divergence` →
  `make test-pilot`. Before every commit run
  `make test-pilot; make test-tumbler; make lint`. Structural sweeps are
  decision-identical; a divergence means the transform leaked semantics —
  revert that file. Decision-affecting commits also regenerate goldens and run
  the specified watchers.
- **Exceptions feed the charter.** A file that resists a pattern is charter
  input: state the pattern's boundary or record the exemption. Never force it.
- Order: Phase 0 charter/free wins → Phase 1 suffix sweep → Phase 2 `pilot.py`
  kernel cut → Phase 3 identity → Phase 4 declared needs → Phase 5 budgets →
  Phase 6 evidence/verdict → Phase 7 proposer/gate → Phase 8 soundness
  relocation and directory materialization. Working-theory Stage 6B starts
  only after Phase 2.
