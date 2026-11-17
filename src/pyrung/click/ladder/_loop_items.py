"""Export forloop body items as CLICK rungs.

Between For and Next, CLICK holds a run of rungs.  A bare body instruction is a
wire-only rung.  A body ``Rung`` (with its local contacts only) and a nested forloop
(a For rung of its own) export through the ordinary rung path.
"""

from __future__ import annotations

from typing import Any, TypeGuard

from pyrung.core.instruction import ForLoopInstruction
from pyrung.core.rung import Rung


def is_own_rung(item: Any) -> TypeGuard[Rung | ForLoopInstruction]:
    """Return True when a loop body item exports as a rung of its own."""
    return isinstance(item, (Rung, ForLoopInstruction))


def loop_item_rung(item: Rung | ForLoopInstruction) -> Rung:
    """Return a standalone rung for one body item, with only its own contacts."""
    if isinstance(item, ForLoopInstruction):
        shadow = Rung(source_file=item.source_file, source_line=item.source_line)
        shadow.add_instruction(item)
        return shadow
    local = item._conditions[item._branch_condition_start :]
    shadow = Rung(
        *local,
        source_file=item.source_file,
        source_line=item.source_line,
        end_line=item.end_line,
    )
    shadow.comment = item.comment
    for instruction in item._instructions:
        shadow.add_instruction(instruction)
    return shadow
