"""forloop body rungs, break_loop()/continue_loop(), and nested loops."""

from __future__ import annotations

from typing import Any

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    ScanContext,
    SystemState,
    TagType,
    branch,
    break_loop,
    calc,
    call,
    comment,
    continue_loop,
    count_up,
    forloop,
    latch,
    out,
    program,
    subroutine,
)
from pyrung.core.executor import ConditionViewCapture, execute_program
from pyrung.core.instruction import (
    BreakInstruction,
    ContinueInstruction,
    CountUpInstruction,
    ForLoopInstruction,
)

Enable = Bool("Enable")
A = Int("A")
B = Int("B")
C = Int("C")
N = Int("N")
CtrA = Counter.clone("CtrA")
CtrB = Counter.clone("CtrB")
Rst = Bool("Rst")


def _loop(logic: Program, rung_index: int = 0) -> ForLoopInstruction:
    loop = logic.rungs[rung_index]._instructions[0]
    assert isinstance(loop, ForLoopInstruction)
    return loop


def _paths(logic: Program, patch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """One scan through every interpreted execution path."""
    results: dict[str, dict[str, Any]] = {}

    plc = PLC(logic)
    plc.patch(patch)
    plc.step()
    results["natural"] = dict(plc.current_state.tags)

    debug = PLC(logic)
    debug.patch(patch)
    for _ in debug.debug.scan_steps_debug():
        pass
    results["debugger"] = dict(debug.current_state.tags)

    state = SystemState().with_tags(patch)
    ctx = ScanContext(state)
    ctx.set_memory("_dt", 0.0)
    logic._evaluate(ctx)
    results["rung.evaluate"] = dict(ctx.commit(dt=0.0).tags)

    ctx = ScanContext(state)
    ctx.set_memory("_dt", 0.0)
    execute_program(logic, ctx, observer=ConditionViewCapture())
    results["observed"] = dict(ctx.commit(dt=0.0).tags)
    return results


def _assert_paths_agree(logic: Program, patch: dict[str, Any], **expected: Any) -> None:
    for path, tags in _paths(logic, patch).items():
        got = {name: tags.get(name) for name in expected}
        assert got == expected, path


def test_break_and_continue_semantics(runner_factory):
    with Program() as logic:
        with Rung(Enable):
            with forloop(5) as loop:
                calc(A + 1, A)
                with Rung(loop.idx == 2):
                    continue_loop()
                calc(B + 1, B)
                with Rung(loop.idx == 3):
                    break_loop()
                calc(C + 1, C)

    runner = runner_factory(logic)
    runner.patch({"Enable": True})
    runner.step()
    tags = runner.current_state.tags
    # Iterations 0..3 run (break at 3); continue skips the rest of iteration 2.
    assert (tags["A"], tags["B"], tags["C"]) == (4, 3, 2)
    _assert_paths_agree(logic, {"Enable": True}, A=4, B=3, C=2)


def test_continue_on_last_iteration_and_break_in_inner_loop_only(runner_factory):
    with Program() as logic:
        with Rung(Enable):
            with forloop(3) as outer:
                with forloop(4) as inner:
                    calc(N + 1, N)
                    with Rung(inner.idx == 1):
                        break_loop()
                calc(A + 1, A)
                with Rung(outer.idx == 2):
                    continue_loop()
                calc(B + 1, B)

    runner = runner_factory(logic)
    runner.patch({"Enable": True})
    runner.step()
    tags = runner.current_state.tags
    assert (tags["N"], tags["A"], tags["B"]) == (6, 3, 2)
    assert outer.idx.name == "_forloop_idx"
    assert inner.idx.name == "_forloop_idx2"
    _assert_paths_agree(logic, {"Enable": True}, N=6, A=3, B=2)


def test_body_rung_contacts_are_evaluated_each_iteration(runner_factory):
    flag = Bool("Flag")
    hits = Int("Hits")

    with Program() as logic:
        with Rung(Enable):
            with forloop(4) as loop:
                with Rung(flag):
                    calc(hits + 1, hits)
                with Rung(loop.idx == 1):
                    latch(flag)

    runner = runner_factory(logic)
    runner.patch({"Enable": True})
    runner.step()
    # Flag is latched in iteration 1, so iterations 2 and 3 count.
    assert runner.current_state.tags["Hits"] == 2
    _assert_paths_agree(logic, {"Enable": True}, Hits=2, Flag=True)


def test_body_rung_contact_driven_by_same_iteration_write(runner_factory):
    trip = Bool("Trip")

    with Program() as logic:
        with Rung(Enable):
            with forloop(5):
                calc(A + 1, A)
                with Rung(A == 3):
                    out(trip)
                with Rung(trip):
                    break_loop()

    runner = runner_factory(logic)
    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["A"] == 3
    _assert_paths_agree(logic, {"Enable": True}, A=3, Trip=True)


def test_loop_rung_false_turns_body_rung_outs_off(runner_factory):
    light = Bool("Light")
    gate = Bool("Gate")

    with Program() as logic:
        with Rung(Enable):
            with forloop(2):
                with Rung(gate):
                    out(light)

    runner = runner_factory(logic)
    runner.patch({"Enable": True, "Gate": True})
    runner.step()
    assert runner.current_state.tags["Light"] is True
    runner.patch({"Enable": False})
    runner.step()
    assert runner.current_state.tags["Light"] is False


def test_body_order_is_source_order():
    with Program() as logic:
        with Rung(Enable):
            with forloop(2) as loop:
                calc(A + 1, A)
                with Rung(loop.idx == 0):
                    calc(B + 1, B)
                calc(C + 1, C)

    loop = _loop(logic)
    kinds = [type(item).__name__ for item in loop.body]
    assert kinds == ["CalcInstruction", "Rung", "CalcInstruction"]
    # The flat view keeps every body instruction in source order.
    assert [str(i.dest.name) for i in loop.instructions] == ["A", "B", "C"]
    assert len(loop.body_rungs) == 1


def test_body_instructions_capture_the_loop_and_body_rung_conditions():
    gate = Bool("Gate")
    with Program() as logic:
        with Rung(Enable):
            with forloop(2):
                count_up(CtrA, 10).reset(Rst)
                with Rung(gate):
                    count_up(CtrB, 10).reset(Rst)

    loop = _loop(logic)
    bare_ctu, body_ctu = loop.instructions
    assert isinstance(bare_ctu, CountUpInstruction)
    assert isinstance(body_ctu, CountUpInstruction)
    # Bare body instructions carry the loop rung's condition; body-rung ones
    # carry the loop rung's and their own.
    assert "Enable" in repr(bare_ctu.up_condition)
    assert "Enable" in repr(body_ctu.up_condition)
    assert "Gate" in repr(body_ctu.up_condition)
    body_rung = loop.body_rungs[0]
    local = body_rung._conditions[body_rung._branch_condition_start :]
    assert len(local) == 1


def test_nesting_limit_is_three():
    with Program(strict=False):
        with Rung():
            with forloop(2):
                with forloop(2):
                    with forloop(2) as third:
                        pass
    assert third.idx.name == "_forloop_idx3"

    with pytest.raises(RuntimeError, match="limited to 3 levels"):
        with Program(strict=False):
            with Rung():
                with forloop(2):
                    with forloop(2):
                        with forloop(2):
                            with forloop(2):
                                pass


def test_break_and_continue_need_a_conditioned_body_rung():
    with pytest.raises(RuntimeError, match="conditioned body rung"):
        with Program(strict=False):
            with Rung():
                with forloop(2):
                    break_loop()

    with pytest.raises(RuntimeError, match="at least one contact"):
        with Program(strict=False):
            with Rung():
                with forloop(2):
                    with Rung():
                        continue_loop()

    with pytest.raises(RuntimeError, match="conditioned body rung"):
        with Program(strict=False):
            with Rung(Enable):
                break_loop()


def test_rung_and_branch_placement_inside_loops():
    with pytest.raises(RuntimeError, match="directly in the loop body"):
        with Program(strict=False):
            with Rung():
                with forloop(2):
                    with Rung(Enable):
                        with Rung(Enable):
                            pass

    with pytest.raises(RuntimeError, match="not permitted inside forloop"):
        with Program(strict=False):
            with Rung():
                with forloop(2):
                    with Rung(Enable):
                        with branch(Enable):
                            pass

    with pytest.raises(RuntimeError, match="continued"):
        with Program(strict=False):
            with Rung():
                with forloop(2):
                    with Rung(Enable).continued():
                        pass


def test_body_rung_does_not_join_the_program():
    with Program() as logic:
        with Rung(Enable):
            with forloop(2):
                with Rung(Enable):
                    out(Bool("LightX"))
        with Rung():
            out(Bool("LightY"))

    assert len(logic.rungs) == 2
    assert [type(i).__name__ for i in logic.rungs[1]._instructions] == ["OutInstruction"]


def test_comment_attaches_to_body_rung_in_strict_program():
    @program
    def logic():
        with Rung(Enable):
            with forloop(2):
                comment("Skip odd slots")
                with Rung(Enable):
                    continue_loop()

    body_rung = _loop(logic).body_rungs[0]
    assert body_rung.comment == "Skip odd slots"
    assert isinstance(body_rung._instructions[0], ContinueInstruction)


def test_subroutine_registered_from_a_loop_body_keeps_its_own_rungs():
    @subroutine("work")
    def work():
        with Rung():
            calc(A + 1, A)

    with Program() as logic:
        with Rung(Enable):
            with forloop(3):
                call(work)

    assert len(_loop(logic).body) == 1
    assert len(logic.subroutines["work"]) == 1
    plc = PLC(logic)
    plc.patch({"Enable": True})
    plc.step()
    assert plc.current_state.tags["A"] == 3


def test_break_instruction_classes():
    with Program() as logic:
        with Rung():
            with forloop(2):
                with Rung(Enable):
                    break_loop()
                with Rung(Enable):
                    continue_loop()

    rungs = _loop(logic).body_rungs
    assert isinstance(rungs[0]._instructions[0], BreakInstruction)
    assert isinstance(rungs[1]._instructions[0], ContinueInstruction)


def test_oneshot_in_body_rung_rearms_when_its_contact_drops(runner_factory):
    gate = Block("Gate", TagType.INT, 0, 3)
    hits = Int("Hits")

    with Program() as logic:
        with Rung(Enable):
            with forloop(4) as loop:
                with Rung(gate[loop.idx] == 1):
                    calc(hits + 1, hits, oneshot=True)

    runner = runner_factory(logic)
    runner.patch({"Enable": True, "Gate0": 1, "Gate1": 0, "Gate2": 1, "Gate3": 1})
    runner.step()
    # Gate2 drops the body rung in iteration 1, re-arming the one-shot.
    assert runner.current_state.tags["Hits"] == 2
