"""Search crossing — ``found`` inverts to an existential over the block.

``search`` sets ``found = True`` iff some element of its range satisfies the
comparison, so ``found == True`` inverts to a :class:`Quant` existential — the
precise shape, kept as a named cell rather than a silent fallthrough.

Marked ``exact=False``: continuous mode resumes from the previous result, so the
existential is necessary but not, in general, sufficient.  Chasing the matched
``result`` address (the positive ``elem@addr`` conjunct plus the "no earlier
match" universal) is the documented frontier and falls through for now.
Multi-character text search also falls through because it compares consecutive
windows rather than individual block elements.
"""

from __future__ import annotations

from typing import Any, assert_never

from pyrung.core.analysis.crossings import BaseCrossing, register
from pyrung.core.analysis.crossings._ranges import range_tags
from pyrung.core.crossing import (
    REVERSE_FALLTHROUGH,
    Constraint,
    CrossingContext,
    Eq,
    Quant,
    ReverseResult,
    single,
)
from pyrung.core.instruction.advanced import SearchInstruction
from pyrung.core.instruction.operands import (
    ConstArg,
    ExprArg,
    PointerArg,
    PointerRangeArg,
    RangeArg,
    Span,
    TagArg,
    Value,
    held,
)


def _fixed_tags(span: Span) -> list[Any] | None:
    """Return the tags of *span*, or ``None`` for a range with pointers."""
    match span:
        case RangeArg(span=fixed):
            return range_tags(fixed)
        case PointerRangeArg():
            return None
        case _:
            assert_never(span)


class SearchCrossing(BaseCrossing):
    """Reverse ``search``: ``found == True`` -> "some element matches"."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        if not (isinstance(target, Eq) and len(target.values) == 1):
            return REVERSE_FALLTHROUGH
        tag = target.tag
        value = next(iter(target.values))
        if tag != instr.found.tag.name or value is not True:
            return REVERSE_FALLTHROUGH  # result-address chase is the frontier
        tags = _fixed_tags(instr.search_range)
        if not tags:
            return REVERSE_FALLTHROUGH
        search_value: Value | Span = instr.value
        match search_value:
            case ConstArg(value=str() as text) if len(text) > 1:
                # Text search compares consecutive windows, not each CHAR cell to
                # the whole string. Quant is element-wise and would omit concrete
                # matching windows, so defer until the constraint algebra has a
                # window-search shape.
                return REVERSE_FALLTHROUGH
            case TagArg(tag=value_tag, immediate=False):
                value_is_tag, bound = True, value_tag.name
            case TagArg() | PointerArg() | ExprArg() | ConstArg() | RangeArg() | PointerRangeArg():
                # The Quant compares each cell with the value as the author wrote it.
                value_is_tag, bound = False, held(search_value)
            case _:
                assert_never(search_value)
        block = tuple(t.name for t in tags)
        return single(
            Quant(
                kind="exists",
                block=block,
                op=instr.condition,
                value=bound,
                value_is_tag=value_is_tag,
            ),
            exact=False,
        )


register(SearchInstruction, SearchCrossing())
