"""Shared helpers for implicit ``return_early()`` control-flow guards."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.simplified import _negate, _sp_to_expr

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import RungNode


def _return_early_guard_exprs(program: Any, rung_node: RungNode) -> list[Any]:
    """Negated conditions of prior ``return_early()`` rungs guarding a writer.

    The PDG stores only guard tag names on ``RungNode.guard_reads``.  Consumers
    that reason about live inputs or writer prerequisites need the actual
    expression polarity: a rung after ``Rung(Enable): return_early()`` executes
    only when ``Enable`` is false.
    """
    if not rung_node.guard_reads or rung_node.subroutine is None or rung_node.branch_path != ():
        return []
    sub_rungs = program.subroutines.get(rung_node.subroutine)
    if sub_rungs is None:
        return []

    from pyrung.core.instruction.control import ReturnInstruction

    guards: list[Any] = []
    for rung in sub_rungs[: rung_node.rung_index]:
        if any(isinstance(instr, ReturnInstruction) for instr in rung._instructions):
            sp = rung.sp_tree()
            if sp is not None:
                guards.append(_negate(_sp_to_expr(sp)))
    return guards
