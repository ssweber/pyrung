"""A compatible same-tag deadline must trace through an earlier writer."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

from pyrung import PLC, Bool, Int, Program, copy, rung
from pyrung.core.analysis.pilot.effect_observation import observe_execution_window
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObligation,
    occurrence_snapshot,
    promote_terminal_target_observation,
)
from pyrung.core.analysis.pilot.execution import CheckpointRef
from pyrung.core.analysis.pilot.requirement_derivation import (
    _refine_preserved_tag_deadlines,
    derive_overwriter_guard_requirement_from_effect,
)
from pyrung.core.analysis.pilot.requirements import (
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
)
from pyrung.core.crossing import Cmp


def test_compatible_same_tag_deadline_refines_through_earlier_writer() -> None:
    """Compose the final rollback guard through the target's first displacement."""

    source_value = 80
    target_value = 81
    intermediate_value = 10
    state = Int("DeadlineCompositionState", default=source_value)
    release = Bool("DeadlineCompositionRelease", default=True, external=True)
    environment_healthy = Bool("DeadlineCompositionEnvironmentHealthy", readonly=True)

    with Program() as program:
        with rung():
            copy(target_value, state, oneshot=True)
        with rung(release, state == target_value):
            copy(intermediate_value, state, oneshot=True)
        with rung(~environment_healthy, state <= 20):
            copy(source_value, state, oneshot=True)

    plc = PLC(program)
    source = plc.fork()
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    assert [
        write.transition.to_value
        for write in projection.writes
        if write.transition.tag_name == state.name
    ] == [target_value, intermediate_value, source_value]

    obligation = EffectObligation(
        state.name,
        target_value,
        (None, 0, ()),
        None,
        (),
        terminal_target=True,
        producer_rung=program.rungs[0],
    )
    ordinary = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        kernel_scan_ids=(1,),
        action_scan=1,
    )[0]
    assert ordinary.displacement is not None
    assert ordinary.displacement.transition.to_value == intermediate_value

    promoted = promote_terminal_target_observation(
        (ordinary,),
        window_entry_value=source_value,
        final_landing_value=source_value,
    )
    assert promoted is not None and promoted.displacement is not None
    assert promoted.displacement.transition.to_value == source_value

    result = derive_overwriter_guard_requirement_from_effect(
        promoted,
        projection,
        execution_epoch=promoted.execution_epoch,
        execution_owner=promoted.execution_owner,
        selected_writer=obligation.producer,
        source_world_key=("deadline-composition-source",),
        source_checkpoint=SimpleNamespace(
            owner=SimpleNamespace(reference=CheckpointRef()),
            world=SimpleNamespace(work=source),
        ),
        preserved_values=((state.name, target_value),),
    )

    assert result.requirement is not None
    condition = result.requirement.condition
    assert isinstance(condition, GuardRequirementExpr)
    assert condition.logic is GuardLogic.ANY
    atoms = cast(tuple[GuardRequirementAtom, ...], condition.terms)
    assert [atom.condition for atom in atoms] == [
        Cmp(environment_healthy.name, "!=", False),
        Cmp(release.name, "!=", True),
    ]
    assert atoms[0].demanding_rung is program.rungs[2]
    assert atoms[1].demanding_rung is program.rungs[1]
    assert atoms[1].deadline.tag == release.name
    assert atoms[1].deadline.ordinal < atoms[0].deadline.ordinal


def test_same_tag_deadline_refinement_fails_closed_without_one_exact_crossing() -> None:
    state = Int("DeadlineControlState", default=30)
    keep = Bool("DeadlineControlKeep", default=True, external=True)
    marker = Int("DeadlineControlMarker")
    with Program() as program:
        with rung(keep, state == 30):
            copy(10, state, oneshot=True)
        with rung(state <= 20):
            copy(1, marker)

    plc = PLC(program)
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    deadline_reads = tuple(
        read
        for read in projection.reads
        if read.run.rung is program.rungs[1] and read.occurrence.name == state.name
    )
    assert len(deadline_reads) == 1
    deadline = occurrence_snapshot(deadline_reads[0])

    no_crossing = GuardRequirementAtom(
        Cmp(state.name, "!=", 5),
        (deadline,),
        deadline,
        (0,),
        demanding_rung=program.rungs[1],
    )
    assert (
        _refine_preserved_tag_deadlines(
            no_crossing,
            projection,
            ((state.name, 81),),
        )
        is no_crossing
    )

    incompatible = replace(no_crossing, condition=Cmp(state.name, ">", 20))
    assert (
        _refine_preserved_tag_deadlines(
            incompatible,
            projection,
            ((state.name, 10),),
        )
        is incompatible
    )

    crossing = replace(no_crossing, condition=Cmp(state.name, ">", 20))
    ambiguous = replace(projection, reads=(*projection.reads, deadline_reads[0]))
    assert (
        _refine_preserved_tag_deadlines(
            crossing,
            ambiguous,
            ((state.name, 81),),
        )
        is crossing
    )
