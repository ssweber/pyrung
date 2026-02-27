# Stage 2: Click Portability Validator (Consumes Stage 1 Walker)

## Goal

Implement Click portability validation by consuming Stage 1 walker facts and applying policy rules:

- mode support: `warn` and `strict`,
- allow pointer (`IndirectRef`) only in `copy` source/target,
- pointer tag must be DS for allowed copy-pointer usage,
- allow math `Expression` only in `calc(expression, dest)`,
- disallow `IndirectBlockRange` (Click has no indirect block copy).

Stage 2 produces a report. It does not modify runtime execution semantics.

---

## Policy decisions locked for this stage

1. `warn` mode:
   - policy violations reported as hints.
2. `strict` mode:
   - same policy violations reported as errors.
3. Pointer scope:
   - allowed: `IndirectRef` only in `CopyInstruction.source` and `CopyInstruction.target`.
   - disallowed: `IndirectRef` in all other contexts (conditions, other instruction args).
4. Pointer bank:
   - allowed copy-pointer requires pointer tag in DS memory type.
5. Arithmetic pointer:
   - `IndirectExprRef` is disallowed everywhere (including copy).
6. Mathematics:
   - `Expression` allowed only in `CalcInstruction.expression`.
   - `Expression` disallowed in rung conditions and all other instruction args.
7. Indirect block range:
   - `IndirectBlockRange` is disallowed everywhere.
   - Click hardware does not support computed block ranges in block copy operations.

---

## Files to add

1. `src/pyrung/click/validation.py`
2. `tests/click/test_validation.py`

Files to modify:

1. `src/pyrung/click/tag_map.py` (add `validate(...)` method)
2. `src/pyrung/click/__init__.py` (export report types and/or validator entrypoint)

Do not alter core runtime execution files for this stage.

---

## Public interface (Stage 2)

## Report model

Define in `src/pyrung/click/validation.py`:

```python
from dataclasses import dataclass, field
from typing import Literal

ValidationMode = Literal["warn", "strict"]
FindingSeverity = Literal["error", "warning", "hint"]

@dataclass(frozen=True)
class ClickFinding:
    code: str
    severity: FindingSeverity
    message: str
    location: str
    suggestion: str | None = None

@dataclass(frozen=True)
class ClickValidationReport:
    errors: tuple[ClickFinding, ...] = field(default_factory=tuple)
    warnings: tuple[ClickFinding, ...] = field(default_factory=tuple)
    hints: tuple[ClickFinding, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        ...
```

## Entry points

In `validation.py`:

```python
def validate_click_program(program: Program, tag_map: TagMap, mode: ValidationMode = "warn") -> ClickValidationReport:
    ...
```

In `TagMap`:

```python
def validate(self, program: Program, mode: ValidationMode = "warn") -> ClickValidationReport:
    return validate_click_program(program, self, mode=mode)
```

Notes:

- Keep report return-only behavior (no raising by default).
- If future helper is needed, add separate `assert_valid_click(...)`, not part of this stage.

---

## Finding codes

Use stable code constants:

- `CLK_PTR_CONTEXT_ONLY_COPY`
- `CLK_PTR_POINTER_MUST_BE_DS`
- `CLK_PTR_EXPR_NOT_ALLOWED`
- `CLK_EXPR_ONLY_IN_CALC`
- `CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED`

Optional extra precision code if needed:

- `CLK_PTR_DS_UNVERIFIED` (pointer tag not resolvable to mapped DS; treated as violation in strict and hint in warn).

---

## Rule engine design

Consume `ProgramFacts` from Stage 1 walker and evaluate each `OperandFact`.

## Rule R1: `IndirectRef` context

If `value_kind == "indirect_ref"`:

- allowed only when:
  - `instruction_type == "CopyInstruction"`
  - `arg_path in {"instruction.source", "instruction.target"}`

Else emit `CLK_PTR_CONTEXT_ONLY_COPY`.

## Rule R2: DS pointer enforcement

For `IndirectRef` that passes R1:

- get pointer name from fact metadata (`pointer_name`),
- resolve pointer tag memory type using TagMap resolver strategy below,
- must resolve to `DS`.

Else emit `CLK_PTR_POINTER_MUST_BE_DS` (or `CLK_PTR_DS_UNVERIFIED` if unresolved).

## Rule R3: Indirect expression reference

If `value_kind == "indirect_expr_ref"`:

- always emit `CLK_PTR_EXPR_NOT_ALLOWED`.

## Rule R4: Expression context

If `value_kind == "expression"`:

- allowed only when:
  - `instruction_type == "CalcInstruction"`
  - `arg_path == "instruction.expression"`

Else emit `CLK_EXPR_ONLY_IN_CALC`.

### R4 end-to-end example: ExprCompare in rung condition

When a user writes:

```python
with Rung((A + B) > 100):
    out(Light)
```

The DSL creates an `ExprCompareGt` condition with `left=AddExpr(TagExpr(A), TagExpr(B))` and `right=LiteralExpr(100)`. The Stage 1 walker extracts two facts from the condition children:

- `arg_path="condition.left"`, `value_kind="expression"` (the `AddExpr`)
- `arg_path="condition.right"`, `value_kind="expression"` (the `LiteralExpr`)

R4 evaluates both facts. Neither has `instruction_type == "CalcInstruction"` (they are condition-level, `instruction_type` is `None`), so both emit `CLK_EXPR_ONLY_IN_CALC`. The suggestion directs the user to pre-compute into a temp tag via `calc()`.

## Rule R5: Indirect block range

If `value_kind == "indirect_block_range"`:

- always emit `CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED`.

Click hardware does not support computed block ranges. Block copy source and dest must use fixed `BlockRange` with literal start/end addresses.

---

## TagMap pointer-memory-type resolution strategy

Implement a helper in `validation.py`:

```python
def _resolve_pointer_memory_type(pointer_name: str, tag_map: TagMap) -> str | None:
    ...
```

Resolution uses `tag_map.mapped_slots()` exclusively (no string parsing of address prefixes):

1. Iterate `tag_map.mapped_slots()` once and build a `logical_name -> memory_type` lookup.
2. If `pointer_name` maps to exactly one `memory_type`, return it.
3. If `pointer_name` is ambiguous across slots (maps to multiple memory types), treat as unresolved (`None`).
4. If `pointer_name` is not found in any slot, treat as unresolved (`None`).

Rationale:

- `mapped_slots()` returns `MappedSlot` which has `logical_name` and `memory_type` directly — no parsing needed.
- If the user hasn't mapped the pointer tag, we cannot verify its bank — unresolved is the honest answer.

Strict mode handling of unresolved:

- unresolved pointer type is treated as violation.

Warn mode handling of unresolved:

- unresolved pointer type emits hint-level violation.

---

## Severity routing

Centralize severity routing in one function:

```python
def _route_severity(code: str, mode: ValidationMode) -> FindingSeverity:
    if mode == "strict":
        return "error"
    return "hint"
```

Stage 2 does not require warning-level findings for these policy checks. Keep warnings bucket available for future checks.

---

## Location string format

Convert Stage 1 `ProgramLocation` into a human-readable location string:

- main rung instruction:
  - `"main.rung[2].instruction[1](CopyInstruction).instruction.source"`
- branch:
  - append `.branch[0].branch[1]...`
- subroutine:
  - `"subroutine[init].rung[0]...."`

Deterministic formatting is required for stable test snapshots.

---

## Suggestion text requirements

Each finding should include concise actionable suggestion:

- `CLK_EXPR_ONLY_IN_CALC`:
  - `"Move expression into calc(expr, temp) and use temp in this context."`
- `CLK_PTR_CONTEXT_ONLY_COPY`:
  - `"Use direct tag addressing in this context; keep pointer usage in copy() only."`
- `CLK_PTR_POINTER_MUST_BE_DS`:
  - `"Use a DS tag as the pointer source for copy() addressing."`
- `CLK_PTR_EXPR_NOT_ALLOWED`:
  - `"Replace computed pointer arithmetic with DS pointer tag updated separately."`
- `CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED`:
  - `"Use a fixed BlockRange with literal start/end addresses for block copy operations."`

---

## Test plan (Stage 2)

Add `tests/click/test_validation.py`:

1. Allowed case:
   - `copy(DD[Pointer], Dest)` where `Pointer` maps to `DS...`.
   - report has zero errors/hints for these rules.
2. Non-DS pointer:
   - pointer tag mapped to non-DS bank.
   - `warn` -> hint `CLK_PTR_POINTER_MUST_BE_DS`.
   - `strict` -> error same code.
3. Pointer in condition:
   - `with Rung(DD[Pointer] > 5): ...`
   - violation `CLK_PTR_CONTEXT_ONLY_COPY`.
4. Pointer expression index:
   - `copy(DD[idx + 1], Dest)`.
   - violation `CLK_PTR_EXPR_NOT_ALLOWED`.
5. Inline expression in condition:
   - `with Rung((A + B) > 10): ...`
   - violation `CLK_EXPR_ONLY_IN_CALC`.
6. Inline expression in copy:
   - `copy(A * 2, Dest)`.
   - violation `CLK_EXPR_ONLY_IN_CALC`.
7. Expression in `calc()`:
   - `calc(A * 2, Dest)`.
   - no expression-context violation.
8. Unresolved pointer bank:
   - pointer name cannot be mapped/proven DS.
   - hint in warn, error in strict.
9. Indirect block range in block copy:
   - `block_copy(DD.select(Start, End), DD.select(100, 110))` where `Start`/`End` are tags.
   - violation `CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED`.
10. ExprCompare in condition (R4 end-to-end):
    - `with Rung((A + B) > 100): out(Light)`.
    - walker extracts `condition.left` and `condition.right` as `expression` facts.
    - both emit `CLK_EXPR_ONLY_IN_CALC` because they are not in `CalcInstruction.expression`.
11. Location formatting deterministic.
12. `TagMap.validate(program, mode)` delegates and returns `ClickValidationReport`.

---

## Acceptance criteria

- `TagMap.validate(program, mode)` exists and works in both modes.
- Stage 2 policy checks exactly enforce the locked decisions.
- Findings use stable codes and deterministic locations.
- Tests for both `warn` and `strict` pass.
- Existing core tests remain unchanged and passing.

---

## Non-goals and guardrails

- No change to core permissive execution.
- No automatic code rewriting.
- No exception-throwing default behavior.
- No implementation of broader `Program.validate(dialect=...)` in this stage.

---

## Follow-on (out of this stage)

See `scratchpad/click-validation-stage-3-facade-and-profile-plan.md` for:

- `Program.validate()` generic dialect facade (registry pattern, core never imports click)
- `ClickProfile` hardware bank capability model (writable, roles, copy compatibility)
- Extended rules R6–R8 (bank writability, instruction role constraints, copy compatibility)
- Separation from DSL strict mode (`Program(strict=True)` vs `validate(mode=...)`)


