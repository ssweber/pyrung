"""Bounded incident investigation for PILOT regressions.

``pilot.py`` decides that the vessel left the bearing.  This module owns the
separate question: what hypotheses are worth replaying, and which ones survive
counterfactual validation?
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

ActionPair = tuple[str, Any]
ReplayFn = Callable[[tuple[ActionPair, ...]], "ReplayOutcome"]

_MAX_CAUSE_DEPTH = 32


@dataclass(frozen=True)
class BearingDeparture:
    """One fact that held at the incident anchor and later departed."""

    tag: str
    value: Any
    scan: int | None


@dataclass(frozen=True)
class DeviationIncident:
    """The bounded window where verify observed a loss of bearing."""

    anchor_scan: int
    departure_scan: int | None
    end_scan: int
    action: tuple[ActionPair, ...]
    bearing: tuple[ActionPair, ...]
    before_snap: Mapping[str, Any]
    after_snap: Mapping[str, Any]
    changed_tags: tuple[str, ...]
    departures: tuple[BearingDeparture, ...]


@dataclass(frozen=True)
class InvestigationHypothesis:
    """A replay-testable explanation for an incident."""

    kind: str
    holds: tuple[ActionPair, ...]
    sources: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ReplayOutcome:
    """Pilot's replay judgment for a proposed hold set."""

    accepted: bool
    trend: int | None
    snapshot: Mapping[str, Any]
    reason: str = ""


@dataclass(frozen=True)
class InvestigationResult:
    """Replay-confirmed corrective information."""

    confirmed_holds: tuple[ActionPair, ...] = ()
    regression_nogoods: frozenset[ActionPair] = frozenset()
    hypotheses: tuple[InvestigationHypothesis, ...] = ()
    confirmed: tuple[InvestigationHypothesis, ...] = ()
    rejected: tuple[InvestigationHypothesis, ...] = ()
    unresolved: tuple[str, ...] = ()


def build_deviation_incident(
    plc: PLC,
    *,
    anchor_scan: int,
    end_scan: int,
    action: tuple[ActionPair, ...],
    bearing: tuple[ActionPair, ...],
    before_snap: Mapping[str, Any],
    after_snap: Mapping[str, Any],
) -> DeviationIncident:
    """Capture the facts inside the known off-course window."""
    changed_tags = _changed_tags_in_window(plc, anchor_scan, end_scan)
    departures = tuple(
        BearingDeparture(tag, value, _first_departure_scan(plc, tag, value, anchor_scan, end_scan))
        for tag, value in bearing
        if not _values_match(after_snap.get(tag), value)
    )
    departure_scans = [d.scan for d in departures if d.scan is not None]
    return DeviationIncident(
        anchor_scan=anchor_scan,
        departure_scan=min(departure_scans) if departure_scans else None,
        end_scan=end_scan,
        action=action,
        bearing=bearing,
        before_snap=before_snap,
        after_snap=after_snap,
        changed_tags=changed_tags,
        departures=departures,
    )


def investigate_deviation(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    replay: ReplayFn,
) -> InvestigationResult:
    """Investigate an incident by proposing hypotheses and replaying them."""
    hypotheses = _dedupe_hypotheses(_cause_hypotheses(plc, incident, ctx))
    confirmed: list[InvestigationHypothesis] = []
    rejected: list[InvestigationHypothesis] = []
    confirmed_holds: list[ActionPair] = []

    for hypothesis in hypotheses:
        if not hypothesis.holds:
            rejected.append(hypothesis)
            continue
        outcome = replay(hypothesis.holds)
        if outcome.accepted:
            confirmed.append(hypothesis)
            confirmed_holds.extend(hypothesis.holds)
        else:
            rejected.append(hypothesis)

    return InvestigationResult(
        confirmed_holds=tuple(_dedupe_pairs(confirmed_holds)),
        regression_nogoods=frozenset(),
        hypotheses=tuple(hypotheses),
        confirmed=tuple(confirmed),
        rejected=tuple(rejected),
        unresolved=incident.changed_tags if not confirmed else (),
    )


def _cause_hypotheses(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[InvestigationHypothesis]:
    """Plain-as-day path: recorded cause names transitioning steerable roots."""
    hypotheses: list[InvestigationHypothesis] = []
    for departure in incident.departures:
        chain = _cause(plc, departure.tag, departure.scan)
        if chain is None:
            continue
        nogoods, holds = _walk_cause_chain(chain, plc, ctx.steerable, set(), 0)
        holds = tuple(
            pair for pair in _dedupe_pairs(holds) if _hold_allowed(ctx, pair)
        )
        if holds:
            hypotheses.append(
                InvestigationHypothesis(
                    kind="recorded-cause",
                    holds=holds,
                    sources=tuple(sorted(nogoods | {departure.tag})),
                    detail=f"{departure.tag} departed at scan {departure.scan}",
                )
            )
    return hypotheses


def _walk_cause_chain(
    chain: Any,
    plc: PLC,
    steerable: frozenset[str],
    seen: set[tuple[str, int | None]],
    depth: int,
) -> tuple[set[str], list[ActionPair]]:
    if depth > _MAX_CAUSE_DEPTH:
        return set(), []

    key = (chain.effect.tag_name, chain.effect.scan_id)
    if key in seen:
        return set(), []
    seen.add(key)

    nogoods: set[str] = set()
    holds: list[ActionPair] = []

    def process_root(root: Any) -> None:
        if root.tag_name in steerable:
            nogoods.add(root.tag_name)
            if root.from_value is not None and not _values_match(root.from_value, root.to_value):
                holds.append((root.tag_name, root.from_value))
            return
        sub = _cause(plc, root.tag_name, root.scan_id)
        if sub is None:
            return
        sub_ng, sub_holds = _walk_cause_chain(sub, plc, steerable, seen, depth + 1)
        nogoods.update(sub_ng)
        holds.extend(sub_holds)

    for root in chain.conjunctive_roots:
        process_root(root)
    for root in chain.ambiguous_roots:
        process_root(root)
    for step in chain.steps:
        for trigger in step.triggers:
            process_root(trigger)

    return nogoods, _dedupe_pairs(holds)


def _cause(plc: PLC, tag: str, scan: int | None) -> Any | None:
    try:
        return plc.cause(tag, scan=scan) if scan is not None else plc.cause(tag)
    except Exception:  # noqa: BLE001
        logger.debug("pilot investigate: cause(%s) raised", tag, exc_info=True)
        return None


def _changed_tags_in_window(plc: PLC, start_scan: int, end_scan: int) -> tuple[str, ...]:
    try:
        states = plc.history.range(start_scan, end_scan + 1)
    except Exception:  # noqa: BLE001
        return ()
    changed: set[str] = set()
    for prev, cur in zip(states, states[1:], strict=False):
        tags = set(prev.tags) | set(cur.tags)
        changed.update(
            tag for tag in tags if not _values_match(prev.tags.get(tag), cur.tags.get(tag))
        )
    return tuple(sorted(changed))


def _first_departure_scan(
    plc: PLC,
    tag: str,
    value: Any,
    start_scan: int,
    end_scan: int,
) -> int | None:
    try:
        states = plc.history.range(start_scan, end_scan + 1)
    except Exception:  # noqa: BLE001
        return None
    for prev, cur in zip(states, states[1:], strict=False):
        if _values_match(prev.tags.get(tag), value) and not _values_match(cur.tags.get(tag), value):
            return cur.scan_id
    return None


def _route_allowed(ctx: Any, pair: ActionPair) -> bool:
    route_allowed = getattr(ctx, "route_allowed", None)
    return bool(route_allowed(pair)) if route_allowed is not None else True


def _hold_allowed(ctx: Any, pair: ActionPair) -> bool:
    tag, _value = pair
    compass = getattr(ctx, "compass", None)
    action_tags = getattr(compass, "action_tags", frozenset())
    return tag not in action_tags and _route_allowed(ctx, pair)


def _dedupe_pairs(pairs: list[ActionPair]) -> list[ActionPair]:
    out: list[ActionPair] = []
    seen: set[ActionPair] = set()
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


def _dedupe_hypotheses(
    hypotheses: list[InvestigationHypothesis],
) -> tuple[InvestigationHypothesis, ...]:
    out: list[InvestigationHypothesis] = []
    seen: set[tuple[ActionPair, ...]] = set()
    for hypothesis in hypotheses:
        key = hypothesis.holds
        if key in seen:
            continue
        seen.add(key)
        out.append(hypothesis)
    return tuple(out)
