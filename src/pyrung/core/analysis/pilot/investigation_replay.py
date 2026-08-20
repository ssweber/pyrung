"""Incident records and disposable replay engines for PILOT investigation.

This module owns recorded-step projection, deviation incidents, causal
regression comparison, bounded replay judgment, and excursion replay. It
returns exact causal evidence or one replay-confirmed excursion correction;
``regression_requirements.py`` adapts that evidence into ordinary requirement
contracts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pyrung.core.analysis.pilot.correction_candidates as _candidates
import pyrung.core.analysis.pilot.refinement as _refinement
from pyrung.core.analysis.pilot.advance import iter_advance_owners
from pyrung.core.analysis.pilot.avoid import _hold_allowed
from pyrung.core.analysis.pilot.causal import (
    _shared_cause,
    chase_cause_roots,
    chase_chain_tags,
)
from pyrung.core.analysis.pilot.coast import (
    _settle_delayed_effects,
)
from pyrung.core.analysis.pilot.correction_records import _ConfirmedCorrection
from pyrung.core.analysis.pilot.execution import ExecutionReceipt
from pyrung.core.analysis.pilot.guard_forcing import break_guard_holds
from pyrung.core.analysis.pilot.incidents import BearingDeparture, DeviationIncident
from pyrung.core.analysis.pilot.navigation_contracts import _ActionPair
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _pilot_rungs_from_proposals,
    _set_pilot_rungs,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.pulse import _apply_pulse
from pyrung.core.analysis.pilot.skiff import run_pinned_scan
from pyrung.core.analysis.pilot.types import _IterationFrame
from pyrung.core.analysis.pilot.world_key import _pilot_state_key
from pyrung.core.analysis.pilot.writer_selection import _can_produce
from pyrung.core.analysis.sp_values import _values_match, _written_value_for_tag
from pyrung.core.context import RungId

if TYPE_CHECKING:
    from pyrung.core.runner import PLC, Epoch, EpochQuery

logger = logging.getLogger(__name__)

ActionPair = tuple[str, Any]


def _deviation_bearing(
    execution: ExecutionReceipt,
    frame: _IterationFrame,
    watch_tags: list[str],
    frontier: tuple[_ActionPair, ...],
) -> tuple[_ActionPair, ...]:
    """Facts the failed operation actually held and then lost."""

    needed_by_tag: dict[str, list[Any]] = {}
    for tag, value in frontier:
        needed_by_tag.setdefault(tag, []).append(value)
    bearing: list[_ActionPair] = [
        (tag, frame.snap.get(tag))
        for tag in watch_tags
        if not _values_match(frame.snap.get(tag), execution.after_snap.get(tag))
        and not any(
            _values_match(execution.after_snap.get(tag), needed)
            for needed in needed_by_tag.get(tag, ())
        )
    ]
    channel = execution.channel_motion.channel_tag
    if channel is not None:
        source = execution.before_snap.get(channel)
        landed = execution.after_snap.get(channel)
        if not _values_match(landed, source):
            bearing = [(tag, value) for tag, value in bearing if tag != channel]
            bearing.append((channel, source))
    return tuple(bearing)


@dataclass(frozen=True)
class CausalOccurrence:
    """One exact rung/write occurrence on a recorded causal explanation."""

    rung: RungId
    tag: str
    value: Any
    scan_id: int | None = None
    occurrence_ordinal: int | None = None
    exact_write: Any = field(default=None, compare=False, repr=False)
    execution_owner: EpochQuery | None = field(default=None, compare=False, repr=False)
    execution_projection: Any = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if (
            self.execution_owner is not None
            and getattr(self.execution_owner, "epoch", None) is None
        ):
            raise ValueError("causal occurrence owner must expose one Epoch")

    @property
    def execution_epoch(self) -> Epoch | None:
        """Derive the physical Epoch from the occurrence's sole owner."""

        if self.execution_owner is None:
            return None
        return self.execution_owner.epoch


@dataclass(frozen=True)
class RegressionWitness:
    """Exact causal explanation for a recorded channel regression."""

    channel_tag: str
    source: Any
    departed: Any
    landing: Any
    departure_scan: int
    cause: tuple[CausalOccurrence, ...]
    causal_spine: frozenset[str]
    causal_roots: tuple[tuple[str, Any], ...] = ()
    owner_snapshot: Mapping[str, Any] | None = None
    # Full, unfiltered deep-chain occurrences.  ``cause`` intentionally begins
    # after the incident anchor for replay comparison; accepted expectations
    # which later participate in a regression can have produced their effect
    # at or before that anchor.  These links retain the projection/owner proof
    # needed to join such a cause back to its expectation receipt.
    receipt_links: tuple[CausalOccurrence, ...] = ()


def incident_regression_witness(
    plc: PLC,
    incident: DeviationIncident,
) -> RegressionWitness | None:
    """Recover the exact causal branch of the incident's channel transition."""

    channel = incident.channel_tag
    departure = next(
        (
            departure
            for departure in incident.departures
            if departure.tag == channel and departure.scan is not None
        ),
        None,
    )
    if channel is None or departure is None or departure.scan is None:
        return None
    chain = _shared_cause(plc, channel, departure.scan)
    if chain is None:
        return None
    effect = chain.effect
    if (
        effect.tag_name != channel
        or effect.scan_id != departure.scan
        or not _values_match(effect.from_value, departure.value)
        or _values_match(effect.from_value, effect.to_value)
    ):
        return None
    # Keep the lineage's retained tip epoch identity.  Slicing the same epoch
    # through ``departure.scan`` would mint a new Epoch/query pair and sever an
    # otherwise exact join to an accepted receipt earlier in that epoch.
    sealed = plc._causal_lineage.seal_through(plc.state.scan_id)

    def _exact_occurrence(step: Any) -> CausalOccurrence | None:
        transition = step.transition
        ordinal = transition.occurrence_ordinal
        if ordinal is None:
            return None
        projection = plc._replay_rung_write_projection_at(transition.scan_id)
        if projection is None:
            return None
        exact = tuple(
            write
            for write in projection.writes
            if write.ordinal == ordinal
            and write.rung_id == RungId(step.subroutine, step.rung_index)
            and write.transition.tag_name == transition.tag_name
            and _values_match(write.transition.to_value, transition.to_value)
        )
        owned = next(
            (
                owner
                for epoch, owner in sealed
                if epoch.first_scan <= transition.scan_id <= epoch.last_scan
            ),
            None,
        )
        if len(exact) != 1 or owned is None:
            return None
        return CausalOccurrence(
            rung=RungId(step.subroutine, step.rung_index),
            tag=transition.tag_name,
            value=transition.to_value,
            scan_id=transition.scan_id,
            occurrence_ordinal=ordinal,
            exact_write=exact[0],
            execution_owner=owned,
            execution_projection=projection,
        )

    receipt_links: list[CausalOccurrence] = []
    for step in chain.steps:
        transition = step.transition
        if transition.scan_id > departure.scan or _values_match(
            transition.from_value, transition.to_value
        ):
            continue
        exact_occurrence = _exact_occurrence(step)
        if exact_occurrence is not None and not any(
            prior.scan_id == exact_occurrence.scan_id
            and prior.occurrence_ordinal == exact_occurrence.occurrence_ordinal
            and prior.execution_owner is exact_occurrence.execution_owner
            for prior in receipt_links
        ):
            receipt_links.append(exact_occurrence)

    # A steady root can have been established by an earlier accepted action.
    # The deep chain records only its held-since boundary, so recover a link
    # only when that scan owns one unambiguous changed rung write.
    for root in chain.roots:
        held_since = root.held_since_scan
        if held_since is None or held_since > departure.scan:
            continue
        projection = plc._replay_rung_write_projection_at(held_since)
        owned = next(
            (owner for epoch, owner in sealed if epoch.first_scan <= held_since <= epoch.last_scan),
            None,
        )
        candidates = (
            tuple(
                write
                for write in projection.writes
                if write.transition.tag_name == root.tag_name
                and _values_match(write.transition.to_value, root.value)
                and not _values_match(write.transition.from_value, write.transition.to_value)
            )
            if projection is not None
            else ()
        )
        if len(candidates) != 1 or owned is None:
            continue
        write = candidates[0]
        exact_occurrence = CausalOccurrence(
            rung=write.rung_id,
            tag=write.transition.tag_name,
            value=write.transition.to_value,
            scan_id=write.scan_id,
            occurrence_ordinal=write.ordinal,
            exact_write=write,
            execution_owner=owned,
            execution_projection=projection,
        )
        if not any(
            prior.scan_id == exact_occurrence.scan_id
            and prior.occurrence_ordinal == exact_occurrence.occurrence_ordinal
            and prior.execution_owner is exact_occurrence.execution_owner
            for prior in receipt_links
        ):
            receipt_links.append(exact_occurrence)

    for transition in chain.conjunctive_roots:
        ordinal = transition.occurrence_ordinal
        if ordinal is None or transition.scan_id > departure.scan:
            continue
        projection = plc._replay_rung_write_projection_at(transition.scan_id)
        candidates = (
            tuple(
                write
                for write in projection.writes
                if write.ordinal == ordinal
                and write.transition.tag_name == transition.tag_name
                and _values_match(write.transition.to_value, transition.to_value)
            )
            if projection is not None
            else ()
        )
        owned = next(
            (
                owner
                for epoch, owner in sealed
                if epoch.first_scan <= transition.scan_id <= epoch.last_scan
            ),
            None,
        )
        if len(candidates) != 1 or owned is None:
            continue
        write = candidates[0]
        exact_occurrence = CausalOccurrence(
            rung=write.rung_id,
            tag=transition.tag_name,
            value=transition.to_value,
            scan_id=transition.scan_id,
            occurrence_ordinal=ordinal,
            exact_write=write,
            execution_owner=owned,
            execution_projection=projection,
        )
        if not any(
            prior.scan_id == exact_occurrence.scan_id
            and prior.occurrence_ordinal == exact_occurrence.occurrence_ordinal
            and prior.execution_owner is exact_occurrence.execution_owner
            for prior in receipt_links
        ):
            receipt_links.append(exact_occurrence)

    cause: list[CausalOccurrence] = []
    for step in chain.steps:
        transition = step.transition
        if (
            transition.scan_id <= incident.anchor_scan
            or transition.scan_id > departure.scan
            or _values_match(transition.from_value, transition.to_value)
        ):
            continue
        occurrence = _exact_occurrence(step)
        if occurrence is None:
            # Generic regression comparison predates exact receipt linking and
            # remains valid with its coarse rung/tag/value designation.  Only
            # ``receipt_links`` is exact-only and fails closed when projection
            # ownership is unavailable.
            occurrence = CausalOccurrence(
                rung=RungId(step.subroutine, step.rung_index),
                tag=transition.tag_name,
                value=transition.to_value,
            )
        if not any(
            prior.rung == occurrence.rung
            and prior.tag == occurrence.tag
            and _values_match(prior.value, occurrence.value)
            for prior in cause
        ):
            cause.append(occurrence)
    if not cause:
        return None
    if not any(
        occurrence.tag == channel and _values_match(occurrence.value, effect.to_value)
        for occurrence in cause
    ):
        return None
    return RegressionWitness(
        channel_tag=channel,
        source=effect.from_value,
        departed=effect.to_value,
        landing=incident.after_snap.get(channel),
        departure_scan=departure.scan,
        cause=tuple(cause),
        causal_spine=frozenset(chase_chain_tags(plc, channel, scan=departure.scan)),
        causal_roots=tuple((root.tag_name, root.value) for root in chain.roots),
        owner_snapshot=(
            dict(plc.history.at(departure.scan - 1).tags)
            if departure.scan > plc.history.oldest_scan_id
            else dict(incident.before_snap)
        ),
        receipt_links=tuple(receipt_links),
    )


# ---------------------------------------------------------------------------
# Excursion investigation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExcursionResult:
    """Replay-confirmed correction from an excursion investigation."""

    reverted: list[str]
    correction: _ConfirmedCorrection | None = None
    replay_fork: Any = None
    replay_timeline: tuple[Any, ...] = ()
    replay_kernel_scan_ids: tuple[int, ...] = ()


def investigate_excursion(
    work: PLC,
    fork: PLC,
    pre_snap: dict[str, Any],
    post_pulse_snap: dict[str, Any],
    pre_key: tuple[Any, ...],
    applied_actions: Sequence[ActionPair],
    *,
    cfg: Any,
    steerable: frozenset[str],
    pilot_rungs: Sequence[Any],
    resting: dict[str, Any],
    edge_tags: set[str],
    scan_budget: int,
    pdg: Any = None,
    program: Any = None,
    ctx: Any = None,
) -> ExcursionResult:
    """Diagnose an excursion and replay-validate candidate holds."""
    from pyrung.core.analysis.pdg import resolve_rung

    reverted: list[str] = []
    for i, name in enumerate(cfg.stateful_names):
        if i in cfg.acc_indices:
            continue
        if not _values_match(pre_snap.get(name), post_pulse_snap.get(name)):
            reverted.append(name)

    candidate_holds: list[ActionPair] = []
    seen: set[ActionPair] = set()

    if pdg is not None and program is not None:
        settled_snap = dict(fork.state.tags)
        mini_ctx = SimpleNamespace(
            pdg=pdg,
            program=program,
            steerable=steerable,
            opaque_loop=frozenset(),
            pipeline_internal_tags=frozenset(),
            route=None,
            domain_prior=None,
            nd_domains=None,
        )
        for tag in reverted:
            desired = post_pulse_snap.get(tag)
            for ni in _implicated_writers(fork, tag, pdg):
                node = pdg.rung_nodes[ni]
                ro = resolve_rung(program, node)
                if ro is None:
                    continue
                if _can_produce(_written_value_for_tag(ro, tag), desired):
                    continue
                holds = break_guard_holds(ro, settled_snap, mini_ctx)
                if holds is None:
                    holds = _skiff_suppression_nominations(
                        work,
                        tag,
                        desired,
                        node,
                        applied_actions,
                        pdg,
                        steerable,
                        pilot_rungs,
                    )
                for hold in holds or ():
                    if hold not in seen:
                        seen.add(hold)
                        candidate_holds.append(hold)

    if not candidate_holds:
        for tag in reverted:
            _, holds = chase_cause_roots(fork, tag, steerable)
            for hold in holds:
                if hold not in seen:
                    seen.add(hold)
                    candidate_holds.append(hold)

            try:
                chain = fork.cause(tag, deep=False)
            except Exception:  # noqa: BLE001
                continue
            if chain is None:
                continue
            for step in chain.steps:
                for enabler in step.enablers:
                    if enabler.tag_name not in steerable:
                        continue
                    if not isinstance(enabler.value, bool):
                        continue
                    hold = (enabler.tag_name, not enabler.value)
                    if hold not in seen:
                        seen.add(hold)
                        candidate_holds.append(hold)

    action_tags = {tag for tag, _ in applied_actions}
    candidate_holds = [(tag, value) for tag, value in candidate_holds if tag not in action_tags]
    if ctx is not None:
        candidate_holds = [hold for hold in candidate_holds if _hold_allowed(ctx, hold)]
    if not candidate_holds:
        return ExcursionResult(reverted=reverted)

    replay_fork = fork_with_pilot_rungs(work, pilot_rungs)
    replay_pilot_rungs = list(pilot_rungs)
    from pyrung.core.analysis.pilot.coast import CoastSession
    from pyrung.core.condition import CompareEq

    preserved_tag = reverted[0]
    preserved = replay_fork._known_tags_by_name[preserved_tag]
    scope = CompareEq(preserved, post_pulse_snap[preserved_tag])
    confirmed_pilot_rungs = tuple(_pilot_rungs_from_proposals(candidate_holds, scope))
    replay_pilot_rungs.extend(confirmed_pilot_rungs)
    _set_pilot_rungs(replay_fork, replay_pilot_rungs)
    kickoff = list(applied_actions)
    kickoff.extend(
        (tag, value)
        for tag, value in candidate_holds
        if tag not in {action_tag for action_tag, _ in applied_actions}
    )
    session = CoastSession(replay_fork, kind="excursion-replay")
    if program is not None:
        session.arm_pens(
            owner.profile.done.name
            for owner in iter_advance_owners(program)
            if owner.profile.done is not None
        )
    _apply_pulse(replay_fork, kickoff, resting, edge_tags, session=session)
    _settle_delayed_effects(replay_fork, scan_budget=scan_budget, session=session)
    replay_snap = dict(replay_fork.state.tags)
    replay_key = _pilot_state_key(replay_snap, cfg)

    if replay_key != pre_key:
        return ExcursionResult(
            reverted=reverted,
            correction=_ConfirmedCorrection(
                identity=_candidates.correction_identity(confirmed_pilot_rungs),
                pilot_rungs=confirmed_pilot_rungs,
                sources=tuple(dict.fromkeys((*reverted, *(tag for tag, _ in candidate_holds)))),
                justification="excursion replay preserved the pulse-established state",
            ),
            replay_fork=replay_fork,
            replay_timeline=session.events,
            replay_kernel_scan_ids=session.kernel_scan_ids,
        )
    return ExcursionResult(reverted=reverted)


def _implicated_writers(plc: PLC, tag: str, pdg: Any) -> list[int]:
    """Return writer-node indices causally implicated in *tag*'s deviation."""
    try:
        chain = plc.cause(tag, deep=False)
    except Exception:  # noqa: BLE001
        chain = None
    if chain is None:
        return []
    implicated = {
        (step.rung_index, step.subroutine)
        for step in chain.steps
        if step.transition.tag_name == tag
    }
    if not implicated:
        return []
    out: list[int] = []
    for ni in pdg.writers_of.get(tag, frozenset()):
        node = pdg.rung_nodes[ni]
        if (node.rung_index, node.subroutine) in implicated:
            out.append(ni)
    return out


def _skiff_suppression_nominations(
    work: PLC,
    tag: str,
    desired: Any,
    node: Any,
    applied_actions: Sequence[ActionPair],
    pdg: Any,
    steerable: frozenset[str],
    pilot_rungs: Sequence[PilotRung],
    *,
    run_pinned: Callable[..., Any] = run_pinned_scan,
) -> list[ActionPair]:
    """Return bounded pinned suppression nominations."""
    return _refinement._skiff_suppression_nominations(
        work,
        tag,
        desired,
        node,
        applied_actions,
        pdg,
        steerable,
        pilot_rungs,
        run_pinned=run_pinned,
    )


def _first_timeline_departure(
    timeline: Sequence[Any],
    tag: str,
    value: Any,
) -> int | None:
    """Return the recorded scan where *tag* first transitioned off *value*."""
    for event in timeline:
        for event_tag, before, after in event.transitions:
            if (
                event_tag == tag
                and _values_match(before, value)
                and not _values_match(after, value)
            ):
                return event.scan
    return None


def build_deviation_incident(
    *,
    anchor_scan: int,
    end_scan: int,
    action: tuple[ActionPair, ...],
    bearing: tuple[ActionPair, ...],
    before_snap: Mapping[str, Any],
    after_snap: Mapping[str, Any],
    timeline: Sequence[Any] = (),
    channel_tag: str | None = None,
) -> DeviationIncident:
    """Capture facts from the known off-course window."""
    changed: set[str] = {tag for event in timeline for tag, _before, _after in event.transitions}
    changed.update(
        tag
        for tag in set(before_snap) | set(after_snap)
        if not _values_match(before_snap.get(tag), after_snap.get(tag))
    )
    departures = tuple(
        BearingDeparture(
            tag,
            value,
            _first_timeline_departure(timeline, tag, value),
        )
        for tag, value in bearing
        if not _values_match(after_snap.get(tag), value)
    )
    departure_scans = [departure.scan for departure in departures if departure.scan is not None]
    return DeviationIncident(
        anchor_scan=anchor_scan,
        departure_scan=min(departure_scans) if departure_scans else None,
        end_scan=end_scan,
        action=action,
        bearing=bearing,
        before_snap=before_snap,
        after_snap=after_snap,
        changed_tags=tuple(sorted(changed)),
        departures=departures,
        channel_tag=channel_tag,
        timeline=tuple(timeline),
    )
