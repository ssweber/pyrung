# CircuitPython Validation Module

## Context

The CircuitPython dialect has its hardware model complete (`catalog.py`, `hardware.py`, 155 tests). The next step toward code generation is a **validation layer** that checks whether a pyrung `Program` can be deployed to a P1AM-200. This gates code generation — `generate_circuitpython()` should refuse programs that fail validation.

The Click dialect provides a proven pattern: a three-stage pipeline (walker facts → DSL policy → hardware profile checks) producing a typed `ValidationReport`. The CircuitPython validator follows the same architecture but with simpler rules — no bank-based memory model, fewer instruction restrictions.

### Design decisions

- **FunctionCallInstruction**: Portable via `inspect.getsource()` embedding in generated code. Codegen will include the function source. Validation emits a **hint** reminding the user to verify the function runs on CircuitPython (no CPython-only stdlib, etc.).
- **Retentive tags**: Supported — codegen will generate a file-store persistence layer (deferred to codegen work). No validation finding needed.

## Finding Codes (3)

| Code | Stage | Description |
|------|-------|-------------|
| `CPY_FUNCTION_CALL_VERIFY` | 2 | `FunctionCallInstruction` / `EnabledFunctionCallInstruction` — codegen will embed source via `inspect.getsource()`; user should verify function is CircuitPython-compatible |
| `CPY_IO_BLOCK_UNTRACKED` | 3 | `InputTag`/`OutputTag` in program not traceable to a configured P1AM slot — codegen won't know how to map it to hardware I/O |
| `CPY_TIMER_RESOLUTION` | 3 | Timer uses `Tms` (millisecond) unit; effective resolution depends on scan time, which may exceed 1ms on CircuitPython |

### Severity routing (same pattern as Click)

- **strict mode**: all findings → `"error"`
- **warn mode**: all findings → `"hint"`

## Files to Create / Modify

### 1. `src/pyrung/circuitpy/validation.py` — NEW (~200 lines)

**Public types:**
```python
ValidationMode = Literal["warn", "strict"]
FindingSeverity = Literal["error", "warning", "hint"]

@dataclass(frozen=True)
class CircuitPyFinding:
    code: str
    severity: FindingSeverity
    message: str
    location: str
    suggestion: str | None = None

@dataclass(frozen=True)
class CircuitPyValidationReport:
    errors: tuple[CircuitPyFinding, ...] = ()
    warnings: tuple[CircuitPyFinding, ...] = ()
    hints: tuple[CircuitPyFinding, ...] = ()
    def summary(self) -> str: ...
```

**Stage 2 — `_evaluate_function_call(instruction, location, mode)`**
- Checks `type(instruction).__name__` against `{"FunctionCallInstruction", "EnabledFunctionCallInstruction"}`
- Emits `CPY_FUNCTION_CALL_VERIFY` with suggestion to test the function on CircuitPython

**Stage 3 — hardware-aware checks (all require `hw: P1AM`):**

`_evaluate_io_provenance(instruction, location, hw_blocks, mode)`:
- Receives pre-collected set of P1AM `Block` objects (by identity)
- Walks instruction fields to find `InputTag`/`OutputTag` instances
- For each I/O tag, checks if it exists in any P1AM block's `_tag_cache`
- Emits `CPY_IO_BLOCK_UNTRACKED` if not found

`_evaluate_timer_resolution(instruction, location, mode)`:
- Checks `OnDelayInstruction` and `OffDelayInstruction`
- If `instruction.unit == TimeUnit.Tms` → emit `CPY_TIMER_RESOLUTION`
- Suggestion: millisecond timers work but accuracy depends on achieving <1ms scan times

**Shared helpers (duplicated from Click `validation.py`, ~50 lines):**
- `_format_location(loc: ProgramLocation) -> str` — deterministic location string
- `_route_severity(code: str, mode: ValidationMode) -> FindingSeverity`
- `_iter_instruction_sites(program: Program) -> list[tuple[Any, ProgramLocation]]` — walks rungs/subroutines/branches/ForLoop

**I/O tag extraction helper:**
- `_collect_hw_blocks(hw: P1AM) -> set[int]` — iterates `hw._slots`, collects `id(block)` for all InputBlock/OutputBlock instances (including both halves of combo tuples)
- `_extract_io_tags(instruction) -> list[Tag]` — walks instruction fields (using `_INSTRUCTION_FIELDS` knowledge from walker), returns all `InputTag`/`OutputTag` instances found. Also walks conditions on the rung.

**Public entry point:**
```python
def validate_circuitpy_program(
    program: Program,
    hw: P1AM | None = None,
    mode: ValidationMode = "warn",
) -> CircuitPyValidationReport:
```

Flow:
1. `_iter_instruction_sites(program)` → instruction sites
2. For each site: `_evaluate_function_call()` (Stage 2)
3. If `hw` is provided:
   - `_collect_hw_blocks(hw)` → known block identities
   - For each site: `_evaluate_io_provenance()`, `_evaluate_timer_resolution()` (Stage 3)
4. Sort findings into errors/warnings/hints by severity
5. Return `CircuitPyValidationReport`

Note: We skip `walk_program()` (Stage 1 walker facts) entirely. Click needed walker facts for R1–R5 (pointer context, expression context, tilde, truthiness, indirect ranges) because Click hardware has strict bank/instruction constraints. CircuitPython has none of these — all DSL constructs (expressions, pointers, indirect refs) are portable to generated Python code.

### 2. `src/pyrung/circuitpy/__init__.py` — MODIFY

Add:
- Import `validate_circuitpy_program`, `CircuitPyFinding`, `CircuitPyValidationReport`, `ValidationMode` from `validation`
- Export them in `__all__`
- Register dialect:
```python
def _circuitpy_dialect_validator(program, *, mode="warn", **kwargs):
    hw = kwargs.pop("hw", None)
    if hw is not None and not isinstance(hw, P1AM):
        raise TypeError(...)
    if mode not in {"warn", "strict"}:
        raise ValueError(...)
    return validate_circuitpy_program(program, hw=hw, mode=cast(ValidationMode, mode))

Program.register_dialect("circuitpy", _circuitpy_dialect_validator)
```

Key difference from Click: `hw` is optional (Click requires `tag_map`). Stage 2 runs without hardware; Stage 3 requires `hw`.

### 3. `tests/circuitpy/test_validation.py` — NEW (~300 lines)

Test classes mirroring Click's test patterns:

| Test Class | What It Covers |
|------------|----------------|
| `TestCleanProgram` | Simple program with P1AM I/O passes with no findings |
| `TestFunctionCallVerify` | `run_function` → `CPY_FUNCTION_CALL_VERIFY` (warn + strict) |
| `TestEnabledFunctionCallVerify` | `run_enabled_function` → same code (warn + strict) |
| `TestIOBlockUntracked` | InputBlock not from P1AM → `CPY_IO_BLOCK_UNTRACKED` |
| `TestIOBlockTracked` | InputBlock from P1AM → no finding |
| `TestTimerMillisecond` | `on_delay` with `Tms` → `CPY_TIMER_RESOLUTION` |
| `TestTimerSeconds` | `on_delay` with `Ts` → no finding |
| `TestOffDelayTimer` | `off_delay` with `Tms` → `CPY_TIMER_RESOLUTION` |
| `TestNoHardware` | `hw=None` → Stage 2 only, no Stage 3 findings |
| `TestDialectRegistration` | `program.validate("circuitpy", hw=hw)` dispatches correctly |
| `TestReportSummary` | `summary()` format matches Click's pattern |
| `TestLocationFormatting` | Location strings are deterministic |
| `TestSuggestionContent` | Suggestions contain actionable context |
| `TestSubroutines` | Findings in subroutines include subroutine location |
| `TestBranches` | Findings in branches include branch path |
| `TestStrictMode` | All findings become errors in strict mode |
| `TestWarnMode` | All findings become hints in warn mode |
| `TestComboModuleIO` | Combo module I/O tags from P1AM → no finding |

Helpers (same pattern as Click tests):
```python
def _build_program(fn):
    prog = Program(strict=False)
    with prog:
        fn()
    return prog

def _finding_codes(report) -> list[str]:
    # collect all codes across all severity buckets
```

## Verification

```bash
make test          # all 1100+ existing tests still pass
make lint          # ruff + ty + codespell clean

# Specific validation tests
uv run pytest tests/circuitpy/test_validation.py -v
```
