"""Recorded read-diff — the mechanical, semantics-free reverse (Crossings Phase 1).

Given a writer that recorded ``cause()`` cannot cross — a non-Boolean writer
(``calc``/``sum``/``copy``/``pack``) whose data reads never appear as SP-tree
contacts, so the backward walk dead-ends at the written tag — observe *which of
its static read footprint changed* between scan N-1 and N, and which reads are
non-zero at N.  No sign reasoning and no inversion: the operand values are
observed, so cancellation is moot.

This is **Tier 1**: the footprint is the PDG's pre-expanded ``data_reads``
(``DS.select(201,300).sum()`` is already ``{DS201..DS300}`` in the node — no
execution), and the values come from two already-cached states
(``history.at(N)`` / ``history.at(N-1)``).  Tier 2 (dynamic/unbounded indirect
whose footprint the PDG could not enumerate) and Tier 3 (truly opaque →
counterfactual fallback) are layered on top by the caller.

The burner acceptance — chase ``A_AlmExtent != 0`` to the truthy door/lint
operands — is met here with no sign oracle: ``nonzero_now`` names exactly the
operands that are non-zero, and ``changed`` names the ones that flipped this
scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import RungNode
    from pyrung.core.history import History


@dataclass(frozen=True)
class ReadDiff:
    """The observed read footprint of a writer across the N-1 → N boundary.

    - ``changed`` — footprint tags whose value differs between N-1 and N, as
      ``(tag, before, after)``; the *triggers* of the write (what flipped it).
    - ``nonzero_now`` — footprint tags whose value at N is truthy/non-zero; the
      *enablers* that an ``!= 0`` aggregate depends on (cancellation is moot —
      values are observed, not reasoned about).
    - ``footprint`` — the full static read set considered.
    - ``enumerable`` — ``False`` when the footprint was not statically
      enumerable (an unbounded indirect read the PDG dropped), signalling the
      caller to escalate to Tier 2 / the counterfactual fallback.
    """

    changed: list[tuple[str, Any, Any]] = field(default_factory=list)
    nonzero_now: list[str] = field(default_factory=list)
    footprint: frozenset[str] = frozenset()
    enumerable: bool = True

    @property
    def empty(self) -> bool:
        """Whether the diff names nothing to chase (no triggers, no enablers)."""
        return not self.changed and not self.nonzero_now


def _is_nonzero(value: Any) -> bool:
    """Whether *value* is a non-zero / truthy read.

    ``0``, ``0.0``, ``False``, ``None``, and empty strings are zero; any other
    Int/Real/Bool/Char is non-zero.  ``bool()`` captures exactly this.
    """
    return bool(value)


def _prev_scan_id(history: History, scan_id: int) -> int | None:
    """The scan id immediately preceding *scan_id* in retained history."""
    ids = history.scan_ids()
    try:
        index = ids.index(scan_id)
    except ValueError:
        return None
    return ids[index - 1] if index > 0 else None


def recorded_read_changes(
    history: History,
    node: RungNode,
    scan_id: int,
    *,
    prev_scan_id: int | None = None,
) -> ReadDiff:
    """Diff *node*'s static read footprint across the N-1 → N boundary.

    *node* is the (node-aware) writer rung whose ``data_reads`` were missed by
    SP-tree attribution.  Returns the changed and non-zero-now reads to continue
    the backward walk from.  When the footprint is empty there is nothing to
    cross — an empty, ``enumerable=True`` diff (the caller keeps its existing
    bare-root behaviour).
    """
    footprint = node.data_reads
    if not footprint:
        return ReadDiff(footprint=frozenset())

    if prev_scan_id is None:
        prev_scan_id = _prev_scan_id(history, scan_id)

    cur = history.at(scan_id)
    prev = history.at(prev_scan_id) if prev_scan_id is not None else None

    changed: list[tuple[str, Any, Any]] = []
    nonzero_now: list[str] = []
    for tag in sorted(footprint):
        after = cur.tags.get(tag)
        if prev is not None:
            before = prev.tags.get(tag)
            if before != after:
                changed.append((tag, before, after))
        if _is_nonzero(after):
            nonzero_now.append(tag)

    return ReadDiff(
        changed=changed,
        nonzero_now=nonzero_now,
        footprint=footprint,
    )
