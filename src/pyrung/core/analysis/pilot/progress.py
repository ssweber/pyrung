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

from pyrung.core.analysis.pilot._ops import _DebugFn, _install_holds
from pyrung.core.analysis.pilot.compass import _action_sort_key
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
        and trial.zoom_governing_tag is not None
    ):
        gov = trial.zoom_governing_tag
        investigated = bool(state.checkpoints)
        ejection = PilotEvent(
            "letrun_ejection",
            state.work.state.scan_id,
            {
                "governing_tag": gov,
                "from_value": trial.zoom_target_value,
                "to_value": trial.fork_snap.get(gov),
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
                f"#     LETRUN-EJECTION (uninvestigated): {gov} left "
                f"{trial.zoom_target_value!r} -> {trial.fork_snap.get(gov)!r}; "
                "no checkpoint to revert to"
            )
            return (ejection,)
        dbg(
            f"#     LETRUN-EJECTION: {gov} left "
            f"{trial.zoom_target_value!r}; investigating coast span "
            f"{trial.scan_before}->{state.work.state.scan_id}"
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
            _Checkpoint(trial.new_key, state.snapshot_world(), trial.trend, trial.frontier)
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
            _Checkpoint(trial.new_key, state.snapshot_world(), trial.trend, trial.frontier)
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
    investigation_holds: list[_ActionPair] = []
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
        if trial.zoom_governing_tag is not None:
            gov = trial.zoom_governing_tag
            gov_actual = trial.fork_snap.get(gov)
            if not _values_match(gov_actual, trial.zoom_target_value):
                bearing_pairs = [(t, v) for t, v in bearing_pairs if t != gov]
                bearing_pairs.append((gov, trial.zoom_target_value))
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
            governing_tag=trial.zoom_governing_tag,
        )

        replay_steps = tuple(
            step for step in state.steps if step.scan_before >= cp_fork.state.scan_id
        )
        replay = build_replay_fn(
            cp_fork,
            cp_trend,
            dict(state.forced_holds),
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
            zoom_governing_tag=trial.zoom_governing_tag,
            zoom_target_value=trial.zoom_target_value,
            terminal_letrun_role_tags=(
                tuple(r.governing_tag for r in ctx.pipeline_roles)
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
            installed=dict(state.forced_holds),
        )
        investigation_nogoods.update(investigation.regression_nogoods)
        # Drop a confirmed hold that is *self-defeating*: held steady it pins a
        # register the target still needs away from its needed value (an init /
        # reset / pause enabler that re-inits progress every scan), so the coast
        # can never reach the target even though the hold "confirmed" against the
        # bounded macro-state check.  The confirmation window is too short to see
        # the lost progress; this catches it statically.  The direct-pin case
        # (a hold on a needed register itself) is the ``needed_tags`` guard.
        investigation_holds.extend(
            (ht, hv)
            for ht, hv in investigation.confirmed_holds
            if ht not in needed_tags
            and not hold_defeats_needed(ht, hv, needed, ctx.pdg, ctx.program)
        )

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
            _install_holds(state.work, investigation_holds, state.forced_holds)
            state.hold_log.append(
                _HoldLogEntry(
                    scan=cp_fork.state.scan_id,
                    tags=tuple(investigation_holds),
                    source="investigation",
                )
            )
            for ht, hv in investigation_holds:
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
            (ht, hv)
            for ht, hv in state.forced_holds.items()
            if hold_defeats_needed(ht, hv, cp_needed, ctx.pdg, ctx.program)
        ]
        for ht, hv in released_holds:
            del state.forced_holds[ht]
            dbg(f"#     RELEASE {ht}={hv!r} (self-defeating vs checkpoint frontier)")
        if released_holds:
            state.hold_log.append(
                _HoldLogEntry(
                    scan=cp_fork.state.scan_id,
                    tags=tuple(released_holds),
                    source="self-defeat-release",
                )
            )

    regression_nogoods = investigation_nogoods | set(trial.regression_nogoods)
    state.nogoods.setdefault(cp_key, set()).update(regression_nogoods)
    dbg(
        f"#     REGRESSION-NOGOOD at checkpoint: {sorted(regression_nogoods, key=_action_sort_key)}"
    )
    state.load_world(cp_world)
    _install_holds(state.work, list(state.forced_holds.items()), {})
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
                "forced_holds": dict(state.forced_holds),
                "released_holds": tuple(released_holds),
                "investigation": investigation_payload,
            },
        ),
    )
