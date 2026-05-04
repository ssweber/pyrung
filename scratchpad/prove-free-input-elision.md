# Free Input Elision — Remove Non-Edge ND Inputs from State Key

## Problem

The BFS state key includes concrete values for all live nondeterministic inputs
(`_extract_state_key` in `kernel.py:342`). This means `(IDLE, CmdStart=True)` and
`(IDLE, CmdStart=False)` are distinct states. On the PackML bench, this inflates
52K visited states from only ~3,672 stateful prefixes — a ~14x expansion.

The docstring states the rationale:

> Inputs are included so the BFS can interleave single-dimension flips
> from each distinct input baseline.

## Observation

ND inputs are external and can take any value on any scan. For inputs that have
**no edge** (no `rise()`, `fall()`, or `oneshot=True`), the current value does not
constrain future scans. Two states that differ only in free ND input values have
identical reachable successors.

Edge-bearing inputs (where prev value matters for rise/fall detection) DO need
their value in the state key — the prev constrains when the edge can fire.

## PackML bench breakdown (17 ND inputs)

Edge-bearing (keep in state key):
- `CmdChgRequest` — explicit `rise(CmdChgRequest)` on line 569
- `ModeChgRequest` — implicit edge via `oneshot=True` on line 561

Free / combinational (can elide from state key):
- `CmdReset` through `CmdComplete` (10 bools) — condition-only, exclusive group
- `ModeProduction`, `ModeMaintenance`, `ModeManual` (3 bools) — condition-only
- `Estop`, `IOModuleError`, `CommFault` (3 bools) — condition-only

That's 2 edge vs 15 free. Eliding the 15 free inputs would collapse the micro-state
expansion from ~2^15 to ~2^2, reducing ~52K states to ~4K–8K.

## Why assignment generation still covers all transitions

The BFS generates assignments via stutter + single-flips + exclusive group canonicals
(`_iter_input_assignments` in `inputs.py:295`). From a merged `(IDLE)` state:

- Exclusive group canonicals try every one-hot command regardless of baseline
- Single-flips try both True and False for non-grouped inputs
- The stutter from one baseline equals the single-flip from another

So no transition is lost by merging states that differ only in free inputs.

## Soundness argument

1. Free ND inputs can take any value on any scan — their current value is not a
   constraint on future behavior.
2. Edge-bearing inputs remain in the state key, preserving rise/fall semantics.
3. Oneshot memory keys remain in the state key (via `memory_key_names`), preserving
   oneshot re-fire prevention.
4. Merging may over-approximate (explore input combos that are unreachable due to
   physical input correlations), but never under-approximate. This is consistent
   with the verifier's existing soundness contract.

## Implementation sketch

1. During `collect_edge_exprs` pass (or a new pass), partition `nondeterministic_names`
   into `edge_bearing_nd` (appears in any rise/fall expr or oneshot condition) and
   `free_nd` (the rest).
2. In `_extract_state_key`, always mask `free_nd` inputs to `_INPUT_DEAD`, regardless
   of liveness. Only include `edge_bearing_nd` values when live.
3. Assignment generation (`_iter_input_assignments`) unchanged — still enumerates
   all live inputs to explore all transitions.
4. Live input cache unchanged — liveness still determines which inputs to enumerate,
   just not which ones appear in the state key.

## Estimated impact

- PackML bench2 cluster 1: ~52K states → ~4K–8K states
- At 550 steps/s: ~31+ min (didn't finish) → ~4–8 min
- Combined with blockless kernel optimization: likely under 2 min

## Open questions

- Does the oneshot memory mechanism fully capture the edge state, or does the raw
  input value also need to be in the key? Need to verify that the `_once_*` memory
  keys are in `memory_key_names`.
- Are there other edge-like mechanisms beyond rise/fall/oneshot that would require
  an input's value in the state key?
- Could this interact with `input_groups` (user-declared joint input moves)?
  Probably fine — input_groups affect enumeration, not the state key.
