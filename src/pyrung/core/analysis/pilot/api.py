"""Public entry points and target parsing for PILOT drives.

The drive engine owns execution and verification. This module translates the
requested conditions into that engine's inputs and assembles public plans.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import pyrung.core.analysis.pilot.pilot as _engine
import pyrung.core.analysis.pilot.target_route as _target_route
from pyrung.core.analysis.graph import Plan, PlanStatus, RouteTaken
from pyrung.core.analysis.pilot.trace import target_reached
from pyrung.core.analysis.pilot.types import PilotEvent, _ActionPair

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
    from pyrung.core.tag import Tag

    forms = {CompareLt: "lt", CompareLe: "le", CompareGt: "gt", CompareGe: "ge"}
    form = forms.get(type(cond))
    if form is None:
        return None
    tag = cond.tag
    tag_name = tag.name if isinstance(tag, Tag) else str(tag)
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
    from pyrung.core.condition import CompareEq
    from pyrung.core.tag import Tag

    if isinstance(cond, Tag):
        return cond.name, True, None

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

    raise ValueError(
        f"pilot: cannot extract a target from {cond!r}.  Pass a Tag (Bool target), "
        "tag == value, or a relational comparison (tag < / <= / > / >= value)."
    )


def _parse_targets(*conditions: Any) -> list[tuple[str, Any, Any]]:
    """Extract one ``(tag, value, predicate)`` per condition (multi-target goals)."""
    if not conditions:
        raise ValueError("pilot: how() requires at least one target condition")
    return [_parse_one(c) for c in conditions]


def _parse_target(*conditions: Any) -> tuple[str, Any, Any]:
    """Single-target parse — for the diagnostic/live entry points."""
    if len(conditions) != 1:
        raise ValueError("pilot currently supports exactly one target condition")
    return _parse_one(conditions[0])


def _single_target_plan(
    setup: _engine._DriveSetup,
    outcome: _engine._DriveOutcome,
    target_tag: str,
    target_value: Any,
    route_taken: RouteTaken | None,
    *,
    include_journal: bool,
) -> Plan:
    """Assemble the common fork/live single-target result without policy drift."""

    linked_block = (
        None
        if outcome.reached
        else _target_route.linked_feedback_block(
            target_tag,
            target_value,
            setup.diag_snapshot,
            setup.pdg,
            setup.program,
            setup.steerable,
            _target_route.harness_couplings(setup.work),
        )
    )
    return Plan(
        reachable=outcome.reached,
        target_tag=target_tag,
        target_value=target_value,
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
        journal=outcome.journal if include_journal else (),
        anchor_scan=setup.anchor_scan,
        journey=outcome.journey,
        hold_log=outcome.knowledge.get("hold_log", ()),
        lever_notes=outcome.knowledge.get("lever_notes", {}),
        avoid_names=outcome.knowledge.get("avoid_names", ()),
    )


def pilot_events(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    unlink: list[str] | None = None,
) -> Iterator[PilotEvent]:
    """PILOT on a fork, yielding structured diagnostic events.

    ``unlink`` frees the named harness-feedback tags for fault injection (see
    :func:`pilot_how`). ``avoid_pred`` excludes routes, actions, and observed
    states the same way ``how(avoid=...)`` does.
    """
    target_tag, target_value, target_predicate = _parse_target(*conditions)
    setup = _engine._prepare_drive(plc, unlink=unlink)
    ctx, _route_taken = _engine._prepare_target_context(
        setup,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
    )
    yield from _engine._pilot_loop_events(setup.work, ctx)


def pilot_how(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    unlink: list[str] | None = None,
    on_event: Callable[[PilotEvent], None] | None = None,
) -> Plan:
    """PILOT on a fork — drive to the target and return the recording. Nothing changes.

    For a multi-route value target (``Bool == True/False`` or word
    ``tag == value``) PILOT starts with a deterministic preferred route and
    records the route that actually reached the goal on ``Plan.route``;
    ``avoid_pred`` excludes a reported route so PILOT can take another.

    ``unlink`` names harness-synthesized feedback tags to free for fault
    injection: the Harness stops driving them and they become steerable, so
    PILOT can reach faults that the intact physical link would otherwise hold
    out of reach (e.g. a dead flow sensor with the valve open).
    """
    targets = _parse_targets(*conditions)
    if len(targets) > 1:
        return _pilot_how_multi(
            plc,
            targets,
            max_scans=max_scans,
            avoid_pred=avoid_pred,
            unlink=unlink,
            on_event=on_event,
        )
    target_tag, target_value, target_predicate = targets[0]
    setup = _engine._prepare_drive(plc, unlink=unlink)
    ctx, route_taken = _engine._prepare_target_context(
        setup,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
    )
    outcome = _engine._pilot_loop(
        setup.work,
        ctx,
        on_event=on_event,
    )

    return _single_target_plan(
        setup,
        outcome,
        target_tag,
        target_value,
        route_taken,
        include_journal=True,
    )


def _failed_multi_plan(
    label: str,
    targets: tuple[_ActionPair, ...],
    reason: str | None,
    status: PlanStatus,
    anchor_scan: int,
) -> Plan:
    """Build the one unreachable multi-target result shape."""

    return Plan(
        reachable=False,
        target_tag=label,
        target_value=True,
        targets=targets,
        reason=reason,
        status=status,
        anchor_scan=anchor_scan,
    )


def _pilot_how_multi(
    plc: PLC,
    targets: list[tuple[str, Any, Any]],
    *,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    unlink: list[str] | None = None,
    on_event: Callable[[PilotEvent], None] | None = None,
) -> Plan:
    """Multi-target ``how(A, B, …)`` — reach one committed scan where every target holds.

    Static read only (``pilot/multitarget.py``): a sound mutual-exclusion prune +
    a clobberer-first order, then the single-target drive loop is run
    sequentially per target on ONE fork.  The fork's recording is the artifact —
    it replays to a state with every target true.  When the static read cannot
    prove ME it falls open to this drive; the final all-targets check is the
    honest oracle (the drive loop is execution truth, never a skiff probe).
    """
    from pyrung.core.analysis.pilot import multitarget as _mt  # noqa: PLC0415

    label = " & ".join(f"{tt}={tv!r}" for tt, tv, _ in targets)
    setup = _engine._prepare_drive(plc, unlink=unlink)

    goal_pairs = tuple((tt, tv) for tt, tv, _ in targets)

    ok, reason, ordered = _mt.analyze(
        setup.diag_snapshot,
        setup.pdg,
        setup.program,
        setup.steerable,
        targets,
    )
    if not ok:
        return _failed_multi_plan(
            label,
            goal_pairs,
            reason,
            PlanStatus.CANNOT_REACH,
            setup.anchor_scan,
        )

    work = setup.work
    compass = setup.compass
    last_knowledge: dict[str, Any] = {}
    last_journey: tuple[Any, ...] = ()
    # The per-target drives run sequentially on ONE fork, so their journals are already
    # in scan order — concatenating them gives the whole passage, not the last leg only.
    journal_steps: list[Any] = []
    for t_tag, t_val, t_pred in ordered:
        if target_reached(dict(work.state.tags), t_tag, t_val, t_pred):
            continue  # already pulled in by an earlier target's drive
        # Same route discipline as single-target how(): infer every admissible
        # current-world route and let Orientation choose among them. ``avoid=``
        # is not tied to any one target, so it constrains every target uniformly.
        ctx, _route_taken = _engine._prepare_target_context(
            setup,
            t_tag,
            t_val,
            t_pred,
            compass=compass,
            max_scans=max_scans,
            avoid_pred=avoid_pred,
            work=work,
        )
        outcome = _engine._pilot_loop(work, ctx, on_event=on_event)
        work = outcome.work
        last_knowledge = outcome.knowledge
        compass = outcome.knowledge.get("compass", compass)
        last_journey = outcome.journey
        journal_steps.extend(outcome.journal)
        if not outcome.reached:
            detail = f"; {outcome.reason}" if outcome.reason else ""
            return _failed_multi_plan(
                label,
                goal_pairs,
                (
                    f"pilot: could not establish {t_tag}={t_val!r} while holding the "
                    f"other target(s){detail}"
                ),
                PlanStatus.STOPPED,
                setup.anchor_scan,
            )

    final = dict(work.state.tags)
    unmet = [(tt, tv) for tt, tv, tp in targets if not target_reached(final, tt, tv, tp)]
    if unmet:
        names = ", ".join(f"{tt}={tv!r}" for tt, tv in unmet)
        return _failed_multi_plan(
            label,
            goal_pairs,
            f"pilot: reached each target individually but {names} did not hold "
            "simultaneously (clobbered during co-establishment).",
            PlanStatus.STOPPED,
            setup.anchor_scan,
        )
    # recording: threaded from the LAST target's drive only (multi runs the loop
    # sequentially per target; the last drive's Knowledge is what survives on ``work``).
    return Plan(
        reachable=True,
        target_tag=label,
        target_value=True,
        targets=goal_pairs,
        fork=work,
        anchor_scan=setup.anchor_scan,
        journal=tuple(journal_steps),
        journey=last_journey,
        hold_log=last_knowledge.get("hold_log", ()),
        lever_notes=last_knowledge.get("lever_notes", {}),
        avoid_names=last_knowledge.get("avoid_names", ()),
    )
