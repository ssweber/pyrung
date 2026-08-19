"""Disposable evidence boundary for occurrence-local traceback research."""

from types import SimpleNamespace

from pyrung import PLC, Bool, Int, Or, Program, Rung, branch, call, copy, out, subroutine
from pyrung.core.analysis.pilot.attempt_observation import _observe_intrascan_act_occurrence
from pyrung.core.analysis.pilot.effects import occurrence_snapshot
from pyrung.core.analysis.pilot.intrascan_research import (
    IntrascanCausalRelation,
    research_intrascan_boundary_realization,
    research_intrascan_traceback,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    IntrascanTracebackRequest,
    ProgramScan,
)
from pyrung.core.context import RungId
from pyrung.core.crossing import Cmp
from pyrung.core.executor import ConditionViewCapture
from pyrung.core.intrascan_counterfactual import CounterfactualPatch, OccurrenceBoundary
from pyrung.core.state import SystemState


def test_traceback_research_detaches_exact_handoff_without_advancing_source() -> None:
    link = Bool("TraceLink", external=True)
    step = Int("TraceStep", default=10)

    @subroutine("trace_route")
    def trace_route() -> None:
        with Rung(~link, step <= 20):
            copy(98, step)
        with Rung(link):
            with branch(step == 98):
                copy(10, step)

    with Program(strict=False) as program:
        with Rung():
            call(trace_route)

    source = PLC(program)
    patch = CounterfactualPatch(
        link.name,
        True,
        ~link,
        OccurrenceBoundary(
            rung_id=RungId("trace_route", 1),
            execution_kind="subroutine",
            caller_rung=0,
            call_stack=("trace_route",),
            depth=1,
            call_invocation=0,
        ),
    )
    request = IntrascanTracebackRequest(patch=patch, requirements=())

    witness = research_intrascan_traceback(request, source, ())

    assert witness.applied_exactly_once
    assert witness.application_values == ((False, True),)
    assert any(write[4:] == (step.name, 98, 10) for write in witness.downstream_writes)
    traceback = witness.traceback_step
    assert traceback is not None
    assert (
        traceback.useful_write.boundary.rung_id,
        traceback.useful_write.tag,
        traceback.useful_write.before,
        traceback.useful_write.after,
    ) == (RungId("trace_route", 1), step.name, 98, 10)
    assert tuple(
        (requirement.tag, requirement.value, requirement.source_kind)
        for requirement in traceback.consumer_requirements
    ) == (
        (link.name, True, "counterfactual_write"),
        (step.name, 98, "program_write"),
    )
    assert len(traceback.producer_traces) == 1
    producer = traceback.producer_traces[0]
    assert (
        producer.write.boundary.rung_id,
        producer.write.tag,
        producer.write.before,
        producer.write.after,
    ) == (RungId("trace_route", 0), step.name, 10, 98)
    assert tuple(
        (requirement.tag, requirement.value, requirement.source_kind)
        for requirement in producer.enabling_requirements
    ) == (
        (link.name, False, "entry"),
        (step.name, 10, "entry"),
    )
    assert (step.name, 10, 10) not in witness.exit_changes
    assert source.state.scan_id == 0
    assert source.state.tags[step.name] == 10
    assert source.state.tags[link.name] is False

    realization = research_intrascan_boundary_realization(
        request,
        witness,
        source,
        (),
    )

    assert realization.witnessed
    assert realization.stage_scan == 1
    assert realization.consumer_scan == 2
    assert realization.consumer_assignments == ((link.name, True),)
    assert realization.stage_write is not None
    assert realization.consumer_write is not None
    assert (
        realization.stage_write.boundary.rung_id,
        realization.stage_write.tag,
        realization.stage_write.before,
        realization.stage_write.after,
    ) == (RungId("trace_route", 0), step.name, 10, 98)
    assert (
        realization.consumer_write.boundary.rung_id,
        realization.consumer_write.tag,
        realization.consumer_write.before,
        realization.consumer_write.after,
    ) == (RungId("trace_route", 1), step.name, 98, 10)
    assert source.state.scan_id == 0

    stage = source.fork()
    stage.step()
    stage_projection = stage._replay_rung_write_projection_at(1)
    assert stage_projection is not None
    assert sum(realization.stage_write.matches(write) for write in stage_projection.writes) == 1
    assert stage.state.tags[realization.stage_write.tag] == realization.stage_write.after

    stage_receipt = _observe_intrascan_act_occurrence(
        SimpleNamespace(
            bearing=SimpleNamespace(
                act=ProgramScan(realization.stage_write, ("traceback-finding", 1)),
            ),
            pulse=SimpleNamespace(
                scan_before=0,
                snap=dict(stage.state.tags),
            ),
            projection_at=lambda scan_id: stage_projection if scan_id == 1 else None,
        )
    )
    assert stage_receipt.witnessed
    assert stage_receipt.matching_writes == 1
    assert stage_receipt.retained


def test_direct_boundary_reads_rearmed_oneshot_from_instruction_memory() -> None:
    """An absent hidden key means the consumer one-shot is ordinarily rearmed."""

    link = Bool("DirectTraceLink", external=True)
    step = Int("DirectTraceStep", default=98)

    with Program(strict=False) as program:
        with Rung(~link, step <= 20):
            copy(98, step, oneshot=True)
        with Rung(link):
            with branch(step == 98):
                copy(10, step, oneshot=True)

    consumer = program.rungs[1]._branches[0]._instructions[0]
    edge_key = consumer.memory_key("_oneshot")
    source = PLC(
        program,
        initial_state=(
            SystemState(scan_id=1).with_tags({step.name: 98}).with_memory({edge_key: False})
        ),
    )
    request = IntrascanTracebackRequest(
        patch=CounterfactualPatch(
            link.name,
            True,
            ~link,
            OccurrenceBoundary(
                rung_id=RungId(None, 0),
                execution_kind="rung",
                caller_rung=0,
                call_stack=(),
                depth=0,
                call_invocation=None,
                run_order=0,
            ),
        ),
        requirements=(),
    )

    witness = research_intrascan_traceback(request, source, ())
    realization = research_intrascan_boundary_realization(
        request,
        witness,
        source,
        (),
    )

    assert witness.traceback_step is not None
    assert any(
        requirement.tag.startswith("_oneshot:")
        and requirement.value is False
        and requirement.source_kind == "entry"
        for requirement in witness.traceback_step.consumer_requirements
    )
    assert realization.witnessed
    assert realization.direct
    assert realization.consumer_assignments == ((link.name, True),)
    assert realization.consumer_write is not None
    assert realization.consumer_write.after == 10
    assert source.state.scan_id == 1
    assert source.state.tags[step.name] == 98


def test_program_owned_branch_need_realizes_through_one_natural_stage_scan() -> None:
    link = Bool("OwnedTraceLink", external=True)
    step = Int("OwnedTraceStep", default=20)

    @subroutine("owned_trace_route")
    def owned_trace_route() -> None:
        with Rung(~link, step <= 20):
            copy(98, step)
        with Rung(link):
            with branch(step == 98):
                copy(10, step)
            with branch(step <= 20):
                copy(94, step)

    with Program(strict=False) as program:
        with Rung():
            call(owned_trace_route)

    source = PLC(program)
    patch = CounterfactualPatch(
        step.name,
        98,
        step != 98,
        OccurrenceBoundary(
            rung_id=RungId("owned_trace_route", 1),
            execution_kind="branch",
            caller_rung=0,
            call_stack=("owned_trace_route",),
            depth=2,
            call_invocation=0,
            run_order=3,
        ),
    )
    request = IntrascanTracebackRequest(
        patch=patch,
        requirements=(),
        consumer_assignments=((link.name, True),),
    )

    witness = research_intrascan_traceback(request, source, ())
    realization = research_intrascan_boundary_realization(
        request,
        witness,
        source,
        (),
    )

    assert witness.applied_exactly_once
    assert witness.traceback_step is not None
    assert witness.traceback_step.useful_write.after == 10
    assert realization.witnessed
    assert realization.staged
    assert realization.consumer_assignments == ((link.name, True),)
    assert realization.stage_write is not None
    assert realization.stage_write.tag == step.name
    assert realization.stage_write.after == 98
    assert tuple(
        (requirement.tag, requirement.value, requirement.source_kind)
        for requirement in realization.stage_requirements
    ) == (
        (link.name, False, "entry"),
        (step.name, 20, "entry"),
    )
    assert realization.consumer_write is not None
    assert realization.consumer_write.after == 10
    assert source.state.scan_id == 0
    assert source.state.tags[step.name] == 20
    assert source.state.tags[link.name] is False


def test_unavailable_program_producer_becomes_an_exact_navigation_goal() -> None:
    """A proven consumer hop stays open past an earlier overwritten Reset."""

    link = Bool("FrontierTraceLink", external=True)
    reset = Bool("FrontierTraceReset", external=True)
    step = Int("FrontierTraceStep", default=98)

    @subroutine("frontier_trace_route")
    def frontier_trace_route() -> None:
        with Rung(~link, step <= 20):
            copy(98, step, oneshot=True)
        with Rung(link):
            with branch(step == 98):
                copy(10, step, oneshot=True)

    with Program(strict=False) as program:
        # This is the neutral ClickNick ordering. The executable World enters
        # at 98. Reset can create the needed <=20 value early, but the normal
        # main-program cascade then carries either 98 or 10 through 22 to 40
        # before the later subroutine producer reads it.
        with Rung(reset, step >= 90):
            copy(10, step, oneshot=True)
        with Rung(Or(step == 98, step == 10)):
            copy(22, step)
        with Rung(step == 22):
            copy(40, step)
        with Rung():
            call(frontier_trace_route)

    source = PLC(program)
    patch = CounterfactualPatch(
        step.name,
        98,
        step != 98,
        OccurrenceBoundary(
            rung_id=RungId("frontier_trace_route", 1),
            execution_kind="branch",
            caller_rung=3,
            call_stack=("frontier_trace_route",),
            depth=2,
            call_invocation=0,
            branch_path=(0,),
        ),
    )
    request = IntrascanTracebackRequest(
        patch=patch,
        requirements=(),
        consumer_assignments=((link.name, True),),
    )

    witness = research_intrascan_traceback(request, source, ())
    realization = research_intrascan_boundary_realization(
        request,
        witness,
        source,
        (),
    )

    assert witness.applied_exactly_once
    assert witness.traceback_step is not None
    assert witness.traceback_step.producer_traces == ()
    assert not realization.witnessed
    assert len(realization.unresolved_producer_goals) == 1
    goal = realization.unresolved_producer_goals[0]
    assert (goal.tag, goal.value) == (step.name, 98)
    assert goal.rung_id == RungId("frontier_trace_route", 0)
    assert goal.branch_path == ()
    assert dict(goal.observed_values) == {
        link.name: False,
        step.name: 40,
    }
    assert tuple(
        tuple((atom.tag, atom.form, atom.operand) for atom in alternative)
        for alternative in goal.guard_alternatives
    ) == (((link.name, "xio", None), (step.name, "le", 20)),)
    assert source.state.scan_id == 0
    assert source.state.tags[step.name] == 98

    natural = source.fork()
    natural.step()
    projection = natural._replay_rung_write_projection_at(1)
    assert projection is not None
    assert tuple(
        (
            write.transition.tag_name,
            write.transition.from_value,
            write.transition.to_value,
        )
        for write in projection.writes
        if write.transition.tag_name == step.name
    ) == (
        (step.name, 98, 22),
        (step.name, 22, 40),
    )
    assert natural.state.tags[step.name] == 40


def test_prevented_overwrite_becomes_a_condition_valued_producer_goal() -> None:
    """A negative edge preserves the hose value without inventing its patch."""

    step = Int("PreventionTraceStep", default=98)
    route_mode = Int("PreventionTraceMode", default=100)
    link = Bool("PreventionTraceLink", external=True)
    reset = Bool("PreventionTraceReset", external=True)
    mode_reset = Bool("PreventionTraceModeReset", external=True)

    @subroutine("prevention_trace_consumer")
    def prevention_trace_consumer() -> None:
        with Rung(~link, step <= 20):
            copy(98, step, oneshot=True)

    with Program(strict=False) as program:
        with Rung(mode_reset):
            copy(0, route_mode, oneshot=True)
        with Rung(reset, step >= 90):
            copy(10, step, oneshot=True)
        with Rung(Or(step == 98, step == 10), route_mode == 100):
            copy(22, step)
        with Rung(step == 22):
            copy(40, step)
        with Rung():
            call(prevention_trace_consumer)

    initial = SystemState(scan_id=1).with_tags({step.name: 98, route_mode.name: 100})
    source = PLC(program, initial_state=initial)

    # Capture the factual harmful write from the rejected Reset attempt. The
    # counterfactual is allowed to claim prevention only against this receipt.
    attempted = source.fork()
    attempted.patch({reset.name: True})
    attempted_capture = ConditionViewCapture()
    attempted._run_single_scan(
        consume_pause_request=False,
        execution_capture=attempted_capture,
    )
    attempted_projection = attempted._projection_from_capture(
        attempted.state.scan_id,
        attempted_capture,
    )
    assert attempted_projection is not None
    harmful = tuple(
        write
        for write in attempted_projection.writes
        if write.rung_id == RungId(None, 2)
        and write.transition.tag_name == step.name
        and write.transition.from_value == 10
        and write.transition.to_value == 22
    )
    assert len(harmful) == 1

    request = IntrascanTracebackRequest(
        patch=CounterfactualPatch(
            route_mode.name,
            99,
            route_mode == 100,
            OccurrenceBoundary(
                rung_id=RungId(None, 2),
                execution_kind="rung",
                caller_rung=2,
                call_stack=(),
                depth=0,
                call_invocation=None,
                branch_path=(),
            ),
        ),
        requirements=(),
        consumer_assignments=((reset.name, True),),
        required_condition=Cmp(route_mode.name, "!=", 100),
        prevented_write=occurrence_snapshot(harmful[0]),
    )

    witness = research_intrascan_traceback(request, source, ())

    assert witness.applied_exactly_once
    step_back = witness.traceback_step
    assert step_back is not None
    assert step_back.relation is IntrascanCausalRelation.PREVENTED_OVERWRITE
    assert step_back.prevented_write is not None
    assert (
        step_back.prevented_write.tag,
        step_back.prevented_write.before,
        step_back.prevented_write.after,
    ) == (step.name, 10, 22)
    assert step_back.preserved_read is not None
    assert (
        step_back.preserved_read.tag,
        step_back.preserved_read.value,
        step_back.preserved_read.source_kind,
    ) == (step.name, 10, "program_write")
    assert (
        step_back.useful_write.tag,
        step_back.useful_write.before,
        step_back.useful_write.after,
    ) == (step.name, 10, 98)
    assert step_back.producer_traces == ()

    realization = research_intrascan_boundary_realization(
        request,
        witness,
        source,
        (),
    )
    assert not realization.witnessed
    assert len(realization.unresolved_producer_goals) == 1
    goal = realization.unresolved_producer_goals[0]
    assert (goal.tag, goal.value, goal.rung_id) == (
        route_mode.name,
        0,
        RungId(None, 0),
    )

    # Once Compass has legitimately established the condition at scan start,
    # the retained witness can prove the ordinary Reset consumer directly.
    ready = PLC(
        program,
        initial_state=SystemState(scan_id=2).with_tags({step.name: 98, route_mode.name: 0}),
    )
    direct = research_intrascan_boundary_realization(request, witness, ready, ())
    assert direct.witnessed
    assert direct.direct
    assert direct.consumer_write is not None
    assert direct.consumer_write.after == 98
    assert source.state.scan_id == 1
    assert source.state.tags[route_mode.name] == 100


def test_exact_counterfactual_reports_a_retained_oneshot_edge_blocker() -> None:
    link = Bool("EdgeTraceLink", external=True)
    step = Int("EdgeTraceStep", default=40)

    with Program(strict=False) as program:
        with Rung(link):
            with branch(step == 98):
                copy(10, step, oneshot=True)

    consumer = program.rungs[0]._branches[0]._instructions[0]
    edge_key = consumer.memory_key("_oneshot")
    source = PLC(
        program,
        initial_state=SystemState()
        .with_tags({link.name: True, step.name: 40})
        .with_memory({edge_key: True}),
    )
    patch = CounterfactualPatch(
        step.name,
        98,
        step != 98,
        OccurrenceBoundary(
            rung_id=RungId(None, 0),
            execution_kind="branch",
            caller_rung=0,
            call_stack=(),
            depth=1,
            call_invocation=None,
            run_order=99,
            branch_path=(0,),
        ),
    )
    request = IntrascanTracebackRequest(
        patch=patch,
        requirements=(),
        consumer_assignments=((link.name, True),),
    )

    witness = research_intrascan_traceback(request, source, ())

    assert witness.applied_exactly_once
    assert witness.traceback_step is None
    assert len(witness.blocked_edges) == 1
    blocker = witness.blocked_edges[0]
    assert blocker.boundary.branch_path == (0,)
    assert blocker.instruction_path == (0,)
    assert (blocker.memory_key, blocker.observed, blocker.required) == (
        edge_key,
        True,
        False,
    )
    assert source.state.scan_id == 0
    assert source.state.memory[edge_key] is True


def test_real_program_producer_can_reach_the_consumer_horizon_without_a_fake_patch() -> None:
    start = Bool("NaturalHorizonStart", external=True)
    ready = Bool("NaturalHorizonReady")
    result = Int("NaturalHorizonResult")

    with Program(strict=False) as program:
        with Rung(start):
            out(ready)
        with Rung(ready):
            copy(1, result)

    source = PLC(program)
    request = IntrascanTracebackRequest(
        patch=CounterfactualPatch(
            ready.name,
            True,
            ~ready,
            OccurrenceBoundary(
                rung_id=RungId(None, 1),
                execution_kind="rung",
                caller_rung=1,
                call_stack=(),
                depth=0,
                call_invocation=None,
                run_order=1,
                branch_path=(),
            ),
        ),
        requirements=(),
        consumer_assignments=((start.name, True),),
        required_condition=Cmp(ready.name, "==", True),
    )

    witness = research_intrascan_traceback(request, source, ())

    assert not witness.applied_exactly_once
    assert witness.application_values == ()
    assert witness.consumer_stop_reached
    assert witness.consumer_horizon_read is not None
    assert (
        witness.consumer_horizon_read.tag,
        witness.consumer_horizon_read.value,
        witness.consumer_horizon_read.source_kind,
    ) == (ready.name, True, "program_write")

    realization = research_intrascan_boundary_realization(request, witness, source, ())
    assert realization.witnessed
    assert realization.consumer_stop_reached
    assert not realization.direct
    assert realization.consumer_write is None
    assert realization.consumer_assignments == ((start.name, True),)
    assert source.state.scan_id == 0
    assert source.state.tags[ready.name] is False
    assert source.state.tags[result.name] == 0
