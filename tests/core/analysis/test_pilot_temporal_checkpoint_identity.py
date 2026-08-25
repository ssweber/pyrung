"""Characterization of temporal-checkpoint admission and resolution identity."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from pyrsistent import pvector

import pyrung.core.analysis.pilot.theory_drive as theory_drive
from pyrung import PLC, Bool, Program, out, rung
from pyrung.core.analysis.pilot.effects import EffectOccurrenceSnapshot
from pyrung.core.analysis.pilot.execution import execution_owner
from pyrung.core.analysis.pilot.navigation_contracts import BearingObjective, TargetSpec
from pyrung.core.analysis.pilot.requirements import ActiveRequirement, OperandAuthority
from pyrung.core.analysis.pilot.theory_evidence import (
    _theory_boundary_from_checkpoint,
    _theory_requirement_snapshot,
)
from pyrung.core.analysis.pilot.working_theory import (
    TemporalNeedRequest,
    TheoryBoundaryIdentity,
    TheoryTemporalIntent,
    theory_boundary_overlay_delta,
)
from pyrung.core.analysis.pilot.world import _CausalCheckpoint, _CheckpointOwner, _World
from pyrung.core.crossing import Cmp


def _executed_work() -> PLC:
    source = Bool("TemporalCheckpointSource", external=True)
    result = Bool("TemporalCheckpointResult")
    with Program() as logic:
        with rung(source):
            out(result)
    work = PLC(logic)
    work.step()
    return work


def _checkpoint(
    work: PLC,
    *,
    owner: _CheckpointOwner | None = None,
    rungs: tuple[tuple[Any, ...], ...] = (),
) -> _CausalCheckpoint:
    return _CausalCheckpoint(
        key=(("state", "source"), rungs),
        world=_World(
            work=work,
            committed_acts=pvector(),
            best_trend=0,
            pilot_rungs=pvector(),
            dwell_scans=0,
        ),
        objective=BearingObjective(TargetSpec("TemporalCheckpointResult", True)),
        owner=owner or _CheckpointOwner(),
    )


def _request(source: TheoryBoundaryIdentity) -> TemporalNeedRequest:
    return TemporalNeedRequest(
        theory_id=("theory",),
        version_id=("version",),
        source=source,
        intent=TheoryTemporalIntent.SETUP_FIRST,
        trigger_attempt_id=("attempt",),
        trigger_act_identity=(("TemporalCheckpointSource", True),),
        requirements=(),
    )


def _select_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    source: TheoryBoundaryIdentity,
    checkpoints: list[_CausalCheckpoint],
) -> _CausalCheckpoint:
    # These tests isolate retained-checkpoint selection. WorkingTheory freshness
    # has its own contract tests and runs immediately before this selector in
    # production.
    monkeypatch.setattr(theory_drive, "assert_temporal_need_current", lambda *_args: None)
    state = SimpleNamespace(
        theory_state=object(),
        expectation_receipts=(),
        failed_effect_receipts=(),
        temporal_checkpoints=checkpoints,
    )
    return theory_drive._temporal_source_checkpoint(state, _request(source), ())


def test_same_checkpoint_owner_selects_its_refreshed_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _checkpoint(_executed_work())
    refreshed = replace(original, world=original.world.set(best_trend=7))
    boundary = _theory_boundary_from_checkpoint(original)

    assert refreshed.owner is original.owner
    assert refreshed.world is not original.world
    assert _theory_boundary_from_checkpoint(refreshed) == boundary
    assert _select_checkpoint(monkeypatch, boundary, [original, refreshed]) is refreshed


def test_distinct_checkpoint_owners_can_name_the_same_executed_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _checkpoint(_executed_work())
    second = replace(first, owner=_CheckpointOwner())
    boundary = _theory_boundary_from_checkpoint(first)

    assert second.owner is not first.owner
    assert second.owner.reference != first.owner.reference
    assert _theory_boundary_from_checkpoint(second) == boundary
    # Distinct CheckpointRefs both survive the resolver's owner projection;
    # matching is intentionally deterministic last-retained selection.
    assert _select_checkpoint(monkeypatch, boundary, [first, second]) is second


def test_same_scan_overlay_change_remains_an_exact_boundary_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_rung = ("hold", "Gate", True)
    added_rung = ("hold", "Permit", False)
    source = _checkpoint(_executed_work(), rungs=(base_rung,))
    overlaid = replace(
        source,
        key=(("state", "source"), (base_rung, added_rung)),
    )
    source_boundary = _theory_boundary_from_checkpoint(source)
    overlay_boundary = _theory_boundary_from_checkpoint(overlaid)

    assert source_boundary.scan_id == overlay_boundary.scan_id
    assert source_boundary.execution_ref == overlay_boundary.execution_ref
    assert source_boundary != overlay_boundary
    assert theory_boundary_overlay_delta(source_boundary, overlay_boundary) == (
        (added_rung,),
        (),
    )
    # One CheckpointRef denotes a refreshed value, not two selectable versions.
    # If its boundary changes, the older boundary is no longer executable.
    with pytest.raises(
        ValueError,
        match="temporal need has no retained executable source checkpoint",
    ):
        _select_checkpoint(monkeypatch, source_boundary, [source, overlaid])
    assert _select_checkpoint(monkeypatch, overlay_boundary, [source, overlaid]) is overlaid

    distinct_owner = replace(overlaid, owner=_CheckpointOwner())
    assert (
        _select_checkpoint(
            monkeypatch,
            source_boundary,
            [source, distinct_owner],
        )
        is source
    )


def test_rollback_reexecution_at_same_scan_is_a_new_epoch_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    earlier = _checkpoint(_executed_work())
    reexecuted = _checkpoint(_executed_work())
    earlier_boundary = _theory_boundary_from_checkpoint(earlier)
    reexecuted_boundary = _theory_boundary_from_checkpoint(reexecuted)

    assert earlier_boundary.world_key == reexecuted_boundary.world_key
    assert earlier_boundary.scan_id == reexecuted_boundary.scan_id
    assert earlier_boundary.execution_ref != reexecuted_boundary.execution_ref
    assert _select_checkpoint(monkeypatch, earlier_boundary, [earlier, reexecuted]) is earlier
    with pytest.raises(
        ValueError,
        match="temporal need has no retained executable source checkpoint",
    ):
        _select_checkpoint(monkeypatch, earlier_boundary, [reexecuted])


def _occurrence(kind: str, ordinal: int) -> EffectOccurrenceSnapshot:
    return EffectOccurrenceSnapshot(
        kind=kind,  # type: ignore[arg-type]
        ordinal=ordinal,
        scan_id=1,
        run_order=ordinal,
        call_invocation=None,
        rung=(None, 0),
        execution_kind="rung",
        caller_rung=0,
        call_stack=(),
        depth=0,
        enabled=True,
        tag="TemporalCheckpointSource",
        values=(True,),
        branch_path=(0,),
    )


def test_live_requirement_resolution_returns_authority_and_rejects_ambiguity() -> None:
    checkpoint = _checkpoint(_executed_work())
    owner = execution_owner(checkpoint.world.work, 1)
    assert owner is not None
    requirement = ActiveRequirement(
        condition=Cmp("TemporalCheckpointSource", "==", True),
        demanding_occurrence=_occurrence("read", 1),
        deadline=_occurrence("read", 2),
        selected_writer=(None, 0, ()),
        operand_authority=OperandAuthority.ADJUSTABLE,
        execution_owner=owner,
        source_world_key=checkpoint.key,
        checkpoint_owner=checkpoint.owner,
        source_checkpoint=checkpoint,
    )
    detached = _theory_requirement_snapshot(requirement)

    resolved = theory_drive._resolve_temporal_requirement_snapshots(
        SimpleNamespace(active_requirements=[requirement]),
        (detached,),
    )

    assert resolved == (requirement,)
    assert resolved[0] is requirement
    duplicate_live_object = replace(requirement)
    assert duplicate_live_object is not requirement
    with pytest.raises(ValueError, match="temporal need requirement is ambiguous"):
        theory_drive._resolve_temporal_requirement_snapshots(
            SimpleNamespace(active_requirements=[requirement, duplicate_live_object]),
            (detached,),
        )
