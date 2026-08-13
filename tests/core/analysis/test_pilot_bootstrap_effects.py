"""Exact ordered-projection classifications used by bootstrap designation."""

from pyrung import PLC, Bool, Int, Program, branch, call, copy, reset, rung, subroutine
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.bootstrap import bootstrap_designations
from pyrung.core.analysis.pilot.pilot import pilot_events
from pyrung.core.analysis.pilot.trace import TraceNode
from pyrung.core.analysis.pilot.types import BootstrapExecutionSnapshot


def _projection(program: Program):
    plc = PLC(program)
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    return projection


def _only(observations):
    assert len(observations) == 1
    return observations[0]


def test_consumer_relative_survival_allows_later_value_advance() -> None:
    effect = Int("BootstrapSurvivedEffect")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung(effect == 1):
            copy(2, effect)

    projection = _projection(program)
    observation = _only(
        projection.observe_appeared_handoff(
            effect.name,
            1,
            producer_rung=program.rungs[0],
            consumer_rung=program.rungs[1],
            required_shape=((effect.name, 1),),
        )
    )

    assert observation.disposition == "SURVIVED"
    assert observation.consumer_read is not None
    assert observation.appeared.ordinal < observation.consumer_read.ordinal
    assert observation.displacement is None


def test_exact_overwrite_precedes_consumer_read() -> None:
    effect = Int("BootstrapOverwrittenEffect")
    out = Int("BootstrapOverwrittenOut")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung():
            copy(2, effect)
        with rung(effect == 1):
            copy(1, out)

    projection = _projection(program)
    observation = _only(
        projection.observe_appeared_handoff(
            effect.name,
            1,
            producer_rung=program.rungs[0],
            consumer_rung=program.rungs[2],
            required_shape=((effect.name, 1),),
        )
    )

    assert observation.disposition == "OVERWRITTEN"
    assert observation.consumer_read is None
    assert observation.displacement is not None
    assert observation.appeared.ordinal < observation.displacement.ordinal
    assert observation.displacement.transition.to_value == 2


def test_appeared_value_without_a_later_consumer_read_is_stranded() -> None:
    effect = Int("BootstrapStrandedEffect")
    out = Int("BootstrapStrandedOut")
    with Program() as program:
        with rung(effect == 1):
            copy(1, out)
        with rung():
            copy(1, effect)

    observation = _only(
        _projection(program).observe_appeared_handoff(
            effect.name,
            1,
            producer_rung=program.rungs[1],
            consumer_rung=program.rungs[0],
            required_shape=((effect.name, 1),),
        )
    )

    assert observation.disposition == "STRANDED"
    assert observation.consumer_read is None
    assert observation.observed_reads == ()


def test_exact_required_shape_displacement_is_identified() -> None:
    effect = Int("BootstrapDisplacedEffect")
    latch = Bool("BootstrapDisplacedLatch", default=True)
    out = Int("BootstrapDisplacedOut")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung():
            reset(latch)
        with rung(effect == 1, latch):
            copy(1, out)

    observation = _only(
        _projection(program).observe_appeared_handoff(
            effect.name,
            1,
            producer_rung=program.rungs[0],
            consumer_rung=program.rungs[2],
            required_shape=((effect.name, 1), (latch.name, True)),
        )
    )

    assert observation.disposition == "DISPLACED"
    assert observation.displaced_read is not None
    assert observation.displaced_read.occurrence.name == latch.name
    assert observation.displaced_read.occurrence.value is False
    assert observation.displacement is not None
    assert observation.displacement.transition.tag_name == latch.name
    assert observation.displacement.transition.to_value is False


def test_missing_designation_is_not_an_absent_failure() -> None:
    effect = Int("BootstrapMissingEffect")
    with Program() as program:
        with rung():
            copy(1, effect)

    observations = _projection(program).observe_appeared_handoff(
        effect.name,
        99,
        producer_rung=program.rungs[0],
        consumer_rung=None,
    )

    assert observations == ()


def test_repeated_appeared_producer_occurrences_are_each_classified() -> None:
    effect = Int("BootstrapAmbiguousEffect")
    with Program() as program:
        with rung():
            copy(1, effect)
            copy(0, effect)
            copy(1, effect)

    observations = _projection(program).observe_appeared_handoff(
        effect.name,
        1,
        producer_rung=program.rungs[0],
        consumer_rung=None,
    )

    assert tuple(observation.disposition for observation in observations) == (
        "OVERWRITTEN",
        "SURVIVED",
    )
    assert observations[0].displacement is not None
    assert observations[0].displacement.transition.to_value == 0
    assert observations[0].appeared.ordinal < observations[1].appeared.ordinal


def test_same_value_intervening_write_displaces_exact_producer_identity() -> None:
    effect = Int("BootstrapSameValueEffect")
    out = Int("BootstrapSameValueOut")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung():
            copy(1, effect)
        with rung(effect == 1):
            copy(1, out)

    projection = _projection(program)
    observation = _only(
        projection.observe_appeared_handoff(
            effect.name,
            1,
            producer_rung=program.rungs[0],
            consumer_rung=program.rungs[2],
            required_shape=((effect.name, 1),),
        )
    )

    assert observation.disposition == "OVERWRITTEN"
    assert observation.consumer_read is None
    assert observation.displacement is not None
    assert observation.displacement.run.rung is program.rungs[1]
    consumer_read = next(
        read
        for read in projection.reads
        if read.run.rung is program.rungs[2] and read.occurrence.name == effect.name
    )
    assert consumer_read.occurrence.source is observation.displacement.occurrence


def test_guard_false_stranding_retains_all_exact_consumer_reads() -> None:
    effect = Int("BootstrapGuardFalseEffect")
    permit = Bool("BootstrapGuardFalsePermit")
    out = Int("BootstrapGuardFalseOut")
    with Program() as program:
        with rung():
            copy(1, effect)
        with rung(effect == 1, permit):
            copy(1, out)

    observation = _only(
        _projection(program).observe_appeared_handoff(
            effect.name,
            1,
            producer_rung=program.rungs[0],
            consumer_rung=program.rungs[1],
            required_shape=((effect.name, 1), (permit.name, True)),
        )
    )

    assert observation.disposition == "STRANDED"
    assert observation.consumer_read is not None
    assert tuple(
        (read.occurrence.name, read.occurrence.value) for read in observation.observed_reads
    ) == ((effect.name, 1), (permit.name, False))


def test_repeated_subroutine_calls_resolve_each_source_linked_consumer() -> None:
    effect = Int("BootstrapRepeatedCallEffect")
    with Program() as program:
        with subroutine("BootstrapRepeatedCall"):
            with rung():
                copy(1, effect)
            with rung(effect == 1):
                copy(2, effect)

        with rung():
            call("BootstrapRepeatedCall")
        with rung():
            call("BootstrapRepeatedCall")

    projection = _projection(program)
    producer = program.subroutines["BootstrapRepeatedCall"][0]
    consumer = program.subroutines["BootstrapRepeatedCall"][1]
    observations = projection.observe_appeared_handoff(
        effect.name,
        1,
        producer_rung=producer,
        consumer_rung=consumer,
        producer_address=("BootstrapRepeatedCall", 0, ()),
        consumer_address=("BootstrapRepeatedCall", 1, ()),
        required_shape=((effect.name, 1),),
    )

    assert tuple(observation.disposition for observation in observations) == (
        "SURVIVED",
        "SURVIVED",
    )
    assert tuple(observation.appeared.run.caller_rung for observation in observations) == (0, 1)
    assert tuple(
        observation.consumer_read.run.caller_rung
        for observation in observations
        if observation.consumer_read is not None
    ) == (0, 1)
    assert all(
        observation.consumer_read is not None
        and observation.consumer_read.occurrence.source is observation.appeared.occurrence
        and observation.consumer_read.call_invocation == observation.appeared.call_invocation
        for observation in observations
    )


def test_exact_source_in_a_later_subroutine_call_does_not_credit_the_wrong_call() -> None:
    mode = Int("BootstrapCrossCallerMode")
    effect = Int("BootstrapCrossCallerEffect")
    out = Int("BootstrapCrossCallerOut")
    with Program() as program:
        with subroutine("BootstrapCrossCaller"):
            with rung(mode == 0):
                copy(1, effect)
            with rung(mode == 1, effect == 1):
                copy(1, out)

        with rung():
            copy(0, mode)
            call("BootstrapCrossCaller")
        with rung():
            copy(1, mode)
            call("BootstrapCrossCaller")

    projection = _projection(program)
    producer = program.subroutines["BootstrapCrossCaller"][0]
    consumer = program.subroutines["BootstrapCrossCaller"][1]
    observation = _only(
        projection.observe_appeared_handoff(
            effect.name,
            1,
            producer_rung=producer,
            consumer_rung=consumer,
            producer_address=("BootstrapCrossCaller", 0, ()),
            consumer_address=("BootstrapCrossCaller", 1, ()),
            required_shape=((effect.name, 1),),
        )
    )

    later_read = next(
        read
        for read in projection.reads
        if read.run.rung is consumer
        and read.occurrence.name == effect.name
        and read.run.caller_rung == 1
    )
    assert later_read.occurrence.source is observation.appeared.occurrence
    assert observation.disposition == "STRANDED"
    assert observation.consumer_read is None
    assert {read.run.caller_rung for read in observation.observed_reads} == {0}


def test_two_calls_from_one_caller_rung_keep_distinct_invocation_identity() -> None:
    mode = Int("BootstrapSameCallerMode")
    effect = Int("BootstrapSameCallerEffect")
    out = Int("BootstrapSameCallerOut")
    with Program() as program:
        with subroutine("BootstrapSameCaller"):
            with rung(mode == 0):
                copy(1, effect)
            with rung(mode == 1, effect == 1):
                copy(1, out)

        with rung():
            copy(0, mode)
            call("BootstrapSameCaller")
            copy(1, mode)
            call("BootstrapSameCaller")

    projection = _projection(program)
    producer = program.subroutines["BootstrapSameCaller"][0]
    consumer = program.subroutines["BootstrapSameCaller"][1]
    observation = _only(
        projection.observe_appeared_handoff(
            effect.name,
            1,
            producer_rung=producer,
            consumer_rung=consumer,
            producer_address=("BootstrapSameCaller", 0, ()),
            consumer_address=("BootstrapSameCaller", 1, ()),
            required_shape=((effect.name, 1),),
        )
    )

    later_read = next(
        read
        for read in projection.reads
        if read.run.rung is consumer and read.occurrence.name == effect.name
    )
    assert later_read.occurrence.source is observation.appeared.occurrence
    assert later_read.run.caller_rung == observation.appeared.run.caller_rung == 0
    assert later_read.run.call_stack == observation.appeared.run.call_stack
    assert later_read.call_invocation != observation.appeared.call_invocation
    assert observation.disposition == "STRANDED"
    assert observation.consumer_read is None


def test_designation_includes_non_target_program_written_handoff_on_selected_path() -> None:
    handoff = Int("BootstrapSelectedHandoff")
    target = Int("BootstrapSelectedHandoffTarget")
    with Program() as program:
        with rung():
            copy(1, handoff)
        with rung(handoff == 1):
            copy(2, target)

    events = pilot_events(PLC(program), target == 2, max_scans=5)
    try:
        assert next(events).kind == "started"
        observed = next(event for event in events if event.kind == "entry_scan_observed")
    finally:
        events.close()

    snapshot = observed.data["execution"]
    assert isinstance(snapshot, BootstrapExecutionSnapshot)
    handoff_designation = next(
        designation for designation in snapshot.designations if designation.tag == handoff.name
    )
    assert handoff_designation.producer == (None, 0, ())
    assert handoff_designation.consumer == (None, 1, ())
    assert any(
        effect.designation == handoff_designation and effect.disposition == "SURVIVED"
        for effect in snapshot.appeared_effects
    )


def test_designation_retains_full_static_branch_path_and_dynamic_address() -> None:
    gate = Bool("BootstrapBranchPathGate", default=True)
    target = Int("BootstrapBranchPathTarget")
    with Program() as program:
        with rung():
            with branch(gate):
                copy(1, target)

    events = pilot_events(PLC(program), target == 1, max_scans=5)
    try:
        assert next(events).kind == "started"
        observed = next(event for event in events if event.kind == "entry_scan_observed")
    finally:
        events.close()

    snapshot = observed.data["execution"]
    assert isinstance(snapshot, BootstrapExecutionSnapshot)
    designation = next(item for item in snapshot.designations if item.tag == target.name)
    assert designation.producer[:2] == (None, 0)
    assert designation.producer[2]
    assert designation.path[0][2] == designation.producer
    effect = next(item for item in snapshot.appeared_effects if item.designation == designation)
    assert effect.appeared.run_order >= 0
    assert effect.appeared.execution_kind == "branch"
    assert effect.appeared.rung == (None, 0)
    assert effect.appeared.call_stack == ()


def _designation_below_excluded_parent(*, heuristic: bool, relational: bool):
    effect = Int(f"BootstrapExcludedEffect{heuristic}{relational}")
    with Program() as program:
        with rung():
            copy(1, effect)
    pdg = build_program_graph(program)
    writer = next(iter(pdg.writers_of[effect.name]))
    child = TraceNode(tag=effect.name, value=1, writer_rung=writer)
    trace = TraceNode(
        tag=f"BootstrapExcludedParent{heuristic}{relational}",
        value=True,
        heuristic=heuristic,
        relational=relational,
        children=[child],
    )
    return bootstrap_designations(
        trace,
        pdg,
        program,
        steerable=frozenset(),
    )


def test_heuristic_parent_prunes_concrete_writer_descendants() -> None:
    assert _designation_below_excluded_parent(heuristic=True, relational=False) == ()


def test_relational_parent_prunes_concrete_writer_descendants() -> None:
    assert _designation_below_excluded_parent(heuristic=False, relational=True) == ()
