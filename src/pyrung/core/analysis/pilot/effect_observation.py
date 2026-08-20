"""Observe selected effect obligations in exact execution evidence.

This module interprets intrascan and multi-scan projections for an already
selected :class:`EffectExpectation`. It records exact observations and
consumer crossings; it never selects a producer, required shape, or route.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.causal._rung_writes import RungRead, RungWrite
from pyrung.core.analysis.write_sites import (
    instruction_write_targets,
    static_write_target_names,
)
from pyrung.core.context import RungId
from pyrung.core.instruction.coils import OutInstruction
from pyrung.core.instruction.control import CallInstruction, ForLoopInstruction

if TYPE_CHECKING:
    from pyrung.core.analysis.causal._rung_writes import (
        OrderedEffectObservation,
        ScanRungWriteProjection,
    )

from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObligation,
    EffectObservation,
)


def observe_expectation(
    expectation: EffectExpectation,
    projections: Iterable[ScanRungWriteProjection],
) -> tuple[EffectObservation, ...]:
    """Observe every exact appeared occurrence, or one ordinary ``ABSENT``.

    Bootstrap intentionally does not call this adapter: its designations are
    not promises and continue to intersect with appeared writes only.
    """

    projection_tuple = tuple(projections)
    result: list[EffectObservation] = []
    for obligation in expectation.obligations:
        appeared: list[tuple[ScanRungWriteProjection, OrderedEffectObservation]] = []
        for projection in projection_tuple:
            appeared.extend(
                (projection, observation)
                for observation in projection.observe_appeared_handoff(
                    obligation.tag,
                    obligation.value,
                    producer_rung=obligation.producer_rung,
                    consumer_rung=obligation.consumer_rung,
                    producer_address=obligation.producer,
                    consumer_address=obligation.consumer,
                    required_shape=obligation.required_shape,
                )
            )
        if not appeared:
            result.append(
                EffectObservation(
                    obligation,
                    "ABSENT",
                    detail="selected producer did not write the expected value",
                    execution_projection=(
                        projection_tuple[0] if len(projection_tuple) == 1 else None
                    ),
                )
            )
            continue
        for projection, observation in appeared:
            if obligation.projected_consumer:
                result.append(_observe_projected_consumer(obligation, observation, projection))
                continue
            later_writes = tuple(
                (later, write)
                for later in projection_tuple
                if later.scan_id > projection.scan_id
                for write in later.writes
                if write.run.enabled and write.transition.tag_name == obligation.tag
            )
            if (
                obligation.consumer_rung is None
                and observation.disposition == "SURVIVED"
                and later_writes
            ):
                later_projection, later_write = later_writes[0]
                result.append(
                    EffectObservation(
                        obligation=obligation,
                        disposition="OVERWRITTEN",
                        appeared=observation.appeared,
                        displacement=later_write,
                        observed_reads=later_projection.enabling_reads_observed_by_write(
                            later_write
                        ),
                        detail="a later corridor write replaced the terminal effect",
                        execution_projection=later_projection,
                    )
                )
                continue
            if observation.disposition == "STRANDED":
                preceding_reads = _consumer_reads_preceding_write(
                    obligation,
                    observation.appeared,
                    projection,
                )
                if len(preceding_reads) > 1:
                    result.append(
                        EffectObservation(
                            obligation,
                            "UNKNOWN",
                            appeared=observation.appeared,
                            observed_reads=preceding_reads,
                            detail="consumer occurrence before scan wrap is ambiguous",
                            execution_projection=projection,
                        )
                    )
                    continue
                adjacent_scan_is_observed = any(
                    candidate.scan_id == projection.scan_id + 1 for candidate in projection_tuple
                )
                if len(preceding_reads) == 1 or adjacent_scan_is_observed:
                    wrapped = _observe_wrapped_handoff(
                        obligation,
                        observation.appeared,
                        projection,
                        projection_tuple,
                    )
                    if wrapped is not None:
                        result.append(wrapped)
                        continue
            result.append(_from_ordered(obligation, observation, projection))
    return tuple(result)


def _observe_projected_consumer(
    obligation: EffectObligation,
    observation: OrderedEffectObservation,
    projection: ScanRungWriteProjection,
) -> EffectObservation:
    """Resolve one statically opaque consumer from exact occurrence sources.

    This is deliberately stricter than choosing the first same-tag read.  The
    read must carry the exact selected write as its source and occur before the
    first displacement.  More than one such read leaves the intended consumer
    ambiguous and therefore fails closed.
    """

    appeared = observation.appeared
    ceiling = observation.displacement.ordinal if observation.displacement is not None else None
    sourced = tuple(
        read
        for read in projection.reads
        if read.occurrence.name == obligation.tag
        and read.ordinal > appeared.ordinal
        and (ceiling is None or read.ordinal < ceiling)
        and (
            (transition := projection.transition_observed_by_read(read)) is not None
            and transition.occurrence_ordinal == appeared.ordinal
            and transition.tag_name == obligation.tag
        )
    )
    if len(sourced) != 1:
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            displacement=observation.displacement,
            observed_reads=sourced,
            detail=(
                "projected consumer read is unavailable"
                if not sourced
                else "projected consumer read is ambiguous"
            ),
            execution_projection=projection,
        )
    consumer_read = sourced[0]
    consumer_shape = projection.observed_shape(consumer_read)
    if not consumer_read.run.enabled:
        return EffectObservation(
            obligation,
            "STRANDED",
            appeared=appeared,
            consumer_read=consumer_read,
            observed_reads=consumer_shape,
            detail="projected consumer read the effect but its run was disabled",
            execution_projection=projection,
        )
    return EffectObservation(
        obligation,
        "SURVIVED",
        appeared=appeared,
        consumer_read=consumer_read,
        observed_reads=consumer_shape,
        detail="exact projection resolved the selected consumer",
        execution_projection=projection,
    )


def fulfilled_expectation_observations(
    expectation: EffectExpectation,
    observations: Iterable[EffectObservation],
) -> tuple[EffectObservation, ...]:
    """One exact surviving occurrence for every selected obligation.

    A producer can execute again during pulse settlement. Once an occurrence
    has reached the selected consumer, a later repeat cannot retroactively
    invalidate that already fulfilled obligation; any later consequence is
    owned by the committed receipt and progress monitor.
    """

    observed = tuple(observations)
    fulfilled: list[EffectObservation] = []
    for obligation in expectation.obligations:
        survived = next(
            (
                item
                for item in observed
                if item.obligation is obligation and item.disposition == "SURVIVED"
            ),
            None,
        )
        if survived is None:
            return ()
        fulfilled.append(survived)
    return tuple(fulfilled)


def effect_reached_consumer(observation: EffectObservation) -> bool:
    """Whether one selected value completed its exact handoff.

    Program cleanup may replace a transient value after its selected consumer
    has read it.  The displacement remains useful execution evidence, but it
    cannot retroactively turn that completed handoff into a failed one.
    """

    if observation.disposition == "SURVIVED":
        return True
    appeared = observation.appeared
    consumer = observation.consumer_read
    displacement = observation.displacement
    return bool(
        observation.disposition == "OVERWRITTEN"
        and appeared is not None
        and consumer is not None
        and displacement is not None
        and appeared.scan_id == consumer.scan_id == displacement.scan_id
        and appeared.ordinal < consumer.ordinal < displacement.ordinal
    )


def _consumer_reads_preceding_write(
    obligation: EffectObligation,
    appeared: RungWrite,
    projection: ScanRungWriteProjection,
) -> tuple[RungRead, ...]:
    """Exact consumer reads which make this handoff due after a cycle wrap."""

    if obligation.consumer_rung is None:
        return ()
    return tuple(
        read
        for run in projection.runs
        if run.rung is obligation.consumer_rung
        and projection._runs_share_selected_transaction(
            appeared.run,
            run,
            obligation.producer,
            obligation.consumer,
        )
        for read in projection.reads_for_run(run)
        if read.occurrence.name == obligation.tag and read.ordinal < appeared.ordinal
    )


def _observe_wrapped_handoff(
    obligation: EffectObligation,
    appeared: RungWrite,
    producer_projection: ScanRungWriteProjection,
    projections: tuple[ScanRungWriteProjection, ...],
) -> EffectObservation | None:
    """Observe the next exact consumer occurrence after scan order wraps.

    Cross-scan entry reads intentionally carry the executor's ``entry`` source
    marker rather than a previous scan's write object. Continuity is therefore
    proved from adjacent exact projections: the selected write wins the
    producer scan, the value is unchanged at the next entry, and no write
    intervenes before the selected consumer reads it.
    """

    next_projection = next(
        (
            candidate
            for candidate in projections
            if candidate.scan_id == producer_projection.scan_id + 1
        ),
        None,
    )
    if next_projection is None:
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            detail="consumer is due after scan wrap but the adjacent scan is unobserved",
            execution_projection=producer_projection,
        )
    if (
        producer_projection.final_write(obligation.tag, obligation.value) is not appeared
        or producer_projection.exit_tags.get(obligation.tag) != obligation.value
        or next_projection.entry_tags.get(obligation.tag) != obligation.value
    ):
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            detail="cross-scan handoff continuity is not exact",
            execution_projection=next_projection,
        )

    consumer_runs = tuple(
        run
        for run in next_projection.runs
        if run.rung is obligation.consumer_rung
        and _wrapped_runs_share_selected_transaction(
            obligation,
            appeared,
            next_projection,
            run,
        )
    )
    if len(consumer_runs) != 1:
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            detail="selected adjacent-scan consumer occurrence is unavailable or ambiguous",
            execution_projection=next_projection,
        )

    consumer_run = consumer_runs[0]
    reads = next_projection.reads_for_run(consumer_run)
    effect_read = next(
        (
            read
            for read in reads
            if read.occurrence.name == obligation.tag and read.occurrence.value == obligation.value
        ),
        None,
    )
    consumer_boundary = (
        effect_read.ordinal
        if effect_read is not None
        else max((read.ordinal for read in reads), default=-1) + 1
    )
    displacement = next(
        (
            write
            for write in next_projection.writes
            if write.run.enabled
            and write.transition.tag_name == obligation.tag
            and write.ordinal < consumer_boundary
        ),
        None,
    )
    if displacement is not None:
        return EffectObservation(
            obligation,
            "OVERWRITTEN",
            appeared=appeared,
            consumer_read=effect_read,
            displacement=displacement,
            observed_reads=next_projection.enabling_reads_observed_by_write(displacement),
            detail="an adjacent-scan write replaced the pending handoff",
            execution_projection=next_projection,
        )
    if effect_read is not None and effect_read.occurrence.source != "entry":
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            consumer_read=effect_read,
            observed_reads=next_projection.reads_for_run(consumer_run),
            detail="adjacent-scan consumer read lacks exact entry continuity",
            execution_projection=next_projection,
        )
    if effect_read is None:
        return EffectObservation(
            obligation,
            "STRANDED",
            appeared=appeared,
            observed_reads=reads,
            detail="adjacent-scan consumer did not read the pending handoff",
            execution_projection=next_projection,
        )

    next_ordinal = -1
    matched: list[RungRead] = []
    for required_tag, required_value in obligation.required_shape:
        observed = next(
            (
                read
                for read in reads
                if read.occurrence.name == required_tag and read.ordinal > next_ordinal
            ),
            None,
        )
        if observed is None:
            return EffectObservation(
                obligation,
                "UNKNOWN",
                appeared=appeared,
                consumer_read=effect_read,
                observed_reads=reads,
                detail=f"required consumer read {required_tag!r} did not occur",
                execution_projection=next_projection,
            )
        matched.append(observed)
        next_ordinal = observed.ordinal
        if observed.occurrence.value != required_value:
            return EffectObservation(
                obligation,
                "STRANDED" if not effect_read.run.enabled else "UNKNOWN",
                appeared=appeared,
                consumer_read=effect_read,
                displaced_read=observed,
                observed_reads=reads,
                detail="adjacent-scan consumer required shape did not hold",
                execution_projection=next_projection,
            )
    if not effect_read.run.enabled:
        return EffectObservation(
            obligation,
            "STRANDED",
            appeared=appeared,
            consumer_read=effect_read,
            observed_reads=reads,
            detail="adjacent-scan consumer read the effect but its guard was false",
            execution_projection=next_projection,
        )
    return EffectObservation(
        obligation,
        "SURVIVED",
        appeared=appeared,
        consumer_read=effect_read,
        observed_reads=tuple(matched),
        execution_projection=next_projection,
    )


def _wrapped_runs_share_selected_transaction(
    obligation: EffectObligation,
    appeared: RungWrite,
    projection: ScanRungWriteProjection,
    consumer_run: Any,
) -> bool:
    """Match dynamic call identity across adjacent scan invocations."""

    producer_sub, producer_rung, producer_branch = obligation.producer
    assert obligation.consumer is not None
    consumer_sub, consumer_rung, consumer_branch = obligation.consumer
    same_subroutine = producer_sub is not None and producer_sub == consumer_sub
    same_branched_rung = (
        producer_sub == consumer_sub
        and producer_rung == consumer_rung
        and bool(producer_branch or consumer_branch)
    )
    if same_subroutine or same_branched_rung:
        if (
            appeared.run.caller_rung != consumer_run.caller_rung
            or appeared.run.call_stack != consumer_run.call_stack
        ):
            return False
    if same_subroutine:
        return appeared.call_invocation == projection._call_invocation_by_run.get(id(consumer_run))
    return True


@dataclass(frozen=True)
class _CertifiedOutProducer:
    authoritative: int | RungId
    shadow_root: int | None


def _certified_out_producer(
    obligation: EffectObligation,
    fork: Any,
    owners: tuple[Any, ...],
) -> _CertifiedOutProducer | None:
    """Return one value-aware OUT producer only when call ownership is exact."""

    subroutine, rung_index, branch_path = obligation.producer
    if branch_path or obligation.value is not True:
        return None
    program = fork._program
    if program is None:
        return None

    if subroutine is None:
        if not 0 <= rung_index < len(program.rungs):
            return None
        rung = program.rungs[rung_index]
    else:
        subroutine_rungs = program.subroutines.get(subroutine)
        if subroutine_rungs is None or not 0 <= rung_index < len(subroutine_rungs):
            return None
        rung = subroutine_rungs[rung_index]
    if rung is not obligation.producer_rung or rung._branches:
        return None
    if any(isinstance(item, CallInstruction | ForLoopInstruction) for item in rung._instructions):
        return None

    matching_instructions: list[Any] = []
    for instruction in rung._instructions:
        for target in instruction_write_targets(instruction):
            names = static_write_target_names(target)
            if not names:
                return None
            if obligation.tag in names:
                matching_instructions.append(instruction)
    if len(matching_instructions) != 1:
        return None
    producer = matching_instructions[0]
    if not isinstance(producer, OutInstruction) or producer._oneshot:
        return None

    if subroutine is None:
        if any(
            not owner.rung_firing_timelines.value_is_known(rung_index, obligation.tag)
            for owner in owners
        ):
            return None
        return _CertifiedOutProducer(rung_index, None)

    pdg = fork._ensure_pdg()
    matching_nodes = tuple(
        (node_index, node)
        for node_index, node in enumerate(pdg.rung_nodes)
        if (node.subroutine, node.rung_index, node.branch_path) == obligation.producer
    )
    if len(matching_nodes) != 1:
        return None
    producer_node_index, producer_node = matching_nodes[0]
    if (
        producer_node_index not in pdg.writers_of.get(obligation.tag, frozenset())
        or obligation.tag not in producer_node.ote_writes
    ):
        return None

    call_nodes = tuple(
        node
        for node in pdg.rung_nodes
        for called_subroutine in node.calls
        if called_subroutine == subroutine
    )
    if len(call_nodes) != 1:
        return None
    caller_node = call_nodes[0]
    if caller_node.subroutine is not None or caller_node.branch_path:
        return None
    caller_index = caller_node.rung_index
    if not 0 <= caller_index < len(program.rungs):
        return None
    caller_rung = program.rungs[caller_index]
    if caller_rung._conditions or caller_rung._branches or len(caller_rung._instructions) != 1:
        return None
    call_instruction = caller_rung._instructions[0]
    if (
        not isinstance(call_instruction, CallInstruction)
        or call_instruction.subroutine_name != subroutine
    ):
        return None

    node_key = RungId(subroutine, rung_index)
    if any(
        not owner.node_firing_timelines.value_is_known(node_key, obligation.tag)
        or not owner.rung_firing_timelines.value_is_known(caller_index, obligation.tag)
        for owner in owners
    ):
        return None
    return _CertifiedOutProducer(node_key, caller_index)


def _effect_projection_scan_ids(
    expectation: EffectExpectation,
    fork: Any,
    exact_scan_ids: Iterable[int],
    *,
    mandatory_scan_ids: Iterable[int],
) -> tuple[int, ...]:
    """Prefilter reconstruction without weakening exact scan ownership.

    Every carried ID is checked. Any missing owner or incomplete historical
    retention certificate disables pruning for the whole window.
    """

    exact = tuple(sorted(set(exact_scan_ids)))
    if not exact:
        return ()
    owners = tuple(fork._causal_lineage.owner_at(scan_id) for scan_id in exact)
    if any(owner is None for owner in owners):
        return exact
    exact_owners = tuple(owner for owner in owners if owner is not None)
    exact_set = set(exact)
    selected = {scan_id for scan_id in mandatory_scan_ids if scan_id in exact}
    for obligation in expectation.obligations:
        retention_complete = all(
            owner.firing_retained_tags is None or obligation.tag in owner.firing_retained_tags
            for owner in exact_owners
        )
        if not retention_complete:
            selected.update(exact)
            continue
        certified = _certified_out_producer(
            obligation,
            fork,
            exact_owners,
        )
        if certified is None:
            obligation_scans = {
                scan_id
                for scan_id, owner in zip(exact, exact_owners, strict=True)
                if owner.rung_firing_timelines.any_wrote_on(obligation.tag, scan_id)
                or owner.node_firing_timelines.any_wrote_on(obligation.tag, scan_id)
            }
            selected.update(obligation_scans)
            if obligation.consumer is not None:
                selected.update(
                    scan_id + 1 for scan_id in obligation_scans if scan_id + 1 in exact_set
                )
            continue

        appeared = False
        producer_scans: set[int] = set()
        for scan_id, owner in zip(exact, exact_owners, strict=True):
            if isinstance(certified.authoritative, int):
                authoritative = owner.rung_firing_timelines
            else:
                authoritative = owner.node_firing_timelines
            produced = (
                authoritative.value_at(
                    certified.authoritative,
                    obligation.tag,
                    scan_id,
                )
                is True
            )
            varied = authoritative.varied_on(
                certified.authoritative,
                obligation.tag,
                scan_id,
            )
            if produced:
                appeared = True
                producer_scans.add(scan_id)
                selected.add(scan_id)
                continue
            if varied:
                # Final value alone cannot say whether the promised value
                # appeared transiently; selected replay resolves the order.
                selected.add(scan_id)
                continue
            if appeared and (
                owner.rung_firing_timelines.any_wrote_on(
                    obligation.tag,
                    scan_id,
                    excluding=(
                        certified.authoritative
                        if isinstance(certified.authoritative, int)
                        else certified.shadow_root
                    ),
                )
                or owner.node_firing_timelines.any_wrote_on(
                    obligation.tag,
                    scan_id,
                    excluding=(
                        certified.authoritative
                        if isinstance(certified.authoritative, RungId)
                        else None
                    ),
                )
            ):
                selected.add(scan_id)
        if obligation.consumer is not None:
            selected.update(scan_id + 1 for scan_id in producer_scans if scan_id + 1 in exact_set)
    return tuple(sorted(selected))


def terminal_target_replay_scan_ids(
    expectation: EffectExpectation,
    fork: Any,
    exact_scan_ids: Iterable[int],
) -> tuple[int, ...]:
    """Nominate exact scans that may contain a zero-net target appearance.

    Cross-scan target arrival is a coast trigger and needs no reconstruction.
    This sparse index serves only the remaining case: a writer attempted the
    target value during one kernel scan but the scan landed elsewhere.  A
    matching final attempted value nominates the scan directly; ``varied`` is
    the compact evidence for one writer attempting unequal values (including
    target-then-reset) in that same scan.

    If causal retention cannot answer the question, fail closed by returning
    the full exact set.  Soundness is preserved while the normal retained-tag
    path avoids replaying constant non-target writes on every timer scan.
    """

    exact = tuple(sorted(set(exact_scan_ids)))
    if not exact:
        return ()

    lineage = fork._causal_lineage
    segments: list[tuple[Any, tuple[int, ...]]] = []
    consumed = 0
    for epoch, first_scan, last_scan in lineage.epochs_covering(exact[0], exact[-1]):
        lo = bisect_left(exact, first_scan, consumed)
        hi = bisect_right(exact, last_scan, lo)
        if lo != consumed:
            break
        if lo < hi:
            segments.append((lineage._query_for(epoch), exact[lo:hi]))
            consumed = hi
    if consumed != len(exact):
        return exact

    exact_owners = tuple(owner for owner, _owned_scans in segments)
    selected: set[int] = set()
    for obligation in expectation.obligations:
        if not obligation.terminal_target:
            continue
        retention_complete = all(
            owner.firing_retained_tags is None or obligation.tag in owner.firing_retained_tags
            for owner in exact_owners
        )
        if not retention_complete:
            selected.update(exact)
            continue
        subroutine, rung_index, _branch_path = obligation.producer
        if subroutine is None:
            timeline_name = "rung_firing_timelines"
            producer: int | RungId = rung_index
        else:
            timeline_name = "node_firing_timelines"
            producer = RungId(subroutine, rung_index)
        if any(
            not getattr(owner, timeline_name).value_is_known(producer, obligation.tag)
            for owner in exact_owners
        ):
            selected.update(exact)
            continue
        for owner, owned_scans in segments:
            timelines = getattr(owner, timeline_name)
            selected.update(
                timelines.value_or_varied_scans(
                    producer,
                    obligation.tag,
                    obligation.value,
                    owned_scans,
                )
            )
    return tuple(sorted(selected))


def observe_execution_window(
    expectation: EffectExpectation | None,
    fork: Any,
    *,
    scan_before: int,
    kernel_scan_ids: Iterable[int],
    action_scan: int | None = None,
    coast_receipt: Any = None,
    projection_at: Callable[[int], ScanRungWriteProjection | None] | None = None,
) -> tuple[EffectObservation, ...]:
    """Observe only exact scans owned by the executed act.

    Edge release is execution setup, not the selected producer occurrence. A
    matching release-scan write therefore cannot satisfy or erase the promise
    made by the assertion. Pulse/Batch observations begin at the assertion scan
    and include only later kernel scans explicitly retained by the execution
    session. Folded logical gaps remain unobserved rather than being replayed
    as a complete logical corridor.
    """

    if expectation is None:
        return ()
    project = projection_at or fork._replay_rung_write_projection_at
    if action_scan is not None:
        exact_scan_ids = {
            scan_id for scan_id in kernel_scan_ids if action_scan <= scan_id <= fork.state.scan_id
        }
        if action_scan not in exact_scan_ids:
            return _bind_execution_owner(
                _unknown_observations(
                    expectation,
                    "assertion scan is absent from the exact kernel scan stream",
                ),
                fork,
                fallback_scan=action_scan,
            )
        selected_scan_ids = _effect_projection_scan_ids(
            expectation,
            fork,
            exact_scan_ids,
            mandatory_scan_ids=(action_scan,),
        )
        projections_by_scan = tuple((scan_id, project(scan_id)) for scan_id in selected_scan_ids)
        if any(projection is None for _scan_id, projection in projections_by_scan):
            return _bind_execution_owner(
                _unknown_observations(
                    expectation,
                    "exact kernel scan projection is unavailable",
                ),
                fork,
                fallback_scan=action_scan,
            )
        projections = tuple(
            projection for _scan_id, projection in projections_by_scan if projection is not None
        )
        if projections:
            return _bind_execution_owner(
                observe_expectation(expectation, projections),
                fork,
                fallback_scan=projections[-1].scan_id,
            )
        return _bind_execution_owner(
            _unknown_observations(expectation, "assertion scan projection is unavailable"),
            fork,
            fallback_scan=action_scan,
        )

    landing_scan = coast_receipt.end_scan if coast_receipt is not None else fork.state.scan_id
    exact_scan_ids = {
        scan_id for scan_id in kernel_scan_ids if scan_before < scan_id <= fork.state.scan_id
    }
    mandatory_scan_ids = tuple(
        scan_id for scan_id in (landing_scan,) if scan_id in exact_scan_ids
    ) + ((min(exact_scan_ids),) if exact_scan_ids else ())
    selected_scan_ids = _effect_projection_scan_ids(
        expectation,
        fork,
        exact_scan_ids,
        mandatory_scan_ids=mandatory_scan_ids,
    )
    projections_by_scan = tuple((scan_id, project(scan_id)) for scan_id in selected_scan_ids)
    if any(projection is None for _scan_id, projection in projections_by_scan):
        return _bind_execution_owner(
            _unknown_observations(
                expectation,
                "exact kernel scan projection is unavailable",
            ),
            fork,
            fallback_scan=landing_scan,
        )
    projections = tuple(
        projection for _scan_id, projection in projections_by_scan if projection is not None
    )
    complete_single_scan = (
        coast_receipt is not None
        and coast_receipt.logical_scans == 1
        and coast_receipt.skipped_scans == 0
        and len(projections) == 1
        and projections[0].scan_id == landing_scan
    )
    if complete_single_scan:
        return _bind_execution_owner(
            observe_expectation(expectation, projections),
            fork,
            fallback_scan=landing_scan,
        )
    if not projections:
        return _bind_execution_owner(
            _unknown_observations(
                expectation,
                "coast has no exact recorded effect scan",
            ),
            fork,
            fallback_scan=landing_scan,
        )

    results: list[EffectObservation] = []
    for obligation in expectation.obligations:
        local_expectation = EffectExpectation((obligation,))
        observations = observe_expectation(local_expectation, projections)
        for observation in observations:
            if observation.disposition in {"OVERWRITTEN", "DISPLACED"}:
                results.append(observation)
            elif observation.disposition == "SURVIVED" and (
                obligation.consumer is not None
                or (
                    observation.appeared is not None
                    and observation.appeared.scan_id == landing_scan
                )
            ):
                results.append(observation)
            else:
                results.append(
                    EffectObservation(
                        obligation,
                        "UNKNOWN",
                        appeared=observation.appeared,
                        consumer_read=observation.consumer_read,
                        displacement=observation.displacement,
                        displaced_read=observation.displaced_read,
                        observed_reads=observation.observed_reads,
                        displacement_enabling_reads=(observation.displacement_enabling_reads),
                        detail="coast corridor contains unobserved or folded scans",
                    )
                )
    return _bind_execution_owner(tuple(results), fork, fallback_scan=landing_scan)


def _bind_execution_owner(
    observations: tuple[EffectObservation, ...],
    fork: Any,
    *,
    fallback_scan: int,
) -> tuple[EffectObservation, ...]:
    """Attach the immutable Epoch owner of each exact observation.

    A scan number is not an execution identity after forks and replay.  Sealing
    through the observation gives receipts one stable detached EpochQuery
    rather than the lineage's mutable live-query adapter. The physical Epoch
    is derived from that owner and is never stored separately.
    """

    observation_scans = tuple(
        tuple(
            occurrence.scan_id
            for occurrence in (
                observation.appeared,
                observation.consumer_read,
                observation.displacement,
                observation.displaced_read,
                *observation.observed_reads,
            )
            if occurrence is not None
        )
        for observation in observations
    )
    latest_scan = max(
        (scan for scans in observation_scans for scan in scans),
        default=fallback_scan,
    )
    sealed = fork._causal_lineage.seal_through(latest_scan)

    result: list[EffectObservation] = []
    for observation, scans in zip(observations, observation_scans, strict=True):
        scan = max(scans, default=fallback_scan)
        owned = next(
            (
                (epoch, owner)
                for epoch, owner in sealed
                if epoch.first_scan <= scan <= epoch.last_scan
            ),
            None,
        )
        if owned is None:
            result.append(
                replace(
                    observation,
                    disposition="UNKNOWN",
                    detail=(f"{observation.detail}; " if observation.detail else "")
                    + "exact execution epoch is unavailable",
                )
            )
            continue
        _epoch, owner = owned
        result.append(
            replace(
                observation,
                execution_owner=owner,
            )
        )
    return tuple(result)


def _unknown_observations(
    expectation: EffectExpectation,
    detail: str,
) -> tuple[EffectObservation, ...]:
    return tuple(
        EffectObservation(obligation, "UNKNOWN", detail=detail)
        for obligation in expectation.obligations
    )


def _from_ordered(
    obligation: EffectObligation,
    observation: OrderedEffectObservation,
    projection: ScanRungWriteProjection,
) -> EffectObservation:
    return EffectObservation(
        obligation=obligation,
        disposition=observation.disposition,
        appeared=observation.appeared,
        consumer_read=observation.consumer_read,
        displacement=observation.displacement,
        displaced_read=observation.displaced_read,
        observed_reads=observation.observed_reads,
        displacement_enabling_reads=observation.displacement_enabling_reads,
        detail=observation.detail,
        execution_projection=projection,
    )
