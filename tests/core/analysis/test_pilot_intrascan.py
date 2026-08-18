"""Report-only one-scan effect forensics over neutral program fixtures."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from pyrung import PLC, Bool, Int, Program, call, copy, reset, rung, subroutine
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.advance import build_advance_index
from pyrung.core.analysis.pilot.attempt_interpretation import (
    AttemptInterpretationKind,
    interpret_attempt,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObligation,
    observe_execution_window,
    observe_expectation,
)
from pyrung.core.analysis.pilot.intrascan import IntrascanQuestion, inspect_assertion_scan
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    Bearing,
    BearingObjective,
    Pulse,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.requirement_evidence import _derive_attempt_requirements
from pyrung.core.analysis.pilot.requirements import (
    GuardRequirementAtom,
    bind_guard_operand_authorities,
    derive_guard_requirement_from_effect,
    derive_overwriter_guard_requirement_from_effect,
)
from pyrung.core.analysis.pilot.types import _AttemptResult, _ExecutedAttempt, _PulseState
from pyrung.core.analysis.pilot.working_theory import TheoryState
from pyrung.core.crossing import Cmp


def test_assertion_scan_uses_an_occurrence_owned_by_the_execution() -> None:
    executed = _ExecutedAttempt(
        pulse=SimpleNamespace(
            action_scan=2,
            coast_receipt=SimpleNamespace(end_scan=2),
            kernel_scan_ids=(3,),
            fork=SimpleNamespace(state=SimpleNamespace(scan_id=3)),
        ),
        bearing=SimpleNamespace(),
    )

    assert executed.assertion_scan == 3


def _adapter_state() -> SimpleNamespace:
    """Minimal state surface required by the production receipt adapter."""

    return SimpleNamespace(
        failed_effect_receipts=[],
        active_requirements=[],
        theory_state=TheoryState(),
        pilot_rungs=(),
    )


def _obligation(
    program: Program,
    effect: Int,
    value: int,
    *,
    producer: int = 0,
    consumer: int | None = None,
    required_shape: tuple[tuple[str, Any], ...] | None = None,
) -> EffectObligation:
    return EffectObligation(
        effect.name,
        value,
        (None, producer, ()),
        (None, consumer, ()) if consumer is not None else None,
        required_shape
        if required_shape is not None
        else ((effect.name, value),)
        if consumer is not None
        else (),
        producer_rung=program.rungs[producer],
        consumer_rung=program.rungs[consumer] if consumer is not None else None,
    )


def _execute(program: Program) -> tuple[PLC, Any, Any]:
    plc = PLC(program)
    source = plc.fork()
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    return plc, source, projection


def _question(
    program: Program,
    plc: PLC,
    source: Any,
    expectation: EffectExpectation,
    *,
    steerable: frozenset[str] = frozenset(),
    projection_at: Any = None,
) -> IntrascanQuestion:
    graph = build_program_graph(program)
    checkpoint = SimpleNamespace(
        key=("intrascan-source", 0),
        owner=object(),
        world=SimpleNamespace(work=source),
    )
    return IntrascanQuestion(
        expectation=expectation,
        execution=plc,
        assertion_scan=1,
        source_checkpoint=checkpoint,
        advance_index=build_advance_index(program),
        operand_authorities={},
        steerable=steerable,
        program_written=frozenset(graph.writers_of),
        projection_at=projection_at,
    )


def _direct_window(expectation: EffectExpectation, plc: PLC, *, projection_at: Any = None):
    return observe_execution_window(
        expectation,
        plc,
        scan_before=0,
        kernel_scan_ids=(1,),
        action_scan=1,
        projection_at=projection_at,
    )


def _snapshots(observations: Any) -> tuple[Any, ...]:
    return tuple(observation.diagnostic_snapshot() for observation in observations)


def _direct_guard_derivation(question: IntrascanQuestion, observation: Any, projection: Any):
    derivation = derive_guard_requirement_from_effect(
        observation,
        projection,
        execution_epoch=observation.execution_epoch,
        execution_owner=observation.execution_owner,
        selected_writer=observation.obligation.producer,
        source_world_key=question.source_checkpoint.key,
        source_checkpoint=question.source_checkpoint,
        provenance="steer",
    )
    return bind_guard_operand_authorities(
        derivation.requirement,
        steerable=question.steerable,
        program_written=question.program_written,
        configured=question.configured_inputs,
    )


def test_report_matches_exact_overwrite_and_existing_requirement_derivation() -> None:
    overwrite_enabled = Bool("ProducerOverwriteEnabled", external=True, default=True)
    effect = Int("ProducerOverwriteValue")
    consumed = Int("ProducerOverwriteConsumed")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung(overwrite_enabled):
            copy(2, effect)
        with rung(effect == 1):
            copy(1, consumed)

    plc, source, projection = _execute(program)
    expectation = EffectExpectation((_obligation(program, effect, 1, consumer=2),))
    question = _question(
        program,
        plc,
        source,
        expectation,
        steerable=frozenset((overwrite_enabled.name,)),
    )
    direct = _direct_window(expectation, plc)
    result = inspect_assertion_scan(question)

    assert _snapshots(result.observations) == _snapshots(direct)
    assert [observation.disposition for observation in result.observations] == ["OVERWRITTEN"]
    assert len(result.findings) == 1
    direct_derivation = derive_overwriter_guard_requirement_from_effect(
        direct[0],
        projection,
        execution_epoch=direct[0].execution_epoch,
        execution_owner=direct[0].execution_owner,
        selected_writer=direct[0].obligation.producer,
        source_world_key=question.source_checkpoint.key,
        source_checkpoint=question.source_checkpoint,
        provenance="steer-overwriter",
    )
    direct_requirement = bind_guard_operand_authorities(
        direct_derivation.requirement,
        steerable=question.steerable,
        program_written=question.program_written,
        configured=question.configured_inputs,
    )
    assert direct_requirement is not None
    assert result.findings[0].derivation.requirement is not None
    assert (
        result.findings[0].derivation.requirement.diagnostic_snapshot()
        == direct_requirement.diagnostic_snapshot()
    )
    snapshot = result.diagnostic_snapshot()[0]
    assert snapshot.observation == direct[0].diagnostic_snapshot()
    assert snapshot.explanation == direct_derivation.explanation
    assert snapshot.requirement == direct_requirement.diagnostic_snapshot()
    assert snapshot.selected_writer == direct[0].obligation.producer
    assert snapshot.source_world_key == question.source_checkpoint.key
    assert snapshot.source_scan == 0
    assert snapshot.causal_identity == (
        id(direct[0].execution_epoch),
        id(direct[0].execution_owner),
        id(question.source_checkpoint.owner),
    )


def test_report_matches_displaced_consumer_shape() -> None:
    effect = Int("ProducerDisplacementValue")
    consumer_permit = Bool("ConsumerDisplacementPermit", default=True)
    consumed = Int("ConsumerDisplacementResult")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung():
            reset(consumer_permit)
        with rung(effect == 1, consumer_permit):
            copy(1, consumed)

    plc, source, _projection = _execute(program)
    expectation = EffectExpectation(
        (
            _obligation(
                program,
                effect,
                1,
                consumer=2,
                required_shape=((effect.name, 1), (consumer_permit.name, True)),
            ),
        )
    )
    direct = _direct_window(expectation, plc)
    result = inspect_assertion_scan(_question(program, plc, source, expectation))

    assert _snapshots(result.observations) == _snapshots(direct)
    assert [observation.disposition for observation in result.observations] == ["DISPLACED"]
    displaced = result.observations[0]
    assert displaced.displaced_read is not None
    assert displaced.displaced_read.occurrence.name == consumer_permit.name
    assert displaced.displacement is not None
    assert displaced.displacement.ordinal < displaced.displaced_read.ordinal


def test_absent_producer_keeps_earlier_guard_writer_and_matches_derivation() -> None:
    producer_enabled = Bool("ProducerAbsentEnabled", external=True, default=True)
    effect = Int("ProducerAbsentValue")
    with Program() as program:
        with rung():
            reset(producer_enabled)
        with rung(producer_enabled):
            copy(1, effect)

    plc, source, projection = _execute(program)
    expectation = EffectExpectation((_obligation(program, effect, 1, producer=1),))
    question = _question(
        program,
        plc,
        source,
        expectation,
        steerable=frozenset((producer_enabled.name,)),
    )
    direct = _direct_window(expectation, plc)
    result = inspect_assertion_scan(question)

    assert _snapshots(result.observations) == _snapshots(direct)
    assert [observation.disposition for observation in result.observations] == ["ABSENT"]
    assert len(result.findings) == 1
    guard_read = next(
        read for read in projection.reads if read.occurrence.name == producer_enabled.name
    )
    assert guard_read.occurrence.source is not None
    assert guard_read.occurrence.source is projection.writes[0].occurrence
    direct_requirement = _direct_guard_derivation(question, direct[0], projection)
    service_requirement = result.findings[0].derivation.requirement
    assert direct_requirement is not None and service_requirement is not None
    assert service_requirement.diagnostic_snapshot() == direct_requirement.diagnostic_snapshot()
    assert isinstance(service_requirement.condition, GuardRequirementAtom)
    assert service_requirement.condition.condition == Cmp(producer_enabled.name, "==", True)


def test_false_consumer_guard_matches_existing_requirement_derivation() -> None:
    consumer_enabled = Bool("ConsumerGuardEnabled", external=True)
    effect = Int("ConsumerGuardValue")
    consumed = Int("ConsumerGuardResult")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung(effect == 1, consumer_enabled):
            copy(1, consumed)

    plc, source, projection = _execute(program)
    expectation = EffectExpectation((_obligation(program, effect, 1, consumer=1),))
    question = _question(
        program,
        plc,
        source,
        expectation,
        steerable=frozenset((consumer_enabled.name,)),
    )
    direct = _direct_window(expectation, plc)
    result = inspect_assertion_scan(question)

    assert _snapshots(result.observations) == _snapshots(direct)
    assert [observation.disposition for observation in result.observations] == ["STRANDED"]
    assert len(result.findings) == 1
    direct_requirement = _direct_guard_derivation(question, direct[0], projection)
    service_requirement = result.findings[0].derivation.requirement
    assert direct_requirement is not None and service_requirement is not None
    assert service_requirement.diagnostic_snapshot() == direct_requirement.diagnostic_snapshot()
    assert isinstance(service_requirement.condition, GuardRequirementAtom)
    assert service_requirement.condition.condition == Cmp(consumer_enabled.name, "==", True)


def test_repeated_calls_keep_dynamic_identity_and_suppress_a_surviving_obligation() -> None:
    effect = Int("ProducerRepeatedValue")

    @subroutine("RepeatedProducer", strict=False)
    def producer() -> None:
        with rung():
            copy(1, effect)

    with Program() as program:
        with rung():
            call(producer)
            call(producer)

    plc, source, projection = _execute(program)
    producer_rung = program.subroutines["RepeatedProducer"][0]
    obligation = EffectObligation(
        effect.name,
        1,
        ("RepeatedProducer", 0, ()),
        None,
        (),
        producer_rung=producer_rung,
    )
    expectation = EffectExpectation((obligation,))
    direct_ordered = observe_expectation(expectation, (projection,))
    result = inspect_assertion_scan(_question(program, plc, source, expectation))

    service_snapshots = _snapshots(result.observations)
    assert service_snapshots == _snapshots(_direct_window(expectation, plc))
    assert tuple(replace(snapshot, execution_epoch=None) for snapshot in service_snapshots) == (
        _snapshots(direct_ordered)
    )
    assert [observation.disposition for observation in result.observations] == [
        "OVERWRITTEN",
        "SURVIVED",
    ]
    appeared = tuple(snapshot.appeared for snapshot in service_snapshots)
    assert appeared[0] is not None and appeared[1] is not None
    assert [item.call_invocation for item in appeared] == [0, 1]
    assert appeared[0].dynamic_address != appeared[1].dynamic_address
    assert result.findings == ()
    assert result.diagnostic_snapshot() == ()


def test_unavailable_assertion_projection_fails_closed() -> None:
    effect = Int("ProducerUnavailableValue")
    with Program() as program:
        with rung():
            copy(1, effect)

    plc, source, _projection = _execute(program)
    expectation = EffectExpectation((_obligation(program, effect, 1),))
    unavailable = lambda _scan_id: None
    direct = _direct_window(expectation, plc, projection_at=unavailable)
    result = inspect_assertion_scan(
        _question(program, plc, source, expectation, projection_at=unavailable)
    )

    assert _snapshots(result.observations) == _snapshots(direct)
    assert [observation.disposition for observation in result.observations] == ["UNKNOWN"]
    assert "projection is unavailable" in result.observations[0].detail
    assert result.findings == ()
    assert result.diagnostic_snapshot() == ()


def test_wrong_assertion_projection_fails_closed() -> None:
    effect = Int("ProducerWrongProjectionValue")
    with Program() as program:
        with rung():
            copy(1, effect)

    plc, source, _projection = _execute(program)
    plc.step()
    wrong_projection = plc._replay_rung_write_projection_at(2)
    assert wrong_projection is not None
    expectation = EffectExpectation((_obligation(program, effect, 1),))
    result = inspect_assertion_scan(
        _question(
            program,
            plc,
            source,
            expectation,
            projection_at=lambda _scan_id: wrong_projection,
        )
    )

    assert [observation.disposition for observation in result.observations] == ["UNKNOWN"]
    assert "projection is unavailable" in result.observations[0].detail
    assert result.findings == ()
    assert result.diagnostic_snapshot() == ()


def test_pilot_adapter_reuses_the_steer_projection_for_overwrite_evidence() -> None:
    overwrite_enabled = Bool("AdapterOverwriteEnabled", external=True, default=True)
    effect = Int("AdapterOverwriteValue")
    consumed = Int("AdapterOverwriteConsumed")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung(overwrite_enabled):
            copy(2, effect)
        with rung(effect == 1):
            copy(1, consumed)

    plc, source, _projection = _execute(program)
    expectation = EffectExpectation((_obligation(program, effect, 1, consumer=2),))
    steerable = frozenset((overwrite_enabled.name,))
    question = _question(program, plc, source, expectation, steerable=steerable)
    report = inspect_assertion_scan(question)
    assert len(report.findings) == 1
    expected = report.diagnostic_snapshot()[0]

    policy = ActPolicy(
        ActSource.TRACE,
        action_pairs=((overwrite_enabled.name, True),),
        applied=((overwrite_enabled.name, True),),
        expectation=expectation,
    )
    bearing = Bearing(
        ("adapter-overwrite",),
        Pulse(policy),
        BearingObjective(TargetSpec(consumed.name, 1)),
    )
    landing = dict(plc.state.tags)
    pulse = _PulseState(
        fork=plc,
        scan_before=0,
        action_scan=1,
        action_snap=landing,
        wait_snaps=(),
        post_pulse_snap=landing,
        post_pulse_key=("adapter-post-pulse",),
        snap=landing,
        key=("adapter-landing",),
        kernel_scan_ids=(1,),
    )
    observations = _direct_window(expectation, plc, projection_at=pulse.projection_at)
    executed = _ExecutedAttempt(
        pulse=pulse,
        bearing=bearing,
        effect_observations=observations,
    )
    state = _adapter_state()
    context = SimpleNamespace(
        program=program,
        pdg=build_program_graph(program),
        steerable=steerable,
    )

    report = _derive_attempt_requirements(
        _AttemptResult(trial=None, executed=executed),
        state,
        context,
        question.source_checkpoint,
    )

    assert pulse.projection_replay_count == 1
    interpretation = interpret_attempt(
        trial=None,
        program_step=None,
        intrascan=report,
        assertion_scan=executed.assertion_scan,
    )
    assert interpretation.kind is AttemptInterpretationKind.RETRY_TOGETHER
    assert pulse.projection_replay_count == 1

    assert len(state.failed_effect_receipts) == len(state.active_requirements) == 1
    receipt = state.failed_effect_receipts[0].diagnostic_snapshot()
    requirement = state.active_requirements[0].diagnostic_snapshot()
    assert receipt.explanation == expected.explanation
    assert receipt.observation == expected.observation
    assert receipt.selected_writer == expected.selected_writer
    assert receipt.source_world_key == expected.source_world_key
    assert receipt.source_scan == expected.source_scan
    assert receipt.causal_identity == expected.causal_identity
    assert requirement == expected.requirement

    replayed = _ExecutedAttempt(
        pulse=executed.pulse,
        bearing=bearing,
        effect_observations=tuple(
            replace(observation, execution_projection=None) for observation in observations
        ),
    )
    replayed_state = _adapter_state()

    _derive_attempt_requirements(
        _AttemptResult(trial=None, executed=replayed),
        replayed_state,
        context,
        question.source_checkpoint,
    )

    assert pulse.projection_replay_count == 1
    assert len(replayed_state.failed_effect_receipts) == 1
    assert len(replayed_state.active_requirements) == 1
    assert (
        replayed_state.failed_effect_receipts[0].diagnostic_snapshot()
        == state.failed_effect_receipts[0].diagnostic_snapshot()
    )


def test_pilot_adapter_matches_service_snapshots_order_and_dedupe() -> None:
    first_consumer_enabled = Bool("FirstConsumerEnabled", external=True)
    second_consumer_enabled = Bool("SecondConsumerEnabled", external=True)
    first_effect = Int("FirstProducerValue")
    second_effect = Int("SecondProducerValue")
    first_consumed = Int("FirstConsumerResult")
    second_consumed = Int("SecondConsumerResult")
    with Program() as program:
        with rung():
            copy(1, first_effect)
        with rung(first_effect == 1, first_consumer_enabled):
            copy(1, first_consumed)
        with rung():
            copy(1, second_effect)
        with rung(second_effect == 1, second_consumer_enabled):
            copy(1, second_consumed)

    plc, source, _projection = _execute(program)
    expectation = EffectExpectation(
        (
            _obligation(program, first_effect, 1, producer=0, consumer=1),
            _obligation(program, second_effect, 1, producer=2, consumer=3),
        )
    )
    steerable = frozenset((first_consumer_enabled.name, second_consumer_enabled.name))
    question = _question(program, plc, source, expectation, steerable=steerable)
    observations = _direct_window(expectation, plc)
    report = inspect_assertion_scan(question)
    report_snapshots = report.diagnostic_snapshot()
    assert [snapshot.observation.obligation.tag for snapshot in report_snapshots] == [
        first_effect.name,
        second_effect.name,
    ]

    policy = ActPolicy(
        ActSource.TRACE,
        action_pairs=((first_consumer_enabled.name, True),),
        applied=((first_consumer_enabled.name, True),),
        expectation=expectation,
    )
    bearing = Bearing(
        ("adapter-attempt",),
        Pulse(policy),
        BearingObjective(TargetSpec(first_consumed.name, 1)),
    )
    executed = _ExecutedAttempt(
        pulse=SimpleNamespace(action_scan=1, coast_receipt=None, fork=plc),
        bearing=bearing,
        effect_observations=observations,
    )
    attempt = _AttemptResult(trial=None, executed=executed)
    state = _adapter_state()
    context = SimpleNamespace(
        program=program,
        pdg=build_program_graph(program),
        steerable=steerable,
    )

    _derive_attempt_requirements(attempt, state, context, question.source_checkpoint)

    receipt_snapshots = tuple(
        receipt.diagnostic_snapshot() for receipt in state.failed_effect_receipts
    )
    requirement_snapshots = tuple(
        requirement.diagnostic_snapshot() for requirement in state.active_requirements
    )
    assert len(receipt_snapshots) == len(requirement_snapshots) == len(report_snapshots) == 2
    for receipt, requirement, finding in zip(
        receipt_snapshots,
        requirement_snapshots,
        report_snapshots,
        strict=True,
    ):
        assert receipt.explanation == finding.explanation
        assert receipt.observation == finding.observation
        assert receipt.selected_writer == finding.selected_writer
        assert receipt.source_world_key == finding.source_world_key
        assert receipt.source_scan == finding.source_scan
        assert receipt.causal_identity == finding.causal_identity
        assert receipt.act_identity == act_identity(bearing.act)
        assert requirement == finding.requirement

    _derive_attempt_requirements(attempt, state, context, question.source_checkpoint)

    assert (
        tuple(receipt.diagnostic_snapshot() for receipt in state.failed_effect_receipts)
        == receipt_snapshots
    )
    assert (
        tuple(requirement.diagnostic_snapshot() for requirement in state.active_requirements)
        == requirement_snapshots
    )
