"""Candidate generation for PILOT — Bool and numeric steers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph


def upstream_candidates(
    stuck_tags: set[str],
    steerable: frozenset[str],
    nogoods: set[tuple[str, Any]],
    snap: dict[str, Any],
    pdg: ProgramGraph,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    needed_values: dict[str, Any] | None = None,
) -> list[tuple[str, Any]]:
    """Steerable inputs upstream of *stuck_tags* with candidate values.

    When *needed_values* maps an input to a trace-derived target, that
    value is proposed directly instead of sweeping the domain.
    """
    candidates: list[tuple[str, Any]] = []
    for st in stuck_tags:
        upstream = pdg.upstream_slice(st)
        for inp in steerable:
            if inp not in upstream:
                continue
            if needed_values is not None and inp in needed_values:
                v = needed_values[inp]
                if not _values_match(snap.get(inp), v) and (inp, v) not in nogoods:
                    candidates.append((inp, v))
            else:
                if not _values_match(snap.get(inp), True) and (inp, True) not in nogoods:
                    candidates.append((inp, True))
    return candidates
