"""Forward-proof tests for one exact program-owned producer."""

from __future__ import annotations

from pyrung import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    call,
    copy,
    latch,
    on_delay,
    reset,
    subroutine,
)
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot._ops import PilotRung
from pyrung.core.analysis.pilot.currents import Producer, WorldView, sibling_producer_family
from pyrung.core.analysis.pilot.program_step import (
    ProgramStepStatus,
    read_program_step,
)
from pyrung.core.analysis.steerable import compute_steerable


def _timer_producer_program(*, clobber: bool = False):
    run = Bool("Run", external=True)
    command = Int("Command")
    timer = Timer.clone("T")
    with Program(strict=False) as program:
        with Rung(run):
            on_delay(timer, 30, "ms")
        with Rung(timer.Done):
            copy(1, command)
        if clobber:
            with Rung():
                copy(0, command)
    return program, run, command, timer


def _world(program, plc):
    pdg = build_program_graph(program)
    steerable = frozenset(compute_steerable(pdg, plc._known_tags_by_name, program))
    return WorldView(
        snapshot=dict(plc.state.tags),
        pdg=pdg,
        program=program,
        steerable=steerable,
        opaque_loop=frozenset(),
    )


def _producer(world):
    family = sibling_producer_family(world, "Command", 1)
    assert family is not None
    assert len(family.program_owned) == 1
    return family.program_owned[0]


def test_running_timer_proves_progress_at_the_immediate_boundary() -> None:
    program, run, _command, timer = _timer_producer_program()
    plc = PLC(program, dt=0.010)
    plc.patch({run.name: True})
    plc.step()
    world = _world(program, plc)

    result = read_program_step(world, _producer(world), plc)

    assert result.status is ProgramStepStatus.KEEP_RUNNING
    assert result.channel == timer.Acc.name
    assert result.boundary is not None
    assert result.boundary.tag == timer.Acc.name
    assert result.projected_changes


def test_stopped_timer_surfaces_its_current_external_input() -> None:
    program, run, _command, _timer = _timer_producer_program()
    plc = PLC(program, dt=0.010)
    plc.step()
    world = _world(program, plc)

    result = read_program_step(world, _producer(world), plc)

    assert result.status is ProgramStepStatus.NEEDS_INPUT
    assert tuple(action.pair for action in result.required_inputs) == ((run.name, True),)


def test_projection_rebuilds_already_installed_pilot_holds() -> None:
    program, run, command, timer = _timer_producer_program()
    plc = PLC(program, dt=0.010)
    plc.step()
    world = _world(program, plc)
    holds = (PilotRung(run.name, True, command == 0),)

    result = read_program_step(world, _producer(world), plc, holds)

    assert result.status is ProgramStepStatus.KEEP_RUNNING
    assert result.channel == timer.Acc.name


def test_later_writer_clobber_is_not_reported_as_forward_motion() -> None:
    program, run, _command, _timer = _timer_producer_program(clobber=True)
    plc = PLC(program, dt=0.010)
    plc.patch({run.name: True})
    plc.run(cycles=4)
    world = _world(program, plc)

    result = read_program_step(world, _producer(world), plc)

    assert result.status is ProgramStepStatus.UNCLEAR
    assert "did not" in result.reason or "survive" in result.reason


def test_projection_traces_the_exact_producer_occurrence_view() -> None:
    gate = Bool("Gate")
    command = Int("Command")

    @subroutine("IssueCommand")
    def issue_command():
        with Rung(gate):
            copy(1, command)

    with Program(strict=False) as program:
        with Rung():
            latch(gate)
            call(issue_command)
            reset(gate)

    plc = PLC(program)
    world = _world(program, plc)
    rung_index = next(iter(world.pdg.writers_of[command.name]))
    producer = Producer(
        rung_index=rung_index,
        kind="program",
        guard_tags=frozenset((gate.name,)),
        co_writes=frozenset(),
        command_tag=command.name,
        command_value=1,
    )

    result = read_program_step(world, producer, plc)

    assert result.status is ProgramStepStatus.KEEP_RUNNING
    assert result.trace is not None
    assert result.trace.children[0].tag == gate.name
    assert result.trace.children[0].satisfied is True
    assert plc.state.tags.get(gate.name, False) is False


def test_repeated_producer_occurrences_decline_instead_of_using_the_last_view() -> None:
    command = Int("RepeatedCommand")

    @subroutine("RepeatedProducer")
    def repeated_producer():
        with Rung():
            copy(1, command)

    with Program(strict=False) as program:
        with Rung():
            call(repeated_producer)
            call(repeated_producer)

    plc = PLC(program)
    world = _world(program, plc)
    rung_index = next(iter(world.pdg.writers_of[command.name]))
    producer = Producer(
        rung_index=rung_index,
        kind="program",
        guard_tags=frozenset(),
        co_writes=frozenset(),
        command_tag=command.name,
        command_value=1,
    )

    result = read_program_step(world, producer, plc)

    assert result.status is ProgramStepStatus.UNCLEAR
    assert "more than once" in result.reason
