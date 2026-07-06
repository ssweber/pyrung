# Handoff: self-defeating-hold filter needs the checkpoint's *frontier*

**The fix (WIP, uncommitted in `investigate.py` + `progress.py`):** the rotate-liveness
investigation confirms a bundle of holds that each keep Execute in the bounded replay
window but, applied together, pin progress forever — `Heat_xInit=1` forces the shared-init
rung (`fill(1, Heat_CurStep)`) while the target needs `Heat_CurStep=3`; `Rotate_xPause=1`
forces `copy(0, Rotate_CurStep)`. `investigate.hold_defeats_needed(tag, value, needed, pdg,
program)` drops such a hold statically: held steady it forces a rung writing a **needed**
register to a contradicting literal.

**The information it needs:** the `needed` set — the target's outstanding non-steerable
prerequisite `(tag, value)` pairs (`Heat_CurStep=3`, `Heat__x=1`, …), i.e. the *frontier*
that `_iteration_payload` already computes as `still_need` (`_all_nodes(frame.tree)` filtered
to unsatisfied, non-steerable, has-children). **Where it breaks:** at the terminal-let-run
regression in `_monitor_trend` (`progress.py`), the live `frame` is a *coast* frame whose
`frame.tree` is empty, and `ordered_actions()` yields only steerable leaves anyway (a coast
frontier has none). Re-deriving via `trace_back` from `cp_fork` also comes back empty for the
same reason. The real frontier lives in the **checkpoint** frame — the Execute frame where
`distance` was last computed and the coast launched — but the checkpoint
(`_Checkpoint = (key, fork, trend)`, created at `pilot.py:1078` / `progress.py:123,139`)
throws its tree/frontier away.

**Ask for the immutable-store refactor:** carry the launching frame's frontier **on the
checkpoint** (a 4th field: the `still_need`-style `tuple[(tag, value), …]`, or the tree ref),
captured at checkpoint creation, so investigation/revert can read `checkpoints[-1].frontier`
directly instead of re-deriving it from a coast frame that no longer has it. When checkpoints
become immutable-store records, make `frontier` a first-class field. Unpack sites to update:
`pilot.py:1078,1119,1386`, `progress.py:123,139,188`.

---

## AGREED DESIGN (discussed 2026-07-06 — build the red test against this seam)

Corrections to the text above:

- The WIP is **already committed** (`hold_defeats_needed` at investigate.py:716; progress.py
  calls it), including the `PILOT_SD_DEBUG` TEMP block and the cp_fork `trace_back`
  re-derivation at progress.py:254–270. The fix replaces that stopgap plumbing.
- `Rotate_xPause=1` is NOT part of the failure — the canonical case is only
  `Heat_xInit=1` forcing `fill(1, Heat_CurStep)` against needed `Heat_CurStep=3`.
- The missing information is not just *which tree* but *which extraction*:
  `ordered_actions()` yields steerable leaves (buttons) — never `Heat_CurStep=3`. The right
  extraction is `_iteration_payload`'s `still_need` filter (unsatisfied, non-steerable,
  not pipeline_internal, has children, snap ≠ needed) → `(tag, value)` pairs. One shared
  helper (e.g. `frontier_pairs(tree, snap)`), used by both `_iteration_payload` and the
  checkpoint capture, so the definitions can't drift.

The seam:

1. `_Checkpoint` becomes a **frozen dataclass** (not a 4-tuple): `key`, `fork`, `trend`,
   `frontier: tuple[tuple[str, Any], ...]`. (Forward-slice of the World/Knowledge split —
   pilot/CLAUDE.md future-direction item 3.)
2. Two creation sites, two sources:
   - entry checkpoint (pilot.py ~1068): extract from `frame.tree` directly;
   - trend/flat checkpoints (`_monitor_trend`): the checkpoint state is the trial's
     *post*-state, so `_TrialResult` grows a `frontier` field populated in `verify_gates`
     from the dead-end gate's `new_tree` (already built; currently discarded after
     `unsatisfied_count()`). Covers all checkpoint-creating paths (target-reached trials
     have `trend=None` and never checkpoint).
3. `_investigate_and_revert` reads `needed = checkpoints[-1].frontier`; **`needed_tags`
   (the `ht not in needed_tags` install guard) is fed from the checkpoint frontier's tags
   too** (decided). Delete the TEMP debug block and the trace_back re-derivation.

