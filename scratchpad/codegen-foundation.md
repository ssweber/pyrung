# CircuitPython Codegen Foundation

## Goal

`generate_circuitpy()` works end-to-end for a simple DI->DO program.

## Scope

This document defines Step 1 (foundation) of the split described in `scratchpad/circuitpython-codegen.md`.

In scope:

- section 3 public API (`generate_circuitpy` signature, preconditions, validation gate, error model)
- section 5 `CodegenContext`, `SlotBinding`, `BlockBinding` data structures
- section 6 tag collection, classification, symbol mangling, default initialization
- section 7 condition compiler (all condition types)
- section 8 expression compiler (all expression nodes)
- section 9.3 coil compilers only (`out`/`latch`/`reset`)
- section 10 I/O mapping (discrete/analog/temperature read+write, roll-call)
- section 11 branch compilation
- section 12 indirect addressing
- section 13 runtime helper emission policy
- section 4 generated `code.py` assembly (template sections 1-5, 8-13)
- section 4 generated `code.py` persistence sections 6-7 emit stubs only
- section 14.2 `src/pyrung/circuitpy/__init__.py` export

Out of scope:

- non-coil instruction families from section 9
- full SD retentive persistence behavior
- watchdog runtime diagnostics and full end-to-end v1 parity

## Required Deliverables

- `src/pyrung/circuitpy/codegen.py` foundation pipeline:
  - context building
  - rung/instruction dispatch for coil-only writes
  - source assembly for required template sections
- `src/pyrung/circuitpy/__init__.py` export wiring for `generate_circuitpy`
- `tests/circuitpy/test_codegen.py` foundation coverage

## Test Coverage (Step 1)

- API validation gate behavior
- deterministic output
- condition compiler coverage
- expression compiler coverage
- coil instruction compilation (`out`, `latch`, `reset`)
- I/O mapping coverage
- indirect addressing coverage
- branch ordering and compilation shape
- generated-source `compile()` smoke test

## Exit Criteria

The verification script in section 16 of `scratchpad/circuitpython-codegen.md` passes:

- a minimal DI->DO program generates valid `code.py`
- generated `code.py` passes `compile(source, "code.py", "exec")`

