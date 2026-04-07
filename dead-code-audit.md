# Dead Code Audit — 2026-04-04

Results from `uvx deadcode ./src/`, verified by grepping the full repo.

## Confirmed Dead

### CircuitPy

- `circuitpy/codegen/_util.py:43` — `_optional_range_type_name`: sibling `_range_type_name` is used, this one isn't
- `circuitpy/codegen/context.py:133,510` — `uses_board_save_memory_cmd`: attribute set but never read (incomplete instrumentation; `uses_board_switch`, `uses_board_led`, `uses_board_neopixel` may be similarly dead)
- `circuitpy/codegen/context.py:389` — `reset_name_counters`: orphaned method, `_name_counters` only accessed via `next_name()`
- `circuitpy/codegen/render_modbus.py:100` — `_modbus_valid_ranges`: never called
- `circuitpy/codegen/render_modbus.py:135` — `_render_sparse_reverse_coil`: never called
- `circuitpy/codegen/render_modbus.py:161` — `_render_reverse_register_case`: never called
- `circuitpy/codegen/render_runtime.py:63` — `_needed_helpers`: logic duplicated inline in render.py:874-880
- `circuitpy/catalog.py:60` — `description` field on `ModuleSpec`: populated in every catalog entry but never read (useful metadata — keep?)

### Click

- `click/codegen/models.py:116` — `operand_str` field on `_RangeDecl`: assigned but never read; code reconstructs from prefix/start/end
- `click/codegen/parser.py:79` — `_load_nicknames_from_csv`: superseded by direct `pyclickplc.read_csv()` usage in TagMap
- `click/ladder/translator.py:203` — `_explicit_count`: orphaned method
- `click/ladder/translator.py:565` — `_require_block_entry`: orphaned method
- `click/system_mappings.py:119` — `SYSTEM_TAG_NAMES_BY_HARDWARE`: computed but never imported/accessed

### Core

- `core/instruction/send_receive.py:243` — `_range_end_for_count`: never called
- `core/runner.py:243,1041,1046` — `_inflight_rung_events`: written in 3 places, never read

## Confirmed False Positives

- `circuitpy/hardware.py:192` — `get_slot`: tested in test_hardware.py
- `circuitpy/validation.py:53` — `suggestion`: dataclass field, actively populated
- `click/validation/findings.py:54` — `suggestion`: dataclass field, actively populated
- `core/program/context.py:155` — `call_subroutine`: intentional backwards-compat public API
- `dap/adapter.py` `_on_*` methods: DAP protocol handlers, dynamically dispatched
- `condition_trace.py` `_` methods: singledispatch/visitor pattern
- `core/runner.py` public API methods: consumer-facing, documented in CLAUDE.md
- `core/program/builders.py` DSL methods: public builder API
- `click/tag_map.py` public methods: consumer-facing API
- `click/capabilities.py` public methods: consumer-facing API
