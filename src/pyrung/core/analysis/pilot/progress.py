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
    _rungs_from_proposals,
    _set_rungs,
    _target_unresolved_condition,
)
from pyrung.core.analysis.pilot.causal import pilot_touched_tags
from pyrung.core.analysis.pilot.compass import _action_sort_key
from pyrung.core.analysis.pilot.detour import (
    Detour,
    classify_departure,
    detour_signature,
)
from pyrung.core.analysis.pilot.investigate import (
    build_deviation_incident,
    build_replay_fn,
    hold_defeats_needed,
    incident_eject_dones,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.outcome import Outcome
from pyrung.core.analysis.pilot.types import (
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


def _monitor_trend(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> tuple[PilotEvent, ...]:
    if trial.new_key is None or trial.trend is None:
        return ()

    assert state.best_trend is not None

    # ── Detour result (detour.py) ──
    # The corridor rejoin is where the provisional verdict becomes real.
    # Gauge advanced → the detour worked, so checkpoint it (baseline resets —
    # trend numbers from the old corridor don't compare). Anything else → the
    # detour failed: revert and remember the signature so the re-ejection
    # classifies as regression and investigation gets a tight fresh window.
    if state.detour is not None:
        settlement = _finish_detour(trial, state, dbg)
        if settlement is not None:
            return settlement

    # A FRONTIER outcome means the pilot knowingly entered a corridor with
    # more prerequisites.  Commit the observation, but keep the previous
    # checkpoint and high-water mark alive: if the new corridor keeps drifting
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

    # A terminal let-run that *ejected* — the macro-state left the value it was
    # held at and the coast wandered into a side branch (Execute -> Aborting).
    # That branch's trace distance is misleadingly LOW (fewer open leaves than the
    # held state), so the ordinary ``trend < best_trend`` test below would
    # checkpoint the ejection as progress.  It is not progress: the watchdog that
    # ejected fired *during the coast*, not after it.  Investigate over the
    # coast-span window (the fork's own history, ``scan_before -> fork end``) so
    # the watchdog Done bit is in ``changed_tags`` and the liveness hold is
    # surfaced, then revert to the pre-coast checkpoint.
    if (
        trial.observe_label == "letrun"
        and trial.outcome == Outcome.AMBIENT_DRIFT
        and trial.zoom_channel_tag is not None
    ):
        chan = trial.zoom_channel_tag
        investigated = bool(state.checkpoints)
        ejection = PilotEvent(
            "letrun_ejection",
            state.work.state.scan_id,
            {
                "channel_tag": chan,
                "from_value": trial.zoom_target_value,
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
                f"{trial.zoom_target_value!r} -> {trial.fork_snap.get(chan)!r}; "
                "no checkpoint to revert to"
            )
            return (ejection,)
        # Classify BEFORE investigating (detour.py): a program-intended detour
        # preserves the progress gauge and offers a clean forward route —
        # reverting it would throw away the whole march, and investigation
        # would honestly confirm nothing. A stopover verdict starts a detour
        # (provisional until corridor rejoin, pre-detour checkpoint retained); a
        # regression verdict falls through to investigate-and-revert unchanged.
        verdict = classify_departure(state, ctx, chan, trial.zoom_target_value)
        if verdict.is_stopover:
            return (ejection, *_start_detour(verdict, trial, state, dbg, chan))
        dbg(
            f"#     LETRUN-EJECTION: {chan} left "
            f"{trial.zoom_target_value!r}; investigating coast span "
            f"{trial.scan_before}->{state.work.state.scan_id} "
            f"(departure: {verdict.reason})"
        )
        return (
            ejection,
            *_investigate_and_revert(
                trial,
                frame,
                state,
                ctx,
                dbg,
                anchor_scan=trial.scan_before,
                end_scan=state.work.state.scan_id,
            ),
        )

    if trial.trend < state.best_trend:
        state.checkpoints.append(
            _Checkpoint(
                trial.new_key,
                state.snapshot_world(),
                trial.trend,
                trial.frontier,
                len(state.rungs),
            )
        )
        state.best_trend = trial.trend
        dbg(f"#     CHECKPOINT: trend {state.best_trend}")
        return (
            PilotEvent(
                "trend_checkpoint",
                state.work.state.scan_id,
                {
                    "trend": state.best_trend,
                    "key": trial.new_key,
                    "checkpoint_count": len(state.checkpoints),
                },
            ),
        )

    if trial.trend == state.best_trend and trial.outcome == Outcome.CONFIRMED:
        state.checkpoints.append(
            _Checkpoint(
                trial.new_key,
                state.snapshot_world(),
                trial.trend,
                trial.frontier,
                len(state.rungs),
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


def _start_detour(
    verdict: Any,
    trial: _TrialResult,
    state: _PilotState,
    dbg: _DebugFn,
    chan: str,
) -> tuple[PilotEvent, ...]:
    """Start a provisional stopover — no investigation, no revert.

    Adopt the settled landing as the working world (the ejection guard paused
    mid-transition; the landing is where the machine actually parked), count
    those scans as dwell, and record the detour. No checkpoint is created until
    the gauge is compared at corridor rejoin (``_finish_detour``)."""
    gauge = state.gauge
    gauge_at_departure = gauge.mark(dict(state.work.state.tags))
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
    state.detour = Detour(
        channel_tag=chan,
        from_value=trial.zoom_target_value,
        gauge_at_departure=gauge_at_departure,
        pre_detour_checkpoint_len=len(state.checkpoints),
        taken_at_scan=scan_before,
        signature=detour_signature(chan, trial.zoom_target_value, verdict.settled_value),
    )
    dbg(
        f"#     DETOUR-STARTED: {chan} {trial.zoom_target_value!r} -> "
        f"{verdict.settled_value!r} ({verdict.reason})"
    )
    return (
        PilotEvent(
            "detour_started",
            state.work.state.scan_id,
            {
                "channel_tag": chan,
                "from_value": trial.zoom_target_value,
                "settled_value": verdict.settled_value,
                "reason": verdict.reason,
                "route": verdict.route,
                "settle_scans": verdict.settle_scans,
                "gauge_at_departure": gauge_at_departure,
            },
        ),
    )


def _finish_detour(
    trial: _TrialResult,
    state: _PilotState,
    dbg: _DebugFn,
) -> tuple[PilotEvent, ...] | None:
    """Finish the active detour when the corridor is rejoined; None = not yet.

    Rejoin = the committed trial's world has the channel back at the detour's
    departure value. Gauge advanced → worked (checkpoint at the rejoin;
    the trend baseline resets — distances from different corridors don't
    compare).  Preserved/behind/unknown → the departure gained nothing:
    revert to the pre-detour checkpoint, remember the failed signature, and
    re-arm let-run there so the ejection recurs and classifies as
    regression (investigation then owns a tight fresh window)."""
    detour: Detour = state.detour
    now_snap = trial.fork_snap or {}
    if not _values_match(now_snap.get(detour.channel_tag), detour.from_value):
        return None  # still on the detour — the detour rides
    gauge = state.gauge
    anchor = dict(detour.gauge_at_departure)
    outcome = gauge.compare(anchor, now_snap)
    if outcome == "advanced":
        state.detour = None
        assert trial.new_key is not None and trial.trend is not None
        state.checkpoints.append(
            _Checkpoint(
                trial.new_key,
                state.snapshot_world(),
                trial.trend,
                trial.frontier,
                len(state.rungs),
            )
        )
        state.best_trend = trial.trend
        dbg(f"#     DETOUR-WORKED: gauge advanced, trend baseline {trial.trend}")
        return (
            PilotEvent(
                "detour_worked",
                state.work.state.scan_id,
                {
                    "channel_tag": detour.channel_tag,
                    "from_value": detour.from_value,
                    "gauge_at_departure": detour.gauge_at_departure,
                    "rejoin_mark": gauge.mark(now_snap),
                    "trend": trial.trend,
                    "checkpoint_count": len(state.checkpoints),
                },
            ),
        )
    # The trip earned nothing (or lost work): fail the detour.
    state.failed_detours.add(detour.signature)
    state.detour = None
    del state.checkpoints[detour.pre_detour_checkpoint_len :]
    checkpoint = state.checkpoints[-1]
    state.load_world(checkpoint.world)
    del state.rungs[checkpoint.rung_cursor :]
    _set_rungs(state.work, state.rungs)
    state.best_trend = checkpoint.trend
    state.letrun_tried.pop(checkpoint.key, None)
    dbg(f"#     DETOUR-FAILED: gauge {outcome} at rejoin; reverted")
    return (
        PilotEvent(
            "detour_failed",
            state.work.state.scan_id,
            {
                "channel_tag": detour.channel_tag,
                "from_value": detour.from_value,
                "outcome": outcome,
                "gauge_at_departure": detour.gauge_at_departure,
                "signature": detour.signature,
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
    distinguishable from a program-intended detour (``6->11`` Held) in the
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
            before_snap=frame.snap,
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
                if trial.observe_label == "letrun"
                else None
            ),
            departure_scan=incident.departure_scan,
            departure_bearing=tuple((d.tag, d.value) for d in incident.departures),
            eject_cause_dones=incident_eject_dones(incident, ctx.program),
        )

        # The register set the target still needs: the checkpoint's *frontier*,
        # captured when the checkpoint was created (the frame that computed the
        # distance and launched the coast).  The live frame here is useless — a
        # terminal-let-run frame is a coast with no tree — and re-deriving loses
        # the non-steerable interior needs (``Heat_CurStep = 3``) that
        # ``ordered_actions()``-style extractions can never surface.
        needed = list(checkpoint.frontier)
        needed_tags = {t for t, _ in needed}
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
        # Drop a confirmed hold that is *self-defeating*: held steady it pins a
        # register the target still needs away from its needed value (an init /
        # reset / pause enabler that re-inits progress every scan), so the coast
        # can never reach the target even though the hold "confirmed" against the
        # bounded macro-state check.  The confirmation window is too short to see
        # the lost progress; this catches it statically.  The direct-pin case
        # (a hold on a needed register itself) is the ``needed_tags`` guard.
        for proposal in investigation.confirmed_holds:
            ht, hv = (
                (proposal.dest, proposal.value) if isinstance(proposal, PilotRung) else proposal
            )
            if ht not in needed_tags and not hold_defeats_needed(
                ht, hv, needed, ctx.pdg, ctx.program
            ):
                investigation_holds.append(proposal)

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
            scope = _target_unresolved_condition(
                state.work,
                ctx.target_tag,
                ctx.target_value,
                getattr(ctx, "target_predicate", None),
            )
            investigation_rungs = _rungs_from_proposals(state.work, investigation_holds, scope)
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

    # Prune *already-installed* holds the checkpoint frontier now proves
    # self-defeating.  An establish-phase prerequisite (``Heat_xInit=1``) is a
    # correct lever for an en-route need (it writes ``Heat_CurStep := 1``), but
    # held steady past that step it pins the chain — a defeat only visible once
    # the coast regresses.  Released here, the next iteration's trace re-proposes
    # the lever as a *pulse* if the en-route need still stands.
    released_holds: list[_ActionPair] = []
    if trial.chase_regression_causes and state.checkpoints[-1].frontier:
        cp_needed = list(state.checkpoints[-1].frontier)
        released_holds = [
            (r.dest, r.value)
            for r in state.rungs
            if hold_defeats_needed(r.dest, r.value, cp_needed, ctx.pdg, ctx.program)
        ]
        released = set(released_holds)
        state.rungs[:] = [r for r in state.rungs if (r.dest, r.value) not in released]
        for ht, hv in released_holds:
            dbg(f"#     RELEASE {ht}={hv!r} (self-defeating vs checkpoint frontier)")
        if released_holds:
            _set_rungs(state.work, state.rungs)
            state.hold_log.append(
                _HoldLogEntry(
                    scan=cp_fork.state.scan_id,
                    tags=tuple(released_holds),
                    source="self-defeat-release",
                )
            )

    # Legibility (recording only): the channel transition(s) this revert undoes.
    # A destructive move (``S_StateCurrent 6->8`` Aborting) and a program-intended
    # detour (``6->11`` Held) both regress the target bearing, but only the former
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

    regression_nogoods = investigation_nogoods | set(trial.regression_nogoods)
    state.nogoods.setdefault(cp_key, set()).update(regression_nogoods)
    dbg(
        f"#     REGRESSION-NOGOOD at checkpoint: {sorted(regression_nogoods, key=_action_sort_key)}"
    )
    # A revert ends any open detour — the world it was riding is gone.
    state.detour = None
    state.load_world(cp_world)
    del state.rungs[checkpoint.rung_cursor :]
    if investigation_rungs:
        _append_rungs(state.work, investigation_rungs, state.rungs)
    else:
        _set_rungs(state.work, state.rungs)
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
                "released_holds": tuple(released_holds),
                "channel_transitions": channel_transitions,
                "investigation": investigation_payload,
            },
        ),
    )
