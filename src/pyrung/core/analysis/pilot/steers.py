"""Candidate generation for PILOT — Bool and numeric steers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph


def upstream_candidates(
    stuck_tags: set[str],
    steerable: frozenset[str],
    nogoods: set[str],
    snap: dict[str, Any],
    pdg: ProgramGraph,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
) -> list[tuple[str, Any]]:
    """Steerable inputs upstream of *stuck_tags* with candidate values.

    For inputs with an ``nd_domains`` entry, generates one ``(inp, v)``
    per domain value (filtering out the current value).  For Bool inputs
    or inputs without a domain, generates ``(inp, True)`` if not already
    True.
    """
    candidates: list[tuple[str, Any]] = []
    for st in stuck_tags:
        upstream = pdg.upstream_slice(st)
        for inp in steerable:
            if inp not in upstream or inp in nogoods:
                continue
            if nd_domains is not None and inp in nd_domains:
                for v in nd_domains[inp]:
                    if not _values_match(snap.get(inp), v):
                        candidates.append((inp, v))
            else:
                if not _values_match(snap.get(inp), True):
                    candidates.append((inp, True))
    return candidates
