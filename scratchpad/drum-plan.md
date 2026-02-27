# Plan: Add `time_drum` and `event_drum` to Core, Click Validation, and CircuitPython Codegen

## Summary
Implement two new terminal instructions with builder chaining and fixed scan-order behavior:
1. `event_drum(...)`
2. `time_drum(...)`

Runtime control precedence is fixed per scan:
1. Auto progression from main enable
2. Reset
3. Jump
4. Jog

Core remains permissive (tags allowed where meaningful).  
Click validation adds strict portability constraints (including literal-only time presets).

## Explicit DSL Examples
Use these as canonical behavior examples in docs/tests.

### Example A: Event drum (core intent, matching your signature)
```python
with Rung(condition):
    event_drum(
        outputs=[Y001, Y002, Y003, Y004],
        events=[C11, C12, C13, C14],
        pattern=[
            [1, 0, 1, 0],
            [0, 1, 1, 0],
            [0, 0, 0, 1],
            [1, 1, 0, 1],
        ],
        current_step=DS1,
        completion_flag=C8,
    ).reset(X002).jump(condition=X003, step=DS2).jog(X004)
```

### Example B: Time drum (core intent, tag presets allowed in core)
```python
with Rung(condition):
    time_drum(
        outputs=[Y001, Y002, Y003, Y004],
        presets=[500, DS11, 200, DS13],
        unit=Tms,
        pattern=[
            [1, 0, 1, 0],
            [0, 1, 1, 0],
            [0, 0, 0, 1],
            [1, 1, 0, 1],
        ],
        current_step=DS1,
        accumulator=DS2,
        completion_flag=C8,
    ).reset(X002).jump(condition=X003, step=DS2).jog(X004)
```

### Example C: Time drum (Click-portable form)
```python
with Rung(condition):
    time_drum(
        outputs=[Y001, Y002, Y003, Y004],
        presets=[500, 750, 200, 1000],  # literal ints only for Click portability
        unit=Tms,
        pattern=[
            [1, 0, 1, 0],
            [0, 1, 1, 0],
            [0, 0, 0, 1],
            [1, 1, 0, 1],
        ],
        current_step=DS1,    # DS only in Click mode
        accumulator=TD1,     # TD only in Click mode
        completion_flag=C8,  # C only in Click mode
    ).reset(X002).jump(condition=X003, step=DS2).jog(X004)
```

### Example D: Control precedence proof case
```python
# Same scan: auto condition true, reset true, jump edge true, jog edge true
# Result must follow precedence: auto -> reset -> jump -> jog
# Final step = jump target + 1 (if not already at last step), accumulator reset, outputs from final step.
```

## Public API and Interface Changes
1. Add DSL builders in `src/pyrung/core/program/builders.py`:
   - `event_drum(...)->EventDrumBuilder`
   - `time_drum(...)->TimeDrumBuilder`
2. Builder contract:
   - `.reset(condition)` required to finalize and add instruction.
   - `.jump(condition=..., step=...)` optional.
   - `.jog(condition)` optional.
3. Export from:
   - `src/pyrung/core/program/__init__.py`
   - `src/pyrung/core/__init__.py`
4. Add instruction classes in `src/pyrung/core/instruction/drums.py`:
   - `EventDrumInstruction`
   - `TimeDrumInstruction`
5. Re-export in `src/pyrung/core/instruction/__init__.py`.

## Core Runtime Spec (Decision Complete)
1. Common validation:
   - steps: `1..16`
   - outputs: `1..16`
   - `pattern` size matches steps × outputs
   - `pattern` values are bool or `0/1`
   - output tags are BOOL, no duplicates
   - `current_step` is INT or DINT
   - `completion_flag` is BOOL
2. `event_drum`:
   - `events` count equals step count
   - event transition is rising-edge only
   - if event is already ON at step entry, no immediate advance until new edge
3. `time_drum`:
   - `presets` count equals step count
   - core accepts each preset as `int` or INT/DINT tag
   - `accumulator` is INT or DINT
   - increment from `_dt` with `TimeUnit` conversion and fractional carry
4. Execution flags:
   - `ALWAYS_EXECUTES=True`
   - `INERT_WHEN_DISABLED=False`
   - `is_terminal()=True`
5. Enable/disabled behavior:
   - main enable OFF pauses auto progression
   - reset still active while disabled
   - jump/jog ignored while disabled
6. Scan control order (fixed):
   - Auto progression
   - Reset (level)
   - Jump (edge)
   - Jog (edge)
7. Jump behavior:
   - out-of-range step ignored
8. Completion flag:
   - sets ON when sequence completes
   - clears only on reset
9. Initialization:
   - invalid current step initializes to step 1 when enabled
   - step 1 outputs applied immediately on valid start/reset

## Click Portability Rules
Apply in `src/pyrung/click/capabilities.py` and `src/pyrung/click/validation.py`.

| Field | Core | Click Portable |
|---|---|---|
| `outputs` | BOOL tags | `Y` or `C` only |
| `current_step` | INT/DINT | `DS` only |
| `completion_flag` | BOOL | `C` only |
| `accumulator` (time) | INT/DINT | `TD` only |
| `presets` (time) | int or tag | literal `int` only |
| `jump(step=...)` | int or tag | if tag, `DS` only |
| event conditions | BOOL condition | bit-level banks only |

Add finding code:
- `CLK_DRUM_TIME_PRESET_LITERAL_REQUIRED`

## CircuitPython Codegen Changes
1. Extend imports and dispatch in `src/pyrung/circuitpy/codegen.py` for both drum instructions.
2. Add compilers:
   - `_compile_event_drum_instruction(...)`
   - `_compile_time_drum_instruction(...)`
3. Keep parity with core semantics:
   - same control precedence
   - same enable/disabled gating
   - same edge behavior and completion lifecycle
4. Emit deterministic `_mem` keys using `ctx.state_key_for(instr)`:
   - event edge memory
   - jump edge memory
   - jog edge memory
   - time fractional carry memory

## Walker / Introspection Changes
Update `src/pyrung/core/validation/walker.py` `_INSTRUCTION_FIELDS` for both drum types so:
1. Click validation sees drum operands.
2. CircuitPython reference collection sees drum operands.
3. Debug/test inspection stays deterministic.

## File Implementation Order
1. `src/pyrung/core/instruction/drums.py`
2. `src/pyrung/core/instruction/__init__.py`
3. `src/pyrung/core/program/builders.py`
4. `src/pyrung/core/program/__init__.py`
5. `src/pyrung/core/__init__.py`
6. `src/pyrung/core/validation/walker.py`
7. `src/pyrung/click/capabilities.py`
8. `src/pyrung/click/validation.py`
9. `src/pyrung/circuitpy/codegen.py`

## Test Cases and Scenarios
1. `tests/core/test_drums.py`
   - constructor validation
   - required `.reset(...)`
   - terminal flow enforcement
   - hold/pause when disabled
   - reset active while disabled
   - jump/jog ignored while disabled
   - event edge semantics
   - precedence auto->reset->jump->jog
   - jump out-of-range ignored
   - completion clears only on reset
   - time accumulation/transition parity
2. `tests/core/test_builder_semantics.py`
   - unresolved builder errors
   - drum chain completion behavior
3. `tests/core/test_source_location.py` and `tests/core/test_scan_steps.py`
   - source metadata and debug substeps
4. `tests/click/test_capabilities.py`
   - new drum role compatibility
5. `tests/click/test_validation_stage3.py`
   - valid mapping pass
   - wrong-role failures
   - literal-only time preset failure
6. `tests/circuitpy/test_codegen.py`
   - dispatch coverage
   - key emission
   - runtime parity smoke scenarios for drum control order and edges

## Acceptance Criteria
1. New drum tests pass in core/click/circuitpy suites.
2. Existing timer/counter/shift/search behavior remains unchanged.
3. Core allows your signature style (including tag presets for `time_drum`).
4. Click validator reliably flags non-portable drum usage.
5. Generated CircuitPython behavior matches core drum semantics.

## Assumptions and Defaults
1. `.reset(...)` is required for both drum builders.
2. `.jump(...)` and `.jog(...)` are optional.
3. Event step transitions require new OFF->ON edge.
4. Completion flag is sticky until reset.
5. Jump out-of-range is ignored.
6. Click constraints intentionally stricter than core.
