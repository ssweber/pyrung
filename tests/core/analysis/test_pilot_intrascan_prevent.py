"""Exact negative occurrence observation contracts for Stage 3."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pyrung import PLC, Bool, Int, Program, call, copy, rung, subroutine
from pyrung.core.analysis.pilot.effect_observation import observe_intrascan_expectation
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObligation,
    EffectPolarity,
    occurrence_selector,
)
from pyrung.core.analysis.pilot.intrascan import IntrascanQuestion, inspect_assertion_scan

RepeatedPermit = Bool("IntrascanRepeatedPermit", external=True, default=True)
RepeatedValue = Int("IntrascanRepeatedValue")


@subroutine("IntrascanRepeatedProducer", strict=False)
def repeated_producer() -> None:
    with rung(RepeatedPermit):
        copy(1, RepeatedValue)


with Program() as repeated_program:
    with rung():
        call(repeated_producer)
        call(repeated_producer)


def _projection(plc: PLC, scan_id: int = 1):
    projection = plc._replay_rung_write_projection_at(scan_id)
    assert projection is not None
    return projection


def _selected_prevention() -> tuple[EffectExpectation, Any]:
    observed = PLC(repeated_program)
    observed.step()
    projection = _projection(observed)
    writes = tuple(
        write
        for write in projection.writes
        if write.transition.tag_name == RepeatedValue.name and write.transition.to_value == 1
    )
    assert [write.call_invocation for write in writes] == [0, 1]
    selector = occurrence_selector(projection, writes[1])
    assert selector is not None
    obligation = EffectObligation(
        tag=RepeatedValue.name,
        value=1,
        producer=("IntrascanRepeatedProducer", 0, ()),
        consumer=None,
        required_shape=(),
        producer_rung=repeated_program.subroutines["IntrascanRepeatedProducer"][0],
        polarity=EffectPolarity.PREVENT,
        occurrence_selector=selector,
    )
    return EffectExpectation((obligation,)), selector


def test_prevent_observation_selects_one_exact_repeated_call_and_its_enabling_reads() -> None:
    expectation, selector = _selected_prevention()
    executed = PLC(repeated_program)
    executed.step()

    observations = observe_intrascan_expectation(expectation, _projection(executed))

    assert len(observations) == 1
    observation = observations[0]
    assert observation.disposition == "FIRED"
    assert observation.appeared is not None
    assert observation.appeared.call_invocation == 1
    assert occurrence_selector(_projection(executed), observation.appeared) == selector
    assert [read.occurrence.name for read in observation.observed_reads] == [RepeatedPermit.name]
    assert [read.call_invocation for read in observation.observed_reads] == [1]


def test_prevent_observation_proves_absence_only_in_the_exact_projected_scope() -> None:
    expectation, _selector = _selected_prevention()
    source = PLC(repeated_program)
    before = source.state
    source.patch({RepeatedPermit.name: False})
    source.step()

    observations = observe_intrascan_expectation(expectation, _projection(source))

    assert len(observations) == 1
    assert observations[0].disposition == "PREVENTED"
    assert observations[0].appeared is None
    assert before.scan_id == 0


@pytest.mark.parametrize("projection_case", ("unavailable", "wrong-scan"))
def test_prevent_observation_is_unknown_when_exact_projection_is_unavailable(
    projection_case: str,
) -> None:
    expectation, _selector = _selected_prevention()
    source = PLC(repeated_program)
    executed = source.fork()
    executed.step()
    wrong = PLC(repeated_program)
    wrong.step()
    wrong.step()
    wrong_projection = _projection(wrong, 2)
    checkpoint = SimpleNamespace(
        owner=object(),
        key=("intrascan-prevent-source",),
        world=SimpleNamespace(work=source),
    )
    project = (
        (lambda _scan_id: None)
        if projection_case == "unavailable"
        else (lambda _scan_id: wrong_projection)
    )
    question = IntrascanQuestion(
        expectation=expectation,
        execution=executed,
        assertion_scan=1,
        source_checkpoint=checkpoint,
        advance_index=None,
        operand_authorities={},
        steerable=frozenset({RepeatedPermit.name}),
        program_written=frozenset({RepeatedValue.name}),
        projection_at=project,
    )

    result = inspect_assertion_scan(question)

    assert len(result.observations) == 1
    assert result.observations[0].disposition == "UNKNOWN"
    assert result.findings == ()
