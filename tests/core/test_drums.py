from __future__ import annotations

import pytest

from pyrung.core import Bool, Int, PLCRunner, Program, Rung, TimeMode, event_drum, out, time_drum


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


def test_event_drum_pause_reset_and_disabled_jump_jog_behavior() -> None:
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
            ).reset(reset).jump(condition=jump, step=1).jog(jog)

    runner = PLCRunner(logic)
    runner.patch({"Enable": True, "Reset": False, "Jump": False, "Jog": False, "E1": False, "E2": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Y1"] is True
    assert runner.current_state.tags["Y2"] is False

    runner.patch({"E1": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2
    assert runner.current_state.tags["Y1"] is False
    assert runner.current_state.tags["Y2"] is True

    runner.patch({"Enable": False, "Jump": True, "Jog": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2
    assert runner.current_state.tags["Y2"] is True

    runner.patch({"Reset": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["Done"] is False
    assert runner.current_state.tags["Y1"] is True
    assert runner.current_state.tags["Y2"] is False


def test_event_drum_event_must_see_new_rising_edge_after_step_entry() -> None:
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

    runner = PLCRunner(logic)
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


def test_time_drum_precedence_auto_reset_jump_jog() -> None:
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
            ).reset(reset).jump(condition=jump, step=3).jog(jog)

    runner = PLCRunner(logic)
    runner.patch({"Enable": False, "Reset": False, "Jump": False, "Jog": False})
    runner.step()

    runner.patch({"Enable": True, "Reset": True, "Jump": True, "Jog": True})
    runner.step()

    assert runner.current_state.tags["Step"] == 4
    assert runner.current_state.tags["Acc"] == 0
    assert runner.current_state.tags["Y4"] is True


def test_time_drum_ignores_jump_target_out_of_range() -> None:
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
            ).reset(reset).jump(condition=jump, step=99)

    runner = PLCRunner(logic)
    runner.patch({"Enable": True, "Reset": False, "Jump": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 1

    runner.patch({"Jump": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 1


def test_event_drum_completion_sticky_until_reset() -> None:
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

    runner = PLCRunner(logic)
    runner.patch({"Enable": True, "Reset": False, "E1": False, "E2": False})
    runner.step()
    runner.patch({"E1": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2

    runner.patch({"E1": False, "E2": True})
    runner.step()
    assert runner.current_state.tags["Done"] is True

    runner.patch({"E2": False})
    runner.step()
    assert runner.current_state.tags["Done"] is True

    runner.patch({"Reset": True})
    runner.step()
    assert runner.current_state.tags["Done"] is False


def test_time_drum_accumulates_and_resets_accumulator_on_step_transition() -> None:
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

    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
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
