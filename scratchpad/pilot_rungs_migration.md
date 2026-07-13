# PILOT evidence-axis migration

This document records the completed architectural migration and the remaining
gates. The design follows `pilot/CLAUDE.md`: sail by a freshly read bearing,
observe rather than invent knowledge, and reject only on proof.

## 1. PilotRungs are executable world state — landed

- All sustained steering is `PilotRung(dest, value, guard)`; the guard is
  required and owned by the proposer.
- PilotRungs are ordered, persistent world state. Active writes execute in
  append order; the last active rung wins.
- Checkpoints snapshot PLC, steps, trend, dwell, and PilotRungs together.
- Revert restores that exact world and rebuilds the synthesis overlay.
- Executable identity and nogoods use PLC projection plus the semantic rung
  fingerprint.
- Patches/pulses remain one-act steering. Momentary commands are never rungs.

## 2. Judgment is evidence on independent axes — landed

`outcome.assess_outcome` returns:

- agency: PILOT / PROGRAM / UNKNOWN;
- immediate bearing: SATISFIED / DEPARTED / UNCHANGED / EXPOSED;
- target progress: ADVANCED / PRESERVED / BEHIND / UNKNOWN;
- novelty: whether a new frontier was exposed; and
- accepted: the policy projection used by VERIFY.

`Outcome` remains a compatibility projection only. Trial shape and
`observe_label` do not select different decision procedures.

Only the immediate requested channel value satisfies the bearing. Stored suffix
membership, `expected_channel_values`, and route-return inference were removed.

Unknown remains epistemically distinct from regression. Operationally, unknown
without affirmative continuation evidence is conservative: investigate/revert,
not free exploration. No negative knowledge is minted merely because evidence
was unavailable.

## 3. Investigation owns correction applicability — landed

Investigation now:

1. builds the bounded incident from recorded scans;
2. proposes competing causal explanations;
3. replays one proposal in the exact source world;
4. observes the corrected landing;
5. derives a finite guard from that evidence;
6. replays the exact guarded PilotRung; and
7. confirms only that installed form.

Latched-failure receipts are filtered to the deep causal spine of the observed
channel departure. Raw corrective replay may follow safe automatic motion past
a waypoint to discover the stable landing; guarded replay stops at the first
landing transition, matching the point where the live loop re-orients.

ASSESS installs confirmed PilotRungs verbatim. The former scope ladder and the
second global `hold_defeats_needed` reinterpretation were removed. Static
self-defeat remains useful for ordinary unscoped trace prerequisites, but it
cannot veto a finite correction that exact guarded replay already proved.

No-op rejection now accounts for observed conflicting co-actions. An input that
was true before an incident is not a no-op correction when the failed act
explicitly drove it false.

An active correction owns its destination until the guard releases. ORIENT may
still report an opposite backward-trace need, but it cannot append an opposing
last-write-wins rung while the correction is active.

## 4. Program departures are ordinary bounded piloting — landed

A departure is classified from observed evidence:

- gauge behind the exact pre-act receipt -> proven regression;
- clean continuation in the current charts, avoiding known resets and
  resurrected obligations -> provisional;
- missing graph, unresolved reset, or no proven clean continuation -> unknown.

Unknown does not become a regression fact and does not authorize wandering. It
uses the conservative investigation/revert behavior.

The active provisional state contains only:

```text
channel + departure value
gauge receipt at the observed departure world
rollback checkpoint depth
start/expiry scans
classification receipt
```

It carries no route. Every ORIENT queries the current compass again.

Settlement never waits for a channel value to recur:

- ADVANCED or target reached -> `provisional_promoted` and checkpoint;
- BEHIND -> `provisional_regressed`, ordinary investigation, revert;
- PRESERVED / UNKNOWN -> keep piloting within the bound;
- bound drained -> `provisional_expired`, rollback with no regression nogood.

The pre-act receipt and provisional gauge receipt deliberately differ. Replay
and regression classification use the exact pre-act world. The provisional
gauge starts at the observed departure world, preventing progress earned during
the triggering coast from being counted twice.

## 5. Live route reading — landed

`routes.live_compass_plan` is a read-side ORIENT instrument. It recomputes the
best bearing from the current snapshot every iteration and filters disallowed
actions before BFS, including actions later in a prospective path.

No route suffix is stored in provisional state. Local backward-trace work has
priority; after the world changes, the next ORIENT re-reads the charts.

Avoid predicates participate in BFS edge filtering against the live snapshot.
When a forbidden operator edge has a statically proven program-owned sibling
producer for the same command value, chart construction retains a parallel
automatic edge. Captain constraints remove the button, not the PLC's current.

## 6. HELD safety gate — green

The focused door-cycle gate proves:

- program Hold opens a provisional attempt at HELD / Step 103;
- local door-open work advances Step 105 and promotes immediately by gauge;
- live route reading supplies Unhold;
- unsafe Unhold exposes a generic latched failure;
- investigation derives and exact-replays the guarded door-close correction;
- active correction ownership prevents the backward trace from overwriting it;
- corrected Unhold reaches Execute and the guard yields there; and
- the machine reaches Completed without pressing the avoided Complete command.

Current full result: `374 passed, 31 skipped, 1 expected failure`. Ruff and ty
pass.

## 7. Remaining work

1. Resolve correction handoff/local recipe work at live HELD / Step 101; the
   solver reaches it at scan 916 but has not yet emitted Step-105
   `provisional_promoted`.
2. Keep sterile-zoom classification on the same evidence axes.
3. Treat cycle-fold performance as an independent instrument improvement.
4. Add second-machine gates before broadening gauge clue families.

Do not introduce a carried transaction object, stable-landing table, waypoint
list, or trial-kind decision branch. Those recreate stored routes and multiply
rules instead of improving PILOT's classification instruments.
