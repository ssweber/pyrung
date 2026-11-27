"""The Search family: search().

laddercodec's Search checks it.
"""

from __future__ import annotations

from typing import Any

from laddercodec import Search

from pyrung.core.instruction.operands import held

from ..fields import Family, refusals_of, render_operand, split_range
from ..resolve import _ResolutionCache

# The argument of the pyrung call that holds each field of laddercodec's Search. The
# comparison, such as ``DS1..DS10 >= 100``, holds the table, the operator and the value.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "Search.table_start": 0,
    "Search.table_end": 0,
    "Search.value": 0,
    "Search.op": 0,
    "Search.result": "result",
    "Search.found": "found",
    "Search.continuous": "continuous",
    "Search.oneshot": "oneshot",
}

# pyrung's advice for a refusal of a field, where the advice names pyrung's own calls or
# arguments.
_ADVICE: dict[tuple[str, str], str] = {
    ("bank-not-allowed", "Search.table_start"): (
        "Copy the values into a range in one of those banks and search that."
    ),
    ("bank-not-allowed", "Search.result"): (
        "Map the result to a DS address, and copy it on from there if needed."
    ),
    ("bank-not-allowed", "Search.found"): (
        "Map found to a C bit, and copy it on from there if needed."
    ),
}


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    return _ADVICE.get((code, field))


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the Search that the export writes."""

    def render(attribute: str, operand: Any) -> str:
        return render_operand(exporter, operand, attribute, instruction)

    table = render("search_range", held(instruction.search_range))
    start, end = split_range(table)
    # The export writes a constant value in hex when the table is in a hex bank.
    with exporter._hex_constants("exact" if exporter._is_hex_bank_operand(table) else None):
        value = render("value", held(instruction.value))
    result = render("result", instruction.result.tag)
    found = render("found", instruction.found.tag)
    return refusals_of(
        lambda: Search(
            table_start=start,
            table_end=end,
            value=value,
            result=result,
            found=found,
            op=instruction.condition,
            continuous=bool(instruction.continuous),
            oneshot=bool(instruction.oneshot),
        )
    )


FAMILY = Family(
    name="search",
    classes={
        "SearchInstruction": {
            "search_range": "comparison",
            "value": "comparison",
            "result": "result",
            "found": "found flag",
        }
    },
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
    ranges={"SearchInstruction": ("search_range",)},
    replaced_codes={
        "CLK_SEARCH_RANGE_BANK": "bank-not-allowed",
        "CLK_SEARCH_RESULT_NOT_DS": "bank-not-allowed",
    },
)
