"""Historical program-guard repair for active Pilot requirements.

This module owns the evidence join which rebases a false program-written guard
to its nearest retained harmful writer.  It does not execute the resulting
repair; WorkingTheory and Compass retain that responsibility.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.intrascan_schedule import iter_guard_alternatives
from pyrung.core.analysis.pilot.navigation_contracts import TargetSpec
from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs
from pyrung.core.analysis.pilot.requirement_derivation import (
    derive_overwriter_guard_requirement_from_write,
)
from pyrung.core.analysis.pilot.requirement_evidence import (
    _bind_guard_derivation_authority,
    _configured_input_names,
    _retain_active_requirement,
)
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    GuardRequirementAtom,
    GuardRequirementCondition,
    GuardRequirementExpr,
    OperandAuthority,
    RequirementStatus,
)
from pyrung.core.analysis.pilot.types import _PilotContext, _PilotState
from pyrung.core.analysis.pilot.world import _CausalCheckpoint
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _semantic_key
from pyrung.core.context import RungId
from pyrung.core.crossing import Cmp
from pyrung.core.instruction.advance import constraint_holds


def mandatory_guard_blocker(
    requirements: tuple[ActiveRequirement, ...],
    snapshot: Mapping[str, Any],
) -> GuardRequirementAtom | None:
    """Name one exact false program-owned guard for a proved landing overwrite."""

    def exhaustive(condition: GuardRequirementCondition) -> bool:
        if isinstance(condition, GuardRequirementAtom):
            return True
        return condition.exhaustive and all(exhaustive(term) for term in condition.terms)

    for requirement in requirements:
        if getattr(requirement, "status", RequirementStatus.ACTIVE) is not RequirementStatus.ACTIVE:
            continue
        condition = requirement.condition
        if not isinstance(condition, GuardRequirementAtom | GuardRequirementExpr):
            continue
        if not any(item and item[0] == "overwriter_guard" for item in requirement.scope):
            # A producer guard can become true through a still-untried sibling
            # action (for example a crossing DNF branch).  Only an observed
            # final landing overwrite proves the local act has reached this
            # mandatory condition and may safely decline here.
            continue
        if not exhaustive(condition):
            continue
        blockers: list[GuardRequirementAtom] = []
        for alternative in iter_guard_alternatives(condition):
            unsatisfied = tuple(
                atom
                for atom in alternative
                if constraint_holds(atom.condition, snapshot) is not True
            )
            if not unsatisfied:
                break
            if any(atom.permits_assignment for atom in unsatisfied):
                break
            blockers.append(unsatisfied[0])
        else:
            if blockers:
                return blockers[0]
    return None


def mandatory_guard_decline_reason(
    blocker: GuardRequirementAtom,
    snapshot: Mapping[str, Any],
    target: TargetSpec,
) -> str:
    """Describe an exact mandatory guard solely in terms of the machine."""

    condition = blocker.condition
    if isinstance(condition, Cmp):
        observed = (
            blocker.deadline.values[-1]
            if blocker.deadline.tag == condition.tag and blocker.deadline.values
            else snapshot.get(condition.tag)
        )
        bound = condition.bound if not condition.bound_is_tag else snapshot.get(condition.bound)
        needed = (
            f"{condition.tag} {condition.op} {condition.bound}={bound!r}"
            if condition.bound_is_tag
            else f"{condition.tag} {condition.op} {bound!r}"
        )
        return (
            f"The machine has {condition.tag}={observed!r}, but "
            f"{target.tag}={target.value!r} requires {needed}; "
            f"{condition.tag} is controlled by the program."
        )
    return (
        f"The machine cannot preserve {target.tag}={target.value!r} because its "
        "required program-controlled condition is false."
    )


def _program_guard_rebase_surfaces(
    state: _PilotState,
    ctx: _PilotContext,
) -> tuple[tuple[_CausalCheckpoint, Any, Any], ...]:
    """Join retained executable boundaries to their exact execution histories."""

    surfaces: list[tuple[_CausalCheckpoint, Any, Any]] = []
    checkpoints: list[_CausalCheckpoint] = [
        _CausalCheckpoint(
            key=checkpoint.key,
            world=checkpoint.world,
            objective=checkpoint.objective,
            configured_inputs=ctx.configured_inputs
            | _configured_input_names(checkpoint.world.work),
            owner=checkpoint.owner,
        )
        for checkpoint in state.checkpoints
    ]
    if state.invocation_checkpoint is not None:
        checkpoints.append(state.invocation_checkpoint)
    bootstrap = state.bootstrap_execution
    if bootstrap is not None:
        checkpoints.append(bootstrap.checkpoint)
        owner = bootstrap.execution.owner_at(bootstrap.scan_after)
        if owner is not None:
            surfaces.append((bootstrap.checkpoint, owner.epoch, owner))
    for receipt in (*state.expectation_receipts, *state.failed_effect_receipts):
        checkpoints.append(receipt.source_checkpoint)
        surfaces.append(
            (receipt.source_checkpoint, receipt.execution_epoch, receipt.execution_owner)
        )
    for requirement in state.active_requirements:
        checkpoints.append(requirement.source_checkpoint)
        surfaces.append(
            (
                requirement.source_checkpoint,
                requirement.execution_epoch,
                requirement.execution_owner,
            )
        )

    # The live lineage owns accepted program motion even when that motion had
    # no expectation receipt. Ordinary progress checkpoints retain the exact
    # executable boundaries on that same lineage.
    lineage = state.work._causal_lineage
    for epoch, owner in lineage.seal_through(state.work.state.scan_id):
        for checkpoint in checkpoints:
            if checkpoint.world.work.state.scan_id < epoch.first_scan:
                surfaces.append((checkpoint, epoch, owner))

    unique: list[tuple[_CausalCheckpoint, Any, Any]] = []
    identities: set[tuple[Any, Any]] = set()
    for checkpoint, epoch, owner in surfaces:
        identity = (checkpoint.owner.reference, epoch.reference)
        if identity in identities or getattr(owner, "epoch", None) is not epoch:
            continue
        identities.add(identity)
        unique.append((checkpoint, epoch, owner))
    return tuple(unique)


def _program_guard_transition_candidates(
    owner: Any,
    rung_ids: frozenset[RungId],
    tag: str,
    *,
    before_scan: int,
) -> tuple[int, ...]:
    """Use compressed firing columns to rank possible transitions newest first."""

    main = frozenset(rung.rung_index for rung in rung_ids if rung.subroutine is None)
    nested = frozenset(rung for rung in rung_ids if rung.subroutine is not None)
    candidates: set[int] = set()
    if main:
        candidates.update(
            owner.rung_firing_timelines.tag_transition_candidate_scans_before(
                main,
                tag,
                before_scan,
            )
        )
    if nested:
        candidates.update(
            owner.node_firing_timelines.tag_transition_candidate_scans_before(
                nested,
                tag,
                before_scan,
            )
        )
    return tuple(sorted(candidates, reverse=True))


def _preinvocation_program_guard_surfaces(
    state: _PilotState,
    ctx: _PilotContext,
    rung_ids: frozenset[RungId],
    tag: str,
    *,
    before_scan: int,
) -> tuple[tuple[_CausalCheckpoint, Any, Any], ...]:
    """Reconstruct exact retained boundaries for pre-drive transition candidates."""

    invocation = state.invocation_checkpoint
    if invocation is None:
        return ()
    invocation_work = invocation.world.work
    invocation_scan = invocation_work.state.scan_id
    surfaces: list[tuple[_CausalCheckpoint, Any, Any]] = []
    for epoch, owner in invocation_work._causal_lineage.seal_through(invocation_scan):
        bounded_before = min(before_scan, invocation_scan + 1, epoch.last_scan + 1)
        for candidate_scan in _program_guard_transition_candidates(
            owner,
            rung_ids,
            tag,
            before_scan=bounded_before,
        ):
            source_scan = candidate_scan - 1
            if source_scan < 0:
                continue
            try:
                source_work = fork_with_pilot_rungs(
                    invocation_work,
                    tuple(invocation.world.pilot_rungs),
                    scan_id=source_scan,
                )
            except KeyError:
                continue
            source_key = (
                _pilot_world_key(
                    dict(source_work.state.tags),
                    state.key_config,
                    invocation.world.pilot_rungs,
                    (),
                )
                if state.key_config is not None
                else None
            )
            surfaces.append(
                (
                    _CausalCheckpoint(
                        key=source_key,
                        world=invocation.world.set(work=source_work),
                        objective=invocation.objective,
                        configured_inputs=invocation.configured_inputs,
                    ),
                    epoch,
                    owner,
                )
            )
    return tuple(surfaces)


def _program_guard_rebase_requirement(
    blocker: GuardRequirementAtom,
    parent: ActiveRequirement,
    state: _PilotState,
    ctx: _PilotContext,
) -> ActiveRequirement | None:
    """Trace one false program guard back to the nearest exact harmful writer."""

    condition = blocker.condition
    if not isinstance(condition, Cmp):
        return None
    failed_snapshot = dict(parent.source_checkpoint.world.work.state.tags)
    if (
        constraint_holds(condition, failed_snapshot) is not False
        or condition.tag not in failed_snapshot
    ):
        return None

    writer_nodes = tuple(
        ctx.pdg.rung_nodes[index] for index in ctx.pdg.writers_of.get(condition.tag, ())
    )
    rung_ids = frozenset(RungId(node.subroutine, node.rung_index) for node in writer_nodes)
    if not rung_ids:
        return None

    failed_scan = parent.source_checkpoint.world.work.state.scan_id
    ranked: list[tuple[int, int, _CausalCheckpoint, Any, Any]] = []
    surfaces = (
        *_program_guard_rebase_surfaces(state, ctx),
        *_preinvocation_program_guard_surfaces(
            state,
            ctx,
            rung_ids,
            condition.tag,
            before_scan=failed_scan + 1,
        ),
    )
    for checkpoint, epoch, owner in surfaces:
        checkpoint_scan = checkpoint.world.work.state.scan_id
        if (
            checkpoint_scan >= failed_scan
            or constraint_holds(condition, checkpoint.world.work.state.tags) is not True
        ):
            continue
        before_scan = min(failed_scan + 1, epoch.last_scan + 1)
        for candidate_scan in _program_guard_transition_candidates(
            owner,
            rung_ids,
            condition.tag,
            before_scan=before_scan,
        ):
            if checkpoint_scan < candidate_scan:
                ranked.append((candidate_scan, checkpoint_scan, checkpoint, epoch, owner))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    seen_candidates: set[tuple[int, Any, Any]] = set()
    for candidate_scan, _checkpoint_scan, checkpoint, epoch, owner in ranked:
        candidate_identity = (
            candidate_scan,
            epoch.reference,
            checkpoint.owner.reference,
        )
        if candidate_identity in seen_candidates:
            continue
        seen_candidates.add(candidate_identity)
        projection = owner.rung_write_projection_at(candidate_scan)
        if projection is None or projection.scan_id != candidate_scan:
            continue
        crossings = []
        for write in projection.writes:
            if write.transition.tag_name != condition.tag:
                continue
            before = dict(projection.entry_tags)
            before[condition.tag] = write.transition.from_value
            after = dict(before)
            after[condition.tag] = write.transition.to_value
            if (
                constraint_holds(condition, before) is True
                and constraint_holds(condition, after) is False
            ):
                crossings.append(write)
        if len(crossings) != 1:
            continue
        displacement = crossings[0]
        exact_nodes = tuple(
            node
            for node in writer_nodes
            if RungId(node.subroutine, node.rung_index) == displacement.rung_id
            and resolve_rung(ctx.program, node) is displacement.run.rung
        )
        if len(exact_nodes) != 1:
            continue
        node = exact_nodes[0]
        selected_writer = (node.subroutine, node.rung_index, node.branch_path)
        derivation = _bind_guard_derivation_authority(
            derive_overwriter_guard_requirement_from_write(
                displacement,
                projection,
                execution_epoch=epoch,
                execution_owner=owner,
                selected_writer=selected_writer,
                source_world_key=checkpoint.key,
                source_checkpoint=checkpoint,
                provenance="program-guard-rebase",
                scope=(("program_guard_rebase", condition),),
            ),
            checkpoint,
            ctx,
        )
        if derivation.requirement is not None:
            return derivation.requirement
    return None


def derive_program_guard_rebases(
    state: _PilotState,
    ctx: _PilotContext,
) -> tuple[tuple[ActiveRequirement, ActiveRequirement], ...]:
    """Add history-backed adjustable facts without executing a repair."""

    added: list[tuple[ActiveRequirement, ActiveRequirement]] = []
    for parent in tuple(state.active_requirements):
        if parent.status is not RequirementStatus.ACTIVE:
            continue
        condition = parent.condition
        if not isinstance(condition, GuardRequirementAtom | GuardRequirementExpr):
            continue
        source_snapshot = dict(parent.source_checkpoint.world.work.state.tags)
        # Boolean DFS owns directly executable alternatives first. Rebasing a
        # program-written sibling before those alternatives have been read
        # changes an OR into an eager historical detour.
        if any(
            all(
                constraint_holds(atom.condition, source_snapshot) is True
                or (
                    constraint_holds(atom.condition, source_snapshot) is False
                    and atom.permits_assignment
                )
                for atom in alternative
            )
            for alternative in iter_guard_alternatives(condition)
        ):
            continue
        atoms = tuple(
            dict.fromkeys(
                atom for alternative in iter_guard_alternatives(condition) for atom in alternative
            )
        )
        parent_rebased = False
        for atom in atoms:
            if atom.operand_authority is not OperandAuthority.PROGRAM_WRITTEN:
                continue
            rebased = _program_guard_rebase_requirement(atom, parent, state, ctx)
            if rebased is not None and any(
                current.provenance == "program-guard-rebase"
                and current.source_checkpoint.owner is rebased.source_checkpoint.owner
                and _semantic_key(current.condition) == _semantic_key(rebased.condition)
                for current in state.active_requirements
            ):
                continue
            if _retain_active_requirement(state, rebased):
                assert rebased is not None
                added.append((parent, rebased))
                parent_rebased = True
        if parent_rebased:
            index = state.active_requirements.index(parent)
            state.active_requirements[index] = replace(
                parent,
                status=RequirementStatus.DISCHARGED,
            )
    return tuple(added)
