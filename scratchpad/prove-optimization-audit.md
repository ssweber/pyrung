Profiled `pyrung lock examples.packml_bench --profile bench.prof` on the PackML benchmark.

## Current picture

| Run | Total | Step attempts | Calls | Notes |
|-----|-------|---------------|-------|-------|
| Baseline | 253s | 73k | 424M | Pre-optimization |
| Post-d9bb478 | 682s | 155k | 1.6B | Longer run (inline block sync + pre-index atoms) |
| Post-TagBackedArray / PackML bench | 113s | 476,837 | 258M | Current `make bench` profile |

The new benchmark changes the bottleneck shape. The dominant issue is no longer block sync or pre-BFS setup; it is **input branching explosion inside `_bfs_explore`**.

## New finding: input combo explosion

- Cluster 1 starts with **18 live Bool inputs**, so the root BFS node enumerates `2^18 = 262,144` input combinations.
- That root expansion produced only **1,008** direct new states, plus **96** additional hidden-event jump states, for **1,105** total visited states.
- **261,136** root combinations were immediate revisits.
- Every sampled depth-1 child still had the same **18 live inputs**, so the same `2^18` branching repeats deeper in the search.
- This explains the bench output: `steps/s` stays healthy while `new/s` repeatedly falls to zero.

Manual specialization experiment on the root node:

- Collapse the 10 command bits to `{none, exactly one command}`
- Collapse the 3 mode bits to `{none, exactly one mode}`
- Leave the other 5 Bool inputs independent

Result:

- Root branching drops from **262,144** combinations to **1,408**
- Root runtime drops from about **18.9s** to about **0.10s**
- The specialized run still reaches **1,041 / 1,105** states from that first expansion, so most of the current work is clearly redundant

## Current hotspots (post-TagBackedArray)

These are all downstream consequences of the branching explosion above:

1. **Kernel step + inline wrapper — ~53s.**
`_step_kernel` / `<block_sync>._step` are no longer copying huge blocks blindly, but we still call them **476,837** times.

2. **State key construction — ~34s.**
`state_key()` / `_extract_state_key()` / `_threshold_vector_key()` are expensive largely because we build them for hundreds of thousands of duplicate successors.

3. **Hidden-event jumping — ~20s.**
`_maybe_jump_hidden_event()` is hot because duplicate pending plateaus keep revisiting the same jump logic. It still matters, but it is now clearly a secondary multiplier on top of the input explosion.

4. **Edge liveness / threshold helpers — ~19s + ~14s.**
Useful to tune, but again they are paying per attempted successor, not per newly discovered state.

## Updated priorities

1. **Input-group specialization.**
Detect one-hot encoder families of external Bool tags and enumerate canonical choices instead of the full Cartesian product. This is now the highest-value fix for `make bench`.

2. **Hidden-event jump memoization.**
Cache jump outcomes for repeated pending plateaus so `_maybe_jump_hidden_event()` does not recompute the same exact/abstract threshold successors over and over.

3. **Shrink the BFS state basis.**
Still valuable. Def-use / feedback analysis to identify scan-entry values that are dead before any read. Fewer distinct states multiplies every other win.

4. **Shrink snapshots to match the reduced basis.**
If a tag drops out of the state key, it can also drop out of `_snapshot_kernel` / `_restore_kernel`.

5. **Dense state-key / threshold encoding.**
Still worth doing after the branching problem is under control.

## No longer current

- **Block sync load/flush** is no longer the main story here. Tag-backed arrays fixed the old 66% wrapper cost.
- **Pre-index atoms by tag name** is already done and not worth more attention right now.
- **“Input-combo specialization — not hot in this profile”** is obsolete for the current PackML benchmark.
- **Projection-first storage** and **bool-only bitslice backend** do not match the present bottleneck profile.
