"""Public entry points and target parsing for PILOT drives.

The drive engine owns execution and verification. This module translates the
requested conditions into that engine's inputs and assembles public plans.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pyrung.core.analysis.pilot.budget as _budget
import pyrung.core.analysis.pilot.drive_setup as _drive_setup
import pyrung.core.analysis.pilot.last_resort as _last_resort
import pyrung.core.analysis.pilot.pilot as _engine
import pyrung.core.analysis.pilot.restart as _restart
import pyrung.core.analysis.pilot.static_proofs as _static_proofs
import pyrung.core.analysis.pilot.target_route as _target_route
import pyrung.core.analysis.pilot.trim as _trim
from pyrung.core.analysis.graph import Plan, PlanStatus, RouteTaken
from pyrung.core.analysis.pilot.avoid import _AvoidCheckRaised
from pyrung.core.analysis.pilot.navigation_contracts import TargetSpec
from pyrung.core.analysis.pilot.types import PilotEvent
from pyrung.core.runner import AvoidCheckError

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _relational_target_atom(cond: Any) -> Any | None:
    """Build a simplified inequality ``Atom`` from a ``Compare*`` target, or None.

    Maps the ordered comparisons (``<``, ``<=``, ``>``, ``>=``) to their atom
    forms so a relational ``how(A > B)`` target rides the same trace machinery as
    a relational prerequisite (live predicate + reactive levers + coast).  The
    operand is the RHS tag name (a *live* threshold) or a literal.
    """
    from pyrung.core.analysis.simplified import Atom
    from pyrung.core.condition import CompareGe, CompareGt, CompareLe, CompareLt
    from pyrung.core.expression import Expression
    from pyrung.core.tag import Tag

    forms = {CompareLt: "lt", CompareLe: "le", CompareGt: "gt", CompareGe: "ge"}
    form = forms.get(type(cond))
    if form is None:
        return None
    tag = cond.tag
    tag_name = tag.name if isinstance(tag, Tag) else str(tag)
    if isinstance(cond.value, Expression):
        raise ValueError(
            f"pilot: how() target {tag_name} is compared with arithmetic; compare it with "
            "a tag or a literal."
        )
    operand = cond.value.name if isinstance(cond.value, Tag) else cond.value
    return Atom(
        tag=tag_name,
        form=form,
        operand=operand,
        operand_is_tag=isinstance(cond.value, Tag),
    )


def _parse_one(cond: Any) -> tuple[str, Any, Any]:
    """Extract ``(tag_name, target_value, predicate)`` from ONE condition.

    Accepts:
    - A Tag object (implies ``tag == True``)
    - A ``tag == value`` comparison (CompareEq)
    - A relational comparison ``A < / <= / > / >= B`` — returned as a live
      ``predicate`` Atom (the goal is the relation, not a frozen value); the
      ``(tag, value)`` pair is a representative for display/keying only.
    """
    from pyrung.core.condition import (
        AnyCondition,
        BitCondition,
        CompareEq,
        NormallyClosedCondition,
    )
    from pyrung.core.tag import Tag

    if isinstance(cond, Tag):
        return cond.name, True, None

    # And(Tag, ...) holds its members as contacts.
    if isinstance(cond, BitCondition) and isinstance(cond.tag, Tag):
        return cond.tag.name, True, None
    if isinstance(cond, NormallyClosedCondition) and isinstance(cond.tag, Tag):
        return cond.tag.name, False, None

    if isinstance(cond, CompareEq):
        tag = cond.tag
        tag_name = tag.name if isinstance(tag, Tag) else str(tag)
        value = cond.value
        if isinstance(value, Tag):
            # The RHS is a Tag, not a concrete value — it would ride through the
            # trace as a TagExpr and crash the (unhashable) crossings machinery.
            # Require an explicit scalar so the target is a frozen value; for a
            # readonly constant (a named-array/enum element) point at its literal.
            hint = f" (e.g. {value.name}.default = {value.default!r})" if value.readonly else ""
            raise ValueError(
                f"pilot: how() target {tag_name} == {value.name!r} compares against a "
                f"Tag, not a concrete value. Pass the value it stands for{hint} or a "
                f"literal so the target is a frozen scalar."
            )
        return tag_name, value, None

    atom = _relational_target_atom(cond)
    if atom is not None:
        return atom.tag, atom.operand, atom

    if isinstance(cond, AnyCondition):
        raise ValueError(
            f"pilot: how() has no either-or target: {cond!r}.  Ask for one arm at a time."
        )

    raise ValueError(
        f"pilot: cannot extract a target from {cond!r}.  Pass a Tag (Bool target), "
        "tag == value, or a relational comparison (tag < / <= / > / >= value)."
    )


def _parse_targets(*conditions: Any) -> list[tuple[str, Any, Any]]:
    """Extract one ``(tag, value, predicate)`` per condition (multi-target goals)."""
    if not conditions:
        raise ValueError("pilot: how() requires at least one target condition")
    return [_parse_one(member) for condition in conditions for member in _members(condition)]


def _members(condition: Any) -> Iterator[Any]:
    from pyrung.core.condition import AllCondition

    if isinstance(condition, AllCondition):
        for member in condition.conditions:
            yield from _members(member)
    else:
        yield condition


def _parse_target(*conditions: Any) -> tuple[str, Any, Any]:
    """Single-target parse — for the diagnostic/live entry points."""
    if len(conditions) != 1:
        raise ValueError("pilot currently supports exactly one target condition")
    return _parse_one(conditions[0])


def _check_operands(targets: list[tuple[str, Any, Any]], setup: _drive_setup.DriveSetup) -> None:
    """A relational operand has to have a value in the search, or the goal has no meaning."""
    for tag, _value, predicate in targets:
        if (
            predicate is not None
            and predicate.operand_is_tag
            and predicate.operand not in setup.pdg.tags
            and predicate.operand not in setup.diag_snapshot
        ):
            raise ValueError(
                f"pilot: how() target {tag} is compared with {predicate.operand}, and no rung "
                "uses that tag, so a search has no value for it. Compare with a literal."
            )


def _carry_goal_tags(setup: _drive_setup.DriveSetup, conditions: tuple[Any, ...]) -> None:
    """Name each tag of the goal to the search: a pointer or a range writes a slot unnamed."""
    from pyrung.core.tag import Tag

    known = setup.work._known_tags_by_name
    for condition in conditions:
        for member in _members(condition):
            tag = member if isinstance(member, Tag) else getattr(member, "tag", None)
            if isinstance(tag, Tag) and tag.name not in known:
                setup.work._register_tag_no_rung_gave(tag)


def _plan_from_outcome(
    setup: _drive_setup.DriveSetup,
    outcome: _engine._DriveOutcome,
    target: TargetSpec,
    route_taken: RouteTaken | None,
) -> Plan:
    """Assemble one complete drive, including every joint goal's diagnostics."""
    target_tag, target_value = target.tag, target.value
    targets = tuple((member.tag, member.value) for member in target.members)

    # A search that did not finish found no block.  The link may still be on its way to one.
    linked_block = (
        None
        if outcome.reached or outcome.cut_short or target.predicate is not None
        else _static_proofs.linked_feedback_block(
            target_tag, target_value, _static_proofs.facts_for(setup)
        )
    )
    return Plan(
        reachable=outcome.reached,
        target_tag=target_tag,
        target_value=target_value,
        targets=targets,
        target_predicate=target.predicate,
        fork=outcome.work if outcome.reached else None,
        reason=linked_block or outcome.reason,
        status=(
            PlanStatus.REACHED
            if outcome.reached
            else PlanStatus.CANNOT_REACH
            if linked_block is not None
            else PlanStatus.STOPPED
        ),
        route=(
            _target_route.report_selected_route(route_taken, outcome.root_route)
            if outcome.reached
            else None
        ),
        journal=outcome.journal,
        anchor_scan=setup.anchor_scan,
        journey=outcome.journey,
        hold_log=outcome.knowledge.get("hold_log", ()),
        lever_notes=outcome.knowledge.get("lever_notes", {}),
        avoid_names=outcome.knowledge.get("avoid_names", ()),
        session_inputs=dict(setup.session_inputs),
        waiting_overrides=setup.waiting_overrides,
    )


@contextmanager
def _avoid_errors_named() -> Iterator[None]:
    """A check that raised in the search reaches the caller under its public name."""
    try:
        yield
    except _AvoidCheckRaised as raised:
        raise AvoidCheckError(raised.check, raised.error) from raised.error


def pilot_events(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    unlink: list[str] | None = None,
    wall_clock_s: float | None = _budget.DEFAULT_WALL_CLOCK_S,
) -> Iterator[PilotEvent]:
    """PILOT on a fork, yielding structured diagnostic events.

    ``unlink`` frees the named harness-feedback tags for fault injection (see
    :func:`pilot_how`). ``avoid_pred`` excludes routes, actions, and observed
    states the same way ``how(avoid=...)`` does, and one that raises on a real
    state ends the stream with ``AvoidCheckError``.
    """
    clock = _budget.WallClock.starting_now(wall_clock_s)
    target_tag, target_value, target_predicate = _parse_target(*conditions)
    setup = _drive_setup.prepare_drive(plc, unlink=unlink)
    _check_operands([(target_tag, target_value, target_predicate)], setup)
    _carry_goal_tags(setup, conditions)
    with _avoid_errors_named():
        ctx, _route_taken = _drive_setup.prepare_target_context(
            setup,
            target_tag,
            target_value,
            target_predicate,
            max_scans=max_scans,
            avoid_pred=avoid_pred,
        )
        yield from _engine._pilot_loop_events(setup.work, replace(ctx, wall_clock=clock))


@_avoid_errors_named()
def pilot_how(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    unlink: list[str] | None = None,
    on_event: Callable[[PilotEvent], None] | None = None,
    wall_clock_s: float | None = _budget.DEFAULT_WALL_CLOCK_S,
) -> Plan:
    """PILOT on a fork — drive to the target and return the recording. Nothing changes.

    For a multi-route value target (``Bool == True/False`` or word
    ``tag == value``) PILOT starts with a deterministic preferred route and
    records the route that actually reached the goal on ``Plan.route``;
    ``avoid_pred`` excludes a reported route so PILOT can take another. One that
    raises on a real state ends the search with ``AvoidCheckError``.

    ``unlink`` names harness-synthesized feedback tags to free for fault
    injection: the Harness stops driving them and they become steerable, so
    PILOT can reach faults that the intact physical link would otherwise hold
    out of reach (e.g. a dead flow sensor with the valve open).

    ``max_scans`` bounds invocation search work, including discarded attempts,
    probe scans, and bounded research requests; restoring a checkpoint never
    refunds it. Each scan the search steps counts one, and so do the first two
    scans and the last scan of each wait. The other scans of a wait count a
    hundredth each, whether the guarded loop ran them or not, so a long timer fits
    a small budget. A wait pyrung cannot run compiled counts one for every scan.
    A Modbus ``send()`` or ``receive()`` makes one, and so do a write to a
    read-only system tag and a ``run_function()`` that keeps state between calls.
    A forced tag makes one too. Research tools also keep their local caps.

    ``wall_clock_s`` is a backstop on real time, since most of a search's time
    is work the ledger does not charge. A how() that runs out of it stops and
    says so. ``None`` turns it off.

    When ordinary steering lacks evidence, a bounded survey tries single and
    paired input assignments for one scan from the same current source. Only
    confirmed observations return to a fresh Compass read.
    """
    clock = _budget.WallClock.starting_now(wall_clock_s)
    plan, setup, outcome = _how_from(
        plc, conditions, max_scans, avoid_pred, unlink, on_event, wall_clock_s
    )
    if plan.status is PlanStatus.REACHED:
        return _trim.trim_holds(
            plan, _goal_check(conditions), avoid_pred, frozenset(setup.steerable), clock
        )
    if plan.status is not PlanStatus.STOPPED or outcome is None:
        return plan
    # A search that ran out of time or work did not find there was nothing left to do.  Under a
    # force it may only have run out: the guarded loop declines those, so its waits cost full
    # scans.  The try without them has a budget of its own, and settles what comes next.
    if outcome.cut_short and not plc._forces:
        return plan

    def asker(*, start_had_a_scan: bool) -> Callable[[PLC, float], tuple[Plan, bool]]:
        def ask(fork: PLC, wall_s: float) -> tuple[Plan, bool]:
            answer, _setup, asked = _how_from(
                fork,
                conditions,
                max_scans,
                avoid_pred,
                unlink,
                None,
                wall_s if clock is None else min(wall_s, clock.left()),
                start_had_a_scan=start_had_a_scan,
            )
            return answer, asked is not None and asked.cut_short

        return ask

    if not outcome.cut_short:
        moved = _last_resort.try_moves(
            plan,
            setup,
            _goal_check(conditions),
            avoid_pred,
            asker(start_had_a_scan=True),
            max_scans,
        )
        if moved is not None:
            return moved
    released, finished = _restart.try_release(plan, plc, asker(start_had_a_scan=True))
    if released is not None:
        return released
    if outcome.cut_short:
        if not finished:
            return plan
        # Forces are free tags to the proof, so what it proves holds with them on.
        blocked = (
            None
            if plan.target_predicate is not None
            else _static_proofs.linked_feedback_block(
                plan.target_tag, plan.target_value, _static_proofs.facts_for(setup)
            )
        )
        if blocked is not None:
            return replace(plan, reason=blocked, status=PlanStatus.CANNOT_REACH)
    return _restart.try_restart(plan, plc, setup, avoid_pred, asker(start_had_a_scan=False))


def _goal_check(conditions: tuple[Any, ...]) -> Callable[[Any], bool] | None:
    """The goal as a check on a state's tags, or None if a condition does not compile to one."""
    from pyrung.core.analysis.prove import _compile_property

    try:
        checks = [_compile_property(condition)[0] for condition in conditions]
    except Exception:
        return None
    return lambda tags: all(check(tags) for check in checks)


def _how_from(
    plc: PLC,
    conditions: tuple[Any, ...],
    max_scans: int,
    avoid_pred: Any,
    unlink: list[str] | None,
    on_event: Callable[[PilotEvent], None] | None,
    wall_clock_s: float | None,
    *,
    start_had_a_scan: bool = True,
) -> tuple[Plan, _drive_setup.DriveSetup, _engine._DriveOutcome | None]:
    """One how() from where *plc* stands, with what the caller needs to decide what comes next."""
    clock = _budget.WallClock.starting_now(wall_clock_s)
    targets = _parse_targets(*conditions)
    setup = _drive_setup.prepare_drive(plc, unlink=unlink)
    _check_operands(targets, setup)
    _carry_goal_tags(setup, conditions)
    goal = TargetSpec.conjunction(tuple(TargetSpec(*target) for target in targets))
    goal_pairs = tuple((tt, tv) for tt, tv, _ in targets) if len(targets) > 1 else ()
    if goal.members:
        from pyrung.core.analysis.pilot import multitarget

        ok, reason = multitarget.analyze(_static_proofs.facts_for(setup), targets)
        if not ok:
            refused = Plan(
                reachable=False,
                target_tag=goal.tag,
                target_value=goal.value,
                targets=goal_pairs,
                target_predicate=goal.predicate,
                reason=reason,
                status=PlanStatus.CANNOT_REACH,
                anchor_scan=setup.anchor_scan,
            )
            return refused, setup, None
    target_tag, target_value, target_predicate = goal.tag, goal.value, goal.predicate
    ctx, route_taken = _drive_setup.prepare_target_context(
        setup,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
        start_had_a_scan=start_had_a_scan,
    )
    ctx = replace(ctx, target=goal, wall_clock=clock)
    outcome = _engine._pilot_loop(
        setup.work,
        ctx,
        on_event=on_event,
    )

    return _plan_from_outcome(setup, outcome, goal, route_taken), setup, outcome
