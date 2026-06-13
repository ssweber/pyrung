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

- `base.py` — tuning constants, `NoGoodStore`/`HoldStore`, `_MustStay`/
  `_StepMonitors` (the composed execution monitors threaded through the
  fold seam), `_WalkBudget`/`_WalkContext` (per-walk-immutable state,
  built once per walk), `_Steer`, `_Action`, `_values_match` (re-exported
  from its neutral home in `sp_values.py`).
- `passes.py` — the pass registry: declared static advice (`WALK_PASSES`,
  each pass an ordering, narrowing, or fold kind), run once per walk by
  `run_walk_passes` into a frozen `_WalkAdvice` + `_WalkJournal`. Passes get
  `(program, pdg)` only — no agenda/fork/store handles; runtime learning
  stays out of the registry. Every pass gets an ablation-matrix row by
  construction (`test_walk_passes.py`); the fold kind's directional rows
  (disabled ⇒ the pre-rung refusal) live in `test_walk_fold_churn.py`.
- `physical.py` — Harness install/replay glue for walk forks.
- `compress.py` — post-plan compression: `_compress_plan` (greedy step
  drop — try removing each non-empty-action step, keep if goal breaks).
  Pure function from `list[_Action]` to `list[_Action]`; imports from
  `base.py` and `physical.py` only.  Called once from `engine.py` between
  `_flatten_plan` and the verify replay.
- `fold.py` — time folding: `_JumpContext`, accumulator-crossing arithmetic,
  `_advance_time` (the plateau-guarded jump loop), and the fold-kind churn
  handling (unread/target-disjoint plateau exclusions, affine(-mod)
  self-calc sources with closed-form patches, acc-mirror threshold
  translation). Every fold widening carries an exactness argument and is
  backstopped by the step-by-step verify replay.
- `steer.py` — steer prefixes and `_apply_steer_fold(done, monitor,
  monitors)`, the one execution-monitoring seam (the two adapters are its
  only callers' shapes; `monitors` is the composed `_StepMonitors` —
  must-stay guards today, future monitors join it as fields rather than
  new threaded parameters).
- `priors.py` — static priors and candidate generation: governing-tag
  selection (with the `_probe_steps` simulation probe), steer alphabet,
  writer-condition/inequality prerequisite extraction (per-writer groups
  via `_unsatisfied_condition_groups`; the union is its first element),
  reference-constant detection (`_reference_constants` — never-written
  copy-source registers, deferred by the `ref_constant_order` pass),
  idx-chasing for indirect-copy writers (`_invert_indirect_source` —
  invert the jump table on the live snapshot, sub-goal the index
  register; hops through calc-defined scratch pointers via pipeline
  functional-dep projections or the sole writer's calc expression),
  decomposition hints.
- `explore.py` — corridor BFS over governing values with three exits
  (`_explore_corridor` → found / stuck / diverged-with-checkpoint;
  `_explore` is the steps-or-None wrapper), hold-aware steer conflicts +
  the divest probe, the blocker-clearing move.
- `agenda.py` — the one deepest-first loop (`_drive`) and its resolver
  pipelines (`_establish` — prerequisites walked as per-writer groups,
  smallest unsatisfied first, corridor probed between groups —
  `_recover`, `_residuals`, `_backjump` — the speculative
  diverged-checkpoint re-entry, segment-chained for long corridors),
  the why-regression fallback goal source (`_why_regression` /
  `_why_regression_goals` — frontier-terminated `why()` on stuck forks,
  feeding the nearest actionable sub-goals through the normal agenda),
  the plan tree (`_PlanNode`, flattened once at Path build; failed
  nodes carry `failure`/`blockers` for diagnosis),
  `_classify_blockers`, independent-fork walks, `_walk_to_goal`
  (single-goal entry).
- `engine.py` — `plan_walk` + `_solve_targets` (the walk root: committed
  conjuncts are must-stays, re-checked after every later goal's walk; a
  regression fails the attempt and `plan_walk`'s reorder loop retries with
  the clobbering goal first), `_diagnose` (the Diagnosis consumer: tree +
  holds + nogoods + journal), and the re-export surface tests/callers
  historically reach internals through.

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
as a static prior, and imports `prove/expr.py:_eval_expr_from_state`. The
shared static value-extraction helpers (`_written_value_for_tag`,
`_extract_condition_values`, `_has_arithmetic_writer`,
`_extract_required_values`) and the inequality-chase family
(`_chase_inequality_source`, `_extract_inequality_prereqs`,
`_operand_candidates`, `_producible_values`, `_values_match`) live in
their neutral home, `core/analysis/sp_values.py`, imported by walk,
prove, and causal (`projected_cause`'s relation moves) — causal never
imports from walk.

## Testing

`make test-walk` — `tests/core/analysis/test_walk_*.py`. Separate from
`make test-prove` (verifier only). Walker tests build small adversarial
programs (coupled latches, guard chains, multi-layer state machines) and
assert both the verdict and the recovery-iteration count where the mechanism
under test is prevention vs. recovery.
