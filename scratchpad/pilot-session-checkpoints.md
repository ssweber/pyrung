# Forkable and saveable PILOT sessions

Status: design sketch only.

## Goal

Make a PILOT drive an explicit value that can be paused at a decision boundary,
forked in memory, inspected in a test, and eventually saved and restored:

```python
pilot = PilotSession.start(
    plc,
    y_BurnerLoop,
    avoid=Cmd_State_Complete,
    max_scans=20_000,
)

event = pilot.advance()       # run through exactly one PILOT decision
held = pilot.fork()           # independent in-memory branch

checkpoint = pilot.snapshot()
checkpoint.save("held.pilot.json")

restored = PilotSession.restore(program, "held.pilot.json")
restored.advance()
```

The first useful cut is in-memory `fork()`. Durable `snapshot()` / `restore()`
comes after its semantics are proven.

## What is being forked?

`PLC.fork()` is necessary but insufficient. It copies the running machine:
tags, accumulators, edge state, clocks, retained history, harness state, and
causal-log ancestry.

`PilotSession.fork()` copies the complete steering attempt:

```text
PilotSession
|-- static drive analysis (shared)
|   |-- program / PDG
|   |-- steerable and edge tags
|   |-- resting values and finite domains
|   |-- opaque-pipeline catalog and static transition graphs
|   `-- gauge definition
|-- target and user constraints (shared values)
|   |-- target / relational predicate
|   |-- avoid / via route lock
|   `-- scan budget
|-- current world (forked)
|   |-- PLC work fork
|   |-- committed steps and step contexts
|   |-- active PilotRungs
|   |-- best trend
|   `-- coast-dwell credit
|-- rollback stack (forked worlds)
|   `-- checkpoints + captured frontiers
`-- committed knowledge (copied or structurally shared)
    |-- CompassKnowledge
    |-- seen world keys
    |-- active provisional attempt
    |-- journey and hold log
    |-- avoid names and lever notes
    `-- last wait record
```

This maps almost directly onto today's objects:

- `_DriveSetup`: shareable static drive analysis.
- `_PilotContext`: target binding plus the current immutable `Compass`.
- `_PilotState.world`: current revertible `_World`.
- `_PilotState` fields outside `world`: knowledge that survives a revert.

The important distinction remains:

> Knowledge commits; the world reverts.

Forking branches both halves. A later regression inside one branch must not
change its sibling.

## Boundary semantics

A session may be captured only at a **decision boundary**:

- after all observations from the previous act have been applied;
- after a committed trial has been retained, reverted, or made provisional;
- with no trial fork currently under verification;
- with no CoastSession seek or fixed dwell in flight;
- before the next `Compass.orient(...)`.

This is the top of `_pilot_loop_events`' `while` body.

Do not attempt to serialize:

- a Python generator instruction pointer;
- a partially applied pulse;
- an in-flight candidate fork;
- a coast halfway between armed bumps;
- investigation halfway through competing hypotheses.

`advance()` completes the current atomic decision and pauses at the next
boundary. A long coast remains one atomic decision for the first cut. A future
monitoring API could surface coast receipts without making them resumable.

## Proposed public surface

### Session

```python
class PilotSession:
    @classmethod
    def start(
        cls,
        plc: PLC,
        *conditions,
        max_scans: int = 3000,
        avoid=None,
        via=None,
        unlink: list[str] | None = None,
    ) -> "PilotSession": ...

    @property
    def scan(self) -> int: ...

    @property
    def done(self) -> bool: ...

    @property
    def current_world(self) -> PLC:
        """Read-only view or defensive fork, not the mutable owned runner."""

    def advance(self) -> tuple[PilotEvent, ...]:
        """Execute one orient -> act -> observe -> retain/recover transaction."""

    def run(self, *, on_event=None) -> Plan:
        """Advance until finished; compatibility engine for plc.how()."""

    def fork(self) -> "PilotSession":
        """Independent in-memory branch at the current decision boundary."""

    def snapshot(self) -> "PilotSnapshot":
        """Portable dynamic state with a program fingerprint."""
```

Possible convenience for tests:

```python
events = pilot.advance_until(
    lambda event: event.kind == "provisional_started",
    max_decisions=10,
)
held = pilot.fork()

next_events = held.advance()
assert next(
    e.data["applied"] for e in next_events if e.kind == "candidate_try"
) == (
    ("Cmd_State_Unhold", True),
    ("x_DoorClosed", False),
)
```

### Snapshot

```python
@dataclass(frozen=True)
class PilotSnapshot:
    schema_version: int
    program_fingerprint: str
    target: TargetSnapshot
    constraints: ConstraintSnapshot
    world: WorldSnapshot
    checkpoints: tuple[CheckpointSnapshot, ...]
    knowledge: KnowledgeSnapshot

    def save(self, path: str | Path) -> None: ...

    @classmethod
    def load(cls, path: str | Path) -> "PilotSnapshot": ...
```

Keep `PilotSnapshot` data-only. It must not contain a live `PLC`, PDG node,
callable predicate, generator, or condition object.

## In-memory fork algorithm

The first implementation should not involve serialization:

```python
def fork(self) -> PilotSession:
    assert self.at_decision_boundary

    child_state = copy_pilot_state(self._state)
    child_context = replace(
        self._context,
        compass=self._context.compass,  # immutable; safe to share
    )
    return PilotSession(
        setup=self._setup,              # immutable/static; safe to share
        context=child_context,
        state=child_state,
        terminal=self._terminal,
    )
```

`copy_pilot_state` needs deliberate field handling:

| Field | Fork behavior |
|---|---|
| current `_World.work` | `PLC.fork(history_budget=...)` |
| steps / step contexts / rungs | share persistent PVectors |
| best trend / dwell credit | copy scalar |
| each checkpoint world | retain persistent values, but ensure any later load forks its PLC as today |
| Compass | share immutable value; branches replace it independently on `apply` |
| seen keys / avoid names | copy sets |
| checkpoints / watch tags / journey / hold log | copy lists |
| provisional / gauge | immutable or treated as values; share |
| lever notes | copy dict |

One subtlety: `_Checkpoint.world.work` is a mutable PLC reference. Current
checkpoint code relies on never advancing that runner and always re-forking it
on `load_world`. A session fork may share those dormant checkpoint runners if
that invariant is made explicit; defensively forking each checkpoint is safer
but potentially expensive. Start safe, then measure.

## Refactoring the loop

Today `_pilot_loop_events` owns both the state and the `while` loop. Its local
`state` cannot be captured through the public event iterator.

Split one loop turn into an operation over explicit session state:

```python
def _advance_one(session: PilotSession) -> tuple[PilotEvent, ...]:
    state = session._state
    ctx = session._context

    # Existing top-of-loop target and budget checks.
    # Existing Compass.orient.
    # Existing execute + _record_attempt.
    # Existing observation application.
    # Existing _commit_and_monitor.
    # Return only after the world/knowledge transaction is complete.
```

Then the current entry points become adapters:

```python
def pilot_events(plc, *conditions, **kwargs):
    session = PilotSession.start(plc, *conditions, **kwargs)
    while not session.done:
        yield from session.advance()


def pilot_how(plc, *conditions, **kwargs):
    return PilotSession.start(plc, *conditions, **kwargs).run()
```

This should be behavior-preserving. The existing decision skeletons are the
cutover gate.

## Durable snapshot contents

### Rebuild rather than serialize

Do not persist static analysis:

- ProgramGraph;
- steerable analysis;
- opaque-loop slices;
- pipeline roles and static graphs;
- reference constants;
- gauge component discovery;
- route enumeration caches.

Restore rebuilds those from the supplied program, then overlays dynamic
snapshot state.

This keeps snapshots smaller and avoids encoding internal graph classes.

### Persist dynamic world state

The PLC snapshot must include more than tag values:

- committed `SystemState`;
- timer/counter/edge memory represented in that state;
- scan id and time mode / `dt`;
- RTC basis if wall-clock instructions depend on it;
- sufficient causal history for future investigation and replay;
- harness configuration and `unlink` choices;
- active synthesized `PilotRung`s.

The cleanest long-term seam is a runner-owned `PLC.snapshot()` /
`PLC.restore(program, snapshot)`. PILOT should compose it rather than know
runner internals.

For the first durable prototype, storing only the current state would make a
restored drive executable but could make later `cause(scan=...)` or incident
replay weaker than the original. Such a snapshot must be named "state-only,"
not silently presented as full fidelity.

### Persist rungs structurally

Do not pickle condition objects. Encode supported conditions as an AST:

```json
{
  "dest": "x_DoorClosed",
  "value": true,
  "guard": {
    "op": "eq",
    "tag": "Sts_StateCurrent",
    "value": 12
  }
}
```

Required guard nodes include:

- truthy / negated Bool;
- eq, ne, lt, le, gt, ge;
- all / any;
- rising / falling edge only if a PilotRung can legitimately own one;
- any explicit oscillator rule representation used by liveness corrections.

Restore resolves tag names against the supplied program and fails closed on an
unknown tag or unsupported condition node.

### Persist Compass knowledge

`CompassKnowledge` is already data-oriented and persistent, but its PMap keys
need explicit encoding:

- entries: `(tag, from_value, cause) -> (to_value, provenance)`;
- action nogoods by world key;
- probe counts and declines;
- coast receipts;
- static-edge overlays.

Causes are either:

- `WAIT`;
- one `(tag, value)` action;
- a composite tuple of action pairs.

World keys and values need a tagged scalar codec so `False`, `0`, strings,
floats, tuples, and sentinel values do not alias in JSON.

Static-edge overlays refer to static edge identities. Restore must verify that
each identity still exists in the rebuilt catalog; stale entries are an
incompatibility error, not quietly ignored knowledge.

### Persist rollback checkpoints

Each checkpoint needs:

- executable world snapshot;
- world key;
- trend;
- captured frontier;
- steps, step contexts, rungs, and dwell credit belonging to that world.

Naively embedding a full PLC history in every checkpoint will be large.
Possible representations:

1. **Simple and safe first:** independent full snapshot per checkpoint.
2. **Content-addressed states:** one state/history object table, checkpoints
   reference object ids.
3. **Anchor + deltas:** smallest, but adds replay/versioning risk and should not
   be the first implementation.

Correctness is more important than compactness for diagnostic checkpoints.

## Program identity and restore validation

Every snapshot carries a deterministic program fingerprint. It should cover:

- ordered subroutines and rungs;
- instruction types and operands;
- declared tag names/types/defaults/choices;
- mapped timer/counter registers;
- synthesis ABI/schema version.

Restore performs:

```text
schema supported?
program fingerprint matches?
all target/constraint tags exist?
all saved state values fit their tag types?
all PilotRung destinations and guard tags resolve?
all Compass static-edge identities still exist?
all checkpoint world keys can be rebuilt under the current key config?
```

Any mismatch raises `PilotSnapshotIncompatible` with the first concrete
difference. Never resume partially.

## Avoid/via predicates

Arbitrary Python callables are not portable. Durable snapshots should support
only constraints lowered from the public condition DSL and encode their AST.

An in-memory `fork()` may retain any callable because it stays in the same
process. `snapshot()` should reject a non-serializable predicate with a clear
message:

```text
cannot save PILOT session: avoid predicate is an opaque callable;
use Tag/Condition expressions for durable checkpoints
```

## History and scan identity

Preserving scan ids matters because steps, incidents, cause chains, hold logs,
and timelines refer to them.

A restored session should continue from the saved scan id. It must not renumber
the restored tip to zero. If the runner cannot yet restore an absolute scan
timeline, durable PILOT snapshots should wait for that seam rather than patch
ids after construction.

Forked sessions may share immutable causal ancestry, just as `PLC.fork()` does
today. A disk snapshot must materialize the required ancestry explicitly.

## Test strategy

### Phase 1: in-memory fork

1. Drive a small program to a decision boundary.
2. Fork the session twice.
3. Advance one branch with a learned observation/correction.
4. Assert the sibling's PLC world, Compass, nogoods, steps, and checkpoints are
   unchanged.
5. Advance both branches from identical snapshots and assert identical decision
   skeletons.

Use the Held checkpoint smoke in
`test_pilot_detour_hold_release.py` as the first consumer:

```python
held = pilot.fork()
events = held.advance()
assert next_applied(events) == (
    ("HRel_C_Unhold", True),
    ("HRel_DoorClosed", False),
)
```

### Phase 2: snapshot value round trip

Round-trip `PilotSnapshot -> dict/JSON -> PilotSnapshot` and compare canonical
data. No execution yet.

### Phase 3: restore execution

For each checkpoint era:

- run continuously from the boundary to the next decision;
- snapshot/restore at the boundary and run to the next decision;
- compare decision skeletons and exact applied actions.

Required eras:

- cold Aborted before Clear/Production co-actions;
- Starting before the door correction;
- Starting with completion prerequisites installed;
- early Held caused by expired door holds;
- recipe-owned Held after the step gauge advanced;
- Execute before rotate-sensor liveness correction.

This is the fast substitute for replaying `how(y_BurnerLoop)` end-to-end in
every focused regression.

### Phase 4: incompatibility gates

Restoring against a program with one changed rung, tag type, timer mapping, or
static edge must fail with a pointable reason.

## Staged implementation

1. Introduce private `_PilotSession` around `_DriveSetup`, `_PilotContext`, and
   `_PilotState`.
2. Extract one decision transaction from `_pilot_loop_events`.
3. Keep `pilot_events` and `pilot_how` as byte-for-byte behavior-compatible
   adapters; run all PILOT goldens.
4. Add in-memory `fork()` and use it in fast checkpoint tests.
5. Promote the class/name publicly after the API feels stable.
6. Add `PilotSnapshot` as a data-only in-memory value.
7. Add runner-level `PLC.snapshot()` / restore with causal-history fidelity.
8. Add structured codecs for PilotRungs, constraints, CompassKnowledge, and
   checkpoint stacks.
9. Add JSON save/load and program fingerprint validation.

## Decisions to settle before coding

1. Public name: `Pilot`, `PilotSession`, or `PilotDrive`.
   `PilotSession` describes the stateful/resumable object most precisely.
2. Does `advance()` mean one orientation attempt or one committed decision?
   Recommend one complete orient/execute/observe/recover transaction.
3. May a snapshot be requested during a coast?
   Recommend no for the first cut: finish the atomic decision, then snapshot.
4. Does a fork share dormant checkpoint PLCs?
   Recommend deep/fresh forks initially; optimize only with measurements.
5. How much causal history is durable?
   Full fidelity should be the contract; state-only snapshots, if offered,
   need a separate explicit type/name.
6. Is JSON the canonical format?
   Recommend a versioned data model with JSON as the first codec, not JSON
   assumptions inside the session API.

## Smallest useful landing

The minimum feature that immediately improves development is:

```python
pilot = _PilotSession.start(plc, target, ...)
pilot.advance_until(lambda e: e.kind == "provisional_started")

held = pilot.fork()
assert next_applied(held.advance()) == expected
```

No disk format is needed for that landing. It gives fast, exact checkpoint
tests and forces the loop/state ownership seam into the right shape. Durable
save/restore can then build on a proven fork model instead of designing a file
format around hidden generator locals.
