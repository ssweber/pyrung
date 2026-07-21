"""Retain, provisionally continue, or revert a committed trial world.

After a trial passes verification, this module compares target distance and
gauge marks, updates checkpoints, and classifies program-owned departures.
Regression handling builds an incident, replay-validates corrective hypotheses,
installs at most one surviving correction, and restores the appropriate
checkpoint. Provisional motion is bounded and is promoted or rolled back from
later gauge evidence.

This is the owner of post-commit recovery policy, not trial execution or local
gate acceptance.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pyrsistent import pvector

from pyrung.core.analysis.pilot._ops import (
    PilotRung,
    _append_rungs,
    _pilot_world_key,
    _semantic_key,
    _set_rungs,
    coast_departure_tags,
)
from pyrung.core.analysis.pilot.compass import ActionNogoodObservation
from pyrung.core.analysis.pilot.detour import (
    DepartureVerdict,
    Provisional,
    classify_departure,
)
from pyrung.core.analysis.pilot.investigate import (
    InvestigationResult,
    ReplayStep,
    build_deviation_incident,
    build_replay_fn,
    correction_identity,
    incident_regression_witness,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
    Outcome,
    ProgressEffect,
)
from pyrung.core.analysis.pilot.trace import frontier_pairs, target_reached
from pyrung.core.analysis.pilot.types import (
    CorrectionStatus,
    MotionKind,
    PilotEvent,
    _ActionPair,
    _Checkpoint,
    _CorrectionReceipt,
    _HoldLogEntry,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _Step,
    _TrialResult,
)
from pyrung.core.analysis.sp_values import _values_match

_PROVISIONAL_SCAN_BUDGET = 2000


def _channel_tenure_checkpoint_index(
    state: _PilotState,
    channel_tag: str,
    channel_value: Any,
) -> int:
    """Return the recovery receipt that began the current channel tenure.

    Target-relative progress may bank several checkpoints while an outer
    operation remains on the same channel value.  A later departure belongs to
    that whole continuous tenure: selecting only the newest progress checkpoint
    would discard earlier changed-write evidence (for example a watchdog that
    fired before a nested timer boundary completed).
    """
    index = len(state.checkpoints) - 1
    while index > 0:
        previous = state.checkpoints[index - 1]
        if not _values_match(
            previous.world.work.state.tags.get(channel_tag),
            channel_value,
        ):
            break
        index -= 1
    return index


def _monitor_trend(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> tuple[PilotEvent, ...]:
    channel_ejection = (
        trial.zoom_channel_tag is not None
        and (
            trial.assessment is not None
            and trial.assessment.bearing is BearingEffect.DEPARTED
            or trial.assessment is None
            and trial.outcome == Outcome.AMBIENT_DRIFT
        )
        and not _values_match(
            trial.fork_snap.get(trial.zoom_channel_tag),
            trial.before_snap.get(trial.zoom_channel_tag),
        )
    )
    # A provisional attempt changes only the rollback boundary. Every trial
    # inside it still passes through the ordinary trend,
    # regression, investigation, and retry machinery below. In particular, an
    # exact coast-departure receipt outranks the corridor's fallback expiry:
    # investigation owns that observed operation before provisional lifetime
    # policy may discard it.
    if state.provisional is not None and not channel_ejection:
        settlement = _finish_provisional(trial, frame, state, ctx)
        if settlement is not None:
            return settlement

    if trial.new_key is None or trial.trend is None:
        return ()

    assert state.best_trend is not None

    # A FRONTIER outcome means the pilot knowingly exposed a world with
    # more prerequisites.  Commit the observation, but keep the previous
    # checkpoint and high-water mark alive: if the new world keeps drifting
    # away, the next verify pass should revert to the pre-frontier checkpoint
    # and chase the PLC-side cause.
    if trial.outcome == Outcome.FRONTIER:
        return (
            PilotEvent(
                "trend_checkpoint",
                state.work.state.scan_id,
                {
                    "trend": trial.trend,
                    "key": trial.new_key,
                    "checkpoint_count": len(state.checkpoints),
                    "frontier": True,
                    "baseline_trend": state.best_trend,
                },
            ),
        )

    # A coast that *ejected* — the macro-state left the value it was held at
    # and wandered into a side branch (Execute -> Holding/Aborting). Route
    # zoom and terminal let-run use the same evidence and rollback mechanics.
    # That branch's trace distance is misleadingly LOW (fewer open leaves than the
    # held state), so the ordinary ``trend < best_trend`` test below would
    # checkpoint the ejection as progress.  It is not progress: the watchdog that
    # ejected fired *during the coast*, not after it.  Investigate over the
    # coast-span window (the fork's own history, ``scan_before -> fork end``) so
    # its exact channel-transition producer and upstream corrective levers are
    # recoverable, then revert to the pre-coast checkpoint.
    if channel_ejection:
        chan = trial.zoom_channel_tag
        assert chan is not None
        departed_from = trial.before_snap.get(chan)
        investigated = bool(state.checkpoints)
        ejection = PilotEvent(
            "letrun_ejection",
            state.work.state.scan_id,
            {
                "channel_tag": chan,
                "from_value": departed_from,
                "requested_value": trial.zoom_target_value,
                "to_value": trial.fork_snap.get(chan),
                "observe_label": trial.observe_label,
                "coast_span": (trial.scan_before, state.work.state.scan_id),
                "investigated": investigated,
                "reason": None if investigated else "no checkpoint to revert to",
            },
        )
        if not investigated:
            # No prior checkpoint to anchor the incident or revert to — the
            # ejected state stands committed.  Surface why so the bail is visible
            # in the event stream rather than a silent ``return ()``.
            return (ejection,)
        # Classify BEFORE investigating (detour.py): program-owned motion may
        # preserves the progress gauge and offers a clean forward route —
        # reverting it would throw away the whole march, and investigation
        # would honestly confirm nothing. Affirmative clean-route evidence opens
        # bounded provisional piloting; regression or unknown evidence follows
        # the conservative investigate-and-revert arm.
        verdict = classify_departure(state, ctx, chan, departed_from, trial.before_snap)
        if verdict.is_provisional:
            prescribed_departure = (
                trial.route_prescribed
                and trial.assessment is not None
                and trial.assessment.agency is Agency.PILOT
            )
            if verdict.progress.effect == "preserved" and not prescribed_departure:
                # A clean route says the landing is usable, but a known-
                # preserved progress receipt says this occurrence earned
                # no program work. For ambient motion it may therefore be
                # a preventable ejection. A Compass/current edge earns
                # tide-table credit only when causal attribution says the
                # pilot actually produced this departure; program-caused
                # motion encountered during a prescribed coast remains
                # ambient.
                #
                # This decision is occurrence-local. An already-open
                # provisional corridor changes only the rollback boundary; it
                # must not suppress understanding the same physical departure.
                # Investigation therefore runs before retention in both cases.
                checkpoint_index = _channel_tenure_checkpoint_index(
                    state,
                    chan,
                    departed_from,
                )
                checkpoint = state.checkpoints[checkpoint_index]
                checkpoint_snap = dict(checkpoint.world.work.state.tags)
                replay_from_checkpoint = (
                    checkpoint.world.work.state.scan_id < trial.scan_before
                    or not _values_match(checkpoint_snap.get(chan), departed_from)
                )
                incident_anchor = (
                    checkpoint.world.work.state.scan_id
                    if replay_from_checkpoint
                    else trial.scan_before
                )
                incident_before = checkpoint_snap if replay_from_checkpoint else frame.snap
                return (
                    ejection,
                    *_investigate_and_revert(
                        trial,
                        frame,
                        state,
                        ctx,
                        anchor_scan=incident_anchor,
                        end_scan=state.work.state.scan_id,
                        incident_before_snap=incident_before,
                        retain_if_unresolved=verdict,
                        checkpoint_index=checkpoint_index,
                    ),
                )
            if state.provisional is None:
                return (
                    ejection,
                    *_start_provisional(verdict, trial, state, ctx, chan),
                )
            # A clean program-owned departure inside an existing bounded
            # attempt that earned work (or fulfilled an explicitly prescribed
            # channel transaction) is ordinary piloting. Keep the original
            # rollback boundary and budget; do not nest another provisional.
            return (ejection,)
        checkpoint_index = _channel_tenure_checkpoint_index(
            state,
            chan,
            departed_from,
        )
        checkpoint = state.checkpoints[checkpoint_index]
        checkpoint_snap = dict(checkpoint.world.work.state.tags)
        # If the latest receipt precedes the channel state this coast launched
        # from, replay must include earlier motion, including the action that armed the fault;
        # using the post-action frame as "before" would already contain alarm
        # triggers and erase the counterfactual evidence that a permissive
        # clears them.
        replay_from_checkpoint = (
            checkpoint.world.work.state.scan_id < trial.scan_before
            or not _values_match(checkpoint_snap.get(chan), departed_from)
        )
        incident_anchor = (
            checkpoint.world.work.state.scan_id if replay_from_checkpoint else trial.scan_before
        )
        incident_before = checkpoint_snap if replay_from_checkpoint else frame.snap
        return (
            ejection,
            *_investigate_and_revert(
                trial,
                frame,
                state,
                ctx,
                anchor_scan=incident_anchor,
                end_scan=state.work.state.scan_id,
                incident_before_snap=incident_before,
                checkpoint_index=checkpoint_index,
            ),
        )

    # A satisfied channel bearing can enter a world whose backward
    # trace has a different coordinate system. Comparing its raw leaf count to
    # the source world is meaningless: Idle may be two leaves from Start,
    # while the expected Starting landing exposes fifteen production
    # prerequisites. Reset the trend baseline, but keep the source checkpoint
    # as the outer rollback receipt. The landing is provisional until ordinary
    # progress banks a checkpoint; if later motion ejects into Alarm,
    # investigation must replay the action itself so it can discover the
    # missing hold and retry from the corrected PilotRungs world.
    if _bearing_satisfied(trial) and trial.trend > state.best_trend:
        assert trial.zoom_channel_tag is not None
        channel_tag = trial.zoom_channel_tag
        previous = state.best_trend
        state.best_trend = trial.trend
        return (
            PilotEvent(
                "trend_checkpoint",
                state.work.state.scan_id,
                {
                    "trend": trial.trend,
                    "key": trial.new_key,
                    "checkpoint_count": len(state.checkpoints),
                    "channel": channel_tag,
                    "channel_value": trial.fork_snap.get(channel_tag),
                    "baseline_trend": previous,
                    "provisional": True,
                },
            ),
        )

    if trial.trend < state.best_trend:
        state.checkpoints.append(
            _Checkpoint(
                trial.new_key,
                state.snapshot_world(),
                trial.trend,
                trial.frontier,
            )
        )
        state.best_trend = trial.trend
        checkpoint_event = PilotEvent(
            "trend_checkpoint",
            state.work.state.scan_id,
            {
                "trend": state.best_trend,
                "key": trial.new_key,
                "checkpoint_count": len(state.checkpoints),
            },
        )
        # "The landing is provisional until ordinary progress banks a
        # checkpoint" — this is that checkpoint.  Banked improved-trend work
        # discharges the open provisional's doubt: the march is real, so its
        # expiry must never roll it back.  Bearing receipts and the settled
        # anchor never take this path; only earned progress promotes.
        if state.provisional is not None:
            provisional: Provisional = state.provisional
            state.provisional = None
            return (
                checkpoint_event,
                PilotEvent(
                    "provisional_promoted",
                    state.work.state.scan_id,
                    {
                        "channel_tag": provisional.channel_tag,
                        "from_value": provisional.from_value,
                        "gauge_at_source": provisional.gauge_at_source,
                        "outcome": "banked ordinary progress",
                        "trend": state.best_trend,
                        "checkpoint_count": len(state.checkpoints),
                    },
                ),
            )
        return (checkpoint_event,)

    if trial.trend == state.best_trend and trial.outcome == Outcome.CONFIRMED:
        state.checkpoints.append(
            _Checkpoint(
                trial.new_key,
                state.snapshot_world(),
                trial.trend,
                trial.frontier,
            )
        )
        return (
            PilotEvent(
                "trend_checkpoint",
                state.work.state.scan_id,
                {
                    "trend": state.best_trend,
                    "key": trial.new_key,
                    "checkpoint_count": len(state.checkpoints),
                    "flat": True,
                },
            ),
        )

    if trial.trend <= state.best_trend or not state.checkpoints:
        return ()

    return _investigate_and_revert(
        trial,
        frame,
        state,
        ctx,
        anchor_scan=state.checkpoints[-1].world.work.state.scan_id,
        end_scan=state.work.state.scan_id,
    )


def _bearing_satisfied(trial: _TrialResult) -> bool:
    """Whether trial verification proved the requested channel value."""
    if trial.zoom_channel_tag is None:
        return False
    if trial.assessment is not None:
        return trial.assessment.bearing is BearingEffect.SATISFIED
    return _values_match(
        trial.fork_snap.get(trial.zoom_channel_tag),
        trial.zoom_target_value,
    )


def _anchor_bearing_receipt(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
) -> None:
    """Capture the world immediately before a satisfied channel bearing.

    A route landing may expose a very different trace distance scale. If later
    motion ejects, investigation must
    replay from the state that launched the edge (Production/Idle before
    ``C_Start``), not from whichever older trend checkpoint happens to be on
    the stack (often cold Aborted).  Capture that source world before commit;
    the ordinary checkpoint/revert machinery owns it from then on.
    """
    if not _bearing_satisfied(trial):
        return
    receipt = _Checkpoint(
        frame.key,
        state.snapshot_world(),
        frame.distance_before,
        frontier_pairs(frame.tree, frame.snap),
    )
    if state.checkpoints and state.checkpoints[-1].key == frame.key:
        # Same executable key can recur with a later clean-path receipt. Keep
        # the exact current world/step boundary without growing duplicate CPs.
        state.checkpoints[-1] = receipt
    else:
        state.checkpoints.append(receipt)


def _start_provisional(
    verdict: Any,
    trial: _TrialResult,
    state: _PilotState,
    ctx: _PilotContext,
    chan: str,
) -> tuple[PilotEvent, ...]:
    """Open a bounded provisional attempt at the settled landing."""
    gauge = state.gauge
    # The exact pre-coast world remains the replay/rollback receipt. The
    # provisional gauge starts at the observed departure world so work already
    # earned during the coast is not counted a second time as side-motion
    # progress (e.g. 101->103 must not prematurely promote before 103->105).
    gauge_at_source = (
        gauge.mark(dict(state.work.state.tags)) if gauge is not None and gauge.components else ()
    )
    departed_from = trial.before_snap.get(chan)
    _adopt_settled_departure(verdict, state)
    search_scan = state.search_scan
    state.provisional = Provisional(
        channel_tag=chan,
        from_value=departed_from,
        gauge_at_source=gauge_at_source,
        checkpoint_depth=len(state.checkpoints),
        started_at=search_scan,
        expires_at=min(ctx.max_scans, search_scan + _PROVISIONAL_SCAN_BUDGET),
        entry_progress=verdict.progress,
    )
    return (
        PilotEvent(
            "provisional_started",
            state.work.state.scan_id,
            {
                "channel_tag": chan,
                "from_value": departed_from,
                "requested_value": trial.zoom_target_value,
                "settled_value": verdict.settled_value,
                "reason": verdict.reason,
                "route": verdict.route,
                "settle_scans": verdict.settle_scans,
                "gauge_at_source": gauge_at_source,
                "entry_progress": verdict.progress,
                "classification": verdict.verdict,
            },
        ),
    )


def _adopt_settled_departure(verdict: DepartureVerdict, state: _PilotState) -> int:
    """Adopt the classifier's settled landing without changing corridor policy.

    Settlement is evidence shared by both a newly-opened provisional and an
    already-open corridor that retained an unresolved departure.  Keeping this
    operation separate prevents ``_start_provisional`` from becoming the only
    way to consume the settled fork.
    Returns the scan at which adoption began.
    """
    settled = verdict.settled_fork
    scan_before = state.work.state.scan_id
    # Rebuild the overlay from the canonical rung list before adopting the
    # settled fork as the working PLC.
    _set_rungs(settled, state.rungs)
    state.work = settled
    state.dwell_scans += settled.state.scan_id - scan_before
    if state.steps:
        # The coast + settlement is one dwell: extend the recorded step's span
        # to the settled landing (mirrors the finished-arm rewrite).
        last = state.steps[-1]
        final_step = _Step(
            inputs=last.inputs,
            scan_before=last.scan_before,
            scan_after=settled.state.scan_id,
        )
        if state.journey and state.journey[-1] is last:
            state.journey[-1] = final_step
        state.steps = state.steps.set(len(state.steps) - 1, final_step)
    return scan_before


def _bank_provisional_landing(trial: _TrialResult, state: _PilotState) -> None:
    """Keep a local recovery receipt inside an existing provisional corridor.

    Retaining an investigated departure is not evidence of earned target
    progress, so this does not move ``best_trend`` or close the corridor.  It
    records the actual first landing solely as the rollback/incident anchor for
    the next recomputed operation.  Provisional promotion or expiry already
    trims checkpoints at ``checkpoint_depth``, so the receipt cannot escape the
    corridor that owns it.
    """
    if trial.new_key is None or trial.trend is None:
        return
    receipt = _Checkpoint(
        trial.new_key,
        state.snapshot_world(),
        trial.trend,
        trial.frontier,
    )
    if state.checkpoints and state.checkpoints[-1].key == trial.new_key:
        state.checkpoints[-1] = receipt
    else:
        state.checkpoints.append(receipt)


def _anchor_provisional(
    frame: _IterationFrame,
    state: _PilotState,
) -> tuple[PilotEvent, ...]:
    """Assess a newly settled provisional world before choosing another act."""
    provisional = state.provisional
    if (
        provisional is None
        or provisional.entry_banked
        or len(state.checkpoints) != provisional.checkpoint_depth
    ):
        return ()
    gauge = state.gauge
    outcome = provisional.entry_progress.effect
    if outcome not in {"advanced", "behind"}:
        outcome = (
            gauge.compare(dict(provisional.gauge_at_source), frame.snap)
            if gauge is not None and gauge.components
            else "unknown"
        )
    if outcome == "advanced":
        state.checkpoints.append(
            _Checkpoint(
                frame.key,
                state.snapshot_world(),
                frame.distance_before,
                frontier_pairs(frame.tree, frame.snap),
            )
        )
        state.best_trend = frame.distance_before
        landing_mark = gauge.mark(frame.snap) if gauge is not None else ()
        # Bank the work without closing the corridor.  The Held checkpoint is
        # now the rollback floor, while the provisional still gives the next
        # Unhold/rejoin transaction its ordinary local recovery semantics.
        state.provisional = replace(
            provisional,
            gauge_at_source=landing_mark,
            entry_banked=True,
        )
        return (
            PilotEvent(
                "provisional_promoted",
                state.work.state.scan_id,
                {
                    "channel_tag": provisional.channel_tag,
                    "from_value": provisional.from_value,
                    "gauge_at_source": provisional.gauge_at_source,
                    "entry_progress": provisional.entry_progress,
                    "landing_mark": landing_mark,
                    "trend": frame.distance_before,
                    "checkpoint_count": len(state.checkpoints),
                    "corridor_open": True,
                },
            ),
        )
    state.checkpoints.append(
        _Checkpoint(
            frame.key,
            state.snapshot_world(),
            frame.distance_before,
            frontier_pairs(frame.tree, frame.snap),
        )
    )
    state.best_trend = frame.distance_before
    return ()


def _finish_provisional(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> tuple[PilotEvent, ...] | None:
    """Settle provisional motion from observed progress, never value return.

    Advanced promotes immediately. Behind is a proven regression and enters
    the ordinary investigation/revert arm. Preserved or incomparable evidence
    may continue until the bounded attempt expires; expiration rolls back but
    creates no regression nogood.
    """
    provisional: Provisional = state.provisional
    now_snap = trial.fork_snap or {}
    reached = target_reached(
        now_snap,
        ctx.target_tag,
        ctx.target_value,
        ctx.target_predicate,
    )
    gauge = state.gauge
    anchor = dict(provisional.gauge_at_source)
    outcome = (
        gauge.compare(anchor, now_snap) if gauge is not None and gauge.components else "unknown"
    )
    # A gauge may advance on the same scan that a pilot act drives the machine
    # into a worse target-relative world (for example, a recipe step increments
    # while an unsafe Unhold enters Aborted). Trial attribution is the narrower
    # causal fact and must win over that incidental ordinal movement.
    if (
        trial.assessment is not None
        and trial.assessment.agency is Agency.PILOT
        and trial.assessment.bearing is BearingEffect.DEPARTED
        and trial.assessment.progress is ProgressEffect.BEHIND
    ):
        outcome = "behind"
    if outcome == "advanced" or reached:
        state.provisional = None
        # Collapse all provisional checkpoints into the promoted rejoin.
        del state.checkpoints[provisional.checkpoint_depth :]
        promoted_trend = trial.trend if trial.trend is not None else 0
        if trial.new_key is not None:
            state.checkpoints.append(
                _Checkpoint(
                    trial.new_key,
                    state.snapshot_world(),
                    promoted_trend,
                    trial.frontier,
                )
            )
        state.best_trend = promoted_trend
        return (
            PilotEvent(
                "provisional_promoted",
                state.work.state.scan_id,
                {
                    "channel_tag": provisional.channel_tag,
                    "from_value": provisional.from_value,
                    "gauge_at_source": provisional.gauge_at_source,
                    "landing_mark": gauge.mark(now_snap) if gauge is not None else (),
                    "trend": promoted_trend,
                    "checkpoint_count": len(state.checkpoints),
                    "terminal": trial.new_key is None,
                },
            ),
        )
    if outcome not in {"behind"} and state.search_scan < provisional.expires_at:
        return None

    state.provisional = None
    banked_checkpoint = (
        state.checkpoints[provisional.checkpoint_depth]
        if provisional.entry_banked and len(state.checkpoints) > provisional.checkpoint_depth
        else None
    )
    del state.checkpoints[provisional.checkpoint_depth :]
    if outcome == "behind":
        event = PilotEvent(
            "provisional_regressed",
            state.work.state.scan_id,
            {
                "channel_tag": provisional.channel_tag,
                "from_value": provisional.from_value,
                "outcome": outcome,
                "gauge_at_source": provisional.gauge_at_source,
            },
        )
        regression = _investigate_and_revert(
            trial,
            frame,
            state,
            ctx,
            anchor_scan=state.checkpoints[-1].world.work.state.scan_id,
            end_scan=state.work.state.scan_id,
            incident_before_snap=dict(state.checkpoints[-1].world.work.state.tags),
        )
        return (event, *regression)

    if banked_checkpoint is not None:
        state.checkpoints.append(banked_checkpoint)
    checkpoint = state.checkpoints[-1]
    state.load_world(checkpoint.world)
    state.best_trend = checkpoint.trend
    return (
        PilotEvent(
            "provisional_expired",
            state.work.state.scan_id,
            {
                "channel_tag": provisional.channel_tag,
                "from_value": provisional.from_value,
                "outcome": outcome,
                "gauge_at_source": provisional.gauge_at_source,
            },
        ),
    )


def _channel_transitions(
    ctx: _PilotContext,
    cp_fork: Any,
    regressed_snap: Any,
) -> tuple[tuple[str, Any, Any], ...]:
    """Channel-register transitions a revert undoes: ``(tag, from, to)``.

    ``from`` is the checkpoint value, ``to`` the regressed frame's value, for the
    navigated channel (the target register) when it moved.  Recording only —
    legibility so a destructive move (``S_StateCurrent 6->8`` Aborting) is
    distinguishable from useful program-owned motion (``6->11`` Held) in the
    transcript.  Scoped to the target bearing to keep the line focused; the
    derived enable/mask pipeline registers are noise here, not navigable channels.
    """
    cp_snap: Any = {}
    try:
        cp_snap = dict(getattr(cp_fork.state, "tags", {}) or {})
    except (AttributeError, TypeError):
        cp_snap = {}
    reg_snap = regressed_snap or {}

    channels: list[str] = []
    target_tag = getattr(ctx, "target_tag", None)
    if target_tag is not None:
        channels.append(target_tag)
    if not channels:
        return ()

    out: list[tuple[str, Any, Any]] = []
    for tag in channels:
        fv = cp_snap.get(tag)
        tv = reg_snap.get(tag)
        if fv is None and tv is None:
            continue
        if not _values_match(fv, tv):
            out.append((tag, fv, tv))
    return tuple(out)


def _replay_step(step: Any, sc: Any) -> ReplayStep:
    """One recorded journey step + its committed context → a replay spec.

    The kind is the RECORDED motion (pulse / zoom / letrun), never inferred
    from position or input emptiness.  A coast step with no channel register
    (the settle-path zoom) replays as a plain dwell, exactly the shape it ran
    live.  A step with no surviving context (pre-loop seeding) degrades to
    pulse-or-dwell by its inputs.
    """
    inputs = tuple(step.inputs.items())
    if sc is None:
        return ReplayStep(inputs=inputs, scans=step.scans, kind="pulse" if inputs else "dwell")
    kind = {
        MotionKind.INTERVENTION: "pulse",
        MotionKind.COAST_TO_BEARING: "zoom",
        MotionKind.COAST_HOLDING_WORLD: "letrun",
    }[sc.motion]
    if kind == "zoom" and sc.channel_tag is None:
        kind = "dwell"
    return ReplayStep(
        inputs=inputs,
        scans=step.scans,
        kind=kind,
        channel_tag=sc.channel_tag,
        channel_target=sc.channel_target,
    )


def _deviation_bearing(
    trial: _TrialResult,
    frame: _IterationFrame,
    watch_tags: list[str],
    frontier: tuple[_ActionPair, ...],
) -> tuple[_ActionPair, ...]:
    """Facts the failed operation actually held and then lost.

    A zoom carries two different channel values: the source it launched from
    and the destination it requested. Only the source can be a departure
    bearing. Recording the unvisited destination here manufactures an
    impossible ``departure_scan=None`` and leaves causal ranking without the
    exact source-to-eject transition.
    """
    needed_by_tag: dict[str, list[Any]] = {}
    for tag, value in frontier:
        needed_by_tag.setdefault(tag, []).append(value)
    bearing: list[_ActionPair] = [
        (tag, frame.snap.get(tag))
        for tag in watch_tags
        if not _values_match(frame.snap.get(tag), trial.fork_snap.get(tag))
        and not any(
            _values_match(trial.fork_snap.get(tag), needed) for needed in needed_by_tag.get(tag, ())
        )
    ]
    channel = trial.zoom_channel_tag
    if channel is not None:
        source = trial.before_snap.get(channel)
        landed = trial.fork_snap.get(channel)
        if not _values_match(landed, source):
            bearing = [(tag, value) for tag, value in bearing if tag != channel]
            bearing.append((channel, source))
    return tuple(bearing)


def _rung_identity(rung: PilotRung) -> tuple[Any, ...]:
    return (
        rung.dest,
        _semantic_key(rung.value),
        _semantic_key(rung.guard),
        _semantic_key(rung.operation),
    )


def _contradicted_corrections(
    state: _PilotState,
    investigation: InvestigationResult,
) -> tuple[_CorrectionReceipt, ...]:
    """Active corrections contradicted by the next incident's exact remedy.

    A later hypothesis that causally names an installed destination and needs a
    value outside the correction's admitted values is evidence that the prior
    correction caused this regression.  Treating the opposite value as another
    durable hold would leave two tools arguing in the overlay.
    """
    if not investigation.confirmed:
        return ()
    remedy = investigation.confirmed[0]
    sources = set(remedy.sources)
    remedy_rungs: dict[str, list[tuple[Any, Any]]] = {}
    for proposal in remedy.holds:
        if isinstance(proposal, PilotRung):
            remedy_rungs.setdefault(proposal.dest, []).append((proposal.value, proposal.operation))
        else:
            tag, value = proposal
            remedy_rungs.setdefault(tag, []).append((value, None))

    def _compatible_phases(new_operation: Any, old: PilotRung) -> bool:
        """Opposite values with distinct owner boundaries are temporal phases."""
        return (
            new_operation is not None
            and old.operation is not None
            and _semantic_key(new_operation.until) != _semantic_key(old.operation.until)
        )

    contradicted: list[_CorrectionReceipt] = []
    for receipt in state.correction_receipts:
        if receipt.status is not CorrectionStatus.ACTIVE:
            continue
        admitted: dict[str, list[PilotRung]] = {}
        for rung in receipt.rungs:
            admitted.setdefault(rung.dest, []).append(rung)
        if any(
            tag in sources
            and all(
                not any(
                    _values_match(remedy_value, old.value)
                    or _compatible_phases(remedy_operation, old)
                    for old in admitted.get(tag, ())
                )
                for remedy_value, remedy_operation in rungs
            )
            for tag, rungs in remedy_rungs.items()
            if tag in admitted
        ):
            contradicted.append(receipt)
    return tuple(contradicted)


def _revoke_corrections(
    state: _PilotState,
    receipts: tuple[_CorrectionReceipt, ...],
    checkpoint: _Checkpoint,
) -> tuple[int, ...]:
    """Revoke causally harmful receipts and rebuild the checkpoint without them."""
    if not receipts:
        return ()
    receipt_ids = {receipt.receipt_id for receipt in receipts}
    revoked_rung_ids = {_rung_identity(rung) for receipt in receipts for rung in receipt.rungs}
    state.correction_receipts = [
        replace(receipt, status=CorrectionStatus.REVOKED)
        if receipt.receipt_id in receipt_ids
        else receipt
        for receipt in state.correction_receipts
    ]
    for receipt in receipts:
        state.correction_nogoods.setdefault(receipt.origin_key, set()).add(receipt.identity)
        state.hold_log.append(
            _HoldLogEntry(
                scan=state.work.state.scan_id,
                tags=tuple((rung.dest, rung.value) for rung in receipt.rungs),
                source="revocation",
                rungs=receipt.rungs,
            )
        )

    remaining = [rung for rung in state.rungs if _rung_identity(rung) not in revoked_rung_ids]
    state.rungs = remaining
    _set_rungs(state.work, remaining)
    key_config = state.key_config
    cleaned_checkpoints: list[_Checkpoint] = []
    for saved in state.checkpoints:
        saved_rungs = [
            rung for rung in saved.world.rungs if _rung_identity(rung) not in revoked_rung_ids
        ]
        if len(saved_rungs) == len(saved.world.rungs):
            cleaned_checkpoints.append(saved)
            continue
        saved_work = saved.world.work.fork()
        _set_rungs(saved_work, saved_rungs)
        saved_world = saved.world.set(work=saved_work, rungs=pvector(saved_rungs))
        saved_key = (
            _pilot_world_key(dict(saved_work.state.tags), key_config, saved_rungs)
            if key_config is not None
            else saved.key
        )
        cleaned_checkpoints.append(_Checkpoint(saved_key, saved_world, saved.trend, saved.frontier))
    state.checkpoints = cleaned_checkpoints
    restored_key = (
        _pilot_world_key(dict(state.work.state.tags), key_config, state.rungs)
        if key_config is not None
        else checkpoint.key
    )
    state.checkpoints[-1] = _Checkpoint(
        restored_key,
        state.snapshot_world(),
        checkpoint.trend,
        checkpoint.frontier,
    )
    # The same machine tags now carry different correction knowledge. Permit
    # the retry; the revoked correction identity will be excluded explicitly.
    state.seen_keys.discard(restored_key)
    return tuple(sorted(receipt_ids))


def _investigate_and_revert(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    *,
    anchor_scan: int,
    end_scan: int,
    incident_before_snap: dict[str, Any] | None = None,
    retain_if_unresolved: DepartureVerdict | None = None,
    checkpoint_index: int = -1,
) -> tuple[PilotEvent, ...]:
    """Build a bounded incident over ``[anchor_scan, end_scan]``, replay-test
    corrective holds, install the confirmed ones, and revert to the last
    checkpoint.

    The window is a parameter because a *regression* anchors at the checkpoint
    scan, while a *terminal-letrun ejection* must anchor at the coast start
    (``trial.scan_before``) — the ejecting watchdog fires mid-coast, so the
    post-eject window the regression path would use misses it.
    """
    checkpoint_index %= len(state.checkpoints)
    checkpoint = state.checkpoints[checkpoint_index]
    cp_key, cp_world, cp_trend = checkpoint.key, checkpoint.world, checkpoint.trend
    cp_fork = cp_world.work
    investigation_holds: list[Any] = []
    investigation_rungs: list[PilotRung] = []
    investigation: InvestigationResult | None = None
    revoked_receipts: tuple[_CorrectionReceipt, ...] = ()
    investigation_nogoods: set[_ActionPair] = set()
    investigation_payload: dict[str, Any] = {}
    if trial.chase_regression_causes:
        # A watch tag that moved TO a value the target still needs (the
        # checkpoint frontier) is *progress*, not a departure — the coast exists
        # to move it (Heat_CurStep 0->1 en route to 3).  Chasing it spawns
        # corrective holds against the plan itself (lock the enabler of the
        # very advance we wanted).  Only anomalous motion enters the bearing.
        bearing = _deviation_bearing(
            trial,
            frame,
            state.watch_tags,
            checkpoint.frontier,
        )
        # The incident's evidence is the recorded step timelines inside the
        # window — the trend recorder's pen marks — never a history re-diff.
        # ``step_contexts`` is world-side, so reverted steps are already gone
        # and the just-committed trial's context is the last entry.
        window_timeline = tuple(
            event
            for sc in state.step_contexts
            for event in sc.timeline
            if anchor_scan <= event.scan <= end_scan
        )
        incident = build_deviation_incident(
            anchor_scan=anchor_scan,
            end_scan=end_scan,
            action=trial.applied,
            bearing=bearing,
            before_snap=incident_before_snap or frame.snap,
            after_snap=trial.fork_snap,
            timeline=window_timeline,
            program=ctx.program,
            channel_tag=trial.zoom_channel_tag,
        )

        # Replay re-arms each step's RECORDED session spec (kind + channel +
        # target off the committed step context), replacing the old positional
        # "last empty-input step is the eject coast" inference.
        sc_by_scan = {c.scan_before: c for c in state.step_contexts}
        replay_steps = tuple(
            _replay_step(step, sc_by_scan.get(step.scan_before))
            for step in state.steps
            if step.scan_before >= cp_fork.state.scan_id
        )
        role_tags = coast_departure_tags(state, ctx)
        correction_progress_mark = (
            retain_if_unresolved.progress.source_mark
            if retain_if_unresolved is not None
            and retain_if_unresolved.progress.effect == "preserved"
            else ()
        )
        regression_progress_floor = dict(cp_fork.state.tags)
        regression_progress_floor.update(correction_progress_mark)
        replay = build_replay_fn(
            cp_fork,
            cp_trend,
            tuple(state.rungs),
            replay_steps,
            resting=ctx.resting,
            edge_tags=ctx.edge_tags,
            target_tag=ctx.target_tag,
            target_value=ctx.target_value,
            pdg=ctx.pdg,
            program=ctx.program,
            steerable=ctx.steerable,
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            route=ctx.route,
            prior=getattr(ctx, "domain_prior", None),
            clear_only=getattr(ctx, "clear_only", frozenset()),
            zoom_channel_tag=trial.zoom_channel_tag,
            zoom_target_value=trial.zoom_target_value,
            terminal_letrun_role_tags=(
                role_tags if trial.motion is MotionKind.COAST_HOLDING_WORLD else None
            ),
            # The replay reproduces the incident, so its eject watch is the
            # departed channel alone when one exists (audit I2 — an explicit
            # caller decision, not buried dispatch); the full role set only
            # when no channel register is recognized.
            replay_watch_roles=(
                (trial.zoom_channel_tag,) if trial.zoom_channel_tag is not None else role_tags
            ),
            departure_bearing=tuple((d.tag, d.value) for d in incident.departures),
            regression_witness=incident_regression_witness(trial.fork, incident),
            progress_gauge=state.gauge,
            progress_anchor=dict(cp_fork.state.tags),
            regression_progress_floor=(
                regression_progress_floor if correction_progress_mark else None
            ),
        )

        # The register set the target still needs: the checkpoint's *frontier*,
        # captured when the checkpoint was created (the frame that computed the
        # distance and launched the coast).  The live frame here is useless — a
        # terminal-let-run frame is a coast with no tree — and re-deriving loses
        # the non-steerable interior needs (``Heat_CurStep = 3``) that
        # ``ordered_actions()``-style extractions can never surface.
        needed = list(checkpoint.frontier)
        investigation = investigate_deviation(
            # Derive hypotheses from the PLC that actually observed the
            # incident.  Replay still starts from ``cp_fork`` above.
            trial.fork,
            incident,
            ctx,
            replay,
            needed=needed,
            installed_rungs=tuple(state.rungs),
            correction_rungs=tuple(
                rung
                for receipt in state.correction_receipts
                if receipt.status is CorrectionStatus.ACTIVE
                for rung in receipt.rungs
            ),
            correction_progress_mark=correction_progress_mark,
            excluded_corrections=frozenset(state.correction_nogoods.get(cp_key, ())),
        )
        investigation_nogoods.update(investigation.regression_nogoods)
        # Investigation has already derived a finite guard and replayed this
        # exact installed form. Post-commit recovery does not reinterpret that proof through
        # a second, globally-steady-hold rule.
        investigation_holds.extend(investigation.confirmed_holds)
        revoked_receipts = _contradicted_corrections(state, investigation)

        def _hyp_detail(h: Any) -> dict[str, Any]:
            return {
                "kind": h.kind,
                "holds": h.holds,
                "sources": h.sources,
                "detail": h.detail,
            }

        def _rejection_detail(rejection: tuple[Any, str], slug: str) -> dict[str, Any]:
            hypothesis, ground = rejection
            return {**_hyp_detail(hypothesis), "slug": slug, "ground": ground}

        # ``rejection_slugs`` is index-aligned with ``rejected``; pad defensively
        # so a serializer never desyncs even if the two ever diverge in length.
        rejection_slugs = investigation.rejection_slugs + ("",) * (
            len(investigation.rejected) - len(investigation.rejection_slugs)
        )
        investigation_payload = {
            "hypotheses": len(investigation.hypotheses),
            "confirmed": len(investigation.confirmed),
            "rejected": len(investigation.rejected),
            "unresolved": investigation.unresolved,
            "hypothesis_detail": tuple(_hyp_detail(h) for h in investigation.hypotheses),
            "confirmed_detail": tuple(_hyp_detail(h) for h in investigation.confirmed),
            "rejected_detail": tuple(
                _rejection_detail(rejection, slug)
                for rejection, slug in zip(investigation.rejected, rejection_slugs, strict=True)
            ),
            "revoked_corrections": tuple(receipt.receipt_id for receipt in revoked_receipts),
        }
        if investigation_holds:
            # Investigation owns applicability and replayed these exact guarded
            # rungs. This module only installs the proved intervention.
            investigation_rungs = [
                proposal for proposal in investigation_holds if isinstance(proposal, PilotRung)
            ]
            state.hold_log.append(
                _HoldLogEntry(
                    scan=cp_fork.state.scan_id,
                    tags=tuple(
                        (p.dest, p.value) if isinstance(p, PilotRung) else p
                        for p in investigation_holds
                    ),
                    source="investigation",
                    rungs=tuple(investigation_rungs),
                )
            )

    if retain_if_unresolved is not None and not investigation_rungs and not revoked_receipts:
        # The departure earned no gauge credit, but investigation also found no
        # executable correction that preserves the target frontier.  The
        # independently-proven continuation therefore receives the ordinary
        # bounded provisional loan. If a corridor is already open, retain its
        # original rollback boundary, budget, and the actual first observed
        # landing. The classifier's later quiescent fork is evidence, not
        # permission to skip the next recomputation point.
        assert trial.zoom_channel_tag is not None
        retained = PilotEvent(
            "departure_investigated",
            state.work.state.scan_id,
            {
                "channel_tag": trial.zoom_channel_tag,
                "from_value": trial.before_snap.get(trial.zoom_channel_tag),
                "retained": True,
                "progress": retain_if_unresolved.progress,
                "investigation": investigation_payload,
            },
        )
        if state.provisional is not None:
            _bank_provisional_landing(trial, state)
            return (retained,)
        return (
            retained,
            *_start_provisional(
                retain_if_unresolved,
                trial,
                state,
                ctx,
                trial.zoom_channel_tag,
            ),
        )

    # Legibility (recording only): the channel transition(s) this revert undoes.
    # A destructive move (``S_StateCurrent 6->8`` Aborting) and a program-intended
    # useful program-owned move (``6->11`` Held) both leave the bearing, but only the former
    # is a genuine error — printing the reverted channel edge separates them in
    # every transcript.  Read the channel value at the checkpoint (from) vs. the
    # regressed frame (to); a channel is any opaque-loop pipeline register.
    channel_transitions: tuple[tuple[str, Any, Any], ...] = _channel_transitions(
        ctx, cp_fork, trial.fork_snap
    )

    # Keep the failed action as a nogood in the world where it failed. A
    # replay-confirmed correction creates a different world key, so the same
    # action is naturally eligible there without deleting valid history.
    regression_nogoods = set(investigation_nogoods)
    regression_nogoods.update(trial.regression_nogoods)
    if regression_nogoods:
        ctx.compass, _ = ctx.compass.apply(
            tuple(ActionNogoodObservation(cp_key, ("pair", pair)) for pair in regression_nogoods)
        )
    # A regression inside provisional motion returns to its local checkpoint
    # and keeps the bounded attempt open. Only an outer revert ends it.
    if state.provisional is not None:
        local_checkpoint = (
            len(state.checkpoints) > state.provisional.checkpoint_depth
            and checkpoint is state.checkpoints[-1]
        )
        if not local_checkpoint:
            state.provisional = None
    # Later checkpoints are target-progress receipts inside the departed
    # channel tenure. Once the incident requires a correction/revert, they no
    # longer describe an executable clean world; return to the tenure owner.
    del state.checkpoints[checkpoint_index + 1 :]
    state.load_world(cp_world)
    revoked_ids = _revoke_corrections(state, revoked_receipts, checkpoint)
    if investigation_rungs:
        # Revocation removes contradicted owners before this append.  The
        # replay-confirmed remedy is therefore an ownership replacement, not a
        # second hold layered over the stale correction.
        correction_origin_key = state.checkpoints[-1].key
        state.rungs = _append_rungs(state.work, investigation_rungs, state.rungs)
        # Bank the corrected world onto the checkpoint.  A replay-confirmed
        # correction is knowledge, but rungs live in the revertible World half —
        # a later revert to this same checkpoint would silently drop it, so
        # round N+1 relearns round N's correction and the restored overlay
        # re-collides with the letrun memo at the pre-correction key.  Replacing
        # the checkpoint's world (same tag state, corrected overlay, re-keyed)
        # is what makes "knowledge accumulates across reverts" true for
        # installed rungs, the way nogoods already survive outside the world.
        key_config = state.key_config
        banked_key = (
            _pilot_world_key(dict(state.work.state.tags), key_config, state.rungs)
            if key_config is not None
            else cp_key
        )
        state.checkpoints[-1] = _Checkpoint(
            banked_key,
            state.snapshot_world(),
            cp_trend,
            checkpoint.frontier,
        )
        assert investigation is not None
        confirmed_hypothesis = investigation.confirmed[0] if investigation.confirmed else None
        proof = investigation.confirmed_outcomes[0] if investigation.confirmed_outcomes else None
        state.correction_receipts.append(
            _CorrectionReceipt(
                receipt_id=(
                    max(
                        (receipt.receipt_id for receipt in state.correction_receipts),
                        default=0,
                    )
                    + 1
                ),
                origin_key=correction_origin_key,
                identity=correction_identity(
                    confirmed_hypothesis.holds
                    if confirmed_hypothesis is not None
                    else investigation_rungs
                ),
                rungs=tuple(investigation_rungs),
                sources=(confirmed_hypothesis.sources if confirmed_hypothesis is not None else ()),
                justification=(
                    proof.justification.value
                    if proof is not None and proof.justification is not None
                    else proof.reason
                    if proof is not None
                    else "replay-confirmed"
                ),
            )
        )
    state.best_trend = cp_trend
    return (
        PilotEvent(
            "trend_regression",
            state.work.state.scan_id,
            {
                "from_trend": trial.trend,
                "to_trend": cp_trend,
                "checkpoint_key": cp_key,
                "regression_nogoods": frozenset(regression_nogoods),
                "rungs": tuple(state.rungs),
                "channel_transitions": channel_transitions,
                "investigation": investigation_payload,
                "revoked_corrections": revoked_ids,
            },
        ),
    )
