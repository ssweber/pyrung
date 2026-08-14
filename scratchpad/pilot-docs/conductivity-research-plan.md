# Conductivity research execution plan

This plan continues the WorkingTheory work after the first-class
`ConductivityFront` boundary. It supersedes the atomic `correction + command`
examples in `working-theory-plan.md` for intrascan temporal recovery.

The governing loop is:

```text
steer
→ inspect the exact executed scan
→ research one repeated conductivity stop
→ record one evidence-only finding
→ compose one correction without a PLC scan
→ reread Compass
→ steer
```

WorkingTheory remembers the immutable history. Compass reads the current
World and decides what happens next. Neither component retains a future action.

## Landed baseline

- Exact intrascan effect observations pass through WorkingTheory unchanged.
- `ConductivityFront` derives occurrence order, consumer reach, and the exact
  displacement boundary from that retained history.
- No-scan correction composition changes the executable World and advances the
  theory progress tip without advancing the physical scan.
- Compass returns `NeedResearch` when consecutive attempts stop at the same
  writer and a causally joined requirement changes.
- The runner records `conductivity_research_requested`, composes one correction,
  and yields to a fresh Compass read before another steer.
- The process-isolated watcher counts steer, composition, probe, and research
  decisions and enforces wall, stall, output, and RSS limits.

- A charted producer receipt may end at one unique automatic outer consumer;
  exact execution still proves that the occurrence crossed that boundary.
- A selected-producer execution window can retain the immediately observed
  actionless successor without requiring a coast or settling past the target.
- The alarm, neutral WorkingTheory, scan-zero, and already-stepped reachability
  contracts are green under the non-atomic lifecycle.

## Next steps

### 1. Record one research finding

Introduce a frozen, detached `ConductivityResearchFinding` and a typed
WorkingTheory fact. The finding owns only:

- the exact comparison identity;
- the repeated displacement occurrence;
- the exact stopping reads;
- the requirement-drift identities;
- the theory version and same-scan World that requested the research.

It must not contain a `Bearing`, correction value, pilot rung, fork, checkpoint
object, or proposed future action. Recording it consumes no PLC scan. The first
slice stops immediately after emitting its receipt.

### 2. Let Compass acknowledge completed research

Expose findings through `TheoryView`. A matching finding suppresses only the
identical `NeedResearch` request. Compass then rereads the same current World;
it does not reuse the candidate read that preceded research.

### 3. Compose exactly one newly justified correction

With the repeated stop researched, normal temporal lowering may select one
correction for the latest live requirement. Replacing an older correction for
the same destination must be an explicit same-scan World transition, not an
atomic command batch and not an additional physical scan.

### 4. Steer once and inspect again

After composition, Compass rereads the changed World and chooses one ordinary
steer. If that scan exposes another stop, WorkingTheory retains it and the loop
returns to research. No phase automatically chains a second steer.

### 5. Prove three sequential issues in the neutral fixture — complete

Extend or variant the neutral route so one invocation demonstrates:

```text
steer → compose A → steer → research → compose B → steer
      → research → compose C → steer
```

Assert exact occurrence ownership and same-scan World changes at every phase.

### 6. Restore reachability contracts — complete

Turn the five intentionally red tests green without weakening their target,
replay, authority, or scan-count contracts. Update only assertions that still
encode atomic composition.

### 7. Replay the ClickNick/HeelStep-shaped case — complete

Run both scan-zero and already-stepped entry Worlds. Entry state must come from
the runner's actual World; bootstrap must not invent a prior scan or bypass
Compass evidence.

### 8. Remove legacy residue — next

Sweep stale atomic-composition language, shadow-era names, tuple protocols,
and folded recovery helpers only after their replacement receipts have focused
coverage.

## OOM-safe execution protocol

Never use an unbounded acceptance run while this plan is active.

1. Prove reducers and derivations with pure focused tests first.
2. Run only the directly affected integration test, with an internal transition
   cap that raises if the expected receipt does not appear.
3. Replay the real route only through `devtools/watch_pilot_decisions.py`.
4. Use `--memory-budget-mb 768`, `--stall-budget 8`, and an explicit
   `--decision-budget` ending at the next expected decision.
5. Increase the decision budget by one only after inspecting the prior receipt.
6. Do not run the monolithic suite until the five reachability contracts are
   expected to pass.

## Completion criteria

- Research, composition, and steering are three distinct decisions.
- Every phase yields to a fresh Compass read of the current World.
- WorkingTheory retains multiple sequential findings and corrections.
- No phase invents a scan, replays proved work, or stores a future action.
- The neutral and ClickNick-shaped pre-stepped runs reach and replay the target.
- The bounded watcher remains below its memory limit and stops at every chosen
  decision boundary.
