"""Generic, policy-free program walker for operand/condition fact extraction.

Walks a Program object graph in deterministic order and emits normalized
OperandFact records describing every instruction argument and rung condition.

This module is dialect-agnostic: it makes no policy decisions about
allowed/disallowed usage and produces no severity levels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    Condition,
    FallingEdgeCondition,
    IndirectCompareEq,
    IndirectCompareGe,
    IndirectCompareGt,
    IndirectCompareLe,
    IndirectCompareLt,
    IndirectCompareNe,
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.expression import (
    ExprCompareEq,
    ExprCompareGe,
    ExprCompareGt,
    ExprCompareLe,
    ExprCompareLt,
    ExprCompareNe,
    Expression,
)
from pyrung.core.memory_block import (
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
)
from pyrung.core.tag import Tag

if TYPE_CHECKING:
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Instruction field extraction map (single source of truth)
# ---------------------------------------------------------------------------

_INSTRUCTION_FIELDS: dict[str, tuple[str, ...]] = {
    "OutInstruction": ("target",),
    "LatchInstruction": ("target",),
    "ResetInstruction": ("target",),
    "CopyInstruction": ("source", "target"),
    "BlockCopyInstruction": ("source", "dest"),
    "MathInstruction": ("expression", "dest"),
    "FillInstruction": ("value", "dest"),
    "SearchInstruction": ("value", "search_range", "condition", "result", "found"),
    "ShiftInstruction": (
        "bit_range",
        "data_condition",
        "clock_condition",
        "reset_condition",
    ),
    "PackBitsInstruction": ("bit_block", "dest"),
    "PackWordsInstruction": ("word_block", "dest"),
    "UnpackToBitsInstruction": ("source", "bit_block"),
    "UnpackToWordsInstruction": ("source", "word_block"),
    "CountUpInstruction": (
        "done_bit",
        "accumulator",
        "setpoint",
        "up_condition",
        "down_condition",
        "reset_condition",
    ),
    "CountDownInstruction": (
        "done_bit",
        "accumulator",
        "setpoint",
        "down_condition",
        "reset_condition",
    ),
    "OnDelayInstruction": (
        "done_bit",
        "accumulator",
        "setpoint",
        "enable_condition",
        "reset_condition",
    ),
    "OffDelayInstruction": (
        "done_bit",
        "accumulator",
        "setpoint",
        "enable_condition",
    ),
    "CallInstruction": ("subroutine_name",),
    "ReturnInstruction": (),
}

# ---------------------------------------------------------------------------
# Condition child extraction sets
# ---------------------------------------------------------------------------

_COMPARE_CONDITIONS: set[type] = {
    CompareEq,
    CompareNe,
    CompareLt,
    CompareLe,
    CompareGt,
    CompareGe,
}

_INDIRECT_COMPARE_CONDITIONS: set[type] = {
    IndirectCompareEq,
    IndirectCompareNe,
    IndirectCompareLt,
    IndirectCompareLe,
    IndirectCompareGt,
    IndirectCompareGe,
}

_EXPR_COMPARE_CONDITIONS: set[type] = {
    ExprCompareEq,
    ExprCompareNe,
    ExprCompareLt,
    ExprCompareLe,
    ExprCompareGt,
    ExprCompareGe,
}

_BIT_CONDITIONS: set[type] = {
    BitCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
    FallingEdgeCondition,
}


# ---------------------------------------------------------------------------
# Value classification
# ---------------------------------------------------------------------------


def _classify_value(
    obj: Any,
) -> tuple[ValueKind, str, str, dict[str, str | int | bool]]:
    """Classify a value by exact type checks in priority order.

    Returns (value_kind, value_type, summary, metadata).
    """
    # 1. IndirectExprRef (before IndirectRef — both are dataclasses, no subclass)
    if isinstance(obj, IndirectExprRef):
        return (
            "indirect_expr_ref",
            type(obj).__name__,
            f"IndirectExprRef({obj.block.name}[{type(obj.expr).__name__}])",
            {"block_name": obj.block.name, "expr_type": type(obj.expr).__name__},
        )

    # 2. IndirectRef
    if isinstance(obj, IndirectRef):
        return (
            "indirect_ref",
            type(obj).__name__,
            f"IndirectRef({obj.block.name}[{obj.pointer.name}])",
            {"block_name": obj.block.name, "pointer_name": obj.pointer.name},
        )

    # 3. Expression
    if isinstance(obj, Expression):
        return (
            "expression",
            type(obj).__name__,
            f"Expression({type(obj).__name__})",
            {"expr_type": type(obj).__name__},
        )

    # 4. IndirectBlockRange (before BlockRange)
    if isinstance(obj, IndirectBlockRange):
        return (
            "indirect_block_range",
            type(obj).__name__,
            f"IndirectBlockRange({obj.block.name})",
            {"block_name": obj.block.name},
        )

    # 5. BlockRange
    if isinstance(obj, BlockRange):
        return (
            "block_range",
            type(obj).__name__,
            f"BlockRange({obj.block.name}[{obj.start}:{obj.end}])",
            {"block_name": obj.block.name, "start": obj.start, "end": obj.end},
        )

    # 6. Condition (and recurse — handled by caller)
    if isinstance(obj, Condition):
        return (
            "condition",
            type(obj).__name__,
            f"Condition({type(obj).__name__})",
            {"condition_type": type(obj).__name__},
        )

    # 7. Tag
    if isinstance(obj, Tag):
        return (
            "tag",
            type(obj).__name__,
            f"Tag({obj.name}:{obj.type.name})",
            {"tag_name": obj.name, "tag_type": obj.type.name},
        )

    # 8. Literal scalars (bool before int since bool is subclass of int)
    if isinstance(obj, bool):
        return ("literal", "bool", repr(obj), {})
    if obj is None:
        return ("literal", "NoneType", "None", {})
    if isinstance(obj, (int, float, str)):
        return ("literal", type(obj).__name__, repr(obj), {})

    # 9. Unknown
    return (
        "unknown",
        type(obj).__name__,
        f"Unknown({type(obj).__name__})",
        {},
    )


# ---------------------------------------------------------------------------
# Condition child extraction
# ---------------------------------------------------------------------------


def _condition_children(cond: Condition) -> list[tuple[str, Any]]:
    """Return (child_name, child_value) pairs for a known Condition subclass.

    Unknown subclasses fall back to iterating public attributes in sorted order.
    """
    cond_type = type(cond)

    if cond_type in {AllCondition, AnyCondition}:
        return [(f"conditions[{i}]", child) for i, child in enumerate(cond.conditions)]

    if cond_type in _COMPARE_CONDITIONS:
        return [("tag", cond.tag), ("value", cond.value)]

    if cond_type in _INDIRECT_COMPARE_CONDITIONS:
        return [("indirect_ref", cond.indirect_ref), ("value", cond.value)]

    if cond_type in _EXPR_COMPARE_CONDITIONS:
        return [("left", cond.left), ("right", cond.right)]

    if cond_type in _BIT_CONDITIONS:
        return [("tag", cond.tag)]

    # Unknown condition: iterate public attributes in sorted key order
    children: list[tuple[str, Any]] = []
    for key in sorted(vars(cond)):
        if key.startswith("_"):
            continue
        val = getattr(cond, key)
        if isinstance(val, (list, tuple)):
            for i, item in enumerate(val):
                children.append((f"{key}[{i}]", item))
        else:
            children.append((key, val))
    return children


# ---------------------------------------------------------------------------
# Walker implementation
# ---------------------------------------------------------------------------


class _Walker:
    """Internal walker state — one instance per walk_program() call."""

    __slots__ = ("_facts", "_seen")

    def __init__(self) -> None:
        self._facts: list[OperandFact] = []
        self._seen: set[tuple[int, str]] = set()

    # -- public entry point ------------------------------------------------

    def walk(self, program: Program) -> ProgramFacts:
        # 1. Main rungs in list order
        for rung_index, rung in enumerate(program.rungs):
            self._walk_rung(rung, "main", None, rung_index, ())

        # 2. Subroutines in sorted name order
        for sub_name in sorted(program.subroutines):
            for rung_index, rung in enumerate(program.subroutines[sub_name]):
                self._walk_rung(rung, "subroutine", sub_name, rung_index, ())

        return ProgramFacts(operands=tuple(self._facts))

    # -- rung traversal ----------------------------------------------------

    def _walk_rung(
        self,
        rung: Rung,
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
    ) -> None:
        # Conditions first
        conditions = rung._conditions
        for cond_idx, cond in enumerate(conditions):
            cond_path = "condition" if len(conditions) == 1 else f"condition[{cond_idx}]"
            self._walk_value(
                cond, scope, subroutine, rung_index, branch_path, None, None, cond_path
            )

        # Instructions in order
        for instr_idx, instr in enumerate(rung._instructions):
            self._walk_instruction(instr, scope, subroutine, rung_index, branch_path, instr_idx)

        # Branches in list order (recursive)
        for branch_idx, branch_rung in enumerate(rung._branches):
            self._walk_rung(
                branch_rung,
                scope,
                subroutine,
                rung_index,
                branch_path + (branch_idx,),
            )

    # -- instruction traversal ---------------------------------------------

    def _walk_instruction(
        self,
        instr: Any,
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
        instr_idx: int,
    ) -> None:
        class_name = type(instr).__name__
        fields = _INSTRUCTION_FIELDS.get(class_name)

        if fields is None:
            # Unknown instruction — emit one unknown fact
            loc = ProgramLocation(
                scope=scope,
                subroutine=subroutine,
                rung_index=rung_index,
                branch_path=branch_path,
                instruction_index=instr_idx,
                instruction_type=class_name,
                arg_path="instruction",
            )
            self._facts.append(
                OperandFact(
                    location=loc,
                    value_kind="unknown",
                    value_type=class_name,
                    summary=f"Unknown({class_name})",
                    metadata={"class_name": class_name},
                )
            )
            return

        for field_name in fields:
            value = getattr(instr, field_name)
            self._walk_value(
                value,
                scope,
                subroutine,
                rung_index,
                branch_path,
                instr_idx,
                class_name,
                f"instruction.{field_name}",
            )

    # -- value classification + recursive descent --------------------------

    def _walk_value(
        self,
        obj: Any,
        scope: FactScope,
        subroutine: str | None,
        rung_index: int,
        branch_path: tuple[int, ...],
        instr_idx: int | None,
        instr_type: str | None,
        arg_path: str,
    ) -> None:
        # Cycle guard
        seen_key = (id(obj), arg_path)
        if seen_key in self._seen:
            return
        self._seen.add(seen_key)

        kind, value_type, summary, metadata = _classify_value(obj)

        loc = ProgramLocation(
            scope=scope,
            subroutine=subroutine,
            rung_index=rung_index,
            branch_path=branch_path,
            instruction_index=instr_idx,
            instruction_type=instr_type,
            arg_path=arg_path,
        )
        self._facts.append(
            OperandFact(
                location=loc,
                value_kind=kind,
                value_type=value_type,
                summary=summary,
                metadata=metadata,
            )
        )

        # Recurse into condition children
        if kind == "condition" and isinstance(obj, Condition):
            for child_name, child_val in _condition_children(obj):
                self._walk_value(
                    child_val,
                    scope,
                    subroutine,
                    rung_index,
                    branch_path,
                    instr_idx,
                    instr_type,
                    f"{arg_path}.{child_name}",
                )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def walk_program(program: Program) -> ProgramFacts:
    """Walk a Program and extract all operand/condition facts.

    Returns a ProgramFacts containing deterministic, ordered OperandFact tuples
    covering every instruction argument and rung condition in the program.

    This function is policy-free: it classifies values but makes no decisions
    about allowed/disallowed usage.
    """
    return _Walker().walk(program)
