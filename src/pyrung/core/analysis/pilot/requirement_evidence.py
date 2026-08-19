"""Derive and retain requirement facts from exact execution evidence.

This module interprets completed scans.  It does not choose navigation,
execute a Bearing, repair a requirement, or advance WorkingTheory.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.advance import build_advance_index
from pyrung.core.analysis.pilot.conductivity import charted_front_extends_current
from pyrung.core.analysis.pilot.effect_observation import (
    effect_reached_consumer,
    fulfilled_expectation_observations,
    observe_execution_window,
    terminal_target_replay_scan_ids,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    expectation_from_writer,
    obligation_snapshot,
    occurrence_snapshot,
    promote_terminal_target_observation,
)
from pyrung.core.analysis.pilot.execution import ExecutionReceipt, execution_owner
from pyrung.core.analysis.pilot.intrascan import (
    IntrascanQuestion,
    IntrascanResult,
    derive_recorded_observations,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    Coast,
    LocalProgressKind,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.pipeline_graph import target_reachable_values
from pyrung.core.analysis.pilot.requirement_derivation import (
    bind_guard_operand_authorities,
    derive_advance_requirement_from_effect,
    derive_overwriter_guard_requirement_from_effect,
    derive_overwriter_guard_requirement_from_write,
)
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    EffectReceiptRole,
    ExpectationReceipt,
    FailedEffectReceipt,
    OperandAuthority,
    classify_bound_operand_authority,
)
from pyrung.core.analysis.pilot.trace import target_reached
from pyrung.core.analysis.pilot.types import (
    TargetReached,
    _AcceptedTrial,
    _AttemptResult,
    _BootstrapExecution,
    _CausalCheckpoint,
    _ExecutedAttempt,
    _IterationFrame,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.verify import _route_blocker_crossings
from pyrung.core.analysis.pilot.working_theory import (
    temporal_setup_rung_identities,
    theory_view,
)
from pyrung.core.analysis.pilot.world_key import _rung_identity
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.context import RungId


def _exact_failed_source(
    requirement: ActiveRequirement,
    state: _PilotState,
) -> FailedEffectReceipt | None:
    """Match one requirement to its exact failed local transaction."""

    matches = tuple(
        receipt
        for receipt in state.failed_effect_receipts
        if receipt.checkpoint_owner is requirement.checkpoint_owner
        and receipt.source_world_key == requirement.source_world_key
        and receipt.selected_writer == requirement.selected_writer
        and receipt.execution_epoch is requirement.execution_epoch
        and receipt.execution_owner is requirement.execution_owner
        and requirement.deadline in receipt.explanation.supporting_occurrences
        and receipt.local_act is not None
        and receipt.local_bearing is not None
        and (
            receipt.expectation_role is EffectReceiptRole.ROUTE_LANDING
            or receipt.expectation is receipt.local_act.policy.expectation
        )
        and receipt.local_bearing.act is receipt.local_act
        and receipt.expectation is not None
        and any(
            obligation_snapshot(obligation) == receipt.observation.obligation
            for obligation in receipt.expectation.obligations
        )
        and receipt.act_identity == act_identity(receipt.local_act)
    )
    return matches[0] if len(matches) == 1 else None


def _configured_input_names(plc: Any) -> frozenset[str]:
    """Snapshot explicit patch/force ownership without retaining its manager."""

    overrides = getattr(plc, "_input_overrides", None)
    if overrides is None:
        return frozenset()
    return frozenset((*overrides.forces, *overrides.pending_patches))


def _checkpoint_configured_inputs(checkpoint: Any) -> frozenset[str]:
    """Read checkpoint provenance, falling back for lightweight test stubs."""

    configured = getattr(checkpoint, "configured_inputs", None)
    if configured is not None:
        return frozenset(configured)
    source_work = checkpoint.world.work
    return _configured_input_names(source_work)


def _bound_operand_authorities(
    projection: Any,
    checkpoint: _CausalCheckpoint,
    ctx: _PilotContext,
    state: _PilotState,
    execution: ExecutionReceipt | None = None,
) -> dict[str, OperandAuthority]:
    """Classify exact boundary operands without inventing write permission."""

    source_work = checkpoint.world.work
    source_tags = source_work.state.tags
    known = source_work._known_tags_by_name
    program_written = frozenset(ctx.pdg.writers_of)
    configured = _checkpoint_configured_inputs(checkpoint)
    temporal_owned = temporal_setup_rung_identities(state.theory_state)
    provisional = frozenset(
        rung.dest for rung in state.pilot_rungs if _rung_identity(rung) in temporal_owned
    )
    if execution is not None:
        provisional |= frozenset(
            tag
            for configuration in execution.applied_configurations
            for tag, _value in configuration.assignments
        )
    result: dict[str, OperandAuthority] = {}
    for read in projection.reads:
        tag = read.occurrence.name
        declared = known.get(tag)
        result[tag] = classify_bound_operand_authority(
            tag,
            source_value=source_tags.get(tag, getattr(declared, "default", None)),
            declared_default=getattr(declared, "default", None),
            steerable=ctx.steerable,
            program_written=program_written,
            configured=configured,
            provisional=provisional,
        )
    return result


def _bind_guard_derivation_authority(
    derivation: Any,
    checkpoint: _CausalCheckpoint,
    ctx: _PilotContext,
) -> Any:
    """Bind arbitrary guard atoms without completion-parameter heuristics."""

    requirement = bind_guard_operand_authorities(
        derivation.requirement,
        steerable=ctx.steerable,
        program_written=frozenset(ctx.pdg.writers_of),
        configured=_checkpoint_configured_inputs(checkpoint),
    )
    return replace(derivation, requirement=requirement)


def _retain_active_requirement(
    state: _PilotState,
    requirement: ActiveRequirement | None,
) -> bool:
    """Append one exact requirement once without changing executable state."""

    if requirement is None or any(
        current.navigation_identity == requirement.navigation_identity
        for current in state.active_requirements
    ):
        return False
    state.active_requirements.append(requirement)
    return True


def _derive_bootstrap_requirements(
    state: _PilotState,
    ctx: _PilotContext,
    receipt: _BootstrapExecution,
    *,
    provenance: str = "bootstrap",
) -> None:
    """Interpret exact appeared bootstrap violations without repairing them."""

    execution_owner = receipt.execution.owner_at(receipt.scan_after)
    if execution_owner is None:
        raise RuntimeError("bootstrap execution receipt lost its landing scan owner")
    index = build_advance_index(
        ctx.program,
        getattr(receipt.checkpoint.world.work, "_harness", None),
    )
    authorities = _bound_operand_authorities(
        receipt.projection,
        receipt.checkpoint,
        ctx,
        state,
    )
    for effect in receipt.appeared_effects:
        derivation = derive_advance_requirement_from_effect(
            index,
            receipt.projection,
            effect.observation,
            operand_authorities=authorities,
            execution_epoch=execution_owner.epoch,
            execution_owner=execution_owner,
            selected_writer=effect.designation.producer,
            source_world_key=receipt.checkpoint.key,
            source_checkpoint=receipt.checkpoint,
            provenance=provenance,
        )
        if derivation.requirement is None:
            derivation = _bind_guard_derivation_authority(
                derive_overwriter_guard_requirement_from_effect(
                    effect.observation,
                    receipt.projection,
                    execution_epoch=execution_owner.epoch,
                    execution_owner=execution_owner,
                    selected_writer=effect.designation.producer,
                    source_world_key=receipt.checkpoint.key,
                    source_checkpoint=receipt.checkpoint,
                    provenance=f"{provenance}-overwriter",
                ),
                receipt.checkpoint,
                ctx,
            )
        _retain_active_requirement(state, derivation.requirement)


def _release_attempt_projections(attempt: _AttemptResult | None) -> None:
    """Release selected-scan replay evidence after its last consumer."""

    if attempt is not None:
        attempt.release_projections()


def _attempt_productive_scan(executed: _ExecutedAttempt) -> int:
    """Return S1, the first physical scan owned by this ordinary bearing."""

    action_scan = executed.pulse.action_scan
    if action_scan is not None and not isinstance(executed.bearing.act, Coast):
        return action_scan
    first_scan = next(
        (
            scan_id
            for scan_id in executed.pulse.kernel_scan_ids
            if scan_id > executed.pulse.scan_before
        ),
        None,
    )
    if first_scan is not None:
        return first_scan
    return executed.assertion_scan


def _derive_route_landing_requirements(
    attempt: _AttemptResult,
    state: _PilotState,
    ctx: _PilotContext,
    checkpoint: _CausalCheckpoint,
) -> tuple[ActiveRequirement, ...]:
    """Turn an owned look-ahead route departure into SETUP_FIRST facts."""

    executed = attempt.executed_attempt
    policy = executed.bearing.act.policy if executed is not None else None
    local_progress = policy.local_progress if policy is not None else None
    # A selected heading is Compass's exact declaration that this execution is
    # serving a structural route boundary.  Its optional look-ahead scan is an
    # owned receipt even when ProgramStep supplied the immediate input and the
    # ordinary verification promise succeeded.  Read collateral writes now;
    # waiting for the later target steer to fail needlessly turns current-world
    # evidence into a historical rebase.
    accepted_route_step = bool(
        attempt.trial is not None and policy is not None and policy.heading is not None
    )
    if (
        executed is None
        or (
            not accepted_route_step
            and local_progress
            not in {LocalProgressKind.TRACE_SETUP, LocalProgressKind.TEMPORAL_SETUP}
        )
        or (
            local_progress is LocalProgressKind.TRACE_SETUP
            and not attempt.proof_rejection
            and not accepted_route_step
        )
        or (local_progress is LocalProgressKind.TEMPORAL_SETUP and attempt.trial is None)
    ):
        return ()
    orientation = executed.bearing.orientation
    if orientation is None:
        return ()
    heading = executed.bearing.act.policy.heading
    preserved_values = ((heading.channel_tag, heading.target_value),) if heading is not None else ()
    advance_index = build_advance_index(
        ctx.program,
        getattr(checkpoint.world.work, "_harness", None),
    )
    derived: list[ActiveRequirement] = []
    for crossing in _route_blocker_crossings(
        executed,
        orientation.world.frame,
        ctx,
        pilot_rungs=state.pilot_rungs,
        resting=ctx.resting,
    ):
        if advance_index.resolve(crossing.tag) is not None:
            # Timer/counter completion bits are derived channels, not local
            # handoffs Pilot may suppress by complementing the producing
            # rung's guard.  Their owner-specific operand inversion needs the
            # later exact consumer read used by ordinary intrascan analysis.
            # Until that receipt exists, fail closed instead of "preventing"
            # productive route work such as the dwell that starts a watchdog.
            continue
        owner = execution_owner(executed.pulse.fork, crossing.projection.scan_id)
        if owner is None:
            continue
        exact_nodes = tuple(
            node
            for node in ctx.pdg.rung_nodes
            if RungId(node.subroutine, node.rung_index) == crossing.write.rung_id
            and resolve_rung(ctx.program, node) is crossing.write.run.rung
        )
        if len(exact_nodes) != 1:
            continue
        node = exact_nodes[0]
        derivation = _bind_guard_derivation_authority(
            derive_overwriter_guard_requirement_from_write(
                crossing.write,
                crossing.projection,
                execution_epoch=owner.epoch,
                execution_owner=owner,
                selected_writer=(node.subroutine, node.rung_index, node.branch_path),
                source_world_key=checkpoint.key,
                source_checkpoint=checkpoint,
                provenance="route-lookahead",
                scope=(("route_landing_blocker", repr(crossing.predicate)),),
                preserved_values=preserved_values,
            ),
            checkpoint,
            ctx,
        )
        if derivation.requirement is not None and _retain_active_requirement(
            state, derivation.requirement
        ):
            derived.append(derivation.requirement)
    return tuple(derived)


def _derive_charted_intrascan_front(
    executed: _ExecutedAttempt,
    state: _PilotState,
    ctx: _PilotContext,
    checkpoint: _CausalCheckpoint,
) -> IntrascanResult | None:
    """Interpret an exact useful chart write displaced later in the same scan.

    A temporal setup can have no act-local expectation while the program still
    moves the target channel through a valid intermediate value and then into
    a hazard value.  The immutable projection is sufficient: select only a
    write whose value has a static path to the target and only when a later
    exact write in that projection leaves the chart.  This creates evidence,
    never a retained route or a hypothetical executable patch.
    """

    graphs = tuple(
        graph
        for graph in (
            *ctx.compass.catalog.graphs,
            *ctx.compass.catalog.chart_graphs,
        )
        if graph.role.channel_tag == ctx.target.tag
    )
    reachable = tuple(
        value for graph in graphs for value in target_reachable_values(graph, ctx.target.value)
    )
    if not reachable:
        return None

    candidates: list[tuple[Any, Any]] = []
    current_writes: list[Any] = []
    for scan_id in executed.pulse.kernel_scan_ids:
        if not (executed.pulse.scan_before < scan_id <= executed.pulse.fork.state.scan_id):
            continue
        projection = executed.projection_at(scan_id)
        if projection is None:
            continue
        writes = tuple(
            write
            for write in projection.writes
            if write.run.enabled and write.transition.tag_name == ctx.target.tag
        )
        current_writes.extend(writes)
        for index, write in enumerate(writes):
            if not any(_values_match(write.transition.to_value, value) for value in reachable):
                continue
            later = writes[index + 1 :]
            if later and any(
                not any(_values_match(item.transition.to_value, value) for value in reachable)
                for item in later
            ):
                candidates.append((projection, write))
    if not candidates and current_writes:
        source_scan = executed.pulse.scan_before
        source_projection = checkpoint.world.work._replay_pilot_rung_write_projection_at(
            source_scan
        )
        source_value = checkpoint.world.work.state.tags.get(ctx.target.tag)
        if source_projection is not None and any(
            not any(_values_match(write.transition.to_value, value) for value in reachable)
            for write in current_writes
        ):
            source_writes = tuple(
                write
                for write in source_projection.writes
                if write.run.enabled
                and write.transition.tag_name == ctx.target.tag
                and _values_match(write.transition.to_value, source_value)
                and any(_values_match(write.transition.to_value, value) for value in reachable)
            )
            if source_writes:
                candidates.append((source_projection, source_writes[-1]))
    if not candidates:
        return None
    projection, productive_write = candidates[-1]
    if projection is None:
        return None
    selected_projection = projection
    exact_nodes = tuple(
        index
        for index, node in enumerate(ctx.pdg.rung_nodes)
        if RungId(node.subroutine, node.rung_index) == productive_write.rung_id
        and resolve_rung(ctx.program, node) is productive_write.run.rung
        and ctx.target.tag in node.writes
    )
    if len(exact_nodes) != 1:
        return None
    expectation = expectation_from_writer(
        ctx.pdg,
        ctx.program,
        writer_node=exact_nodes[0],
        tag=ctx.target.tag,
        value=productive_write.transition.to_value,
        boundary=(ctx.target.tag, productive_write.transition.to_value),
    )
    if expectation is None:
        return None

    def projection_at(scan_id: int) -> Any:
        if scan_id == selected_projection.scan_id:
            return selected_projection
        return executed.projection_at(scan_id)

    observations = observe_execution_window(
        expectation,
        executed.pulse.fork,
        scan_before=projection.scan_id - 1,
        action_scan=None,
        kernel_scan_ids=tuple(dict.fromkeys((projection.scan_id, *executed.pulse.kernel_scan_ids))),
        projection_at=projection_at,
    )
    if not any(
        observation.disposition in {"OVERWRITTEN", "DISPLACED"} for observation in observations
    ):
        return None
    question = IntrascanQuestion(
        expectation=expectation,
        execution=executed.pulse.fork,
        assertion_scan=projection.scan_id,
        source_checkpoint=checkpoint,
        advance_index=None,
        operand_authorities={},
        steerable=ctx.steerable,
        program_written=frozenset(ctx.pdg.writers_of),
        configured_inputs=_checkpoint_configured_inputs(checkpoint),
        advance_index_factory=lambda: build_advance_index(
            ctx.program,
            getattr(checkpoint.world.work, "_harness", None),
        ),
        operand_authorities_at=lambda current: _bound_operand_authorities(
            current,
            checkpoint,
            ctx,
            state,
            executed.execution,
        ),
        projection_at=projection_at,
    )
    return derive_recorded_observations(
        question,
        observations,
        fallback_scan=projection.scan_id,
    )


def _derive_attempt_requirements(
    attempt: _AttemptResult,
    state: _PilotState,
    ctx: _PilotContext,
    checkpoint: _CausalCheckpoint | None,
) -> IntrascanResult | None:
    """Retain and return one interpretation of a disposable steer's receipts."""

    # VERIFY's terminal verdict is stronger than any subordinate handoff
    # receipt in the same owned execution.  Pipeline/request registers may be
    # cleaned up after doing their work; once the final objective is true that
    # cleanup cannot authorize a corrective detour.
    if attempt.trial is not None and isinstance(attempt.trial.verification, TargetReached):
        return None

    executed = attempt.executed_attempt
    exact_displacement = bool(
        executed is not None
        and any(
            observation.disposition in {"OVERWRITTEN", "DISPLACED"}
            for observation in executed.effect_observations
        )
    )
    # A normal accepted act is locally successful and retains its expectation
    # receipt for later regression. An active-theory lookahead is different:
    # if its own scan already proves a selected effect was overwritten or
    # displaced, intrascan owns that new requirement immediately.
    if checkpoint is None:
        return None
    if executed is None:
        return None
    if attempt.trial is not None and not exact_displacement:
        charted_report = (
            _derive_charted_intrascan_front(executed, state, ctx, checkpoint)
            if theory_view(state.theory_state) is not None
            else None
        )
        charted_observations = (
            tuple(finding.observation.diagnostic_snapshot() for finding in charted_report.findings)
            if charted_report is not None
            else ()
        )
        view = theory_view(state.theory_state)
        if (
            charted_report is not None
            and charted_report.findings
            and view is not None
            and charted_front_extends_current(view, charted_observations)
        ):
            _retain_intrascan_findings(
                charted_report,
                state,
                checkpoint,
                executed,
                accepted=True,
            )
            return charted_report
        _derive_route_landing_requirements(attempt, state, ctx, checkpoint)
        return None
    fallback_scan = _attempt_productive_scan(executed)
    question = IntrascanQuestion(
        expectation=executed.bearing.expectation,
        execution=executed.pulse.fork,
        assertion_scan=fallback_scan,
        source_checkpoint=checkpoint,
        advance_index=None,
        operand_authorities={},
        steerable=ctx.steerable,
        program_written=frozenset(ctx.pdg.writers_of),
        configured_inputs=_checkpoint_configured_inputs(checkpoint),
        advance_index_factory=lambda: build_advance_index(
            ctx.program,
            getattr(checkpoint.world.work, "_harness", None),
        ),
        operand_authorities_at=lambda projection: _bound_operand_authorities(
            projection,
            checkpoint,
            ctx,
            state,
            attempt.execution_receipt,
        ),
        projection_at=executed.projection_at,
    )
    report = derive_recorded_observations(
        question,
        executed.effect_observations,
        fallback_scan=fallback_scan,
    )
    exact_report_displacement = any(
        finding.observation.disposition in {"OVERWRITTEN", "DISPLACED"}
        for finding in report.findings
    )
    if not exact_displacement and not exact_report_displacement:
        # Route look-ahead complements a crossing only as a fallback. The
        # recorded route-landing receipt may be more exact than the immediate
        # act expectation, so decide precedence after both are interpreted.
        _derive_route_landing_requirements(attempt, state, ctx, checkpoint)
    _retain_intrascan_findings(
        report,
        state,
        checkpoint,
        executed,
        accepted=attempt.trial is not None,
    )
    return report


def _retain_intrascan_findings(
    report: IntrascanResult,
    state: _PilotState,
    checkpoint: _CausalCheckpoint,
    executed: _ExecutedAttempt,
    *,
    accepted: bool,
) -> None:
    """Commit exact intrascan findings through the common receipt boundary."""

    for finding in report.findings:
        # A useful landing after the selected consumer read the transient value
        # normally owns the continuation. While a WorkingTheory is assembling
        # one complete scan, however, a later exact displacement is the next
        # hose front to resolve: crossing the local consumer did not make the
        # outer transaction durable. Retain that occurrence as a requirement;
        # it still has to pass through fresh Compass before any correction.
        if (
            accepted
            and effect_reached_consumer(finding.observation)
            and theory_view(state.theory_state) is None
        ):
            continue
        observation = finding.observation
        observation_owner = observation.execution_owner
        if observation_owner is None:
            continue
        derivation = finding.derivation
        diagnostic = finding.diagnostic_snapshot()
        immediate_expectation = executed.bearing.expectation
        receipt_expectation = (
            immediate_expectation
            if immediate_expectation is not None
            and any(
                obligation is observation.obligation
                for obligation in immediate_expectation.obligations
            )
            else EffectExpectation((observation.obligation,))
        )
        expectation_role = (
            EffectReceiptRole.IMMEDIATE
            if receipt_expectation is immediate_expectation
            else EffectReceiptRole.ROUTE_LANDING
        )
        failed = FailedEffectReceipt(
            explanation=diagnostic.explanation,
            observation=diagnostic.observation,
            selected_writer=diagnostic.selected_writer,
            source_world_key=diagnostic.source_world_key,
            checkpoint_owner=checkpoint.owner,
            execution_owner=observation_owner,
            source_checkpoint=checkpoint,
            act_identity=act_identity(executed.bearing.act),
            local_act=executed.bearing.act,
            local_bearing=executed.bearing,
            # The failed occurrence may belong to the route-landing receipt,
            # not the act's immediate side-effect expectation.  Retain the
            # exact obligation that intrascan inverted so WorkingTheory charts
            # the failed route edge rather than a coincident successful effect.
            expectation=receipt_expectation,
            expectation_role=expectation_role,
        )
        if not any(current.identity == failed.identity for current in state.failed_effect_receipts):
            state.failed_effect_receipts.append(failed)
        _retain_active_requirement(state, derivation.requirement)


def _derive_settled_target_requirements(
    trial: _AcceptedTrial,
    state: _PilotState,
    ctx: _PilotContext,
    checkpoint: _CausalCheckpoint | None,
) -> IntrascanResult | None:
    """Interpret a zero-net target loss inside monitor-owned settlement.

    Post-commit departure settling is ordinary execution, but it may own the
    first exact occurrence of the selected terminal writer.  When that value
    is displaced before the scan exits, hand the recorded projection to the
    same intrascan interpreter used by a disposable steer.  The resulting
    failed-effect receipt lets the normal working-theory lifecycle restore the
    original source and compose a fresh Bearing.
    """

    if checkpoint is None or target_reached(
        dict(state.work.state.tags),
        ctx.target.tag,
        ctx.target.value,
        ctx.target.predicate,
    ):
        return None
    executed = trial.attempt
    # ``fork`` is the committed post-execution runner.  Its current scan is
    # the end of the observation window, not the source boundary.  The pulse
    # receipt retains the exact pre-execution scan even after adoption.
    scan_before = executed.pulse.scan_before
    scan_after = state.work.state.scan_id
    if scan_after <= scan_before:
        return None
    orientation = executed.bearing.orientation
    if orientation is None:
        return None
    frame = orientation.world.frame
    expectation = _selected_terminal_target_expectation(frame, ctx.target, ctx)
    if expectation is None:
        return None
    exact_scans = tuple(range(scan_before + 1, scan_after + 1))
    candidate_scans = terminal_target_replay_scan_ids(
        expectation,
        state.work,
        exact_scans,
    )
    if not candidate_scans:
        return None

    def projection_at(scan_id: int) -> Any:
        projection = state.work._replay_rung_write_projection_at(scan_id)
        return projection if projection is not None and projection.scan_id == scan_id else None

    if any(projection_at(scan_id) is None for scan_id in candidate_scans):
        return None
    entry_projection = projection_at(candidate_scans[0])
    assert entry_projection is not None
    observations = observe_execution_window(
        expectation,
        state.work,
        scan_before=scan_before,
        action_scan=None,
        kernel_scan_ids=candidate_scans,
        projection_at=projection_at,
    )
    promoted = promote_terminal_target_observation(
        observations,
        # Sparse nomination defines the terminal transaction's exact local
        # window. Earlier settlement scans may establish this source value;
        # they are not part of the later zero-net target occurrence.
        window_entry_value=entry_projection.entry_tags.get(ctx.target.tag),
        final_landing_value=state.work.state.tags.get(ctx.target.tag),
    )
    if promoted is None:
        return None
    fallback_scan = next(
        (
            occurrence.scan_id
            for occurrence in (promoted.displacement, promoted.appeared)
            if occurrence is not None
        ),
        candidate_scans[0],
    )
    question = IntrascanQuestion(
        expectation=expectation,
        execution=state.work,
        assertion_scan=fallback_scan,
        source_checkpoint=checkpoint,
        advance_index=None,
        operand_authorities={},
        steerable=ctx.steerable,
        program_written=frozenset(ctx.pdg.writers_of),
        configured_inputs=_checkpoint_configured_inputs(checkpoint),
        advance_index_factory=lambda: build_advance_index(
            ctx.program,
            getattr(checkpoint.world.work, "_harness", None),
        ),
        operand_authorities_at=lambda projection: _bound_operand_authorities(
            projection,
            checkpoint,
            ctx,
            state,
            trial.execution,
        ),
        projection_at=projection_at,
    )
    report = derive_recorded_observations(
        question,
        (promoted,),
        fallback_scan=fallback_scan,
    )
    _retain_intrascan_findings(
        report,
        state,
        checkpoint,
        executed,
        accepted=True,
    )
    return report


def _verified_progress_landing(
    trial: _AcceptedTrial,
) -> tuple[EffectExpectation, tuple[Any, ...]] | None:
    """Bind VERIFY's accepted frontier to its unique exact landing write.

    ProgramStep is a pre-execution reading and may remain ``UNCLEAR`` while an
    exact scan still proves target-relative progress.  In that case VERIFY's
    ``ScanProgressReceipt`` owns the positive claim and runner history owns its
    occurrence.  Joining them here gives later departure recovery a causal
    source without promoting chart geometry or a speculative projection to
    action authority.
    """

    progress = trial.execution.scan_progress
    attempt = trial.attempt
    heading = attempt.bearing.act.policy.heading
    if (
        progress is None
        or not progress.landing_owns_tip
        or progress.kind not in {"frontier", "selected-producer"}
        or heading is None
        or heading.channel_tag is None
    ):
        return None
    tag = heading.channel_tag
    value = attempt.pulse.snap.get(tag)
    candidates = tuple(
        write
        for scan_id in attempt.pulse.kernel_scan_ids
        if progress.source_scan < scan_id <= progress.landing_scan
        if (projection := attempt.projection_at(scan_id)) is not None
        for write in projection.writes
        if write.run.enabled
        and write.transition.tag_name == tag
        and _values_match(write.transition.to_value, value)
    )
    if not candidates:
        return None
    landing_write = candidates[-1]
    orientation = attempt.bearing.orientation
    if orientation is None:
        return None
    ctx = orientation.world.context
    writer_nodes = tuple(
        index
        for index, node in enumerate(ctx.pdg.rung_nodes)
        if RungId(node.subroutine, node.rung_index) == landing_write.rung_id
        and resolve_rung(ctx.program, node) is landing_write.run.rung
        and tag in node.writes
    )
    if len(writer_nodes) != 1:
        return None
    expectation = expectation_from_writer(
        ctx.pdg,
        ctx.program,
        writer_node=writer_nodes[0],
        tag=tag,
        value=value,
        boundary=(tag, value),
    )
    if expectation is None:
        return None
    pulse = attempt.pulse
    observations = observe_execution_window(
        expectation,
        pulse.fork,
        scan_before=pulse.scan_before,
        action_scan=pulse.action_scan,
        kernel_scan_ids=pulse.kernel_scan_ids,
        projection_at=pulse.projection_at,
    )
    fulfilled = fulfilled_expectation_observations(expectation, observations)
    return (expectation, fulfilled) if len(fulfilled) == len(expectation.obligations) else None


def _retain_expectation_receipt(
    trial: _AcceptedTrial,
    act: Any,
    state: _PilotState,
    checkpoint: _CausalCheckpoint | None,
) -> None:
    """Journal every accepted expectation role with its exact occurrences.

    One physical act may prove both its immediate selected writer and a later
    route landing.  They are distinct causal receipts: a subsequent departure
    can originate at the landing even when the immediate handoff remains
    valid.  Retaining only the policy expectation loses that ownership and
    forces post-commit recovery to guess from an unbound incident.
    """

    if checkpoint is None:
        return
    progress_landing = (
        _verified_progress_landing(trial) if trial.attempt.landing_expectation is None else None
    )
    expectations = (
        (
            EffectReceiptRole.IMMEDIATE,
            trial.attempt.bearing.expectation,
            trial.attempt.effect_observations,
        ),
        (
            EffectReceiptRole.ROUTE_LANDING,
            trial.attempt.landing_expectation,
            trial.attempt.effect_observations,
        ),
        (
            EffectReceiptRole.ROUTE_LANDING,
            progress_landing[0] if progress_landing is not None else None,
            progress_landing[1] if progress_landing is not None else (),
        ),
    )
    for expectation_role, expectation, evidence in expectations:
        if expectation is None:
            continue
        observations = fulfilled_expectation_observations(
            expectation,
            evidence,
        )
        if len(observations) != len(expectation.obligations):
            continue
        epochs = {id(item.execution_epoch) for item in observations}
        owners = {id(item.execution_owner) for item in observations}
        if len(epochs) != 1 or len(owners) != 1:
            continue
        first = observations[0]
        if first.execution_epoch is None or first.execution_owner is None:
            continue
        producers = tuple(
            occurrence_snapshot(item.appeared) for item in observations if item.appeared is not None
        )
        consumers = tuple(
            occurrence_snapshot(item.consumer_read)
            for item in observations
            if item.consumer_read is not None
        )
        if not producers:
            continue
        receipt = ExpectationReceipt(
            source_world_key=checkpoint.key,
            checkpoint_owner=checkpoint.owner,
            act_identity=act_identity(act),
            active_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
            obligations=tuple(obligation_snapshot(item.obligation) for item in observations),
            producer_occurrences=producers,
            consumer_occurrences=consumers,
            execution=trial.execution,
            source_checkpoint=checkpoint,
            local_act=act,
            local_bearing=trial.attempt.bearing,
            expectation=expectation,
            expectation_role=expectation_role,
        )
        if not any(current.identity == receipt.identity for current in state.expectation_receipts):
            state.expectation_receipts.append(receipt)


def _disposable_requirement_state(
    state: _PilotState,
    checkpoint: _CausalCheckpoint,
) -> _PilotState:
    """Clone one exact causal world without sharing Phase-5 knowledge lists."""

    clone = _PilotState(
        world=checkpoint.world,
        key_config=state.key_config,
        seen_keys=set(state.seen_keys),
        checkpoints=[],
        watch_tags=list(state.watch_tags),
        invocation_checkpoint=state.invocation_checkpoint,
        bootstrap_execution=state.bootstrap_execution,
        active_requirements=list(state.active_requirements),
        expectation_receipts=list(state.expectation_receipts),
        failed_effect_receipts=list(state.failed_effect_receipts),
        temporal_checkpoints=list(state.temporal_checkpoints),
        theory_state=state.theory_state,
        recovery_continuation=state.recovery_continuation,
        proof_rejected_acts=set(state.proof_rejected_acts),
        search_start_scan=state.search_start_scan,
        earned_work=state.earned_work,
    )
    clone.load_world(checkpoint.world)
    return clone


def _selected_terminal_target_expectation(
    frame: _IterationFrame,
    target: TargetSpec,
    ctx: _PilotContext,
) -> EffectExpectation | None:
    """Name the exact selected root writer for an equality target.

    This is only a designation until execution proves the writer appeared.
    Relational targets and unresolved/ambiguous root writers fail closed.
    """

    if target.predicate is not None:
        return None
    root = frame.tree
    if (
        root.writer_rung is None
        or root.tag != target.tag
        or not _values_match(root.value, target.value)
    ):
        return None
    return expectation_from_writer(
        ctx.pdg,
        ctx.program,
        writer_node=root.writer_rung,
        tag=target.tag,
        value=target.value,
        boundary=(target.tag, target.value),
        terminal_target=True,
    )
