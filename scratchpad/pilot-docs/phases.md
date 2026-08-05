# Failed-effect recovery landing phases

This is the concise sequencing companion to
[`init-constant-recovery-plan.md`](init-constant-recovery-plan.md). The main
plan owns the design details; this document answers only: **what should land,
and in what order?**

The ordering rule is: record the event before interpreting it, preserve why a
candidate was selected before judging it, derive an exact deadline before
composing a correction, and reread the PLC after every scan.

## Phase 1 — Make scan 0 observable

- Retain boundary 0 as a causal checkpoint before the hidden first `step()`.
- Execute exactly one program-owned scan and retain its exact read/write
  projection and landing.
- Do not change recovery behavior yet; when nothing interpretable is found,
  continue from the same scan-1 world as today.

**Landed when:** tests pin boundary `0 -> 1`, the fixture's ordered reads and
writes, and a bootstrap execution receipt. No target reasoning is hidden inside
startup orchestration.

## Phase 2 — Designate target-relevant scan-0 work

- From the pre-scan trace, designate the target and conservative concrete
  program-written operation/channel handoffs on valid target paths. Keep each
  designation's path and consumer identity; do not watch every write or trace
  node.
- Observe only designated effects which actually appeared. Missing watchlist
  members are not failed promises.
- When the target remains unresolved, classify exact overwritten-before-
  consumption, stranded, or displaced effects as known violations. Correctly
  consumed values may legitimately advance afterward.
- Leave unrelated or ambiguous motion with ordinary progress or
  `PendingDeparture`.

**Landed when:** destructive startup identifies `AT_TARGET` and its exact
pre-boundary `ABORTED` displacement without relying on endpoint difference or a
search-scan timeout.

## Phase 3 — Pass “why this act” into every steer

- Define one atomic `EffectObligation`: selected producer/effect -> obliged
  consumer -> required local enabling/value-producing reads.
- Let an act-owned `EffectExpectation` carry one obligation normally, or an
  ordered conjunctive tuple for a genuinely multi-handoff path. A batch acts as
  a whole; its co-actions and holds are requirements, not independent
  expectations. Alternatives remain separate Bearings.
- Carry the selected obligation unchanged through `_Candidate` ->
  `ActPolicy`/`Bearing` -> execution. Never silently change the producer.
- Isolate adjustable `required_shape(selected path)` policy from factual
  `observed_shape(exact occurrence)` projection queries.
- Observe `ABSENT`, `OVERWRITTEN`, `STRANDED`, `DISPLACED`, or consumer-relative
  `SURVIVED` before generic spin/dead-end verification.

**Landed when:** pass-through tests prove the selected obligation survives all
lowering seams and bootstrap/ordinary steers use the same factual observation
contract.

## Phase 4 — Create exact active requirements

- Explain only the selected absent writer as guard-false, spent, not executable,
  or unknown. A false guard becomes a narrower trace requirement; another
  producer requires a later ordinary Orientation read.
- Emit a typed condition with its exact demanding `(scan, ordinal)` occurrence.
  An earlier same-scan writer is timely; a later writer is not.
- Allow causal inversion to move the actionable deadline upstream, such as
  `Done == False` at an alarm read becoming `Acc < Preset` at the timer's
  completion reads.
- Retain requirements separately, compile compatible same-phase assignments
  together, keep release/assert ordered, and reject only an incompatible exact
  repair.
- Expose active requirements to Compass/Orientation as constraints:
  **given this world and these active requirements, what can Pilot do next?**

**Landed when:** events say what was missing, which exact consumer needed it,
its deadline/scope, and the source causal checkpoint without yet assuming a
future PLC prefix.

## Phase 5 — Repair from the causal checkpoint and move forward

- If the deadline is past, match the exact expectation receipt and restore its
  source checkpoint, including boundary 0 when implicated.
- Execute only the repaired local steer/transaction. Do not retain or reapply
  historical actions.
- Remove the `RetainedReplay` act, retained-prefix execution, retained-Bearing
  composition, and independent retained blocker search. Rehome only exact
  occurrence addressing and causal-checkpoint selection if still needed.
- After every scan or phase, rebuild the projection/world and recalculate.
  Requirements may persist; predicted worlds, ordinals, and action suffixes do
  not.
- Mark `LOCALLY_REPAIRED` when the original whole-shape obligation still works
  and the new requirement is installed early enough. Adopt that landing and
  return immediately to ordinary Orientation.
- Keep a delayed requirement active until its real demanding occurrence marks
  it `DISCHARGED`; strengthen a failed timer/counter constraint only as a new
  exact attempt from observed Crossings/`AdvanceProfile` evidence.

**Landed when:** destructive startup and disposable failed-alarm fixtures reach
their targets through the same checkpoint/local-repair contract, with no
retained action prefix.

## Phase 5B — Rebase later program guards through retained history

- When an exact failed effect is blocked by a program-owned guard, search the
  retained writer timelines from the demanding boundary backward. Rank
  candidate transitions nearest to furthest; scan 1 is an ordinary candidate,
  not a special recovery destination.
- Treat the compressed timeline only as an index. Accept a candidate only when
  its owner-bound scan projection contains one exact good-to-bad writer and an
  executable retained checkpoint exists before it. Prefer the nearest valid
  checkpoint for the newest exact candidate.
- Retain the drive's invocation boundary at every scan number. If a live runner
  already crossed the guard before `how` began, reconstruct the exact boundary
  immediately before the newest candidate from retained runner history; a
  generated launcher's initial settle scan is not special.
- Complement that writer's exact enabled guard path into an ordinary Compass
  target. Restore the selected checkpoint, obtain a normal current-world
  Bearing, and send it through the existing steer, execute, verify, observe,
  and commit loop.
- Execute only that prevention Bearing. After it lands, discard the old
  prediction and return to fresh Orientation for the original target.

**Landed when:** both a scan-1 bootstrap writer and a later writer select their
nearest retained causal boundaries, locally repair through normal Bearings,
and reach the target without a program-owned false-impossibility report.

## Phase 6 — Complete and harden recovery

- Add exact one-shot spent/rearm evidence and reread between ordered
  release/assert phases.
- Isolate adjustable requirement lifetime policy:
  `ACTIVE | DISCHARGED | INVALIDATED | AMBIGUOUS`.
- Retain boundary 0, expectation-bearing source checkpoints, and active-corridor
  scan boundaries; prune only when no live obligation or incident refers back.
- Record exact repair attempts keyed by checkpoint/world, obligation/writer,
  phases, scopes, deadlines, and corrections. Only complete-domain evidence may
  create a nogood.
- Harden receipt ambiguity, masking/self-defeat, incompatible requirements,
  checkpoint pruning, budget exits, and reporting.

**Landed when:** the committed-spent fixture proves rearm, delayed requirements
discharge at their real consumers, Compass never silently drops an active
requirement, and no empirical failure is reported as impossibility.

## Dependency summary

```text
observable scan 0
    -> conservative target-relevant designation
    -> explicit producer-to-consumer obligation
    -> exact active requirement
    -> causal checkpoint repair and fresh forward orientation
    -> one-shot, lifetime, checkpoint, and proof hardening
```

Phases 1 and 2 expose useful evidence before changing steering. Phases 3
through 5 establish the common failed-effect loop. Phase 6 broadens that proven
loop; it must not introduce another orchestration path.
