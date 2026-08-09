"""Stage 5 combines existing receipts without doing new program work."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung.core.analysis.pilot.attempt_interpretation import (
    AttemptInterpretationKind,
    interpret_attempt,
)
from pyrung.core.analysis.pilot.intrascan import IntrascanResult
from pyrung.core.analysis.pilot.program_step import ProgramStepStatus
from pyrung.core.analysis.pilot.requirements import OperandAuthority


class _Finding:
    def __init__(
        self,
        *,
        deadline_scan: int,
        authority: OperandAuthority,
        consumer: bool,
        owner_bound: bool = False,
        label: str,
    ) -> None:
        requirement = SimpleNamespace(
            deadline=SimpleNamespace(scan_id=deadline_scan),
            condition=object(),
            operand_authority=authority,
        )
        self.derivation = SimpleNamespace(
            requirement=requirement,
            source_walk=None,
            explanation=SimpleNamespace(
                supporting_occurrences=(("owner",),) if owner_bound else (),
            ),
        )
        self.observation = SimpleNamespace(
            consumer_read=object() if consumer else None,
            disposition="STRANDED" if consumer else "ABSENT",
            displacement=None,
        )
        self._label = label

    def diagnostic_snapshot(self) -> tuple[str, str]:
        return ("finding", self._label)


def _report(*findings: Any) -> IntrascanResult:
    return IntrascanResult((), tuple(findings))


def test_prior_deadline_is_setup_first() -> None:
    result = interpret_attempt(
        trial=None,
        program_step=None,
        intrascan=_report(
            _Finding(
                deadline_scan=4,
                authority=OperandAuthority.ADJUSTABLE,
                consumer=False,
                label="prior",
            )
        ),
        assertion_scan=5,
    )

    assert result.kind is AttemptInterpretationKind.SETUP_FIRST
    assert result.opens_theory


def test_owner_bound_program_condition_is_setup_first() -> None:
    result = interpret_attempt(
        trial=None,
        program_step=None,
        intrascan=_report(
            _Finding(
                deadline_scan=5,
                authority=OperandAuthority.PROGRAM_WRITTEN,
                consumer=False,
                owner_bound=True,
                label="owned",
            )
        ),
        assertion_scan=5,
    )

    assert result.kind is AttemptInterpretationKind.SETUP_FIRST


def test_conflicting_exact_findings_fail_closed() -> None:
    result = interpret_attempt(
        trial=None,
        program_step=None,
        intrascan=_report(
            _Finding(
                deadline_scan=4,
                authority=OperandAuthority.ADJUSTABLE,
                consumer=False,
                label="prior",
            ),
            _Finding(
                deadline_scan=5,
                authority=OperandAuthority.ADJUSTABLE,
                consumer=True,
                label="same-scan",
            ),
        ),
        assertion_scan=5,
    )

    assert result.kind is AttemptInterpretationKind.UNRESOLVED
    assert "conflicting" in result.reason
    assert not result.opens_theory


def test_program_motion_and_same_scan_repair_conflict_fails_closed() -> None:
    program_step = SimpleNamespace(
        status=ProgramStepStatus.KEEP_RUNNING,
        producer=SimpleNamespace(rung_index=7, command_tag="Command", command_value=True),
        boundary=None,
        channel="Timer.Acc",
        producer_observed=True,
        projected_changes=(("Timer.Acc", 1, 2),),
        reason="the timer moved",
    )
    result = interpret_attempt(
        trial=None,
        program_step=program_step,
        intrascan=_report(
            _Finding(
                deadline_scan=5,
                authority=OperandAuthority.ADJUSTABLE,
                consumer=True,
                label="same-scan",
            )
        ),
        assertion_scan=5,
    )

    assert result.kind is AttemptInterpretationKind.UNRESOLVED
    assert "conflicts" in result.reason


def test_missing_shared_report_is_unresolved_without_fallback_work() -> None:
    result = interpret_attempt(
        trial=None,
        program_step=None,
        intrascan=None,
        assertion_scan=5,
    )

    assert result.kind is AttemptInterpretationKind.UNRESOLVED
    assert "no shared intrascan" in result.reason
