  TL;DR

  The architecture is sound and the plan document is unusually honest about what's proven versus hoped. But the code has drifted from the plan's
  own stated principle ("one generic backtracking loop, not per-pattern handlers"): there are now four implementations of the same solve loop
  (plan_walk's compound-goal loop, _walk_to_goal's prereq path, _recover_via_oracle, _check_residuals) and four call sites for
  _try_independent_walks. The core insight that I think collapses both current and future code: the plan representation is too thin.
  list[(patch, scans)] discards exactly the information Phases 3–6 all need — which inputs must stay held, for which goal, until when. Making
  holds (protection intervals) first-class would convert your dominant recovery pattern into a prevention pattern. More on that below.

  I also found four concrete bugs/inconsistencies while reading.

  Concrete issues in walk.py

  1. nogoods is dropped at one _check_residuals call site — the one at the end of the serial-prerequisite path in _walk_to_goal
  (walk.py:2422-2438) passes nd_domains and explore_context but not nogoods=nogoods, so _check_residuals constructs a fresh empty store. This is
  the most clobber-prone path — exactly where learned nogoods matter most. The other three sites thread it correctly. This is also evidence for
  the parameter-threading problem (point 1 under "mechanical collapses").
  2. unlink= doesn't reach the verify fork. plan_walk forks verify from the original plc and installs a fresh Harness(verify)
  (walk.py:2685-2689) — with all couplings intact. A plan computed under unlink=["Fb"] (walker forces the fb tag directly) is then verified with
  the coupling live. If the upstream chain never fires during replay, no scheduled feedback patch conflicts and the test passes by luck; in
  general the fault-scenario plan is verified against a different physical model than it was planned under. The fix is to apply the same unlink
  list to the verify harness. The annotate fork (walk.py:2708) gets no harness at all, so constraint annotations can be computed against
  divergent feedback state — lower stakes (annotations only), but same class.
  3. The transition=(gov_value, gov_value) passed to _log_decomposition_hint (walk.py:2400, and (target_value, target_value) at 2535) will
  essentially never match a recorded nogood key — nogoods are recorded as (current_from_value, target_value) in _recover_via_oracle
  (walk.py:1914,1936). So the all_orderings_blocked OR-in to the Tier 2 hint is dead code in practice. Either pass the actual from-value or drop
  the parameter until Tier 2 lands.
  4. Minor duplication: the _OPS comparison dict is defined twice (_satisfying_value, _extract_inequality_prereqs); _extract_inequality_prereqs
  has an if/elif on isinstance(operand, str) whose two branches are identical (walk.py:583-585).

  The core insight: holds as first-class plan output

  Your recurring failure mode — across serial clobber, residual clobber, rendezvous, cone-filter "steer-release contamination," divest points —
  is one phenomenon: the walker establishes conditions but doesn't represent the obligation to maintain them. Each mechanism that landed
  re-derives that obligation after the fact:

  - The pulse steer's prefix releases every held external input (_steer_prefix, walk.py:1476) to get a clean rising edge. That global release is
  the clobber generator; your own _try_independent_walks docstring says so.
  - _try_independent_walks then re-mines holds out of sub-plan patches with cone filtering — a heuristic patch over not having tracked them in
  the first place.
  - _recover_via_oracle exists largely to repair what the global release broke.
  - Divest points, window characterization, Phase 3 must-stay monitoring, and Tier 2 deadline checks all need exactly this data, and each will
  re-derive it again.

  The collapse: let every successful sub-walk return (actions, holds) where holds maps input → (value, established_for_goal). Then:

  - Pulse release becomes selective: release the steered input and the edge_ext tags that aren't protected holds. Many clobbers simply stop
  happening — prevention instead of recovery. The cone-filtering heuristic can be deleted.
  - Serial composition becomes a check: a sub-walk that must release a protected hold is either re-ordered, a divest point (your "emergent
  waypoint" — now detected by construction rather than discovered), or a genuine conflict → nogood. The clobber-detection half of
  _recover_via_oracle shrinks; the oracle stays for the cases where the program (not the walker's own release) breaks a condition.
  - Window characterization is an annotation on a hold (open scan, deadline scan). Phase 3 must-stay is a monitor over holds. Tier 2's deadline
  comparison is a hold with an expiry.

  This doesn't violate your "static analysis is a prior, never correctness-bearing" principle, and that's worth stating precisely: holds
  describe the walker's own commitments over external inputs, which are sticky and entirely under walker control. There's no abstraction gap to
  be wrong about — you're modeling your own hand, not the program. The prior art slot in your research table is POCL/causal-link planning (an
  action establishes p for a consumer; threats to the protection interval are resolved by ordering or rejected) — it fits right next to the
  System-R row, and it's the classical answer to exactly the clobber problem you've been solving empirically.

  Two companion representation upgrades, same theme:

  Plan as tree, not flat list. all_steps: list[_Action] loses which actions served which goal. Backjump (your next item) needs "drop the subtree
  for the goal that diverged"; Phase 5's NotFound(best partial plan, first failing edge) needs the tree to print; per-goal holds attach
  naturally to tree nodes. A flat list forces all of these to re-derive structure. If each _walk_to_goal returns a node (goal, actions, holds,
  children) and flattening happens once at the end for Path, backjump and diagnosis become tree operations instead of new bookkeeping.

  Unify the fold monitor. _apply_steer (watch one governing tag leave a value) and _apply_steer_compound (sequential iteration over a goal list)
  are the same function parameterized by a done(state) predicate. Collapse them, and Phase 3's three items — path-sequence divergence,
  must-stay violation, deadline race — become richer monitors plugged into the same point rather than three new code paths. The deadline-race
  item even says it: "the crossing arithmetic is built; this adds the deadline as a competing entry." A monitor abstraction is where that entry
  goes.

  Mechanical collapses worth doing now

  1. A _WalkContext object. Nearly every function threads (pdg, program, known, ext_inputs, edge_ext, nd_domains, explore_context, nogoods) —
  eight parameters of per-walk-immutable state, and the missing-nogoods bug above is the predictable failure mode of threading by hand. Bundle
  them; keep the genuinely per-call values (work, goal, budget, depth, visited) explicit.
  2. Build _JumpContext once per walk. _build_jump_context does a full _collect_acc_sources + _scan_rung_reads (whole-program SP-tree walk) and
  is called twice per _walk_to_goal level, once per recovery iteration, and once per independent walk — at depth 6 with recovery that's a lot of
  redundant whole-program scans. Everything in it except normal_dt/profile_fb_names is static per program, and those are stable per walk
  (harness propagates through forks). Same for memoizing _governing's _probe_steps results per tag — the probe forks once per alphabet entry and
  runs at every recursion level.
  3. One solve loop, pluggable goal sources. The four loop variants differ mainly in where goals come from: static SP-tree
  (_unsatisfied_conditions), projected oracle (_recheck_prereqs), latch-break, compound-target decomposition. A single _solve(goals, ctx) with
  the uniform strategy order — satisfied-check → independence gate + merge-holds → corridor explore → sub-goal recursion → nogood + retry —
  would mean the third _explore exit and backjump each land in one place instead of four. Right now _walk_to_goal alone has two near-identical
  tails (explore-succeeded vs. prereqs-then-re-explore), both ending in _check_residuals, which itself delegates back to _recover_via_oracle.

  My strongest sequencing recommendation: do the consolidation before backjump and the third _explore exit. Backjump multiplies control-flow
  complexity at every site it touches; landing it into four loops then consolidating means paying for it twice. The plan's own guiding question
  ("does it extend the engine's reach or add a parallel path?") has been answered "parallel path" four times in a row for _try_independent_walks
  insertion — that's the tell.

  On the plan document itself

  It's genuinely strong — the theory statement (PSPACE-general gadget mazes, tractable subclass by standards enforcement), the three-layer
  oracle table, and the "Findings" section that prevents re-derivation are all things most plans lack. Three additions I'd make:

  - A global resource guard. Budget is counted in actions, checked after the fact (len(all_steps) > budget post-extend, at six different sites).
  Nothing bounds total forks or wall-clock across the recursion × recovery × probe product. The caps compose multiplicatively (_MAX_NODES ×
  alphabet × _MAX_PREREQ_DEPTH × _MAX_RECHECK_ITERS); a shared decrementing fork-budget in the context object is cheap insurance and gives Phase
  5's NotFound an honest "budget exhausted" trigger.
  - Note the seen-key growth interaction. The nogood store is shared per plan_walk and add-only; once blocking names accumulate, every
  subsequent _explore partitions states by those names — including for unrelated goals — so seen can fragment and burn _MAX_NODES on
  distinctions that don't matter for the current goal. Probably fine under current caps, but worth a line in Open Items: project per-goal (only
  nogoods whose (from,to) involves the current governing tag) if it bites.
  - Phase 5's Diagnosis should be specified as a consumer of the plan tree + holds, not a separate mechanism — "best partial plan, first failing
  edge, accumulated nogoods" is exactly a partial tree with annotations. Spec'ing the return type now would let the representation work above
  serve double duty.

  The honest open item I'd add to the poke list: the unlink verify mismatch (bug 2 above) means the fault-scenario validation row in your table
  is currently weaker than it reads.