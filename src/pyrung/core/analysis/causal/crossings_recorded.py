"""Recorded read-diff — the mechanical, semantics-free reverse (Crossings Phase 1).

Given a writer that recorded ``cause()`` cannot cross — a non-Boolean writer
(``calc``/``sum``/``copy``/``pack``) whose data reads never appear as SP-tree
contacts, so the backward walk dead-ends at the written tag — observe *which of
its static read footprint changed* between scan N-1 and N, and which reads are
non-zero at N.  No sign reasoning and no inversion: the operand values are
observed, so cancellation is moot.

This is **Tier 1**: the footprint is the PDG's pre-expanded ``data_reads``
(``DS.select(201,300).sum()`` is already ``{DS201..DS300}`` in the node — no
execution).  The *after* value of each operand is what the writer read at fire
time (the rung's entry-time view), and *before* is the committed value entering
the scan (``history.at(N-1)``) — never end-of-scan N, which would mis-name an
operand reset later the same scan as the trigger.  Tier 2 (dynamic/unbounded
indirect whose footprint the PDG could not enumerate) and Tier 3 (truly opaque →
counterfactual fallback) are layered on top by the caller.

The burner acceptance — chase ``A_AlmExtent != 0`` to the truthy door/lint
operands — is met here with no sign oracle: ``nonzero_now`` names exactly the
operands that are non-zero, and ``changed`` names the ones that flipped this
scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.reverse_semantics import normalize_reverse_result
from pyrung.core.crossing import (
    AffineCmp,
    Cmp,
    CondAttr,
    Constraint,
    Eq,
    External,
    Mask,
    Prior,
    Quant,
    ReverseResult,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    footprint: frozenset[str],
    scan_id: int,
    *,
    prev_scan_id: int | None = None,
    read_values: Mapping[str, Any] | None = None,
) -> ReadDiff:
    """Diff a writer's read *footprint* against the value it read at fire time.

    *footprint* is the writer's data-read set (a single node's ``data_reads``,
    or the union across a rung's branches that write the tag — over-approximate
    so no real operand is missed).  Returns the changed and non-zero-now reads
    to continue the backward walk from.  When the footprint is empty there is
    nothing to cross — an empty, ``enumerable=True`` diff (the caller keeps its
    existing bare-root behaviour).

    The *after* value of each operand is the value the writer **actually read at
    fire time** (*read_values*, from the rung's entry-time view), not end-of-scan
    state.  An operand read-then-reset later the same scan (consume-on-read) ends
    the scan at its reset value; using that as *after* would mis-name the consume
    as the trigger.  *read_values* is ``None`` only when no interpreted replay is
    available (a logic-list PLC, or a scan out of replay range); then *after*
    falls back to end-of-scan state.  *before* is always the committed value
    entering the scan (end of N-1).

    A value established in an earlier scan remains an enabler, even when this
    writer consumes or clears it later in the current scan. Deep cause follows
    that enabler to its establishing transition; widening the trigger window
    would collapse two distinct causal hops and mislabel a steady read.
    """
    if not footprint:
        return ReadDiff(footprint=frozenset())

    if prev_scan_id is None:
        prev_scan_id = _prev_scan_id(history, scan_id)

    cur = history.at(scan_id)
    prev = history.at(prev_scan_id) if prev_scan_id is not None else None
    changed: list[tuple[str, Any, Any]] = []
    nonzero_now: list[str] = []
    for tag in sorted(footprint):
        if read_values is not None and tag in read_values:
            after = read_values[tag]
        else:
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


# --- the recorded resolver ----------------------------------------------------
#
# A projected crossing emits constraints in the shared algebra; the *recorded*
# mechanism discharges each against an observed scan.  This is the other half of
# the recorded<->projected unification: the registry expresses a crossing once,
# and this resolver — instead of the walker's interpreted fork — reads the answer
# out of history.  A ``Prior`` is where the two genuinely differ: it shifts the
# chase one scan back (the value the writer copied forward came from the previous
# scan), which only the recorded side, with history in hand, can follow directly.


@dataclass(frozen=True)
class ResolvedConstraint:
    """A single constraint discharged against an observed scan.

    - ``tag`` / ``scan_id`` — the tag to continue chasing and the scan to read it
      at (``scan_id`` for a same-scan constraint; the *previous* scan for a
      :class:`~pyrung.core.crossing.Prior`).  ``None`` for a leaf.
    - ``before`` / ``after`` / ``changed`` — the value across that scan's N-1->N
      boundary; ``changed`` distinguishes a trigger (flipped) from an enabler
      (held) for the recorded cause walk.
    - ``kind`` — ``"value"`` (chase ``tag``), ``"external"`` (a leaf input stop),
      ``"condition"`` (attribute the rung via ``attribute()``; see ``expected``),
      or ``"frontier"`` (a quantified search the recorded walk does not expand).
    """

    kind: str
    tag: str | None = None
    scan_id: int | None = None
    before: Any = None
    after: Any = None
    changed: bool = False
    expected: bool | None = None
    # Fidelity of the ReverseResult that produced this recorded conclusion.
    # ``None`` when resolved directly from a bare Constraint rather than through
    # the branch adapter.
    exact: bool | None = None


def _read_pair(history: History, tag: str, scan_id: int) -> tuple[Any, Any, bool]:
    """``(before, after, changed)`` for *tag* across the boundary ending at *scan_id*."""
    after = history.at(scan_id).tags.get(tag)
    prev = _prev_scan_id(history, scan_id)
    before = history.at(prev).tags.get(tag) if prev is not None else None
    return before, after, before != after


def resolve_recorded(
    constraint: Constraint, *, history: History, scan_id: int
) -> ResolvedConstraint | None:
    """Discharge one *constraint* against the observed scan *scan_id*.

    Returns the chase fact the recorded cause walk continues from, or ``None``
    when there is nothing to read (a ``Prior`` with no previous scan).  The
    recorded mechanism observes values rather than reasoning about them, so an
    ``Eq``/``Cmp`` value bound is not re-checked here — the resolver reads what
    the operand actually held and lets the walk continue.
    """
    if isinstance(constraint, Eq) and not constraint.values:
        return None
    if isinstance(constraint, (Eq, Cmp, AffineCmp, Mask)):
        before, after, changed = _read_pair(history, constraint.tag, scan_id)
        return ResolvedConstraint("value", constraint.tag, scan_id, before, after, changed)
    if isinstance(constraint, Prior):
        prev = _prev_scan_id(history, scan_id)
        if prev is None:
            return None  # no earlier scan to source the carried-forward value from
        before, after, changed = _read_pair(history, constraint.source, prev)
        return ResolvedConstraint("value", constraint.source, prev, before, after, changed)
    if isinstance(constraint, External):
        return ResolvedConstraint("external", constraint.tag, scan_id)
    if isinstance(constraint, CondAttr):
        return ResolvedConstraint("condition", expected=constraint.expected)
    if isinstance(constraint, Quant):
        return ResolvedConstraint("frontier")
    return None


def _resolve_recorded_constraint(
    constraint: Constraint, *, history: History, scan_id: int
) -> tuple[ResolvedConstraint, ...] | None:
    """Resolve every value-bearing side of one constraint.

    ``resolve_recorded`` retains its one-result public contract.  A tag-bound
    comparison additionally depends on its bound tag, so branch resolution
    carries both observed values into the cause adapter.
    """
    resolved = resolve_recorded(constraint, history=history, scan_id=scan_id)
    if resolved is None:
        return None
    left_tag = constraint.tag if isinstance(constraint, (Cmp, AffineCmp)) else None
    bound_tag = (
        constraint.bound
        if isinstance(constraint, Cmp)
        and constraint.bound_is_tag
        and isinstance(constraint.bound, str)
        else constraint.bound_tag
        if isinstance(constraint, AffineCmp)
        else None
    )
    if bound_tag is not None and bound_tag != left_tag:
        before, after, changed = _read_pair(history, bound_tag, scan_id)
        return (
            resolved,
            ResolvedConstraint(
                "value",
                bound_tag,
                scan_id,
                before,
                after,
                changed,
            ),
        )
    return (resolved,)


def resolve_recorded_branches(
    result: ReverseResult, *, history: History, scan_id: int
) -> list[list[ResolvedConstraint]]:
    """Discharge a DNF :class:`ReverseResult` — one resolved list per branch.

    Fallthrough and contradiction yield ``[]``.  An unresolvable constraint
    invalidates its whole conjunction; it must not be silently removed and turn
    the remaining constraints into a weaker causal explanation.  An original
    empty conjunction remains a trivially-satisfied branch.
    """
    normalized = normalize_reverse_result(result)
    if normalized.fallthrough or normalized.contradiction:
        return []
    resolved: list[list[ResolvedConstraint]] = []
    for branch in normalized.branches:
        resolved_branch: list[ResolvedConstraint] = []
        for constraint in branch:
            facts = _resolve_recorded_constraint(
                constraint,
                history=history,
                scan_id=scan_id,
            )
            if facts is None:
                break
            resolved_branch.extend(replace(fact, exact=normalized.exact) for fact in facts)
        else:
            resolved.append(resolved_branch)
    return resolved
