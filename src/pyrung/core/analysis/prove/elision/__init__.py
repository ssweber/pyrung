"""State-key elision: slice-based write-before-read analysis."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .slice import _elide_sliced

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program


def _elide_scan_local_stateful_dims(
    program: Program,
    graph: ProgramGraph,
    stateful_dims: Mapping[str, tuple[Any, ...]],
    nondeterministic_dims: Mapping[str, tuple[Any, ...]],
    *,
    observed: frozenset[str] = frozenset(),
) -> tuple[dict[str, tuple[Any, ...]], dict[str, str]]:
    """Return the stateful dims that stay, and each elided tag with its method.

    *observed* names the tags read from outside the program.
    """
    return _elide_sliced(program, graph, stateful_dims, nondeterministic_dims, observed=observed)
