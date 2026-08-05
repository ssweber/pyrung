"""Phase-3 selected-effect pass-through and factual observation contracts."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from types import SimpleNamespace

from pyrung import PLC, Bool, Int, Program, calc, call, copy, out, rung, subroutine
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.analysis.pilot.coast import (
    TARGET,
    CoastReceipt,
    CoastSession,
    CoastTriggerEvent,
    value_trigger,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObligation,
    EffectPathStep,
    observe_execution_window,
    observe_expectation,
    required_shape,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    BatchPulse,
    Bearing,
    BearingObjective,
    ChannelHeading,
    Coast,
    Pulse,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.options import _Candidate
from pyrung.core.analysis.pilot.orientation import _pulse_policy
from pyrung.core.analysis.pilot.recording import _accepted_payload, _candidate_payload
from pyrung.core.analysis.pilot.skiff import _skiff_expectation, run_pinned_scan
from pyrung.core.analysis.pilot.steer import _compass_observations, _executed_attempt
from pyrung.core.analysis.pilot.trace import TraceNode
from pyrung.core.analysis.pilot.types import (
    ChannelMotion,
    TargetReached,
    _AcceptedTrial,
    _ExecutedAttempt,
    _ExecutionEvidence,
)
from pyrung.core.analysis.pilot.verify import _owned_channel_motion, _rebind_replay_attempt


def _terminal_obligation(program: Program, tag: str, value: object) -> EffectObligation:
    return EffectObligation(
        tag=tag,
        value=value,
        producer=(None, 0, ()),
        consumer=None,
        required_shape=(),
        producer_rung=program.rungs[0],
    )


def test_required_shape_preserves_repeated_ordered_handoffs() -> None:
    producer = SimpleNamespace(
        condition_reads=frozenset(), guard_reads=frozenset(), data_reads=frozenset()
    )
    consumer = SimpleNamespace(
        condition_reads=frozenset({"Repeated"}),
        guard_reads=frozenset(),
        data_reads=frozenset(),
    )
    pdg = SimpleNamespace(rung_nodes=(producer, consumer))
    path = (
        EffectPathStep(1, "Consumer", True, (("Repeated", 1), ("Repeated", 1))),
        EffectPathStep(0, "Repeated", 1),
    )

    assert required_shape(path, pdg) == (("Repeated", 1), ("Repeated", 1))


def test_repeated_shape_requires_two_increasing_consumer_reads() -> None:
    effect = Int("RepeatedShapeEffect")
    result = Int("RepeatedShapeResult")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung(effect == 1, effect == 1):
            copy(1, result)
    obligation = EffectObligation(
        tag=effect.name,
        value=1,
        producer=(None, 0, ()),
        consumer=(None, 1, ()),
        required_shape=((effect.name, 1), (effect.name, 1)),
        producer_rung=program.rungs[0],
        consumer_rung=program.rungs[1],
    )
    plc = PLC(program)
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None

    observation = observe_expectation(EffectExpectation((obligation,)), (projection,))[0]

    assert observation.disposition == "SURVIVED"
    assert len(observation.observed_reads) == 2
    assert observation.observed_reads[0].ordinal < observation.observed_reads[1].ordinal


def test_action_projection_preserves_sparse_nonzero_run_identity() -> None:
    unrelated = Int("SparseProjectionUnrelated")
    effect = Int("SparseProjectionEffect")
    result = Int("SparseProjectionResult")
    with Program() as program:
        with rung():
            copy(1, unrelated)
        with rung():
            copy(1, effect)
        with rung(effect == 1, effect == 1):
            copy(1, result)
    obligation = EffectObligation(
        tag=effect.name,
        value=1,
        producer=(None, 1, ()),
        consumer=(None, 2, ()),
        required_shape=((effect.name, 1), (effect.name, 1)),
        producer_rung=program.rungs[1],
        consumer_rung=program.rungs[2],
    )
    expectation = EffectExpectation((obligation,))
    plc = PLC(program)
    plc.step()
    observations = observe_execution_window(
        expectation,
        plc,
        scan_before=0,
        action_scan=1,
    )

    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    assert len(projection.runs) == 3
    assert [item.disposition for item in observations] == ["SURVIVED"]
    assert len(observations[0].observed_reads) == 2
    assert observations[0].observed_reads[0].ordinal < observations[0].observed_reads[1].ordinal


def test_route_and_program_coast_observe_the_execution_corridor_not_scan_before() -> None:
    effect = Int("CoastCorridorEffect")
    with Program() as program:
        with rung():
            copy(1, effect)
    expectation = EffectExpectation((_terminal_obligation(program, effect.name, 1),))

    for source in (ActSource.ROUTE, ActSource.PROGRAM):
        plc = PLC(program)
        plc.step()
        bearing = Bearing(
            (),
            Coast(
                "bearing",
                ActPolicy(
                    source,
                    heading=ChannelHeading(effect.name, 1),
                    expectation=expectation,
                ),
            ),
            BearingObjective(TargetSpec(effect.name, 1)),
        )
        receipt = CoastReceipt(
            "bearing",
            0,
            1,
            "reached",
            ("target",),
            (),
            1,
            kernel_scans=1,
        )
        pulse = SimpleNamespace(
            fork=plc,
            scan_before=0,
            action_scan=0,
            coast_receipt=receipt,
            timeline=(),
        )

        attempt = _executed_attempt(bearing, pulse)  # type: ignore[arg-type]

        assert attempt.bearing.expectation is expectation
        assert [item.disposition for item in attempt.effect_observations] == ["SURVIVED"]
        assert attempt.effect_observations[0].appeared is not None
        assert attempt.effect_observations[0].appeared.scan_id == 1


def test_learned_observation_retains_only_a_unique_static_writer(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.steer as steer

    effect = Int("LearnedReceiptEffect")
    with Program() as unique_program:
        with rung():
            copy(1, effect)
    unique = PLC(unique_program)
    unique.step()
    monkeypatch.setattr(steer, "_action_caused_change", lambda *_args, **_kwargs: True)

    def learned(program: Program, plc: PLC):
        pdg = build_program_graph(program)
        frame = SimpleNamespace(tree=TraceNode(effect.name, 1))
        ctx = SimpleNamespace(
            pdg=pdg,
            program=program,
            steerable=frozenset({"Action"}),
            collect_action_attribution=True,
        )
        return _compass_observations(
            ("Action", True),
            frame,
            {effect.name: 0},
            {effect.name: 1},
            ctx,
            contradict_no_change=False,
            world_key=("world",),
            applied=(("Action", True),),
            fork=plc,
            scan=1,
            start_scan=1,
        )[0]

    assert learned(unique_program, unique).expectation is not None

    other = Bool("LearnedReceiptOther")
    with Program() as ambiguous_program:
        with rung():
            copy(1, effect)
        with rung(other == 0):
            copy(1, effect)
    ambiguous = PLC(ambiguous_program)
    ambiguous.step()

    assert learned(ambiguous_program, ambiguous).expectation is None


def test_learned_capture_excludes_release_scan_writer(monkeypatch) -> None:
    import pyrung.core.analysis.pilot.steer as steer

    release = Bool("LearnedReleaseWriterGate", external=True)
    effect = Int("LearnedReleaseOnlyEffect")
    with Program() as program:
        with rung(release):
            copy(1, effect)
    pdg = build_program_graph(program)
    plc = PLC(program)
    plc.patch({release.name: True})
    plc.step()  # release/setup scan owns the only matching write
    plc.patch({release.name: False})
    plc.step()  # assertion scan owns no write
    monkeypatch.setattr(steer, "_action_caused_change", lambda *_args, **_kwargs: True)
    observation = _compass_observations(
        ("Action", True),
        SimpleNamespace(tree=TraceNode(effect.name, 1)),
        {effect.name: 0},
        {effect.name: 1},
        SimpleNamespace(
            pdg=pdg,
            program=program,
            steerable=frozenset({"Action"}),
            collect_action_attribution=True,
        ),
        contradict_no_change=False,
        world_key=("world",),
        applied=(("Action", True),),
        fork=plc,
        scan=2,
        start_scan=1,
    )[0]

    assert observation.expectation is None


def test_skiff_result_captures_only_a_unique_exact_writer() -> None:
    action = Bool("SkiffReceiptAction", external=True)
    effect = Int("SkiffReceiptEffect")
    with Program() as unique_program:
        with rung(action):
            copy(1, effect)
    unique_pdg = build_program_graph(unique_program)
    unique = run_pinned_scan(
        PLC(unique_program),
        frozenset({action.name, effect.name}),
        unique_pdg,
        pilot_rungs=(),
        actions=((action.name, True),),
    )
    expectation = _skiff_expectation(
        unique,
        SimpleNamespace(pdg=unique_pdg, program=unique_program),
        effect.name,
        1,
    )
    assert expectation is not None
    assert expectation.obligations[0].producer == (None, 0, ())

    with Program() as ambiguous_program:
        with rung(action):
            copy(1, effect)
        with rung(action):
            copy(1, effect)
    ambiguous_pdg = build_program_graph(ambiguous_program)
    ambiguous = run_pinned_scan(
        PLC(ambiguous_program),
        frozenset({action.name, effect.name}),
        ambiguous_pdg,
        pilot_rungs=(),
        actions=((action.name, True),),
    )
    assert (
        _skiff_expectation(
            ambiguous,
            SimpleNamespace(pdg=ambiguous_pdg, program=ambiguous_program),
            effect.name,
            1,
        )
        is None
    )
    assert (
        _skiff_expectation(
            unique,
            SimpleNamespace(pdg=unique_pdg, program=unique_program),
            action.name,
            True,
        )
        is None
    )


def test_expectation_is_same_object_through_candidate_policy_bearing_and_execution() -> None:
    effect = Int("ExpectationPassThroughEffect")
    with Program() as program:
        with rung():
            copy(1, effect)

    obligation = _terminal_obligation(program, effect.name, 1)
    expectation = EffectExpectation((obligation,))
    candidate = _Candidate(effect.name, 1, ActSource.TRACE, expectation=expectation)
    policy = _pulse_policy(candidate, ((effect.name, 1),))
    bearing = Bearing(
        (),
        Pulse(policy),
        BearingObjective(TargetSpec("Target", True)),
    )

    plc = PLC(program)
    plc.step()
    pulse = SimpleNamespace(
        fork=plc,
        scan_before=0,
        action_scan=1,
        coast_receipt=None,
        timeline=(),
    )
    attempt = _executed_attempt(bearing, pulse)  # type: ignore[arg-type]

    assert candidate.expectation is expectation
    assert policy.expectation is expectation
    assert bearing.expectation is expectation
    assert attempt.bearing.expectation is expectation
    assert attempt.effect_observations[0].obligation is obligation
    assert _candidate_payload(policy)["effect_expectation"][0].producer == (None, 0, ())


def test_real_trace_candidate_records_selected_producer_consumer_and_shape() -> None:
    command = Bool("LoweringCommand", external=True)
    effect = Int("LoweringEffect")
    target = Bool("LoweringTarget")
    with Program() as program:
        with rung(command):
            copy(1, effect)
        with rung(effect == 1):
            out(target)
    built = []

    pilot_how(
        PLC(program),
        target,
        on_event=lambda event: (
            built.append(event.data) if event.kind == "candidates_built" else None
        ),
    )

    candidate = next(item for item in built[0]["candidates"] if item["tag"] == command.name)
    obligation = candidate["effect_expectation"][0]
    assert obligation.tag == effect.name
    assert obligation.producer == (None, 0, ())
    assert obligation.consumer == (None, 1, ())
    assert obligation.required_shape == ((effect.name, 1),)


def test_batch_co_actions_share_one_expectation_and_do_not_mint_more() -> None:
    effect = Int("BatchExpectationEffect")
    with Program() as program:
        with rung():
            copy(1, effect)
    expectation = EffectExpectation((_terminal_obligation(program, effect.name, 1),))
    policy = ActPolicy(
        ActSource.WIDENING,
        action_pairs=(("Command", True), ("Hold", True)),
        applied=(("Command", True), ("Hold", True)),
        expectation=expectation,
    )
    batch = BatchPulse(policy)

    assert batch.policy.expectation is expectation
    assert len(batch.policy.expectation.obligations) == 1


def test_same_physical_pair_on_distinct_writer_paths_stays_alternative() -> None:
    first = TraceNode(
        "FirstEffect",
        1,
        writer_rung=1,
        children=[TraceNode("SharedCommand", True, is_steerable=True)],
    )
    second = TraceNode(
        "SecondEffect",
        1,
        writer_rung=2,
        children=[TraceNode("SharedCommand", True, is_steerable=True)],
    )
    tree = TraceNode("Target", True, writer_rung=0, children=[first, second])

    details = tree.ordered_action_details()

    assert [detail.pair for detail in details] == [
        ("SharedCommand", True),
        ("SharedCommand", True),
    ]
    assert details[0].effect_path != details[1].effect_path


def test_same_physical_act_keeps_alternative_bearings_without_changing_nogood_identity() -> None:
    effect = Int("AlternativeIdentityEffect")
    with Program() as program:
        with rung():
            copy(1, effect)
    first = _terminal_obligation(program, effect.name, 1)
    second = EffectObligation(
        tag=effect.name,
        value=1,
        producer=(None, 0, (1,)),
        consumer=None,
        required_shape=(),
        producer_rung=program.rungs[0],
    )
    applied = (("SharedCommand", True),)
    first_act = Pulse(
        ActPolicy(ActSource.TRACE, applied, applied, expectation=EffectExpectation((first,)))
    )
    second_act = Pulse(
        ActPolicy(ActSource.TRACE, applied, applied, expectation=EffectExpectation((second,)))
    )

    first_bearing = Bearing((), first_act, BearingObjective(TargetSpec("Target", True)))
    second_bearing = Bearing((), second_act, BearingObjective(TargetSpec("Target", True)))

    assert first_bearing.expectation != second_bearing.expectation
    # Phase 3 does not change empirical/nogood identity; attempt/proof identity
    # is intentionally deferred to the later learning phase.
    assert act_identity(first_act) == act_identity(second_act)


def test_ordinary_absent_is_window_relative_but_bootstrap_facts_are_shared() -> None:
    enabled = Bool("AbsentEnabled")
    effect = Int("AbsentEffect")
    with Program() as program:
        with rung(enabled):
            copy(1, effect)
    expectation = EffectExpectation((_terminal_obligation(program, effect.name, 1),))
    plc = PLC(program)
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None

    ordinary = observe_expectation(expectation, (projection,))
    bootstrap_facts = projection.observe_appeared_handoff(
        effect.name,
        1,
        producer_rung=program.rungs[0],
        consumer_rung=None,
    )

    assert [item.disposition for item in ordinary] == ["ABSENT"]
    assert bootstrap_facts == ()


def test_ordinary_observer_classifies_overwritten_stranded_and_displaced() -> None:
    effect = Int("OrdinaryDispositionEffect")
    permit = Bool("OrdinaryDispositionPermit")
    latch = Int("OrdinaryDispositionLatch")
    out_tag = Int("OrdinaryDispositionOut")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung():
            copy(2, effect)
        with rung(effect == 1, permit):
            copy(1, out_tag)
        with rung():
            copy(1, latch)
        with rung():
            copy(0, latch)
        with rung(effect == 2, latch == 1):
            copy(2, out_tag)
    plc = PLC(program)
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None

    overwritten = EffectObligation(
        effect.name,
        1,
        (None, 0, ()),
        (None, 2, ()),
        ((effect.name, 1),),
        producer_rung=program.rungs[0],
        consumer_rung=program.rungs[2],
    )
    stranded = EffectObligation(
        effect.name,
        2,
        (None, 1, ()),
        (None, 2, ()),
        ((effect.name, 2),),
        producer_rung=program.rungs[1],
        consumer_rung=program.rungs[2],
    )
    displaced = EffectObligation(
        effect.name,
        2,
        (None, 1, ()),
        (None, 5, ()),
        ((effect.name, 2), (latch.name, 1)),
        producer_rung=program.rungs[1],
        consumer_rung=program.rungs[5],
    )

    assert (
        observe_expectation(EffectExpectation((overwritten,)), (projection,))[0].disposition
        == "OVERWRITTEN"
    )
    assert (
        observe_expectation(EffectExpectation((stranded,)), (projection,))[0].disposition
        == "STRANDED"
    )
    displaced_result = observe_expectation(EffectExpectation((displaced,)), (projection,))[0]
    assert displaced_result.disposition == "DISPLACED"
    assert [read.occurrence.name for read in displaced_result.observed_reads] == [
        effect.name,
        latch.name,
    ]


def test_repeated_producer_occurrences_keep_mixed_per_occurrence_truth() -> None:
    effect = Int("RepeatedOrdinaryEffect")

    @subroutine("RepeatedOrdinaryProducer", strict=False)
    def producer() -> None:
        with rung():
            copy(1, effect)

    with Program() as program:
        with rung():
            call(producer)
            call(producer)
    obligation = EffectObligation(
        effect.name,
        1,
        ("RepeatedOrdinaryProducer", 0, ()),
        None,
        (),
        producer_rung=program.subroutines["RepeatedOrdinaryProducer"][0],
    )
    plc = PLC(program)
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None

    observations = observe_expectation(EffectExpectation((obligation,)), (projection,))

    assert [item.disposition for item in observations] == ["OVERWRITTEN", "SURVIVED"]
    appeared = tuple(item.appeared for item in observations)
    assert all(item is not None and item.scan_id == 1 for item in appeared)
    assert appeared[0] is not None and appeared[1] is not None
    assert appeared[0].ordinal < appeared[1].ordinal
    assert observations[0].appeared is not observations[1].appeared


def test_release_then_assert_window_observes_one_exact_appeared_occurrence() -> None:
    command = Bool("WindowCommand")
    effect = Int("WindowEffect")
    with Program() as program:
        with rung(command):
            copy(1, effect)
    expectation = EffectExpectation((_terminal_obligation(program, effect.name, 1),))
    plc = PLC(program)
    plc.patch({command.name: False})
    plc.step()
    plc.patch({command.name: True})
    plc.step()

    observations = observe_execution_window(
        expectation,
        plc,
        scan_before=0,
        action_scan=2,
    )

    assert [item.disposition for item in observations] == ["SURVIVED"]
    assert observations[0].appeared is not None
    assert observations[0].appeared.scan_id == 2
    assert observations[0].appeared.ordinal > 0


def test_release_scan_matching_write_cannot_satisfy_assertion_expectation() -> None:
    command = Bool("AdversarialWindowCommand")
    effect = Int("AdversarialWindowEffect")
    with Program() as program:
        with rung(~command):
            copy(1, effect)
    expectation = EffectExpectation((_terminal_obligation(program, effect.name, 1),))
    plc = PLC(program)
    plc.patch({command.name: False})
    plc.step()
    plc.patch({command.name: True})
    plc.step()

    release = plc._replay_rung_write_projection_at(1)
    assert release is not None
    assert release.observe_appeared_handoff(
        effect.name,
        1,
        producer_rung=program.rungs[0],
        consumer_rung=None,
    )

    observations = observe_execution_window(
        expectation,
        plc,
        scan_before=0,
        action_scan=2,
    )
    assert [item.disposition for item in observations] == ["ABSENT"]


def test_action_scan_strands_consumer_even_if_a_later_scan_reads_the_value() -> None:
    phase = Int("CrossScanPhase")
    effect = Int("CrossScanEffect")
    out = Int("CrossScanOut")
    with Program() as program:
        with rung(phase == 0):
            copy(1, effect)
        with rung(phase == 1, effect == 1):
            copy(1, out)
    obligation = EffectObligation(
        tag=effect.name,
        value=1,
        producer=(None, 0, ()),
        consumer=(None, 1, ()),
        required_shape=((effect.name, 1),),
        producer_rung=program.rungs[0],
        consumer_rung=program.rungs[1],
    )
    plc = PLC(program)
    plc.patch({phase.name: 0})
    plc.step()
    plc.patch({phase.name: 1})
    plc.step()

    observations = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        action_scan=1,
    )

    assert [item.disposition for item in observations] == ["STRANDED"]
    snapshot = observations[0].diagnostic_snapshot()
    assert snapshot.appeared is not None and snapshot.appeared.scan_id == 1
    assert snapshot.observed_reads
    assert all(read.scan_id == 1 for read in snapshot.observed_reads)
    assert all(read.tag != effect.name for read in snapshot.observed_reads)


def test_producer_below_consumer_is_due_when_the_scan_wraps() -> None:
    """A handoff written after its consumer remains pending until next scan."""

    effect = Int("WrappedHandoffEffect")
    out = Int("WrappedHandoffOutput")
    command = Bool("WrappedHandoffCommand")
    with Program() as program:
        with rung(effect == 1):
            copy(1, out)
        with rung(command):
            copy(1, effect, oneshot=True)
    obligation = EffectObligation(
        tag=effect.name,
        value=1,
        producer=(None, 1, ()),
        consumer=(None, 0, ()),
        required_shape=((effect.name, 1),),
        producer_rung=program.rungs[1],
        consumer_rung=program.rungs[0],
    )
    plc = PLC(program)
    plc.patch({command.name: True})
    plc.step()
    plc.step()

    observations = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        action_scan=1,
    )

    assert [item.disposition for item in observations] == ["SURVIVED"]
    assert observations[0].appeared is not None
    assert observations[0].appeared.scan_id == 1
    assert observations[0].consumer_read is not None
    assert observations[0].consumer_read.scan_id == 2


def test_ambiguous_consumers_before_producer_fail_closed_at_scan_wrap() -> None:
    """Two possible pre-wrap consumers cannot designate one exact handoff."""

    effect = Int("AmbiguousWrappedEffect")
    out = Int("AmbiguousWrappedOutput")
    command = Bool("AmbiguousWrappedCommand")

    @subroutine("AmbiguousWrappedConsumer", strict=False)
    def consumer() -> None:
        with rung(effect == 1):
            copy(1, out)

    with Program() as program:
        with rung():
            call(consumer)
            call(consumer)
        with rung(command):
            copy(1, effect, oneshot=True)
    obligation = EffectObligation(
        tag=effect.name,
        value=1,
        producer=(None, 1, ()),
        consumer=("AmbiguousWrappedConsumer", 0, ()),
        required_shape=((effect.name, 1),),
        producer_rung=program.rungs[1],
        consumer_rung=program.subroutines["AmbiguousWrappedConsumer"][0],
    )
    plc = PLC(program)
    plc.patch({command.name: True})
    plc.step()
    plc.step()

    observations = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        action_scan=1,
    )

    assert [item.disposition for item in observations] == ["UNKNOWN"]
    assert "ambiguous" in observations[0].detail
    assert len(observations[0].observed_reads) == 2


def test_exact_next_scan_write_overwrites_pending_wrapped_handoff() -> None:
    """An exact write before the next consumer is proof, not lost continuity."""

    armed = Int("OverwrittenWrappedArmed")
    effect = Int("OverwrittenWrappedEffect")
    out = Int("OverwrittenWrappedOutput")
    command = Bool("OverwrittenWrappedCommand")
    with Program() as program:
        with rung(armed == 1):
            copy(1, effect)
        with rung(effect == 1):
            copy(1, out)
        with rung(command):
            copy(1, armed, oneshot=True)
            copy(1, effect, oneshot=True)
    obligation = EffectObligation(
        tag=effect.name,
        value=1,
        producer=(None, 2, ()),
        consumer=(None, 1, ()),
        required_shape=((effect.name, 1),),
        producer_rung=program.rungs[2],
        consumer_rung=program.rungs[1],
    )
    plc = PLC(program)
    plc.patch({command.name: True})
    plc.step()
    plc.step()

    observations = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        action_scan=1,
    )

    assert [item.disposition for item in observations] == ["OVERWRITTEN"]
    assert observations[0].consumer_read is not None
    assert observations[0].consumer_read.occurrence.source != "entry"
    assert observations[0].displacement is not None
    assert observations[0].displacement.run.rung is program.rungs[0]


def test_excursion_replay_recomputes_effect_receipt_from_replacement_fork() -> None:
    enabled = Bool("ReplayExpectationEnabled")
    effect = Int("ReplayExpectationEffect")
    with Program() as program:
        with rung(enabled):
            copy(1, effect)
    expectation = EffectExpectation((_terminal_obligation(program, effect.name, 1),))
    policy = ActPolicy(ActSource.TRACE, expectation=expectation)
    bearing = Bearing((), Pulse(policy), BearingObjective(TargetSpec("Target", True)))

    original = PLC(program)
    original.step()
    old_pulse = SimpleNamespace(
        fork=original,
        scan_before=0,
        action_scan=1,
        coast_receipt=None,
        timeline=(),
    )
    old_attempt = _executed_attempt(bearing, old_pulse)  # type: ignore[arg-type]
    assert old_attempt.effect_observations[0].disposition == "ABSENT"

    replay = PLC(program)
    replay.patch({enabled.name: True})
    replay.step()
    replay_pulse = SimpleNamespace(
        fork=replay,
        scan_before=0,
        action_scan=1,
        coast_receipt=None,
        timeline=(),
    )
    rebound = _rebind_replay_attempt(old_attempt, replay_pulse)  # type: ignore[arg-type]

    assert rebound.effect_observations[0].disposition == "SURVIVED"
    assert rebound.effect_observations is not old_attempt.effect_observations


def test_coast_replay_rebind_preserves_execution_corridor_mode() -> None:
    effect = Int("ReplayCoastEffect")
    with Program() as program:
        with rung():
            copy(1, effect)
    expectation = EffectExpectation((_terminal_obligation(program, effect.name, 1),))
    bearing = Bearing(
        (),
        Coast(
            "bearing",
            ActPolicy(
                ActSource.PROGRAM,
                heading=ChannelHeading(effect.name, 1),
                expectation=expectation,
            ),
        ),
        BearingObjective(TargetSpec(effect.name, 1)),
    )
    original = PLC(program)
    original.step()
    original_receipt = CoastReceipt("bearing", 0, 1, "reached", ("target",), (), 1, kernel_scans=1)
    original_pulse = SimpleNamespace(
        fork=original,
        scan_before=0,
        action_scan=0,
        coast_receipt=original_receipt,
        timeline=(),
    )
    attempt = _executed_attempt(bearing, original_pulse)  # type: ignore[arg-type]
    replay = PLC(program)
    replay.step()
    replay_receipt = CoastReceipt("bearing", 0, 1, "reached", ("target",), (), 1, kernel_scans=1)
    replay_pulse = SimpleNamespace(
        fork=replay,
        scan_before=0,
        action_scan=0,
        coast_receipt=replay_receipt,
        timeline=(),
    )

    rebound = _rebind_replay_attempt(attempt, replay_pulse)  # type: ignore[arg-type]

    assert [item.disposition for item in attempt.effect_observations] == ["SURVIVED"]
    assert [item.disposition for item in rebound.effect_observations] == ["SURVIVED"]
    assert rebound.effect_observations[0].appeared is not None
    assert rebound.effect_observations[0].appeared.scan_id == 1


def test_alarm_reset_action_scan_records_exact_watchdog_overwrite() -> None:
    from tests.fixtures.pilot_alarm_presets.alarmed_at_start import (
        ALARMED,
        COMPLETE,
        AtTarget,
        ProcessStep,
        Reset,
        logic,
    )

    plc = PLC(logic, dt=0.010)
    plc.force(Reset, True)
    plc.force(AtTarget, True)
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    complete_write = next(
        write
        for write in projection.writes
        if write.transition.tag_name == ProcessStep.name and write.transition.to_value == COMPLETE
    )
    obligation = EffectObligation(
        ProcessStep.name,
        COMPLETE,
        (None, 1, ()),
        None,
        (),
        producer_rung=complete_write.run.rung,
    )

    observations = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        action_scan=1,
    )

    assert plc.state.tags[ProcessStep.name] == ALARMED
    assert [item.disposition for item in observations] == ["OVERWRITTEN"]
    assert observations[0].appeared is complete_write
    assert observations[0].displacement is not None
    assert observations[0].displacement.transition.to_value == ALARMED
    assert [
        (read.occurrence.name, read.occurrence.value) for read in observations[0].observed_reads
    ] == [("Watchdog_Done", True)]


def test_expectation_bearing_coast_keeps_cyclefold_and_gap_is_unknown() -> None:
    enabled = Bool("FoldedEffectEnabled", external=True)
    effect = Int("FoldedEffect")
    with Program() as program:
        with rung(enabled):
            copy(1, effect)
    plc = PLC(program)
    expectation = EffectExpectation((_terminal_obligation(program, effect.name, 1),))
    session = CoastSession(plc, kind="expectation-fold")

    receipt = session.seek(
        [value_trigger(plc, "never", TARGET, effect.name, 1)],
        budget=80,
    )
    observations = observe_execution_window(
        expectation,
        plc,
        scan_before=0,
        coast_receipt=receipt,
        timeline=session.events,
    )

    assert receipt.macro_folds >= 1
    assert receipt.skipped_scans > 0
    assert [item.disposition for item in observations] == ["UNKNOWN"]


def test_consumer_survival_is_final_but_early_terminal_survival_is_unknown() -> None:
    trigger = Int("CoastEventTrigger", default=1)
    effect = Int("CoastEventEffect")
    consumed = Int("CoastEventConsumed")
    never = Int("CoastEventNever")
    with Program() as program:
        with rung(trigger):
            copy(1, effect)
            copy(0, trigger)
        with rung(effect == 1):
            copy(1, consumed)
    consumer_obligation = EffectObligation(
        effect.name,
        1,
        (None, 0, ()),
        (None, 1, ()),
        ((effect.name, 1),),
        producer_rung=program.rungs[0],
        consumer_rung=program.rungs[1],
    )
    terminal_obligation = EffectObligation(
        effect.name,
        1,
        (None, 0, ()),
        None,
        (),
        producer_rung=program.rungs[0],
    )
    plc = PLC(program)
    session = CoastSession(plc, kind="expectation-event")
    session.arm_pens((effect.name,))

    receipt = session.seek(
        [value_trigger(plc, "never", TARGET, never.name, 1)],
        budget=80,
    )
    observations = observe_execution_window(
        EffectExpectation((consumer_obligation, terminal_obligation)),
        plc,
        scan_before=0,
        coast_receipt=receipt,
        timeline=session.events,
    )

    assert receipt.logical_scans > 1
    assert [item.disposition for item in observations] == ["SURVIVED", "UNKNOWN"]
    assert observations[0].consumer_read is not None
    assert observations[0].consumer_read.scan_id == 1


def test_exact_coast_events_compose_producer_then_later_overwriter() -> None:
    phase = Int("CoastExactOverwritePhase", external=True)
    effect = Int("CoastExactOverwriteEffect")
    with Program() as program:
        with rung(phase == 0):
            copy(1, effect)
        with rung(phase == 1):
            copy(2, effect)
    plc = PLC(program)
    plc.patch({phase.name: 0})
    plc.step()
    plc.patch({phase.name: 1})
    plc.step()
    events = (
        CoastTriggerEvent("producer", "pen", 1, ((effect.name, 0, 1),)),
        CoastTriggerEvent("overwriter", "pen", 2, ((effect.name, 1, 2),)),
    )
    receipt = CoastReceipt(
        "bearing",
        0,
        2,
        "departed",
        ("overwriter",),
        events,
        2,
        kernel_scans=2,
    )
    expectation = EffectExpectation((_terminal_obligation(program, effect.name, 1),))

    observations = observe_execution_window(
        expectation,
        plc,
        scan_before=0,
        coast_receipt=receipt,
        timeline=events,
    )

    assert [item.disposition for item in observations] == ["OVERWRITTEN"]
    assert observations[0].appeared is not None
    assert observations[0].appeared.scan_id == 1
    assert observations[0].displacement is not None
    assert observations[0].displacement.scan_id == 2
    assert [
        (read.occurrence.name, read.occurrence.value) for read in observations[0].observed_reads
    ] == [(phase.name, 1)]


def test_coast_preserves_all_repeated_producer_occurrence_results() -> None:
    effect = Int("CoastRepeatedEffect")

    @subroutine("CoastRepeatedProducer", strict=False)
    def producer() -> None:
        with rung():
            copy(1, effect)

    with Program() as program:
        with rung():
            call(producer)
            call(producer)
    obligation = EffectObligation(
        effect.name,
        1,
        ("CoastRepeatedProducer", 0, ()),
        None,
        (),
        producer_rung=program.subroutines["CoastRepeatedProducer"][0],
    )
    plc = PLC(program)
    plc.step()
    plc.step()
    events = (
        CoastTriggerEvent("first", "pen", 1, ((effect.name, 0, 1),)),
        CoastTriggerEvent("second", "pen", 2, ((effect.name, 1, 1),)),
    )
    receipt = CoastReceipt(
        "bearing",
        0,
        2,
        "reached",
        ("second",),
        events,
        2,
        kernel_scans=2,
    )

    observations = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        coast_receipt=receipt,
        timeline=events,
    )

    assert [item.disposition for item in observations] == [
        "OVERWRITTEN",
        "OVERWRITTEN",
        "OVERWRITTEN",
        "SURVIVED",
    ]
    assert [item.appeared.scan_id for item in observations if item.appeared is not None] == [
        1,
        1,
        2,
        2,
    ]


def test_coast_landing_writer_is_retained_without_claiming_gap_coverage() -> None:
    counter = Int("LandingWriterCounter")
    effect = Int("LandingWriterEffect")
    with Program() as program:
        with rung(counter == 3):
            copy(1, effect)
        with rung():
            calc(counter + 1, counter)
    plc = PLC(program)
    expectation = EffectExpectation((_terminal_obligation(program, effect.name, 1),))
    session = CoastSession(plc, kind="expectation-landing")

    receipt = session.seek(
        [value_trigger(plc, "effect", TARGET, effect.name, 1)],
        budget=10,
    )
    observations = observe_execution_window(
        expectation,
        plc,
        scan_before=0,
        coast_receipt=receipt,
        timeline=session.events,
    )

    assert receipt.stop_reason == "reached"
    assert [item.disposition for item in observations] == ["SURVIVED"]
    assert observations[0].appeared is not None
    assert observations[0].appeared.scan_id == receipt.end_scan


def test_channel_abort_receipt_remains_authoritative_with_unknown_effect() -> None:
    receipt = CoastReceipt(
        "bearing",
        10,
        20,
        "departed",
        ("ejected",),
        (),
        20,
        kernel_scans=5,
        macro_folds=1,
    )
    trial = SimpleNamespace(
        snap={"State": 91},
        action_snap={"State": 40},
        coast_receipt=receipt,
    )

    motion = _owned_channel_motion(trial, ChannelMotion("State", 80))

    assert motion.departed
    assert motion.stop_reason == "departed"


def test_execution_and_recording_retain_only_detached_effect_observations() -> None:
    effect = Int("DetachedExpectationEffect")
    with Program() as program:
        with rung():
            copy(1, effect)
    expectation = EffectExpectation((_terminal_obligation(program, effect.name, 1),))
    policy = ActPolicy(ActSource.TRACE, expectation=expectation)
    bearing = Bearing((), Pulse(policy), BearingObjective(TargetSpec("Target", True)))
    plc = PLC(program)
    plc.step()
    raw = observe_execution_window(expectation, plc, scan_before=0, action_scan=1)
    snapshots = tuple(item.diagnostic_snapshot() for item in raw)
    evidence = _ExecutionEvidence({}, {}, ChannelMotion(), None, (), snapshots)
    pulse = SimpleNamespace(
        fork=plc,
        scan_before=0,
        action_scan=1,
        post_pulse_snap={},
    )
    accepted = _AcceptedTrial(
        _ExecutedAttempt(pulse, bearing, raw),  # type: ignore[arg-type]
        evidence,
        TargetReached(),
    )
    frame = SimpleNamespace(
        tree=SimpleNamespace(pivot_tags=lambda: set(), tag="Target", value=True),
        distance_before=1,
    )
    state = SimpleNamespace(watch_tags=set(), seen_keys=set())

    payload = _accepted_payload(policy, accepted, frame, state)

    assert payload["effect_observations"] == snapshots

    def _walk(value: object) -> None:
        assert not isinstance(value, PLC)
        assert type(value).__name__ not in {
            "RungRun",
            "InstructionRun",
            "ReadOccurrence",
            "WriteOccurrence",
        }
        if isinstance(value, Mapping):
            for key, member in value.items():
                _walk(key)
                _walk(member)
        elif isinstance(value, tuple | list):
            for member in value:
                _walk(member)
        elif is_dataclass(value):
            for declared in fields(value):
                _walk(getattr(value, declared.name))

    _walk(payload["effect_observations"])
