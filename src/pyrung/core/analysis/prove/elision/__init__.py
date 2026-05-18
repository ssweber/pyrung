"""Traced influence-graph state-key elision."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from .trace import _elide_traced, _ExitSubstitution

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.simplified import Expr
    from pyrung.core.program import Program


def _elide_scan_local_stateful_dims(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    observer_exprs: tuple[Expr, ...] = (),
    observer_tag_names: frozenset[str] = frozenset(),
    progress: Callable[[str], None] | None = None,
    progress_prefix: Callable[[], str] | None = None,
    unclassified_tags: frozenset[str] = frozenset(),
    infeasible_out: set[str] | None = None,
) -> tuple[
    dict[str, tuple[Any, ...]],
    dict[str, str],
    dict[str, tuple[tuple[str, str], ...]],
    dict[str, _ExitSubstitution],
]:
    """Return (reduced stateful dims, elided tag -> method, tag -> proof detail, substitutions) after conservative elision."""
    if not stateful_dims:
        return {}, {}, {}, {}

    return _elide_traced(
        program,
        graph,
        stateful_dims,
        nondeterministic_dims,
        observer_exprs=observer_exprs,
        observer_tag_names=observer_tag_names,
        progress=progress,
        progress_prefix=progress_prefix,
        unclassified_tags=unclassified_tags,
        infeasible_out=infeasible_out,
    )
