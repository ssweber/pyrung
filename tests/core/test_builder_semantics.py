import pytest

from pyrung.core import (
    Block,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    TagType,
    Timer,
    branch,
    count_down,
    count_up,
    event_drum,
    forloop,
    latch,
    off_delay,
    on_delay,
    out,
    shift,
    time_drum,
)

Counter2 = Counter.clone("Counter2")
Counter3 = Counter.clone("Counter3")
Counter4 = Counter.clone("Counter4")
Counter5 = Counter.clone("Counter5")
Counter6 = Counter.clone("Counter6")
Counter7 = Counter.clone("Counter7")
Counter8 = Counter.clone("Counter8")
Timer2 = Timer.clone("Timer2")
Timer3 = Timer.clone("Timer3")


def test_count_up_missing_reset_raises() -> None:
    enable = Bool("Enable")

    with pytest.raises(RuntimeError, match="count_up"):
        with Program():
            with Rung(enable):
                count_up(Counter[1], preset=5)


def test_count_down_missing_reset_raises() -> None:
    enable = Bool("Enable")

    with pytest.raises(RuntimeError, match="count_down"):
        with Program():
            with Rung(enable):
                count_down(Counter2, preset=5)


def test_event_drum_missing_reset_raises() -> None:
    enable = Bool("Enable")
    step = Int("Step")
    done = Bool("Done")
    out1 = Bool("Out1")
    event1 = Bool("Event1")

    with pytest.raises(RuntimeError, match="event_drum"):
        with Program():
            with Rung(enable):
                event_drum(
                    outputs=[out1],
                    events=[event1],
                    pattern=[[1]],
                    current_step=step,
                    completion_flag=done,
                )


def test_time_drum_missing_reset_raises() -> None:
    enable = Bool("Enable")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    out1 = Bool("Out1")

    with pytest.raises(RuntimeError, match="time_drum"):
        with Program():
            with Rung(enable):
                time_drum(
                    outputs=[out1],
                    presets=[100],
                    pattern=[[1]],
                    current_step=step,
                    accumulator=acc,
                    completion_flag=done,
                )


def test_shift_missing_reset_raises() -> None:
    enable = Bool("Enable")
    clock = Bool("Clock")
    bits = Block("C", TagType.BOOL, 1, 8)

    with pytest.raises(RuntimeError, match="shift"):
        with Program():
            with Rung(enable):
                shift(bits.select(1, 4)).clock(clock)


def test_pending_required_builder_blocks_following_dsl_statements() -> None:
    enable = Bool("Enable")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="count_up"):
        with Program():
            with Rung(enable):
                count_up(Counter3, preset=5)
                out(light)


def test_pending_required_builder_blocks_branch_entry() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    reset = Bool("Reset")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="count_up"):
        with Program(strict=False):
            with Rung(enable):
                builder = count_up(Counter4, preset=5)
                with branch(mode):
                    out(light)
                builder.reset(reset)


def test_parent_flow_terminal_blocks_following_instruction() -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="has pins, so it ends its rung"):
        with Program():
            with Rung(enable):
                count_up(Counter5, preset=5).reset(reset)
                out(light)


def test_parent_flow_terminal_blocks_following_branch() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    reset = Bool("Reset")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="has pins, so it ends its rung"):
        with Program():
            with Rung(enable):
                count_down(Counter6, preset=5).reset(reset)
                with branch(mode):
                    out(light)


def test_branch_flow_terminal_blocks_following_branch_local_instruction() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    reset = Bool("Reset")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="has pins, so it ends its rung"):
        with Program():
            with Rung(enable):
                with branch(mode):
                    count_up(Counter7, preset=5).reset(reset)
                    out(light)


def test_box_with_pins_in_a_branch_ends_the_whole_rung() -> None:
    """A box with pins ends its rung, not just its branch: CLICK draws its pins
    from the box's own wire, so a sibling branch goes in a continued() rung."""
    enable = Bool("Enable")
    mode_a = Bool("ModeA")
    mode_b = Bool("ModeB")
    reset = Bool("Reset")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match=r"Move out\(\) \(line \d+\) off this rung") as info:
        with Program():
            with Rung(enable):
                with branch(mode_a):
                    count_up(Counter8, preset=5).reset(reset)
                with branch(mode_b):
                    out(light)
    message = str(info.value)
    assert message.startswith("count_up() (line ")
    assert "`with Rung(...).continued():`" in message
    assert message.isascii()


def test_box_with_pins_refuses_an_output_above_it() -> None:
    enable = Bool("Enable")
    seen = Bool("Seen")
    mode = Bool("Mode")
    reset = Bool("Reset")

    with pytest.raises(RuntimeError, match=r"count_up\(\) .* Move latch\(\) .* a rung above"):
        with Program():
            with Rung(enable):
                latch(seen)
                with branch(mode):
                    count_up(Counter8, preset=5).reset(reset)


def test_box_with_pins_alone_in_a_branch_is_accepted() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    reset = Bool("Reset")

    with Program():
        with Rung(enable):
            with branch(mode):
                count_up(Counter8, preset=5).reset(reset)


def test_box_with_pins_splits_into_continued_rungs() -> None:
    enable = Bool("Enable")
    seen = Bool("Seen")
    auto = Bool("Auto")
    reset = Bool("Reset")
    lamp = Bool("Lamp")

    with Program() as logic:
        with Rung(enable):
            latch(seen)
        with Rung(enable, auto).continued():
            count_up(Counter8, preset=10).reset(reset)
        with Rung(enable).continued():
            out(lamp)
    assert len(logic.rungs) == 3


def test_box_with_pins_beside_bare_body_rows_in_a_forloop_is_accepted() -> None:
    """A forloop body's bare instructions are each a body rung of their own."""
    enable = Bool("Enable")
    reset = Bool("Reset")
    lamp = Bool("Lamp")

    with Program():
        with Rung(enable):
            with forloop(2):
                out(lamp)
                count_up(Counter8, preset=5).reset(reset)


def test_box_with_pins_before_a_bare_body_row_in_a_forloop_is_accepted() -> None:
    """The box is a body rung of its own, so the next bare row is another."""
    enable = Bool("Enable")
    reset = Bool("Reset")
    lamp = Bool("Lamp")
    done = Bool("Done")

    with Program() as logic:
        with Rung(enable):
            with forloop(2):
                count_up(Counter8, preset=5).reset(reset)
                out(lamp)
                count_up(Counter7, preset=5).reset(reset)
                out(done)
    (loop,) = logic.rungs[0]._instructions
    body = [type(item).__name__ for item in loop.body]
    assert body == ["CountUpInstruction", "OutInstruction"] * 2


def test_box_with_pins_beside_an_output_in_a_forloop_body_rung_is_refused() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    reset = Bool("Reset")
    lamp = Bool("Lamp")

    with pytest.raises(RuntimeError, match="has pins, so it ends its rung") as info:
        with Program():
            with Rung(enable):
                with forloop(2):
                    with Rung(mode):
                        out(lamp)
                        count_up(Counter8, preset=5).reset(reset)
    # A body rung has no .continued(): the fix is a body rung of its own.
    message = str(info.value)
    assert "a body rung of its own" in message
    assert "continued" not in message


def test_ton_allows_following_instruction_and_branch() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    light = Bool("Light")
    branch_light = Bool("BranchLight")

    with Program():
        with Rung(enable):
            on_delay(Timer[1], preset=5)
            out(light)
            with branch(mode):
                out(branch_light)


def test_tof_allows_following_instruction_and_branch() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    light = Bool("Light")
    branch_light = Bool("BranchLight")

    with Program():
        with Rung(enable):
            off_delay(Timer2, preset=5)
            out(light)
            with branch(mode):
                out(branch_light)


def test_rton_is_terminal_in_same_flow() -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="has pins, so it ends its rung"):
        with Program():
            with Rung(enable):
                on_delay(Timer3, preset=5).reset(reset)
                out(light)


def test_event_drum_chain_can_finalize_with_reset_then_jump_and_jog() -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    jump = Bool("Jump")
    jog = Bool("Jog")
    step = Int("Step")
    done = Bool("Done")
    out1 = Bool("Out1")
    out2 = Bool("Out2")
    event1 = Bool("Event1")
    event2 = Bool("Event2")

    with Program():
        with Rung(enable):
            event_drum(
                outputs=[out1, out2],
                events=[event1, event2],
                pattern=[[1, 0], [0, 1]],
                current_step=step,
                completion_flag=done,
            ).reset(reset).jump(jump, step=step).jog(jog)


def test_time_drum_chain_can_finalize_with_reset_then_jump_and_jog() -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    jump = Bool("Jump")
    jog = Bool("Jog")
    step = Int("Step")
    acc = Int("Acc")
    done = Bool("Done")
    out1 = Bool("Out1")
    out2 = Bool("Out2")

    with Program():
        with Rung(enable):
            time_drum(
                outputs=[out1, out2],
                presets=[100, 100],
                pattern=[[1, 0], [0, 1]],
                current_step=step,
                accumulator=acc,
                completion_flag=done,
            ).reset(reset).jump(jump, step=step).jog(jog)
