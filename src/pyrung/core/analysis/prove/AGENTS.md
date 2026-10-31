# Prover Agent Guidance

This file provides guidance to coding agents working in this subsystem.

## What this subsystem does

`prove/` is an exhaustive state-space verifier for pyrung programs. It runs BFS over all reachable states using the compiled replay kernel as the execution oracle. Two uses of the BFS engine: `always(logic, condition)` checks a safety property (with `never()` as the dual), and `reachable_states(logic)` computes the full reachable set for lock files. Both use `depth_budget` as a plain BFS depth (the longest scan sequence explored). `plc.how(*conditions)` is NOT this subsystem — it is PILOT (`../pilot/`, with its own AGENTS.md), which consumes this pipeline's `_ExploreContext` as a static prior.

The verifier strives to be sound — no false negatives. It may over-approximate domains (include unreachable values), which can only produce false positives (Intractable, never a missed violation). The fuzzer and soundness matrix continuously validate this property.

Timers are black boxes (`blackbox.py`): the search kernel is compiled with `abstract_timers=True`, and each timed tick (on-delay, off-delay, time drum step, Out debounce/flasher/timed internals) is a search choice instead of `+ dt`. Every counterexample is replayed on the concrete PLC before it is reported; one that does not replay is an `Intractable` of kind `"timing"` (or `"property"` for a property's own `rise()`/`fall()`). Nothing in the search re-models timer, counter or drum timing, so a conformance semantics change touches the runtime, codegen and fold, and here only the kernel flag's `_bb_tick` call sites. A `prove/` edit needed beyond that is a design miss to raise.

## Build and test

```
make test          # always use this, never uv run pytest
make lint          # codespell + ruff + ty
```

## Optimization glossary

- **Elision** — Removes scan-local tags from tracked state. Risk: misclassifying a cross-scan tag as scan-local. The slice fast path trusts def-use chains, which leave out an instruction's read of its own accumulator (`exclusive_reads`); `self_read_tags` sends those to the full check, or a timer's accumulator would leave the key. The full check runs a scan on the concrete runtime at a fixed 10 ms tick, where a timer short of its preset never reaches Done; under black-box timers it can, so a slice whose closure reads an on/off-delay's Done or Acc bails (`_collect_hidden_memory_info(black_box_timers=True)`, as drums and the Out coils always do). How()'s prior keeps the exact model and its `PENDING` guards.
- **Black-box timers** — Each timed tick may hold or jump: to its limit (the preset), to a value a comparison of the accumulator splits at (`_TimerModel.reps`), or, for an accumulator read as a value (`_TimerModel.exact`, from `accumulator_reads`), to any value one unit at a time. `choice_runs` enumerates one scan's choices depth first (cap `_CHOICE_RUN_CAP` -> Intractable kind `"timers"`). A tick of a timer nothing reads never branches. `settled=True` skips a state whose scan left a tick below its limit (`_TimerOracle.open`). Risk: a value set taken as complete that is not (a compared tag's values must be every value it takes; an input's partition comes from its own literal comparisons, see `TestKnownHoles`); a reduction that drops a ticking accumulator from the key.
- **Counterexample replay** — `blackbox.confirm` replays each trace on a `PLC` at the search's `dt`: plain steps as recorded, a step that moved a tick as a fold-backed wait until the compared state changes (`_fire_step`, input splits, `_slide_wait`). Unconfirmed traces become `Intractable(kind="timing")` with the abstract trace and `diverged_at`. Risk: accepting a replay state the search would not judge (settled, property edges).
- **Count clamps** — A counter whose accumulator nothing but its own instruction reads, with a literal preset and one writer, is keyed no further than the preset (`count_clamps`). Counters otherwise count concretely, edge by edge. Risk: a read the walk in `accumulator_reads` misses.
- **Backward propagation** — Traces comparisons backward through arithmetic/copies to seed input domains. Risk: can't invert all operations; may not follow chains far enough.
- **Edge compression** — Removes dead rise/fall prev values from state keys. Risk: merging states that should be distinct.
- **Edge inputs** — Enumerates rising/falling edge combinations via single-flip expansion. Risk: missing simultaneous edge combinations.
- **Exclusive inputs** — Prunes mutually-exclusive boolean input combinations. Risk: over-pruning across scans instead of within a scan.
- **Edge demotion** — Removes scan-local edge-source tags (`rise()`/`fall()` targets) from the state key. Their B_prev values are forwarded on BFS transitions instead of tracked as dimensions. Qualifying: OTE-written, unconditional-copy-written, or combinational tags. Non-qualifying: latches, accumulators, self-referencing writes. Risk: parent_map must key on `(state_key, b_prev)` to avoid trace-reconstruction cycles.
- **Free-input factoring** — Partitions independent free inputs into groups via a static independence relation (disjoint influenced-rung cones, no write/read overlap). Each group is evaluated independently and composed via delta merge — `O(sum)` kernel evals instead of `O(product)`. Risk: misclassifying dependent inputs as independent (the cone traversal must be conservative).
- **Split_at** — User directive promoting a stateful coupling tag to nondeterministic. The promoted tag acts as a barrier in cone traversal (writes don't expand through its readers) and is excluded from the Union-Find partition, appearing instead as a `shared_input` varied with every group. Over-approximates: explores values the tag may never reach. Risk: counterexamples may exercise unreachable split-tag values (caveat attached).
- **Pacing** — Semantic parameter (`paced=True` on `always()`). Forces a stutter scan after any input flip. The pacing bit (`just_flipped`) is tracked in the BFS state key so stutter-reached and flip-reached states have different legal successors. Not an optimization — it restricts the state space to realistic input timing. Two-pass: paced first, aggressive second (only if paced proves).

## Module map

Each module has a docstring with implementation details. This map is for navigation.

- **`__init__.py`** — Public API (`always`, `never`, `reachable_states`) and re-exports. Property compilation, batch partitioning, progress reporters.
- **`results.py`** — Result types: `Proven`, `Counterexample`, `Intractable` (with `kind`: `"search"`, `"timing"`, `"timers"`, `"property"`; `trace`, `diverged_at`), `TraceStep` (with `timers`), `StateDiff`, `PENDING` (how() prior only). Journal framework: `Decision`, `TagEntry`, `Journal`.
- **`bfs.py`** — BFS exploration loop (with the tick-choice runs and factored free-input composition) and trace/projection helpers.
- **`blackbox.py`** — Black-box timers: timed sites, `accumulator_reads`, the timer model and oracle, count clamps, and concrete replay (`confirm`). See its docstring.
- **`lockfile.py`** — Lock-file I/O, choice/band label resolution, JSON serialization.
- **`passes.py`** — Pre-BFS pass pipeline building the `_ExploreContext`, `_OptConfig`/`_BFSConfig`. `_JournalBuilder` accumulates per-tag Decision records; `freeze()` produces the immutable context for BFS.
- **`classify.py`** — Dimension classification (stateful / nondeterministic / combinational) and domain inference. See docstring for the 7-level domain inference stack. Two modes: the search's (`abstract_timers=True`, no absorption) and how()'s domain prior (absorbing, three-valued Done).
- **`absorb.py`** — Done/Acc pairing, the data-read exclusivity check, and the absorptions of how()'s domain prior only (PILOT's world key: `_done_acc_state`, threshold vectors, `_StateKeyDoneSpec`). The search never absorbs.
- **`kernel.py`** — Kernel integration. Snapshot/restore, state key extraction (with count clamps), edge compression, live input caching. See docstring for state key composition.
- **`expr.py`** — Expression tree helpers. Partial evaluation, tag reference collection, atom indexing, edge-bearing input partition.
- **`independence.py`** — Static independence relation (single-scan commutativity of input actions) and free-input factoring partition. Used by BFS for factored evaluation. Also provides `_find_bridge_tags` for Intractable hint generation.
- **`system_points.py`** — System-tag model for the kernel: power-on constants, `first_scan` as a stateful bit, scan-start resets of fault/command bits, and which scan-, clock- and time-derived tags BFS explores as free inputs. System tags only the property, scope or projection names come in through a `build_program_graph(observed_system=...)` variant (`passes._observed_system_tags`), which also counts the implicit writers of observed fault flags; callable properties read through `guarded_state`, which refuses unexplored system tags.
- **`inputs.py`** — Input-group detection and successor enumeration. Cross-product of three dimensions: edge single-flips, encoder-group canonicals, free-input combos.
- **`elision/`** — Slice-based state-key elision.
  - **`__init__.py`** — Pipeline entry point (delegates to slice.py).
  - **`slice.py`** — Sound-by-construction write-before-read enumeration: per candidate, enumerates entry-state combinations over the tag's slice and checks that every path writes before reading. A tag touched only inside one subroutine, and read by nothing outside the program, is checked per call of that subroutine instead (`_local_slice_check`), so its call sites' conditions stay out of the enumeration.

## Pipeline overview

```
Program
  → _run_pre_bfs_pipeline (passes.py)
    → classify → validate_declared_bounds → split_at → elide → compile (abstract timers) → timer model → freeze
  → _ExploreContext (frozen, immutable)
    → _bfs_explore (bfs.py)  [tick choices; factored free-input composition]
    → _confirm_results → blackbox.confirm  [concrete replay of every counterexample]
  → Proven | Counterexample | Intractable
```

See `passes.py` for the full pass sequence with data flow. When `domains_only=True` (the `how()` path), the search's passes (bounds validation, timer model, edge expressions, memory keys) are skipped; classification absorbs and `build_prior_key_specs` builds PILOT's world-key Done specs.

`_OptConfig.all_off()` is a sound baseline: every remaining toggle is a reduction (`_REDUCTION_OPTIMIZATIONS`) the soundness fuzzer checks against plain BFS. `sound_baseline()` is `all_off()` with the declared-bounds check kept; `_skip_optimizations=True` selects it.

## Invariants to preserve

- **Soundness goal**: every reachable state must be visited. Over-approximation (extra states) is safe; under-approximation is not. If you change domain inference or absorption, the new rule must be at least as conservative as the old one.
- **Black-box completeness**: every timed tick must offer every value the program can tell apart, and every Done sequence a real timer can produce must be a choice sequence. A timed instruction compiled for the search must call `_bb_tick`.
- **Replay rule**: no `Counterexample` leaves `_always_compiled` unreplayed (except a free-system-input trace with no branching timer, which keeps its caveat). Tests follow the same two-oracle rule.
- **Edge compression correctness**: dead edge prevs use a sentinel `_EDGE_DEAD`. An edge is dead only when partial eval proves all containing expressions constant — false negatives (marking a live edge dead) would lose states.
- **State key completeness**: every tag whose cross-scan value affects reachability must appear in the state key. Missing a dimension silently merges distinguishable states.

## Testing

Tests are in `tests/core/analysis/`, split thematically across `test_prove_*.py` files:

- `test_prove_dimension_classification.py` — dimension classification
- `test_prove_value_domain_extraction.py` — value domain extraction
- `test_prove_dont_care_pruning.py` — don't-care pruning
- `test_prove_bfs_api.py` — BFS exploration and public API (`always`, `never`, `reachable_states`)
- `test_prove_kernel_oracle.py` — kernel oracle soundness
- `test_prove_lock_file.py` — lock file I/O and diffing
- `test_prove_partitioning.py` — batch and reachable-state partitioning/slicing
- `test_prove_blackbox.py` — black-box timers: the model, replay, unconfirmed kinds, review regressions, known holes
- `test_prove_timer_abstraction.py` — timers and counters under black-box timers: coarse units, tag presets, accumulator thresholds, drums
- `test_prove_intractable_hints.py` — intractable tags and blocker hints
- `test_prove_kernel_domains.py` — kernel domain discovery
- `test_prove_settlement.py` — `settled=True` over chained timers
- `test_prove_coverage_misc.py` — edge conditions, scope, annotated functions, lock change
- `test_prove_inputblock_tagmap_input_inference.py` — InputBlock/TagMap input inference
- `test_prove_free_input_elision.py` — free input elision
- `test_prove_absorption_gap_regression_tests.py` — regressions from the deleted exact-timing layer, as black-box verdicts
- `test_prove_simultaneous_edge_coverage_tests.py` — simultaneous edge coverage and adversarial elision
- `test_prove_fuzz_reproducer_regressions.py` — fuzz reproducer regressions
- `test_prove_paced_exploration.py` — paced exploration and journal integration
- `test_prove_matrix.py` — soundness coverage matrix
- `test_prove_passes.py` — pre-BFS pass pipeline unit tests
- `test_prove_factoring.py` — free-input factoring partition, composition, and BFS integration
- `test_prove_decomposition.py` — split_at validation, always/never/reachable_states integration
- `test_prove_input_groups.py` — input group detection
- `test_prove_edge_demotion.py` — edge-source tag demotion (classification, correctness, soundness agreement)
- `test_prove_system_points.py` — system tags (first_scan, constants, free clocks/counters, scan-start resets)
- `test_packml_diagnosis.py` — PackML-specific regression tests

**Counterexample replay rule**: every `Counterexample` assertion in the soundness matrix must be followed by `_assert_trace_replays(logic, result, "TagName")`. This is the two-oracle check — always() found a violation, concrete PLC confirms it.

## Performance

BFS is the primary cost center. Run `make bench` on the PackML example (`pyrung lock examples.packml_bench`: 505 states in about 80 s, 2026-10-30).

## Formal foundations

See `docs/internal/prove-formal-foundations.md` for the theoretical basis of each optimization.

## External integration

- **CLI**: `pyrung lock` / `pyrung lock --check` in `src/pyrung/cli.py`
- **Public API**: re-exported from `src/pyrung/core/analysis/__init__.py`
- **DAP miner**: `src/pyrung/dap/miner.py` uses `read_lock()` to filter candidates
- **Compiled kernel**: same codegen path as CircuitPython output (`pyrung.circuitpy.codegen.compile_kernel()`)
