# Memory savings — follow-ups

Context: DAP session running `examples/click_conveyor.py` was growing
memory heavily on every scan. Commits `e97138c` and `d85af97` on `dev`
eliminated the bulk of it (rung-trace retention, input-force diffing,
system-point no-op writes, `_dt` write, `_prev:sys.scan_counter` churn).
After those, idle scans leave `state.memory` structurally shared with
the prior scan (locked in by `tests/core/test_scan_pmap_sharing.py`).

Two known residual sources of per-scan memory growth remain.

## 1. `state.tags` rebuilds every scan due to `sys.scan_counter`

`SystemPointRuntime.on_scan_end` always increments `sys.scan_counter`
and writes it into the tag evolver. Even on an otherwise fully idle
scan, this dirties the evolver and forces `commit()` to return a new
`tags` PMap.

Pyrsistent's HAMT only rebuilds nodes on the path to the changed key,
so per-scan cost is small (~40–80 bytes of internal trie nodes), but
it is nonzero and cumulative across `History`.

### Directions to investigate
- Check whether `sys.scan_counter` and `SystemState.scan_id` are ever
  out of lockstep. If not, derive the user-facing tag from `scan_id`
  via the resolver (like `sys.first_scan`) and stop storing it.
- Alternatively, promote scan_counter to a dedicated `SystemState`
  field outside the tags PMap. Cheaper per scan but more invasive.
- Confirm current `SystemState.scan_id` semantics against Click's
  SC1/scan-count convention so any unification doesn't regress the
  Click dialect.

## 2. `_rung_firings_by_scan` grows one dict entry per retained scan

`PLC._commit_scan` now reuses the prior PMap object when the new
firings are structurally identical (commit `e97138c`) — so the PMap
data isn't duplicated, but each scan still gets its own key/value
slot in `_rung_firings_by_scan`. With `history_limit=1000` (typical
DAP), that's ~1000 dict entries per debug session. Not a leak (bounded
by history_limit), but real steady-state overhead.

### Directions to investigate
- Coalesce adjacent scan-id ranges that share the same firings PMap
  object: replace the `scan_id -> PMap` dict with a sparse run-length
  structure. Lookup stays O(log n) with a sorted list of (start,
  PMap) or O(1) amortized with a range tree.
- Or only record an entry when firings differ from the previous
  scan, and on lookup fall back to the most recent `<= scan_id`
  entry. Simplest change; keeps the existing dict shape but makes
  it sparse.
- Validate against `query.hot_rungs` and any other consumer that
  expects an entry per scan id — the "empty pmap means rung didn't
  fire" contract must still hold.

## Status

Both items are pure optimization; no observable behavior changes
expected. Priority is (2) first because dict-entry overhead is
independent of HAMT internals and easier to measure directly.
