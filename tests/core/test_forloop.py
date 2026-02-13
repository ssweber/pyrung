"""Tests for forloop() DSL support."""

import pytest

from pyrung.core import (
    Block,
    Bool,
    Int,
    PLCRunner,
    Program,
    Rung,
    SystemState,
    TagType,
    branch,
    copy,
    forloop,
    out,
)
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
def test_forloop_zero_or_negative_count_skips_body(count):
    target = Int("Target")

    with Program() as prog:
        with Rung():
            with forloop(count):
                copy(99, target)

    state = SystemState().with_tags({"Target": 0})
    state = evaluate_program(prog, state)
    assert state.tags["Target"] == 0


def test_nested_forloop_raises_runtime_error():
    with pytest.raises(RuntimeError, match="Nested forloop is not permitted"):
        with Program(strict=False):
            with Rung():
                with forloop(2):
                    with forloop(1):
                        pass


def test_branch_inside_forloop_is_rejected():
    with pytest.raises(RuntimeError, match="branch\\(\\) is not permitted inside forloop\\(\\)"):
        with Program(strict=False):
            with Rung():
                with forloop(2):
                    with branch():
                        pass


def test_forloop_rung_false_resets_coils_and_child_oneshot():
    enable = Bool("Enable")
    light = Bool("Light")
    counter = Int("Counter")

    with Program() as logic:
        with Rung(enable):
            with forloop(2):
                out(light)
                copy(counter + 1, counter, oneshot=True)

    runner = PLCRunner(logic)
    runner.patch({"Enable": True, "Light": False, "Counter": 0})

    runner.step()
    assert runner.current_state.tags["Light"] is True
    assert runner.current_state.tags["Counter"] == 1

    runner.step()
    assert runner.current_state.tags["Light"] is True
    assert runner.current_state.tags["Counter"] == 1

    runner.patch({"Enable": False})
    runner.step()
    assert runner.current_state.tags["Light"] is False
    assert runner.current_state.tags["Counter"] == 1

    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Light"] is True
    assert runner.current_state.tags["Counter"] == 2


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
        if f.location.arg_path == "instruction.target" and f.metadata.get("tag_name") == "Target"
    ]
    assert child_target_facts
