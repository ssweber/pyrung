"""Exact producer, consumer, and displacement occurrence boundaries."""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, copy, latch, rung
from pyrung.core.analysis.pilot.effects import (
    ConsumerBoundary,
    EffectObligation,
    EffectObservation,
    consumer_boundary_reached,
    consumer_stop_reached,
    displacement_consumer_read,
    occurrence_selector,
    occurrence_snapshot,
    resolve_occurrence_selector,
)


def _projection(plc: PLC):
    projection = plc._replay_rung_write_projection_at(plc.state.scan_id)
    assert projection is not None
    return projection


def _produce(program: Program, rung_index: int, tag: Int, value: int) -> EffectObligation:
    return EffectObligation(
        tag.name,
        value,
        (None, rung_index, ()),
        None,
        (),
        producer_rung=program.rungs[rung_index],
    )


def test_consumer_boundary_matches_the_exact_producer_sourced_read() -> None:
    request = Bool("ConsumerBoundaryRequest", external=True)
    step = Int("ConsumerBoundaryStep")
    seen = Bool("ConsumerBoundarySeen")
    with Program() as program:
        with rung(request):
            copy(41, step)
        with rung(step == 41):
            latch(seen)

    plc = PLC(program)
    plc.patch({request.name: True})
    plc.step()
    projection = _projection(plc)
    produced = next(write for write in projection.writes if write.transition.tag_name == step.name)
    consumed = next(
        read
        for read in projection.reads
        if read.occurrence.name == step.name
        and projection.transition_observed_by_read(read) is not None
    )
    producer_selector = occurrence_selector(projection, produced)
    consumer_selector = occurrence_selector(projection, consumed)
    assert producer_selector is not None
    assert consumer_selector is not None
    boundary = ConsumerBoundary(
        produced_occurrence=occurrence_snapshot(produced),
        consumer_occurrence=occurrence_snapshot(consumed),
        producer_selector=producer_selector,
        consumer_selector=consumer_selector,
        producer_scan_offset=1,
        consumer_scan_offset=1,
    )

    assert resolve_occurrence_selector(projection, producer_selector) is produced
    assert resolve_occurrence_selector(projection, consumer_selector) is consumed
    assert consumer_boundary_reached(
        boundary,
        source_scan=0,
        projection_at=lambda scan_id: projection if scan_id == 1 else None,
    )


def test_consumer_boundary_proves_one_retained_cross_scan_handoff() -> None:
    produce = Bool("CrossScanProduce", external=True)
    consume = Bool("CrossScanConsume", external=True)
    step = Int("CrossScanStep")
    seen = Bool("CrossScanSeen")
    with Program() as program:
        with rung(produce):
            copy(41, step)
        with rung(consume, step == 41):
            latch(seen)

    plc = PLC(program)
    plc.patch({produce.name: True, consume.name: False})
    plc.step()
    produced_projection = _projection(plc)
    produced = next(
        write for write in produced_projection.writes if write.transition.tag_name == step.name
    )
    plc.patch({produce.name: False, consume.name: True})
    plc.step()
    consumer_projection = _projection(plc)
    consumed = next(read for read in consumer_projection.reads if read.occurrence.name == step.name)
    producer_selector = occurrence_selector(produced_projection, produced)
    consumer_selector = occurrence_selector(consumer_projection, consumed)
    assert producer_selector is not None
    assert consumer_selector is not None
    boundary = ConsumerBoundary(
        produced_occurrence=occurrence_snapshot(produced),
        consumer_occurrence=occurrence_snapshot(consumed),
        producer_selector=producer_selector,
        consumer_selector=consumer_selector,
        producer_scan_offset=1,
        consumer_scan_offset=2,
    )
    projections = {
        produced_projection.scan_id: produced_projection,
        consumer_projection.scan_id: consumer_projection,
    }

    assert consumer_boundary_reached(
        boundary,
        source_scan=0,
        projection_at=projections.get,
    )

    replay = PLC(program)
    replay.step()
    replay.patch({step.name: 50, consume.name: True})
    replay.step()
    changed_consumer_projection = _projection(replay)
    changed_projections = {changed_consumer_projection.scan_id: changed_consumer_projection}

    assert consumer_stop_reached(
        boundary,
        source_scan=0,
        projection_at=changed_projections.get,
    )
    assert not consumer_boundary_reached(
        boundary,
        source_scan=0,
        projection_at=changed_projections.get,
    )


def test_displacement_guard_names_one_cross_scan_consumer() -> None:
    produce = Bool("DisplacementProduce", external=True)
    displace = Bool("DisplacementEnable", external=True)
    step = Int("DisplacementStep")
    with Program() as program:
        with rung(produce):
            copy(41, step)
        with rung(displace, step == 41):
            copy(94, step)

    plc = PLC(program)
    plc.patch({produce.name: True, displace.name: False})
    plc.step()
    produced_projection = _projection(plc)
    produced = next(
        write for write in produced_projection.writes if write.transition.tag_name == step.name
    )
    plc.patch({produce.name: False, displace.name: True})
    plc.step()
    displacement_projection = _projection(plc)
    displaced = next(
        write for write in displacement_projection.writes if write.transition.tag_name == step.name
    )
    enabling_reads = displacement_projection.enabling_read_closure_observed_by_write(displaced)
    observation = EffectObservation(
        obligation=_produce(program, 0, step, 41),
        disposition="OVERWRITTEN",
        appeared=produced,
        displacement=displaced,
        displacement_enabling_reads=enabling_reads,
    )

    consumer = displacement_consumer_read(observation)
    assert consumer is not None
    assert consumer.occurrence.name == step.name
    assert consumer.occurrence.value == 41
