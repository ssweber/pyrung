"""Unit gates for the target-relative progress gauge (pilot/gauge.py)."""

from __future__ import annotations

from pyrung import (
    PLC,
    Bool,
    Int,
    Program,
    Timer,
    calc,
    copy,
    latch,
    on_delay,
    out,
    reset,
    rise,
    rung,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.gauge import GaugeMovement, build_gauge
from pyrung.core.analysis.pilot.pilot import _build_prover_context
from pyrung.core.analysis.steerable import compute_clear_only, compute_steerable
from tests.core.analysis.test_pilot_detour_progress import _knock_three_times_program


def _gauge_for(logic, plc, target_tag, channel_tags=frozenset()):
    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    clear_only = compute_clear_only(pdg, plc._known_tags_by_name, logic)
    prover = _build_prover_context(logic, dict(plc.state.tags))
    assert prover.key_config is not None
    return build_gauge(
        pdg,
        logic,
        target_tag,
        prover.key_config,
        steerable=steerable,
        clear_only=clear_only,
        edge_tags=frozenset(),
        pipeline_internal_tags=frozenset(),
        channel_tags=channel_tags,
        harness=None,
    )


def test_knock_count_is_an_ordinal_component() -> None:
    """The threshold-absorbed knock counter joins the gauge as an ordinal.

    The search key masks ``Knock_Count`` behind ``count < 3``; the gauge
    carries its raw value so ``(AtDoor, 1) -> (AtDoor, 2)`` reads as an earn.
    """
    logic, _knock, channel, count, *_ = _knock_three_times_program()
    plc = PLC(logic, dt=0.010)
    plc.step()
    gauge = _gauge_for(logic, plc, channel.name)

    kinds = {c.tag: c.kind for c in gauge.components}
    assert kinds.get(count.name) == "ordinal"
    assert gauge.receipt({count.name: 1}, {count.name: 2}).any_forward
    assert not gauge.receipt({count.name: 2}, {count.name: 2}).any_forward
    assert not gauge.receipt({count.name: 2}, {count.name: 0}).any_forward


def test_gauge_receipt_keeps_source_landing_and_progress_order() -> None:
    """A consumer receives the evidence, not only a transient comparison."""
    from pyrung.core.analysis.pilot.gauge import Gauge, GaugeComponent

    gauge = Gauge((GaugeComponent("Step", "stepper", 1),))

    advanced = gauge.receipt({"Step": 101}, {"Step": 103})
    assert advanced.source_mark == (("Step", 101),)
    assert advanced.landing_mark == (("Step", 103),)
    assert advanced.movement is GaugeMovement.FORWARD
    assert advanced.any_forward

    assert gauge.receipt({"Step": 103}, {"Step": 103}).movement is GaugeMovement.UNCHANGED
    assert gauge.receipt({"Step": 103}, {"Step": 101}).movement is GaugeMovement.BACKWARD
    assert gauge.receipt({}, {"Step": 103}).movement is GaugeMovement.UNKNOWN
    assert Gauge(()).receipt({}, {}).movement is GaugeMovement.UNKNOWN

    mixed = Gauge(
        (
            GaugeComponent("Step", "stepper", 1),
            GaugeComponent("Batch", "ordinal", 1),
        )
    ).receipt({"Step": 3, "Batch": 7}, {"Step": 4, "Batch": 6})
    assert mixed.movement is GaugeMovement.BACKWARD
    assert mixed.any_forward
    assert tuple(reading.movement for reading in mixed.readings) == (
        GaugeMovement.FORWARD,
        GaugeMovement.BACKWARD,
    )


def _step_chain_program():
    """A discrete stepper with a reset — the recipe-coordinate shape.

    ``Step`` advances +1 under a transition pulse armed by step-derived flags
    (self-limiting), and a ``Resetting``-gated literal load rolls it back to 1.
    ``Resetting`` is written under ``Mode == 15`` so the reset's enabling
    channel value is one alias hop away, exactly like the real burner.
    """
    Go = Bool("SC_Go", external=True)
    Mode = Int("SC_Mode")
    Step = Int("SC_Step", default=1)
    AtTwo = Bool("SC_AtTwo")
    Trans = Bool("SC_Trans")
    Resetting = Bool("SC_Resetting")
    Done = Bool("SC_Done")
    Dwell = Timer.clone("SC_Dwell")

    with Program() as logic:
        with rung(Mode == 15):
            out(Resetting)
        with rung(Step == 2):
            out(AtTwo)
        with rung(Step == 1, rise(Go)):
            latch(Trans)
        with rung(AtTwo):
            on_delay(Dwell, 30, "ms")
        with rung(AtTwo, Dwell.Done):
            latch(Trans)
        with rung(Trans):
            calc(Step + 1, Step)
            reset(Trans)
        with rung(Resetting):
            copy(1, Step)
        with rung(Step == 3):
            latch(Done)

    return logic, Step, Mode, Done


def test_step_chain_stepper_with_alias_resolved_reset() -> None:
    logic, Step, Mode, Done = _step_chain_program()
    plc = PLC(logic, dt=0.010)
    plc.step()
    gauge = _gauge_for(logic, plc, Done.name, channel_tags=frozenset({Mode.name}))

    by_tag = {c.tag: c for c in gauge.components}
    assert Step.name in by_tag, [c.tag for c in gauge.components]
    component = by_tag[Step.name]
    assert component.kind == "stepper"
    assert component.direction == 1

    resolved = [e for e in component.resets if not e.init_only]
    assert resolved, "the Resetting-gated load must be recorded as a reset"
    reset = resolved[0]
    assert reset.resolved
    assert reset.value == 1
    assert reset.channel_tag == Mode.name
    assert reset.enabling_channel_values == (15,)
    assert not gauge.has_banked_work({Step.name: 1})
    assert gauge.has_banked_work({Step.name: 2})

    # Anchor-relative readings: forward, unchanged, and backward.
    assert gauge.receipt({Step.name: 2}, {Step.name: 3}).movement is GaugeMovement.FORWARD
    assert gauge.receipt({Step.name: 2}, {Step.name: 2}).movement is GaugeMovement.UNCHANGED
    assert gauge.receipt({Step.name: 2}, {Step.name: 1}).movement is GaugeMovement.BACKWARD


def _banked_reset_alternative_program(*, initial_step: int):
    """A destructive target writer followed by a work-preserving sibling."""

    ResetTarget = Bool("BR_ResetTarget", external=True)
    SafeTarget = Bool("BR_SafeTarget", external=True)
    Advance = Bool("BR_Advance", external=True)
    Step = Int("BR_Step", default=initial_step)
    Done = Bool("BR_Done")
    SafeWake1 = Bool("BR_SafeWake1")
    SafeWake2 = Bool("BR_SafeWake2")
    SafeWake3 = Bool("BR_SafeWake3")

    with Program() as logic:
        # Keep the destructive writer first so it is the first target route.
        with rung(ResetTarget):
            latch(Done)
        # The destructive effect is a separate program consequence of the same
        # action. Candidate construction sees an ordinary target writer; the
        # interpreted trial and gauge own the whole-world verdict.
        with rung(ResetTarget):
            copy(1, Step)
        with rung(SafeTarget):
            latch(Done)
            latch(SafeWake1)
            latch(SafeWake2)
            latch(SafeWake3)
        with rung(Step == 2, rise(Advance)):
            calc(Step + 1, Step)
        with rung(Step == 3):
            latch(Done)

    return logic, ResetTarget, SafeTarget, Step, Done


def test_loop_nogoods_a_destructive_candidate_then_uses_its_sibling() -> None:
    """The live banked-work gate, not a construction filter, owns rejection."""

    logic, ResetTarget, SafeTarget, Step, Done = _banked_reset_alternative_program(initial_step=2)
    events = list(pilot_events(PLC(logic), Done, max_scans=100))

    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    tried = [event.data["candidate"]["tag"] for event in events if event.kind == "candidate_try"]
    assert tried.count(ResetTarget.name) == 1
    assert SafeTarget.name in tried

    rejection = next(
        event
        for event in events
        if event.kind == "candidate_rejected" and event.data["candidate"]["tag"] == ResetTarget.name
    )
    assert rejection.data["gates"][-1].event == "banked-work"
    assert rejection.data["gates"][-1].evidence["effect"] == "behind"

    accepted = next(
        event
        for event in events
        if event.kind == "candidate_accepted"
        and event.data["candidate_detail"]["tag"] == SafeTarget.name
    )
    assert accepted.data["snapshot"][Step.name] == 2


def test_reset_at_its_floor_is_not_rejected_as_banked_work() -> None:
    """The same target writer remains legal when it destroys no earned work."""

    logic, ResetTarget, _SafeTarget, Step, Done = _banked_reset_alternative_program(initial_step=1)
    events = list(pilot_events(PLC(logic), Done, max_scans=100))

    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    accepted = next(event for event in events if event.kind == "candidate_accepted")
    assert accepted.data["candidate_detail"]["tag"] == ResetTarget.name
    assert accepted.data["snapshot"][Step.name] == 1
    assert not any(
        gate.event == "banked-work" for event in events for gate in event.data.get("gates", ())
    )


def test_level_driven_counter_stays_out_of_the_gauge() -> None:
    """A free-running accumulator (level guard, no event) must not earn.

    The key config is fabricated (the tiny program is beneath the prover's
    notice) so family B genuinely considers ``Count`` — and rejects it: its
    only advancing writer is gated by a plain level, neither discrete nor
    self-limiting.
    """
    from pyrung.core.analysis.pilot._ops import _StateKeyConfig

    Run = Bool("LV_Run", external=True)
    Count = Int("LV_Count")
    Done = Bool("LV_Done")

    with Program() as logic:
        with rung(Run):
            calc(Count + 1, Count)
        with rung(Count >= 100):
            latch(Done)

    plc = PLC(logic, dt=0.010)
    plc.step()
    pdg = build_program_graph(logic)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, logic)
    clear_only = compute_clear_only(pdg, plc._known_tags_by_name, logic)
    cfg = _StateKeyConfig(
        stateful_names=(Count.name, Done.name),
        done_specs=(),
        threshold_vector_specs=(),
        acc_indices=frozenset(),
    )
    gauge = build_gauge(
        pdg,
        logic,
        Done.name,
        cfg,
        steerable=steerable,
        clear_only=clear_only,
        edge_tags=frozenset(),
        pipeline_internal_tags=frozenset(),
        channel_tags=frozenset(),
        harness=None,
    )
    assert Count.name not in {c.tag for c in gauge.components}
