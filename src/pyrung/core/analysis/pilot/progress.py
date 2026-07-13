"""ASSESS — the trend/checkpoint/revert phase of the PILOT loop.

Tracks distance trend across iterations, manages checkpoints, and reverts on
regression.  Improved → checkpoint; plateau → re-orient (escalate a reading
tier, not a new Act heuristic); sustained decline → revert to checkpoint.

Investigate is an *escalation inside* ASSESS's regression arm — not a phase of
its own: when ASSESS reverts, ``investigate_deviation`` mines the incident for a
single corrective hold.  Compass is a noun (the knowledge store), never a phase.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pilot._ops import (
    PilotRung,
    _append_rungs,
    _DebugFn,
    _set_rungs,
)
from pyrung.core.analysis.pilot.causal import pilot_touched_tags
from pyrung.core.analysis.pilot.compass import _action_sort_key
from pyrung.core.analysis.pilot.detour import (
    Provisional,
    classify_departure,
)
from pyrung.core.analysis.pilot.investigate import (
    build_deviation_incident,
    build_replay_fn,
    incident_eject_dones,
    incident_eject_latches,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.outcome import BearingEffect, Outcome
from pyrung.core.analysis.pilot.trace import frontier_pairs, target_reached
from pyrung.core.analysis.pilot.types import (
    MotionKind,
    PilotEvent,
    _ActionPair,
    _Checkpoint,
    _HoldLogEntry,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _Step,
    _TrialResult,
)
from pyrung.core.analysis.sp_values import _values_match

_PROVISIONAL_SCAN_BUDGET = 2000


def _monitor_trend(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> tuple[PilotEvent, ...]:
    # A provisional attempt changes only the rollback boundary. Every trial
    # inside it still passes through the ordinary trend,
    # regression, investigation, and retry machinery below.
    if state.provisional is not None:
        settlement = _finish_provisional(trial, frame, state, ctx, dbg)
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
        dbg(f"#     FRONTIER: trend {state.best_trend} -> {trial.trend}")
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
    # the watchdog Done bit is in ``changed_tags`` and the liveness hold is
    # surfaced, then revert to the pre-coast checkpoint.
    if (
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
    ):
        chan = trial.zoom_channel_tag
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
            dbg(
                f"#     LETRUN-EJECTION (uninvestigated): {chan} left "
                f"{departed_from!r} -> {trial.fork_snap.get(chan)!r}; "
                "no checkpoint to revert to"
            )
            return (ejection,)
        # Classify BEFORE investigating (detour.py): program-owned motion may
        # preserves the progress gauge and offers a clean forward route —
        # reverting it would throw away the whole march, and investigation
        # would honestly confirm nothing. Affirmative clean-route evidence opens
        # bounded provisional piloting; regression or unknown evidence follows
        # the conservative investigate-and-revert arm.
        verdict = classify_departure(state, ctx, chan, departed_from, trial.before_snap)
        if verdict.is_provisional:
            if state.provisional is None:
                return (
                    ejection,
                    *_start_provisional(verdict, trial, state, ctx, dbg, chan),
                )
            # A clean program-owned departure inside an existing bounded
            # attempt is just more piloting. Keep the original rollback
            # boundary and budget; do not nest another provisional mechanism
            # or reinterpret the motion as a regression.
            dbg(
                f"#     PROVISIONAL-CONTINUES: {chan} {departed_from!r} -> "
                f"{trial.fork_snap.get(chan)!r} ({verdict.reason})"
            )
            return (ejection,)
        dbg(
            f"#     LETRUN-EJECTION: {chan} left "
            f"{departed_from!r}; investigating coast span "
            f"{trial.scan_before}->{state.work.state.scan_id} "
            f"(departure: {verdict.reason})"
        )
        checkpoint = state.checkpoints[-1]
        checkpoint_snap = dict(checkpoint.world.work.state.tags)
        # If the latest receipt precedes the channel state this coast launched
        # from, replay must include earlier motion, including the action that armed the fault;
        # using the post-action frame as "before" would already contain alarm
        # triggers and erase the counterfactual evidence that a permissive
        # clears them.
        replay_from_checkpoint = not _values_match(checkpoint_snap.get(chan), departed_from)
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
                dbg,
                anchor_scan=incident_anchor,
                end_scan=state.work.state.scan_id,
                incident_before_snap=incident_before,
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
        dbg(f"#     BEARING-LANDING: trend baseline {previous} -> {trial.trend}")
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
        dbg(f"#     CHECKPOINT: trend {state.best_trend}")
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
            dbg("#     PROVISIONAL-PROMOTED: ordinary progress banked a checkpoint")
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
        dbg(f"#     CHECKPOINT-FLAT: trend {state.best_trend}")
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

    dbg(f"#     REGRESSION: trend {state.best_trend} -> {trial.trend}, reverting to checkpoint")
    return _investigate_and_revert(
        trial,
        frame,
        state,
        ctx,
        dbg,
        anchor_scan=state.checkpoints[-1].world.work.state.scan_id,
        end_scan=state.work.state.scan_id,
    )


def _bearing_satisfied(trial: _TrialResult) -> bool:
    """Whether VERIFY proved the immediate requested channel bearing."""
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
    dbg: _DebugFn,
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
    assert trial.zoom_channel_tag is not None
    channel_tag = trial.zoom_channel_tag
    dbg(f"#     BEARING-RECEIPT: {channel_tag}={frame.snap.get(channel_tag)!r} before landing")


def _start_provisional(
    verdict: Any,
    trial: _TrialResult,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
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
    state.provisional = Provisional(
        channel_tag=chan,
        from_value=departed_from,
        gauge_at_source=gauge_at_source,
        checkpoint_depth=len(state.checkpoints),
        started_at=scan_before,
        expires_at=min(ctx.max_scans, scan_before + _PROVISIONAL_SCAN_BUDGET),
        classification=verdict.verdict,
    )
    dbg(
        f"#     PROVISIONAL-STARTED: {chan} {departed_from!r} -> "
        f"{verdict.settled_value!r} ({verdict.reason})"
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
                "classification": verdict.verdict,
            },
        ),
    )


def _anchor_provisional(
    frame: _IterationFrame,
    state: _PilotState,
    dbg: _DebugFn,
) -> tuple[PilotEvent, ...]:
    """Assess a newly settled provisional world before choosing another act."""
    provisional = state.provisional
    if provisional is None or len(state.checkpoints) != provisional.checkpoint_depth:
        return ()
    gauge = state.gauge
    outcome = (
        gauge.compare(dict(provisional.gauge_at_source), frame.snap)
        if gauge is not None and gauge.components
        else "unknown"
    )
    if outcome == "advanced":
        state.provisional = None
        state.checkpoints.append(
            _Checkpoint(
                frame.key,
                state.snapshot_world(),
                frame.distance_before,
                frontier_pairs(frame.tree, frame.snap),
            )
        )
        state.best_trend = frame.distance_before
        dbg(f"#     PROVISIONAL-PROMOTED: gauge advanced, trend baseline {frame.distance_before}")
        return (
            PilotEvent(
                "provisional_promoted",
                state.work.state.scan_id,
                {
                    "channel_tag": provisional.channel_tag,
                    "from_value": provisional.from_value,
                    "gauge_at_source": provisional.gauge_at_source,
                    "landing_mark": gauge.mark(frame.snap) if gauge is not None else (),
                    "trend": frame.distance_before,
                    "checkpoint_count": len(state.checkpoints),
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
    dbg(
        f"#     PROVISIONAL-CHECKPOINT: {provisional.channel_tag}="
        f"{frame.snap.get(provisional.channel_tag)!r}, trend {frame.distance_before}"
    )
    return ()


def _finish_provisional(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
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
        dbg(f"#     PROVISIONAL-PROMOTED: gauge {outcome}, trend {promoted_trend}")
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
    if outcome not in {"behind"} and state.work.state.scan_id < provisional.expires_at:
        return None

    state.provisional = None
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
            dbg,
            anchor_scan=state.checkpoints[-1].world.work.state.scan_id,
            end_scan=state.work.state.scan_id,
            incident_before_snap=dict(state.checkpoints[-1].world.work.state.tags),
        )
        return (event, *regression)

    checkpoint = state.checkpoints[-1]
    state.load_world(checkpoint.world)
    state.best_trend = checkpoint.trend
    dbg(f"#     PROVISIONAL-EXPIRED: gauge {outcome}; reverted without a nogood")
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


def _investigate_and_revert(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
    *,
    anchor_scan: int,
    end_scan: int,
    incident_before_snap: dict[str, Any] | None = None,
) -> tuple[PilotEvent, ...]:
    """Build a bounded incident over ``[anchor_scan, end_scan]``, replay-test
    corrective holds, install the confirmed ones, and revert to the last
    checkpoint.

    The window is a parameter because a *regression* anchors at the checkpoint
    scan, while a *terminal-letrun ejection* must anchor at the coast start
    (``trial.scan_before``) — the ejecting watchdog fires mid-coast, so the
    post-eject window the regression path would use misses it.
    """
    checkpoint = state.checkpoints[-1]
    cp_key, cp_world, cp_trend = checkpoint.key, checkpoint.world, checkpoint.trend
    cp_fork = cp_world.work
    investigation_holds: list[Any] = []
    investigation_rungs: list[PilotRung] = []
    investigation_nogoods: set[_ActionPair] = set()
    investigation_payload: dict[str, Any] = {}
    if trial.chase_regression_causes:
        # A watch tag that moved TO a value the target still needs (the
        # checkpoint frontier) is *progress*, not a departure — the coast exists
        # to move it (Heat_CurStep 0->1 en route to 3).  Chasing it spawns
        # corrective holds against the plan itself (lock the enabler of the
        # very advance we wanted).  Only anomalous motion enters the bearing.
        needed_by_tag: dict[str, list[Any]] = {}
        for nt, nv in checkpoint.frontier:
            needed_by_tag.setdefault(nt, []).append(nv)
        bearing_pairs: list[_ActionPair] = [
            (wt, frame.snap.get(wt))
            for wt in state.watch_tags
            if not _values_match(frame.snap.get(wt), trial.fork_snap.get(wt))
            and not any(
                _values_match(trial.fork_snap.get(wt), nv) for nv in needed_by_tag.get(wt, ())
            )
        ]
        if trial.zoom_channel_tag is not None:
            chan = trial.zoom_channel_tag
            chan_actual = trial.fork_snap.get(chan)
            if not _values_match(chan_actual, trial.zoom_target_value):
                bearing_pairs = [(t, v) for t, v in bearing_pairs if t != chan]
                bearing_pairs.append((chan, trial.zoom_target_value))
        bearing = tuple(bearing_pairs)
        incident = build_deviation_incident(
            state.work,
            anchor_scan=anchor_scan,
            end_scan=end_scan,
            action=trial.applied,
            bearing=bearing,
            before_snap=incident_before_snap or frame.snap,
            after_snap=trial.fork_snap,
            program=ctx.program,
            channel_tag=trial.zoom_channel_tag,
        )

        replay_steps = tuple(
            step for step in state.steps if step.scan_before >= cp_fork.state.scan_id
        )
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
                tuple(r.channel_tag for r in ctx.pipeline_roles)
                if trial.motion is MotionKind.COAST_HOLDING_WORLD
                else None
            ),
            departure_scan=incident.departure_scan,
            departure_bearing=tuple((d.tag, d.value) for d in incident.departures),
            eject_cause_dones=incident_eject_dones(incident, ctx.program),
            progress_gauge=state.gauge,
            progress_anchor=dict(cp_fork.state.tags),
            eject_latch_baseline=incident_eject_latches(state.work, incident, ctx.pdg, ctx.program),
        )

        # The register set the target still needs: the checkpoint's *frontier*,
        # captured when the checkpoint was created (the frame that computed the
        # distance and launched the coast).  The live frame here is useless — a
        # terminal-let-run frame is a coast with no tree — and re-deriving loses
        # the non-steerable interior needs (``Heat_CurStep = 3``) that
        # ``ordered_actions()``-style extractions can never surface.
        needed = list(checkpoint.frontier)
        investigation = investigate_deviation(
            state.work,
            incident,
            ctx,
            replay,
            needed=needed,
            installed={r.dest: r.value for r in state.rungs},
            pilot_touched=pilot_touched_tags(
                state.hold_log, state.journey, {r.dest: r.value for r in state.rungs}
            ),
        )
        investigation_nogoods.update(investigation.regression_nogoods)
        # Investigation has already derived a finite guard and replayed this
        # exact installed form. ASSESS does not reinterpret that proof through
        # a second, globally-steady-hold rule.
        investigation_holds.extend(investigation.confirmed_holds)

        def _hyp_detail(h: Any) -> dict[str, Any]:
            return {
                "kind": h.kind,
                "holds": h.holds,
                "sources": h.sources,
                "detail": h.detail,
            }

        investigation_payload = {
            "hypotheses": len(investigation.hypotheses),
            "confirmed": len(investigation.confirmed),
            "rejected": len(investigation.rejected),
            "unresolved": investigation.unresolved,
            "hypothesis_detail": tuple(_hyp_detail(h) for h in investigation.hypotheses),
            "confirmed_detail": tuple(_hyp_detail(h) for h in investigation.confirmed),
            "rejected_detail": tuple(_hyp_detail(h) for h in investigation.rejected),
        }
        if investigation_holds:
            # Investigation owns applicability and replayed these exact guarded
            # rungs. ASSESS only installs the proved intervention.
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
                )
            )
            for proposal in investigation_holds:
                ht, hv = (
                    (proposal.dest, proposal.value) if isinstance(proposal, PilotRung) else proposal
                )
                dbg(f"#     HOLD {ht}={hv!r} (from investigation)")

    # Legibility (recording only): the channel transition(s) this revert undoes.
    # A destructive move (``S_StateCurrent 6->8`` Aborting) and a program-intended
    # useful program-owned move (``6->11`` Held) both leave the bearing, but only the former
    # is a genuine error — printing the reverted channel edge separates them in
    # every transcript.  Read the channel value at the checkpoint (from) vs. the
    # regressed frame (to); a channel is any opaque-loop pipeline register.
    channel_transitions: tuple[tuple[str, Any, Any], ...] = _channel_transitions(
        ctx, cp_fork, trial.fork_snap
    )
    if channel_transitions:
        dbg(
            "#     REGRESSION channel: "
            + ", ".join(f"{t} {fv!r}->{tv!r}" for t, fv, tv in channel_transitions)
        )

    # Keep the failed action as a nogood in the world where it failed. A
    # replay-confirmed correction creates a different world key, so the same
    # action is naturally eligible there without deleting valid history.
    regression_nogoods = set(investigation_nogoods)
    regression_nogoods.update(trial.regression_nogoods)
    state.nogoods.setdefault(cp_key, set()).update(regression_nogoods)
    dbg(
        f"#     REGRESSION-NOGOOD at checkpoint: {sorted(regression_nogoods, key=_action_sort_key)}"
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
    state.load_world(cp_world)
    if investigation_rungs:
        state.rungs = _append_rungs(state.work, investigation_rungs, state.rungs)
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
            },
        ),
    )
