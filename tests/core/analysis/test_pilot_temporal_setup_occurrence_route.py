"""Acceptance contract for occurrence-observed temporal setup steering."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import PLC
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.effects import occurrence_snapshot
from pyrung.core.analysis.pilot.execution import CheckpointRef
from pyrung.core.analysis.pilot.requirements import ActiveRequirement, OperandAuthority
from pyrung.core.analysis.pilot.verify import _observe_temporal_setup_occurrences
from pyrung.core.crossing import Cmp
from tests.fixtures import pilot_temporal_setup_occurrence_route as fixture


def _only_read(projection, *, rung, tag):
    reads = tuple(
        read
        for read in projection.reads
        if read.run.rung is rung and read.occurrence.name == tag.name
    )
    assert len(reads) == 1
    return reads[0]


def test_gate_restoration_is_proved_at_its_exact_short_circuit_read() -> None:
    # This is the second field-like attempt: reset reaches PRODUCTIVE while
    # Gate=False prevents the first fault and exposes the later displacement.
    source = PLC(fixture.logic)
    source.patch(
        {
            fixture.ResetCommand.name: True,
            fixture.GateAvailable.name: False,
        }
    )
    source.step()
    assert source.state.tags[fixture.SequenceState.name] == fixture.SOURCE
    source_projection = source._replay_pilot_rung_write_projection_at(1)
    assert source_projection is not None
    fault_rung = fixture.logic.rungs[3]
    gate_read = _only_read(
        source_projection,
        rung=fault_rung,
        tag=fixture.GateAvailable,
    )
    demanding_read = _only_read(
        source_projection,
        rung=fault_rung,
        tag=fixture.SequenceState,
    )

    owner = source._causal_lineage.owner_at(1)
    assert owner is not None
    checkpoint_owner = SimpleNamespace(reference=CheckpointRef())
    requirement = ActiveRequirement(
        condition=Cmp(fixture.GateAvailable.name, "!=", False),
        demanding_occurrence=occurrence_snapshot(demanding_read),
        deadline=occurrence_snapshot(gate_read),
        selected_writer=(None, 3, ()),
        operand_authority=OperandAuthority.ADJUSTABLE,
        execution_owner=owner,
        source_world_key=("occurrence-route-source",),
        checkpoint_owner=checkpoint_owner,
        source_checkpoint=SimpleNamespace(
            configured_inputs=frozenset(),
            owner=checkpoint_owner,
        ),
    )

    # At the restored source Gate is already true. One assertion scan changes
    # no endpoint tag, but the exact fault guard reads Gate=True and therefore
    # short-circuits before the historical SequenceState demanding read.
    candidate = PLC(fixture.logic)
    observed_names = {
        fixture.SequenceState.name,
        fixture.ResetCommand.name,
        fixture.GateAvailable.name,
    }
    before = {name: candidate.state.tags[name] for name in observed_names}
    candidate.step()
    assert {name: candidate.state.tags[name] for name in observed_names} == before
    candidate_projection = candidate._replay_pilot_rung_write_projection_at(1)
    assert candidate_projection is not None
    receipt = _observe_temporal_setup_occurrences(
        SimpleNamespace(
            assertion_scan=1,
            projection_at=lambda scan_id: candidate_projection if scan_id == 1 else None,
        ),
        (requirement,),
        ((fixture.GateAvailable.name, True),),
        SimpleNamespace(
            steerable=frozenset({fixture.GateAvailable.name}),
            pdg=build_program_graph(fixture.logic),
            configured_inputs=frozenset(),
        ),
    )

    assert receipt.requirements_observed is True
    assert receipt.consumed_actions == ((fixture.GateAvailable.name, True),)
    assert receipt.observations[0][0] == "satisfied"
    assert receipt.observations[0][4] == ((fixture.GateAvailable.name, (True,), (None, 3)),)

    unrelated = _observe_temporal_setup_occurrences(
        SimpleNamespace(
            assertion_scan=1,
            projection_at=lambda scan_id: candidate_projection if scan_id == 1 else None,
        ),
        (requirement,),
        ((fixture.ResetCommand.name, True),),
        SimpleNamespace(
            steerable=frozenset({fixture.GateAvailable.name, fixture.ResetCommand.name}),
            pdg=build_program_graph(fixture.logic),
            configured_inputs=frozenset(),
        ),
    )
    assert unrelated.requirements_observed is True
    assert unrelated.consumed_actions == ()
