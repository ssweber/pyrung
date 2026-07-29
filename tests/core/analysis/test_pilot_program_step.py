"""Forward-proof tests for one exact program-owned producer."""

from __future__ import annotations

from pyrung import (
    PLC,
    And,
    Bool,
    Int,
    Or,
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
from pyrung.core.analysis.pilot._ops import PilotRung, _settle_delayed_effects
from pyrung.core.analysis.pilot.awaited_actions import Producer, sibling_producer_family
from pyrung.core.analysis.pilot.evidence import PipelineRoles
from pyrung.core.analysis.pilot.program_step import (
    ProgramStepStatus,
    _first_advance,
    read_program_step,
)
from pyrung.core.analysis.pilot.trace import TraceNode
from pyrung.core.analysis.pilot.types import WorldView
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.crossing import Cmp, Eq
from pyrung.core.instruction.advance import AdvanceStep


def _timer_producer_program(
    *,
    clobber: bool = False,
    preset: int = 30,
    unit: str = "ms",
):
    run = Bool("Run", external=True)
    command = Int("Command")
    timer = Timer.clone("T")
    with Program(strict=False) as program:
        with Rung(run):
            on_delay(timer, preset, unit)
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


def test_first_advance_preserves_depth_first_trace_order() -> None:
    heat = AdvanceStep(Eq("HeatAcc", frozenset((60,))))
    fluff = AdvanceStep(Eq("FluffAcc", frozenset((5,))))
    tree = TraceNode(
        "Command",
        1,
        children=[
            TraceNode(
                "HeatBranch",
                True,
                children=[TraceNode("HeatAcc", 60, advance=heat)],
            ),
            TraceNode("FluffAcc", 5, advance=fluff),
        ],
    )

    assert _first_advance(tree) is heat


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


def test_running_timer_reports_progress_while_quantized_accumulator_stays_zero() -> None:
    program, run, _command, timer = _timer_producer_program(preset=2, unit="s")
    plc = PLC(program, dt=0.010)
    plc.patch({run.name: True})
    plc.step()
    world = _world(program, plc)

    result = read_program_step(world, _producer(world), plc)

    assert plc.state.tags[timer.Acc.name] == 0
    assert result.status is ProgramStepStatus.KEEP_RUNNING
    assert result.boundary is not None
    assert result.boundary.tag == timer.Acc.name
    assert "reports progress" in result.reason


def test_stopped_timer_surfaces_its_current_external_input() -> None:
    program, run, _command, timer = _timer_producer_program()
    plc = PLC(program, dt=0.010)
    plc.step()
    world = _world(program, plc)

    result = read_program_step(world, _producer(world), plc)

    assert result.status is ProgramStepStatus.NEEDS_INPUT
    assert tuple(action.pair for action in result.required_inputs) == ((run.name, True),)
    assert len(result.input_handoffs) == 1
    assert result.input_handoffs[0].action == (run.name, True)
    assert result.input_handoffs[0].channel == timer.Acc.name
    assert result.input_handoffs[0].boundary.tag == timer.Acc.name


def test_supplied_input_hands_the_timer_back_to_the_exact_producer_reader() -> None:
    """Generic pulse settlement must not consume the next owned operation."""
    program, run, _command, timer = _timer_producer_program()
    plc = PLC(program, dt=0.010)
    plc.step()
    stopped_world = _world(program, plc)
    stopped = read_program_step(stopped_world, _producer(stopped_world), plc)
    assert stopped.input_handoffs[0].channel == timer.Acc.name
    plc.patch({run.name: True})
    plc.step()
    scan_at_handoff = plc.state.scan_id

    receipts = _settle_delayed_effects(
        plc,
        scan_budget=500,
    )
    world = _world(program, plc)
    result = read_program_step(world, _producer(world), plc)

    assert receipts == []
    assert plc.state.scan_id == scan_at_handoff
    assert plc.state.tags[timer.TT.name] is True
    assert result.status is ProgramStepStatus.KEEP_RUNNING
    assert result.boundary is not None
    assert result.boundary.tag == timer.Acc.name


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


def test_unrelated_channel_motion_does_not_accept_a_bypassed_producer_input() -> None:
    """A safety transition cannot stand in for the exact producer firing."""
    hazard = Bool("Hazard", default=True, external=True)
    supply = Bool("Supply", external=True)
    state = Int("State", default=6)
    command = Int("Command")

    with Program(strict=False) as program:
        with Rung(And(hazard, state == 6)):
            copy(11, state)
        with Rung(And(state == 6, supply)):
            copy(10, command)

    plc = PLC(program)
    world = _world(program, plc)
    world = WorldView(
        **{
            **world.__dict__,
            "opaque_loop": frozenset((state.name,)),
            "pipeline_roles": (PipelineRoles(state.name, request_tags=frozenset((command.name,))),),
        }
    )
    rung_index = next(iter(world.pdg.writers_of[command.name]))
    producer = Producer(
        rung_index=rung_index,
        kind="program",
        guard_tags=frozenset((state.name, supply.name)),
        co_writes=frozenset(),
        command_tag=command.name,
        command_value=10,
    )
    result = read_program_step(world, producer, plc)

    assert result.status is ProgramStepStatus.INTERRUPTED
    assert result.required_inputs == ()
    assert "State moved" in result.reason
    assert "no longer current" in result.reason
    assert result.preserve_channels == ("State",)


def test_pipeline_motion_interrupts_an_owned_boundary_without_external_input() -> None:
    """An in-flight channel owner outranks a later timer producer reading."""
    hazard = Bool("OwnedHazard", default=True, external=True)
    state = Int("OwnedState", default=6)
    timer = Timer.clone("OwnedTimer")
    command = Int("OwnedCommand")

    with Program(strict=False) as program:
        with Rung(And(hazard, state == 6)):
            copy(11, state)
        with Rung(state == 6):
            on_delay(timer, 1, "s")
        with Rung(timer.Done):
            copy(10, command)

    plc = PLC(program)
    world = _world(program, plc)
    world = WorldView(
        **{
            **world.__dict__,
            "opaque_loop": frozenset((state.name,)),
            "pipeline_roles": (PipelineRoles(state.name, request_tags=frozenset((command.name,))),),
        }
    )
    producer = Producer(
        rung_index=next(iter(world.pdg.writers_of[command.name])),
        kind="program",
        guard_tags=frozenset((timer.Done.name,)),
        co_writes=frozenset(),
        command_tag=command.name,
        command_value=10,
    )

    result = read_program_step(world, producer, plc)

    assert result.status is ProgramStepStatus.INTERRUPTED
    assert result.required_inputs == ()
    assert result.boundary == Cmp(timer.Acc.name, ">=", 1)
    assert result.preserve_channels == (state.name,)
    assert "operation reading is no longer current" in result.reason


def _sequencer_program():
    """One producer whose requirement differs on each side of an owned boundary.

    ``SeqStep`` advances 2 -> 3 by itself, one scan behind the rung that gates
    the command.  A trace taken mid-crossing still reads ``SeqStep == 2``, where
    the only way through the gate's ``Or`` is the operator button; once the
    advance settles that leaf is satisfied and the button is not wanted at all.
    ``SeqReady`` keeps the producer short of its command in both worlds, so the
    two readings differ only in what they ask the operator for.  ``SeqEnable``
    parks the advance so the same program can also be read while it is genuinely
    stopped at that button.
    """
    button = Bool("SeqButton", external=True)
    enable = Bool("SeqEnable", external=True, default=True)
    step = Int("SeqStep", default=2)
    advance = Bool("SeqAdvance")
    ready = Bool("SeqReady")
    gate = Bool("SeqGate")
    command = Int("SeqCommand")

    with Program(strict=False) as program:
        with Rung(And(Or(step == 3, button), ready)):
            latch(gate)
        with Rung(gate):
            copy(10, command)
        with Rung(advance):
            copy(3, step)
        with Rung(And(step == 2, enable)):
            latch(advance)
        with Rung(step == 4):
            latch(ready)
    return program, button, enable, step, command


def _sequencer_producer(world, command):
    return Producer(
        rung_index=next(iter(world.pdg.writers_of[command.name])),
        kind="program",
        guard_tags=frozenset(("SeqGate",)),
        co_writes=frozenset(),
        command_tag=command.name,
        command_value=10,
    )


def test_owned_step_advance_is_not_read_as_the_next_step_s_input() -> None:
    """A requirement read mid-crossing belongs to the next world, not this one."""
    program, button, _enable, step, command = _sequencer_program()
    plc = PLC(program)
    world = _world(program, plc)

    result = read_program_step(world, _sequencer_producer(world, command), plc)

    # The owned crossing is progress, not interference: it becomes the immediate
    # boundary to coast to, and the next world's button is never surfaced.
    assert result.status is ProgramStepStatus.KEEP_RUNNING
    assert result.required_inputs == ()
    assert result.channel == step.name
    assert result.boundary == Eq(step.name, frozenset((3,)))
    assert "crossing a boundary the program owns" in result.reason
    assert f"{button.name} is not required once that motion settles" in result.reason


def test_a_genuinely_awaited_input_survives_the_owned_motion_check() -> None:
    """Program motion alone must not suppress a real stopped-at-input reading."""
    program, button, enable, _step, command = _sequencer_program()
    plc = PLC(program)
    # Park the automatic advance: nothing moves on its own, so the button is
    # what the producer is actually stopped at.
    plc.patch({enable.name: False})
    plc.step()
    world = _world(program, plc)

    result = read_program_step(world, _sequencer_producer(world, command), plc)

    assert result.status is ProgramStepStatus.NEEDS_INPUT
    assert tuple(action.pair for action in result.required_inputs) == ((button.name, True),)


def test_input_must_reach_the_exact_producer_not_merely_move_its_channel() -> None:
    """A crossed channel's motion is not evidence the selected writer fired."""
    hazard = Bool("BarrierHazard", default=True, external=True)
    supply = Bool("BarrierSupply", external=True)
    state = Int("BarrierState", default=6, external=True)
    command = Int("BarrierCommand")

    with Program(strict=False) as program:
        with Rung(And(hazard, state == 6)):
            copy(11, state)
        with Rung(And(state == 6, supply)):
            copy(10, command)

    plc = PLC(program)
    world = _world(program, plc)
    world = WorldView(
        **{
            **world.__dict__,
            "opaque_loop": frozenset((state.name,)),
            "pipeline_roles": (PipelineRoles(state.name, request_tags=frozenset((command.name,))),),
        }
    )
    producer = Producer(
        rung_index=next(iter(world.pdg.writers_of[command.name])),
        kind="program",
        guard_tags=frozenset((state.name, supply.name)),
        co_writes=frozenset(),
        command_tag=command.name,
        command_value=10,
    )

    result = read_program_step(world, producer, plc)

    assert result.status is ProgramStepStatus.UNCLEAR
    assert "BarrierState is not accepted" in result.reason
