"""Derive delayed repair requirements from exact regression evidence.

This module joins a later causal departure to one accepted expectation receipt
and adapts that evidence into the ordinary failed-effect requirement contracts.
It does not choose a correction, restore a checkpoint, or handle departures.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pilot.advance import build_advance_index
from pyrung.core.analysis.pilot.effects import EffectObservation, occurrence_snapshot
from pyrung.core.analysis.pilot.investigation_replay import RegressionWitness
from pyrung.core.analysis.pilot.requirement_derivation import (
    derive_advance_requirement_from_effect,
    derive_overwriter_guard_requirement_from_effect,
)
from pyrung.core.analysis.pilot.requirements import (
    ExpectationReceipt,
    FailedEffectReceipt,
    classify_bound_operand_authority,
    match_expectation_receipt,
)
from pyrung.core.analysis.pilot.types import (
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.working_theory import temporal_setup_rung_identities
from pyrung.core.analysis.pilot.world import _CausalCheckpoint
from pyrung.core.analysis.pilot.world_key import _rung_identity
from pyrung.core.analysis.sp_values import _values_match


def _delayed_overwriter_fallback_allowed(observation: EffectObservation) -> bool:
    """Only a promoted global-target receipt may widen delayed recovery."""

    return observation.obligation.terminal_target


def _regression_expectation_source(
    state: _PilotState,
    witness: RegressionWitness | None,
) -> tuple[ExpectationReceipt, Any] | None:
    """Join an unfiltered exact causal link to one accepted expectation."""

    if witness is None:
        return None
    matches: list[tuple[ExpectationReceipt, Any]] = []
    for link in witness.receipt_links:
        if link.exact_write is None or link.execution_owner is None:
            continue
        receipt = match_expectation_receipt(
            state.expectation_receipts,
            occurrence=link.exact_write,
            execution_owner=link.execution_owner,
        )
        if receipt is not None:
            matches.append((receipt, link))
    return matches[0] if len(matches) == 1 else None


def _match_regression_expectation_receipt(
    state: _PilotState,
    witness: RegressionWitness | None,
) -> ExpectationReceipt | None:
    """Return only a unique exact accepted source; ambiguity fails closed."""

    source = _regression_expectation_source(state, witness)
    return source[0] if source is not None else None


def _delayed_requirement_from_regression(
    state: _PilotState,
    ctx: _PilotContext,
    witness: RegressionWitness | None,
    *,
    recovery_checkpoint: _CausalCheckpoint | None = None,
) -> tuple[_CausalCheckpoint, Any, EffectObservation, FailedEffectReceipt] | None:
    """Adapt a later exact regression into the ordinary failed-effect seam."""

    source = _regression_expectation_source(state, witness)
    if source is None or witness is None:
        return None
    receipt, source_link = source
    source_checkpoint = recovery_checkpoint or receipt.source_checkpoint
    source_world_key = source_checkpoint.key
    if source_world_key is None:
        return None
    producer_snapshot = occurrence_snapshot(source_link.exact_write)
    producer_indices = tuple(
        index
        for index, occurrence in enumerate(receipt.producer_occurrences)
        if occurrence == producer_snapshot
    )
    harmful = tuple(
        occurrence
        for occurrence in witness.cause
        if occurrence.tag == witness.channel_tag
        and occurrence.scan_id == witness.departure_scan
        and _values_match(occurrence.value, witness.departed)
        and occurrence.exact_write is not None
        and occurrence.execution_owner is not None
    )
    if len(producer_indices) != 1 or len(harmful) != 1:
        return None
    index = producer_indices[0]
    harmful_link = harmful[0]
    harmful_owner = harmful_link.execution_owner
    if harmful_owner is None:
        return None
    projection = harmful_link.execution_projection
    if projection is None or not any(
        write is harmful_link.exact_write for write in projection.writes
    ):
        return None
    obligation = receipt.expectation.obligations[index]
    observation = EffectObservation(
        obligation=obligation,
        disposition="DISPLACED",
        appeared=source_link.exact_write,
        displacement=harmful_link.exact_write,
        observed_reads=projection.enabling_reads_observed_by_write(harmful_link.exact_write),
        detail="accepted effect participated in a later exact regression cause",
        execution_owner=harmful_link.execution_owner,
        execution_projection=projection,
    )
    source_work = source_checkpoint.world.work
    source_tags = source_work.state.tags
    known = source_work._known_tags_by_name
    configured = getattr(source_checkpoint, "configured_inputs", None)
    if configured is None:
        # Lightweight unit-test checkpoints predate the immutable provenance
        # field; retain their exact manager-backed behavior as a safe fallback.
        overrides = source_work._input_overrides
        configured = frozenset((*overrides.forces, *overrides.pending_patches))
    else:
        configured = frozenset(configured)
    # A retained theory correction is configuration, but it is configuration
    # owned by this exact execution attempt. Preserve the receipt provenance so
    # newer evidence may refine it; external force/patch configuration remains
    # authoritative in classify_bound_operand_authority.
    temporal_owned = temporal_setup_rung_identities(state.theory_state)
    provisional = frozenset(
        rung.dest for rung in state.pilot_rungs if _rung_identity(rung) in temporal_owned
    ) | frozenset(
        tag
        for configuration in receipt.execution.applied_configurations
        for tag, _value in configuration.assignments
    )
    authorities = {
        read.occurrence.name: classify_bound_operand_authority(
            read.occurrence.name,
            source_value=source_tags.get(
                read.occurrence.name,
                getattr(known.get(read.occurrence.name), "default", None),
            ),
            declared_default=getattr(known.get(read.occurrence.name), "default", None),
            steerable=ctx.steerable,
            program_written=frozenset(ctx.pdg.writers_of),
            configured=configured,
            provisional=provisional,
        )
        for read in projection.reads
    }
    advance_index = build_advance_index(
        ctx.program,
        getattr(source_work, "_harness", None),
    )
    derivation = derive_advance_requirement_from_effect(
        advance_index,
        projection,
        observation,
        operand_authorities=authorities,
        execution_owner=harmful_owner,
        selected_writer=obligation.producer,
        source_world_key=source_world_key,
        source_checkpoint=source_checkpoint,
        provenance="delayed-regression",
    )
    if derivation.requirement is None and _delayed_overwriter_fallback_allowed(observation):
        derivation = derive_overwriter_guard_requirement_from_effect(
            observation,
            projection,
            execution_owner=harmful_owner,
            selected_writer=obligation.producer,
            source_world_key=source_world_key,
            source_checkpoint=source_checkpoint,
            provenance="delayed-regression-overwriter",
        )
    if derivation.requirement is None:
        return None
    failed = FailedEffectReceipt(
        explanation=derivation.explanation,
        observation=observation.diagnostic_snapshot(),
        selected_writer=obligation.producer,
        source_world_key=source_world_key,
        checkpoint_owner=source_checkpoint.owner,
        execution_owner=harmful_owner,
        source_checkpoint=source_checkpoint,
        act_identity=receipt.act_identity,
        local_act=receipt.local_act,
        local_bearing=receipt.local_bearing,
        expectation=receipt.expectation,
        expectation_role=receipt.expectation_role,
    )
    if not any(current.identity == failed.identity for current in state.failed_effect_receipts):
        state.failed_effect_receipts.append(failed)
    requirement = derivation.requirement
    if not any(
        current.navigation_identity == requirement.navigation_identity
        for current in state.active_requirements
    ):
        state.active_requirements.append(requirement)
    return source_checkpoint, requirement, observation, failed
