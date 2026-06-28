"""Trend monitoring and regression recovery for PILOT.

Tracks distance trend across iterations, manages checkpoints, and triggers
investigation when the pilot regresses.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pilot._ops import _DebugFn, _install_holds
from pyrung.core.analysis.pilot.investigate import (
    build_deviation_incident,
    build_replay_fn,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.outcome import Outcome
from pyrung.core.analysis.pilot.types import (
    PilotEvent,
    _ActionPair,
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
        if not state.checkpoints:
            return ()
        dbg(
            f"#     LETRUN-EJECTION: {trial.zoom_governing_tag} left "
            f"{trial.zoom_target_value!r}; investigating coast span "
            f"{trial.scan_before}->{state.work.state.scan_id}"
        )
        return _investigate_and_revert(
            trial,
            frame,
            state,
            ctx,
            dbg,
            anchor_scan=trial.scan_before,
            end_scan=state.work.state.scan_id,
        )

    if trial.trend < state.best_trend:
        state.checkpoints.append((trial.new_key, state.work.fork(), trial.trend))
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
        state.checkpoints.append((trial.new_key, state.work.fork(), trial.trend))
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
        anchor_scan=state.checkpoints[-1][1].state.scan_id,
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
    cp_key, cp_fork, cp_trend = state.checkpoints[-1]
    investigation_holds: list[_ActionPair] = []
    investigation_nogoods: set[_ActionPair] = set()
    investigation_payload: dict[str, Any] = {}
    if trial.chase_regression_causes:
        bearing_pairs: list[_ActionPair] = [
            (wt, frame.snap.get(wt))
            for wt in state.watch_tags
            if not _values_match(frame.snap.get(wt), trial.fork_snap.get(wt))
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
            action=trial.pulse_actions,
            bearing=bearing,
            before_snap=frame.snap,
            after_snap=trial.fork_snap,
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
            choice=ctx.choice,
            zoom_governing_tag=trial.zoom_governing_tag,
            zoom_target_value=trial.zoom_target_value,
            terminal_letrun_role_tags=(
                tuple(r.governing_tag for r in ctx.pipeline_roles)
                if trial.observe_label == "letrun"
                else None
            ),
            departure_scan=incident.departure_scan,
            departure_bearing=tuple(
                (d.tag, d.value) for d in incident.departures
            ),
        )

        investigation = investigate_deviation(state.work, incident, ctx, replay)
        investigation_nogoods.update(investigation.regression_nogoods)
        needed_tags = {a for a, _ in frame.tree.ordered_actions()}
        investigation_holds.extend(
            (ht, hv) for ht, hv in investigation.confirmed_holds if ht not in needed_tags
        )
        investigation_payload = {
            "hypotheses": len(investigation.hypotheses),
            "confirmed": len(investigation.confirmed),
            "rejected": len(investigation.rejected),
            "unresolved": investigation.unresolved,
            "hypothesis_detail": tuple(
                {
                    "kind": h.kind,
                    "holds": h.holds,
                    "sources": h.sources,
                    "detail": h.detail,
                }
                for h in investigation.hypotheses
            ),
        }
        if investigation_holds:
            _install_holds(state.work, investigation_holds, state.forced_holds)
            for ht, hv in investigation_holds:
                dbg(f"#     HOLD {ht}={hv!r} (from investigation)")

    regression_nogoods = investigation_nogoods | set(trial.regression_nogoods)
    state.nogoods.setdefault(cp_key, set()).update(regression_nogoods)
    dbg(f"#     REGRESSION-NOGOOD at checkpoint: {sorted(regression_nogoods)}")
    state.work = cp_fork.fork()
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
                "investigation": investigation_payload,
            },
        ),
    )
