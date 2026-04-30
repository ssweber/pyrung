# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this subsystem does

`prove/` is an exhaustive state-space verifier for pyrung programs. It runs BFS over all reachable states using the compiled replay kernel as the execution oracle. Two entry points: `prove(logic, condition)` checks a safety property, `reachable_states(logic)` computes the full reachable set for lock files.

The verifier is sound — no false negatives. It may over-approximate domains (include unreachable values), which can only produce false positives (Intractable, never a missed violation).

## Build and test

```
make test          # always use this, never uv run pytest
make lint          # codespell + ruff + ty
```

Test files:
- `tests/core/analysis/test_prove.py` — integration tests (28 test classes, ~3200 lines)
- `tests/core/analysis/test_prove_passes.py` — pre-BFS pass pipeline unit tests

## Module map

```
__init__.py  — Public API (prove, reachable_states, write_lock, check_lock, diff_states)
               BFS loop (_bfs_explore), property compilation, batch partitioning,
               cluster projection with Cartesian product
passes.py    — Pre-BFS pass pipeline (_run_pre_bfs_pipeline). 10 ordered passes that
               build the _ExploreContext. Mutable _PassContext accumulates intermediate
               state; freeze() produces the immutable _ExploreContext for BFS.
classify.py  — Dimension classification and domain inference. Partitions tags into
               stateful / nondeterministic / combinational. Extracts finite value
               domains from expression trees, literal writes, structural propagation.
absorb.py    — Accumulator absorption and threshold abstraction. Removes timer/counter
               Acc tags from the state space by collapsing to Done-bit three-valued
               states or threshold-crossing vectors. See the northstar docstring at the
               top — it explains the exclusivity principle.
events.py    — Hidden-event scheduling. Settles pending timers/counters without
               stepping through every tick. Fast-forwards to the next crossing via
               linear acceleration. Materializes abstract threshold branches.
kernel.py    — Kernel integration. Snapshot/restore, state key extraction, edge
               compression, live input caching, inline step compilation.
expr.py      — Expression tree helpers. Partial evaluation, tag reference collection,
               atom indexing, live-input analysis.
slicer.py    — Whole-rung program slicing. Builds a reduced program containing only
               rungs in the upstream cone of seed tags.
```

## Data flow through the pipeline

```
Program
  → _run_pre_bfs_pipeline (passes.py)
    → build_graph: ProgramGraph + all condition/write-site expressions
    → classify_dimensions: stateful/ND/combinational + value domains
    → pilot_sweep: fallback domain discovery via kernel execution (if classify returned Intractable)
    → compile_kernel: CompiledKernel + stateful/edge tag name tuples
    → collect_done_acc_pairs: Done→Acc mapping from timer/counter instructions
    → find_redundant_absorptions: Acc tags absorbed into 3-valued Done bits
    → find_threshold_absorptions: progress accumulators → threshold-crossing vectors
    → build_event_specs: DoneEventSpec + ThresholdEventSpec for hidden-event scheduling
    → collect_edge_exprs: rise/fall expression map for edge compression
    → discover_memory_keys: kernel memory keys via pilot scan
  → _ExploreContext (frozen, immutable)
    → _bfs_explore (__init__.py)
      → per-state: enumerate live inputs, step kernel, extract state key
      → hidden events: settle pending timers, jump to threshold crossings
      → property check: evaluate predicates, build counterexample traces
  → Proven | Counterexample | Intractable
```

## Key abstractions

### State key (`_extract_state_key` in kernel.py)

The BFS visited set uses a tuple key: `(stateful_tag_values..., threshold_vectors..., edge_prevs..., memory_keys...)`. This is the identity of a state — two kernel snapshots with the same key are treated as equivalent.

Done bits use three-valued abstraction: `False` / `PENDING` / `True` (derived from Done + Acc via `_done_acc_state`). Threshold vectors replace concrete accumulator values with a tuple of crossed/uncrossed booleans per comparison threshold.

Edge compression: rise/fall prev values are only included when "live" — when partial evaluation of their containing expression doesn't resolve to a constant under the current stateful configuration.

### Dimension classification (`classify.py`)

Tags partition into three roles:
- **Stateful**: latch/reset, timer/counter, copy, calc — tracked in visited set
- **Nondeterministic**: external inputs — enumerated at each BFS state
- **Combinational**: OTE-only writes with no cross-scan readers — ignored

Domain inference stack (from most to least specific):
1. Bool → `{False, True}`
2. `choices=` metadata → explicit finite set
3. `min=`/`max=` metadata → integer range (capped at 1000)
4. Literal-write mining (`_collect_literal_write_domains`) → values from copy(literal, tag)
5. Structural propagation (`_collect_structural_domains`) → fixed-point over write graph
6. Expression partition (`_extract_value_domain`) → comparison literals ± 1
7. eq/ne enum closure → `{literals..., OTHER}` for tags only tested for equality
8. Pilot sweep (`_pilot_sweep_domains`) → forward simulation fallback

No domain → `Intractable` with hints.

### Threshold absorption (`absorb.py`)

The core principle: a threshold's concrete value is irrelevant to reachability if it's only used in threshold comparisons (the "exclusivity" check). Whether a timer preset is 100 or 4000, the same states are reachable — only WHEN crossings occur changes, and prove doesn't model time.

Three absorption paths:
1. **Redundant Acc absorption** — Acc only compared against Done-triggering boundary. Acc + preset tag both removed; synthetic preset=1. Gate: exclusivity only.
2. **Threshold vector absorption** — progress accumulator with upward-crossing comparisons. Concrete value replaced by crossed/uncrossed boolean vector. Gate: exclusivity + owner-only writes.
3. **Comparison-only absorption** — written tags observed only through comparisons. Concrete value replaced by comparison outcome vector. Gate: domain > 16 values, exclusivity, not projected.

### Hidden-event scheduling (`events.py`)

Timers/counters accumulate over many scans but the BFS would revisit the same PENDING state repeatedly. The event scheduler accelerates this:

1. `_scans_until_done_event` / `_scans_until_threshold_event` — compute scans to next crossing from the per-scan delta
2. `_advance_hidden_progress` — fast-forward accumulator by skipped scans
3. `_settle_pending` — cascade: resolve nearest event, re-check, repeat (bounded by event count)
4. `_maybe_jump_hidden_event` — when BFS revisits a known PENDING state, jump directly to the crossed successor

Abstract thresholds (dynamic presets): `_materialize_abstract_threshold_outcome` creates a representative crossed state without knowing the concrete preset value.

### Optimizations active during BFS (`_BFSConfig`)

- **live_input_pruning** — skip inputs masked by current state (partial eval)
- **edge_compression** — collapse dead edge prevs to sentinel
- **hidden_event_jumping** — jump from revisited pending plateaus
- **pending_settlement** — settle pending timers before evaluating failing properties

All four are on by default. Each has its own cache keyed by stateful prefix + threshold vector (caches are on `_EdgeCompressor` and `_LiveInputCache`).

## Formal foundations

See `scratchpad/prove-formal-foundations.md` for the citation map. Key results:

- **Exclusivity principle** (absorb.py): data independence (Wolper 1986) + time-abstracting bisimulation (Tripakis & Yovine 2001)
- **Event acceleration**: flat acceleration for linear counter automata (Leroux & Sutre 2005)
- **Done-bit three-valued abstraction**: zone abstraction / DBMs (Dill 1989; Mine 2001)
- **Domain partition from comparisons**: Cartesian predicate abstraction (Ball, Podelski, Rajamani 2001)
- **Structural domain propagation**: abstract interpretation fixed-point (Cousot & Cousot 1977)
- **Scope + absorption ordering**: cone-of-influence reduction commutes with abstraction for safety properties (Clarke, Grumberg, Long 1992)

## Performance profile

The dominant costs on a real program (see `scratchpad/prove-lock-profile-hotspots.md`):

1. Block sync in compiled kernel wrapper (~66% pre-optimization, addressed by inline step compilation)
2. State key + threshold vector construction (~11%)
3. Hidden-event jumping (~12%)
4. Snapshot/restore (~7%)
5. Kernel step execution (~5%)

The pre-BFS pipeline itself costs ~45s on a large program, dominated by threshold absorption discovery and dimension classification.

## Invariants to preserve

- **Soundness**: every reachable state must be visited. Over-approximation (extra states) is safe; under-approximation is not. If you change domain inference or absorption, the new rule must be at least as conservative as the old one.
- **Threshold absorption gate**: the exclusivity check (`_has_forbidden_data_read`) is the soundness gate. Stability checks are pragmatic implementation constraints, not soundness requirements.
- **Settle-pending termination**: bounded by event count + 1. Accumulators must not decrement during settling.
- **Edge compression correctness**: dead edge prevs use a sentinel `_EDGE_DEAD`. An edge is dead only when partial eval proves all containing expressions constant — false negatives (marking a live edge dead) would lose states.
- **State key completeness**: every tag whose cross-scan value affects reachability must appear in the state key. Missing a dimension silently merges distinguishable states.

## External integration

- **CLI**: `pyrung lock` / `pyrung check` in `src/pyrung/cli.py` — calls `reachable_states()`, `write_lock()`, `check_lock()`
- **Public API**: re-exported from `src/pyrung/core/analysis/__init__.py` — `prove`, `reachable_states`, `diff_states`, `Proven`, `Counterexample`, `Intractable`, `StateDiff`, `TraceStep`
- **DAP miner**: `src/pyrung/dap/miner.py` uses `read_lock()` to filter candidates
- **Examples**: `examples/fault_coverage.py` demonstrates two-pass fault coverage (structural via prove + timing via force tests)
- **Compiled kernel**: `prove` uses `pyrung.circuitpy.codegen.compile_kernel()` — the same codegen path as CircuitPython output. The compiled kernel is the execution oracle for BFS steps.
