"""Turn observed retained departures into ordinary replay bearings."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from copy import copy
from dataclasses import dataclass, replace
from typing import Any

from pyrsistent import pvector

from pyrung.core.analysis.causal._rung_writes import (
    ScanRungWriteProjection,
    build_scan_rung_write_projection,
)
from pyrung.core.analysis.pilot.coast import CoastTriggerEvent
from pyrung.core.analysis.pilot.constrained_reachability import NoRoute
from pyrung.core.analysis.pilot.correction_candidates import (
    UnsupportedOccurrenceScope,
    _active_pilot_rungs_defeat_needed,
    _continuation_with_active_correction,
    _exploratory_correction_rungs,
    _rank_hypotheses,
    correction_identity,
)
from pyrung.core.analysis.pilot.corrections import derive_correction_hypotheses
from pyrung.core.analysis.pilot.investigate import build_deviation_incident
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    Bearing,
    ExpectationExemption,
    NavigationConstraints,
    NeedProbe,
    OrientationWorld,
    RetainedOccurrence,
    RetainedReplay,
    Stuck,
    act_identity,
)
from pyrung.core.analysis.pilot.options import _holds_defeat_needed
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _merged_pilot_rungs,
    _pilot_rung_execution_receipt,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.recovery import (
    AttemptContext,
    CompositionBudget,
    Extend,
    Reject,
    Stop,
    Succeed,
    compose_corrections,
)
from pyrung.core.analysis.pilot.trace import target_reached
from pyrung.core.analysis.pilot.types import (
    ChannelMotion,
    PilotGateEvent,
    _ConfirmedCorrection,
    _ExecutedAttempt,
    _PilotContext,
    _PilotState,
    _PulseState,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.context import RungId

logger = logging.getLogger(__name__)

_MAX_RETAINED_COMPOSITIONS = 8


@dataclass(frozen=True)
class _RetainedCompositionCandidate:
    """One retained Bearing and its transaction-local Compass knowledge."""

    bearing: Bearing
    compass: Any


def _correction_pairs(pilot_rungs: Iterable[PilotRung]) -> tuple[tuple[str, Any], ...]:
    return tuple((rung.dest, rung.value) for rung in pilot_rungs)


def _scan_projection(work: Any, scan: int) -> ScanRungWriteProjection | None:
    return build_scan_rung_write_projection(
        work.history,
        scan,
        work._replay_rung_runs_at(scan),
    )


def _write_address(projection: ScanRungWriteProjection, write: Any) -> tuple[Any, ...]:
    """Normalize one dynamic write independently of scan-global ordinals."""

    run = write.run
    peers = tuple(
        candidate
        for candidate in projection.runs
        if candidate.rung_id == run.rung_id
        and candidate.call_stack == run.call_stack
        and candidate.caller_rung == run.caller_rung
    )
    run_rank = next(index for index, candidate in enumerate(peers) if candidate is run)
    direct = tuple(
        candidate
        for candidate in projection.writes_for_run(run)
        if candidate.transition.tag_name == write.transition.tag_name
    )
    write_rank = next(index for index, candidate in enumerate(direct) if candidate is write)
    return (run.call_stack, run.caller_rung, run_rank, write_rank)


def _writer_occurrence(
    work: Any,
    tag: str,
    current: Any,
    through_scan: int,
) -> tuple[Any, tuple[str | None, int], Any, tuple[Any, ...]] | None:
    """Resolve the latest exact causal write behind a retained blocker."""

    # Retained recovery only needs the selected writer occurrence.  A deep
    # explanation recursively asks when every steady enabler last changed;
    # across a long folded wait that can force dense reconstruction of the
    # entire cold interval even though none of those roots are consumed here.
    chain = work.cause(tag, scan=through_scan, deep=False)
    if (
        chain is None
        or not _values_match(chain.effect.to_value, current)
        or chain.effect.occurrence_ordinal is None
    ):
        boundary = work.history.previous_transition(tag, to=current)
        if boundary is None:
            return None
        chain = work.cause(tag, scan=boundary.scan_id, deep=False)
        if chain is None or not _values_match(chain.effect.to_value, current):
            return None
    effect = chain.effect
    if effect.occurrence_ordinal is None:
        return None
    projection = _scan_projection(work, effect.scan_id)
    if projection is None:
        return None
    write = projection.write_at_ordinal(effect.occurrence_ordinal)
    if write is None or write.transition.tag_name != tag:
        return None
    rung_id = write.rung_id
    return (
        write.run.rung,
        (rung_id.subroutine, rung_id.rung_index),
        write.transition,
        _write_address(projection, write),
    )


def _occurrence_repeated(replay: Any, occurrence: RetainedOccurrence) -> bool:
    """Whether replay reproduced the exact recorded writer occurrence."""

    projection = _scan_projection(replay, occurrence.scan)
    if projection is None:
        return False
    rung_id = RungId(*occurrence.writer)
    call_stack, caller_rung, run_rank, write_rank = occurrence.address
    runs = tuple(
        run
        for run in projection.runs
        if run.rung_id == rung_id
        and run.call_stack == call_stack
        and run.caller_rung == caller_rung
    )

    def matches(write: Any) -> bool:
        transition = write.transition
        return (
            transition.tag_name == occurrence.tag
            and _values_match(
                transition.from_value,
                occurrence.from_value,
            )
            and _values_match(transition.to_value, occurrence.to_value)
        )

    if run_rank < len(runs):
        addressed = tuple(
            write
            for write in projection.writes_for_run(runs[run_rank])
            if write.transition.tag_name == occurrence.tag
        )
        if write_rank < len(addressed) and matches(addressed[write_rank]):
            return True

    # Deleting an otherwise-identical earlier invocation shifts the local rank
    # of every later peer. There is no stable call-site coordinate in RungRun,
    # so fail closed: any same-context write with the same exact transition is
    # repetition evidence, never proof that the selected occurrence vanished.
    return any(matches(write) for run in runs for write in projection.writes_for_run(run))


def replay_retained_prefix(
    source: Any,
    floor_scan: int,
    through_scan: int,
    pilot_rungs: Iterable[PilotRung],
) -> Any:
    """Re-execute a retained suffix under one complete overlay.

    The immutable prefix through ``floor_scan`` is shared from its execution
    epoch; the old suffix is never inherited. The returned fork records the
    corrected suffix and is itself the hold-complete recording consumed by
    ``Plan.replay()``.
    """

    history = source.history
    if not history.contains(floor_scan) or not history.contains(through_scan):
        raise KeyError((floor_scan, through_scan))
    log = source._scan_log.snapshot()
    if floor_scan < log.base_scan:
        raise ValueError(f"retained floor {floor_scan} predates scan-log horizon {log.base_scan}")

    replay = fork_with_pilot_rungs(
        source,
        tuple(pilot_rungs),
        scan_id=floor_scan,
        inherit_log=False,
        history_budget=math.inf,
    )
    force_map = source._replay_force_map_at_scan(floor_scan, log)
    replay._input_overrides._forces.clear()
    replay._input_overrides._forces.update(force_map)

    lifecycle_by_scan: dict[int, list[Any]] = {}
    for event in log.lifecycle_events:
        lifecycle_by_scan.setdefault(event.at_scan_id, []).append(event)

    # Replaying a corrected retained prefix scan-by-scan made every candidate
    # pay the full age of the world (hundreds of thousands of scans in tumbler).
    # Between recorded nondeterministic events the corrected overlay is an
    # ordinary deterministic PLC, so use the same exact endpoint cycle/plateau
    # folding as live coasts. REALTIME logs retain per-scan dt and stay dense.
    event_scans = sorted(
        scan
        for scan in {
            *log.patches_by_scan,
            *log.force_changes_by_scan,
            *log.rtc_base_changes,
            *log.io_submits_by_scan,
            *log.io_drains_by_scan,
            *(event.at_scan_id for event in log.lifecycle_events),
        }
        if floor_scan < scan <= through_scan
    )

    def _advance_quietly(endpoint: int) -> None:
        logical = endpoint - replay.state.scan_id
        if logical <= 0:
            return
        if log.dts is not None or logical < 32:
            for _scan in range(replay.state.scan_id + 1, endpoint + 1):
                replay.step()
            return
        from pyrung.core.analysis.pilot.cyclefold import cycle_fold_until

        cycle_fold_until(
            replay,
            lambda state: state.scan_id >= endpoint,
            budget=logical,
            kernel_budget=False,
            fold_ctx=replay._ensure_fold_context(),
            predicate_reads=frozenset(),
        )

    for event_scan in event_scans:
        _advance_quietly(event_scan - 1)
        source._apply_log_entries_for_scan(replay, event_scan, log, lifecycle_by_scan)
        replay.step()
    _advance_quietly(through_scan)
    return replay


def _retained_correction_candidates(
    work: Any,
    incident: Any,
    ctx: _PilotContext,
    *,
    installed: tuple[PilotRung, ...],
    excluded: frozenset[tuple[tuple[Any, ...], ...]],
    needed: tuple[tuple[str, Any], ...],
) -> tuple[_ConfirmedCorrection, ...]:
    """Materialize one occurrence-scoped hypothesis without judging its replay.

    The ordinary attempt/verification primitive owns that judgment.  This
    reader only turns recorded causal evidence into the next executable
    correction, so an unchanged counterfactual landing can be oriented again
    instead of being mislabeled as locally successful.
    """

    overlay = _pilot_rung_execution_receipt(installed, dict(incident.before_snap))
    installed_active = {rung.dest: rung.value for rung in overlay.effective}
    produced, absence = derive_correction_hypotheses(
        work,
        incident,
        ctx,
        installed=installed_active,
    )
    logger.debug(
        "retained raw hypotheses=%r absence=%r",
        tuple((item.kind, item.holds, item.sources) for item in produced),
        absence,
    )
    candidates: list[_ConfirmedCorrection] = []
    for hypothesis in _rank_hypotheses(
        work,
        produced,
        incident,
        primal_extra=absence,
    ):
        if not hypothesis.holds:
            continue
        scoped = _exploratory_correction_rungs(
            work,
            hypothesis.holds,
            incident,
            (),
            ctx,
        )
        if not scoped or not all(isinstance(rung, PilotRung) for rung in scoped):
            continue
        # The correction is guaranteed to be active at this retained
        # occurrence even when its exact guard is dormant in before_snap.
        # Judge the installed values themselves so a repair that suppresses
        # every writer of a required sibling frontier never becomes the new
        # outer-loop tip.
        if _holds_defeat_needed(
            _correction_pairs(scoped),
            (*incident.bearing, *needed),
            ctx.pdg,
            ctx.program,
        ):
            continue
        if isinstance(
            _continuation_with_active_correction(
                scoped,
                incident.before_snap,
                ctx,
            ),
            NoRoute,
        ):
            continue
        if _active_pilot_rungs_defeat_needed(
            scoped,
            (*incident.bearing, *needed),
            incident.before_snap,
            ctx.pdg,
            ctx.program,
        ):
            continue
        identity = correction_identity(scoped)
        if identity in excluded:
            continue
        candidates.append(
            _ConfirmedCorrection(
                identity=identity,
                pilot_rungs=tuple(scoped),
                sources=hypothesis.sources,
                justification=hypothesis.detail or hypothesis.kind,
            )
        )
    logger.debug(
        "retained correction candidates=%r",
        tuple(_correction_pairs(item.pilot_rungs) for item in candidates),
    )
    return tuple(candidates)


def read_retained_replay(world: Any) -> RetainedReplay | None:
    """Return one exact retained-occurrence correction for this world."""

    frame = world.frame
    state: _PilotState = world.state
    ctx: _PilotContext = world.context
    work = getattr(state, "work", None)
    if work is None or not hasattr(work, "history"):
        return None
    history = work.history
    floor_scan = history.oldest_scan_id
    through_scan = work.state.scan_id
    installed = tuple(getattr(state, "pilot_rungs", ()))
    excluded = frozenset(getattr(state, "correction_nogoods", {}).get(frame.key, set()))

    blockers: list[tuple[str, Any]] = []
    seen_blockers: set[tuple[str, str]] = set()
    nodes = tuple(frame.tree.iter_nodes(order="depth_first"))
    frontier_needs = tuple(
        (node.tag, node.value) for node in nodes if not node.satisfied and node.value is not None
    )
    for node in reversed(nodes):
        if (
            node.satisfied
            or node.is_steerable
            or getattr(node, "pipeline_internal", False)
            or getattr(node, "relational", False)
            or node.value is None
        ):
            continue
        key = (node.tag, repr(node.value))
        if key in seen_blockers:
            continue
        seen_blockers.add(key)
        blockers.append((node.tag, node.value))

    for tag, needed in blockers:
        current = frame.snap.get(tag)
        if _values_match(current, needed):
            continue
        writer = _writer_occurrence(work, tag, current, through_scan)
        logger.debug(
            "retained blocker %s=%r (current=%r) writer=%r floor=%s",
            tag,
            needed,
            current,
            writer,
            floor_scan,
        )
        if writer is None:
            continue
        rung, writer_identity, exact_transition, address = writer
        predecessor = exact_transition.scan_id - 1
        if predecessor < floor_scan:
            continue
        if not history.contains(predecessor) or not history.contains(exact_transition.scan_id):
            continue
        occurrence_conditions = tuple(getattr(rung, "_conditions", ()) or ())
        if not occurrence_conditions:
            continue

        occurrence = RetainedOccurrence(
            floor_scan=floor_scan,
            scan=exact_transition.scan_id,
            ordinal=exact_transition.occurrence_ordinal,
            tag=exact_transition.tag_name,
            from_value=exact_transition.from_value,
            to_value=exact_transition.to_value,
            writer=writer_identity,
            address=address,
        )
        before = dict(history.at(predecessor).tags)
        after = dict(history.at(exact_transition.scan_id).tags)
        event = CoastTriggerEvent(
            "retained-occurrence",
            "pen",
            exact_transition.scan_id,
            (
                (
                    exact_transition.tag_name,
                    exact_transition.from_value,
                    exact_transition.to_value,
                ),
            ),
        )
        incident = replace(
            build_deviation_incident(
                anchor_scan=predecessor,
                end_scan=exact_transition.scan_id,
                action=(),
                bearing=((tag, needed),),
                before_snap=before,
                after_snap=after,
                timeline=(event,),
                channel_tag=tag,
            ),
            occurrence_conditions=occurrence_conditions,
            occurrence_writer=writer_identity,
        )

        try:
            corrections = _retained_correction_candidates(
                work,
                incident,
                ctx,
                installed=installed,
                excluded=excluded,
                needed=frontier_needs,
            )
        except (KeyError, UnsupportedOccurrenceScope, ValueError):
            logger.debug(
                "retained occurrence %s@%s did not yield a correction",
                occurrence.tag,
                occurrence.scan,
                exc_info=True,
            )
            continue
        if not corrections:
            logger.debug(
                "retained occurrence %s@%s yielded no correction candidate",
                occurrence.tag,
                occurrence.scan,
            )
            continue
        for correction in corrections:
            pairs = _correction_pairs(correction.pilot_rungs)
            act = RetainedReplay(
                policy=ActPolicy(
                    source=ActSource.RETAINED,
                    action_pairs=pairs,
                    applied=(),
                    provenance=(
                        "retained-history",
                        f"{occurrence.tag}@{occurrence.scan}",
                        f"writer={occurrence.writer!r}",
                    ),
                    note=correction.justification,
                    expectation_exemption=ExpectationExemption.LEGACY_RETAINED_REPLAY,
                ),
                occurrence=occurrence,
                correction=correction,
            )
            if not ctx.compass.knowledge.act_is_nogood(frame.key, act_identity(act)):
                return act
    return None


def _disposable_state(
    source: _PilotState,
    work: Any,
    pilot_rungs: tuple[PilotRung, ...],
    *,
    rebased: bool,
) -> _PilotState:
    """Clone orchestration handles around one throwaway executable world."""

    state = copy(source)
    state.world = source.world.set(
        work=work,
        pilot_rungs=pvector(pilot_rungs),
        committed_acts=(pvector([]) if rebased else source.committed_acts),
    )
    state.seen_keys = set(source.seen_keys)
    state.checkpoints = [] if rebased else list(source.checkpoints)
    state.watch_tags = list(source.watch_tags)
    state.consumed_revisits = set(source.consumed_revisits)
    state.journey = list(source.journey)
    state.hold_log = list(source.hold_log)
    state.correction_receipts = list(source.correction_receipts)
    state.correction_nogoods = {
        key: set(values) for key, values in source.correction_nogoods.items()
    }
    state.avoid_names = set(source.avoid_names)
    state.lever_notes = dict(source.lever_notes)
    return state


def _merge_retained_bearings(base: Bearing, addition: Bearing) -> Bearing | None:
    """Compose two retained corrections while preserving the original replay."""

    base_act = base.act
    addition_act = addition.act
    if not isinstance(base_act, RetainedReplay) or not isinstance(
        addition_act,
        RetainedReplay,
    ):
        return None
    rungs = tuple(
        _merged_pilot_rungs(
            addition_act.correction.pilot_rungs,
            base_act.correction.pilot_rungs,
        )
    )
    if len(rungs) == len(base_act.correction.pilot_rungs):
        return None
    correction = _ConfirmedCorrection(
        identity=correction_identity(rungs),
        pilot_rungs=rungs,
        sources=tuple(
            dict.fromkeys((*base_act.correction.sources, *addition_act.correction.sources))
        ),
        justification=(
            f"{base_act.correction.justification}; then {addition_act.correction.justification}"
        ),
    )
    pairs = _correction_pairs(rungs)
    act = replace(
        base_act,
        policy=replace(
            base_act.policy,
            action_pairs=pairs,
            note=correction.justification,
            provenance=(
                *base_act.policy.provenance,
                f"replacement={addition_act.occurrence.tag}@{addition_act.occurrence.scan}",
            ),
        ),
        correction=correction,
        # The prior probe proved only the prior correction set.  The composed
        # overlay needs one new suffix-local execution before it can be
        # promoted.
        prepared_world=None,
        prepared_journey=None,
    )
    return replace(
        base,
        act=act,
        rationale=(
            f"{base.rationale}; compose retained replacement "
            f"{addition_act.occurrence.tag}@{addition_act.occurrence.scan}"
        ),
    )


def compose_retained_bearing(
    compass: Any,
    bearing: Bearing,
    target: Any,
    constraints: NavigationConstraints,
) -> Bearing:
    """Boundedly compose retained corrections using ordinary orient/attempt.

    Every attempt starts from the original observed world.  A rejected
    counterfactual landing is wrapped in isolated orchestration state and fed
    through the same one-Bearing Compass orientation.  Only retained replay
    Bearings compose; observations and nogoods remain local, and this function
    never installs, commits, or advances PILOT's outer world.
    """

    if not isinstance(bearing.act, RetainedReplay) or bearing.orientation is None:
        return bearing

    from pyrung.core.analysis.pilot.compass import ActionNogoodObservation
    from pyrung.core.analysis.pilot.pilot import _transition_once

    source_world = bearing.orientation.world
    source_state: _PilotState = source_world.state
    source_rungs = tuple(source_state.pilot_rungs)

    def _attempt_composition(
        candidate: _RetainedCompositionCandidate,
        recovery_ctx: AttemptContext,
    ):
        current = candidate.bearing
        current_act = current.act
        if not isinstance(current_act, RetainedReplay):
            return Succeed(current)
        attempt_state = _disposable_state(
            source_state,
            source_state.work,
            source_rungs,
            rebased=False,
        )
        recovery_ctx.register_disposable_state(attempt_state)
        attempt_state.key_config = source_world.key_config
        attempt_ctx = replace(
            source_world.context,
            compass=candidate.compass,
            collect_action_attribution=False,
            retained_recovery_first=True,
        )
        transition = _transition_once(
            attempt_state,
            attempt_ctx,
            target,
            constraints,
            oriented=current,
        )
        attempt = transition.attempt
        assert attempt is not None
        logger.debug(
            "retained composition attempt=%r accepted=%s executed=%s",
            _correction_pairs(current_act.correction.pilot_rungs),
            transition.trial is not None,
            attempt.executed is not None,
        )
        if attempt.executed is None:
            return Stop(current)
        landing = attempt.executed.pulse.fork
        accepted = transition.trial is not None
        if accepted:
            # This is not merely a witness about the retained correction: it
            # is the corrected execution epoch itself.  Carry it back to the
            # live loop and let the ordinary commit adopt it directly.
            current = replace(
                current,
                act=replace(
                    current_act,
                    prepared_world=attempt_state.world,
                    prepared_journey=tuple(attempt_state.journey),
                ),
            )
        if accepted and target_reached(
            dict(landing.state.tags),
            source_world.context.target.tag,
            source_world.context.target.value,
            source_world.context.target.predicate,
        ):
            return Succeed(current)

        # `_transition_once` applied the ordinary observations/nogood to the
        # transaction-local Compass and, on acceptance, adopted the replay fork
        # with its correction. A rejected replay still exposes its exact
        # counterfactual landing, so orient it with the attempted overlay.
        local_compass = attempt_ctx.compass
        if accepted:
            local_state = attempt_state
        else:
            combined = tuple(
                _merged_pilot_rungs(
                    current_act.correction.pilot_rungs,
                    source_rungs,
                )
            )
            local_state = _disposable_state(
                source_state,
                landing,
                combined,
                rebased=True,
            )
            local_state.key_config = source_world.key_config
        local_ctx = replace(
            source_world.context,
            compass=local_compass,
            collect_action_attribution=False,
            retained_recovery_first=True,
        )
        local_world = OrientationWorld(
            world_key=(),
            snapshot=dict(landing.state.tags),
            frame=None,
            state=local_state,
            context=local_ctx,
            key_config=local_state.key_config,
        )
        while True:
            replacement = local_compass.orient(local_world, target, constraints)
            if isinstance(replacement, Bearing) and isinstance(
                replacement.act,
                RetainedReplay,
            ):
                merged = _merge_retained_bearings(current, replacement)
                if merged is None:
                    return Stop(current)
                return Extend(
                    act_identity(merged.act),
                    lambda merged=merged, local_compass=local_compass: (
                        _RetainedCompositionCandidate(merged, local_compass)
                    ),
                    Stop(current),
                    Stop(current),
                )

            # A probe request is unresolved evidence, not proof that the root
            # correction is dead.  The bounded composer never runs skiff.
            if isinstance(replacement, NeedProbe):
                return Succeed(current) if accepted else Stop(current)
            if isinstance(replacement, Stuck):
                if accepted:
                    return Succeed(current)
                return Reject(current)
            if not isinstance(replacement, Bearing):
                return Stop(current)

            # Match the incident-local investigation boundary: another exact
            # retained occurrence composes above; an ordinary future Bearing
            # is the handoff to the live outer loop.  Do not turn this causal
            # closure into a nested PILOT route search.
            if accepted:
                return Succeed(current)
            return Reject(current)

    def _rollback_to_sibling(
        candidate: _RetainedCompositionCandidate,
        rejected: Bearing,
        recovery_ctx: AttemptContext,
    ):
        # Roll back the disposable branch and its derived knowledge.  Only the
        # root-scoped rejection survives into sibling selection; landing-local
        # action/coast receipts must not poison another source transaction.
        local_compass, _ = candidate.compass.apply(
            (
                ActionNogoodObservation(
                    rejected.world_key,
                    act_identity(rejected.act),
                ),
            )
        )
        sibling_state = _disposable_state(
            source_state,
            source_state.work,
            source_rungs,
            rebased=False,
        )
        recovery_ctx.register_disposable_state(sibling_state)
        sibling_state.key_config = source_world.key_config
        sibling_ctx = replace(
            source_world.context,
            compass=local_compass,
            collect_action_attribution=False,
            retained_recovery_first=True,
        )
        sibling_world = replace(
            source_world,
            state=sibling_state,
            context=sibling_ctx,
        )
        while recovery_ctx.budget.remaining > 0:
            sibling = local_compass.orient(sibling_world, target, constraints)
            if isinstance(sibling, NeedProbe | Stuck):
                return Stop(rejected)
            if not isinstance(sibling, Bearing):
                return Stop(rejected)
            if isinstance(sibling.act, RetainedReplay):
                sibling_identity = act_identity(sibling.act)
                return Extend(
                    sibling_identity,
                    lambda sibling=sibling, local_compass=local_compass: (
                        _RetainedCompositionCandidate(sibling, local_compass)
                    ),
                    Stop(rejected),
                    Stop(rejected),
                )

            # Rollback may expose an ordinary source alternative before the
            # next retained sibling. Exercise it with the same transition
            # kernel: rejection adds a local nogood and re-orients this source;
            # acceptance makes that ordinary Bearing the honest outer choice.
            if not recovery_ctx.consume_auxiliary():
                return Stop(rejected)
            source_transition = _transition_once(
                sibling_state,
                sibling_ctx,
                target,
                constraints,
                oriented=sibling,
            )
            local_compass = sibling_ctx.compass
            if source_transition.trial is not None:
                return Succeed(sibling)
            sibling_world = OrientationWorld(
                world_key=(),
                snapshot=dict(sibling_state.work.state.tags),
                frame=None,
                state=sibling_state,
                context=sibling_ctx,
                key_config=sibling_state.key_config,
            )
        return Stop(rejected)

    composition = compose_corrections(
        _RetainedCompositionCandidate(bearing, compass),
        budget=CompositionBudget(_MAX_RETAINED_COMPOSITIONS + 1),
        attempt=_attempt_composition,
        budget_exhausted=lambda candidate: candidate.bearing,
        initial_identity=act_identity(bearing.act),
        rollback_to_sibling=_rollback_to_sibling,
        protected_states=(source_state,),
    )
    return composition.value


def execute_retained_replay(
    bearing: Any,
    frame: Any,
    state: _PilotState,
    ctx: _PilotContext,
) -> Any:
    """Execute a retained replay bearing through the ordinary verify gates."""

    from pyrung.core.analysis.pilot.verify import verify_gates

    act = bearing.act
    if not isinstance(act, RetainedReplay):
        raise TypeError(type(act).__name__)
    combined = tuple(_merged_pilot_rungs(act.correction.pilot_rungs, state.pilot_rungs))
    replay = act.prepared_world.work if act.prepared_world is not None else None
    if replay is None:
        # The exact predecessor owns the immutable prefix.  Only the suffix in
        # which the corrected writer occurs is new execution; replaying from a
        # public reporting floor would duplicate unrelated history.
        replay = replay_retained_prefix(
            state.work,
            act.occurrence.scan - 1,
            state.work.state.scan_id,
            combined,
        )
    snap = dict(replay.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key = _pilot_world_key(
        snap,
        key_config,
        combined,
        state.active_requirements,
    )
    pulse = _PulseState(
        fork=replay,
        scan_before=state.work.state.scan_id,
        action_scan=replay.state.scan_id,
        action_snap=snap,
        wait_snaps=(),
        post_pulse_snap=snap,
        post_pulse_key=key,
        snap=snap,
        key=key,
        channel_motion=ChannelMotion(
            act.occurrence.tag,
            act.occurrence.from_value,
            stop_reason="reached",
        ),
        confirmed_correction=act.correction,
    )
    executed = _ExecutedAttempt(pulse=pulse, bearing=bearing)
    result = verify_gates(executed, frame, state, ctx)
    if _occurrence_repeated(replay, act.occurrence):
        return replace(
            result,
            trial=None,
            gate_events=(
                *result.gate_events,
                PilotGateEvent(
                    "retained-occurrence",
                    "recorded retained occurrence repeated under the correction",
                ),
            ),
        )
    return result
