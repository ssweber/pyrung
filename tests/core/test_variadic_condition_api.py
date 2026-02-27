from __future__ import annotations

from pyrung.core import (
    Block,
    Bool,
    Dint,
    Int,
    PLCRunner,
    Program,
    Rung,
    TagType,
    count_down,
    count_up,
    event_drum,
    on_delay,
    shift,
    time_drum,
)


def test_count_up_down_and_reset_accept_variadic_grouped_conditions() -> None:
    enable = Bool("Enable")
    done = Bool("Done")
    acc = Dint("Acc")
    down_a = Bool("DownA")
    down_b = Bool("DownB")
    reset_a = Bool("ResetA")
    reset_b = Bool("ResetB")

    with Program() as logic:
        with Rung(enable):
            count_up(done, acc, preset=10).down((down_a,), [down_b]).reset((reset_a,), reset_b)

    runner = PLCRunner(logic)
    runner.patch(
        {
            "Enable": True,
            "DownA": True,
            "DownB": False,
            "ResetA": False,
            "ResetB": False,
        }
    )
    runner.step()
    assert runner.current_state.tags["Acc"] == 1

    runner.patch({"DownB": True})
    runner.step()
    assert runner.current_state.tags["Acc"] == 1

    runner.patch({"ResetA": True, "ResetB": False})
    runner.step()
    assert runner.current_state.tags["Acc"] == 1

    runner.patch({"ResetB": True})
    runner.step()
    assert runner.current_state.tags["Acc"] == 0
    assert runner.current_state.tags["Done"] is False


def test_shift_clock_and_reset_accept_variadic_grouped_conditions() -> None:
    data = Bool("Data")
    clock_a = Bool("ClockA")
    clock_b = Bool("ClockB")
    reset_a = Bool("ResetA")
    reset_b = Bool("ResetB")
    bits = Block("C", TagType.BOOL, 1, 8)

    with Program() as logic:
        with Rung(data):
            shift(bits.select(1, 3)).clock((clock_a,), [clock_b]).reset((reset_a,), reset_b)

    runner = PLCRunner(logic)
    runner.patch(
        {
            "Data": True,
            "ClockA": False,
            "ClockB": True,
            "ResetA": False,
            "ResetB": False,
            "C1": True,
            "C2": False,
            "C3": False,
        }
    )
    runner.step()
    assert runner.current_state.tags["C2"] is False

    runner.patch({"ClockA": True})
    runner.step()
    assert runner.current_state.tags["C2"] is True

    runner.patch({"ResetA": True, "ResetB": False})
    runner.step()
    assert runner.current_state.tags["C1"] is True

    runner.patch({"ResetB": True})
    runner.step()
    assert runner.current_state.tags["C1"] is False
    assert runner.current_state.tags["C2"] is False
    assert runner.current_state.tags["C3"] is False


def test_on_delay_reset_accepts_variadic_conditions() -> None:
    enable = Bool("Enable")
    done = Bool("TimerDone")
    acc = Int("TimerAcc")
    reset_a = Bool("ResetA")
    reset_b = Bool("ResetB")

    with Program() as logic:
        with Rung(enable):
            on_delay(done, acc, preset=100).reset((reset_a,), reset_b)

    runner = PLCRunner(logic)
    runner.patch({"Enable": True, "ResetA": False, "ResetB": False})
    runner.step()
    assert runner.current_state.tags["TimerAcc"] == 100

    runner.patch({"ResetA": True, "ResetB": False})
    runner.step()
    assert runner.current_state.tags["TimerAcc"] == 200

    runner.patch({"ResetB": True})
    runner.step()
    assert runner.current_state.tags["TimerAcc"] == 0
    assert runner.current_state.tags["TimerDone"] is False


def test_count_down_reset_accepts_variadic_grouped_conditions() -> None:
    enable = Bool("Enable")
    done = Bool("Done")
    acc = Dint("Acc")
    reset_a = Bool("ResetA")
    reset_b = Bool("ResetB")

    with Program() as logic:
        with Rung(enable):
            count_down(done, acc, preset=5).reset((reset_a,), [reset_b])

    runner = PLCRunner(logic)
    runner.patch({"Enable": True, "ResetA": False, "ResetB": False})
    runner.step()
    assert runner.current_state.tags["Acc"] == -1

    runner.patch({"ResetA": True, "ResetB": False})
    runner.step()
    assert runner.current_state.tags["Acc"] == -2

    runner.patch({"ResetB": True})
    runner.step()
    assert runner.current_state.tags["Acc"] == 0
    assert runner.current_state.tags["Done"] is False


def test_event_drum_reset_and_jog_accept_variadic_conditions() -> None:
    enable = Bool("Enable")
    reset_a = Bool("ResetA")
    reset_b = Bool("ResetB")
    jog_a = Bool("JogA")
    jog_b = Bool("JogB")
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
            ).reset((reset_a,), reset_b).jog(jog_a, [jog_b])

    runner = PLCRunner(logic)
    runner.patch(
        {
            "Enable": True,
            "ResetA": False,
            "ResetB": False,
            "JogA": False,
            "JogB": False,
            "E1": False,
            "E2": False,
        }
    )
    runner.step()
    assert runner.current_state.tags["Step"] == 1

    runner.patch({"JogA": True, "JogB": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 1

    runner.patch({"JogB": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2

    runner.patch({"ResetA": True, "ResetB": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 2

    runner.patch({"ResetB": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 1


def test_time_drum_reset_and_jog_accept_variadic_conditions() -> None:
    enable = Bool("Enable")
    reset_a = Bool("ResetA")
    reset_b = Bool("ResetB")
    jog_a = Bool("JogA")
    jog_b = Bool("JogB")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    y1 = Bool("Y1")
    y2 = Bool("Y2")

    with Program() as logic:
        with Rung(enable):
            time_drum(
                outputs=[y1, y2],
                presets=[1000, 1000],
                pattern=[[1, 0], [0, 1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset((reset_a,), [reset_b]).jog((jog_a,), jog_b)

    runner = PLCRunner(logic)
    runner.patch(
        {
            "Enable": True,
            "ResetA": False,
            "ResetB": False,
            "JogA": False,
            "JogB": False,
        }
    )
    runner.step()
    assert runner.current_state.tags["Step"] == 1

    runner.patch({"JogA": True, "JogB": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 1

    runner.patch({"JogB": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2

    runner.patch({"ResetA": True, "ResetB": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 2

    runner.patch({"ResetB": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 1


def test_event_drum_jump_accepts_variadic_grouped_conditions() -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    jump_a = Bool("JumpA")
    jump_b = Bool("JumpB")
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
            ).reset(reset).jump((jump_a,), jump_b, step=2)

    runner = PLCRunner(logic)
    runner.patch(
        {
            "Enable": True,
            "Reset": False,
            "JumpA": False,
            "JumpB": False,
            "E1": False,
            "E2": False,
        }
    )
    runner.step()
    assert runner.current_state.tags["Step"] == 1

    runner.patch({"JumpA": True, "JumpB": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 1

    runner.patch({"JumpB": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2


def test_time_drum_jump_accepts_variadic_grouped_conditions() -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    jump_a = Bool("JumpA")
    jump_b = Bool("JumpB")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    y1 = Bool("Y1")
    y2 = Bool("Y2")

    with Program() as logic:
        with Rung(enable):
            time_drum(
                outputs=[y1, y2],
                presets=[1000, 1000],
                pattern=[[1, 0], [0, 1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset).jump((jump_a,), jump_b, step=2)

    runner = PLCRunner(logic)
    runner.patch(
        {
            "Enable": True,
            "Reset": False,
            "JumpA": False,
            "JumpB": False,
        }
    )
    runner.step()
    assert runner.current_state.tags["Step"] == 1

    runner.patch({"JumpA": True, "JumpB": False})
    runner.step()
    assert runner.current_state.tags["Step"] == 1

    runner.patch({"JumpB": True})
    runner.step()
    assert runner.current_state.tags["Step"] == 2


def test_runner_when_and_run_until_accept_variadic_grouped_conditions() -> None:
    a = Bool("A")
    b = Bool("B")
    c = Bool("C")

    runner = PLCRunner(logic=[])
    runner.when((a, [b]), c).pause()
    runner.patch({"A": True, "B": True, "C": False})
    runner.run(cycles=3)
    assert runner.current_state.scan_id == 3

    runner.patch({"C": True})
    runner.run(cycles=5)
    assert runner.current_state.scan_id == 4

    runner2 = PLCRunner(logic=[])
    runner2.patch({"A": True, "B": False, "C": True})
    result = runner2.run_until((a, [b]), c, max_cycles=3)
    assert result.scan_id == 3

    runner2.patch({"B": True})
    result = runner2.run_until((a, [b]), c, max_cycles=5)
    assert result.scan_id == 4


def test_single_condition_forms_remain_supported() -> None:
    enable = Bool("Enable")
    done = Bool("Done")
    acc = Dint("Acc")
    reset = Bool("Reset")
    fault = Bool("Fault")

    with Program() as logic:
        with Rung(enable):
            count_down(done, acc, preset=5).reset(reset)

    runner = PLCRunner(logic)
    runner.patch({"Enable": True, "Reset": False})
    runner.step()
    assert runner.current_state.tags["Acc"] == -1

    runner2 = PLCRunner(logic=[])
    runner2.when(fault).pause()
    runner2.patch({"Fault": True})
    runner2.run(cycles=3)
    assert runner2.current_state.scan_id == 1

    runner3 = PLCRunner(logic=[])
    result = runner3.run_until(~fault, max_cycles=1)
    assert result.scan_id == 1
