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

1. **Naming:** the approved tranche in G, then re-audit the deferred names.

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
