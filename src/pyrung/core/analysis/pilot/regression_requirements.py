"""Derive ordinary repair requirements from exact regression evidence.

This module joins a later causal departure to one accepted expectation receipt,
discovers and boundedly proves exact corrective evidence where necessary, and
adapts regression or excursion evidence into ordinary failed-effect requirement
contracts. It does not record WorkingTheory, restore a checkpoint, or handle
departures.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pyrung.core.analysis.pilot.correction_candidates as _candidates
from pyrung.core.analysis.pdg import _extract_reads_from_condition
from pyrung.core.analysis.pilot.advance import (
    build_advance_index,
    demand_holds,
    iter_advance_owners,
)
from pyrung.core.analysis.pilot.constrained_reachability import NoRoute
from pyrung.core.analysis.pilot.correction_records import _ConfirmedCorrection
from pyrung.core.analysis.pilot.corrections import derive_correction_hypotheses
from pyrung.core.analysis.pilot.effect_observation import write_replay_scan_ids
from pyrung.core.analysis.pilot.effects import EffectObservation, occurrence_snapshot
from pyrung.core.analysis.pilot.execution import execution_owner
from pyrung.core.analysis.pilot.incidents import DeviationIncident
from pyrung.core.analysis.pilot.intrascan_schedule import satisfying_values
from pyrung.core.analysis.pilot.investigation_replay import RegressionWitness
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _merged_pilot_rungs,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.requirement_derivation import (
    derive_advance_requirement_from_effect,
    derive_overwriter_guard_requirement_from_effect,
)
from pyrung.core.analysis.pilot.requirement_evidence import _bind_guard_derivation_authority
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    ExpectationReceipt,
    FailedEffectReceipt,
    OperandAuthority,
    RequirementPhase,
    RequirementStatus,
    classify_bound_operand_authority,
    classify_guard_operand_authority,
    match_expectation_receipt,
)
from pyrung.core.analysis.pilot.types import (
    _ExecutedAttempt,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.working_theory import (
    temporal_setup_configuration_tags,
    temporal_setup_rung_identities,
)
from pyrung.core.analysis.pilot.world import _CausalCheckpoint
from pyrung.core.analysis.pilot.world_key import _rung_identity, _semantic_key
from pyrung.core.analysis.simplified import Atom
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.crossing import Cmp, Eq

_MAX_TENTATIVE_PROOF_SCANS = 8


@dataclass(frozen=True)
class _ExactRegressionCorrection:
    holds: tuple[PilotRung, ...]
    done_tag: str
    obstruction: Any
    execution_owner: Any
    channel_tag: str | None
    departure_scan: int
    causal_spine: frozenset[str]
    hypothesis_kind: str


@dataclass(frozen=True)
class _TentativeRungProof:
    admitted: bool
    reason: str
    scans: tuple[int, ...] = ()


def _operation_requirement_condition(operation: Any) -> Cmp | None:
    """Normalize one owner-declared completion boundary to a scalar need."""

    boundary = operation.until
    if isinstance(boundary, Eq) and len(boundary.values) == 1:
        return Cmp(boundary.tag, "==", next(iter(boundary.values)))
    if isinstance(boundary, Cmp):
        return boundary
    if (
        not isinstance(boundary, Atom)
        or boundary.unsupported is not None
        or boundary.operand_scale != 1
        or boundary.operand_offset != 0
    ):
        return None
    if boundary.form in {"xic", "truthy"}:
        return Cmp(boundary.tag, "==", True)
    if boundary.form == "xio":
        return Cmp(boundary.tag, "==", False)
    op = {
        "eq": "==",
        "ne": "!=",
        "lt": "<",
        "le": "<=",
        "gt": ">",
        "ge": ">=",
    }.get(boundary.form)
    return (
        Cmp(
            boundary.tag,
            op,
            boundary.operand,
            bound_is_tag=boundary.operand_is_tag,
        )
        if op is not None
        else None
    )


def _ordinary_correction_order(
    corrections: tuple[_ExactRegressionCorrection, ...],
) -> tuple[_ExactRegressionCorrection, ...]:
    """Prefer one already-proved coordinated cut over its partial members.

    The hypothesis producer deliberately emits both minimal alternatives and
    an existing coordinated candidate when several independent faults fired.
    Bounded executor proof may select any candidate it actually proves.  If
    that tiny historical fork is unavailable, ordinary Working Theory should
    not arbitrarily choose the first partial member and repeat the same PackML
    action for every remaining member.  Reorder only when one candidate's
    assignments contain every other candidate's assignments; never synthesize
    a union between alternatives here.
    """

    if len(corrections) < 2:
        return corrections
    assignments = tuple(
        frozenset((hold.dest, _semantic_key(hold.value)) for hold in correction.holds)
        for correction in corrections
    )
    coordinated = tuple(
        index
        for index, candidate in enumerate(assignments)
        if candidate and all(other <= candidate for other in assignments)
    )
    if len(coordinated) != 1:
        return corrections
    selected = coordinated[0]
    return (
        corrections[selected],
        *(item for index, item in enumerate(corrections) if index != selected),
    )


def _hypothesis_source_obstruction(
    state: _PilotState,
    incident: DeviationIncident,
    source_tags: tuple[str, ...],
    hold_tags: frozenset[str],
) -> tuple[Any, Any] | None:
    """Bind a legacy hypothesis source to its first exact harmful write.

    Hypothesis derivation already orders causal sources before response-side
    suppressors. Preserve that ordering and bind its latest incident-local
    transition only when the executor journal contains one matching write.
    """

    for tag in source_tags:
        if tag in hold_tags:
            continue
        scan = _candidates._last_transition_scan(
            state.work,
            tag,
            incident.anchor_scan,
            incident.end_scan,
        )
        if scan is None:
            continue
        projection = state.work._replay_rung_write_projection_at(scan)
        if projection is None:
            continue
        writes = tuple(
            write
            for write in projection.writes
            if write.transition.tag_name == tag
            and not _values_match(write.transition.from_value, write.transition.to_value)
        )
        point = state.world.execution_at(scan)
        if len(writes) == 1 and point is not None:
            return writes[0], point.owner
    return None


def _owner_progress_obstruction(
    state: _PilotState,
    ctx: _PilotContext,
    owner: Any,
    completion: Any,
) -> tuple[Any, Any] | None:
    """Locate the exact occurrence which armed a later owner completion.

    An absence correction prevents the owner from starting; testing it at the
    later Done edge is already too late for a retentive fault upstream of that
    owner.  ``AdvanceProfile`` exposes the owner's observable progress demand,
    so walk only the contiguous recorded progress interval and bind its first
    transition back to the executor journal.
    """

    done = owner.profile.done
    if done is None or completion.scan_id <= state.work.history.oldest_scan_id:
        return None
    before_completion = state.work.history.at(completion.scan_id - 1)
    step = owner.profile.plan(
        Eq(done.name, frozenset((True,))),
        before_completion.tags,
    )
    progress = step.progress if step is not None else None
    if progress is None or progress.condition is None:
        return None
    scan = completion.scan_id - 1
    while scan >= state.work.history.oldest_scan_id and demand_holds(
        progress, state.work.history.at(scan).tags
    ):
        scan -= 1
    activation_scan = scan + 1
    if activation_scan >= completion.scan_id:
        return None
    projection = state.work._replay_rung_write_projection_at(activation_scan)
    if projection is None:
        return None
    progress_tags = _extract_reads_from_condition(progress.condition, ctx.pdg.tags)
    activations = tuple(
        write
        for write in projection.writes
        if write.transition.tag_name in progress_tags
        and not _values_match(write.transition.from_value, write.transition.to_value)
        and demand_holds(progress, state.work.history.at(activation_scan).tags)
    )
    point = state.world.execution_at(activation_scan)
    if len(activations) != 1 or point is None:
        return None
    return activations[0], point.owner


def _exact_regression_corrections(
    state: _PilotState,
    ctx: _PilotContext,
    incident: DeviationIncident,
    witness: RegressionWitness | None,
    *,
    progress_mark: tuple[tuple[str, Any], ...] = (),
) -> tuple[_ExactRegressionCorrection, ...]:
    """Find narrowly executable corrections for one exact Done occurrence.

    The completed owner may directly declare a reset operation, or a recorded
    absence root may name an external leaf upstream of that owner.  This pass
    only binds hypotheses to exact history and materializes their finite
    occurrence scope.  A separate bounded executor trial must still prove the
    tentative rung before Working Theory may compose it.
    """

    departure_scan = (
        witness.departure_scan
        if witness is not None and witness.departure_scan is not None
        else incident.end_scan
        if incident.channel_tag is not None
        and any(
            event.scan == incident.end_scan
            and any(
                tag == incident.channel_tag and not _values_match(before, after)
                for tag, before, after in event.transitions
            )
            for event in incident.timeline
        )
        else None
    )
    if departure_scan is None:
        return ()
    causal_spine = witness.causal_spine if witness is not None else frozenset()
    hypotheses, _absence = derive_correction_hypotheses(
        state.work,
        incident,
        ctx,
        incident_local_only=True,
        causal_spine=causal_spine,
    )
    if not hypotheses:
        # A retry loop can place its final watchdog completion in this incident
        # while its external reset leaf has remained steady since an older
        # epoch. Widen only the static/history derivation here; the exact owner
        # occurrence and unique latest-completion checks below still gate the
        # executable correction, and no counterfactual replay is performed.
        hypotheses, _absence = derive_correction_hypotheses(
            state.work,
            incident,
            ctx,
            causal_spine=causal_spine,
        )
    ranked = tuple(
        hypothesis
        for hypothesis in _candidates._rank_hypotheses(
            state.work,
            hypotheses,
            incident,
            primal_extra=_absence,
        )
        if hypothesis.holds
        and hypothesis.sources
        and (not causal_spine or hypothesis.sources[0] in causal_spine)
    )
    done_owners = {
        owner.profile.done.name: owner
        for owner in iter_advance_owners(
            ctx.program,
            getattr(state.work, "_harness", None),
        )
        if owner.profile.done is not None
    }
    exact: list[_ExactRegressionCorrection] = []
    for hypothesis in ranked:
        materialized = (
            tuple(hypothesis.holds)
            if all(isinstance(hold, PilotRung) for hold in hypothesis.holds)
            else _candidates._exploratory_correction_rungs(
                state.work,
                hypothesis.holds,
                incident,
                # A never-moved leaf may need to be established before the
                # exact EarnedWork coordinate becomes true.  Keep the channel
                # state as its finite lifetime and let the bounded executor
                # choose the latest safe installation scan.  A statically
                # complete producer envelope already names every conductive
                # reader context for its correction. Intersecting that guard
                # with the current EarnedWork coordinate would turn the same
                # Door/Lint rule into one semantic PilotRung per recipe step,
                # forcing another PackML action merely to rediscover it.
                # Only an unproved/local hypothesis needs that narrower
                # occurrence coordinate.
                (
                    ()
                    if hypothesis.kind == "absence-root" or hypothesis.producer_envelope
                    else progress_mark
                ),
                ctx,
            )
        )
        if not materialized or not all(isinstance(hold, PilotRung) for hold in materialized):
            continue
        holds = tuple(materialized)
        if hypothesis.kind == "liveness" and any(hold.operation is None for hold in holds):
            continue
        downstream = {
            tag
            for hold in holds
            for tag in {
                hold.dest,
                *ctx.pdg.downstream_slice(hold.dest, follow_calls=True),
            }
        }
        candidate_done_tags = (
            (hypothesis.sources[0],)
            if hypothesis.kind == "liveness"
            else tuple(
                name
                for name in done_owners
                if name in downstream and (not causal_spine or name in causal_spine)
            )
        )
        occurrences: list[tuple[str, Any, Any]] = []
        for done_tag in candidate_done_tags:
            completion_scans = tuple(
                event.scan
                for event in incident.timeline
                if event.scan <= departure_scan
                and any(
                    tag == done_tag and not _values_match(before, after) and bool(after)
                    for tag, before, after in event.transitions
                )
            )
            if len(completion_scans) != 1:
                continue
            projection = state.work._replay_rung_write_projection_at(completion_scans[0])
            if projection is None:
                continue
            completions = tuple(
                write
                for write in projection.writes
                if write.transition.tag_name == done_tag
                and not _values_match(write.transition.from_value, write.transition.to_value)
                and bool(write.transition.to_value)
            )
            if len(completions) != 1:
                continue
            point = state.world.execution_at(completions[0].scan_id)
            if point is not None:
                occurrences.append((done_tag, completions[0], point.owner))
        selected: tuple[str, Any, Any] | None = None
        if occurrences:
            latest_scan = max(completion.scan_id for _tag, completion, _owner in occurrences)
            latest = tuple(
                occurrence for occurrence in occurrences if occurrence[1].scan_id == latest_scan
            )
            if len(latest) == 1:
                selected = latest[0]
        if selected is None and hypothesis.kind not in {"liveness", "absence-root"}:
            direct = _hypothesis_source_obstruction(
                state,
                incident,
                hypothesis.sources,
                frozenset(hold.dest for hold in holds),
            )
            if direct is not None:
                obstruction, execution_owner = direct
                selected = (obstruction.transition.tag_name, obstruction, execution_owner)
        if selected is None:
            continue
        done_tag, completion, execution_owner = selected
        obstruction = completion
        if hypothesis.kind == "absence-root":
            progress_obstruction = _owner_progress_obstruction(
                state,
                ctx,
                done_owners[done_tag],
                completion,
            )
            if progress_obstruction is None:
                continue
            obstruction, execution_owner = progress_obstruction
        exact.append(
            _ExactRegressionCorrection(
                holds,
                done_tag,
                obstruction,
                execution_owner,
                incident.channel_tag,
                departure_scan,
                causal_spine,
                hypothesis.kind,
            )
        )
    return tuple(exact)


def _tentative_rung_prevents_completion(
    state: _PilotState,
    ctx: _PilotContext,
    incident: DeviationIncident,
    evidence: _ExactRegressionCorrection,
    source_checkpoint: _CausalCheckpoint,
) -> _TentativeRungProof:
    """Check only the exact occurrence a tentative correction claims to cut.

    This tiny historical fork is an admission check, not continuation proof.
    Normal execution owns target, avoid, side effects, and any next obstruction.
    An earlier valid transition is therefore acceptable, exactly as it would be
    after a technician installed the same rung before retrying the operation.
    """

    del ctx, incident
    source_scan = source_checkpoint.world.work.state.scan_id
    obstruction = evidence.obstruction
    horizon = obstruction.scan_id - source_scan
    if horizon <= 0 or horizon > _MAX_TENTATIVE_PROOF_SCANS:
        return _TentativeRungProof(False, "obstruction lies outside the bounded proof horizon")

    candidate = fork_with_pilot_rungs(
        source_checkpoint.world.work,
        _merged_pilot_rungs(evidence.holds, source_checkpoint.world.pilot_rungs),
        inherit_log=True,
    )
    effective: set[tuple[str, Any]] = set()
    scans: list[int] = []
    for _ in range(horizon):
        candidate.step()
        scan_id = candidate.state.scan_id
        scans.append(scan_id)
        projection = candidate._replay_pilot_rung_write_projection_at(scan_id)
        if projection is None:
            return _TentativeRungProof(
                False,
                "candidate executor projection is unavailable",
                tuple(scans),
            )
        effective.update(
            (hold.dest, hold.value)
            for hold in evidence.holds
            if _values_match(candidate.state.tags.get(hold.dest), hold.value)
        )
        if any(
            write.rung_id == obstruction.rung_id
            and write.transition.tag_name == obstruction.transition.tag_name
            and _values_match(
                write.transition.to_value,
                obstruction.transition.to_value,
            )
            for write in projection.writes
        ):
            return _TentativeRungProof(
                False,
                "the exact harmful occurrence still occurred",
                tuple(scans),
            )

    admitted = len(effective) == len({(hold.dest, hold.value) for hold in evidence.holds})
    return _TentativeRungProof(
        admitted,
        "tentative rung suppressed the exact harmful occurrence"
        if admitted
        else "tentative rung never established its proposed value",
        tuple(scans),
    )


def _exact_correction_requirement_from_regression(
    state: _PilotState,
    ctx: _PilotContext,
    incident: DeviationIncident,
    evidence: _ExactRegressionCorrection,
    source_checkpoint: _CausalCheckpoint,
    *,
    require_bounded_proof: bool = True,
) -> ActiveRequirement | None:
    """Bind one exact correction to a JIT or ordinary retry source."""

    refined_holds: list[PilotRung] = []
    active = tuple(getattr(state, "active_requirements", ()))
    for candidate in evidence.holds:
        constraints = tuple(
            requirement.condition
            for requirement in active
            if not requirement.corrective_pilot_rungs
            and requirement.status is RequirementStatus.ACTIVE
            and requirement.phase is RequirementPhase.STEADY
            and requirement.operand_authority is OperandAuthority.ADJUSTABLE
            and isinstance(requirement.condition, Cmp)
            and not requirement.condition.bound_is_tag
            and requirement.condition.tag == candidate.dest
        )
        if not constraints:
            refined_holds.append(candidate)
            continue
        tag = source_checkpoint.world.work._known_tags_by_name.get(candidate.dest)
        values = (
            satisfying_values(
                tag,
                constraints,
                dict(source_checkpoint.world.work.state.tags),
            )
            if tag is not None
            else ()
        )
        if not values:
            return None
        refined_holds.append(replace(candidate, value=values[0]))
    holds = tuple(refined_holds)
    evidence = replace(evidence, holds=holds)
    hold = holds[0]
    obstruction = evidence.obstruction
    done_tag = evidence.done_tag
    # The overlay's hold section runs after the user program.  The first fresh
    # scan installs the correction; the historical consumer may read it on the
    # following scan.
    horizon = obstruction.scan_id - source_checkpoint.world.work.state.scan_id
    if horizon <= 0 or (require_bounded_proof and horizon > _MAX_TENTATIVE_PROOF_SCANS):
        return None
    configured = frozenset(
        {
            *getattr(ctx, "configured_inputs", frozenset()),
            *source_checkpoint.configured_inputs,
        }
    )
    provisional = temporal_setup_configuration_tags(state.theory_state)
    authorities = tuple(
        classify_guard_operand_authority(
            candidate.dest,
            steerable=ctx.steerable,
            program_written=frozenset(ctx.pdg.writers_of),
            configured=configured - provisional,
        )
        for candidate in holds
    )
    if not authorities or any(
        authority is not OperandAuthority.ADJUSTABLE for authority in authorities
    ):
        return None
    for snapshot in (
        source_checkpoint.world.work.state.tags,
        incident.before_snap,
        incident.after_snap,
    ):
        continuation = _candidates._continuation_with_active_correction(
            holds,
            snapshot,
            ctx,
        )
        continuation_frontier = getattr(continuation, "frontier", ())
        if isinstance(continuation, NoRoute) or (
            continuation_frontier
            and _candidates._active_pilot_rungs_defeat_needed(
                holds,
                continuation_frontier,
                snapshot,
                ctx.pdg,
                ctx.program,
            )
        ):
            return None
    proof = (
        _tentative_rung_prevents_completion(
            state,
            ctx,
            incident,
            evidence,
            source_checkpoint,
        )
        if require_bounded_proof
        else _TentativeRungProof(
            True,
            "ordinary Working Theory execution owns validation",
        )
    )
    if not proof.admitted:
        return None
    occurrence = occurrence_snapshot(obstruction)
    operation = hold.operation
    if operation is None:
        condition = Cmp(hold.dest, "==", hold.value)
    else:
        condition = _operation_requirement_condition(operation)
        if condition is None:
            return None
    return ActiveRequirement(
        condition=condition,
        demanding_occurrence=occurrence,
        deadline=occurrence,
        selected_writer=(
            obstruction.rung_id.subroutine,
            obstruction.rung_id.rung_index,
            (),
        ),
        operand_authority=OperandAuthority.ADJUSTABLE,
        execution_owner=evidence.execution_owner,
        source_world_key=source_checkpoint.key,
        checkpoint_owner=source_checkpoint.owner,
        source_checkpoint=source_checkpoint,
        phase=RequirementPhase.STEADY,
        provenance="exact-regression-corrective",
        scope=(
            "exact-regression-corrective",
            evidence.hypothesis_kind,
            evidence.channel_tag,
            done_tag,
            obstruction.transition.tag_name,
            tuple((candidate.dest, candidate.value) for candidate in holds),
            (
                "tentative-execution",
                source_checkpoint.world.work.state.scan_id,
                proof.reason,
                proof.scans,
            ),
        ),
        obstruction_occurrence=occurrence,
        corrective_pilot_rungs=holds,
    )


def _confirmed_correction_requirement_from_excursion(
    state: _PilotState,
    ctx: _PilotContext,
    executed: _ExecutedAttempt,
    correction: _ConfirmedCorrection,
    source_checkpoint: _CausalCheckpoint,
    reverted: tuple[str, ...],
) -> ActiveRequirement | None:
    """Adapt one replay-confirmed excursion into an inert exact requirement.

    The excursion replay remains useful evidence that a proposed guard cut keeps
    the pulse-established value alive.  It does not authorize adopting that
    replay or installing its overlay.  Admission additionally requires the
    original execution to identify one exact first clobbering write and owner;
    ambiguity fails closed and ordinary navigation receives no correction.
    """

    holds = tuple(correction.pilot_rungs)
    if (
        len(reverted) != 1
        or not holds
        or not all(isinstance(hold, PilotRung) for hold in holds)
        or correction.identity != _candidates.correction_identity(holds)
    ):
        return None

    pulse = executed.pulse
    preserved_tag = reverted[0]
    desired = pulse.post_pulse_snap.get(preserved_tag)
    first_scan = None
    harmful: tuple[Any, ...] = ()
    exact_scan_ids = tuple(
        scan_id for scan_id in pulse.kernel_scan_ids if scan_id > pulse.scan_before
    )
    replay_scan_ids = write_replay_scan_ids(
        pulse.fork,
        exact_scan_ids,
        (preserved_tag,),
    )
    for scan_id in replay_scan_ids:
        projection = executed.projection_at(scan_id)
        if projection is None:
            continue
        writes = tuple(
            write
            for write in projection.writes
            if write.transition.tag_name == preserved_tag
            and _values_match(write.transition.from_value, desired)
            and not _values_match(write.transition.to_value, desired)
        )
        if writes:
            first_scan = scan_id
            harmful = writes
            break
    if first_scan is None:
        return None
    owner = execution_owner(pulse.fork, first_scan)
    if len(harmful) != 1 or owner is None:
        return None

    configured = frozenset(
        {
            *getattr(ctx, "configured_inputs", frozenset()),
            *source_checkpoint.configured_inputs,
        }
    )
    provisional = temporal_setup_configuration_tags(state.theory_state)
    if any(
        classify_guard_operand_authority(
            hold.dest,
            steerable=ctx.steerable,
            program_written=frozenset(ctx.pdg.writers_of),
            configured=configured - provisional,
        )
        is not OperandAuthority.ADJUSTABLE
        for hold in holds
    ):
        return None

    first = holds[0]
    operation = first.operation
    if operation is None:
        condition = Cmp(first.dest, "==", first.value)
    else:
        condition = _operation_requirement_condition(operation)
        if condition is None:
            return None
    obstruction = harmful[0]
    occurrence = occurrence_snapshot(obstruction)
    return ActiveRequirement(
        condition=condition,
        demanding_occurrence=occurrence,
        deadline=occurrence,
        selected_writer=(
            obstruction.rung_id.subroutine,
            obstruction.rung_id.rung_index,
            (),
        ),
        operand_authority=OperandAuthority.ADJUSTABLE,
        execution_owner=owner,
        source_world_key=source_checkpoint.key,
        checkpoint_owner=source_checkpoint.owner,
        source_checkpoint=source_checkpoint,
        phase=RequirementPhase.STEADY,
        provenance="exact-excursion-legacy-receipt",
        scope=(
            "excursion-replay-corrective",
            correction.identity,
            correction.sources,
            preserved_tag,
            first_scan,
        ),
        obstruction_occurrence=occurrence,
        corrective_pilot_rungs=holds,
    )


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
    if derivation.requirement is None:
        derivation = _bind_guard_derivation_authority(
            derive_overwriter_guard_requirement_from_effect(
                observation,
                projection,
                execution_owner=harmful_owner,
                selected_writer=obligation.producer,
                source_world_key=source_world_key,
                source_checkpoint=source_checkpoint,
                provenance="delayed-regression-overwriter",
            ),
            source_checkpoint,
            ctx,
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
    requirement = derivation.requirement
    return source_checkpoint, requirement, observation, failed
