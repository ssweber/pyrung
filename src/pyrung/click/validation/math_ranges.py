"""pyrung's check of the ranges in a math() formula.

laddercodec's Math checks the rest of math(), in the family module
:mod:`pyrung.click.validation.families.math`. This check stays in pyrung because only pyrung
knows which tags a range holds. The export writes ``SUM ( first : last )`` from the
addresses of the first and last tag, and laddercodec sees only those two ends. CLICK reads
every address between them, so the tags in between must map to those addresses.
"""

from __future__ import annotations

from typing import Any

from pyclickplc.addresses import format_address_display

from pyrung.core.expression import Expression, RangeAggregateExpr, SumExpr
from pyrung.core.memory_block import BlockRange
from pyrung.core.validation.render import operand_name
from pyrung.core.validation.walker import ProgramLocation

from .families.math import formula_expression
from .findings import (
    ClickFinding,
    ValidationMode,
    _route_severity,
    range_aggregate_advice,
    range_aggregate_problem,
    range_not_consecutive_text,
)
from .resolve import (
    _format_location,
    _instruction_location,
    _ResolutionCache,
)


def _collect_range_aggregates(expr: Any, out: list[tuple[str, BlockRange]], seen: set[int]) -> None:
    if id(expr) in seen or not isinstance(expr, Expression):
        return
    seen.add(id(expr))
    if isinstance(expr, SumExpr):
        out.append(("sum", expr.block_range))
        return
    if isinstance(expr, RangeAggregateExpr):
        out.append((expr.kind, expr.block_range))
        return
    for child in vars(expr).values():
        _collect_range_aggregates(child, out, seen)


def _evaluate_range_aggregates(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """Report each math() range whose tags CLICK cannot read as one range.

    The codes are CLK_MATH_RANGE_NOT_CONTIGUOUS and CLK_MATH_RANGE_MIXED_BANKS.
    """
    if type(instruction).__name__ != "CalcInstruction":
        return []
    aggregates: list[tuple[str, BlockRange]] = []
    _collect_range_aggregates(formula_expression(instruction.source_expression), aggregates, set())
    if not aggregates:
        return []

    location_text = _format_location(_instruction_location(base_location, "instruction.expression"))
    findings: list[ClickFinding] = []
    for kind, block_range in aggregates:
        tags = block_range.tags()
        resolved: list[tuple[str, int]] = []
        for tag in tags:
            slot = resolver.resolve_direct_tag(tag)
            if slot is None or slot.address is None:
                # The family reports a tag that has no CLICK address.
                break
            resolved.append((slot.memory_type, slot.address))
        else:
            name = operand_name(block_range)
            judged = range_aggregate_problem(
                name,
                [tag.name for tag in tags],
                [format_address_display(bank, address) for bank, address in resolved],
                reversed_order=block_range.reverse_order,
            )
            if judged is not None:
                code, problem = judged
                findings.append(
                    ClickFinding(
                        code=code,
                        severity=_route_severity(code, mode),
                        message=(
                            f"pyrung cannot export {name}.{kind}() at {location_text} "
                            "as one CLICK range. "
                            f"{range_not_consecutive_text([problem])}"
                        ),
                        location=location_text,
                        suggestion=range_aggregate_advice(problem),
                    )
                )
    return findings
