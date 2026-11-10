"""Tests for forloop() DSL support."""

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    CompiledPLC,
    Counter,
    Dint,
    Int,
    Program,
    Rung,
    SystemState,
    TagType,
    Timer,
    branch,
    copy,
    count_up,
    forloop,
    on_delay,
    out,
)
from pyrung.core.instruction import control
from pyrung.core.instruction.control import for_loop_iterations
from pyrung.core.validation.walker import walk_program
from tests.conftest import evaluate_program


def test_forloop_literal_count_executes_body_n_times():
    counter = Int("Counter")

    with Program() as prog:
        with Rung():
            with forloop(5):
                copy(counter + 1, counter)

    state = evaluate_program(prog, SystemState())
    assert state.tags["Counter"] == 5


def test_forloop_count_from_tag_is_resolved_each_scan():
    count = Int("Count")
    counter = Int("Counter")

    with Program() as prog:
        with Rung():
            with forloop(count):
                copy(counter + 1, counter)

    state = SystemState().with_tags({"Count": 3, "Counter": 0})
    state = evaluate_program(prog, state)
    assert state.tags["Counter"] == 3

    state = state.with_tags({"Count": 2})
    state = evaluate_program(prog, state)
    assert state.tags["Counter"] == 5


def test_forloop_tag_count_zero_runs_no_iteration():
    count = Int("Count")
    counter = Int("Counter")

    with Program() as prog:
        with Rung():
            with forloop(count):
                copy(counter + 1, counter)

    state = SystemState().with_tags({"Count": 0, "Counter": 0})
    state = evaluate_program(prog, state)
    assert state.tags["Counter"] == 0


@pytest.mark.parametrize(
    ("loops", "count", "iterations"),
    [
        # CLICK reads an Int count as unsigned 16-bit: -1 runs 65535 iterations.
        (Int("Loops"), -1, 65535),
        (Int("Loops"), -32768, 32768),
        (Int("Loops"), 0, 0),
        # and a DD count whole: 65537 runs 65537, 65536 runs 65536 (hardware 2026-11-05),
        # and signed: -1, -65535 and -2 run none (hardware 2026-11-06 and 2026-11-10).
        (Dint("Loops"), 65537, 65537),
        (Dint("Loops"), 65536, 65536),
        (Dint("Loops"), 3, 3),
        (Dint("Loops"), 0, 0),
        (Dint("Loops"), -1, 0),
        (Dint("Loops"), -65535, 0),
        (Dint("Loops"), -2, 0),
        (Dint("Loops"), -2147483648, 0),
    ],
    ids=[
        "int-1",
        "int-32768",
        "int0",
        "dint65537",
        "dint65536",
        "dint3",
        "dint0",
        "dint-1",
        "dint-65535",
        "dint-2",
        "dint-min",
    ],
)
def test_forloop_count_is_read_by_its_width(loops, count, iterations, runner_factory):
    counter = Dint("Counter")

    with Program() as prog:
        with Rung():
            with forloop(loops):
                copy(counter + 1, counter)

    runner = runner_factory(prog)
    runner.patch({"Loops": count, "Counter": 0})
    runner.step()
    assert runner.current_state.tags["Counter"] == iterations


def test_forloop_dint_element_count_is_read_whole(runner_factory):
    counts = Block("Counts", TagType.DINT, 1, 2)
    pointer = Int("Pointer")
    counter = Dint("Counter")

    with Program() as prog:
        with Rung():
            with forloop(counts[pointer]):
                copy(counter + 1, counter)

    runner = runner_factory(prog)
    runner.patch({"Counts2": 65537, "Pointer": 2, "Counter": 0})
    runner.step()
    assert runner.current_state.tags["Counter"] == 65537
    runner.patch({"Counts1": -1, "Pointer": 1, "Counter": 0})
    runner.step()
    assert runner.current_state.tags["Counter"] == 0


def test_for_count_mask_follows_the_count_width():
    words = Block("Words", TagType.INT, 1, 2)
    longs = Block("Longs", TagType.DINT, 1, 2)
    pointer = Int("Pointer")
    assert control.for_count_mask(Int("A")) == 0xFFFF
    assert control.for_count_mask(Dint("B")) == 0xFFFFFFFF
    assert control.for_count_mask(words[pointer]) == 0xFFFF
    assert control.for_count_mask(longs[pointer]) == 0xFFFFFFFF
    assert control.for_count_mask(7) == 0xFFFFFFFF
    assert control.for_count_mask(7.0) == 0xFFFF


def test_forloop_constant_count_is_read_whole(runner_factory):
    """A constant 70000 runs 70000 (hardware 2026-11-10), not 4464, its low 16 bits."""
    counter = Dint("Counter")

    with Program() as prog:
        with Rung():
            with forloop(70000):
                copy(counter + 1, counter)

    runner = runner_factory(prog)
    runner.step()
    assert runner.current_state.tags["Counter"] == 70000


def test_for_zero_count_switch(monkeypatch):
    assert for_loop_iterations(0) == 0
    assert for_loop_iterations(-1) == 65535
    assert for_loop_iterations(65536) == 0
    assert for_loop_iterations(65536, control.FOR_COUNT_MASK_32) == 65536
    # A Dint is read signed: -1 runs none (hardware 2026-11-06).
    assert for_loop_iterations(-1, control.FOR_COUNT_MASK_32) == 0
    assert for_loop_iterations(-(2**31), control.FOR_COUNT_MASK_32) == 0
    assert for_loop_iterations(2**31 - 1, control.FOR_COUNT_MASK_32) == 2**31 - 1
    assert for_loop_iterations(7) == 7
    monkeypatch.setattr(control, "FOR_ZERO_COUNT_ITERATIONS", 65536)
    assert for_loop_iterations(0) == 65536
    assert for_loop_iterations(65536) == 65536
    assert for_loop_iterations(0, control.FOR_COUNT_MASK_32) == 65536
    assert for_loop_iterations(-1, control.FOR_COUNT_MASK_32) == 0
    assert for_loop_iterations(3) == 3


def test_forloop_idx_supports_indirect_addressing():
    src = Block("Src", TagType.INT, 1, 10)
    dst = Block("Dst", TagType.INT, 1, 10)

    with Program() as prog:
        with Rung():
            with forloop(3) as loop:
                copy(src[loop.idx + 1], dst[loop.idx + 1])

    state = SystemState().with_tags(
        {
            "Src1": 11,
            "Src2": 22,
            "Src3": 33,
            "Dst1": 0,
            "Dst2": 0,
            "Dst3": 0,
        }
    )
    state = evaluate_program(prog, state)

    assert state.tags["Dst1"] == 11
    assert state.tags["Dst2"] == 22
    assert state.tags["Dst3"] == 33


@pytest.mark.parametrize("count", [0, -5, 2**31])
def test_forloop_literal_count_outside_signed_32_bit_raises(count):
    with pytest.raises(ValueError, match=r"forloop count must be 1\.\.2147483647"):
        forloop(count)


def test_branch_inside_forloop_is_rejected():
    with pytest.raises(RuntimeError, match="branch\\(\\) is not permitted inside forloop\\(\\)"):
        with Program(strict=False):
            with Rung():
                with forloop(2):
                    with branch():
                        pass


def test_forloop_rung_false_skips_body(runner_factory):
    # CLICK: a false For rung skips its body.  Its coils keep their values and
    # its one-shots do not re-arm.
    enable = Bool("Enable")
    light = Bool("Light")
    counter = Int("Counter")

    with Program() as logic:
        with Rung(enable):
            with forloop(2):
                out(light)
                copy(counter + 1, counter, oneshot=True)

    runner = runner_factory(logic)
    runner.patch({"Enable": True, "Light": False, "Counter": 0})

    runner.step()
    assert runner.current_state.tags["Light"] is True
    assert runner.current_state.tags["Counter"] == 1

    runner.step()
    assert runner.current_state.tags["Light"] is True
    assert runner.current_state.tags["Counter"] == 1

    runner.patch({"Enable": False})
    runner.step()
    assert runner.current_state.tags["Light"] is True
    assert runner.current_state.tags["Counter"] == 1

    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Light"] is True
    assert runner.current_state.tags["Counter"] == 1


def test_forloop_own_oneshot_rearms_when_rung_false(runner_factory):
    enable = Bool("Enable")
    counter = Int("Counter")

    with Program() as logic:
        with Rung(enable):
            with forloop(2, oneshot=True):
                copy(counter + 1, counter)

    runner = runner_factory(logic)
    runner.patch({"Enable": True, "Counter": 0})
    runner.step()
    runner.step()
    assert runner.current_state.tags["Counter"] == 2

    runner.patch({"Enable": False})
    runner.step()
    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Counter"] == 4


def test_forloop_rung_false_holds_body_timer_and_counter(runner_factory):
    # The assumption conformance probe F19 checks on a CLICK: a false For rung
    # skips its body, so a TON there keeps its Acc (no unpowered scan resets
    # it) and a counter keeps its Acc with its reset input on (not scanned).
    enable = Bool("Enable")
    rst = Bool("Rst")
    timer = Timer.clone("T")
    counter = Counter.clone("C")

    with Program() as logic:
        with Rung(enable):
            with forloop(1):
                on_delay(timer, 30000)
                count_up(counter, 100).reset(rst)

    runner = runner_factory(logic)
    runner.patch({"Enable": True, "Rst": False})
    for _ in range(3):
        runner.step()
    timer_acc = runner.current_state.tags[timer.Acc.name]
    counter_acc = runner.current_state.tags[counter.Acc.name]
    assert timer_acc > 0
    assert counter_acc > 0

    runner.patch({"Enable": False, "Rst": True})
    for _ in range(3):
        runner.step()
    assert runner.current_state.tags[timer.Acc.name] == timer_acc
    assert runner.current_state.tags[counter.Acc.name] == counter_acc


def test_oneshot_forloop_state_is_memory_backed_for_parity():
    enable = Bool("Enable", external=True)
    target = Bool("Target")
    count = Int("Count")

    with Program(strict=False) as logic:
        with Rung(enable):
            with forloop(count, oneshot=True):
                out(target)

    interpreted = PLC(logic, dt=0.010)
    compiled = CompiledPLC(logic, dt=0.010)

    interpreted.patch({"Enable": False, "Count": 0})
    compiled.patch({"Enable": False, "Count": 0})
    interpreted.step()
    compiled.step()

    assert dict(interpreted.current_state.tags) == dict(compiled.current_state.tags)
    assert dict(interpreted.current_state.memory) == dict(compiled.current_state.memory)


def test_multiple_forloops_in_different_rungs_are_independent():
    a = Int("A")
    b = Int("B")

    with Program() as prog:
        with Rung():
            with forloop(2):
                copy(a + 1, a)

        with Rung():
            with forloop(3):
                copy(b + 1, b)

    state = SystemState().with_tags({"A": 0, "B": 0})
    state = evaluate_program(prog, state)

    assert state.tags["A"] == 2
    assert state.tags["B"] == 3


def test_walker_recurses_into_forloop_children():
    target = Int("Target")

    with Program() as prog:
        with Rung():
            with forloop(2):
                copy(1, target)

    facts = walk_program(prog)

    count_facts = [f for f in facts.operands if f.location.arg_path == "instruction.count"]
    assert count_facts

    child_target_facts = [
        f
        for f in facts.operands
        if f.location.arg_path == "instruction.dest" and f.metadata.get("tag_name") == "Target"
    ]
    assert child_target_facts
