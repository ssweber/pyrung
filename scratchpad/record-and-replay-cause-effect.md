# Cause/effect performance under record-and-replay

> Design note for work that lands after Stage 7 (rung-firings refactor)
> and before Stage 10 (tune K, finalize). Companion to
> `record-and-replay.md` and `record-and-replay-checklist.md`. Focuses
> on `plc.cause()` / `plc.effect()` cost, which is the primary
> user-visible debugger query and the path most exposed to the
> snapshot-replaced-by-replay tradeoff.

## Context

`plc.cause()` / `plc.effect()` in `analysis/causal.py` walk causal
chains step-by-step. Each step currently dispatches multiple
`history.at(scan_id)` calls — one for SP-tree attribution at the
step's scan (`causal.py:614`, feeding `attribute(sp_tree, _eval)` at
`causal.py:620`), plus per-contact reads inside
`_find_recent_transition` and `_find_last_transition_scan`. Before the
record-and-replay migration this was cheap (deque lookups); after,
every `history.at()` past the recent-state window triggers a
`replay_to` from the nearest checkpoint. A 10-step chain with
per-contact reads multiplies quickly for older scans.

Two `TODO(stage-7)` markers at `causal.py:385` and `causal.py:439`
already acknowledge that most of those state reads can be routed
through the firing timeline once Stage 7 lands. The genuinely
state-dependent read is the SP-tree attribution — required to
classify contacts as **proximate** (transitioned, drove the rung's
output) vs **enabling** (held steady, required but didn't change).
Classification needs the full contact-value tuple at the step's scan;
the timeline alone can't give that.

This note specifies the path from Stages 7 and 8 (widened cache) to
fast `cause()` / `effect()` at the tip and graceful degradation
elsewhere, without introducing a mode flag.

## The three information tiers

`_walk_backward` currently mixes three tiers of information without a
clean separation. Naming them makes the rest of the design fall out:

- **Structural** — PDG (`writers_of`, `readers_of`) plus rung shape.
  Free. Always available. Tells us which rungs can write which tags
  and what contacts each rung reads.
- **Timeline** — firing log from Stage 7. O(log S) per query, no
  state read. Which rung fired at scan N, what it wrote, which of the
  rung's contacts transitioned at or near N.
- **State** — `SystemState` at scan N. Cheap if cached, otherwise a
  `replay_to` dispatch. Required only for SP-tree attribution and for
  reading stable enabling-contact values.

The split makes explicit why proximate causes are cheap (timeline +
structural) and enabling conditions are expensive (state). It also
clarifies the PDG-filter corner — see below.

## Changes

### 1. Memory-bounded recent-state cache

Replace `PLC._recent_state_window: deque[SystemState]` (currently
`maxlen=_RECENT_STATE_WINDOW_SIZE=20`) with a byte-bounded cache.
Module constant `_RECENT_STATE_CACHE_BUDGET_BYTES = 100 * 1024 *
1024` (100 MB default); `recent_state_cache_bytes=` keyword-only
`PLC` constructor param, propagated through `fork()`, ValueError
below 1 MB (below that you're evicting on every step).

Maintain a floor of 20 entries regardless of budget. Monitor
`previous_value` / `_prev:*` reads assume N-1 is always present; the
Stage 5 contract must not regress under budget pressure.

Byte accounting uses a coarse estimator — PMap node count × fixed
per-node constant, not `sys.getsizeof`. States are structurally
shared, so summing independent sizes massively overcounts. Write
`_estimate_state_bytes(state)` as a standalone pure function and test
it against crafted PMap shapes separately from the eviction logic.
The estimator is a conservative ceiling, not an allocator-accurate
measure.

Eviction: pop oldest while over budget, stop at the floor. Evicted
states reconstruct through the existing `_state_at` fallthrough
(window → checkpoint → `_initial_state` → `replay_to`). Routing shape
is unchanged; only the hit zone widens.

`fork()` inherits an empty cache (same as today — forks anchor from a
single state).

Measurement pass on `click_conveyor.py` at 1 hour / 100 Hz / 100 MB
budget: given idle-marginal of ~100 bytes/scan and active-marginal of
~2 KB/scan under PMap structural sharing, a full hour is expected to
fit. If that holds, `replay_to` becomes cold-path-only for the
debugger UX, and Stage 10's K-tuning target shifts accordingly.

### 2. Route transition finding through the firing timeline

Resolve both `TODO(stage-7)` markers; they're the bulk of the current
state-read cost in cause walks.

- `_find_transition` / `_find_transition_at_scan` (`causal.py:375`,
  `:421`) currently diff state via `history.at()` to detect
  transitions. Replace with a timeline lookup: "did tag T transition
  at N?" becomes "did any rung in `writers_of(T)` fire at N with T in
  its writes, to a value different from T's value at N-1?" The "value
  at N-1" piece comes from the timeline's own records for earlier
  scans (the prior write's value), not from state.
- `_find_last_transition_scan` (`causal.py:430`) currently walks
  history backward state-by-state. Replace with a reverse iteration
  over `writers_of(tag_name)` timelines — seek back from
  `before_scan_id` through each writer rung's ranges, find the most
  recent range that wrote `tag_name` to a changed value.
- `_find_recent_transition` (`causal.py:451`) composes the above; no
  separate work.

After these changes, per-contact state reads disappear from every
chain step. The only remaining state read in `_walk_backward` is the
SP-tree attribution at `causal.py:614`.

Self-contained change; no API surface impact. The existing
`cause()` / `effect()` test suite should pass unchanged.

ling, with `held_since_scan` annotations).
- **Cache miss** — timeline-derived skeleton: `rung_index`,
  `transition` (from the firing log's `writes` map), and
  `proximate_causes` as candidates computed by intersecting
  `rung.contacts` (structural) with "tags whose writers fired at N or
  N-1" (timeline). `enabling_conditions` is empty, because
  stable-contact values require state.

The candidate proximate set is a superset of the true proximate set
— without SP-tree attribution we can't confirm which contacts were
load-bearing — but it's the correct shape for UX rendering and for
the walk's own downstream recursion, which uses proximate causes as
the recursion frontier. Each recursive step independently
cache-checks and produces its own fidelity level, so a single chain
can be mixed: recent steps full, deeper steps timeline-only, no
upfront mode choice.

#### Caller-side hydration

A caller that wants full detail for a degraded step re-invokes
`cause()` with the scan_id of interest. `cause(tag, scan_id=N)`
already exists as the entry point for "explain the transition at
exactly scan N" — extend it so that an explicit `scan_id` argument
forces a `replay_to(N)` before the walk starts. This warms the cache
at N, making the first step full-fidelity, and subsequent walks
through the same scan benefit from the now-cached state.

Implicit walks (no `scan_id`) never force replay; they return what
the cache supports. The explicit-scan_id-forces-replay rule captures
the user's pattern literally: "recall it with scan_id to get the
full."

For batch hydration across a range (e.g. before opening a scrubbing
session over the last hour), expose:

```python
class PLC:
    def hydrate(self, scan_range: tuple[int, int]) -> None:
        """Warm the recent-state cache across scan_range (inclusive).

        Dispatches replay_to for uncached scans in the range. Callers
        use this before batch analysis to convert would-be cache-miss
        cause/effect steps to cache-hit. No-op for scans already
        cached.
        """
```

`hydrate` is a pure side-effect method; it returns nothing and
doesn't guarantee the range stays cached (subsequent activity may
evict). Callers who need firm guarantees should operate the cache
budget upward rather than relying on `hydrate`.

#### Per-step fidelity signaling

Add to `ChainStep`:

```python
@dataclass(frozen=True)
class ChainStep:
    # existing fields...
    fidelity: Literal["full", "timeline"] = "full"
```

Default `"full"` preserves existing call-site behavior and test
equality. Timeline-only steps set `"timeline"`. Update
`ChainStep.to_dict()` and `CausalChain.to_config()` to carry the
field. `__str__` renders enabling-condition rows only when
`fidelity == "full"` — a timeline step shows "Rung 7: Temp_High →
True (partial; re-run with scan_id to hydrate)" or similar.

Per-step rather than per-chain matches the mixed-fidelity reality.
UI consumers can render differently — grey out the enabling-condition
section, show a "load details" affordance that dispatches the
explicit-scan_id recall — without guessing which steps to treat
specially.

#### PDG-filter corner (non-Bool terminals)

`_fallback_writers_from_pdg` (`causal.py:672`) fires when the firing
log doesn't identify a writer — for non-Bool terminal outputs whose
writes the capture filter drops, per `context.py::capturing_rung`.
The filter preserves Bool writes regardless of read status (low
cardinality, user-facing state, common `cause()` target), so the
typical coil target hits the direct log path. Non-Bool terminals
(e.g. `Timer_Acc` with no reader) are the affected set — exactly the
writes the filter exists to drop.

This path requires state reads to re-evaluate candidate SP trees
against historical state; there's no timeline degradation available,
because by definition the write isn't in the timeline. On a cache
miss against a non-Bool terminal deep in history, the chain's first
step returns empty and the walk terminates with no root.

Ship the first pass accepting this corner. Document it in the
`causal.py` module docstring next to the existing PDG-fallback note.
A capture-layer "rung R fired at N, writes filtered" sentinel would
close the gap but adds a new capture artifact; defer until a real
workflow demands it.

## Ordering

Change 1 (cache widening) and change 2 (transition routing) are
self-contained and can land in either order or in parallel. Change 3
depends on both — it needs the cache to make "hit" a common case and
the timeline routing to make cache-miss steps cheap to produce.
`hydrate()` is trivial once change 1 is in.

Stages 10 (tune K, finalize) and 11 (derived edge tags, optional)
proceed unchanged after this work lands. The K-tuning measurement
should use the post-change-1 cache as its baseline, since the cache's
coverage materially changes what K needs to defend against.

## Success criteria

- **`cause(tag)` at tip** returns a full-fidelity chain in
  single-digit milliseconds. No cache misses for tip-adjacent walks
  under the 100 MB default budget.
- **`cause(tag, scan_id=N)` with explicit scan_id** forces
  hydration of N and returns full fidelity at the target scan, even
  when N is deep history. Deeper chain steps may degrade if their
  own scans are uncached.
- **Implicit `cause(tag)` against a cold cache region** returns a
  mixed-fidelity chain in single-digit milliseconds, no replay
  dispatched. `fidelity == "timeline"` on degraded steps;
  `enabling_conditions=()` on those steps; `proximate_causes` populated
  from the timeline + structural intersection.
- **`PLC.hydrate((a, b))`** bounded cost proportional to `(b - a) /
  K` replay dispatches. Subsequent cause/effect walks in the range
  are full-fidelity.
- **Existing `cause()` / `effect()` tests pass unchanged.** Default
  `fidelity="full"` preserves equality; cache warmth at tip means
  current tests hit the full path.
- **Non-Bool terminal with cold cache**: `cause()` returns an empty
  chain and documents the reason. Not a regression — the same call
  against an in-cache scan returns full fidelity.

## Out of scope

- Persisting cache entries across sessions or forks.
- Background/predictive hydration based on UI navigation heuristics.
- A capture-layer sentinel for PDG-filtered writes (deferred until a
  real workflow demands it).
- Any changes to `effect()` forward-walk counterfactual evaluation
  beyond the per-step fidelity signaling — the
  `_CounterfactualView` path at `causal.py:710` is orthogonal to
  this work.
