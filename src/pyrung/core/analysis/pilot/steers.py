"""Candidate generation for PILOT."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph


def candidate_values_for_tag(
    tag: str,
    snap: dict[str, Any],
    nogoods: set[tuple[str, Any]],
    *,
    needed_values: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    """Concrete values worth trying for one action tag.

    ``needed_values`` is trace-derived: when the trace can name the desired
    value, try that exact value.  Otherwise only synthesize the smallest
    generic action we can defend from the current snapshot: toggle a Bool.
    Prover nondeterministic domains are deliberately not swept here; they are
    value domains, not operator-action domains.
    """
    values: list[Any] = []
    if needed_values is not None and tag in needed_values:
        values.append(needed_values[tag])
    elif isinstance(snap.get(tag), bool):
        values.append(not snap[tag])
    return tuple(
        value
        for value in values
        if not _values_match(snap.get(tag), value) and (tag, value) not in nogoods
    )


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
    value is proposed directly.  Otherwise the generic fallback is limited
    to Bool toggles; nondeterministic domains are context, not a candidate
    action sweep.
    """
    del nd_domains
    candidates: list[tuple[str, Any]] = []
    for st in stuck_tags:
        upstream = pdg.upstream_slice(st)
        for inp in steerable:
            if inp not in upstream:
                continue
            candidates.extend(
                (inp, value)
                for value in candidate_values_for_tag(
                    inp,
                    snap,
                    nogoods,
                    needed_values=needed_values,
                )
            )
    return candidates
