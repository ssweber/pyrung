"""Admit trace evidence and separate executable prerequisites.

This module applies current-world action policy to immutable trace/wait reads,
forms exact multi-input operation batches, and lowers durable prerequisite
overlays. It does not choose among candidate sources or execute a Bearing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pyrung.core.analysis.pilot.candidate_read as _candidate_read
from pyrung.core.analysis.pilot.availability import _WriterAvailability
from pyrung.core.analysis.pilot.avoid import _avoid_forces
from pyrung.core.analysis.pilot.candidate_policy import (
    _action_allowed,
    hold_defeats_needed,
)
from pyrung.core.analysis.pilot.effects import expectation_from_selected_path
from pyrung.core.analysis.pilot.navigation_contracts import (
    ChannelHeading,
    CrossingFidelity,
    _ActionPair,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _until_unresolved_condition,
)
from pyrung.core.analysis.pilot.route_options import (
    _managed_boolean_rungs,
    _oscillating_rungs,
)
from pyrung.core.analysis.pilot.trace_tree import frontier_pairs
from pyrung.core.analysis.pilot.wait_options import _boundary_heading
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.trace_tree import TraceAction


def _effect_operation_batches(
    details: Sequence[TraceAction],
    snapshot: Mapping[str, Any],
    pdg: Any,
    program: Any,
    steerable: frozenset[str],
) -> tuple[_candidate_read.CrossingBatchRead, ...]:
    """Compose inputs which cover one exact writer's local conjunction.

    Target-wide trace actions are not an executable batch.  They become one
    only when their exact effect paths converge on the same selected writer
    and, collectively, cover every currently-unsatisfied local requirement of
    that writer.  Each action must own the immediate operation boundary it
    covers; a deeper leaf which merely passes through the requirement cannot
    hitchhike.  The resulting promise is the common writer itself.
    """

    operations: dict[tuple[int, str, str], dict[str, Any]] = {}
    for detail in details:
        for index, step in enumerate(detail.effect_path[:-1]):
            requirements = tuple(dict.fromkeys(step.local_requirements))
            if len(requirements) < 2:
                continue
            key = (step.node_index, step.tag, repr(step.value))
            operation = operations.setdefault(
                key,
                {
                    "step": step,
                    "path": detail.effect_path[: index + 1],
                    "by_requirement": {},
                },
            )
            if len(detail.effect_path[: index + 1]) < len(operation["path"]):
                operation["path"] = detail.effect_path[: index + 1]
            child = detail.effect_path[index + 1]
            for requirement in requirements:
                if (
                    detail.operation_boundary is not None
                    and detail.operation_boundary[0] == requirement[0]
                    and _values_match(detail.operation_boundary[1], requirement[1])
                    and child.tag == requirement[0]
                    and _values_match(child.value, requirement[1])
                ):
                    operation["by_requirement"].setdefault(requirement, []).append(detail.pair)

    reads: list[_candidate_read.CrossingBatchRead] = []
    seen: set[tuple[_ActionPair, ...]] = set()
    for operation in operations.values():
        step = operation["step"]
        pending = tuple(
            requirement
            for requirement in dict.fromkeys(step.local_requirements)
            if not _values_match(snapshot.get(requirement[0]), requirement[1])
        )
        if len(pending) < 2 or any(
            requirement not in operation["by_requirement"] for requirement in pending
        ):
            continue
        choices = tuple(
            tuple(dict.fromkeys(operation["by_requirement"][requirement]))
            for requirement in pending
        )
        # More than one action for a requirement is an OR, not permission to
        # enumerate speculative mixtures.  Leave that ambiguity to ordinary
        # orientation; a local operation batch is exact only when every open
        # input has one uniquely selected action receipt.
        if any(len(choice) != 1 for choice in choices):
            continue
        for selected in (tuple(choice[0] for choice in choices),):
            actions = tuple(dict.fromkeys(selected))
            if len(actions) < 2 or len({tag for tag, _value in actions}) != len(actions):
                continue
            if actions in seen:
                continue
            expectation = expectation_from_selected_path(
                operation["path"],
                pdg,
                program,
                boundary=None,
                selected_pairs=actions,
                snapshot=snapshot,
                steerable=steerable,
                require_ready=False,
            )
            if expectation is None:
                continue
            seen.add(actions)
            reads.append(
                _candidate_read.CrossingBatchRead(
                    actions=actions,
                    fidelity=CrossingFidelity(
                        constraints=(),
                        reason=f"exact local operation for {step.tag}={step.value!r}",
                        verify_required=True,
                        exact=True,
                        proposed=False,
                    ),
                    expectation=expectation,
                )
            )
    return tuple(reads)


def _admit_trace_details(
    details: tuple[TraceAction, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> _candidate_read._TraceAdmission:
    """Apply one admission policy to every current-world trace reading.

    The broad target trace and supplemental completion/program reads differ in
    provenance, not privilege.  Duplicate details preserve the target trace's
    evidence while composing an owned lifetime discovered by the narrower
    read.  Nothing enters candidate ranking by being appended after this pass.
    """

    detail_by_pair: dict[_ActionPair, TraceAction] = {}
    ordered_details: list[TraceAction] = []
    for detail in details:
        pair = detail.pair
        matching_index = next(
            (
                index
                for index, existing in enumerate(ordered_details)
                if existing.pair == pair and existing.effect_path == detail.effect_path
            ),
            None,
        )
        if matching_index is None:
            ordered_details.append(detail)
        else:
            existing = ordered_details[matching_index]
            preferred = detail if detail.availability < existing.availability else existing
            lifetime_owner = next(
                (candidate for candidate in (existing, detail) if candidate.until is not None),
                None,
            )
            detail = replace(
                preferred,
                until=(lifetime_owner.until if lifetime_owner is not None else preferred.until),
                operation=(
                    lifetime_owner.operation
                    if lifetime_owner is not None and lifetime_owner.operation is not None
                    else preferred.operation
                ),
            )
            ordered_details[matching_index] = detail

        operational = detail_by_pair.get(pair)
        if operational is None:
            detail_by_pair[pair] = detail
            continue
        # The broad target trace and an exact ProgramStep read can describe
        # different effect paths for the same physical input. Compose only the
        # orthogonal execution facts: the narrower reader may prove present
        # availability while the outer route owns the honest lifetime. Effect
        # paths/provenance remain separate in ``ordered_details``.
        preferred = detail if detail.availability < operational.availability else operational
        lifetime_owner = next(
            (candidate for candidate in (operational, detail) if candidate.until is not None),
            None,
        )
        detail_by_pair[pair] = replace(
            preferred,
            until=(lifetime_owner.until if lifetime_owner is not None else preferred.until),
            operation=(
                lifetime_owner.operation
                if lifetime_owner is not None and lifetime_owner.operation is not None
                else preferred.operation
            ),
        )

    active_details = tuple(
        detail
        for detail in ordered_details
        for pair in (detail.pair,)
        if _action_allowed(ctx, pair)
        and (
            not _values_match(frame.snap.get(pair[0]), pair[1])
            or pair[0] in ctx.edge_tags
            or detail.pulse
            or detail.until is not None
        )
    )
    spent_edges = frozenset(
        tag
        for tag in getattr(ctx, "edge_tags", ())
        if not _values_match(
            frame.snap.get(tag),
            getattr(ctx, "resting", {}).get(tag, False),
        )
        and any(
            detail.tag == tag
            and _values_match(
                detail.value,
                getattr(ctx, "resting", {}).get(tag, False),
            )
            for detail in active_details
        )
    )
    if spent_edges:
        # A selected trace can contain both the next assertion and its release
        # edge.  When the input is still asserted, the release is the current
        # operation: asserting again would hide that scan inside _apply_pulse
        # and verify a later route promise against pre-release state.  Preserve
        # trace order otherwise; fresh orientation will reread after the
        # ordinary release bearing lands.
        active_details = tuple(
            sorted(
                active_details,
                key=lambda detail: (
                    0
                    if detail.tag in spent_edges
                    and _values_match(
                        detail.value,
                        getattr(ctx, "resting", {}).get(detail.tag, False),
                    )
                    else 1
                ),
            )
        )
    trace_action_details = tuple(
        detail for detail in active_details if detail.pair not in key_nogoods
    )
    trace_actions = tuple(dict.fromkeys(detail.pair for detail in trace_action_details))
    active_trace_actions = tuple(dict.fromkeys(detail.pair for detail in active_details))

    managed_boolean_rungs, lowered_rung_pairs = _managed_boolean_rungs(
        trace_action_details, frame, state, ctx
    )
    if lowered_rung_pairs:
        trace_actions = tuple(pair for pair in trace_actions if pair not in lowered_rung_pairs)
        active_trace_actions = tuple(
            pair for pair in active_trace_actions if pair not in lowered_rung_pairs
        )
        trace_action_details = tuple(
            detail for detail in trace_action_details if detail.pair not in lowered_rung_pairs
        )

    establish_details = tuple(detail for detail in trace_action_details if detail.establish)
    establish_pending = bool(establish_details)
    if establish_pending:
        establish_pairs = {detail.pair for detail in establish_details}
        trace_actions = tuple(pair for pair in trace_actions if pair in establish_pairs)
        active_trace_actions = tuple(
            pair for pair in active_trace_actions if pair in establish_pairs
        )
        trace_action_details = establish_details

    return _candidate_read._TraceAdmission(
        active_actions=active_trace_actions,
        actions=trace_actions,
        read_details=tuple(ordered_details),
        details=trace_action_details,
        detail_by_pair=MappingProxyType(detail_by_pair),
        managed_boolean_rungs=managed_boolean_rungs,
        establish_pending=establish_pending,
    )


def _admit_wait_read(
    read: _candidate_read.WaitRead,
    base_details: tuple[TraceAction, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> _candidate_read._AdmittedWait:
    """Admit one whole wait read through the candidate pool's only policy."""

    return _candidate_read._AdmittedWait(
        read=read,
        admission=_admit_trace_details(
            (*base_details, *read.details),
            frame,
            state,
            ctx,
            key_nogoods,
        ),
    )


def _separate_prerequisites(
    route_and_wait: _candidate_read._RouteAndCompletionRead,
    frame: Any,
    state: Any,
    ctx: Any,
) -> _candidate_read._PrerequisiteSeparation:
    """Separate executable holds without selecting among wait sources."""

    admission = route_and_wait.trace
    is_charted_completion = route_and_wait.charted_wait is not None
    is_coast = any(
        getattr(node, "advance", None) is not None and not node.satisfied
        for node in frame.tree.leaves()
    )
    instruction_boundary: ChannelHeading | None = None
    instruction_node: Any | None = None
    if is_coast:
        for node in frame.tree.leaves():
            step = getattr(node, "advance", None)
            if step is None or node.satisfied:
                continue
            boundary_pair = (
                getattr(node, "owner_boundary", None)
                if getattr(node, "linear_boundary", False)
                else None
            )
            if boundary_pair is not None:
                boundary = (
                    getattr(node, "owner_condition", None)
                    if getattr(node, "linear_boundary", False)
                    else None
                ) or step.until
                channel_tag, target_value = boundary_pair
                instruction_boundary = ChannelHeading(
                    channel_tag=channel_tag,
                    target_value=target_value,
                    boundary=boundary,
                )
            else:
                instruction_boundary = _boundary_heading(step.until, frame, state)
            if instruction_boundary is not None:
                instruction_node = node
                break
    if instruction_boundary is not None:
        trace_owned_rendezvous = any(
            detail.until is not None and not detail.pulse for detail in admission.details
        )
        hard_blockers = tuple(
            node
            for node in frame.tree.leaves()
            if node is not instruction_node
            and not node.satisfied
            and not node.is_steerable
            and getattr(node, "advance", None) is None
            and not getattr(node, "pipeline_internal", False)
        )
        if hard_blockers and not trace_owned_rendezvous:
            # A standalone instruction boundary is only coastable inside its
            # selected route shape.  Reaching a timer Done through an alternate
            # enable while a non-steerable sibling guard is false is not route
            # progress and must leave the next trace alternative visible. A
            # trace-owned lifetime is different: it is the selected producer's
            # rendezvous receipt, so durable siblings are positioned before
            # coasting that instruction boundary.
            instruction_boundary = None

    prerequisite_pilot_rungs = list(admission.managed_boolean_rungs)
    trace_actions = admission.actions
    active_trace_actions = admission.active_actions
    trace_action_details = admission.details
    if is_charted_completion or is_coast:
        completion_detail_pairs = (
            frozenset(detail.pair for detail in route_and_wait.charted_wait.details)
            if is_charted_completion and route_and_wait.charted_wait is not None
            else frozenset()
        )
        route = route_and_wait.route
        if route is not None:
            edge = route.plan.first_edge
            route_actions = () if edge.action is None else (edge.action, *edge.co_actions)
            route_request = (
                () if edge.request_tag is None else ((edge.request_tag, edge.request_value),)
            )
            route_needed = (
                *route_actions,
                *route_request,
                *edge.source_constraints,
                *edge.enablers,
                *edge.completion,
            )
        else:
            route_needed = ()
        # A prerequisite cannot make the selected route non-executable.  Put
        # the chart's immediate edge first because it is the concrete bearing;
        # the broader target trace follows as supporting evidence.
        needed = (*route_needed, *frontier_pairs(frame.tree, frame.snap))
        prerequisite_pilot_rungs = [
            rung
            for rung in prerequisite_pilot_rungs
            if not hold_defeats_needed(rung.dest, rung.value, needed, ctx.pdg, ctx.program)
        ]
        pulse_tags = {detail.tag for detail in trace_action_details if detail.pulse}
        seen_prereq: set[str] = set()
        for tag, value in trace_actions:
            if tag in seen_prereq or tag in {rung.dest for rung in state.pilot_rungs}:
                continue
            detail = admission.detail_by_pair.get((tag, value))
            if detail is None or detail.until is None:
                continue
            if (
                detail.availability > _WriterAvailability.AFTER_PREREQ
                and not _values_match(value, ctx.resting.get(tag, False))
                and (tag, value) not in completion_detail_pairs
                and (is_charted_completion or instruction_boundary is None)
            ):
                # A chart route and a target-wide trace are separate readings:
                # only the chart's selected completion detail may position an
                # unavailable future input for that coast. A standalone
                # instruction boundary is different. It came from this same
                # trace, so the trace-owned lifetime is the exact rendezvous
                # receipt for concurrent inputs which must persist while the
                # instruction advances. Releases remain valid so a spent
                # transaction cannot block later structural motion.
                continue
            scope = _until_unresolved_condition(state.work, detail.until)
            if tag in pulse_tags:
                seen_prereq.add(tag)
                if _action_allowed(ctx, (tag, value)):
                    prerequisite_pilot_rungs.extend(_oscillating_rungs(tag, ctx, scope, state.work))
            elif (
                tag not in ctx.edge_tags
                and tag not in ctx.clear_only
                and not detail.pulse
                and not _values_match(frame.snap.get(tag), value)
            ):
                if hold_defeats_needed(tag, value, needed, ctx.pdg, ctx.program):
                    continue
                seen_prereq.add(tag)
                if _action_allowed(ctx, (tag, value)) and not _avoid_forces(
                    ctx, [(tag, value)], frame.snap
                ):
                    prerequisite_pilot_rungs.append(PilotRung(tag, value, scope))
        prereq_tags = {rung.dest for rung in prerequisite_pilot_rungs}
        trace_actions = tuple(pair for pair in trace_actions if pair[0] not in prereq_tags)
        active_trace_actions = tuple(
            pair for pair in active_trace_actions if pair[0] not in prereq_tags
        )

    updated_trace = replace(
        admission,
        active_actions=active_trace_actions,
        actions=trace_actions,
        details=trace_action_details,
    )
    return _candidate_read._PrerequisiteSeparation(
        updated_trace,
        _candidate_read.PrerequisiteRead(tuple(prerequisite_pilot_rungs)),
        instruction_boundary,
    )
