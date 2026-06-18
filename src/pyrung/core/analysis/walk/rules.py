"""Learned temporal rule evidence and recovery helpers for the walker."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.walk.base import (
    AvoidEvent,
    EvidenceRef,
    LevelRule,
    TemporalRule,
    _Action,
    _StepMonitors,
    _values_match,
    _WalkContext,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.causal.models import CausalChain, Transition
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_MAX_CAUSE_DEPTH = 32
_MAX_CYCLE_SEGMENTS = 8


@dataclass(frozen=True)
class RecursiveEvidence:
    """Compact result of recursive trigger/enabler chasing."""

    goals: tuple[tuple[str, Any], ...]
    roots: tuple[Any, ...]


def recursive_cause_evidence(
    ctx: _WalkContext,
    work: PLC,
    chain: CausalChain,
    *,
    target_tag: str,
    monitors: _StepMonitors,
) -> RecursiveEvidence:
    """Chase triggers, falling back to non-stay enablers when roots are empty."""
    protected = _protected_names(ctx, monitors)
    stay_context = _stay_context_from_monitors(monitors)
    goals: list[tuple[str, Any]] = []
    seen_goals: set[tuple[str, Any]] = set()

    def add_goal(name: str, value: Any) -> None:
        key = (name, value)
        if key in seen_goals or name == target_tag:
            return
        if _values_match(work.state.tags.get(name), value):
            return
        seen_goals.add(key)
        goals.append(key)

    roots = _walk_chain(ctx, work, chain, protected, stay_context, set(), 0)
    for root in roots:
        if _is_actionable_root(ctx, root.tag_name):
            add_goal(root.tag_name, root.to_value)
    return RecursiveEvidence(tuple(goals), tuple(roots))


def record_regression_evidence(
    ctx: _WalkContext,
    work: PLC,
    goal: tuple[str, Any],
    broken_by: tuple[str, Any],
) -> None:
    """Record actual-cause evidence for a committed goal that regressed."""
    scan = _leaving_committed_scan(work, goal[0], goal[1])
    try:
        chain = work.cause(goal[0], scan=scan) if scan is not None else work.cause(goal[0])
    except Exception:  # noqa: BLE001 - evidence is best-effort
        logger.debug("walk: regression cause(%s) raised", goal[0], exc_info=True)
        return
    if chain is None:
        return
    recursive_cause_evidence(ctx, work, chain, target_tag=goal[0], monitors=_StepMonitors())
    if ctx.debug_sink is not None:
        ctx.debug_sink.emit(
            "rule-evidence",
            tag=goal[0],
            value=goal[1],
            detail=f"regressed after {broken_by[0]}={broken_by[1]!r}",
        )


def mine_regression_holds(
    ctx: _WalkContext,
    work: PLC,
    regressed_goal: tuple[str, Any],
    *,
    scan: int | None = None,
) -> list[tuple[str, Any]]:
    """Mine protective input holds from the actual cause of a regression."""
    cause_scan = (
        scan
        if scan is not None
        else _leaving_committed_scan(work, regressed_goal[0], regressed_goal[1])
    )
    try:
        chain = (
            work.cause(regressed_goal[0], scan=cause_scan)
            if cause_scan is not None
            else work.cause(regressed_goal[0])
        )
    except Exception:  # noqa: BLE001 - evidence is best-effort
        logger.debug("walk: regression cause(%s) raised", regressed_goal[0], exc_info=True)
        return []
    if chain is None:
        return []

    monitors = _StepMonitors()
    roots = _walk_chain(
        ctx,
        work,
        chain,
        _protected_names(ctx, monitors),
        _stay_context_from_monitors(monitors),
        set(),
        0,
    )
    holds: list[tuple[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    held_names = ctx.holds.protected_names() if ctx.holds else frozenset()
    for root in roots:
        if not _is_actionable_root(ctx, root.tag_name):
            continue
        if root.from_value is None:
            continue
        if _values_match(root.from_value, root.to_value):
            # Steady-state enabler: this input didn't change but it enables
            # the regressed state.  For Bool inputs the protective hold is
            # the opposite value — flipping it disables the regression path.
            if root.tag_name in held_names:
                continue
            if isinstance(root.to_value, bool):
                hold = (root.tag_name, not root.to_value)
                if hold not in seen:
                    seen.add(hold)
                    holds.append(hold)
            continue
        hold = (root.tag_name, root.from_value)
        if hold in seen:
            continue
        seen.add(hold)
        holds.append(hold)

    return holds


def _committed_departure(work: PLC, tag: str, committed: Any) -> tuple[int, int] | None:
    """Return ``(holding_scan, departure_scan)`` for *tag*'s latest departure.

    ``holding_scan`` is the last scan where *tag* still held *committed*;
    ``departure_scan`` is the next scan, where it left.  ``None`` when *tag*
    never departed *committed* in retained history.
    """
    try:
        history = work.history
        states = history.range(history.oldest_scan_id, history.newest_scan_id + 1)
    except Exception:  # noqa: BLE001 - regression protection is best-effort
        logger.debug("walk: regression history scan(%s) raised", tag, exc_info=True)
        return None

    for index in range(len(states) - 1, 0, -1):
        prev = states[index - 1].tags.get(tag)
        cur = states[index].tags.get(tag)
        if _values_match(prev, committed) and not _values_match(cur, committed):
            return states[index - 1].scan_id, states[index].scan_id
    return None


def _leaving_committed_scan(work: PLC, tag: str, committed: Any) -> int | None:
    """Return the latest scan where *tag* departed its committed value."""
    found = _committed_departure(work, tag, committed)
    return found[1] if found is not None else None


def _last_committed_scan(work: PLC, tag: str, committed: Any) -> int | None:
    """Return the latest scan where *tag* still held its committed value.

    The pre-departure anchor for the counterfactual hold sweep: a scan where
    the goal provably held, so a perturb-and-survive probe is well-posed.
    """
    found = _committed_departure(work, tag, committed)
    return found[0] if found is not None else None


def temporal_cycle_recovery(
    ctx: _WalkContext,
    work: PLC,
    target_tag: str,
    target_value: Any,
    budget: int,
    monitors: _StepMonitors,
) -> list[_Action] | None:
    """Try active cycle rules as late recovery candidates."""
    if budget <= 0:
        return None
    for learned in ctx.rules.active_temporal():
        payload = learned.payload
        if not isinstance(payload, TemporalRule) or payload.kind != "cycle":
            continue
        if payload.tag not in ctx.ext_inputs and payload.tag not in ctx.edge_ext:
            continue
        steps = _cycle_candidate(ctx, work, payload, target_tag, target_value, budget, monitors)
        if steps is not None:
            logger.info(
                "walk: temporal cycle rule %d recovered %s -> %r via %s",
                learned.id,
                target_tag,
                target_value,
                payload.tag,
            )
            if ctx.debug_sink is not None:
                ctx.debug_sink.emit(
                    "temporal-rule",
                    tag=payload.tag,
                    detail=f"rule={learned.id}, target={target_tag}={target_value!r}",
                )
            return steps
    return None


def _walk_chain(
    ctx: _WalkContext,
    work: PLC,
    chain: CausalChain,
    protected: frozenset[str],
    stay_context: tuple[tuple[str, Any], ...],
    seen: set[tuple[str, int | None]],
    depth: int,
) -> list[Transition]:
    if depth > _MAX_CAUSE_DEPTH:
        return []
    key = (chain.effect.tag_name, chain.effect.scan_id)
    if key in seen:
        return []
    seen.add(key)

    roots: list[Transition] = []
    for root in chain.conjunctive_roots:
        roots.extend(_expand_transition(ctx, work, root, protected, stay_context, seen, depth + 1))
    for root in chain.ambiguous_roots:
        roots.extend(_expand_transition(ctx, work, root, protected, stay_context, seen, depth + 1))

    for step in chain.steps:
        for trigger in step.triggers:
            roots.extend(
                _expand_transition(ctx, work, trigger, protected, stay_context, seen, depth + 1)
            )

    has_actionable = any(_is_actionable_root(ctx, r.tag_name) for r in roots)
    if not has_actionable:
        for step in chain.steps:
            if step.triggers:
                continue
            for enabler in step.enablers:
                if enabler.tag_name in protected:
                    continue
                sub = _cause_at(work, enabler.tag_name, enabler.held_since_scan)
                if sub is None:
                    roots.append(_held_transition(enabler))
                    continue
                roots.extend(_walk_chain(ctx, work, sub, protected, stay_context, seen, depth + 1))

    if roots:
        _record_done_boundary(ctx, work, chain, roots, stay_context)
    return _dedup_transitions(roots)


def _expand_transition(
    ctx: _WalkContext,
    work: PLC,
    transition: Transition,
    protected: frozenset[str],
    stay_context: tuple[tuple[str, Any], ...],
    seen: set[tuple[str, int | None]],
    depth: int,
) -> list[Transition]:
    if _is_actionable_root(ctx, transition.tag_name):
        return [transition]
    sub = _cause_at(work, transition.tag_name, transition.scan_id)
    if sub is None:
        return [transition]
    return _walk_chain(ctx, work, sub, protected, stay_context, seen, depth)


def _cause_at(work: PLC, tag: str, scan: int | None) -> CausalChain | None:
    try:
        if scan is None:
            return work.cause(tag)
        return work.cause(tag, scan=scan)
    except Exception:  # noqa: BLE001 - recursive evidence is best-effort
        logger.debug("walk: recursive cause(%s, scan=%r) raised", tag, scan, exc_info=True)
        return None


def _held_transition(enabler: Any) -> Transition:
    from pyrung.core.analysis.causal.models import Transition

    return Transition(
        enabler.tag_name,
        enabler.held_since_scan if enabler.held_since_scan is not None else -1,
        enabler.value,
        enabler.value,
    )


def _record_done_boundary(
    ctx: _WalkContext,
    work: PLC,
    chain: CausalChain,
    roots: list[Transition],
    stay: tuple[tuple[str, Any], ...],
) -> None:
    # Only a rising done event (the timer expiring) bounds the steer window;
    # a falling edge is a reset.  The structural "is this a timer done bit"
    # gate is _timer_safe_scans, which returns None for any non-timer tag.
    if chain.effect.to_value is not True:
        return
    max_scans = _timer_safe_scans(ctx, work, chain.effect.tag_name)
    if max_scans is None:
        return
    for root in roots:
        if not isinstance(root.to_value, bool):
            continue
        if not _is_actionable_root(ctx, root.tag_name):
            continue
        avoid = AvoidEvent(
            event_tag=chain.effect.tag_name,
            while_tag=root.tag_name,
            while_value=root.to_value,
            stay_context=stay,
            max_scans=max_scans,
        )
        level = LevelRule(
            tag=root.tag_name,
            value=root.to_value,
            kind="cannot_hold",
            stay_context=stay,
            avoid_event=avoid,
            evidence=(
                EvidenceRef(
                    "cause",
                    chain.effect.tag_name,
                    scan=chain.effect.scan_id,
                    detail="timer Done reached with held input activity",
                ),
            ),
        )
        learned = ctx.rules.add_level(level)
        if learned is not None:
            logger.info(
                "walk: learned temporal level %s=%r cannot hold before %s",
                root.tag_name,
                root.to_value,
                chain.effect.tag_name,
            )
            if ctx.debug_sink is not None:
                ctx.debug_sink.emit(
                    "rule-learned",
                    tag=root.tag_name,
                    value=root.to_value,
                    detail=f"cannot_hold before {chain.effect.tag_name}",
                )


def _timer_safe_scans(ctx: _WalkContext, work: PLC, done_tag: str) -> int | None:
    instr = _timer_instruction_for_done(ctx, done_tag)
    if instr is None:
        return None
    preset = _resolve_preset(instr, work)
    if preset is None or preset <= 0:
        return None
    dt = float(getattr(work, "_dt", 0.010) or 0.010)
    units_per_scan = float(instr.unit.dt_to_units(dt))
    if units_per_scan <= 0:
        return None
    scans_to_done = max(1, int(math.ceil(float(preset) / units_per_scan)))
    return max(1, scans_to_done - 1)


def _timer_instruction_for_done(ctx: _WalkContext, done_tag: str) -> Any | None:
    from pyrung.core.instruction.timers import OnDelayInstruction

    for node_index in ctx.pdg.writers_of.get(done_tag, frozenset()):
        if node_index >= len(ctx.pdg.rung_nodes):
            continue
        rung = resolve_rung(ctx.program, ctx.pdg.rung_nodes[node_index])
        if rung is None:
            continue
        for instr in _iter_rung_instructions(rung):
            if getattr(getattr(instr, "done_bit", None), "name", None) == done_tag:
                if isinstance(instr, OnDelayInstruction):
                    return instr
    return None


def _iter_rung_instructions(rung: Any) -> Any:
    from pyrung.core.instruction.control import ForLoopInstruction

    for instr in rung._instructions:
        yield instr
        if isinstance(instr, ForLoopInstruction) and hasattr(instr, "instructions"):
            yield from _iter_instruction_list(instr.instructions)
    for branch in rung._branches:
        yield from _iter_rung_instructions(branch)


def _iter_instruction_list(instructions: list[Any]) -> Any:
    from pyrung.core.instruction.control import ForLoopInstruction

    for instr in instructions:
        yield instr
        if isinstance(instr, ForLoopInstruction) and hasattr(instr, "instructions"):
            yield from _iter_instruction_list(instr.instructions)


def _resolve_preset(instr: Any, work: PLC) -> int | None:
    preset = getattr(instr, "preset", None)
    name = getattr(preset, "name", None)
    if name is not None:
        value = work.state.tags.get(name, getattr(preset, "default", None))
    else:
        value = preset
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cycle_candidate(
    ctx: _WalkContext,
    work: PLC,
    rule: TemporalRule,
    target_tag: str,
    target_value: Any,
    budget: int,
    monitors: _StepMonitors,
) -> list[_Action] | None:
    by_value = {
        c.while_value: c
        for c in rule.constraints
        if c.while_tag == rule.tag and isinstance(c.while_value, bool)
    }
    if set(by_value) != {False, True}:
        return None
    trial = work.fork()
    ctx.budget.forks += 1
    actions: list[_Action] = []
    current = bool(trial.state.tags.get(rule.tag))
    order = [not current, current]

    def done() -> bool:
        return _values_match(trial.state.tags.get(target_tag), target_value) or (
            monitors.active and monitors.landed(dict(trial.state.tags))
        )

    def context_ok() -> bool:
        return not monitors.active or monitors.violation(dict(trial.state.tags)) is None

    if done():
        return []
    for i in range(_MAX_CYCLE_SEGMENTS):
        value = order[i % 2]
        constraint = by_value[value]
        max_scans = constraint.max_scans or _timer_safe_scans(ctx, trial, constraint.event_tag)
        if max_scans is None:
            return None
        if not _apply_candidate_action(ctx, trial, actions, {rule.tag: value}, 1, monitors):
            return None
        if not context_ok() or trial.state.tags.get(constraint.event_tag) is True:
            return None
        if done():
            return actions
        wait = max(0, max_scans - 1)
        if wait and not _apply_candidate_action(ctx, trial, actions, {}, wait, monitors):
            return None
        if not context_ok() or trial.state.tags.get(constraint.event_tag) is True:
            return None
        if len(actions) > budget:
            return None
        if done():
            return actions
    return None


def _apply_candidate_action(
    ctx: _WalkContext,
    trial: PLC,
    actions: list[_Action],
    action: dict[str, Any],
    scans: int,
    monitors: _StepMonitors,
) -> bool:
    if action:
        trial.patch(action)
    for _ in range(scans):
        trial.step()
        if monitors.active and monitors.violation(dict(trial.state.tags)) is not None:
            return False
    ctx.budget.scans += scans
    actions.append((action, scans))
    return True


def _protected_names(ctx: _WalkContext, monitors: _StepMonitors) -> frozenset[str]:
    names: set[str] = set()
    if ctx.holds is not None:
        names.update(ctx.holds.protected_names())
    for guard in monitors.must_stay:
        names.update(tag for tag, _value in guard.must)
        names.update(tag for tag, _value in guard.until)
    return frozenset(names)


def _stay_context_from_monitors(monitors: _StepMonitors) -> tuple[tuple[str, Any], ...]:
    return tuple(sorted({item for guard in monitors.must_stay for item in guard.must}))


def _is_actionable_root(ctx: _WalkContext, name: str) -> bool:
    return name in ctx.ext_inputs or name in ctx.edge_ext or not ctx.pdg.writers_of.get(name)


def _dedup_transitions(items: list[Transition]) -> list[Transition]:
    out: list[Transition] = []
    seen: set[tuple[str, int, Any, Any]] = set()
    for item in items:
        key = (item.tag_name, item.scan_id, item.from_value, item.to_value)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
