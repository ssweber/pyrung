# PILOT Refactoring Charter

The product is `how(State)`: an evidenced explanation of how to reach a state,
from reset or a tag dump, with no domain annotation. Every structural decision
serves that. Receipts are the product's internals, not overhead.

## Target architecture

- **kernel/** — world, fork, identity, verify, commit, the drive loop, the
  ledger. The soundness-critical set. Small, trusted, owns all mutation.
- **instruments/** — pure readers. `read(world, target, board) -> Reading`.
  Return facts and unresolved frontiers; never choose actions, never mutate,
  never fork.
- **probes/** — fork-licensed readers (program_step, skiff) with a declared
  `ForkBudget`, kernel-enforced. Report readings; only skiff may propose.
- The instrument/probe registry replaces the one-owner prose. A new reader is
  a file plus one registry entry.

## Patterns (with the reference example)

1. **Evidence / verdict split** — impure gathering returns one frozen
   evidence record; a pure function classifies it. See: program_step
   (`StepEvidence` / `classify_step`). Applies to: verify, departure.
2. **Proposer + single gate** — precedence is an ordered tuple of small
   generators yielding `(act, rationale)`; admission is checked once. See:
   orientation `PRECEDENCE`. Statement-order precedence is a bug.
3. **Declared needs** — no `getattr(ctx, x, default)`. Dependencies are typed
   fields or declared `needs`; absence fails loud at construction.
4. **Declared budgets** — every fork-consuming reader states its fork/scan
   budget; the kernel enforces it. Self-policed loops are a smell.
5. **Bills and receipts** — a bill (expectation, requirement, hold) binds the
   future and must be discharged or explicitly expired; a receipt records the
   past and is immutable. Every bill has a receipt path; every receipt traces
   to a bill or a named observation.
6. **Suffix legend** — `*Expectation`/requirement = bill. `*Receipt` =
   settled immutable past fact with identities. `*Observation` = detached, no
   authority; only `Compass.apply` promotes. `*Verdict`/`*Proof` = terminal,
   requires a complete domain. `*Knowledge` = accumulated, scoped, survives
   revert. A type violating its suffix is a boundary crack.
7. **One identity vocabulary** — occurrence, act_identity, world_key defined
   once, consumed everywhere. No local near-duplicates.

## Rules of extraction

- **Two consumers or stay inline.** A primitive earns existence by reuse.
- **Every receipt has a consumer** — verify, progress, the oracle, or the
  rendered `how` prose. Unread records get a reader or get deleted.
- **Composition stops at exactness contracts.** Algorithms with proofs are
  not pipelines.
- Deletion test: removing a module may cost reach or speed, never soundness.
  Soundness lives only in the kernel.

## Do not touch

- verify's gate order; the strictly-decreasing ordinal walk; program_step's
  classification cascade order; occurrence/epoch identity rules; the
  complete-finite-domain requirement for permanent rejection; "budget
  exhaustion is never impossibility."

## Process

- **Sweep by pattern, not by file.** One branch = one pattern applied
  everywhere it fits. Homogeneous diffs only.
- **Gate mechanically.** Per file: transform → `pilot_divergence` →
  `make test-pilot` → commit. Structural sweeps are decision-identical; a
  divergence means the transform leaked semantics — revert that file.
- **Exceptions feed the charter.** A file that resists a pattern is charter
  input: state the pattern's boundary or record the exemption. Never force it.
- Order: suffix audit → identity consolidation → declared needs →
  evidence/verdict → proposer/gate. Kernel extraction (the pilot.py cut the
  working-theory plan already mandates) is its own tracked effort, done
  before 6B.
