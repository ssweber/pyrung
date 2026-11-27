"""The For family: forloop().

laddercodec's ForLoop checks it.
"""

from __future__ import annotations

from typing import Any

from laddercodec import ForLoop

from ..fields import Family, refusals_of, render_arg
from ..resolve import _ResolutionCache

# The argument of the pyrung call that holds each field of laddercodec's ForLoop.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "ForLoop.count": 0,
    "ForLoop.oneshot": "oneshot",
}

# pyrung's advice for a code, where the advice names pyrung's own calls or arguments.
_ADVICE: dict[str, str] = {
    "for-count-bank": "Copy the count into a DS or DD tag and loop on that.",
}


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    return _ADVICE.get(code)


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the For that the export writes."""
    count = render_arg(exporter, instruction.count, "count", instruction)
    oneshot = bool(instruction.oneshot)
    return refusals_of(lambda: ForLoop(count=count, oneshot=oneshot))


FAMILY = Family(
    name="forloop",
    classes={"ForLoopInstruction": {"count": "count"}},
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
    replaced_codes={"CLK_FORLOOP_COUNT_BANK": "for-count-bank"},
)
