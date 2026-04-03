# Module Split Candidates

Files that would benefit from splitting into packages, ranked by how clean the
split would be.

## 1. `core/instruction/send_receive.py` (1179 lines) — Excellent

Already has explicit `# ---` section banners marking five distinct layers with
clean import boundaries.

| Proposed submodule | Content |
|--------------------|---------|
| `types.py` | `WordOrder`, `RegisterType`, `ModbusAddress`, `ModbusTcpTarget`, `ModbusRtuTarget` |
| `helpers.py` | Click-specific and generic helpers, value packing/unpacking |
| `backends.py` | Click async backend + raw pymodbus backend + shared helpers |
| `instructions.py` | `ModbusSendInstruction`, `ModbusReceiveInstruction` |
| `__init__.py` | re-exports `send`, `receive`, target/address types |

## 2. `click/tag_map.py` (1866 lines) — Good

Three distinct responsibilities: dataclasses, ~14 pure parsing/conversion
helpers, and the `TagMap` class itself.

| Proposed submodule | Content |
|--------------------|---------|
| `_types.py` | `MappedSlot`, `OwnerInfo`, `StructuredImport`, `_TagEntry`, `_BlockEntry`, `_BlockImportSpec` |
| `_parsers.py` | All `_parse_*`, `_format_*`, `_compress_*`, `_build_block_spec` functions (~370 lines) |
| `_map.py` | `TagMap` class (~1400 lines) |
| `__init__.py` | re-exports `TagMap`, `MappedSlot`, `OwnerInfo`, `StructuredImport` |

Note: parsers are tightly coupled to TagMap internals (one-directional), but
they are pure functions and cleanly testable in isolation.

## 3. `circuitpy/codegen/compile.py` (2331 lines) — Good, but cross-coupled

Big dispatch table — public API is 4 functions, but ~2000 lines of private
`_compile_*` functions cross-call shared primitives heavily.

| Proposed submodule | Content |
|--------------------|---------|
| `_core.py` | `compile_condition`, `compile_expression`, `compile_instruction`, `compile_rung` (~500 lines) |
| `_instructions_basic.py` | out/latch/reset/call/return, timers, counters, copy (~400 lines) |
| `_instructions_block.py` | blockcopy, fill, search, shift, drums (~500 lines) |
| `_instructions_pack.py` | pack_bits/words/text, unpack_bits/words (~400 lines) |
| `_modbus.py` | Modbus client spec + send/receive compilation (~180 lines) |
| `_primitives.py` | `_compile_value`, `_compile_lvalue`, `_compile_range_setup`, etc. (~350 lines) |
| `__init__.py` | re-exports 4 public functions |

Concern: `_primitives.py` is a shared dependency for all other submodules.
Workable but means it must not import from the others.

## 4. `core/program/context.py` (664 lines) — Good, watch circular imports

Three clearly bounded zones: thread-local state management, `Program` class,
and control-flow context managers (`Rung`, `Subroutine`, `ForLoop`, `Branch`).

| Proposed submodule | Content |
|--------------------|---------|
| `_state.py` | Thread-local context functions (~50 lines, zero dependencies) |
| `_program.py` | `Program` class |
| `_rung.py` | `Rung` class |
| `_control_flow.py` | `Subroutine`, `SubroutineFunc`, `ForLoop`, `Branch` |
| `__init__.py` | re-exports all public names |

Concern: `Rung.__exit__` and `Subroutine.__enter__` call `Program.current()`.
Needs `TYPE_CHECKING`-guarded imports.

## 5. `core/memory_block.py` (1014 lines) — Borderline

Five classes with little cross-coupling, but they form a unified "block
addressing" model. Splitting would increase import paths without reducing
cognitive load. Probably not worth it.

## NOT recommended (large but cohesive)

- **`circuitpy/codegen/render.py`** (1218) — one responsibility, already split across render/render_modbus/render_runtime
- **`circuitpy/codegen/render_modbus.py`** (1291) — one responsibility, internal groupings are already separate functions
- **`core/condition.py`** (577) — shallow class hierarchy, each class ~15 lines
- **`click/codegen/analyzer.py`** (783) — three pipeline phases but one algorithm
- **`core/program/builders.py`** (959) — all builders follow one pattern, just lots of it
