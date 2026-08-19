"""Incident records and disposable replay engines for PILOT investigation.

This module owns recorded-step projection, deviation incidents, causal
regression comparison, bounded replay judgment, and excursion replay.  It
returns evidence or one confirmation; candidate selection and composition
remain in :mod:`investigate`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
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
    _COAST_BUDGET,
    AVOID,
    _coast_holding_state,
    _settle_delayed_effects,
)
from pyrung.core.analysis.pilot.constrained_reachability import (
    FrontierStatus,
    Reachable,
    Unknown,
)
from pyrung.core.analysis.pilot.corrections import break_guard_holds
from pyrung.core.analysis.pilot.earned_work import EarnedWorkMovement
from pyrung.core.analysis.pilot.execution import ExecutionReceipt, MotionKind
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _pilot_rungs_from_proposals,
    _set_pilot_rungs,
    _target_unresolved_condition,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.pulse import _apply_pulse
from pyrung.core.analysis.pilot.skiff import run_pinned_scan
from pyrung.core.analysis.pilot.trace import target_reached, trace_back
from pyrung.core.analysis.pilot.types import (
    BearingDeparture,
    DeviationIncident,
    _ActionPair,
    _ConfirmedCorrection,
    _IterationFrame,
    _Step,
    _StepContext,
)
from pyrung.core.analysis.pilot.world_key import _pilot_state_key
from pyrung.core.analysis.pilot.writer_selection import _can_produce
from pyrung.core.analysis.sp_values import _values_match, _written_value_for_tag
from pyrung.core.context import RungId

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.types import _PilotContext
    from pyrung.core.runner import PLC, Epoch, EpochQuery

logger = logging.getLogger(__name__)

ActionPair = tuple[str, Any]
ReplayFn = Callable[[tuple[Any, ...]], "ReplayOutcome"]


@dataclass(frozen=True)
class ReplayStep:
    """One recorded physical step and its replay operation context."""

    inputs: tuple[tuple[str, Any], ...]
    scans: int
    kind: str
    channel_tag: str | None = None
    channel_target: Any = None
    channel_boundary: Any = None
    # Original scan interval, retained so replay can distinguish the committed
    # prefix from the one physical operation that produced an incident.
    scan_before: int | None = None
    scan_after: int | None = None


def _replay_step(step: _Step, context: _StepContext) -> ReplayStep:
    """Map one recorded physical step and its operation context to replay."""

    kind = {
        MotionKind.INTERVENTION: "pulse",
        MotionKind.COAST_TO_BEARING: "bearing_coast",
        MotionKind.COAST_HOLDING_WORLD: "letrun",
    }[context.policy.motion]
    channel_motion = context.execution.channel_motion
    replay_motion = context.execution.replay_motion
    if not replay_motion.active:
        replay_motion = channel_motion
    if kind == "bearing_coast" and replay_motion.channel_tag is None:
        kind = "dwell"
    return ReplayStep(
        inputs=tuple(step.inputs.items()),
        scans=step.scans,
        kind=kind,
        channel_tag=replay_motion.channel_tag,
        channel_target=replay_motion.target_value,
        channel_boundary=replay_motion.boundary,
        scan_before=getattr(step, "scan_before", None),
        scan_after=getattr(step, "scan_after", None),
    )


def _step_owns_departure(step: ReplayStep, witness: RegressionWitness | None) -> bool:
    """Whether *step*'s recorded operation contains the incident transition."""

    if witness is None or step.scan_before is None or step.scan_after is None:
        return False
    return step.scan_before < witness.departure_scan <= step.scan_after


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


@dataclass(frozen=True)
class ReplacementEvidence:
    """A counterfactual branch that reproduced a channel departure."""

    plc: Any
    incident: DeviationIncident
    witness: RegressionWitness
    shared_suffix: tuple[CausalOccurrence, ...] = ()


@dataclass(frozen=True)
class ReplayIncident:
    """The bounded occurrence and judgment evidence a replay must reproduce."""

    channel_tag: str | None = None
    channel_target: Any = None
    terminal_role_tags: tuple[str, ...] | None = None
    watch_roles: tuple[str, ...] = ()
    departure_bearing: tuple[ActionPair, ...] = ()
    regression_witness: RegressionWitness | None = None
    earned_work: Any = None
    progress_anchor: Mapping[str, Any] | None = None
    regression_progress_floor: Mapping[str, Any] | None = None


class ReplayJustification(Enum):
    """The target-relative ground on which a replayed correction succeeded."""

    REACHED = "reached"
    NEUTRALIZED = "neutralized"
    ADVANCED = "advanced"
    BEARING_HELD = "bearing-held"


@dataclass(frozen=True)
class ReplayOutcome:
    """PILOT's replay judgment for a proposed hold set."""

    accepted: bool
    trend: int | None
    snapshot: Mapping[str, Any]
    reason: str = ""
    justification: ReplayJustification | None = None
    continuation: FrontierStatus = Unknown("target continuation was not inspected")
    continuation_snapshot: Mapping[str, Any] | None = None
    landed: bool = True
    replacement_cause: frozenset[str] = frozenset()
    replacement: ReplacementEvidence | None = None


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


def _regression_cause_replayed(
    plc: PLC,
    witness: RegressionWitness,
    *,
    start_scan: int,
    end_scan: int,
) -> bool:
    """Whether replay reproduced every changed write on the recorded cause."""

    remaining = list(witness.cause)
    for scan in range(start_scan + 1, end_scan + 1):
        main_firings = plc.rung_firings(scan)
        node_firings = plc._node_firings_at(scan)
        ambiguous: set[RungId] = set()
        matched: list[CausalOccurrence] = []
        for occurrence in remaining:
            writes = (
                main_firings.get(occurrence.rung.rung_index)
                if occurrence.rung.subroutine is None
                else node_firings.get(occurrence.rung)
            )
            if writes is None:
                continue
            if occurrence.tag in writes and _values_match(
                writes[occurrence.tag],
                occurrence.value,
            ):
                matched.append(occurrence)
            elif not writes:
                ambiguous.add(occurrence.rung)
        if ambiguous:
            for run in plc._replay_rung_runs_at(scan):
                if run.rung_id not in ambiguous or not run.enabled:
                    continue
                attempted = dict(run.writes)
                for occurrence in remaining:
                    if (
                        occurrence.rung == run.rung_id
                        and occurrence.tag in attempted
                        and _values_match(attempted[occurrence.tag], occurrence.value)
                    ):
                        matched.append(occurrence)
        if matched:
            remaining = [occurrence for occurrence in remaining if occurrence not in matched]
            if not remaining:
                return True
    return False


@dataclass(frozen=True)
class _RegressionOwnership:
    """Current replay evidence about one recorded regression branch."""

    source_preserved: bool
    cause_silenced: bool
    replacement_cause: frozenset[str] | None
    replacement_owned: bool | None
    replacement_replays_recorded: bool | None
    unrelated_departure: bool
    neutralized: bool
    shared_suffix: tuple[CausalOccurrence, ...] = ()


def _replacement_departure_scan(
    witness: RegressionWitness,
    events: Sequence[Any],
) -> int | None:
    """First counterfactual departure from the recorded channel source."""

    return next(
        (
            event.scan
            for event in events
            for tag, before, after in event.transitions
            if tag == witness.channel_tag
            and _values_match(before, witness.source)
            and not _values_match(after, witness.source)
        ),
        None,
    )


def _same_occurrence(left: CausalOccurrence, right: CausalOccurrence) -> bool:
    return (
        left.rung == right.rung and left.tag == right.tag and _values_match(left.value, right.value)
    )


def _same_bounded_channel_outcome(
    recorded: RegressionWitness,
    replacement: RegressionWitness,
) -> bool:
    """Whether two witnesses describe the same bounded transition outcome."""

    return (
        recorded.channel_tag == replacement.channel_tag
        and _values_match(recorded.source, replacement.source)
        and _values_match(recorded.departed, replacement.departed)
        and _values_match(recorded.landing, replacement.landing)
    )


def _same_bounded_channel_departure(
    recorded: RegressionWitness,
    replacement: RegressionWitness,
) -> bool:
    """Whether replay preserved the incident's first channel transition."""

    return (
        recorded.channel_tag == replacement.channel_tag
        and _values_match(recorded.source, replacement.source)
        and _values_match(recorded.departed, replacement.departed)
    )


def _shared_causal_suffix(
    recorded: RegressionWitness,
    replacement: RegressionWitness | None,
) -> tuple[CausalOccurrence, ...]:
    """Exact downstream pipeline shared by two effect-backward witnesses."""

    if replacement is None or not _same_bounded_channel_outcome(recorded, replacement):
        return ()
    common: list[CausalOccurrence] = []
    for left, right in zip(recorded.cause, replacement.cause, strict=False):
        if not _same_occurrence(left, right):
            break
        common.append(left)
    return tuple(common) if len(common) >= 2 else ()


def _regression_ownership(
    plc: PLC,
    witness: RegressionWitness,
    events: Sequence[Any],
    proposal_tags: set[str],
    *,
    start_scan: int,
    end_scan: int,
    replacement_witness: RegressionWitness | None = None,
    cause_replayed: Callable[..., bool] = _regression_cause_replayed,
) -> _RegressionOwnership:
    """Judge the recorded branch and any replacement inside bounded replay."""

    bounded_events = tuple(event for event in events if event.scan <= end_scan)
    source_preserved = _values_match(
        plc.state.tags.get(witness.channel_tag), witness.source
    ) and not any(
        tag == witness.channel_tag and not _values_match(after, witness.source)
        for event in bounded_events
        for tag, _before, after in event.transitions
    )
    changed_writes_silenced = not cause_replayed(
        plc,
        witness,
        start_scan=start_scan,
        end_scan=end_scan,
    )
    replacement_cause = (
        replacement_witness.causal_spine if replacement_witness is not None else None
    )
    replacement_owned = (
        bool(proposal_tags & replacement_cause) if replacement_cause is not None else None
    )
    replacement_replays_recorded = (
        _same_bounded_channel_outcome(witness, replacement_witness)
        and witness.causal_spine.issubset(replacement_cause)
        if replacement_cause is not None and replacement_witness is not None
        else None
    )
    unrelated_departure = (
        replacement_cause is not None
        and replacement_replays_recorded is False
        and replacement_owned is False
    )
    shared_suffix = _shared_causal_suffix(witness, replacement_witness)
    branch_replaced = (
        bool(shared_suffix) and replacement_replays_recorded is False and replacement_owned is False
    )
    proposal_owned_detour = (
        replacement_witness is not None
        and replacement_owned is True
        and replacement_replays_recorded is False
        and _same_bounded_channel_departure(witness, replacement_witness)
        and not _same_bounded_channel_outcome(witness, replacement_witness)
    )
    cause_silenced = changed_writes_silenced or branch_replaced or proposal_owned_detour
    unrelated_departure = unrelated_departure and not shared_suffix
    return _RegressionOwnership(
        source_preserved=source_preserved,
        cause_silenced=cause_silenced,
        replacement_cause=replacement_cause,
        replacement_owned=replacement_owned,
        replacement_replays_recorded=replacement_replays_recorded,
        unrelated_departure=unrelated_departure,
        neutralized=(
            (source_preserved and cause_silenced)
            or branch_replaced
            or proposal_owned_detour
            or unrelated_departure
        ),
        shared_suffix=shared_suffix,
    )


@dataclass(frozen=True)
class ReplayHooks:
    """Facade callbacks whose current bindings must survive monkeypatching."""

    regression_cause_replayed: Callable[..., bool]
    incident_regression_witness: Callable[[Any, DeviationIncident], RegressionWitness | None]
    build_deviation_incident: Callable[..., DeviationIncident]
    implicated_writers: Callable[[Any, str, Any], list[int]]
    suppression_nominations: Callable[..., list[ActionPair]]


def _default_replay_hooks() -> ReplayHooks:
    return ReplayHooks(
        regression_cause_replayed=_regression_cause_replayed,
        incident_regression_witness=incident_regression_witness,
        build_deviation_incident=build_deviation_incident,
        implicated_writers=_implicated_writers,
        suppression_nominations=_skiff_suppression_nominations,
    )


def build_replay_fn(
    cp_fork: PLC,
    cp_trend: int,
    pilot_rungs: Sequence[Any],
    steps: Sequence[ReplayStep],
    *,
    ctx: _PilotContext,
    incident: ReplayIncident | None = None,
    hooks: ReplayHooks | None = None,
) -> ReplayFn:
    """Build the bounded correction replay callback for an investigation."""

    hooks = hooks or _default_replay_hooks()
    incident = incident or ReplayIncident()
    resting = ctx.resting
    edge_tags = ctx.edge_tags
    target_tag = ctx.target.tag
    target_value = ctx.target.value
    pdg = ctx.pdg
    program = ctx.program
    steerable = ctx.steerable
    opaque_loop = ctx.opaque_loop
    pipeline_internal_tags = ctx.pipeline_internal_tags
    route = ctx.route
    prior = getattr(ctx, "domain_prior", None)
    clear_only = getattr(ctx, "clear_only", frozenset())
    bearing_channel_tag = incident.channel_tag
    bearing_target_value = incident.channel_target
    terminal_letrun_role_tags = incident.terminal_role_tags
    replay_watch_roles = incident.watch_roles
    departure_bearing = incident.departure_bearing
    regression_witness = incident.regression_witness
    earned_work = incident.earned_work
    progress_anchor = incident.progress_anchor
    regression_progress_floor = incident.regression_progress_floor
    replay_cache: dict[tuple[bool, tuple[tuple[str, Any], ...]], ReplayOutcome] = {}

    def _replay(
        holds: tuple[Any, ...],
        *,
        prove_continuation: bool = False,
    ) -> ReplayOutcome:
        from pyrung.core.analysis.pilot.coast import CoastSession

        replay_key = (
            prove_continuation,
            tuple(_candidates._proposal_identity(hold) for hold in holds),
        )
        cached = replay_cache.get(replay_key)
        if cached is not None:
            return cached

        def _remember(outcome: ReplayOutcome) -> ReplayOutcome:
            replay_cache[replay_key] = outcome
            return outcome

        probe = fork_with_pilot_rungs(cp_fork, pilot_rungs)
        probe_pilot_rungs = list(pilot_rungs)
        scope = _target_unresolved_condition(probe, target_tag, target_value)
        probe_pilot_rungs.extend(_pilot_rungs_from_proposals(list(holds), scope))
        _set_pilot_rungs(probe, probe_pilot_rungs)
        session = CoastSession(probe, kind="replay", kernel_budget=False)
        session.arm_avoid(getattr(ctx, "avoid_pred", None))
        eject_receipt: Any = None
        if bearing_channel_tag is not None:
            session.arm_pens((bearing_channel_tag,))
        for step in steps:
            if step.kind == "pulse" and step.inputs:
                _apply_pulse(probe, list(step.inputs), resting, edge_tags, session=session)
            elif step.kind == "letrun":
                eject_receipt = _coast_holding_state(
                    probe,
                    target_tag,
                    target_value,
                    replay_watch_roles,
                    budget=max(1, step.scans),
                    session=session,
                )
            elif step.kind == "bearing_coast" and step.channel_tag is not None:
                # Replay the operation that was physically attempted, not the
                # semantic channel later selected to own its ejection.  The
                # incident channel becomes an additional departure trigger
                # only for the recorded operation that produced it.  Earlier
                # operations are the committed prefix and must reach their own
                # boundaries before the incident watch is armed.
                from pyrung.core.analysis.pilot.steer import (
                    _coast_to_bearing,
                    _terminal_target_trigger,
                )

                _trajectory, eject_receipt = _coast_to_bearing(
                    probe,
                    step.channel_tag,
                    step.channel_target,
                    watched_tags=frozenset(),
                    session=session,
                    boundary=step.channel_boundary,
                    terminal_target=_terminal_target_trigger(probe, ctx.target),
                    departure_tags=(
                        tuple(replay_watch_roles)
                        if _step_owns_departure(step, regression_witness)
                        else ()
                    ),
                    budget=(max(1, step.scans) if regression_witness is not None else None),
                )
            else:
                session.dwell(max(1, step.scans))
        incident_replay_end = probe.state.scan_id
        snap = dict(probe.state.tags)
        avoid_fired = any(event.kind == AVOID for event in session.events)
        terminal_reached = not avoid_fired and target_reached(
            snap,
            target_tag,
            target_value,
            ctx.target.predicate,
        )
        proposal_tags = {_candidates._proposal_pair(hold)[0] for hold in holds}
        replacement_incident: DeviationIncident | None = None
        replacement_witness: RegressionWitness | None = None
        if regression_witness is not None:
            replacement_scan = _replacement_departure_scan(
                regression_witness,
                session.events,
            )
            if replacement_scan is not None:
                replacement_incident = hooks.build_deviation_incident(
                    anchor_scan=cp_fork.state.scan_id,
                    end_scan=incident_replay_end,
                    action=(),
                    bearing=((regression_witness.channel_tag, regression_witness.source),),
                    before_snap=dict(cp_fork.state.tags),
                    after_snap=snap,
                    timeline=session.events,
                    channel_tag=regression_witness.channel_tag,
                )
                replacement_witness = hooks.incident_regression_witness(
                    probe,
                    replacement_incident,
                )
        ownership = (
            _regression_ownership(
                probe,
                regression_witness,
                session.events,
                proposal_tags,
                start_scan=cp_fork.state.scan_id,
                end_scan=incident_replay_end,
                replacement_witness=replacement_witness,
                cause_replayed=hooks.regression_cause_replayed,
            )
            if regression_witness is not None
            else None
        )
        progress_erased = (
            ownership is not None
            and ownership.neutralized
            and earned_work is not None
            and regression_progress_floor is not None
            and earned_work.receipt(regression_progress_floor, snap).movement
            is EarnedWorkMovement.BACKWARD
        )
        # Equal channel values do not make two departures the same occurrence.
        # If the replacement writer belongs to a world that has advanced beyond
        # the recorded incident's earned-work floor, it is a later program
        # occurrence (for example, the genuine station Hold after a repaired
        # premature Hold).  Do not recursively compose a correction for it.
        replacement_after_progress = (
            replacement_witness is not None
            and replacement_witness.owner_snapshot is not None
            and earned_work is not None
            and regression_progress_floor is not None
            and earned_work.receipt(
                regression_progress_floor,
                replacement_witness.owner_snapshot,
            ).movement
            is EarnedWorkMovement.FORWARD
        )
        neutralized = (
            ownership is not None
            and (ownership.neutralized or replacement_after_progress)
            and not progress_erased
        )
        source_preserved = ownership is not None and ownership.source_preserved
        continuation_snapshot = snap
        continuation: FrontierStatus = Unknown(
            "bounded replay did not witness the target",
            ((target_tag, target_value),),
        )
        if terminal_reached:
            continuation = Reachable(("actual-target-witness",))
        elif neutralized and prove_continuation:
            continuation_receipt = _coast_holding_state(
                probe,
                target_tag,
                target_value,
                ((bearing_channel_tag,) if bearing_channel_tag is not None else ()),
                budget=min(
                    _COAST_BUDGET,
                    max(1, int(getattr(ctx, "max_scans", _COAST_BUDGET))),
                ),
                reached_fn=(
                    (
                        lambda state: target_reached(
                            state.tags,
                            target_tag,
                            target_value,
                            ctx.target.predicate,
                        )
                    )
                    if ctx.target.predicate is not None
                    else None
                ),
                session=session,
            )
            continuation_snapshot = dict(probe.state.tags)
            if target_reached(
                continuation_snapshot,
                target_tag,
                target_value,
                ctx.target.predicate,
            ):
                continuation = Reachable(("actual-target-witness", "coast"))
            else:
                continuation = Unknown(
                    "coast-only continuation did not reach the target"
                    f" ({continuation_receipt.stop_reason})",
                    ((target_tag, target_value),),
                )
        if logger.isEnabledFor(logging.DEBUG):
            roles = terminal_letrun_role_tags or ()
            logger.debug(
                "replay probe: cp_scan=%s end_scan=%s steps=%d shape=%s channel=%s=%r roles=%s",
                cp_fork.state.scan_id,
                probe.state.scan_id,
                len(steps),
                "letrun" if terminal_letrun_role_tags is not None else "bearing_coast",
                bearing_channel_tag,
                snap.get(bearing_channel_tag) if bearing_channel_tag else None,
                {tag: snap.get(tag) for tag in roles},
            )

        if bearing_channel_tag is not None:
            reached = (
                not avoid_fired
                and terminal_letrun_role_tags is None
                and _values_match(snap.get(bearing_channel_tag), bearing_target_value)
            )
            neutralized_reason = None
            if neutralized and regression_witness is not None:
                neutralized_reason = (
                    "recorded regression neutralized: "
                    f"preserved {bearing_channel_tag}={regression_witness.source!r} "
                    f"and suppressed its {len(regression_witness.cause)}-write causal branch"
                    if source_preserved
                    else "recorded cause silenced before an unrelated replacement departure"
                )
            progressed = neutralized_reason
            earned_work_advanced = False
            if (
                not reached
                and progressed is None
                and earned_work is not None
                and progress_anchor is not None
                and earned_work.receipt(progress_anchor, snap).movement
                is EarnedWorkMovement.FORWARD
            ):
                earned_work_advanced = True
                progressed = "target-relative progress advanced"
            cause_repeated = regression_witness is not None and not neutralized
            rejection_reason = (
                "correction erased the recorded incident's progress receipt"
                if progress_erased
                else (
                    "recorded regression cause replayed; correction masked its result"
                    if cause_repeated
                    else (
                        f"{bearing_channel_tag} -> {bearing_target_value!r} reached={reached}"
                        + (
                            f" (eject coast: {eject_receipt.stop_reason})"
                            if eject_receipt is not None
                            else ""
                        )
                    )
                )
            )
            accepted = (
                terminal_reached
                or earned_work_advanced
                or (not cause_repeated and (reached or progressed is not None))
            ) and not progress_erased
            return _remember(
                ReplayOutcome(
                    accepted=accepted,
                    trend=None,
                    snapshot=snap,
                    reason=(progressed if accepted else rejection_reason) or rejection_reason,
                    continuation=continuation,
                    continuation_snapshot=continuation_snapshot,
                    landed=(
                        reached
                        or (
                            neutralized
                            and not source_preserved
                            and replacement_witness is not None
                            and regression_witness is not None
                            and not _values_match(
                                replacement_witness.landing,
                                regression_witness.landing,
                            )
                        )
                    ),
                    justification=(
                        (
                            ReplayJustification.REACHED
                            if isinstance(continuation, Reachable)
                            else (
                                ReplayJustification.NEUTRALIZED
                                if neutralized_reason is not None
                                else ReplayJustification.ADVANCED
                                if progressed is not None
                                else None
                            )
                        )
                        if accepted
                        else None
                    ),
                    replacement_cause=(
                        ownership.replacement_cause or frozenset()
                        if ownership is not None
                        else frozenset()
                    ),
                    replacement=(
                        ReplacementEvidence(
                            plc=probe,
                            incident=replacement_incident,
                            witness=replacement_witness,
                            shared_suffix=ownership.shared_suffix,
                        )
                        if ownership is not None
                        and replacement_incident is not None
                        and replacement_witness is not None
                        and not replacement_after_progress
                        else None
                    ),
                )
            )

        if terminal_letrun_role_tags is not None:
            reached = terminal_reached
            return _remember(
                ReplayOutcome(
                    accepted=reached,
                    trend=None,
                    snapshot=snap,
                    reason=f"{target_tag} -> {target_value!r} reached={reached}",
                    justification=ReplayJustification.REACHED if reached else None,
                    continuation=(
                        Reachable(("actual-target-witness",))
                        if reached
                        else Unknown(
                            "bounded terminal replay did not reach the target",
                            ((target_tag, target_value),),
                        )
                    ),
                    continuation_snapshot=snap,
                )
            )

        if departure_bearing:
            held = all(_values_match(snap.get(tag), value) for tag, value in departure_bearing)
            return _remember(
                ReplayOutcome(
                    accepted=held,
                    trend=None,
                    snapshot=snap,
                    reason=f"bearing {'held' if held else 'departed'} at bounded replay",
                    justification=ReplayJustification.BEARING_HELD if held else None,
                    continuation=continuation,
                    continuation_snapshot=continuation_snapshot,
                )
            )

        tree = trace_back(
            target_tag,
            target_value,
            snap,
            pdg,
            program,
            steerable,
            clear_only=clear_only,
            opaque_loop=opaque_loop,
            pipeline_internal_tags=pipeline_internal_tags,
            route=route,
            prior=prior,
        )
        trend = tree.unsatisfied_count()
        return _remember(
            ReplayOutcome(
                accepted=trend <= cp_trend,
                trend=trend,
                snapshot=snap,
                reason=f"trend {trend} <= checkpoint {cp_trend}",
                justification=ReplayJustification.ADVANCED if trend < cp_trend else None,
                continuation=continuation,
                continuation_snapshot=continuation_snapshot,
            )
        )

    _replay.with_continuation = lambda holds: _replay(  # ty: ignore[unresolved-attribute]
        holds, prove_continuation=True
    )
    return _replay


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
    hooks: ReplayHooks | None = None,
) -> ExcursionResult:
    """Diagnose an excursion and replay-validate candidate holds."""
    from pyrung.core.analysis.pdg import resolve_rung

    hooks = hooks or _default_replay_hooks()
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
            for ni in hooks.implicated_writers(fork, tag, pdg):
                node = pdg.rung_nodes[ni]
                ro = resolve_rung(program, node)
                if ro is None:
                    continue
                if _can_produce(_written_value_for_tag(ro, tag), desired):
                    continue
                holds = break_guard_holds(ro, settled_snap, mini_ctx)
                if holds is None:
                    holds = hooks.suppression_nominations(
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
