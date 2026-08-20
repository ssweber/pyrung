"""Characterize Pilot's deliberately distinct occurrence identity modes."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from pyrung.core.analysis.pilot.conductivity import (
    _effect_occurrence_identity,
    _front_identity,
    _requirement_occurrence_identity,
    _stop_identity,
)
from pyrung.core.analysis.pilot.effects import (
    EffectOccurrenceSelector,
    EffectOccurrenceSnapshot,
)
from pyrung.core.analysis.pilot.execution import CheckpointRef
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    OperandAuthority,
    _scheduled_occurrence_identity,
)
from pyrung.core.analysis.pilot.theory_evidence import _theory_occurrence_identity
from pyrung.core.runner import EpochRef


def _occurrence(**changes: Any) -> EffectOccurrenceSnapshot:
    occurrence = EffectOccurrenceSnapshot(
        kind="read",
        ordinal=11,
        scan_id=7,
        run_order=3,
        call_invocation=2,
        rung=("Worker", 4),
        execution_kind="subroutine",
        caller_rung=9,
        call_stack=("Worker",),
        depth=1,
        enabled=True,
        tag="Ready",
        values=(True,),
        branch_path=(0,),
    )
    return replace(occurrence, **changes)


@pytest.mark.parametrize(
    "changes",
    (
        {"scan_id": 8},
        {"ordinal": 12},
        {"run_order": 4},
        {"call_invocation": 3},
        {"values": (False,)},
        {"enabled": False},
    ),
)
def test_historical_theory_occurrence_identity_retains_exact_execution_facts(
    changes: dict[str, Any],
) -> None:
    occurrence = _occurrence()

    assert _theory_occurrence_identity(occurrence) != _theory_occurrence_identity(
        replace(occurrence, **changes)
    )


def _active_requirement(
    occurrence: EffectOccurrenceSnapshot,
    *,
    epoch_ref: EpochRef,
    checkpoint_ref: CheckpointRef,
) -> ActiveRequirement:
    checkpoint_owner = SimpleNamespace(reference=checkpoint_ref)
    return ActiveRequirement(
        condition=("Ready", "==", True),  # type: ignore[arg-type]
        demanding_occurrence=occurrence,
        deadline=occurrence,
        selected_writer=("Worker", 4, (0,)),
        operand_authority=OperandAuthority.PROGRAM_WRITTEN,
        execution_owner=SimpleNamespace(epoch=SimpleNamespace(reference=epoch_ref)),
        source_world_key=("source",),
        checkpoint_owner=checkpoint_owner,
        source_checkpoint=SimpleNamespace(owner=checkpoint_owner),
    )


@pytest.mark.parametrize("changed_owner", ("epoch", "checkpoint"))
def test_scheduled_retry_identity_ignores_exact_receipt_owner(
    changed_owner: str,
) -> None:
    occurrence = _occurrence()
    original = _active_requirement(
        occurrence,
        epoch_ref=EpochRef(1),
        checkpoint_ref=CheckpointRef(1),
    )
    retry = _active_requirement(
        occurrence,
        epoch_ref=EpochRef(2 if changed_owner == "epoch" else 1),
        checkpoint_ref=CheckpointRef(2 if changed_owner == "checkpoint" else 1),
    )

    assert original.identity != retry.identity
    assert original.navigation_identity == retry.navigation_identity


@pytest.mark.parametrize(
    "changes",
    (
        {"scan_id": 8},
        {"values": (False,)},
        {"enabled": False},
    ),
)
def test_scheduled_retry_occurrence_ignores_observation_local_facts(
    changes: dict[str, Any],
) -> None:
    occurrence = _occurrence()

    assert _scheduled_occurrence_identity(occurrence) == _scheduled_occurrence_identity(
        replace(occurrence, **changes)
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"ordinal": 12},
        {"run_order": 4},
        {"call_invocation": 3},
    ),
)
def test_scheduled_retry_occurrence_retains_position_in_the_scheduled_scan(
    changes: dict[str, Any],
) -> None:
    occurrence = _occurrence()

    assert _scheduled_occurrence_identity(occurrence) != _scheduled_occurrence_identity(
        replace(occurrence, **changes)
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"scan_id": 8},
        {"ordinal": 12},
        {"run_order": 4},
        {"call_invocation": 3},
        {"values": (False,)},
        {"enabled": False},
    ),
)
def test_cross_attempt_stop_identity_uses_only_the_structural_writer(
    changes: dict[str, Any],
) -> None:
    occurrence = _occurrence(kind="write")

    assert _stop_identity(occurrence) == _stop_identity(replace(occurrence, **changes))


@pytest.mark.parametrize(
    "changes",
    (
        {"scan_id": 8},
        {"ordinal": 12},
        {"run_order": 4},
        {"values": (False,)},
        {"enabled": False},
    ),
)
def test_cross_attempt_read_identity_ignores_physical_position_and_observation(
    changes: dict[str, Any],
) -> None:
    occurrence = _occurrence()

    assert _effect_occurrence_identity(occurrence) == _effect_occurrence_identity(
        replace(occurrence, **changes)
    )


def test_cross_attempt_read_identity_retains_dynamic_call_invocation() -> None:
    occurrence = _occurrence()

    assert _effect_occurrence_identity(occurrence) != _effect_occurrence_identity(
        replace(occurrence, call_invocation=3)
    )
    assert _requirement_occurrence_identity(
        _theory_occurrence_identity(occurrence)
    ) == _effect_occurrence_identity(occurrence)


@pytest.mark.parametrize(
    "changes",
    (
        {"scan_id": 8},
        {"ordinal": 12},
        {"run_order": 4},
        {"call_invocation": 3},
        {"enabled": False},
    ),
)
def test_cross_attempt_front_identity_ignores_physical_occurrence_details(
    changes: dict[str, Any],
) -> None:
    occurrence = _occurrence(kind="write")
    flow = SimpleNamespace(appeared=occurrence, obligations=())
    changed = SimpleNamespace(appeared=replace(occurrence, **changes), obligations=())

    assert _front_identity(flow) == _front_identity(changed)


def test_cross_attempt_front_identity_retains_the_produced_value() -> None:
    occurrence = _occurrence(kind="write")
    flow = SimpleNamespace(appeared=occurrence, obligations=())
    changed = SimpleNamespace(appeared=replace(occurrence, values=(False,)), obligations=())

    assert _front_identity(flow) != _front_identity(changed)


def test_replay_selector_identity_remains_a_distinct_relocation_contract() -> None:
    selector = EffectOccurrenceSelector(
        kind="read",
        tag="Ready",
        static_address=("Worker", 4, (0,)),
        instruction_path=(1,),
        execution_kind="subroutine",
        caller_rung=9,
        call_stack=("Worker",),
        depth=1,
        call_invocation=2,
        access_index=0,
    )

    assert selector != replace(selector, instruction_path=(2,))
    assert selector != replace(selector, access_index=1)
    assert selector != replace(selector, call_invocation=3)
