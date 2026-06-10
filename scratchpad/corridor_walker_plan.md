# Corridor Walker — Living Plan

Companion to `corridor_walker_brief.md` (the original design hypotheses) and
`recovery_mechanisms.md` (the mechanism catalog). This file tracks what's
**done** and what's **left**, and sequences the work. Update the checkboxes as
we go.

**One-line status:** Walker is the sole `how()` path — BFS/waypoint fallback
removed. Pipeline context (domains, classifications) wired in. Non-Bool input
steers, inequality prereqs, multi-input steers, and seal-in break (inverse
regression for OTE/latch) done. Governance selection uses simulation probe
(`_probe_steps`) as ground truth — static classification is a fast path only.
Harness propagates through `fork()` — linked feedback tags excluded from walker
steer inputs; `how(unlink=)` models fault scenarios. Profile-gated walker paths
done. **Serial-clobber recovery landed** — the first Phase 4 recovery loop:
when a residual/prereq sub-walk clobbers an earlier corridor, the oracle
(`cause(tag, to=value)`, projected) re-derives what still blocks the target and
re-walks it; `_needs_decomposition` flags coupled prereqs as the Phase 6 Tier 2
insertion point. **Nogood learning landed** — `NoGoodStore` accumulates
precondition-failure memory keyed on `(from, to, frozenset(blocking))`;
`_explore`'s seen-key projects onto the cause()-identified blocking-tag names
(plus a blocker-clearing move) so a re-walk re-enters a governing value under
cleared constraints; the recovery loop records the nogood before re-exploring
and converges in ≤2 recovery iters on the cross-guard mutual-clobber tripwire
(naive loop fails outright). **Real-program pattern tests added** — 5 structural
patterns extracted from the APC_PackTag_SFC template: command protocol, return_early
flow gating, rendezvous, step sequencer, deep call chain. Two bugs fixed:
(1) `projected_cause()` resolved subroutine writers against main logic (same class
as the `why()` fix in 6f443d9); (2) `_walk_to_goal` returned `None` immediately
when any prerequisite walk failed, blocking retry of `_explore` which could handle
intermediate results (e.g. Trans) via time-folding. **All 5 patterns now pass**
including rendezvous. **Independent-fork walk generalized** — not a Phase 6
special case but the default strategy at every serial-walking site.
`_try_independent_walks` walks each sub-goal on a separate fork, extracts the
required external-input holds (cone-filtered to avoid steer-release
contamination), and applies them simultaneously via a multi-steer;
`_advance_time` handles multi-timer convergence.  Independence gate: pairwise
disjoint upstream cones. Applied at four sites: (A) `_walk_to_goal` early path
when `governing != target_tag`, (B) `_walk_to_goal` prerequisite-level fallback
when `_explore` fails, (C) `_recover_via_oracle` recovery-goals loop, (D)
`plan_walk` compound-goals loop. Site D uses `_apply_steer_compound` for
sequential monitor iteration — fold with each unsatisfied goal as monitor until
all are satisfied (converges because accumulation is monotone). Rendezvous test
solved in 2 actions / 30 scans. **Holds (protection intervals) landed** — a
hold = (external input, value, committed goal that depends on it); one
`HoldStore` per `plan_walk`; committed corridors register their commitments via
`_commit_holds` (wrapper, delegate-corridor, and recovery commit points);
`_steer_prefix` skips protected names in every implicit release (global
release, edge release, edge blast), so serial walks no longer self-clobber —
**prevention**, with the oracle recovery loop retained as backstop (tripwires
verified to still exercise it: cross-guard 2 iters, serial-clobber 3 iters).
Intended writes to a protected input pass the empirical **divest probe** (fork,
apply unprotected, settle, hold-goal-survives check — the seal-in case),
recorded per-branch on `_Node.released` in `_explore` and reconciled into the
store at commit (`_reconcile_divests`). `Path.holds` surfaces the surviving
commitments — plans now read "Holds: EnableA=true (for StageA)". Review fixes
landed alongside: `nogoods` threading gap on the serial-prereq residual path,
`unlink=` mirrored on the verify/annotate replay forks via
`_install_replay_harness` (fault plans were being verified against an intact
physical chain), decomposition-hint transition keyed on the real from-value
(`all_orderings_blocked` matches per-transition). Tests:
`test_walk_holds.py` (prevention A/B with zero recovery iters, divest
point, conflict-skip honesty, rendering, store unit). `test-prove` green
(757 pass, 4 xfail). **Walker relocated** to its own package
`src/pyrung/core/analysis/walk/` (engine.py + CLAUDE.md with the walker
contract); `_how_via_bfs` renamed `_how_via_walk`; tests renamed
`test_walk_*` with a dedicated `make test-walk` target. Next: consolidation
per `scratchpad/walker-consolidation-recap.md` (one agenda loop, plan tree,
unified fold monitor), then backjump + the third `_explore` exit
(reached-but-diverged).

---

## Theory statement

The corridor walker rests on a provable structural argument, not just an
engineering bet.

A single-scan, no-interrupt PLC program is a deterministic function from
(state, inputs) → state′. PLC programs are producer-consumer hierarchies of
sequential corridors coupled through narrow handshake interfaces (ISA-88,
PackML, IEC 61131-3 SFC enforce this by design). The program is its own
executable model — forkable, steppable, fully observable. Forward progress is
ground truth (step and observe). Backward structure is exact (read the SP-tree
via `simplified()`/`cause()`/`why()`). To solve a reachability goal: factor
into subsystems via the coupling structure, walk each corridor forward using
backward structure to steer and recover, force coupling signals to decouple
timing, verify feasibility by summing achieved depths against handshake
deadlines.

This is lock-and-key maze solving in a structurally tractable slice: most gates
are one-state (interlocks — polynomial), the gates are readable
(simplified/cause — no search needed for key identification), and the timed
gates decompose (producer-consumer, not adversarial). The general gadget-maze
problem is PSPACE-complete (Demaine, Hendrickson, Lynch); PLC programs are in
the easy subclass because the standards enforce simple locks, readable
conditions, and hierarchical key ordering.

**Scope constraint:** single-scan PLC without interrupts. Multi-task PLCs with
priority-based preemption (S7-1500 OBs, ControlLogix periodic/event tasks)
break the deterministic-order guarantee and are out of scope. Extension would
require modeling interrupt semantics as additional nondeterminism.

---

## Unifying principle: hierarchical planner

The corridor walker is a **hierarchical planner** with three layers:

1. **Abstract** — collapse the full PLC state space to one governing tag's
   value graph (tiny: mode machines have single-digit values).
2. **Plan** — BFS over that abstract value space (cheap).
3. **Refine** — for each abstract edge, find concrete inputs via interpreted
   simulation on forks (sound by construction — immune to static-analysis
   blindness).

Everything in LEFT maps to one of three extensions of this core:

| Extension | What it does | Planning concept |
|---|---|---|
| **Widen the input** | Let the engine accept goals it currently punts on (Or/And decomposition, multi-tag factoring) | Better abstraction / goal preprocessing |
| **Widen the alphabet** | Let refinement succeed on more transitions (non-Bool inputs, multi-input steers, link-aware de-energize) | Richer action space for refinement |
| **Backtrack on failure** | When refinement fails or execution diverges, re-plan with learned constraints | Hierarchical backtracking |
| **Diagnose infeasibility** | When no abstract plan is feasible, explain why | Explanation generation |

The guiding question for every new mechanism: **does it extend the existing
engine's reach, or does it add a parallel path?** Prefer the former. A generic
backtracking loop that retries decompositions scales; hand-coded pattern
handlers grow with every new PLC idiom.

**Static analysis is a prior, never correctness-bearing** — it picks the
governing tag, narrows the steer alphabet, and sets the horizon. Correctness
comes from simulation. Validation is always interpreted.

### The oracle advantage

Unlike classical planners (which reason over a symbolic model that may diverge
from reality), the corridor walker operates on **the program itself as a
white-box oracle**: forkable, per-rung steppable, with full observability of
what was read/written. There is no abstraction gap — simulation IS the
program. This shapes the architecture:

| Layer | Role | Tools | Properties |
|-------|------|-------|------------|
| **Generate candidates** | Narrow the search space | `why()` (state-aware minimal), `simplified()` (structural), PDG (coarse) | Heuristic — may over-/under-generate |
| **Forward exploration** | Try candidates | `fork()` + step (the walker engine) | Ground truth — deterministic, sound |
| **Validate / explain** | Confirm cause or diagnose failure | `cause()` on scan log | Recorded truth — what actually happened |

The symbolic layer generates candidates; the interpreted layer validates them.
No CEGAR loop needed because the "refinement check" runs the real program —
spurious abstract paths are caught in one step, not iteratively refined away.

**Candidate generation tools (finest → coarsest):**

- **`why(tag)`** — backward SP-tree attribution from a snapshot. Gives the
  *minimal load-bearing contacts* explaining the current value. State-aware:
  prunes irrelevant formula branches given the actual fork state. Use when you
  need "what's holding this tag HERE" (steer prioritization, regression
  sub-goals, factoring).
- **`simplified(condition)`** — resolved Boolean form to input-level. Structural:
  all paths through the formula regardless of current state. Use when you need
  "what COULD make this true/false" (full regression, enabling-condition
  analysis).
- **PDG** — `upstream_slice`, `writers_of`, `condition_reads`. Coarsest static
  connectivity. Use for cone-narrowing, solve-order proposals, independence
  screening.

**Validation tool:**

- **`cause(tag)`** — recorded-mode causal analysis on the scan log. Gives
  trigger vs. enabler split: what *transitioned* the tag vs. what was already
  in place. Use after simulation to confirm which inputs were load-bearing, to
  extract nogoods from failures, and to produce actionable diagnosis.

---

## Architecture (decided)

`how()` first tries a **corridor walk** on the PLC runner; anything it can't
walk returns `None` and falls back to the existing waypoint/BFS planner
(unchanged, transitional).

> **Settled end-state (achieved):** **no BFS fallback for `how()`** — the walker
> returns a `Path` or `Path(reachable=False)`. BFS stays only for `always()`/
> `never()`/`reachable_states()`. The old BFS/waypoint code is disabled behind
> `if False:` in `runner.py` for audit reference.

**The engine = interpreted best-first search over the governing stateful tag's
value graph.** Each value is expanded by a **steer alphabet** (empty-step /
pulse-input); every edge is discovered by *simulation on forks*, so it's sound
by construction.

Files: `src/pyrung/core/analysis/walk/engine.py` (engine; own package with
`CLAUDE.md` carrying the walker contract),
`src/pyrung/core/runner.py` `_how_via_walk` (the `how()` entry, ~line 1015).

---

## DONE

- [x] **Reconnaissance** — confirmed/refuted every brief claim against the code
  (see "Findings" below).
- [x] **Milestone 0: walk a known corridor.** `plan_walk` reaches `StateCurrent==EXECUTE`
  from ABORTED via input-steer corridor; replay-verified to 6.
- [x] **Unified a→e corridor source** — one interpreted best-first engine with static
  priors (governing tag / steer alphabet / horizon). Subsumes direct-literal,
  copy-coupling, copy-chains, counters, and target→governing indirection.
- [x] **Governing-tag selection** (`_governing`) — derived coil delegates to the
  richest stateful tag that gates it; multi-value tag governs itself. A self-updating
  `calc` whose wrapper op hides the step (`(Step+1)%6`) governs its own corridor
  (`_calc_self_referential`).  **Simulation probe** (`_probe_steps`): when static
  signals (stepping_tags, _value_richness) miss a tag, fork-steer-observe discovers
  whether it actually visits multiple values.  Ground truth — immune to copy-chain,
  tag-indirect-write, or any other mechanism that defeats static classification.
  Fixes PackML `how(IDLE)` / `how(EXECUTE)` from cold/ABORTED where pipeline
  `stepping_tags` missed `StateCurrent` (written via tag-to-tag copy from readonly
  named_array constants, invisible to `_literal_write_values`).
- [x] **Steer alphabet** (`_steer_alphabet`) — empty + pulse-each-cone-input, with
  fallback to all external Bool inputs; edge-gated commands use release-then-pulse
  (external inputs are *sticky*, so a clean rising edge needs an explicit release).
- [x] **Time-as-horizon** — a held wait advances timers to the crossing, so "advance
  time" is just an empty steer with a long horizon.
- [x] **Time-folding (tesseract).** Held waits **jump** to the nearest actionable
  accumulator crossing instead of ticking: **timers** ride the dt knob (one real step
  covers N dt-scans); **per-scan counters** are patched forward by `(skip-1)*delta`
  with the jump scan's own `execute` supplying the final increment (phase-kept, since a
  counter is timeless and the dt knob can't move it). The **plateau guard**
  (only-accumulators-moved) is the soundness gate; the actionable-crossing set
  (`_nearest_skip` over read done-bits + acc comparisons) only sets *how far* to jump.
  Emitted as one `({}, scans)` entry recording **real elapsed scans**, replay-verified
  at normal dt.
- [x] **Dynamic reaction budget.** The per-steer fold cap became a *reaction* budget
  (consecutive churn scans before a plateau forms), so a pulse that merely *starts* a
  dwell folds too — previously the pulse cap stopped at 6 scans and `how()` fell back to
  BFS. Inert/oscillating pulses still bail fast; the empty steer is effectively
  unbounded. Productive folding always runs to `_EMPTY_CAP` regardless of the budget.
- [x] **`fork()` captures clock + mid-accumulation fraction** — verified: `_frac:` timer
  fraction lives in `.memory` and is carried; a forked runner stays bit-identical across
  20+ scans. The re-fork/replay backbone the recovery layer needs is sound. (Reviewer
  linchpin; backjump via `fork(scan_id)` rests on this.)
- [x] **Interpreted verification** — replay the assembled path on a fresh fork; only
  return valid `Path`s.
- [x] **Wired into `_how_via_walk`** — tried before kernel compilation; on success it
  never compiles the kernel or runs BFS. Skipped when `avoid` is given (M0).
- [x] **Example conversion** — `examples/packml_bench.py` task converted to the
  resting Advance-flag/even-auto-advance pattern (cf. `examples/task_example.py`).
  `_CurStep` now *rests* at 1/3/5; modulo wrap (`% 6`) keeps it bounded for the prover.
- [x] **Real walker unit tests** — `tests/core/analysis/test_walk.py`: counter
  acc-patch path up & down (reachability via the walker, exact-crossing landing in a
  handful of real steps, normal-dt replay), pulse-started fold, and churn-budget bail.
- [x] **Vacuous-test fix (`f9e128d`)** — `bool(Condition)` now raises `TypeError`,
  root-causing the silently-passing PackML `how()` assertions across the suite; also fixed
  `_scalar_eq` in `waypoints.py`/`absorb.py`.
- [x] PackML baseline + pass-pipeline tests pass; full prove suite green (681) after
  both folding and the cap-lift.
- [x] **`avoid=` support** — `plan_walk` accepts `avoid_pred`; replay
  verification checks each intermediate state and rejects paths that pass
  through avoided states. Also fixed `avoid_pred` call signature to receive
  `dict(tags)` instead of state object.
- [x] **Multi-tag factoring** — `_walk_to_goal` with recursive prerequisite
  discovery via `_unsatisfied_conditions` (writer-rung enabling conditions +
  subroutine call gates) and `_check_residuals` (residual conditions when
  governing ≠ target). `plan_walk` delegates to `_walk_to_goal` instead of
  calling `_explore` directly. Depth-bounded at 6 levels with cycle detection.
  Tripwire test `test_walk_nested` (3-layer timer-gated state machine)
  solved: 5 steps, 1598 scans (~16 s simulated), ~1.3 s wall-clock. Prove
  suite green (686).
- [x] **Harness propagation through `fork()`** — `PLC._harness` attribute;
  `fork()` propagates installed Harnesses so forked runners inherit feedback
  couplings and pending patches. Feedback timing is preserved across forks
  without manual re-installation. Profile `_on_pre_scan` hook ticks on forked
  runners. Infrastructure for profile-gated walker paths and linked Fb
  exclusion.
- [x] **Linked feedback exclusion** — walker's `_steer_alphabet` excludes tags
  driven by the Harness (linked feedback tags via `Physical.link=`). The walker
  doesn't try to steer what the Harness synthesizes — it lets the Harness drive
  feedback and steers the enables/inputs instead. Profile fb tags also excluded
  from the plateau guard so they don't look like churn.
- [x] **`how(unlink=)` for fault scenarios** — `how(unlink=["Feedback"])`
  calls `Harness.unlink()` on the forked runner before walking, dropping named
  couplings so the walker forces the feedback tag directly (bypassing the
  physical chain and its delay). Models a broken sensor; the plan output with
  forces is the commissioning workaround. `Harness.unlink()` also exposed as
  a standalone API for manual fault-scenario modeling.
- [x] **Serial-clobber recovery (oracle-driven re-check)** — the first concrete
  Phase 4 recovery loop. Walking prerequisites/residuals serially on one fork
  can clobber an earlier corridor (a later sub-walk's side effect breaks a
  condition an earlier one established). `_recheck_prereqs` asks the projected
  oracle `cause(tag, to=value)` what still blocks the target — mining both
  proximate-cause `triggers` (projected mode) and `blockers` (unreachable mode)
  for actionable `(tag, value)` sub-goals — and `_recover_via_oracle` re-walks
  them, bounded by `_MAX_RECHECK_ITERS=3`. This **subsumed** the static
  `_unsatisfied_conditions` residual sweep in `_check_residuals`: the oracle
  loop both walks the normal residuals and recovers from clobbers in one bounded
  loop (the static residual special-case was removed, not layered over).
  `_needs_decomposition` (pairwise `upstream_slice` overlap + `writers_of`
  shared-writer check) logs a Tier 2 (force-and-solve) hint via
  `_log_decomposition_hint` before giving up; a `checkpoint = work.fork()` of
  the pre-clobber state is captured for that future path. Tests:
  `tests/core/analysis/test_walk_decomposition.py` (premise drive,
  walker recovery, replay, detection unit test). Oracle choice settled by
  exploration: `cause(target, to=True)` projected gives the cleanest actionable
  pairs (full fidelity; `why()` on a fork snapshot is only structural).

- [x] **Holds as first-class plan output (prevention before recovery)** — the
  POCL/causal-link insight: the walker's dominant clobber was self-inflicted
  (the pulse steer's global release), so represent the walker's own
  commitments explicitly. `_Hold`/`HoldStore` (per `plan_walk`, threaded
  keyword-only like `NoGoodStore`; empty store bit-identical).
  `_extract_holds` (strict for the independent-fork merge, last-wins for
  registration) + `_commit_holds` at every corridor commit point — including
  the delegate-corridor and recovery commits that bypass the `_walk_to_goal`
  wrapper. `_steer_prefix` skips protected names in all implicit writes;
  intended conflicting writes pass the divest probe (empirical
  goal-survives-release check, the seal-in case), tracked per-branch on
  `_Node.released` and made official at commit by `_reconcile_divests`.
  `Path.holds` + "Holds:" rendering surface the commitments to the operator.
  Holds never assert reachability: worst case is a premature `None` (safe
  direction) and `plan_walk` re-validates on a fresh fork. Recovery loop
  retained as backstop and still tripwire-covered. Tests:
  `tests/core/analysis/test_walk_holds.py`.

---

## LEFT — sequenced by planning concept

### Phase 1: Widen the input (let the engine accept more goals)

These are not "recovery" — they remove restrictions on what the existing engine
can even attempt. Highest leverage because they turn `None → fallback` into
`walk succeeds` without adding new mechanism code.

- ✅ **Or/And goal decomposition** — `_extract_goals` (replacing
  `_target_tag_value`) decomposes compound expressions via waypoints'
  `_extract_required_values`: `And` → collect all `(tag, value)` pairs and walk
  sequentially (chaining corridors on one fork); `Or` → pick the cheapest
  branch. Verification evaluates the full `expr`, not a single tag. Walker now
  solves `how(Ready, Done)` end-to-end (no BFS fallback). See
  `tests/core/analysis/test_walk_nested.py` for a condensed "hard for
  walk" program (nested timer-gated state machines) — tripwire for
  prerequisite-corridor support (Phase 1 factoring).

- ✅ **Multi-tag factoring** — recursive prerequisite discovery via
  `_walk_to_goal` + `_unsatisfied_conditions` + `_check_residuals`.  When
  `_explore` fails, `_unsatisfied_conditions` extracts the enabling
  conditions (including subroutine call gates) of the governing tag's
  target-value writer rung(s) that aren't met in the current state. Each
  prerequisite is walked recursively (depth-bounded at 6).  When `_explore`
  succeeds but the actual target tag isn't satisfied (governing ≠ target),
  residual conditions from the target's own writer are walked the same way.
  `plan_walk` now delegates to `_walk_to_goal` instead of calling `_explore`
  directly.  The tripwire test (`test_walk_nested`, 3-layer timer-gated
  state machine: PackML → production sequencer → heat task → y_Burner) is
  solved in 5 steps / 1598 scans (~16 s simulated, ~1.3 s wall-clock):
  CmdMode pulse, release, CmdStart pulse, fold 1096 scans (StateTimer +
  HeatDelay), fold 499 scans (HeatTimer).

- [x] **Non-Bool input steers (pipeline-aware)** — `_steer_alphabet` generates
  `_Steer("set", input, value)` entries for non-Bool ND inputs using
  `nondeterministic_dims` from the prover pipeline. `_steer_prefix` handles
  them as single-scan patches. `_governing` and `_richness` use pipeline
  classifications (`stateful_dims`, `nondeterministic_dims`,
  `combinational_tags`, `elided_tags`, `init_constant_projections`) for
  richness instead of the static heuristic. Inequality atoms (`gt/ge/lt/le`)
  in writer conditions are now resolved to concrete satisfying values via
  `_extract_inequality_prereqs` and `_extract_inequality_governing`.
  `_unsatisfied_conditions` no longer skips INPUT tags (the walker steers
  them) and extracts inequality prerequisites from writer SP trees including
  subroutine call-site conditions.
- [x] **Pipeline context integration** — `_how_via_walk` compiles kernel + runs
  `_build_explore_context(allow_partial=True)` before the walker. Pipeline
  tolerates infeasible tags (soft Intractable skipped); infeasible tags are
  simply absent from dimension dicts. Walker receives `explore_context`,
  `atom_index`, `domain_sources` for annotated `ReachabilityStep` output
  with semantic constraints. `_ExploreContext` gains `combinational_tags`,
  `elided_tags`, `functional_dep_projections`, `init_constant_projections`
  fields; `_PassContext.freeze()` populates them.
- [x] **BFS/waypoint fallback removed** — `how()` now uses the corridor walker
  as the sole path. On walker failure returns `Path(reachable=False,
  reason="walker: target not reachable")`. Old BFS/waypoint code retained
  behind `if False:` for audit reference. One test xfailed: opaque callable
  predicates need expr decomposition. Prove suite green (685 pass, 4 xfail).

  **What's left from the original plan description:** the *static*
  decomposition (SCC-condense the causal sub-graph, topological solve-order,
  `cause()`-based independence validation after each corridor). The dynamic
  fallback implemented here is the per-edge special case; the static
  decomposition is the generalization that would handle cyclic coupling and
  multi-corridor timing (Phase 6). Not needed until a program demonstrates
  the limitation.

### Phase 2: Widen the alphabet (let refinement succeed on more transitions)

These extend the action space so that `_apply_steer` / `_explore` can realize
transitions it currently can't express. They keep the engine's loop unchanged.

- ✅ **Helpful-steer ordering** — `_steer_alphabet` now orders candidates by
  relevance: inputs appearing in the enabling condition (`sp_tree()`) of the
  governing tag's target-value write-sites are tried first. Pure efficiency —
  no new coverage, just faster `_explore` convergence.

- ✅ **Non-Bool inputs** — analog/Int steers via pipeline `nondeterministic_dims`;
  inequality prerequisite resolution from `gt/ge/lt/le` atoms.

- ✅ **Drive-LOW steers** — `_steer_alphabet` now generates `"low"` steers for
  inputs appearing as `xio`/`fall` in the enabling condition. `_steer_prefix`
  handles falling-edge inputs (high→low sequence for `fall()`-gated transitions).

- ✅ **Multi-input steers** — `_conjunctive_input_groups` extracts
  multi-input patches from `And` nodes in enabling conditions;
  `_steer_alphabet` generates `_Steer("multi", patch={...})` entries;
  `_steer_prefix` handles simultaneous application with edge-aware release.
  Tested: two-key interlock (both high) and selector switch (mixed polarity).

- ✅ **Link-aware de-energization** — `link=` serves three purposes:

  1. **Plan realism** — linked fb tags are excluded from the steer alphabet
     (`plan_walk` lines 1733–1739), so the walker *must* steer the upstream
     enables/inputs; the Harness converts those into feedback with proper
     delay. The plan reads as operator actions, not abstract state mutations.
  2. **Timing accuracy** — `Physical.on_delay` is respected: the Harness
     handles feedback timing on every fork (propagated via `PLC._harness`);
     `_harness_nearest_scan` constrains fold distance so jumps don't skip
     pending feedback patches.
  3. **Resource visibility** — deferred to Phase 6 (multi-corridor
     mutual-hold conflicts: two corridors can't both hold their enables
     simultaneously). Not needed until a multi-corridor program demonstrates
     the limitation.

  Implementation achieved by exclusion: linked fb tags filtered from
  `ext_inputs` and `edge_ext`; Harness installed on work fork and propagated
  to trial forks via `fork()`; verify fork gets its own Harness for
  step-by-step replay at full fidelity.

  **Fault-scenario override (`unlink=[...]`):** `how(unlink=["Feedback"])`
  calls `Harness.unlink()` on the forked runner, dropping named couplings so
  the walker forces the feedback tag directly (bypassing the physical chain).
  Also exposed as `Harness.unlink()` standalone API.

### Phase 3: Execution monitoring (detect when the plan goes wrong)

Lightweight; triggers re-planning rather than being a mechanism itself.

- ☐ **Path-sequence divergence** — during commit, assert the governing value
  follows the planned value sequence. Mismatch = divergence → triggers
  backtracking (Phase 4). The path already implies the expected sequence; this
  is ~5 lines of checking, not a new data structure.

- ☐ **Must-stay violation** — assert held waypoints across the suffix, not just
  the final value.

- ☐ **Deadline-race** — when a divergence deadline is known, race it against
  the target crossing in the time-jump math (jump to whichever fires first).
  The crossing arithmetic is built; this adds the deadline as a competing entry.

### Phase 4: Backtracking on refinement failure (the recovery loop)

This is where the engine becomes a proper hierarchical planner with learning.
The key architectural choice: **one generic backtracking loop with learned
constraints**, not per-pattern handlers. Each "mechanism" is a composable
strategy the loop can invoke, but the loop itself is uniform. Recovery is
pluggable — new mechanisms extend reach without changing the loop.

The oracle layers apply here as: `why()` generates regression sub-goals
(what's holding the governing tag stuck); `cause()` extracts nogoods from
failed simulation (what actually blocked the transition). Together they replace
the symbolic nogood-learning and weakest-precondition machinery of classical
planners with empirical observation.

- ✅ **Serial-clobber recovery (first recovery loop)** — `_recover_via_oracle`
  in `walk/engine.py`. When the serial prereq/residual walk leaves the target short of
  its value, `_recheck_prereqs` queries projected `cause(tag, to=value)` for the
  still-unsatisfied proximate causes (`triggers`) and blockers, and the loop
  re-walks them (bounded by `_MAX_RECHECK_ITERS=3`). Subsumed the static
  residual sweep in `_check_residuals`. **Now nogood-aware** (next item): the
  loop records the cause()-named blocking assignment and re-explores with the
  refined seen-key instead of blindly re-walking cause() goals in order.

- ☐ **Third `_explore` exit** — today: success or `None`. Add:
  reached-governing-but-diverged (carrying a `cause()` payload from the scan
  log). This is the backtracking trigger.

- ✅ **Precondition accumulation (nogood learning)** — `NoGoodStore` (+ frozen
  `_NoGood`) in `walk/engine.py`. A nogood is keyed on `(from_value, to_value,
  frozenset(blocking))` where `blocking` is the precise cause()-named
  `(tag, needed_value)` assignment (from `_recheck_prereqs` /
  `cause(tag, to=value)`, projected). Add-only over the finite (gov value) ×
  (gov value) × (powerset of blocking pairs) product ⇒ termination;
  `_MAX_RECHECK_ITERS=3` stays the hard cap. The store is constructed once per
  `plan_walk` and threaded (keyword-only, `None`→fresh) through `_walk_to_goal`
  → `_explore` / `_recover_via_oracle` / `_check_residuals`; with `nogoods=None`
  the seen-key projects to `()` so the whole existing suite is bit-identical.
  `_recover_via_oracle` records the nogood *before* re-exploring, so the refined
  seen-key (projection onto `blocking_tag_names()`) plus a **blocker-clearing
  move** in `_explore` (a non-governing steer that clears a learned blocker —
  e.g. `Reset` clearing `Guard_A` without changing `Latch_B`) opens the
  guard-clearing corridor that the bare-value seen-key collapsed onto the start
  node. A repeat config trips `is_blocked` and bails immediately.
  `_needs_decomposition` gained an optional `nogoods` + `(from,to)` hook that
  OR-s in `all_orderings_blocked`. **Result:** the cross-guard mutual-clobber
  tripwire (`tests/core/analysis/test_walk_nogood.py`, two self-sealing
  latches cross-gated by guards sealed at each other's timer-done arm) is solved
  in ≤2 recovery iters; the pre-Phase-4 loop returned `reachable=False` (blindly
  re-walked cause() goals in order → re-clobbered → no convergence). Exploration
  settled the blocking source on cause() over the SP-tree:
  `_unsatisfied_conditions(Latch_B)` returns `[]` for the guard-gated arm while
  `cause(Latch_B, to=True)` cleanly names `Guard_A=False`. No symbolic
  implication graph — the scan log IS the implication graph.

- ☐ **Backjump to cause origin** — `fork(scan_id)` checkpoints; reuse
  `_find_backjump_target` (waypoints.py:2162). Go back to where the divergence
  originated, not just one step.

- ☐ **Constructive regression** — `why(governing_tag)` on the stuck fork gives
  the minimal load-bearing contacts. Each conjunctive root that is NOT an
  external input becomes a sub-walk goal. `simplified()` expands to full
  formula when `why()` roots are insufficient (rare: `why()` already prunes
  irrelevant branches). Depth-bounded (3 levels) to avoid combinatorial
  explosion.

- ✅ **Inverse regression (seal-in break)** — `_latch_break_conditions`
  extracts inputs that falsify the writer rung's `And` conjuncts (skipping
  self-references).  `_unsatisfied_conditions` falls back to this when no
  writer produces the target value.  Handles both OTE seal-ins (`out()` with
  OR feedback) and `latch()` instructions.  The full `why()`-based reset
  path from the plan (using `_walk_reset_path` for explicit `reset()`
  instructions) is not yet needed — the seal-in break covers the common
  pattern.  Phase 4's backtracking loop would generalize this.

- ✅ **`seen` keyed on `(value, nogood_state)`** — `_explore` now keys `seen` on
  `(governing_value, nogoods.project(snapshot))` for the start key and every
  successor. The projection is onto `blocking_tag_names()` (cause()-identified
  blocking tags only) and returns `()` for an empty store, so a fresh walk
  partitions exactly as the bare value did. After a nogood is learned, distinct
  blocking-tag configs at the same governing value become distinct keys, letting
  a re-walk re-enter a value under different learned constraints (the nogood set
  is the distinguisher). Paired with the blocker-clearing move so a steer that
  only changes a learned blocker (not the governing value) is still enqueued.

- ☐ **Keep failed forks alive** — each `_Node`'s fork *is* the checkpoint;
  retain parent pointers instead of dropping on `popleft`.

### Phase 5: Diagnosis (explain infeasibility)

This is what lets the walker return `Diagnosis` instead of `None` — the gate
for removing BFS fallback entirely.

The oracle architecture makes diagnosis concrete: `why()` on the stuck state
gives the minimal explanation of what's holding the governing tag; `cause()`
on each failed steer attempt gives "rung N blocked because tag X was Y, held
by rung M's seal-in." No symbolic unsolvability proof needed — exhaustive
steer trial + `cause()` on each failure IS the certificate for a deterministic
system from a specific state.

The core diagnosis work depends on recorded `cause()`, which is available now
(not on projected mode). Recorded `cause()` already works at full ScanLog
fidelity; the real interpreter + scan log is the diagnosis substrate.

- ☐ **Trigger/enabler split via `cause()`** — recorded mode at full ScanLog
  fidelity (trigger = what transitioned; enabler = what was already wrong).

- ☐ **`effect()`-confirmed minimal cause** — fork-and-test each enabler.

- ☐ **Deadline extraction** — timer Done → annotate with preset: "establish
  enabler within N scans."

- ☐ **Diagnosis as return type** — `how()` returns `Path | Diagnosis`.
  `Diagnosis` carries: preconditions tried (from Phase 4 nogoods),
  contradicting enablers (from `cause()`), actionable blockers (from `why()`
  roots). Distinguish:
  - `Unsolvable(cert)` — all steers exhausted from this state + `cause()`
    shows each failure is structural (not timing-dependent). The certificate
    is the set of `cause()` payloads proving closure.
  - `NotFound(reasons)` — budget exhausted. Carries: best partial plan,
    first failing edge, accumulated nogoods. Always actionable: "got to
    value V but couldn't reach W; `why()` says tag X is blocking."
  Recognize transient/never-rests (`_stable_step_values`, waypoints.py:1449)
  → "unreachable: transient" instead of silent fall-through.

### Phase 6: Multi-corridor timing resolution

Runs *above* the per-corridor layer. Three tiers, simplest first:

**Tier 1 — Force and sum (DONE, generalized).** Pin the coupling signal as a
constant. Each corridor solves independently. The mechanism is not a Phase 6
special case — it's the generic independent-fork walk (`_try_independent_walks`)
applied at every serial-walking site: (A) target prereqs in `_walk_to_goal`,
(B) governing prereqs after `_explore` fails, (C) recovery goals in
`_recover_via_oracle`, (D) compound goals in `plan_walk`. Each site: check ≥2
sub-goals → independence gate (pairwise disjoint `upstream_slice`) → walk each
on a fresh fork → cone-filter holds → multi-steer + time-fold. Site D uses
`_apply_steer_compound` for sequential monitor iteration (fold with each
unsatisfied goal as monitor until all satisfied). Validated: rendezvous — 2
actions, 30 scans.

**Tier 2 — Force and check the deadline.** Same as Tier 1, but the coupling
carries a timer preset. Compare the producer's achieved depth against the
preset. One number. The deadline-extraction from Phase 5 (timer preset
annotation on `cause()` triggers) provides this number. **Detection wired:**
`_needs_decomposition` (pairwise `upstream_slice` overlap + `writers_of`
shared-writer check) flags coupled prerequisites, logged via
`_log_decomposition_hint` at the give-up point, with a pre-clobber
`checkpoint = work.fork()` captured to fork from. The force-and-solve mechanism
itself waits for a mutual-interference test case.

**Tier 3 — Iterate to fixed point (cyclic coupling).** The coupling time is
itself the unknown — A's timing depends on B's depends on A's. No constant to
pin. Guess the coupling time, force it, solve both, read achieved times, feed
back as the next guess. Converges when the timing-update map is consistent;
diverges (diagnosably) when the program has a genuine timing contradiction.
Same converge-or-diagnose shape as single-corridor recovery, lifted to timing.

**⚠ Open: Tier 3 convergence oscillation.** The iteration can limit-cycle
(neither converge nor diverge) if the timing-update map is non-monotone. Need
a cycle-detection guard: track the full history of (checkpoint, timing-guess)
pairs, stop on revisit. Without this, Tier 3 can spin silently. See Open Items.

Additional multi-corridor items:

- ☐ Convergence diagnosis (relative timing across a sync edge). `cause()` on
  the clobber scan identifies which corridor wrote what and when.
- ☐ Divest-as-sync-edge (see "Divest points" below).
- ☐ Reschedule (different linearization, not a precondition fix). Try
  alternative topological orderings from the Phase 1 condensation DAG.
- ☐ Co-advance cyclic synchronization (SCC of subsystems). When both orderings
  (A-before-B, B-before-A) yield clobbers → true deadlock; diagnose the
  mutual exclusion.

### Window characterization (how() output spec)

Each step in the plan output carries a timing window: how long the receptive
state persists before a deadline closes it. Computed from data already
available:

- The receptive state opened when the interlocks were satisfied (the walker
  knows when).
- The deadline closes it when a timer preset expires (from the crossing
  schedule).
- The window = deadline scan − opening scan.

No perturbation replay needed. The walker already observed both events. The
output becomes a timed opportunity map: "flip CmdStart — you have 60 scans
(~3 seconds) before the start timeout." The narrowest window in the plan is the
plan's overall timing fragility.

Most steps are level-gated (wide windows). The plan only records external ND
inputs, not internal edges, so edge-sensitivity (rise/fall) is rarely the
fragile case — the fragile case is a short deadline window.

### Divest points as emergent waypoints

The precondition set's non-monotonic points — where forward progress requires
releasing a held precondition — are natural phase/waypoint boundaries. Within a
phase, accumulation is monotonic (establish and hold). At the boundary,
something load-bearing for phase N must be released because it blocks phase N+1.
The divest IS the waypoint, discovered by walking rather than by static
analysis.

This matters most when no single tag's value graph defines the corridor
(compound goals, coupled subsystems). The divest structure is more fundamental
than the mode-value structure and still defines phases even when the
value-transition graph doesn't.

Divests that land on narrow interfaces are convergence constraints — where one
corridor's recovery becomes another's scheduling constraint. This bridges the
single-corridor and multi-corridor scopes.

### Still-open odds & ends

- ✅ **`avoid=` support** — `plan_walk` accepts `avoid_pred`; replay
  verification checks each intermediate state and rejects paths that pass
  through avoided states.
- ☐ **Cheap trial** — `with plc.trial():` snapshot/restore instead of
  `fork()`-per-candidate (fork is ~ms; lookahead does many).

### Superseded / deliberately dropped

- ~~**Planner B as a per-segment scoped-BFS fallback**~~ — killed by *no BFS
  fallback*. A stuck hop is handled by Phase 4 backtracking.
- ~~**Split-horizon cap**~~ — replaced by the dynamic reaction budget (DONE).

---

## Findings (so we don't re-derive)

- **`fork()` is a true checkpoint** — carries `.tags`, `.memory` (incl. `_frac:` timer
  fraction), time mode, dt, RTC, and `_harness` (feedback couplings + pending patches).
  Verified bit-identical continuation across 20+ scans after a mid-fraction fork.
  Backjump via `fork(scan_id)` (runner.py) rests on this. Harness propagation means
  forked runners inherit physical feedback timing — the walker's forks see the same
  feedback behavior as the parent.
- **Corridor source is NOT the existing waypoint front-half.** `_order_waypoints`
  collapses the StateCurrent↔StateRequested↔StateEnableYes SCC into one cone-21
  mega-waypoint (> `_MEGA_CONE_LIMIT=18`), so today's `how(EXECUTE)` falls straight to
  the OOM-prone undecomposed BFS. And `_build_value_transitions("StateCurrent")` is
  **empty** because StateCurrent is `copy`-written. The real graph comes from chasing
  the copy coupling (or, generally, interpreted probing).
- **Steering uses the interpreted runner, not projected `cause()`/`effect()`.** Projected
  mode is now copy/calc-aware (d450a64), but the interpreted runner remains the forward
  oracle for steering — strictly more faithful (multi-scan, full state). `cause()` is
  reserved for the *backward* (divergence) direction, where recorded mode works at full
  ScanLog fidelity.
- **`events.py` crossing scheduler is welded to the BFS** (`_ExploreContext` +
  `ReplayKernel` + absorption pipeline) — not runner-consumable wholesale. The walker
  re-derived the held-wait crossing *arithmetic* from timer/counter instruction
  introspection (`.accumulator`/`.preset`/`.done_bit`), which is the simple slice (no
  co-firing / input-variant / abstract-threshold branching).
- **External inputs are sticky** (hold last value); `patch()` clears the patch, not the
  tag. Edge-gated commands need release-then-pulse.
- **Verification is interpreted** (replay on a fresh fork) — no compiled-kernel
  agreement risk for the walk path.
- **No abstraction gap (the oracle advantage).** Classical planners suffer
  because their model (PDDL operators, BDDs) can diverge from reality. Our
  "model" is the program running on a fork — correct by construction. The
  tradeoff: can't generalize as freely (a nogood at state S might not hold at
  S'), but never have false positives. Closer to concolic execution or
  sample-based motion planning (RRT) than classical AI planning.
- **`why()` is the state-aware candidate generator.** Unlike `simplified()`
  (structural, all formula paths) or PDG (coarsest connectivity), `why()`
  gives the *minimal load-bearing contacts* for the current fork state via
  SP-tree attribution. Terminates at external inputs. Handles stateful (latch
  seal-in) vs. stateless (OTE) differently. Gives a unified tree for multi-tag
  queries — the tree's structure IS the factoring structure.
- **`why()` resolves subroutine rungs.** The `_RungResolver` uses the PDG
  node's `subroutine` field to look up rungs in `program.subroutines[name]`
  instead of only seeing the main logic. Before this fix, `why()` silently
  skipped any writer inside a subroutine (the old `rung_index >= len(logic)`
  guard). Now `why(y_Burner)` from cold start produces the full 3-layer
  causal tree through `burner_prod_steps` and `burner_heat_task` — the
  factoring structure for Phase 1 is directly visible.
- **Multi-tag factoring uses writer-condition extraction, not `why()`.** The
  plan hypothesized `why()` as the prerequisite discoverer, but the simpler
  path is direct: `_unsatisfied_conditions` reads the writer rung's SP tree
  via `_extract_condition_values` and includes the subroutine call gate by
  scanning `pdg.rung_nodes` for callers. This is sufficient for the
  producer-consumer hierarchies PLC programs enforce. `why()` remains
  available for the static SCC decomposition (Phase 6) if needed.
- **Dynamic prerequisite ordering is sufficient.** No explicit topological
  sort needed: when prerequisite A depends on B, walking A recursively
  discovers B as its own prerequisite. The `visited` frozenset prevents
  re-walking already-satisfied goals. The depth bound (6) is conservative;
  the tripwire test uses 4 levels.
- **`cause()` is the validation/nogood oracle.** Recorded-mode causal analysis
  on the scan log. Gives trigger vs. enabler split. Use after simulation to
  extract nogoods (what blocked), confirm independence (no clobber), and
  produce actionable diagnosis. The scan log IS the implication graph — no
  symbolic derivation needed.
- **Pipeline `allow_partial` is safe for the walker.** Infeasible tags just
  don't appear in dimension dicts — the walker works with what it gets. The
  `always()`/`never()` paths never pass `allow_partial`, so their Intractable
  gate is unchanged.
- **`avoid_pred` receives `dict(tags)`, not state.** The `avoid_pred` callback
  in `plan_walk` replay verification passes `dict(verify.state.tags)` not the
  raw state object. This matches how BFS `state_filter` was invoked.
- **`projected_cause()` had the same subroutine blindness as `why()`.** Writers
  inside subroutines were resolved against `logic` (main rungs) using
  `rung_idx < len(logic)` — when the index happened to be in range it silently
  resolved to the wrong rung; when out of range it was silently dropped. Both
  paths made `cause(tag, to=value)` return `NO_OBSERVED_TRANSITION` for any tag
  written inside a subroutine. Same class of bug fixed in `why()` (6f443d9).
  Fix: check `node.subroutine` first, resolve from `program.subroutines`.
  `effect()` is NOT affected — its `rung_idx` comes from simulation capture
  (main-program indices only), not from PDG nodes.
- **Intermediate-result prerequisites should not block retry.** When
  `_unsatisfied_conditions` extracts writer-rung conditions as prerequisites,
  some may be intermediate results that the corridor handles internally via
  time-folding (e.g. `Trans==1` is set when a timer completes during CurStep
  corridor execution). Walking these as standalone goals fails (the timer needs
  the corridor context). Fix: `continue` past failed prerequisites instead of
  `return None`, then retry `_explore` — the corridor handles them.
- **Pipeline domains are boundary-focused.** Behavioral bisection produces
  expression partition values (comparison literals ± 1 + default), typically
  5–10 values per ND input. No extra thinning needed for steer alphabet.
- **Tier 1 insertion is before the delegate corridor, not after.** The initial
  hypothesis was to insert Tier 1 after the serial prerequisite walk fails.
  But for the rendezvous pattern, `_governing` picks InitA (a delegate), and
  `_explore` SUCCEEDS at driving InitA — the failure happens downstream in
  `_check_residuals` when driving InitB clobbers InitA. The correct insertion
  point is before the delegate-corridor path: when `governing != target_tag`,
  extract the *target's* unsatisfied conditions and try Tier 1 on those. This
  preempts the serial walk entirely. A secondary insertion remains in the
  prerequisite section of `_walk_to_goal` for cases where `_explore` fails
  with independent prerequisites for the *governing* tag.
- **Cone-filtered hold extraction prevents steer-release contamination.** Each
  independent fork's action list includes "release all held inputs" actions
  from the pulse steer prefix. Naively extracting all external-input changes
  would capture cross-cone releases (fork 2 releasing fork 1's enable as a
  side effect). Filtering holds to the prerequisite's upstream cone avoids
  this — only inputs causally connected to the prerequisite are collected.

---

## Validation status

| Target | Corridor type | Steer | Result | Notes |
|---|---|---|---|---|
| `StateCurrent==EXECUTE` from ABORTED | mode machine | input pulses | walk ~2 s, replay→6 | go/no-go |
| `_CurStep==5` from EXECUTE | task timer wait | empty (folded) | walk, replay→5 | now **folded** via dt-knob (was ticked); old BFS = wrong "unreachable" |
| counter dwell 0→1 (synthetic) | per-scan counter | empty + pulse | folds via acc-patch | `test_walk` — up & down, exact landing, replay-verified |
| `_CurStep==5` from cold/STOPPED | nested | — | None → fallback | needs Phase 1 factoring |
| `how(Ready, Done)` (two-step latch) | compound And | input pulses | walk 3 steps, 0.0 s | Phase 1 Or/And decomposition |
| `y_Burner` from cold (nested) | 3-layer timer-gated | CmdMode + CmdStart + 2 folds | walk 5 steps, 1598 scans, ~1.3 s | Phase 1 factoring: recursive prereqs through 3 subroutine layers |
| `StateCurrent=="IDLE"` from cold | mode (string operand) | input pulses | walk 2 steps | simulation probe finds StateCurrent steps |
| inequality-gated transitions | analog/Int ND input | set-value | walk via pipeline domains | 16 tests fixed with `nondeterministic_dims` steers |
| callable predicate (`expr=None`) | opaque | — | xfail | walker needs expr decomposition |
| linked feedback exclusion | Harness-driven fb | input steers | walk via enables | fb tags excluded from steer alphabet |
| `how(unlink=["Fb"])` fault | broken sensor | direct force | walk forces fb | bypasses physical chain delay |
| profile-gated (`Temp >= 5.0`) | analog ramp | hold + profile | walk ~500 scans | Harness ticks profile on fork |
| serial clobber (Latch_A/Latch_B share Input_B cone) | coupled latches | pulses + reset | walk recovers via oracle re-check | `test_walk_decomposition`; `cause(Target, to=True)` re-derives Latch_A after Latch_B clobbers it |
| cross-guard mutual clobber (Latch_A/Latch_B each gated by the other's guard) | coupled latches + 2 timers | holds + reset | walk recovers, ≤2 recovery iters | `test_walk_nogood`; nogood records cause()-named `Guard_A=False` blocker, refined seen-key + blocker-clearing move opens Reset-then-hold-B; naive loop returned `reachable=False` |
| Int command protocol (Stopped→Idle→Execute) | multi-hop state machine | CmdReset + CmdStart pulses | walk 3 actions | `test_walk_real_patterns`; validates 2-step command sequence through Int validation gate |
| return_early() flow gating | subroutine flow control | Enable pulse | walk reachable | `test_walk_real_patterns`; PDG models return_early as enabling condition |
| rendezvous (two SFCs, simultaneous hold) | independent subsystems | multi-steer (Tier 1) | walk 2 actions, 30 scans | `test_walk_real_patterns`; Tier 1 simultaneous hold: walks each prereq on independent fork, merges holds, fold converges both timers |
| odd/even step sequencer (CurStep%2 auto-advance) | self-increment + even skip | Advance pulse + fold | walk reachable | `test_walk_real_patterns`; probe + time-fold handles CurStep self-write |
| deep call chain (Mode→State→SFC→Step→Output) | 5-level prereqs, 3 sub scopes | CmdProd + CmdReset + CmdStart + fold + Confirm | walk reachable | `test_walk_real_patterns`; required cause() subroutine fix + prereq-skip retry |
| full suite | all types | all steers | test-prove 726 pass, 4 xfail | BFS fallback removed, walker-only; Tier 1 simultaneous hold |

---

## Open items / poke list

Honest accounting of what's unresolved:

1. **Convergence oscillation (Tier 3).** The fixed-point iteration over cyclic
   coupling can limit-cycle if the timing-update map is non-monotone. Need a
   cycle-detection guard: track the full history of (checkpoint, timing-guess)
   pairs, stop on revisit. The current spin guard only catches
   identical-set-identical-state; oscillation between different states cycles
   forever.

2. **Narrow-cut cardinality screening.** The factoring pass screens for
   syntactically narrow interfaces (≤2 tags). A two-tag interface carrying a
   Boolean plus a multi-valued state channel looks narrow but behaves wide.
   Screen on domain cardinality: Boolean-only cuts are safe; anything with
   domain > 2 deserves skepticism. Not fatal (walk fails and cause explains),
   but saves wasted corridor attempts.

3. **Multi-corridor validation (partial).** The rendezvous pattern (two
   independent SFCs, simultaneous hold, And gate) has been walked end to end
   via Tier 1. This validates the "independent subsystems" case. Still
   missing: a program with coupled subsystems, a real handshake, and a
   deadline, walked including a convergence repair (Tier 2/3).

4. **Input timing fragility.** Plans assume inputs arrive on the exact scan the
   walker placed them. For level-gated steps this doesn't matter (wide window).
   For tight-deadline windows it could. The window characterization (above)
   surfaces this; no further mechanism needed beyond making it visible.

5. **Spin guard (termination).** If the nogood set hasn't grown since the last
   attempt from this checkpoint, stop. Identical set + identical state + still
   failing = not an ordering problem; report the contradiction. Multi-corridor
   variant: each corridor individually solved but convergence still infeasible
   after rescheduling = coordination contradiction, not a precondition gap.

6. **Callable predicate (`expr=None`).** One test xfailed: opaque callable
   predicates can't be decomposed into tag/value goals. Needs expr
   decomposition or a thin adapter that tries the predicate after walking.

8. **Profile-gated walker paths (DONE).** `_advance_time` recognizes active
   profile couplings as a source of progress — when no traditional accumulator
   is advancing but a profile is ramping, it keeps stepping (one scan at a
   time) instead of bailing. The Harness ticks the profile each scan via
   `_on_pre_scan`. Profile fb tags are excluded from the plateau guard so
   they don't look like churn. Tested: `Temp >= 5.0` via linear thermal
   profile at 0.01/scan reaches goal in ~500 scans. Harness now propagates
   through `fork()` (`PLC._harness`), so profile-aware walking works on
   forked runners without manual re-installation.

7. **Dead BFS code.** The old BFS/waypoint fallback is behind `if False:` in
   `runner.py`. Should be deleted once the walker covers the remaining edge
   cases (Phase 4 backtracking, Phase 5 diagnosis).

---

## Research grounding

The corridor walker sits at the intersection of several established fields.
The individual mechanisms all have prior art. The novel contribution is
precisely scoped below.

### Prior art by mechanism

| Mechanism | Reference | Relationship |
|-----------|-----------|--------------|
| Corridor walk (directed forward search) | Directed model checking (Edelkamp, Lluch-Lafuente, Leue) | Heuristic-guided forward search over the executable system. The walk-and-steer loop. Our PDB over the value graph is their abstraction-based heuristic. |
| Helpful-steer ordering | FF helpful actions (Hoffmann-Nebel) | Applied via exact structure instead of delete-relaxation |
| Time-jump at crossings | Hidden-event acceleration / timed-automata event-driven simulation | |
| Causal diagnosis (`cause()`) | Halpern-Pearl actual causality; "Explaining Counterexamples Using Causality" (Beer, Ben-David, Chockler); causality checking (Leitner-Fischer, Leue) | Formalized what `cause()` does. They diagnose to explain; we diagnose to *act* (feed cause back into the planner as a repair signal). |
| Regression sub-goals | System-R regression (Bonet, Geffner) | Choose first unsatisfied subgoal, regress it, progress the state through the achiever, repeat. The pre-reasoned skeleton of the simplified()-recursion-with-repair loop. |
| Nogood / precondition accumulation | Conflict-driven state-space search (Steinmetz-Hoffmann); CDCL (SAT) | The precondition set IS the no-good set. `cause()` replaces their expensive conflict analysis (Algorithm 2). |
| Backjump to cause origin | Conflict-directed backjumping (CDCL / CSP); Steinmetz-Hoffmann for planning | |
| Factoring (causal graph decomposition) | Helmert causal graphs; star-topology decoupled search (Gnad-Hoffmann) | |
| Convergence / deadline diagnosis | Timed automata (Alur-Dill; UPPAAL); fault ascription (Leitner-Fischer, Leue) | Relevant to Tier 2/3 feasibility checking |
| Lock-and-key / gadget-maze planning | Demaine, Hendrickson, Lynch; Hoffmann Grid benchmark | General problem is PSPACE-complete. PLC programs are in the tractable subclass (one-state gates, readable locks, hierarchical ordering). |

### What's novel

The **closed loop** — actual-cause attribution (`cause()`) as the repair
signal in a solver-free forward planner over the executable program, aimed at
producing an operator-executable plan. The analyzer is Halpern-Pearl. The
planner is directed model checking. The regression is System-R. Wiring them
together without a solver, because the program is the model: that's the
contribution.

Classical planners need a solver to bridge the gap between model and reality.
When the model IS reality (forkable, per-rung steppable, deterministic), the
solver collapses to "try it and observe." The program runs on forks; `why()`
generates candidates by backward SP-tree attribution on the live state;
`cause()` validates by recorded-mode scan-log analysis; the walker steps
forward on the real interpreter.

### Key papers

- Steinmetz & Hoffmann (2016), *Towards Clause-Learning State Space Search*,
  AAAI — the conflict-driven learning loop; `cause()` replaces their Algorithm 2.
  https://fai.cs.uni-saarland.de/hoffmann/papers/aaai16.pdf
- Steinmetz & Hoffmann (2016), *State Space Search Nogood Learning*, AIJ —
  length-independent sound nogoods (trustworthy `Unsolvable`).
  https://www.sciencedirect.com/science/article/pii/S0004370216301448
- Steinmetz (2022), PhD thesis, *Conflict-Driven Learning in AI Planning
  State-Space Search* — convergence + trap learning.
  https://dblp.org/rec/phd/dnb/Steinmetz22.html
- Lipovetzky & Geffner (2017), *Best-First Width Search* — novelty/memory bound
  (if a residual segment ever needs real search).
- Helmert (2006), *The Fast Downward Planning System* — causal graph
  decomposition, domain transition graphs.
- Timing/deadlines: timed-automata tradition (Alur–Dill; UPPAAL).
