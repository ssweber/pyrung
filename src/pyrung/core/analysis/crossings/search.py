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

from typing import Any

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


class SearchCrossing(BaseCrossing):
    """Reverse ``search``: ``found == True`` -> "some element matches"."""

    def reverse(
        self, instr: Any, rung: Any, target: Constraint, ctx: CrossingContext
    ) -> ReverseResult:
        if not (isinstance(target, Eq) and len(target.values) == 1):
            return REVERSE_FALLTHROUGH
        tag = target.tag
        value = next(iter(target.values))
        if tag != instr.found.name or value is not True:
            return REVERSE_FALLTHROUGH  # result-address chase is the frontier
        tags = range_tags(getattr(instr, "search_range", None))
        if not tags:
            return REVERSE_FALLTHROUGH
        search_value = instr.value
        if isinstance(search_value, str) and len(search_value) > 1:
            # Text search compares consecutive windows, not each CHAR cell to
            # the whole string. Quant is element-wise and would omit concrete
            # matching windows, so defer until the constraint algebra has a
            # window-search shape.
            return REVERSE_FALLTHROUGH
        value_is_tag = hasattr(search_value, "name")
        bound = search_value.name if value_is_tag else search_value
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
