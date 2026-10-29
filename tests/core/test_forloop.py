"""Tests for forloop() DSL support."""

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    CompiledPLC,
    Dint,
    Int,
    Program,
    Rung,
    SystemState,
    TagType,
    branch,
    copy,
    forloop,
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
    ("count", "iterations"),
    [(-65535, 1), (-65534, 2), (65539, 3), (-65536, 0)],
)
def test_forloop_count_is_read_as_unsigned_16_bit(count, iterations, runner_factory):
    # CLICK reads the count as unsigned 16-bit: -1 runs 65535 iterations.
    loops = Dint("Loops")
    counter = Int("Counter")

    with Program() as prog:
        with Rung():
            with forloop(loops):
                copy(counter + 1, counter)

    runner = runner_factory(prog)
    runner.patch({"Loops": count, "Counter": 0})
    runner.step()
    assert runner.current_state.tags["Counter"] == iterations


def test_for_zero_count_switch(monkeypatch):
    assert for_loop_iterations(0) == 0
    assert for_loop_iterations(-1) == 65535
    assert for_loop_iterations(7) == 7
    monkeypatch.setattr(control, "FOR_ZERO_COUNT_ITERATIONS", 65536)
    assert for_loop_iterations(0) == 65536
    assert for_loop_iterations(65536) == 65536
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


@pytest.mark.parametrize("count", [0, -5])
def test_forloop_zero_or_negative_literal_count_raises(count):
    with pytest.raises(ValueError, match="forloop count must be >= 1"):
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
