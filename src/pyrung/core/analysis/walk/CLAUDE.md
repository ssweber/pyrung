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
- Living plan: `scratchpad/corridor_walker_plan.md` (theory, DONE/LEFT,
  findings). Settled direction: `scratchpad/walker-consolidation-recap.md`
  (one agenda loop, pass registry, triangle table).

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
