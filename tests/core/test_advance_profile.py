"""Instruction-level tests for the next-operation advance contract."""

from __future__ import annotations

from pyrung.core import (
    Block,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    TagType,
    Timer,
    count_down,
    count_up,
    event_drum,
    off_delay,
    on_delay,
    out,
    rise,
    shift,
    time_drum,
)
from pyrung.core.crossing import Cmp, Eq
from pyrung.core.instruction.advance import AdvanceProfile
from pyrung.core.validation._common import walk_instructions


def _only_profile(program: Program) -> AdvanceProfile | None:
    profiles = [
        profile
        for instruction in walk_instructions(program)
        if (profile := instruction.advance_profile()) is not None
    ]
    return profiles[0] if profiles else None


def test_on_delay_describes_one_hold_to_the_accumulator_boundary() -> None:
    timer = Timer.clone("WD")
    run = Bool("run", external=True)
    with Program() as program:
        with Rung(run):
            on_delay(timer, 2000, "ms")

    profile = _only_profile(program)
    assert profile is not None
    target = Eq(timer.Done.name, frozenset((True,)))
    snapshot = {timer.Done.name: False, timer.Acc.name: 0}
    step = profile.plan(target, snapshot)

    assert profile.channels == (timer.Acc, timer.Done, timer.TT)
    assert profile.accumulator is timer.Acc
    assert profile.done is timer.Done
    assert profile.active is timer.TT
    assert step is not None
    assert step.until == Cmp(timer.Acc.name, ">=", 2000)
    assert step.holds[0].value is True
    assert step.pulse is None
    assert profile.linear is not None
    assert profile.linear.estimate_scans(target, snapshot, 0.010) == 200
    assert not hasattr(profile, "kind")


def test_off_delay_advances_while_its_enable_is_false() -> None:
    timer = Timer.clone("TOF")
    run = Bool("run", external=True)
    with Program() as program:
        with Rung(run):
            off_delay(timer, 1000, "ms")

    profile = _only_profile(program)
    assert profile is not None
    step = profile.plan(
        Eq(timer.Done.name, frozenset((False,))),
        {timer.Done.name: True, timer.Acc.name: 0},
    )
    assert step is not None
    assert step.holds[0].value is False


def test_done_settlement_uses_the_visible_done_boundary() -> None:
    timer = Timer.clone("Settle")
    run = Bool("run", external=True)
    with Program() as program:
        with Rung(run):
            on_delay(timer, 20, "ms")

    profile = _only_profile(program)
    assert profile is not None
    target = Eq(timer.Done.name, frozenset((True,)))
    step = profile.plan(
        target,
        {timer.Done.name: False, timer.Acc.name: 20},
    )

    assert step is not None
    assert step.until == target


def test_edge_counter_requests_a_pulse_and_keeps_one_bounded_frontier() -> None:
    reset = Bool("reset", external=True)
    clock = Bool("clock", external=True)
    with Program() as program:
        with Rung(rise(clock)):
            count_up(Counter[1], preset=7).reset(reset)

    profile = _only_profile(program)
    assert profile is not None
    target = Eq(Counter[1].Done.name, frozenset((True,)))
    snapshot = {Counter[1].Done.name: False, Counter[1].Acc.name: 2}
    step = profile.plan(target, snapshot)

    assert step is not None
    assert step.pulse is not None
    assert step.holds == ()
    assert profile.linear is not None
    assert profile.linear.direction == 1
    assert profile.linear.estimate_scans(target, snapshot, 0.010) == 5


def test_count_down_uses_a_negative_boundary() -> None:
    reset = Bool("reset", external=True)
    clock = Bool("clock", external=True)
    with Program() as program:
        with Rung(clock):
            count_down(Counter[1], preset=4).reset(reset)

    profile = _only_profile(program)
    assert profile is not None
    target = Eq(Counter[1].Done.name, frozenset((True,)))
    snapshot = {Counter[1].Done.name: False, Counter[1].Acc.name: -1}
    step = profile.plan(target, snapshot)

    assert step is not None
    assert step.until == Cmp(Counter[1].Acc.name, "<=", -4)
    assert profile.linear is not None
    assert profile.linear.direction == -1
    assert profile.linear.estimate_scans(target, snapshot, 0.010) == 3


def test_plain_output_has_no_advance_profile() -> None:
    source = Bool("source", external=True)
    result = Bool("result")
    with Program() as program:
        with Rung(source):
            out(result)
    assert _only_profile(program) is None


def test_event_drum_describes_only_the_next_event_boundary() -> None:
    enable = Bool("Enable", external=True)
    reset = Bool("Reset", external=True)
    e1, e2, e3 = (
        Bool("E1", external=True),
        Bool("E2", external=True),
        Bool("E3", external=True),
    )
    step = Int("Step")
    done = Bool("Done")
    y1, y2 = Bool("Y1"), Bool("Y2")
    with Program() as program:
        with Rung(enable):
            event_drum(
                outputs=[y1, y2],
                events=[e1, e2, e3],
                pattern=[[1, 0], [0, 1], [1, 1]],
                current_step=step,
                completion_flag=done,
            ).reset(reset)

    profile = _only_profile(program)
    assert profile is not None
    operation = profile.plan(
        Eq(step.name, frozenset((3,))),
        {step.name: 1, done.name: False},
    )

    assert profile.channels == (step, done)
    assert profile.done is done
    assert profile.accumulator is None
    assert profile.linear is None
    assert operation is not None
    assert operation.until == Cmp(step.name, ">=", 2)
    assert operation.holds[0].value is True
    assert operation.pulse is not None
    assert operation.pulse.condition.tag is e1


def test_time_drum_retraces_at_each_step_boundary() -> None:
    enable = Bool("Auto", external=True)
    reset = Bool("Reset", external=True)
    step = Int("Step")
    acc = Int("DrumAcc")
    done = Bool("DrumDone")
    output = Bool("Y1")
    with Program() as program:
        with Rung(enable):
            time_drum(
                outputs=[output],
                presets=[50, 60, 70],
                pattern=[[1], [0], [1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset)

    profile = _only_profile(program)
    assert profile is not None
    operation = profile.plan(
        Eq(step.name, frozenset((3,))),
        {step.name: 1, acc.name: 0, done.name: False},
    )

    assert profile.channels == (step, acc, done)
    assert profile.accumulator is acc
    assert profile.done is done
    assert profile.linear is None
    assert operation is not None
    assert operation.until == Cmp(step.name, ">=", 2)
    assert operation.pulse is None
    assert operation.holds[0].value is True


def test_time_drum_uses_the_step_boundary_when_acc_will_reset() -> None:
    enable = Bool("Auto", external=True)
    reset = Bool("Reset", external=True)
    step = Int("Step")
    acc = Int("DrumAcc")
    done = Bool("DrumDone")
    output = Bool("Y1")
    with Program() as program:
        with Rung(enable):
            time_drum(
                outputs=[output],
                presets=[50, 100],
                pattern=[[1], [0]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset)

    profile = _only_profile(program)
    assert profile is not None
    operation = profile.plan(
        Cmp(acc.name, ">=", 80),
        {step.name: 1, acc.name: 10, done.name: False},
    )

    assert operation is not None
    assert operation.until == Cmp(step.name, ">=", 2)


def test_shift_establishes_one_more_prefix_bit_then_retraces() -> None:
    data = Bool("Data", external=True)
    clock = Bool("Clock", external=True)
    reset = Bool("Reset", external=True)
    bits = Block("AdvanceBits", TagType.BOOL, 1, 3)
    selected = bits.select(1, 3)
    with Program() as program:
        with Rung(data):
            shift(selected).clock(clock).reset(reset)

    profile = _only_profile(program)
    assert profile is not None
    operation = profile.plan(
        Eq(bits[3].name, frozenset((True,))),
        {bits[1].name: True, bits[2].name: False, bits[3].name: False},
    )

    assert profile.channels == (bits[1], bits[2], bits[3])
    assert profile.linear is None
    assert operation is not None
    assert operation.until == Eq(bits[2].name, frozenset((True,)))
    assert [demand.value for demand in operation.holds] == [True, False]
    assert operation.pulse is not None
