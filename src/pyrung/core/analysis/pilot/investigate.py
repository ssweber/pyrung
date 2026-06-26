"""Bounded incident investigation for PILOT regressions.

``pilot.py`` decides that the vessel left the bearing.  This module owns the
separate question: what hypotheses are worth replaying, and which ones survive
counterfactual validation?
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import _apply_pulse, _install_holds
from pyrung.core.analysis.pilot.causal import chase_cause_roots
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.trace import TraceChoice
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

ActionPair = tuple[str, Any]
ReplayFn = Callable[[tuple[ActionPair, ...]], "ReplayOutcome"]


# ---------------------------------------------------------------------------
# Incident / hypothesis / result types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Replay harness — fork, hold, replay steps, trace-back, judge
# ---------------------------------------------------------------------------


def build_replay_fn(
    cp_fork: PLC,
    cp_trend: int,
    forced_holds: dict[str, Any],
    steps: Sequence[Any],
    *,
    resting: dict[str, Any],
    edge_tags: set[str],
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    pipeline_internal_tags: frozenset[str],
    choice: TraceChoice | None,
) -> ReplayFn:
    """Build a replay callback for ``investigate_deviation``.

    The returned function forks from the checkpoint, installs existing holds
    plus the proposed hypothesis holds, replays the recorded step sequence,
    then traces back to judge whether the trend improved.
    """

    def _replay(holds: tuple[ActionPair, ...]) -> ReplayOutcome:
        probe = cp_fork.fork()
        _install_holds(probe, list(forced_holds.items()), {})
        _install_holds(probe, list(holds), {})
        for step in steps:
            if step.action:
                _apply_pulse(probe, list(step.action.items()), resting, edge_tags)
            else:
                for _ in range(max(1, step.scans)):
                    probe.step()
        snap = dict(probe.state.tags)
        tree = trace_back(
            target_tag,
            target_value,
            snap,
            pdg,
            program,
            steerable,
            opaque_loop=opaque_loop,
            pipeline_internal_tags=pipeline_internal_tags,
            choice=choice,
        )
        trend = tree.unsatisfied_count()
        return ReplayOutcome(
            accepted=trend <= cp_trend,
            trend=trend,
            snapshot=snap,
            reason=f"trend {trend} <= checkpoint {cp_trend}",
        )

    return _replay


# ---------------------------------------------------------------------------
# Incident construction
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Investigation engine
# ---------------------------------------------------------------------------


def investigate_deviation(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
    replay: ReplayFn,
) -> InvestigationResult:
    """Investigate an incident by proposing hypotheses and replaying them."""
    raw: list[InvestigationHypothesis] = []
    raw.extend(_cause_hypotheses(plc, incident, ctx))
    raw.extend(_latch_exposure_hypotheses(plc, incident, ctx))
    raw.extend(_upstream_hypotheses(incident, ctx))
    hypotheses = _dedupe_hypotheses(raw)
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


# ---------------------------------------------------------------------------
# Hypothesis generation
# ---------------------------------------------------------------------------


def _cause_hypotheses(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[InvestigationHypothesis]:
    """Plain-as-day path: recorded cause names transitioning steerable roots."""
    hypotheses: list[InvestigationHypothesis] = []
    for departure in incident.departures:
        nogoods, holds = chase_cause_roots(plc, departure.tag, ctx.steerable, scan=departure.scan)
        holds_filtered = tuple(pair for pair in _dedupe_pairs(holds) if _hold_allowed(ctx, pair))
        if holds_filtered:
            hypotheses.append(
                InvestigationHypothesis(
                    kind="recorded-cause",
                    holds=holds_filtered,
                    sources=tuple(sorted(nogoods | {departure.tag})),
                    detail=f"{departure.tag} departed at scan {departure.scan}",
                )
            )
    return hypotheses


def _latch_exposure_hypotheses(
    plc: PLC,
    incident: DeviationIncident,
    ctx: Any,
) -> list[InvestigationHypothesis]:
    """Latch-exposure: a latch fired because a guard input wasn't held.

    Scan changed_tags for tags written by LatchInstruction.  For each,
    check if the latch writer's condition includes both an opaque-loop
    state tag (that the pilot just entered) AND a steerable input (that
    wasn't held).  Propose holding the input at its safe value.
    """
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.instruction.coils import LatchInstruction

    pdg = getattr(ctx, "pdg", None)
    steerable = getattr(ctx, "steerable", frozenset())
    opaque_loop = getattr(ctx, "opaque_loop", frozenset())
    program = getattr(ctx, "program", None)
    if pdg is None or program is None:
        return []

    hypotheses: list[InvestigationHypothesis] = []
    for tag in incident.changed_tags:
        if not _values_match(incident.after_snap.get(tag), True):
            continue
        writers = pdg.writers_of.get(tag, frozenset())
        for ri in writers:
            rn = pdg.rung_nodes[ri]
            ro = resolve_rung(program, rn)
            if ro is None:
                continue
            is_latch = any(isinstance(i, LatchInstruction) for i in ro._instructions)
            if not is_latch:
                continue
            sp = ro.sp_tree()
            if sp is None:
                continue
            condition_tags = set(getattr(sp, "tag_names", ()) or ())
            has_state = bool(condition_tags & opaque_loop)
            steerable_in_cond = condition_tags & steerable
            if not has_state or not steerable_in_cond:
                continue
            for st in sorted(steerable_in_cond):
                safe = not incident.after_snap.get(st) if isinstance(incident.after_snap.get(st), bool) else True
                hold = (st, safe)
                if _hold_allowed(ctx, hold):
                    hypotheses.append(
                        InvestigationHypothesis(
                            kind="latch-exposure",
                            holds=(hold,),
                            sources=(tag, st),
                            detail=f"latch {tag} fired with {st}={incident.after_snap.get(st)!r}",
                        )
                    )
    return hypotheses


def _upstream_hypotheses(
    incident: DeviationIncident,
    ctx: Any,
) -> list[InvestigationHypothesis]:
    """Heuristic upstream: steerable inputs in the PDG cone of departed tags.

    For each departure, find steerable inputs in the upstream cone.
    Propose holding each at its pre-incident (bearing) value.
    """
    pdg = getattr(ctx, "pdg", None)
    steerable = getattr(ctx, "steerable", frozenset())
    if pdg is None or not steerable:
        return []

    hypotheses: list[InvestigationHypothesis] = []
    seen: set[ActionPair] = set()
    for departure in incident.departures:
        try:
            cone = pdg.upstream_slice(departure.tag)
        except Exception:  # noqa: BLE001
            continue
        candidates = cone & steerable
        for st in sorted(candidates):
            before_val = incident.before_snap.get(st)
            after_val = incident.after_snap.get(st)
            if _values_match(before_val, after_val):
                hold = (st, before_val)
            else:
                hold = (st, before_val)
            if hold in seen:
                continue
            seen.add(hold)
            if _hold_allowed(ctx, hold):
                hypotheses.append(
                    InvestigationHypothesis(
                        kind="heuristic-upstream",
                        holds=(hold,),
                        sources=(departure.tag, st),
                        detail=f"{st} in upstream cone of {departure.tag}",
                    )
                )
    return hypotheses


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
