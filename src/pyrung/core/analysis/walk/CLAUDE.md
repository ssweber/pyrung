# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this subsystem does

`walk/` is the corridor walker — the sole engine behind `plc.how()`. It plans
forward on the real interpreter: fork the runner, steer external inputs, fold
time to accumulator crossings, recurse on prerequisites, and assemble a
replay-verified `Path`. It is a planner, not a verifier — `prove/`'s
exhaustiveness invariants do **not** apply here.

- Entry point: `plan_walk` (`engine.py`), called from `PLC._how_via_walk`
  (`core/runner.py`).
- Living plan: `scratchpad/corridor_walker_plan.md` — the single consolidated
  document: theory, POCL vocabulary, what's built, settled direction (one
  agenda loop, pass registry, triangle table), and the staged execution plan.

## Module map

Dependency order, bottom up (each module imports only from those above it):

- `base.py` — tuning constants, `NoGoodStore`/`HoldStore`, `_WalkBudget`/
  `_WalkContext` (per-walk-immutable state, built once per walk), `_Steer`,
  `_Action`, `_values_match`.
- `physical.py` — Harness install/replay glue for walk forks.
- `fold.py` — time folding: `_JumpContext`, accumulator-crossing arithmetic,
  `_advance_time` (the plateau-guarded jump loop).
- `steer.py` — steer prefixes and `_apply_steer_fold(done, monitor)`, the one
  execution-monitoring seam (the two adapters are its only callers' shapes).
- `priors.py` — static priors and candidate generation: governing-tag
  selection (with the `_probe_steps` simulation probe), steer alphabet,
  writer-condition/inequality prerequisite extraction, decomposition hints.
- `explore.py` — corridor BFS over governing values (`_explore`), hold-aware
  steer conflicts + the divest probe, the blocker-clearing move.
- `agenda.py` — the one deepest-first loop (`_drive`) and its resolver
  pipelines (`_establish`, `_recover`, `_residuals`), the plan tree
  (`_PlanNode`, flattened once at Path build), `_classify_blockers`,
  independent-fork walks, `_walk_to_goal` (single-goal entry).
- `engine.py` — `plan_walk` + `_solve_targets` (the walk root) and the
  re-export surface tests/callers historically reach internals through.

Logging: per-module loggers under `pyrung.core.analysis.walk.*`; tests
capture at the package parent logger.

## Contract / invariants

- **Replay verification carries soundness.** Every returned `Path` is replayed
  on a fresh fork (with the same Harness couplings and `unlink=` list the walk
  used). Heuristics — steer ordering, cone filters, governing-tag selection —
  affect completeness only. A wrong heuristic may cause a premature
  "not reachable", never a wrong plan.
- **Static analysis is a prior, never correctness-bearing.** Pipeline context,
  SP-trees, and the PDG generate candidates; the interpreted fork is ground
  truth.
- **Holds never assert reachability.** A hold conflict may skip a steer (safe
  direction: premature `None`); it must never manufacture a plan.
- **Nogoods record program facts only** — `cause()`-named blockers. Walker
  self-conflicts (a blocker that is a held input) are classified one layer up
  and routed to the divest probe or a reorder; they never enter the
  `NoGoodStore`.

## Relationship to prove/

One-way dependency, walk → prove, never the reverse. The walker consumes the
prover pipeline's `_ExploreContext` (built in `runner.py`, `allow_partial=True`)
as a static prior, and imports a handful of SP-tree helpers from
`prove/waypoints.py` (`_written_value_for_tag`, `_extract_condition_values`,
`_has_arithmetic_writer`, `_extract_required_values`) plus
`prove/expr.py:_eval_expr_from_state`. Those helpers need a neutral home when
the dead BFS code is deleted; until then the imports stay as-is.

## Testing

`make test-walk` — `tests/core/analysis/test_walk_*.py`. Separate from
`make test-prove` (verifier only). Walker tests build small adversarial
programs (coupled latches, guard chains, multi-layer state machines) and
assert both the verdict and the recovery-iteration count where the mechanism
under test is prevention vs. recovery.
