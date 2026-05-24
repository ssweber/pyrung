# Deadcode Review (2026-05-24)

Tool: `uvx deadcode src/pyrung/ tests/`

154 findings in `src/pyrung/`. Majority are false positives from dynamic dispatch patterns.

## False Positives (~110 findings)

| Pattern | Count | Why |
|---------|-------|-----|
| DAP `_on_*` methods | ~35 | Dispatched via `getattr(self, f"_on_{command}")` |
| DAP `_cmd_*` console functions | ~30 | Registered via `@register()` decorator |
| `condition_trace.py` `_` methods | ~20 | `@singledispatchmethod` overloads |
| pytest plugin hooks | 5 | `pytest_addoption`, `pytest_configure`, etc. — discovered at runtime |
| `sys.argv` in cli.py | 1 | Setting `sys.argv`, not a custom attribute |
| `Result3`/`Result4` in twin/_slot.py | 2 | Fixed memory-layout fields for hardware registers |
| `ConditionGroup` type alias | 1 | Used by `ConditionInput` on the next line |
| `spec_index` in events.py | 1 | Actually used at lines 536/556 |

## Write-Only Attributes (assigned, never read)

| Location | Name | Notes |
|----------|------|-------|
| `prove/events.py:183-186,1091,1093` | `jump_hits/misses`, `settle_hits/misses` | Stats counters — incremented but never read. Debug/profiling artifacts. |
| `prove/passes.py:258,1094` | `_functional_dep_projections` | Assigned in pass, never read later |
| `prove/passes.py:259,1358` | `_init_constant_projections` | Same — assigned, never consumed |
| `prove/results.py:141` | `dimensions` | Field on result type, never accessed |
| `runner.py:515,2303,2308` | `_inflight_rung_events` | Dict assigned/cleared, never read |
| `condenser.py:30` | `span_scans` | Field on `CommandInfo`, never accessed |
| `stuck_bits.py:130` | `reachable_sites` | Field on result, never accessed |
| `codegen/context.py:164,620` | `uses_board_save_memory_cmd` | Attribute assigned, never read |
| `codegen/models.py:151` | `operand_str` | Field assigned, never accessed |

## Dead Functions/Methods

| Location | Name | Notes |
|----------|------|-------|
| `prove/__init__.py:684` | `_stderr_progress` | No callers |
| `prove/absorb.py:452` | `_is_stable_dynamic_preset` | Documented as eliminated in Phase 1 |
| `prove/passes.py:85` | `_narrow_indirect_block_specs` | No callers |
| `prove/passes.py:542` | `_OptConfig.all_off` | Only mentioned in a docstring |
| `prove/passes.py:1717` | `_unoptimized_passes` | No callers (referenced in scratchpad docs only) |
| `analysis/pdg.py:174` | `upstream_slice_all` | No callers (superseded by `upstream_slice_strict`) |
| `analysis/dataview.py:264` | `isolated` | Only in docs, never called from Python |
| `analysis/simplified.py:35` | `Atom._key` | Superseded by `_expr_key()` |
| `analysis/causal/history.py:13` | `_scan_ids_descending` | No callers |
| `condition.py:125` | `_resolve_value` | No callers |
| `compiled_plc.py:104` | `set_memory_bulk` | No callers |
| `context.py:261` | `set_memory_bulk` | No callers |
| `kernel.py:71,91` | `snapshot_tags`, `capture_prev` | No callers |
| `rung.py:184` | `_execute_instructions` | No callers |
| `harness.py:169` | `pending_count` property | No callers |
| `harness.py:369` | `_delay_scans` | No callers |
| `system_points.py:360` | `is_system_tag` | No callers |
| `instruction/base.py:50,74` | `exclusive_resources`, `always_execute` | Defined + overridden, never called |
| `instruction/send_receive/helpers.py:31` | `_range_end_for_count` | No callers |
| `click/codegen/parser.py:79` | `_load_nicknames_from_csv` | No callers |
| `click/ladder/translator.py:204,566` | `_explicit_count`, `_require_block_entry` | No callers |
| `click/system_mappings.py:119` | `SYSTEM_TAG_NAMES_BY_HARDWARE` | No callers |
| `circuitpy/codegen/_util.py:43` | `_optional_range_type_name` | No callers |
| `circuitpy/codegen/context.py:486` | `reset_name_counters` | No callers |
| `circuitpy/codegen/render_modbus.py:101,136,162` | Three modbus helpers | No callers |
| `circuitpy/codegen/render_runtime.py:64` | `_needed_helpers` | No callers |
| `dap/handlers/stack_variables_evaluate.py:155` | `evaluate_repl_command_locked` | Old implementation, superseded by `dispatch()` |
| `causal/models.py:47` | `STRUCTURAL_CONTRADICTION` | Enum variant never used |

## Borderline / Keep for Now

- `isolated()` on DataView — documented public API, just no Python callers yet
- `_unoptimized_passes()` — referenced in design doc as intentional ("NOT an optimization"), but no code callers

## Recommended Cleanup Priority

Highest-confidence removals:

1. `evaluate_repl_command_locked` in `stack_variables_evaluate.py` — clearly superseded
2. `_is_stable_dynamic_preset` — design docs explicitly say it was eliminated
3. `Atom._key` — superseded by `_expr_key()`
4. Write-only stats counters in `prove/events.py`
5. The `set_memory_bulk` / `snapshot_tags` / `capture_prev` cluster in core — never wired up
