from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    event_drum,
    latch,
    out,
    time_drum,
    time_event_drum,
)


def test_event_drum_requires_reset_builder() -> None:
    enable = Bool("Enable")
    step = Int("Step")
    done = Bool("Done")
    y1 = Bool("Y1")
    e1 = Bool("E1")

    with pytest.raises(RuntimeError, match="event_drum"):
        with Program():
            with Rung(enable):
                event_drum(
                    outputs=[y1],
                    events=[e1],
                    pattern=[[1]],
                    current_step=step,
                    completion_flag=done,
                )


def test_time_drum_requires_reset_builder() -> None:
    enable = Bool("Enable")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    y1 = Bool("Y1")

    with pytest.raises(RuntimeError, match="time_drum"):
        with Program():
            with Rung(enable):
                time_drum(
                    outputs=[y1],
                    presets=[100],
                    pattern=[[1]],
                    current_step=step,
                    accumulator=acc,
                    completion_flag=done,
                )


def test_drum_is_terminal_in_flow() -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    step = Int("Step")
    done = Bool("Done")
    y1 = Bool("Y1")
    e1 = Bool("E1")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="terminal"):
        with Program():
            with Rung(enable):
                event_drum(
                    outputs=[y1],
                    events=[e1],
                    pattern=[[1]],
                    current_step=step,
                    completion_flag=done,
                ).reset(reset)
                out(light)


def test_event_drum_paused_takes_jump_jog_and_reset_without_writing_outputs(
    runner_factory,
) -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    jump = Bool("Jump")
    jog = Bool("Jog")
    step = Int("Step")
    done = Bool("Done")
    y1 = Bool("Y1")
    y2 = Bool("Y2")
    e1 = Bool("E1")
    e2 = Bool("E2")

    with Program() as logic:
        with Rung(enable):
            event_drum(
                outputs=[y1, y2],
                events=[e1, e2],
                pattern=[
                    [1, 0],
                    [0, 1],
                ],
                current_step=step,
                completion_flag=done,
            ).reset(reset).jump(jump, step=1).jog(jog)

    runner = runner_factory(logic)
    runner.patch(
        {"Enable": True, "Reset": False, "Jump": False, "Jog": False, "E1": False, "E2": False}
    )
    runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Y1"] is True
    assert runner.current_state.tags["Y2"] is False

    runner.patch({"E1": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2
    assert runner.current_state.tags["Y1"] is False
    assert runner.current_state.tags["Y2"] is True

    # Paused: the jump and jog still act on their edges, jump last (to step 1),
    # and the paused drum leaves its outputs as they are.
    runner.patch({"Enable": False, "Jump": True, "Jog": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Y1"] is False
    assert runner.current_state.tags["Y2"] is True

    # Reset is a level that wins, paused or not: every output off.
    runner.patch({"Reset": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Done"] is False
    assert runner.current_state.tags["Y1"] is False
    assert runner.current_state.tags["Y2"] is False

    # Step 1's output comes on once the reset ends and the rung is true.
    runner.patch({"Reset": False, "Enable": True})
    runner.step()
    assert runner.current_state.tags["Y1"] is True
    assert runner.current_state.tags["Y2"] is False


def test_event_drum_event_must_see_new_rising_edge_after_step_entry(runner_factory) -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    step = Int("Step")
    done = Bool("Done")
    y1 = Bool("Y1")
    y2 = Bool("Y2")
    y3 = Bool("Y3")
    e1 = Bool("E1")
    e2 = Bool("E2")
    e3 = Bool("E3")

    with Program() as logic:
        with Rung(enable):
            event_drum(
                outputs=[y1, y2, y3],
                events=[e1, e2, e3],
                pattern=[
                    [1, 0, 0],
                    [0, 1, 0],
                    [0, 0, 1],
                ],
                current_step=step,
                completion_flag=done,
            ).reset(reset)

    runner = runner_factory(logic)
    runner.patch({"Enable": True, "Reset": False, "E1": False, "E2": True, "E3": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 1

    runner.patch({"E1": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2

    runner.patch({"E1": False, "E2": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2

    runner.patch({"E2": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 2

    runner.patch({"E2": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 3


def test_event_drum_events_use_rung_entry_snapshot(runner_factory) -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    event = Bool("Event")
    step = Int("Step")
    done = Bool("Done")
    y1 = Bool("Y1")
    y2 = Bool("Y2")

    with Program() as logic:
        with Rung(enable):
            latch(event)
            event_drum(
                outputs=[y1, y2],
                events=[event, event],
                pattern=[[1, 0], [0, 1]],
                current_step=step,
                completion_flag=done,
            ).reset(reset)

    runner = runner_factory(logic)
    runner.patch({"Enable": True, "Reset": False, "Event": False})

    runner.step()
    assert runner.current_state.tags["Event"] is True
    assert runner.current_state.tags["Step"] == 1

    runner.step()
    assert runner.current_state.tags["Step"] == 2


def test_time_drum_reset_wins_then_jump_beats_jog(runner_factory) -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    jump = Bool("Jump")
    jog = Bool("Jog")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    y1 = Bool("Y1")
    y2 = Bool("Y2")
    y3 = Bool("Y3")
    y4 = Bool("Y4")

    with Program() as logic:
        with Rung(enable):
            time_drum(
                outputs=[y1, y2, y3, y4],
                presets=[0, 0, 0, 0],
                pattern=[
                    [1, 0, 0, 0],
                    [0, 1, 0, 0],
                    [0, 0, 1, 0],
                    [0, 0, 0, 1],
                ],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset).jump(jump, step=3).jog(jog)

    runner = runner_factory(logic)
    runner.patch({"Enable": False, "Reset": False, "Jump": False, "Jog": False})
    runner.step()

    # Reset wins and drops the scan's jump and jog: step 1, every output off.
    runner.patch({"Enable": True, "Reset": True, "Jump": True, "Jog": True})
    runner.step()

    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Acc"] == 0
    assert not any(runner.current_state.tags[f"Y{i}"] for i in range(1, 5))

    # The dropped edges are spent: held through the reset's end they do nothing.
    runner.patch({"Reset": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 2  # step 1's 0 ms preset is up
    assert runner.current_state.tags["Y2"] is True

    # Jump and jog on one scan: the jog runs first, the jump (to 3) wins.
    runner.patch({"Jump": False, "Jog": False})
    runner.step()
    runner.patch({"Jump": True, "Jog": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 3
    assert runner.current_state.tags["Acc"] == 0
    assert runner.current_state.tags["Y3"] is True


def test_time_drum_completion_outputs_stay_off_through_a_jump_until_a_jog(
    runner_factory,
) -> None:
    with Program() as logic:
        with Rung(Bool("Enable")):
            time_drum(
                outputs=[Bool("Y1"), Bool("Y2")],
                presets=[20, 20],
                pattern=[[1, 0], [0, 1]],
                current_step=Int("Step"),
                accumulator=Int("Acc"),
                completion_flag=Bool("Done"),
            ).reset(Bool("Reset")).jump(Bool("Jump"), step=1).jog(Bool("Jog"))

    runner = runner_factory(logic, dt=0.010)
    runner.patch({"Enable": True})
    for _ in range(3):  # the first scan's dt is 0
        runner.step()
    assert runner.current_state.tags["Step"] == 2
    assert runner.current_state.tags["Y2"] is True
    runner.step()
    assert runner.current_state.tags["Y2"] is True
    runner.step()
    assert runner.current_state.tags["Done"] is True
    assert runner.current_state.tags["Y1"] is False
    assert runner.current_state.tags["Y2"] is False
    runner.step()
    assert runner.current_state.tags["Y2"] is False

    # A jump moves the step and clears its time, but the outputs stay off.
    runner.patch({"Jump": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Acc"] == 0
    assert runner.current_state.tags["Done"] is True
    assert runner.current_state.tags["Y1"] is False

    # A jog brings them back: step 2's output on, the flag still on.
    runner.patch({"Jump": False, "Jog": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2
    assert runner.current_state.tags["Acc"] == 0
    assert runner.current_state.tags["Done"] is True
    assert runner.current_state.tags["Y2"] is True


def test_event_drum_advance_after_a_jump_ends_completion(runner_factory) -> None:
    """pyrung's reading (no CLICK probe yet): the drum's own step forward, like a
    jog, ends the all-outputs-off state a completion left through a jump."""
    with Program() as logic:
        with Rung(Bool("Enable")):
            event_drum(
                outputs=[Bool("Y1"), Bool("Y2")],
                events=[Bool("E1"), Bool("E2")],
                pattern=[[1, 0], [0, 1]],
                current_step=Int("Step"),
                completion_flag=Bool("Done"),
            ).reset(Bool("Reset")).jump(Bool("Jump"), step=1)

    runner = runner_factory(logic)
    runner.patch({"Enable": True})
    runner.step()
    runner.patch({"E1": True})
    runner.step()
    runner.patch({"E1": False, "E2": True})
    runner.step()
    assert runner.current_state.tags["Done"] is True
    assert runner.current_state.tags["Y2"] is False

    runner.patch({"E2": False, "Jump": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Y1"] is False

    runner.patch({"Jump": False, "E1": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2
    assert runner.current_state.tags["Done"] is True
    assert runner.current_state.tags["Y2"] is True


def test_time_drum_jump_uses_rung_entry_snapshot(runner_factory) -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    jump = Bool("Jump")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    y1 = Bool("Y1")
    y2 = Bool("Y2")

    with Program() as logic:
        with Rung(enable):
            latch(jump)
            time_drum(
                outputs=[y1, y2],
                presets=[1000, 1000],
                pattern=[[1, 0], [0, 1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset).jump(jump, step=2)

    runner = runner_factory(logic, dt=0.010)
    runner.patch({"Enable": True, "Reset": False, "Jump": False})

    runner.step()
    assert runner.current_state.tags["Jump"] is True
    assert runner.current_state.tags["Step"] == 1

    runner.step()
    assert runner.current_state.tags["Step"] == 2


def test_time_drum_ignores_jump_target_out_of_range(runner_factory) -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    jump = Bool("Jump")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    y1 = Bool("Y1")

    with Program() as logic:
        with Rung(enable):
            time_drum(
                outputs=[y1],
                presets=[1000],
                pattern=[[1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset).jump(jump, step=99)

    runner = runner_factory(logic)
    runner.patch({"Enable": True, "Reset": False, "Jump": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 1

    runner.patch({"Jump": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 1


def test_event_drum_completion_sticky_until_reset(runner_factory) -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    step = Int("Step")
    done = Bool("Done")
    y1 = Bool("Y1")
    y2 = Bool("Y2")
    e1 = Bool("E1")
    e2 = Bool("E2")

    with Program() as logic:
        with Rung(enable):
            event_drum(
                outputs=[y1, y2],
                events=[e1, e2],
                pattern=[[1, 0], [0, 1]],
                current_step=step,
                completion_flag=done,
            ).reset(reset)

    runner = runner_factory(logic)
    runner.patch({"Enable": True, "Reset": False, "E1": False, "E2": False})
    runner.step()
    runner.patch({"E1": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2

    runner.patch({"E1": False, "E2": True})
    runner.step()
    assert runner.current_state.tags["Done"] is True

    # Completion turns every output off, and they stay off.
    assert runner.current_state.tags["Y1"] is False
    assert runner.current_state.tags["Y2"] is False

    runner.patch({"E2": False})
    runner.step()
    assert runner.current_state.tags["Done"] is True
    assert runner.current_state.tags["Y2"] is False

    runner.patch({"Reset": True})
    runner.step()
    assert runner.current_state.tags["Done"] is False
    assert runner.current_state.tags["Y1"] is False

    runner.patch({"Reset": False})
    runner.step()
    assert runner.current_state.tags["Y1"] is True


def test_time_drum_accumulates_and_resets_accumulator_on_step_transition(runner_factory) -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    y1 = Bool("Y1")
    y2 = Bool("Y2")

    with Program() as logic:
        with Rung(enable):
            time_drum(
                outputs=[y1, y2],
                presets=[50, 100],
                pattern=[[1, 0], [0, 1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset)

    runner = runner_factory(logic, dt=0.010)
    runner.patch({"Enable": True, "Reset": False})

    for _ in range(4):
        runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Acc"] == 30

    runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Acc"] == 40

    runner.step()
    assert runner.current_state.tags["Step"] == 2
    assert runner.current_state.tags["Acc"] == 0
    assert runner.current_state.tags["Y2"] is True

    runner.step()
    runner.step()
    runner.step()
    assert runner.current_state.tags["Acc"] == 30


def _time_event_drum_program(
    presets: list[int | Int | None], events: list[Bool | None], *, latch_event: Bool | None = None
) -> Program:
    pattern = [[step == row for step in range(len(presets))] for row in range(len(presets))]
    with Program(strict=False) as logic:
        with Rung(Bool("Enable")):
            if latch_event is not None:
                latch(latch_event)
            time_event_drum(
                outputs=[Bool(f"Y{i + 1}") for i in range(len(presets))],
                presets=presets,
                events=events,
                pattern=pattern,
                current_step=Int("Step"),
                accumulator=Int("Acc"),
                completion_flag=Bool("Done"),
            ).reset(Bool("Reset"))
    return logic


def test_time_event_drum_requires_reset_builder() -> None:
    with pytest.raises(RuntimeError, match="time_event_drum"):
        with Program():
            with Rung(Bool("Enable")):
                time_event_drum(
                    outputs=[Bool("Y1")],
                    presets=[100],
                    events=[None],
                    pattern=[[1]],
                    current_step=Int("Step"),
                    accumulator=Int("Acc"),
                    completion_flag=Bool("Done"),
                )


@pytest.mark.parametrize(
    ("presets", "events", "match"),
    [
        ([100, None], [None, None], "step 2 needs a preset, an event, or both"),
        ([100, 200], [None], "events count"),
        ([100, 200, 300], [None, None], "presets count"),
    ],
)
def test_time_event_drum_rejects_bad_steps(
    presets: list[int | None], events: list[Bool | None], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        with Program():
            with Rung(Bool("Enable")):
                time_event_drum(
                    outputs=[Bool("Y1"), Bool("Y2")],
                    presets=presets,
                    events=events,
                    pattern=[[1, 0], [0, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("Acc"),
                    completion_flag=Bool("Done"),
                ).reset(Bool("Reset"))


def test_time_event_drum_timed_step_waits_for_its_event_while_acc_counts(runner_factory) -> None:
    runner = runner_factory(_time_event_drum_program([50, 100], [Bool("E1"), None]), dt=0.010)
    runner.patch({"Enable": True})

    for _ in range(8):
        runner.step()
    # Time is up at 50 ms; the step waits for E1 and Acc keeps counting.
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Acc"] == 70

    runner.patch({"E1": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2
    assert runner.current_state.tags["Acc"] == 0
    assert runner.current_state.tags["Y2"] is True


def test_time_event_drum_level_event_already_on_ends_step_when_time_is_up(runner_factory) -> None:
    runner = runner_factory(_time_event_drum_program([30, 100], [Bool("E1"), None]), dt=0.010)
    runner.patch({"Enable": True, "E1": True})

    for _ in range(3):
        runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Acc"] == 20

    runner.step()
    assert runner.current_state.tags["Step"] == 2


def test_time_event_drum_event_only_steps_follow_the_level(runner_factory) -> None:
    runner = runner_factory(
        _time_event_drum_program([None, None, 40], [Bool("E1"), Bool("E1"), None]), dt=0.010
    )
    runner.patch({"Enable": True})
    runner.step()
    runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Acc"] == 10

    # A level held across two event-only steps ends one per scan.
    runner.patch({"E1": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2
    runner.step()
    assert runner.current_state.tags["Step"] == 3
    assert runner.current_state.tags["Acc"] == 0

    for _ in range(4):
        runner.step()
    assert runner.current_state.tags["Done"] is True
    assert runner.current_state.tags["Step"] == 3
    assert not any(runner.current_state.tags[f"Y{i}"] for i in range(1, 4))

    runner.patch({"Reset": True})
    runner.step()
    assert runner.current_state.tags["Done"] is False
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Acc"] == 0


def test_time_event_drum_outputs_stay_off_after_completion_until_a_jog(
    runner_factory,
) -> None:
    with Program() as logic:
        with Rung(Bool("Enable")):
            time_event_drum(
                outputs=[Bool("Y1"), Bool("Y2")],
                presets=[None, None],
                events=[Bool("E1"), Bool("E2")],
                pattern=[[1, 0], [0, 1]],
                current_step=Int("Step"),
                accumulator=Int("Acc"),
                completion_flag=Bool("Done"),
            ).reset(Bool("Reset")).jump(Bool("Jump"), step=1).jog(Bool("Jog"))

    runner = runner_factory(logic, dt=0.010)
    runner.patch({"Enable": True, "E1": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2
    assert runner.current_state.tags["Y2"] is True

    runner.patch({"E2": True})
    runner.step()
    assert runner.current_state.tags["Done"] is True
    assert runner.current_state.tags["Y2"] is False

    # The last event is a level; it going off does not bring the outputs back.
    runner.patch({"E2": False})
    runner.step()
    assert runner.current_state.tags["Y2"] is False

    # Paused: a jump still acts, but only the enabled drum writes its outputs.
    runner.patch({"Enable": False, "E1": False, "Jump": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Y1"] is False

    # Back on, the drum is still complete: the jump left its outputs off.
    runner.patch({"Enable": True, "Jump": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Done"] is True
    assert runner.current_state.tags["Y1"] is False

    # A jog brings them back.
    runner.patch({"Jog": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2
    assert runner.current_state.tags["Done"] is True
    assert runner.current_state.tags["Y2"] is True


def test_time_event_drum_events_use_rung_entry_snapshot(runner_factory) -> None:
    event = Bool("E1")
    runner = runner_factory(
        _time_event_drum_program([None, 100], [event, None], latch_event=event), dt=0.010
    )
    runner.patch({"Enable": True})

    runner.step()
    assert runner.current_state.tags["E1"] is True
    assert runner.current_state.tags["Step"] == 1

    runner.step()
    assert runner.current_state.tags["Step"] == 2


def test_time_event_drum_tag_presets_jump_and_jog(runner_factory) -> None:
    with Program() as logic:
        with Rung(Bool("Enable")):
            time_event_drum(
                outputs=[Bool("Y1"), Bool("Y2"), Bool("Y3")],
                presets=[Int("P1"), None, 20],
                events=[None, Bool("E2"), Bool("E3")],
                pattern=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                current_step=Int("Step"),
                accumulator=Int("Acc"),
                completion_flag=Bool("Done"),
            ).reset(Bool("Reset")).jump(Bool("Jump"), step=3).jog(Bool("Jog"))

    runner = runner_factory(logic, dt=0.010)
    runner.patch({"Enable": True, "P1": 20})
    for _ in range(3):
        runner.step()
    assert runner.current_state.tags["Step"] == 2

    runner.patch({"Jog": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 3
    assert runner.current_state.tags["Acc"] == 0

    runner.patch({"Jog": False, "Reset": True})
    runner.step()
    runner.patch({"Reset": False, "Jump": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 3
    assert runner.current_state.tags["Y3"] is True


def _long_run_time_program(kind: str) -> Program:
    with Program(strict=False) as logic:
        with Rung(Bool("Enable")):
            if kind == "time":
                time_drum(
                    outputs=[Bool("Y1"), Bool("Y2"), Bool("Y3")],
                    presets=[500, 300, 700],
                    unit="ms",
                    pattern=[[1, 0, 0], [0, 1, 0], [0, 1, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("Acc"),
                    completion_flag=Bool("Done"),
                ).reset(Bool("Reset")).jump(Bool("Jump"), step=2).jog(Bool("Jog"))
            else:
                time_event_drum(
                    outputs=[Bool("Y1"), Bool("Y2"), Bool("Y3")],
                    presets=[500, None, 700],
                    events=[None, Bool("E2"), Bool("E3")],
                    unit="ms",
                    pattern=[[1, 0, 0], [0, 1, 0], [0, 1, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("Acc"),
                    completion_flag=Bool("Done"),
                ).reset(Bool("Reset")).jump(Bool("Jump"), step=2).jog(Bool("Jog"))
        with Rung(Bool("Y3")):
            out(Bool("Lamp"))
        with Rung(Bool("Done")):
            out(Bool("DoneLamp"))
    return logic


def _long_run_event_program() -> Program:
    with Program(strict=False) as logic:
        with Rung(Bool("Enable")):
            event_drum(
                outputs=[Bool("Y1"), Bool("Y2")],
                events=[Bool("E1"), Bool("E2")],
                pattern=[[1, 0], [0, 1]],
                current_step=Int("Step"),
                completion_flag=Bool("Done"),
            ).reset(Bool("Reset")).jump(Bool("Jump"), step=1).jog(Bool("Jog"))
        with Rung(Bool("Y2")):
            out(Bool("Lamp"))
    return logic


@pytest.mark.parametrize("kind", ["time", "time_event"])
def test_time_drums_complete_pause_and_reset_over_long_runs(kind: str) -> None:
    """Long ``run_for`` windows, which fold: ``make test-fold`` checks them scan by scan."""
    plc = PLC(_long_run_time_program(kind), dt=0.01)
    plc.patch({"Enable": True, "E3": True})
    plc.run_for(0.4)
    plc.patch({"E2": True})
    plc.run_for(3.0)  # completes; outputs off; clamped timing past completion
    assert plc.current_state.tags["Done"] is True
    assert plc.current_state.tags["Y3"] is False
    # Paused: jog/jump still act.
    plc.patch({"Enable": False, "Jump": True})
    plc.run_for(0.5)
    assert plc.current_state.tags["Step"] == 2
    plc.patch({"Enable": True, "Jump": False})
    plc.run_for(2.0)
    plc.patch({"Jog": True})
    plc.run_for(1.0)
    plc.patch({"Reset": True, "Jog": False})
    plc.run_for(0.5)
    assert plc.current_state.tags["Y1"] is False
    plc.patch({"Reset": False})
    plc.run_for(5.0)


def test_event_drum_completes_pauses_and_resets_over_long_runs() -> None:
    plc = PLC(_long_run_event_program(), dt=0.01)
    plc.patch({"Enable": True})
    plc.run_for(0.5)
    plc.patch({"E1": True})
    plc.run_for(0.5)
    plc.patch({"E2": True})
    plc.run_for(0.5)
    assert plc.current_state.tags["Y2"] is False
    plc.patch({"Enable": False, "Jump": True})
    plc.run_for(0.5)
    plc.patch({"Enable": True, "Jump": False})
    plc.run_for(0.5)
    assert plc.current_state.tags["Step"] == 1
    assert plc.current_state.tags["Y1"] is False  # a jump leaves a completed drum off
    plc.patch({"Jog": True})
    plc.run_for(0.5)
    assert plc.current_state.tags["Step"] == 2
    assert plc.current_state.tags["Y2"] is True
    plc.patch({"Reset": True, "Jog": False})
    plc.run_for(0.5)
    plc.patch({"Reset": False})
    plc.run_for(0.5)


@pytest.mark.parametrize("kind", ["event", "time", "time_event"])
def test_drum_reads_its_pins_before_it_corrects_an_invalid_step(kind: str, monkeypatch) -> None:
    """A box reads its pins before any write of its own (the live row solve's order).

    Under the rung-entry snapshot the order cannot show, so the box's view is
    made live here: a jump pin keyed on the uncorrected step must see it.
    """
    from pyrung.core.instruction import drums

    monkeypatch.setattr(drums, "instruction_condition_view", lambda ctx: ctx)
    step = Int("Step")
    builder_args = {
        "outputs": [Bool("Y1"), Bool("Y2"), Bool("Y3")],
        "pattern": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        "current_step": step,
        "completion_flag": Bool("Done"),
    }
    with Program(strict=False) as logic:
        with Rung(Bool("Enable")):
            if kind == "event":
                drum = event_drum(events=[Bool("E1"), Bool("E2"), Bool("E3")], **builder_args)
            elif kind == "time":
                drum = time_drum(presets=[1000] * 3, accumulator=Int("Acc"), **builder_args)
            else:
                drum = time_event_drum(
                    presets=[1000] * 3,
                    events=[None] * 3,
                    accumulator=Int("Acc"),
                    **builder_args,
                )
            drum.reset(Bool("Reset")).jump(Bool("Go"), step == 7, step=3)

    plc = PLC(logic)
    plc.patch({"Step": 7})
    plc.step()
    assert plc.current_state.tags["Step"] == 7  # paused, no jump: nothing corrected
    plc.patch({"Enable": True, "Go": True})
    plc.step()
    assert plc.current_state.tags["Step"] == 3
