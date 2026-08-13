"""Name the next technician question from receipts an ordinary attempt already made.

This module combines facts owned elsewhere.  It does not execute a PLC, read
Compass, build a projection, monitor progress, or nominate a retry.  Missing or
conflicting receipts fail closed as :class:`AttemptInterpretationKind.UNRESOLVED`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pyrung.core.analysis.pilot.intrascan import IntrascanFinding, IntrascanResult
from pyrung.core.analysis.pilot.intrascan_schedule import iter_guard_alternatives
from pyrung.core.analysis.pilot.program_step import ProgramStep, ProgramStepStatus
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    FailedEffectReceipt,
    GuardRequirementAtom,
    GuardRequirementExpr,
    OperandAuthority,
    RequirementSourceWalkStatus,
)
from pyrung.core.analysis.pilot.types import AssessedMotion, TargetReached, _AcceptedTrial
from pyrung.core.analysis.pilot.world_key import _semantic_key


class AttemptInterpretationKind(StrEnum):
    """The five plain outcomes of reading one already-executed attempt."""

    KEEP_AND_REREAD = "keep_and_reread"
    COAST_TO_BOUNDARY = "coast_to_boundary"
    SETUP_FIRST = "setup_first"
    RETRY_TOGETHER = "retry_together"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class AttemptInterpretation:
    """Detached diagnosis with the exact receipt identities that support it."""

    kind: AttemptInterpretationKind
    reason: str
    supporting_identities: tuple[tuple[Any, ...], ...] = ()

    @property
    def opens_theory(self) -> bool:
        """Whether this diagnosis is actionable temporal work."""

        return self.kind in {
            AttemptInterpretationKind.SETUP_FIRST,
            AttemptInterpretationKind.RETRY_TOGETHER,
        }


def _program_step_identity(step: ProgramStep) -> tuple[Any, ...]:
    producer = step.producer
    return (
        "program-step",
        step.status.value,
        getattr(producer, "rung_index", None),
        getattr(producer, "command_tag", None),
        _semantic_key(getattr(producer, "command_value", None)),
        _semantic_key(step.boundary),
        step.channel,
        step.producer_observed,
        tuple(_semantic_key(change) for change in step.projected_changes),
    )


def _trial_identity(trial: _AcceptedTrial) -> tuple[Any, ...]:
    verification = trial.verification
    if isinstance(verification, TargetReached):
        outcome: tuple[Any, ...] = ("target-reached",)
    else:
        assert isinstance(verification, AssessedMotion)
        outcome = (
            "assessed-motion",
            verification.assessment.agency.value,
            verification.assessment.bearing.value,
            verification.assessment.progress.value,
            verification.assessment.new_frontier,
        )
    return (
        "accepted-trial",
        outcome,
        trial.earned_work_receipt.movement.value,
        tuple(_semantic_key(reading) for reading in trial.earned_work_receipt.readings),
    )


def _useful_landing(trial: _AcceptedTrial | None) -> bool:
    # VERIFY has already rejected sterile channel waits, unsafe regressions,
    # spins, dead ends, and failed expectations.  An _AcceptedTrial is the
    # existing admission receipt for a landing worth keeping through ordinary
    # post-commit monitoring; Stage 5 must not second-guess that judgment from
    # one narrower outcome axis.
    return trial is not None


def _guard_authorities(condition: Any) -> frozenset[OperandAuthority]:
    if isinstance(condition, GuardRequirementAtom):
        return frozenset((condition.operand_authority,))
    if isinstance(condition, GuardRequirementExpr):
        return frozenset(
            authority for term in condition.terms for authority in _guard_authorities(term)
        )
    return frozenset()


def _finding_identity(finding: IntrascanFinding) -> tuple[Any, ...]:
    snapshot = finding.diagnostic_snapshot()
    return ("intrascan-finding", _semantic_key(snapshot))


def _classify_finding(
    finding: IntrascanFinding,
    assertion_scan: int,
) -> AttemptInterpretationKind:
    requirement = finding.derivation.requirement
    if requirement is None:
        return AttemptInterpretationKind.UNRESOLVED
    # A selected value which reached its exact consumer before normal
    # program-owned cleanup does not authorize assignment of the cleaned-up
    # internal tag.  It is nevertheless exact proof that the producer belongs
    # in this scan and needs its remaining live transaction shape alongside
    # it.  Compass must rediscover that shape; this receipt alone never names
    # or executes a sibling.
    if (
        getattr(finding, "consumed_before_displacement", False)
        and requirement.deadline.scan_id == assertion_scan
    ):
        return AttemptInterpretationKind.RETRY_TOGETHER
    source_walk = finding.derivation.source_walk
    return _classify_requirement(
        requirement,
        finding.observation,
        assertion_scan,
        source_walk_incomplete=(
            source_walk is not None and source_walk.status is RequirementSourceWalkStatus.INCOMPLETE
        ),
        owner_bound=bool(finding.derivation.explanation.supporting_occurrences),
        prior_source=source_walk is not None and bool(source_walk.links),
    )


def _classify_requirement(
    requirement: ActiveRequirement,
    observation: Any,
    assertion_scan: int,
    *,
    source_walk_incomplete: bool = False,
    owner_bound: bool = False,
    prior_source: bool = False,
) -> AttemptInterpretationKind:
    """Classify one exact requirement, regardless of which observer found it."""

    if requirement.deadline.scan_id < assertion_scan:
        return AttemptInterpretationKind.SETUP_FIRST
    if requirement.deadline.scan_id > assertion_scan:
        # A retained look-ahead may discover a condition on a later scan than
        # the original assertion. With an exact owner-bound occurrence this is
        # actionable prior setup for the next productive edge, not ambiguity.
        return (
            AttemptInterpretationKind.SETUP_FIRST
            if owner_bound or prior_source
            else AttemptInterpretationKind.UNRESOLVED
        )
    if source_walk_incomplete:
        return AttemptInterpretationKind.UNRESOLVED

    authorities = _guard_authorities(requirement.condition) or frozenset(
        (requirement.operand_authority,)
    )
    exact_consumer_shape = observation.consumer_read is not None or (
        observation.disposition in {"OVERWRITTEN", "DISPLACED"}
        and observation.displacement is not None
    )
    if OperandAuthority.UNKNOWN in authorities or OperandAuthority.CONFIGURED in authorities:
        return AttemptInterpretationKind.UNRESOLVED
    if OperandAuthority.PROGRAM_WRITTEN in authorities:
        # An OR may expose a directly adjustable branch beside a program-owned
        # branch. The reader will yield those branches lazily, so the mere
        # presence of the program alternative must not suppress an executable
        # setup-first theory. AND/mixed atoms within one branch remain
        # unresolved here.
        if OperandAuthority.ADJUSTABLE in authorities:
            condition = requirement.condition
            if not isinstance(condition, (GuardRequirementAtom, GuardRequirementExpr)):
                return AttemptInterpretationKind.UNRESOLVED
            if any(
                alternative
                and all(
                    atom.operand_authority is OperandAuthority.ADJUSTABLE for atom in alternative
                )
                for alternative in iter_guard_alternatives(condition)
            ):
                if exact_consumer_shape or owner_bound or prior_source:
                    return AttemptInterpretationKind.RETRY_TOGETHER
                return (
                    AttemptInterpretationKind.SETUP_FIRST
                    if owner_bound or prior_source
                    else AttemptInterpretationKind.UNRESOLVED
                )
            return AttemptInterpretationKind.UNRESOLVED
        return (
            AttemptInterpretationKind.SETUP_FIRST
            if owner_bound or prior_source
            else AttemptInterpretationKind.UNRESOLVED
        )

    if authorities == frozenset((OperandAuthority.ADJUSTABLE,)) and exact_consumer_shape:
        return AttemptInterpretationKind.RETRY_TOGETHER
    return AttemptInterpretationKind.UNRESOLVED


def interpret_failed_requirements(
    *,
    exact_pairs: Sequence[tuple[ActiveRequirement, FailedEffectReceipt]],
    assertion_scan: int,
    landing_owns_tip: bool = True,
) -> AttemptInterpretation:
    """Interpret exact post-commit failures already produced by progress policy."""

    if not exact_pairs:
        return AttemptInterpretation(
            AttemptInterpretationKind.UNRESOLVED,
            "post-commit monitoring produced no exact failed requirement",
        )
    classified = tuple(
        (
            _classify_requirement(
                requirement,
                failed.observation,
                assertion_scan,
                owner_bound=bool(failed.explanation.supporting_occurrences),
            ),
            requirement,
            failed,
        )
        for requirement, failed in exact_pairs
    )
    kinds = {kind for kind, _requirement, _failed in classified}
    identities = tuple(
        (
            "post-commit-failed-requirement",
            _semantic_key(requirement.diagnostic_snapshot()),
            _semantic_key(failed.diagnostic_snapshot()),
        )
        for _kind, requirement, failed in classified
    )
    if AttemptInterpretationKind.UNRESOLVED in kinds or len(kinds) != 1:
        return AttemptInterpretation(
            AttemptInterpretationKind.UNRESOLVED,
            "the exact post-commit failures require conflicting temporal responses",
            identities,
        )
    kind = next(iter(kinds))
    reason = (
        "the missing condition must precede a scan whose landing lost the selected route"
        if kind is AttemptInterpretationKind.SETUP_FIRST and not landing_owns_tip
        else "the failed condition's deadline precedes the original pulse"
        if kind is AttemptInterpretationKind.SETUP_FIRST
        else "the original pulse and its missing exact consumer shape belong in one scan"
    )
    return AttemptInterpretation(kind, reason, identities)


def _interpret_intrascan(
    report: IntrascanResult | None,
    assertion_scan: int,
) -> AttemptInterpretation:
    if report is None:
        return AttemptInterpretation(
            AttemptInterpretationKind.UNRESOLVED,
            "the ordinary attempt produced no shared intrascan interpretation",
        )
    if not report.findings:
        return AttemptInterpretation(
            AttemptInterpretationKind.UNRESOLVED,
            "the recorded effects do not contain one exact actionable requirement",
            tuple(
                ("effect-observation", _semantic_key(item.diagnostic_snapshot()))
                for item in report.observations
            ),
        )

    classified = tuple(
        (_classify_finding(finding, assertion_scan), finding) for finding in report.findings
    )
    kinds = {kind for kind, _finding in classified}
    identities = tuple(_finding_identity(finding) for _kind, finding in classified)
    if AttemptInterpretationKind.UNRESOLVED in kinds or len(kinds) != 1:
        return AttemptInterpretation(
            AttemptInterpretationKind.UNRESOLVED,
            "the exact findings are incomplete or require conflicting temporal responses",
            identities,
        )

    kind = next(iter(kinds))
    reason = (
        "a program-owned condition must be established before retrying the pulse"
        if kind is AttemptInterpretationKind.SETUP_FIRST
        else "the original pulse and its missing exact consumer shape belong in one scan"
    )
    return AttemptInterpretation(kind, reason, identities)


def interpret_attempt(
    *,
    trial: _AcceptedTrial | None,
    program_step: ProgramStep | None,
    intrascan: IntrascanResult | None,
    assertion_scan: int,
) -> AttemptInterpretation:
    """Combine existing specialist receipts without producing new evidence."""

    intrascan_interpretation = _interpret_intrascan(intrascan, assertion_scan)
    if _useful_landing(trial):
        assert trial is not None
        # Acceptance says the landed world is worth committing and rereading;
        # it does not erase a more exact temporal receipt from the same owned
        # execution.  When the selected effect was displaced and intrascan has
        # already derived one actionable requirement, WorkingTheory must own
        # the source-bound setup/retry lifecycle before ordinary navigation is
        # allowed to continue from that provisional landing.
        if intrascan_interpretation.opens_theory:
            return intrascan_interpretation
        return AttemptInterpretation(
            AttemptInterpretationKind.KEEP_AND_REREAD,
            "ordinary verification accepted the landing for post-commit observation",
            (_trial_identity(trial),),
        )

    if program_step is not None and program_step.status is ProgramStepStatus.KEEP_RUNNING:
        if intrascan_interpretation.opens_theory:
            return AttemptInterpretation(
                AttemptInterpretationKind.UNRESOLVED,
                "program-owned continuation conflicts with an actionable intrascan finding",
                (
                    _program_step_identity(program_step),
                    *intrascan_interpretation.supporting_identities,
                ),
            )
        return AttemptInterpretation(
            AttemptInterpretationKind.COAST_TO_BOUNDARY,
            program_step.reason or "the selected instruction-owned operation is still advancing",
            (_program_step_identity(program_step),),
        )
    return intrascan_interpretation
