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

- `base.py` — tuning constants, `NoGoodStore`/`HoldStore`/`RuleStore`,
  `_MustStay`/`_StepMonitors` (the composed execution monitors threaded
  through the fold seam), `_WalkBudget`/`_WalkContext` (per-walk state,
  built once per walk; includes `committed_values` for always-on
  regression detection and `progress_goals` for depth-limit scaling),
  `_DebugSink`/`_DebugEvent` (structured debug trace for
  `how(debug=True)`), `_Steer`, `_Action`, `_values_match` (re-exported
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
- `fold.py` — time folding: `_FoldContext`, accumulator-crossing arithmetic,
  `_advance_time` (the plateau-guarded fold loop), and the fold-kind churn
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
- `rules.py` — learned temporal rule evidence and recovery:
  `recursive_cause_evidence` (chase cause chains to external-input roots),
  `mine_regression_holds` (extract protective `from_value` holds from
  regression cause chains), `record_regression_evidence` (debug-side
  recording), `temporal_cycle_recovery` (cycle-rule late recovery).
- `explore.py` — corridor BFS over governing values with three exits
  (`_explore_corridor` → found / stuck / diverged-with-checkpoint;
  `_explore` is the steps-or-None wrapper), hold-aware steer conflicts +
  the divest probe, the blocker-clearing move.
- `scheduler.py` — the one deepest-first loop (`_drive`) and its
  frame-stack machinery: `_PlanNode` (flattened once at Path build;
  failed nodes carry `failure`/`blockers` for diagnosis), `_Request`,
  hold/progress helpers (`_check_progress_regression` — always-on;
  detects regressed committed goals after child-frame completion,
  mines holds via `rules.mine_regression_holds`, installs them and
  patches the work fork; target-decomposition frames are handled by
  `engine._solve_targets`' reorder loop instead).
- `establish.py` — the `_establish` generator that `_drive` pushes:
  discovers prerequisites walked as per-writer groups (smallest
  unsatisfied first, corridor probed between groups), `_residuals`
  (leftover flaw resolution), and candidate ordering.
- `recovery.py` — fallback resolvers: `_recover` (nogood-and-retry
  stage of the establish pipeline), `_backjump` (the speculative
  diverged-checkpoint re-entry, segment-chained for long corridors),
  `_why_regression` / `_why_regression_goals` (frontier-terminated
  `why()` on stuck forks, feeding the nearest actionable sub-goals
  through the normal agenda), `_classify_blockers`, oracle helpers.
- `independent.py` — `_try_independent_walks` (solves disjoint-cone
  prerequisites on separate forks and merges their holds),
  `_walk_to_goal` (single-goal entry, used by tests).
- `engine.py` — `plan_walk` + `_solve_targets` (the walk root: committed
  conjuncts are must-stays, re-checked after every later goal's walk; a
  regression fails the attempt and `plan_walk`'s reorder loop retries with
  the clobbering goal first), `_diagnose` (the Diagnosis consumer: tree +
  holds + nogoods + journal), and the re-export surface tests/callers
  historically reach internals through.

Logging: per-module loggers under `pyrung.core.analysis.walk.*`; tests
capture at the package parent logger.

Debug trace: `how(tag, debug=True)` enables a structured event collector
(`_DebugSink` on `_WalkContext.debug_sink`). Events cover PDG cone
snapshots, goal lifecycle (start/resolved/failed), oracle chain dumps
(`projected_cause` and `why_cause` results), goals mined from oracles,
hold registrations, and budget exhaustion. The trace attaches to the
returned `Path.debug_trace` and renders via `str()`. Zero cost when
`debug=False` — every emit site checks `ctx.debug_sink is not None`.

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
