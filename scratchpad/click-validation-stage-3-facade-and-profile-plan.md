# Stage 3 Plan: Validation Facade + Click Hardware Profile + R6-R8

## Summary
Stage 3 adds three capabilities on top of Stage 1/2 planning:

1. A dialect-agnostic `Program.validate(...)` facade in core, implemented via registry dispatch (core never imports Click).
2. A canonical Click hardware capability model in `pyclickplc` (source of truth), including address-aware writability, instruction-role compatibility, and copy-family compatibility.
3. Extended Click validator rules (R6-R8) in `pyrung` that consume `TagMap` + walker/instruction context + `pyclickplc` profile adapter.

This plan also explicitly separates:
- build-time structural guard: `Program(strict=True)`
- post-build dialect portability validation: `validate(mode="strict")`

These remain distinct entry points.

## Locked Decisions
1. R6 writability is address-aware for SC/SD (not bank-only).
2. Missing capability profile is explicit failure (`CLK_PROFILE_UNAVAILABLE`), never silent fallback.
3. R8 includes all copy-family instructions in this stage (`copy`, `blockcopy`, `fill`, `pack_bits`, `pack_words`, `unpack_to_bits`, `unpack_to_words`).

## Dependency Status and Sequencing
Current branch has Stage 1/2 plan docs but not implementation modules yet. Stage 3 implementation order must be:

1. `pyclickplc` capability model and tests.
2. `pyrung` profile adapter to `pyclickplc`.
3. `pyrung` validator extensions (R6-R8).
4. `Program.validate(...)` facade + Click self-registration.
5. End-to-end tests in `pyrung`.

## Capability Tables to Encode (from Click docs + existing pyclickplc conventions)

### 1) Bank Writability Baseline
| Bank | Writable for ladder validation | Notes |
|---|---|---|
| X | No | Input bank |
| Y | Yes | Output bank |
| C | Yes | Internal bit bank |
| T | No | Timer done/status bank (instruction-owned role) |
| CT | No | Counter done/status bank (instruction-owned role) |
| SC | Address subset | Use writable subset constant |
| DS | Yes | Data register |
| DD | Yes | Data register |
| DH | Yes | Data register |
| DF | Yes | Data register |
| XD | No | Input register bank |
| YD | No | Output register bank (treated read-only for portability checks) |
| TD | Yes | Timer current value register bank |
| CTD | Yes | Counter current value register bank |
| SD | Address subset | Use writable subset constant |
| TXT | Yes | Text register bank |

`SC` writable subset (ladder validation profile): `{50, 51, 53, 55, 60, 61, 65, 66, 67, 75, 76, 120, 121}`  
`SD` writable subset (ladder validation profile): `{29, 31, 32, 34, 35, 36, 40, 41, 42, 50, 51, 60, 61, 106, 107, 108, 112, 113, 114, 140, 141, 142, 143, 144, 145, 146, 147, 214, 215}`

### 2) Instruction Role Compatibility (R7)
| Role | Allowed bank(s) |
|---|---|
| `timer_done_bit` | `T` |
| `timer_accumulator` | `TD` |
| `timer_setpoint` | `DS` |
| `counter_done_bit` | `CT` |
| `counter_accumulator` | `CTD` |
| `counter_setpoint` | `DS`, `DD` |
| `copy_pointer` | `DS` |

### 3) Copy-Family Compatibility (R8)
Define operation-specific compatibility matrices keyed by `operation + source_bank + dest_bank`.

Operations:
- `single`
- `block`
- `fill`
- `pack_bits`
- `pack_words`
- `unpack_bits`
- `unpack_words`

Bank-pair rules:
- `single`: support bit-source to bit-dest (`X/Y/C/T/CT/SC -> Y/C`) and register/text pathways per Click single-copy matrix.
- `block`: support source/dest pathways per block-copy matrix (stricter than single-copy).
- `fill`: support source/dest pathways per fill matrix.
- `pack_bits`: support bit bank to register bank pathways per pack matrix.
- `pack_words`: `DS/DH -> DD/DF`.
- `unpack_bits`: `DS/DH/DD/DF -> Y/C` per unpack matrix constraints.
- `unpack_words`: `DD/DF -> DS/DH`.

## Part A: Core Facade (`Program.validate`)

### API Additions (`src/pyrung/core/program.py`)
```python
DialectValidator = Callable[..., Any]

class Program:
    _dialect_validators: ClassVar[dict[str, DialectValidator]]

    @classmethod
    def register_dialect(cls, name: str, validator: DialectValidator) -> None: ...
    @classmethod
    def registered_dialects(cls) -> tuple[str, ...]: ...
    def validate(self, dialect: str, *, mode: str = "warn", **kwargs: Any) -> Any: ...
```

Behavior:
1. No core import of `pyrung.click`.
2. Unknown dialect raises `KeyError` with available dialects and import hint.
3. Validator argument errors are surfaced (no swallowing).
4. Registry overwrite is allowed for identical function object, rejected for conflicting function (prevents accidental double-registration conflicts).

### Click Self-Registration (`src/pyrung/click/__init__.py`)
On import, Click registers itself:
- dialect name: `"click"`
- callback: wrapper around `validate_click_program(...)`
- wrapper enforces `tag_map` kwarg presence and type

## Part B: `pyclickplc` Hardware Capability Model (source of truth)

### New Module
- `../pyclickplc/src/pyclickplc/capabilities.py`

### Public Types
```python
InstructionRole = Literal[...]
CopyOperation = Literal[...]
@dataclass(frozen=True)
class BankCapability: ...
class ClickHardwareProfile:
    def is_writable(self, memory_type: str, address: int | None = None) -> bool: ...
    def valid_for_role(self, memory_type: str, role: InstructionRole) -> bool: ...
    def copy_compatible(
        self,
        operation: CopyOperation,
        source_type: str,
        dest_type: str,
    ) -> bool: ...
```

### Public Exports
- Add to `../pyclickplc/src/pyclickplc/__init__.py`:
  - `ClickHardwareProfile`
  - default instance, e.g. `CLICK_HARDWARE_PROFILE`
  - role/operation literals if exported as constants

### Data Policy
1. Capability data is static Click hardware reference data.
2. Keep Modbus writability and ladder-validation writability separate.
3. `ClickHardwareProfile` methods are pure and deterministic.
4. Unknown bank/role/operation raises `KeyError` (invalid caller contract).

## Part C: Pyrung Adapter + Extended Rules

### New Adapter Module
- `src/pyrung/click/profile.py`

### Protocol in Pyrung
```python
class HardwareProfile(Protocol):
    def is_writable(self, memory_type: str, address: int | None = None) -> bool: ...
    def valid_for_role(self, memory_type: str, role: str) -> bool: ...
    def copy_compatible(self, operation: str, source_type: str, dest_type: str) -> bool: ...
```

Adapter class wraps `pyclickplc.CLICK_HARDWARE_PROFILE` and isolates `pyrung` from upstream shape changes.

### Validator Signature (`src/pyrung/click/validation.py`)
```python
def validate_click_program(
    program: Program,
    tag_map: TagMap,
    mode: ValidationMode = "warn",
    profile: HardwareProfile | None = None,
) -> ClickValidationReport:
    ...
```

Profile behavior:
1. If `profile` provided, use it.
2. If `profile is None`, try default adapter.
3. If unavailable, emit `CLK_PROFILE_UNAVAILABLE` and skip R6-R8 (still run R1-R5).

### Finding Codes (Stage 3)
- `CLK_PROFILE_UNAVAILABLE`
- `CLK_BANK_UNRESOLVED`
- `CLK_BANK_NOT_WRITABLE`
- `CLK_BANK_WRONG_ROLE`
- `CLK_COPY_BANK_INCOMPATIBLE`

### Rule Definitions

#### R6: Writable Target
Apply to write targets except role-owned timer/counter fields checked by R7.

Target fields:
- `OutInstruction.target`
- `LatchInstruction.target`
- `ResetInstruction.target`
- `CopyInstruction.target`
- `BlockCopyInstruction.dest`
- `FillInstruction.dest`
- `CalcInstruction.dest`
- `SearchInstruction.result`
- `SearchInstruction.found`
- `ShiftInstruction.bit_range`
- `PackBitsInstruction.dest`
- `PackWordsInstruction.dest`
- `UnpackToBitsInstruction.bit_block`
- `UnpackToWordsInstruction.word_block`

For each resolved mapped slot: `profile.is_writable(memory_type, address)` must be true.

#### R7: Role Compatibility
Validate fixed instruction-role fields:
- `OnDelayInstruction.done_bit` -> `timer_done_bit`
- `OnDelayInstruction.accumulator` -> `timer_accumulator`
- `OnDelayInstruction.setpoint` (if Tag) -> `timer_setpoint`
- `OffDelayInstruction.done_bit` -> `timer_done_bit`
- `OffDelayInstruction.accumulator` -> `timer_accumulator`
- `OffDelayInstruction.setpoint` (if Tag) -> `timer_setpoint`
- `CountUpInstruction.done_bit` -> `counter_done_bit`
- `CountUpInstruction.accumulator` -> `counter_accumulator`
- `CountUpInstruction.setpoint` (if Tag) -> `counter_setpoint`
- `CountDownInstruction.done_bit` -> `counter_done_bit`
- `CountDownInstruction.accumulator` -> `counter_accumulator`
- `CountDownInstruction.setpoint` (if Tag) -> `counter_setpoint`

#### R8: Copy-Family Compatibility
Instruction-to-operation mapping:
- `CopyInstruction` -> `single`
- `BlockCopyInstruction` -> `block`
- `FillInstruction` -> `fill`
- `PackBitsInstruction` -> `pack_bits`
- `PackWordsInstruction` -> `pack_words`
- `UnpackToBitsInstruction` -> `unpack_bits`
- `UnpackToWordsInstruction` -> `unpack_words`

For each operation, resolve source/dest banks from mapped operands and require:
`profile.copy_compatible(operation, source_type, dest_type) == True`.

### Resolution Rules
1. Resolve logical tags through `TagMap` mappings.
2. If mapping is missing/ambiguous, emit `CLK_BANK_UNRESOLVED`.
3. Constants are excluded from bank-pair checks (R8 applies when both sides resolve to mapped banks).

### Severity Routing
- `CLK_PROFILE_UNAVAILABLE`: `warning` in `warn`, `error` in `strict`.
- Other R6-R8 findings: `hint` in `warn`, `error` in `strict` (matches Stage 2 strictness policy).

## Separation from `Program(strict=True)`
Document and test this contract:

1. `Program(strict=True)`:
   - build-time AST/DSL structural guard
   - raises immediately
2. `validate(..., mode="strict")`:
   - post-build portability validation
   - report-based, no default raising

No shared entry point. No cross-calling.

## Files to Add/Modify

### `pyclickplc`
1. Add `../pyclickplc/src/pyclickplc/capabilities.py`
2. Modify `../pyclickplc/src/pyclickplc/__init__.py` exports
3. Add `../pyclickplc/tests/test_capabilities.py`
4. Update docs (`../pyclickplc/README.md` and docs index/API pages)

### `pyrung`
1. Modify `src/pyrung/core/program.py` (facade registry + dispatch)
2. Add `src/pyrung/click/profile.py` (adapter + protocol)
3. Modify or add `src/pyrung/click/validation.py` (R6-R8 + profile handling)
4. Modify `src/pyrung/click/__init__.py` (self-registration and exports)
5. Modify `src/pyrung/click/tag_map.py` (`TagMap.validate(...)` profile passthrough)
6. Add tests:
   - `tests/core/test_program_validation_facade.py`
   - `tests/click/test_profile_adapter.py`
   - `tests/click/test_validation_stage3.py` (or extend Stage 2 validation tests)

## Test Cases and Scenarios

### `pyclickplc` tests
1. `is_writable` bank defaults and SC/SD subset addressing.
2. `valid_for_role` for each supported role, with negative cases.
3. `copy_compatible` for all operations with representative valid/invalid pairs.
4. Unknown bank/role/operation behavior.
5. Export availability from package root.

### `pyrung` core facade tests
1. Registry registration and discovery.
2. Unknown dialect error message.
3. Click registration occurs on `import pyrung.click`.
4. `Program.validate("click", ...)` dispatches correctly.
5. `Program(strict=True)` behavior unchanged by facade.

### `pyrung` click validator tests (R6-R8)
1. Non-writable target bank violation (e.g., write to X/XD/YD).
2. SC/SD address-aware writable subset pass/fail.
3. Timer role pass and fail (`done_bit`, `accumulator`, `setpoint`).
4. Counter role pass and fail (`done_bit`, `accumulator`, `setpoint`).
5. Copy-family compatibility pass/fail for each operation.
6. Unmapped/ambiguous tag mapping -> `CLK_BANK_UNRESOLVED`.
7. Missing profile -> `CLK_PROFILE_UNAVAILABLE`, R6-R8 skipped, R1-R5 still emitted.
8. `Program.validate(...dialect="click"... )` and `TagMap.validate(...)` parity.

## Acceptance Criteria
1. `Program.validate(dialect="click", mode=..., tag_map=...)` works without core importing Click.
2. Click self-registers on import and can be discovered via registry.
3. `pyclickplc` owns bank capability truth; `pyrung` only consumes through adapter protocol.
4. R6-R8 findings are deterministic with stable codes and location formatting.
5. Missing profile produces explicit finding, never silent downgrade.
6. `Program(strict=True)` and portability validation remain separate concerns.
7. New tests pass in both repos without changing runtime execution semantics.

## Assumptions and Defaults
1. Stage 1/2 implementation may lag Stage 3 planning; this plan remains implementation-ready with defined sequencing.
2. R8 checks mapped bank pairs only; literal constant source compatibility is out of scope in this stage.
3. SC/SD writable subsets use the validation profile constants (ladder semantics), not Modbus write FC capability.
4. No temporary hardcoded fallback profile is added in `pyrung`.


Additional conversation:

I recommend both: add compare compatibility and make bank-pair rules explicit in the plan. Compare rules are the same class of hardware portability constraint as copy compatibility, and explicit matrices remove ambiguity for implementation/testing.

R9 would be:

Rule target: compare conditions (==, !=, <, <=, >, >=) where operands are concrete tags/constants.
Check: operand compatibility using the Click data-compatibility matrix (data_compatibility.md).
Cases:
tag vs tag: validate bank-pair compatibility.
tag vs constant: validate bank vs constant-kind (int1, int2, float, hex, text).
Not in R9:
expression compares (already blocked by Stage 2 R4),
pointer compares (already blocked by Stage 2 R1/R3).
Capability API (pyclickplc): add explicit methods like compare_compatible(left_bank, right_bank) and compare_constant_compatible(bank, const_kind).
Findings: add stable code(s), e.g. CLK_COMPARE_INCOMPATIBLE.
Bank-pair rules should be explicitly listed in the plan for copy and compare so implementation does not infer from prose.

Yes to both.

Add compare_compatible in the capability API.
Make bank-pair rules explicit in the plan (not inferred from prose).
I’d add this to Stage 3 as R9:

R9: compare operand compatibility for compare contacts (==, !=, <, <=, >, >=).
tag vs tag: validate bank-pair via explicit compare matrix.
tag vs constant: validate bank vs constant-kind compatibility.
Skip expression/pointer compare paths since Stage 2 already disallows them.
Recommended additions:

Capability methods in pyclickplc:
compare_compatible(left_bank, right_bank) -> bool
compare_constant_compatible(bank, const_kind) -> bool
Finding code:
CLK_COMPARE_INCOMPATIBLE (or split constant/tag variants if you want finer reporting).
Tests:
valid numeric compare, valid hex compare, valid text compare
invalid cross-family compares (numeric vs hex, text vs numeric, etc.)
invalid constant kinds per bank.
For explicit bank-pair rules: yes, add literal tables/sets for each operation (single, block, fill, pack_bits, pack_words, unpack_bits, unpack_words, and compare) directly in the plan so implementation is decision-free.


Is it worth using names for Rules instead of R1, R2 etc? What is the convention here

Short answer: use both.

R1/R2 is fine for discussion order, but long-term convention is:

Stable machine IDs for enforcement/findings (CLK_PTR_CONTEXT_ONLY_COPY).
Human-readable rule names in docs (Pointer Context: copy-only).
Optional numeric alias for continuity (R1).
Pure numbers are hard to grep and get messy when inserting rules.
Pure names can drift.
Best pattern is code + title (+ legacy R# alias) and keep code as the canonical identifier in tests and reports.


I'd rather not have 'legacy'. We are still planning!


