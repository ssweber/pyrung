"""forloop body items as CLICK rungs.

Between For and Next, CLICK holds a run of rungs.  A bare body instruction is a
wire-only rung; a body ``Rung`` (its local contacts only) and a nested forloop
(a For rung of its own) export through the ordinary rung path.
"""

from __future__ import annotations

from typing import Any, TypeGuard

from pyrung.core.instruction import ForLoopInstruction
from pyrung.core.rung import Rung


def is_own_rung(item: Any) -> TypeGuard[Rung | ForLoopInstruction]:
    """Whether a loop body item exports as a rung of its own."""
    return isinstance(item, (Rung, ForLoopInstruction))


def loop_item_rung(item: Rung | ForLoopInstruction) -> Rung:
    """A standalone rung for one body item, carrying only its own contacts."""
    if isinstance(item, ForLoopInstruction):
        shadow = Rung(source_file=item.source_file, source_line=item.source_line)
        shadow.add_instruction(item)
        return shadow
    shadow = _shadow(item, (), item._branch_condition_start)
    shadow.comment = item.comment
    return shadow


def _shadow(rung: Rung, inherited: tuple[Any, ...], start: int) -> Rung:
    """*rung* re-rooted on *inherited*, dropping the loop rung's contacts
    (its first *start* conditions); branches are re-rooted the same way."""
    shadow = Rung(
        *inherited,
        *rung._conditions[start:],
        source_file=rung.source_file,
        source_line=rung.source_line,
        end_line=rung.end_line,
    )
    shadow._branch_condition_start = len(inherited)
    for item in rung._execution_items:
        if isinstance(item, Rung):
            shadow.add_branch(
                _shadow(item, tuple(shadow._conditions), item._branch_condition_start)
            )
        else:
            shadow.add_instruction(item)
    return shadow
