  Explore-less how()

  Goal

  how(plc, target_condition) — find a path from the current snapshot to a state satisfying the target, without requiring
  explore() or any precomputed state graph.

  Architecture: two phases

  Phase 0 — Snapshot-seeded BFS (DONE — commit ab01682)

  PLC.how() runs a snapshot-seeded BFS directly from the current PLC state. No explore() call needed.

  What was built:

  1. Snapshot-aware seeding. _discover_domains / _seed_stateful_via_trace / _seed_nd_via_bisection in seeding.py accept
  initial_state=. The kernel starts from the snapshot for trace-observation and bisection, and snapshot values are
  included in discovered domains. This makes many programs tractable that were Intractable from defaults — the snapshot
  resolves cross-tag comparisons by providing concrete boundary values.

  2. initial_state threading. _PassContext (passes.py), _build_explore_context (__init__.py), and _bfs_explore (bfs.py)
  all accept initial_state=. The BFS kernel is seeded from the snapshot before exploration starts.

  3. Inverted-predicate trick. Instead of converting _bfs_explore to a generator, PLC.how() passes
  predicates=[lambda s: not target_pred(s)]. A Counterexample trace = a path where the target IS satisfied.
  _bfs_explore with a single predicate already returns on first match, so no generator conversion was needed.

  4. Replay verification. Every returned path is replayed through a fresh kernel from the snapshot to verify the target
  holds at the end. This is the soundness contract: returned paths must be valid. Missing a valid path is acceptable;
  returning an invalid one is not.

  5. Dispatch in PLC.how(). If an explored transition graph exists, delegates to _how_via_graph (full feature support:
  avoid=, minimize="changes"). Otherwise uses _how_via_bfs (snapshot-seeded, heuristic_domain_seeding=True).

  6. Path format. Counterexample.trace (list[TraceStep]) is converted to Path with ReachabilityStep entries, matching
  the existing output format.

  What was NOT done in Phase 0:

  - Generator conversion of _bfs_explore — not needed for Phase 0 since single-predicate BFS returns on first match.
    Needed for Phase 1's waypoint backtracking (pull next() to resume and get alternate solutions).
  - avoid= parameter for BFS path — requires state filtering in _bfs_explore, deferred.
  - minimize="changes" for BFS path — requires Dijkstra variant, deferred.

  Phase 1 — Waypoint planner (DONE)

  A decomposition layer on top of the Phase 0 BFS. Breaks one potentially-large BFS into several small ones.

  What was built:

  1. Generator conversion of _bfs_explore. _bfs_explore_gen in bfs.py is the generator form — yields each time all
  predicates are resolved, resets results, and continues BFS. _bfs_explore wraps it with next() for backward
  compatibility. Existing callers (always/never/reachable_states/explore) unchanged. how() can use _bfs_explore_gen
  directly for backtracking via next().

  2. Waypoint discovery. _discover_waypoints in waypoints.py does a backward walk from the target expression through
  writers_of + SP trees + back_propagate_value. Identifies stateful tags (non-INPUT, has writers) that need to change
  value. External inputs are excluded. Uses _extract_required_values to invert Expr trees into (tag, required_value)
  pairs — handles xic/xio/eq atoms, And (union of pairs), Or (greedy cheapest branch). Rise/fall/truthy return None
  (fall back to undecomposed BFS).

  3. Waypoint ordering. _order_waypoints does Kahn's algorithm topological sort by condition-reads dependency. Waypoint
  B depends on A if any rung writing B reads A in its condition. Cycles → fall back to undecomposed BFS.

  4. Mini-BFS orchestration. _run_waypoint_plan executes a scoped mini-BFS per waypoint (cone-restricted via
  pdg.upstream_slice, budget 10k states, depth_budget = max_steps / (n_waypoints + 1)). Each mini-BFS uses the
  inverted-predicate trick. Backtracking: when waypoint i+1 fails, resume waypoint i's generator via next(). Up to 3
  retries per waypoint.

  5. Integration in PLC._how_via_bfs. Tries waypoint plan first when expr is available (structured conditions). On
  success, replay-verifies the combined trace. On failure (None return from any step), falls back to undecomposed BFS.
  _replay_trace extracted as a shared static method.

  6. Tests in test_prove_waypoints.py: unit tests for _extract_required_values (atoms, And, Or, rise/fall, Const),
  _discover_waypoints (single latch, two-step, already satisfied, external input filtering), _order_waypoints
  (dependency ordering, single waypoint), and integration tests via PLC.how() (simple latch, two-step, three-step,
  replay validation, already-satisfied zero-step, multiple AND conditions, callable fallback, from-stepped-state).

  Phase 2 — DAP / live integration (DONE)

  Wire how() through the DAP console and live session without requiring explore().

  What was built:

  1. Removed stale explore() guards from console.py (_cmd_how) and handlers/causal.py that blocked how() when no
  transition graph existed. runner.how() already dispatches correctly.

  2. to_conditions() in dap/expressions.py converts the DAP expression AST (Compare/Not/And/Or over tag names) into
  pyrung DSL condition objects (Tag references with __eq__/__lt__/etc). This gives runner.how() structured expressions
  for auto-scoping and waypoint decomposition, instead of opaque callables that lose all structure.

  3. Pre-compiled kernel in _how_via_bfs. The heuristic domain seeding pass needs a compiled kernel for trace-based
  seeding (stateful tags) and bisection seeding (ND inputs). Previously, validate_declared_bounds would compile the
  kernel only if there were tags with declared bounds — programs without bounds got compiled=None, and the seeding
  fell back to type-boundary-only domains. Now _how_via_bfs compiles upfront and passes through to
  _build_explore_context, _try_waypoint_plan, and _run_waypoint_plan.

  4. Test: test_how_with_int_step_counter in test_prove_waypoints.py exercises the compiled-kernel seeding path with
  a copy-based Int step counter.

  Known issues found during testing:

  - Threshold absorption is unsound for oneshot calc accumulators: calc(Step + 1, Step, oneshot=True) with Step == N
    threshold — absorption falsely concludes the threshold can never be crossed. never(Done) returns Proven when Done
    is reachable via input cycling. Reproducer: tests/fuzz/reproducers/soundness_20260602_oneshot_calc_absorption.py

  - ND domain seeding for Real inputs with cross-correlated comparisons: bisection gives single-value domains (e.g.
    systemLevel_opt2011: (0.0,)) when the behavioral fingerprint doesn't differentiate across probe values. The
    snapshot value should seed the ND domain center, and comparison partners (via cross-tag comparisons like
    pv_LevelHt >= calc_levelSvUpperWBand) should propagate domain constraints between correlated inputs. Without
    this, how() on the fill system can't find a path because the BFS never explores level values that trigger
    the state transition.

  Still deferred:

  - avoid= parameter for BFS path — requires state filtering in _bfs_explore.
  - minimize="changes" for BFS path — requires Dijkstra variant.
  - Smarter ND seeding: use snapshot values as seed centers for Real/Int ND inputs (not just stateful tags).
    Propagate comparison-derived constraints across cross-tag pairs so correlated inputs get compatible domains.
