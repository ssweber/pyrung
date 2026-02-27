# Plan: Create `scratchpad/circuitpython-codegen.md`

## Context

The CircuitPython dialect has its hardware model complete and its validation layer being implemented by another developer. The next stage is **code generation** — translating a pyrung `Program` + `P1AM` hardware config into a standalone CircuitPython `code.py` that runs on a P1AM-200 PLC.

This plan produces a handoff brief (`scratchpad/circuitpython-codegen.md`) following the same pattern as the validation scratchpad, for another developer to implement.

## Design Decisions (from user discussion)

- **P1 instruction scope**: out/set/reset, on_delay/off_delay, count_up/count_down, copy/calc, blockcopy/fill, rise/fall, comparisons, branches, subroutines, function calls. Deferred: shift, search, pack/unpack, for_loop.
- **Tag representation**: Flat global variables for non-block tags; Python lists for blocks (enables indirect addressing via `DS[idx - 1]`)
- **Retentive persistence**: Deferred to a persistence file store later design
- **Output format**: Single standalone `code.py` file with inline runtime helpers
- **Indirect addressing**: Blocks → lists, so `IndirectRef(DS, Idx)` → `DS[int(Idx) - 1]` naturally

## File to Create

### `scratchpad/circuitpython-codegen.md` (~500 lines)

Handoff brief covering:

1. **Context** — what codegen does, where it fits in the pipeline (after validation)
2. **Design decisions** — tag representation, generated code structure, P1 scope
3. **Public API** — `generate_circuitpy(program, hw, *, target_scan_ms, watchdog_ms) -> str`
4. **Generated code structure** — complete annotated template showing all sections
5. **CodegenContext class** — metadata collector that walks the program
6. **Tag collection & classification** — I/O vs internal, blocks vs scalars, UDT flattening
7. **Condition compiler** — maps each condition type to Python expression strings
8. **Expression compiler** — recursive walk of expression trees → Python arithmetic
9. **Instruction compilers** — per-instruction-type code generation patterns:
   - out/set/reset (coils)
   - on_delay/off_delay (timers with fractional accumulation)
   - count_up/count_down (counters with DINT clamp)
   - copy/calc (data transfer with clamp/wrap)
   - blockcopy/fill (range operations on lists)
   - function calls (source embedding via `inspect.getsource`)
   - subroutine call/return
10. **I/O mapping** — discrete bitmapped vs analog per-channel read/write
11. **Branch compilation** — nested if-else for parallel branches
12. **Indirect addressing approach** — blocks as lists, 1→0 index conversion, bounds checking
13. **Runtime helpers** — _clamp_int, _wrap_int, _rise, _fall (only emitted if used)
14. **Files to create/modify** with code signatures
15. **Test plan** — test classes covering each compilation target
16. **Verification** — make test, make lint, generated code runs on CircuitPython

## Critical Reference Files

- `scratchpad/circuitpython-validation.md` — pattern to follow for document structure
- `src/pyrung/circuitpy/hardware.py` — P1AM class, `_slots` dict, block construction
- `src/pyrung/circuitpy/catalog.py` — ModuleSpec, ChannelGroup, MODULE_CATALOG
- `src/pyrung/core/rung.py` — Rung.evaluate(), branch execution model
- `src/pyrung/core/instruction/` — all instruction execute() methods (reference semantics)
- `src/pyrung/core/condition.py` — all condition types and evaluate() methods
- `src/pyrung/core/expression.py` — expression tree types
- `src/pyrung/core/tag.py` — Tag, InputTag, OutputTag, TagType
- `src/pyrung/core/memory_block.py` — Block, InputBlock, OutputBlock
- `src/pyrung/core/structure.py` — @udt(), @named_array() tag structure
- `examples/traffic_light.py` — good reference program for testing codegen

## P1AM CircuitPython API (target platform)

```python
import P1AM
base = P1AM.Base()                        # 1-indexed by default
base.rollCall(["P1-08SIM", "P1-08TRS"])   # verify modules match config
base.readDiscrete(slot)                   # → bitmapped int (LSB = ch1)
base.writeDiscrete(bitmask, slot)         # bitmapped write
base.readAnalog(slot, channel)            # → int (raw ADC counts)
base.writeAnalog(value, slot, channel)    # analog write
base.config_watchdog(ms)                  # optional
base.start_watchdog() / base.pet_watchdog()
```

## Verification

After the scratchpad is written:
- Review against validation scratchpad for consistency in format and depth
- Confirm all P1 instructions have compilation patterns documented
- Confirm generated code template covers full scan cycle
- Confirm test plan covers each instruction type and edge case
