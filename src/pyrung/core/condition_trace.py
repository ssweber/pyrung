"""Condition trace evaluation and expression rendering for debugger output."""

from __future__ import annotations

from functools import singledispatchmethod
from typing import Any

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
    FallingEdgeCondition,
    IndirectCompareEq,
    IndirectCompareGe,
    IndirectCompareGt,
    IndirectCompareLe,
    IndirectCompareLt,
    IndirectCompareNe,
    IntTruthyCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.context import ScanContext
from pyrung.core.expression import (
    ExprCompareEq,
    ExprCompareGe,
    ExprCompareGt,
    ExprCompareLe,
    ExprCompareLt,
    ExprCompareNe,
    Expression,
)
from pyrung.core.memory_block import IndirectExprRef, IndirectRef
from pyrung.core.tag import Tag
from pyrung.core.trace_formatter import TraceFormatter

_DIRECT_COMPARE_OPERATOR_BY_CLASS_NAME: dict[str, str] = {
    "CompareEq": "==",
    "CompareNe": "!=",
    "CompareLt": "<",
    "CompareLe": "<=",
    "CompareGt": ">",
    "CompareGe": ">=",
}
_INDIRECT_COMPARE_OPERATOR_BY_CLASS_NAME: dict[str, str] = {
    "IndirectCompareEq": "==",
    "IndirectCompareNe": "!=",
    "IndirectCompareLt": "<",
    "IndirectCompareLe": "<=",
    "IndirectCompareGt": ">",
    "IndirectCompareGe": ">=",
}
_EXPR_COMPARE_OPERATOR_BY_CLASS_NAME: dict[str, str] = {
    "ExprCompareEq": "==",
    "ExprCompareNe": "!=",
    "ExprCompareLt": "<",
    "ExprCompareLe": "<=",
    "ExprCompareGt": ">",
    "ExprCompareGe": ">=",
}


class ConditionTraceEngine:
    """Evaluate conditions and build stable trace payloads."""

    def __init__(self, *, formatter: TraceFormatter | None = None) -> None:
        self._formatter = formatter if formatter is not None else TraceFormatter()

    @staticmethod
    def _detail(name: str, value: Any) -> dict[str, Any]:
        return {"name": name, "value": value}

    @staticmethod
    def _resolve_operand(value: Any, ctx: ScanContext) -> Any:
        if isinstance(value, Expression):
            return value.evaluate(ctx)
        if isinstance(value, Tag):
            return ctx.get_tag(value.name, value.default)
        if isinstance(value, (IndirectRef, IndirectExprRef)):
            target = value.resolve_ctx(ctx)
            return ctx.get_tag(target.name, target.default)
        return value

    def _right_operand_details(self, right_operand: Any, ctx: ScanContext) -> list[dict[str, Any]]:
        right_details: list[dict[str, Any]] = []
        if isinstance(right_operand, Tag):
            right_details.append(self._detail("right", right_operand.name))
            return right_details
        if isinstance(right_operand, IndirectRef):
            right_target = right_operand.resolve_ctx(ctx)
            right_pointer_name = right_operand.pointer.name
            right_pointer_value = ctx.get_tag(right_pointer_name, right_operand.pointer.default)
            right_details.extend(
                [
                    self._detail("right", right_target.name),
                    self._detail(
                        "right_pointer_expr",
                        f"{right_operand.block.name}[{right_pointer_name}]",
                    ),
                    self._detail("right_pointer", right_pointer_name),
                    self._detail("right_pointer_value", right_pointer_value),
                ]
            )
            return right_details
        if isinstance(right_operand, IndirectExprRef):
            # Collapse expression refs to concrete resolved tag labels (for concise trace display).
            right_target = right_operand.resolve_ctx(ctx)
            right_details.append(self._detail("right", right_target.name))
            return right_details
        if isinstance(right_operand, Expression):
            right_details.append(self._detail("right", repr(right_operand)))
        return right_details

    def evaluate(self, condition: Any, ctx: ScanContext) -> tuple[bool, list[dict[str, Any]]]:
        """Return condition value with trace details for debugger display."""
        return self._evaluate(condition, ctx)

    @singledispatchmethod
    def _evaluate(self, condition: Any, ctx: ScanContext) -> tuple[bool, list[dict[str, Any]]]:
        value = bool(condition.evaluate(ctx))
        return value, []

    @_evaluate.register
    def _(self, condition: BitCondition, ctx: ScanContext) -> tuple[bool, list[dict[str, Any]]]:
        value = bool(ctx.get_tag(condition.tag.name, False))
        return value, [self._detail("tag", condition.tag.name), self._detail("value", value)]

    @_evaluate.register
    def _(
        self, condition: IntTruthyCondition, ctx: ScanContext
    ) -> tuple[bool, list[dict[str, Any]]]:
        raw = ctx.get_tag(condition.tag.name, condition.tag.default)
        value = int(raw) != 0
        return value, [self._detail("tag", condition.tag.name), self._detail("value", raw)]

    @_evaluate.register
    def _(
        self, condition: NormallyClosedCondition, ctx: ScanContext
    ) -> tuple[bool, list[dict[str, Any]]]:
        raw = bool(ctx.get_tag(condition.tag.name, False))
        value = not raw
        return value, [self._detail("tag", condition.tag.name), self._detail("value", raw)]

    @_evaluate.register
    def _(
        self, condition: RisingEdgeCondition, ctx: ScanContext
    ) -> tuple[bool, list[dict[str, Any]]]:
        current = bool(ctx.get_tag(condition.tag.name, False))
        previous = bool(ctx.get_memory(f"_prev:{condition.tag.name}", False))
        value = current and not previous
        return value, [
            self._detail("tag", condition.tag.name),
            self._detail("current", current),
            self._detail("previous", previous),
        ]

    @_evaluate.register
    def _(
        self, condition: FallingEdgeCondition, ctx: ScanContext
    ) -> tuple[bool, list[dict[str, Any]]]:
        current = bool(ctx.get_tag(condition.tag.name, False))
        previous = bool(ctx.get_memory(f"_prev:{condition.tag.name}", False))
        value = (not current) and previous
        return value, [
            self._detail("tag", condition.tag.name),
            self._detail("current", current),
            self._detail("previous", previous),
        ]

    @_evaluate.register(CompareEq)
    @_evaluate.register(CompareNe)
    @_evaluate.register(CompareLt)
    @_evaluate.register(CompareLe)
    @_evaluate.register(CompareGt)
    @_evaluate.register(CompareGe)
    def _evaluate_direct_compare(
        self, condition: Any, ctx: ScanContext
    ) -> tuple[bool, list[dict[str, Any]]]:
        left_label = condition.tag.name
        left_value = ctx.get_tag(condition.tag.name, condition.tag.default)
        right_details = self._right_operand_details(condition.value, ctx)
        right_value = self._resolve_operand(condition.value, ctx)
        value = bool(condition.evaluate(ctx))
        return value, [
            self._detail("left", left_label),
            self._detail("left_value", left_value),
            self._detail("right_value", right_value),
            *right_details,
        ]

    @_evaluate.register(IndirectCompareEq)
    @_evaluate.register(IndirectCompareNe)
    @_evaluate.register(IndirectCompareLt)
    @_evaluate.register(IndirectCompareLe)
    @_evaluate.register(IndirectCompareGt)
    @_evaluate.register(IndirectCompareGe)
    def _evaluate_indirect_compare(
        self, condition: Any, ctx: ScanContext
    ) -> tuple[bool, list[dict[str, Any]]]:
        target = condition.indirect_ref.resolve_ctx(ctx)
        left_label = target.name
        left_value = ctx.get_tag(target.name, target.default)
        pointer_name = condition.indirect_ref.pointer.name
        pointer_value = ctx.get_tag(pointer_name, condition.indirect_ref.pointer.default)
        extra_details = [
            self._detail(
                "left_pointer_expr", f"{condition.indirect_ref.block.name}[{pointer_name}]"
            ),
            self._detail("left_pointer", pointer_name),
            self._detail("left_pointer_value", pointer_value),
        ]
        right_details = self._right_operand_details(condition.value, ctx)
        right_value = self._resolve_operand(condition.value, ctx)
        value = bool(condition.evaluate(ctx))
        return value, [
            self._detail("left", left_label),
            self._detail("left_value", left_value),
            self._detail("right_value", right_value),
            *extra_details,
            *right_details,
        ]

    @_evaluate.register(ExprCompareEq)
    @_evaluate.register(ExprCompareNe)
    @_evaluate.register(ExprCompareLt)
    @_evaluate.register(ExprCompareLe)
    @_evaluate.register(ExprCompareGt)
    @_evaluate.register(ExprCompareGe)
    def _evaluate_expr_compare(
        self, condition: Any, ctx: ScanContext
    ) -> tuple[bool, list[dict[str, Any]]]:
        left_value = condition.left.evaluate(ctx)
        right_value = condition.right.evaluate(ctx)
        value = bool(condition.evaluate(ctx))
        return value, [
            self._detail("left", repr(condition.left)),
            self._detail("left_value", left_value),
            self._detail("right", repr(condition.right)),
            self._detail("right_value", right_value),
        ]

    @_evaluate.register
    def _(self, condition: AllCondition, ctx: ScanContext) -> tuple[bool, list[dict[str, Any]]]:
        child_results: list[str] = []
        result = True
        for idx, child in enumerate(condition.conditions):
            child_result, child_details = self.evaluate(child, ctx)
            child_text = self.summary(child, child_details)
            child_results.append(f"{child_text}({str(child_result).lower()})")
            if not child_result:
                result = False
                for skipped in condition.conditions[idx + 1 :]:
                    child_results.append(f"{self.expression(skipped)}(skipped)")
                break
        return result, [self._detail("terms", " & ".join(child_results))]

    @_evaluate.register
    def _(self, condition: AnyCondition, ctx: ScanContext) -> tuple[bool, list[dict[str, Any]]]:
        child_results: list[str] = []
        result = False
        for idx, child in enumerate(condition.conditions):
            child_result, child_details = self.evaluate(child, ctx)
            child_text = self.summary(child, child_details)
            child_results.append(f"{child_text}({str(child_result).lower()})")
            if child_result:
                result = True
                for skipped in condition.conditions[idx + 1 :]:
                    child_results.append(f"{self.expression(skipped)}(skipped)")
                break
        return result, [self._detail("terms", " | ".join(child_results))]

    @staticmethod
    def _value_text(value: Any) -> str:
        if isinstance(value, Tag):
            return value.name
        if isinstance(value, IndirectRef):
            return f"{value.block.name}[{value.pointer.name}]"
        if isinstance(value, IndirectExprRef):
            return f"{value.block.name}[{value.expr!r}]"
        return repr(value)

    @staticmethod
    def _indirect_ref_text(value: IndirectRef) -> str:
        return f"{value.block.name}[{value.pointer.name}]"

    def expression(self, condition: Any) -> str:
        """Render a stable, user-facing condition expression."""
        return self._expression(condition)

    @singledispatchmethod
    def _expression(self, condition: Any) -> str:
        return condition.__class__.__name__

    @_expression.register
    def _(self, condition: BitCondition) -> str:
        return condition.tag.name

    @_expression.register
    def _(self, condition: IntTruthyCondition) -> str:
        return f"{condition.tag.name} != 0"

    @_expression.register
    def _(self, condition: NormallyClosedCondition) -> str:
        return f"!{condition.tag.name}"

    @_expression.register
    def _(self, condition: RisingEdgeCondition) -> str:
        return f"rise({condition.tag.name})"

    @_expression.register
    def _(self, condition: FallingEdgeCondition) -> str:
        return f"fall({condition.tag.name})"

    @_expression.register(CompareEq)
    @_expression.register(CompareNe)
    @_expression.register(CompareLt)
    @_expression.register(CompareLe)
    @_expression.register(CompareGt)
    @_expression.register(CompareGe)
    def _expression_direct_compare(self, condition: Any) -> str:
        op = _DIRECT_COMPARE_OPERATOR_BY_CLASS_NAME[type(condition).__name__]
        return f"{condition.tag.name} {op} {self._value_text(condition.value)}"

    @_expression.register(IndirectCompareEq)
    @_expression.register(IndirectCompareNe)
    @_expression.register(IndirectCompareLt)
    @_expression.register(IndirectCompareLe)
    @_expression.register(IndirectCompareGt)
    @_expression.register(IndirectCompareGe)
    def _expression_indirect_compare(self, condition: Any) -> str:
        op = _INDIRECT_COMPARE_OPERATOR_BY_CLASS_NAME[type(condition).__name__]
        return (
            f"{self._indirect_ref_text(condition.indirect_ref)} "
            f"{op} {self._value_text(condition.value)}"
        )

    @_expression.register(ExprCompareEq)
    @_expression.register(ExprCompareNe)
    @_expression.register(ExprCompareLt)
    @_expression.register(ExprCompareLe)
    @_expression.register(ExprCompareGt)
    @_expression.register(ExprCompareGe)
    def _expression_expr_compare(self, condition: Any) -> str:
        op = _EXPR_COMPARE_OPERATOR_BY_CLASS_NAME[type(condition).__name__]
        return f"{condition.left!r} {op} {condition.right!r}"

    @_expression.register
    def _(self, condition: AllCondition) -> str:
        terms = " & ".join(self.expression(child) for child in condition.conditions)
        return f"({terms})"

    @_expression.register
    def _(self, condition: AnyCondition) -> str:
        terms = " | ".join(self.expression(child) for child in condition.conditions)
        return f"({terms})"

    def summary(self, condition: Any, details: list[dict[str, Any]]) -> str:
        """Render summary text combining expression and details."""
        expression = self.expression(condition)
        return self._formatter.condition_term_text(expression=expression, details=details)

    def annotation(self, *, status: str, expression: str, summary: str) -> str:
        """Render compact status annotation used by debugger traces."""
        return self._formatter.condition_annotation(
            status=status,
            expression=expression,
            summary=summary,
        )
