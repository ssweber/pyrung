import pytest

from pyrung.core import (
    Block,
    Bool,
    Dint,
    Int,
    Program,
    Rung,
    TagType,
    branch,
    count_down,
    count_up,
    off_delay,
    on_delay,
    out,
    shift,
)


def test_count_up_missing_reset_raises() -> None:
    enable = Bool("Enable")
    done = Bool("ct.Done")
    acc = Dint("ctd.Acc")

    with pytest.raises(RuntimeError, match="count_up"):
        with Program():
            with Rung(enable):
                count_up(done, acc, preset=5)


def test_count_down_missing_reset_raises() -> None:
    enable = Bool("Enable")
    done = Bool("ct.Done")
    acc = Dint("ctd.Acc")

    with pytest.raises(RuntimeError, match="count_down"):
        with Program():
            with Rung(enable):
                count_down(done, acc, preset=5)


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
    done = Bool("ct.Done")
    acc = Dint("ctd.Acc")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="count_up"):
        with Program():
            with Rung(enable):
                count_up(done, acc, preset=5)
                out(light)


def test_pending_required_builder_blocks_branch_entry() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    done = Bool("ct.Done")
    acc = Dint("ctd.Acc")
    reset = Bool("Reset")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="count_up"):
        with Program(strict=False):
            with Rung(enable):
                builder = count_up(done, acc, preset=5)
                with branch(mode):
                    out(light)
                builder.reset(reset)


def test_parent_flow_terminal_blocks_following_instruction() -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    done = Bool("ct.Done")
    acc = Dint("ctd.Acc")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="terminal"):
        with Program():
            with Rung(enable):
                count_up(done, acc, preset=5).reset(reset)
                out(light)


def test_parent_flow_terminal_blocks_following_branch() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    reset = Bool("Reset")
    done = Bool("ct.Done")
    acc = Dint("ctd.Acc")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="terminal"):
        with Program():
            with Rung(enable):
                count_down(done, acc, preset=5).reset(reset)
                with branch(mode):
                    out(light)


def test_branch_flow_terminal_blocks_following_branch_local_instruction() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    reset = Bool("Reset")
    done = Bool("ct.Done")
    acc = Dint("ctd.Acc")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="terminal"):
        with Program():
            with Rung(enable):
                with branch(mode):
                    count_up(done, acc, preset=5).reset(reset)
                    out(light)


def test_terminal_inside_branch_does_not_block_sibling_branch() -> None:
    enable = Bool("Enable")
    mode_a = Bool("ModeA")
    mode_b = Bool("ModeB")
    reset = Bool("Reset")
    done = Bool("ct.Done")
    acc = Dint("ctd.Acc")
    light = Bool("Light")

    with Program():
        with Rung(enable):
            with branch(mode_a):
                count_up(done, acc, preset=5).reset(reset)
            with branch(mode_b):
                out(light)


def test_ton_allows_following_instruction_and_branch() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    done = Bool("t.Done")
    acc = Int("td.Acc")
    light = Bool("Light")
    branch_light = Bool("BranchLight")

    with Program():
        with Rung(enable):
            on_delay(done, acc, preset=5)
            out(light)
            with branch(mode):
                out(branch_light)


def test_tof_allows_following_instruction_and_branch() -> None:
    enable = Bool("Enable")
    mode = Bool("Mode")
    done = Bool("t.Done")
    acc = Int("td.Acc")
    light = Bool("Light")
    branch_light = Bool("BranchLight")

    with Program():
        with Rung(enable):
            off_delay(done, acc, preset=5)
            out(light)
            with branch(mode):
                out(branch_light)


def test_rton_is_terminal_in_same_flow() -> None:
    enable = Bool("Enable")
    reset = Bool("Reset")
    done = Bool("t.Done")
    acc = Int("td.Acc")
    light = Bool("Light")

    with pytest.raises(RuntimeError, match="terminal"):
        with Program():
            with Rung(enable):
                on_delay(done, acc, preset=5).reset(reset)
                out(light)
