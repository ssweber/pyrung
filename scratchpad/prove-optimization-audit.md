Profiled `pyrung lock main --profile` on the user program.

## Profile history

| Run | Total | BFS steps | Calls | Notes |
|-----|-------|-----------|-------|-------|
| Baseline | 253s | 73k | 424M | Pre-optimization |
| Post-d9bb478 | 682s | 155k | 1.6B | Longer run (inline block sync + pre-index atoms) |
| Post-TagBackedArray | — | — | — | Pending measurement |

## Investigations

A. **Shrink the BFS state basis.**
Use def-use / feedback analysis to identify tags whose scan-entry value is dead — recomputed every scan before any read. Drop them from the visited-set key. This reduces the number of distinct states (and therefore BFS steps), which multiplicatively reduces every per-step cost below.

B. **Shrink snapshots to match the reduced basis.**
Tags dropped from the state key can also be dropped from `_snapshot_kernel` / `_restore_kernel` dict copies. One analysis pass yields both wins: fewer steps (A) and cheaper per-step snapshots (B). Currently 46s for snapshot/restore across 155k steps (12.6s snapshot + 17s update + 15s clear).

## Hot spots (ranked by profile share, post-d9bb478)

1. **Block sync load/flush — 450s (66%).** Done → TagBackedArray.
The inline step (`_compile_inline_step`) synced 7,664 block tags per BFS step. Only 309 positions are accessed statically by the kernel; big blocks (DS: 4,500 tags, C: 2,000) have dynamic indexing so sparse-sync isn't viable. Replaced block `list` arrays with `_TagBackedArray` proxies that read/write `kernel.tags` directly. Load/flush eliminated; kernel sees the same `__getitem__`/`__setitem__` interface. ~441 block accesses per step via proxy vs ~15,328 dict.get+set per step for sync.

2. Pre-index atoms by tag name in domain classification. **~4s.** Done (d9bb478).
`_build_atom_index()` in `prove/expr.py`. Down from 55s.

3. **State keys + threshold vectors — 77s (11%).**
`_extract_state_key` (35s, 155k calls) + `_threshold_vector_key` (42s, 311k calls). `_threshold_crossed` called 20M times. Consider dense int encoding, caching threshold vectors across steps with same stateful prefix, or bitpacking boolean threshold results.

4. **Hidden event jumping — 81s (12%).**
`_maybe_jump_hidden_event` → `_resolve_nearest_exact_hidden_event` + `_abstract_threshold_outcomes`. `_scans_until_threshold_event` 8.6M calls (32s), `_progress_delta_and_current` 8.7M calls (16.6s). Caching across repeated visits to the same plateau could help.

5. **Snapshot / restore — 46s (7%).**
`_snapshot_kernel` (12.6s, 311k) + `_restore_kernel` dict.clear (15s, 1.9M) + dict.update (17s, 1.9M). See Investigation B.

6. Compiled kernel step — 31s (5%). Healthy; sub-functions: MaterialInterlock (7s), PLCDateTime (4s), Validation (3s).

## Previous items (re-assessed)

7. Scope-sliced kernel compilation — Done. Kernel step is 31s (5%).

8. Shrink the state basis — See Investigation A.

9. Input-combo specialization — Not hot in this profile.

10. Per-parent memoization — Not hot in this profile.

11. Bool-only bitslice backend — Not applicable (program has timers/integers).

12. Projection-first storage — Not hot in this profile.
