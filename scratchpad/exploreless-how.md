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

  What was NOT done in Phase 1:

  Prerequisites from Phase 0:

  - Generator conversion of _bfs_explore. Make it a generator that yields each time a predicate match is found. The
    three early-return paths (lines ~631, ~855, ~858 in bfs.py) become yield instead of return. Existing callers
    (always/never in __init__.py) wrap with list() or consume until all predicates are resolved. how() pulls one result
    with next() and stops. This enables backtracking: the mini-BFS generator yields the first path, pauses, and resumes
    if downstream waypoints fail.

  Step 1 — Backward trace to find waypoints. From the target condition, walk backward through writers_of + SP trees
  (generalized why(), inverted direction). Stateful tags with enumerable domains (bools, choice enums) that need to
  change value become waypoints. Copy/calc chains resolve via back_propagate_value against the snapshot — propagate the
  required value backward through write expressions. Combinational tags trace through to inputs. Most conditions resolve
  instantly from the snapshot.

  Step 2 — Order waypoints. Dependency edges give a partial order. Topological sort.

  Step 3 — Mini-BFS per waypoint. Walk the sequence forward. At each waypoint, call the Phase 0 generator: seed from
  current state, scope to the waypoint's influence cone, bounded budget. Each solution becomes the starting state for
  the next waypoint.

  Yielding BFS enables backtracking. The mini-BFS is a generator — it yields the first path that hits the waypoint
  condition, then pauses. If the downstream waypoint's mini-BFS fails (the state we landed in doesn't allow progress),
  pull next() to resume and get the next solution. The BFS queue and visited set live in the generator frame — zero cost
  to resume.

  def scoped_bfs(state, target, cone, budget):
      queue = deque([(state, [])])
      visited = set()
      while queue and len(visited) < budget:
          current, path = queue.popleft()
          if target(current):
              yield path          # pause; resume if caller needs another
          # ... expand successors within cone

  # Orchestrator
  state = snapshot
  for wp in waypoints:
      for path in scoped_bfs(state, wp.condition, wp.cone, budget=N):
          state = path.final_state
          result.extend(path.steps)
          break
      else:
          return undecomposed_fallback(snapshot, target)  # drop decomposition

  Fallback. If any mini-BFS exhausts its budget, drop the decomposition and run a single undecomposed BFS from the
  original snapshot to the target over the full cone. Zero waypoints = this case automatically.

  Existing infrastructure to reuse

  - pdg.writers_of — DTG edges (which rungs write each tag)
  - rung.sp_tree() — edge conditions
  - attribute() / evaluate_sp() — contact attribution (firing or blocking)
  - _collect_sp_leaves() — input tags per rung condition
  - pdg.upstream_slice() — influence cone
  - back_propagate_value() — value propagation through copy/calc chains
  - classify.py — dimension classification and domain inference (subset: choices, literal writes, comparison-derived
  domains)
  - kernel.py — snapshot/restore/step, state-key extraction
  - independence.py — cone traversal and input factoring for scoping mini-BFS
  - events.py — timer/counter fast-forward for timer-gated waypoints
  - expr.py — partial evaluation, tag reference collection

  Soundness contract

  Inverted from prove() (always/never). For prove(): every reachable state must be visited (over-approximation safe,
  under-approximation not). For how(): the returned path must be valid — kernel-replay the steps and confirm the target
  holds (under-approximation safe, invalid paths not). Missing a valid path is acceptable; returning an invalid one is
  not. Every path is verified by kernel replay before returning.

  Output format

  Same as existing how():
  Path (2 step(s), 3 input change(s)):
    Step 1: CmdClear=True, CmdChgRequest=True  (2 scan(s))
    Step 2: CmdReset=True, CmdChgRequest=True  (2 scan(s))

  Testing

  - Phase 0 (done): Snapshot-seeded BFS produces valid paths (kernel replay). Test on programs that are Intractable
  from defaults but tractable from snapshot. Target already satisfied (zero-step). Target unreachable (graceful failure).
  - Phase 1: PackML benchmark (ABORTED->EXECUTE). Programs with timers, bitmask validation, indirect addressing.
  Waypoint decomposition produces shorter search times than undecomposed BFS. Backtracking recovers when first waypoint
  solution leads to dead end.

  Relevant files

  - src/pyrung/core/analysis/prove/bfs.py — _bfs_explore() is the BFS loop (~line 104). Main expansion loop tracks
  visited, queue, calls edge_collector when present. Predicate-match early returns at lines ~635, ~859, ~862 (shifted
  by Phase 0 edits). Now accepts initial_state= parameter.
  - src/pyrung/core/analysis/prove/__init__.py — always(), never(), reachable_states(), explore() all call _bfs_explore.
  _build_explore_context() accepts initial_state=. These are the callers that need to adapt to the generator change.
  - src/pyrung/core/analysis/prove/classify.py — dimension classification and domain inference.
  - src/pyrung/core/analysis/prove/passes.py — pre-BFS pass pipeline that builds _ExploreContext. _PassContext has
  initial_state field.
  - src/pyrung/core/analysis/prove/seeding.py — heuristic domain seeding. Now accepts initial_state= throughout.
  - src/pyrung/core/runner.py — PLC.how() dispatches to _how_via_graph (graph exists) or _how_via_bfs (snapshot BFS).
  - src/pyrung/core/analysis/prove/CLAUDE.md — prover internals, optimization glossary, module map, invariants.
