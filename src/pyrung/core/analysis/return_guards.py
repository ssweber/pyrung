"""Shared subroutine-entry and ``return_early()`` control-flow guards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.simplified import Const, _negate, _sp_to_expr

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph, RungNode
    from pyrung.core.condition import Condition
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung

_MAX_REACH_CHAINS = 64


@dataclass(frozen=True)
class ReachChain:
    """One path to a rung: condition terms plus exact early-return guards.

    ``conditions`` stays in the ladder Condition vocabulary used by structural
    SAT checks. ``return_guards`` retains the simplified expressions, including
    their negated polarity, for consumers able to reason at that richer layer.
    ``complete`` is false when the bounded path expansion omitted alternatives.
    A conservative consumer can require :attr:`return_stable` instead.
    """

    conditions: tuple[Condition, ...] = ()
    return_guards: tuple[Any, ...] = ()
    complete: bool = True

    @property
    def return_stable(self) -> bool:
        """Whether no earlier ``return_early()`` can suppress this path."""
        return self.complete and not self.return_guards


def _instructions_have_return(instructions: list[Any]) -> bool:
    from pyrung.core.instruction.control import ForLoopInstruction, ReturnInstruction

    for instruction in instructions:
        if isinstance(instruction, ReturnInstruction):
            return True
        if isinstance(instruction, ForLoopInstruction) and _instructions_have_return(
            instruction.instructions
        ):
            return True
    return False


def _return_guard_exprs_in_rung(rung: Rung) -> tuple[Any, ...]:
    guards: list[Any] = []
    if _instructions_have_return(rung._instructions):
        sp = rung.sp_tree()
        guards.append(_negate(_sp_to_expr(sp)) if sp is not None else Const(False))
    for branch in rung._branches:
        guards.extend(_return_guard_exprs_in_rung(branch))
    return tuple(guards)


def return_early_guard_exprs(
    program: Program,
    rung_node: RungNode,
    *,
    include_current: bool = False,
) -> tuple[Any, ...]:
    """Negated conditions of returns that may gate ``rung_node``.

    Branch return conditions retain their inherited parent condition structure.
    An unconditional prior return is represented as ``Const(False)`` rather than
    disappearing, so callers can distinguish unreachable from unguarded code.
    ``include_current`` conservatively includes returns anywhere in the current
    top-level rung when instruction ordering is unavailable to the consumer.
    """
    if rung_node.subroutine is None:
        return ()
    sub_rungs = program.subroutines.get(rung_node.subroutine)
    if sub_rungs is None:
        return ()
    stop = rung_node.rung_index + int(include_current)
    return tuple(guard for rung in sub_rungs[:stop] for guard in _return_guard_exprs_in_rung(rung))


def _rung_conditions(program: Program, node: RungNode) -> tuple[Condition, ...]:
    from pyrung.core.analysis.pdg import resolve_rung

    rung = resolve_rung(program, node)
    return tuple(rung._conditions) if rung is not None else ()


def scope_reach_chains(
    program: Program,
    graph: ProgramGraph,
) -> dict[str, tuple[ReachChain, ...]]:
    """Return paths from Main to each subroutine entry.

    Call edges come exclusively from :meth:`ProgramGraph.call_sites`. Each path
    carries the call-rung conditions and exact prior-return guards through nested
    callers. Cycles add no synthetic route from Main and expansion is capped.
    """
    calls: dict[str, list[tuple[str | None, ReachChain]]] = {}
    for site in graph.call_sites():
        node = graph.rung_nodes[site.node_index]
        calls.setdefault(site.callee, []).append(
            (
                site.caller,
                ReachChain(
                    conditions=_rung_conditions(program, node),
                    return_guards=return_early_guard_exprs(
                        program,
                        node,
                        include_current=True,
                    ),
                ),
            )
        )

    memo: dict[str, tuple[ReachChain, ...]] = {}

    def resolve(name: str, active: frozenset[str]) -> tuple[ReachChain, ...]:
        if name in memo:
            return memo[name]
        chains: list[ReachChain] = []
        overflow = False
        for caller, local in calls.get(name, ()):
            if caller is None:
                chains.append(local)
            elif caller not in active:
                for prefix in resolve(caller, active | {name}):
                    chains.append(
                        ReachChain(
                            conditions=(*prefix.conditions, *local.conditions),
                            return_guards=(*prefix.return_guards, *local.return_guards),
                            complete=prefix.complete and local.complete,
                        )
                    )
            if len(chains) > _MAX_REACH_CHAINS:
                overflow = True
                break
        selected = chains[:_MAX_REACH_CHAINS]
        result = tuple(
            ReachChain(
                chain.conditions,
                chain.return_guards,
                complete=chain.complete and not overflow,
            )
            for chain in selected
        )
        if not active:
            memo[name] = result
        return result

    return {name: resolve(name, frozenset()) for name in program.subroutines}


def effective_reach_chains(
    program: Program,
    graph: ProgramGraph,
    rung_node: RungNode,
    *,
    scope_chains: dict[str, tuple[ReachChain, ...]] | None = None,
) -> tuple[ReachChain, ...]:
    """Return Main-to-rung paths including local conditions and return guards."""
    local_conditions = _rung_conditions(program, rung_node)
    local_returns = return_early_guard_exprs(program, rung_node, include_current=True)
    if rung_node.subroutine is None:
        return (ReachChain(local_conditions, local_returns),)

    entries = scope_chains if scope_chains is not None else scope_reach_chains(program, graph)
    return tuple(
        ReachChain(
            conditions=(*entry.conditions, *local_conditions),
            return_guards=(*entry.return_guards, *local_returns),
            complete=entry.complete,
        )
        for entry in entries.get(rung_node.subroutine, ())
    )


__all__ = [
    "ReachChain",
    "effective_reach_chains",
    "return_early_guard_exprs",
    "scope_reach_chains",
]
