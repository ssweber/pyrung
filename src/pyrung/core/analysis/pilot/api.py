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
import pyrung.core.analysis.pilot.pilot as _engine
import pyrung.core.analysis.pilot.static_proofs as _static_proofs
import pyrung.core.analysis.pilot.target_route as _target_route
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
    from pyrung.core.condition import AllCondition

    def flat(condition: Any) -> Iterator[Any]:
        if isinstance(condition, AllCondition):
            for member in condition.conditions:
                yield from flat(member)
        else:
            yield condition

    return [_parse_one(member) for condition in conditions for member in flat(condition)]


def _parse_target(*conditions: Any) -> tuple[str, Any, Any]:
    """Single-target parse — for the diagnostic/live entry points."""
    if len(conditions) != 1:
        raise ValueError("pilot currently supports exactly one target condition")
    return _parse_one(conditions[0])


def _freeze_unused_operands(
    targets: list[tuple[str, Any, Any]], setup: _drive_setup.DriveSetup
) -> list[tuple[str, Any, Any]]:
    """A relational operand no rung uses has one value through a search: compare with that.

    The search only carries tags the program uses, so it has no live value to read.
    """
    frozen = []
    for tag, value, predicate in targets:
        if (
            predicate is not None
            and predicate.operand_is_tag
            and predicate.operand not in setup.pdg.tags
        ):
            known = setup.work._known_tags_by_name.get(predicate.operand)
            default = known.default if known is not None else 0
            literal = setup.diag_snapshot.get(predicate.operand, default)
            predicate = replace(predicate, operand=literal, operand_is_tag=False)
            value = literal
        frozen.append((tag, value, predicate))
    return frozen


def _goal_tags_it_cannot_read(
    targets: list[tuple[str, Any, Any]], setup: _drive_setup.DriveSetup
) -> list[str]:
    """Tags a relational or joint goal names and the search does not carry.

    Such a goal is lowered to a condition on program tags, which it cannot be. A single
    ``tag == value`` is judged by value, and stops by itself when nothing moves it.
    """
    if len(targets) == 1 and targets[0][2] is None:
        return []
    known = setup.work._known_tags_by_name
    return [tag for tag, _value, _predicate in targets if tag not in known]


def _plan_from_outcome(
    setup: _drive_setup.DriveSetup,
    outcome: _engine._DriveOutcome,
    target: TargetSpec,
    route_taken: RouteTaken | None,
) -> Plan:
    """Assemble one complete drive, including every joint goal's diagnostics."""
    target_tag, target_value = target.tag, target.value
    targets = tuple((member.tag, member.value) for member in target.members)

    # The proof reads the program, not the search, so a search cut short does not weaken it.
    linked_block = (
        None
        if outcome.reached or target.predicate is not None
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
    target = _parse_target(*conditions)
    setup = _drive_setup.prepare_drive(plc, unlink=unlink)
    target_tag, target_value, target_predicate = _freeze_unused_operands([target], setup)[0]
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
    refunds it. Work is in interpreted scans: a scan that ran in the guarded
    loop costs a hundredth of one, charged when its seek returns, so a seek in
    the loop is not stopped partway and may reach a long timer on a small budget.
    Research tools also keep their local caps.

    ``wall_clock_s`` is a backstop on real time, since most of a search's time
    is work the ledger does not charge. A how() that runs out of it stops and
    says so. ``None`` turns it off.

    When ordinary steering lacks evidence, a bounded survey tries single and
    paired input assignments for one scan from the same current source. Only
    confirmed observations return to a fresh Compass read.
    """
    clock = _budget.WallClock.starting_now(wall_clock_s)
    targets = _parse_targets(*conditions)
    setup = _drive_setup.prepare_drive(plc, unlink=unlink)
    targets = _freeze_unused_operands(targets, setup)
    goal = TargetSpec.conjunction(tuple(TargetSpec(*target) for target in targets))
    goal_pairs = tuple((tt, tv) for tt, tv, _ in targets) if len(targets) > 1 else ()
    unread = _goal_tags_it_cannot_read(targets, setup)
    if unread:
        return Plan(
            reachable=False,
            target_tag=goal.tag,
            target_value=goal.value,
            targets=goal_pairs,
            target_predicate=goal.predicate,
            reason=f"no rung uses {', '.join(unread)}, so how() has nothing to steer it with",
            status=PlanStatus.STOPPED,
            anchor_scan=setup.anchor_scan,
        )
    if goal.members:
        from pyrung.core.analysis.pilot import multitarget

        ok, reason = multitarget.analyze(_static_proofs.facts_for(setup), targets)
        if not ok:
            return Plan(
                reachable=False,
                target_tag=goal.tag,
                target_value=goal.value,
                targets=goal_pairs,
                target_predicate=goal.predicate,
                reason=reason,
                status=PlanStatus.CANNOT_REACH,
                anchor_scan=setup.anchor_scan,
            )
    target_tag, target_value, target_predicate = goal.tag, goal.value, goal.predicate
    ctx, route_taken = _drive_setup.prepare_target_context(
        setup,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
    )
    ctx = replace(ctx, target=goal, wall_clock=clock)
    outcome = _engine._pilot_loop(
        setup.work,
        ctx,
        on_event=on_event,
    )

    return _plan_from_outcome(setup, outcome, goal, route_taken)
