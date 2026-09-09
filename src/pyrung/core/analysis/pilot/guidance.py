"""Bounded breadth-first survey of assignments at one exact scan boundary.

The compiled kernel screens single changes before pairs. Promising changes
are checked on ordinary disposable PLC forks. Only their observations escape;
there is no successor queue, predecessor chain, retained fork, or future act.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from pyrung.core.analysis.pilot.compass import CompassObservation
from pyrung.core.analysis.pilot.skiff import _declared_domain, _skiff_expectation, run_pinned_scan
from pyrung.core.analysis.pilot.steer import StaleBearingError
from pyrung.core.analysis.pilot.world_key import ReadIdentity
from pyrung.core.analysis.prove.kernel import _restore_kernel, _snapshot_kernel
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.tag import TagType


@dataclass(frozen=True)
class SurveyResult:
    observations: tuple[CompassObservation, ...] = ()
    evaluations: int = 0
    confirmations: int = 0
    reason: str = ""


def survey_current_inputs(world: Any, *, max_evaluations: int = 64) -> SurveyResult:
    """Survey finite current-scan assignments; findings grant no adoption."""
    state, ctx = world.state, world.context
    work = state.work
    frame = world.frame
    if ReadIdentity.capture(work, ctx.compass.knowledge) != world.read_identity:
        raise StaleBearingError("cannot survey inputs from a stale current-world read")
    relevant = frozenset(
        node.tag
        for node in frame.tree.iter_nodes()
        if node.tag in ctx.pdg.writers_of and node.tag not in ctx.steerable
    )
    cone: set[str] = set(relevant)
    for tag in relevant:
        cone.update(ctx.pdg.upstream_slice(tag, follow_calls=True))
    changes = []
    for name in sorted(ctx.steerable & cone):
        ref = work._known_tags_by_name.get(name)
        domain = (
            (False, True) if ref is not None and ref.type is TagType.BOOL else _declared_domain(ref)
        )
        if domain is None:
            continue
        for value in domain:
            if (name, value) not in ctx.blocked_actions and not _values_match(
                frame.snap.get(name), value
            ):
                changes.append((name, value))
    if not changes or not relevant:
        return SurveyResult(reason="no finite relevant input domain")
    compiled = work._compiled_replay_supported_kernel()
    if compiled is None or work._dt_override_for_next_scan is not None:
        return SurveyResult(reason="current execution configuration has no compiled survey")
    replay = CompiledPLC(work._soft_exec_program(), work.state, dt=work._dt, compiled=compiled)
    replay._set_rtc_internal(work._system_runtime._rtc_now(work.state), work.state.timestamp)
    source = _snapshot_kernel(replay._kernel)
    pending = dict(work._input_overrides.pending_patches)
    forces = dict(work._input_overrides.forces)
    evaluations = 0
    confirmations = 0

    def preview(actions: tuple[tuple[str, Any], ...]) -> dict[str, Any] | None:
        nonlocal evaluations
        if evaluations >= max_evaluations or state.budget.remaining(ctx.max_scans) < 1:
            return None
        state.budget.charge()
        evaluations += 1
        _restore_kernel(replay._kernel, source)
        for spec in compiled.block_specs.values():
            replay._kernel.load_block_from_tags(spec)
        replay._state = work.state
        replay._sync_runtime_flags_from_state()
        replay._input_overrides.pending_patches.clear()
        replay._input_overrides.forces_mutable.clear()
        replay._input_overrides.forces_mutable.update(forces)
        replay.patch({**pending, **dict(actions)})
        replay.step_replay()
        return dict(replay._kernel.tags)

    baseline = preview(())
    if baseline is None:
        return SurveyResult(evaluations=evaluations, reason="survey budget exhausted")
    allowed = frozenset(work.state.tags) | frozenset(work._known_tags_by_name)
    control = None
    # Width is bounded before evaluation, and cardinality is breadth-first.
    # Even a pair is one simultaneous assignment, never two future steps.
    for width in (1, 2):
        for actions in itertools.combinations(changes, width):
            if len({name for name, _value in actions}) != width:
                continue
            prediction = preview(actions)
            if prediction is None:
                return SurveyResult(
                    evaluations=evaluations,
                    confirmations=confirmations,
                    reason="survey budget exhausted",
                )
            useful = tuple(
                tag
                for tag in sorted(relevant)
                if not _values_match(prediction.get(tag), baseline.get(tag))
                and not _values_match(prediction.get(tag), frame.snap.get(tag))
            )
            if not useful:
                continue
            # Require an ordinary control and intervention from the same
            # source; a compiled prediction is never navigation evidence.
            needed = 2 if control is None else 1
            if state.budget.remaining(ctx.max_scans) < needed:
                return SurveyResult(
                    evaluations=evaluations,
                    confirmations=confirmations,
                    reason="confirmation budget exhausted",
                )
            if control is None:
                state.budget.charge()
                confirmations += 1
                control = run_pinned_scan(work, allowed, ctx.pdg, pilot_rungs=state.pilot_rungs)
            state.budget.charge()
            confirmations += 1
            result = run_pinned_scan(
                work, allowed, ctx.pdg, pilot_rungs=state.pilot_rungs, actions=actions
            )
            cause: Any = actions[0] if width == 1 else actions
            observations = tuple(
                CompassObservation(
                    "edge",
                    tag,
                    cause,
                    frame.snap.get(tag),
                    result.after.get(tag),
                    frame.key,
                    tuple(sorted(frame.snap.items())),
                    actions,
                    _skiff_expectation(result, ctx, tag, result.after.get(tag)),
                )
                for tag in useful
                if _values_match(result.after.get(tag), prediction.get(tag))
                and _values_match(control.after.get(tag), frame.snap.get(tag))
            )
            if observations:
                return SurveyResult(
                    observations, evaluations, confirmations, "confirmed current-scan effect"
                )
    return SurveyResult(
        evaluations=evaluations,
        confirmations=confirmations,
        reason="no useful effect in bounded assignment survey",
    )
