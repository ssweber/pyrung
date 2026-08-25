"""Solve finite-domain guard assignments through structural steerable drivers.

This module owns the policy-free forcing primitive shared by correction
hypothesis generation and investigation replay. It resolves condition reads to
structural actions without selecting a correction family or mutating Pilot
state.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from pyrung.core.analysis.pilot.overlay import OperationReceipt
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.pilot.trace_read import UnsupportedConstruct
from pyrung.core.analysis.pilot.trace_tree import TraceAction
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.crossing import Eq

ActionPair = tuple[str, Any]


# ---------------------------------------------------------------------------
# Finite-domain guard forcing and structural driver resolution
# ---------------------------------------------------------------------------


def _resolve_steerable_action(
    read_tag: str,
    value: Any,
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    steerable: frozenset[str] | None = None,
) -> TraceAction | None:
    """Structural action that drives ``read_tag == value``.

    Either *read_tag* is itself steerable, or ``trace_back`` bridges it to its
    nearest steerable driver (e.g. the ``i_DoorClosed`` PIVOT to physical
    ``x_DoorClosed``). The returned action keeps the intermediate owner's
    boundary and progress receipt.
    """
    steerable = getattr(ctx, "steerable", frozenset()) if steerable is None else steerable
    if read_tag in steerable:
        boundary = Eq(read_tag, frozenset((value,)))
        return TraceAction(
            read_tag,
            value,
            until=boundary,
            operation=OperationReceipt(boundary),
        )
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return None

    def _actions(tag: str, val: Any, view: dict[str, Any]) -> list[TraceAction]:
        tree = trace_back(
            tag,
            val,
            view,
            pdg,
            program,
            steerable,
            clear_only=getattr(ctx, "clear_only", frozenset()),
            opaque_loop=getattr(ctx, "opaque_loop", frozenset()),
            pipeline_internal_tags=getattr(ctx, "pipeline_internal_tags", frozenset()),
            route=getattr(ctx, "route", None),
            prior=getattr(ctx, "domain_prior", None),
        )
        return tree.ordered_action_details()

    try:
        view = dict(snap)
        if isinstance(value, bool) and _values_match(view.get(read_tag), value):
            # Already satisfied at the snapshot — the driver exists regardless,
            # but tracing a satisfied target returns a leafless stub (this is
            # what made a complement-reset "oscillation" one-way: the resting
            # polarity resolved to None and its watchdog contributed nothing).
            # Discover the driver via the opposite polarity — naturally
            # unsatisfied at the original snapshot — then flip read and driver
            # in the view so the wanted-polarity trace walks the writer and
            # does the polarity math itself.
            probe = _actions(read_tag, not value, dict(snap))
            view[read_tag] = not value
            for action in probe:
                view[action.tag] = action.value
        actions = _actions(read_tag, value, view)
    except UnsupportedConstruct:
        raise
    except Exception:  # noqa: BLE001
        return None
    if not actions:
        return None
    action = actions[0]
    if action.operation is not None:
        return action
    # Ordinary trace edges (for example physical input -> mapped contact) have
    # no intermediate cross-scan owner. Their trace handoff, when present, is
    # still the honest operation boundary; otherwise the physical assignment
    # itself is a one-scan boundary. Do not discard these actions merely because
    # no timer sits between the physical lever and the watchdog reset.
    boundary = action.until or Eq(action.tag, frozenset((action.value,)))
    return replace(
        action,
        until=boundary,
        operation=OperationReceipt(boundary),
    )


def _cannot_hold_pairs(demand: Any, snap: Mapping[str, Any], ctx: Any) -> list[tuple[str, Any]]:
    """Coordinated steerable holds that stop one demanded condition.

    Enumerates the advance condition over its reads' value spaces to find the
    minimal lever assignment that makes it evaluate ``!= advance_value`` (stops
    advancing), then resolves each participating read to its steerable driver.
    A single-read advance yields one lever; a conjunction yields the cheapest
    single conjunct to break; an ``Or`` yields every arm as a
    coordinated set. Returns ``[]`` when no steerable stopping assignment exists.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition

    if demand is None or demand.condition is None:
        return []
    reads = _extract_reads_from_condition(demand.condition, {})
    if not reads:
        return []
    holds = _best_forcing_holds(
        demand.condition,
        reads,
        snap,
        ctx,
        satisfies=lambda evaluated: bool(evaluated) is not demand.value,
    )
    return holds or []


# ---------------------------------------------------------------------------
# Condition enumeration — the minimal-lever primitive shared by both arms
# ---------------------------------------------------------------------------


def _read_domains(
    reads: set[str], snap: Mapping[str, Any], ctx: Any
) -> dict[str, tuple[Any, ...]] | None:
    """Finite value domain per read, or ``None`` if any read's domain is unknown.

    Bool reads resolve to ``(False, True)``; int reads use the prover's
    ``nd_domains`` (``ctx.domain_prior``) or the tag's declared ``choices``, then
    the producible-value resolution ``tide_tables._guard_operand_domain``
    already implements.  Reusing that resolver keeps the Bool+int domain handling
    identical to the tide tables rather than reinventing it.
    """
    from pyrung.core.analysis.pilot.tide_tables import _guard_operand_domain

    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return None
    prior = getattr(ctx, "domain_prior", None)
    base = dict(getattr(prior, "nd_domains", None) or {})
    tags = getattr(pdg, "tags", {})
    for tag in reads:
        if tag in base:
            continue
        tag_ref = tags.get(tag)
        choices = getattr(tag_ref, "choices", None) if tag_ref is not None else None
        if choices:
            base[tag] = tuple(choices)
    domains: dict[str, tuple[Any, ...]] = {}
    for tag in reads:
        cur = snap.get(tag)
        if isinstance(cur, bool):
            domains[tag] = (False, True)
            continue
        dom = _guard_operand_domain(tag, dict(snap), pdg, program, base)
        if not dom:
            return None
        domains[tag] = dom
    return domains


def _minimal_forcing_sets(
    condition: Any,
    order: tuple[str, ...],
    domains: dict[str, tuple[Any, ...]],
    satisfies: Callable[[Any], bool],
    base: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Minimal partial assignments that *force* ``satisfies(condition.evaluate)``.

    A partial assignment forces iff every completion over the remaining reads'
    domains evaluates to a satisfying value (an undecidable term disqualifies
    it).  Minimal = no already-found forcing set is a subset.  This is
    prime-implicant enumeration: for a conjunction the sole forcing set is every
    conjunct; for a disjunction each arm is its own single-literal set.  Returned
    smallest-first.
    """
    base_values = dict(base or {})

    def _eval(assignment: dict[str, Any]) -> bool | None:
        try:
            return bool(satisfies(condition.evaluate(_SnapView({**base_values, **assignment}))))
        except Exception:  # noqa: BLE001
            return None

    minimal: list[dict[str, Any]] = []
    for k in range(1, len(order) + 1):
        for subset in itertools.combinations(order, k):
            others = [t for t in order if t not in subset]
            other_doms = [domains[t] for t in others]
            for vcombo in itertools.product(*(domains[t] for t in subset)):
                partial = dict(zip(subset, vcombo, strict=True))
                if any(m.items() <= partial.items() for m in minimal):
                    continue  # a smaller forcing subset already covers this
                forced = True
                for ocombo in itertools.product(*other_doms):
                    completion = {**partial, **dict(zip(others, ocombo, strict=True))}
                    if _eval(completion) is not True:
                        forced = False
                        break
                if forced:
                    minimal.append(partial)
    return minimal


def _resolve_partial_actions(
    partial: dict[str, Any],
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    steerable: frozenset[str] | None = None,
) -> list[TraceAction] | None:
    """Resolve each literal without discarding its intermediate operation owner.

    ``None`` if any read resolves to no steerable driver (the assignment is
    unsteerable) or two literals demand conflicting values of one driver.
    """
    actions: dict[str, TraceAction] = {}
    for read, value in partial.items():
        action = _resolve_steerable_action(
            read,
            value,
            snap,
            ctx,
            steerable=steerable,
        )
        if action is None:
            return None
        existing = actions.get(action.tag)
        if existing is not None and not _values_match(existing.value, action.value):
            return None
        if existing is None or (existing.operation is None and action.operation is not None):
            actions[action.tag] = action
    return list(actions.values())


def _best_forcing_actions(
    condition: Any,
    reads: set[str],
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    satisfies: Callable[[Any], bool],
    base: Mapping[str, Any] | None = None,
    steerable: frozenset[str] | None = None,
) -> list[TraceAction] | None:
    """Cheapest structural driver operations that force *condition* to satisfy.

    Enumerates the reads' finite domains (capped like ``tide_tables``), finds
    the minimal forcing assignments, and among the steerable ones prefers (a)
    fewest levers that differ from the current snapshot, then (b) fewest levers
    total. ``None`` means no forcing assignment is steerable.
    """
    from pyrung.core.analysis.pilot.tide_tables import bounded_product

    order = tuple(sorted(reads))
    if not order:
        return None
    domains = _read_domains(reads, snap, ctx)
    if domains is None:
        return None
    if bounded_product(domains.values()) is None:
        return None

    sets = _minimal_forcing_sets(condition, order, domains, satisfies, base)
    if not sets:
        return None

    def _rank(partial: dict[str, Any]) -> tuple[int, int]:
        changes = sum(1 for t, v in partial.items() if not _values_match(snap.get(t), v))
        return (changes, len(partial))

    for partial in sorted(sets, key=_rank):
        actions = _resolve_partial_actions(partial, snap, ctx, steerable=steerable)
        if actions:
            return actions
    return None


def _best_forcing_holds(
    condition: Any,
    reads: set[str],
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    satisfies: Callable[[Any], bool],
    base: Mapping[str, Any] | None = None,
    steerable: frozenset[str] | None = None,
) -> list[ActionPair] | None:
    """Return the minimum steady pair pins that force the requested truth."""
    actions = _best_forcing_actions(
        condition,
        reads,
        snap,
        ctx,
        satisfies=satisfies,
        base=base,
        steerable=steerable,
    )
    return None if actions is None else [action.pair for action in actions]


def break_guard_holds(
    rung_obj: Any,
    snap: Mapping[str, Any],
    ctx: Any,
    *,
    changeable: set[str] | frozenset[str] | None = None,
    fixed: Mapping[str, Any] | None = None,
    steerable: frozenset[str] | None = None,
) -> list[ActionPair] | None:
    """Minimal steerable lever set that forces *rung_obj*'s enable guard FALSE.

    The **suppression dual** of the accumulator arm's satisfy-the-reset
    enumeration: the same :func:`_best_forcing_holds` machinery with the polarity
    inverted (``satisfies=lambda v: not v`` instead of ``bool``).  This
    *suppresses* a clobbering writer — forces its guard false so the deviated
    register keeps the value the pulse established.

    ``changeable`` narrows the correction frontier to selected guard reads,
    while every other read stays at ``snap``.  ``fixed`` overlays at-fire values
    for protected reads (normally the actual triggers that represent intended
    progress). ``steerable`` optionally supplies the exact terminal lever set
    for this incident, so program-written or protected tags are traversed rather
    than mistaken for free inputs.

    Returns coordinated ``(phys, value)`` holds, or ``None`` when the guard is
    unreadable/unsteerable — no candidate reads, an unknown (live-word) domain,
    or no steerable forcing assignment. ``None`` is the **punt signal** the
    caller escalates to the skiff on.  Rejection stays over COMPLETE finite
    domains only (inherited from ``_best_forcing_holds`` / ``_read_domains``);
    it never fabricates a hold it cannot read.
    """
    from pyrung.core.analysis.pdg import _extract_reads_from_condition

    guard = rung_obj._get_combined_condition()
    if guard is None:
        return None
    reads = _extract_reads_from_condition(guard, {})
    if not reads:
        return None
    if changeable is not None:
        reads &= set(changeable)
    if not reads:
        return None
    base = dict(snap)
    if fixed:
        base.update(fixed)
    return _best_forcing_holds(
        guard,
        reads,
        snap,
        ctx,
        satisfies=lambda evaluated: not evaluated,
        base=base,
        steerable=steerable,
    )


class _SnapView:
    """Minimal ``ConditionView`` over a dict — just enough to evaluate a reset or
    advance condition over a trial assignment (``Condition.evaluate`` only calls
    ``get_tag(name, default)``)."""

    def __init__(self, snap: Mapping[str, Any]):
        self._snap = snap

    def get_tag(self, name: str, default: Any = None) -> Any:
        return self._snap.get(name, default)
