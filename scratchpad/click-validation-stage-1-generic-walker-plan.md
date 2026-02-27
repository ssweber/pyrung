# Stage 1: Generic Operand/Condition Walker (Dialect-Agnostic)

## Goal

Build a single, core-level walker that inspects a `Program` and emits normalized facts about:

- instruction arguments (operands),
- rung/branch/subroutine conditions,
- type of value used (`Tag`, `IndirectRef`, `IndirectExprRef`, `Expression`, etc.),
- exact location in program structure.

Stage 1 must be policy-free. It does not decide allowed/disallowed usage and does not produce warn/strict severity.

---

## Why this stage exists

This stage centralizes traversal and extraction once so dialect validators (Click now, others later) do not duplicate:

- rung/branch/subroutine traversal,
- instruction arg introspection,
- condition tree walking,
- expression and indirect reference detection.

---

## Scope

In scope:

- Add a new core walker module and data model.
- Add tests proving deterministic fact extraction.
- Keep walker independent of `pyrung.click`, `TagMap`, and any dialect logic.

Out of scope:

- No click-specific checks.
- No `warn` vs `strict`.
- No validation report severity.
- No runtime behavior changes in execution engine.

---

## Files to add

1. `src/pyrung/core/validation/__init__.py`
2. `src/pyrung/core/validation/walker.py`
3. `tests/core/test_validation_walker.py`

No modifications to instruction execution, memory resolution, or existing tests beyond adding the new walker tests.

---

## Public interface (Stage 1)

Expose these from `src/pyrung/core/validation/walker.py`:

```python
from dataclasses import dataclass
from typing import Literal

ValueKind = Literal[
    "tag",
    "indirect_ref",
    "indirect_expr_ref",
    "expression",
    "block_range",
    "indirect_block_range",
    "condition",
    "literal",
    "unknown",
]

FactScope = Literal["main", "subroutine"]

@dataclass(frozen=True)
class ProgramLocation:
    scope: FactScope
    subroutine: str | None
    rung_index: int
    branch_path: tuple[int, ...]
    instruction_index: int | None
    instruction_type: str | None
    arg_path: str

@dataclass(frozen=True)
class OperandFact:
    location: ProgramLocation
    value_kind: ValueKind
    value_type: str
    summary: str
    metadata: dict[str, str | int | bool]

@dataclass(frozen=True)
class ProgramFacts:
    operands: tuple[OperandFact, ...]

def walk_program(program: Program) -> ProgramFacts:
    ...
```

Notes:

- `summary` is a short deterministic string for debugging (`repr`-safe, no memory ids).
- `metadata` carries normalized details used by policies later.
- `arg_path` is dotted path-like context (examples below).

---

## Required `arg_path` conventions

Use these exact path conventions:

- instruction args: `"instruction.<field_name>"`
  - Example: `instruction.source`, `instruction.target`, `instruction.expression`.
- rung conditions: `"condition"`
- nested condition fields: append path segments:
  - Example: `condition.left`, `condition.right`, `condition.conditions[0]`.

This keeps Stage 2 rule matching trivial and avoids string parsing ambiguity.

---

## Traversal contract

## Program traversal

Walk in deterministic order:

1. `program.rungs` in list order (`scope="main"`, `subroutine=None`).
2. `program.subroutines` in sorted subroutine-name order, each rung list order (`scope="subroutine"`).

## Branch traversal

For each rung, recursively walk `_branches` in list order.

- Root rung `branch_path=()`.
- First nested branch is `(0,)`, next `(1,)`.
- Nested within first branch -> `(0, 0)`, etc.

## Instruction traversal

For each rung/branch:

- Walk `_conditions` first.
- Walk `_instructions` in order, with `instruction_index`.

---

## Instruction argument extraction map

Use an explicit map in walker (single source of truth):

- `OutInstruction`: `target`
- `LatchInstruction`: `target`
- `ResetInstruction`: `target`
- `CopyInstruction`: `source`, `target`
- `BlockCopyInstruction`: `source`, `dest`
- `CalcInstruction`: `expression`, `dest`
- `FillInstruction`: `value`, `dest`
- `SearchInstruction`: `value`, `search_range`, `operator`, `result`, `found`
- `ShiftInstruction`: `bit_range`, `data_condition`, `clock_condition`, `reset_condition`
- `PackBitsInstruction`: `bit_block`, `dest`
- `PackWordsInstruction`: `word_block`, `dest`
- `UnpackToBitsInstruction`: `source`, `bit_block`
- `UnpackToWordsInstruction`: `source`, `word_block`
- `CountUpInstruction`: `done_bit`, `accumulator`, `setpoint`, `up_condition`, `down_condition`, `reset_condition`
- `CountDownInstruction`: `done_bit`, `accumulator`, `setpoint`, `down_condition`, `reset_condition`
- `OnDelayInstruction`: `done_bit`, `timer_tag`, `setpoint`, `enable_condition`, `reset_condition`
- `OffDelayInstruction`: `done_bit`, `timer_tag`, `setpoint`, `enable_condition`
- `CallInstruction`: `subroutine_name`
- `ReturnInstruction`: (no operands — emit zero facts, not an unknown)

If a class is not listed, emit one `unknown` operand fact at `instruction` level with class name in metadata. Do not fail.

### Explicitly excluded fields

The following fields exist on instruction classes but are **not** extracted as operands:

- `oneshot` (bool on `OutInstruction`, `CopyInstruction`, `CalcInstruction`, `FillInstruction`,
  `BlockCopyInstruction`, `SearchInstruction`, `PackBitsInstruction`, `PackWordsInstruction`,
  `UnpackToBitsInstruction`, `UnpackToWordsInstruction`): execution modifier, not a data operand.
- `CallInstruction._program` (back-reference to parent `Program`): internal wiring, not user-facing.

---

## Condition child extraction map

Use explicit condition child mapping:

- `AllCondition`, `AnyCondition`: `conditions` (iterate with index)
- `CompareEq`, `CompareNe`, `CompareLt`, `CompareLe`, `CompareGt`, `CompareGe`: `tag`, `value`
- `IndirectCompareEq`, `IndirectCompareNe`, `IndirectCompareLt`, `IndirectCompareLe`, `IndirectCompareGt`, `IndirectCompareGe`: `indirect_ref`, `value`
- `ExprCompareEq`, `ExprCompareNe`, `ExprCompareLt`, `ExprCompareLe`, `ExprCompareGt`, `ExprCompareGe`: `left`, `right`
- `BitCondition`, `NormallyClosedCondition`, `RisingEdgeCondition`, `FallingEdgeCondition`: `tag`

Unknown `Condition` subclass:

- iterate public attributes from `vars(obj)` in sorted key order,
- recurse through supported value containers (`list`, `tuple`),
- mark unknown elements as `unknown`.

---

## Value classification rules

Classify by exact type checks in this order:

1. `IndirectExprRef` -> `indirect_expr_ref`
2. `IndirectRef` -> `indirect_ref`
3. `Expression` -> `expression`
4. `IndirectBlockRange` -> `indirect_block_range`
5. `BlockRange` -> `block_range`
6. `Condition` -> `condition` (and recurse using condition map)
7. `Tag` -> `tag`
8. literal scalars (`int`, `float`, `str`, `bool`, `None`) -> `literal`
9. anything else -> `unknown`

Metadata requirements per kind:

- `indirect_ref`:
  - `block_name`
  - `pointer_name`
- `indirect_expr_ref`:
  - `block_name`
  - `expr_type`
- `expression`:
  - `expr_type`
- `tag`:
  - `tag_name`
  - `tag_type`
- `block_range`:
  - `block_name`
  - `start`
  - `end`
- `indirect_block_range`:
  - `block_name`
- `condition`:
  - `condition_type`

---

## Determinism requirements

Facts must be stable across runs for same program object graph:

- deterministic traversal order,
- deterministic `summary` content without object ids,
- deterministic unknown attribute iteration (`sorted` keys).

---

## Error handling requirements

- Walker must never raise for unknown instruction/condition/value types.
- Walker returns `unknown` facts for unsupported nodes.
- Internal recursion must guard against cycles:
  - use a `seen` set keyed by `(id(obj), current_path)` to prevent infinite loops.

---

## Test plan (Stage 1)

Add `tests/core/test_validation_walker.py` with these cases:

1. `copy(DS[1] * 2, Result)` produces:
   - `instruction.source` fact as `expression`,
   - child expression details discoverable via condition/operand recursion.
2. `copy(DD[Index], DD[Dst])` produces `indirect_ref` for source and target.
3. `copy(DD[idx + 1], Result)` produces `indirect_expr_ref`.
4. `with Rung((A + B) > 100): ...` captures expression facts under `condition...`.
5. Branch path correctness:
   - root rung `()`,
   - nested branches get tuple indexes.
6. Subroutine coverage:
   - facts from subroutine scope include `scope="subroutine"` and subroutine name.
7. Deterministic ordering:
   - repeated `walk_program` calls return equal tuples.
8. Unknown object resilience:
   - custom instruction with nonstandard fields yields `unknown` fact, no exception.

---

## Acceptance criteria

- `walk_program` exists and returns `ProgramFacts`.
- Stage 1 tests pass without modifying existing core tests.
- No click imports in Stage 1 module.
- No policy decisions encoded in Stage 1.
- Existing runtime behavior remains unchanged.

---

## Non-goals and guardrails

- Do not introduce severity levels in Stage 1.
- Do not couple to TagMap or hardware addresses.
- Do not perform any remediation suggestions in Stage 1.
- Do not alter `Program`, `Rung`, or instruction semantics.


