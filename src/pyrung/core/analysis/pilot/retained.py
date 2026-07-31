"""Turn observed retained departures into ordinary replay bearings."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from pyrung.core.analysis.causal._rung_writes import (
    ScanRungWriteProjection,
    build_scan_rung_write_projection,
)
from pyrung.core.analysis.pilot.coast import CoastTriggerEvent
from pyrung.core.analysis.pilot.constrained_reachability import Reachable, Unknown
from pyrung.core.analysis.pilot.investigate import (
    ReplayJustification,
    ReplayOutcome,
    UnsupportedOccurrenceScope,
    build_deviation_incident,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    RetainedOccurrence,
    RetainedReplay,
)
from pyrung.core.analysis.pilot.overlay import PilotRung, fork_with_pilot_rungs
from pyrung.core.analysis.pilot.trace import target_reached, trace_back
from pyrung.core.analysis.pilot.types import (
    ChannelMotion,
    _ExecutedAttempt,
    _PilotContext,
    _PilotState,
    _PulseState,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _semantic_key
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.context import RungId

logger = logging.getLogger(__name__)


def _correction_pairs(pilot_rungs: Iterable[PilotRung]) -> tuple[tuple[str, Any], ...]:
    return tuple((rung.dest, rung.value) for rung in pilot_rungs)


def _unresolved_blockers(tree: Any) -> frozenset[tuple[str, Any]]:
    """Retain the complete demand frontier hidden by its coarse count."""

    return frozenset(
        (node.tag, _semantic_key(node.value))
        for node in tree.iter_nodes(order="depth_first")
        if not node.satisfied
        and node.value is not None
    )


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

    chain = work.cause(tag, scan=through_scan, deep=True)
    if (
        chain is None
        or not _values_match(chain.effect.to_value, current)
        or chain.effect.occurrence_ordinal is None
    ):
        boundary = work.history.previous_transition(tag, to=current)
        if boundary is None:
            return None
        chain = work.cause(tag, scan=boundary.scan_id, deep=True)
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
        return transition.tag_name == occurrence.tag and _values_match(
            transition.from_value,
            occurrence.from_value,
        ) and _values_match(transition.to_value, occurrence.to_value)

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
    return any(
        matches(write)
        for run in runs
        for write in projection.writes_for_run(run)
    )


def replay_retained_prefix(
    source: Any,
    floor_scan: int,
    through_scan: int,
    pilot_rungs: Iterable[PilotRung],
) -> Any:
    """Re-execute the public retained prefix under one complete overlay.

    The old suffix is never inherited. The returned fork starts at the public
    floor, records every ordinary replayed scan, and therefore is itself the
    hold-complete recording consumed by ``Plan.replay()``.
    """

    history = source.history
    if not history.contains(floor_scan) or not history.contains(through_scan):
        raise KeyError((floor_scan, through_scan))
    log = source._scan_log.snapshot()
    if floor_scan < log.base_scan:
        raise ValueError(
            f"retained floor {floor_scan} predates scan-log horizon {log.base_scan}"
        )

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
    for scan_id in range(floor_scan + 1, through_scan + 1):
        source._apply_log_entries_for_scan(replay, scan_id, log, lifecycle_by_scan)
        replay.step()
    return replay


def _retained_replay_outcome(
    work: Any,
    floor_scan: int,
    through_scan: int,
    installed: tuple[PilotRung, ...],
    holds: tuple[Any, ...],
    occurrence: RetainedOccurrence,
    frame: Any,
    ctx: _PilotContext,
) -> ReplayOutcome:
    # Retained investigation must supply the occurrence-scoped executable form;
    # a raw pair has no defensible lifetime and fails closed.
    if not all(isinstance(hold, PilotRung) for hold in holds):
        return ReplayOutcome(False, None, frame.snap, reason="unscoped retained hold")
    proposed = tuple(holds)
    replay = replay_retained_prefix(
        work,
        floor_scan,
        through_scan,
        (*installed, *proposed),
    )
    snap = dict(replay.state.tags)
    occurrence_suppressed = not _occurrence_repeated(replay, occurrence)
    tree = trace_back(
        ctx.target.tag,
        ctx.target.value,
        snap,
        ctx.pdg,
        ctx.program,
        ctx.steerable,
        clear_only=ctx.clear_only,
        opaque_loop=ctx.opaque_loop,
        pipeline_internal_tags=ctx.pipeline_internal_tags,
        route=ctx.route,
        prior=ctx.domain_prior,
    )
    trend = tree.unsatisfied_count()
    reached = target_reached(snap, ctx.target.tag, ctx.target.value, ctx.target.predicate)
    old_blockers = _unresolved_blockers(frame.tree)
    new_blockers = _unresolved_blockers(tree)
    blocker_frontier_preserved = bool(old_blockers) and new_blockers <= old_blockers
    accepted = occurrence_suppressed and (
        reached or trend < frame.distance_before or blocker_frontier_preserved
    )
    logger.debug(
        "retained replay holds=%r occurrence_suppressed=%s trend=%s->%s "
        "blockers=%r->%r accepted=%s",
        _correction_pairs(proposed),
        occurrence_suppressed,
        frame.distance_before,
        trend,
        old_blockers,
        new_blockers,
        accepted,
    )
    return ReplayOutcome(
        accepted=accepted,
        trend=trend,
        snapshot=snap,
        reason=(
            "retained occurrence suppressed without introducing a blocker"
            if accepted
            else "retained replay did not suppress the exact occurrence with forward progress"
        ),
        justification=(ReplayJustification.REACHED if reached else ReplayJustification.ADVANCED)
        if accepted
        else None,
        continuation=(
            Reachable(("actual-target-witness",))
            if reached
            else Unknown("retained replay advanced but did not yet reach the target")
        ),
        continuation_snapshot=snap,
        landed=True,
    )


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
    correction_rungs = tuple(
        rung
        for receipt in getattr(state, "correction_receipts", ())
        for rung in receipt.pilot_rungs
    )
    excluded = frozenset(
        getattr(state, "correction_nogoods", {}).get(frame.key, set())
    )

    blockers: list[tuple[str, Any]] = []
    seen_blockers: set[tuple[str, str]] = set()
    nodes = tuple(frame.tree.iter_nodes(order="depth_first"))
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

        def replay(
            holds: tuple[Any, ...],
            occurrence: RetainedOccurrence = occurrence,
        ) -> ReplayOutcome:
            return _retained_replay_outcome(
                work,
                floor_scan,
                through_scan,
                installed,
                holds,
                occurrence,
                frame,
                ctx,
            )

        try:
            investigation = investigate_deviation(
                work,
                incident,
                ctx,
                replay,
                installed_pilot_rungs=installed,
                correction_pilot_rungs=correction_rungs,
                excluded_corrections=excluded,
            )
        except (KeyError, UnsupportedOccurrenceScope, ValueError):
            logger.debug(
                "retained occurrence %s@%s did not yield a correction",
                occurrence.tag,
                occurrence.scan,
                exc_info=True,
            )
            continue
        correction = investigation.correction
        if correction is None:
            logger.debug(
                "retained occurrence %s@%s rejected hypotheses: %s",
                occurrence.tag,
                occurrence.scan,
                tuple((item.slug, item.ground) for item in investigation.rejected),
            )
            continue
        pairs = _correction_pairs(correction.pilot_rungs)
        return RetainedReplay(
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
            ),
            occurrence=occurrence,
            correction=correction,
        )
    return None


def execute_retained_replay(
    bearing: Any,
    frame: Any,
    state: _PilotState,
    ctx: _PilotContext,
) -> Any:
    """Execute a retained replay bearing through the ordinary verify gates."""

    from pyrung.core.analysis.pilot.overlay import _merged_pilot_rungs
    from pyrung.core.analysis.pilot.verify import verify_gates

    act = bearing.act
    if not isinstance(act, RetainedReplay):
        raise TypeError(type(act).__name__)
    combined = tuple(_merged_pilot_rungs(act.correction.pilot_rungs, state.pilot_rungs))
    replay = replay_retained_prefix(
        state.work,
        act.occurrence.floor_scan,
        state.work.state.scan_id,
        combined,
    )
    snap = dict(replay.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key = _pilot_world_key(snap, key_config, combined)
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
    return verify_gates(_ExecutedAttempt(pulse=pulse, bearing=bearing), frame, state, ctx)
