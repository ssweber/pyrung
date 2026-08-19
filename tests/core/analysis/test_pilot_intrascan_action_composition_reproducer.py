"""Minimal reproducer for a consumed but incomplete intrascan action."""

from pyrung import PLC, Bool, Int, Program, call, copy, reset, rung, subroutine
from pyrung.core.analysis.pilot.effect_observation import observe_expectation
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObligation,
)
from pyrung.core.analysis.pilot.pulse import _apply_pulse

StepperRequest = Bool("CompositionStepperRequest", external=True)
ProducerLatch = Int("CompositionProducerLatch")
ConsumerSelect = Bool("CompositionConsumerSelect", external=True)
ConsumerResult = Int("CompositionConsumerResult")


@subroutine("CompositionConsumer", strict=False)
def composition_consumer() -> None:
    with rung(ConsumerSelect):
        copy(1, ConsumerResult)
    with rung():
        copy(0, ProducerLatch)
        reset(StepperRequest)
        reset(ConsumerSelect)


with Program() as composition_program:
    with rung(StepperRequest):
        copy(1, ProducerLatch, oneshot=True)
    with rung(ProducerLatch == 1):
        call(composition_consumer)


UnmanagedInput = Bool("CompositionUnmanagedInput", external=True)
UnmanagedResult = Int("CompositionUnmanagedResult")

with Program() as unmanaged_program:
    with rung(UnmanagedInput):
        copy(1, UnmanagedResult)


def test_consumed_request_needs_a_same_scan_consumer_selection() -> None:
    request_only = PLC(composition_program)
    request_only.patch({StepperRequest.name: True})
    request_only.step()

    assert request_only.state.tags[ConsumerResult.name] == 0
    assert request_only.state.tags[ProducerLatch.name] == 0
    assert request_only.state.tags[StepperRequest.name] is False

    composed = PLC(composition_program)
    composed.patch(
        {
            StepperRequest.name: True,
            ConsumerSelect.name: True,
        }
    )
    composed.step()

    assert composed.state.tags[ConsumerResult.name] == 1
    assert composed.state.tags[ProducerLatch.name] == 0
    assert composed.state.tags[StepperRequest.name] is False
    assert composed.state.tags[ConsumerSelect.name] is False


def test_exact_consumer_distinguishes_handoff_from_scan_exit_survival() -> None:
    plc = PLC(composition_program)
    plc.patch({StepperRequest.name: True})
    plc.step()
    projection = plc._replay_pilot_rung_write_projection_at(plc.state.scan_id)
    assert projection is not None

    terminal = EffectObligation(
        ProducerLatch.name,
        1,
        (None, 0, ()),
        None,
        ((StepperRequest.name, True),),
        producer_rung=composition_program.rungs[0],
    )
    consumed = EffectObligation(
        ProducerLatch.name,
        1,
        (None, 0, ()),
        None,
        ((StepperRequest.name, True),),
        projected_consumer=True,
        producer_rung=composition_program.rungs[0],
    )

    terminal_observation = observe_expectation(
        EffectExpectation((terminal,)),
        (projection,),
    )[0]
    consumed_observation = observe_expectation(
        EffectExpectation((consumed,)),
        (projection,),
    )[0]

    assert terminal_observation.disposition == "OVERWRITTEN"
    assert terminal_observation.consumer_read is None
    assert terminal_observation.displacement is not None
    assert consumed_observation.disposition == "SURVIVED"
    assert consumed_observation.consumer_read is not None
    assert consumed_observation.consumer_read.run.rung is composition_program.rungs[1]
    assert consumed_observation.consumer_read.ordinal < terminal_observation.displacement.ordinal


def test_pulse_does_not_release_an_unmanaged_input_after_assertion() -> None:
    plc = PLC(unmanaged_program)

    _apply_pulse(
        plc,
        [(UnmanagedInput.name, True)],
        {UnmanagedInput.name: False},
        set(),
    )

    assert plc.state.tags[UnmanagedInput.name] is True
    assert plc.state.tags[UnmanagedResult.name] == 1
