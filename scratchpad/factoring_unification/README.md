# Factored successor unification

## Why the production fallback exists

Free-input factoring currently runs only inside the base successor's
single-outcome fast path in `prove/bfs.py`:

1. Execute the base input assignment.
2. Ask the hidden-event scheduler for settlement/jump outcomes.
3. If hidden outcomes exist, process the base and hidden successors.
4. Otherwise, process the base successor and then compose factored successors.

That makes factored input enumeration conditional on the base assignment not
having hidden outcomes. A timer/counter event on the base path can therefore
skip every factored combination. The production patch disables factoring for
programs with hidden-event specs and falls back to exact joint enumeration.

Moving the factoring block below the `if alt_outcomes` statement is not enough:
hidden processing mutates the shared kernel and leaves it restored to the last
hidden outcome rather than the base post-scan state.

## Target architecture

Separate one-scan successor construction from successor processing:

```text
parent snapshot
    |
    +-- ordinary assignment ----------+
    |                                 |
    +-- factored delta composition ---+--> ScanCandidate snapshots
                                          |
                                          v
                               one successor pipeline
                                          |
                       +------------------+------------------+
                       |                  |                  |
                    base state       settlement states    jump states
                       |                  |                  |
                       +--------- filter/project/enqueue ---+
```

The key rule is that hidden-event expansion happens after a complete one-scan
candidate has been constructed. It must run independently for every ordinary
or factored candidate.

## Proposed internal types

```python
@dataclass(frozen=True, slots=True)
class _ScanCandidate:
    snapshot: _KernelSnapshot
    inputs: dict[str, Any]
    child_flipped: bool


@dataclass(frozen=True, slots=True)
class _CandidateResult:
    intractable: Intractable | None = None
    ready: list[Counterexample | Proven | Intractable] | None = None
```

`_ScanCandidate` owns a complete post-scan snapshot. It must not be a partial
tag delta: the consumer should not need to know whether the state came from a
normal scan or factored composition.

## Candidate producer

Sketch:

```python
def _iter_scan_candidates(input_assignment):
    restore(parent_snapshot)
    apply(input_assignment)
    step()
    base = snapshot()
    yield _ScanCandidate(base, dict(input_assignment), child_flipped(...))

    if not factoring_active:
        return

    base_parts = copy_snapshot_parts(base)
    group_deltas = evaluate_groups_from(parent_snapshot, base_parts)
    for composed in product(*group_deltas):
        full_inputs = merge_inputs(input_assignment, composed)
        full_snapshot = compose_snapshot(base_parts, full_inputs, composed)
        yield _ScanCandidate(
            full_snapshot,
            full_inputs,
            child_flipped(full_inputs),
        )
```

Important details:

- Group evaluations always start from the parent snapshot.
- Deltas are measured against the base post-scan snapshot.
- The base snapshot is captured before any candidate enters hidden-event
  processing.
- Empty state deltas can still represent a paced input transition. Candidate
  pruning must account for that rather than checking only tag/memory/prev
  dictionaries.
- Factored snapshots should be materialized with the same scoped/full snapshot
  contract as ordinary successors.

## Unified candidate consumer

Sketch:

```python
def _process_candidate(candidate, *, parent_snapshot, ...):
    restore(candidate.snapshot)
    if filtered():
        return _CandidateResult()

    key = state_key_for_current_kernel(candidate.child_flipped)
    jump_self_loop = is_self_loop(key, parent_key)
    hidden = collect_hidden_outcomes(
        key,
        parent_snapshot,
        jump_self_loop=jump_self_loop,
    )

    process_current_successor(
        key,
        candidate.inputs,
        candidate.child_flipped,
        record_failures=not hidden or not settled,
        skip_project_duplicate=not hidden,
    )
    if hidden:
        process_hidden_successors(
            hidden,
            candidate.inputs,
            candidate.child_flipped,
        )

    return _CandidateResult(
        intractable=...,
        ready=ready_results(),
    )
```

Both ordinary and factored candidates call this helper. There should be no
second copy of key construction, hidden-event dispatch, predicate recording,
projection recording, or enqueue handling.

## What makes the refactor tricky

### 1. The BFS function is a generator

`_ready_results()` can cause an intermediate `yield`. A normal helper cannot
yield through its caller without turning the helper into another generator.
Returning `_CandidateResult` keeps the outer generator in control, but every
early `Intractable` and ready-result path must be converted consistently.

### 2. The kernel is shared mutable scratch space

Hidden-event processing repeatedly restores different snapshots and leaves the
kernel at the last branch. Candidate generation therefore cannot depend on the
kernel retaining the base state after a candidate has been processed.

The cleanest option is to finish constructing the base snapshot and all group
deltas before yielding the first candidate. Full composed snapshots can then
be materialized lazily from immutable copied dictionaries.

### 3. Base filtering currently suppresses factoring

The existing fast path does:

```python
if base_state_filtered:
    continue
```

before factored composition. A filtered base state does not imply that every
factored variant is filtered. Unification must apply `state_filter` to each
complete candidate independently.

### 4. Hidden-event decisions are candidate-specific

The threshold vector, live inputs, abstract key, self-loop check, pending
settlement, and jump cache lookup all depend on the complete candidate. Reusing
the base candidate's hidden outcomes for composed candidates would be unsound.

### 5. Paced mode needs a separate invariant

Free inputs are normally absent from the state key because they are enumerated
afresh at every state. In paced mode, a flipped successor gets a mandatory scan
with its input values held. An input-only factored candidate may therefore be
semantically relevant even when all state deltas are empty.

Before enabling factoring in paced mode, either:

- include the held free-input values in the one-scan paced phase key; or
- conservatively disable factoring for paced exploration.

The current reproducer uses unpaced `reachable_states()`, so this is a separate
hardening item rather than its root cause.

### 6. Delta conflicts need explicit validation

Tag deltas are restricted to each group's static write cone, but memory and
`prev` deltas are currently collected globally. Two groups can report the same
memory/prev key. Composition assumes those values agree. The refactor should
assert equality on overlapping deltas instead of silently accepting
last-group-wins behavior.

### 7. Trace and edge-collector behavior is observable

Parent links, input dictionaries, scan counts, demoted edge previous values,
and `edge_collector` calls must remain identical enough for counterexample
replay and causal tooling. Candidate processing should be centralized before
moving any trace behavior.

## Suggested implementation sequence

1. Extract key/hidden/current/hidden-successor handling into
   `_process_candidate()` while keeping the existing base and factored call
   sites. No control-flow change yet.
2. Introduce `_ScanCandidate` and make the ordinary base path call the helper.
3. Materialize factored compositions as `_ScanCandidate` snapshots and call the
   same helper.
4. Move factored candidate generation outside the base hidden-outcome branch.
5. Remove the hidden-event fallback guard.
6. Add overlap assertions for composed memory/prev deltas.
7. Profile evaluation counts and wall time before considering empty-candidate
   pruning.

## Required tests before removing the fallback

- The July 2026 counter/timer reproducer with factoring isolated against
  `sound_baseline()`.
- Base candidate has hidden outcomes while a factored candidate changes the
  counter state.
- Factored candidate has hidden outcomes while the base candidate does not.
- Base candidate is filtered while a factored candidate survives.
- Projection mode and predicate mode.
- Counterexample trace replay, including demoted edges.
- Pending settlement and hidden-event jumping independently enabled.
- Scoped snapshots on and off.
- Paced mode either disabled for factoring or covered by held-input key tests.
- Evaluation-count comparison on two-, three-, and four-group factoring cases.

## Complexity assessment

This is a medium-to-high risk refactor, not a one-line relocation. The core
shape is straightforward, but generator control flow, mutable-kernel ownership,
hidden-event snapshot restoration, and trace production are tightly coupled.
A reasonable production change is likely 100-200 lines touched plus focused
tests. It should be a separate change from the conservative fallback.
