Profiled `pyrung lock main --profile` on the user program (253s, 424M calls, 73k BFS steps across 12 clusters).

## Hot spots (ranked by profile self-time)

1. Inline block load/flush into compiled step function. **83s (33%)**
`load_block_from_tags` (36s) + `flush_block_to_tags` (47s), 517k calls each.
Pure-Python for loop per block per BFS step: enumerate, `in` check, dict read/write per element.
The codegen already inlines scalar tags (load at entry, write-back at exit). Do the same for block-backed tags — emit `_b_DS[0] = tags["DS[0]"]` at entry, reverse at exit. Remove external load/flush loops from `_step_kernel`.

2. Pre-index atoms by tag name in domain classification. **55s (22%)**
`_collect_atoms_for_tag` walks all expression trees (94M recursive `_walk_atoms` calls with isinstance) once per tag per fixed-point iteration in `_collect_structural_domains`. Build a `dict[str, list[Atom]]` index in one pass before the loop; replace tree walks with dict lookups.

3. Pack / shrink state keys and threshold vectors. **37s (15%)**
`_extract_state_key` + `_threshold_vector_key` called 148k times. `_threshold_crossed` called 9.5M times via nested genexprs. Consider dense int encoding, caching threshold vectors across steps with same stateful prefix, or bitpacking boolean threshold results.

4. Hidden event jumping. **33s (13%)**
`_maybe_jump_hidden_event` → `_resolve_nearest_exact_hidden_event` + `_abstract_threshold_outcomes` per BFS step. `_scans_until_threshold_event` called 4M times, `_progress_delta_and_current` 4M times. Mostly unavoidable for timer-settling semantics, but caching intermediate results across repeated visits to the same plateau could help.

5. Snapshot / restore. **22s (9%)**
`_snapshot_kernel` (5s) + `_restore_kernel` (11s via dict clear+update, 886k calls each). `dict()` copy for tags/memory/prev on every snapshot. Compact struct-based snapshots or COW dicts would reduce Python object churn.

## Previous items (re-assessed)

6. Compact snapshots. - partially done, still 22s. See #5.

7. Scope-sliced kernel compilation. - Done. Compiled kernel step is only 15s (6%).

8. Shrink the state basis.
Still valuable — reducing BFS steps is multiplicative. Not a top self-time item but affects everything above.

9. Input-combo specialization. Not hot in this profile.

10. Per-parent memoization. Not hot in this profile.

11. Bool-only bitslice backend. Not applicable (program has timers/integers).

12. Projection-first storage. Not hot in this profile.
