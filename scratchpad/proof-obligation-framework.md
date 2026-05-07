.# Proof-Obligation Framework for `prove/`

## Context

The `prove/` BFS verifier has grown organically — 12 pre-BFS passes, 6 optimizations, ~4500 lines across 11 files. An investigation identified four soundness gaps (A-D) where optimization assumptions go unchecked. Gaps A (edge-read elision) and C (condition-gated counters) are being fixed as inline static gates. This plan addresses the structural problem underneath: **the pipeline doesn't declare or verify the assumptions its optimizations depend on**.

We're building three things layered on a prerequisite refactor:
- **Phase 1**: `requires`/`provides` contracts on passes with DAG validation
- **Phase 2**: Unified `_DiagnosticAccumulator`
- **Phase 3**: Proof obligation protocol with discharge handlers for Gaps B and D

---

## Phase 1: `requires`/`provides` on `_PreBFSPass`

**File**: `src/pyrung/core/analysis/prove/passes.py`

### Add fields to `_PreBFSPass` (line 262)

```python
@dataclass(frozen=True)
class _PreBFSPass:
    name: str
    description: str
    run: Callable[[_PassContext], None]
    enabled: bool = True
    requires: frozenset[str] = frozenset()
    provides: frozenset[str] = frozenset()
```

Values use semantic group names (not raw field names) to keep annotations readable:

| # | Pass | requires | provides |
|---|------|----------|----------|
| 1 | build_graph | `{}` | `{graph, all_exprs}` |
| 2 | classify_dimensions | `{graph, all_exprs}` | `{classification}` |
| 3 | pilot_sweep | `{graph, classification}` | (none — updates in place) |
| 4 | diagnose_unwritten_tags | `{graph, classification}` | (none — side effect) |
| 5 | elide_scan_local_state | `{graph, all_exprs, classification}` | (none — updates in place) |
| 6 | compile_kernel | `{classification}` | `{compiled_names}` |
| 7 | collect_done_acc_pairs | `{}` | `{done_acc_info}` |
| 8 | find_redundant_absorptions | `{graph, all_exprs, done_acc_info}` | `{absorptions}` |
| 9 | find_threshold_absorptions | `{graph, all_exprs}` | `{threshold_absorptions}` |
| 10 | build_event_specs | `{compiled_names, classification, threshold_absorptions}` | `{event_specs}` |
| 11 | collect_edge_exprs | `{compiled_names}` | `{edge_tag_exprs}` |
| 12 | discover_memory_keys | `{compiled_names, absorptions}` | `{memory_key_names}` |

`classification` = stateful_dims, nondeterministic_dims, done_acc, done_presets, done_kinds.
`compiled_names` = compiled, stateful_names, edge_tag_names.
`event_specs` = state_key_done_specs, done_event_specs, threshold_event_specs.

### Add DAG validation function (~10 lines)

```python
def _validate_pass_dag(passes: tuple[_PreBFSPass, ...]) -> None:
    available: set[str] = set()
    for p in passes:
        if not p.enabled:
            continue
        missing = p.requires - available
        if missing:
            raise ValueError(
                f"Pass {p.name!r} requires {sorted(missing)} "
                f"but only {sorted(available)} available"
            )
        available |= p.provides
```

Call at top of `_run_pre_bfs_pipeline()`, before the pass loop.

### Annotate `_DEFAULT_PRE_BFS_PASSES` (line 594)

Add `requires=frozenset({...})` and `provides=frozenset({...})` to each of the 12 `_PreBFSPass(...)` entries.

### Update `TestPassManifest`

**File**: `tests/core/analysis/test_prove_passes.py`

Add `test_default_passes_have_valid_dag` — calls `_validate_pass_dag(_DEFAULT_PRE_BFS_PASSES)`, asserts no error.

Add `test_reordered_passes_fail_dag_validation` — swap two dependent passes, assert `ValueError`.

Add `test_disabled_provider_detected` — disable a providing pass, verify downstream requiring pass fails validation.

---

## Phase 2: `_DiagnosticAccumulator`

**File**: `src/pyrung/core/analysis/prove/passes.py`

### New types (~25 lines)

```python
@dataclass(frozen=True)
class _DiagnosticEntry:
    level: str    # "info" | "warning"
    source: str   # pass name or "discharge"
    message: str

@dataclass
class _DiagnosticAccumulator:
    _entries: list[_DiagnosticEntry] = field(default_factory=list)

    def info(self, source: str, message: str) -> None:
        self._entries.append(_DiagnosticEntry("info", source, message))

    def warning(self, source: str, message: str) -> None:
        self._entries.append(_DiagnosticEntry("warning", source, message))

    def emit_to(self, callback: Callable[[str], None] | None) -> None:
        if callback is None:
            return
        for e in self._entries:
            callback(f"{e.level} | {e.source} | {e.message}")

    def as_caveats(self) -> tuple[str, ...]:
        return tuple(e.message for e in self._entries if e.level == "warning")
```

### Add to `_PassContext` (line 136)

```python
diagnostics: _DiagnosticAccumulator = field(default_factory=_DiagnosticAccumulator)
```

### Migrate `_pass_diagnose_unwritten_tags` (lines 433-455 of passes.py)

Replace direct `ctx.progress_info(f"diagnostic | ...")` calls with `ctx.diagnostics.info("diagnose_unwritten_tags", ...)`.

### Integrate into orchestrator

In `_run_pre_bfs_pipeline`, after the pass loop (and after obligation discharge — Phase 3), before returning:
```python
ctx.diagnostics.emit_to(ctx.progress_info)
```

### Merge into freeze caveats

In `_PassContext.freeze()`, merge accumulator warnings with `_detect_edge_caveats()`:
```python
caveats = _detect_edge_caveats(...) + self.diagnostics.as_caveats()
```

### Tests

**File**: `tests/core/analysis/test_prove_passes.py`

- `test_diagnostic_accumulator_info_emitted` — add info, verify emit_to callback receives it
- `test_diagnostic_accumulator_warnings_become_caveats` — add warning, verify as_caveats returns it
- `test_info_not_in_caveats` — add info, verify as_caveats excludes it
- `test_diagnose_unwritten_tags_uses_accumulator` — run pass, verify diagnostics populated (not just callback)

---

## Phase 3a: Proof Obligation Framework (implemented)

**File**: `src/pyrung/core/analysis/prove/passes.py`

Implemented the obligation protocol infrastructure:
- `_ProofObligation` frozen dataclass (tag, kind, source_pass, context)
- `obligations` list on `_PassContext`
- `_DISCHARGE_HANDLERS` registry (extensible dict)
- `_discharge_obligations()` orchestrator with diagnostic warnings
- `_DOWNSTREAM_PASS_NAMES` for re-running passes after reverts
- Wired into `_run_pre_bfs_pipeline` after the pass loop

No concrete handlers or obligation generation sites — the framework is
available for future passes that need discharge-or-revert semantics.

Framework tests (4):
- `test_discharge_no_obligations_is_noop`
- `test_discharge_passing_obligation_no_revert`
- `test_discharge_failing_obligation_triggers_revert`
- `test_unknown_kind_produces_warning`

## Phase 3b: Gaps B and D — closed, no handlers needed

The original investigation identified two claimed soundness gaps:

**Gap B (constant_delta)**: Threshold event scheduling assumes constant
per-scan delta for extrapolation.  Re-investigation found this is sound:
- When delta=0 (timer/counter condition not met), no jump happens
- The exclusivity gate (`_has_forbidden_data_read`) ensures the concrete
  Acc value only appears in threshold comparisons, so a "wrong"
  extrapolated value can't affect other tag computations
- Jumps add states to the BFS queue (over-approximation), never remove
  them — sound for safety properties
- The BFS explores all ND input combos independently; each gets its own
  delta computation

**Gap D (absorption_nd_gated)**: Redundant Acc absorption merges concrete
Acc values into three-valued Done, claimed unsafe when the timer rung is
gated by an ND input.  Re-investigation found this is sound:
- `_is_acc_done_redundant` verifies every Acc atom is a Done-boundary
  comparison — no intermediate thresholds can be hidden
- `_has_forbidden_data_read` blocks absorption when Acc appears in
  data-flow
- ND-gating affects *when* transitions happen (timing), not *whether*
  they're reachable (state observability) — and prove doesn't model time
- The BFS explores all ND input combos at every state, so all Done
  transitions (False→PENDING→True and resets) remain reachable

Both gaps are closed by existing exclusivity and atom-boundary checks.
No discharge handlers or revert mechanics are needed.

---

## Implementation Order

1. **Phase 1** — `requires`/`provides` on `_PreBFSPass` with DAG validation. ✓
2. **Phase 2** — `_DiagnosticAccumulator` with structured diagnostic pipeline. ✓
3. **Phase 3a** — Proof obligation framework (infrastructure only). ✓
4. **Phase 3b** — Gaps B and D discharge handlers. ✗ Closed — not needed.

## Verification

- `make test` — 3610 tests pass (11 new: 3 DAG + 4 diagnostic + 4 obligation framework)
- `make lint` — clean
- No obligation generation sites active — framework is dormant until a
  real gap surfaces
