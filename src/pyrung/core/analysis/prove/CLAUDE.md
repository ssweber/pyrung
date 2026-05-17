# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this subsystem does

`prove/` is an exhaustive state-space verifier for pyrung programs. It runs BFS over all reachable states using the compiled replay kernel as the execution oracle. Two entry points: `prove(logic, condition)` checks a safety property, `reachable_states(logic)` computes the full reachable set for lock files. Both use `depth_budget` as an abstract BFS work budget; hidden-event acceleration can cover more concrete scans than that number.

The verifier strives to be sound — no false negatives. It may over-approximate domains (include unreachable values), which can only produce false positives (Intractable, never a missed violation). The fuzzer and soundness matrix continuously validate this property.

## Build and test

```
make test          # always use this, never uv run pytest
make lint          # codespell + ruff + ty
```

## Optimization glossary

- **Elision** — Removes scan-local tags from tracked state. Risk: misclassifying a cross-scan tag as scan-local.
- **Threshold absorption** — Replaces concrete accumulator values with crossed/uncrossed boolean vectors. Risk: missing boundaries, or assuming monotonicity when conditional resets exist.
- **Fast-forward / tesseracting (hidden events)** — Skips timer/counter scans, branching at crossing events. Like the Wrinkle in Time tesseract: folds over intermediate ticks to land directly at the crossing. Risk: missing input combinations that change mid-accumulation.
- **Backward propagation** — Traces comparisons backward through arithmetic/copies to seed input domains. Risk: can't invert all operations; may not follow chains far enough.
- **Edge compression** — Removes dead rise/fall prev values from state keys. Risk: merging states that should be distinct.
- **Edge inputs** — Enumerates rising/falling edge combinations via single-flip expansion. Risk: missing simultaneous edge combinations.
- **Exclusive inputs** — Prunes mutually-exclusive boolean input combinations. Risk: over-pruning across scans instead of within a scan.
- **Edge demotion** — Removes scan-local edge-source tags (`rise()`/`fall()` targets) from the state key. Their B_prev values are forwarded on BFS transitions instead of tracked as dimensions. Qualifying: OTE-written, unconditional-copy-written, or combinational tags. Non-qualifying: latches, accumulators, self-referencing writes. Risk: parent_map must key on `(state_key, b_prev)` to avoid trace-reconstruction cycles.
- **Pacing** — Semantic parameter (`paced=True` on `prove()`). Forces a stutter scan after any input flip. The pacing bit (`just_flipped`) is tracked in the BFS state key so stutter-reached and flip-reached states have different legal successors. Not an optimization — it restricts the state space to realistic input timing. Two-pass: paced first, aggressive second (only if paced proves).

## Module map

Each module has a docstring with implementation details. This map is for navigation.

- **`__init__.py`** — Public API (`prove`, `reachable_states`) and re-exports. Property compilation, batch partitioning, progress reporters.
- **`results.py`** — Result types: `Proven`, `Counterexample`, `Intractable`, `TraceStep`, `StateDiff`, `PENDING`. Journal framework: `Decision`, `TagEntry`, `Journal`.
- **`bfs.py`** — BFS exploration loop and trace/projection helpers.
- **`lockfile.py`** — Lock-file I/O, choice/band label resolution, JSON serialization.
- **`passes.py`** — Pre-BFS pass pipeline. 12 ordered passes building the `_ExploreContext`. `_JournalBuilder` accumulates per-tag Decision records; `freeze()` produces the immutable context for BFS.
- **`classify.py`** — Dimension classification (stateful / nondeterministic / combinational) and domain inference. See docstring for the 8-level domain inference stack.
- **`absorb.py`** — Accumulator absorption and threshold abstraction. Three paths: redundant Acc, threshold vector, comparison-only. All gated by the exclusivity check. See northstar docstring.
- **`events.py`** — Hidden-event scheduling. Settles pending timers/counters without stepping through every tick. See docstring for the settle cascade.
- **`kernel.py`** — Kernel integration. Snapshot/restore, state key extraction, edge compression, live input caching. See docstring for state key composition.
- **`expr.py`** — Expression tree helpers. Partial evaluation, tag reference collection, atom indexing, edge-bearing input partition.
- **`inputs.py`** — Input-group detection and successor enumeration. Cross-product of three dimensions: edge single-flips, encoder-group canonicals, free-input combos.
- **`elision/`** — Traced influence-graph state-key elision.
  - **`__init__.py`** — Pipeline entry point (delegates to trace.py).
  - **`trace.py`** — Traced influence graph: instruments program execution to build a dependency graph, then backward-cone analysis from observers determines elidable tags.

## Pipeline overview

```
Program
  → _run_pre_bfs_pipeline (passes.py)
    → classify → elide → compile → absorb → build events → freeze
  → _ExploreContext (frozen, immutable)
    → _bfs_explore (bfs.py)
  → Proven | Counterexample | Intractable
```

See `passes.py` for the full 12-pass sequence with data flow.

## Invariants to preserve

- **Soundness goal**: every reachable state must be visited. Over-approximation (extra states) is safe; under-approximation is not. If you change domain inference or absorption, the new rule must be at least as conservative as the old one.
- **Threshold absorption gate**: the exclusivity check (`_has_forbidden_data_read`) is the soundness gate. Stability checks are pragmatic implementation constraints, not soundness requirements.
- **Settle-pending termination**: bounded by event count + 1. Accumulators must not decrement during settling.
- **Edge compression correctness**: dead edge prevs use a sentinel `_EDGE_DEAD`. An edge is dead only when partial eval proves all containing expressions constant — false negatives (marking a live edge dead) would lose states.
- **State key completeness**: every tag whose cross-scan value affects reachability must appear in the state key. Missing a dimension silently merges distinguishable states.

## Testing

Tests are in `tests/core/analysis/`, split thematically across `test_prove_*.py` files:

- `test_prove_dimension_classification.py` — dimension classification
- `test_prove_value_domain_extraction.py` — value domain extraction
- `test_prove_dont_care_pruning.py` — don't-care pruning
- `test_prove_bfs_api.py` — BFS exploration and public API (`prove`, `reachable_states`)
- `test_prove_kernel_oracle.py` — kernel oracle soundness
- `test_prove_lock_file.py` — lock file I/O and diffing
- `test_prove_partitioning.py` — batch and reachable-state partitioning/slicing
- `test_prove_timer_abstraction.py` — timer/counter absorption and threshold events
- `test_prove_intractable_hints.py` — intractable tags and blocker hints
- `test_prove_kernel_domains.py` — kernel domain discovery
- `test_prove_settlement.py` — pending settlement chains
- `test_prove_coverage_misc.py` — edge conditions, scope, annotated functions, lock change
- `test_prove_inputblock_tagmap_input_inference.py` — InputBlock/TagMap input inference
- `test_prove_free_input_elision.py` — free input elision
- `test_prove_absorption_gap_regression_tests.py` — absorption gap regressions
- `test_prove_simultaneous_edge_coverage_tests.py` — simultaneous edge coverage and adversarial elision
- `test_prove_fuzz_reproducer_regressions.py` — fuzz reproducer regressions
- `test_prove_paced_exploration.py` — paced exploration and journal integration
- `test_prove_matrix.py` — soundness coverage matrix
- `test_prove_passes.py` — pre-BFS pass pipeline unit tests
- `test_prove_input_groups.py` — input group detection
- `test_prove_edge_demotion.py` — edge-source tag demotion (classification, correctness, soundness agreement)
- `test_packml_diagnosis.py` — PackML-specific regression tests

**Counterexample replay rule**: every `Counterexample` assertion in the soundness matrix must be followed by `_assert_trace_replays(logic, result, "TagName")`. This is the two-oracle check — prove() found a violation, concrete PLC confirms it.

## Performance

Traced elision and BFS are the primary cost centers. Run `make bench` on the PackML example.

## Formal foundations

See `docs/internal/prove-formal-foundations.md` for the theoretical basis of each optimization.

## External integration

- **CLI**: `pyrung lock` / `pyrung check` in `src/pyrung/cli.py`
- **Public API**: re-exported from `src/pyrung/core/analysis/__init__.py`
- **DAP miner**: `src/pyrung/dap/miner.py` uses `read_lock()` to filter candidates
- **Compiled kernel**: same codegen path as CircuitPython output (`pyrung.circuitpy.codegen.compile_kernel()`)
