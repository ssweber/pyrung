"""Execute and verify one proposed PILOT action or wait.

The `_try_*` functions prepare a fork, settle prerequisite regions, pulse an
action or coast through a dwell, and pass the resulting trial to
``verify.verify_gates``. They return an ``_AttemptResult`` containing the
verdict, receipts, transition observations, or the exact execution of a
verification-time excursion for the drive loop to investigate.

This module does not apply observations, replace the committed world, manage
checkpoints, or install corrections.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.advance import estimate_owned_boundary_scans
from pyrung.core.analysis.pilot.avoid import _avoid_violations
from pyrung.core.analysis.pilot.bootstrap import (
    selected_route_landing_expectation,
    unexplained_route_landing_tags,
)
from pyrung.core.analysis.pilot.causal import action_caused_change as _action_caused_change
from pyrung.core.analysis.pilot.coast import (
    _COAST_BUDGET,
    LIMITS,
    TARGET,
    CoastSession,
    CoastTrigger,
    _coast_holding_state,
    _coast_until,
    _has_pending_effects,
    coast_departure_tags,
    predicate_trigger,
    value_trigger,
)
from pyrung.core.analysis.pilot.compass import WAIT, ActionPair, CompassObservation, is_action
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObservation,
    consumer_stop_reached,
    effect_reached_consumer,
    expectation_from_writer,
    observe_execution_window,
    promote_route_landing_observations,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    BatchPulse,
    Bearing,
    Coast,
    Dwell,
    IntrascanPulse,
    LandingReceiptAuthority,
    ObserveScan,
    OrientationWorld,
    ProgramScan,
    Pulse,
    PulseHorizon,
    StopCondition,
    StopReceipt,
    act_identity,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _atom_condition,
    _constraint_condition,
    _merged_pilot_rungs,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.pipeline_graph import target_reachable_values
from pyrung.core.analysis.pilot.trace import scan_transient_rest, target_reached
from pyrung.core.analysis.pilot.types import (
    ChannelMotion,
    ExecutionReceipt,
    PilotGateEvent,
    _ActionPair,
    _AttemptResult,
    _ExecutedAttempt,
    _HoldLogEntry,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _PulseState,
    capture_execution_spans,
)
from pyrung.core.analysis.pilot.verify import verify_gates
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _rung_identity
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.compass import TransitionCause
    from pyrung.core.analysis.pilot.working_theory import ScanEntryConfiguration
    from pyrung.core.runner import PLC


# ---------------------------------------------------------------------------
# Cone settlement — dwell control
# ---------------------------------------------------------------------------

_SETTLE_CONE_CEILING = LIMITS.cone_ceiling
_LETRUN_DWELL_CEILING = LIMITS.dwell_ceiling


@dataclass(frozen=True)
class _ExecutionLaunch:
    """One prepared execution fork and its exact scan-entry configuration."""

    fork: PLC
    scan_before: int
    entry_snap: dict[str, Any]
    configurations: tuple[ScanEntryConfiguration, ...]


def _fork_for_execution(
    state: _PilotState,
    configurations: tuple[ScanEntryConfiguration, ...],
) -> _ExecutionLaunch:
    """Prepare one fork and apply declared configuration before its first scan."""

    fork = fork_with_pilot_rungs(state.work, state.pilot_rungs)
    scan_before = fork.state.scan_id
    entry_snap = dict(fork.state.tags)
    assignments = tuple(
        assignment for configuration in configurations for assignment in configuration.assignments
    )
    names = tuple(tag for tag, _value in assignments)
    if len(set(names)) != len(names):
        raise ValueError("scan-entry configurations assign one tag more than once")
    if assignments:
        fork.patch(dict(assignments))
        entry_snap.update(assignments)
    return _ExecutionLaunch(
        fork=fork,
        scan_before=scan_before,
        entry_snap=entry_snap,
        configurations=tuple(configurations),
    )


def _charted_route_values(
    ctx: _PilotContext,
    channel_tag: str,
    target_value: Any,
) -> tuple[Any, ...]:
    """Return exact channel values with a concrete path to one route target."""

    values: list[Any] = []
    for graph in ctx.compass.chart_graphs:
        if graph.role.channel_tag != channel_tag:
            continue
        for value in target_reachable_values(graph, target_value):
            if not any(_values_match(value, current) for current in values):
                values.append(value)
    return tuple(values)


class StaleBearingError(RuntimeError):
    """The world changed after orientation and before execution."""


def _reconcile_landing_receipts(
    immediate: tuple[EffectObservation, ...],
    landing: tuple[EffectObservation, ...],
    *,
    heading: Any,
    final_landing: dict[str, Any],
) -> tuple[EffectObservation, ...]:
    """Keep local route facts without making them outrank a completed bearing.

    A selected trace may name one local handoff through a step channel while
    the program takes another local path through that same operation.  The
    missed handoff remains factual.  Once the enclosing structural boundary
    has both landed and reached its exact selected consumer, however, that
    subordinate miss did not fail this Bearing.  Mark it as subsumed so the
    next fresh Orientation owns any consequence in the landed world.

    The structural channel itself, terminal targets, and landings without an
    exact completed boundary receipt remain authoritative failures.
    """

    if (
        not landing
        or heading is None
        or not _values_match(
            final_landing.get(heading.channel_tag),
            heading.target_value,
        )
    ):
        return landing
    boundary = (heading.channel_tag, heading.target_value)
    boundary_completed = any(
        observation.obligation.boundary == boundary
        and observation.appeared is not None
        and observation.consumer_read is not None
        and effect_reached_consumer(observation)
        for observation in (*immediate, *landing)
    )
    if not boundary_completed:
        return landing
    return tuple(
        replace(
            observation,
            disposition="SUBSUMED",
            detail=(
                "the enclosing structural boundary completed through an exact "
                "consumer; the missed local route is retained for the fresh "
                "landing read"
            ),
        )
        if observation.disposition in {"OVERWRITTEN", "STRANDED", "DISPLACED"}
        and observation.obligation.consumer is not None
        and observation.obligation.tag != heading.channel_tag
        else observation
        for observation in landing
    )


def _reconcile_completed_handoffs(
    observations: tuple[EffectObservation, ...],
) -> tuple[EffectObservation, ...]:
    """Let an exact consumer receipt own a weaker producer-only view.

    Immediate and route-landing expectations can observe the same physical
    write at different resolutions.  A producer-only expectation reports the
    value as overwritten at scan exit, while the route expectation can prove
    that its selected consumer read that exact occurrence before cleanup.  The
    overwrite remains recorded, but it is not a failed handoff.

    This is deliberately occurrence-addressed.  A different write, a missed
    selected consumer, or a transient terminal target remains authoritative.
    """

    completed = tuple(
        observation
        for observation in observations
        if observation.appeared is not None
        and observation.consumer_read is not None
        and effect_reached_consumer(observation)
    )
    selected_landings = tuple(
        observation
        for observation in observations
        if observation.appeared is not None
        and observation.obligation.boundary is not None
        and observation.obligation.boundary[0] == observation.obligation.tag
    )

    def completed_same_occurrence(observation: EffectObservation) -> bool:
        if (
            observation.disposition not in {"OVERWRITTEN", "STRANDED", "DISPLACED"}
            or observation.appeared is None
            or observation.obligation.consumer is not None
            or observation.obligation.terminal_target
        ):
            return False
        return any(
            receipt is not observation
            and receipt.appeared == observation.appeared
            and receipt.obligation.producer == observation.obligation.producer
            and receipt.obligation.tag == observation.obligation.tag
            and _values_match(receipt.obligation.value, observation.obligation.value)
            for receipt in completed
        )

    def superseded_by_selected_landing(observation: EffectObservation) -> bool:
        """A downstream route producer may legitimately bypass this writer."""

        if (
            observation.disposition != "ABSENT"
            or observation.obligation.consumer is not None
            or observation.obligation.terminal_target
        ):
            return False
        return any(
            receipt.obligation.tag == observation.obligation.tag
            and receipt.obligation.producer != observation.obligation.producer
            for receipt in selected_landings
        )

    return tuple(
        replace(
            observation,
            disposition="SUBSUMED",
            detail=(
                "the exact appeared write reached a selected consumer before "
                "program cleanup; the producer-only overwrite is retained as "
                "subordinate evidence"
            ),
        )
        if completed_same_occurrence(observation)
        else replace(
            observation,
            disposition="SUBSUMED",
            detail=(
                "a selected downstream route producer appeared; the earlier "
                "producer-only absence is subordinate to that landing"
            ),
        )
        if superseded_by_selected_landing(observation)
        else observation
        for observation in observations
    )


def _executed_attempt(bearing: Bearing, pulse: _PulseState) -> _ExecutedAttempt:
    """Bind immediate and route-landing expectations to one physical window."""

    action_scan = (
        None
        if isinstance(bearing.act, (Coast, Dwell, ObserveScan, ProgramScan))
        else pulse.action_scan
    )
    immediate = observe_execution_window(
        bearing.expectation,
        pulse.fork,
        scan_before=pulse.scan_before,
        action_scan=action_scan,
        coast_receipt=pulse.coast_receipt,
        kernel_scan_ids=pulse.kernel_scan_ids,
        projection_at=pulse.projection_at,
    )
    landing_expectation = None
    projections = ()
    heading = None
    orientation = bearing.orientation
    route_landing_admissible = pulse.coast_receipt is None or not pulse.coast_receipt.avoided
    if (
        route_landing_admissible
        and orientation is not None
        and not isinstance(
            bearing.act,
            (ObserveScan, ProgramScan, IntrascanPulse),
        )
    ):
        world = orientation.world
        ctx = world.context
        heading = bearing.act.policy.heading
        route = heading.route if heading is not None else None
        # ProgramStep already owns the exact present-tense continuation and
        # its input handoffs. The act may still be navigationally ROUTE-owned;
        # receipt authority is an orthogonal, explicitly carried reading.
        program_handoff = (
            bearing.act.policy.landing_receipt_authority is LandingReceiptAuthority.PROGRAM_STEP
        )
        route_effect_tag = route.effect_tag or route.channel_tag if route is not None else None
        charted_target_values = (
            _charted_route_values(ctx, route.channel_tag, route.target_value)
            if route is not None
            else ()
        )
        charted_route = (
            route is not None and route_effect_tag == ctx.target.tag and charted_target_values
        )
        charted_landing = bool(
            charted_route
            and any(
                _values_match(pulse.snap.get(route_effect_tag), value)
                or _values_match(pulse.snap.get(route.channel_tag), value)
                for value in charted_target_values
            )
        )
        if charted_route:
            # The chart landing receipt owns every write after the selected
            # edge appears: a downstream corridor value is progress, while an
            # off-route value is attributed to its exact final writer.  Keep
            # A missing selected producer remains an immediate failure unless
            # this exact execution retained another value on the same chart.
            # In that case the terminal receipt belongs to a later coast; the
            # current act is judged by its concrete intermediate landing.
            immediate = tuple(
                observation
                for observation in immediate
                if not (
                    observation.obligation.tag == route_effect_tag
                    and (
                        observation.disposition in {"OVERWRITTEN", "STRANDED", "DISPLACED"}
                        or (charted_landing and observation.disposition == "ABSENT")
                    )
                )
            )
        channel_tags: frozenset[str] = frozenset()
        charted_values: dict[str, tuple[Any, ...]] = {}
        unexplained_landing = frozenset()
        if not program_handoff:
            channel_tags = frozenset(
                {
                    ctx.target.tag,
                    *(
                        role.channel_tag
                        for role in (*ctx.pipeline_roles, *ctx.chart_roles)
                        if role.channel_tag not in ctx.opaque_loop
                        and role.channel_tag not in ctx.edge_tags
                        and role.channel_tag not in ctx.clear_only
                        and not scan_transient_rest(
                            role.channel_tag,
                            ctx.pdg,
                            ctx.program,
                        )[0]
                    ),
                }
            )
            charted_values = (
                {
                    ctx.target.tag: charted_target_values,
                    route.channel_tag: charted_target_values,
                }
                if route is not None
                else {}
            )
            unexplained_landing = unexplained_route_landing_tags(
                world.frame.tree,
                ctx.pdg,
                ctx.program,
                landing=pulse.snap,
                steerable=ctx.steerable,
                channel_tags=channel_tags,
                charted_values=charted_values,
            )
        if unexplained_landing:
            exact_scan_ids = tuple(
                scan_id
                for scan_id in pulse.kernel_scan_ids
                if pulse.scan_before < scan_id <= pulse.fork.state.scan_id
                and (action_scan is None or scan_id >= action_scan)
            )
            projections = tuple(
                projection
                for scan_id in exact_scan_ids
                if (projection := pulse.projection_at(scan_id)) is not None
            )
        if unexplained_landing and len(projections) == len(exact_scan_ids):
            # A literal-looking register can be discovered as a chart role and
            # still be an opaque indirection or a same-scan handoff that
            # provably returns to rest.  Both remain exact execution evidence,
            # but neither is a retained route coordinate.  Keep the terminal
            # target authoritative; immediate expectations still own exact
            # transient consumers when the selected bearing depends on one.
            landing_expectation = selected_route_landing_expectation(
                world.frame.tree,
                ctx.pdg,
                ctx.program,
                projections,
                landing=pulse.snap,
                steerable=ctx.steerable,
                channel_tags=channel_tags,
                charted_values=charted_values,
            )
            if bearing.expectation is not None and landing_expectation is not None:
                distinct = tuple(
                    obligation
                    for obligation in landing_expectation.obligations
                    if obligation not in bearing.expectation.obligations
                )
                landing_expectation = EffectExpectation(distinct) if distinct else None
    landing = observe_execution_window(
        landing_expectation,
        pulse.fork,
        scan_before=pulse.scan_before,
        action_scan=action_scan,
        coast_receipt=pulse.coast_receipt,
        kernel_scan_ids=pulse.kernel_scan_ids,
        projection_at=pulse.projection_at,
    )
    if landing:
        landing = promote_route_landing_observations(
            landing,
            projections,
            final_landing=pulse.snap,
        )
        landing = _reconcile_landing_receipts(
            immediate,
            landing,
            heading=(bearing.act.policy.heading),
            final_landing=pulse.snap,
        )
    observations = _reconcile_completed_handoffs((*immediate, *landing))
    source_snap = getattr(pulse, "source_snap", None)
    after_snap = getattr(pulse, "snap", dict(pulse.fork.state.tags))
    applied_configurations = tuple(getattr(pulse, "applied_configurations", ()))
    execution = ExecutionReceipt(
        before_snap=(source_snap or getattr(pulse, "action_snap", after_snap)),
        after_snap=after_snap,
        channel_motion=getattr(pulse, "channel_motion", ChannelMotion()),
        coast_receipt=pulse.coast_receipt,
        timeline=pulse.timeline,
        effect_observations=tuple(
            observation.diagnostic_snapshot() for observation in observations
        ),
        replay_motion=getattr(pulse, "replay_motion", ChannelMotion()),
        spans=capture_execution_spans(pulse.fork, pulse.kernel_scan_ids),
        source_scan=pulse.scan_before,
        source_world=bearing.world_key,
        decision_identity=act_identity(bearing.act),
        applied_configurations=applied_configurations,
        entry_snap=(source_snap if applied_configurations else None),
        stop=getattr(pulse, "stop_receipt", None),
    )
    return _ExecutedAttempt(
        pulse=pulse,
        bearing=bearing,
        effect_observations=observations,
        landing_expectation=landing_expectation,
        execution=execution,
    )


def _install_prerequisites(
    state: _PilotState,
    prerequisites: tuple[PilotRung, ...],
    *,
    source: str = "prerequisite",
) -> None:
    """Install only prerequisite rungs that do not already have an owner."""
    existing = {_rung_identity(rung) for rung in state.pilot_rungs}
    new_pilot_rungs = tuple(rung for rung in prerequisites if _rung_identity(rung) not in existing)
    if not new_pilot_rungs:
        return
    state.pilot_rungs = _merged_pilot_rungs(new_pilot_rungs, state.pilot_rungs)
    state.hold_log.append(
        _HoldLogEntry(
            scan=state.work.state.scan_id,
            source=source,
            pilot_rungs=new_pilot_rungs,
        )
    )


def _settle_watched_tags(
    fork: PLC,
    watched_tags: frozenset[str],
    *,
    floor: int = LIMITS.cone_floor,
    ceiling: int = _SETTLE_CONE_CEILING,
    reached_fn: Callable[[dict[str, Any]], bool] | None = None,
    session: CoastSession | None = None,
) -> list[dict[str, Any]]:
    """Coast *fork* until the watched tags stop moving — dwell control only.

    Thin wrapper over :meth:`CoastSession.settle` (see its docstring for the
    fixpoint/floor/transient semantics); returns the per-scan trajectory.
    *session*, when given, records the dwell onto that session's timeline.

    Settle never accepts or rejects.  Attributing the trajectory to one of the
    five verify outcomes — who moved what — is the caller's job via ``cause()``.
    """
    if session is None:
        session = CoastSession(fork, kind="settle")
    assert session.plc is fork
    receipt = session.settle(watched_tags, floor=floor, ceiling=ceiling, reached_fn=reached_fn)
    return list(receipt.trajectory)


def _pen_tags(state: _PilotState, ctx: _PilotContext) -> frozenset[str]:
    """The trial recorder's pen universe.

    Profile Done bits (a fire-then-reset watchdog pulse must be two recorded
    transitions, not a net no-op) plus the loop's watch tags and pipeline role
    channels (bearing departures land as recorded events with exact scans).
    Accumulator registers are excluded: a change-pen on a per-scan-churny tag
    would collapse every fold to step-mode, and acc membership in the
    incident's changed set is served by endpoint diff instead.
    """
    from pyrung.core.analysis.pilot.advance import iter_advance_owners

    dones: set[str] = set()
    accs: set[str] = set()
    if ctx.program is not None:
        for owner in iter_advance_owners(ctx.program):
            profile = owner.profile
            if profile.done is not None:
                dones.add(profile.done.name)
            if profile.accumulator is not None:
                accs.add(profile.accumulator.name)
    tags = dones | set(state.watch_tags) | {r.channel_tag for r in ctx.pipeline_roles}
    return frozenset(tags - accs)


def _watched_tags(frame: _IterationFrame, ctx: _PilotContext) -> frozenset[str]:
    """The tags whose motion matters this iteration.

    The trace-tree prerequisites toward the goal — satisfied *and* unsatisfied,
    so a prerequisite slipping back (divergence) is visible, not just one being
    met — plus the channel / opaque-loop registers.  Steerable inputs are
    excluded: those are held, not watched.
    """
    tags = {n.tag for n in frame.tree.iter_nodes() if not n.is_steerable}
    return frozenset(tags | ctx.opaque_loop)


# ---------------------------------------------------------------------------
# Pulse execution
# ---------------------------------------------------------------------------


def _apply_actions(
    actions: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    *,
    entry_configurations: tuple[ScanEntryConfiguration, ...] = (),
    horizon: PulseHorizon = PulseHorizon.ASSERTION_SCAN,
    consumer_boundary: Any = None,
    stop_condition: StopCondition | None = None,
) -> _PulseState:
    key_config = state.key_config
    assert key_config is not None

    stop = stop_condition or StopCondition(horizon, consumer_boundary)
    horizon = stop.horizon
    consumer_boundary = stop.consumer_boundary
    launch = _fork_for_execution(state, entry_configurations)
    fork = launch.fork
    scan_before = launch.scan_before
    source_snap = launch.entry_snap
    session = CoastSession(
        fork,
        kind="pulse",
        kernel_budget=(None if getattr(ctx, "collect_action_attribution", True) else False),
    )
    session.arm_avoid(ctx.avoid_pred)
    session.arm_pens(_pen_tags(state, ctx))
    patch = {t: v for t, v in actions}
    needs_edge = any(
        t in ctx.edge_tags and not _values_match(value, ctx.resting.get(t, False))
        for t, value in patch.items()
    )

    if needs_edge:
        release = {t: ctx.resting.get(t, False) for t in patch if t in ctx.edge_tags}
        if release:
            fork.patch(release)
            session.step_kernel()
            session.note_pens()

    fork.patch(patch)
    session.step_kernel()
    session.note_pens()
    action_snap = dict(fork.state.tags)
    action_scan = fork.state.scan_id

    # Stop the settle the scan the target holds — otherwise the watched-tag fixpoint
    # coast (and the delayed-effect fast-forward) steps straight through a
    # one-scan transient (STARTING → EXECUTE) and the post-settle check never
    # sees it.  Landing the fork on the transient lets verify confirm it.
    def _reached(tags: dict[str, Any]) -> bool:
        return target_reached(tags, ctx.target.tag, ctx.target.value, ctx.target.predicate)

    if _reached(action_snap) or horizon is PulseHorizon.ASSERTION_SCAN:
        wait_snaps: list[dict[str, Any]] = []
    elif horizon is PulseHorizon.LOOKAHEAD_SCAN:
        session.step_kernel()
        session.note_pens()
        wait_snaps = [dict(fork.state.tags)]
    else:
        assert horizon is PulseHorizon.CONSUMER_BOUNDARY
        assert consumer_boundary is not None
        wait_snaps = []
        consumer_scan = scan_before + consumer_boundary.consumer_scan_offset
        while fork.state.scan_id < consumer_scan:
            session.step_kernel()
            session.note_pens()
            wait_snaps.append(dict(fork.state.tags))
            if _reached(wait_snaps[-1]):
                break

    post_pulse_snap = dict(fork.state.tags)
    post_pulse_key = _pilot_world_key(
        post_pulse_snap,
        key_config,
        state.pilot_rungs,
        getattr(state, "active_requirements", ()),
    )
    fork_snap = dict(fork.state.tags)
    if wait_snaps and wait_snaps[-1] != fork_snap:
        wait_snaps.append(fork_snap)
    elif not wait_snaps and action_snap != fork_snap:
        wait_snaps.append(fork_snap)
    pulse = _PulseState(
        fork=fork,
        scan_before=scan_before,
        action_scan=action_scan,
        action_snap=action_snap,
        wait_snaps=tuple(wait_snaps),
        post_pulse_snap=post_pulse_snap,
        post_pulse_key=post_pulse_key,
        snap=fork_snap,
        key=_pilot_world_key(
            fork_snap,
            key_config,
            state.pilot_rungs,
            getattr(state, "active_requirements", ()),
        ),
        coast_receipt=None,
        timeline=session.events,
        kernel_scan_ids=session.kernel_scan_ids,
        source_snap=source_snap,
        applied_configurations=launch.configurations,
    )
    reached_consumer = (
        consumer_stop_reached(
            consumer_boundary,
            source_scan=scan_before,
            projection_at=pulse.projection_at,
        )
        if horizon is PulseHorizon.CONSUMER_BOUNDARY
        else None
    )
    pulse.stop_receipt = StopReceipt(
        condition=stop,
        stopped_scan=fork.state.scan_id,
        reached=(reached_consumer is True if reached_consumer is not None else True),
    )
    return pulse


# ---------------------------------------------------------------------------
# Compass observation gathering — execution observes; the drive loop applies
# ---------------------------------------------------------------------------


def _compass_observations(
    cause: TransitionCause,
    frame: _IterationFrame,
    before_snap: dict[str, Any],
    after_snap: dict[str, Any],
    ctx: _PilotContext,
    *,
    contradict_no_change: bool,
    world_key: tuple[Any, ...],
    applied: tuple[_ActionPair, ...] = (),
    fork: PLC | None = None,
    scan: int | None = None,
    start_scan: int | None = None,
    timeline: tuple[Any, ...] = (),
) -> tuple[CompassObservation, ...]:
    """Return compass-relevant motion between two snapshots without applying it.

    The causal chase is evidence gathering. The drive loop later applies the
    returned observations to its persistent compass value.
    """
    action_tag = cause[0] if is_action(cause) else None
    observations: list[CompassObservation] = []
    learning_writes: list[Any] = []
    if fork is not None and action_tag is not None:
        assertion_scan = fork.state.scan_id if scan is None else scan
        projection = fork._replay_rung_write_projection_at(assertion_scan)
        if projection is not None:
            learning_writes.extend(projection.writes)

    def _learned_expectation(tag: str, value: Any) -> Any:
        if fork is None or not hasattr(ctx, "pdg") or not hasattr(ctx, "program"):
            return None
        writer_ids: set[int] = set()
        for write in learning_writes:
            if (
                not write.run.enabled
                or write.transition.tag_name != tag
                or not _values_match(write.transition.to_value, value)
            ):
                continue
            matches = [
                index
                for index in ctx.pdg.writers_of.get(tag, frozenset())
                if resolve_rung(ctx.program, ctx.pdg.rung_nodes[index]) is write.run.rung
            ]
            writer_ids.update(matches)
        if len(writer_ids) != 1:
            return None
        return expectation_from_writer(
            ctx.pdg,
            ctx.program,
            writer_node=next(iter(writer_ids)),
            tag=tag,
            value=value,
            boundary=(tag, value),
        )

    for n in frame.tree.iter_nodes():
        # pipeline_internal nodes are included: the learned table is the
        # pipeline instrument's own memory, and a live trial is the strongest
        # evidence there is — both for new edges and for falsifying stale
        # static-catalog ones.
        if n.satisfied or n.is_steerable:
            continue
        old_v = before_snap.get(n.tag)
        new_v = after_snap.get(n.tag)
        if old_v != new_v and new_v is not None:
            if (
                action_tag is not None
                and fork is not None
                and not getattr(ctx, "collect_action_attribution", True)
            ):
                continue
            if (
                action_tag is not None
                and fork is not None
                and not _action_caused_change(
                    fork,
                    action_tag,
                    n.tag,
                    ctx.steerable,
                    scan=scan,
                    start_scan=start_scan,
                    timeline=timeline,
                )
            ):
                continue
            observations.append(
                CompassObservation(
                    "edge",
                    n.tag,
                    cause,
                    old_v,
                    new_v,
                    world_key,
                    tuple(sorted(before_snap.items())),
                    applied,
                    _learned_expectation(n.tag, new_v),
                )
            )
        elif contradict_no_change:
            # The cause fired from old_v under a full settle window and the
            # register did not move — falsify any learned edge claiming it
            # would (a static-catalog route ignores unreadable enablers),
            # and mark the probe so it is not re-sent.
            observations.append(
                CompassObservation(
                    "contradict",
                    n.tag,
                    cause,
                    old_v,
                    None,
                    world_key,
                    tuple(sorted(before_snap.items())),
                    applied,
                )
            )
    return tuple(observations)


# ---------------------------------------------------------------------------
# Try-verify wrappers
# ---------------------------------------------------------------------------


def _try_action_batch(
    bearing: Bearing,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    *,
    observation_action: ActionPair | None = None,
) -> _AttemptResult:
    policy = bearing.act.policy
    # ── Action gate (avoid=) ──────────────────────────────────────────────
    # Before the pulse: a candidate whose overlaid action makes the avoid
    # predicate true is a path that depends on the avoided condition — reject it
    # *without* pressing, so a momentary command (avoid=C_Complete) is never
    # pulsed.  Static: overlay the applied set onto the live snapshot and read
    # the predicate.  nogood the choice so the next iteration stops surfacing it
    # (candidates filters nogoods), and record the names so the terminal decline
    # can point at what excluded the path.
    avoid_names = _avoid_violations(ctx, policy.applied, frame.snap)
    if avoid_names:
        return _AttemptResult(
            trial=None,
            gate_events=(
                PilotGateEvent("avoid", f"action would enter avoid: {', '.join(avoid_names)}"),
            ),
            nogood_pairs=(
                frozenset({policy.nogood_pair}) if policy.nogood_pair is not None else frozenset()
            ),
            avoid_names=tuple(avoid_names),
        )

    trial = _apply_actions(
        policy.applied,
        frame,
        state,
        ctx,
        entry_configurations=bearing.entry_configurations,
        horizon=policy.pulse_horizon,
        consumer_boundary=policy.consumer_boundary,
        stop_condition=bearing.stop_condition,
    )
    key_config = state.key_config
    assert key_config is not None

    observations: list[CompassObservation] = []
    if observation_action is not None:
        observations.extend(
            _compass_observations(
                observation_action,
                frame,
                frame.snap,
                trial.action_snap,
                ctx,
                contradict_no_change=True,
                world_key=_pilot_world_key(
                    frame.snap,
                    key_config,
                    state.pilot_rungs,
                    getattr(state, "active_requirements", ()),
                ),
                applied=policy.applied,
                fork=trial.fork,
                scan=trial.action_scan,
                start_scan=trial.scan_before + 1,
                timeline=trial.timeline,
            )
        )
    wait_before = trial.action_snap
    for wait_after in trial.wait_snaps:
        observations.extend(
            _compass_observations(
                WAIT,
                frame,
                wait_before,
                wait_after,
                ctx,
                contradict_no_change=False,
                world_key=_pilot_world_key(
                    wait_before,
                    key_config,
                    state.pilot_rungs,
                    getattr(state, "active_requirements", ()),
                ),
            )
        )
        wait_before = wait_after

    result = verify_gates(
        _executed_attempt(bearing, trial),
        frame,
        state,
        ctx,
    )
    return replace(result, observations=tuple(observations))


def execute(
    bearing: Bearing,
    world: OrientationWorld,
) -> _AttemptResult:
    """Execute exactly the act declared by a current-world bearing.

    This is deliberately narrower than orientation: it validates the world
    binding, installs declared prerequisites, and dispatches one act through
    the existing verification pipeline.  It never selects a fallback and
    applies the declared ``ActPolicy`` without decoding its provenance.
    """

    frame = world.frame
    state = world.state
    ctx = world.context
    key_config = state.key_config
    if key_config is None:
        raise StaleBearingError("cannot execute a bearing before the world key is configured")
    live_key = _pilot_world_key(
        dict(state.work.state.tags),
        key_config,
        state.pilot_rungs,
        getattr(state, "active_requirements", ()),
    )
    if live_key != bearing.world_key:
        raise StaleBearingError(
            f"bearing world {bearing.world_key!r} is stale; current world is {live_key!r}"
        )

    if bearing.prerequisites:
        _install_prerequisites(state, tuple(bearing.prerequisites))

    act = bearing.act
    if isinstance(act, Pulse):
        return _try_action_batch(
            bearing,
            frame,
            state,
            ctx,
            observation_action=act.action,
        )
    if isinstance(act, BatchPulse):
        return _try_action_batch(
            bearing,
            frame,
            state,
            ctx,
        )
    if isinstance(act, IntrascanPulse):
        return _try_action_batch(
            bearing,
            frame,
            state,
            ctx,
        )
    if isinstance(act, Coast):
        if act.mode == "bearing":
            return _try_bearing_coast(
                bearing,
                frame,
                state,
                ctx,
            )
        return _try_terminal_letrun(
            bearing,
            frame,
            state,
            ctx,
        )
    if isinstance(act, Dwell):
        return _try_terminal_dwell(
            bearing,
            frame,
            state,
            ctx,
        )
    if isinstance(act, (ObserveScan, ProgramScan)):
        return _try_single_program_scan(bearing, frame, state, ctx)
    raise TypeError(f"unsupported navigation act {type(act).__name__}")


def _try_single_program_scan(
    bearing: Bearing,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AttemptResult:
    """Execute an observation or evidence-selected stage for exactly one scan."""

    launch = _fork_for_execution(state, bearing.entry_configurations)
    fork = launch.fork
    scan_before = launch.scan_before
    snap_before = launch.entry_snap
    session = CoastSession(
        fork,
        kind=("observe_scan" if isinstance(bearing.act, ObserveScan) else "program_scan"),
        kernel_budget=(None if getattr(ctx, "collect_action_attribution", True) else False),
    )
    session.arm_avoid(ctx.avoid_pred)
    session.arm_pens(_pen_tags(state, ctx))
    session.step_kernel()
    session.note_pens()
    snap_after = dict(fork.state.tags)
    key_config = state.key_config
    assert key_config is not None
    trial = _PulseState(
        fork=fork,
        scan_before=scan_before,
        action_scan=None,
        action_snap=snap_before,
        wait_snaps=(snap_after,),
        post_pulse_snap=snap_before,
        post_pulse_key=frame.key,
        snap=snap_after,
        key=_pilot_world_key(
            snap_after,
            key_config,
            state.pilot_rungs,
            getattr(state, "active_requirements", ()),
        ),
        timeline=session.events,
        kernel_scan_ids=session.kernel_scan_ids,
        source_snap=snap_before,
        applied_configurations=launch.configurations,
    )
    return verify_gates(_executed_attempt(bearing, trial), frame, state, ctx)


# ---------------------------------------------------------------------------
# Bearing coast — cross timer/step-counter plateaus
# ---------------------------------------------------------------------------


def _terminal_target_trigger(work: PLC, target: Any) -> CoastTrigger:
    """Build the exact user-target trigger shared by live and replay coasts."""

    if target.predicate is None:
        return value_trigger(
            work,
            "global-target",
            TARGET,
            target.tag,
            target.value,
        )
    return predicate_trigger(
        "global-target",
        TARGET,
        lambda current: target_reached(
            current.tags,
            target.tag,
            target.value,
            target.predicate,
        ),
        condition=_atom_condition(work, target.predicate),
        watched=(target.tag,),
    )


def _try_bearing_coast(
    bearing: Bearing,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AttemptResult:
    """Run a bearing coast through the verify pipeline.

    Forks, coasts past timer/step-counter plateaus, then runs the shared
    verify gates. The outcome classifier sees coast results the same way it
    sees command results: SPIN if nothing moved, CONFIRMED if the channel
    register transitioned forward, AMBIENT_DRIFT if the program ejected.

    An ejection (e.g. S_StateCurrent 3→9) is AMBIENT_DRIFT with trend
    regression.  ``_monitor_trend`` reverts to the last checkpoint; a future
    investigation layer should own bounded incident analysis and replay-tested
    corrective holds.
    """
    coast = bearing.act
    assert isinstance(coast, Coast)
    heading = coast.policy.heading
    route = heading.route if heading is not None else None
    channel_tag = heading.channel_tag if heading is not None else None
    target_value = heading.target_value if heading is not None else None
    boundary = heading.boundary if heading is not None else None
    route_channel_tag = route.channel_tag if route is not None else None
    replay_motion = ChannelMotion(channel_tag, target_value, boundary)
    launch = _fork_for_execution(state, bearing.entry_configurations)
    fork = launch.fork
    scan_before = launch.scan_before
    snap_before = launch.entry_snap

    # Confirmed conditional holds (oscillation correctives) animate during the
    # channel coast, same as the terminal let-run — fork_with_pilot_rungs installs
    # only the steady half.
    session = CoastSession(
        fork,
        kind="bearing_coast",
        kernel_budget=(None if getattr(ctx, "collect_action_attribution", True) else False),
    )
    session.arm_avoid(ctx.avoid_pred)
    session.arm_pens(_pen_tags(state, ctx))
    global_target_trigger = _terminal_target_trigger(fork, ctx.target)
    dwell, bearing_coast_receipt = _coast_to_bearing(
        fork,
        channel_tag,
        target_value,
        watched_tags=_watched_tags(frame, ctx),
        session=session,
        boundary=boundary,
        route_channel_tag=route_channel_tag,
        terminal_target=global_target_trigger,
        departure_tags=coast_departure_tags(state, ctx),
    )

    snap_after = dict(fork.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key_after = _pilot_world_key(
        snap_after,
        key_config,
        state.pilot_rungs,
        getattr(state, "active_requirements", ()),
    )

    observations: list[CompassObservation] = []
    wait_before = snap_before
    for wait_after in dwell:
        observations.extend(
            _compass_observations(
                WAIT,
                frame,
                wait_before,
                wait_after,
                ctx,
                contradict_no_change=False,
                world_key=_pilot_world_key(
                    wait_before,
                    key_config,
                    state.pilot_rungs,
                    getattr(state, "active_requirements", ()),
                ),
            )
        )
        wait_before = wait_after

    departed = bearing_coast_receipt is not None and bearing_coast_receipt.stop_reason == "departed"
    verify_channel = channel_tag
    verify_target = target_value
    if departed:
        departure_transitions = bearing_coast_receipt.departure_transitions
        # A single scan can move both a table-derived heading and the route's
        # state register.  Every such move must terminate the coast, but the
        # route channel is the semantic navigation boundary and therefore owns
        # verification when it is one of the landing transitions.  Tuple order
        # is only trigger-registration order; it is not causal precedence.
        transition = next(
            (
                item
                for item in departure_transitions
                if route is not None and item[0] == route.channel_tag
            ),
            next(iter(departure_transitions), None),
        )
        if transition is not None:
            verify_channel, held_value, _after = transition
            verify_target = (
                route.target_value
                if route is not None and verify_channel == route.channel_tag
                else held_value
            )
            boundary = None

    trial = _PulseState(
        fork=fork,
        scan_before=scan_before,
        action_scan=scan_before,
        action_snap=snap_before,
        wait_snaps=tuple(dwell),
        post_pulse_snap=snap_before,
        post_pulse_key=frame.key,
        snap=snap_after,
        key=key_after,
        coast_receipt=bearing_coast_receipt,
        timeline=session.events,
        kernel_scan_ids=session.kernel_scan_ids,
        channel_motion=ChannelMotion(
            verify_channel,
            verify_target,
            boundary,
        ),
        replay_motion=replay_motion,
        source_snap=snap_before,
        applied_configurations=launch.configurations,
    )

    result = verify_gates(
        _executed_attempt(bearing, trial),
        frame,
        state,
        ctx,
    )
    return replace(result, observations=tuple(observations))


def _try_terminal_letrun(
    bearing: Bearing,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AttemptResult:
    """Generalized terminal let-run — the bottom-of-loop fallback.

    Reached here when no route bearing coast, command candidate, or widening made
    progress, yet the watched tags are still live (things pending). The only move left is
    to hold the current macro-state and coast toward the global target, letting
    the program's self-advancing sub-processes (timers, step-counters) complete.

    Nothing about intermediate bearings is assumed: the heading is the global
    target, and the ejection guard is the recognized state-machine roles held at
    their current values.  Outcomes route through the shared verify pipeline:

      - target reached  -> CONFIRMED (the global-target check in verify_gates).
      - macro-state left -> AMBIENT_DRIFT; commit + _monitor_trend hands the
        ejection to investigation (the same path the doors took).
      - stall (budget, no target, no ejection) -> dead-end reject; the caller
        falls back to a bounded watched-tag settle.
    """
    role_tags = coast_departure_tags(state, ctx)
    # fork_with_pilot_rungs re-establishes the steady holds on the coast fork: force
    # overrides do not propagate through fork(), and a freshly-installed
    # prerequisite — e.g. the Enable that drives a harness sensor's ramp — has not
    # been scanned onto state.work yet, so its value isn't carried either.
    launch = _fork_for_execution(state, bearing.entry_configurations)
    fork = launch.fork
    scan_before = launch.scan_before
    snap_before = launch.entry_snap
    start_roles = {t: snap_before.get(t) for t in role_tags}

    # Confirmed conditional holds animate during the coast as oscillating rungs
    # in the holds overlay (cyclefold dispatch inside the coast session); they
    # are never forced steady.

    # A relational target (Temp >= 5.0) is reached when its predicate holds, not
    # when the register hits an exact value — coast on the predicate so a sensor
    # ramp driven by a held prerequisite (Enable) stops the moment it crosses.
    reached_fn = (
        (lambda s: target_reached(s.tags, ctx.target.tag, ctx.target.value, ctx.target.predicate))
        if ctx.target.predicate is not None
        else None
    )

    exact_assertion_scan = bool(
        bearing.stop_condition is not None
        and bearing.stop_condition.horizon is PulseHorizon.ASSERTION_SCAN
    )
    budget = (
        1
        if exact_assertion_scan
        else min(
            _COAST_BUDGET,
            max(
                2,
                state.remaining_search_scans(ctx.max_scans, scan_id=scan_before),
            ),
        )
    )
    session = CoastSession(
        fork,
        kind="letrun",
        kernel_budget=(None if getattr(ctx, "collect_action_attribution", True) else False),
    )
    session.arm_avoid(ctx.avoid_pred)
    session.arm_pens(_pen_tags(state, ctx))
    letrun_receipt = _coast_holding_state(
        fork,
        ctx.target.tag,
        ctx.target.value,
        role_tags,
        budget=budget,
        reached_fn=reached_fn,
        session=session,
    )

    snap_after = dict(fork.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key_after = _pilot_world_key(
        snap_after,
        key_config,
        state.pilot_rungs,
        getattr(state, "active_requirements", ()),
    )

    observations = _compass_observations(
        WAIT,
        frame,
        snap_before,
        snap_after,
        ctx,
        contradict_no_change=False,
        world_key=_pilot_world_key(
            snap_before,
            key_config,
            state.pilot_rungs,
            getattr(state, "active_requirements", ()),
        ),
    )

    # Decide the outcome here — only the let-run knows the macro-state sentinel.
    #   reached  -> let the global-target check in verify_gates accept (CONFIRMED).
    #   ejected  -> a role left its held value: AMBIENT_DRIFT, handed to
    #               investigation via the changed role as the deviation bearing.
    #   stall    -> nothing reached, no role moved: a true dead end; let the
    #               caller fall back to a bounded watched-tag settle.
    reached = target_reached(snap_after, ctx.target.tag, ctx.target.value, ctx.target.predicate)
    changed_channel = next(
        (t for t in role_tags if not _values_match(snap_after.get(t), start_roles[t])),
        None,
    )
    if not reached and changed_channel is None:
        # Hand the stall's receipt + pending flag to the loop: a quiescent
        # stall is trustworthy memo material (skip the re-coast at this world
        # key); a stall with a timer mid-flight must stay re-runnable.
        stalled = _PulseState(
            fork=fork,
            scan_before=scan_before,
            action_scan=scan_before,
            action_snap=snap_before,
            wait_snaps=(snap_after,),
            post_pulse_snap=snap_before,
            post_pulse_key=frame.key,
            snap=snap_after,
            key=key_after,
            coast_receipt=letrun_receipt,
            timeline=session.events,
            kernel_scan_ids=session.kernel_scan_ids,
            source_snap=snap_before,
            applied_configurations=launch.configurations,
            stop_receipt=(
                StopReceipt(bearing.stop_condition, fork.state.scan_id, True)
                if exact_assertion_scan and bearing.stop_condition is not None
                else None
            ),
        )
        result = verify_gates(
            _executed_attempt(bearing, stalled),
            frame,
            state,
            ctx,
        )
        gate_events = result.gate_events
        if result.trial is None and gate_events and gate_events[0].event == "spin":
            first = gate_events[0]
            gate_events = (
                replace(
                    first,
                    event="dead-end",
                    detail="terminal stall, no ejection",
                ),
                *gate_events[1:],
            )
        return replace(
            result,
            gate_events=gate_events,
            observations=observations,
            stall_receipt=letrun_receipt,
            stall_pending=_has_pending_effects(fork),
        )

    if reached:
        chan_tag: str | None = None
        chan_val: Any = None
    else:
        assert changed_channel is not None
        chan_tag = changed_channel
        chan_val = snap_before.get(changed_channel)

    trial = _PulseState(
        fork=fork,
        scan_before=scan_before,
        action_scan=scan_before,
        action_snap=snap_before,
        wait_snaps=(snap_after,),
        post_pulse_snap=snap_before,
        post_pulse_key=frame.key,
        snap=snap_after,
        key=key_after,
        coast_receipt=letrun_receipt,
        timeline=session.events,
        kernel_scan_ids=session.kernel_scan_ids,
        channel_motion=ChannelMotion(chan_tag, chan_val),
        source_snap=snap_before,
        applied_configurations=launch.configurations,
        stop_receipt=(
            StopReceipt(bearing.stop_condition, fork.state.scan_id, True)
            if exact_assertion_scan and bearing.stop_condition is not None
            else None
        ),
    )

    result = verify_gates(
        _executed_attempt(bearing, trial),
        frame,
        state,
        ctx,
    )
    return replace(result, observations=observations)


def _try_terminal_dwell(
    bearing: Bearing,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AttemptResult:
    """Run one bounded repeated dwell through the shared trial gates.

    Reached only when Compass knowledge carries a terminal-coast receipt for
    this world key. The coast is deterministic under the held inputs,
    so repeating the full ejection-guarded let-run would reproduce the same
    departure.

    Perform one deterministic watched-tag settle on a fork and route it through the
    same :func:`verify_gates` target gate as terminal let-run:

      - a self-advancing frontier that crosses the target during the dwell is
        CONFIRMED through the shared target gate (verify stays the sole source);
      - anything else is a legible terminal stall (dead-end reject), handed back
        to the caller's skiff / stuck exit.

    No ejection is committed and no investigation re-runs, so the loop cannot spin
    re-ejecting: a non-completing dwell terminates at the stuck exit rather than
    repeatedly spending the invocation's remaining search budget.
    """
    launch = _fork_for_execution(state, bearing.entry_configurations)
    fork = launch.fork
    scan_before = launch.scan_before
    snap_before = launch.entry_snap

    def _reached(tags: dict[str, Any]) -> bool:
        return target_reached(tags, ctx.target.tag, ctx.target.value, ctx.target.predicate)

    ceiling = min(
        _LETRUN_DWELL_CEILING,
        max(
            2,
            state.remaining_search_scans(ctx.max_scans, scan_id=scan_before),
        ),
    )
    session = CoastSession(
        fork,
        kind="settle",
        kernel_budget=(None if getattr(ctx, "collect_action_attribution", True) else False),
    )
    session.arm_pens(_pen_tags(state, ctx))
    dwell = _settle_watched_tags(
        fork,
        _watched_tags(frame, ctx),
        floor=2,
        ceiling=ceiling,
        reached_fn=_reached,
        session=session,
    )

    snap_after = dict(fork.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key_after = _pilot_world_key(
        snap_after,
        key_config,
        state.pilot_rungs,
        getattr(state, "active_requirements", ()),
    )

    observations = _compass_observations(
        WAIT,
        frame,
        snap_before,
        snap_after,
        ctx,
        contradict_no_change=False,
        world_key=_pilot_world_key(
            snap_before,
            key_config,
            state.pilot_rungs,
            getattr(state, "active_requirements", ()),
        ),
    )

    if not _reached(snap_after):
        # No new input is possible here and the watched tags quiesced without crossing the
        # target: a true terminal stall.  Do not classify a self-ejection as an
        # advance — return dead-end so the caller routes to the skiff / stuck exit.
        return _AttemptResult(
            trial=None,
            gate_events=(PilotGateEvent("dead-end", "terminal dwell settled short of target"),),
            observations=observations,
        )

    trial = _PulseState(
        fork=fork,
        scan_before=scan_before,
        action_scan=scan_before,
        action_snap=snap_before,
        wait_snaps=tuple(dwell),
        post_pulse_snap=snap_before,
        post_pulse_key=frame.key,
        snap=snap_after,
        key=key_after,
        timeline=session.events,
        kernel_scan_ids=session.kernel_scan_ids,
        source_snap=snap_before,
        applied_configurations=launch.configurations,
    )

    # Empty actions, no channel register: the settled fork already reached the
    # target, so verify_gates accepts through its target gate (CONFIRMED).  Reuse
    # the "letrun" observe labels so commit folds the steady holds into the
    # recorded inputs the same way (the coast's driver is the held context).
    result = verify_gates(
        _executed_attempt(bearing, trial),
        frame,
        state,
        ctx,
    )
    return replace(result, observations=observations)


def _coast_to_bearing(
    work: PLC,
    channel_tag: str | None,
    target_value: Any,
    watched_tags: frozenset[str],
    session: CoastSession | None = None,
    *,
    boundary: Any = None,
    route_channel_tag: str | None = None,
    terminal_target: CoastTrigger | None = None,
    departure_tags: tuple[str, ...] = (),
    budget: int | None = None,
) -> tuple[list[dict[str, Any]], Any]:
    """Coast the live state past timer/step-counter plateaus.

    The bearing coast has its own generous budget (``_COAST_BUDGET``) — it does NOT
    consume the pilot's iteration budget.  Timer dwell is waiting, not
    searching.

    With a channel register and target value, seek with the target and
    departure triggers armed — the coast lands on the exact scan either fires
    and the returned receipt says which.  Without a channel register, fall
    back to the bounded single-step watched-tag settle (no receipt — outcome's
    settle-path arm depends on its absence; the session still records pens).

    Returns ``(trajectory, receipt_or_None)``.
    """
    if channel_tag is None:
        return (
            _settle_watched_tags(
                work,
                watched_tags,
                floor=2,
                ceiling=_LETRUN_DWELL_CEILING,
                session=session,
            ),
            None,
        )

    coast_budget = _COAST_BUDGET if budget is None else max(1, budget)
    held_tags = list(dict.fromkeys(departure_tags))
    if route_channel_tag is not None and route_channel_tag not in held_tags:
        held_tags.append(route_channel_tag)
    departure_excluding: dict[str, Any] = {}
    if boundary is None and channel_tag not in held_tags:
        held_tags.append(channel_tag)
    if boundary is None:
        departure_excluding[channel_tag] = target_value

    if boundary is not None:
        from pyrung.core.instruction.advance import constraint_holds

        estimate = estimate_owned_boundary_scans(work, boundary)
        # Live steering owns a generous seek and may extend it to a statically
        # estimated boundary. Incident replay supplies its recorded occurrence
        # horizon explicitly; a counterfactual that makes the distant boundary
        # unreachable must not turn that bounded proof into a fresh long coast.
        if estimate is not None and budget is None:
            coast_budget = max(coast_budget, estimate + 2)
        heading_target = predicate_trigger(
            "target",
            TARGET,
            lambda state: constraint_holds(boundary, state.tags) is True,
            condition=_constraint_condition(work, boundary),
            watched=(channel_tag,),
        )
    else:
        heading_target = value_trigger(
            work,
            "target",
            TARGET,
            channel_tag,
            target_value,
        )
    receipt = _coast_until(
        work,
        heading_target,
        tuple(held_tags),
        budget=coast_budget,
        session=session,
        extra_triggers=((terminal_target,) if terminal_target is not None else ()),
        departure_excluding=departure_excluding,
    )
    return [dict(work.state.tags)], receipt
