# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this subsystem does

`prove/` is an exhaustive state-space verifier for pyrung programs. It runs BFS over all reachable states using the compiled replay kernel as the execution oracle. Two entry points: `prove(logic, condition)` checks a safety property, `reachable_states(logic)` computes the full reachable set for lock files. Both use `depth_budget` as an abstract BFS work budget; hidden-event acceleration can cover more concrete scans than that number.

The verifier is sound — no false negatives. It may over-approximate domains (include unreachable values), which can only produce false positives (Intractable, never a missed violation).

## Build and test

```
make test          # always use this, never uv run pytest
make lint          # codespell + ruff + ty
```

## Optimization glossary

- **Elision** — Removes scan-local tags from tracked state. Risk: misclassifying a cross-scan tag as scan-local.
- **Threshold absorption** — Replaces concrete accumulator values with crossed/uncrossed boolean vectors. Risk: missing boundaries, or assuming monotonicity when conditional resets exist.
- **Fast-forward (hidden events)** — Skips timer/counter scans, branching at crossing events. Risk: missing input combinations that change mid-accumulation.
- **Backward propagation** — Traces comparisons backward through arithmetic/copies to seed input domains. Risk: can't invert all operations; may not follow chains far enough.
- **Edge compression** — Removes dead rise/fall prev values from state keys. Risk: merging states that should be distinct.
- **Edge inputs** — Enumerates rising/falling edge combinations via single-flip expansion. Risk: missing simultaneous edge combinations.
- **Exclusive inputs** — Prunes mutually-exclusive boolean input combinations. Risk: over-pruning across scans instead of within a scan.

## Module map

```
__init__.py  — Public API (prove, reachable_states) and re-exports from submodules.
               Property compilation, batch partitioning, progress reporters.
results.py   — Result types: Proven, Counterexample, Intractable, TraceStep, StateDiff, PENDING.
               Journal framework: Decision, TagEntry, Journal (see below).
bfs.py       — BFS exploration loop (_bfs_explore) and trace/projection helpers.
lockfile.py  — Lock-file I/O (write_lock, read_lock, check_lock, diff_states, program_hash),
               choice/band label resolution, JSON serialization.
passes.py    — Pre-BFS pass pipeline (_run_pre_bfs_pipeline). 12 ordered passes that
               build the _ExploreContext. Mutable _PassContext accumulates intermediate
               state; freeze() produces the immutable _ExploreContext for BFS.
               _PreBFSPass has requires/provides contracts; _validate_pass_dag checks
               ordering at startup. Diagnostic info messages route directly to the
               progress_info callback.
               _JournalBuilder accumulates per-tag Decision records during passes;
               freeze() builds the final Journal attached to _ExploreContext.
classify.py  — Dimension classification and domain inference. Partitions tags into
               stateful / nondeterministic / combinational. Extracts finite value
               domains from expression trees, literal writes, structural propagation.
               Backward propagation (_backward_propagate_comparison_boundaries)
               inverts write instructions to seed source domains from target
               comparisons. Supports +, -, *, unary, copy, fill, blockcopy.
               Non-invertible shapes (%, /, bitwise) trigger a fallback: widen to
               declared domain or flag as reverse soundness blocker → Intractable.
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
               atom indexing, live-input analysis. Edge-bearing input partition
               (_partition_edge_bearing_inputs) for free-input elision.
inputs.py    — Input-group detection and successor enumeration. _ExclusiveInputGroup
               identifies Bool input families (e.g. encoder-style one-hot sets) whose
               multi-hot combos are redundant. _iter_input_assignments builds the
               cross-product of three independent change dimensions (edge single-flips,
               encoder-group canonicals, free-input combos) to ensure every combination
               is explored.
elision/     — Two-phase state-key elision sub-pipeline.
  __init__.py  — Pipeline orchestration: _ElisionContext, _ElisionPass, _AbstractRule,
                 _run_elision_pipeline, _elide_scan_local_stateful_dims.
  abstract.py  — Abstract provenance analysis. _TagElisionCheck (abstract interpreter),
                 _ScanLocalStateElider (fixed-point driver), _pass_abstract,
                 _rule_provenance, _DEFAULT_ABSTRACT_RULES tuple.
  concrete.py  — Concrete kernel proofs. _ConcreteStateElider (enumeration-based proofs),
                 _collect_forced_true_coverage, _pass_concrete_batch.
```

## Data flow through the pipeline

```
Program
  → _run_pre_bfs_pipeline (passes.py)
    → build_graph: ProgramGraph + all condition/write-site expressions
    → classify_dimensions: stateful/ND/combinational + value domains
    → pilot_sweep: fallback domain discovery via kernel execution (if classify returned Intractable)
    → diagnose_unwritten_tags: surface never-written tags as user diagnostics
    → elide_scan_local_state: abstract + concrete proof that tags are WBR
    → compile_kernel: CompiledKernel + stateful/edge tag name tuples
    → collect_done_acc_pairs: Done→Acc mapping from timer/counter instructions
    → find_redundant_absorptions: Acc tags absorbed into 3-valued Done bits
    → find_threshold_absorptions: progress accumulators → threshold-crossing vectors
    → build_event_specs: DoneEventSpec + ThresholdEventSpec for hidden-event scheduling
    → collect_edge_exprs: rise/fall expression map for edge compression
    → discover_memory_keys: kernel memory keys via pilot scan
  → freeze() (passes.py)
    → partition ND inputs: edge-bearing (state key) vs free (enumerated only)
  → _ExploreContext (frozen, immutable)
    → _bfs_explore (bfs.py)
      → per-state: cross-product three input dimensions (edge single-flips, encoder-group canonicals, free-input combos), step kernel, extract state key
      → hidden events: settle pending timers, jump to threshold crossings
      → property check: evaluate predicates, build counterexample traces
  → Proven | Counterexample | Intractable
```

## Key abstractions

### State key (`_extract_state_key` in kernel.py)

The BFS visited set uses a tuple key: `(stateful_tag_values..., threshold_vectors..., nd_input_values..., edge_prevs..., memory_keys...)`. This is the identity of a state — two kernel snapshots with the same key are treated as equivalent.

Only **edge-bearing** ND inputs appear in the key (`nondeterministic_names`). Free inputs — those without rise()/fall() or implicit-edge usage (shift clock, drum jog/jump/events) — are excluded (`free_input_names`). Their current value doesn't constrain future behavior, so states differing only in free inputs are equivalent. Free inputs are still fully enumerated (Cartesian product) at each BFS state to explore all successor combinations.

Done bits use three-valued abstraction: `False` / `PENDING` / `True` (derived from Done + Acc via `_done_acc_state`). Threshold vectors replace concrete accumulator values with a tuple of crossed/uncrossed booleans per comparison threshold.

Edge compression: rise/fall prev values are only included when "live" — when partial evaluation of their containing expression doesn't resolve to a constant under the current stateful configuration.

### Dimension classification (`classify.py`)

Tags partition into three roles:
- **Stateful**: latch/reset, timer/counter, copy, calc — tracked in visited set
- **Nondeterministic**: external inputs — enumerated at each BFS state
- **Combinational**: OTE-only writes with no cross-scan readers — ignored

`run_function` and `run_enabled_function` outputs are classified as stateful writes, but the function body is opaque — the verifier cannot introspect user Python. Domain inference falls through to tag metadata (`choices=`, `min=/max=`). Unannotated outputs trigger `_detect_function_escape_hatches` in classify.py, which returns `Intractable`.

Domain inference stack (from most to least specific):
1. Bool → `{False, True}`
2. `choices=` metadata → explicit finite set
3. `min=`/`max=` metadata → integer range (capped at 1000)
4. Literal-write mining (`_collect_literal_write_domains`) → values from copy(literal, tag)
5. Structural propagation (`_collect_structural_domains`) → fixed-point over write graph
6. Expression partition (`_extract_value_domain`) → comparison literals ± 1 + tag default
7. eq/ne enum closure → `{literals..., OTHER}` for tags only tested for equality
8. Pilot sweep (`_pilot_sweep_domains`) → forward simulation fallback

No domain → `Intractable` with hints.

### Threshold absorption (`absorb.py`)

The core principle: a threshold's concrete value is irrelevant to reachability if it's only used in threshold comparisons (the "exclusivity" check). Whether a timer preset is 100 or 4000, the same states are reachable — only WHEN crossings occur changes, and prove doesn't model time.

Three absorption paths:
1. **Redundant Acc absorption** — Acc only compared against Done-triggering boundary. Acc + preset tag both removed; synthetic preset=1. Gate: exclusivity only.
2. **Threshold vector absorption** — progress accumulator with threshold comparisons (both upward-crossing like `Acc >= T` and below-threshold like `Acc < T`; below-threshold normalizes to the same crossing boundary). Concrete value replaced by crossed/uncrossed boolean vector. Gate: exclusivity + owner-only writes.
3. **Comparison-only absorption** — written tags observed only through comparisons. Concrete value replaced by comparison outcome vector. Gate: domain > 16 values, exclusivity, not projected.

### Hidden-event scheduling (`events.py`)

Timers/counters accumulate over many scans but the BFS would revisit the same PENDING state repeatedly. The event scheduler accelerates this:

1. `_scans_until_done_event` / `_scans_until_threshold_event` — compute scans to next crossing from the per-scan delta
2. `_advance_hidden_progress` — fast-forward accumulator by skipped scans
3. `_settle_pending` — cascade: resolve nearest event, re-check, repeat (bounded by event count). Abstract threshold branches that arm later exact timers must keep settling until no exact pending work remains.
4. `_maybe_jump_hidden_event` — when BFS revisits a known PENDING state, jump directly to the crossed successor

Abstract thresholds (dynamic presets): `_materialize_abstract_threshold_outcome` creates a representative crossed state without knowing the concrete preset value. Counterexamples that depend on this representative witness must surface a caveat because replaying `TraceStep.inputs` alone may not reproduce the violation.

### Optimizations active during BFS (`_BFSConfig`)

- **live_input_pruning** — skip inputs masked by current state (partial eval)
- **exclusive_input_grouping** — collapse mutually exclusive Bool input families into canonical assignments
- **edge_compression** — collapse dead edge prevs to sentinel
- **hidden_event_jumping** — jump from revisited pending plateaus
- **pending_settlement** — settle pending timers before evaluating failing properties

All five are on by default. Each has its own cache keyed by stateful prefix + threshold vector (caches are on `_EdgeCompressor` and `_LiveInputCache`).

### Journal framework (`results.py`, `passes.py`)

`prove(logic, condition, journal=True)` attaches a `Journal` to the result. The journal is a `MappingProxyType[str, TagEntry]` keyed by tag name, recording every decision the pipeline made about each tag.

- `Decision(pass_name, kind, outcome, reason, detail)` — one decision from one pass. `kind` values: `"classification"`, `"domain"`, `"elision"`, `"absorption"`, `"absorption_skipped"`, `"absorption_blocked"`, `"exclusion"`, `"pairing"`, `"input_partition"`, `"exclusive_group"`.
- `TagEntry(name, outcome, domain, domain_source, decisions)` — final state of one tag. `outcome` values: `"stateful"`, `"nondeterministic:edge"`, `"nondeterministic:free"`, `"combinational"`, `"elided:provenance"`, `"elided:concrete"`, `"excluded:<reason>"`.
- `Journal` supports `__getitem__`, `__contains__`, `__iter__`, `__len__`, `__str__`. Also carries `notes` for skip_optimizations flags and depth truncation.

`_JournalBuilder` in `passes.py` accumulates decisions during the pass pipeline. Each instrumented pass calls `builder.record(tag_name, Decision(...))` when `ctx.journal_builder is not None`. The builder's `freeze()` method synthesizes `TagEntry` outcomes from the accumulated decisions plus the final classification dicts.

When `journal=False` (default), `journal_builder` is `None`, no `Decision` objects are created, and `result.journal` is `None`. Zero overhead on the default path.

## Formal foundations

See `scratchpad/prove-formal-foundations.md` for the citation map. Key results:

- **Exclusivity principle** (absorb.py): data independence (Wolper 1986) + time-abstracting bisimulation (Tripakis & Yovine 2001)
- **Event acceleration**: flat acceleration for linear counter automata (Leroux & Sutre 2005)
- **Done-bit three-valued abstraction**: zone abstraction / DBMs (Dill 1989; Mine 2001)
- **Domain partition from comparisons**: Cartesian predicate abstraction (Ball, Podelski, Rajamani 2001)
- **Structural domain propagation**: abstract interpretation fixed-point (Cousot & Cousot 1977)
- **Scope + absorption ordering**: cone-of-influence reduction commutes with abstraction for safety properties (Clarke, Grumberg, Long 1992)

## Performance profile

Benchmark: `make bench` (PackML example, 128 reachable states, ~82s wall).

The pipeline is dominated by the **elision sub-pipeline** (~66s, 80% of wall time). BFS takes ~17s. No clustering or Cartesian product — a single projection pass covers all projected tags.

Within elision (~66s):

1. **Concrete kernel proofs** (~36s, 54%) — `_can_elide` enumerates (state, input) pairs per candidate, calling `_step_compiled_kernel` for each. Compiled `_kernel_step` self-time: 12s. Kernel context allocation (`ScanContext.__init__`): 2.4s.
2. **Abstract interpreter** (~30s, 46%) — `_pass_abstract` runs provenance analysis per candidate. Hot paths: `_eval_conditions` (10s), `_merge_states` (9.5s), `_read_names` via `pdg.walk` (8s).
3. **Cross-cutting interpreter overhead** — `dict.get` 8s (51M calls), `isinstance` 5s (30M calls), abstract value get/set/merge ~5s combined.

## Invariants to preserve

- **Soundness**: every reachable state must be visited. Over-approximation (extra states) is safe; under-approximation is not. If you change domain inference or absorption, the new rule must be at least as conservative as the old one.
- **Threshold absorption gate**: the exclusivity check (`_has_forbidden_data_read`) is the soundness gate. Stability checks are pragmatic implementation constraints, not soundness requirements.
- **Settle-pending termination**: bounded by event count + 1. Accumulators must not decrement during settling.
- **Edge compression correctness**: dead edge prevs use a sentinel `_EDGE_DEAD`. An edge is dead only when partial eval proves all containing expressions constant — false negatives (marking a live edge dead) would lose states.
- **State key completeness**: every tag whose cross-scan value affects reachability must appear in the state key. Missing a dimension silently merges distinguishable states.

## Testing

Test files:
- `tests/core/analysis/test_prove.py` — integration tests (30 test classes, ~3500 lines)
- `tests/core/analysis/test_prove_matrix.py` — soundness coverage matrix (subsystem interaction tests)
- `tests/core/analysis/test_prove_passes.py` — pre-BFS pass pipeline unit tests
- `tests/core/analysis/test_elision_agreement.py` — three-way agreement harness for elision
- `tests/core/analysis/test_packml_diagnosis.py` — PackML-specific regression tests (cross-product input enumeration, stuck-state diagnosis)

### Counterexample trace replay (`test_prove_matrix.py`)

Every `Counterexample` assertion in the soundness matrix must be followed by a call to `_assert_trace_replays(logic, result, "TagName")`. This replays the prove() trace on a concrete PLC and verifies the violation actually occurs. Traces with caveats (abstract threshold witnesses) are skipped automatically.

**When adding a new test that asserts `isinstance(result, Counterexample)`**: always add a `_assert_trace_replays` call immediately after. This is the two-oracle check: prove() found a violation (Oracle A), and the concrete PLC confirms it (Oracle B). Without the replay, we only know prove() *claims* a violation exists — not that the trace is valid.

### Three-way elision agreement (`test_elision_agreement.py`)

Runs three oracles on every elision candidate and checks consistency:
1. **Interpreted** — `ScanContext` + `Program._evaluate` (the PLC scan path)
2. **Compiled kernel** — `_step_compiled_kernel` (same path `prove()` uses)
3. **Abstract prediction** — `_ScanLocalStateElider.elide()` (provenance analysis)

Contracts verified:
- **(a) Interpreted == Compiled** on every (state, input) pair — catches compiler bugs
- **(b) Abstract ⊇ Concrete** — if abstract says elidable, concrete must agree (catches abstract unsoundness)
- **(c) Pipeline consistency** — elided tags are valid against the final retained set

To add a new test program: define a builder function returning `(Program, stateful_dims, nd_dims)` and add it to `_UNIT_PROGRAMS`, or add a `test_*` method in `TestExampleProgramAgreement` that imports from `examples/`.

## External integration

- **CLI**: `pyrung lock` / `pyrung check` in `src/pyrung/cli.py` — calls `reachable_states()`, `write_lock()`, `check_lock()`
- **Public API**: re-exported from `src/pyrung/core/analysis/__init__.py` — `prove`, `reachable_states`, `diff_states`, `Proven`, `Counterexample`, `Intractable`, `StateDiff`, `TraceStep`, `Decision`, `TagEntry`, `Journal`
- **DAP miner**: `src/pyrung/dap/miner.py` uses `read_lock()` to filter candidates
- **Examples**: `examples/fault_coverage.py` demonstrates two-pass fault coverage (structural via prove + timing via force tests)
- **Compiled kernel**: `prove` uses `pyrung.circuitpy.codegen.compile_kernel()` — the same codegen path as CircuitPython output. The compiled kernel is the execution oracle for BFS steps.
