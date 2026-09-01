"""Definite arithmetic-fault validation for calc instructions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.condition import CompareNe
from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr, UnaryExpr
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.control import ForLoopInstruction
from pyrung.core.validation._common import (
    RungLoc,
    _conjunction_satisfiable,
    iter_rungs,
)
from pyrung.core.validation.display import FindingDisplay, Frame, _FindingTextMixin
from pyrung.core.validation.render import caret_of, operand_name, render_expr, render_instruction
from pyrung.core.validation.severity import Severity

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pyrung.core.analysis.return_guards import ReachChain
    from pyrung.core.program import Program
    from pyrung.core.validation.context import ValidationContext


MATH_DIV_ZERO = "MATH_DIV_ZERO"


@dataclass(frozen=True)
class MathConditionFinding(_FindingTextMixin):
    code: str
    target_name: str
    display: FindingDisplay
    severity: Severity = "error"

    @property
    def message(self) -> str:
        return self.display.as_text()


@dataclass(frozen=True)
class MathConditionReport:
    findings: tuple[MathConditionFinding, ...]

    def summary(self) -> str:
        if not self.findings:
            return "No math-condition findings."
        return f"{len(self.findings)} math-condition finding(s)."


def _division_nodes(value: Any) -> Iterator[BinaryExpr]:
    if isinstance(value, BinaryExpr):
        yield from _division_nodes(value.left)
        yield from _division_nodes(value.right)
        if value.symbol in ("/", "//", "%"):
            yield value
    elif isinstance(value, UnaryExpr):
        yield from _division_nodes(value.operand)


def _constant_value(value: Any) -> int | float | None:
    if isinstance(value, LiteralExpr):
        raw = value.value
        return raw if isinstance(raw, (int, float)) else None
    if isinstance(value, UnaryExpr):
        operand = _constant_value(value.operand)
        if operand is None:
            return None
        try:
            result = value.op(operand)
        except (ArithmeticError, TypeError, ValueError):
            return None
        return result if isinstance(result, (int, float)) else None
    if isinstance(value, BinaryExpr):
        left = _constant_value(value.left)
        right = _constant_value(value.right)
        if left is None or right is None:
            return None
        try:
            result = value.op(left, right)
        except (ArithmeticError, TypeError, ValueError):
            return None
        return result if isinstance(result, (int, float)) else None
    return None


def _definitely_zero(
    denominator: Any,
    chains: tuple[ReachChain, ...],
    domains: dict[str, set[int | float]],
    *,
    guards_stable: bool,
) -> bool:
    live_chains = tuple(
        chain.conditions for chain in chains if _conjunction_satisfiable(chain.conditions, domains)
    )
    stable_live_chains = tuple(
        chain.conditions
        for chain in chains
        if chain.return_stable and _conjunction_satisfiable(chain.conditions, domains)
    )
    if not stable_live_chains:
        return False

    constant = _constant_value(denominator)
    if constant is not None:
        return constant == 0

    if not isinstance(denominator, TagExpr):
        return False
    tag = denominator.tag
    domain = domains.get(tag.name)
    if domain is None:
        return False
    if domain and all(value == 0 for value in domain):
        return True
    if not guards_stable:
        return False
    return all(
        not _conjunction_satisfiable((*chain, CompareNe(tag, 0)), domains) for chain in live_chains
    )


def _display(loc: RungLoc, instr: CalcInstruction, denominator: Any) -> FindingDisplay:
    dest = operand_name(instr.dest)
    code, _ = render_instruction(instr, dest)
    token = render_expr(denominator)
    span = caret_of(code, token)
    return FindingDisplay(
        code=MATH_DIV_ZERO,
        severity="error",
        frames=(
            Frame(
                location=loc.compact,
                lines=(code,),
                caret=(0, span[0], span[1]) if span else None,
                caret_label="divisor is always 0" if span else "",
            ),
        ),
        hint="make the divisor nonzero before this calc executes",
    )


def _calc_instructions(instructions: list[Any]) -> Iterator[CalcInstruction]:
    for instr in instructions:
        if isinstance(instr, CalcInstruction):
            yield instr
        elif isinstance(instr, ForLoopInstruction):
            yield from _calc_instructions(instr.instructions)


def validate_math_conditions(
    program: Program,
    *,
    _context: ValidationContext | None = None,
) -> MathConditionReport:
    """Report only calc divisors proved zero on every executable call path."""
    from pyrung.core.analysis.return_guards import effective_reach_chains
    from pyrung.core.validation.context import ValidationContext

    context = _context or ValidationContext(program)
    graph = context.graph
    raw_domains = context.closed_domains
    domains = {
        name: set(values)
        for name, values in raw_domains.items()
        if all(isinstance(value, (int, float)) for value in values)
    }
    reach = context.scope_reach_chains
    findings: list[MathConditionFinding] = []

    for loc, rung in iter_rungs(program):
        node = graph.rung_node(
            scope=loc.scope,
            subroutine=loc.subroutine,
            rung_index=loc.rung_index,
            branch_path=loc.branch_path,
        )
        if node is None:
            continue
        chains = effective_reach_chains(program, graph, node, scope_chains=reach)
        for instr in _calc_instructions(rung._instructions):
            for division in _division_nodes(instr.expression):
                guards_stable = not (
                    isinstance(division.right, TagExpr)
                    and graph.writers_of.get(division.right.tag.name)
                )
                if not _definitely_zero(
                    division.right,
                    chains,
                    domains,
                    guards_stable=guards_stable,
                ):
                    continue
                findings.append(
                    MathConditionFinding(
                        code=MATH_DIV_ZERO,
                        target_name=operand_name(instr.dest),
                        display=_display(loc, instr, division.right),
                    )
                )

    return MathConditionReport(findings=tuple(findings))


__all__ = [
    "MATH_DIV_ZERO",
    "MathConditionFinding",
    "MathConditionReport",
    "validate_math_conditions",
]
