# CircuitPython Codegen Feature Complete

## Goal

Complete v1 spec with full instruction coverage, SD persistence behavior, watchdog support, and end-to-end smoke coverage.

## Prerequisite

Step 1 foundation (`scratchpad/codegen-foundation.md`) is complete and stable.

## Scope

This document defines Step 2 (additive completion) of the split described in `scratchpad/circuitpython-codegen.md`.

In scope:

- section 9.2 one-shot handling
- section 9.4 timers (TON, RTON, TOFF)
- section 9.5 counters (up/down)
- section 9.6 copy + calc (clamp vs wrap semantics)
- section 9.7 block operations (`blockcopy`, `fill`)
- section 9.8 function call compilers (inspectability, source embedding, output mapping)
- section 9.9 subroutine call/return
- section 9.10 search (numeric/text, continuous resume)
- section 9.11 shift (rising-edge, reset priority)
- section 9.12 pack/unpack (bits, words, text, IEEE reinterpretation)
- section 9.13 for-loop (disabled-path child reset)
- section 2.3 and section 4 sections 6-7 SD retentive persistence:
  - mount/load/save flow
  - optional NVM dirty-flag behavior
  - SD status points and command bit acknowledgements
- section 2.6 and section 4 section 4 watchdog API binding and scan-overrun diagnostics

## Required Deliverables

- extend `src/pyrung/circuitpy/codegen.py` instruction dispatch with all remaining section 9 compilers
- complete runtime assembly for persistence and watchdog sections (no longer stubs)
- finalize helper emission and state handling needed by timers/counters/search/pack/unpack/loops
- expand `tests/circuitpy/test_codegen.py` to full section 15 scenario coverage

## Test Coverage (Step 2)

All section 15 test classes not covered in foundation:

- timers
- counters
- copy/calc
- block operations
- search
- shift
- pack/unpack
- function calls
- for-loop
- retentive persistence
- watchdog binding
- scan diagnostics
- end-to-end generated-code smoke with stubbed `P1AM`

## Exit Criteria

- `make test` passes
- `make lint` passes
- all 21 required scenarios from section 15.11 in `scratchpad/circuitpython-codegen.md` are covered

## Implementation Note

Step 2 is intentionally additive. Once Step 1 has established context building, compile, assemble, and emit, each remaining compiler should slot into existing dispatch paths without structural rework. SD persistence and watchdog behavior should slot into already-emitted template sections.

