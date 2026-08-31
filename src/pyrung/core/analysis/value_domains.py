"""Stable finite value-domain analysis shared across core consumers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag, TagType

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program

_DEFAULT_DECLARED_DOMAIN_CAP = 4096


def declared_value_domain(
    tag: Tag | None,
    *,
    max_values: int = _DEFAULT_DECLARED_DOMAIN_CAP,
) -> tuple[Any, ...] | None:
    """Return a complete finite declared domain, or ``None`` when open.

    ``max_values`` bounds integer range materialization for callers with tighter
    analysis budgets. Bool and explicit choices are declaration-sized already.
    """
    if tag is None:
        return None
    if tag.type is TagType.BOOL:
        return False, True
    if tag.choices:
        return tuple(sorted(tag.choices, key=repr))
    if tag.type in (TagType.INT, TagType.DINT, TagType.WORD):
        lo, hi = tag.min, tag.max
        if (
            isinstance(lo, int)
            and isinstance(hi, int)
            and not isinstance(lo, bool)
            and not isinstance(hi, bool)
            and 0 <= hi - lo < max_values
        ):
            return tuple(range(lo, hi + 1))
    return None


def produced_value_domains(
    program: Program,
    graph: ProgramGraph,
) -> dict[str, tuple[Any, ...]]:
    """Return finite domains justified solely by understood program producers.

    The producer fixed point remains part of prover classification for now, but
    this stable analysis facade prevents consumers from depending on that private
    module boundary. The local import avoids an analysis/prover import cycle.
    """
    from pyrung.core.analysis.prove.classify import collect_produced_value_domains

    return collect_produced_value_domains(program, graph)


def closed_value_domains(
    program: Program,
    graph: ProgramGraph,
) -> dict[str, tuple[Any, ...]]:
    """Return complete finite domains from contracts or understood producers."""
    produced = produced_value_domains(program, graph)
    domains: dict[str, tuple[Any, ...]] = {}
    for name, tag in graph.tags.items():
        declared = declared_value_domain(tag)
        if declared is not None:
            domains[name] = declared
        elif name in produced:
            domains[name] = produced[name]
    return domains


__all__ = [
    "closed_value_domains",
    "declared_value_domain",
    "produced_value_domains",
]
